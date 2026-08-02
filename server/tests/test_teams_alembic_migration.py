"""Alembic coverage for the agent teams migration: the tables, constraints and
index it adds, and that downgrading leaves no residue behind."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from agent_control_server.config import db_config
from alembic import command

SERVER_DIR = Path(__file__).resolve().parents[1]
PRE_MIGRATION_REVISION = "e2b7f4a9c6d1"
MIGRATION_REVISION = "d3a5c81f7b42"
TEAM_TABLES = ("teams", "team_members")
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Teams Alembic migration tests require PostgreSQL.",
)


@pytest.fixture
def temp_db_url() -> str:
    temp_db_name = f"agent_control_teams_{uuid.uuid4().hex[:12]}"
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


def test_pre_migration_revision_has_no_team_tables(
    upgrade_to,
    temp_engine: Engine,
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(TEAM_TABLES)


def test_upgrade_creates_team_tables_with_expected_shape(
    upgrade_to,
    temp_engine: Engine,
) -> None:
    upgrade_to(MIGRATION_REVISION)

    inspector = inspect(temp_engine)
    assert set(TEAM_TABLES).issubset(_table_names(temp_engine))

    team_constraints = {c["name"] for c in inspector.get_unique_constraints("teams")}
    assert {"uq_teams_namespace_slug", "uq_teams_namespace_id"} <= team_constraints

    assert {c["name"] for c in inspector.get_columns("teams")} == {
        "id",
        "namespace_key",
        "slug",
        "display_name",
        "description",
        "created_at",
        "updated_at",
    }

    member_pk = inspector.get_pk_constraint("team_members")
    assert member_pk["constrained_columns"] == ["namespace_key", "team_id", "agent_name"]

    foreign_keys = inspector.get_foreign_keys("team_members")
    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk["referred_table"] == "teams"
    assert fk["constrained_columns"] == ["namespace_key", "team_id"]
    assert fk["referred_columns"] == ["namespace_key", "id"]
    assert fk["options"]["ondelete"] == "CASCADE"

    member_indexes = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("team_members")
    }
    assert member_indexes["idx_team_members_agent"] == ["namespace_key", "agent_name"]


def test_upgrade_leaves_pre_existing_tables_untouched(
    upgrade_to,
    temp_engine: Engine,
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)
    after = _table_names(temp_engine)

    assert after - before == set(TEAM_TABLES)


def test_downgrade_leaves_no_residue(
    upgrade_to,
    downgrade_to,
    temp_engine: Engine,
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    tables_before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine) == tables_before
    assert _current_revision(temp_engine) == PRE_MIGRATION_REVISION

    with temp_engine.begin() as conn:
        remaining_indexes = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexname LIKE '%team%'"
            )
        ).fetchall()
    assert remaining_indexes == []


def test_downgrade_drops_populated_tables(
    upgrade_to,
    downgrade_to,
    temp_engine: Engine,
) -> None:
    upgrade_to(MIGRATION_REVISION)
    with temp_engine.begin() as conn:
        team_id = conn.execute(
            text(
                "INSERT INTO teams (namespace_key, slug, display_name) "
                "VALUES ('ns-one', 'sales-outreach', 'Sales & Outreach') RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO team_members (namespace_key, team_id, agent_name) "
                "VALUES ('ns-one', :team_id, 'outreach-bot-one')"
            ),
            {"team_id": team_id},
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(TEAM_TABLES)


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to,
    downgrade_to,
    temp_engine: Engine,
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert set(TEAM_TABLES).issubset(_table_names(temp_engine))
    assert _current_revision(temp_engine) == MIGRATION_REVISION
