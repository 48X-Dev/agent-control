"""Corpus database isolation, proved by attempting the connections.

Extends ``test_adk_db_isolation``'s method to the two knowledge roles: every
privilege assertion goes through a real Postgres connection, never through a
grep for a GRANT statement, because Postgres grants are additive in ways that
make the SQL text a bad proxy for the outcome.

Two directions, and the second one is the one a suite usually forgets. The
negative: neither knowledge role may reach a control-plane database, so a sync
process prompt-injected by document content cannot touch `controls` or
`control_bindings`. The positive: the reader must actually be able to SELECT,
because a missing grant reads as an empty corpus and a suite that only proved
the negative would pass in exactly that broken state.

Everything runs against throwaway roles and throwaway databases created per
test, so the live stack is never the subject.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Final

import psycopg
import pytest
import sqlalchemy as sa
from agent_control_server.config import db_config
from sqlalchemy.pool import NullPool

from tests.knowledge_provisioning import (
    connect,
    migrate,
    run_init_script,
    scalar,
    unavailable_reason,
    url_for,
)

_SKIP_REASON = unavailable_reason()
if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)

# One instance-wide critical section, for ``test_adk_db_isolation``'s reason:
# ``knowledge_db_init.sql`` hands an explicit CONNECT to every non-superuser
# login role on the instance before it revokes CONNECT from PUBLIC, so two
# copies of this module grant each other privileges on databases the other is
# about to drop, and the loser's DROP ROLE fails at teardown.
MODULE_LOCK_KEY: Final = 0x4B4E4F57  # "KNOW"
MODULE_LOCK_TIMEOUT: Final = 300

GUARDRAIL_TABLES: Final = ("controls", "policies", "control_bindings")


@dataclass(frozen=True)
class Probe:
    """One throwaway universe: two knowledge roles, a corpus, a control plane."""

    sync_role: str
    read_role: str
    password: str
    corpus_db: str
    control_db: str
    bystander: str


@pytest.fixture(scope="module")
def superuser_conn() -> Iterator[psycopg.Connection[Any]]:
    conn = connect("postgres", db_config.user, db_config.password)
    scalar(conn, "SELECT set_config('lock_timeout', %s, false)", f"{MODULE_LOCK_TIMEOUT}s")
    try:
        scalar(conn, "SELECT pg_advisory_lock(%s)", MODULE_LOCK_KEY)
    except psycopg.errors.LockNotAvailable:
        pytest.fail(
            "another run of this module has held the instance-wide lock on "
            f"{db_config.host}:{db_config.port} for more than {MODULE_LOCK_TIMEOUT}s. "
            "Only one copy may own login roles at a time, because "
            "knowledge_db_init.sql grants CONNECT to every login role it finds."
        )
    try:
        with conn:
            yield conn
    finally:
        pass


@pytest.fixture
def probe(superuser_conn: psycopg.Connection[Any]) -> Iterator[Probe]:
    suffix = uuid.uuid4().hex[:10]
    universe = Probe(
        sync_role=f"kn_probe_sync_{suffix}",
        read_role=f"kn_probe_read_{suffix}",
        password=f"probe_pw_{suffix}",
        corpus_db=f"kn_probe_corpus_{suffix}",
        control_db=f"kn_probe_control_{suffix}",
        bystander=f"kn_probe_bystander_{suffix}",
    )
    with superuser_conn.cursor() as cur:
        # A control database in its default state: PUBLIC holds CONNECT,
        # exactly as agent_control did before any of this existed.
        cur.execute(f'CREATE DATABASE "{universe.control_db}"')
        cur.execute(
            f"CREATE ROLE \"{universe.bystander}\" LOGIN PASSWORD '{universe.password}'"
        )

    with connect(universe.control_db, db_config.user, db_config.password) as conn:
        with conn.cursor() as cur:
            for table in GUARDRAIL_TABLES:
                cur.execute(f'CREATE TABLE "{table}" (id integer primary key, body text)')
                cur.execute(f"INSERT INTO \"{table}\" VALUES (1, 'guardrail')")

    try:
        yield universe
    finally:
        _drop_probe(superuser_conn, universe)


def _drop_probe(conn: psycopg.Connection[Any], probe: Probe) -> None:
    """Remove the whole universe, then re-raise whatever went wrong.

    ``DROP OWNED BY`` before ``DROP ROLE``, for ``test_adk_db_isolation``'s
    reason: the init script grants CONNECT on shared objects to every login
    role it finds, so a role can hold a privilege on a database this process
    never created. Databases go first - a role cannot be dropped while it owns
    one. Every step is attempted before anything is raised, because a leaked
    database outlives the process and changes what the next run sees.
    """
    failures: list[BaseException] = []
    for database in (probe.corpus_db, probe.control_db):
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        except psycopg.Error as exc:
            failures.append(exc)

    for role in (probe.sync_role, probe.read_role, probe.bystander):
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
                if cur.fetchone() is None:
                    continue
                cur.execute(f'DROP OWNED BY "{role}"')
                cur.execute(f'DROP ROLE "{role}"')
        except psycopg.Error as exc:
            failures.append(exc)

    if failures:
        raise failures[0]


def _provision(probe: Probe) -> None:
    run_init_script(
        knowledge_db=probe.corpus_db,
        control_db=probe.control_db,
        sync_role=probe.sync_role,
        read_role=probe.read_role,
        sync_password=probe.password,
        read_password=probe.password,
    )


def _assert_refused(database: str, user: str, password: str) -> None:
    """Attempt the connection for real and assert Postgres refused it on privilege.

    A wrong password also raises ``OperationalError``, which would make this
    pass for the wrong reason, so that case is excluded explicitly.
    """
    try:
        connect(database, user, password).close()
    except psycopg.OperationalError as exc:
        message = str(exc)
    else:
        pytest.fail(
            f"{user!r} opened a connection to {database!r}, so a control-plane "
            "database is reachable from a knowledge role"
        )
    assert "password authentication failed" not in message, message
    assert "permission denied for database" in message, message
    assert database in message, message


def test_neither_knowledge_role_can_reach_the_control_plane(probe: Probe) -> None:
    _provision(probe)

    _assert_refused(probe.control_db, probe.sync_role, probe.password)
    _assert_refused(probe.control_db, probe.read_role, probe.password)


def test_a_bystander_role_keeps_the_connection_it_had(probe: Probe) -> None:
    """Closing the control plane to two roles must not lock out every other one.

    The script revokes CONNECT from PUBLIC, which is the grant every other
    non-superuser login role was relying on. It hands each of them an explicit
    CONNECT first, and this is that promise, attempted rather than read.
    """
    _provision(probe)

    with connect(probe.control_db, probe.bystander, probe.password) as conn:
        assert scalar(conn, "SELECT current_user") == probe.bystander


def test_the_sync_role_owns_its_own_database(probe: Probe) -> None:
    _provision(probe)

    with connect(probe.corpus_db, probe.sync_role, probe.password) as conn:
        assert scalar(conn, "SELECT current_user") == probe.sync_role


def test_the_reader_can_connect_but_the_corpus_is_not_public(probe: Probe) -> None:
    _provision(probe)

    with connect(probe.corpus_db, probe.read_role, probe.password) as conn:
        assert scalar(conn, "SELECT current_user") == probe.read_role
    with connect("postgres", db_config.user, db_config.password) as conn:
        assert scalar(
            conn,
            "SELECT has_database_privilege('public', %s, 'CONNECT')",
            probe.corpus_db,
        ) is False


def test_the_reader_can_select_after_the_migrations_and_still_cannot_write(probe: Probe) -> None:
    """The positive direction, which the negative one cannot stand in for."""
    _provision(probe)
    sync_url = url_for(probe.sync_role, probe.password, probe.corpus_db)
    read_url = url_for(probe.read_role, probe.password, probe.corpus_db)
    migrate(sync_url)

    engine = sa.create_engine(read_url, future=True, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version FROM schema_meta")).scalar_one() >= 1
            assert conn.execute(sa.text("SELECT count(*) FROM sources")).scalar_one() == 0
        with engine.connect() as conn, pytest.raises(sa.exc.ProgrammingError):
            conn.execute(sa.text("UPDATE schema_meta SET version = 99"))
    finally:
        engine.dispose()


def test_the_migration_grants_the_reader_its_select_without_the_script(
    probe: Probe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration 001's grant block, isolated as the only grantor.

    Everything else here provisions and then migrates, which is the deployment
    order and hides this: the init script's section 4 declares its default
    privileges FOR ROLE <sync>, so tables the migration creates afterwards are
    already readable and the migration's own grants prove nothing. Delete them
    and every other test still passes.

    So the script's contribution is revoked between the two steps and the
    migration is left to stand on its own. What is being guarded is the
    silent failure: the reader connects, sees no tables, and every search
    refuses exactly as it would against a corpus nobody has synced.
    """
    _provision(probe)
    with connect(probe.corpus_db, db_config.user, db_config.password) as conn:
        conn.execute(
            f'ALTER DEFAULT PRIVILEGES FOR ROLE "{probe.sync_role}" IN SCHEMA public '
            f'REVOKE SELECT ON TABLES FROM "{probe.read_role}"'
        )
        conn.execute(f'REVOKE USAGE ON SCHEMA public FROM "{probe.read_role}"')

    monkeypatch.setenv("AGENT_KNOWLEDGE_READ_ROLE", probe.read_role)
    migrate(url_for(probe.sync_role, probe.password, probe.corpus_db))

    with connect(probe.corpus_db, probe.read_role, probe.password) as conn:
        assert scalar(conn, "SELECT version FROM schema_meta WHERE id = 1") >= 1


