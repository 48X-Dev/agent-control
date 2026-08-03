"""Alembic coverage for the attachments revision.

Mirrors ``test_agent_workflows_alembic_migration.py``: what the revision adds,
that it leaves what was there alone, that a downgrade leaves no residue, and
that the schema a deployment gets from the revisions is the schema the ORM
describes. That last one is why this file is not optional - ``conftest`` builds
the test database from ``Base.metadata.create_all`` and every deployment builds
it from the revisions, so a column that exists in only one of them leaves the
whole suite green and fails on the first request after a real upgrade.

Three assertions here are more than schema bookkeeping.

**The cascade is two hops deep and namespace-leading.** Deleting a session has
to take its attachments *and* their bytes, and a single-column foreign key would
look identical in every inspection until the day two namespaces shared a session
id. Asserted by deleting a session and counting blobs.

**Content uniqueness is per session.** Per namespace would be a content oracle:
a caller in a shared namespace could learn that somebody else had already
uploaded a given file by observing a dedupe hit.

**The size CHECK is the hard constant, not the configured one.** It is the bound
a direct database write cannot smuggle past, which is the whole reason it exists
alongside the streamed count in the handler.
"""

from __future__ import annotations

import contextlib
import hashlib
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
PRE_MIGRATION_REVISION = "a3f9d2c81e64"
MIGRATION_REVISION = "b2e7c94a1d55"
_BASE_DB_URL = make_url(db_config.get_url())

NEW_TABLES = {
    "agent_session_attachments",
    "agent_session_attachment_blobs",
    "agent_turn_attachments",
}

pytestmark = pytest.mark.skipif(
    _BASE_DB_URL.get_backend_name() != "postgresql",
    reason="Attachment Alembic migration tests require PostgreSQL.",
)


@contextlib.contextmanager
def _temp_database() -> Iterator[str]:
    """Yield the URL of an empty database that is dropped on the way out."""
    temp_db_name = f"agent_control_att_{uuid.uuid4().hex[:12]}"
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


def _step_columns(engine: Engine) -> set[str]:
    return {column["name"] for column in inspect(engine).get_columns("agent_task_steps")}


def _current_revision(engine: Engine) -> str | None:
    with engine.begin() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def _insert_session(engine: Engine, *, namespace_key: str = "default") -> tuple[int, str]:
    session_key = uuid.uuid4().hex
    with engine.begin() as conn:
        session_id = conn.execute(
            text(
                "INSERT INTO agent_sessions "
                "(namespace_key, session_key, agent_name, executor_kind, "
                " executor_app_name, executor_user_id, executor_session_id, status) "
                "VALUES (:ns, :key, 'an_agent_x', 'google_adk', 'app', :user, :sid, 'active') "
                "RETURNING id"
            ),
            {
                "ns": namespace_key,
                "key": session_key,
                "user": f"{namespace_key}:{uuid.uuid4().hex}",
                "sid": uuid.uuid4().hex,
            },
        ).scalar_one()
    return session_id, session_key


def _insert_attachment(
    engine: Engine,
    *,
    session_id: int,
    namespace_key: str = "default",
    source_sha256: str | None = None,
    size_bytes: int = 64,
) -> int:
    digest = source_sha256 or hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO agent_session_attachments "
                "(namespace_key, session_id, attachment_key, display_name, "
                " original_name_sha256, declared_mime, sniffed_mime, size_bytes, "
                " source_sha256) "
                "VALUES (:ns, :sid, :key, 'spec.pdf', :namehash, 'application/pdf', "
                "        'application/pdf', :size, :sha) "
                "RETURNING id"
            ),
            {
                "ns": namespace_key,
                "sid": session_id,
                "key": uuid.uuid4().hex,
                "namehash": digest,
                "size": size_bytes,
                "sha": digest,
            },
        ).scalar_one()


def _insert_blob(
    engine: Engine, *, attachment_id: int, variant: str = "original", size_bytes: int = 64
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_session_attachment_blobs "
                "(namespace_key, attachment_id, variant, content_type, size_bytes, sha256, data) "
                "VALUES ('default', :aid, :variant, 'application/pdf', :size, :sha, :data)"
            ),
            {
                "aid": attachment_id,
                "variant": variant,
                "size": size_bytes,
                "sha": hashlib.sha256(b"x").hexdigest(),
                "data": b"\x00" * size_bytes,
            },
        )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_the_previous_revision_has_none_of_it(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)

    assert NEW_TABLES & _table_names(temp_engine) == set()
    assert "attachments_summary" not in _step_columns(temp_engine)


def test_upgrade_adds_exactly_three_tables(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(PRE_MIGRATION_REVISION)
    before = _table_names(temp_engine)

    upgrade_to(MIGRATION_REVISION)

    assert _table_names(temp_engine) - before == NEW_TABLES


def test_the_attachment_table_has_the_columns_the_plan_names(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)

    columns = {c["name"] for c in inspect(temp_engine).get_columns("agent_session_attachments")}

    assert {"origin", "origin_ref", "source_sha256", "delivered_sha256"} <= columns
    # The extraction columns are Phase 6 and are deliberately absent. The
    # descriptor already carries those fields reading null, so adding them later
    # changes a migration and nothing else.
    assert "extracted_text" not in columns
    assert "text_chars" not in columns


def test_origin_defaults_to_operator_upload(upgrade_to, temp_engine: Engine) -> None:
    """The column that makes "what did the tracker put in this conversation"
    one query, and it must not need a backfill to be readable."""
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)
    attachment_id = _insert_attachment(temp_engine, session_id=session_id)

    with temp_engine.connect() as conn:
        origin, status = conn.execute(
            text("SELECT origin, status FROM agent_session_attachments WHERE id = :id"),
            {"id": attachment_id},
        ).one()

    assert origin == "operator_upload"
    assert status == "pending"


