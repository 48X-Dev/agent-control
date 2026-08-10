"""The per-source status read, framework-free like the rest of this package.

Staleness keys on ``last_verified_at`` and never on cursor movement (plan
section 10), and the two source states that present as success are reported as
``failing`` rather than left as zeros. ``last_failure_code`` stays the sync's
own word for what went wrong, so this module never writes one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_control_models.knowledge_render import neutralize_header_field
from agent_control_models.knowledge_status import KnowledgeSourceKind
from sqlalchemy import Row, bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import KnowledgeSettings, knowledge_settings
from .engine import KnowledgeUnavailableError, knowledge_session
from .store import is_supported_schema, read_schema_version

logger = logging.getLogger(__name__)

__all__ = ["CorpusStatus", "SourceStatus", "read_status"]

# The corpus stores the longer form; 'drive' and 'github' are the wire's words.
_KIND_LABELS: dict[str, KnowledgeSourceKind] = {
    "drive_folder": "drive",
    "github_repo": "github",
}

# Tombstone reasons that are refusals. 'deleted' is upstream lifecycle - the
# file went away - rather than something the sync declined to index.
_REFUSAL_REASONS = ("unshared", "excluded", "oversize", "secret_file")

_SOURCES_SQL = """
SELECT
    s.id                  AS id,
    s.kind                AS kind,
    s.ref                 AS ref,
    s.enabled             AS enabled,
    s.last_verified_at    AS last_verified_at,
    s.cursor_advanced_at  AS cursor_advanced_at,
    s.last_run_status     AS last_run_status,
    s.last_run_error_code AS last_run_error_code,
    (SELECT count(*) FROM documents d
      WHERE d.source_id = s.id
        AND d.tombstoned_at IS NULL
        AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)) AS document_count,
    now() AS observed_at
FROM sources s
ORDER BY s.id
"""

_CHUNKS_SQL = """
SELECT count(*) AS chunk_count
FROM chunks c
JOIN documents d ON d.id = c.document_id
JOIN sources s ON s.id = d.source_id
WHERE s.enabled AND d.tombstoned_at IS NULL
"""

_REFUSALS_SQL = """
SELECT d.source_id AS source_id, d.tombstone_reason AS reason, count(*) AS refusals
FROM documents d
WHERE d.tombstoned_at IS NOT NULL
  AND d.tombstone_reason IN :reasons
GROUP BY d.source_id, d.tombstone_reason
"""


@dataclass(frozen=True)
class SourceStatus:
    """One source's freshness, reach and failure state."""

    source_id: str
    kind: KnowledgeSourceKind
    enabled: bool
    last_verified_at: datetime | None
    cursor_advanced_at: datetime | None
    stale_seconds: int | None
    document_count: int
    failing: bool
    last_failure_code: str | None
    refusals_by_code: dict[str, int]


@dataclass(frozen=True)
class CorpusStatus:
    """The mirror overall, and every source under it.

    ``schema_supported`` false means the counts are zeros this module wrote
    rather than zeros it counted.
    """

    schema_version: int | None
    schema_supported: bool
    document_count: int
    chunk_count: int
    stale_seconds: int | None
    sources_failing: int
    sources: tuple[SourceStatus, ...]


async def read_status(settings: KnowledgeSettings | None = None) -> CorpusStatus:
    """The mirror's account of itself, or a stated inability to read it.

    Never raises: off, unreachable and unreadable all answer
    ``schema_supported`` false, because reporting a broken corpus is the job.
    """
    resolved = settings or knowledge_settings
    try:
        async with knowledge_session(resolved) as session:
            version = await read_schema_version(session)
            # The engine asserts this once per pool, so this catches the case
            # it cannot: a sync migrating under a pool that is already open.
            if not is_supported_schema(version):
                logger.warning(
                    "Knowledge corpus reports schema version %s, which this "
                    "server does not read; status reported unsupported.",
                    version,
                )
                return _unreadable(version)
            return await _read(session, version)
    except KnowledgeUnavailableError:
        return _unreadable(None)


def _unreadable(version: int | None) -> CorpusStatus:
    return CorpusStatus(
        schema_version=version,
        schema_supported=False,
        document_count=0,
        chunk_count=0,
        stale_seconds=None,
        sources_failing=0,
        sources=(),
    )


async def _read(session: AsyncSession, version: int | None) -> CorpusStatus:
    refusals = await _refusals_by_source(session)
    rows = (await session.execute(text(_SOURCES_SQL))).all()
    chunk_count = int((await session.execute(text(_CHUNKS_SQL))).scalar_one())

    unknown = {str(row.kind) for row in rows} - set(_KIND_LABELS)
    if unknown:
        logger.warning(
            "Knowledge corpus carries source kinds this server does not know: %s",
            sorted(unknown),
        )
        return _unreadable(version)

    sources = tuple(_source(row, refusals.get(int(row.id), {})) for row in rows)
    enabled = [source for source in sources if source.enabled]
    return CorpusStatus(
        schema_version=version,
        schema_supported=True,
        document_count=sum(source.document_count for source in enabled),
        chunk_count=chunk_count,
        stale_seconds=_corpus_stale_seconds(enabled),
        sources_failing=sum(1 for source in sources if source.failing),
        sources=sources,
    )


async def _refusals_by_source(session: AsyncSession) -> dict[int, dict[str, int]]:
    statement = text(_REFUSALS_SQL).bindparams(bindparam("reasons", expanding=True))
    rows = await session.execute(statement, {"reasons": list(_REFUSAL_REASONS)})
    counted: dict[int, dict[str, int]] = {}
    for row in rows:
        counted.setdefault(int(row.source_id), {})[str(row.reason)] = int(row.refusals)
    return counted


def _source(row: Row[Any], refusals: dict[str, int]) -> SourceStatus:
    document_count = int(row.document_count)
    enabled = bool(row.enabled)
    failing, code = _failure_state(row, enabled=enabled, document_count=document_count)
    return SourceStatus(
        source_id=neutralize_header_field(str(row.ref)) or str(row.id),
        kind=_KIND_LABELS[str(row.kind)],
        enabled=enabled,
        last_verified_at=row.last_verified_at,
        cursor_advanced_at=row.cursor_advanced_at,
        stale_seconds=_age(row.observed_at, row.last_verified_at),
        document_count=document_count,
        failing=failing,
        last_failure_code=code,
        refusals_by_code=refusals,
    )


def _failure_state(
    row: Row[Any], *, enabled: bool, document_count: int
) -> tuple[bool, str | None]:
    """Two independent facts: whether to act, and what the sync recorded.

    Nothing is invented for the code. An enabled source holding nothing needs
    attention and has no code, because the sync recorded none; a run that
    finished carrying one reports it without ``failing``, because a run that
    finished is not a source that stopped.
    """
    failing = row.last_run_status == "failed" or (enabled and document_count == 0)
    recorded = row.last_run_error_code
    return failing, str(recorded) if recorded else None


def _corpus_stale_seconds(enabled: Sequence[SourceStatus]) -> int | None:
    """The oldest enabled source's age, or None when one has never verified."""
    ages = [source.stale_seconds for source in enabled]
    if not ages or any(age is None for age in ages):
        return None
    return max(age for age in ages if age is not None)


def _age(observed_at: datetime, verified_at: datetime | None) -> int | None:
    if verified_at is None:
        return None
    return max(0, int((observed_at - verified_at).total_seconds()))
