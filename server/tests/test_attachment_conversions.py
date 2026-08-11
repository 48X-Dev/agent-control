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
from agent_control_models import attachment_converter_backends as backends_module
from agent_control_models.attachment_converter import (
    FAILURE_OCR_CONVERTER_ABSENT,
    ConversionResult,
    ConversionStatus,
)
from agent_control_models.attachment_converter_backends import (
    FAILURE_CONVERTER_ERROR,
    DoclingBackend,
)
from agent_control_models.attachment_converter_cache import (
    conversion_cache_key,
    installed_capability_fingerprint,
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
    failure_code: str | None = None,
    capability_fingerprint: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_attachment_conversions "
                "(namespace_key, cache_key, source_sha256, state, status, "
                " text_body, text_chars, meaningful_chars, failure_code, "
                " capability_fingerprint) "
                "VALUES (:ns, :key, :sha, :state, :status, :body, :chars, :chars, "
                " :code, :fingerprint)"
            ),
            {
                "ns": _NAMESPACE,
                "key": conversion_cache_key(source_sha256),
                "sha": source_sha256,
                "state": state,
                "status": status,
                "body": body,
                "chars": len(body or ""),
                "code": failure_code,
                "fingerprint": capability_fingerprint,
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


# ---------------------------------------------------------------------------
# Capability-gated retry: a failure is only as durable as the toolbox it cites
# ---------------------------------------------------------------------------


def _pin(source_sha256: str) -> str:
    return f"pin:{source_sha256[:32]}"


@pytest.fixture()
def pinned_key(monkeypatch: pytest.MonkeyPatch):
    """Hold the cache key still while capabilities move.

    The incident under test is the one the key cannot see: a format extra
    arriving inside an installed MarkItDown changes no backend's
    ``available()``, so the key holds still and the stored refusal keeps
    answering. Pinning the key reproduces that blindness for any capability
    change, so these tests exercise the row fingerprint rather than the key
    rotation that already covers whole converters.
    """
    monkeypatch.setattr(conversions, "conversion_cache_key", _pin)


def _docling(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    monkeypatch.setattr(DoclingBackend, "available", lambda self: present)


def _unavailable(code: str = FAILURE_OCR_CONVERTER_ABSENT) -> ConversionResult:
    return ConversionResult(status=ConversionStatus.CONVERTER_UNAVAILABLE, failure_code=code)


async def _fresh_read(db, source_sha256: str):  # type: ignore[no-untyped-def]
    """Re-read past the session's identity map, which read_cached never needs
    in production: each turn's session is new, while a test reuses one."""
    db.expire_all()
    return await read_cached(db, namespace_key=_NAMESPACE, source_sha256=source_sha256)


def _pinned_row_count(source_sha256: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM agent_attachment_conversions"
                " WHERE namespace_key = :ns AND cache_key = :key"
            ),
            {"ns": _NAMESPACE, "key": _pin(source_sha256)},
        ).scalar_one()


async def test_a_capability_absent_failure_is_retried_after_the_capability_appears(
    async_db, fake_converter, pinned_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual DELETE, retired.

    A deck fails while a converter is missing; the converter arrives; the next
    read answers as a miss, the scheduler takes the failed row over, and the
    same row ends up carrying the text. Before the fingerprint, the second
    half of that story was an operator deleting the row by hand.
    """
    calls, outcome = fake_converter
    _docling(monkeypatch, present=False)
    outcome["result"] = _unavailable()
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=sha)
    await scheduler.drain()

    cached = await _fresh_read(async_db, sha)
    assert cached is not None
    assert cached.state == STATE_FAILED
    assert cached.failure_code == FAILURE_OCR_CONVERTER_ABSENT

    _docling(monkeypatch, present=True)
    outcome["result"] = _result("read at last", ConversionStatus.OCR_EXTRACTED)

    assert await _fresh_read(async_db, sha) is None
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=1, source_sha256=sha)
    await scheduler.drain()

    cached = await _fresh_read(async_db, sha)
    assert cached is not None
    assert cached.state == STATE_DONE
    assert cached.text == "read at last"
    assert len(calls) == 2
    assert _pinned_row_count(sha) == 1  # the verdict was replaced, never duplicated


async def test_a_genuine_failure_is_not_retried_by_a_capability_change(
    async_db, fake_converter, pinned_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``converter_error`` describes the file, and new tools change nothing
    about the file: served forever, converted once, whatever gets installed."""
    calls, outcome = fake_converter
    _docling(monkeypatch, present=False)
    outcome["result"] = ConversionResult(
        status=ConversionStatus.FAILED, failure_code=FAILURE_CONVERTER_ERROR
    )
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=2, source_sha256=sha)
    await scheduler.drain()

    _docling(monkeypatch, present=True)
    cached = await _fresh_read(async_db, sha)
    assert cached is not None
    assert cached.state == STATE_FAILED
    assert cached.failure_code == FAILURE_CONVERTER_ERROR

    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=2, source_sha256=sha)
    await scheduler.drain()
    assert len(calls) == 1


