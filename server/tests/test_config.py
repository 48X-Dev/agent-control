"""Tests for server configuration helpers."""

import pytest
from agent_control_models.tasks import MAX_TURNS_PER_STEP

from agent_control_server.config import (
    AgentControlServerDatabaseConfig,
    DispatchSettings,
    LoggingSettings,
    ObservabilitySettings,
    Settings,
    executor_settings,
)


def test_db_config_prefers_explicit_url() -> None:
    # Given: a database config with an explicit URL set
    explicit_url = "sqlite:///tmp/test.db"
    config = AgentControlServerDatabaseConfig(url=explicit_url)

    # When: getting the database URL
    resolved = config.get_url()

    # Then: the explicit URL is returned
    assert resolved == explicit_url


def test_db_config_reads_agent_control_url_from_env(monkeypatch) -> None:
    # Given: the canonical database URL env var is set
    monkeypatch.setenv("AGENT_CONTROL_DB_URL", "sqlite:///tmp/canonical.db")

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the canonical Agent Control env var is used
    assert config.get_url() == "sqlite:///tmp/canonical.db"


def test_db_config_reads_database_url_from_env(monkeypatch) -> None:
    # Given: only the legacy DATABASE_URL env var is set
    monkeypatch.delenv("AGENT_CONTROL_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/legacy.db")

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the legacy env var is still supported during migration
    assert config.get_url() == "sqlite:///tmp/legacy.db"