def test_the_step_summary_column_is_nullable_jsonb(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)

    column = next(
        c
        for c in inspect(temp_engine).get_columns("agent_task_steps")
        if c["name"] == "attachments_summary"
    )

    assert column["nullable"] is True
    assert "JSONB" in str(column["type"]).upper()


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_the_same_bytes_twice_on_one_session_are_refused(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)
    digest = hashlib.sha256(b"the same file").hexdigest()
    _insert_attachment(temp_engine, session_id=session_id, source_sha256=digest)

    with pytest.raises(IntegrityError):
        _insert_attachment(temp_engine, session_id=session_id, source_sha256=digest)


def test_the_same_bytes_on_two_sessions_are_allowed(
    upgrade_to, temp_engine: Engine
) -> None:
    """Per session rather than per namespace, because per namespace would let a
    dedupe hit answer "has anybody else uploaded this file"."""
    upgrade_to(MIGRATION_REVISION)
    first, _ = _insert_session(temp_engine)
    second, _ = _insert_session(temp_engine)
    digest = hashlib.sha256(b"the same file").hexdigest()

    _insert_attachment(temp_engine, session_id=first, source_sha256=digest)
    _insert_attachment(temp_engine, session_id=second, source_sha256=digest)

    with temp_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM agent_session_attachments")).scalar_one()
            == 2
        )


def test_the_size_check_is_the_hard_constant(upgrade_to, temp_engine: Engine) -> None:
    """52,428,800, not the configured ceiling. A direct write must not be able
    to smuggle past a bound the reader assumes."""
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)

    with pytest.raises(IntegrityError):
        _insert_attachment(temp_engine, session_id=session_id, size_bytes=52_428_801)


def test_a_zero_byte_attachment_is_refused_by_the_check(
    upgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)

    with pytest.raises(IntegrityError):
        _insert_attachment(temp_engine, session_id=session_id, size_bytes=0)


def test_one_variant_per_attachment(upgrade_to, temp_engine: Engine) -> None:
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)
    attachment_id = _insert_attachment(temp_engine, session_id=session_id)
    _insert_blob(temp_engine, attachment_id=attachment_id)

    with pytest.raises(IntegrityError):
        _insert_blob(temp_engine, attachment_id=attachment_id)


def test_binding_one_file_to_one_turn_twice_is_refused(
    upgrade_to, temp_engine: Engine
) -> None:
    """Idempotent by primary key, which is the same reasoning one-halt-per-turn
    uses: the constraint is the mechanism, not a check somebody remembers."""
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)
    attachment_id = _insert_attachment(temp_engine, session_id=session_id)
    trace_id = uuid.uuid4().hex

    def bind() -> None:
        with temp_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_turn_attachments "
                    "(namespace_key, session_id, trace_id, attachment_id, position) "
                    "VALUES ('default', :sid, :trace, :aid, 0)"
                ),
                {"sid": session_id, "trace": trace_id, "aid": attachment_id},
            )

    bind()
    with pytest.raises(IntegrityError):
        bind()


def test_deleting_a_session_takes_the_attachment_the_blob_and_the_binding(
    upgrade_to, temp_engine: Engine
) -> None:
    """Two hops of cascade, and every foreign key on the way is composite and
    namespace-leading."""
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)
    attachment_id = _insert_attachment(temp_engine, session_id=session_id)
    _insert_blob(temp_engine, attachment_id=attachment_id)
    with temp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_turn_attachments "
                "(namespace_key, session_id, trace_id, attachment_id, position) "
                "VALUES ('default', :sid, :trace, :aid, 0)"
            ),
            {"sid": session_id, "trace": uuid.uuid4().hex, "aid": attachment_id},
        )

    with temp_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_sessions WHERE id = :id"), {"id": session_id})

    with temp_engine.connect() as conn:
        for table in NEW_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0


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
    assert "attachments_summary" not in _step_columns(temp_engine)
    assert _current_revision(temp_engine) == PRE_MIGRATION_REVISION
    with temp_engine.begin() as conn:
        remaining = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "  AND indexname LIKE '%attachment%'"
            )
        ).fetchall()
    assert remaining == []


def test_downgrade_drops_populated_tables(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    """Rolling back under a deployment that has already stored a file is the
    only time anybody runs this."""
    upgrade_to(MIGRATION_REVISION)
    session_id, _ = _insert_session(temp_engine)
    attachment_id = _insert_attachment(temp_engine, session_id=session_id)
    _insert_blob(temp_engine, attachment_id=attachment_id)

    downgrade_to(PRE_MIGRATION_REVISION)

    assert NEW_TABLES & _table_names(temp_engine) == set()
    with temp_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agent_sessions")).scalar_one() == 1


def test_upgrade_downgrade_upgrade_is_repeatable(
    upgrade_to, downgrade_to, temp_engine: Engine
) -> None:
    upgrade_to(MIGRATION_REVISION)
    downgrade_to(PRE_MIGRATION_REVISION)
    upgrade_to(MIGRATION_REVISION)

    assert NEW_TABLES <= _table_names(temp_engine)
    assert _current_revision(temp_engine) == MIGRATION_REVISION
    # And the content constraint still bites after the round trip.
    session_id, _ = _insert_session(temp_engine)
    digest = hashlib.sha256(b"again").hexdigest()
    _insert_attachment(temp_engine, session_id=session_id, source_sha256=digest)
    with pytest.raises(IntegrityError):
        _insert_attachment(temp_engine, session_id=session_id, source_sha256=digest)


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


@pytest.mark.parametrize(
    "table",
    [
        "agent_session_attachments",
        "agent_session_attachment_blobs",
        "agent_turn_attachments",
        "agent_task_steps",
    ],
)
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
