"""Plan section 3.9: the step order, and what crosses the wire because of it.

Before this, ``_run_step`` built the envelope first and opened the step third,
so an envelope could not describe a fetch that had not happened yet. These
tests pin the order and the two consequences of changing it: the count line
reaching the agent, and an over-long envelope closing a step that is now open.
"""

from __future__ import annotations

import asyncio
import io
from typing import Any

from agent_control_dispatcher import dispatch as dispatch_module
from agent_control_dispatcher.dispatch import ChainStep, DispatchOptions, TaskResult
from agent_control_dispatcher.ledger import Claim
from agent_control_dispatcher.sources.base import SourceItem
from agent_control_models.attachments import (
    AttachmentOrigin,
    StepAttachmentSummary,
    StepFilesSummary,
    TurnAttachmentVerdict,
)
from conftest import StubClient

DELIVERED_KEY = "0123456789abcdef" * 2


def _summary() -> StepFilesSummary:
    return StepFilesSummary(
        found=3,
        delivered=1,
        files=[
            StepAttachmentSummary(
                display_name="EarlyCore_MSSP_Deck.pdf",
                sha256="b" * 64,
                size_bytes=1_414_578,
                sniffed_mime="application/pdf",
                origin=AttachmentOrigin.LINEAR,
                verdict=TurnAttachmentVerdict.PENDING,
                attachment_key=DELIVERED_KEY,
                text_ready=True,
            ),
            StepAttachmentSummary(
                display_name="notes.docx",
                origin=AttachmentOrigin.LINEAR,
                verdict=TurnAttachmentVerdict.BLOCKED,
                failure_code="unsupported_type",
            ),
        ],
    )


class RecordingLedger:
    """A :class:`TaskLedger` that records the order it was called in."""

    def __init__(self, files: StepFilesSummary | None) -> None:
        self.files = files
        self.calls: list[str] = []
        self.finished: list[dict[str, Any]] = []

    def session_task_key(self, *, source_kind: str, ref: str) -> str | None:
        return "k" * 32

    async def record_session(self, **_: Any) -> StepFilesSummary | None:
        self.calls.append("record_session")
        return self.files

    async def prior_report(self, **_: Any) -> None:
        return None

    async def complete_step(self, **_: Any) -> None:
        self.calls.append("complete_step")

    async def finish(self, **kwargs: Any) -> None:
        self.calls.append("finish")
        self.finished.append(kwargs)

    async def get(self, **_: Any) -> Claim | None:
        return None

    async def register(self, **_: Any) -> None:
        return None

    async def claim(self, **_: Any) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _run(ledger: RecordingLedger, *, brief: str = "Review the deck.") -> TaskResult:
    stream = io.StringIO()
    client = StubClient()
    result = asyncio.run(
        dispatch_module._run_step(
            client=client,
            ledger=ledger,
            source_kind="linear",
            item=SourceItem(ref="OPS-2", title="Review the deck", body="See attached."),
            step=ChainStep(index=0, agent_name="reviewer", brief=brief),
            is_last=True,
            options=DispatchOptions(
                source_spec="linear://team/t",
                agent_name="reviewer",
                base_url="http://localhost:8000",
                api_key="k",
                max_tasks=1,
            ),
            stream=stream,
            opened_sessions=[],
        )
    )
    _run.client = client  # type: ignore[attr-defined]
    _run.text = stream.getvalue()  # type: ignore[attr-defined]
    return result


def test_the_session_and_the_step_come_before_the_envelope_is_built() -> None:
    """The whole reordering, stated as one assertion: the turn's message
    contains a count that only the step call could have produced."""
    ledger = RecordingLedger(_summary())
    _run(ledger)
    client: StubClient = _run.client  # type: ignore[attr-defined]

    assert ledger.calls[0] == "record_session"
    assert "1 of 3 files on this issue were delivered" in client.turns[0]


def test_only_the_delivered_keys_reach_the_turn() -> None:
    """A refused file has no key, and the dispatcher has no URL to substitute."""
    _run(RecordingLedger(_summary()))
    client: StubClient = _run.client  # type: ignore[attr-defined]

    assert client.turn_attachment_keys[0] == [DELIVERED_KEY]


def test_a_step_that_found_nothing_still_says_so() -> None:
    _run(RecordingLedger(StepFilesSummary(found=0, delivered=0, files=[])))
    client: StubClient = _run.client  # type: ignore[attr-defined]

    assert "No files are attached to this issue." in client.turns[0]


def test_a_ledger_that_did_not_look_leaves_the_envelope_unchanged() -> None:
    _run(RecordingLedger(None))
    client: StubClient = _run.client  # type: ignore[attr-defined]

    assert "Files attached to this task" not in client.turns[0]
    assert client.turn_attachment_keys[0] == []


def test_an_over_long_envelope_now_closes_the_step_it_opened() -> None:
    """Before the reorder there was no open step here. Leaving it as it was
    would strand a row at ``running`` until a reclaim swept it."""
    ledger = RecordingLedger(_summary())
    result = _run(ledger, brief="x" * 20_000)

    assert result.outcome_code == dispatch_module.ENVELOPE_TOO_LONG
    assert ledger.calls == ["record_session", "finish"]
    assert ledger.finished[0]["step_index"] == 0
    assert result.session_key == "session-0"


def test_a_stored_file_whose_text_is_missing_is_still_named_on_the_turn() -> None:
    """Its key goes, because the server's own delivery section names the file
    and states that its contents were not included. Withholding the key would
    leave the count line as the only mention of a file the server holds, and an
    operator reading the transcript later could not tell it had been fetched.
    """
    files = StepFilesSummary(
        found=1,
        delivered=0,
        files=[
            StepAttachmentSummary(
                display_name="deck.pdf",
                sha256="c" * 64,
                size_bytes=2048,
                sniffed_mime="application/pdf",
                origin=AttachmentOrigin.LINEAR,
                verdict=TurnAttachmentVerdict.PENDING,
                attachment_key=DELIVERED_KEY,
                failure_code="not_converted",
                text_ready=False,
            )
        ],
    )
    _run(RecordingLedger(files))
    client: StubClient = _run.client  # type: ignore[attr-defined]

    assert client.turn_attachment_keys[0] == [DELIVERED_KEY]
    assert "0 of 1 files on this issue were delivered" in client.turns[0]
    assert "has not been read yet" in client.turns[0]


def test_a_tracker_that_could_not_be_listed_reaches_the_agent_as_a_warning() -> None:
    """Not "no files are attached". The dispatcher renders whichever of the
    three states the server reported, and this one exists so a Linear outage
    cannot produce a confident wrong sentence."""
    _run(RecordingLedger(StepFilesSummary(found=0, delivered=0, files=[], read_failed=True)))
    client: StubClient = _run.client  # type: ignore[attr-defined]

    assert "No files are attached to this issue." not in client.turns[0]
    assert "could not be listed" in client.turns[0]
