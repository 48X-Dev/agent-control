"""One pass over the Drive corpus: claim the lease, walk or replay, ingest, release."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import SyncConfig
from .drive_auth import DriveTokenProvider
from .drive_client import (
    DriveChange,
    DriveClient,
    DriveError,
    DriveItem,
    DriveRefusalError,
    LocationUnknown,
    OutsideRoot,
    UnderRoot,
)
from .drive_transport import DriveTransport
from .ingest import Ingestor, TombstoneReason
from .ingest_guard import AgentOutputGuard, DriveAncestry
from .journal import RunCounters, SourceState, SyncFailedError, SyncJournal, Tally
from .lease import SessionFactory, SyncLease, hold_lease, mint_token
from .schema import chunks, documents, sources, sync_runs

IngestorFactory = Callable[[int], Ingestor]

LEASE_RENEW_EVERY = 100
"""Documents between renewals during a walk; the replay renews per drained batch."""

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CorpusStatus:
    """What ``status`` prints: the mirror, its freshness and its last run."""

    documents: int
    chunks: int
    sources_enabled: int
    sources_failing: int
    last_verified_at: datetime | None
    stale_seconds: int | None
    last_run_status: str | None
    last_run_finished_at: datetime | None
    last_run_error_code: str | None


_STATUS = sa.select(
    sa.select(sa.func.count())
    .select_from(documents)
    .where(
        documents.c.tombstoned_at.is_(None),
        sa.exists().where(chunks.c.document_id == documents.c.id),
        sa.exists().where(sa.and_(sources.c.id == documents.c.source_id, sources.c.enabled)),
    )
    .scalar_subquery()
    .label("documents"),
    sa.select(sa.func.count()).select_from(chunks).scalar_subquery().label("chunks"),
    sa.select(sa.func.count())
    .select_from(sources)
    .where(sources.c.enabled)
    .scalar_subquery()
    .label("sources_enabled"),
    sa.select(sa.func.count())
    .select_from(sources)
    .where(sources.c.enabled, sources.c.last_run_status == "failed")
    .scalar_subquery()
    .label("sources_failing"),
    sa.select(sa.func.min(sources.c.last_verified_at))
    .where(sources.c.enabled)
    .scalar_subquery()
    .label("oldest_verified_at"),
    sa.select(sa.func.count())
    .select_from(sources)
    .where(sources.c.enabled, sources.c.last_verified_at.is_(None))
    .scalar_subquery()
    .label("never_verified"),
    sa.func.now().label("observed_at"),
)

_LAST_RUN = (
    sa.select(sync_runs.c.status, sync_runs.c.finished_at, sync_runs.c.error_code)
    .order_by(sync_runs.c.id.desc())
    .limit(1)
)


@asynccontextmanager
async def corpus_sessions(config: SyncConfig) -> AsyncIterator[SessionFactory]:
    """One engine for one process, disposed on the way out."""
    engine = create_async_engine(config.database_url)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def run_once(config: SyncConfig) -> RunCounters:
    """One sync pass, end to end. What ``once`` and a cron entry both call."""
    async with corpus_sessions(config) as sessions:
        async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as http:
            tokens = DriveTokenProvider(config.credentials, http)
            client = DriveClient(tokens, http, config)
            guard = AgentOutputGuard(
                config.executor_drive_root_id,
                DriveAncestry(DriveTransport(tokens, http, config)),
            )
            async with hold_lease(sessions, holder=mint_token()) as lease:
                return await run_once_with(
                    config,
                    client=client,
                    journal=SyncJournal(sessions),
                    lease=lease,
                    ingestor_factory=lambda source_id: Ingestor(
                        sessions, source_id, guard=guard
                    ),
                )


async def run_once_with(
    config: SyncConfig,
    *,
    client: DriveClient,
    journal: SyncJournal,
    lease: SyncLease,
    ingestor_factory: IngestorFactory,
) -> RunCounters:
    """The orchestration, with the lease already held and collaborators supplied."""
    await journal.assert_schema()
    await journal.lapse_orphans(lease.holder)
    run_id = await journal.open_run(lease.holder)
    tally = Tally()
    try:
        root = await _resolve_root(client, journal, config.root_folder_id)
        source = await journal.ensure_source(ref=config.root_folder_id, display_name=root.name)
        ingestor = ingestor_factory(source.id)
        truncated = await _sweep(config, client, journal, ingestor, lease, source, tally)
        tally.secrets_skipped = ingestor.secrets_skipped
    except BaseException as exc:
        # BaseException, not Exception: a Ctrl-C or a cancelled task must still
        # close its `sync_runs` row, or the next claimant reads it as orphaned.
        code = getattr(exc, "code", "run_failed")
        await journal.close_run(
            run_id,
            status="failed",
            counters=tally.freeze(),
            tally=tally,
            error_code=code if isinstance(code, str) else "run_failed",
        )
        raise

    _fold_walk_refusals(client, tally)
    counters = tally.freeze()
    status = "partial" if truncated else "ok"
    error_code = "source_ceiling" if truncated else None
    await journal.mark_verified(source.id, status=status, error_code=error_code)
    await journal.close_run(
        run_id, status=status, counters=counters, tally=tally, error_code=error_code
    )
    return counters


async def _resolve_root(client: DriveClient, journal: SyncJournal, ref: str) -> DriveItem:
    """Abort loudly: a root that did not resolve reads exactly like an empty corpus."""
    try:
        return await client.resolve_root()
    except DriveError as exc:
        await journal.mark_source_failed(ref=ref, error_code=exc.code)
        raise SyncFailedError(str(exc), code=exc.code) from exc


async def _sweep(
    config: SyncConfig,
    client: DriveClient,
    journal: SyncJournal,
    ingestor: Ingestor,
    lease: SyncLease,
    source: SourceState,
    tally: Tally,
) -> bool:
    """The full walk or the changes replay. Returns whether a ceiling truncated it."""
    if source.cursor is None:
        return await _walk(config, client, journal, ingestor, lease, source, tally)
    return await _replay(client, journal, ingestor, lease, source, source.cursor, tally)


async def _walk(
    config: SyncConfig,
    client: DriveClient,
    journal: SyncJournal,
    ingestor: Ingestor,
    lease: SyncLease,
    source: SourceState,
    tally: Tally,
) -> bool:
    """First run: take the cursor before the walk, so nothing between the two is lost."""
    cursor = await client.start_cursor()
    walked = 0
    async for item in client.walk_subtree():
        walked += 1
        await _ingest_one(item, client=client, ingestor=ingestor, tally=tally)
        if walked % LEASE_RENEW_EVERY == 0:
            await _renew(lease)
    await _renew(lease)
    if client.walk_truncated:
        # No cursor: storing one would strand what the walk never reached.
        _LOG.warning(
            "walk truncated at ceiling=%d after %d documents", config.max_documents_per_run, walked
        )
        return True
    await journal.advance_cursor(source.id, cursor)
    return False


async def _renew(lease: SyncLease) -> None:
    """A lease that will not renew is one another process now holds; stop before writing more."""
    if not await lease.renew():
        raise SyncFailedError(
            "The sync lease was stolen mid-run; the cursor was left where it was.",
            code="lease_lost",
        )


async def _replay(
    client: DriveClient,
    journal: SyncJournal,
    ingestor: Ingestor,
    lease: SyncLease,
    source: SourceState,
    cursor: str,
    tally: Tally,
) -> bool:
    """Later runs: one drained batch from the stored cursor, then advance it."""
    changes, new_cursor = await client.list_changes(cursor)
    for change in changes:
        await _apply(change, client=client, ingestor=ingestor, tally=tally)
    await _renew(lease)
    # Section 10 splits verification from advancement: a poll that came back
    # holding the same token verified the source without moving its cursor, and
    # a `cursor_advanced_at` stamped anyway is a diagnostic that always agrees
    # with `last_verified_at` and so says nothing.
    if new_cursor != cursor:
        await journal.advance_cursor(source.id, new_cursor)
    return False


async def _apply(
    change: DriveChange, *, client: DriveClient, ingestor: Ingestor, tally: Tally
) -> None:
    """One changes-feed entry: a tombstone, an out-of-scope skip, or an ingest."""
    if change.removed or change.item is None:
        # No ancestry to walk on a file that is gone, and none is needed: a
        # tombstone for something never indexed answers False and costs one row read.
        tally.seen += 1
        if await ingestor.tombstone(change.file_id):
            tally.tombstoned += 1
        return
    location = await client.resolve_folder_path(change.file_id)
    match location:
        case UnderRoot(folders):
            item = replace(change.item, folder_path=folders)
            await _ingest_one(item, client=client, ingestor=ingestor, tally=tally)
        case OutsideRoot():
            # A document moved out of the root is still readable, so the feed
            # never flags it removed. Tombstoning is the only thing that takes it out.
            if await ingestor.tombstone(change.file_id, reason=TombstoneReason.EXCLUDED):
                tally.seen += 1
                tally.tombstoned += 1
        case LocationUnknown(code, detail):
            tally.seen += 1
            tally.refuse(code)
            _LOG.warning("refused item=%s code=%s: %s", change.file_id, code, detail)
        case _:
            assert_never(location)


async def _ingest_one(
    item: DriveItem, *, client: DriveClient, ingestor: Ingestor, tally: Tally
) -> None:
    """Fetch and ingest one document, counting every refusal by its own code."""
    tally.seen += 1
    try:
        content = await client.fetch_content(item)
    except DriveRefusalError as refusal:
        tally.refuse(refusal.code)
        _LOG.warning("refused item=%s code=%s: %s", item.id, refusal.code, refusal)
        if await ingestor.refuse_fetch(item.id, refusal.code):
            tally.tombstoned += 1
        return
    tally.bytes_fetched += len(content.data)
    outcome = await ingestor.ingest(item, content)
    if outcome.refusal_code is not None:
        tally.refuse(outcome.refusal_code)
        _LOG.warning("refused item=%s code=%s", item.id, outcome.refusal_code)
    elif outcome.skipped_unchanged:
        tally.unchanged += 1
    else:
        tally.indexed += 1


def _fold_walk_refusals(client: DriveClient, tally: Tally) -> None:
    """The walk survives an unreadable folder or shortcut; the run must still report it."""
    for record in client.refusals:
        tally.refuse(record.code)


async def read_status(sessions: SessionFactory) -> CorpusStatus:
    """The corpus as ``status`` reports it, with staleness keyed on verification."""
    async with sessions() as session:
        row = (await session.execute(_STATUS)).one()
        last_run = (await session.execute(_LAST_RUN)).first()
    oldest = row.oldest_verified_at
    stale_seconds: int | None = None
    if oldest is not None and not row.never_verified:
        stale_seconds = max(0, int((row.observed_at - oldest).total_seconds()))
    return CorpusStatus(
        documents=int(row.documents),
        chunks=int(row.chunks),
        sources_enabled=int(row.sources_enabled),
        sources_failing=int(row.sources_failing),
        last_verified_at=oldest,
        stale_seconds=stale_seconds,
        last_run_status=None if last_run is None else last_run.status,
        last_run_finished_at=None if last_run is None else last_run.finished_at,
        last_run_error_code=None if last_run is None else last_run.error_code,
    )
