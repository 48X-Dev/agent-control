"""What must not happen, proved by absence rather than by argument.

Three claims in section 4.1 are claims about things that do not occur, and each
one reads as success when it is broken. The control plane's per-test truncation
must not reach the corpus, or the suite empties the mirror halfway through and
the symptom is "search finds nothing". A server with the feature off must not
open a connection, or a deployment that never enabled knowledge still fails
when the corpus database is down. And the corpus schema must stay outside
Alembic's autogenerate surface, or a routine ``--autogenerate`` proposes
dropping seven tables it has never owned.

The instrument for the connection claims is ``pg_stat_database.sessions``,
which Postgres increments when a backend attaches to a database. Each test
module here provisions its own corpus database, so nothing else on the instance
moves that counter. Every absence assertion is paired with a positive one in
the same test, because a counter that never moves proves nothing about a
counter that cannot move.

The other direction, that ``knowledge_sync`` cannot reach the control plane,
belongs to ``test_knowledge_db_isolation.py`` and cannot be checked from here:
this suite's control database is a throwaway created from ``template1`` and has
never had ``adk_db_init.sql``'s REVOKE applied to it, so an assertion made
against it would be an assertion about privileges no deployment has. That file
provisions a control database properly and takes a lock while it does.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from agent_control_server.config import KnowledgeSettings, db_config, knowledge_settings
from agent_control_server.db import Base
from agent_control_server.knowledge import (
    KNOWLEDGE_METADATA,
    KnowledgeUnavailableError,
    corpus_stats,
    knowledge_session,
)
from agent_control_server.knowledge.seed import SeedDocument, seed_corpus
from agent_control_server.services.knowledge_quota import reset_knowledge_quota
from fastapi.testclient import TestClient
from sqlalchemy.pool import NullPool

from tests.conftest import _truncate_all_tables
from tests.conftest import engine as control_plane_engine
from tests.knowledge.support import LAPTOPS, handbook, seed, settings_for
from tests.knowledge_provisioning import (
    READ_PASSWORD,
    READ_ROLE,
    Corpus,
    connect,
    scalar,
)


@pytest.fixture()
def sessions(corpus: Corpus) -> Iterator[Any]:
    """A counter of backends that have attached to this test's corpus database."""
    admin = connect("postgres", db_config.user, db_config.password)

    def count() -> int:
        with admin.cursor() as cur:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute(
                "SELECT sessions FROM pg_stat_database WHERE datname = %s", (corpus.database,)
            )
            row = cur.fetchone()
        assert row is not None, f"{corpus.database} has no stats row"
        return int(row[0])

    try:
        yield count
    finally:
        admin.close()


# --- A server with the feature off touches nothing --------------------------


async def test_a_disabled_corpus_is_never_connected_to(corpus: Corpus, sessions: Any) -> None:
    """Off means off at the socket, not off at the last minute.

    The refusal has to happen before a connection is attempted, or a deployment
    that never turned knowledge on still waits out a connect timeout, still
    holds a pool slot, and still fails differently when the corpus database is
    down. The positive half of this test is what proves the counter can move at
    all.
    """
    off = settings_for(corpus, enabled=False)
    before = sessions()

    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(off):
            pass

    assert caught.value.code == "knowledge_disabled"
    assert sessions() == before, "a disabled corpus opened a connection"

    async with knowledge_session(settings_for(corpus)) as session:
        await corpus_stats(session)

    assert sessions() > before, "the counter never moved, so its silence proved nothing"


async def test_the_half_on_state_connects_to_nothing_either(corpus: Corpus, sessions: Any) -> None:
    """Enabled with no DSN is the state the startup warning exists for.

    It refuses in a way that looks exactly like an empty corpus from the
    outside, which is why the log line names it. What it must not do is reach
    for a default connection and find one.
    """
    before = sessions()

    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(KnowledgeSettings(enabled=True, db_url=None)):
            pass

    assert caught.value.code == "knowledge_unavailable"
    assert sessions() == before


MISTYPED_DSNS = [
    pytest.param("postgresql+asyncpg://u:p@127.0.0.1:1/db", id="asyncpg-unreachable"),
    pytest.param("not-a-dsn", id="unparseable"),
    pytest.param("postgresql://u:p@127.0.0.1:1/db", id="no-driver-suffix"),
    pytest.param("postgresql+psycopg://u:p@127.0.0.1:1/db", id="psycopg-unreachable"),
]


