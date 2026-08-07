"""The corpus pool: bounded, lazy, its own, and never a hang.

The engine's docstring makes three promises, and each one is a promise about a
failure that has already cost this repo something elsewhere. A pool shared with
the control plane means a knowledge database that stops answering drains the
connections agent turns need, and chat goes down with a feature nobody enabled.
An engine built at import means a malformed DSN crashes startup for a corpus
that is off by default. A query with no ceiling means a turn waiting on
Postgres rather than a refusal it can act on.

None of those are visible in a green suite unless something asserts them, so
this file connects and measures rather than reading the constructor.
"""

from __future__ import annotations

import time

import pytest
import sqlalchemy as sa
from agent_control_models.knowledge import KnowledgeRefusalCode
from agent_control_server.db import async_engine as control_plane_engine
from agent_control_server.knowledge import (
    KnowledgeUnavailableError,
    dispose_knowledge_engine,
    knowledge_session,
)
from agent_control_server.knowledge import engine as knowledge_engine
from agent_control_server.knowledge.schema import SUPPORTED_SCHEMA_VERSIONS
from sqlalchemy.pool import NullPool

from tests.knowledge.support import settings_for
from tests.knowledge_provisioning import Corpus

_CURRENT_SCHEMA_VERSION = max(SUPPORTED_SCHEMA_VERSIONS)


async def test_no_pool_exists_until_a_caller_asks_for_one(corpus: Corpus) -> None:
    """Configured is not connected.

    ``db.py`` builds its engine at import because the server cannot run without
    that database. This one is optional, so the DSN is not even parsed until a
    search happens, and a deployment with a wrong DSN and the feature off still
    boots.
    """
    assert knowledge_engine._engine is None

    settings = settings_for(corpus)
    assert settings.is_configured()
    assert knowledge_engine._engine is None

    async with knowledge_session(settings) as session:
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1

    assert knowledge_engine._engine is not None


async def test_the_pool_is_small_and_cannot_grow(corpus: Corpus) -> None:
    """Two connections and no overflow, so an outage cannot become a stampede.

    ``max_overflow=0`` is the half that matters: a pool that can grow under
    load turns a slow corpus into as many blocked backends as there are
    requests.
    """
    settings = settings_for(corpus, pool_size=2)

    async with knowledge_session(settings) as session:
        await session.execute(sa.text("SELECT 1"))

    engine = knowledge_engine._engine
    assert engine is not None
    pool = engine.sync_engine.pool
    assert pool.size() == settings.pool_size
    # Private, and the only spelling SQLAlchemy offers for the bound itself.
    assert pool._max_overflow == 0


async def test_a_corpus_query_never_borrows_the_control_planes_connections(
    corpus: Corpus,
) -> None:
    """The pool agent turns depend on is untouched while a search is open."""
    before = control_plane_engine.sync_engine.pool.checkedout()

    async with knowledge_session(settings_for(corpus)) as session:
        await session.execute(sa.text("SELECT count(*) FROM documents"))
        during = control_plane_engine.sync_engine.pool.checkedout()

    assert during == before
    assert knowledge_engine._engine is not control_plane_engine
    assert knowledge_engine._engine is not None
    assert knowledge_engine._engine.url.database != control_plane_engine.url.database


async def test_a_tighter_ceiling_is_honoured_rather_than_dropped(corpus: Corpus) -> None:
    """The cache is keyed on what shapes the engine, not on the DSN alone.

    ``knowledge_session`` takes settings per call, so a caller that narrows the
    statement ceiling for one query and silently gets the pool built for
    whoever asked first has a parameter that works or not depending on call
    order. The DSN is deliberately identical on both sides here; only the
    ceiling moves.
    """
    async with knowledge_session(settings_for(corpus, statement_timeout_seconds=8)) as session:
        first = (
            await session.execute(sa.text("SELECT current_setting('statement_timeout')"))
        ).scalar_one()
    first_engine = knowledge_engine._engine

    async with knowledge_session(settings_for(corpus, statement_timeout_seconds=2)) as session:
        second = (
            await session.execute(sa.text("SELECT current_setting('statement_timeout')"))
        ).scalar_one()

    assert first in {"8s", "8000ms"}
    assert second in {"2s", "2000ms"}
    assert knowledge_engine._engine is not first_engine


async def test_repointing_the_dsn_builds_a_new_pool_rather_than_reusing_the_old(
    corpus: Corpus,
) -> None:
    """A cached engine keyed on nothing would answer as the previous role.

    Which is the failure a test suite meets first: it repoints at a throwaway
    corpus, gets the last one, and proves something about a database it is not
    using.
    """
    async with knowledge_session(settings_for(corpus)) as session:
        reader = (await session.execute(sa.text("SELECT current_user"))).scalar_one()
    first_engine = knowledge_engine._engine

    async with knowledge_session(settings_for(corpus, db_url=corpus.sync_url)) as session:
        writer = (await session.execute(sa.text("SELECT current_user"))).scalar_one()

    assert reader == "knowledge_read"
    assert writer == "knowledge_sync"
    assert knowledge_engine._engine is not first_engine


