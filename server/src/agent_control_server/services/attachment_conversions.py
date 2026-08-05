"""Conversion, run out of band and read from a cache. Nothing here ever waits.

The measurement this module is built around: five image attachments on one
issue is roughly a hundred seconds of OCR, against a twenty-five second
per-step budget. Conversion therefore cannot run on the path that needs its
output, at any timeout, and this is a structural requirement rather than a
number to tune.

So the two halves are separated. :func:`read_cached` is a single indexed read
and is what a turn calls; it answers with what is stored or with nothing.
:func:`schedule_conversion` hands the work to a background worker and returns
immediately. A miss is rendered as a stated "not yet converted" line in front
of the agent (see :mod:`services.attachment_delivery`), never as a wait, and
the next turn carrying the same file finds the answer.

**The cache is keyed on content, not on the attachment.** The same spec
uploaded into a second session, or the same tracker file fetched by two steps
of one chain, converts once. What makes reuse safe is that the key folds in the
conversion contract version and which converters are installed, so installing
OCR does not leave every zero-character image answering forever from the day it
was unavailable. :func:`conversion_cache_key` owns that rule and this module
only calls it.

**A claim is a lease.** Converting the same content twice is waste; converting
it never is a file the agent is told it cannot read, permanently, with nothing
an operator can do about it. So the row a worker inserts to say "mine" expires
(:data:`CONVERSION_LEASE_SECONDS`), and a cancelled run drops it on the way
out. Both exist because the key is derived from the content: a stuck claim
cannot be cleared by re-uploading the file.

**A failure cites the toolbox that produced it.** ``failed`` is permanent only
when it describes the file. A verdict like ``ocr_converter_absent`` describes
the deployment, and deployments change: every stored verdict carries
:func:`installed_capability_fingerprint` from the moment it was written,
:func:`read_cached` answers a capability-absent failure as a miss once the
stamp no longer matches, and :meth:`ConversionScheduler._claim` takes the row
over so the retry runs once rather than per poll. The incident this pays for:
a real deck failed as ``ocr_converter_absent``, the image was rebuilt with the
pptx extra (a change invisible to the cache key, which reads whole
converters), and the cached refusal had to be deleted by hand.

**Two things this deliberately does not do.**

It does not run in a sidecar. The plan's isolated converter process is a later,
optional phase; until then conversion runs in this process, off the event loop
in a thread. A thread is not isolation and is not claimed to be - it is the
difference between a slow conversion and a stopped server. A deployment that
needs a memory-unsafe parser away from its database credentials should not
install a converter here.

It does not persist the full extracted text. What is stored is capped at
:data:`CACHED_TEXT_MAX_CHARS`, comfortably above anything one turn can carry
and far below what a long document produces, and ``stored_truncated`` records
the cut. Nothing writes the per-attachment ``extracted_text`` blob variant yet,
so downloading that variant answers 404. Both are stated rather than discovered
because a cache that quietly held megabytes per distinct file, reclaimed by
nothing, is how this table becomes the thing the byte quotas exist to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from agent_control_models.attachment_converter import (
    CAPABILITY_ABSENT_FAILURE_CODES,
    ConversionResult,
    ConversionStatus,
    convert_attachment_async,
)
from agent_control_models.attachments import AttachmentVariant
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ..db import AsyncSessionLocal
from ..models import AgentAttachmentConversion
from .attachment_blobs import AttachmentBlobStore, get_attachment_blob_store
from .attachment_converter_cache import (
    conversion_cache_key,
    installed_capability_fingerprint,
)
from .executor_metrics import (
    ATTACHMENT_CONVERSION_DROPPED,
    ATTACHMENT_CONVERSION_DURATION,
    ATTACHMENT_CONVERSION_EMPTY,
    ATTACHMENT_CONVERSION_FAILED,
    ATTACHMENT_CONVERSION_OK,
    ATTACHMENT_CONVERSIONS,
)

_logger = logging.getLogger(__name__)

CACHED_TEXT_MAX_CHARS = 64_000
"""How much of one conversion's text is kept.

