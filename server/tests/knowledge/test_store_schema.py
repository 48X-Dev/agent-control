"""What the migrated corpus refuses, and what it maintains on its own.

``test_store.py`` proves the reader's privilege and that the declared metadata
names the same columns the migrations created. This file asks the next
question: are the rules in those ``CHECK`` clauses actually enforced, and does
the database keep the parts of a row it promised to keep?

Both halves matter to a reader that never writes. The closed enums are the only
thing standing between a sync bug and a ``tombstone_reason`` retrieval has
never seen; the generated ``tsvector`` is the entire search path, and a column
that stopped regenerating on UPDATE would show up as a document that answers
its old text forever.

Every statement here runs as ``knowledge_sync``. Standing in for the writer is
the only way to test what the writer is refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from agent_control_server.knowledge import (
    KnowledgeUnavailableError,
    knowledge_session,
    read_schema_version,
)
from agent_control_server.knowledge.schema import SUPPORTED_SCHEMA_VERSIONS
from agent_control_server.knowledge.seed import SeedDocument
from agent_control_server.knowledge.store import is_supported_schema, search_chunks
from sqlalchemy.pool import NullPool

from tests.knowledge.support import LAPTOPS, handbook, seed, settings_for
from tests.knowledge_provisioning import Corpus

_A_SOURCE = "INSERT INTO sources (kind, ref, display_name, trust) VALUES (:kind, :ref, 'X', :trust)"

_A_DOCUMENT = """
INSERT INTO documents (
    source_id, external_id, path, title, author_kind, content_sha256,
    synced_at, conversion_status, bytes, tombstoned_at, tombstone_reason
) VALUES (
    :source_id, :external_id, 'p', 't', :author_kind, repeat('a', 64),
    now(), 'exported', 1, :tombstoned_at, :tombstone_reason
)
"""


@pytest.fixture()
def sync(corpus: Corpus) -> Iterator[sa.Connection]:
    """One autocommitting connection as the role that owns the schema."""
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        yield conn
    engine.dispose()


def _source(conn: sa.Connection, *, ref: str = "s", kind: str = "drive_folder") -> int:
    conn.execute(sa.text(_A_SOURCE), {"kind": kind, "ref": ref, "trust": "workspace"})
    return int(
        conn.execute(sa.text("SELECT id FROM sources WHERE ref = :ref"), {"ref": ref}).scalar_one()
    )


def _document(
    conn: sa.Connection, source_id: int, *, external_id: str = "e", **overrides: object
) -> int:
    values: dict[str, object] = {
        "source_id": source_id,
        "external_id": external_id,
        "author_kind": "workspace",
        "tombstoned_at": None,
        "tombstone_reason": None,
    }
    values.update(overrides)
    conn.execute(sa.text(_A_DOCUMENT), values)
    return int(
        conn.execute(
            sa.text("SELECT id FROM documents WHERE external_id = :e"), {"e": external_id}
        ).scalar_one()
    )


# --- The closed enums, enforced rather than documented -----------------------


@pytest.mark.parametrize(
    ("column", "statement", "params"),
    [
        (
            "sources.kind",
            _A_SOURCE,
            {"kind": "confluence_space", "ref": "bad-kind", "trust": "workspace"},
        ),
        (
            "sources.trust",
            _A_SOURCE,
            {"kind": "drive_folder", "ref": "bad-trust", "trust": "trusted"},
        ),
    ],
)
def test_a_source_outside_the_vocabulary_is_refused(
    sync: sa.Connection, column: str, statement: str, params: dict[str, str]
) -> None:
    with pytest.raises(sa.exc.IntegrityError) as caught:
        sync.execute(sa.text(statement), params)

    assert "violates check constraint" in str(caught.value), column


def test_a_tombstone_reason_outside_the_closed_enum_is_refused(sync: sa.Connection) -> None:
    """The reason column is the answer to "what happened to that document".

    An open text column would let a sync invent a reason retrieval and the
    console have never seen, and the first anyone would know is a status page
    printing a word nobody wrote.
    """
    source_id = _source(sync, ref="tombstones")

    with pytest.raises(sa.exc.IntegrityError):
        _document(
            sync,
            source_id,
            external_id="bad-reason",
            tombstoned_at=datetime.now(UTC),
            tombstone_reason="archived",
        )


def test_an_author_kind_outside_the_closed_enum_is_refused(sync: sa.Connection) -> None:
    """``author_kind`` decides ``external_author_count``, which a control reads."""
    source_id = _source(sync, ref="authors")

    with pytest.raises(sa.exc.IntegrityError):
        _document(sync, source_id, external_id="bad-author", author_kind="contractor")


@pytest.mark.parametrize("status", ["succeeded", "done", ""])
def test_a_run_status_outside_the_closed_enum_is_refused(sync: sa.Connection, status: str) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        sync.execute(
            sa.text(
                "INSERT INTO sync_runs (holder, started_at, status) "
                "VALUES ('token', now(), :status)"
            ),
            {"status": status},
        )


# --- Identity, and the pairs that must stay unique --------------------------


def test_one_source_cannot_be_registered_twice(sync: sa.Connection) -> None:
    _source(sync, ref="duplicate")

    with pytest.raises(sa.exc.IntegrityError):
        _source(sync, ref="duplicate")


def test_one_document_cannot_arrive_twice_from_the_same_source(sync: sa.Connection) -> None:
    """The incremental sync's upsert key. Without it a replayed feed doubles the corpus."""
    source_id = _source(sync, ref="repeat")
    _document(sync, source_id, external_id="file-1")

    with pytest.raises(sa.exc.IntegrityError):
        _document(sync, source_id, external_id="file-1")