# --- The two failures a turn must survive -----------------------------------


async def test_the_statement_timeout_reaches_the_backend(corpus: Corpus) -> None:
    """Pool timeouts bound waiting for a connection; this bounds the work."""
    async with knowledge_session(settings_for(corpus, statement_timeout_seconds=3)) as session:
        configured = (
            await session.execute(sa.text("SELECT current_setting('statement_timeout')"))
        ).scalar_one()

    assert configured in {"3s", "3000ms"}


async def test_a_query_that_outruns_the_ceiling_is_a_refusal_and_not_a_wait(
    corpus: Corpus,
) -> None:
    """The whole point of the ceiling: the turn proceeds.

    Both halves are asserted, because either one alone is a false comfort. It
    has to come back quickly, and it has to come back as a code from the closed
    enum rather than as whatever Postgres says about cancelled statements.
    """
    started = time.monotonic()

    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(
            settings_for(corpus, statement_timeout_seconds=0.5)
        ) as session:
            await session.execute(sa.text("SELECT pg_sleep(10)"))

    elapsed = time.monotonic() - started
    assert elapsed < 5
    assert caught.value.code == KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE
    message = str(caught.value).lower()
    for leak in ("psycopg", "canceling", "statement timeout", "pg_sleep"):
        assert leak not in message


async def test_a_write_through_the_readers_session_refuses_without_quoting_postgres(
    corpus: Corpus,
) -> None:
    """Read-only by credential, and the refusal says nothing a model can use.

    The reader's SELECT-only grant is proved elsewhere at the driver. What
    matters here is the shape the caller sees: the same typed refusal an
    unreachable corpus produces, carrying no table name and no error text.
    """
    with pytest.raises(KnowledgeUnavailableError) as caught:
        async with knowledge_session(settings_for(corpus)) as session:
            await session.execute(
                sa.text(
                    "INSERT INTO sources (kind, ref, display_name, trust) "
                    "VALUES ('drive_folder', 'smuggled', 'smuggled', 'workspace')"
                )
            )

    assert caught.value.code == KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE
    message = str(caught.value).lower()
    assert "permission denied" not in message
    assert "sources" not in message


async def test_a_corpus_this_server_cannot_read_refuses_rather_than_guessing(
    corpus: Corpus,
) -> None:
    """Plan 4.1's version check, on the first query through a pool.

    A sync that has moved the schema ahead of the reader hands back rows whose
    columns mean something else. Mis-parsing them is worse than refusing, and
    the refusal is the same closed-enum code an unreachable corpus produces:
    an agent learns the base could not be consulted, never a version number.
    """
    _stamp_schema_version(corpus, 99)
    try:
        with pytest.raises(KnowledgeUnavailableError) as caught:
            async with knowledge_session(settings_for(corpus)) as session:
                await session.execute(sa.text("SELECT count(*) FROM documents"))

        assert caught.value.code == KnowledgeRefusalCode.KNOWLEDGE_UNAVAILABLE
        assert "99" not in str(caught.value)
    finally:
        _stamp_schema_version(corpus, _CURRENT_SCHEMA_VERSION)


async def test_the_version_is_read_once_per_pool_and_not_once_per_query(
    corpus: Corpus,
) -> None:
    """Cheap enough to be honest: the check is a pool property, not a tax.

    Startup is the wrong place for it (the server must boot with no corpus at
    all) and every query is the wasteful one, so it lands on the first query
    through each pool and stays there until the pool is rebuilt.
    """
    async with knowledge_session(settings_for(corpus)) as session:
        await session.execute(sa.text("SELECT 1"))
    checked = knowledge_engine._schema_checked

    async with knowledge_session(settings_for(corpus)) as session:
        await session.execute(sa.text("SELECT 1"))

    assert checked is not None
    assert knowledge_engine._schema_checked == checked
    assert knowledge_engine._schema_checked == knowledge_engine._engine_key


def _stamp_schema_version(corpus: Corpus, version: int) -> None:
    """Move the marker row, which only the sync role may do."""
    engine = sa.create_engine(corpus.sync_url, future=True, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE schema_meta SET version = :version"), {"version": version})
    finally:
        engine.dispose()


async def test_disposing_the_pool_twice_leaves_a_usable_store(corpus: Corpus) -> None:
    """Shutdown runs it, and so does every test that repoints the DSN."""
    async with knowledge_session(settings_for(corpus)) as session:
        await session.execute(sa.text("SELECT 1"))

    await dispose_knowledge_engine()
    await dispose_knowledge_engine()
    assert knowledge_engine._engine is None

    async with knowledge_session(settings_for(corpus)) as session:
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
