"""``fleet bind``: write the runtime rows once the IPs exist, then exit."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from .config import FleetConfig, Placement
from .container import ContainerError, ContainerRuntime
from .server import AgentRuntimeRow, ServerClient

__all__ = ["BindError", "bind_runtimes", "is_fleet_written"]


class BindError(RuntimeError):
    """A binding that was refused rather than overwritten, named by code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _Observed:
    placement: Placement
    base_url: str


def is_fleet_written(row: AgentRuntimeRow, placement: Placement) -> bool:
    """True when this row has the shape ``bind`` writes, whatever IP it carries."""
    if row.executor_app_name != placement.agent.agent_name:
        return False
    prefix, _, remainder = row.base_url.partition("://")
    if prefix != "http":
        return False
    host, _, port = remainder.partition(":")
    if port != str(placement.agent.port):
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

    observed = tuple(_observe(runtime, placement) for placement in config.placements)
    rows = {row.agent_name: row for row in client.list_runtimes()}
    if not adopt:
        _refuse_foreign_rows(observed, rows)
    for entry in observed:
        agent_name = entry.placement.agent.agent_name
        existing = rows.get(agent_name)
        if existing is not None and existing.base_url == entry.base_url:
            print(f"   {agent_name} already at {entry.base_url}")
            continue
        client.bind_runtime(
            agent_name=agent_name,
            base_url=entry.base_url,
            executor_app_name=agent_name,
        )
        print(f"   {agent_name} -> {entry.base_url}")


def _observe(runtime: ContainerRuntime, placement: Placement) -> _Observed:
    """The address is the group's; the port is the agent's."""

    try:
        address = runtime.ipv4_address(placement.group.container_name)
    except ContainerError as exc:
        raise BindError(
            "executor_not_running",
            f"{placement.group.container_name} has no address, so there is nothing to "
            f"bind {placement.agent.agent_name} to: {exc}",
        ) from exc
    return _Observed(placement=placement, base_url=placement.base_url(address))


def _refuse_foreign_rows(
    observed: tuple[_Observed, ...], rows: dict[str, AgentRuntimeRow]
) -> None:
    foreign = [
        (entry.placement, rows[entry.placement.agent.agent_name])
        for entry in observed
        if entry.placement.agent.agent_name in rows
        and not is_fleet_written(rows[entry.placement.agent.agent_name], entry.placement)
    ]
    if not foreign:
        return
    described = "; ".join(
        f"{placement.agent.agent_name} is bound to {row.base_url} as "
        f"{row.executor_app_name!r}"
        for placement, row in foreign
    )
    raise BindError(
        "binding_not_written_by_fleet",
        f"{described}. None of those rows has the shape this fleet writes, so "
        "overwriting one would silently repoint a binding somebody else made. Run "
        "`agent-control-fleet bind --adopt` once to take them over; after that any "
        "row that still differs is genuinely foreign.",
    )
