"""The out-of-band half: the cache a turn reads and the queue it never waits on.

The property under test throughout is the one the plan calls structural rather
than tunable: **nothing on a request path ever waits for a conversion.** So the
scheduler is asserted on what it does to the caller (returns immediately,
refuses rather than growing, deduplicates) and the cache on what it answers
before, during and after the work.

The converter itself is replaced here. Its own behaviour has three test files of
its own, and against a bare checkout it would answer ``converter_unavailable``
for every input, which would make every assertion in this file vacuous.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from agent_control_models.attachment_converter import (
    ConversionResult,
    ConversionStatus,
)
from agent_control_models.attachments import AttachmentVariant
from sqlalchemy import text

from agent_control_server.services import attachment_conversions as conversions
from agent_control_server.services.attachment_conversions import (
    STATE_DONE,
    STATE_FAILED,
    STATE_RUNNING,
    ConversionScheduler,
    read_cached,
)
from agent_control_server.services.attachment_converter_cache import (
    conversion_cache_key,
)

from .conftest import engine

_NAMESPACE = "default"


def _sha() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _seed_entry(
    *,
    source_sha256: str,
    state: str = STATE_DONE,
    body: str | None = "extracted words",
    status: str = ConversionStatus.TEXT_LAYER_EXTRACTED.value,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_attachment_conversions "
                "(namespace_key, cache_key, source_sha256, state, status, "
                " text_body, text_chars, meaningful_chars) "
                "VALUES (:ns, :key, :sha, :state, :status, :body, :chars, :chars)"
            ),
            {
                "ns": _NAMESPACE,
                "key": conversion_cache_key(source_sha256),
                "sha": source_sha256,
                "state": state,
                "status": status,
                "body": body,
                "chars": len(body or ""),
            },
        )


class _FakeBlobStore:
    """Only ``open`` is reached by the scheduler; the rest would be a lie."""

    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.opened: list[int] = []

    async def open(self, db, *, namespace_key, attachment_id, variant):  # type: ignore[no-untyped-def]
        del db, namespace_key
        assert variant is AttachmentVariant.ORIGINAL
        self.opened.append(attachment_id)
        if self.data is None:
            return None
        from agent_control_server.services.attachment_blobs import StoredBlob

        return StoredBlob(
            content_type="application/pdf",
            size_bytes=len(self.data),
            sha256="0" * 64,
            data=self.data,
        )


def _result(text_body: str, status: ConversionStatus) -> ConversionResult:
    return ConversionResult(
        status=status,
        text=text_body,
        converter="fake",
        meaningful_chars=len(text_body),
    )


@pytest.fixture()
def fake_converter(monkeypatch: pytest.MonkeyPatch):
    """Replace the converter and record what it was asked to convert."""
    calls: list[bytes] = []
    outcome = {"result": _result("hello from the document", ConversionStatus.OCR_EXTRACTED)}

    async def _convert(data: bytes, **kwargs: object) -> ConversionResult:
        calls.append(data)
        return outcome["result"]

    monkeypatch.setattr(conversions, "convert_attachment_async", _convert)
    return calls, outcome


# ---------------------------------------------------------------------------
# The cache a turn reads
# ---------------------------------------------------------------------------


async def test_content_nobody_has_converted_answers_nothing(async_db) -> None:
    """A miss is ``None`` rather than an empty result.

    The delivery path tells "not read yet" and "read and empty" apart, and it
    can only do that if a miss is distinguishable from a finished conversion
    that found nothing.
    """
    assert await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=_sha()) is None


async def test_a_finished_conversion_answers_with_its_text(async_db) -> None:
    sha = _sha()
    _seed_entry(source_sha256=sha)
    cached = await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha)
    assert cached is not None
    assert cached.is_finished
    assert cached.has_text
    assert cached.text == "extracted words"


async def test_a_conversion_still_running_answers_unfinished(async_db) -> None:
    """Which is what the turn renders as "not read yet" without waiting."""
    sha = _sha()
    _seed_entry(source_sha256=sha, state=STATE_RUNNING, body=None)
    cached = await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha)
    assert cached is not None
    assert cached.is_finished is False


async def test_another_namespaces_entry_is_not_read(async_db) -> None:
    sha = _sha()
    _seed_entry(source_sha256=sha)
    assert await read_cached(async_db, namespace_key="other", source_sha256=sha) is None


async def test_an_entry_written_when_a_converter_was_missing_is_not_reused(
    async_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason the key is not the content hash on its own.

    Installing OCR must not leave every zero-character image answering forever
    from the day it was unavailable, so a change in which converters are
    installed changes the key and the old entry is simply not found.
    """
    sha = _sha()
    _seed_entry(source_sha256=sha, body="", status=ConversionStatus.EMPTY.value)
    assert await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha) is not None

    class _Backend:
        name = "ocr"

        def available(self) -> bool:
            return True

    monkeypatch.setattr(
        conversions,
        "conversion_cache_key",
        lambda sha256: conversion_cache_key(sha256, backends=(_Backend(),)),  # type: ignore[arg-type]
    )
    assert await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha) is None