def test_two_chunks_cannot_share_an_ordinal(sync: sa.Connection) -> None:
    """``(content_sha256, ordinal)`` is the dedupe key, so ordinals cannot collide."""
    source_id = _source(sync, ref="ordinals")
    document_id = _document(sync, source_id, external_id="doc")
    insert = (
        "INSERT INTO chunks (document_id, ordinal, body, chars) VALUES (:document_id, 0, 'text', 4)"
    )
    sync.execute(sa.text(insert), {"document_id": document_id})

    with pytest.raises(sa.exc.IntegrityError):
        sync.execute(sa.text(insert), {"document_id": document_id})


def test_the_lease_cannot_gain_a_second_row(sync: sa.Connection) -> None:
    """A singleton by constraint, not by convention.

    Two lease rows means two syncs each holding "the" lease, which is the one
    thing the lease exists to prevent.
    """
    with pytest.raises(sa.exc.IntegrityError):
        sync.execute(sa.text("INSERT INTO sync_lease (id) VALUES (2)"))


# --- What the database maintains without being asked ------------------------


def test_deleting_a_document_takes_its_chunks_with_it(sync: sa.Connection) -> None:
    source_id = _source(sync, ref="cascade")
    document_id = _document(sync, source_id, external_id="doc")
    sync.execute(
        sa.text(
            "INSERT INTO chunks (document_id, ordinal, body, chars) "
            "VALUES (:document_id, 0, 'text', 4)"
        ),
        {"document_id": document_id},
    )

    sync.execute(sa.text("DELETE FROM documents WHERE id = :id"), {"id": document_id})

    assert sync.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one() == 0


def test_a_tombstone_keeps_the_row_and_loses_only_the_chunks(sync: sa.Connection) -> None:
    """The shape section 4.4 asks for: the row is the answer to what agents read.

    Three hundred bytes of provenance survives a file being unshared; the text
    does not, which is what makes the document unsearchable the moment the
    sweep runs.
    """
    source_id = _source(sync, ref="tombstone-shape")
    document_id = _document(sync, source_id, external_id="doc")
    sync.execute(
        sa.text(
            "INSERT INTO chunks (document_id, ordinal, body, chars) "
            "VALUES (:document_id, 0, 'text', 4)"
        ),
        {"document_id": document_id},
    )

    sync.execute(sa.text("DELETE FROM chunks WHERE document_id = :id"), {"id": document_id})
    sync.execute(
        sa.text(
            "UPDATE documents SET tombstoned_at = now(), tombstone_reason = 'unshared' "
            "WHERE id = :id"
        ),
        {"id": document_id},
    )

    row = sync.execute(
        sa.text("SELECT path, tombstone_reason FROM documents WHERE id = :id"),
        {"id": document_id},
    ).one()
    assert row.tombstone_reason == "unshared"
    assert sync.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one() == 0


