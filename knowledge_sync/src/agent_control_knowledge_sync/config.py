"""The sync's environment, read once into a frozen config that names what is missing."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .drive_auth import DriveCredentials

CLIENT_ID_ENV = "AGENT_KNOWLEDGE_DRIVE_CLIENT_ID"
CLIENT_SECRET_ENV = "AGENT_KNOWLEDGE_DRIVE_CLIENT_SECRET"
REFRESH_TOKEN_ENV = "AGENT_KNOWLEDGE_DRIVE_REFRESH_TOKEN"
ROOT_FOLDER_ENV = "AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID"
DATABASE_URL_ENV = "AGENT_KNOWLEDGE_DB_URL"
MAX_FILE_BYTES_ENV = "AGENT_KNOWLEDGE_FILE_MAX_BYTES"
MAX_DOCUMENTS_ENV = "AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_RUN"
REQUEST_TIMEOUT_ENV = "AGENT_KNOWLEDGE_REQUEST_TIMEOUT_SECONDS"
EXECUTOR_DRIVE_ROOT_ENV = "AGENT_CONTROL_EXECUTOR_DRIVE_ROOT_ID"
GITHUB_TOKEN_ENV = "AGENT_KNOWLEDGE_GITHUB_TOKEN"
ALLOWLIST_PATH_ENV = "AGENT_KNOWLEDGE_ALLOWLIST_PATH"
SOURCE_MAX_BYTES_ENV = "AGENT_KNOWLEDGE_SOURCE_MAX_BYTES"
RUN_MAX_FETCH_BYTES_ENV = "AGENT_KNOWLEDGE_RUN_MAX_FETCH_BYTES"
TOMBSTONE_RETENTION_ENV = "AGENT_KNOWLEDGE_TOMBSTONE_RETENTION_DAYS"
SYNC_INTERVAL_ENV = "AGENT_KNOWLEDGE_SYNC_INTERVAL_SECONDS"

# Plan 5.4 and section 12: matching `attachment_max_bytes`'s reasoning.
MAX_FILE_BYTES_DEFAULT = 20_971_520

# Plan 5.4's two remaining ceilings: 2GB per source, 4GB per process.
SOURCE_MAX_BYTES_DEFAULT = 2_147_483_648
RUN_MAX_FETCH_BYTES_DEFAULT = 4_294_967_296

# Plan 4.4's retention window, and 10's cadence.
TOMBSTONE_RETENTION_DAYS_DEFAULT = 180
SYNC_INTERVAL_SECONDS_DEFAULT = 900

ALLOWLIST_PATH_DEFAULT = Path("knowledge.yaml")

GUARD_DISABLED_LINE = "agent-output ingest guard disabled: executor Drive root id not configured"

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """A missing or unusable environment value, named so an operator can fix it."""


def _value(env: Mapping[str, str], name: str) -> str:
    return env.get(name, "").strip()


def _required(env: Mapping[str, str], name: str, purpose: str) -> str:
    found = _value(env, name)
    if not found:
        raise ConfigError(f"{name} is unset or empty; the sync needs it for {purpose}.")
    return found


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _value(env, name)
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, not {raw!r}.") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero, not {parsed}.")
    return parsed


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _value(env, name)
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number of seconds, not {raw!r}.") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than zero, not {parsed}.")
    return parsed


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """Everything one sync run needs: a credential, a root, a database and its ceilings."""

    credentials: DriveCredentials
    root_folder_id: str
    database_url: str
    max_file_bytes: int = MAX_FILE_BYTES_DEFAULT
    max_documents_per_run: int = 10_000
    source_max_bytes: int = SOURCE_MAX_BYTES_DEFAULT
    run_max_fetch_bytes: int = RUN_MAX_FETCH_BYTES_DEFAULT
    tombstone_retention_days: int = TOMBSTONE_RETENTION_DAYS_DEFAULT
    sync_interval_seconds: int = SYNC_INTERVAL_SECONDS_DEFAULT
    """Section 10's cadence, read by ``serve``; ``once`` makes one pass and stops."""
    request_timeout_seconds: float = 120.0
    executor_drive_root_id: str | None = None
    """Section 11's ingest guard. Unset disables the guard, loudly."""
    github_token: str | None = None
    """Unset leaves the GitHub channel off; Drive syncs exactly as it did."""
    allowlist_path: Path = ALLOWLIST_PATH_DEFAULT
    """Section 6: under a classic PAT this file is the only thing enforcing scope."""

    def __post_init__(self) -> None:
        """Ceilings that are zero or negative would disable themselves silently."""
        if not self.root_folder_id:
            raise ConfigError("root_folder_id is empty; the corpus root has no id.")
        if not self.database_url:
            raise ConfigError("database_url is empty; the sync has nowhere to write.")
        for name in (
            "max_file_bytes",
            "max_documents_per_run",
            "source_max_bytes",
            "run_max_fetch_bytes",
            "tombstone_retention_days",
            "sync_interval_seconds",
        ):
            value = getattr(self, name)
            if value <= 0:
                raise ConfigError(f"{name} must be greater than zero, not {value}.")
        if self.request_timeout_seconds <= 0:
            raise ConfigError(
                f"request_timeout_seconds must be greater than zero, "
                f"not {self.request_timeout_seconds}."
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SyncConfig:
        """Builds the config, refusing with the variable's name rather than a KeyError."""
        credentials = DriveCredentials(
            client_id=_required(env, CLIENT_ID_ENV, "the Drive OAuth client"),
            client_secret=_required(env, CLIENT_SECRET_ENV, "the Drive OAuth client"),
            refresh_token=_required(env, REFRESH_TOKEN_ENV, "minting access tokens"),
        )
        executor_root = _value(env, EXECUTOR_DRIVE_ROOT_ENV)
        _announce_guard(executor_root)
        allowlist = _value(env, ALLOWLIST_PATH_ENV)
        return cls(
            credentials=credentials,
            root_folder_id=_required(env, ROOT_FOLDER_ENV, "the corpus root folder"),
            database_url=_required(env, DATABASE_URL_ENV, "writing the corpus"),
            max_file_bytes=_int(env, MAX_FILE_BYTES_ENV, MAX_FILE_BYTES_DEFAULT),
            max_documents_per_run=_int(env, MAX_DOCUMENTS_ENV, 10_000),
            source_max_bytes=_int(env, SOURCE_MAX_BYTES_ENV, SOURCE_MAX_BYTES_DEFAULT),
            run_max_fetch_bytes=_int(
                env, RUN_MAX_FETCH_BYTES_ENV, RUN_MAX_FETCH_BYTES_DEFAULT
            ),
            tombstone_retention_days=_int(
                env, TOMBSTONE_RETENTION_ENV, TOMBSTONE_RETENTION_DAYS_DEFAULT
            ),
            sync_interval_seconds=_int(env, SYNC_INTERVAL_ENV, SYNC_INTERVAL_SECONDS_DEFAULT),
            request_timeout_seconds=_float(env, REQUEST_TIMEOUT_ENV, 120.0),
            executor_drive_root_id=executor_root or None,
            github_token=_value(env, GITHUB_TOKEN_ENV) or None,
            allowlist_path=Path(allowlist) if allowlist else ALLOWLIST_PATH_DEFAULT,
        )


def _announce_guard(executor_root: str) -> None:
    """Section 12: a half-on state names itself at startup or nobody learns it is half on."""
    if executor_root:
        logger.info("agent-output ingest guard enabled: executor Drive root %s", executor_root)
    else:
        logger.warning(GUARD_DISABLED_LINE)
