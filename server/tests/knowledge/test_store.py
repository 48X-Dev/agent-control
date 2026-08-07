"""The corpus store's provisioning contract: privileges, schema, the lease.

Nothing here creates a table from metadata. Each run executes the shipped
``knowledge_db_init.sql`` and then the shipped migrations against a fresh
database, because the privilege that makes retrieval work at all - the reader's
SELECT - comes from migration 001 and from nowhere else. A fixture that built
the schema by hand would pass with that grant missing, and the missing-grant
failure mode reads as an empty corpus rather than as an error.

That is also why the first assertion below is a positive one.
"""

from __future__ import annotations

import warnings

import pytest
import sqlalchemy as sa
from agent_control_server.knowledge import (
    KNOWLEDGE_METADATA,
    knowledge_session,
    read_schema_version,
)
from agent_control_server.knowledge.schema import SUPPORTED_SCHEMA_VERSIONS
from agent_control_server.knowledge.store import is_supported_schema
from sqlalchemy.pool import NullPool

from tests.knowledge.support import handbook, seed, settings_for
from tests.knowledge_provisioning import Corpus

# --- Provisioning: the privilege, both directions ---------------------------


def test_the_reader_can_select_the_seeded_tables(corpus: Corpus) -> None:
    """The assertion that fails when migration 001's GRANT is missing.

    Without it, ``knowledge_read`` connects, sees nothing, and every search
    refuses exactly as it would against a corpus nobody has synced yet. A suite
    that only proved the reader cannot write would pass in that state.
    """
    seed(corpus, **handbook())
    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM documents")).scalar_one() == 2
            assert conn.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one() >= 2
            assert conn.execute(sa.text("SELECT count(*) FROM sources")).scalar_one() == 1
    finally:
        engine.dispose()


def test_the_reader_cannot_write(corpus: Corpus) -> None:
    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn, pytest.raises(sa.exc.ProgrammingError) as caught:
            conn.execute(
                sa.text(
                    "INSERT INTO sources (kind, ref, display_name, trust) "
                    "VALUES ('drive_folder', 'x', 'x', 'workspace')"
                )
            )
        assert "permission denied" in str(caught.value)
    finally:
        engine.dispose()


def test_the_reader_holds_select_on_tables_created_after_the_grant(corpus: Corpus) -> None:
    """ALTER DEFAULT PRIVILEGES, proved rather than assumed.

    Every later migration creates tables the reader was never named on. The
    default privilege is what carries the grant forward, and this is the check
    that it did.
    """
    sync = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    read = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with sync.begin() as conn:
            conn.execute(sa.text("CREATE TABLE later_arrival (id integer)"))
        with read.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM later_arrival")).scalar_one() == 0
        with sync.begin() as conn:
            conn.execute(sa.text("DROP TABLE later_arrival"))
    finally:
        sync.dispose()
        read.dispose()


async def test_the_schema_version_is_one_this_server_understands(corpus: Corpus) -> None:
    async with knowledge_session(settings_for(corpus)) as session:
        version = await read_schema_version(session)

    assert version in SUPPORTED_SCHEMA_VERSIONS
    assert is_supported_schema(version)
    assert not is_supported_schema(None)
    assert not is_supported_schema(max(SUPPORTED_SCHEMA_VERSIONS) + 1)


def test_the_lease_singleton_exists_before_any_claimant(corpus: Corpus) -> None:
    """The claim is one UPDATE, and an UPDATE never inserts."""
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            claimed = conn.execute(
                sa.text(
                    "UPDATE sync_lease SET holder = 'run-token', "
                    "lease_expires_at = now() + interval '30 minutes' "
                    "WHERE id = 1 AND lease_expires_at < now() RETURNING id"
                )
            ).scalar_one_or_none()
            assert claimed == 1

            contended = conn.execute(
                sa.text(
                    "UPDATE sync_lease SET holder = 'other-token' "
                    "WHERE id = 1 AND lease_expires_at < now() RETURNING id"
                )
            ).scalar_one_or_none()
            assert contended is None
    finally:
        engine.dispose()


def test_the_declared_metadata_matches_the_migrated_database(corpus: Corpus) -> None:
    """Two spellings of one schema, kept honest.

    The migrations are the source of truth and ``schema.py`` is what the store
    queries through. A column added to one and not the other is a query that
    fails on a deployment and passes nowhere useful, so the drift is checked
    rather than hoped for.
    """
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        inspector = sa.inspect(engine)
        for table in KNOWLEDGE_METADATA.sorted_tables:
            with warnings.catch_warnings():
                # Reflection has no Python type for `tsquery`, which is not a
                # drift and is not this check's business: names are.
                warnings.simplefilter("ignore", sa.exc.SAWarning)
                actual = {column["name"] for column in inspector.get_columns(table.name)}
            declared = {column.name for column in table.columns}
            assert declared == actual, table.name
    finally:
        engine.dispose()
