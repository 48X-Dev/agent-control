"""Retrieval, governed: bounds, ceilings, metering, and the fields defused.

The endpoint is a thin caller of this module, and so is anything that comes
later - a console panel, an MCP surface, a script. Nothing here imports FastAPI
or touches a request object, because the moment retrieval policy lives in a
route handler there are two policies the day a second surface appears.

The order of the checks is the design, cheapest and most-certain first:

1. **Off** answers ``knowledge_disabled`` without opening a connection.
2. **Query bounds** answer typed, before any metering. A two-character query
   costs nothing to refuse and the refusal tells a model how to fix itself;
   spending a search from the window on it would be spending the ceiling on
   the one call that was never going to reach the database.
3. **The window** answers ``rate_limited`` with the seconds until it is worth
   asking again.
4. **The corpus** answers ``knowledge_unavailable`` when it cannot be reached,
   ``corpus_empty`` when there is nothing in it, and results otherwise.

"Nothing matched" is deliberately **not** a refusal code. Found-nothing and
could-not-look are different facts, and an operator debugging "search finds
nothing" needs to be told which one they have.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from agent_control_models.knowledge import KnowledgeRefusalCode
from agent_control_models.knowledge_render import (
    neutralize,
    neutralize_header_field,
)
from agent_control_models.knowledge_search import (
    QUERY_MAX_CHARS,
    QUERY_MIN_CHARS,
    KnowledgeCorpus,
    KnowledgeSearchResponse,
    KnowledgeSnippet,
)

from ..config import KnowledgeSettings, knowledge_settings
from ..knowledge.engine import KnowledgeUnavailableError, knowledge_session
from ..knowledge.store import (
    CorpusStats,
    SnippetRow,
    any_of,
    corpus_stats,
    recent_documents,
    search_chunks,
    search_chunks_trigram,
)
from . import knowledge_quota

logger = logging.getLogger(__name__)

WORKSPACE_AUTHOR = "workspace"
"""The only authorship that does not count towards ``external_author_count``.

'unknown' counts as external, and that direction is the whole point: the count
exists for a deny control, and reading "the sync could not establish who wrote
this" as "safe" would be failing open in the one field built to fail closed."""


async def search(
    *,
    namespace_key: str,
    session_key: str,
    meter_key: str | None = None,
    query: str,
    max_results: int | None = None,
    settings: KnowledgeSettings | None = None,
) -> KnowledgeSearchResponse:
    """Rank the corpus against a query, k-capped, metered and defused.

    ``session_key`` names the session for the log line. ``meter_key`` is what
    the window spends, and the two are separate because only one of them is
    ever verified: see ``_meter``.
    """

    resolved = settings or knowledge_settings
    if not resolved.enabled:
        return _refused(KnowledgeRefusalCode.KNOWLEDGE_DISABLED, settings=resolved)

    cleaned = query.strip()
    if len(cleaned) < QUERY_MIN_CHARS:
        return _refused(KnowledgeRefusalCode.QUERY_TOO_SHORT, settings=resolved)
    if len(cleaned) > QUERY_MAX_CHARS:
        return _refused(KnowledgeRefusalCode.QUERY_TOO_LONG, settings=resolved)

    limited = _meter(namespace_key=namespace_key, meter_key=meter_key, settings=resolved)
    if limited is not None:
        return limited

    limit = _clamped_limit(max_results, resolved)
    logger.debug("Knowledge search on session %s: %r", session_key, cleaned)
    try:
        async with knowledge_session(resolved) as session:
            stats = await corpus_stats(session)
            if stats.documents == 0:
                return _refused(
                    KnowledgeRefusalCode.CORPUS_EMPTY, stats=stats, settings=resolved
                )
            rows = await search_chunks(
                session,
                query=cleaned,
                limit=limit,
                snippet_max_chars=resolved.snippet_max_chars,
            )
            if not rows:
                # An agent asks in a sentence and websearch_to_tsquery ANDs, so
                # every word has to land in one chunk. Broaden before reaching
                # for trigram, which needs word similarity a long phrase never has.
                broadened = any_of(cleaned)
                if broadened:
                    rows = await search_chunks(
                        session,
                        query=broadened,
                        limit=limit,
                        snippet_max_chars=resolved.snippet_max_chars,
                    )
            if not rows:
                # Only when both found nothing, which is where the misspellings
                # and the code-name fragments live.
                rows = await search_chunks_trigram(
                    session,
                    query=cleaned,
                    limit=limit,
                    snippet_max_chars=resolved.snippet_max_chars,
                )
            return _answered(rows, stats, resolved)
    except KnowledgeUnavailableError as exc:
        return _refused(_refusal_code(exc), settings=resolved)


async def recent(
    *,
    namespace_key: str,
    session_key: str,
    meter_key: str | None = None,
    days: int | None = None,
    max_results: int | None = None,
    settings: KnowledgeSettings | None = None,
) -> KnowledgeSearchResponse:
    """What moved inside a capped window, newest first, one page and no cursor.

    The third verb, because ranked search only answers what *is* and an agent
    asked what is going on needs what changed. It is not the enumeration search
    refuses: the window and the k-cap bound it, asking again returns the same
    page, and there is no parameter that would advance it.
    """

    resolved = settings or knowledge_settings
    if not resolved.enabled:
        return _refused(KnowledgeRefusalCode.KNOWLEDGE_DISABLED, settings=resolved)

    limited = _meter(namespace_key=namespace_key, meter_key=meter_key, settings=resolved)
    if limited is not None:
        return limited

    window = _clamped_days(days, resolved)
    limit = _clamped_limit(max_results, resolved)
    try:
        async with knowledge_session(resolved) as session:
            stats = await corpus_stats(session)
            if stats.documents == 0:
                return _refused(
                    KnowledgeRefusalCode.CORPUS_EMPTY, stats=stats, settings=resolved
                )
            rows = await recent_documents(
                session,
                days=window,
                limit=limit,
                snippet_max_chars=resolved.snippet_max_chars,
            )
            return _answered(rows, stats, resolved)
    except KnowledgeUnavailableError as exc:
        return _refused(_refusal_code(exc), settings=resolved)


