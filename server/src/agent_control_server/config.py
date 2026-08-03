"""Server configuration settings."""

import logging
import os
import re
import secrets
from functools import cached_property
from typing import Any

from agent_control_models import JSONObject
from agent_control_models.agent_configs import (
    AgentModelOption,
    ModelCostTier,
    ModelProvider,
)
from agent_control_models.dispatch import (
    DEFAULT_MAX_TASKS_PER_HOUR,
    DEFAULT_MAX_TURNS_PER_HOUR,
)
from agent_control_models.tasks import (
    IMPORT_MAX_ITEMS,
    MAX_STEPS_PER_TASK,
    MAX_TURNS_PER_STEP,
)
from agent_control_telemetry import DEFAULT_CONTROL_EVENT_SINK_NAME
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_config_logger = logging.getLogger(__name__)

# Name of the dotenv file every settings class below reads, resolved once at
# import.
#
# ``AGENT_CONTROL_SETTINGS_ENV_FILE`` overrides it, and setting it to the empty
# string disables the dotenv file entirely so settings come from the process
# environment and the declared defaults alone. That empty-string case exists for
# the test suite: without it, whatever a developer happens to have in the
# repository-root ``.env`` decides what the tests assert, which has already cost
# one debugging session. Deployments have no reason to set it.
_ENV_FILE_ENV_VAR = "AGENT_CONTROL_SETTINGS_ENV_FILE"
_DEFAULT_ENV_FILE = ".env"


def _resolve_env_file() -> str | None:
    override = os.environ.get(_ENV_FILE_ENV_VAR)
    if override is None:
        return _DEFAULT_ENV_FILE
    return override or None


_COMMON_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=_resolve_env_file(),
    env_file_encoding="utf-8",
    case_sensitive=False,
    env_ignore_empty=True,
    extra="ignore",
    populate_by_name=True,
)


def _env_alias_field(default: Any, *env_names: str) -> Any:
    """Create a field that accepts multiple environment variable names."""
    return Field(default=default, validation_alias=AliasChoices(*env_names))


