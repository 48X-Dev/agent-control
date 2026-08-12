"""The fleet's own environment, and the environment it hands an executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentSpec, GroupSpec

__all__ = [
    "EXECUTOR_API_KEY_ENV",
    "EXECUTOR_CPUS_ENV",
    "EXECUTOR_MEMORY_ENV",
    "FLEET_AGENTS_ENV",
    "PASSTHROUGH_ENV",
    "REGISTER_API_KEY_ENV",
    "FleetSettings",
    "NetworkAddresses",
    "SettingsError",
    "default_executor_memory",
    "group_environment",
    "model_base_url",
    "register_environment",
]

CONFIG_PATH_ENV = "AGENT_CONTROL_FLEET_CONFIG_PATH"
REGISTER_API_KEY_ENV = "AGENT_CONTROL_FLEET_REGISTER_API_KEY"
EXECUTOR_API_KEY_ENV = "AGENT_CONTROL_FLEET_EXECUTOR_API_KEY"
MODEL_BASE_URL_ENV = "AGENT_CONTROL_FLEET_MODEL_BASE_URL"
SERVER_URL_ENV = "AGENT_CONTROL_FLEET_SERVER_URL"
EXECUTOR_MEMORY_ENV = "AGENT_CONTROL_FLEET_EXECUTOR_MEMORY"
EXECUTOR_CPUS_ENV = "AGENT_CONTROL_FLEET_EXECUTOR_CPUS"

CONFIG_PATH_DEFAULT = Path("fleet.yaml")
SERVER_URL_DEFAULT = "http://localhost:8000"

# Measured 12 Aug in a two-agent group: 401MB resident idle and 587MB peak
# through one turn that ran a knowledge search and a web search. The flat 512MB
# this used to default to sat under that peak, and the cost is not a slow
# container: the OOM killer takes one process, and the entrypoint deliberately
# takes the whole group down when any process exits. A default has to scale
# with the group, because a group is now one container running N of these.
#
# The 11 Aug figures this replaces (37MB idle, 105MB peak) were one process
# with no knowledge tools, which is no longer the shape being sized.
EXECUTOR_MEMORY_BASE_MB = 512
EXECUTOR_MEMORY_PER_AGENT_MB = 384
EXECUTOR_MEMORY_FLOOR_MB = 1024
EXECUTOR_CPUS_DEFAULT = 2


def default_executor_memory(agent_count: int) -> str:
    """What a group of ``agent_count`` agents gets when the operator sets nothing."""

    scaled = EXECUTOR_MEMORY_BASE_MB + EXECUTOR_MEMORY_PER_AGENT_MB * agent_count
    return f"{max(EXECUTOR_MEMORY_FLOOR_MB, scaled)}MB"


# The container's own list of processes: one ``<agent_name>:<port>`` per agent,
# because the entrypoint starts one ``adk api_server`` per entry.
FLEET_AGENTS_ENV = "AGENT_CONTROL_FLEET_AGENTS"

SERVER_PORT = 8000
POSTGRES_PORT = 5432

# The OpenAI-compatible proxy, loopback-bound on the host. Reached through the
# gateway because that is the VM-to-host path and not a LAN path.
MODEL_PROXY_PORT = 10531
MODEL_PROXY_PATH = "/v1"

HEALTH_TIMEOUT_SECONDS = 120.0
READY_TIMEOUT_SECONDS = 180.0

# Forwarded verbatim when set. The example reads every one of these, and a
# container that gets some of them has features that read as available and are
# off. Anything the fleet computes is deliberately absent from this list.
PASSTHROUGH_ENV = (
    "AGENT_CONTROL_DEFAULT_MODEL",
    "AGENT_CONTROL_KNOWLEDGE_TOOLS",
    "AGENT_CONTROL_TRACKER_TOOLS",
    "AGENT_MODEL",
    "EXA_API_KEY",
    "EXA_MCP_URL",
    "EXA_TOOL_ALLOWLIST",
    "GOOGLE_API_KEY",
    "GOOGLE_MODEL",
    "OPENAI_API_KEY",
)


class SettingsError(RuntimeError):
    """A missing or unusable fleet setting, named by code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FleetSettings:
    """What the fleet reads from its own environment, validated once."""

    config_path: Path
    server_url: str
    register_api_key: str
    executor_api_key: str
    model_base_url_override: str | None
    adk_db_password: str
    executor_memory: str | None
    executor_cpus: int

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, require_credentials: bool) -> FleetSettings:
        return cls(
            config_path=Path(env.get(CONFIG_PATH_ENV, "").strip() or CONFIG_PATH_DEFAULT),
            server_url=(env.get(SERVER_URL_ENV, "").strip() or SERVER_URL_DEFAULT).rstrip("/"),
            register_api_key=_api_key(env, REGISTER_API_KEY_ENV, require_credentials),
            executor_api_key=_api_key(env, EXECUTOR_API_KEY_ENV, require_credentials),
            model_base_url_override=_model_base_url_override(env),
            adk_db_password=env.get("ADK_DB_PASSWORD", "").strip() or "adk_local",
            # None means "scale it to the group"; only an operator override is a value here.
            executor_memory=env.get(EXECUTOR_MEMORY_ENV, "").strip() or None,
            executor_cpus=_positive_int(env, EXECUTOR_CPUS_ENV, EXECUTOR_CPUS_DEFAULT),
        )


