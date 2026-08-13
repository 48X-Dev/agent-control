"""The queries retrieval runs, and nothing else.

Framework-free on purpose: the endpoint, the console panel and the MCP surface
are three thin callers of this one core, and a store that imported FastAPI
would make two of them impossible. Core SQL rather than ORM entities, so
nothing lazy-loads on attribute access after the session has closed.

Every caller-supplied string is a bound parameter. The query text descends from
a task body written by whoever files issues, and the only thing it is ever
allowed to do is parameterize a SELECT: no flag, no sync, no fetch hangs off
it. ``websearch_to_tsquery`` parses arbitrary input without raising, which is
why it and not ``to_tsquery`` is the entry point.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_control_models.knowledge_render import truncate_snippet
from agent_control_models.knowledge_search import QUERY_MAX_CHARS, QUERY_MIN_CHARS
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from .schema import SUPPORTED_SCHEMA_VERSIONS

# Re-exported: the bounds are the wire contract, so they live in the shared
# models package where the tool can refuse a bad query without a round trip,
# and a second copy here would be a second copy to drift.
__all__ = [
    "QUERY_MAX_CHARS",
    "QUERY_MIN_CHARS",
    "CorpusStats",
    "SnippetRow",
    "corpus_stats",
    "is_supported_schema",
    "read_schema_version",
    "recent_documents",
    "search_chunks",
    "search_chunks_trigram",
    "any_of",
]


_TSQUERY_KEYWORDS = frozenset({"or", "and", "not"})


def any_of(query: str) -> str | None:
    """The same query with its words OR'd, or ``None`` when that would change nothing.

    ``websearch_to_tsquery`` ANDs, so a question phrased as a sentence needs every
    word in one chunk and matches nothing. Still its own input, so it still parses.
    """
    words = [w for w in re.findall(r"[\w-]+", query) if w.lower() not in _TSQUERY_KEYWORDS]
    return " OR ".join(words) if len(words) > 1 else None

# How far past k to look before collapsing duplicates. Two copies of the same
# paragraph reachable from two sources must not spend two of the k slots, and
# the collapse happens after ranking, so the fetch has to have somewhere to
# collapse from. Bounded, because an unbounded over-fetch is the enumeration
# this design refuses wearing a performance hat.
_OVERFETCH_FACTOR = 4
_OVERFETCH_CEILING = 32

# pg_trgm's own default. K6 owns the real number: it is measured against a
# corpus, not chosen at a desk.
TRIGRAM_THRESHOLD_DEFAULT = 0.5

# No markup in a snippet. It travels to a model as plain text and renders in
# the console as a text node, so highlight tags would be noise in one place and
# something a plain-text rule has to strip in the other.
_HEADLINE_OPTIONS = (
    'MaxFragments=2, MaxWords=40, MinWords=15, StartSel="", StopSel="", '
    'FragmentDelimiter=" ... "'
)

_SNIPPET_COLUMNS = """
    d.path                AS path,
    c.heading_path        AS heading_path,
    d.title               AS title,
    s.kind                AS source_kind,
    s.display_name        AS source_name,
    d.author_kind         AS author_kind,
    s.trust               AS trust,
    d.source_modified_at  AS modified_at,
    d.synced_at           AS synced_at,
    d.content_sha256      AS content_sha256,
    c.ordinal             AS ordinal
"""

# The curated rewrite, applied to the parsed query and not to the caller's
# string. Full-text search misses synonymy outright - "laptop policy" against a
# document that only says "hardware provisioning" - and before embeddings the
# cheapest real answer is twenty rows of the company's own vocabulary. It is
# operator-curated configuration: the sync never writes this table, and every
# rewrite is inspectable in a way a vector similarity is not. An empty table
# leaves the query exactly as it arrived.
_REWRITE = "SELECT source_query, target_query FROM synonyms"

_SEARCH_SQL = f"""
WITH q AS (
    SELECT ts_rewrite(websearch_to_tsquery('english', :query), '{_REWRITE}') AS tsq
)
SELECT
    ts_headline('english', c.body, q.tsq, '{_HEADLINE_OPTIONS}') AS snippet,
    ts_rank(c.body_tsv, q.tsq) AS rank,
{_SNIPPET_COLUMNS}
FROM chunks c
JOIN documents d ON d.id = c.document_id
JOIN sources s ON s.id = d.source_id
CROSS JOIN q
WHERE s.enabled
  AND d.tombstoned_at IS NULL
  AND c.body_tsv @@ q.tsq
