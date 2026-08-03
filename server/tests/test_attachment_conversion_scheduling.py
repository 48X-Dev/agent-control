"""Who asks for a conversion, and what happens when two of them ask at once.

The cache and the queue have their own file. This one is about the seams
around them, each of which is invisible from a response body:

* an upload queues the work, so the text is usually there by the time somebody
  presses send - and a request that waited for it would answer the same 201
  twenty seconds later;
* two *processes* reaching the same content convert it once. The in-flight set
  is per instance and cannot see across a process boundary, so the claim in the
  database is the whole mechanism and a second scheduler is the only way to
  test it;
* scheduling from somewhere with no event loop refuses rather than raising,
  because every caller is fire-and-forget and a raise would turn a background
  optimisation into a failed request;
* shutdown gives up rather than holding the process open for a minute of OCR.

The last one is why a claim is a lease. A cancelled run drops its own claim,
and a run that never gets the chance - killed, evicted, or failed between the
claim and the store - is recovered by the claim expiring. Both are tested here,
along with the other half of that rule: a claim inside its lease is not stolen.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.config import executor_settings
from agent_control_server.endpoints import agent_attachments as attachments_endpoint
from agent_control_server.services import attachment_conversions as conversions
from agent_control_server.services.attachment_conversions import (
    STATE_RUNNING,
    ConversionScheduler,
    schedule_conversion,
)
from agent_control_server.services.attachment_converter import ConversionStatus
from agent_control_server.services.attachment_converter_cache import (
    conversion_cache_key,
)
from agent_control_server.services.attachment_quota import reset_attachment_quota

from .conftest import engine
from .test_agent_attachments_endpoints import PDF_BYTES, make_session, upload
from .test_attachment_conversions import _FakeBlobStore, _result

_NAMESPACE = "default"


@pytest.fixture(autouse=True)
def attachments_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(executor_settings, "enabled", True)
    monkeypatch.setattr(executor_settings, "attachments_enabled", True)
    reset_attachment_quota()
    yield
    reset_attachment_quota()


def _sha() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _insert_running_claim(source_sha256: str) -> None:
    """The row a worker that never came back leaves behind."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_attachment_conversions "
                "(namespace_key, cache_key, source_sha256, state) "
                "VALUES (:ns, :key, :sha, 'running')"
            ),
            {
                "ns": _NAMESPACE,
                "key": conversion_cache_key(source_sha256),
                "sha": source_sha256,
            },
        )


def _rows_for(source_sha256: str) -> list[tuple[str, str | None]]:
    with engine.begin() as conn:
        return [
            (row[0], row[1])
            for row in conn.execute(
                text(
                    "SELECT state, text_body FROM agent_attachment_conversions "
                    " WHERE namespace_key = :ns AND cache_key = :key"
                ),
                {"ns": _NAMESPACE, "key": conversion_cache_key(source_sha256)},
            )
        ]


# ---------------------------------------------------------------------------
# The upload seam
# ---------------------------------------------------------------------------


def test_an_upload_queues_the_work_and_does_not_do_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued at upload is what makes the text usually present at send.

    Asserted on the submission rather than on a stored conversion, because the
    two differ by roughly twenty seconds and only one of them is allowed to
    happen inside the request.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        attachments_endpoint,
        "schedule_conversion",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    session_key = make_session()

    created = upload(client, session_key)

    assert created.status_code == 201, created.text
    attachment = created.json()["attachment"]
    assert len(calls) == 1
    assert calls[0]["source_sha256"] == attachment["source_sha256"]
    assert calls[0]["declared_mime"] == attachment["sniffed_mime"]
    assert calls[0]["namespace_key"] == _NAMESPACE


