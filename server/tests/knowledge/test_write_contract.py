"""What a row has to look like, stated where Phase 2 will have to match it.

The seed helper is the only writer this slice ships, and retrieval was tested
against the rows it produces. A sync that hashed a different string, numbered
chunks differently, or counted characters where the column says bytes would be
writing rows nothing has ever been run against, and the symptoms would show up
in the ranked results rather than in an error.

So these assert the derivations rather than the round trip: the hash is the
hash of the body, the ordinals are the chunker's own and stay dense when the
scrub removes one, and a stored chunk is byte for byte what ``chunk_markdown``
returned. Two implementations that must agree will not, which is why the
chunker lives in the shared models package and why this file compares against
it directly.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
import sqlalchemy as sa
from agent_control_models.knowledge import chunk_markdown
from agent_control_server.knowledge.seed import SeedDocument, seed_corpus
from sqlalchemy.pool import NullPool

from tests.knowledge.support import LAPTOPS, handbook, seed
from tests.knowledge_provisioning import Corpus

# One credential-bearing section between two clean ones. Each section clears the
# 200-character floor on its own, so the chunker leaves three, and the middle
# one is the one the scrub has to take.
DEPLOY_WITH_A_SECRET = (
    "# Deploy\n\n"
    + "The release manager runs the deploy script on Thursday afternoon. " * 4
    + "\n\n## Credentials\n\n"
    + "The staging key is sk-abcdefghijklmnopqrstuvwx and it rotates monthly. " * 4
    + "\n\n## Rollback\n\n"
    + "A rollback is one command and the on-call engineer runs it without asking. " * 4
)

COSTS_IN_STERLING = (
    "# Costs\n\n"
    + "The laptop stipend is £1200 a year and the desk budget is £300 on top. " * 4
)


def rows(corpus: Corpus, sql: str, *, as_writer: bool = False) -> list[Any]:
    """Read back as the reader, which is the credential retrieval will hold."""
    engine = sa.create_engine(
        corpus.sync_url if as_writer else corpus.read_url, future=True, poolclass=NullPool
    )
    try:
        with engine.begin() as conn:
            return list(conn.execute(sa.text(sql)).all())
    finally:
        engine.dispose()


def write(corpus: Corpus, sql: str) -> None:
    """A statement with nothing to return, run as the role that owns the corpus."""
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(sql))
    finally:
        engine.dispose()


# --- The dedupe key -----------------------------------------------------------


def test_the_stored_hash_is_the_hash_of_the_body_retrieval_collapses_on(corpus: Corpus) -> None:
    """``(content_sha256, ordinal)`` is what stops one paragraph spending two slots.

    The key only works if both copies derive it the same way, so the assertion
    is against ``hashlib`` rather than against the other row: two wrong copies
    would agree with each other perfectly.
    """
    for ref, name in (("folder-a", "Folder A"), ("folder-b", "Folder B")):
        seed_corpus(
            corpus.sync_url,
            source_ref=ref,
            source_name=name,
            docs=[SeedDocument(path=f"{name}/laptops.md", body=LAPTOPS)],
        )

    expected = hashlib.sha256(LAPTOPS.encode("utf-8")).hexdigest()
    stored = rows(corpus, "SELECT content_sha256 FROM documents ORDER BY id")
    assert [row[0] for row in stored] == [expected, expected]

    per_copy = rows(
        corpus,
        "SELECT d.content_sha256, c.ordinal FROM chunks c "
        "JOIN documents d ON d.id = c.document_id ORDER BY d.id, c.ordinal",
    )
    half = len(per_copy) // 2
    assert half >= 1
    assert per_copy[:half] == per_copy[half:]


def test_ordinals_stay_dense_when_the_scrub_drops_a_chunk(corpus: Corpus) -> None:
    """A gap would be a document whose second paragraph is numbered third.

    Two things break on it. The unique key on ``(document_id, ordinal)`` stops
    meaning "the nth chunk", and the duplicate collapse compares ordinals
    across copies, so one copy losing a chunk to the scrub would misalign every
    ordinal after it.
    """
    result = seed_corpus(
        corpus.sync_url,
        source_ref="repo",
        source_name="agent-control",
        docs=[SeedDocument(path="agent-control/docs/deploy.md", body=DEPLOY_WITH_A_SECRET)],
    )

    assert result.secrets_skipped == 1
    ordinals = [row[0] for row in rows(corpus, "SELECT ordinal FROM chunks ORDER BY ordinal")]
    assert ordinals == list(range(result.chunks_written))


def test_the_scrub_takes_the_chunk_with_the_credential_and_leaves_its_neighbours(
    corpus: Corpus,
) -> None:
    """Per chunk, not per document. A secret in an appendix must not cost the policy."""
    seed_corpus(
        corpus.sync_url,
        source_ref="repo",
        source_name="agent-control",
        docs=[SeedDocument(path="agent-control/docs/deploy.md", body=DEPLOY_WITH_A_SECRET)],
    )

    bodies = [row[0] for row in rows(corpus, "SELECT body FROM chunks ORDER BY ordinal")]

    assert bodies
    assert not any("sk-abcdefghijklmnopqrstuvwx" in body for body in bodies)
    assert any("deploy script" in body for body in bodies)
    assert any("rollback is one command" in body for body in bodies)


# --- The rows are the chunker's own output ------------------------------------


def test_a_stored_chunk_is_what_the_chunker_returned(corpus: Corpus) -> None:
    """The sync and the reader have to agree byte for byte about what a chunk is."""
    seed(corpus, **handbook())

    expected = chunk_markdown(LAPTOPS)
    stored = rows(
        corpus,
        "SELECT c.ordinal, c.heading_path, c.body, c.chars FROM chunks c "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE d.path = 'Ops Handbook/Onboarding/laptops.md' ORDER BY c.ordinal",
    )

    assert len(stored) == len(expected)
    for row, chunk in zip(stored, expected, strict=True):
        assert row[0] == chunk.ordinal
        assert row[1] == chunk.heading_path
        assert row[2] == chunk.body
        assert row[3] == chunk.chars == len(chunk.body)


def test_the_stored_size_counts_bytes_and_not_characters(corpus: Corpus) -> None:
    """``bytes`` is what the ceilings in section 5.4 are spent against.

    A pound sign is one character and two bytes, and a corpus measured in
    characters is a corpus whose size limit is wrong by however much of it is
    not ASCII.
    """
    seed_corpus(
        corpus.sync_url,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[SeedDocument(path="Ops Handbook/costs.md", body=COSTS_IN_STERLING)],
    )

    stored = rows(corpus, "SELECT bytes FROM documents")[0][0]

    assert stored == len(COSTS_IN_STERLING.encode("utf-8"))
    assert stored > len(COSTS_IN_STERLING)


# --- Registering the same thing twice -----------------------------------------


def test_re_registering_a_source_updates_it_rather_than_adding_a_second(corpus: Corpus) -> None:
    """``UNIQUE (kind, ref)`` is the identity, and a re-run is the normal case."""
    first = seed(corpus, **handbook())

    again = seed_corpus(
        corpus.sync_url,
        source_ref="ops-handbook",
        source_name="Ops Handbook (renamed)",
        trust="external_authors",
        docs=[],
    )

    assert again.source_ids["ops-handbook"] == first.source_ids["ops-handbook"]
    registered = rows(corpus, "SELECT display_name, trust FROM sources")
    assert registered == [("Ops Handbook (renamed)", "external_authors")]


def test_a_lapsed_lease_is_there_for_the_next_run_to_take(corpus: Corpus) -> None:
    """The row the migration seeded, doing the job it was seeded for.

    A run that died without releasing must not lock the corpus out forever, so
    the claim is guarded by the expiry and not by the holder. One statement,
    because a read-then-write is two runs both believing they won.
    """
    write(
        corpus,
        "UPDATE sync_lease SET holder = 'dead-run', "
        "lease_expires_at = now() - interval '1 minute' WHERE id = 1",
    )

    claimed = rows(
        corpus,
        "UPDATE sync_lease SET holder = 'next-run', "
        "lease_expires_at = now() + interval '30 minutes' "
        "WHERE id = 1 AND lease_expires_at < now() RETURNING holder",
        as_writer=True,
    )

    assert [row[0] for row in claimed] == ["next-run"]


def test_a_live_lease_is_not_taken_and_its_holder_is_not_disturbed(corpus: Corpus) -> None:
    """The half a failed claim usually forgets.

    An UPDATE that matches nothing returns nothing, and the interesting
    assertion is the second one: the loser must not have overwritten the
    winner's holder on the way past.
    """
    write(
        corpus,
        "UPDATE sync_lease SET holder = 'live-run', "
        "lease_expires_at = now() + interval '30 minutes' WHERE id = 1",
    )

    claimed = rows(
        corpus,
        "UPDATE sync_lease SET holder = 'intruder' "
        "WHERE id = 1 AND lease_expires_at < now() RETURNING holder",
        as_writer=True,
    )

    assert claimed == []
    assert rows(corpus, "SELECT holder FROM sync_lease")[0][0] == "live-run"


def test_writing_the_same_document_twice_is_refused_by_the_database(corpus: Corpus) -> None:
    """The helper is not an upsert, and the constraint says so out loud.

    Worth pinning because the next writer is the sync, which re-reads documents
    it has already seen on every incremental run. It will have to update rather
    than insert, and finding that out from a failing test beats finding it out
    from a corpus with two of everything.
    """
    seed(corpus, **handbook())

    with pytest.raises(sa.exc.IntegrityError):
        seed(corpus, **handbook())

    assert rows(corpus, "SELECT count(*) FROM documents")[0][0] == 2
