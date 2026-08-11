"""The fleet's own environment, and the environment it hands an executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import AgentSpec

__all__ = [
    "EXECUTOR_API_KEY_ENV",
    "EXECUTOR_CPUS_ENV",
    "EXECUTOR_MEMORY_ENV",
    "EXECUTOR_PORT",
    "PASSTHROUGH_ENV",
    "REGISTER_API_KEY_ENV",
    "FleetSettings",
    "NetworkAddresses",
    "SettingsError",
    "executor_environment",
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

# Measured on 11 Aug: an idle executor is 37MB resident and peaks at 105MB
# through a turn with a tool call. The runtime default is 1024MB per VM, which
# at eight agents reserves 8GB to run under half a gigabyte of Python.
EXECUTOR_MEMORY_DEFAULT = "512MB"
EXECUTOR_CPUS_DEFAULT = 2

EXECUTOR_PORT = 8000
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
    executor_memory: str
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
            executor_memory=env.get(EXECUTOR_MEMORY_ENV, "").strip()
            or EXECUTOR_MEMORY_DEFAULT,
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


def executor_environment(
    spec: AgentSpec,
    *,
    settings: FleetSettings,
    addresses: NetworkAddresses,
    env: Mapping[str, str],
) -> dict[str, str]:
    """The environment one executor container gets, computed rather than inherited."""

    computed = {
        "AGENT_CONTROL_AGENT_NAME": spec.agent_name,
        "AGENT_CONTROL_URL": f"http://{addresses.server_ip}:{SERVER_PORT}",
        "AGENT_CONTROL_API_KEY": settings.executor_api_key,
        "AGENT_CONTROL_MODEL_BASE_URL": model_base_url(settings, addresses),
        "AGENT_CONTROL_WEB_TOOLS": "1" if spec.web_tools else "0",
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
    """The executor's environment with the admin key, differing in nothing else."""

    return {
        **executor_environment(spec, settings=settings, addresses=addresses, env=env),
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
