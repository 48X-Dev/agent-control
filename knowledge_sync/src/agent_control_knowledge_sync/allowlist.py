"""The repo allowlist, which under a classic PAT is the only thing enforcing scope.

Every ambiguity refuses: unknown keys, wildcards, anything not an explicit ``owner/name``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "REFUSED_PATH_SEGMENTS",
    "AllowlistError",
    "RepoConfig",
    "RepoRef",
    "load_allowlist",
    "parse_repo",
]

# Refused whatever admits them, so an include_paths entry naming one fails at
# load time rather than being silently dropped during the walk.
REFUSED_PATH_SEGMENTS = frozenset(
    {"vendor", "node_modules", "third_party", "dist", "build", ".git"}
)

_TOP_LEVEL_KEYS = frozenset({"github"})
_GITHUB_KEYS = frozenset({"repos"})
_REPO_KEYS = frozenset({"repo", "include_paths", "github_issues_enabled"})

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_GLOB_CHARS = frozenset("*?[]{}!")

_MAX_INCLUDE_PATH_CHARS = 200


class AllowlistError(RuntimeError):
    """A refused allowlist, named by code so an operator can fix the file."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RepoRef:
    """One repository, always spelled out; there is no wildcard form of this."""

    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class RepoConfig:
    """One allowlist entry: a repo, what it widens, and whether Phase 6 reads it."""

    repo: RepoRef
    include_paths: tuple[str, ...] = ()
    github_issues_enabled: bool = False


def load_allowlist(path: Path) -> tuple[RepoConfig, ...]:
    """Parse the allowlist, refusing anything ambiguous; absent or empty means none."""

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise AllowlistError("allowlist_unreadable", f"{path} could not be read: {exc}") from exc
    return parse_allowlist(raw, origin=str(path))


def parse_allowlist(raw: str, *, origin: str = "<allowlist>") -> tuple[RepoConfig, ...]:
    """The YAML half of :func:`load_allowlist`, separated so tests need no file."""

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AllowlistError("allowlist_malformed", f"{origin} is not valid YAML: {exc}") from exc
    if document is None:
        return ()
    section = _github_section(document, origin)
    entries = section.get("repos")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise AllowlistError(
            "allowlist_bad_value",
            f"{origin}: github.repos must be a list of entries, not {_kind(entries)}.",
        )
    return _parse_entries(entries, origin)


def parse_repo(raw: object, *, origin: str = "<allowlist>") -> RepoRef:
    """``owner/name`` and nothing else: no globs, no bare owner, no org-wide form."""

    if not isinstance(raw, str) or not raw.strip():
        raise AllowlistError(
            "allowlist_repo_form", f"{origin}: a repo must be a non-empty 'owner/name' string."
        )
    value = raw.strip()
    if _GLOB_CHARS.intersection(value):
        raise AllowlistError(
            "allowlist_repo_form",
            f"{origin}: {value!r} contains a wildcard. This allowlist is the only thing "
            "keeping the sync inside its scope, so every repository is named in full.",
        )
    owner, separator, name = value.partition("/")
    if not separator or not name:
        raise AllowlistError(
            "allowlist_repo_form",
            f"{origin}: {value!r} names no repository. An owner on its own would mean "
            "every repository that owner has, which this never does.",
        )
    if "/" in name:
        raise AllowlistError(
            "allowlist_repo_form", f"{origin}: {value!r} has more than one '/'."
        )
    if not _OWNER_RE.match(owner):
        raise AllowlistError("allowlist_repo_form", f"{origin}: {owner!r} is not a GitHub owner.")
    if not _NAME_RE.match(name) or name in {".", ".."}:
        raise AllowlistError(
            "allowlist_repo_form", f"{origin}: {name!r} is not a GitHub repository name."
        )
    return RepoRef(owner=owner, name=name)