Four times what a single turn could ever carry, so it is never the constraint
delivery hits, and small enough that a thousand distinct documents is sixty
megabytes rather than gigabytes. A cut sets ``stored_truncated`` and the
delivery line says so, because text that stops without saying it stopped is the
failure this whole path exists to avoid."""

CONVERSION_QUEUE_DEPTH = 64
"""Files that may be waiting at once. Past this, work is dropped rather than
queued: a queue with no bound is a memory leak with a schedule, and a dropped
conversion is already a designed outcome - the turn says "not yet converted"
and the next upload schedules it again."""

CONVERSION_CONCURRENCY = 1
"""How many conversions run at once.

One, because OCR is torch on CPU and saturates the machine on its own. Two
would not halve the wall clock and would double the resident cost inside a
process that is also serving policy evaluation for every other agent."""

CONVERSION_LEASE_SECONDS = 900
"""How long a claim is honoured before another worker may take it.

**The claim is a lease and not a marker, and that distinction is the whole
recovery story.** A worker that inserts ``running`` and then dies - killed
mid-deploy, cancelled at shutdown five seconds into a twenty-second OCR run,
or hit by a database error before it can store - leaves a row nobody is
working on. Without an expiry that row answers every later attempt with "one
is already running", for that content, in that namespace, for ever: the key is
content-derived, so re-uploading the file does not help, and the turn renders
:data:`~services.attachment_delivery.REASON_NOT_CONVERTED` with no remedy an
operator could find.