class AuthSettings(BaseSettings):
    """Authentication configuration for API key validation."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS_CONFIG, env_prefix="AGENT_CONTROL_")

    # Master toggle for authentication (disabled by default for local development)
    # Enable in production: AGENT_CONTROL_API_KEY_ENABLED=true
    api_key_enabled: bool = False

    # API keys (comma-separated list supports multiple keys for rotation)
    # Env: AGENT_CONTROL_API_KEYS="key1,key2,key3"
    api_keys: str = ""

    # Admin API keys (subset with elevated privileges)
    # Env: AGENT_CONTROL_ADMIN_API_KEYS="admin-key1,admin-key2"
    admin_api_keys: str = ""

    # Secret for signing session JWTs.
    # Env: AGENT_CONTROL_SESSION_SECRET="<random-string>"
    # If unset, a random secret is generated at startup (sessions won't survive
    # restarts or work across multiple server instances).
    session_secret: str = ""

    @cached_property
    def _parsed_api_keys(self) -> set[str]:
        """Parse and cache API keys from comma-separated string."""
        if not self.api_keys:
            return set()
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @cached_property
    def _parsed_admin_api_keys(self) -> set[str]:
        """Parse and cache admin API keys from comma-separated string."""
        if not self.admin_api_keys:
            return set()
        return {k.strip() for k in self.admin_api_keys.split(",") if k.strip()}

    @cached_property
    def _all_valid_keys(self) -> set[str]:
        """Cache the union of all valid keys for fast lookup."""
        return self._parsed_api_keys | self._parsed_admin_api_keys

    def get_api_keys(self) -> set[str]:
        """Get parsed API keys (cached)."""
        return self._parsed_api_keys

    def get_admin_api_keys(self) -> set[str]:
        """Get parsed admin API keys (cached)."""
        return self._parsed_admin_api_keys

    def is_valid_api_key(self, key: str) -> bool:
        """Check if key is a valid API key (regular or admin). O(1) lookup."""
        return key in self._all_valid_keys

    def is_admin_api_key(self, key: str) -> bool:
        """Check if key is an admin API key. O(1) lookup."""
        return key in self._parsed_admin_api_keys

    @cached_property
    def _resolved_session_secret(self) -> str:
        """Resolve session secret, generating an ephemeral one if not configured."""
        if self.session_secret:
            return self.session_secret
        _config_logger.warning(
            "AGENT_CONTROL_SESSION_SECRET is not set. Using an ephemeral random secret. "
            "Sessions will not survive server restarts or work across multiple instances. "
            "Set AGENT_CONTROL_SESSION_SECRET for production deployments."
        )
        return secrets.token_urlsafe(32)

    def get_session_secret(self) -> str:
        """Get the JWT signing secret (cached)."""
        return self._resolved_session_secret


class AgentControlServerDatabaseConfig(BaseSettings):
    """Database configuration for the server."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS_CONFIG, env_prefix="AGENT_CONTROL_DB_")

    # Allow direct URL override for SQLite in local dev
    url: str | None = _env_alias_field(None, "AGENT_CONTROL_DB_URL", "DATABASE_URL", "DB_URL")

    # PostgreSQL settings (only used if url is not set)
    host: str = _env_alias_field("localhost", "AGENT_CONTROL_DB_HOST", "DB_HOST")
    port: int = _env_alias_field(5432, "AGENT_CONTROL_DB_PORT", "DB_PORT")
    user: str = _env_alias_field("agent_control", "AGENT_CONTROL_DB_USER", "DB_USER")
    password: str = _env_alias_field(
        "agent_control",
        "AGENT_CONTROL_DB_PASSWORD",
        "DB_PASSWORD",
    )
    database: str = _env_alias_field(
        "agent_control",
        "AGENT_CONTROL_DB_DATABASE",
        "DB_DATABASE",
    )
    driver: str = _env_alias_field("psycopg", "AGENT_CONTROL_DB_DRIVER", "DB_DRIVER")
    pool_size: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices("AGENT_CONTROL_DB_POOL_SIZE", "DB_POOL_SIZE"),
    )
    max_overflow: int = Field(
        default=10,
        ge=0,
        validation_alias=AliasChoices("AGENT_CONTROL_DB_MAX_OVERFLOW", "DB_MAX_OVERFLOW"),
    )
    pool_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        validation_alias=AliasChoices(
            "AGENT_CONTROL_DB_POOL_TIMEOUT_SECONDS",
            "DB_POOL_TIMEOUT_SECONDS",
        ),
    )
    connect_timeout_seconds: int = Field(
        default=5,
        ge=1,
        validation_alias=AliasChoices(
            "AGENT_CONTROL_DB_CONNECT_TIMEOUT_SECONDS",
            "DB_CONNECT_TIMEOUT_SECONDS",
        ),
    )
    # 0 disables the server-side statement timeout.
    statement_timeout_seconds: float = Field(
        default=50.0,
        ge=0,
        validation_alias=AliasChoices(
            "AGENT_CONTROL_DB_STATEMENT_TIMEOUT_SECONDS",
            "DB_STATEMENT_TIMEOUT_SECONDS",
        ),
    )

    def get_url(self) -> str:
        """Get database URL, preferring an explicit URL if configured."""
        if self.url:
            return self.url
        return (
            f"postgresql+{self.driver}://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"
        )


