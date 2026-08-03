"""Executor database isolation, proved by attempting the connections.

Every privilege assertion goes through a real Postgres connection or a real
psql run. None asserts that a GRANT statement appears in a file: the feature is
a runtime privilege, and Postgres grants are additive in ways that make the SQL
text a bad proxy for the outcome. ``test_naive_revoke_from_the_role_alone_is_a
_no_op`` exists precisely because SQL that reads correctly leaves the control
plane wide open.

Two groups: the deployed state on the running instance, and
``server/scripts/adk_db_init.sql`` run against a throwaway role and a throwaway
pair of databases created per test, so the live stack is never the subject.
Skips, never silent passes: each reason says what is missing and how to get it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest
import yaml  # type: ignore[import-untyped]

from agent_control_server.config import AgentControlServerDatabaseConfig, db_config

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
INIT_SCRIPT: Final = REPO_ROOT / "server" / "scripts" / "adk_db_init.sql"

ADK_ROLE: Final = "adk"
ADK_DB: Final = "adk_runtime"
CONTROL_DATABASES: Final = ("agent_control", "agent_control_test")
GUARDRAIL_TABLES: Final = ("controls", "policies", "control_bindings")
CONNECT_TIMEOUT: Final = 5

# One instance-wide critical section, shared by every process that runs this
# module against the same Postgres.
#
# The tests below create real login roles, and ``adk_db_init.sql`` deliberately
# reaches every non-superuser login role on the instance: before it revokes
# CONNECT from PUBLIC it hands each one an explicit CONNECT, which is how it
# avoids locking real roles out. Two copies of this module therefore grant each
# other privileges on throwaway databases the other is about to drop, and the
# loser's ``DROP ROLE`` fails at teardown - one error in an otherwise green
# suite, in a module that is clean every time you run it on its own.
#
# Serialising the role-owning window is the fix. It is not a retry: contention
# is resolved by waiting for the other run to finish its two seconds of work,
# and a lock that never arrives is reported rather than slept through.
MODULE_LOCK_KEY: Final = 0x41444B49  # "ADKI"
MODULE_LOCK_TIMEOUT: Final = 300

# Sentinel: "the probe's own password", as distinct from "no password at all".
_PROBE_DEFAULT: Final = "\x00use-probe-password"

# Same default as the ADK_DB_PASSWORD fallback in docker-compose.dev.yml.
ADK_PASSWORD: Final = os.environ.get("ADK_DB_PASSWORD", "adk_local")


# --- Connection helpers ---------------------------------------------------


def _connect(database: str, user: str, password: str) -> psycopg.Connection[Any]:
    conn = psycopg.connect(
        host=db_config.host,
        port=db_config.port,
        user=user,
        password=password,
        dbname=database,
        connect_timeout=CONNECT_TIMEOUT,
    )
    conn.autocommit = True
    return conn


def _scalar(conn: psycopg.Connection[Any], sql: str, *params: Any) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params or None)  # type: ignore[arg-type]
        row = cur.fetchone()
    return None if row is None else row[0]


def _drop_roles(conn: psycopg.Connection[Any], *roles: str) -> None:
    """Drop throwaway roles, including privileges somebody else granted them.

    ``DROP ROLE`` refuses while a privilege on a *shared* object still points at
    the role, and database CONNECT is exactly such a privilege::

        role "adk_probe_a" cannot be dropped because some objects depend on it
        DETAIL:  privileges for database adk_probe_control_b

    Note the two different suffixes. Nothing in this module grants a probe role
    anything on another probe's database - ``adk_db_init.sql`` does. Before it
    revokes CONNECT from PUBLIC it hands an explicit CONNECT to every
    non-superuser login role on the instance, which is how it avoids locking
    real roles out. A second process running this same module owns some of
    those roles, so its probes pick up a grant on a control database this
    process never created and will never drop, and the teardown below fails for
    whichever process gets there first. Twenty tests pass and the module
    reports one error, which is what made this look like a random flake.

    ``DROP OWNED BY`` clears precisely that: privileges on shared objects, plus
    anything the role owns in the current database. It has no ``IF EXISTS``
    form and errors on an absent role, hence the existence check rather than a
    swallowed exception.
    """
    for role in roles:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                continue
            cur.execute(f'DROP OWNED BY "{role}"')
            cur.execute(f'DROP ROLE "{role}"')


def _assert_refused(database: str, user: str, password: str) -> str:
    """Attempt the connection for real and assert Postgres refused it.

    A wrong password also raises ``OperationalError``, which would make this
    pass for the wrong reason, so that case is excluded explicitly.
    """
    try:
        _connect(database, user, password).close()
    except psycopg.OperationalError as exc:
        message = str(exc)
    else:
        pytest.fail(
            f"{user!r} opened a connection to {database!r}, so the control plane is "
            f"reachable from the executor's role. If {database!r} was created or "
            "recreated after provisioning, re-run the adk-db-init job against it."
        )
    assert "password authentication failed" not in message, (
        f"connection to {database!r} as {user!r} failed on authentication, not on "
        f"privilege, so this proves nothing about isolation: {message}"
    )
    assert "permission denied for database" in message, message
    assert database in message, message
    return message


# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(scope="module")
def maintenance_conn() -> Iterator[psycopg.Connection[Any]]:
    """Superuser-ish connection to the maintenance database."""
    try:
        conn = _connect("postgres", db_config.user, db_config.password)
    except psycopg.OperationalError as exc:
        pytest.skip(
            "no Postgres reachable at "
            f"{db_config.host}:{db_config.port} as {db_config.user!r} ({exc}); "
            "start it with `docker compose -f docker-compose.dev.yml up -d`"
        )
    with conn:
        yield conn


@pytest.fixture(scope="module")
def superuser_conn(
    maintenance_conn: psycopg.Connection[Any],
) -> Iterator[psycopg.Connection[Any]]:
    """The maintenance connection, held under this module's instance-wide lock.

    Every test that creates a login role goes through this fixture, so taking
    the lock here covers the whole window in which a concurrent run could grant
    our throwaway roles something. See ``MODULE_LOCK_KEY``. The lock is
    session-scoped in Postgres, so a crashed run releases it on disconnect
    rather than wedging the next one.
    """
    if not _scalar(maintenance_conn, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
        pytest.skip(
            f"role {db_config.user!r} is not a superuser, so this test cannot create the "
            "throwaway roles and databases it needs to prove isolation empirically"
        )

    _scalar(
        maintenance_conn,
        "SELECT set_config('lock_timeout', %s, false)",
        f"{MODULE_LOCK_TIMEOUT}s",
    )
    try:
        _scalar(maintenance_conn, "SELECT pg_advisory_lock(%s)", MODULE_LOCK_KEY)
    except psycopg.errors.LockNotAvailable:
        pytest.fail(
            "another run of this module has held the instance-wide lock on "
            f"{db_config.host}:{db_config.port} for more than {MODULE_LOCK_TIMEOUT}s. "
            "Only one copy may own login roles at a time, because adk_db_init.sql "
            "grants CONNECT to every login role it finds."
        )

    try:
        yield maintenance_conn
    finally:
        _scalar(maintenance_conn, "SELECT pg_advisory_unlock(%s)", MODULE_LOCK_KEY)
        _scalar(maintenance_conn, "SELECT set_config('lock_timeout', '0', false)")


@pytest.fixture(scope="module")
def provisioned(maintenance_conn: psycopg.Connection[Any]) -> None:
    """Skip unless the deployed executor role and database are actually there."""
    hint = (
        "run `docker compose -f docker-compose.dev.yml up adk-db-init`, or "
        f"psql -f {INIT_SCRIPT.relative_to(REPO_ROOT)} against the maintenance database"
    )
    if not _scalar(maintenance_conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", ADK_ROLE):
        pytest.skip(f"role {ADK_ROLE!r} does not exist on this instance; {hint}")
    if not _scalar(maintenance_conn, "SELECT 1 FROM pg_database WHERE datname = %s", ADK_DB):
        pytest.skip(f"database {ADK_DB!r} does not exist on this instance; {hint}")
    try:
        _connect(ADK_DB, ADK_ROLE, ADK_PASSWORD).close()
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"cannot authenticate as {ADK_ROLE!r} with the password from ADK_DB_PASSWORD "
            f"(default {ADK_PASSWORD!r}); set it to the provisioned one to run these tests ({exc})"
        )


@pytest.fixture(scope="module")
def psql_bin() -> str:
    found = shutil.which("psql")
    if found is None:
        pytest.skip("psql is not on PATH; it is required to execute server/scripts/adk_db_init.sql")
    if not INIT_SCRIPT.is_file():
        pytest.skip(f"{INIT_SCRIPT} is missing")
    return found


@dataclass(frozen=True)
class Probe:
    """One throwaway executor role, runtime database and control database.

    Every name is uuid-suffixed, so tests never touch the live databases and
    never collide with each other by name. Names are not the whole story:
    ``adk_db_init.sql`` reaches every login role on the instance, so teardown
    has to cope with a probe from a concurrent run holding a grant on this
    one's database. See ``_drop_roles``.
    """

    role: str
    password: str
    runtime_db: str
    control_db: str
    bystander: str
    bystander_password: str


@pytest.fixture
def probe(superuser_conn: psycopg.Connection[Any], psql_bin: str) -> Iterator[Probe]:
    suffix = uuid.uuid4().hex[:10]
    p = Probe(
        role=f"adk_probe_{suffix}",
        password=f"probe_pw_{suffix}",
        runtime_db=f"adk_probe_runtime_{suffix}",
        control_db=f"adk_probe_control_{suffix}",
        bystander=f"adk_probe_bystander_{suffix}",
        bystander_password=f"bystander_pw_{suffix}",
    )
    with superuser_conn.cursor() as cur:
        # A control database in its default state: PUBLIC holds CONNECT, exactly
        # as agent_control did before this feature.
        cur.execute(f'CREATE DATABASE "{p.control_db}"')
        cur.execute(f"CREATE ROLE \"{p.role}\" LOGIN PASSWORD '{p.password}'")
        cur.execute(f"CREATE ROLE \"{p.bystander}\" LOGIN PASSWORD '{p.bystander_password}'")

    with _connect(p.control_db, db_config.user, db_config.password) as conn, conn.cursor() as cur:
        for table in GUARDRAIL_TABLES:
            cur.execute(f'CREATE TABLE "{table}" (id integer primary key, body text)')
            cur.execute(f'INSERT INTO "{table}" VALUES (1, \'guardrail\')')

    try:
        yield p
    finally:
        _drop_probe(superuser_conn, p)


def _drop_probe(conn: psycopg.Connection[Any], p: Probe) -> None:
    """Remove the probe's universe, then re-raise whatever went wrong.

    Every step is attempted before anything is raised. A cleanup that stops at
    the first error leaves the rest of the universe on the instance, and a
    leaked database or role outlives the process and changes what the next run
    sees - which turns one honest failure into an intermittent one. The first
    error still propagates, so nothing is swallowed.

    Databases go first: a role cannot be dropped while it owns one.
    """
    failures: list[BaseException] = []

    for database in (p.runtime_db, p.control_db):
        try:
            with conn.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        except psycopg.Error as exc:
            failures.append(exc)

    for role in (p.role, p.bystander):
        try:
            _drop_roles(conn, role)
        except psycopg.Error as exc:
            failures.append(exc)

    if failures:
        raise failures[0]


def _apply(
    psql_bin: str,
    p: Probe,
    *,
    password: str | None = _PROBE_DEFAULT,
    control_db: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the init script against the probe's universe.

    ``password=None`` omits ``-v adk_password`` entirely, which is how the
    script's own missing-password guard gets exercised.
    """
    if password == _PROBE_DEFAULT:
        password = p.password

    # -w so a missing PGPASSWORD fails instead of hanging on a prompt.
    args = [psql_bin, "-w", "-v", "ON_ERROR_STOP=1"]
    args += ["-h", db_config.host, "-p", str(db_config.port)]
    args += ["-U", db_config.user, "-d", "postgres"]
    args += ["-v", f"adk_role={p.role}", "-v", f"adk_db={p.runtime_db}"]
    args += ["-v", f"control_db={control_db or p.control_db}"]
    if password is not None:
        args += ["-v", f"adk_password={password}"]
    args += ["-f", str(INIT_SCRIPT)]

    return subprocess.run(
        args,
        env={**os.environ, "PGPASSWORD": db_config.password},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _database_acl(conn: psycopg.Connection[Any], database: str) -> str:
    return str(_scalar(conn, "SELECT datacl::text FROM pg_database WHERE datname = %s", database))


# --- The deployed state -----------------------------------------------------


@pytest.mark.parametrize("control_db", CONTROL_DATABASES)
def test_adk_role_is_refused_a_connection_to_the_control_plane(
    maintenance_conn: psycopg.Connection[Any], provisioned: None, control_db: str
) -> None:
    if not _scalar(maintenance_conn, "SELECT 1 FROM pg_database WHERE datname = %s", control_db):
        pytest.skip(f"{control_db!r} does not exist on this instance")

    message = _assert_refused(control_db, ADK_ROLE, ADK_PASSWORD)
    assert "does not have CONNECT privilege" in message


def test_adk_role_owns_and_can_use_its_own_database(provisioned: None) -> None:
    table = f"probe_{uuid.uuid4().hex[:10]}"
    with _connect(ADK_DB, ADK_ROLE, ADK_PASSWORD) as conn:
        assert _scalar(conn, "SELECT current_database()") == ADK_DB
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE "{table}" (id integer primary key, payload text)')
            cur.execute(f'INSERT INTO "{table}" VALUES (1, \'session\')')
            cur.execute(f'SELECT payload FROM "{table}" WHERE id = 1')
            assert cur.fetchone() == ("session",)
            cur.execute(f'DROP TABLE "{table}"')


def test_adk_role_cannot_reach_guardrail_tables_by_any_route(provisioned: None) -> None:
    """Direct connection is covered above; this closes the ways around it."""
    with _connect(ADK_DB, ADK_ROLE, ADK_PASSWORD) as conn:
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            _scalar(conn, "SELECT * FROM agent_control.public.controls LIMIT 1")

        # A cross-database pivot needs one of these, and creating an extension
        # is superuser territory.
        for extension in ("dblink", "postgres_fdw"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                _scalar(conn, f"CREATE EXTENSION {extension}")

        with pytest.raises(psycopg.errors.UndefinedFunction):
            _scalar(conn, "SELECT * FROM dblink('dbname=agent_control', 'select 1') AS t(x int)")


def test_adk_role_holds_no_privilege_on_the_guardrail_tables(
    maintenance_conn: psycopg.Connection[Any], provisioned: None
) -> None:
    """Defence in depth: even granted CONNECT tomorrow, it could read nothing."""
    with _connect("agent_control", db_config.user, db_config.password) as conn:
        present = [
            table
            for table in GUARDRAIL_TABLES
            if _scalar(conn, "SELECT to_regclass(%s) IS NOT NULL", f"public.{table}")
        ]
        assert present, "no guardrail tables in agent_control; this test's assumption is stale"

        sql = "SELECT has_table_privilege(%s, %s, %s)"
        for table in present:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                granted = _scalar(conn, sql, ADK_ROLE, f"public.{table}", privilege)
                assert granted is False, f"{ADK_ROLE} holds {privilege} on {table}"

        can_create = _scalar(conn, "SELECT has_schema_privilege(%s, 'public', 'CREATE')", ADK_ROLE)
        assert can_create is False


def test_adk_role_inherits_nothing(
    maintenance_conn: psycopg.Connection[Any], provisioned: None
) -> None:
    row = _scalar(
        maintenance_conn,
        "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
        "OR rolinherit FROM pg_roles WHERE rolname = %s",
        ADK_ROLE,
    )
    assert row is False, f"{ADK_ROLE} carries an attribute that could route around the revoke"

    memberships = _scalar(
        maintenance_conn,
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
        "WHERE r.rolname = %s",
        ADK_ROLE,
    )
    assert memberships == 0


@pytest.mark.parametrize("control_db", CONTROL_DATABASES)
def test_public_connect_leaves_no_bypass_on_the_control_plane(
    superuser_conn: psycopg.Connection[Any], control_db: str
) -> None:
    """A brand-new role, granted nothing, must still be refused.

    If PUBLIC still held CONNECT, this role would get in with no grant of its
    own - exactly how ``adk`` reached the control plane before the revoke.
    """
    if not _scalar(superuser_conn, "SELECT 1 FROM pg_database WHERE datname = %s", control_db):
        pytest.skip(f"{control_db!r} does not exist on this instance")

    role = f"adk_public_probe_{uuid.uuid4().hex[:10]}"
    password = f"pw_{uuid.uuid4().hex[:10]}"
    with superuser_conn.cursor() as cur:
        cur.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
    try:
        _assert_refused(control_db, role, password)
    finally:
        _drop_roles(superuser_conn, role)


def test_control_plane_role_is_unaffected(maintenance_conn: psycopg.Connection[Any]) -> None:
    exists = "SELECT 1 FROM pg_database WHERE datname = %s"
    for control_db in CONTROL_DATABASES:
        if not _scalar(maintenance_conn, exists, control_db):
            continue
        with _connect(control_db, db_config.user, db_config.password) as conn:
            assert _scalar(conn, "SELECT current_database()") == control_db
            tables = _scalar(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
            assert isinstance(tables, int)


# --- The init script --------------------------------------------------------


def test_naive_revoke_from_the_role_alone_is_a_no_op(
    superuser_conn: psycopg.Connection[Any], probe: Probe
) -> None:
    """The trap this feature exists to avoid, pinned as a test.

    ``REVOKE CONNECT ON DATABASE <db> FROM <role>`` reads like it closes the
    door. It does not: grants are additive, revoking a privilege never granted
    directly to the role creates no negative grant, and CONNECT arrives via
    PUBLIC. A failure here means the script was "simplified" back to it.
    """
    with superuser_conn.cursor() as cur:
        cur.execute(f'REVOKE CONNECT ON DATABASE "{probe.control_db}" FROM "{probe.role}"')

    with _connect(probe.control_db, probe.role, probe.password) as conn:
        assert _scalar(conn, "SELECT current_database()") == probe.control_db
        tables = _scalar(conn, "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        assert tables >= len(GUARDRAIL_TABLES), "the role can enumerate the guardrail tables"


def test_init_script_provisions_and_isolates_on_an_already_initialised_cluster(
    superuser_conn: psycopg.Connection[Any], psql_bin: str, probe: Probe
) -> None:
    """The failure mode the plan warns about: the data directory already exists.

    This cluster was initialised long before the script existed, so anything
    dropped into /docker-entrypoint-initdb.d would never have run here.
    """
    assert not _scalar(
        superuser_conn, "SELECT 1 FROM pg_database WHERE datname = %s", probe.runtime_db
    ), "the runtime database exists before the script ran; this proves nothing"

    result = _apply(psql_bin, probe)
    assert result.returncode == 0, result.stderr

    owner_sql = "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = %s"
    assert _scalar(superuser_conn, owner_sql, probe.runtime_db) == probe.role

    with _connect(probe.runtime_db, probe.role, probe.password) as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE sessions (id integer primary key, body text)")
        cur.execute("INSERT INTO sessions VALUES (1, 'turn')")
        cur.execute("SELECT body FROM sessions WHERE id = 1")
        assert cur.fetchone() == ("turn",)
        cur.execute("DROP TABLE sessions")

    _assert_refused(probe.control_db, probe.role, probe.password)


def test_init_script_preserves_access_for_other_login_roles(psql_bin: str, probe: Probe) -> None:
    """Revoking CONNECT from PUBLIC must not lock out roles relying on it."""
    with _connect(probe.control_db, probe.bystander, probe.bystander_password) as conn:
        assert _scalar(conn, "SELECT current_database()") == probe.control_db

    assert _apply(psql_bin, probe).returncode == 0

    with _connect(probe.control_db, probe.bystander, probe.bystander_password) as conn:
        assert _scalar(conn, "SELECT current_database()") == probe.control_db

    _assert_refused(probe.control_db, probe.role, probe.password)


def test_init_script_is_idempotent(
    superuser_conn: psycopg.Connection[Any], psql_bin: str, probe: Probe
) -> None:
    def acls() -> tuple[str, str]:
        return (
            _database_acl(superuser_conn, probe.control_db),
            _database_acl(superuser_conn, probe.runtime_db),
        )

    assert _apply(psql_bin, probe).returncode == 0
    baseline = acls()

    for run in range(2):
        result = _apply(psql_bin, probe)
        assert result.returncode == 0, f"re-run {run + 2} failed: {result.stderr}"
        assert acls() == baseline, f"re-run {run + 2} changed the ACLs: {baseline} -> {acls()}"

    _assert_refused(probe.control_db, probe.role, probe.password)
    _connect(probe.runtime_db, probe.role, probe.password).close()


def test_init_script_refuses_to_run_without_a_password(psql_bin: str, probe: Probe) -> None:
    result = _apply(psql_bin, probe, password=None)
    assert result.returncode != 0
    assert "adk_password is required" in result.stderr


def test_init_script_skips_a_missing_control_database(psql_bin: str, probe: Probe) -> None:
    result = _apply(psql_bin, probe, control_db=f"absent_{uuid.uuid4().hex[:10]}")
    assert result.returncode == 0, result.stderr
    assert "skipped the REVOKE step" in (result.stdout + result.stderr)


# --- This module's own cleanup ----------------------------------------------


def test_a_probe_role_is_droppable_after_a_concurrent_run_grants_it_connect(
    superuser_conn: psycopg.Connection[Any],
) -> None:
    """Teardown must not assume this process is the only one on the instance.

    Reproduces the grant that ``adk_db_init.sql`` makes on every non-superuser
    login role it finds, standing in for a second process running this module
    at the same time. Revert ``_drop_roles`` to a bare ``DROP ROLE`` and this
    fails with ``DependentObjectsStillExist``, which is the error that used to
    surface as one teardown error somewhere in a full suite run and never once
    when the module was run on its own.
    """
    suffix = uuid.uuid4().hex[:10]
    role = f"adk_probe_concurrent_{suffix}"
    foreign_db = f"adk_probe_foreign_{suffix}"

    with superuser_conn.cursor() as cur:
        cur.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD 'pw_{suffix}'")

    # Inside the try, so a failure here still reaches the cleanup below. A test
    # about not leaking roles has no business leaking one of its own.
    try:
        with superuser_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{foreign_db}"')
            cur.execute(f'GRANT CONNECT ON DATABASE "{foreign_db}" TO "{role}"')

        _drop_roles(superuser_conn, role)

        assert _scalar(superuser_conn, "SELECT 1 FROM pg_roles WHERE rolname = %s", role) is None
    finally:
        with superuser_conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{foreign_db}" WITH (FORCE)')
        _drop_roles(superuser_conn, role)


# --- Deployment path and credential separation ------------------------------


def _compose(name: str) -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load((REPO_ROOT / name).read_text())
    return parsed


@pytest.mark.parametrize("compose_file", ("docker-compose.yml", "docker-compose.dev.yml"))
def test_no_compose_service_relies_on_the_postgres_init_directory(compose_file: str) -> None:
    """Init scripts run only against an empty data directory, so this path is a trap."""
    for name, service in _compose(compose_file).get("services", {}).items():
        for volume in service.get("volumes", []) or []:
            target = volume.get("target", "") if isinstance(volume, dict) else volume
            assert "docker-entrypoint-initdb.d" not in target, (
                f"{compose_file}:{name} provisions through the Postgres init directory, "
                "which is skipped whenever the pgdata volume already exists"
            )


def test_dev_compose_gates_provisioning_on_a_healthy_postgres() -> None:
    service = _compose("docker-compose.dev.yml")["services"]["adk-db-init"]
    assert service["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert service.get("restart") == "no"
    assert any("adk_db_init.sql" in str(volume) for volume in service["volumes"])


def test_published_compose_file_carries_no_executor_wiring() -> None:
    """The quick start stays untouched until the executor is real."""
    text = (REPO_ROOT / "docker-compose.yml").read_text().lower()
    assert "adk" not in text


def test_executor_password_cannot_leak_into_server_database_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADK_DB_PASSWORD is deliberately not an AGENT_CONTROL_ setting."""
    monkeypatch.setenv("ADK_DB_PASSWORD", "executor-only-secret")
    for leaky in ("AGENT_CONTROL_DB_URL", "DATABASE_URL", "DB_URL"):
        monkeypatch.delenv(leaky, raising=False)

    config = AgentControlServerDatabaseConfig()
    assert config.password != "executor-only-secret"
    assert "executor-only-secret" not in config.get_url()
