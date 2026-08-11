"""The argv this package builds, because a missing flag is a published port."""

from __future__ import annotations

import json
from typing import Any

import pytest
from agent_control_fleet import container as container_module
from agent_control_fleet.container import NETWORK_NAME, ContainerError, ContainerRuntime

INSPECT = json.dumps(
    [{"status": {"networks": [{"ipv4Address": "192.168.64.7/24", "ipv4Gateway": "192.168.64.1"}]}}]
)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture(
    monkeypatch: pytest.MonkeyPatch, stdout: str = "", returncode: int = 0
) -> list[list[str]]:
    invocations: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> _Completed:
        invocations.append(argv)
        return _Completed(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(container_module.subprocess, "run", run)
    return invocations


def test_a_detached_run_publishes_no_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    invocations = _capture(monkeypatch)
    ContainerRuntime().run_detached(
        name="ac-executor-marketing-researcher",
        image="executor:local",
        environment={"AGENT_CONTROL_AGENT_NAME": "marketing_researcher"},
        read_only=True,
        tmpfs=("/agents",),
        uid=10003,
        gid=10003,
    )
    argv = invocations[0]
    assert "-p" not in argv and "--publish" not in argv
    assert argv[:3] == ["container", "run", "-d"]
    for flag in ("--read-only", "--tmpfs", "--uid", "--gid"):
        assert flag in argv
    assert argv[argv.index("--network") + 1] == NETWORK_NAME
    assert argv[-1] == "executor:local"


def test_a_one_shot_run_removes_itself_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations = _capture(monkeypatch, returncode=3)
    code = ContainerRuntime().run_to_completion(
        name="ac-register-marketing-researcher",
        image="executor:local",
        environment={},
        arguments=("register",),
    )
    assert code == 3
    assert "--rm" in invocations[0]
    assert invocations[0][-1] == "register"


def test_the_address_drops_the_cidr_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, stdout=INSPECT)
    assert ContainerRuntime().ipv4_address("ac-server") == "192.168.64.7"


def test_the_gateway_is_read_from_the_same_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, stdout=INSPECT)
    assert ContainerRuntime().ipv4_gateway("ac-server") == "192.168.64.1"


def test_a_container_on_no_network_is_an_error_not_an_empty_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, stdout=json.dumps([{"status": {"networks": []}}]))
    with pytest.raises(ContainerError) as caught:
        ContainerRuntime().ipv4_address("ac-server")
    assert caught.value.code == "inspect_unreadable"


def test_a_missing_network_names_the_script_that_creates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture(monkeypatch, returncode=1)
    with pytest.raises(ContainerError) as caught:
        ContainerRuntime().require_network()
    assert caught.value.code == "network_missing"
    assert "apple-container-up.sh" in str(caught.value)


def test_running_matches_the_whole_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch, stdout="ac-executor-marketing-researcher  running\nac-server  running\n")
    runtime = ContainerRuntime()
    assert runtime.is_running("ac-server") is True
    assert runtime.is_running("ac-serv") is False