ORDER BY rank DESC, (s.trust = 'workspace') DESC, d.source_modified_at DESC NULLS LAST, c.id
LIMIT :fetch_limit
"""

# The casts are load-bearing: an untyped bind parameter beside `<%` leaves
# Postgres resolving `unknown <% text` and refusing the operator outright.
# One character past the ceiling, so ``_collapse`` can tell a body that ended
# from a body that was cut and say which one it is handing back.
_TRIGRAM_SQL = f"""
SELECT
    left(c.body, :snippet_chars + 1) AS snippet,
    word_similarity(cast(:query AS text), c.body) AS rank,
{_SNIPPET_COLUMNS}
FROM chunks c
JOIN documents d ON d.id = c.document_id
JOIN sources s ON s.id = d.source_id
WHERE s.enabled
  AND d.tombstoned_at IS NULL
  AND cast(:query AS text) <% c.body
ORDER BY rank DESC, (s.trust = 'workspace') DESC, d.source_modified_at DESC NULLS LAST, c.id
LIMIT :fetch_limit
"""

_RECENT_SQL = f"""
SELECT
    c.snippet AS snippet,
    0.0::float8 AS rank,
{_SNIPPET_COLUMNS}
FROM documents d
JOIN sources s ON s.id = d.source_id
JOIN LATERAL (
    SELECT ch.heading_path, ch.ordinal, left(ch.body, :snippet_chars + 1) AS snippet
      FROM chunks ch
     WHERE ch.document_id = d.id
     ORDER BY ch.ordinal
     LIMIT 1
) c ON true
WHERE s.enabled
  AND d.tombstoned_at IS NULL
  AND d.source_modified_at IS NOT NULL
  AND d.source_modified_at >= now() - make_interval(days => :days)
ORDER BY d.source_modified_at DESC, d.id
LIMIT :fetch_limit
"""

_STATS_SQL = """
SELECT
    (SELECT count(*) FROM documents d
      WHERE d.tombstoned_at IS NULL
        AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)
        AND EXISTS (SELECT 1 FROM sources s
                     WHERE s.id = d.source_id AND s.enabled)) AS documents,
    (SELECT count(*) FROM sources WHERE enabled) AS sources_enabled,
    (SELECT count(*) FROM sources
      WHERE enabled AND last_run_status = 'failed') AS sources_failing,
    (SELECT max(last_verified_at) FROM sources WHERE enabled) AS last_sync_at,
    (SELECT min(last_verified_at) FROM sources WHERE enabled) AS oldest_verified_at,
    (SELECT count(*) FROM sources
      WHERE enabled AND last_verified_at IS NULL) AS never_verified,
    now() AS observed_at
