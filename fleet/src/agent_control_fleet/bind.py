"""``fleet bind``: write the runtime rows once the IPs exist, then exit."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .config import AgentSpec, FleetConfig
from .container import ContainerError, ContainerRuntime
from .server import AgentRuntimeRow, ServerClient
from .settings import EXECUTOR_PORT

__all__ = ["BindError", "bind_runtimes", "is_fleet_written"]


class BindError(RuntimeError):
    """A binding that was refused rather than overwritten, named by code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Observed:
    spec: AgentSpec
    base_url: str


def is_fleet_written(row: AgentRuntimeRow, spec: AgentSpec) -> bool:
    """True when this row has the shape ``bind`` writes, whatever IP it carries."""
    if row.executor_app_name != spec.agent_name:
        return False
    prefix, _, remainder = row.base_url.partition("://")
    if prefix != "http":
        return False
    host, _, port = remainder.partition(":")
    if port != str(EXECUTOR_PORT):
        return False
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return True


def bind_runtimes(
    config: FleetConfig,
    *,
    runtime: ContainerRuntime,
    client: ServerClient,
    adopt: bool,
) -> None:
    """Point every agent in the fleet file at the container now serving it."""

    observed = tuple(_observe(runtime, spec) for spec in config.agents)
    rows = {row.agent_name: row for row in client.list_runtimes()}
    if not adopt:
        _refuse_foreign_rows(observed, rows)
    for entry in observed:
        existing = rows.get(entry.spec.agent_name)
        if existing is not None and existing.base_url == entry.base_url:
            print(f"   {entry.spec.agent_name} already at {entry.base_url}")
            continue
        client.bind_runtime(
            agent_name=entry.spec.agent_name,
            base_url=entry.base_url,
            executor_app_name=entry.spec.agent_name,
        )
        print(f"   {entry.spec.agent_name} -> {entry.base_url}")


def _observe(runtime: ContainerRuntime, spec: AgentSpec) -> _Observed:
    try:
        address = runtime.ipv4_address(spec.container_name)
    except ContainerError as exc:
        raise BindError(
            "executor_not_running",
            f"{spec.container_name} has no address, so there is nothing to bind "
            f"{spec.agent_name} to: {exc}",
        ) from exc
    return _Observed(spec=spec, base_url=f"http://{address}:{EXECUTOR_PORT}")


def _refuse_foreign_rows(
    observed: tuple[_Observed, ...], rows: dict[str, AgentRuntimeRow]
) -> None:
    foreign = [
        (entry.spec, rows[entry.spec.agent_name])
        for entry in observed
        if entry.spec.agent_name in rows
        and not is_fleet_written(rows[entry.spec.agent_name], entry.spec)
    ]
    if not foreign:
        return
    described = "; ".join(
        f"{spec.agent_name} is bound to {row.base_url} as {row.executor_app_name!r}"
        for spec, row in foreign
    )
    raise BindError(
        "binding_not_written_by_fleet",
        f"{described}. None of those rows has the shape this fleet writes, so "
        "overwriting one would silently repoint a binding somebody else made. Run "
        "`agent-control-fleet bind --adopt` once to take them over; after that any "
        "row that still differs is genuinely foreign.",
    )
