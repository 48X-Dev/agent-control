"""One pass over the corpus: claim the lease, sweep every source, ingest, release."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import assert_never

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .config import SyncConfig
from .conversion_cache import open_conversion_cache
from .drive_auth import DriveTokenProvider
from .drive_client import (
    DriveChange,
    DriveClient,
    DriveError,
    DriveItem,
    DriveRefusalError,
    FetchedContent,
    LocationUnknown,
    OutsideRoot,
    UnderRoot,
)
from .drive_transport import DriveTransport
from .github_run import GitHubChannel, github_channel, github_journal, run_github
from .ingest import Ingestor, SourceItem, TombstoneReason
from .ingest_guard import AgentOutputGuard, DriveAncestry
from .journal import RunCounters, SourceState, SyncFailedError, SyncJournal, Tally
from .lease import SessionFactory, SyncLease, hold_lease, mint_token
from .schema import chunks, documents, sources, sync_runs

IngestorFactory = Callable[[int], Ingestor]
GitHubPass = Callable[[Tally], Awaitable[str | None]]
"""The GitHub half of a run, or nothing at all when the channel is unconfigured."""

LEASE_RENEW_EVERY = 100
"""Documents between renewals during a walk; the replay renews per drained batch."""

SOURCE_CEILING = "source_ceiling"
RUN_FETCH_CEILING = "run_fetch_ceiling"
GITHUB_SKIPPED = "github channel skipped: the run's fetch ceiling was already spent"

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class FetchBudget:
    """5.4's byte ceilings: one source's fetches, and one process's across all of them."""

    source_max_bytes: int
    run_max_fetch_bytes: int
    source_bytes: int = 0
    run_bytes: int = 0

    def spend(self, count: int) -> None:
        self.source_bytes += count
        self.run_bytes += count

    @property
    def exceeded(self) -> str | None:
        """The ceiling that stopped this run, named so the status can say which."""
        if self.run_bytes >= self.run_max_fetch_bytes:
            return RUN_FETCH_CEILING
        if self.source_bytes >= self.source_max_bytes:
            return SOURCE_CEILING
        return None


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
    channel = github_channel(config)
    with open_conversion_cache(config.database_url):
        return await _run_once(config, channel)


async def _run_once(config: SyncConfig, channel: GitHubChannel | None) -> RunCounters:
    """The pass itself, with the conversion cache already installed for this process."""
    async with corpus_sessions(config) as sessions:
        async with httpx.AsyncClient(timeout=config.request_timeout_seconds) as http:
            tokens = DriveTokenProvider(config.credentials, http)
            client = DriveClient(tokens, http, config)
            guard = AgentOutputGuard(
                config.executor_drive_root_id,
                DriveAncestry(DriveTransport(tokens, http, config)),
            )
            github = None if channel is None else _github_pass(channel, config, http, sessions)
            async with hold_lease(sessions, holder=mint_token()) as lease:
                return await run_once_with(
                    config,
                    client=client,
                    journal=SyncJournal(sessions),
                    lease=lease,
                    ingestor_factory=lambda source_id: Ingestor(sessions, source_id, guard=guard),
                    github=github,
                )


def _github_pass(
    channel: GitHubChannel,
    config: SyncConfig,
    http: httpx.AsyncClient,
    sessions: SessionFactory,
) -> GitHubPass:
    """The configured half, bound to this run's HTTP client and corpus sessions."""

    async def sweep(tally: Tally) -> str | None:
        return await run_github(
            channel,
            tally,
            config=config,
            http=http,
            journal=github_journal(sessions),
            sessions=sessions,
        )

    return sweep


async def run_once_with(
    config: SyncConfig,
    *,
    client: DriveClient,
    journal: SyncJournal,
    lease: SyncLease,
    ingestor_factory: IngestorFactory,
    github: GitHubPass | None = None,
) -> RunCounters:
    """The orchestration, with the lease already held and collaborators supplied."""
    await journal.assert_schema()
    await journal.lapse_orphans(lease.holder)
    run_id = await journal.open_run(lease.holder)
    tally = Tally()
    budget = FetchBudget(config.source_max_bytes, config.run_max_fetch_bytes)
    try:
        root = await _resolve_root(client, journal, config.root_folder_id)
        source = await journal.ensure_source(ref=config.root_folder_id, display_name=root.name)
        ingestor = ingestor_factory(source.id)
        ceiling = await _sweep(config, client, journal, ingestor, lease, source, tally, budget)
        tally.secrets_skipped = ingestor.secrets_skipped
        reported = await _run_github(github, tally, budget)
        await journal.sweep_tombstones(config.tombstone_retention_days)
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
    # The Drive source row keeps its own ceiling; the run row reports whichever
    # half first went short, so a repo nobody could read is not an `ok` run.
    await journal.mark_verified(
        source.id, status="partial" if ceiling else "ok", error_code=ceiling
    )
    run_code = ceiling or reported
    await journal.close_run(
        run_id,
        status="partial" if run_code else "ok",
        counters=counters,
        tally=tally,
        error_code=run_code,
    )
    return counters


