"""``fleet.yaml``: which agents get a process, and which processes share a container."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "AgentSpec",
    "FIRST_EXECUTOR_PORT",
    "FleetConfig",
    "FleetConfigError",
    "GroupSpec",
    "Placement",
    "SUPPORTED_VERSION",
    "load_fleet_config",
]

SUPPORTED_VERSION = 1

_TOP_LEVEL_KEYS = frozenset({"version", "image", "defaults", "groups"})
_DEFAULTS_KEYS = frozenset({"web_tools"})
_GROUP_KEYS = frozenset({"agents", "name"})
_AGENT_KEYS = frozenset({"agent_name", "web_tools"})

# One process per agent means one port per agent, allocated from here inside the
# container and published nowhere. The only rule is that a group's own processes
# do not collide, so every group counts up from the same number.
FIRST_EXECUTOR_PORT = 8000

# Narrower than the server's ``^[a-z0-9:_-]+$`` on purpose: this name is also a
# directory under /agents, and ADK's _validate_agent_name refuses ':' and '-'.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_AGENT_NAME_MIN_LENGTH = 10

# The group name becomes a container name, where hyphens are the convention and
# the underscores an agent name may carry are translated.
_GROUP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_GROUP_NAME_MAX_LENGTH = 40

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
    """One agent that should have a process, the tools it runs with and its port."""

    agent_name: str
    web_tools: bool
    port: int


@dataclass(frozen=True, slots=True)
class GroupSpec:
    """One container, and the one process per agent it runs."""

    name: str
    agents: tuple[AgentSpec, ...]

    @property
    def container_name(self) -> str:
        return f"ac-executor-{self.name.replace('_', '-')}"

    @property
    def web_tools(self) -> bool:
        """Group-wide because validation refuses members that disagree."""

        return self.agents[0].web_tools


@dataclass(frozen=True, slots=True)
class Placement:
    """One agent process, and the container it shares with the rest of its group."""

    group: GroupSpec
    agent: AgentSpec

    def base_url(self, address: str) -> str:
        """Section 3.3: the port is per-agent where the address is per-group."""

        return f"http://{address}:{self.agent.port}"


@dataclass(frozen=True, slots=True)
class FleetConfig:
    """The image every executor runs, and which agents share which container."""

    image: str
    groups: tuple[GroupSpec, ...]

    @property
    def agents(self) -> tuple[AgentSpec, ...]:
        return tuple(agent for group in self.groups for agent in group.agents)

    @property
    def placements(self) -> tuple[Placement, ...]:
        return tuple(
            Placement(group=group, agent=agent)
            for group in self.groups
            for agent in group.agents
        )


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
    groups = _parse_groups(document.get("groups"), origin, default_web_tools)
    return FleetConfig(image=image, groups=groups)


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


def _parse_groups(
    raw: object, origin: str, default_web_tools: bool
) -> tuple[GroupSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise FleetConfigError(
            "fleet_bad_value", f"{origin}: groups must be a list of entries, not {_kind(raw)}."
        )
    groups: list[GroupSpec] = []
    seen_groups: dict[str, str] = {}
    seen_agents: dict[str, str] = {}
    for index, entry in enumerate(raw):
        where = f"{origin}: groups[{index}]"
        if not isinstance(entry, dict):
            raise FleetConfigError(
                "fleet_bad_value", f"{where} must be a mapping, not {_kind(entry)}."
            )
        _refuse_unknown(entry, _GROUP_KEYS, origin, f"groups[{index}]")
        name = _parse_group_name(entry.get("name"), where)
        first = seen_groups.get(name)
        if first is not None:
            raise FleetConfigError(
                "fleet_duplicate_group",
                f"{where}: {name} is already declared as {first}. Two entries with one "
                "name are two containers with one name, so only the second would run.",
            )
        seen_groups[name] = f"groups[{index}]"
        agents = _parse_agents(entry.get("agents"), where, default_web_tools, seen_agents)
        group = GroupSpec(name=name, agents=agents)
        _refuse_mixed_egress(group, where)
        groups.append(group)
    return tuple(groups)


def _parse_agents(
    raw: object, where: str, default_web_tools: bool, seen: dict[str, str]
) -> tuple[AgentSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise FleetConfigError(
            "fleet_bad_value",
            f"{where}: agents must be a non-empty list of entries, not {_kind(raw)}. A "
            "group with no agent is a container with nothing to run.",
        )
    specs: list[AgentSpec] = []
    for index, entry in enumerate(raw):
        entry_where = f"{where}.agents[{index}]"
        if not isinstance(entry, dict):
            raise FleetConfigError(
                "fleet_bad_value", f"{entry_where} must be a mapping, not {_kind(entry)}."
            )
        _refuse_unknown(entry, _AGENT_KEYS, where, f"agents[{index}]")
        if "agent_name" not in entry:
            raise FleetConfigError("fleet_bad_value", f"{entry_where} has no 'agent_name' key.")
        agent_name = _parse_agent_name(entry["agent_name"], entry_where)
        first = seen.get(agent_name)
        if first is not None:
            raise FleetConfigError(
                "fleet_duplicate_agent",
                f"{entry_where}: {agent_name} is already listed as {first}. One agent is "
                "one process, so two entries make it ambiguous which one starts.",
            )
        seen[agent_name] = entry_where
        web_tools = (
            default_web_tools
            if "web_tools" not in entry
            else _parse_flag(entry["web_tools"], entry_where, "web_tools")
        )
        specs.append(
            AgentSpec(
                agent_name=agent_name,
                web_tools=web_tools,
                port=FIRST_EXECUTOR_PORT + index,
            )
        )
    return tuple(specs)


def _refuse_mixed_egress(group: GroupSpec, where: str) -> None:
    """Section 3.3: a group is a trust boundary, so egress cannot differ inside one."""

    enabled = sorted(spec.agent_name for spec in group.agents if spec.web_tools)
    disabled = sorted(spec.agent_name for spec in group.agents if not spec.web_tools)
    if not enabled or not disabled:
        return
    raise FleetConfigError(
        "fleet_group_mixed_egress",
        f"{where}: group {group.name!r} has web_tools on for {enabled!r} and off for "
        f"{disabled!r}. Members of a group share a network and a PID namespace, so an "
        "injected sibling reaches the network the web-enabled process reaches and can "
        "read its environment; the difference the file declares is not one the runtime "
        "enforces. Put them in two groups instead.",
    )


def _parse_group_name(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise FleetConfigError(
            "fleet_group_name",
            f"{where}: name must be a non-empty string. It becomes a container name, "
            "which is what an operator reads in `container ls` and stops by hand.",
        )
    value = raw.strip()
    if len(value) > _GROUP_NAME_MAX_LENGTH or not _GROUP_NAME_RE.match(value):
        raise FleetConfigError(
            "fleet_group_name",
            f"{where}: {value!r} is not up to {_GROUP_NAME_MAX_LENGTH} characters of "
            "lowercase letters, digits, hyphens and underscores.",
        )
    return value


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