"""


@dataclass(frozen=True)
class SnippetRow:
    """One result, with the provenance that makes it checkable by a human."""

    snippet: str
    path: str
    heading_path: str | None
    title: str
    source_kind: str
    source_name: str
    author_kind: str
    trust: str
    modified_at: datetime | None
    synced_at: datetime
    content_sha256: str
    ordinal: int
    rank: float


@dataclass(frozen=True)
class CorpusStats:
    """What every response says about the mirror, refusals included.

    ``documents`` counts what is actually *searchable*, which means the same
    three conditions every search query applies: a live document, holding at
    least one chunk, on an enabled source. A document whose conversion failed
    keeps its row and has no chunks, and a switched-off source keeps all of
    them; counting either here would let an agent be told the base holds
    something nobody can read, then find nothing, with no way to tell "not in
    the corpus" from "the source is off".

    ``stale_seconds`` is ``None`` when it cannot be computed rather than zero,
    because an enabled source that has never verified is not a fresh source.
    It keys on ``last_verified_at`` and not on cursor movement: a repo with no
    new commits produces no batch, and a staleness clock keyed on cursor
    advancement would warn forever on a healthy deployment.
    """

    documents: int
    sources_enabled: int
    sources_failing: int
    last_sync_at: datetime | None
    stale_seconds: int | None


async def read_schema_version(session: AsyncSession) -> int | None:
    """The corpus schema version, or ``None`` when the marker row is absent."""
    result = await session.execute(text("SELECT version FROM schema_meta WHERE id = 1"))
    row = result.first()
    return None if row is None else int(row[0])


def is_supported_schema(version: int | None) -> bool:
    """Whether this server knows how to read rows of that shape."""
    return version in SUPPORTED_SCHEMA_VERSIONS


async def corpus_stats(session: AsyncSession) -> CorpusStats:
    """The corpus counters that ride along with every response."""
    row = (await session.execute(text(_STATS_SQL))).one()
    oldest = row.oldest_verified_at
    stale_seconds: int | None = None
    if oldest is not None and not row.never_verified:
        stale_seconds = max(0, int((row.observed_at - oldest).total_seconds()))
    return CorpusStats(
        documents=int(row.documents),
        sources_enabled=int(row.sources_enabled),
        sources_failing=int(row.sources_failing),
        last_sync_at=row.last_sync_at,
        stale_seconds=stale_seconds,
    )


async def search_chunks(
    session: AsyncSession,
    *,
    query: str,
    limit: int,
    snippet_max_chars: int,
) -> list[SnippetRow]:
    """Rank chunks against a query, k-capped and duplicate-collapsed.

    There is no offset and no cursor, here or anywhere else in this module.
    Ranked search with a hard k is the whole of an agent's reach into the
    corpus; wholesale export is exfiltration shaped like a feature, and the
    parameter that would enable it is the one that does not exist.
    """
    rows = await session.execute(
        text(_SEARCH_SQL),
        {"query": query, "fetch_limit": _fetch_limit(limit)},
    )
    return _collapse(rows.fetchall(), limit=limit, snippet_max_chars=snippet_max_chars)


async def search_chunks_trigram(
    session: AsyncSession,
    *,
    query: str,
    limit: int,
    snippet_max_chars: int,
    threshold: float = TRIGRAM_THRESHOLD_DEFAULT,
) -> list[SnippetRow]:
    """The fallback for the queries full-text search has nothing to say about.

    Misspellings and code-name fragments ("ACME-7") return no lexeme match at
    all, and an empty result there is a bad answer rather than an honest one.
    ``word_similarity`` and not ``similarity``: the query is short and the body
    is long, so what matters is whether the query's trigrams sit inside some
    continuous extent of the body, which is also the form the GIN index serves.
    """
    await session.execute(
        text("SELECT set_config('pg_trgm.word_similarity_threshold', :threshold, true)"),
        {"threshold": str(threshold)},
    )
    rows = await session.execute(
        text(_TRIGRAM_SQL),
        {
            "query": query,
            "fetch_limit": _fetch_limit(limit),
            "snippet_chars": snippet_max_chars,
        },
    )
    return _collapse(rows.fetchall(), limit=limit, snippet_max_chars=snippet_max_chars)


async def recent_documents(
    session: AsyncSession,
    *,
    days: int,
    limit: int,
    snippet_max_chars: int,
) -> list[SnippetRow]:
    """What moved inside a bounded window, newest first, one page only.

    Not the enumeration section 8.4 refuses: the window and the k-cap bound it
    to one page of a fortnight, there is no cursor, and calling it again
    returns the same page, so a loop gains nothing. Documents with no chunks
    are absent for the reason failed conversions are absent from search - an
    agent must not cite a document nobody can read.
    """
    rows = await session.execute(
        text(_RECENT_SQL),
        {
            "days": days,
            "fetch_limit": _fetch_limit(limit),
            "snippet_chars": snippet_max_chars,
        },
    )
    return _collapse(rows.fetchall(), limit=limit, snippet_max_chars=snippet_max_chars)


def _fetch_limit(limit: int) -> int:
    return min(max(limit, 1) * _OVERFETCH_FACTOR, _OVERFETCH_CEILING)


def _collapse(
    rows: Sequence[Row[Any]],
    *,
    limit: int,
    snippet_max_chars: int,
) -> list[SnippetRow]:
    """Drop duplicate paragraphs, then cut to k.

    The same deck reachable from two Drive folders is two documents on purpose:
    provenance is the product and the copies differ in path, mtime and possibly
    trust. What must not happen is spending two of k's slots showing an agent
    the same paragraph twice, so identical ``(content_sha256, ordinal)`` pairs
    collapse to the higher-ranked one. Nothing here decides which copy is
    canonical, because nothing here can.
    """
    seen: set[tuple[str, int]] = set()
    collapsed: list[SnippetRow] = []
    for row in rows:
        key = (row.content_sha256, int(row.ordinal))
        if key in seen:
            continue
        seen.add(key)
        collapsed.append(
            SnippetRow(
                snippet=truncate_snippet(row.snippet or "", snippet_max_chars),
                path=row.path,
                heading_path=row.heading_path,
                title=row.title,
                source_kind=row.source_kind,
                source_name=row.source_name,
                author_kind=row.author_kind,
                trust=row.trust,
                modified_at=row.modified_at,
                synced_at=row.synced_at,
                content_sha256=row.content_sha256,
                ordinal=int(row.ordinal),
                rank=float(row.rank),
            )
        )
        if len(collapsed) >= limit:
            break
    return collapsed
