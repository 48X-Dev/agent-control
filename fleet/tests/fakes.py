"""Fakes for the two things this package talks to: `container` and the server."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_control_fleet.server import AgentRuntimeRow


@dataclass
class StartedContainer:
    """One recorded ``container run``, so tests can assert the flags."""

    name: str
    image: str
    environment: dict[str, str]
    arguments: tuple[str, ...]
    read_only: bool
    tmpfs: tuple[str, ...]
    uid: int | None
    gid: int | None
    memory: str | None = None
    cpus: int | None = None


@dataclass
class FakeRuntime:
    """A ``ContainerRuntime`` that records instead of shelling out."""

    addresses: dict[str, str] = field(default_factory=dict)
    gateway: str = "192.168.64.1"
    running: set[str] = field(default_factory=set)
    register_exit_codes: dict[str, int] = field(default_factory=dict)
    started: list[StartedContainer] = field(default_factory=list)
    completed: list[StartedContainer] = field(default_factory=list)

    def require_available(self) -> None:
        return

    def require_network(self) -> None:
        return

    def is_running(self, name: str) -> bool:
        return name in self.running

    def ipv4_address(self, name: str) -> str:
        from agent_control_fleet.container import ContainerError

        if name not in self.addresses:
            raise ContainerError("address_unreadable", f"{name} has no address")
        return self.addresses[name]

    def ipv4_gateway(self, name: str) -> str:
        return self.gateway

    def remove(self, name: str) -> None:
        self.running.discard(name)

    def run_detached(
        self,
        *,
        name: str,
        image: str,
        environment: Mapping[str, str],
        arguments: Sequence[str] = (),
        read_only: bool = False,
        tmpfs: Sequence[str] = (),
        uid: int | None = None,
        gid: int | None = None,
        memory: str | None = None,
        cpus: int | None = None,
    ) -> None:
        self.started.append(
            StartedContainer(
                name,
                image,
                dict(environment),
                tuple(arguments),
                read_only,
                tuple(tmpfs),
                uid,
                gid,
                memory,
                cpus
            )
        )
        self.running.add(name)

    def run_to_completion(
        self,
        *,
        name: str,
        image: str,
        environment: Mapping[str, str],
        arguments: Sequence[str] = (),
        read_only: bool = False,
        tmpfs: Sequence[str] = (),
        uid: int | None = None,
        gid: int | None = None,
        memory: str | None = None,
        cpus: int | None = None,
    ) -> int:
        self.completed.append(
            StartedContainer(
                name, image, dict(environment), tuple(arguments), read_only, tuple(tmpfs), uid, gid
            )
        )
        return self.register_exit_codes.get(name, 0)


@dataclass
class FakeClient:
    """A ``ServerClient`` that records calls and raises what a test asks it to."""

    rows: tuple[AgentRuntimeRow, ...] = ()
    registered: tuple[str, ...] = ()
    health_error: Exception | None = None
    credentials_error: Exception | None = None
    halt_error: Exception | None = None
    calls: list[str] = field(default_factory=list)
    bound: list[dict[str, Any]] = field(default_factory=list)

    def wait_for_health(self, *, timeout_seconds: float) -> None:
        self.calls.append("health")
        if self.health_error is not None:
            raise self.health_error

    def refuse_when_credentials_are_off(self) -> None:
        self.calls.append("credentials")
        if self.credentials_error is not None:
            raise self.credentials_error

    def refuse_when_executor_credential_cannot_halt(self, executor_api_key: str) -> None:
        self.calls.append("halt-probe")
        if self.halt_error is not None:
            raise self.halt_error

    def list_runtimes(self) -> tuple[AgentRuntimeRow, ...]:
        self.calls.append("list-runtimes")
        return self.rows

    def list_registered_agents(self) -> tuple[str, ...]:
        return self.registered

    def bind_runtime(self, *, agent_name: str, base_url: str, executor_app_name: str) -> None:
        self.calls.append("bind")
        self.bound.append(
            {
                "agent_name": agent_name,
                "base_url": base_url,
                "executor_app_name": executor_app_name,
            }
        )