def _meter(
    *,
    namespace_key: str,
    meter_key: str | None,
    settings: KnowledgeSettings,
) -> KnowledgeSearchResponse | None:
    """Spend one search from the window, or say when the window reopens.

    ``meter_key`` is a binding the server verified, or ``None``. It is never
    the session key out of the path: under both providers that do not verify a
    binding, the path segment is a string the caller typed, and a window keyed
    on it is one a loop refreshes by counting upwards. ``None`` puts every such
    caller in one namespace-wide bucket, which is the direction plan 8.1 asks
    for and the direction ``turn_quota`` already takes for the same reason.
    """
    wait = knowledge_quota.try_acquire(
        namespace_key=namespace_key,
        meter_key=meter_key,
        max_per_minute=settings.searches_per_minute,
    )
    if wait is None:
        return None
    return _refused(
        KnowledgeRefusalCode.RATE_LIMITED,
        settings=settings,
        retry_after_seconds=max(1, int(wait) + 1),
    )


def _clamped_limit(requested: int | None, settings: KnowledgeSettings) -> int:
    """k, never above the configured cap. Clamped rather than refused.

    A model that asked for twelve results gets five and a sentence built from
    what it received; refusing the call outright would spend a turn teaching it
    a number it could have been handed.
    """
    if requested is None:
        return settings.search_max_results
    return max(1, min(requested, settings.search_max_results))


def _clamped_days(requested: int | None, settings: KnowledgeSettings) -> int:
    if requested is None:
        return settings.recent_window_days_max
    return max(1, min(requested, settings.recent_window_days_max))


def _refusal_code(exc: KnowledgeUnavailableError) -> str:
    """The engine's own code, or the safe one if it ever carries something else."""
    try:
        return str(KnowledgeRefusalCode(exc.code))
    except ValueError:
        logger.warning("Knowledge engine raised an unmapped code; refusing unavailable")
        return KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE


def _refused(
    code: str,
    *,
    settings: KnowledgeSettings,
    stats: CorpusStats | None = None,
    retry_after_seconds: int | None = None,
) -> KnowledgeSearchResponse:
    """A refusal, carrying both counters, because the deny control fails closed.

    Zero results and zero external authors are true statements about this
    response. What matters is that the fields are *present*: the shipped deny
    control selects the whole object and constrains a named key, so a missing
    key is an error the evaluator reports as a match. Omitting them here would
    make a refusal deny and an ordinary answer pass, which is backwards.
    """
    return KnowledgeSearchResponse(
        results=[],
        result_count=0,
        external_author_count=0,
        corpus=_corpus(stats, settings),
        refusal_code=KnowledgeRefusalCode(code),
        retry_after_seconds=retry_after_seconds,
    )


def _answered(
    rows: Sequence[SnippetRow], stats: CorpusStats, settings: KnowledgeSettings
) -> KnowledgeSearchResponse:
    results = [_snippet(row) for row in rows]
    return KnowledgeSearchResponse(
        results=results,
        result_count=len(results),
        external_author_count=sum(
            1 for row in rows if row.author_kind != WORKSPACE_AUTHOR
        ),
        corpus=_corpus(stats, settings),
        refusal_code=None,
    )


def _snippet(row: SnippetRow) -> KnowledgeSnippet:
    """One row, with every string a document or a filename could have chosen.

    Neutralized here, once, before the response leaves the server: the tool
    renders these fields into a fence header, the console renders them as text
    nodes, and a name that could forge a header must be inert for both. The
    body gets the same treatment as the names, because a document's text is
    exactly as attacker-authored as its title.
    """
    return KnowledgeSnippet(
        snippet=neutralize(row.snippet),
        path=neutralize_header_field(row.path) or "unknown",
        heading_path=neutralize_header_field(row.heading_path),
        title=neutralize_header_field(row.title) or "unknown",
        source_kind=row.source_kind,
        source_name=neutralize_header_field(row.source_name) or "unknown",
        author_kind=row.author_kind,
        modified_at=row.modified_at,
        synced_at=row.synced_at,
    )


def _corpus(stats: CorpusStats | None, settings: KnowledgeSettings) -> KnowledgeCorpus:
    """The counters, and whether anybody counted them.

    ``measured`` is set here and only here, from the one thing that decides it:
    whether the store was opened. A caller that passes stats has read the
    corpus; a caller that does not was refused before reaching it, and the
    defaults it gets back are placeholders no reader should print as a reading.
    """
    if stats is None:
        return KnowledgeCorpus()
    return KnowledgeCorpus(
        documents=stats.documents,
        sources=stats.sources_enabled,
        sources_failing=stats.sources_failing,
        last_sync_at=stats.last_sync_at,
        stale_seconds=stats.stale_seconds,
        measured=True,
        staleness_warn_seconds=settings.staleness_warn_seconds,
    )
