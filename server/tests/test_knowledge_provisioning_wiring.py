"""The provisioning script and the DSN, checked against each other.

``test_knowledge_settings.py`` proves every knowledge flag reaches every
runtime. This file asks the next question, which a name grep cannot: do the
values agree? The roles are created by ``knowledge_db_init.sql`` with one set of
names and passwords, and the server is handed a DSN built somewhere else with
another. Nothing connects the two but a convention, and when the convention
breaks the server refuses every search with ``knowledge_unavailable``, which
reads exactly like a corpus nobody has synced.

Parity is the standing rule, and the two runtimes answer it differently
because they are shaped differently. ``scripts/apple-container-up.sh``
provisions the corpus and starts the server, so both halves are its own and
they are checked against each other. ``docker-compose.yml`` starts a server and
provisions no corpus at all, so the DSN it must NOT invent is the thing checked
there, and the reader a compose operator dials by hand is checked against
``docker-compose.dev.yml``, which is what creates the role.

No database. These are file facts, and they must fail on a laptop with nothing
running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
COMPOSE: Final = REPO_ROOT / "docker-compose.yml"
DEV_COMPOSE: Final = REPO_ROOT / "docker-compose.dev.yml"
APPLE_SCRIPT: Final = REPO_ROOT / "scripts" / "apple-container-up.sh"
ENV_EXAMPLE: Final = REPO_ROOT / "server" / ".env.example"
INIT_SQL: Final = REPO_ROOT / "server" / "scripts" / "knowledge_db_init.sql"

INIT_TEXT: Final = INIT_SQL.read_text()

# The reader's password variable. The sync's is KNOWLEDGE_DB_PASSWORD, and the
# server is never allowed to see it: section 2.2's credential matrix is that the
# control plane holds SELECT and nothing that can write the corpus.
READ_PASSWORD_VAR: Final = "KNOWLEDGE_READ_DB_PASSWORD"
SYNC_PASSWORD_VAR: Final = "KNOWLEDGE_DB_PASSWORD"

_DSN_RE: Final = re.compile(
    r"postgresql\+psycopg://(?P<user>[^:@\s]+):(?P<password>[^@\s]+)"
    r"@(?P<host>[^:/\s]+):(?P<port>\d+)/(?P<database>[A-Za-z0-9_]+)"
)


@dataclass(frozen=True)
class Runtime:
    """One way this stack is brought up, and the two halves it has to keep in step."""

    name: str
    provisions: str
    consumes: str


# Every file that runs the init script. A required variable nobody passes is a
# fresh volume with no roles on it, whichever runtime got there first.
PROVISIONERS: Final = (
    Runtime("compose", DEV_COMPOSE.read_text(), ""),
    Runtime("apple", APPLE_SCRIPT.read_text(), APPLE_SCRIPT.read_text()),
)

# The runtimes that hand a server a corpus DSN, which is the only place the two
# halves can disagree about a role, a database or a password. Compose is absent
# on purpose and the absence is asserted below: it provisions no corpus, so a
# DSN in it would name a database nothing created.
RUNTIMES: Final = tuple(runtime for runtime in PROVISIONERS if runtime.consumes)


def script_default(variable: str) -> str:
    """What the init script falls back to when a runtime passes nothing."""
    match = re.search(rf"^\s*\\set {variable} (\S+)$", INIT_TEXT, re.MULTILINE)
    assert match, f"{variable} has no default in knowledge_db_init.sql"
    return match.group(1)


def required_script_variables() -> list[str]:
    """The variables the script defaults to empty and then raises on.

    Derived rather than listed, so a new required variable that no runtime
    passes fails here instead of at three in the morning on a fresh volume.
    """
    return re.findall(r"^\s*\\set (\w+) ''$", INIT_TEXT, re.MULTILINE)


def passed_variable(provisions: str, variable: str) -> str | None:
    """The value a runtime hands the script for one psql variable, if any."""
    match = re.search(rf"-v\s+{variable}=(\S+)", provisions)
    return None if match is None else match.group(1).strip("\"'")


def knowledge_dsn(consumes: str) -> re.Match[str]:
    """The one DSN the server is handed for the corpus, found by its own name.

    Line-scoped rather than searched across the file, because every runtime
    carries a control-plane DSN too and the two must never be confused for one
    another - which is the point of the whole arrangement.
    """
    matches = [
        found
        for line in consumes.splitlines()
        if "AGENT_CONTROL_KNOWLEDGE_DB_URL" in line
        for found in [_DSN_RE.search(line)]
        if found is not None
    ]
    assert len(matches) == 1, f"expected exactly one knowledge DSN, found {len(matches)}"
    return matches[0]


def default_of(expression: str, variable: str) -> str | None:
    """The ``:-fallback`` inside a shell or compose interpolation."""
    match = re.search(rf"\$\{{{variable}:-([^}}]+)\}}", expression)
    return None if match is None else match.group(1)


def compose(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(path.read_text())
    return parsed


# --- The two halves have to name the same corpus ----------------------------


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda runtime: runtime.name)
def test_the_dsn_names_the_role_and_database_the_script_provisions(runtime: Runtime) -> None:
    """A DSN pointing at a database nobody created refuses every search.

    Neither runtime overrides the script's role or database names, so the
    defaults inside the SQL are the contract. Read them out of the SQL rather
    than repeating them here: a rename that touches only one side is exactly
    the failure this is for.
    """
    dsn = knowledge_dsn(runtime.consumes)

    expected_role = passed_variable(runtime.provisions, "knowledge_read_role") or script_default(
        "knowledge_read_role"
    )
    expected_database = passed_variable(runtime.provisions, "knowledge_db") or script_default(
        "knowledge_db"
    )

    assert dsn.group("user") == expected_role
    assert dsn.group("database") == expected_database


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda runtime: runtime.name)
def test_the_password_the_reader_is_created_with_is_the_one_the_server_sends(
    runtime: Runtime,
) -> None:
    """One variable, two places, and the same fallback in both.

    Divergent fallbacks are the interesting case: a deployment that sets the
    variable works, and a developer who runs `up` with an empty environment
    gets a role created with one password and a server dialling with another.
    """
    provisioned = passed_variable(runtime.provisions, "knowledge_read_password")
    assert provisioned is not None, f"{runtime.name} never passes knowledge_read_password"
    assert READ_PASSWORD_VAR in provisioned

    dsn_password = knowledge_dsn(runtime.consumes).group("password")
    assert READ_PASSWORD_VAR in dsn_password

    provisioned_default = default_of(provisioned, READ_PASSWORD_VAR) or default_of(
        runtime.provisions, READ_PASSWORD_VAR
    )
    assert provisioned_default == default_of(dsn_password, READ_PASSWORD_VAR)


@pytest.mark.parametrize("runtime", PROVISIONERS, ids=lambda runtime: runtime.name)
def test_every_variable_the_script_insists_on_is_passed(runtime: Runtime) -> None:
    """The script raises on an empty password rather than creating a role without one."""
    required = required_script_variables()

    assert required, "knowledge_db_init.sql declares no required variables"
    for variable in required:
        assert passed_variable(runtime.provisions, variable) is not None, (
            f"{runtime.name} does not pass {variable} to knowledge_db_init.sql"
        )


# --- What the control plane must never be handed ----------------------------


@pytest.mark.parametrize("runtime", RUNTIMES, ids=lambda runtime: runtime.name)
def test_the_server_is_never_handed_the_sync_credential(runtime: Runtime) -> None:
    """Proof by absence, and the credential matrix is the whole design.

    The sync role owns the corpus and parses hostile bytes. A control plane
    holding its password could rewrite the snippets a later turn reads back,
    and the separation would be a comment rather than a privilege.
    """
    dsn = knowledge_dsn(runtime.consumes).group(0)

    assert "knowledge_sync" not in dsn
    assert SYNC_PASSWORD_VAR not in dsn

    for line in runtime.consumes.splitlines():
        if SYNC_PASSWORD_VAR in line:
            assert "knowledge_sync_password" in line, line.strip()


def test_the_published_compose_file_carries_no_sync_credential_at_all() -> None:
    """The file a deployment copies has no reason to know the writer exists."""
    assert SYNC_PASSWORD_VAR not in COMPOSE.read_text()


# --- The runtime that provisions nothing must not claim a corpus ------------


def test_compose_provisions_no_corpus_and_therefore_names_none() -> None:
    """A DSN for a database this file never creates is worse than no DSN.

    ``docker-compose.yml`` starts a server and no ``knowledge-db-init``. With a
    URL baked in, the two states an operator most needs to tell apart become
    one: the feature off logs the configured-but-disabled line on every boot of
    every stack that never wanted it, and the feature on without provisioning
    refuses every search with no startup line at all, because the half-on guard
    only fires on enabled-with-no-DSN.
    """
    parsed = compose(COMPOSE)

    assert "knowledge-db-init" not in parsed["services"]

    environment = parsed["services"]["server"]["environment"]
    spelled_out = {
        name for name, value in environment.items() if _DSN_RE.search(str(value)) is not None
    }
    assert spelled_out == {"AGENT_CONTROL_DB_URL"}


def test_compose_passes_the_dsn_through_rather_than_defaulting_it() -> None:
    """Set in `.env`, it has to reach the container; unset, it has to stay unset."""
    environment = compose(COMPOSE)["services"]["server"]["environment"]

    assert environment["AGENT_CONTROL_KNOWLEDGE_DB_URL"] == "${AGENT_CONTROL_KNOWLEDGE_DB_URL:-}"


# --- The one-shot, and the documentation a developer copies -----------------


def test_the_dev_one_shot_waits_for_a_healthy_postgres() -> None:
    """Provisioning that races the database it provisions leaves no roles."""
    service = compose(DEV_COMPOSE)["services"]["knowledge-db-init"]

    assert service["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert service.get("restart", "no") == "no"


def test_the_one_shot_runs_the_shipped_script_read_only() -> None:
    """A copy of the SQL inside a compose file is a second script to forget."""
    service = compose(DEV_COMPOSE)["services"]["knowledge-db-init"]
    mounts = [str(volume) for volume in service["volumes"]]

    assert any("server/scripts/knowledge_db_init.sql" in mount for mount in mounts), mounts
    assert all(mount.endswith(":ro") for mount in mounts if "knowledge_db_init.sql" in mount)


def test_the_documented_dsn_points_at_the_corpus_the_script_provisions() -> None:
    """`.env.example` is what a compose operator copies, so it is compose's half.

    The password is checked against ``docker-compose.dev.yml``, which is the
    file that actually creates the role. Documenting one password and creating
    the role with another is a reader that connects and reads nothing.
    """
    dsn = knowledge_dsn(ENV_EXAMPLE.read_text())

    assert dsn.group("user") == script_default("knowledge_read_role")
    assert dsn.group("database") == script_default("knowledge_db")
    assert dsn.group("password") == default_of(DEV_COMPOSE.read_text(), READ_PASSWORD_VAR)
