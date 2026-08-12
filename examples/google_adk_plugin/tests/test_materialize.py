"""What the executor image refuses, and the one directory each process gets."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "my_agent" / "materialize.py"


def _load() -> ModuleType:
    """By path, because ``my_agent/__init__`` imports the agent and calls the server.

    The entrypoint runs this file the same way, so the test and the container
    load the same thing.
    """

    spec = importlib.util.spec_from_file_location("_materialize_under_test", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


materialize_module = _load()
ExecutorEnvError = materialize_module.ExecutorEnvError


def _refusal(call: object, *args: object) -> str:
    with pytest.raises(ExecutorEnvError) as caught:
        call(*args)  # type: ignore[operator]
    return str(caught.value.code)


def test_the_process_list_carries_a_port_for_every_agent() -> None:
    processes = materialize_module.validated_processes(
        "marketing_researcher:8000,marketing_copywriter:8001"
    )
    assert [(each.agent_name, each.port) for each in processes] == [
        ("marketing_researcher", 8000),
        ("marketing_copywriter", 8001),
    ]


def test_an_empty_process_list_has_no_single_agent_default() -> None:
    assert _refusal(materialize_module.validated_processes, "") == "fleet_agents_missing"


@pytest.mark.parametrize("raw", ["marketing_researcher", "marketing_researcher:http", "a:70000"])
def test_an_entry_that_is_not_name_and_port_is_refused(raw: str) -> None:
    assert _refusal(materialize_module.validated_processes, raw) == "fleet_agents_malformed"


@pytest.mark.parametrize("raw", ["a_agent_01:8000,a_agent_01:8001", "a_agent_01:8000,b_agent:8000"])
def test_a_repeated_name_or_port_is_refused(raw: str) -> None:
    assert _refusal(materialize_module.validated_processes, raw) == "fleet_agents_duplicate"


@pytest.mark.parametrize("name", ["marketing-researcher", "marketing:researcher", "../etc"])
def test_a_name_adk_cannot_route_never_reaches_the_filesystem(name: str) -> None:
    assert _refusal(materialize_module.validated_processes, f"{name}:8000") == "agent_name_invalid"


def test_each_process_gets_a_root_holding_exactly_its_own_package(tmp_path: Path) -> None:
    """Section 3.4: a shared root would make every process advertise every name."""

    for agent_name in ("marketing_researcher", "marketing_copywriter"):
        materialize_module.materialize(tmp_path, agent_name)
    for agent_name in ("marketing_researcher", "marketing_copywriter"):
        root = tmp_path / agent_name
        assert [entry.name for entry in root.iterdir()] == [agent_name]
        assert (root / agent_name / "agent.py").read_text().count(f'App(name="{agent_name}"') == 1


def test_a_postgres_uri_with_no_driver_is_refused_rather_than_left_to_fail_at_startup() -> None:
    code = _refusal(materialize_module.validated_session_service_uri, "postgresql://adk:x@db/adk")
    assert code == "session_uri_driver"


def test_an_executor_with_no_credential_is_refused() -> None:
    assert _refusal(materialize_module.validated_api_key, "  ") == "executor_api_key_missing"