async def test_an_unchanged_capability_set_serves_the_failure_without_rerunning(
    async_db, fake_converter, pinned_key
) -> None:
    """No thrash: the fingerprint gates on change, not on the failure existing.

    Three reads and a forced resubmission against the same installed set: the
    stored refusal is served as a hit every time, so the binding path never
    reschedules it, and the claim refuses the takeover even when something
    schedules it anyway.
    """
    calls, outcome = fake_converter
    outcome["result"] = _unavailable()
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=3, source_sha256=sha)
    await scheduler.drain()

    for _ in range(3):
        cached = await _fresh_read(async_db, sha)
        assert cached is not None
        assert cached.state == STATE_FAILED

    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=3, source_sha256=sha)
    await scheduler.drain()
    assert len(calls) == 1


async def test_a_retry_that_fails_again_waits_for_the_next_change(
    async_db, fake_converter, pinned_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One capability change buys one retry, not a loop.

    The retry re-fails - the deck needed something the change did not bring -
    and the fresh verdict carries the new fingerprint, so every later poll is
    a hit again until the *next* change.
    """
    calls, outcome = fake_converter
    _docling(monkeypatch, present=False)
    outcome["result"] = _unavailable()
    sha = _sha()
    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=4, source_sha256=sha)
    await scheduler.drain()

    _docling(monkeypatch, present=True)
    assert await _fresh_read(async_db, sha) is None  # the change arms one retry
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=4, source_sha256=sha)
    await scheduler.drain()
    assert len(calls) == 2

    cached = await _fresh_read(async_db, sha)  # restamped, so a hit again
    assert cached is not None
    assert cached.state == STATE_FAILED
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=4, source_sha256=sha)
    await scheduler.drain()
    assert len(calls) == 2


async def test_a_failure_cached_before_the_fingerprint_existed_retries_once(
    async_db, fake_converter
) -> None:
    """The deployed backlog, recovered without psql.

    Rows written before the column carry ``NULL``, which ``IS DISTINCT FROM``
    reads as "not the current set": one retry, and the fresh verdict stamps
    them like any other row.
    """
    calls, _ = fake_converter
    sha = _sha()
    _seed_entry(
        source_sha256=sha,
        state=STATE_FAILED,
        body=None,
        status=ConversionStatus.CONVERTER_UNAVAILABLE.value,
        failure_code=FAILURE_OCR_CONVERTER_ABSENT,
    )
    assert await read_cached(async_db, namespace_key=_NAMESPACE, source_sha256=sha) is None

    scheduler = ConversionScheduler(blobs=_FakeBlobStore(b"%PDF-1.7 body"))
    assert scheduler.submit(namespace_key=_NAMESPACE, attachment_id=5, source_sha256=sha)
    await scheduler.drain()

    cached = await _fresh_read(async_db, sha)
    assert cached is not None
    assert cached.state == STATE_DONE
    assert cached.text == "hello from the document"
    assert len(calls) == 1


def test_a_format_extra_changes_the_fingerprint_but_not_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blindness the incident rode in on, pinned as an invariant.

    The key reads whole backends, deliberately: rotating it on a format extra
    would retire every successful conversion too. The extra still has to show
    up somewhere, and the fingerprint is that somewhere.
    """
    installed = {"markitdown"}
    monkeypatch.setattr(backends_module, "_module_installed", lambda module: module in installed)
    sha = _sha()
    key_before = conversion_cache_key(sha)
    fingerprint_before = installed_capability_fingerprint()

    installed.add("pptx")
    assert conversion_cache_key(sha) == key_before
    assert installed_capability_fingerprint() != fingerprint_before