def test_a_second_upload_of_the_same_bytes_still_asks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deduplicated upload is where "nothing here is load-bearing" is paid.

    Identical bytes return the existing key rather than a second row, and it
    would be easy to skip the scheduling with it. That would leave content
    whose first conversion was dropped - a full queue, a shutdown - with no
    second chance until somebody uploaded it into a different session. The
    scheduler's own dedupe is what makes asking twice cheap.
    """
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        attachments_endpoint,
        "schedule_conversion",
        lambda **kwargs: calls.append(kwargs) or True,
    )
    session_key = make_session()

    upload(client, session_key)
    again = upload(client, session_key, data=PDF_BYTES)

    assert again.status_code == 201, again.text
    assert again.json()["deduplicated"] is True
    assert len(calls) == 2
    assert calls[0]["source_sha256"] == calls[1]["source_sha256"]


# ---------------------------------------------------------------------------
# Two processes, one file
# ---------------------------------------------------------------------------


async def test_two_processes_racing_on_one_file_convert_it_once(
    fake_converter: Any,
) -> None:
    """The claim is the only thing standing between two workers and two OCR runs.

    Two schedulers stand in for two server processes: the in-flight set that
    deduplicates within one of them is a plain Python set and cannot see across
    a process boundary. ``INSERT ... ON CONFLICT DO NOTHING`` is what makes the
    loser return instead of converting, and a select-then-insert would pass
    every single-process test in the suite while doubling the work - and the
    memory - on the deployment that actually runs two.
    """
    calls, _ = fake_converter
    source_sha256 = _sha()
    blobs = _FakeBlobStore(b"%PDF-1.7 body")
    first = ConversionScheduler(blobs=blobs)
    second = ConversionScheduler(blobs=blobs)

    for scheduler in (first, second):
        assert scheduler.submit(
            namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256
        )
    await asyncio.gather(first.drain(timeout=5.0), second.drain(timeout=5.0))

    assert len(calls) == 1
    assert _rows_for(source_sha256) == [("done", "hello from the document")]


async def test_the_loser_of_the_race_reads_the_winners_answer(
    fake_converter: Any,
) -> None:
    """One entry serves both, which is the reason the loser may simply stop."""
    calls, outcome = fake_converter
    outcome["result"] = _result("the winner's text", ConversionStatus.OCR_EXTRACTED)
    source_sha256 = _sha()
    blobs = _FakeBlobStore(b"%PDF-1.7 body")
    winner = ConversionScheduler(blobs=blobs)
    loser = ConversionScheduler(blobs=blobs)

    assert winner.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256)
    await winner.drain(timeout=5.0)
    assert loser.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256)
    await loser.drain(timeout=5.0)

    assert len(calls) == 1
    assert _rows_for(source_sha256) == [("done", "the winner's text")]


# ---------------------------------------------------------------------------
# Refusing rather than raising
# ---------------------------------------------------------------------------


def test_scheduling_with_no_event_loop_refuses_instead_of_raising() -> None:
    """Every caller is fire-and-forget, so this must not be able to fail a request.

    ``asyncio.get_running_loop`` raises off the loop, and an uncaught raise here
    would turn "the text will be a bit late" into a 500 on an upload that had
    already stored the bytes.
    """
    assert (
        schedule_conversion(
            namespace_key=_NAMESPACE, attachment_id=1, source_sha256=_sha()
        )
        is False
    )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_shutdown_gives_up_rather_than_holding_the_process_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A minute of OCR is never worth a minute of shutdown.

    The converter here outlives any patience, and ``drain`` has to come back
    anyway - cancelled, not awaited to completion - with nothing escaping into
    the loop.
    """
    started = asyncio.Event()

    async def _slow(data: bytes, **kwargs: object) -> object:
        del data, kwargs
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("the conversion was allowed to finish")

    monkeypatch.setattr(conversions, "convert_attachment_async", _slow)
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=_sha())
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await asyncio.wait_for(scheduler.drain(timeout=0.05), timeout=5.0)

    assert scheduler.pending == 0


