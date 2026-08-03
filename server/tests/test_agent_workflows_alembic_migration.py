"""Alembic coverage for the workflows revision.

Mirrors ``test_agent_tasks_alembic_migration.py``: what the revision adds, that
it leaves what was already there alone, that a downgrade leaves no residue, and
that the schema a deployment gets from these revisions is the schema the ORM
describes. That last one is the reason this file is not optional - ``conftest``
builds the test database from ``Base.metadata.create_all`` and every deployment
builds it from the revisions, so a column that exists in only one of them leaves
the whole suite green and fails on the first request after a real upgrade.

Two assertions here are more than schema bookkeeping.

**Neither ``agent_workflows.team_slug`` nor ``teams.default_agent_name`` carries
a foreign key.** A cascade from ``teams`` would delete a workflow when a team
went away, taking the record of what running tasks were configured to do with
it. The plan's answer is that a workflow which outlives its team stops resolving
and shows up as ``blocked``, which is legible; a row that vanished would not be.

**The downgrade has to survive populated tables.** Rolling this revision back
under a deployment that has already configured a workflow and set a default
agent is exactly when somebody reaches for it.
"""

from __future__ import annotations

import contextlib
import json
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
PRE_MIGRATION_REVISION = "f1a6c30d8e77"
MIGRATION_REVISION = "a3f9d2c81e64"
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Workflow Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    """Yield the URL of an empty database that is dropped on the way out."""
    temp_db_name = f"agent_control_wf_{uuid.uuid4().hex[:12]}"
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


def _team_columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("teams")}


def _current_revision(engine: Engine) -> str | None:
    with engine.begin() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _insert_workflow(
    engine: Engine,
    *,
    namespace_key: str = "default",
    workflow_key: str = "triage-and-fix",
    team_slug: str | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_workflows (namespace_key, workflow_key, display_name, "
                "  team_slug, steps) "
                "VALUES (:ns, :key, 'A chain', :team, CAST(:steps AS jsonb))"
            ),
            {
                "ns": namespace_key,
                "key": workflow_key,
                "team": team_slug,
                "steps": json.dumps(
                    steps if steps is not None else [{"agent_name": "an_agent_x", "brief": ""}]
                ),
            },
        )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_previous_revision_has_neither_the_table_nor_the_column(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert "agent_workflows" not in _table_names(temp_engine)
    assert "default_agent_name" not in _team_columns(temp_engine)


def test_upgrade_creates_the_workflow_table(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_workflows")} == {
        "id",
        "namespace_key",
        "workflow_key",
        "display_name",
        "team_slug",
        "steps",
        "created_at",
        "updated_at",
    }
    unique = {
        c["name"]: c["column_names"] for c in inspector.get_unique_constraints("agent_workflows")
    }
    assert unique["ux_agent_workflows_key"] == ["namespace_key", "workflow_key"]
    indexes = {
        index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
        for index in inspector.get_indexes("agent_workflows")
    }
    assert indexes["ix_agent_workflows_team"] == (("namespace_key", "team_slug"), False)


def test_upgrade_adds_the_nullable_default_agent_to_teams(
    upgrade_to, temp_engine: Engine
) -> None:
    """Nullable with no backfill: a deployment that configured nothing keeps
    running its one-step default with the agent an operator names."""
    upgrade_to(MIGRATION_REVISION)

    column = next(
        c for c in inspect(temp_engine).get_columns("teams") if c["name"] == "default_agent_name"
    )
    assert column["nullable"] is True


def test_neither_new_reference_carries_a_foreign_key(
    upgrade_to, temp_engine: Engine
) -> None:
    """A cascade from ``teams`` would delete a workflow when a team was
    removed, taking the record of what four running tasks were configured to do
    with it."""
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert inspector.get_foreign_keys("agent_workflows") == []
    assert all(
        "default_agent_name" not in fk["constrained_columns"]
        for fk in inspector.get_foreign_keys("teams")
    )


def test_upgrade_adds_exactly_one_table(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == {"agent_workflows"}


def test_a_workflow_key_is_unique_within_a_namespace_and_not_across_them(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    _insert_workflow(temp_engine, namespace_key="ns-one")

    _insert_workflow(temp_engine, namespace_key="ns-two")

    with pytest.raises(IntegrityError):
        _insert_workflow(temp_engine, namespace_key="ns-one")


def test_a_workflow_may_name_a_team_that_does_not_exist_in_the_table(
    upgrade_to, temp_engine: Engine
) -> None:
    """No foreign key, restated as behaviour: the row survives a team that has
    gone, and the service's refusal is what stops one being created."""
    upgrade_to(MIGRATION_REVISION)

    _insert_workflow(temp_engine, team_slug="a-team-that-is-not-there")

    with temp_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM agent_workflows")).scalar_one() == 1
        )


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
    assert "default_agent_name" not in _team_columns(temp_engine)
    assert _current_revision(temp_engine) == PRE_MIGRATION_REVISION
    with temp_engine.begin() as conn:
        remaining = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "  AND indexname LIKE '%agent_workflow%'"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_a_populated_table_and_a_populated_column(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    """Rolling back under a deployment that has already configured something is
    the only time anybody runs this."""
    upgrade_to(MIGRATION_REVISION)
    _insert_workflow(temp_engine)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO teams (namespace_key, slug, display_name, default_agent_name) "
                "VALUES ('default', 'marketing', 'Marketing', 'an_agent_x')"
            )
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    assert "agent_workflows" not in _table_names(temp_engine)
    with temp_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM teams")).scalar_one() == 1


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert "agent_workflows" in _table_names(temp_engine)
    assert _current_revision(temp_engine) == MIGRATION_REVISION
    # And the unique key still bites after the round trip.
    _insert_workflow(temp_engine)
    with pytest.raises(IntegrityError):
        _insert_workflow(temp_engine)


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


@pytest.mark.parametrize("table", ["agent_workflows", "teams"])
def test_the_migration_builds_what_the_orm_describes(
    upgrade_to, temp_engine: Engine, table: str
) -> None:
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
