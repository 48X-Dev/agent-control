"""Stand up a real ``agent_knowledge`` for a test run, and take it down again.

The suite provisions the corpus the way a deployment does - the shipped init
script, then the shipped migrations - rather than by creating tables from
metadata. Provisioning *is* the feature here: the reader's SELECT arrives from
an ``ALTER DEFAULT PRIVILEGES`` in migration 001 and from nowhere else, so a
fixture that built the schema by hand would test a database no deployment ever
has and would pass with the grants missing.

The database name carries a per-process token, mirroring ``server/conftest.py``
for its reason: two pytest processes against one Postgres must not be inside
each other's data. The roles are instance-global and shared, exactly as ``adk``
is, and nothing here mutates them beyond converging them on the same password
the compose files use.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import psycopg
from agent_control_server.config import db_config

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SERVER_DIR: Final = REPO_ROOT / "server"
INIT_SCRIPT: Final = SERVER_DIR / "scripts" / "knowledge_db_init.sql"
ALEMBIC_INI: Final = SERVER_DIR / "knowledge_alembic.ini"
ALEMBIC_DIR: Final = SERVER_DIR / "knowledge_alembic"

SYNC_ROLE: Final = "knowledge_sync"
READ_ROLE: Final = "knowledge_read"

# Same variables and defaults as docker-compose.dev.yml, so a developer who
# provisioned by hand and a developer who ran compose both land here.
SYNC_PASSWORD: Final = os.environ.get("KNOWLEDGE_DB_PASSWORD", "knowledge_local")
READ_PASSWORD: Final = os.environ.get("KNOWLEDGE_READ_DB_PASSWORD", "knowledge_read_local")

CONNECT_TIMEOUT: Final = 5


@dataclass(frozen=True)
class Corpus:
    """One throwaway corpus database, provisioned and migrated."""

    database: str
    sync_url: str
    read_url: str


def unavailable_reason() -> str | None:
    """Why this run cannot provision a corpus, or ``None`` when it can."""
    if not INIT_SCRIPT.is_file():
        return f"{INIT_SCRIPT} is missing"
    if shutil.which("psql") is None:
        return "psql is not on PATH; it is required to run server/scripts/knowledge_db_init.sql"
    try:
        with connect("postgres", db_config.user, db_config.password) as conn:
            if not scalar(conn, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
                return (
                    f"role {db_config.user!r} is not a superuser, so this run cannot create "
                    "the corpus database and its roles"
                )
    except psycopg.OperationalError as exc:
        return (
            f"no Postgres reachable at {db_config.host}:{db_config.port} as "
            f"{db_config.user!r} ({exc}); start it with "
            "`docker compose -f docker-compose.dev.yml up -d`"
        )
    return None


def provision(prefix: str = "agent_knowledge_test") -> Corpus:
    """Create, provision and migrate a fresh corpus database.

    Init first, then migrate, because that is the order a deployment runs them
    in. Note what that order means and do not mistake it for coverage: the init
    script's section 4 declares its default privileges FOR ROLE <sync>, so
    every table the migration then creates is readable whether or not the
    migration granted anything. Nothing reached from this helper can fail on a
    missing grant in migration 001. The test that isolates it revokes the
    script's contribution in between, and it lives in
    ``test_knowledge_db_isolation.py`` because that is where the throwaway
    roles are.
    """
    database = f"{prefix}_{os.getpid()}_{secrets.token_hex(3)}"[:63]
    run_init_script(knowledge_db=database)
    corpus = Corpus(
        database=database,
        sync_url=url_for(SYNC_ROLE, SYNC_PASSWORD, database),
        read_url=url_for(READ_ROLE, READ_PASSWORD, database),
    )
    migrate(corpus.sync_url)
    return corpus


def teardown(corpus: Corpus) -> None:
    with connect("postgres", db_config.user, db_config.password) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{corpus.database}" WITH (FORCE)')


def url_for(role: str, password: str, database: str) -> str:
    return f"postgresql+psycopg://{role}:{password}@{db_config.host}:{db_config.port}/{database}"


def run_init_script(
    *,
    knowledge_db: str,
    control_db: str | None = None,
    sync_role: str = SYNC_ROLE,
    read_role: str = READ_ROLE,
    sync_password: str = SYNC_PASSWORD,
    read_password: str = READ_PASSWORD,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the shipped script exactly as a deployment does.

    ``control_db`` defaults to a database that does not exist, so the script
    reports and skips its instance-wide REVOKE step. That step grants CONNECT
    to every login role on the instance before revoking it from PUBLIC, which
    is not something a store test should do to a shared Postgres.
    ``test_knowledge_db_isolation.py`` passes a throwaway control database and
    takes a lock while it does.
    """
    command = [
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        db_config.host,
        "-p",
        str(db_config.port),
        "-U",
        db_config.user,
        "-d",
        "postgres",
        "-v",
        f"knowledge_sync_role={sync_role}",
        "-v",
        f"knowledge_read_role={read_role}",
        "-v",
        f"knowledge_sync_password={sync_password}",
        "-v",
        f"knowledge_read_password={read_password}",
        "-v",
        f"knowledge_db={knowledge_db}",
        "-v",
        f"control_db={control_db or f'knowledge_absent_control_{secrets.token_hex(4)}'}",
        "-f",
        str(INIT_SCRIPT),
    ]
    result = subprocess.run(
        command,
        env={**os.environ, "PGPASSWORD": db_config.password},
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"knowledge_db_init.sql failed for {knowledge_db!r}:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def migrate(sync_url: str) -> None:
    from alembic import command as alembic_command
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR).replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", sync_url.replace("%", "%%"))
    alembic_command.upgrade(config, "head")


def connect(database: str, user: str, password: str) -> psycopg.Connection[Any]:
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


def scalar(conn: psycopg.Connection[Any], sql: str, *params: Any) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params or None)  # type: ignore[arg-type]
        row = cur.fetchone()
    return None if row is None else row[0]