Fifteen minutes, because it has to sit above the slowest honest run by a wide
margin - measured Docling is about sixty seconds on a trivial PDF and twenty
per image - and a file that waits fifteen minutes for a retry has still been
delivered with a stated line on every turn in between. Cancellation also
releases its claim directly (:meth:`ConversionScheduler._run`), so the lease is
the backstop for the deaths that get no chance to clean up rather than the
common path."""

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CachedConversion:
    """One cache entry, as a turn reads it.

    ``state`` is what the delivery path branches on and it has three answers
    that are not the same: nothing stored at all (this object is ``None``),
    stored but not finished, and finished. Only the third can carry text, and
    finished-with-no-text is its own answer again - a scanned page nobody could
    read is not a page nobody tried to read, and an agent told the wrong one of
    those will draw the wrong conclusion.
    """

    state: str
    status: ConversionStatus | None
    text: str
    text_chars: int
    meaningful_chars: int
    stored_truncated: bool
    failure_code: str | None
    converter: str | None

    @property
    def is_finished(self) -> bool:
        return self.state in (STATE_DONE, STATE_FAILED)

    @property
    def has_text(self) -> bool:
        return bool(self.text)


async def read_cached(
    db: AsyncSession, *, namespace_key: str, source_sha256: str
) -> CachedConversion | None:
    """Read the entry for this content, or ``None`` when there is none.

    One indexed read on the turn's critical path. It never schedules, never
    waits and never raises for a missing converter: the caller decides what a
    miss is worth, and for a turn the answer is always a printed line rather
    than a delay.

    One class of stored answer is refused here: a ``failed`` entry whose
    ``failure_code`` names an absent capability, once the installed set no
    longer matches the fingerprint stamped on it. That verdict was about the
    deployment rather than the file, so it is answered as a miss - the caller
    schedules a conversion exactly as it would for content nobody has tried,
    and :meth:`ConversionScheduler._claim` keeps the retry single.
    """
    key = conversion_cache_key(source_sha256)
    stmt = (
        select(AgentAttachmentConversion)
        .options(undefer(AgentAttachmentConversion.text_body))
        .where(
            AgentAttachmentConversion.namespace_key == namespace_key,
            AgentAttachmentConversion.cache_key == key,
        )
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        return None
    if (
        row.state == STATE_FAILED
        and row.failure_code in CAPABILITY_ABSENT_FAILURE_CODES
        and row.capability_fingerprint != installed_capability_fingerprint()
    ):
        return None
    return CachedConversion(
        state=row.state,
        status=ConversionStatus(row.status) if row.status else None,
        text=row.text_body or "",
        text_chars=row.text_chars,
        meaningful_chars=row.meaningful_chars,
        stored_truncated=row.stored_truncated,
        failure_code=row.failure_code,
        converter=row.converter,
    )


class ConversionScheduler:
    """The background half. Bounded, fire-and-forget, and cancellable.

    Instantiated once per process. It holds no database session between jobs
    and opens its own for each one, because a worker that borrowed the
    request's session would keep a pooled connection for the length of an OCR
    run - the same defect the turn path spends a paragraph avoiding.
    """

    def __init__(
        self,
        *,
        concurrency: int = CONVERSION_CONCURRENCY,
        queue_depth: int = CONVERSION_QUEUE_DEPTH,
        lease_seconds: int = CONVERSION_LEASE_SECONDS,
        blobs: AttachmentBlobStore | None = None,
    ) -> None:
        self._concurrency = concurrency
        self._queue_depth = queue_depth
        self._lease_seconds = lease_seconds
        self._blobs = blobs
        self._inflight: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._gate: asyncio.Semaphore | None = None

    @property
    def pending(self) -> int:
        return len(self._tasks)

    def submit(
        self,
        *,
        namespace_key: str,
        attachment_id: int,
        source_sha256: str,
        declared_mime: str | None = None,
    ) -> bool:
        """Queue one conversion. Returns whether it was accepted.

        Deduplicated on ``(namespace, content)`` while in flight, so three
        turns carrying the same unconverted file schedule one conversion rather
        than three. Refusing past the queue depth is the designed behaviour and
        not an error: the caller has a line to print either way.
        """
        marker = (namespace_key, source_sha256)
        if marker in self._inflight:
            return False
        if len(self._tasks) >= self._queue_depth:
            ATTACHMENT_CONVERSIONS.labels(result=ATTACHMENT_CONVERSION_DROPPED).inc()
            _logger.warning(
                "Conversion queue full at %d; dropping one attachment's conversion",
                self._queue_depth,
            )
            return False

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: nothing can run in the background. Callers are
            # fire-and-forget, so this is a refusal rather than a failure.
            return False

        self._inflight.add(marker)
        task = loop.create_task(
            self._run(
                namespace_key=namespace_key,
                attachment_id=attachment_id,
                source_sha256=source_sha256,
                declared_mime=declared_mime,
            )
        )
        # Held, because a task nobody references can be garbage collected
        # mid-run and the conversion would simply vanish.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def drain(self, *, timeout: float = 5.0) -> None:
        """Wait briefly for outstanding work, then cancel it.

        Called at shutdown. A conversion is worth a few seconds and never worth
        holding a process open for a minute of OCR: the entry it would have
        written is a cache entry, so losing it costs one repeat.

        Costing one repeat is a property of :meth:`_run` rather than of this
        method - a cancelled run drops its claim, and the lease covers the
        kills that never get here at all.
        """
        if not self._tasks:
            return
        pending = list(self._tasks)
        done, still_running = await asyncio.wait(pending, timeout=timeout)
        del done
        for task in still_running:
            task.cancel()
        if still_running:
            await asyncio.gather(*still_running, return_exceptions=True)

    async def _run(
        self,
        *,
        namespace_key: str,
        attachment_id: int,
        source_sha256: str,
        declared_mime: str | None,
    ) -> None:
        """One conversion, start to finish. Never raises into the loop.

        The gate is what makes queued work queued. Without it every accepted
        submission would start immediately and sixty-four simultaneous OCR runs
        would take the process with them; with it, the queue depth bounds how
        many are *waiting* and the semaphore bounds how many are *running*.
        """
        started = time.monotonic()
        if self._gate is None:
            self._gate = asyncio.Semaphore(self._concurrency)
        cache_key = conversion_cache_key(source_sha256)
        try:
            async with self._gate:
                await self._convert_and_store(
                    namespace_key=namespace_key,
                    attachment_id=attachment_id,
                    source_sha256=source_sha256,
                    declared_mime=declared_mime,
                )
        except asyncio.CancelledError:
            # Shutdown cancels a run mid-conversion, and the claim this run
            # holds outlives the process that made it. Dropping it here is what
            # makes "losing one costs one repeat" true: the alternative is a row
            # nobody is working on that answers every later attempt at this
            # content, in this namespace, until the lease expires.
            await self._release_quietly(namespace_key=namespace_key, cache_key=cache_key)
            raise
        except Exception:
            # A background task whose exception nobody retrieves is logged by
            # asyncio at an unhelpful moment and with no context. This is the
            # context.
            ATTACHMENT_CONVERSIONS.labels(result=ATTACHMENT_CONVERSION_FAILED).inc()
            _logger.warning(
                "Background conversion failed for attachment %d",
                attachment_id,
                exc_info=True,
            )
            # Released for the same reason, and note what this does not do: a
            # conversion that ran and *decided* it could not read the file is
            # stored as ``failed`` by ``_store`` and keeps its row. Only a run
            # that never reached a verdict is made retryable here; the one
            # exception for stored verdicts - a capability-absent failure
            # against a changed installed set - belongs to ``_claim``.
            await self._release_quietly(namespace_key=namespace_key, cache_key=cache_key)
        finally:
            ATTACHMENT_CONVERSION_DURATION.observe(max(0.0, time.monotonic() - started))
            self._inflight.discard((namespace_key, source_sha256))

    async def _convert_and_store(
        self,
        *,
        namespace_key: str,
        attachment_id: int,
        source_sha256: str,
        declared_mime: str | None,
    ) -> None:
        blobs = self._blobs or get_attachment_blob_store()
        cache_key = conversion_cache_key(source_sha256)
        # Read once for the whole run: the claim this takes and the verdict it
        # stores must cite the same capability set, or a retry could re-arm
        # against a fingerprint it never ran under.
        capability_fingerprint = installed_capability_fingerprint()

        async with AsyncSessionLocal() as db:
            claimed = await self._claim(
                db,
                namespace_key=namespace_key,
                cache_key=cache_key,
                source_sha256=source_sha256,
                capability_fingerprint=capability_fingerprint,
            )
            if not claimed:
                return
            blob = await blobs.open(
                db,
                namespace_key=namespace_key,
                attachment_id=attachment_id,
                variant=AttachmentVariant.ORIGINAL,
            )
            await db.commit()

        if blob is None:
            # The bytes went away between scheduling and running - deleted, or
            # reclaimed by the retention sweep. There is nothing to convert and
            # nothing wrong; the claim is released so a later upload of the
            # same content can try again.
            await self._release(namespace_key=namespace_key, cache_key=cache_key)
            return

        result = await convert_attachment_async(
            blob.data, declared_mime=declared_mime or blob.content_type
        )

        async with AsyncSessionLocal() as db:
            await self._store(
                db,
                namespace_key=namespace_key,
                cache_key=cache_key,
                result=result,
                capability_fingerprint=capability_fingerprint,
            )
            await db.commit()

        if result.has_text:
            ATTACHMENT_CONVERSIONS.labels(result=ATTACHMENT_CONVERSION_OK).inc()
        elif result.status is ConversionStatus.EMPTY:
            ATTACHMENT_CONVERSIONS.labels(result=ATTACHMENT_CONVERSION_EMPTY).inc()
        else:
            ATTACHMENT_CONVERSIONS.labels(result=ATTACHMENT_CONVERSION_FAILED).inc()

    async def _claim(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        cache_key: str,
        source_sha256: str,
        capability_fingerprint: str,
    ) -> bool:
        """Take the lease on this content, or find somebody else holding it.

        One statement rather than a read then a write: two workers in two
        processes reach here for the same content, and a select-then-insert
        would have both of them convert. The one whose statement affects a row
        converts; the other returns and the entry the winner writes serves them
        both.

        **The conflict branch is an update and not a no-op, and that is the
        recovery path.** A claim older than the lease belongs to a worker that
        is gone - the process was killed, or shutdown cancelled it before its
        own release could run - and taking it over is the only thing that ever
        does. ``DO NOTHING`` here would mean one interrupted run poisons that
        file's cache entry permanently, since the key is derived from the
        content and re-uploading produces the same one. The ``WHERE`` reads the
        stored row, so a ``done`` entry is never disturbed and a live claim is
        never stolen.

        **A failed row is not always a stored answer.** A verdict whose
        ``failure_code`` names an absent capability holds only for the
        installed set stamped on it, so a row bearing a different stamp is
        taken over exactly like an expired lease, and the verdict this run
        stores - success or a fresh failure - carries the current one. The
        restamp is what keeps the retry single: same fingerprint, no takeover.
        ``IS DISTINCT FROM`` rather than ``!=`` is what lets the pre-column
        ``NULL`` stamp count as different, so failures cached before the
        fingerprint existed retry once instead of needing the hand-written
        DELETE this mechanism replaces. A ``failed`` row outside those codes -
        a parser that broke on the file itself - matches neither branch and
        keeps its row.
        """
        stale_before = func.now() - func.make_interval(0, 0, 0, 0, 0, 0, self._lease_seconds)
        stmt = (
            pg_insert(AgentAttachmentConversion)
            .values(
                namespace_key=namespace_key,
                cache_key=cache_key,
                source_sha256=source_sha256,
                state=STATE_RUNNING,
            )
            .on_conflict_do_update(
                index_elements=["namespace_key", "cache_key"],
                set_={"state": STATE_RUNNING, "updated_at": func.now()},
                where=or_(
                    and_(
                        AgentAttachmentConversion.state == STATE_RUNNING,
                        AgentAttachmentConversion.updated_at < stale_before,
                    ),
                    and_(
                        AgentAttachmentConversion.state == STATE_FAILED,
                        AgentAttachmentConversion.failure_code.in_(
                            sorted(CAPABILITY_ABSENT_FAILURE_CODES)
                        ),
                        AgentAttachmentConversion.capability_fingerprint.is_distinct_from(
                            capability_fingerprint
                        ),
                    ),
                ),
            )
            .returning(AgentAttachmentConversion.id)
        )
        try:
            claimed = (await db.execute(stmt)).scalars().first()
        except SQLAlchemyError:
            _logger.warning("Could not claim a conversion", exc_info=True)
            return False
        return claimed is not None

    async def _release_quietly(self, *, namespace_key: str, cache_key: str) -> None:
        """Release a claim on a path that is already failing.

        Never raises. This runs from the cancellation handler, where the loop
        may be closing under it and the session may not open at all; the lease
        in :data:`CONVERSION_LEASE_SECONDS` is what covers the release that does
        not land, and an exception raised here would replace the original
        failure with a less informative one.
        """
        try:
            await self._release(namespace_key=namespace_key, cache_key=cache_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.warning(
                "Could not release a conversion claim; it expires with the lease",
                exc_info=True,
            )

    async def _release(self, *, namespace_key: str, cache_key: str) -> None:
        """Delete an unfinished claim, so the content is retryable at once.

        Scoped to ``running`` so it can never remove a stored answer: by the
        time this runs the same key may hold a finished conversion, either this
        worker's own or one written by whoever took the lease over.
        """
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(AgentAttachmentConversion).where(
                    AgentAttachmentConversion.namespace_key == namespace_key,
                    AgentAttachmentConversion.cache_key == cache_key,
                    AgentAttachmentConversion.state == STATE_RUNNING,
                )
            )
            await db.commit()

    async def _store(
        self,
        db: AsyncSession,
        *,
        namespace_key: str,
        cache_key: str,
        result: ConversionResult,
        capability_fingerprint: str,
    ) -> None:
        text = result.text[:CACHED_TEXT_MAX_CHARS]
        truncated = result.text_truncated or len(text) < len(result.text)
        values = {
            "state": STATE_DONE if result.has_text else STATE_FAILED,
            "status": result.status.value,
            "converter": result.converter,
            "text_body": text or None,
            "text_chars": result.text_chars,
            "meaningful_chars": result.meaningful_chars,
            "stored_truncated": truncated,
            "failure_code": result.failure_code,
            "capability_fingerprint": capability_fingerprint,
        }
        await db.execute(
            update(AgentAttachmentConversion)
            .where(
                AgentAttachmentConversion.namespace_key == namespace_key,
                AgentAttachmentConversion.cache_key == cache_key,
            )
            .values(**values)
        )


_scheduler = ConversionScheduler()


def get_conversion_scheduler() -> ConversionScheduler:
    """The process-wide scheduler."""
    return _scheduler


def reset_conversion_scheduler() -> None:
    """Forget what this process thinks is in flight.

    For tests, following ``reset_turn_quota`` and ``reset_attachment_quota``.
    The in-flight set is cleared by :meth:`ConversionScheduler._run`'s own
    ``finally``, which a server process always reaches; a test whose event loop
    ends with a task still pending never runs it, and the marker it left behind
    would make the next test's identical content refuse to schedule at all.
    """
    _scheduler._inflight.clear()


def schedule_conversion(
    *,
    namespace_key: str,
    attachment_id: int,
    source_sha256: str,
    declared_mime: str | None = None,
) -> bool:
    """Ask for this content to be converted, without waiting for it.

    Safe to call from a request handler: it takes no lock, opens no session and
    returns in microseconds whether or not the work is accepted.
    """
    return _scheduler.submit(
        namespace_key=namespace_key,
        attachment_id=attachment_id,
        source_sha256=source_sha256,
        declared_mime=declared_mime,
    )


async def shutdown_attachment_conversions() -> None:
    """Release background conversions at shutdown."""
    await _scheduler.drain()
