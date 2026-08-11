"""The ordering, and what each gate stops when it fails."""

from __future__ import annotations

from typing import Any, cast

import pytest
from agent_control_fleet import up as up_module
from agent_control_fleet.config import parse_fleet_config
from agent_control_fleet.container import ContainerRuntime
from agent_control_fleet.executor import EXECUTOR_GID, EXECUTOR_UID
from agent_control_fleet.register import RegisterError
from agent_control_fleet.server import ServerClient, ServerError
from agent_control_fleet.settings import EXECUTOR_API_KEY_ENV, REGISTER_API_KEY_ENV, FleetSettings
from agent_control_fleet.up import bring_up

from .fakes import FakeClient, FakeRuntime

FLEET = parse_fleet_config(
    """
version: 1
image: agent-control-executor:local
agents:
  - agent_name: marketing_researcher
  - agent_name: sales_outreach_drafter
    web_tools: false
"""
)

ENV = {REGISTER_API_KEY_ENV: "admin-key", EXECUTOR_API_KEY_ENV: "executor-key"}
SETTINGS = FleetSettings.from_env(ENV, require_credentials=True)


@pytest.fixture(autouse=True)
def _serving_executors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        up_module, "wait_until_serving", lambda address, spec, timeout_seconds: None
    )


def _runtime() -> FakeRuntime:
    return FakeRuntime(
        addresses={
            "ac-server": "192.168.64.4",
            "ac-postgres": "192.168.64.3",
            "ac-executor-marketing-researcher": "192.168.64.7",
            "ac-executor-sales-outreach-drafter": "192.168.64.8",
        }
    )


def _bring_up(runtime: FakeRuntime, client: FakeClient, adopt: bool = False) -> None:
    bring_up(
        FLEET,
        runtime=cast(ContainerRuntime, runtime),
        client=cast(ServerClient, client),
        settings=SETTINGS,
        env=ENV,
        adopt=adopt,
    )


def test_the_whole_sequence_runs_in_order() -> None:
    runtime, client = _runtime(), FakeClient()
    _bring_up(runtime, client)
    assert client.calls[:3] == ["health", "halt-probe", "credentials"]
    assert [job.name for job in runtime.completed] == [
        "ac-register-marketing-researcher",
        "ac-register-sales-outreach-drafter",
    ]
    assert [started.name for started in runtime.started] == [
        "ac-executor-marketing-researcher",
        "ac-executor-sales-outreach-drafter",
    ]
    assert [entry["base_url"] for entry in client.bound] == [
        "http://192.168.64.7:8000",
        "http://192.168.64.8:8000",
    ]


def test_an_unreachable_server_starts_nothing() -> None:
    runtime = _runtime()
    client = FakeClient(health_error=ServerError("server_unreachable", "no"))
    with pytest.raises(ServerError):
        _bring_up(runtime, client)
    assert runtime.started == []
    assert runtime.completed == []


def test_an_executor_credential_that_cannot_halt_starts_nothing() -> None:
    runtime = _runtime()
    client = FakeClient(halt_error=ServerError("executor_credential_cannot_halt", "no"))
    with pytest.raises(ServerError):
        _bring_up(runtime, client)
    assert runtime.started == []
    assert runtime.completed == []


def test_credentials_switched_off_starts_nothing() -> None:
    runtime = _runtime()
    client = FakeClient(credentials_error=ServerError("credentials_disabled", "no"))
    with pytest.raises(ServerError):
        _bring_up(runtime, client)
    assert runtime.started == []


def test_a_non_zero_register_exit_refuses_everything_downstream() -> None:
    runtime = _runtime()
    runtime.register_exit_codes["ac-register-marketing-researcher"] = 1
    with pytest.raises(RegisterError):
        _bring_up(runtime, FakeClient())
    assert runtime.started == []
    assert len(runtime.completed) == 1, "the second agent is not registered after the first fails"


def test_executors_publish_no_ports_and_run_hardened() -> None:
    runtime = _runtime()
    _bring_up(runtime, FakeClient())
    for started in runtime.started:
        assert started.uid == EXECUTOR_UID
        assert started.gid == EXECUTOR_GID
        assert started.read_only is True
        assert started.tmpfs == ("/agents",)


def test_no_executor_is_handed_the_register_credential() -> None:
    runtime = _runtime()
    _bring_up(runtime, FakeClient())
    holders = [
        started.name
        for started in runtime.started
        if "admin-key" in started.environment.values()
    ]
    assert not holders, f"{holders} carry the admin key"


def test_the_register_jobs_are_handed_the_credential_they_need() -> None:
    runtime = _runtime()
    _bring_up(runtime, FakeClient())
    assert all(
        job.environment["AGENT_CONTROL_API_KEY"] == "admin-key" for job in runtime.completed
    )


def test_a_re_run_restarts_only_what_is_missing() -> None:
    runtime = _runtime()
    runtime.running.add("ac-executor-marketing-researcher")
    _bring_up(runtime, FakeClient())
    assert [started.name for started in runtime.started] == [
        "ac-executor-sales-outreach-drafter"
    ]


def test_binding_happens_after_the_executors_are_serving() -> None:
    runtime, client = _runtime(), FakeClient()
    order: list[str] = []
    original = runtime.run_detached

    def recording(**kwargs: Any) -> None:
        order.append(f"start:{kwargs['name']}")
        original(**kwargs)

    runtime.run_detached = recording  # type: ignore[method-assign]
    client.bind_runtime = _recording_bind(client, order)  # type: ignore[method-assign]
    _bring_up(runtime, client)
    assert order.index("start:ac-executor-sales-outreach-drafter") < order.index(
        "bind:marketing_researcher"
    )


def _recording_bind(client: FakeClient, order: list[str]) -> Any:
    def bind(*, agent_name: str, base_url: str, executor_app_name: str) -> None:
        order.append(f"bind:{agent_name}")
        client.bound.append({"agent_name": agent_name, "base_url": base_url})

    return bind
