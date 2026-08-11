"""The corpus rows one run owns: its ``sync_runs`` entry and its source's cursor."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .lease import SessionFactory
from .schema import SUPPORTED_SCHEMA_VERSIONS, documents, schema_meta, sources, sync_runs

# 4.2 spells the Drive cursor {"start_page_token": ...}, and the status endpoint
# and the console panel both read this column by that documented name. GitHub's
# pair lives on `github_source`, which is where the sweep that mints it lives.
DRIVE_SOURCE_KIND = "drive_folder"
DRIVE_CURSOR_KEY = "start_page_token"

_LOG = logging.getLogger(__name__)


class SyncFailedError(RuntimeError):
    """A run that could not complete, carrying the code ``sync_runs`` records."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RunCounters:
    """What one pass did, in the shape ``once`` prints and ``sync_runs`` stores."""

    seen: int
    indexed: int
    unchanged: int
    tombstoned: int
    refused: int
    refusals_by_code: dict[str, int]


@dataclass(slots=True)
class Tally:
    """The mutable accumulator behind :class:`RunCounters`."""

    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    tombstoned: int = 0
    bytes_fetched: int = 0
    secrets_skipped: int = 0
    refusals: Counter[str] = field(default_factory=Counter)

    def refuse(self, code: str) -> None:
        self.refusals[code] += 1

    def freeze(self) -> RunCounters:
        return RunCounters(
            seen=self.seen,
            indexed=self.indexed,
            unchanged=self.unchanged,
            tombstoned=self.tombstoned,
            refused=sum(self.refusals.values()),
            refusals_by_code=dict(self.refusals),
        )


@dataclass(frozen=True, slots=True)
class SourceState:
    """The corpus row this run walks, and the cursor it left behind last time."""

    id: int
    cursor: str | None


def _cursor_json(cursor_key: str) -> sa.Function[str]:
    return sa.func.jsonb_build_object(
        sa.cast(sa.literal(cursor_key), sa.Text), sa.cast(sa.bindparam("cursor"), sa.Text)
    )


def _ensure_source_statement(kind: str, cursor_key: str) -> sa.Executable:
    insert = pg_insert(sources).values(
        kind=kind,
        ref=sa.bindparam("ref"),
        display_name=sa.bindparam("display_name"),
        trust="workspace",
    )
    return insert.on_conflict_do_update(
        index_elements=[sources.c.kind, sources.c.ref],
        set_={"display_name": insert.excluded.display_name},
    ).returning(sources.c.id, sources.c.cursor[cursor_key].astext.label("cursor"))


class SyncJournal:
    """The corpus rows a run owns, for one ``sources.kind`` and the cursor it stores."""

    def __init__(
        self,
        sessions: SessionFactory,
        *,
        kind: str = DRIVE_SOURCE_KIND,
        cursor_key: str = DRIVE_CURSOR_KEY,
    ) -> None:
        self._sessions = sessions
        self._kind = kind
        self._cursor_json = _cursor_json(cursor_key)
        self._ensure_source = _ensure_source_statement(kind, cursor_key)

    async def assert_schema(self) -> int:
        """Refuse a corpus this build does not know how to write."""
        async with self._sessions() as session:
            row = (
                await session.execute(sa.select(schema_meta.c.version).where(schema_meta.c.id == 1))
            ).first()
        version = None if row is None else int(row.version)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise SyncFailedError(
                f"The corpus reports schema version {version}; this sync writes "
                f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}. Run the migrations first.",
                code="schema_unsupported",
            )
        return version

    async def lapse_orphans(self, holder: str) -> int:
        """Close a ``running`` row whose process died; fenced on the holder, never our own."""
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.update(sync_runs)
                    .where(sync_runs.c.status == "running", sync_runs.c.holder != holder)
                    .values(status="lapsed", finished_at=sa.func.now())
                    .returning(sync_runs.c.id)
                )
            ).all()
            await session.commit()
        if rows:
            _LOG.warning("lapsed %d orphaned run row(s)", len(rows))
        return len(rows)

    async def open_run(self, holder: str) -> int:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    sa.insert(sync_runs)
                    .values(
                        holder=holder,
                        started_at=sa.func.now(),
                        status="running",
                        files_seen=0,
                        files_converted=0,
                        files_failed=0,
                        files_skipped=0,
                        secrets_skipped=0,
                        bytes_fetched=0,
                    )
                    .returning(sync_runs.c.id)
                )
            ).one()
            await session.commit()
        return int(row.id)

    async def close_run(
        self,
        run_id: int,
        *,
        status: str,
        counters: RunCounters,
        tally: Tally,
        error_code: str | None,
    ) -> None:
        async with self._sessions() as session:
            await session.execute(
                sa.update(sync_runs)
                .where(sync_runs.c.id == run_id)
                .values(
                    finished_at=sa.func.now(),
                    status=status,
                    files_seen=counters.seen,
                    files_converted=counters.indexed,
                    files_failed=counters.refused,
                    files_skipped=counters.unchanged,
                    secrets_skipped=tally.secrets_skipped,
                    bytes_fetched=tally.bytes_fetched,
                    error_code=error_code,
                )
            )
            await session.commit()

    async def ensure_source(self, *, ref: str, display_name: str) -> SourceState:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    self._ensure_source, {"ref": ref, "display_name": display_name}
                )
            ).one()
            await session.commit()
        return SourceState(id=int(row.id), cursor=row.cursor)

    async def advance_cursor(self, source_id: int, cursor: str) -> None:
        """Called only after the batch it belongs to has committed."""
        async with self._sessions() as session:
            await session.execute(
                sa.update(sources)
                .where(sources.c.id == sa.bindparam("source_id"))
                .values(cursor=self._cursor_json, cursor_advanced_at=sa.func.now()),
                {"source_id": source_id, "cursor": cursor},
            )
            await session.commit()

    async def sweep_tombstones(self, retention_days: int) -> int:
        """4.4: a tombstone past its window is deleted, and the count is the point."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self._sessions() as session:
            rows = (
                await session.execute(
                    sa.delete(documents)
                    .where(documents.c.tombstoned_at < cutoff)
                    .returning(documents.c.id)
                )
            ).all()
            await session.commit()
        if rows:
            _LOG.info("deleted %d tombstone(s) older than %d days", len(rows), retention_days)
        return len(rows)

    async def mark_verified(self, source_id: int, *, status: str, error_code: str | None) -> None:
        """Section 10: every completed check stamps this, zero-change runs included."""
        async with self._sessions() as session:
            await session.execute(
                sa.update(sources)
                .where(sources.c.id == source_id)
                .values(
                    last_verified_at=sa.func.now(),
                    last_run_status=status,
                    last_run_error_code=error_code,
                )
            )
            await session.commit()

    async def mark_source_failed(self, *, ref: str, error_code: str) -> None:
        """For a failure before the source row is known; a no-op on a first run."""
        async with self._sessions() as session:
            await session.execute(
                sa.update(sources)
                .where(sources.c.kind == self._kind, sources.c.ref == ref)
                .values(last_run_status="failed", last_run_error_code=error_code)
            )
            await session.commit()
