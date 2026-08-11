"""Proof by absence that no executor holds the admin key, and its counterpart."""

from __future__ import annotations

import pytest
from agent_control_fleet import container as container_module
from agent_control_fleet.config import AgentSpec
from agent_control_fleet.container import ContainerRuntime
from agent_control_fleet.settings import (
    EXECUTOR_API_KEY_ENV,
    PASSTHROUGH_ENV,
    REGISTER_API_KEY_ENV,
    FleetSettings,
    NetworkAddresses,
    executor_environment,
    register_environment,
)

ADDRESSES = NetworkAddresses(
    server_ip="192.168.64.4", postgres_ip="192.168.64.3", gateway="192.168.64.1"
)
SPECS = (
    AgentSpec(agent_name="marketing_researcher", web_tools=True),
    AgentSpec(agent_name="sales_outreach_drafter", web_tools=False),
)

ADMIN_KEY = "admin-key-fca31d"
EXECUTOR_KEY = "executor-key-90b7e2"
BASE_ENV = {REGISTER_API_KEY_ENV: ADMIN_KEY, EXECUTOR_API_KEY_ENV: EXECUTOR_KEY}


def _settings() -> FleetSettings:
    return FleetSettings.from_env(BASE_ENV, require_credentials=True)


def _env_with_passthrough() -> dict[str, str]:
    return {**BASE_ENV, **{name: "set" for name in PASSTHROUGH_ENV}}


def test_no_executor_is_ever_handed_the_admin_key() -> None:
    settings = _settings()
    for spec in SPECS:
        environment = executor_environment(
            spec, settings=settings, addresses=ADDRESSES, env=_env_with_passthrough()
        )
        assert ADMIN_KEY not in environment.values(), (
            f"{spec.agent_name} carries the register credential"
        )


def test_the_executor_is_handed_the_credential_it_needs() -> None:
    """So nobody satisfies the absence above by handing the executor nothing."""

    environment = executor_environment(
        SPECS[0], settings=_settings(), addresses=ADDRESSES, env=BASE_ENV
    )
    assert environment["AGENT_CONTROL_API_KEY"] == EXECUTOR_KEY


def test_the_register_job_is_handed_the_admin_key_and_not_the_executor_key() -> None:
    environment = register_environment(
        SPECS[0], settings=_settings(), addresses=ADDRESSES, env=BASE_ENV
    )
    assert environment["AGENT_CONTROL_API_KEY"] == ADMIN_KEY
    assert EXECUTOR_KEY not in environment.values()


def test_no_fleet_credential_setting_is_forwarded_by_passthrough() -> None:
    """A credential added to the passthrough list would reach every executor."""

    forwarded = {
        name
        for name in PASSTHROUGH_ENV
        if name in {REGISTER_API_KEY_ENV, EXECUTOR_API_KEY_ENV}
        or name.startswith("AGENT_CONTROL_FLEET_")
    }
    assert not forwarded, f"passthrough would forward a fleet credential: {sorted(forwarded)}"


def test_the_admin_key_never_reaches_an_executor_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment becomes ``-e K=V`` argv, which is readable from a process list."""

    invocations: list[list[str]] = []

    class _Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(argv: list[str], **kwargs: object) -> _Completed:
        invocations.append(argv)
        return _Completed()

    monkeypatch.setattr(container_module.subprocess, "run", run)

    environment = executor_environment(
        SPECS[0], settings=_settings(), addresses=ADDRESSES, env=_env_with_passthrough()
    )
    ContainerRuntime().run_detached(
        name="ac-executor-marketing-researcher",
        image="agent-control-executor:local",
        environment=environment,
        read_only=True,
        tmpfs=("/agents",),
        uid=10003,
        gid=10003,
    )

    assert invocations, "no container invocation was recorded"
    for argv in invocations:
        assert not any(ADMIN_KEY in arg for arg in argv)