class Settings(BaseSettings):
    """Server configuration settings."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS_CONFIG, env_prefix="AGENT_CONTROL_")

    # Server settings
    host: str = _env_alias_field("0.0.0.0", "AGENT_CONTROL_HOST", "HOST")
    port: int = _env_alias_field(8000, "AGENT_CONTROL_PORT", "PORT")
    debug: bool = _env_alias_field(False, "AGENT_CONTROL_DEBUG", "DEBUG")

    # API settings
    api_version: str = _env_alias_field("v1", "AGENT_CONTROL_API_VERSION", "API_VERSION")
    api_prefix: str = _env_alias_field("/api", "AGENT_CONTROL_API_PREFIX", "API_PREFIX")

    # Prometheus metrics settings
    prometheus_metrics_prefix: str = _env_alias_field(
        "agent_control_server",
        "AGENT_CONTROL_PROMETHEUS_METRICS_PREFIX",
        "PROMETHEUS_METRICS_PREFIX",
    )

    # CORS settings
    cors_origins: list[str] | str = _env_alias_field(
        "*",
        "AGENT_CONTROL_CORS_ORIGINS",
        "CORS_ORIGINS",
    )
    allow_methods: list[str] | str = _env_alias_field(
        ["*"],
        "AGENT_CONTROL_ALLOW_METHODS",
        "ALLOW_METHODS",
    )
    allow_headers: list[str] | str = _env_alias_field(
        ["*"],
        "AGENT_CONTROL_ALLOW_HEADERS",
        "ALLOW_HEADERS",
    )

    def get_cors_origins(self) -> list[str]:
        """Parse CORS origins from string or list."""
        return self._parse_list_setting(self.cors_origins)

    def get_allow_methods(self) -> list[str]:
        """Parse allow_methods from string or list."""
        return self._parse_list_setting(self.allow_methods)

    def get_allow_headers(self) -> list[str]:
        """Parse allow_headers from string or list."""
        return self._parse_list_setting(self.allow_headers)

    @staticmethod
    def _parse_list_setting(value: list[str] | str) -> list[str]:
        """Parse wildcard/comma-separated settings from string or list."""
        if isinstance(value, str):
            if value == "*":
                return ["*"]
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class ObservabilitySettings(BaseSettings):
    """Observability configuration settings."""

    model_config = SettingsConfigDict(
        **_COMMON_SETTINGS_CONFIG,
        env_prefix="AGENT_CONTROL_OBSERVABILITY_",
    )

    # Enable/disable observability features
    enabled: bool = True

    # Event sink selection. These use server-specific env names so SDK sink
    # selections do not leak into server startup in shared deployments.
    server_sink_name: str = _env_alias_field(
        DEFAULT_CONTROL_EVENT_SINK_NAME,
        "AGENT_CONTROL_SERVER_OBSERVABILITY_SINK_NAME",
    )
    server_sink_config: JSONObject = _env_alias_field(
        {},
        "AGENT_CONTROL_SERVER_OBSERVABILITY_SINK_CONFIG",
    )

    # Stdout logging of events
    stdout: bool = False

    @property
    def sink_name(self) -> str:
        """Compatibility accessor for the configured server sink name."""
        return self.server_sink_name

    @sink_name.setter
    def sink_name(self, value: str) -> None:
        self.server_sink_name = value

    @property
    def sink_config(self) -> JSONObject:
        """Compatibility accessor for the configured server sink config."""
        return self.server_sink_config

    @sink_config.setter
    def sink_config(self, value: JSONObject) -> None:
        self.server_sink_config = value


class LoggingSettings(BaseSettings):
    """Server logging configuration settings."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS_CONFIG, env_prefix="AGENT_CONTROL_LOG_")

    configure_logging: bool = _env_alias_field(True, "AGENT_CONTROL_CONFIGURE_LOGGING")
    access_log: bool = _env_alias_field(True, "AGENT_CONTROL_ACCESS_LOG")
    level: str | None = None
    json_logs: bool = _env_alias_field(False, "AGENT_CONTROL_LOG_JSON")


class UISettings(BaseSettings):
    """Static UI hosting configuration settings."""

    model_config = SettingsConfigDict(**_COMMON_SETTINGS_CONFIG, env_prefix="AGENT_CONTROL_UI_")

    dist_dir: str | None = None


class LinearSettings(BaseSettings):
    """Configuration for reading team milestones from Linear.

    The API key lives on the server and nowhere else. It is held as a
    ``SecretStr`` so an accidental ``repr``, log line, or ``model_dump`` of
    these settings prints a placeholder instead of the credential; reaching the
    real value takes an explicit :meth:`get_api_key` call.

    Leaving the key unset is a supported configuration, not a broken one. The
    milestone endpoint reports ``not_configured`` and never calls Linear.
    """

    model_config = SettingsConfigDict(
        **_COMMON_SETTINGS_CONFIG,
        env_prefix="AGENT_CONTROL_LINEAR_",
    )

    # Env: AGENT_CONTROL_LINEAR_API_KEY="lin_api_..."
    api_key: SecretStr = SecretStr("")

    api_url: str = "https://api.linear.app/graphql"

    timeout_seconds: float = Field(default=10.0, gt=0)

    # How long a successful read is reused before Linear is called again. Kept
    # short: milestones move slowly, but a stale board is more confusing than a
    # slightly slower page.
    cache_ttl_seconds: float = Field(default=60.0, ge=0)

    # How long a cached read stays usable as a fallback once it has expired.
    # An entry this old is only served when Linear is failing or rate-limiting,
    # which beats showing an error for a board that has not changed.
    stale_ttl_seconds: float = Field(default=900.0, ge=0)

    # How long to stop calling Linear after a failed read that carried no
    # Retry-After. Without it, an unreachable Linear costs a full request
    # timeout on every page view. A 429 overrides this with what Linear asked.
    error_cooldown_seconds: float = Field(default=30.0, ge=0)

    # Ceilings on the single GraphQL request, so one enormous Linear workspace
    # cannot turn a page view into an unbounded read.
    max_projects: int = Field(default=50, ge=1, le=250)
    max_milestones_per_project: int = Field(default=50, ge=1, le=250)

    def get_api_key(self) -> str | None:
        """Return the configured API key, or ``None`` when Linear is not set up."""
        key = self.api_key.get_secret_value().strip()
        return key or None


