"""Alembic coverage for the write-back queue revision.

Small on purpose: the table's behaviour is covered through the API in
``test_agent_task_writebacks.py`` against the ORM-created schema, so what this
file owns is that the *migrated* schema is that schema. Three assertions
matter beyond shape: the unique constraint that makes the enqueue idempotent
actually refuses a duplicate ``(task_id, step_index, kind)``; the composite
foreign key cascades a task delete through its write-backs; and downgrade
leaves nothing behind.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import IntegrityError

import agent_control_server.models  # noqa: F401  registers the ORM tables
from agent_control_server.config import db_config
from alembic import command

SERVER_DIR = Path(__file__).resolve().parents[1]
PRE_MIGRATION_REVISION = "c4a91e7b3d26"
MIGRATION_REVISION = "e9d3b7a54c12"
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Write-back Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    temp_db_name = f"agent_control_wb_{uuid.uuid4().hex[:12]}"
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
        cleanup = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with cleanup.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    " WHERE datname = :db AND pid <> pg_backend_pid()"
                ),
                {"db": temp_db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{temp_db_name}"'))
        cleanup.dispose()


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


def _insert_task(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO agent_tasks (namespace_key, task_key, source_kind, "
                    "  source_ref, title, workflow_key, status, deadline_at) "
                    "VALUES ('default', :key, 'linear', :ref, 'a title', 'default', "
                    "        'completed', now() + interval '1 hour') RETURNING id"
                ),
                {"key": uuid.uuid4().hex, "ref": f"issue-{uuid.uuid4().hex[:8]}"},
            ).scalar_one()
        )


def _insert_writeback(engine: Engine, task_id: int, *, kind: str = "comment") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_task_writebacks (namespace_key, task_id, "
                "  step_index, kind, status, body) "
                "VALUES ('default', :task_id, 0, :kind, 'pending', 'a body')"
            ),
            {"task_id": task_id, "kind": kind},
        )


def test_upgrade_creates_the_queue_and_downgrade_removes_it(
    alembic_config: Config, temp_engine: Engine
) -> None:
    command.upgrade(alembic_config, PRE_MIGRATION_REVISION)
    assert "agent_task_writebacks" not in inspect(temp_engine).get_table_names()

    command.upgrade(alembic_config, MIGRATION_REVISION)
    inspector = inspect(temp_engine)
    assert {c["name"] for c in inspector.get_columns("agent_task_writebacks")} == {
        "id",
        "namespace_key",
        "task_id",
        "step_index",
        "kind",
        "status",
        "body",
        "target_state_id",
        "decision_digest",
        "approved_by_hash",
        "approved_at",
        "rejected_reason",
        "attempts",
        "last_error",
        "created_at",
        "updated_at",
    }
    foreign_keys = inspector.get_foreign_keys("agent_task_writebacks")
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["constrained_columns"] == ["namespace_key", "task_id"], (
        "the composite key is what stops a row referencing another namespace's task"
    )

    command.downgrade(alembic_config, PRE_MIGRATION_REVISION)
    assert "agent_task_writebacks" not in inspect(temp_engine).get_table_names()


def test_the_same_step_and_kind_cannot_queue_twice(
    alembic_config: Config, temp_engine: Engine
) -> None:
    """The idempotent enqueue is a constraint, not a habit."""
    command.upgrade(alembic_config, MIGRATION_REVISION)
    task_id = _insert_task(temp_engine)

    _insert_writeback(temp_engine, task_id, kind="comment")
    _insert_writeback(temp_engine, task_id, kind="status_change")

    with pytest.raises(IntegrityError):
        _insert_writeback(temp_engine, task_id, kind="comment")


def test_deleting_a_task_cascades_through_its_writebacks(
    alembic_config: Config, temp_engine: Engine
) -> None:
    command.upgrade(alembic_config, MIGRATION_REVISION)
    task_id = _insert_task(temp_engine)
    _insert_writeback(temp_engine, task_id)

    with temp_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_tasks WHERE id = :id"), {"id": task_id})
        remaining = conn.execute(
            text("SELECT count(*) FROM agent_task_writebacks WHERE task_id = :id"),
            {"id": task_id},
        ).scalar_one()

    assert remaining == 0