# ---------------------------------------------------------------------------
# The queue nothing waits on
# ---------------------------------------------------------------------------


async def test_submitting_returns_before_the_work_does(fake_converter) -> None:
    """The measurement this module exists for, asserted rather than asserted-about.

    The converter here blocks until released. ``submit`` still returns, and it
    returns having accepted the work, which is what makes a twenty-second OCR
    invisible to the request that triggered it.
    """
    calls, outcome = fake_converter
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow(data: bytes, **kwargs: object) -> ConversionResult:
        started.set()
        await release.wait()
        calls.append(data)
        return outcome["result"]

    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    conversions.convert_attachment_async = _slow  # type: ignore[assignment]
    try:
        assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=_sha())
        await asyncio.wait_for(started.wait(), timeout=5)
        assert calls == []
        release.set()
        await scheduler.drain()
    finally:
        release.set()


async def test_the_same_content_is_only_converted_once_at_a_time(
    fake_converter,
) -> None:
    """Three turns carrying the same unread file schedule one conversion."""
    sha = _sha()
    release = asyncio.Event()

    async def _slow(data: bytes, **kwargs: object) -> ConversionResult:
        await release.wait()
        return _result("text", ConversionStatus.OCR_EXTRACTED)

    conversions.convert_attachment_async = _slow  # type: ignore[assignment]
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    accepted = [
        scheduler.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=sha)
        for _ in range(3)
    ]
    release.set()
    await scheduler.drain()
    assert accepted == [True, False, False]


async def test_a_full_queue_refuses_rather_than_growing(fake_converter) -> None:
    """A queue with no bound is a memory leak with a schedule.

    Refusing is a designed outcome and not a failure: the turn that wanted the
    file prints "not read yet" either way and schedules it again next time.
    """
    release = asyncio.Event()

    async def _slow(data: bytes, **kwargs: object) -> ConversionResult:
        await release.wait()
        return _result("text", ConversionStatus.OCR_EXTRACTED)

    conversions.convert_attachment_async = _slow  # type: ignore[assignment]
    scheduler = ConversionScheduler(queue_depth=2, blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    accepted = [
        scheduler.submit(namespace_key=_NAMESPACE, attachment_id=index, source_sha256=_sha())
        for index in range(4)
    ]
    release.set()
    await scheduler.drain()
    assert accepted == [True, True, False, False]


async def test_a_finished_conversion_is_stored_under_the_content_key(
    async_db, fake_converter
) -> None:
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=7, source_sha256=sha)
    await scheduler.drain()

    cached = await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha)
    assert cached is not None
    assert cached.state == STATE_DONE
    assert cached.text == "hello from the document"
    assert cached.status is ConversionStatus.OCR_EXTRACTED


async def test_a_conversion_that_found_nothing_is_stored_as_finished_and_empty(
    async_db, fake_converter
) -> None:
    """Finished-and-empty has to be stored, not left as a miss.

    Leaving it absent would re-queue an unreadable file on every turn that
    carried it, and tell the agent "not read yet" forever about a file that has
    been read three times.
    """
    _, outcome = fake_converter
    outcome["result"] = _result("", ConversionStatus.EMPTY)
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    scheduler.submit(namespace_key=_NAMESPACE, attachment_id=8, source_sha256=sha)
    await scheduler.drain()

    cached = await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha)
    assert cached is not None
    assert cached.state == STATE_FAILED
    assert cached.is_finished is True
    assert cached.has_text is False


async def test_bytes_that_vanished_leave_no_claim_behind(async_db, fake_converter) -> None:
    """Deleted or reclaimed between scheduling and running.

    The claim has to go, or the content is permanently marked "running" and a
    later upload of the same file never converts.
    """
    calls, _ = fake_converter
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(None))
    scheduler.submit(namespace_key=_NAMESPACE, attachment_id=9, source_sha256=sha)
    await scheduler.drain()

    assert calls == []
    assert await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha) is None


async def test_stored_text_is_capped_and_says_so(async_db, fake_converter) -> None:
    _, outcome = fake_converter
    oversized = "z" * (conversions.CACHED_TEXT_MAX_CHARS + 500)
    outcome["result"] = _result(oversized, ConversionStatus.TEXT_LAYER_EXTRACTED)
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    scheduler.submit(namespace_key=_NAMESPACE, attachment_id=10, source_sha256=sha)
    await scheduler.drain()

    cached = await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha)
    assert cached is not None
    assert len(cached.text) == conversions.CACHED_TEXT_MAX_CHARS
    assert cached.stored_truncated is True
    assert cached.text_chars == len(oversized)
