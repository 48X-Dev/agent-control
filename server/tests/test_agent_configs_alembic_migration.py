"""Alembic coverage for the agent configuration tables.

Mirrors ``test_agent_sessions_alembic_migration.py``: what the revision adds,
that it touches nothing that was already there, and that downgrading leaves no
residue.

Two assertions here are doing more than schema bookkeeping.

``ck_agent_configs_model_id_shape`` must exist and must actually reject a
slashed id. A slash prefix re-selects the underlying provider and a configured
``api_base`` is ignored for routing, so a slashed id is a per-agent endpoint by
another name. This constraint is the layer that catches a write path nobody has
reviewed yet, which is the only layer that still works when the code is wrong.

**Neither table may grow a ``base_url``, ``api_base``, ``endpoint`` or
``api_key`` column.** That is not an oversight to be corrected later; a
per-agent endpoint is data exfiltration wearing a config field, and ADMIN does
not defend it because the shipped default authorizes ADMIN for everyone. If
somebody adds one, this file is what fails.
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
PRE_MIGRATION_REVISION = "a1c4e7b93d80"
MIGRATION_REVISION = "e2b7d4a15c93"
CONFIG_TABLES = ("agent_configs", "agent_config_versions")
_BASE_DB_URL = make_url(db_config.get_url())

#: Not a column list to be extended. See the module docstring.
_FORBIDDEN_COLUMNS = {"base_url", "api_base", "endpoint", "api_key", "api_key_id"}

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Agent config Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    """Yield the URL of an empty database that is dropped on the way out."""
    temp_db_name = f"agent_control_configs_{uuid.uuid4().hex[:12]}"
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


def _check_constraint_names(engine: Engine, table: str) -> set[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT con.conname FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "WHERE rel.relname = :table AND con.contype = 'c'"
            ),
            {"table": table},
        ).fetchall()
    return {row[0] for row in rows}


def _seed_agent(engine: Engine, name: str, namespace: str = "default") -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agents (namespace_key, name, data) "
                "VALUES (:ns, :name, '{}'::json)"
            ),
            {"ns": namespace, "name": name},
        )


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def test_the_previous_revision_has_no_config_tables(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    assert _table_names(temp_engine).isdisjoint(CONFIG_TABLES)


def test_upgrade_creates_the_configuration_table(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_configs")} == {
        "namespace_key",
        "agent_name",
        "body",
        "body_format",
        "prompt_enabled",
        "model_id",
        "current_version",
        "etag",
        "source_instruction",
        "source_reported_at",
        "created_by_hash",
        "updated_by_hash",
        "created_at",
        "updated_at",
    }

    pk = inspector.get_pk_constraint("agent_configs")
    assert pk["constrained_columns"] == ["namespace_key", "agent_name"]

    foreign_keys = inspector.get_foreign_keys("agent_configs")
    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "agents"
    assert foreign_keys[0]["constrained_columns"] == ["namespace_key", "agent_name"]
    assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"


def test_upgrade_creates_the_version_table(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)

    assert {c["name"] for c in inspector.get_columns("agent_config_versions")} == {
        "id",
        "namespace_key",
        "agent_name",
        "version_num",
        "event_type",
        "origin",
        "body",
        "body_format",
        "model_id",
        "etag",
        "note",
        "scan_findings",
        "changed_by_hash",
        "created_at",
    }

    unique = {
        c["name"]: c["column_names"]
        for c in inspector.get_unique_constraints("agent_config_versions")
    }
    assert unique["uq_agent_config_versions_agent_version"] == [
        "namespace_key",
        "agent_name",
        "version_num",
    ]

    indexes = {i["name"] for i in inspector.get_indexes("agent_config_versions")}
    assert "idx_agent_config_versions_agent_recent" in indexes


def test_the_version_table_points_at_agents_rather_than_at_the_config_row(
    upgrade_to, temp_engine: Engine
) -> None:
    """Clearing must not be able to destroy the history that makes it reversible.

    A foreign key to ``agent_configs`` would tie the audit log to the state it
    describes, so removing a prompt would remove the record of ever having had
    one.
    """
    upgrade_to(MIGRATION_REVISION)
    foreign_keys = inspect(temp_engine).get_foreign_keys("agent_config_versions")

    assert len(foreign_keys) == 1
    assert foreign_keys[0]["referred_table"] == "agents"
    assert foreign_keys[0]["options"]["ondelete"] == "CASCADE"


def test_the_version_table_carries_its_own_namespace_key(
    upgrade_to, temp_engine: Engine
) -> None:
    """The deliberate divergence from ``control_versions``.

    That table has no namespace column and relies on the call site loading the
    parent first, which makes every future query against it namespace-blind by
    default. Here the isolation filter is local to the query.
    """
    upgrade_to(MIGRATION_REVISION)
    columns = {c["name"] for c in inspect(temp_engine).get_columns("agent_config_versions")}
    assert "namespace_key" in columns


@pytest.mark.parametrize("table", CONFIG_TABLES)
def test_neither_table_has_anywhere_to_put_an_endpoint(
    upgrade_to, temp_engine: Engine, table: str
) -> None:
    """The absence is the feature. See the module docstring."""
    upgrade_to(MIGRATION_REVISION)
    columns = {c["name"] for c in inspect(temp_engine).get_columns(table)}
    assert columns.isdisjoint(_FORBIDDEN_COLUMNS)


def test_the_named_check_constraints_all_exist(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)

    assert {
        "ck_agent_configs_body_max_length",
        "ck_agent_configs_body_format",
        "ck_agent_configs_model_id_shape",
    } <= _check_constraint_names(temp_engine, "agent_configs")

    assert {
        "ck_agent_config_versions_event_type",
        "ck_agent_config_versions_origin",
    } <= _check_constraint_names(temp_engine, "agent_config_versions")


# ---------------------------------------------------------------------------
# The constraints doing their job
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    ["bedrock/anthropic.claude-v2", "https://evil.example.com/v1", "openai/gpt-5.4"],
)
def test_the_shape_constraint_rejects_a_destination_selector(
    upgrade_to, temp_engine: Engine, model_id: str
) -> None:
    upgrade_to(MIGRATION_REVISION)
    _seed_agent(temp_engine, "config-agent-one")

    with pytest.raises(IntegrityError, match="ck_agent_configs_model_id_shape"):
        with temp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_configs (namespace_key, agent_name, model_id) "
                    "VALUES ('default', 'config-agent-one', :model_id)"
                ),
                {"model_id": model_id},
            )


def test_the_shape_constraint_admits_an_ordinary_id_and_a_null(
    upgrade_to, temp_engine: Engine
) -> None:
    """It has to admit everything the allowlist can hold, and "unmanaged"."""
    upgrade_to(MIGRATION_REVISION)
    _seed_agent(temp_engine, "config-agent-one")
    _seed_agent(temp_engine, "config-agent-two")

    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_configs (namespace_key, agent_name, model_id) "
                "VALUES ('default', 'config-agent-one', 'gpt-5.4-mini')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_configs (namespace_key, agent_name) "
                "VALUES ('default', 'config-agent-two')"
            )
        )


def test_the_body_cap_is_enforced_in_the_database_too(
    upgrade_to, temp_engine: Engine
) -> None:
    """A direct write must not smuggle past a bound the resolver assumes."""
    upgrade_to(MIGRATION_REVISION)
    _seed_agent(temp_engine, "config-agent-one")

    with pytest.raises(IntegrityError, match="ck_agent_configs_body_max_length"):
        with temp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_configs (namespace_key, agent_name, body) "
                    "VALUES ('default', 'config-agent-one', :body)"
                ),
                {"body": "x" * 32_001},
            )


def test_an_unknown_event_type_is_rejected(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    _seed_agent(temp_engine, "config-agent-one")

    with pytest.raises(IntegrityError, match="ck_agent_config_versions_event_type"):
        with temp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_config_versions "
                    "(namespace_key, agent_name, version_num, event_type) "
                    "VALUES ('default', 'config-agent-one', 1, 'cleared')"
                )
            )


def test_deleting_the_agent_cascades_to_both_tables(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    _seed_agent(temp_engine, "config-agent-one")

    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_configs (namespace_key, agent_name, body) "
                "VALUES ('default', 'config-agent-one', 'body')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_config_versions "
                "(namespace_key, agent_name, version_num, event_type, body) "
                "VALUES ('default', 'config-agent-one', 1, 'created', 'body')"
            )
        )
        conn.execute(
            text("DELETE FROM agents WHERE name = 'config-agent-one'")
        )
        assert (
            conn.execute(text("SELECT count(*) FROM agent_configs")).scalar_one() == 0
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM agent_config_versions")
            ).scalar_one()
            == 0
        )


def test_a_configuration_for_an_unregistered_agent_cannot_be_inserted(
    upgrade_to, temp_engine: Engine
) -> None:
    """The agent row is the tenancy anchor, enforced rather than assumed."""
    upgrade_to(MIGRATION_REVISION)

    with pytest.raises(IntegrityError):
        with temp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_configs (namespace_key, agent_name) "
                    "VALUES ('default', 'never-registered-agent')"
                )
            )


# ---------------------------------------------------------------------------
# Downgrade
# ---------------------------------------------------------------------------


def test_upgrade_leaves_pre_existing_tables_untouched(
    upgrade_to, temp_engine: Engine
) -> None:
    """Additive only. No backfill and no data migration of anything existing."""
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == set(CONFIG_TABLES)


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
                "AND indexname LIKE '%agent_config%'"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_populated_tables(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    _seed_agent(temp_engine, "config-agent-one")
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_configs (namespace_key, agent_name, body, model_id) "
                "VALUES ('default', 'config-agent-one', 'body', 'gpt-5.4-mini')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO agent_config_versions "
                "(namespace_key, agent_name, version_num, event_type) "
                "VALUES ('default', 'config-agent-one', 1, 'created')"
            )
        )

    downgrade_to(PRE_MIGRATION_REVISION)

    assert _table_names(temp_engine).isdisjoint(CONFIG_TABLES)


def test_upgrade_downgrade_upgrade_lands_on_the_same_schema(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    """A downgrade that half-cleans up makes the next upgrade fail on a Friday."""
    upgrade_to(MIGRATION_REVISION)
    inspector = inspect(temp_engine)
    first = {
        table: sorted(c["name"] for c in inspector.get_columns(table))
        for table in CONFIG_TABLES
    }
    first_constraints = {
        table: _check_constraint_names(temp_engine, table) for table in CONFIG_TABLES
    }

    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    inspector = inspect(temp_engine)
    assert {
        table: sorted(c["name"] for c in inspector.get_columns(table))
        for table in CONFIG_TABLES
    } == first
    assert {
        table: _check_constraint_names(temp_engine, table) for table in CONFIG_TABLES
    } == first_constraints
    assert _current_revision(temp_engine) == MIGRATION_REVISION
