"""``fleet.yaml``: which registered agents should have an executor process."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "AgentSpec",
    "FleetConfig",
    "FleetConfigError",
    "SUPPORTED_VERSION",
    "load_fleet_config",
]

SUPPORTED_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"version", "image", "defaults", "agents"})
_DEFAULTS_KEYS = frozenset({"web_tools"})
_AGENT_KEYS = frozenset({"agent_name", "web_tools"})

# Narrower than the server's ``^[a-z0-9:_-]+$`` on purpose: this name is also a
# directory under /agents, and ADK's _validate_agent_name refuses ':' and '-'.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_AGENT_NAME_MIN_LENGTH = 10

_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?$")
_IMAGE_MAX_CHARS = 200

_WEB_TOOLS_DEFAULT = True


class FleetConfigError(RuntimeError):
    """A refused fleet file, named by code so an operator can fix it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One agent that should have a process, and the tools it runs with."""

    agent_name: str
    web_tools: bool

    @property
    def container_name(self) -> str:
        return f"ac-executor-{self.agent_name.replace('_', '-')}"


@dataclass(frozen=True, slots=True)
class FleetConfig:
    """The image every executor runs, and which agents get one."""

    image: str
    agents: tuple[AgentSpec, ...]


def load_fleet_config(path: Path) -> FleetConfig:
    """Parse the fleet file; absent is not a default, it is a refusal."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FleetConfigError("fleet_absent", f"{path} does not exist.") from exc
    except OSError as exc:
        raise FleetConfigError("fleet_unreadable", f"{path} could not be read: {exc}") from exc
    return parse_fleet_config(raw, origin=str(path))


def parse_fleet_config(raw: str, *, origin: str = "<fleet.yaml>") -> FleetConfig:
    """The YAML half of :func:`load_fleet_config`, separated so tests need no file."""

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FleetConfigError("fleet_malformed", f"{origin} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise FleetConfigError(
            "fleet_malformed",
            f"{origin}: the top level must be a mapping, not {_kind(document)}.",
        )
    _refuse_unknown(document, _TOP_LEVEL_KEYS, origin, "the top level")
    _parse_version(document.get("version"), origin)
    image = _parse_image(document.get("image"), origin)
    default_web_tools = _parse_defaults(document.get("defaults"), origin)
    agents = _parse_agents(document.get("agents"), origin, default_web_tools)
    return FleetConfig(image=image, agents=agents)


def _parse_version(raw: object, origin: str) -> None:
    if raw is None:
        raise FleetConfigError(
            "fleet_bad_value",
            f"{origin}: version is required. A file with no version is a file whose "
            "schema this build has to guess at.",
        )
    if not isinstance(raw, int) or isinstance(raw, bool) or raw != SUPPORTED_VERSION:
        raise FleetConfigError(
            "fleet_bad_value",
            f"{origin}: version must be the integer {SUPPORTED_VERSION}, not {raw!r}.",
        )


def _parse_image(raw: object, origin: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise FleetConfigError(
            "fleet_bad_value",
            f"{origin}: image must be a non-empty string naming the executor image.",
        )
    value = raw.strip()
    if len(value) > _IMAGE_MAX_CHARS or not _IMAGE_RE.match(value):
        raise FleetConfigError(
            "fleet_bad_value", f"{origin}: {raw!r} is not a usable image reference."
        )
    return value


def _parse_defaults(raw: object, origin: str) -> bool:
    if raw is None:
        return _WEB_TOOLS_DEFAULT
    if not isinstance(raw, dict):
        raise FleetConfigError(
            "fleet_bad_value", f"{origin}: defaults must be a mapping, not {_kind(raw)}."
        )
    _refuse_unknown(raw, _DEFAULTS_KEYS, origin, "defaults")
    if "web_tools" not in raw:
        return _WEB_TOOLS_DEFAULT
    return _parse_flag(raw["web_tools"], f"{origin}: defaults", "web_tools")


def _parse_agents(raw: object, origin: str, default_web_tools: bool) -> tuple[AgentSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise FleetConfigError(
            "fleet_bad_value", f"{origin}: agents must be a list of entries, not {_kind(raw)}."
        )
    specs: list[AgentSpec] = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(raw):
        where = f"{origin}: agents[{index}]"
        if not isinstance(entry, dict):
            raise FleetConfigError(
                "fleet_bad_value", f"{where} must be a mapping, not {_kind(entry)}."
            )
        _refuse_unknown(entry, _AGENT_KEYS, origin, f"agents[{index}]")
        if "agent_name" not in entry:
            raise FleetConfigError("fleet_bad_value", f"{where} has no 'agent_name' key.")
        agent_name = _parse_agent_name(entry["agent_name"], where)
        first = seen.get(agent_name)
        if first is not None:
            raise FleetConfigError(
                "fleet_duplicate_agent",
                f"{where}: {agent_name} is already listed as {first}. One agent is one "
                "process, so two entries make it ambiguous which one starts.",
            )
        seen[agent_name] = f"agents[{index}]"
        web_tools = (
            default_web_tools
            if "web_tools" not in entry
            else _parse_flag(entry["web_tools"], where, "web_tools")
        )
        specs.append(AgentSpec(agent_name=agent_name, web_tools=web_tools))
    return tuple(specs)


def _parse_agent_name(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise FleetConfigError(
            "fleet_agent_name", f"{where}: agent_name must be a non-empty string."
        )
    value = raw.strip()
    if len(value) < _AGENT_NAME_MIN_LENGTH:
        raise FleetConfigError(
            "fleet_agent_name",
            f"{where}: {value!r} is shorter than the {_AGENT_NAME_MIN_LENGTH} characters "
            "the server requires, so no agent can be registered under it.",
        )
    if not _AGENT_NAME_RE.match(value):
        raise FleetConfigError(
            "fleet_agent_name",
            f"{where}: {value!r} is not lowercase letters, digits and underscores. This "
            "name becomes a directory under /agents and ADK refuses '-' and ':' there, "
            "so a name the server would accept is still unroutable as an app.",
        )
    return value


def _parse_flag(raw: object, where: str, key: str) -> bool:
    if not isinstance(raw, bool):
        raise FleetConfigError(
            "fleet_bad_value",
            f"{where}: {key} must be true or false, not {_kind(raw)}. A string here "
            'would read as true, and web_tools: "false" would give web access to the '
            "one container it was written to take it away from.",
        )
    return raw


def _refuse_unknown(
    mapping: dict[str, Any], known: frozenset[str], origin: str, where: str
) -> None:
    """A typo must fail loudly: ignored, it reads as a setting that is applied."""

    unknown = sorted(key for key in mapping if key not in known)
    if unknown:
        raise FleetConfigError(
            "fleet_unknown_key",
            f"{origin}: {where} has unknown key(s) {unknown!r}; known keys are "
            f"{sorted(known)!r}. Unknown keys are refused rather than ignored, because "
            "a misspelled setting that is ignored is a setting that never applied.",
        )


def _kind(value: object) -> str:
    return type(value).__name__
