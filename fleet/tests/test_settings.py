"""The settings, and the register-environment parity assertion from section 6.1."""

from __future__ import annotations

import pytest
from agent_control_fleet.config import AgentSpec, GroupSpec
from agent_control_fleet.settings import (
    EXECUTOR_API_KEY_ENV,
    EXECUTOR_CPUS_ENV,
    EXECUTOR_MEMORY_ENV,
    FLEET_AGENTS_ENV,
    PASSTHROUGH_ENV,
    REGISTER_API_KEY_ENV,
    FleetSettings,
    NetworkAddresses,
    SettingsError,
    default_executor_memory,
    group_environment,
    model_base_url,
    register_environment,
)

ADDRESSES = NetworkAddresses(
    server_ip="192.168.64.4", postgres_ip="192.168.64.3", gateway="192.168.64.1"
)
SPEC = AgentSpec(agent_name="marketing_researcher", web_tools=True, port=8000)
GROUP = GroupSpec(name="marketing", agents=(SPEC,))

BASE_ENV = {REGISTER_API_KEY_ENV: "admin-key", EXECUTOR_API_KEY_ENV: "executor-key"}


def _settings(**overrides: str) -> FleetSettings:
    return FleetSettings.from_env({**BASE_ENV, **overrides}, require_credentials=True)


def test_a_missing_credential_names_its_setting() -> None:
    with pytest.raises(SettingsError) as caught:
        FleetSettings.from_env({EXECUTOR_API_KEY_ENV: "k"}, require_credentials=True)
    assert REGISTER_API_KEY_ENV in str(caught.value)


def test_a_comma_joined_key_list_is_refused() -> None:
    with pytest.raises(SettingsError) as caught:
        _settings(**{REGISTER_API_KEY_ENV: "one,two"})
    assert "AGENT_CONTROL_ADMIN_API_KEYS" in str(caught.value)


def test_doctor_may_run_without_credentials() -> None:
    assert FleetSettings.from_env({}, require_credentials=False).register_api_key == ""


def test_the_model_base_url_is_computed_from_the_gateway() -> None:
    assert model_base_url(_settings(), ADDRESSES) == "http://192.168.64.1:10531/v1"


@pytest.mark.parametrize(
    "override",
    [
        "http://127.0.0.1:10531/v1",
        "http://localhost:10531/v1",
        "http://host.docker.internal:10531/v1",
    ],
)
def test_a_loopback_or_docker_host_override_is_refused(override: str) -> None:
    with pytest.raises(SettingsError):
        _settings(AGENT_CONTROL_FLEET_MODEL_BASE_URL=override)


def test_an_override_wins_over_the_gateway() -> None:
    settings = _settings(AGENT_CONTROL_FLEET_MODEL_BASE_URL="http://10.0.0.9:11434/v1/")
    assert model_base_url(settings, ADDRESSES) == "http://10.0.0.9:11434/v1"


def test_the_executor_reaches_the_server_by_container_ip() -> None:
    environment = group_environment(
        GROUP, settings=_settings(), addresses=ADDRESSES, env=BASE_ENV
    )
    assert environment["AGENT_CONTROL_URL"] == "http://192.168.64.4:8000"


def test_every_process_in_the_group_is_named_with_the_port_it_listens_on() -> None:
    """The container has no other source for either, and no default to fall back to."""

    group = GroupSpec(
        name="marketing",
        agents=(
            SPEC,
            AgentSpec(agent_name="marketing_copywriter", web_tools=True, port=8001),
        ),
    )
    environment = group_environment(
        group, settings=_settings(), addresses=ADDRESSES, env=BASE_ENV
    )
    assert environment[FLEET_AGENTS_ENV] == (
        "marketing_researcher:8000,marketing_copywriter:8001"
    )
    assert "AGENT_CONTROL_AGENT_NAME" not in environment


def test_web_tools_off_reaches_the_container_as_a_value_not_an_absence() -> None:
    off = AgentSpec(agent_name="sales_outreach_drafter", web_tools=False, port=8000)
    environment = group_environment(
        GroupSpec(name="sales", agents=(off,)),
        settings=_settings(),
        addresses=ADDRESSES,
        env=BASE_ENV,
    )
    assert environment["AGENT_CONTROL_WEB_TOOLS"] == "0"


