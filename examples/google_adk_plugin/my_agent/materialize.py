"""Materialize the agent packages this executor serves, refusing an environment that cannot."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AGENTS_DIR",
    "FLEET_AGENTS_ENV",
    "ExecutorEnvError",
    "Process",
    "materialize",
    "shim_source",
    "validated_agent_name",
    "validated_api_key",
    "validated_processes",
    "validated_session_service_uri",
]

AGENTS_DIR = Path("/agents")

# One ``<agent_name>:<port>`` per process in this container.
FLEET_AGENTS_ENV = "AGENT_CONTROL_FLEET_AGENTS"

# ADK's own rule (AgentLoader._validate_agent_name, google-adk 1.37.0), which is
# also what keeps this variable from reaching the filesystem as a path.
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9_]+$")

_MIN_PORT = 1024
_MAX_PORT = 65535

# The App is rebuilt rather than re-exported: Runner takes app_name from app.name
# when it is handed an App, so the shared App(name="my_agent") would make every
# executor look its sessions up under "my_agent" while the route created them
# under the folder name.
_SHIM = '''"""Generated at container start. One executor, one agent, one package."""

from google.adk.apps import App

from my_agent.agent import plugin, root_agent

app = App(name="{agent_name}", root_agent=root_agent, plugins=[plugin])

__all__ = ["app", "root_agent"]
'''


class ExecutorEnvError(RuntimeError):
    """A refused executor environment, named by code so an operator can fix the container."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Process:
    """One ``adk api_server``: the agent it serves and the port it listens on."""

    agent_name: str
    port: int

    @property
    def agents_root(self) -> str:
        """The directory this process is given, holding its one package and no sibling."""
        return str(AGENTS_DIR / self.agent_name)


def validated_agent_name(raw: str | None) -> str:
    """The agent name, checked against ADK's rule before it is used as a directory name."""
    name = (raw or "").strip()
    if not name:
        raise ExecutorEnvError(
            "agent_name_missing",
            "An entry in AGENT_CONTROL_FLEET_AGENTS names no agent. One process serves "
            "one agent and its name is the directory ADK routes on, so there is nothing "
            "to default to.",
        )
    if not _AGENT_NAME_RE.fullmatch(name):
        raise ExecutorEnvError(
            "agent_name_invalid",
            f"Agent name {name!r} is not ^[a-zA-Z0-9_]+$. ADK refuses the name, "
            "and it is written here as a path.",
        )
    return name


def validated_processes(raw: str | None) -> tuple[Process, ...]:
    """The container's process list, parsed from ``<agent_name>:<port>`` entries."""
    value = (raw or "").strip()
    if not value:
        raise ExecutorEnvError(
            "fleet_agents_missing",
            f"{FLEET_AGENTS_ENV} is unset. It is how this container learns which agents "
            "to run and on which ports, and there is no single-agent default to fall "
            "back to.",
        )
    processes: list[Process] = []
    names: set[str] = set()
    ports: set[int] = set()
    for entry in value.split(","):
        name, separator, port = entry.strip().rpartition(":")
        if not separator:
            raise ExecutorEnvError(
                "fleet_agents_malformed",
                f"{FLEET_AGENTS_ENV} entry {entry!r} is not <agent_name>:<port>.",
            )
        process = Process(agent_name=validated_agent_name(name), port=_validated_port(port))
        if process.agent_name in names:
            raise ExecutorEnvError(
                "fleet_agents_duplicate",
                f"{FLEET_AGENTS_ENV} lists {process.agent_name} twice. Two processes under "
                "one name would materialize into one directory and race for it.",
            )
        if process.port in ports:
            raise ExecutorEnvError(
                "fleet_agents_duplicate",
                f"{FLEET_AGENTS_ENV} lists port {process.port} twice. These processes share "
                "a network namespace, so the second would fail to bind.",
            )
        names.add(process.agent_name)
        ports.add(process.port)
        processes.append(process)
    return tuple(processes)


def validated_api_key(raw: str | None) -> str:
    """The executor credential, required because nudges and halts have no other source."""
    key = (raw or "").strip()
    if not key:
        raise ExecutorEnvError(
            "executor_api_key_missing",
            "AGENT_CONTROL_API_KEY is unset. nudges/claim fires at every model boundary and "
            "halts/claim at every tool boundary; with no credential they answer 403 once and "
            "back off for 300 seconds, so the operator STOP button is gone while the console "
            "still shows the halt recorded.",
        )
    return key


def validated_session_service_uri(raw: str | None) -> str | None:
    """The ADK session backend, refused when a Postgres URI names no driver."""
    uri = (raw or "").strip()
    if not uri:
        return None
    scheme = uri.split("://", 1)[0]
    if scheme in {"postgres", "postgresql"}:
        raise ExecutorEnvError(
            "session_uri_driver",
            f"ADK_SESSION_SERVICE_URI is {scheme}://, which SQLAlchemy resolves to psycopg2. "
            "That is not installed and ADK fails at startup. Use postgresql+asyncpg://.",
        )
    return uri


def shim_source(agent_name: str) -> str:
    """The generated agent module: it imports the shared one and names the App for this executor."""
    return _SHIM.format(agent_name=agent_name)


def materialize(agents_dir: Path, agent_name: str) -> Path:
    """Write one package under a root of its own, because list_agents() enumerates directories."""
    module = agents_dir / agent_name / agent_name / "agent.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(shim_source(agent_name), encoding="utf-8")
    return module


def _validated_port(raw: str) -> int:
    try:
        port = int(raw.strip())
    except ValueError:
        raise ExecutorEnvError(
            "fleet_agents_malformed", f"{FLEET_AGENTS_ENV} port {raw!r} is not an integer."
        ) from None
    if not _MIN_PORT <= port <= _MAX_PORT:
        raise ExecutorEnvError(
            "fleet_agents_malformed",
            f"{FLEET_AGENTS_ENV} port {port} is outside {_MIN_PORT}-{_MAX_PORT}.",
        )
    return port


def main() -> int:
    """Validate the container's environment, then write the packages the servers will load."""
    try:
        processes = validated_processes(os.getenv(FLEET_AGENTS_ENV))
        validated_api_key(os.getenv("AGENT_CONTROL_API_KEY"))
        validated_session_service_uri(os.getenv("ADK_SESSION_SERVICE_URI"))
    except ExecutorEnvError as exc:
        print(f"executor refused to start [{exc.code}]: {exc}", file=sys.stderr)
        return 1

    for process in processes:
        module = materialize(AGENTS_DIR, process.agent_name)
        print(f"Materialized {module} for port {process.port}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
