"""Adoption, and what a row this fleet did not write does to a run."""

from __future__ import annotations

from typing import cast

import pytest
from agent_control_fleet.bind import BindError, bind_runtimes, is_fleet_written
from agent_control_fleet.config import AgentSpec, parse_fleet_config
from agent_control_fleet.container import ContainerRuntime
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
SPEC = FLEET.agents[0]

HAND_WRITTEN = AgentRuntimeRow(
    agent_name="marketing_researcher",
    base_url="http://192.168.64.1:8085",
    executor_app_name="my_agent",
    enabled=True,
)
FLEET_WRITTEN = AgentRuntimeRow(
    agent_name="marketing_researcher",
    base_url="http://192.168.64.7:8000",
    executor_app_name="marketing_researcher",
    enabled=True,
)


def _runtime(address: str = "192.168.64.7") -> FakeRuntime:
    return FakeRuntime(addresses={"ac-executor-marketing-researcher": address})


def _bind(runtime: FakeRuntime, client: FakeClient, adopt: bool = False) -> None:
    bind_runtimes(
        FLEET,
        runtime=cast(ContainerRuntime, runtime),
        client=cast(ServerClient, client),
        adopt=adopt,
    )


def test_an_absent_row_is_written_without_adoption() -> None:
    client = FakeClient()
    _bind(_runtime(), client)
    assert client.bound == [
        {
            "agent_name": "marketing_researcher",
            "base_url": "http://192.168.64.7:8000",
            "executor_app_name": "marketing_researcher",
        }
    ]


def test_a_hand_written_row_aborts_and_names_itself() -> None:
    client = FakeClient(rows=(HAND_WRITTEN,))
    with pytest.raises(BindError) as caught:
        _bind(_runtime(), client)
    assert caught.value.code == "binding_not_written_by_fleet"
    assert "my_agent" in str(caught.value)
    assert client.bound == []


def test_adopt_rewrites_the_hand_written_row() -> None:
    client = FakeClient(rows=(HAND_WRITTEN,))
    _bind(_runtime(), client, adopt=True)
    assert client.bound[0]["base_url"] == "http://192.168.64.7:8000"
    assert client.bound[0]["executor_app_name"] == "marketing_researcher"


def test_a_row_this_fleet_wrote_is_rewritten_when_the_ip_moved() -> None:
    client = FakeClient(rows=(FLEET_WRITTEN,))
    _bind(_runtime("192.168.64.9"), client)
    assert client.bound[0]["base_url"] == "http://192.168.64.9:8000"


def test_an_unchanged_row_is_left_alone() -> None:
    client = FakeClient(rows=(FLEET_WRITTEN,))
    _bind(_runtime("192.168.64.7"), client)
    assert client.bound == []


def test_binding_refuses_when_the_container_has_no_address() -> None:
    client = FakeClient()
    with pytest.raises(BindError) as caught:
        _bind(FakeRuntime(), client)
    assert caught.value.code == "executor_not_running"


@pytest.mark.parametrize(
    ("base_url", "app_name", "expected"),
    [
        ("http://192.168.64.7:8000", "marketing_researcher", True),
        ("http://192.168.64.7:8000", "my_agent", False),
        ("http://192.168.64.1:8085", "marketing_researcher", False),
        ("http://executor.internal:8000", "marketing_researcher", False),
        ("https://192.168.64.7:8000", "marketing_researcher", False),
    ],
)
def test_the_fleet_shape_is_an_ip_on_the_executor_port_under_the_agents_own_name(
    base_url: str, app_name: str, expected: bool
) -> None:
    row = AgentRuntimeRow(
        agent_name="marketing_researcher",
        base_url=base_url,
        executor_app_name=app_name,
        enabled=True,
    )
    assert is_fleet_written(row, AgentSpec("marketing_researcher", web_tools=True)) is expected