@dataclass(frozen=True, slots=True)
class NetworkAddresses:
    """Addresses read back from the live network, none of which are configurable."""

    server_ip: str
    postgres_ip: str
    gateway: str


def model_base_url(settings: FleetSettings, addresses: NetworkAddresses) -> str:
    """The gateway is the host here; ``host.docker.internal`` resolves to nothing."""

    if settings.model_base_url_override is not None:
        return settings.model_base_url_override
    return f"http://{addresses.gateway}:{MODEL_PROXY_PORT}{MODEL_PROXY_PATH}"


def group_environment(
    group: GroupSpec,
    *,
    settings: FleetSettings,
    addresses: NetworkAddresses,
    env: Mapping[str, str],
) -> dict[str, str]:
    """The environment one container gets, computed rather than inherited.

    One variable covers every process in it. ``web_tools`` is the group's
    because validation refuses a group whose members disagree.
    """

    computed = {
        FLEET_AGENTS_ENV: ",".join(f"{spec.agent_name}:{spec.port}" for spec in group.agents),
        "AGENT_CONTROL_URL": f"http://{addresses.server_ip}:{SERVER_PORT}",
        "AGENT_CONTROL_API_KEY": settings.executor_api_key,
        "AGENT_CONTROL_MODEL_BASE_URL": model_base_url(settings, addresses),
        "AGENT_CONTROL_WEB_TOOLS": "1" if group.web_tools else "0",
        # ADK's --session_service_uri has no envvar binding, so the entrypoint
        # reads this and passes the flag. Explicit driver: the bare form fails
        # at import with "no psycopg2".
        "ADK_SESSION_SERVICE_URI": (
            f"postgresql+asyncpg://adk:{settings.adk_db_password}"
            f"@{addresses.postgres_ip}:{POSTGRES_PORT}/adk_runtime"
        ),
    }
    passthrough = {
        name: env[name].strip() for name in PASSTHROUGH_ENV if env.get(name, "").strip()
    }
    return {**passthrough, **computed}


def register_environment(
    spec: AgentSpec,
    *,
    settings: FleetSettings,
    addresses: NetworkAddresses,
    env: Mapping[str, str],
) -> dict[str, str]:
    """One agent's container environment with the admin key, differing in nothing else.

    Registration is one agent per container whatever the grouping is, because a
    second import in one process registers the first agent again.
    """

    group = GroupSpec(name=spec.agent_name, agents=(spec,))
    return {
        **group_environment(group, settings=settings, addresses=addresses, env=env),
        "AGENT_CONTROL_API_KEY": settings.register_api_key,
    }


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError("setting_invalid", f"{name} must be an integer, got {raw!r}.") from None
    if value < 1:
        raise SettingsError("setting_invalid", f"{name} must be at least 1, got {value}.")
    return value


def _api_key(env: Mapping[str, str], name: str, required: bool) -> str:
    value = env.get(name, "").strip()
    if not value:
        if required:
            raise SettingsError("setting_missing", f"{name} is unset or empty.")
        return ""
    if "," in value:
        raise SettingsError(
            "setting_bad_value",
            f"{name} contains a comma. The server's setting is "
            "AGENT_CONTROL_ADMIN_API_KEYS, plural and comma-separated; a whole list "
            "pasted into this slot produces a 401 that reads as a server fault.",
        )
    return value


def _model_base_url_override(env: Mapping[str, str]) -> str | None:
    value = env.get(MODEL_BASE_URL_ENV, "").strip()
    if not value:
        return None
    host = urlsplit(value).hostname or ""
    if host in {"127.0.0.1", "localhost", "::1", "host.docker.internal"}:
        raise SettingsError(
            "setting_bad_value",
            f"{MODEL_BASE_URL_ENV} is {value!r}. From inside these VMs that address is "
            "the container itself, not the host, and host.docker.internal resolves to "
            "nothing here. Leave it unset and the gateway is computed at up time.",
        )
    return value.rstrip("/")