async def test_an_edited_chunk_is_searchable_by_its_new_words(corpus: Corpus) -> None:
    """The generated column regenerates, which is the whole search path.

    A ``STORED`` generated column that stopped tracking its source would leave
    every re-synced document answering to the text it used to hold, and the
    symptom would be an agent citing yesterday's policy with today's date on
    it.
    """
    seed(corpus, **handbook())
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE chunks SET body = 'Bicycles are reimbursed up to 400 GBP.' "
                    "WHERE ordinal = 0"
                )
            )
    finally:
        engine.dispose()

    async with knowledge_session(settings_for(corpus)) as session:
        found = await search_chunks(session, query="bicycles", limit=5, snippet_max_chars=400)
        stale = await search_chunks(session, query="laptops", limit=5, snippet_max_chars=400)

    assert found, "the edited body never reached the tsvector"
    assert stale == []


# --- The objects retrieval silently depends on ------------------------------


def test_the_trigram_extension_is_installed(sync: sa.Connection) -> None:
    """Without it the fallback's ``<%`` operator does not exist at all."""
    installed = sync.execute(
        sa.text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")
    ).scalar_one()

    assert installed == 1


@pytest.mark.parametrize(
    ("index", "expected"),
    [
        ("ix_chunks_tsv", "USING gin (body_tsv)"),
        ("ix_chunks_trgm", "gin_trgm_ops"),
        ("ix_documents_modified_at", "WHERE (tombstoned_at IS NULL)"),
        ("ix_documents_content_sha256", "content_sha256"),
        ("ix_documents_source_id", "source_id"),
    ],
)
def test_every_index_retrieval_reads_through_exists(
    sync: sa.Connection, index: str, expected: str
) -> None:
    """Named one by one, because a partial upgrade is the realistic accident.

    A ``chunks`` table without its GIN index still answers every query in this
    suite, on a corpus of five documents, in single-digit milliseconds. It
    stops answering them on a corpus of four hundred, in production, at which
    point the missing line is three migrations back.
    """
    definition = sync.execute(
        sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": index}
    ).scalar_one_or_none()

    assert definition is not None, f"{index} is missing"
    assert expected in definition, definition


# --- The version marker, and what a reader does with one it cannot read -----


async def test_a_schema_the_reader_does_not_know_is_refused_rather_than_parsed(
    corpus: Corpus,
) -> None:
    """Read the marker, judge it, refuse - all three, not the first two.

    Mis-parsing rows written by a sync that has moved ahead is the failure this
    exists to prevent, and a version that is merely *detectable* prevents
    nothing. The refusal itself, and what it does not say, is
    ``test_engine_bounds.py``'s; what matters here is that the marker the sync
    owns is what decides it.
    """
    unknown = max(SUPPORTED_SCHEMA_VERSIONS) + 7
    assert not is_supported_schema(unknown)
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE schema_meta SET version = :version WHERE id = 1"),
                {"version": unknown},
            )

        with pytest.raises(KnowledgeUnavailableError):
            async with knowledge_session(settings_for(corpus)) as session:
                await session.execute(sa.text("SELECT count(*) FROM documents"))
    finally:
        with engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE schema_meta SET version = :version WHERE id = 1"),
                {"version": max(SUPPORTED_SCHEMA_VERSIONS)},
            )
        engine.dispose()

    async with knowledge_session(settings_for(corpus)) as session:
        assert is_supported_schema(await read_schema_version(session))


async def test_a_document_the_seed_wrote_carries_every_column_retrieval_reads(
    corpus: Corpus,
) -> None:
    """The write contract Phase 2 has to match, asserted column by column.

    A sync that filled these differently would be filling rows retrieval was
    never tested against, and the columns that go missing quietly are the
    optional ones nothing crashes without.
    """
    seed(
        corpus,
        source_ref="ops-handbook",
        source_name="Ops Handbook",
        docs=[SeedDocument(path="Ops Handbook/laptops.md", body=LAPTOPS)],
    )

    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM documents")).mappings().one()
            chunk = (
                conn.execute(sa.text("SELECT * FROM chunks ORDER BY ordinal")).mappings().first()
            )
    finally:
        engine.dispose()

    assert row["content_sha256"] and len(row["content_sha256"]) == 64
    assert row["synced_at"] is not None
    assert row["source_modified_at"] is not None
    assert row["bytes"] == len(LAPTOPS.encode("utf-8"))
    assert row["conversion_status"] == "exported"
    assert row["tombstoned_at"] is None
    assert chunk is not None
    assert chunk["chars"] == len(chunk["body"])
    assert chunk["body_tsv"] is not None
