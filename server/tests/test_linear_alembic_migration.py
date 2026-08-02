"""Alembic coverage for the ``teams.linear_team_key`` migration.

The migration is additive and reversible, so the interesting properties are
that upgrading touches nothing but the one column, that existing teams survive
it unlinked, and that downgrading leaves the table exactly as it was.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from agent_control_server.config import db_config
from alembic import command

SERVER_DIR = Path(__file__).resolve().parents[1]
PRE_MIGRATION_REVISION = "d3a5c81f7b42"
MIGRATION_REVISION = "b6f1c92d4a07"
COLUMN = "linear_team_key"
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Linear team key Alembic migration tests require PostgreSQL.",
)


@pytest.fixture
def temp_db_url() -> str:
    temp_db_name = f"agent_control_linear_{uuid.uuid4().hex[:12]}"
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


def _team_columns(engine: Engine) -> dict[str, dict]:
    return {column["name"]: column for column in inspect(engine).get_columns("teams")}


def _current_revision(engine: Engine) -> str | None:
    with engine.begin() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _insert_team(engine: Engine, *, slug: str, namespace: str = "ns-one") -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO teams (namespace_key, slug, display_name) "
                "VALUES (:ns, :slug, :name) RETURNING id"
            ),
            {"ns": namespace, "slug": slug, "name": slug.title()},
        ).scalar_one()


def test_migration_graph_has_a_single_head(alembic_config: Config) -> None:
    """One head, with this migration somewhere in its history.

    Originally this asserted the head *was* this revision, which only held
    while it happened to be the newest one and turned every later migration
    into a failure here. What matters is that the history stayed linear and
    that this revision is still on it; ``test_alembic_single_head.py`` owns the
    single-head rule for the repository as a whole.
    """
    script = ScriptDirectory.from_config(alembic_config)

    assert len(script.get_heads()) == 1
    assert MIGRATION_REVISION in {
        revision.revision for revision in script.walk_revisions()
    }


def test_pre_migration_teams_table_has_no_linear_column(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert COLUMN not in _team_columns(temp_engine)


def test_upgrade_adds_a_nullable_short_string_column(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)

    column = _team_columns(temp_engine)[COLUMN]
    assert column["nullable"] is True
    assert column["type"].length == 20


def test_upgrade_adds_nothing_but_that_column(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = set(_team_columns(temp_engine))

    upgrade_to(MIGRATION_REVISION)

    assert set(_team_columns(temp_engine)) - before == {COLUMN}


def test_upgrade_leaves_existing_teams_unlinked(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    team_id = _insert_team(temp_engine, slug="sales-outreach")

    upgrade_to(MIGRATION_REVISION)

    with temp_engine.begin() as conn:
        stored = conn.execute(
            text("SELECT linear_team_key FROM teams WHERE id = :id"), {"id": team_id}
        ).scalar()
    assert stored is None


def test_upgrade_adds_no_constraint_on_the_new_column(
    upgrade_to, temp_engine: Engine
) -> None:
    """Two teams pointing at one Linear team is a legitimate arrangement."""
    upgrade_to(MIGRATION_REVISION)

    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO teams (namespace_key, slug, display_name, linear_team_key) "
                "VALUES ('ns-one', 'engineering', 'Engineering', 'ENG'), "
                "('ns-one', 'platform', 'Platform', 'ENG')"
            )
        )
        count = conn.execute(
            text("SELECT count(*) FROM teams WHERE linear_team_key = 'ENG'")
        ).scalar()
    assert count == 2


def test_downgrade_removes_only_that_column(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = set(_team_columns(temp_engine))

    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)

    assert set(_team_columns(temp_engine)) == before
    assert _current_revision(temp_engine) == PRE_MIGRATION_REVISION


def test_downgrade_keeps_the_teams_themselves(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO teams (namespace_key, slug, display_name, linear_team_key) "
                "VALUES ('ns-one', 'engineering', 'Engineering', 'ENG')"
            )
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    with temp_engine.begin() as conn:
        slugs = conn.execute(text("SELECT slug FROM teams")).scalars().all()
    assert slugs == ["engineering"]


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert COLUMN in _team_columns(temp_engine)
    assert _current_revision(temp_engine) == MIGRATION_REVISION


def test_upgrading_from_base_reaches_the_new_head(
    upgrade_to, alembic_config: Config, temp_engine: Engine
) -> None:
    """A full upgrade lands on the head and carries this column with it.

    The head moves every time a migration is added, so it is read from the
    script directory rather than pinned to this revision.
    """
    upgrade_to("head")

    script = ScriptDirectory.from_config(alembic_config)
    assert _current_revision(temp_engine) == script.get_current_head()
    assert COLUMN in _team_columns(temp_engine)
