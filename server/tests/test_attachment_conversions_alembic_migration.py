"""Alembic coverage for the conversion cache revision.

Small, because the revision is one additive table with no foreign keys. Two of
these assertions are not bookkeeping.

**The ORM and the revisions have to describe the same table.** ``conftest``
builds the test database from ``Base.metadata.create_all`` and every deployment
builds it from the revisions, so a column present in only one of them leaves the
whole suite green and fails on the first turn after a real upgrade.

**A downgrade has to leave nothing behind.** This table has no cascade holding
it and no other revision references it, so a leftover index or a leftover table
would only be discovered by the next person whose upgrade collided with it.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

import agent_control_server.models  # noqa: F401  registers the ORM tables
from agent_control_server.config import db_config
from agent_control_server.db import Base
from alembic import command

SERVER_DIR = Path(__file__).resolve().parents[1]
_BASE_DB_URL = make_url(db_config.get_url())

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Alembic migration tests require PostgreSQL.",
)


# The harness is duplicated from ``test_agent_attachments_alembic_migration``
# rather than imported. pytest resolves a fixture by name, so importing one
# into a second module rebinds the same name twice and the linter is right to
# say so; hoisting it into ``conftest`` would arm a database-creating fixture
# for every test in the suite. Forty lines is the cheaper of the three.
@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    """Yield the URL of an empty database that is dropped on the way out."""
    name = f"agent_control_conv_{uuid.uuid4().hex[:12]}"
    admin_url = _BASE_DB_URL.set(database="postgres").render_as_string(hide_password=False)
    target_url = _BASE_DB_URL.set(database=name).render_as_string(hide_password=False)

    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
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
                {"db": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
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


@pytest.fixture
def upgrade_to(alembic_config: Config) -> Callable[[str], None]:
    def _upgrade(revision: str) -> None:
        command.upgrade(alembic_config, revision)

    return _upgrade


@pytest.fixture
def downgrade_to(alembic_config: Config) -> Callable[[str], None]:
    def _downgrade(revision: str) -> None:
        command.downgrade(alembic_config, revision)

    return _downgrade


PRE_MIGRATION_REVISION = "b2e7c94a1d55"
MIGRATION_REVISION = "c4a91e7b3d26"
TABLE = "agent_attachment_conversions"


def _columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns(TABLE)}


def test_the_revision_adds_the_cache_table(
    temp_engine: Engine, upgrade_to: Callable[[str], None]
) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    assert TABLE not in set(inspect(temp_engine).get_table_names(schema="public"))

    upgrade_to(MIGRATION_REVISION)
    assert TABLE in set(inspect(temp_engine).get_table_names(schema="public"))
    assert _columns(temp_engine) == {column.name for column in Base.metadata.tables[TABLE].columns}


def test_one_content_key_can_only_be_claimed_once_per_namespace(
    temp_engine: Engine, upgrade_to: Callable[[str], None]
) -> None:
    """The claim that makes two workers converge on one conversion.

    ``ON CONFLICT DO NOTHING`` is what stops both of them paying for the same
    twenty seconds of OCR, and it only stops them because this constraint
    exists. Asserted here rather than trusted, because a missing unique
    constraint turns the whole scheduler into a race that merely looks correct.
    """
    upgrade_to(MIGRATION_REVISION)
    insert = text(
        f"INSERT INTO {TABLE} (namespace_key, cache_key, source_sha256, state) "
        "VALUES (:ns, 'acv1:abc', :sha, 'running') "
        "ON CONFLICT (namespace_key, cache_key) DO NOTHING RETURNING id"
    )
    with temp_engine.begin() as conn:
        first = conn.execute(insert, {"ns": "default", "sha": "a" * 64}).scalar()
        second = conn.execute(insert, {"ns": "default", "sha": "a" * 64}).scalar()
        other = conn.execute(insert, {"ns": "other", "sha": "a" * 64}).scalar()
    assert first is not None
    assert second is None
    assert other is not None


def test_the_downgrade_leaves_no_residue(
    temp_engine: Engine,
    upgrade_to: Callable[[str], None],
    downgrade_to: Callable[[str], None],
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)

    inspector = inspect(temp_engine)
    assert TABLE not in set(inspector.get_table_names(schema="public"))
    with temp_engine.begin() as conn:
        leftover = conn.execute(
            text(
                "SELECT count(*) FROM pg_indexes "
                " WHERE indexname LIKE 'idx_agent_attachment_conversions%'"
            )
        ).scalar()
    assert leftover == 0


def test_upgrade_downgrade_upgrade_is_clean(
    temp_engine: Engine,
    upgrade_to: Callable[[str], None],
    downgrade_to: Callable[[str], None],
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)
    assert TABLE in set(inspect(temp_engine).get_table_names(schema="public"))


@pytest.mark.parametrize(
    ("column", "default"),
    [("state", "queued"), ("text_chars", "0"), ("stored_truncated", "false")],
)
def test_the_defaults_a_deployment_gets_are_the_ones_the_orm_assumes(
    temp_engine: Engine,
    upgrade_to: Callable[[str], None],
    column: str,
    default: str,
) -> None:
    upgrade_to(MIGRATION_REVISION)
    columns = {c["name"]: c for c in inspect(temp_engine).get_columns(TABLE)}
    assert default in str(columns[column]["default"])
