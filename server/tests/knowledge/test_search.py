"""Search, recency and the counters that ride along with every response.

Three verbs over one corpus: rank a query, list what moved inside a window,
and say how fresh the mirror is. What is deliberately absent is a fourth -
there is no offset, no cursor and no list, because wholesale export is
exfiltration shaped like a feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from agent_control_server.knowledge import (
    corpus_stats,
    knowledge_session,
    recent_documents,
    search_chunks,
    search_chunks_trigram,
)
from agent_control_server.knowledge.seed import SeedDocument
from sqlalchemy.pool import NullPool

from tests.knowledge.support import LAPTOPS, RELEASES, handbook, seed, settings_for
from tests.knowledge_provisioning import Corpus

# --- Search -----------------------------------------------------------------


async def test_a_search_returns_the_snippet_with_its_provenance(corpus: Corpus) -> None:
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=1200
        )

    assert results, "the corpus holds a laptop policy and search did not find it"
    top = results[0]
    assert "laptop" in top.snippet.lower()
    assert top.path == "Ops Handbook/Onboarding/laptops.md"
    assert top.heading_path == "Onboarding > Laptops"
    assert top.title == "laptops.md"
    assert top.source_name == "Ops Handbook"
    assert top.source_kind == "drive_folder"
    assert top.author_kind == "workspace"
    assert top.synced_at is not None


async def test_a_snippet_carries_no_markup(corpus: Corpus) -> None:
    """It travels to a model as text and renders in the console as a text node."""
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=1200
        )

    assert "<b>" not in results[0].snippet
    assert "</b>" not in results[0].snippet


async def test_a_snippet_is_cut_to_the_ceiling(corpus: Corpus) -> None:
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=40
        )

    assert all(len(result.snippet) <= 40 for result in results)


async def test_results_are_capped_at_k(corpus: Corpus) -> None:
    docs = [
        SeedDocument(path=f"Ops Handbook/copy-{index}.md", body=LAPTOPS.replace("1500", str(index)))
        for index in range(10)
    ]
    seed(corpus, source_ref="ops-handbook", source_name="Ops Handbook", docs=docs)

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=2, snippet_max_chars=1200
        )

    assert len(results) == 2


async def test_a_tombstoned_document_is_unsearchable(corpus: Corpus) -> None:
    seed(
        corpus,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[
            SeedDocument(
                path="Ops Handbook/Onboarding/laptops.md",
                body=LAPTOPS,
                tombstoned_at=datetime.now(UTC),
                tombstone_reason="unshared",
            )
        ],
    )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=1200
        )

    assert results == []


async def test_a_disabled_source_is_unsearchable(corpus: Corpus) -> None:
    seed(corpus, enabled=False, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=1200
        )

    assert results == []


async def test_the_same_paragraph_from_two_sources_spends_one_slot(corpus: Corpus) -> None:
    """Provenance stays split; the k slots do not get spent twice on one paragraph."""
    for ref, name in (("drive-a", "Folder A"), ("drive-b", "Folder B")):
        seed(
            corpus,
            source_ref=ref,
            source_name=name,
            docs=[SeedDocument(path=f"{name}/laptops.md", body=LAPTOPS)],
        )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=1200
        )

    keys = [(result.content_sha256, result.ordinal) for result in results]
    assert len(keys) == len(set(keys))
    assert len(results) == 1


async def test_an_external_authored_source_ranks_below_a_workspace_one(corpus: Corpus) -> None:
    """Trust changes ordering and ceilings. It never changes the fencing.

    Both snippets are returned and both will be fenced as DATA; what the tier
    buys is which one an agent reads first when the rank cannot separate them.
    """
    matching = "Laptops are reimbursed by finance. "
    seed(
        corpus,
        source_ref="drive-external",
        source_name="Shared Folder",
        trust="external_authors",
        docs=[
            SeedDocument(
                path="Shared Folder/laptops.md",
                body=matching + ("outsider " * 30),
                author_kind="external",
            )
        ],
    )
    seed(
        corpus,
        source_ref="drive-workspace",
        source_name="Ops Handbook",
        trust="workspace",
        docs=[
            SeedDocument(
                path="Ops Handbook/laptops.md",
                body=matching + ("colleague " * 30),
            )
        ],
    )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="laptops reimbursed", limit=5, snippet_max_chars=1200
        )

    assert len(results) == 2
    assert results[0].rank == results[1].rank, "this test only means anything at equal rank"
    assert results[0].trust == "workspace"
    assert results[1].trust == "external_authors"


async def test_a_query_that_matches_nothing_returns_nothing(corpus: Corpus) -> None:
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        results = await search_chunks(
            session, query="submarine procurement", limit=5, snippet_max_chars=1200
        )

    assert results == []


async def test_an_adversarial_query_parameterizes_rather_than_executing(corpus: Corpus) -> None:
    """The query descends from text somebody else wrote. It can only SELECT."""
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        for hostile in ("'; DROP TABLE documents; --", "laptop & | ! ( )", "*"):
            await search_chunks(session, query=hostile, limit=5, snippet_max_chars=1200)

    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM documents")).scalar_one() == 2
    finally:
        engine.dispose()


async def test_the_trigram_fallback_finds_what_full_text_search_misses(corpus: Corpus) -> None:
    seed(corpus, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        exact = await search_chunks(session, query="reimbursd", limit=5, snippet_max_chars=1200)
        fuzzy = await search_chunks_trigram(
            session, query="reimbursd", limit=5, snippet_max_chars=1200
        )

    assert exact == [], "full-text search is expected to miss a misspelling"
    assert fuzzy, "the trigram fallback is what backstops it"
    assert "reimburs" in fuzzy[0].snippet.lower()


# --- Recency ----------------------------------------------------------------


async def test_recency_returns_what_moved_inside_the_window(corpus: Corpus) -> None:
    now = datetime.now(UTC)
    seed(
        corpus,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[
            SeedDocument(
                path="Ops Handbook/laptops.md",
                body=LAPTOPS,
                source_modified_at=now - timedelta(days=2),
            ),
            SeedDocument(
                path="Ops Handbook/releases.md",
                body=RELEASES,
                source_modified_at=now - timedelta(days=90),
            ),
        ],
    )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await recent_documents(session, days=14, limit=5, snippet_max_chars=400)

    assert [result.path for result in results] == ["Ops Handbook/laptops.md"]


async def test_recency_is_newest_first_and_capped(corpus: Corpus) -> None:
    now = datetime.now(UTC)
    docs = [
        SeedDocument(
            path=f"Ops Handbook/note-{index}.md",
            body=RELEASES.replace("Thursdays", f"day {index}"),
            source_modified_at=now - timedelta(days=index),
        )
        for index in range(6)
    ]
    seed(corpus, source_ref="ops-handbook", source_name="Ops Handbook", docs=docs)

    async with knowledge_session(settings_for(corpus)) as session:
        results = await recent_documents(session, days=14, limit=3, snippet_max_chars=400)

    assert len(results) == 3
    assert [result.path for result in results] == [
        "Ops Handbook/note-0.md",
        "Ops Handbook/note-1.md",
        "Ops Handbook/note-2.md",
    ]


async def test_recency_repeated_returns_the_same_page(corpus: Corpus) -> None:
    """No cursor exists, so a loop over this verb gains nothing."""
    now = datetime.now(UTC)
    docs = [
        SeedDocument(
            path=f"Ops Handbook/note-{index}.md",
            body=RELEASES.replace("Thursdays", f"day {index}"),
            source_modified_at=now - timedelta(days=index),
        )
        for index in range(6)
    ]
    seed(corpus, source_ref="ops-handbook", source_name="Ops Handbook", docs=docs)

    async with knowledge_session(settings_for(corpus)) as session:
        first = await recent_documents(session, days=14, limit=2, snippet_max_chars=400)
        second = await recent_documents(session, days=14, limit=2, snippet_max_chars=400)

    assert [result.path for result in first] == [result.path for result in second]


async def test_a_document_with_no_chunks_is_never_a_recency_result(corpus: Corpus) -> None:
    """A failed conversion must not be citable from its title alone."""
    seed(
        corpus,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[
            SeedDocument(
                path="Ops Handbook/scan.pdf",
                body="",
                conversion_status="failed",
                source_modified_at=datetime.now(UTC),
            )
        ],
    )

    async with knowledge_session(settings_for(corpus)) as session:
        results = await recent_documents(session, days=14, limit=5, snippet_max_chars=400)

    assert results == []


# --- Corpus counters --------------------------------------------------------


async def test_the_counters_describe_the_searchable_corpus(corpus: Corpus) -> None:
    verified = datetime.now(UTC) - timedelta(seconds=480)
    seed(corpus, last_verified_at=verified, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        stats = await corpus_stats(session)

    assert stats.documents == 2
    assert stats.sources_enabled == 1
    assert stats.sources_failing == 0
    assert stats.stale_seconds is not None
    assert 470 <= stats.stale_seconds <= 600


async def test_a_disabled_sources_documents_are_not_counted_as_searchable(
    corpus: Corpus,
) -> None:
    """The counter has to apply the filter the search applies.

    This number rides on every response, refusals included, and 8.6's empty
    result reads "the knowledge base holds N documents from M sources". With a
    source switched off and its documents still counted, an agent is told the
    base holds two documents, finds nothing, and cannot tell "not in the
    corpus" from "the source is off" - which is the whole distinction this
    design exists to keep.
    """
    seed(corpus, enabled=False, **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        stats = await corpus_stats(session)
        results = await search_chunks(
            session, query="laptop reimbursement", limit=5, snippet_max_chars=1200
        )

    assert results == []
    assert stats.documents == 0
    assert stats.sources_enabled == 0


async def test_a_failing_source_is_countable_in_every_response(corpus: Corpus) -> None:
    seed(corpus, last_run_status="failed", **handbook())

    async with knowledge_session(settings_for(corpus)) as session:
        stats = await corpus_stats(session)

    assert stats.sources_failing == 1


async def test_a_source_that_never_verified_is_not_reported_as_fresh(corpus: Corpus) -> None:
    seed(corpus, last_verified_at=None, **handbook())
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE sources SET last_verified_at = NULL"))
    finally:
        engine.dispose()

    async with knowledge_session(settings_for(corpus)) as session:
        stats = await corpus_stats(session)

    assert stats.stale_seconds is None


async def test_an_empty_corpus_reports_zero_rather_than_failing(corpus: Corpus) -> None:
    async with knowledge_session(settings_for(corpus)) as session:
        stats = await corpus_stats(session)

    assert stats.documents == 0
    assert stats.sources_enabled == 0
