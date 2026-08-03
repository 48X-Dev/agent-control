"""Alembic coverage for the dispatch ledger revision.

Mirrors ``test_agent_sessions_alembic_migration.py``: what the revision adds,
that it touches nothing that was already there, that downgrading leaves no
residue, and that the schema a deployment gets from these revisions is the
schema the ORM describes.

Three assertions here are doing more than schema bookkeeping, and each would
be a silent correctness bug rather than a cosmetic drift.

``ux_agent_tasks_open_source_ref`` must be **unique** and **partial**. Unique is
what makes "the same tracker item worked twice" impossible for two dispatchers,
two replicas and a double-clicked button at once, in the database rather than
in a handler. Partial over exactly the three terminal statuses is what stops a
finished task blocking the same item next month, because reopened issues are
real. Both halves are asserted by inserting rows rather than by reading a
predicate string, because a predicate that reads correctly and excludes the
wrong statuses looks identical.

The foreign key on ``agent_task_steps`` must be **composite** on
``(namespace_key, task_id)``. A single-column version would let a step row in
one namespace reference a task in another, and every namespace assertion
elsewhere in the suite would still pass.

``agent_sessions.agent_task_id`` must carry **no** foreign key. A composite
``ON DELETE SET NULL`` would try to null ``namespace_key`` alongside it, and
that column is NOT NULL, so deleting a task with a live session would abort.
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
PRE_MIGRATION_REVISION = "e2b7d4a15c93"
MIGRATION_REVISION = "d7e4a91c60b2"
LEDGER_TABLES = ("agent_tasks", "agent_task_steps")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
HELD_STATUSES = ("queued", "running", "blocked", "paused_quota", "running_unknown")
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Dispatch ledger Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    """Yield the URL of an empty database that is dropped on the way out."""
    temp_db_name = f"agent_control_tasks_{uuid.uuid4().hex[:12]}"
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
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    " WHERE datname = :db_name AND pid <> pg_backend_pid()"
                ),
                {"db_name": temp_db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{temp_db_name}"'))
        cleanup_engine.dispose()


@pytest.fixture
def temp_db_url() -> Iterator[str]:
    with _temp_database() as url:
        yield url


@pytest.fixture
def alembic_config(temp_db_url: str) -> Config:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", temp_db_url)
    return cfg


@pytest.fixture
def temp_engine(temp_db_url: str) -> Iterator[Engine]:
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


def _insert_task(
    engine: Engine,
    *,
    namespace_key: str = "default",
    source_kind: str = "linear",
    source_ref: str = "ISS-1",
    status: str = "queued",
    task_key: str | None = None,
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO agent_tasks (namespace_key, task_key, source_kind, "
                    "  source_ref, title, workflow_key, status, deadline_at) "
                    "VALUES (:ns, :key, :kind, :ref, 'a title', 'default', :status, "
                    "        now() + interval '1 hour') "
                    "RETURNING id"
                ),
                {
                    "ns": namespace_key,
                    "key": task_key or uuid.uuid4().hex,
                    "kind": source_kind,
                    "ref": source_ref,
                    "status": status,
                },
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_pre_migration_revision_has_no_ledger_tables(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(LEDGER_TABLES)
    assert "agent_task_id" not in {
        column["name"] for column in inspect(temp_engine).get_columns("agent_sessions")
    }


def test_upgrade_creates_the_task_table(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_tasks")} == {
        "id",
        "namespace_key",
        "task_key",
        "source_kind",
        "source_ref",
        "source_url",
        "source_scope_kind",
        "source_scope_ref",
        "source_scope_name",
        "source_team_key",
        "title",
        "body",
        "team_slug",
        "workflow_key",
        "status",
        "dry_run",
        "created_by_hash",
        "claimed_by_hash",
        "claimed_by",
        "claimed_at",
        "heartbeat_at",
        "deadline_at",
        "chain_trace_id",
        "current_step",
        "turns_used",
        "failure_code",
        "failure_detail",
        "created_at",
        "updated_at",
    }
    constraints = {
        c["name"]: c["column_names"] for c in inspector.get_unique_constraints("agent_tasks")
    }
    assert constraints["uq_agent_tasks_key"] == ["namespace_key", "task_key"]
    # The target of the step table's composite foreign key.
    assert constraints["uq_agent_tasks_namespace_id"] == ["namespace_key", "id"]

    indexes = {
        index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
        for index in inspector.get_indexes("agent_tasks")
    }
    assert indexes["ux_agent_tasks_open_source_ref"] == (
        ("namespace_key", "source_kind", "source_ref"),
        True,
    )
    assert indexes["ix_agent_tasks_scope"][1] is False


def test_upgrade_creates_the_step_table_with_a_namespace_safe_foreign_key(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_task_steps")} == {
        "id",
        "namespace_key",
        "task_id",
        "step_index",
        "agent_name",
        "brief",
        "session_key",
        "turn_trace_id",
        "status",
        "output_text",
        "output_truncated",
        "attempts",
        "failure_code",
        "failure_detail",
        "started_at",
        "ended_at",
    }

    foreign_keys = inspector.get_foreign_keys("agent_task_steps")
    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk["referred_table"] == "agent_tasks"
    assert fk["constrained_columns"] == ["namespace_key", "task_id"]
    assert fk["referred_columns"] == ["namespace_key", "id"]
    assert fk["options"]["ondelete"] == "CASCADE"

    unique = {
        c["name"]: c["column_names"]
        for c in inspector.get_unique_constraints("agent_task_steps")
    }
    assert unique["ux_agent_task_steps_index"] == ["task_id", "step_index"]


def test_the_session_column_carries_no_foreign_key(
    upgrade_to, temp_engine: Engine
) -> None:
    """A composite ``ON DELETE SET NULL`` would try to null ``namespace_key``."""
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert "agent_task_id" in {
        column["name"] for column in inspector.get_columns("agent_sessions")
    }
    assert inspector.get_foreign_keys("agent_sessions") == []
    assert "idx_agent_sessions_task" in {
        index["name"] for index in inspector.get_indexes("agent_sessions")
    }


def test_upgrade_leaves_pre_existing_tables_untouched(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == set(LEDGER_TABLES)


# ---------------------------------------------------------------------------
# The index that stops one item being worked twice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", HELD_STATUSES)
def test_a_non_terminal_task_holds_its_source_ref(
    upgrade_to, temp_engine: Engine, status: str
) -> None:
    """Every status but the three terminal ones keeps the slot.

    ``paused_quota`` and ``running_unknown`` are the ones worth naming: a task
    that is merely stuck must not be queued a second time underneath itself,
    and the reclaim predicate covers the same set from the other side so a held
    slot is always recoverable by something.
    """
    upgrade_to(MIGRATION_REVISION)
    _insert_task(temp_engine, source_ref="HELD-1", status=status)

    with pytest.raises(IntegrityError):
        _insert_task(temp_engine, source_ref="HELD-1", status="queued")


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_a_terminal_task_releases_its_source_ref(
    upgrade_to, temp_engine: Engine, status: str
) -> None:
    """Reopened issues are real, so a finished task must not block one for ever."""
    upgrade_to(MIGRATION_REVISION)
    _insert_task(temp_engine, source_ref="FREE-1", status=status)

    _insert_task(temp_engine, source_ref="FREE-1", status="queued")

    with temp_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM agent_tasks WHERE source_ref = 'FREE-1'")
        ).scalar_one()
    assert count == 2


def test_the_dedup_index_is_scoped_by_namespace_and_by_source_kind(
    upgrade_to, temp_engine: Engine
) -> None:
    """One tenant cannot block another, and two id spaces are not one.

    A Linear issue id and a file line id colliding is a coincidence rather than
    the same work, which is why the kind is in the key. What is *not* split is
    the milestone path from the label path: both are ``linear``, so one issue
    arriving by both routes is still one open task.
    """
    upgrade_to(MIGRATION_REVISION)
    _insert_task(temp_engine, namespace_key="ns-one", source_kind="linear", source_ref="X")

    _insert_task(temp_engine, namespace_key="ns-two", source_kind="linear", source_ref="X")
    _insert_task(temp_engine, namespace_key="ns-one", source_kind="file", source_ref="X")

    with pytest.raises(IntegrityError):
        _insert_task(
            temp_engine, namespace_key="ns-one", source_kind="linear", source_ref="X"
        )


def test_a_step_cannot_reference_a_task_in_another_namespace(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    task_id = _insert_task(temp_engine, namespace_key="ns-one")

    with pytest.raises(IntegrityError), temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_task_steps (namespace_key, task_id, step_index, "
                "  agent_name, status) VALUES ('ns-two', :task_id, 0, 'a_agent', 'running')"
            ),
            {"task_id": task_id},
        )


def test_deleting_a_task_takes_its_steps_with_it(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    task_id = _insert_task(temp_engine)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_task_steps (namespace_key, task_id, step_index, "
                "  agent_name, status) VALUES ('default', :task_id, 0, 'a_agent', 'running')"
            ),
            {"task_id": task_id},
        )

    with temp_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_tasks WHERE id = :id"), {"id": task_id})
        remaining = conn.execute(text("SELECT count(*) FROM agent_task_steps")).scalar_one()

    assert remaining == 0


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def test_downgrade_leaves_no_residue(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    tables_before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine) == tables_before
    assert _current_revision(temp_engine) == PRE_MIGRATION_REVISION
    assert "agent_task_id" not in {
        column["name"] for column in inspect(temp_engine).get_columns("agent_sessions")
    }

    with temp_engine.begin() as conn:
        remaining = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "  AND (indexname LIKE '%agent_task%')"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_populated_tables(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    task_id = _insert_task(temp_engine)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_task_steps (namespace_key, task_id, step_index, "
                "  agent_name, status, output_text) "
                "VALUES ('default', :task_id, 0, 'a_agent', 'completed', 'a report')"
            ),
            {"task_id": task_id},
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(LEDGER_TABLES)


def test_downgrade_leaves_a_bound_session_row_intact(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    """Dropping the column must not take the session with it.

    The binding is nullable and carries no foreign key precisely so the ledger
    can be rolled back out from under a running deployment without deleting
    anybody's transcript.
    """
    upgrade_to(MIGRATION_REVISION)
    task_id = _insert_task(temp_engine)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_sessions (namespace_key, session_key, agent_name, "
                "  executor_app_name, executor_user_id, executor_session_id, agent_task_id) "
                "VALUES ('default', 'sk-1', 'chat-agent-one', 'app', 'default:u', 's', :task_id)"
            ),
            {"task_id": task_id},
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    with temp_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM agent_sessions WHERE session_key = 'sk-1'")
            ).scalar_one()
            == 1
        )


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert set(LEDGER_TABLES).issubset(_table_names(temp_engine))
    assert _current_revision(temp_engine) == MIGRATION_REVISION
    # And the index still bites after the round trip, which a re-created index
    # with a dropped predicate would not.
    _insert_task(temp_engine, source_ref="REPEAT-1")
    with pytest.raises(IntegrityError):
        _insert_task(temp_engine, source_ref="REPEAT-1")


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


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_the_migration_builds_what_the_orm_describes(
    upgrade_to, temp_engine: Engine, table: str
) -> None:
    """The suite tests one schema and production runs another.

    ``conftest`` builds the test database from ``Base.metadata.create_all``;
    every deployment builds it from these revisions. A column that exists only
    in the ORM would leave this whole suite green and fail on the first request
    after a real upgrade.
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


def test_the_partial_index_survives_create_all_as_well_as_the_migration(
    upgrade_to, temp_engine: Engine
) -> None:
    """The predicate is the part a shape comparison cannot see.

    ``get_indexes`` reports the columns and the uniqueness of a partial index
    and not the ``WHERE`` clause, so an ORM index that forgot the predicate
    would match the migration's on every field the test above compares - and
    would then refuse to queue a reopened issue for ever. This asserts the
    behaviour instead.
    """
    del upgrade_to
    with _temp_database() as orm_db_url:
        orm_engine = create_engine(orm_db_url, future=True)
        try:
            Base.metadata.create_all(bind=orm_engine)
            _insert_task(orm_engine, source_ref="ORM-1", status="completed")
            _insert_task(orm_engine, source_ref="ORM-1", status="queued")
            with pytest.raises(IntegrityError):
                _insert_task(orm_engine, source_ref="ORM-1", status="queued")
        finally:
            orm_engine.dispose()
