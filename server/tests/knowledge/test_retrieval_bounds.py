"""The bounds on an agent's reach, and the parameter that does not exist.

Section 8.4 is a promise about what cannot be built out of this module: an
agent reaches the corpus through ranked search with a hard k, and wholesale
export is exfiltration shaped like a feature. A promise about absence is only
worth what the check on it is worth, so the first test here reads the module's
own signatures rather than trusting its docstring.

The rest bounds the things that do exist. Over-fetch, snippets, the recency
window, the fallback's threshold: each one is a number that would quietly widen
under feature pressure, and a widened number is not a bug anybody notices from
a stack trace.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from agent_control_server.knowledge import (
    knowledge_session,
    recent_documents,
    search_chunks,
    search_chunks_trigram,
)
from agent_control_server.knowledge import store as store_module
from agent_control_server.knowledge.seed import SeedDocument
from agent_control_server.knowledge.store import (
    _OVERFETCH_CEILING,
    QUERY_MAX_CHARS,
    QUERY_MIN_CHARS,
    _fetch_limit,
)
from sqlalchemy.pool import NullPool

from tests.knowledge.support import LAPTOPS, RELEASES, handbook, seed, settings_for
from tests.knowledge_provisioning import Corpus

# Names that would turn ranked search into a download. Checked against the
# signatures rather than the prose, because prose does not fail a build.
PAGING_PARAMETERS = frozenset(
    {"offset", "cursor", "page", "page_token", "after", "before_id", "skip", "start", "all"}
)

QUERY_VERBS = (search_chunks, search_chunks_trigram, recent_documents)

GARBAGE_QUERIES = [
    "'",
    '"unbalanced',
    "AND OR NOT",
    "laptop & | ! ( ) <->",
    "*:*",
    "\\",
    "%%%",
    "-- comment",
    "'; DROP TABLE documents; --",
    "🙂🙂🙂",
    "‮laptops",
    "a" * QUERY_MAX_CHARS,
    "  ",
    "0",
]


# --- The absent parameter ---------------------------------------------------


@pytest.mark.parametrize("verb", QUERY_VERBS, ids=lambda verb: verb.__name__)
def test_no_query_verb_offers_a_way_to_page_through_the_corpus(verb: object) -> None:
    """The check that fails the day somebody adds ``offset=`` to help a caller.

    A list endpoint plus a loop is the whole corpus in a transcript in an
    afternoon, which defeats every per-call ceiling one page at a time. There
    is no polite version of this parameter, so its absence is asserted rather
    than described.
    """
    parameters = set(inspect.signature(verb).parameters)  # type: ignore[arg-type]

    assert not parameters & PAGING_PARAMETERS, sorted(parameters & PAGING_PARAMETERS)


def test_no_statement_in_the_module_pages_or_writes() -> None:
    """The same promise one layer down, where raw SQL could route around it.

    Every statement this module runs is a module-level constant, so they can be
    read as a set. Two things must be absent from all of them: a paging clause,
    and any verb the reader's credential would be refused for anyway. The
    credential is the enforcement; this is the check that catches the mistake
    before Postgres has to.
    """
    statements = {
        name: value
        for name, value in vars(store_module).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    assert statements, "the statements moved and this check stopped checking anything"

    for name, sql in statements.items():
        upper = sql.upper()
        assert "OFFSET" not in upper, name
        for verb in ("INSERT ", "UPDATE ", "DELETE ", "COPY ", "TRUNCATE "):
            assert verb not in upper, f"{name} carries {verb.strip()}"


@pytest.mark.parametrize("verb", QUERY_VERBS, ids=lambda verb: verb.__name__)
def test_every_verb_takes_a_hard_limit(verb: object) -> None:
    parameters = inspect.signature(verb).parameters  # type: ignore[arg-type]

    assert "limit" in parameters
    assert parameters["limit"].default is inspect.Parameter.empty, "k must be chosen, not defaulted"


# --- Over-fetch, and what it is allowed to cost -----------------------------


@pytest.mark.parametrize("limit", [1, 5, 8, 50, 5_000])
def test_the_overfetch_is_bounded_however_large_k_is(limit: int) -> None:
    """Duplicates collapse after ranking, so the fetch needs slack. Bounded slack.

    An unbounded over-fetch is the enumeration this design refuses wearing a
    performance hat: it is the same rows leaving the database, differing only
    in whether the last step throws them away.
    """
    fetched = _fetch_limit(limit)

    assert 1 <= fetched <= _OVERFETCH_CEILING


def test_a_nonsense_limit_still_fetches_something() -> None:
    """A zero or negative k is a caller bug, and it must not become SQL saying LIMIT 0."""
    assert _fetch_limit(0) >= 1
    assert _fetch_limit(-3) >= 1


async def test_a_corpus_full_of_matches_still_answers_with_k(corpus: Corpus) -> None:
    """The ceiling holds when the corpus is large enough for it to matter."""
    docs = [
        SeedDocument(
            path=f"Ops Handbook/copy-{index}.md",
            body=LAPTOPS.replace("1500", f"{1000 + index}"),
        )
        for index in range(40)
    ]
    seed(corpus, source_ref="ops-handbook", source_name="Ops Handbook", docs=docs)

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=8, snippet_max_chars=1200
        )

    assert len(results) == 8


# --- Queries written by whoever files issues --------------------------------


@pytest.mark.parametrize("query", GARBAGE_QUERIES)
async def test_a_garbage_query_is_answered_rather_than_raising(corpus: Corpus, query: str) -> None:
    """``websearch_to_tsquery`` parses arbitrary input without raising.

    That is the entire reason it and not ``to_tsquery`` is the entry point: a
    query descends from a task body somebody else wrote, and a parser that
    raised on punctuation would turn a rude issue title into a 500.
    """
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        exact = await search_chunks(session, query=query, limit=5, snippet_max_chars=200)
        fuzzy = await search_chunks_trigram(session, query=query, limit=5, snippet_max_chars=200)

    assert isinstance(exact, list)
    assert isinstance(fuzzy, list)


async def test_a_query_of_pure_punctuation_matches_nothing_rather_than_everything(
    corpus: Corpus,
) -> None:
    """An empty tsquery matches no row. The dangerous failure is the other one."""
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(session, query="!!! ???", limit=5, snippet_max_chars=200)

    assert results == []


async def test_the_store_does_not_bound_the_query_string_and_the_caller_must(
    corpus: Corpus,
) -> None:
    """Where the typed refusals for query length have to live, stated once.

    ``QUERY_MIN_CHARS`` and ``QUERY_MAX_CHARS`` are exported constants and this
    module does not apply them: a one-character query runs and returns whatever
    rank noise it finds. The refusal belongs to the layer that can answer with
    a code from the enum, so an endpoint or tool built on this store without
    checking them ships the bound in name only.
    """
    seed(corpus, **handbook())

    assert (QUERY_MIN_CHARS, QUERY_MAX_CHARS) == (3, 500)

    async with knowledge_session(settings_for(corpus)) as session:
        short = await search_chunks(session, query="a", limit=5, snippet_max_chars=200)
        long_query = await search_chunks(
            session, query="laptop " * 200, limit=5, snippet_max_chars=200
        )

    assert isinstance(short, list)
    assert isinstance(long_query, list)


# --- The fallback's threshold -----------------------------------------------


async def test_a_stricter_trigram_threshold_returns_less(corpus: Corpus) -> None:
    """K6 picks the number against a corpus; this pins that the number is used."""
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        loose = await search_chunks_trigram(
            session, query="reimbursd", limit=5, snippet_max_chars=200, threshold=0.3
        )
        strict = await search_chunks_trigram(
            session, query="reimbursd", limit=5, snippet_max_chars=200, threshold=0.99
        )

    assert loose, "a misspelling is exactly what the fallback is for"
    assert len(strict) < len(loose)


async def test_the_trigram_threshold_does_not_outlive_its_transaction(
    corpus: Corpus,
) -> None:
    """A pooled connection carries whatever the last caller left on it.

    ``set_config(..., true)`` is transaction-local for this reason. Were it
    session-local, one fallback search at a loose threshold would quietly widen
    every later search that happened to be handed the same connection out of a
    pool of two. The pool is pinned to one connection here so that reuse is
    certain rather than likely, which is the only way this test is about
    anything.
    """
    settings = settings_for(corpus, pool_size=1)
    seed(corpus, **handbook())

    async with knowledge_session(settings) as session:
        await search_chunks_trigram(
            session, query="reimbursd", limit=5, snippet_max_chars=200, threshold=0.31
        )

    async with knowledge_session(settings) as session:
        leaked = await session.execute(
            sa.text("SELECT current_setting('pg_trgm.word_similarity_threshold')")
        )

    assert float(leaked.scalar_one()) != pytest.approx(0.31)


# --- Snippets, on every verb ------------------------------------------------


async def test_every_verb_cuts_its_snippet_to_the_ceiling(corpus: Corpus) -> None:
    """Worst case per call is k times this number, and it is the whole contract.

    The arithmetic that keeps one call from returning half a converted deck
    only holds if all three verbs respect the same ceiling. The recency verb is
    the easy one to forget, because its snippet comes from ``left()`` rather
    than from ``ts_headline``.
    """
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        exact = await search_chunks(session, query="laptop", limit=5, snippet_max_chars=32)
        fuzzy = await search_chunks_trigram(session, query="laptop", limit=5, snippet_max_chars=32)
        recent = await recent_documents(session, days=14, limit=5, snippet_max_chars=32)

    assert exact and fuzzy and recent
    for results in (exact, fuzzy, recent):
        assert all(len(row.snippet) <= 32 for row in results)


async def test_every_verb_carries_the_field_the_external_author_control_reads(
    corpus: Corpus,
) -> None:
    """``external_author_count`` is computed from this column, and it fails closed.

    The shipped deny control refuses a result whose ``external_author_count``
    is missing, so the field must never be missing, so the column it is counted
    from must arrive on every row of every verb. A verb that forgot to select
    it would not fail: it would produce a count of zero, which reads as "no
    external authors" and denies nothing.
    """
    seed(
        corpus,
        source_ref="drive-external",
        source_name="Shared Folder",
        trust="external_authors",
        docs=[SeedDocument(path="Shared Folder/laptops.md", body=LAPTOPS, author_kind="external")],
    )

    async with knowledge_session(settings_for(corpus)) as session:
        rows = [
            *await search_chunks(session, query="laptop", limit=5, snippet_max_chars=200),
            *await search_chunks_trigram(session, query="laptop", limit=5, snippet_max_chars=200),
            *await recent_documents(session, days=14, limit=5, snippet_max_chars=200),
        ]

    assert len(rows) >= 3
    assert all(row.author_kind == "external" for row in rows)
    assert all(row.trust == "external_authors" for row in rows)


# --- Recency, and the rows it must never surface ----------------------------


async def test_recency_never_shows_a_tombstoned_document(corpus: Corpus) -> None:
    """A file unshared this morning must not be listed as what changed today."""
    now = datetime.now(UTC)
    seed(
        corpus,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[
            SeedDocument(
                path="Ops Handbook/laptops.md",
                body=LAPTOPS,
                source_modified_at=now - timedelta(hours=2),
                tombstoned_at=now,
                tombstone_reason="unshared",
            ),
            SeedDocument(
                path="Ops Handbook/releases.md",
                body=RELEASES,
                source_modified_at=now - timedelta(hours=1),
            ),
        ],
    )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await recent_documents(session, days=14, limit=5, snippet_max_chars=200)

    assert [row.path for row in results] == ["Ops Handbook/releases.md"]


async def test_recency_never_shows_a_disabled_source(corpus: Corpus) -> None:
    seed(corpus, enabled=False, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await recent_documents(session, days=14, limit=5, snippet_max_chars=200)

    assert results == []


async def test_a_document_with_no_modified_date_is_not_recent(corpus: Corpus) -> None:
    """Unknown is not new. A NULL mtime sorting to the top would be a lie."""
    seed(corpus, **handbook())
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE documents SET source_modified_at = NULL"))
    finally:
        engine.dispose()

    async with knowledge_session(settings_for(corpus)) as session:
        results = await recent_documents(session, days=14, limit=5, snippet_max_chars=200)

    assert results == []


async def test_the_surviving_copy_of_a_duplicated_paragraph_is_the_ranked_one(
    corpus: Corpus,
) -> None:
    """Collapse keeps the better copy, and better is decided before the collapse.

    Two folders holding the same deck is the case dedupe exists for. Which of
    the two an agent is shown is not arbitrary: at equal rank the workspace
    source wins, so the citation points at the copy an operator can vouch for.
    """
    for ref, name, trust in (
        ("drive-external", "Shared Folder", "external_authors"),
        ("drive-workspace", "Ops Handbook", "workspace"),
    ):
        seed(
            corpus,
            source_ref=ref,
            source_name=name,
            trust=trust,
            docs=[SeedDocument(path=f"{name}/laptops.md", body=LAPTOPS)],
        )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=400
        )

    assert len(results) == 1
    assert results[0].source_name == "Ops Handbook"