class ExecutorSettings(BaseSettings):
    """Configuration for the executor processes that run agents.

    An executor is a separate service. Agent Control never runs agent code, so
    every setting here is about talking to something else: where the shared
    secret lives, how long to wait, and how much concurrent work one server
    process will carry.

    Off by default. Every endpoint that needs an executor answers with a typed
    503 while ``enabled`` is false, so this whole feature is inert for existing
    deployments until someone opts in.

    The shared secret is a ``SecretStr`` for the same reason the Linear key is:
    an accidental ``repr`` or ``model_dump`` of these settings prints a
    placeholder. It is worth setting, but it is not the control that keeps an
    executor safe - ``adk api_server`` ships with no authentication and will
    not check the header. The control is that its port is never published.
    """

    model_config = SettingsConfigDict(
        **_COMMON_SETTINGS_CONFIG,
        env_prefix="AGENT_CONTROL_EXECUTOR_",
    )

    enabled: bool = False

    # Sent on every executor request. Defence in depth in front of a service
    # that does not authenticate; not a substitute for network isolation.
    shared_secret: SecretStr = SecretStr("")
    shared_secret_header: str = "X-Agent-Control-Executor-Secret"

    # Bounds one non-streaming executor call: session CRUD, history, health.
    timeout_seconds: float = Field(default=30.0, gt=0)

    # Bounds one blocking turn. Separate from ``timeout_seconds`` because the
    # two are different by an order of magnitude: a session read that takes
    # thirty seconds is broken, and a turn that takes thirty seconds is
    # ordinary. Sharing one number means either giving CRUD a five-minute rope
    # or 504-ing every real turn.
    turn_timeout_seconds: float = Field(default=300.0, gt=0)

    # How long a turn lock may sit untouched before another turn may take it.
    # This is the recovery path for a handler that died without clearing its
    # lock - a killed replica, an OOM - and nothing else clears it: there is no
    # sweeper, the predicate lives inside the acquire statement.
    turn_stale_after_seconds: float = Field(default=900.0, gt=0)

    # Streaming bounds. Unused until the streaming turn route exists; declared
    # here so the environment contract is settled in one place rather than
    # growing a new prefix later.
    stream_idle_timeout_seconds: float = Field(default=60.0, gt=0)
    max_stream_seconds: float = Field(default=900.0, gt=0)
    max_concurrent_streams: int = Field(default=8, ge=1)

    # Cost ceilings. A turn is the first request in this product that spends
    # money per call, and per-session limits do not help when a caller can open
    # more sessions.
    max_turns_per_minute: int = Field(default=30, ge=1)
    max_concurrent_sessions: int = Field(default=100, ge=1)

    # Outbound connection-pool ceilings, set explicitly rather than left to
    # httpx defaults so one unreachable executor cannot accumulate sockets.
    max_connections: int = Field(default=32, ge=1)
    max_keepalive_connections: int = Field(default=8, ge=0)

    # Local-development escape hatch for the startup refusal below. Never set
    # this in a deployment: it re-opens an unauthenticated path to endpoints
    # that spend money and inject text into running agents.
    allow_insecure_local_dev: bool = False

    @model_validator(mode="after")
    def _stale_window_must_outlast_a_turn(self) -> "ExecutorSettings":
        """Refuse a staleness window a live turn would trip over.

        The window exists to reclaim a lock whose handler died. Set it below the
        turn timeout and it reclaims locks whose handler is alive and working:
        a second turn starts against a session that already has one running,
        two invocations then write the same conversation, and the guard that
        exists to prevent exactly that has been configured into doing the
        opposite. Cheaper to refuse at import than to debug at 3am.
        """
        if self.turn_stale_after_seconds <= self.turn_timeout_seconds:
            raise ValueError(
                "AGENT_CONTROL_EXECUTOR_TURN_STALE_AFTER_SECONDS must be greater "
                "than AGENT_CONTROL_EXECUTOR_TURN_TIMEOUT_SECONDS. A shorter "
                "window lets a second turn start while the first is still "
                "running."
            )
        return self

    def get_shared_secret(self) -> str | None:
        """Return the configured shared secret, or ``None`` when unset."""
        secret = self.shared_secret.get_secret_value().strip()
        return secret or None


