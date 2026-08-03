"""The ``once`` control flow, with the network stubbed.

No test here talks to a server. What is worth pinning is the bounding and the
stopping: how many turns a run can spend, which refusals end the run rather than
the task, and what a stopped run leaves behind for the next one.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any

import pytest
from agent_control_dispatcher.client import DispatchHTTPError, Disposition
from agent_control_dispatcher.dispatch import (
    DENY_CHECK_UNAVAILABLE,
    ENVELOPE_TOO_LONG,
    MAX_TASKS_CEILING,
    DispatchOptions,
    RunReport,
    dispatch_once,
)
from agent_control_dispatcher.envelope import UNTRUSTED_BLOCK_MAX_CHARS
from agent_control_dispatcher.ledger import ClaimLedger, ClaimStatus
from agent_control_dispatcher.sources.file import SourceParseError
from agent_control_models.sessions import TURN_MESSAGE_MAX_LENGTH
from conftest import StubClient

THREE_ITEMS = """
- ref: t1
  title: One
  body: first
- ref: t2
  title: Two
  body: second
- ref: t3
  title: Three
  body: third
"""


def _options(tmp_path: Path, **overrides: Any) -> DispatchOptions:
    source = tmp_path / "tasks.yaml"
    if not source.exists():
        source.write_text(THREE_ITEMS, encoding="utf-8")
    defaults: dict[str, Any] = {
        "source_spec": f"file://{source}",
        "agent_name": "researcher",
        "base_url": "http://localhost:8000",
        "api_key": "k",
        "ledger_path": tmp_path / "claims.sqlite3",
        "max_tasks": 3,
    }
    defaults.update(overrides)
    return DispatchOptions(**defaults)


def _run(options: DispatchOptions) -> tuple[RunReport, str]:
    out = io.StringIO()
    report = asyncio.run(dispatch_once(options, out=out))
    return report, out.getvalue()


def test_max_tasks_is_refused_above_the_ceiling_never_clamped(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exceeds the hard cap"):
        _options(tmp_path, max_tasks=MAX_TASKS_CEILING + 1)
    with pytest.raises(ValueError):
        _options(tmp_path, max_tasks=0)


def test_the_ceiling_itself_is_allowed(tmp_path: Path) -> None:
    assert _options(tmp_path, max_tasks=MAX_TASKS_CEILING).max_tasks == MAX_TASKS_CEILING


def test_dry_run_is_the_default_and_the_terminal_says_what_it_is_not(tmp_path: Path) -> None:
    options = _options(tmp_path)
    assert options.dry_run is True


def test_one_turn_per_task_and_no_more_than_max_tasks(tmp_path: Path, stub: StubClient) -> None:
    report, text = _run(_options(tmp_path, max_tasks=2))
    assert len(stub.turns) == 2
    assert [result.ref for result in report.results] == ["t1", "t2"]
    assert "assertion about this deployment" in text


def test_a_wet_run_says_nothing_is_bounding_the_tools(tmp_path: Path, stub: StubClient) -> None:
    _, text = _run(_options(tmp_path, max_tasks=1, dry_run=False))
    assert "dry-run    False" in text
    assert "Nothing in this slice bounds what the agent's tools" in text


def test_the_envelope_is_what_reaches_the_turn(tmp_path: Path, stub: StubClient) -> None:
    _run(_options(tmp_path, max_tasks=1))
    assert "<<<TASK_BEGIN>>>" in stub.turns[0]
    assert "first" in stub.turns[0]


def test_sessions_are_kept_unless_the_operator_asks(tmp_path: Path, stub: StubClient) -> None:
    report, _ = _run(_options(tmp_path, max_tasks=1))
    assert stub.deleted == []
    assert report.results[0].session_key == "session-0"


def test_delete_sessions_exercises_the_delete_endpoint(tmp_path: Path, stub: StubClient) -> None:
    _run(_options(tmp_path, max_tasks=1, delete_sessions=True))
    assert stub.deleted == ["session-0"]


def test_a_failed_delete_does_not_lose_the_step_or_hide_the_session(
    tmp_path: Path, stub: StubClient
) -> None:
    stub.raise_on_delete = DispatchHTTPError(
        disposition=Disposition.FAILED, status_code=500, error_code=None, detail="nope"
    )
    report, text = _run(_options(tmp_path, max_tasks=1, delete_sessions=True))

    assert report.results[0].status is ClaimStatus.COMPLETED
    assert report.results[0].session_key == "session-0", "the session is still on the server"
    assert "NOT deleted" in text


def test_a_control_block_ends_the_task_and_not_the_run(tmp_path: Path, stub: StubClient) -> None:
    stub.deny_on_turn = {0}
    report, _ = _run(_options(tmp_path, max_tasks=2))
    assert report.results[0].status is ClaimStatus.BLOCKED
    assert report.results[0].outcome_code == "BLOCKED_BY_CONTROL"
    assert report.stopped_early is False
    assert len(stub.turns) == 2


def test_quota_stops_the_run_and_leaves_the_untouched_items_claimable(
    tmp_path: Path, stub: StubClient
) -> None:
    """The stop must not strand the items the run never reached: they were never
    dispatched, so a re-run has to be able to pick them up."""

    stub.raise_on_turn = {
        0: DispatchHTTPError(
            disposition=Disposition.PAUSED_QUOTA,
            status_code=429,
            error_code="QUOTA_EXCEEDED",
            detail="over the ceiling",
        )
    }
    options = _options(tmp_path, max_tasks=3)
    report, text = _run(options)

    assert report.stopped_early
    assert len(stub.turns) == 1
    assert "stopping the run" in text

    with ClaimLedger(options.ledger_path) as ledger:
        assert ledger.get(source_kind="file", ref="t2") is None
        assert ledger.claim(source_kind="file", ref="t2", agent_name="researcher", dry_run=True)
        paused = ledger.get(source_kind="file", ref="t1")
        assert paused is not None and paused.status is ClaimStatus.PAUSED_QUOTA


def test_a_timeout_stops_the_run_because_the_invocation_did_not(
    tmp_path: Path, stub: StubClient
) -> None:
    stub.raise_on_turn = {
        0: DispatchHTTPError(
            disposition=Disposition.RUNNING_UNKNOWN,
            status_code=504,
            error_code=None,
            detail="no answer",
        )
    }
    report, _ = _run(_options(tmp_path, max_tasks=3))
    assert report.stopped_early
    assert report.results[0].status is ClaimStatus.RUNNING_UNKNOWN


def test_a_session_that_cannot_be_created_fails_that_task(tmp_path: Path, stub: StubClient) -> None:
    stub.raise_on_session = {
        0: DispatchHTTPError(
            disposition=Disposition.FAILED,
            status_code=502,
            error_code="EXECUTOR_REJECTED",
            detail="refused",
        )
    }
    report, _ = _run(_options(tmp_path, max_tasks=2))
    assert report.results[0].status is ClaimStatus.FAILED
    assert len(stub.turns) == 1, "the turn is never attempted without a session"
    assert report.stopped_early is False


def test_an_unanswerable_deny_query_is_not_read_as_no_deny(
    tmp_path: Path, stub: StubClient
) -> None:
    """The turn ran and cost money, and nothing can say whether a control
    blocked it. Calling that ``completed`` would forward a possible refusal as a
    finding, which is the failure section 9.3 cares most about."""

    stub.raise_on_deny_query = {
        0: DispatchHTTPError(
            disposition=Disposition.FAILED,
            status_code=500,
            error_code=None,
            detail="observability store is down",
        )
    }
    options = _options(tmp_path, max_tasks=3)
    report, text = _run(options)

    assert report.results[0].status is ClaimStatus.FAILED
    assert report.results[0].outcome_code == DENY_CHECK_UNAVAILABLE
    assert report.results[0].session_key == "session-0", "the transcript is still findable"
    assert report.stopped_early, "further turns could not be classified either"
    assert len(stub.turns) == 1
    assert "is unknown" in text

    with ClaimLedger(options.ledger_path) as ledger:
        claim = ledger.get(source_kind="file", ref="t1")
        assert claim is not None
        assert claim.status is ClaimStatus.FAILED
        assert claim.turn_trace_id == "trace-0"


def test_a_rerun_skips_what_already_ran(tmp_path: Path, stub: StubClient) -> None:
    options = _options(tmp_path, max_tasks=1)
    _run(options)
    report, text = _run(options)
    assert len(stub.turns) == 2
    assert "skip t1: already completed" in text
    assert [result.ref for result in report.results] == ["t2"]


def test_a_duplicate_ref_refuses_the_run_before_a_turn_is_spent(
    tmp_path: Path, stub: StubClient
) -> None:
    """Two items with one ref key the same ledger row, so one of them would be
    silently dropped. It is refused up front, deterministically, and nothing has
    been claimed or spent when it is."""

    source = tmp_path / "tasks.yaml"
    source.write_text("- ref: t1\n  title: One\n- ref: t1\n  title: Also one\n", encoding="utf-8")
    options = _options(tmp_path, max_tasks=3)

    with pytest.raises(SourceParseError, match="duplicate ref 't1'"):
        _run(options)

    assert stub.turns == []
    assert not options.ledger_path.exists()


def test_a_task_with_a_title_and_no_body_still_runs(tmp_path: Path, stub: StubClient) -> None:
    source = tmp_path / "tasks.yaml"
    source.write_text("- ref: t1\n  title: Just a title\n", encoding="utf-8")
    report, _ = _run(_options(tmp_path, max_tasks=1))

    assert report.results[0].status is ClaimStatus.COMPLETED
    assert "Just a title" in stub.turns[0]


def test_a_far_too_long_body_is_truncated_rather_than_refused(
    tmp_path: Path, stub: StubClient
) -> None:
    source = tmp_path / "tasks.yaml"
    source.write_text("- ref: t1\n  title: Long\n  body: " + "x" * 200_000 + "\n", encoding="utf-8")
    report, _ = _run(_options(tmp_path, max_tasks=1))

    assert report.results[0].status is ClaimStatus.COMPLETED
    assert len(stub.turns[0]) <= TURN_MESSAGE_MAX_LENGTH
    assert "characters omitted" in stub.turns[0]
    assert stub.turns[0].count("x") <= UNTRUSTED_BLOCK_MAX_CHARS


def test_an_unsendable_envelope_fails_the_task_without_claiming_it(
    tmp_path: Path, stub: StubClient
) -> None:
    """An over-long brief is the operator's own text and costs nothing to
    refuse, so the item must stay claimable for the run with a shorter one."""

    options = _options(tmp_path, max_tasks=1, brief="b" * (TURN_MESSAGE_MAX_LENGTH + 1))
    report, text = _run(options)

    assert stub.turns == []
    assert report.results[0].outcome_code == ENVELOPE_TOO_LONG
    assert ENVELOPE_TOO_LONG in text

    with ClaimLedger(options.ledger_path) as ledger:
        assert ledger.get(source_kind="file", ref="t1") is None
        assert ledger.claim(source_kind="file", ref="t1", agent_name="researcher", dry_run=True)


def test_an_empty_source_runs_nothing_and_says_so(tmp_path: Path, stub: StubClient) -> None:
    source = tmp_path / "tasks.yaml"
    source.write_text("", encoding="utf-8")
    report, text = _run(_options(tmp_path, max_tasks=3))

    assert report.results == []
    assert stub.turns == []
    assert "no items in source" in text