async def test_a_cancelled_conversion_is_converted_by_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Losing one costs one repeat" has to be true of a cancelled run too.

    Measured OCR is about twenty seconds an image against a five second drain,
    so a deploy while anything is converting cancels it. The claim that run
    inserted outlives its process, and the cache key is derived from the
    content: if cancellation left the claim behind, that file would answer "not
    yet converted" in every session and every turn in the namespace, for ever,
    and re-uploading it would produce the same key and the same answer. The
    operator has no remedy and no signal, which is the worst shape a failure
    here can take.
    """
    started = asyncio.Event()

    async def _slow(data: bytes, **kwargs: object) -> object:
        del data, kwargs
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("the conversion was allowed to finish")

    monkeypatch.setattr(conversions, "convert_attachment_async", _slow)
    source_sha256 = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(
        namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256
    )
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await asyncio.wait_for(scheduler.drain(timeout=0.05), timeout=5.0)

    # Nothing claims the content, so nothing has to expire before a retry.
    assert _rows_for(source_sha256) == []

    calls: list[bytes] = []

    async def _record(data: bytes, **kwargs: object) -> object:
        del kwargs
        calls.append(data)
        return _result("second attempt", ConversionStatus.OCR_EXTRACTED)

    monkeypatch.setattr(conversions, "convert_attachment_async", _record)
    retry = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert retry.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256)
    await retry.drain(timeout=5.0)

    assert len(calls) == 1
    assert _rows_for(source_sha256) == [("done", "second attempt")]


async def test_a_claim_nobody_is_holding_expires_and_is_taken_over(
    fake_converter: Any,
) -> None:
    """The backstop for the death that gets no chance to release anything.

    A ``SIGKILL``, a container evicted, a database error between the claim and
    the store: none of them run the cancellation handler, so the claim can only
    be recovered by expiring. This drives it with a zero-second lease rather
    than by waiting fifteen minutes, and the assertion is that the second
    worker converts - not merely that it did not crash.
    """
    calls, _ = fake_converter
    source_sha256 = _sha()

    _insert_running_claim(source_sha256)
    assert _rows_for(source_sha256) == [(STATE_RUNNING, None)]

    taker = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"), lease_seconds=0)
    assert taker.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256)
    await taker.drain(timeout=5.0)

    assert len(calls) == 1
    assert _rows_for(source_sha256) == [("done", "hello from the document")]


async def test_a_live_claim_is_not_stolen_by_the_lease(fake_converter: Any) -> None:
    """The expiry must not make the race check meaningless.

    A fresh claim is inside its lease, so the second worker still finds one
    running and returns. Without this the previous test would pass just as
    happily against an implementation that overwrote every claim it met.
    """
    calls, _ = fake_converter
    source_sha256 = _sha()
    _insert_running_claim(source_sha256)

    latecomer = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert latecomer.submit(
        namespace_key=_NAMESPACE, attachment_id=1, source_sha256=source_sha256
    )
    await latecomer.drain(timeout=5.0)

    assert calls == []
    assert _rows_for(source_sha256) == [(STATE_RUNNING, None)]


async def test_draining_nothing_is_free(fake_converter: Any) -> None:
    """Shutdown on an idle process does not wait for its timeout."""
    del fake_converter
    scheduler = ConversionScheduler()
    await asyncio.wait_for(scheduler.drain(timeout=30.0), timeout=1.0)


@pytest.fixture()
def fake_converter(monkeypatch: pytest.MonkeyPatch):
    """The converter, replaced and recorded.

    Declared here rather than imported from the neighbouring file: pytest
    resolves fixtures by name, and importing one only makes two modules share a
    symbol without making the dependency clearer.
    """
    calls: list[bytes] = []
    outcome = {"result": _result("hello from the document", ConversionStatus.OCR_EXTRACTED)}

    async def _convert(data: bytes, **kwargs: object) -> object:
        calls.append(data)
        return outcome["result"]

    monkeypatch.setattr(conversions, "convert_attachment_async", _convert)
    return calls, outcome


@pytest.fixture(autouse=True)
def _no_leftover_conversions() -> None:
    """One test's cache rows are never another's cache hit."""
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_attachment_conversions"))