class DispatchSettings(BaseSettings):
    """Ceilings on the dispatch ledger, kept where they cannot be bypassed.

    The loop runs outside this server. Every bound on it lives inside, because
    a budget enforced by the process being budgeted is not a control: a
    dispatcher in a retry loop, a second dispatcher started by a different
    operator, or a bad release all spend without consulting a limit that lives
    in their own memory.

    Nothing here starts anything. There is no interval, no poll, no worker and
    no timer, and if one appears in this class the architectural line has been
    crossed. These are numbers the request path reads.
    """

    model_config = SettingsConfigDict(
        **_COMMON_SETTINGS_CONFIG,
        env_prefix="AGENT_CONTROL_DISPATCH_",
    )

    # How long a claim survives without a heartbeat. Read by the claim
    # statement's reclaim predicate and returned to the holder, so the
    # dispatcher does not get to pick its own lease.
    task_lease_seconds: int = Field(default=1800, ge=60)

    # Set on the row at claim time and checked before each step starts, so a
    # dispatcher that hangs cannot outlive its own budget.
    task_deadline_seconds: int = Field(default=3600, ge=60)

    # A workflow cannot loop. A ceiling on chain length, not a guess about
    # usefulness.
    max_steps_per_task: int = Field(default=MAX_STEPS_PER_TASK, ge=1, le=MAX_STEPS_PER_TASK)

    # One import call, one page. The Linear read is capped at the same number.
    max_import_items: int = Field(default=IMPORT_MAX_ITEMS, ge=1, le=IMPORT_MAX_ITEMS)

    # How many tasks one dispatcher may hold at once. Not enforced by this
    # server - it has no loop to enforce it on - but it is the number the
    # session-ceiling relationship below is computed from, so it lives here
    # rather than in the process it bounds, where it could be edited by whoever
    # wanted more throughput.
    max_concurrent_tasks: int = Field(default=4, ge=1)

    # Concurrent dispatch turns against one agent. One, because the ADK
    # plugin's concurrent-invocation safety has never been demonstrated: two
    # invocations sharing one plugin instance is not a risk this design takes
    # on an unverified assumption. Enforced on the turn path, counted over
    # sessions belonging to a task that are running a turn right now, which is
    # the observable that actually bounds concurrent plugin invocations.
    #
    # The ceiling is capped at one rather than merely defaulted to it. Raising
    # it is what spike E5a exists to license, and licensing it should be a code
    # change somebody reviews, not an environment variable somebody exports at
    # 3am to clear a backlog.
    max_concurrent_tasks_per_agent: int = Field(default=1, ge=1, le=1)

    # Seeded onto ``agent_dispatch_state`` when a namespace's row is first
    # created. The row is authoritative afterwards, so changing these does not
    # retroactively move a namespace that has already dispatched anything.
    default_max_tasks_per_hour: int = Field(default=DEFAULT_MAX_TASKS_PER_HOUR, ge=0)
    default_max_turns_per_hour: int = Field(default=DEFAULT_MAX_TURNS_PER_HOUR, ge=0)

    @model_validator(mode="after")
    def _fleet_must_not_squeeze_human_chat_out_of_the_session_ceiling(
        self,
    ) -> "DispatchSettings":
        """Refuse a fleet that could consume the namespace's session ceiling.

        ``max_concurrent_sessions`` (default 100) is a standing ceiling on
        sessions that *exist*, not a rate per day:
        ``count_open_sessions`` counts ``ACTIVE`` and ``ARCHIVED``, archiving is
        a UI gesture, and there is no ``closed`` status. One session per step
        plus twenty tasks a day exhausts a namespace permanently within the
        week, and because the check sits in ``open_session`` before any binding
        work, the resulting 429 also blocks every human opening a chat in the
        console. An autonomous loop would have silently disabled the product for
        its own operators.

        Half rather than all, because human chat shares the ceiling and must
        never be squeezed out by the fleet. With the shipped defaults the
        fleet's standing draw is at most sixteen against a hundred.

        Cheaper to refuse at import than to debug at 3am, which is the argument
        ``ExecutorSettings._stale_window_must_outlast_a_turn`` already makes.
        """
        fleet_draw = self.max_concurrent_tasks * self.max_steps_per_task
        fleet_share = executor_settings.max_concurrent_sessions / 2
        if fleet_draw > fleet_share:
            raise ValueError(
                f"AGENT_CONTROL_DISPATCH_MAX_CONCURRENT_TASKS "
                f"({self.max_concurrent_tasks}) times "
                f"AGENT_CONTROL_DISPATCH_MAX_STEPS_PER_TASK "
                f"({self.max_steps_per_task}) is {fleet_draw} sessions, which is "
                f"more than half of AGENT_CONTROL_EXECUTOR_MAX_CONCURRENT_SESSIONS "
                f"({executor_settings.max_concurrent_sessions}). The other half is "
                "for humans: the session ceiling is a standing limit on sessions "
                "that exist, so a fleet that can fill it locks every operator out "
                "of the console."
            )
        return self

    @model_validator(mode="after")
    def _lease_must_outlast_a_step_and_fit_inside_the_deadline(self) -> "DispatchSettings":
        """Refuse a lease that reclaims live work, or one that outlives the task.

        Two failures, opposite in shape and both silent.

        A lease shorter than a step lets a second dispatcher claim a task whose
        first dispatcher is mid-turn. Both then open sessions against the same
        agent, and per-agent concurrency of one - which exists because the
        plugin's concurrent-invocation safety is unverified - has been
        configured away. A step is bounded by the turn timeout times the
        per-step turn ceiling, so the lease has to clear that with margin.

        A lease longer than the deadline is the mirror image: the task is past
        the point where a step may start, and the row is still held by a
        process nobody can outwait. Cheaper to refuse at import than to debug
        at 3am, which is the same argument
        ``ExecutorSettings._stale_window_must_outlast_a_turn`` already makes.
        """
        longest_step = executor_settings.turn_timeout_seconds * MAX_TURNS_PER_STEP
        if self.task_lease_seconds <= longest_step:
            raise ValueError(
                "AGENT_CONTROL_DISPATCH_TASK_LEASE_SECONDS must exceed "
                f"{longest_step:.0f}s, which is "
                "AGENT_CONTROL_EXECUTOR_TURN_TIMEOUT_SECONDS times the "
                f"per-step turn ceiling of {MAX_TURNS_PER_STEP}. A shorter lease "
                "lets a second dispatcher claim a task whose step is still running."
            )
        if self.task_lease_seconds > self.task_deadline_seconds:
            raise ValueError(
                "AGENT_CONTROL_DISPATCH_TASK_LEASE_SECONDS must not exceed "
                "AGENT_CONTROL_DISPATCH_TASK_DEADLINE_SECONDS. A lease that "
                "outlives the deadline holds a task nobody may run and nobody "
                "may reclaim."
            )
        return self