async def _run_github(github: GitHubPass | None, tally: Tally, budget: FetchBudget) -> str | None:
    """Off unless configured, and skipped rather than silent when the run is out of budget."""
    if github is None:
        return None
    if budget.exceeded is not None:
        _LOG.warning(GITHUB_SKIPPED)
        return None
    return await github(tally)


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
    budget: FetchBudget,
) -> str | None:
    """The full walk or the changes replay. Answers the ceiling that truncated it."""
    if source.cursor is None:
        return await _walk(config, client, journal, ingestor, lease, source, tally, budget)
    return await _replay(
        config, client, journal, ingestor, lease, source, source.cursor, tally, budget
    )


async def _walk(
    config: SyncConfig,
    client: DriveClient,
    journal: SyncJournal,
    ingestor: Ingestor,
    lease: SyncLease,
    source: SourceState,
    tally: Tally,
    budget: FetchBudget,
) -> str | None:
    """First run: take the cursor before the walk, so nothing between the two is lost."""
    cursor = await client.start_cursor()
    walked = 0
    async for item in client.walk_subtree():
        walked += 1
        await _ingest_one(item, client=client, ingestor=ingestor, tally=tally, budget=budget)
        if walked % LEASE_RENEW_EVERY == 0:
            await _renew(lease)
        ceiling = budget.exceeded
        if ceiling is not None:
            await _renew(lease)
            return _truncated(ceiling, walked, budget)
    await _renew(lease)
    if client.walk_truncated:
        # No cursor: storing one would strand what the walk never reached.
        _LOG.warning(
            "walk truncated at ceiling=%d after %d documents", config.max_documents_per_run, walked
        )
        return SOURCE_CEILING
    await journal.advance_cursor(source.id, cursor)
    return None


def _truncated(ceiling: str, applied: int, budget: FetchBudget) -> str:
    """A ceiling that stopped a pass names itself, or it is an invisible half-mirror."""
    _LOG.warning(
        "%s reached after %d documents and %d fetched bytes; the cursor stayed where it was",
        ceiling,
        applied,
        budget.run_bytes,
    )
    return ceiling


async def _renew(lease: SyncLease) -> None:
    """A lease that will not renew is one another process now holds; stop before writing more."""
    if not await lease.renew():
        raise SyncFailedError(
            "The sync lease was stolen mid-run; the cursor was left where it was.",
            code="lease_lost",
        )


async def _replay(
    config: SyncConfig,
    client: DriveClient,
    journal: SyncJournal,
    ingestor: Ingestor,
    lease: SyncLease,
    source: SourceState,
    cursor: str,
    tally: Tally,
    budget: FetchBudget,
) -> str | None:
    """Later runs: one drained batch from the stored cursor, then advance it."""
    changes, new_cursor = await client.list_changes(cursor)
    applied = 0
    for change in changes:
        # A ceiling reached mid-batch leaves the cursor alone, so the next run
        # replays the same feed rather than skipping what this one never applied.
        ceiling = budget.exceeded or (
            SOURCE_CEILING if applied >= config.max_documents_per_run else None
        )
        if ceiling is not None:
            await _renew(lease)
            return _truncated(ceiling, applied, budget)
        await _apply(change, client=client, ingestor=ingestor, tally=tally, budget=budget)
        applied += 1
    await _renew(lease)
    # Section 10 splits verification from advancement: a poll that came back
    # holding the same token verified the source without moving its cursor, and
    # a `cursor_advanced_at` stamped anyway is a diagnostic that always agrees
    # with `last_verified_at` and so says nothing.
    if new_cursor != cursor:
        await journal.advance_cursor(source.id, new_cursor)
    return None


async def _apply(
    change: DriveChange,
    *,
    client: DriveClient,
    ingestor: Ingestor,
    tally: Tally,
    budget: FetchBudget,
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
            await _ingest_one(item, client=client, ingestor=ingestor, tally=tally, budget=budget)
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
    item: DriveItem,
    *,
    client: DriveClient,
    ingestor: Ingestor,
    tally: Tally,
    budget: FetchBudget,
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
    budget.spend(len(content.data))
    outcome = await ingestor.ingest(drive_source_item(item, content))
    if outcome.refusal_code is not None:
        tally.refuse(outcome.refusal_code)
        _LOG.warning("refused item=%s code=%s", item.id, outcome.refusal_code)
    elif outcome.skipped_unchanged:
        tally.unchanged += 1
    else:
        tally.indexed += 1


def drive_source_item(item: DriveItem, content: FetchedContent) -> SourceItem:
    """One Drive file as the corpus stores it; the citation is the chain from the root."""
    return SourceItem(
        external_id=item.id,
        path="/".join((*item.folder_path, item.name)),
        title=item.name,
        data=content.data,
        media_type=content.media_type,
        source_mime=item.mime_type,
        modified_at=item.modified_time,
        size=item.size,
        deleted=item.trashed,
        shortcut=bool(item.shortcut_target_id),
    )


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