@pytest.mark.parametrize("dsn", MISTYPED_DSNS)
def test_a_dsn_an_operator_could_plausibly_write_still_refuses(
    client: TestClient, dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed refusal and a turn that proceeds, for every DSN and not just one.

    That promise is the reason this feature gets its own engine, and it held
    for exactly one of the four shapes below. Only ``+psycopg`` produces a
    SQLAlchemy error; asyncpg lets ``ConnectionRefusedError`` through untouched,
    an unparseable URL fails while the engine is being built rather than while
    it is being used, and a DSN with no driver suffix asks for a psycopg2
    nobody installed and raises ``ModuleNotFoundError``. Each of the last three
    reached the ASGI layer as an exception.

    Parametrized rather than written once because those three failed in three
    different places, and a single case would pin one of them.
    """
    reset_knowledge_quota()
    monkeypatch.setattr(knowledge_settings, "enabled", True)
    monkeypatch.setattr(knowledge_settings, "db_url", dsn)

    answer = client.post(
        "/api/v1/agent-sessions/sess-bad-dsn/knowledge/search",
        json={"query": "laptop policy"},
    )

    assert answer.status_code == 200, answer.text
    assert answer.json()["refusal_code"] == "knowledge_unavailable"


@pytest.mark.parametrize("dsn", MISTYPED_DSNS)
async def test_a_mistyped_dsn_says_nothing_about_the_driver_that_rejected_it(
    dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator gets the exception's name; the agent gets neither.

    A refusal that carried "No module named 'psycopg2'" would be a driver
    message on its way to a model, which is what the closed enum exists to
    prevent. The log is the other half: an operator debugging a bad DSN needs
    to be told which of the four shapes they have, and a type name is enough
    to tell them without putting a password-bearing URL in a log line.
    """
    settings = KnowledgeSettings(enabled=True, db_url=dsn)

    with caplog.at_level("WARNING"):
        with pytest.raises(KnowledgeUnavailableError) as caught:
            async with knowledge_session(settings):
                pass

    assert caught.value.code == "knowledge_unavailable"
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert logged, "nothing was logged, so this proved nothing about what is logged"
    assert dsn not in logged
    assert "psycopg2" not in logged


# --- The control plane's housekeeping cannot reach the corpus ---------------


def test_the_suite_truncation_leaves_the_corpus_standing(corpus: Corpus) -> None:
    """The reason the corpus is a second database, executed rather than argued.

    ``tests/conftest.py`` truncates every table in the control plane's schema
    before every test. Were the corpus tables in that schema, this suite would
    empty the mirror between tests and every retrieval test would be asserting
    against whatever it had just written. Two databases is what makes that
    impossible, and this is the check that the two are still two.
    """
    seed(corpus, **handbook())

    _truncate_all_tables()

    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            documents = conn.execute(sa.text("SELECT count(*) FROM documents")).scalar_one()
            chunks = conn.execute(sa.text("SELECT count(*) FROM chunks")).scalar_one()
    finally:
        engine.dispose()

    assert documents == 2
    assert chunks >= 2


def test_the_truncation_would_have_emptied_a_table_it_could_reach(corpus: Corpus) -> None:
    """The positive half: the housekeeping under test does empty what it owns."""
    with control_plane_engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (namespace_key, name) VALUES ('default', 'knowledge-probe') "
                "ON CONFLICT DO NOTHING"
            )
        )
        planted = conn.execute(sa.text("SELECT count(*) FROM agents")).scalar_one()
    assert planted >= 1

    _truncate_all_tables()

    with control_plane_engine.begin() as conn:
        remaining = conn.execute(sa.text("SELECT count(*) FROM agents")).scalar_one()
    assert remaining == 0


def test_no_corpus_table_is_in_the_control_planes_migration_surface() -> None:
    """Alembic autogenerates against ``Base.metadata`` and must not see these.

    A corpus schema owned by a different process, inside the control plane's
    autogenerate surface, produces a migration that drops it. The separation
    is the protection, and it holds only while the two metadata objects stay
    disjoint.
    """
    control_plane = set(Base.metadata.tables)
    corpus = set(KNOWLEDGE_METADATA.tables)

    assert corpus, "the corpus metadata is empty and this check stopped checking anything"
    assert not control_plane & corpus, sorted(control_plane & corpus)


# --- The writer's credential is the only one that can write -----------------


def test_the_readers_credential_cannot_run_the_seed_helper(corpus: Corpus) -> None:
    """The one writer in the package refuses to be handed the reader's DSN.

    Not by checking, which could be forgotten, but by being refused by
    Postgres. That is what makes "the control plane cannot write to the corpus"
    a property of the deployment rather than of this package's discipline.
    """
    with pytest.raises((sa.exc.ProgrammingError, psycopg.errors.InsufficientPrivilege)) as caught:
        seed_corpus(
            corpus.read_url,
            source_ref="attempted",
            source_name="Attempted",
            docs=[SeedDocument(path="Attempted/laptops.md", body=LAPTOPS)],
        )

    assert "permission denied" in str(caught.value)

    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT count(*) FROM sources")).scalar_one() == 0
    finally:
        engine.dispose()


def test_the_reader_cannot_reach_the_corpus_through_a_function_either(corpus: Corpus) -> None:
    """SELECT and nothing else, including nothing that writes on its behalf."""
    engine = sa.create_engine(corpus.read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            for statement in (
                "TRUNCATE chunks",
                "DROP TABLE chunks",
                "CREATE TABLE smuggled (id integer)",
                "UPDATE sources SET enabled = false",
            ):
                with pytest.raises(sa.exc.ProgrammingError) as caught:
                    conn.execute(sa.text(statement))
                assert "permission denied" in str(caught.value) or "must be owner" in str(
                    caught.value
                )
                conn.rollback()
    finally:
        engine.dispose()


def test_the_reader_can_still_reach_its_own_corpus(corpus: Corpus) -> None:
    """The positive that keeps the refusals above from passing in a dead state."""
    seed(corpus, **handbook())
    conn = connect(corpus.database, READ_ROLE, READ_PASSWORD)
    try:
        assert scalar(conn, "SELECT count(*) FROM documents") == 2
    finally:
        conn.close()
