"""Alembic coverage for the plan-steps migration.

Same shape as ``test_agent_halts_alembic_migration.py``, and it exists for the
same reason: the suite runs against ``Base.metadata.create_all`` while every
deployment runs against these revisions, so a column present in only one of
them leaves the whole suite green and fails on the first request after a real
upgrade.

Three assertions here are load-bearing rather than bookkeeping.

The primary key must carry ``plan_revision``. Drop it and a re-declared plan
collides with the one it replaced, which turns "the agent replanned" into
either a lost record or a 500, depending on which way somebody then patches it.

The foreign key must be **composite** on ``(namespace_key, session_id)``.
Scoped to ``session_id`` alone, one namespace's plan could hang off another
namespace's session - and this composite key is the only boundary there is,
because the executor's own session store has no namespace concept at all.

``declared_at`` and ``updated_at`` must both exist. One is a fact about the
past and the other moves; a table with only ``updated_at`` would have to derive
the declaration time from the earliest step update, which is right until the
last step is marked and then quietly reports a declaration that happened later
than it did.
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
from sqlalchemy.exc import IntegrityError

import agent_control_server.models  # noqa: F401  registers the ORM tables
from agent_control_server.config import db_config
from agent_control_server.db import Base
from alembic import command

SERVER_DIR = Path(__file__).resolve().parents[1]
PRE_MIGRATION_REVISION = "f4c7a2b9e310"
MIGRATION_REVISION = "a1c4e7b93d80"
PLAN_TABLE = "agent_session_plan_steps"
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Plan-step Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    temp_db_name = f"agent_control_plans_{uuid.uuid4().hex[:12]}"
    admin_url = _BASE_DB_URL.set(database="postgres").render_as_string(
        hide_password=False
    )
    target_url = _BASE_DB_URL.set(database=temp_db_name).render_as_string(
        hide_password=False
    )

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


def _seed_session(engine: Engine, *, namespace: str = "ns-one") -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO agent_sessions (namespace_key, session_key, "
                    "  agent_name, executor_app_name, executor_user_id, "
                    "  executor_session_id) "
                    "VALUES (:ns, :key, 'chat-agent', 'app', :user, :sid) "
                    "RETURNING id"
                ),
                {
                    "ns": namespace,
                    "key": uuid.uuid4().hex,
                    "user": f"{namespace}:u",
                    "sid": uuid.uuid4().hex,
                },
            ).scalar_one()
        )


def _insert_step(
    engine: Engine,
    *,
    namespace: str,
    session_id: int,
    revision: int = 1,
    index: int = 0,
    title: str = "a step",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_plan_steps "
                "  (namespace_key, session_id, plan_revision, step_index, title) "
                "VALUES (:ns, :sid, :rev, :idx, :title)"
            ),
            {
                "ns": namespace,
                "sid": session_id,
                "rev": revision,
                "idx": index,
                "title": title,
            },
        )


# ---------------------------------------------------------------------------
# What the revision adds
# ---------------------------------------------------------------------------


def test_the_previous_revision_has_no_plan_table(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert PLAN_TABLE not in _table_names(temp_engine)


def test_upgrade_creates_the_plan_table_with_both_clocks(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns(PLAN_TABLE)} == {
        "namespace_key",
        "session_id",
        "plan_revision",
        "step_index",
        "title",
        "status",
        "note",
        "declared_at",
        "updated_at",
    }
    # No surrogate id and no percentage-shaped column. There is nothing here to
    # count but the rows themselves.
    assert "id" not in {c["name"] for c in inspector.get_columns(PLAN_TABLE)}


def test_the_primary_key_carries_the_revision(upgrade_to, temp_engine: Engine) -> None:
    """Without it a replan collides with the plan it replaced."""
    upgrade_to(MIGRATION_REVISION)

    assert inspect(temp_engine).get_pk_constraint(PLAN_TABLE)[
        "constrained_columns"
    ] == ["namespace_key", "session_id", "plan_revision", "step_index"]


def test_the_foreign_key_is_composite_and_cascades(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)

    (fk,) = inspect(temp_engine).get_foreign_keys(PLAN_TABLE)
    assert fk["constrained_columns"] == ["namespace_key", "session_id"], (
        "a session_id-only key would let one namespace's plan point at another "
        "namespace's session"
    )
    assert fk["referred_table"] == "agent_sessions"
    assert fk["referred_columns"] == ["namespace_key", "id"]
    assert fk["options"]["ondelete"] == "CASCADE"


def test_a_replan_and_its_predecessor_coexist(upgrade_to, temp_engine: Engine) -> None:
    """Asserted as an accepted write rather than a constraint name.

    The service is allowed to change how it allocates revisions; it is not
    allowed to be the only thing that lets a superseded plan survive.
    """
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)

    _insert_step(temp_engine, namespace="ns-one", session_id=session_id, revision=1)
    _insert_step(temp_engine, namespace="ns-one", session_id=session_id, revision=2)

    with temp_engine.begin() as conn:
        assert (
            conn.execute(
                text(f"SELECT count(*) FROM {PLAN_TABLE}")  # noqa: S608 - fixed name
            ).scalar()
            == 2
        )

    # But one revision cannot hold the same step index twice, which is what
    # keeps indexes dense and addressable.
    with pytest.raises(IntegrityError):
        _insert_step(
            temp_engine, namespace="ns-one", session_id=session_id, revision=2
        )


def test_a_plan_row_cannot_reference_a_session_in_another_namespace(
    upgrade_to, temp_engine: Engine
) -> None:
    """The assertion that fails if the composite key is ever "simplified"."""
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine, namespace="ns-one")

    with pytest.raises(IntegrityError):
        _insert_step(temp_engine, namespace="ns-two", session_id=session_id)


def test_deleting_a_session_takes_its_plan_with_it(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)
    _insert_step(temp_engine, namespace="ns-one", session_id=session_id)

    with temp_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM agent_sessions WHERE id = :sid"), {"sid": session_id}
        )
        assert (
            conn.execute(
                text(f"SELECT count(*) FROM {PLAN_TABLE}")  # noqa: S608 - fixed name
            ).scalar()
            == 0
        )


def test_a_step_defaults_to_pending_and_to_revision_one(
    upgrade_to, temp_engine: Engine
) -> None:
    """A row that says nothing about itself says "not marked", not "done"."""
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)

    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_plan_steps "
                "  (namespace_key, session_id, step_index, title) "
                "VALUES ('ns-one', :sid, 0, 'a step')"
            ),
            {"sid": session_id},
        )
        row = conn.execute(
            text(
                "SELECT plan_revision, status, note, declared_at, updated_at "
                f"  FROM {PLAN_TABLE}"  # noqa: S608 - fixed name
            )
        ).one()

    assert row[0] == 1
    assert row[1] == "pending"
    assert row[2] is None
    assert row[3] is not None and row[4] is not None


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------


def test_upgrade_leaves_pre_existing_tables_untouched(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == {PLAN_TABLE}


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
                "AND indexname LIKE '%agent_session_plan_steps%'"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_a_populated_table(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)
    _insert_step(temp_engine, namespace="ns-one", session_id=session_id)

    downgrade_to(PRE_MIGRATION_REVISION)

    assert PLAN_TABLE not in _table_names(temp_engine)


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert PLAN_TABLE in _table_names(temp_engine)
    assert _current_revision(temp_engine) == MIGRATION_REVISION


# ---------------------------------------------------------------------------
# The migration and the ORM have to agree
# ---------------------------------------------------------------------------


def _shape_of(engine: Engine, table: str) -> dict[str, Any]:
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


def test_the_migration_builds_what_the_orm_describes(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    migrated = _shape_of(temp_engine, PLAN_TABLE)

    with _temp_database() as orm_db_url:
        orm_engine = create_engine(orm_db_url, future=True)
        try:
            Base.metadata.create_all(bind=orm_engine)
            declared = _shape_of(orm_engine, PLAN_TABLE)
        finally:
            orm_engine.dispose()

    assert migrated == declared