def test_db_config_reads_legacy_db_prefix_from_env(monkeypatch) -> None:
    # Given: only the legacy DB_* env vars are set
    monkeypatch.delenv("AGENT_CONTROL_DB_HOST", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_DB_PORT", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_DB_USER", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_DB_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_DB_DATABASE", raising=False)
    monkeypatch.delenv("AGENT_CONTROL_DB_DRIVER", raising=False)
    monkeypatch.setenv("DB_HOST", "db.example")
    monkeypatch.setenv("DB_PORT", "15432")
    monkeypatch.setenv("DB_USER", "legacy_user")
    monkeypatch.setenv("DB_PASSWORD", "legacy_password")
    monkeypatch.setenv("DB_DATABASE", "legacy_db")
    monkeypatch.setenv("DB_DRIVER", "psycopg")

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the legacy env vars remain compatible
    assert config.get_url() == "postgresql+psycopg://legacy_user:legacy_password@db.example:15432/legacy_db"


def test_db_config_prefers_agent_control_env_over_legacy(monkeypatch) -> None:
    # Given: both canonical and legacy database URLs are present
    monkeypatch.setenv("AGENT_CONTROL_DB_URL", "sqlite:///tmp/canonical.db")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/legacy.db")

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the canonical env var wins
    assert config.get_url() == "sqlite:///tmp/canonical.db"


def test_db_config_ignores_blank_agent_control_url_and_uses_legacy(monkeypatch) -> None:
    # Given: the canonical URL is blank but a legacy URL is still configured
    monkeypatch.setenv("AGENT_CONTROL_DB_URL", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/legacy.db")

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the blank canonical env var is ignored
    assert config.get_url() == "sqlite:///tmp/legacy.db"


def test_db_config_reads_pool_settings_from_env(monkeypatch) -> None:
    # Given: database pool settings are configured via environment variables
    monkeypatch.setenv("AGENT_CONTROL_DB_POOL_SIZE", "7")
    monkeypatch.setenv("AGENT_CONTROL_DB_MAX_OVERFLOW", "2")
    monkeypatch.setenv("AGENT_CONTROL_DB_POOL_TIMEOUT_SECONDS", "3.5")
    monkeypatch.setenv("AGENT_CONTROL_DB_CONNECT_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("AGENT_CONTROL_DB_STATEMENT_TIMEOUT_SECONDS", "2.5")

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the explicit pool settings are used
    assert config.pool_size == 7
    assert config.max_overflow == 2
    assert config.pool_timeout_seconds == 3.5
    assert config.connect_timeout_seconds == 4
    assert config.statement_timeout_seconds == 2.5


def test_db_config_pool_defaults(monkeypatch) -> None:
    # Given: no pool or timeout settings in the environment
    for name in (
        "POOL_SIZE",
        "MAX_OVERFLOW",
        "POOL_TIMEOUT_SECONDS",
        "CONNECT_TIMEOUT_SECONDS",
        "STATEMENT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(f"AGENT_CONTROL_DB_{name}", raising=False)
        monkeypatch.delenv(f"DB_{name}", raising=False)

    # When: loading DB config from the environment
    config = AgentControlServerDatabaseConfig()

    # Then: the pool is bounded but keeps burst overflow and sane timeouts
    assert config.pool_size == 5
    assert config.max_overflow == 10
    assert config.pool_timeout_seconds == 5.0
    assert config.connect_timeout_seconds == 5
    assert config.statement_timeout_seconds == 50.0


def test_settings_parses_cors_origins_string() -> None:
    # Given: a comma-separated CORS origins string
    settings = Settings(cors_origins="https://a.example, https://b.example")

    # When: parsing CORS origins
    origins = settings.get_cors_origins()

    # Then: the origins are split and trimmed
    assert origins == ["https://a.example", "https://b.example"]


def test_settings_reads_agent_control_prefixed_env_vars(monkeypatch) -> None:
    # Given: canonical Agent Control server env vars are set
    monkeypatch.setenv("AGENT_CONTROL_HOST", "127.0.0.1")
    monkeypatch.setenv("AGENT_CONTROL_CORS_ORIGINS", "https://a.example, https://b.example")
    monkeypatch.setenv("AGENT_CONTROL_ALLOW_METHODS", "GET, POST")
    monkeypatch.setenv("AGENT_CONTROL_ALLOW_HEADERS", "Authorization, Content-Type")

    # When: loading settings from the environment
    config = Settings()

    # Then: the canonical env vars are parsed correctly
    assert config.host == "127.0.0.1"
    assert config.get_cors_origins() == ["https://a.example", "https://b.example"]
    assert config.get_allow_methods() == ["GET", "POST"]
    assert config.get_allow_headers() == ["Authorization", "Content-Type"]


def test_settings_reads_legacy_env_vars(monkeypatch) -> None:
    # Given: only legacy server env vars are set
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("API_VERSION", "v2")
    monkeypatch.setenv("API_PREFIX", "/legacy")
    monkeypatch.setenv("PROMETHEUS_METRICS_PREFIX", "legacy_metrics")
    monkeypatch.setenv("CORS_ORIGINS", "https://legacy.example")
    monkeypatch.setenv("ALLOW_METHODS", "GET, POST")
    monkeypatch.setenv("ALLOW_HEADERS", "Authorization, Content-Type")

    # When: loading settings from the environment
    config = Settings()

    # Then: the legacy env vars remain compatible
    assert config.host == "127.0.0.1"
    assert config.port == 9000
    assert config.debug is True
    assert config.api_version == "v2"
    assert config.api_prefix == "/legacy"
    assert config.prometheus_metrics_prefix == "legacy_metrics"
    assert config.get_cors_origins() == ["https://legacy.example"]
    assert config.get_allow_methods() == ["GET", "POST"]
    assert config.get_allow_headers() == ["Authorization", "Content-Type"]


def test_settings_prefers_agent_control_env_vars_over_legacy(monkeypatch) -> None:
    # Given: both canonical and legacy server env vars are set
    monkeypatch.setenv("AGENT_CONTROL_PORT", "7000")
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("AGENT_CONTROL_CORS_ORIGINS", "https://canonical.example")
    monkeypatch.setenv("CORS_ORIGINS", "https://legacy.example")

    # When: loading settings from the environment
    config = Settings()

    # Then: the canonical env vars win
    assert config.port == 7000
    assert config.get_cors_origins() == ["https://canonical.example"]


def test_settings_ignore_blank_agent_control_port_and_use_legacy(monkeypatch) -> None:
    # Given: the canonical port is blank but the legacy port is still set
    monkeypatch.setenv("AGENT_CONTROL_PORT", "")
    monkeypatch.setenv("PORT", "9000")

    # When: loading settings from the environment
    config = Settings()

    # Then: the blank canonical env var is ignored
    assert config.port == 9000


def test_settings_returns_cors_origins_list_unchanged() -> None:
    # Given: a CORS origins list
    settings = Settings(cors_origins=["https://a.example", "https://b.example"])

    # When: parsing CORS origins
    origins = settings.get_cors_origins()

    # Then: the list is returned as-is
    assert origins == ["https://a.example", "https://b.example"]


def test_observability_settings_support_prefixed_env_vars(monkeypatch) -> None:
    # Given: canonical observability env vars are set
    monkeypatch.setenv("AGENT_CONTROL_OBSERVABILITY_ENABLED", "false")
    monkeypatch.setenv("AGENT_CONTROL_OBSERVABILITY_STDOUT", "true")
    monkeypatch.setenv("AGENT_CONTROL_SERVER_OBSERVABILITY_SINK_NAME", "default")

    # When: loading observability settings from the environment
    config = ObservabilitySettings()

    # Then: the Agent Control-prefixed env vars are used
    assert config.enabled is False
    assert config.stdout is True
    assert config.sink_name == "default"


def test_observability_settings_parse_sink_config(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CONTROL_SERVER_OBSERVABILITY_SINK_NAME", "custom")
    monkeypatch.setenv("AGENT_CONTROL_SERVER_OBSERVABILITY_SINK_CONFIG", '{"project":"demo"}')

    config = ObservabilitySettings()

    assert config.sink_name == "custom"
    assert config.sink_config == {"project": "demo"}


def test_observability_settings_ignore_shared_sink_selection_env_vars(monkeypatch) -> None:
    # Given: only the shared SDK-facing sink selection env vars are set
    monkeypatch.setenv("AGENT_CONTROL_OBSERVABILITY_SINK_NAME", "registered")
    monkeypatch.setenv("AGENT_CONTROL_OBSERVABILITY_SINK_CONFIG", '{"project":"demo"}')

    # When: loading server observability settings
    config = ObservabilitySettings()

    # Then: the server keeps its own default sink selection
    assert config.sink_name == "default"
    assert config.sink_config == {}


def test_observability_settings_ignore_legacy_env_vars(monkeypatch) -> None:
    # Given: only legacy observability env vars are set
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "false")
    monkeypatch.setenv("OBSERVABILITY_STDOUT", "true")

    # When: loading observability settings from the environment
    config = ObservabilitySettings()

    # Then: the legacy env vars are ignored
    assert config.enabled is True
    assert config.stdout is False


def test_logging_settings_configure_logging_defaults_to_true() -> None:
    config = LoggingSettings()

    assert config.configure_logging is True


def test_logging_settings_supports_host_owned_logging(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CONTROL_CONFIGURE_LOGGING", "false")

    config = LoggingSettings()

    assert config.configure_logging is False


def test_logging_settings_access_log_defaults_to_true() -> None:
    config = LoggingSettings()

    assert config.access_log is True


def test_logging_settings_access_log_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_CONTROL_ACCESS_LOG", "false")

    config = LoggingSettings()

    assert config.access_log is False


# ---------------------------------------------------------------------------
# Dispatch ceilings
#
# The dispatch loop runs outside this server; every bound on it lives inside,
# because a budget enforced by the process being budgeted is not a control.
# The two refusals below are the ones that would otherwise be discovered by a
# duplicated turn at 3am.
# ---------------------------------------------------------------------------


def test_dispatch_settings_defaults_are_the_ones_the_plan_states() -> None:
    config = DispatchSettings(_env_file=None)

    assert config.task_lease_seconds == 1800
    assert config.task_deadline_seconds == 3600
    assert config.max_steps_per_task == 4
    assert config.max_import_items == 100


def test_a_lease_shorter_than_a_step_is_refused_at_import(monkeypatch) -> None:
    """A lease that expires mid-turn hands a live task to a second dispatcher.

    Both then open sessions against the same agent, and per-agent concurrency
    of one - which exists because the plugin's concurrent-invocation safety is
    unverified - has been configured away. The longest a step can legitimately
    run is the turn timeout times the per-step turn ceiling.
    """
    floor = executor_settings.turn_timeout_seconds * MAX_TURNS_PER_STEP
    monkeypatch.setenv("AGENT_CONTROL_DISPATCH_TASK_LEASE_SECONDS", str(int(floor)))

    with pytest.raises(ValueError, match="must exceed"):
        DispatchSettings(_env_file=None)


def test_a_lease_that_outlives_the_deadline_is_refused_too(monkeypatch) -> None:
    """The mirror image: a task past the point where a step may start, still
    held by a process nobody can outwait."""
    monkeypatch.setenv("AGENT_CONTROL_DISPATCH_TASK_LEASE_SECONDS", "7200")
    monkeypatch.setenv("AGENT_CONTROL_DISPATCH_TASK_DEADLINE_SECONDS", "3600")

    with pytest.raises(ValueError, match="must not exceed"):
        DispatchSettings(_env_file=None)


def test_a_deployment_may_lower_a_ceiling_and_not_raise_it(monkeypatch) -> None:
    """The models' constants are the ceiling; a deployment's number can only
    be at or under it, so a setting cannot widen what the wire model allows."""
    monkeypatch.setenv("AGENT_CONTROL_DISPATCH_MAX_STEPS_PER_TASK", "2")
    monkeypatch.setenv("AGENT_CONTROL_DISPATCH_MAX_IMPORT_ITEMS", "10")
    lowered = DispatchSettings(_env_file=None)
    assert lowered.max_steps_per_task == 2
    assert lowered.max_import_items == 10

    monkeypatch.setenv("AGENT_CONTROL_DISPATCH_MAX_STEPS_PER_TASK", "99")
    with pytest.raises(ValueError):
        DispatchSettings(_env_file=None)