def _github_section(document: object, origin: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise AllowlistError(
            "allowlist_malformed",
            f"{origin}: the top level must be a mapping, not {_kind(document)}.",
        )
    _refuse_unknown(document, _TOP_LEVEL_KEYS, origin, "the top level")
    section = document.get("github")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise AllowlistError(
            "allowlist_bad_value", f"{origin}: github must be a mapping, not {_kind(section)}."
        )
    _refuse_unknown(section, _GITHUB_KEYS, origin, "github")
    return section


def _parse_entries(entries: list[Any], origin: str) -> tuple[RepoConfig, ...]:
    configs: list[RepoConfig] = []
    seen: dict[str, str] = {}
    for index, entry in enumerate(entries):
        where = f"{origin}: github.repos[{index}]"
        if not isinstance(entry, dict):
            raise AllowlistError(
                "allowlist_bad_value", f"{where} must be a mapping, not {_kind(entry)}."
            )
        _refuse_unknown(entry, _REPO_KEYS, origin, f"github.repos[{index}]")
        if "repo" not in entry:
            raise AllowlistError("allowlist_bad_value", f"{where} has no 'repo' key.")
        repo = parse_repo(entry["repo"], origin=where)
        first = seen.get(repo.full_name.lower())
        if first is not None:
            raise AllowlistError(
                "allowlist_duplicate_repo",
                f"{where}: {repo.full_name} is already listed as {first}. Two entries for "
                "one repository make it ambiguous which filters apply.",
            )
        seen[repo.full_name.lower()] = repo.full_name
        configs.append(
            RepoConfig(
                repo=repo,
                include_paths=_parse_include_paths(entry.get("include_paths"), where),
                github_issues_enabled=_parse_flag(
                    entry.get("github_issues_enabled"), where, "github_issues_enabled"
                ),
            )
        )
    return tuple(configs)


def _parse_include_paths(raw: object, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AllowlistError(
            "allowlist_bad_value", f"{where}: include_paths must be a list, not {_kind(raw)}."
        )
    return tuple(_parse_include_path(entry, where) for entry in raw)


def _parse_include_path(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise AllowlistError(
            "allowlist_include_path", f"{where}: an include_paths entry must be a non-empty string."
        )
    value = raw.strip().strip("/")
    if not value or len(value) > _MAX_INCLUDE_PATH_CHARS:
        raise AllowlistError(
            "allowlist_include_path", f"{where}: {raw!r} is not a usable path prefix."
        )
    if _GLOB_CHARS.intersection(value) or "\\" in value:
        raise AllowlistError(
            "allowlist_include_path",
            f"{where}: {raw!r} contains a wildcard. include_paths are literal prefixes, "
            "because a glob is how a widening becomes larger than its reviewer thought.",
        )
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise AllowlistError(
            "allowlist_include_path", f"{where}: {raw!r} has an empty or relative segment."
        )
    refused = REFUSED_PATH_SEGMENTS.intersection(segments)
    if refused:
        raise AllowlistError(
            "allowlist_include_path",
            f"{where}: {raw!r} names {sorted(refused)!r}, which is refused whatever admits "
            "it. Admitting it here would be a filter that reads as enabled and is not.",
        )
    return value


def _parse_flag(raw: object, where: str, key: str) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise AllowlistError(
            "allowlist_bad_value",
            f"{where}: {key} must be true or false, not {_kind(raw)}. A string here would "
            "read as true and turn a channel on by accident.",
        )
    return raw


def _refuse_unknown(
    mapping: dict[str, Any], known: frozenset[str], origin: str, where: str
) -> None:
    """A typo must fail loudly: silently ignored, it reads as a filter that is on."""

    unknown = sorted(key for key in mapping if key not in known)
    if unknown:
        raise AllowlistError(
            "allowlist_unknown_key",
            f"{origin}: {where} has unknown key(s) {unknown!r}; known keys are "
            f"{sorted(known)!r}. Unknown keys are refused rather than ignored, because a "
            "misspelled filter that is ignored is a filter that is off.",
        )


def _kind(value: object) -> str:
    return type(value).__name__
