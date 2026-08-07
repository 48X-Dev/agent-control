"""The knowledge flags: their defaults, their ceilings, and their wiring.

No database here on purpose. These are the checks that must run on a machine
with no Postgres, because the failure they guard against is a flag that exists
in code and reaches no container - the exists-versus-reaches class that cost
four incidents in one week.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import pytest
from agent_control_server.config import (
    KnowledgeSettings,
    check_knowledge_startup_state,
    knowledge_settings,
)
from pydantic import ValidationError

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
COMPOSE: Final = REPO_ROOT / "docker-compose.yml"
APPLE_SCRIPT: Final = REPO_ROOT / "scripts" / "apple-container-up.sh"
ENV_EXAMPLE: Final = REPO_ROOT / "server" / ".env.example"
INIT_SCRIPT: Final = REPO_ROOT / "server" / "scripts" / "knowledge_db_init.sql"
DEV_COMPOSE: Final = REPO_ROOT / "docker-compose.dev.yml"


def env_var_for(field_name: str) -> str:
    return f"AGENT_CONTROL_KNOWLEDGE_{field_name.upper()}"


# The fields a deployment is expected to set. The rest are tuning knobs with
# working defaults that no compose file needs to carry.
WIRED_FIELDS: Final = (
    "enabled",
    "db_url",
    "search_max_results",
    "snippet_max_chars",
    "searches_per_minute",
    "staleness_warn_seconds",
    "recent_window_days_max",
)


def test_the_feature_is_off_and_unconfigured_by_default() -> None:
    assert knowledge_settings.enabled is False
    assert knowledge_settings.db_url is None
    assert knowledge_settings.is_configured() is False


def test_the_result_ceiling_is_capped_in_code_not_merely_defaulted() -> None:
    """8 x 1,200 characters is the arithmetic the delivery ceiling is judged on."""
    assert KnowledgeSettings(search_max_results=8).search_max_results == 8
    with pytest.raises(ValidationError):
        KnowledgeSettings(search_max_results=9)


def test_the_recency_window_is_capped_in_code() -> None:
    with pytest.raises(ValidationError):
        KnowledgeSettings(recent_window_days_max=90)


def test_enabled_with_no_dsn_says_so_in_one_line(caplog: pytest.LogCaptureFixture) -> None:
    """Otherwise the state is indistinguishable from an empty corpus."""
    settings = KnowledgeSettings(enabled=True, db_url=None)

    with caplog.at_level(logging.WARNING, logger="agent_control_server.config"):
        check_knowledge_startup_state(settings)

    assert "AGENT_CONTROL_KNOWLEDGE_DB_URL" in caplog.text
    assert "knowledge_unavailable" in caplog.text


def test_a_configured_but_disabled_corpus_is_reported_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = KnowledgeSettings(enabled=False, db_url="postgresql+psycopg://r:p@host/db")

    with caplog.at_level(logging.INFO, logger="agent_control_server.config"):
        check_knowledge_startup_state(settings)

    assert "AGENT_CONTROL_KNOWLEDGE_ENABLED" in caplog.text


def test_the_off_state_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="agent_control_server.config"):
        check_knowledge_startup_state(KnowledgeSettings())

    assert caplog.text == ""


def test_a_healthy_configuration_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    settings = KnowledgeSettings(enabled=True, db_url="postgresql+psycopg://r:p@host/db")

    with caplog.at_level(logging.INFO, logger="agent_control_server.config"):
        check_knowledge_startup_state(settings)

    assert caplog.text == ""


@pytest.mark.parametrize("field_name", WIRED_FIELDS)
def test_every_flag_reaches_every_runtime(field_name: str) -> None:
    """A flag the container never receives is a flag that does not exist.

    Both runtimes, not one: this deployment runs under Apple ``container`` as
    well as compose, and a service that exists only in the compose file does
    not exist on the machine the stack actually runs on.
    """
    variable = env_var_for(field_name)

    assert variable in COMPOSE.read_text(), f"{variable} missing from docker-compose.yml"
    assert variable in APPLE_SCRIPT.read_text(), f"{variable} missing from apple-container-up.sh"
    assert variable in ENV_EXAMPLE.read_text(), f"{variable} missing from server/.env.example"


def test_every_settings_field_is_either_wired_or_deliberately_internal() -> None:
    """Adding a field is a decision about whether a deployment can set it."""
    internal = {"pool_size", "connect_timeout_seconds", "statement_timeout_seconds"}

    assert set(KnowledgeSettings.model_fields) == set(WIRED_FIELDS) | internal


def test_provisioning_reaches_both_runtimes_as_well() -> None:
    """The roles arrive from a script, and a script nobody runs provisions nothing."""
    assert "knowledge_db_init.sql" in DEV_COMPOSE.read_text()
    assert "knowledge_db_init.sql" in APPLE_SCRIPT.read_text()
    assert INIT_SCRIPT.is_file()