def check_executor_startup_requirements(
    *,
    executor: "ExecutorSettings",
    auth: AuthSettings,
    cors_origins: list[str],
    allow_credentials: bool,
) -> None:
    """Raise when the executor is enabled on a server that cannot protect it.

    Two refusals, both about what the executor turns an open server into.
    Before this feature, an unauthenticated caller on a published port could
    tamper with configuration. After it, the same caller can start turns that
    spend the operator's model quota and put text in front of a running agent.

    The first refusal is disableable for local development because a developer
    running everything on a laptop is a real case. The second is not: a
    wildcard CORS origin with credentialed requests means any page in any tab
    can drive this API as the logged-in operator, and the fix is to name the
    origin, which takes one line and costs nothing.
    """
    if not executor.enabled:
        return

    if not auth.api_key_enabled and not executor.allow_insecure_local_dev:
        raise RuntimeError(
            "AGENT_CONTROL_EXECUTOR_ENABLED=true requires "
            "AGENT_CONTROL_API_KEY_ENABLED=true. With credential checks off, "
            "every operation succeeds unauthenticated, including the ones "
            "that spend model quota and inject text into a running agent. "
            "Set AGENT_CONTROL_EXECUTOR_ALLOW_INSECURE_LOCAL_DEV=true only on "
            "a local machine."
        )

    if allow_credentials and "*" in cors_origins:
        raise RuntimeError(
            "AGENT_CONTROL_EXECUTOR_ENABLED=true refuses a wildcard "
            "AGENT_CONTROL_CORS_ORIGINS while credentialed requests are "
            "allowed. Name the UI origin explicitly, e.g. "
            'AGENT_CONTROL_CORS_ORIGINS="https://console.example.com".'
        )


