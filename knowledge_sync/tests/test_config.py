"""The config refuses by name, because an unset variable presents as an empty corpus."""

from __future__ import annotations

import logging

import pytest
from agent_control_knowledge_sync.config import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    DATABASE_URL_ENV,
    EXECUTOR_DRIVE_ROOT_ENV,
    GUARD_DISABLED_LINE,
    MAX_DOCUMENTS_ENV,
    MAX_FILE_BYTES_ENV,
    REFRESH_TOKEN_ENV,
    REQUEST_TIMEOUT_ENV,
    ROOT_FOLDER_ENV,
    ConfigError,
    SyncConfig,
)
from agent_control_knowledge_sync.drive_auth import DriveCredentials

FULL_ENV = {
    CLIENT_ID_ENV: "123456789012-abcdefg.apps.googleusercontent.com",
    CLIENT_SECRET_ENV: "GOCSPX-not-a-real-secret",
    REFRESH_TOKEN_ENV: "1//0e-not-a-real-refresh-token",
    ROOT_FOLDER_ENV: "0ABCDEF_shared_drive_folder",
    DATABASE_URL_ENV: "postgresql+psycopg://knowledge_sync@postgres:5432/agent_knowledge",
}


def test_a_full_environment_builds_the_config() -> None:
    config = SyncConfig.from_env(FULL_ENV)
    assert config.root_folder_id == "0ABCDEF_shared_drive_folder"
    assert config.database_url.endswith("/agent_knowledge")
    assert config.credentials == DriveCredentials(
        client_id=FULL_ENV[CLIENT_ID_ENV],
        client_secret=FULL_ENV[CLIENT_SECRET_ENV],
        refresh_token=FULL_ENV[REFRESH_TOKEN_ENV],
    )


def test_the_defaults_are_the_documented_ceilings() -> None:
    """Compose and the Apple script both pass empty, so the code default is what runs.

    Plan 5.4 and section 12 both say 20,971,520, matching `attachment_max_bytes`'s
    reasoning. Anything larger is hostile bytes reaching MarkItDown that nothing
    sanctioned.
    """
    config = SyncConfig.from_env(FULL_ENV)
    assert config.max_file_bytes == 20_971_520
    assert config.max_documents_per_run == 10_000
    assert config.request_timeout_seconds == 120.0


def test_an_unset_executor_root_names_itself_at_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Section 11 makes this line normative; a guard nobody knows is off is worse than none."""
    with caplog.at_level(logging.WARNING):
        config = SyncConfig.from_env(FULL_ENV)

    assert config.executor_drive_root_id is None
    assert GUARD_DISABLED_LINE in caplog.text


def test_the_executor_root_is_read_and_announced(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        config = SyncConfig.from_env(dict(FULL_ENV, **{EXECUTOR_DRIVE_ROOT_ENV: "exec-root-1"}))

    assert config.executor_drive_root_id == "exec-root-1"
    assert "agent-output ingest guard enabled" in caplog.text
    assert "exec-root-1" in caplog.text


def test_a_blank_executor_root_is_unset_rather_than_empty() -> None:
    """An empty compose passthrough is the shape this actually arrives in."""
    config = SyncConfig.from_env(dict(FULL_ENV, **{EXECUTOR_DRIVE_ROOT_ENV: "  "}))

    assert config.executor_drive_root_id is None


@pytest.mark.parametrize(
    "missing",
    [CLIENT_ID_ENV, CLIENT_SECRET_ENV, REFRESH_TOKEN_ENV, ROOT_FOLDER_ENV, DATABASE_URL_ENV],
)
def test_a_missing_variable_is_refused_by_name(missing: str) -> None:
    env = {key: value for key, value in FULL_ENV.items() if key != missing}
    with pytest.raises(ConfigError) as caught:
        SyncConfig.from_env(env)
    assert missing in str(caught.value)


def test_whitespace_is_the_same_as_unset() -> None:
    """A quoted empty value in an env file is the shape this actually arrives in."""
    env = dict(FULL_ENV, **{ROOT_FOLDER_ENV: "   "})
    with pytest.raises(ConfigError) as caught:
        SyncConfig.from_env(env)
    assert ROOT_FOLDER_ENV in str(caught.value)


def test_the_database_url_has_exactly_one_spelling() -> None:
    """A second name for one value is the drift the parity check exists to stop."""
    env = {key: value for key, value in FULL_ENV.items() if key != DATABASE_URL_ENV}
    env["AGENT_KNOWLEDGE_DATABASE_URL"] = FULL_ENV[DATABASE_URL_ENV]
    with pytest.raises(ConfigError) as caught:
        SyncConfig.from_env(env)
    assert DATABASE_URL_ENV in str(caught.value)


def test_the_ceilings_are_overridable() -> None:
    env = dict(
        FULL_ENV,
        **{
            MAX_FILE_BYTES_ENV: "1048576",
            MAX_DOCUMENTS_ENV: "25",
            REQUEST_TIMEOUT_ENV: "7.5",
        },
    )
    config = SyncConfig.from_env(env)
    assert config.max_file_bytes == 1_048_576
    assert config.max_documents_per_run == 25
    assert config.request_timeout_seconds == 7.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (MAX_FILE_BYTES_ENV, "twenty megabytes"),
        (MAX_FILE_BYTES_ENV, "0"),
        (MAX_DOCUMENTS_ENV, "-1"),
        (REQUEST_TIMEOUT_ENV, "soon"),
        (REQUEST_TIMEOUT_ENV, "0"),
    ],
)
def test_an_unusable_ceiling_is_refused_rather_than_ignored(name: str, value: str) -> None:
    """A ceiling silently reset to its default is a ceiling nobody can trust."""
    with pytest.raises(ConfigError) as caught:
        SyncConfig.from_env(dict(FULL_ENV, **{name: value}))
    assert name in str(caught.value)


def test_direct_construction_is_validated_too() -> None:
    with pytest.raises(ConfigError):
        SyncConfig(
            credentials=DriveCredentials(client_id="a", client_secret="b", refresh_token="c"),
            root_folder_id="root",
            database_url="postgresql://x",
            max_documents_per_run=0,
        )


def test_the_config_is_frozen() -> None:
    config = SyncConfig.from_env(FULL_ENV)
    with pytest.raises(AttributeError):
        config.root_folder_id = "somewhere-else"  # type: ignore[misc]
