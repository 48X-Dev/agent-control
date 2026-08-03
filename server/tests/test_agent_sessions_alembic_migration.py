"""Alembic coverage for the agent sessions and executor bindings migration.

Mirrors ``test_teams_alembic_migration.py``: the tables, constraints and indexes
the revision adds, that it touches nothing that was already there, and that
downgrading leaves no residue.

One assertion here is doing more than schema bookkeeping.
``uq_agent_sessions_executor_global`` must not carry ``namespace_key``. The
executor's own session store has no namespace concept, so this table is the only
boundary between one namespace's transcripts and another's; scoped per
namespace, the constraint would permit a row in namespace B pointing at exactly
the same executor session as a namespace-A row. If somebody "fixes" that
constraint by adding the namespace to it, this file is what fails.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

import agent_control_server.models  # noqa: F401  registers the ORM tables
from agent_control_server.config import db_config
from agent_control_server.db import Base
from alembic import command

SERVER_DIR = Path(__file__).resolve().parents[1]
PRE_MIGRATION_REVISION = "b6f1c92d4a07"
MIGRATION_REVISION = "c8d1e5a3f720"
SESSION_TABLES = ("agent_runtimes", "agent_sessions")
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Agent session Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    """Yield the URL of an empty database that is dropped on the way out."""
    temp_db_name = f"agent_control_sessions_{uuid.uuid4().hex[:12]}"
    admin_url = _BASE_DB_URL.set(database="postgres").render_as_string(hide_password=False)
    target_url = _BASE_DB_URL.set(database=temp_db_name).render_as_string(hide_password=False)

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{temp_db_name}"'))
    admin_engine.dispose()

    try:
        yield target_url
    finally:
        cleanup_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with cleanup_engine.connect() as conn:
            conn.execute(
                text(
                    """
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity
                    WHERE datname = :db_name AND pid <> pg_backend_pid()
                    """
                ),
                {"db_name": temp_db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{temp_db_name}"'))
        cleanup_engine.dispose()


@pytest.fixture
def temp_db_url() -> str:
    with _temp_database() as url:
        yield url


@pytest.fixture
def alembic_config(temp_db_url: str) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", temp_db_url)
    return cfg


@pytest.fixture
def temp_engine(temp_db_url: str) -> Engine:
    engine = create_engine(temp_db_url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def upgrade_to(alembic_config: Config):
    def _upgrade(revision: str) -> None:
        command.upgrade(alembic_config, revision)

    return _upgrade


@pytest.fixture
def downgrade_to(alembic_config: Config):
    def _downgrade(revision: str) -> None:
        command.downgrade(alembic_config, revision)

    return _downgrade


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names(schema="public"))


def _current_revision(engine: Engine) -> str | None:
    with engine.begin() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_pre_migration_revision_has_no_session_tables(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(SESSION_TABLES)


def test_upgrade_creates_the_binding_table(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_runtimes")} == {
        "namespace_key",
        "agent_name",
        "executor_kind",
        "base_url",
        "executor_app_name",
        "enabled",
        "created_at",
        "updated_at",
    }
    pk = inspector.get_pk_constraint("agent_runtimes")
    assert pk["constrained_columns"] == ["namespace_key", "agent_name"]

    foreign_keys = inspector.get_foreign_keys("agent_runtimes")
    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk["referred_table"] == "agents"
    assert fk["constrained_columns"] == ["namespace_key", "agent_name"]
    assert fk["options"]["ondelete"] == "CASCADE"


def test_upgrade_creates_the_session_table(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_sessions")} == {
        "id",
        "namespace_key",
        "session_key",
        "agent_name",
        "team_id",
        "executor_kind",
        "executor_app_name",
        "executor_user_id",
        "executor_session_id",
        "title",
        "status",
        "created_by_hash",
        "last_trace_id",
        "in_flight_since",
        "in_flight_trace_id",
        "last_activity_at",
        "created_at",
        "updated_at",
    }

    constraints = {
        c["name"]: c["column_names"]
        for c in inspector.get_unique_constraints("agent_sessions")
    }
    assert constraints["uq_agent_sessions_namespace_key"] == [
        "namespace_key",
        "session_key",
    ]
    assert constraints["uq_agent_sessions_namespace_id"] == ["namespace_key", "id"]
    # Global on purpose: this constraint exists to prevent one namespace
    # adopting another's executor session, so it must not be namespace-scoped.
    assert constraints["uq_agent_sessions_executor_global"] == [
        "executor_app_name",
        "executor_user_id",
        "executor_session_id",
    ]

    # team_id deliberately carries no foreign key; a composite ON DELETE SET
    # NULL would try to null namespace_key and abort the team delete.
    assert inspector.get_foreign_keys("agent_sessions") == []

    index_names = {index["name"] for index in inspector.get_indexes("agent_sessions")}
    assert {
        "idx_agent_sessions_agent_recent",
        "idx_agent_sessions_in_flight",
        "idx_agent_sessions_team",
    } <= index_names


def test_upgrade_leaves_pre_existing_tables_untouched(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == set(SESSION_TABLES)


def test_downgrade_leaves_no_residue(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    tables_before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine) == tables_before
    assert _current_revision(temp_engine) == PRE_MIGRATION_REVISION

    with temp_engine.begin() as conn:
        remaining = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND indexname LIKE '%agent_sessions%'"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_populated_tables(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_sessions (namespace_key, session_key, agent_name, "
                "executor_app_name, executor_user_id, executor_session_id) "
                "VALUES ('ns-one', 'aaaa', 'chat-agent-one', 'app', 'ns-one:u', 's')"
            )
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(SESSION_TABLES)


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert set(SESSION_TABLES).issubset(_table_names(temp_engine))
    assert _current_revision(temp_engine) == MIGRATION_REVISION


# ---------------------------------------------------------------------------
# The migration and the ORM have to agree
# ---------------------------------------------------------------------------


def _shape_of(engine: Engine, table: str) -> dict[str, Any]:
    """Everything about a table that a query could notice, as plain data."""
    inspector = inspect(engine)
    return {
        "columns": sorted(
            (col["name"], str(col["type"]), bool(col["nullable"]))
            for col in inspector.get_columns(table)
        ),
        "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
        "unique": sorted(
            (constraint["name"], tuple(constraint["column_names"]))
            for constraint in inspector.get_unique_constraints(table)
        ),
        "indexes": sorted(
            (index["name"], tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(table)
        ),
        "foreign_keys": sorted(
            (
                fk["referred_table"],
                tuple(fk["constrained_columns"]),
                tuple(fk["referred_columns"]),
                (fk.get("options") or {}).get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(table)
        ),
    }


@pytest.mark.parametrize("table", SESSION_TABLES)
def test_the_migration_builds_what_the_orm_describes(
    upgrade_to, temp_engine: Engine, table: str
) -> None:
    """The suite tests one schema and production runs another.

    ``conftest`` builds the test database from ``Base.metadata.create_all``;
    every deployment builds it from these revisions. Nothing else in this repo
    compares the two, so a column that exists only in the ORM would leave the
    whole suite green and fail on the first request after a real upgrade. This
    is the assertion that would have to fail first.

    Upgraded to ``head`` rather than to this file's own revision, and that is a
    correction rather than a widening. The ORM side of the comparison is always
    today's ``Base.metadata``, so pinning the migration side to one revision
    only agreed while no later revision touched these tables. The first one that
    did - ``d7e4a91c60b2``, which adds ``agent_sessions.agent_task_id`` - made
    the two disagree by construction and turned a real invariant into a
    tripwire for anybody extending the table. Every other assertion in this
    module still pins ``MIGRATION_REVISION``, because those are about what this
    revision itself does.
    """
    upgrade_to("head")
    migrated = _shape_of(temp_engine, table)

    with _temp_database() as orm_db_url:
        orm_engine = create_engine(orm_db_url, future=True)
        try:
            Base.metadata.create_all(bind=orm_engine)
            declared = _shape_of(orm_engine, table)
        finally:
            orm_engine.dispose()

    assert migrated == declared