class ModelSettings(BaseSettings):
    """The allowlist of models an agent may be configured to run on.

    Server configuration, not a hardcoded table and not a live query. A live
    ``GET /v1/models`` is not the source of truth and the reason is concrete
    rather than hypothetical: the endpoint this was built against advertises
    ``gpt-image-2``, which an operator would select in a picker, save cleanly,
    and then get failures three layers away. A live list is a list of what an
    endpoint serves, not a list of what this product can use.

    Empty by default, so the model half of the feature is inert for existing
    deployments in the same way ``ExecutorSettings.enabled = False`` makes the
    executor inert.

    **There is no endpoint field here and there is no per-agent endpoint
    anywhere.** A per-agent ``api_base`` means every prompt, tool result and
    piece of customer data an agent handles is posted to a host of the writer's
    choosing, which is data exfiltration wearing a config field, plus SSRF onto
    whatever segment the executor sits on. ADMIN does not defend it either:
    ``api_key_enabled`` defaults false, which installs ``NoAuthProvider`` and
    authorizes every operation for everyone. The endpoint comes from the
    executor process's own environment - ``AGENT_CONTROL_MODEL_BASE_URL`` or
    ``OPENAI_BASE_URL``, co-equal - and the control plane never sets, reads or
    stores either.

    Configured as JSON in one variable::

        AGENT_CONTROL_MODELS_ALLOWLIST='[{"id":"gpt-5.4-mini","label":"GPT 5.4 mini",
          "provider":"openai_compatible","cost_tier":"economy","recommended":true}]'
    """

    model_config = SettingsConfigDict(
        **_COMMON_SETTINGS_CONFIG,
        env_prefix="AGENT_CONTROL_MODELS_",
    )

    allowlist: list[AgentModelOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_allowlist(self) -> "ModelSettings":
        """Refuse to start on an entry that could re-route traffic.

        Shape is already enforced on :class:`AgentModelOption` - no ``/``, no
        ``://``. What is added here is the third check and the uniqueness one,
        both of which need the whole list to be visible.

        The provider-agreement check exists because an entry such as
        ``{"id": "gpt-5.6-sol", "provider": "gemini"}`` is a plausible slip when
        adding a line to env config, and it would take the Gemini construction
        branch for a name that ADK's own registry resolves to an OpenAI client.
        The SDK closes that structurally by never handing a bare string to the
        registry; this closes it again at the point a human types it.
        """
        seen: set[str] = set()
        for entry in self.allowlist:
            if entry.id in seen:
                raise ValueError(
                    f"AGENT_CONTROL_MODELS_ALLOWLIST lists model id {entry.id!r} "
                    "more than once. Two entries for one id means the picker "
                    "shows two rows that resolve to different providers."
                )
            seen.add(entry.id)

            lowered = entry.id.lower()
            if entry.provider == ModelProvider.GEMINI:
                if not _GEMINI_ID_PATTERN.match(lowered):
                    raise ValueError(
                        f"AGENT_CONTROL_MODELS_ALLOWLIST entry {entry.id!r} "
                        "declares provider 'gemini' but the id does not look "
                        "like a Gemini or Gemma model. Google ADK's model "
                        "registry picks a client class by matching the name, so "
                        "a mislabelled entry would send traffic to a vendor "
                        "nobody chose."
                    )
            elif _GEMINI_ID_PATTERN.match(lowered):
                raise ValueError(
                    f"AGENT_CONTROL_MODELS_ALLOWLIST entry {entry.id!r} "
                    "declares provider 'openai_compatible' but the id matches "
                    "Google ADK's Gemini naming. Use provider 'gemini' for it."
                )
        return self

    def find(self, model_id: str) -> AgentModelOption | None:
        """Return the allowlist entry for an id, or ``None`` when it is gone.

        Membership is re-evaluated on every read rather than written back to the
        row. Removing an entry from server config must not silently rewrite
        stored model choices across a namespace with nothing recording it.
        """
        for entry in self.allowlist:
            if entry.id == model_id:
                return entry
        return None


# Names ADK's ``LLMRegistry`` resolves to its Google client classes. Used only
# to refuse a mislabelled allowlist entry at load time.
_GEMINI_ID_PATTERN = re.compile(r"^(gemini|gemma)[-.]")


# Resolved once at startup by :func:`check_agent_config_startup_requirements`.
# Module-level rather than a settings field because it is a derived conclusion
# about the resolved authorizer, not something an operator sets directly.
AGENT_CONFIG_DELIVERY_ALLOWED = True
AGENT_CONFIG_MODEL_TIER_LIMIT: str | None = None


def resolve_default_auth_mode(auth: AuthSettings) -> str:
    """Return the mode ``_build_default_provider`` will resolve to.

    Duplicating that rule here rather than importing it keeps this check
    callable before the auth framework is wired, which is where startup needs
    it. The rule itself is one line and is asserted by a test.
    """
    explicit = os.environ.get("AGENT_CONTROL_AUTH_MODE", "").strip().lower()
    if explicit:
        return explicit
    return "api_key" if auth.api_key_enabled else "none"


def check_agent_config_startup_requirements(*, auth: AuthSettings) -> None:
    """Decide whether saved agent configuration may reach a running agent.

    Delivery is gated, storage is not. The server does not refuse to start and
    it does not refuse writes: the editor, the history and the audit trail stay
    fully usable on a laptop with no credentials configured, which is how
    everyone will first meet this feature. What the gate suppresses is the one
    thing that changes a running agent.

    Why it exists. ``AuthSettings.api_key_enabled`` defaults false, which
    resolves the default authorizer to ``NoAuthProvider`` and authorizes every
    operation - ADMIN included - for anyone who can open a TCP connection to the
    server port. So "writes are ADMIN" is a claim about a configured server, and
    the shipped default is not that server. On it, an anonymous caller could
    otherwise put text in front of a running agent that no control evaluates,
    and point every agent in the namespace at the priciest model on the
    operator's own quota.

    The local-dev override opens the prompt fully and the model **only for
    economy-tier entries**. One boolean a developer sets on day one should not
    be the whole distance between a laptop and unbounded spend on a personal
    subscription; a developer who genuinely needs the premium model locally sets
    ``AGENT_CONTROL_API_KEY_ENABLED=true`` with a local key, which is thirty
    seconds of work and the behaviour worth incentivising anyway.

    This differs from ``check_executor_startup_requirements``, which refuses to
    start outright. The executor's whole purpose is to run turns, so a gated
    executor is useless; a gated config store is still a working config store.
    """
    global AGENT_CONFIG_DELIVERY_ALLOWED, AGENT_CONFIG_MODEL_TIER_LIMIT

    if resolve_default_auth_mode(auth) != "none":
        AGENT_CONFIG_DELIVERY_ALLOWED = True
        AGENT_CONFIG_MODEL_TIER_LIMIT = None
        return

    override = os.environ.get(
        "AGENT_CONTROL_AGENT_CONFIG_ALLOW_INSECURE_LOCAL_DEV", ""
    ).strip().lower()
    if override in {"1", "true", "yes", "on"}:
        AGENT_CONFIG_DELIVERY_ALLOWED = True
        AGENT_CONFIG_MODEL_TIER_LIMIT = ModelCostTier.ECONOMY.value
        _config_logger.warning(
            "Agent configuration delivery is enabled with credential checks "
            "off, because AGENT_CONTROL_AGENT_CONFIG_ALLOW_INSECURE_LOCAL_DEV "
            "is set. Every operation on this server succeeds unauthenticated, "
            "including the ADMIN write that changes what a running agent does. "
            "Managed models are limited to the 'economy' cost tier while this "
            "is the case. Never set this in a deployment."
        )
        return

    AGENT_CONFIG_DELIVERY_ALLOWED = False
    AGENT_CONFIG_MODEL_TIER_LIMIT = None
    _config_logger.warning(
        "Agent configuration will be stored and versioned but not delivered: "
        "AGENT_CONTROL_API_KEY_ENABLED is false, so every operation on this "
        "server succeeds unauthenticated and an admin-only write is an "
        "anonymous write. Saved prompts and models resolve to the agent's own "
        "code declaration. Set AGENT_CONTROL_API_KEY_ENABLED=true, or "
        "AGENT_CONTROL_AGENT_CONFIG_ALLOW_INSECURE_LOCAL_DEV=true on a local "
        "machine only."
    )


auth_settings = AuthSettings()
db_config = AgentControlServerDatabaseConfig()
settings = Settings()
observability_settings = ObservabilitySettings()
ui_settings = UISettings()
linear_settings = LinearSettings()
executor_settings = ExecutorSettings()
model_settings = ModelSettings()
# After ``executor_settings``: the lease refusal reads the turn timeout off it.
dispatch_settings = DispatchSettings()