def test_rerunning_provisioning_repairs_a_reader_that_lost_its_grant(probe: Probe) -> None:
    """Section 4 of the script is a repair, and section 5 is its receipt.

    The failure being repaired is the one that looks like nothing: with SELECT
    revoked, the reader connects fine and every query answers "no rows".
    """
    _provision(probe)
    sync_url = url_for(probe.sync_role, probe.password, probe.corpus_db)
    read_url = url_for(probe.read_role, probe.password, probe.corpus_db)
    migrate(sync_url)

    sync_engine = sa.create_engine(sync_url, future=True, poolclass=NullPool)
    read_engine = sa.create_engine(read_url, future=True, poolclass=NullPool)
    try:
        with sync_engine.begin() as conn:
            conn.execute(sa.text(f'REVOKE SELECT ON schema_meta FROM "{probe.read_role}"'))
        with read_engine.connect() as conn, pytest.raises(sa.exc.ProgrammingError):
            conn.execute(sa.text("SELECT version FROM schema_meta"))

        _provision(probe)

        with read_engine.connect() as conn:
            assert conn.execute(sa.text("SELECT version FROM schema_meta")).scalar_one() >= 1
    finally:
        sync_engine.dispose()
        read_engine.dispose()


def test_provisioning_is_idempotent(probe: Probe) -> None:
    _provision(probe)
    _provision(probe)

    _assert_refused(probe.control_db, probe.sync_role, probe.password)
    with connect(probe.corpus_db, probe.read_role, probe.password) as conn:
        assert scalar(conn, "SELECT 1") == 1


def test_a_missing_control_database_is_reported_and_skipped(probe: Probe) -> None:
    """The extra run compose wires in unconditionally must not fail the job."""
    result = run_init_script(
        knowledge_db=probe.corpus_db,
        control_db=f"kn_absent_{uuid.uuid4().hex[:8]}",
        sync_role=probe.sync_role,
        read_role=probe.read_role,
        sync_password=probe.password,
        read_password=probe.password,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "control database not found" in result.stdout + result.stderr


def test_the_script_refuses_to_run_without_passwords(probe: Probe) -> None:
    result = run_init_script(
        knowledge_db=probe.corpus_db,
        sync_role=probe.sync_role,
        read_role=probe.read_role,
        sync_password="",
        read_password="",
        check=False,
    )

    assert result.returncode != 0
    assert "knowledge_sync_password is required" in result.stderr
