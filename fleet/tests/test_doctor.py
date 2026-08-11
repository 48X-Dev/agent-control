"""Every row of the section 6.4 table, and the refusal to fix any of them."""

from __future__ import annotations

from typing import Any, cast

import pytest
from agent_control_fleet import doctor as doctor_module
from agent_control_fleet.config import parse_fleet_config
from agent_control_fleet.container import ContainerRuntime
from agent_control_fleet.doctor import diagnose, render
from agent_control_fleet.server import AgentRuntimeRow, ServerClient

from .fakes import FakeClient, FakeRuntime

FLEET = parse_fleet_config(
    """
version: 1
image: executor:local
agents:
  - agent_name: marketing_researcher
"""
)
CONTAINER = "ac-executor-marketing-researcher"


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"apps": ["marketing_researcher"]}
    monkeypatch.setattr(doctor_module, "_list_apps", lambda address: state["apps"])
    return state


def _healthy_runtime() -> FakeRuntime:
    return FakeRuntime(addresses={CONTAINER: "192.168.64.7"}, running={CONTAINER})


def _row(**overrides: Any) -> AgentRuntimeRow:
    fields: dict[str, Any] = {
        "agent_name": "marketing_researcher",
        "base_url": "http://192.168.64.7:8000",
        "executor_app_name": "marketing_researcher",
        "enabled": True,
    }
    return AgentRuntimeRow(**{**fields, **overrides})


def _codes(runtime: FakeRuntime, client: FakeClient) -> list[str]:
    findings = diagnose(
        FLEET, runtime=cast(ContainerRuntime, runtime), client=cast(ServerClient, client)
    )
    return [finding.code for finding in findings]


def test_a_healthy_fleet_reports_nothing(served: dict[str, Any]) -> None:
    assert _codes(_healthy_runtime(), FakeClient(rows=(_row(),))) == []


def test_an_agent_with_no_row(served: dict[str, Any]) -> None:
    assert _codes(_healthy_runtime(), FakeClient()) == ["runtime_missing"]


def test_an_agent_with_no_container(served: dict[str, Any]) -> None:
    assert _codes(FakeRuntime(), FakeClient(rows=(_row(),))) == ["container_missing"]


def test_a_row_naming_an_agent_no_fleet_file_wants(served: dict[str, Any]) -> None:
    stray = _row(agent_name="sales_prospector")
    assert _codes(_healthy_runtime(), FakeClient(rows=(_row(), stray))) == [
        "runtime_without_intent"
    ]


def test_a_base_url_that_is_not_the_observed_address(served: dict[str, Any]) -> None:
    stale = _row(base_url="http://192.168.64.99:8000")
    assert _codes(_healthy_runtime(), FakeClient(rows=(stale,))) == ["base_url_stale"]


def test_an_app_name_that_is_not_the_agent_name(served: dict[str, Any]) -> None:
    assert "app_name_mismatch" in _codes(
        _healthy_runtime(), FakeClient(rows=(_row(executor_app_name="my_agent"),))
    )


def test_a_container_serving_a_different_agent(served: dict[str, Any]) -> None:
    served["apps"] = ["my_agent"]
    assert _codes(_healthy_runtime(), FakeClient(rows=(_row(),))) == ["serves_wrong_app"]


def test_a_registered_agent_in_neither_is_informational(served: dict[str, Any]) -> None:
    client = FakeClient(rows=(_row(),), registered=("marketing_researcher", "google-adk-plugin"))
    findings = diagnose(
        FLEET, runtime=cast(ContainerRuntime, _healthy_runtime()), client=cast(ServerClient, client)
    )
    assert [(finding.code, finding.informational) for finding in findings] == [
        ("registered_only", True)
    ]


def test_doctor_writes_nothing(served: dict[str, Any]) -> None:
    runtime = FakeRuntime()
    client = FakeClient()
    diagnose(FLEET, runtime=cast(ContainerRuntime, runtime), client=cast(ServerClient, client))
    assert runtime.started == []
    assert runtime.completed == []
    assert client.bound == []


def test_render_says_so_when_there_is_nothing_to_say() -> None:
    assert "matching app name" in render(())
