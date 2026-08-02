"""Alembic coverage for the nudge and halt migration.

Mirrors ``test_agent_sessions_alembic_migration.py``: upgrade, downgrade,
residue sweep, upgrade-downgrade-upgrade, and the comparison between what the
revision builds and what the ORM declares. That last one matters more than it
looks: the suite runs against ``Base.metadata.create_all`` and every deployment
runs against these revisions, so a column that exists in only one of them would
leave the whole suite green and fail on the first request after a real upgrade.

Two assertions here are load-bearing rather than bookkeeping.

Both foreign keys must be **composite** on ``(namespace_key, session_id)``.
Scoped to ``session_id`` alone, a row in one namespace could reference a
session in another, and these tables are the boundary the executor's own
namespace-blind store does not have.

``uq_agent_session_halts_turn`` must be a real unique constraint over all three
columns. A halt is a latch, and the constraint is what makes a double-click one
event by construction. Weaken it to a partial index over live statuses and a
double-click becomes two rows, which the transcript renders as two stops that
never happened.
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
PRE_MIGRATION_REVISION = "c8d1e5a3f720"
MIGRATION_REVISION = "f4c7a2b9e310"
PHASE_FIVE_TABLES = ("agent_session_halts", "agent_session_nudges")
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Nudge and halt Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    temp_db_name = f"agent_control_halts_{uuid.uuid4().hex[:12]}"
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


# ---------------------------------------------------------------------------
# What the revision adds
# ---------------------------------------------------------------------------


def test_the_previous_revision_has_neither_table(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(PHASE_FIVE_TABLES)


def test_upgrade_creates_the_nudge_queue(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_session_nudges")} == {
        "id",
        "namespace_key",
        "session_id",
        "body",
        "status",
        "created_by_hash",
        "created_at",
        "claimed_at",
        "claimed_by",
        "claim_expires_at",
        "applied_at",
        "applied_trace_id",
        "claim_count",
        "injection_attempts",
        "rejected_by_control",
    }

    (fk,) = inspector.get_foreign_keys("agent_session_nudges")
    assert fk["constrained_columns"] == ["namespace_key", "session_id"], (
        "a session_id-only key would let one namespace's nudge point at "
        "another namespace's session"
    )
    assert fk["referred_table"] == "agent_sessions"
    assert fk["referred_columns"] == ["namespace_key", "id"]
    assert fk["options"]["ondelete"] == "CASCADE"

    indexes = {index["name"] for index in inspector.get_indexes("agent_session_nudges")}
    assert "idx_agent_session_nudges_drain" in indexes


def test_upgrade_creates_the_halt_latch(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_session_halts")} == {
        "id",
        "namespace_key",
        "session_id",
        "target_trace_id",
        "mode",
        "status",
        "created_by_hash",
        "created_at",
        "applied_at",
        "applied_at_boundary",
        "applied_tool_name",
        "turn_ended_at",
    }
    # The halt table has no claim columns, and that absence is the design: claim
    # and apply are one transaction, so there is no window for them to describe.
    assert {"claimed_at", "claimed_by", "claim_expires_at", "claim_count"}.isdisjoint(
        {c["name"] for c in inspector.get_columns("agent_session_halts")}
    )

    constraints = {
        c["name"]: c["column_names"]
        for c in inspector.get_unique_constraints("agent_session_halts")
    }
    assert constraints["uq_agent_session_halts_turn"] == [
        "namespace_key",
        "session_id",
        "target_trace_id",
    ]

    (fk,) = inspector.get_foreign_keys("agent_session_halts")
    assert fk["constrained_columns"] == ["namespace_key", "session_id"]
    assert fk["referred_columns"] == ["namespace_key", "id"]
    assert fk["options"]["ondelete"] == "CASCADE"


def test_one_turn_can_only_ever_hold_one_halt(
    upgrade_to, temp_engine: Engine
) -> None:
    """The idempotence of a double-click, enforced by the database.

    Asserted as a rejected write rather than as a constraint name, because the
    service is allowed to change how it handles the conflict and is not allowed
    to be the only thing preventing two stops for one turn.
    """
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)

    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES ('ns-one', :sid, 'trace-a')"
            ),
            {"sid": session_id},
        )

    with pytest.raises(IntegrityError), temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES ('ns-one', :sid, 'trace-a')"
            ),
            {"sid": session_id},
        )

    # A different turn on the same session is a different halt, though.
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES ('ns-one', :sid, 'trace-b')"
            ),
            {"sid": session_id},
        )
        assert (
            conn.execute(text("SELECT count(*) FROM agent_session_halts")).scalar() == 2
        )


def test_deleting_a_session_takes_its_nudges_and_halts_with_it(
    upgrade_to, temp_engine: Engine
) -> None:
    """Both cascades, because an orphaned nudge is guidance for nobody."""
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)

    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_nudges "
                "  (namespace_key, session_id, body) "
                "VALUES ('ns-one', :sid, 'guidance')"
            ),
            {"sid": session_id},
        )
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES ('ns-one', :sid, 'trace-a')"
            ),
            {"sid": session_id},
        )
        conn.execute(
            text("DELETE FROM agent_sessions WHERE id = :sid"), {"sid": session_id}
        )
        assert (
            conn.execute(text("SELECT count(*) FROM agent_session_nudges")).scalar() == 0
        )
        assert (
            conn.execute(text("SELECT count(*) FROM agent_session_halts")).scalar() == 0
        )


def test_a_row_cannot_reference_a_session_in_another_namespace(
    upgrade_to, temp_engine: Engine
) -> None:
    """The composite key, asserted as a refused write.

    This is the assertion that fails if somebody ever "simplifies" the foreign
    key down to ``session_id``.
    """
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine, namespace="ns-one")

    with pytest.raises(IntegrityError), temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_nudges "
                "  (namespace_key, session_id, body) "
                "VALUES ('ns-two', :sid, 'not yours')"
            ),
            {"sid": session_id},
        )

    with pytest.raises(IntegrityError), temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES ('ns-two', :sid, 'trace-a')"
            ),
            {"sid": session_id},
        )


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------


def test_upgrade_leaves_pre_existing_tables_untouched(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == set(PHASE_FIVE_TABLES)


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
                "AND (indexname LIKE '%agent_session_nudges%' "
                "     OR indexname LIKE '%agent_session_halts%')"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_populated_tables(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    session_id = _seed_session(temp_engine)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_nudges (namespace_key, session_id, body) "
                "VALUES ('ns-one', :sid, 'guidance')"
            ),
            {"sid": session_id},
        )
        conn.execute(
            text(
                "INSERT INTO agent_session_halts "
                "  (namespace_key, session_id, target_trace_id) "
                "VALUES ('ns-one', :sid, 'trace-a')"
            ),
            {"sid": session_id},
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(PHASE_FIVE_TABLES)


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert set(PHASE_FIVE_TABLES).issubset(_table_names(temp_engine))
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


@pytest.mark.parametrize("table", PHASE_FIVE_TABLES)
def test_the_migration_builds_what_the_orm_describes(
    upgrade_to, temp_engine: Engine, table: str
) -> None:
    upgrade_to(MIGRATION_REVISION)
    migrated = _shape_of(temp_engine, table)

    with _temp_database() as orm_db_url:
        orm_engine = create_engine(orm_db_url, future=True)
        try:
            Base.metadata.create_all(bind=orm_engine)
            declared = _shape_of(orm_engine, table)
        finally:
            orm_engine.dispose()

    assert migrated == declared