def test_the_session_uri_names_an_explicit_driver() -> None:
    environment = group_environment(
        GROUP, settings=_settings(), addresses=ADDRESSES, env=BASE_ENV
    )
    assert environment["ADK_SESSION_SERVICE_URI"].startswith("postgresql+asyncpg://adk:")
    assert "192.168.64.3:5432/adk_runtime" in environment["ADK_SESSION_SERVICE_URI"]


def test_every_passthrough_variable_the_example_reads_reaches_the_container() -> None:
    env = {**BASE_ENV, **{name: f"value-of-{name}" for name in PASSTHROUGH_ENV}}
    environment = group_environment(GROUP, settings=_settings(), addresses=ADDRESSES, env=env)
    missing = [name for name in PASSTHROUGH_ENV if environment.get(name) != f"value-of-{name}"]
    assert not missing, f"the container never sees {missing}"


def test_a_passthrough_the_fleet_computes_is_not_overridden_by_the_host() -> None:
    env = {**BASE_ENV, "AGENT_CONTROL_WEB_TOOLS": "1", FLEET_AGENTS_ENV: "wrong:9"}
    off = AgentSpec(agent_name="sales_outreach_drafter", web_tools=False, port=8000)
    environment = group_environment(
        GroupSpec(name="sales", agents=(off,)),
        settings=_settings(),
        addresses=ADDRESSES,
        env=env,
    )
    assert environment["AGENT_CONTROL_WEB_TOOLS"] == "0"
    assert environment[FLEET_AGENTS_ENV] == "sales_outreach_drafter:8000"


def test_register_differs_from_the_executor_in_the_credential_and_nothing_else() -> None:
    settings = _settings()
    env = {**BASE_ENV, **{name: "set" for name in PASSTHROUGH_ENV}}
    executor = group_environment(GROUP, settings=settings, addresses=ADDRESSES, env=env)
    register = register_environment(SPEC, settings=settings, addresses=ADDRESSES, env=env)
    differing = {
        key for key in executor.keys() | register.keys() if executor.get(key) != register.get(key)
    }
    assert differing == {"AGENT_CONTROL_API_KEY"}
    assert register["AGENT_CONTROL_API_KEY"] == "admin-key"
    assert executor["AGENT_CONTROL_API_KEY"] == "executor-key"


def test_executor_limits_leave_memory_to_the_group_and_default_the_cpus() -> None:
    """Memory cannot be one number for the fleet once a container holds N agents.

    The flat 512MB this asserted was measured against a single process with no
    knowledge tools. A two-agent group idles at 401MB and peaks at 587MB, so
    that value OOM-killed the group it was meant to size.
    """

    settings = _settings()
    assert settings.executor_memory is None
    assert settings.executor_cpus == 2


def test_a_cpu_count_that_is_not_a_positive_integer_is_refused() -> None:
    for bad in ("nope", "0", "-1"):
        with pytest.raises(SettingsError) as caught:
            _settings(**{EXECUTOR_CPUS_ENV: bad})
        assert EXECUTOR_CPUS_ENV in str(caught.value)


def test_a_group_gets_memory_scaled_to_the_processes_it_runs() -> None:
    """A two-agent group peaked at 587MB in measurement; a flat 512MB OOM-killed it."""

    assert default_executor_memory(1) == "1024MB"
    assert default_executor_memory(2) == "1280MB"
    assert default_executor_memory(6) == "2816MB"


def test_an_operator_memory_override_wins_over_the_scaled_default() -> None:
    settings = FleetSettings.from_env(
        {**BASE_ENV, EXECUTOR_MEMORY_ENV: "8GB"}, require_credentials=False
    )
    assert settings.executor_memory == "8GB"


def test_memory_is_unset_when_the_operator_says_nothing() -> None:
    """``None`` is what lets ``up`` scale it per group instead of per fleet."""

    assert FleetSettings.from_env(BASE_ENV, require_credentials=False).executor_memory is None
