"""``fleet doctor``: read-only reconciliation of intent against fact."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .config import AgentSpec, FleetConfig
from .container import ContainerError, ContainerRuntime
from .server import AgentRuntimeRow, ServerClient
from .settings import EXECUTOR_PORT

__all__ = ["Finding", "diagnose", "render"]

_REQUEST_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Finding:
    """One row of the section 6.4 table, against one agent."""

    code: str
    subject: str
    meaning: str
    informational: bool = False


def diagnose(
    config: FleetConfig, *, runtime: ContainerRuntime, client: ServerClient
) -> tuple[Finding, ...]:
    """Compare fleet.yaml, agent_runtimes and the live containers."""

    rows = {row.agent_name: row for row in client.list_runtimes()}
    intended = {spec.agent_name: spec for spec in config.agents}
    findings: list[Finding] = []

    for spec in config.agents:
        findings.extend(_diagnose_agent(spec, rows.get(spec.agent_name), runtime))

    for agent_name in sorted(set(rows) - set(intended)):
        findings.append(
            Finding(
                "runtime_without_intent",
                agent_name,
                f"Bound to {rows[agent_name].base_url} but named in no fleet.yaml entry. "
                "A binding with no intended process.",
            )
        )

    for agent_name in sorted(set(client.list_registered_agents()) - set(intended) - set(rows)):
        findings.append(
            Finding(
                "registered_only",
                agent_name,
                "Registered, in neither fleet.yaml nor agent_runtimes.",
                informational=True,
            )
        )
    return tuple(findings)


def _diagnose_agent(
    spec: AgentSpec, row: AgentRuntimeRow | None, runtime: ContainerRuntime
) -> list[Finding]:
    findings: list[Finding] = []
    if row is None:
        findings.append(
            Finding(
                "runtime_missing",
                spec.agent_name,
                "No row in agent_runtimes. Bind did not run, and opening a session "
                "answers 409 AGENT_RUNTIME_NOT_BOUND.",
            )
        )
    elif row.executor_app_name != spec.agent_name:
        findings.append(
            Finding(
                "app_name_mismatch",
                spec.agent_name,
                f"executor_app_name is {row.executor_app_name!r}, not the agent name. "
                "A pre-3.4 row: the first turn returns 500 and reads as a broken executor.",
            )
        )

    address = _address(runtime, spec)
    if address is None:
        findings.append(
            Finding(
                "container_missing",
                spec.agent_name,
                f"{spec.container_name} is not running. There is no restart policy, so "
                "it stays down until `agent-control-fleet up` runs again.",
            )
        )
        return findings

    expected = f"http://{address}:{EXECUTOR_PORT}"
    if row is not None and row.base_url != expected:
        findings.append(
            Finding(
                "base_url_stale",
                spec.agent_name,
                f"agent_runtimes says {row.base_url}; the container is at {expected}. "
                "Stale after recreation, or hand-edited.",
            )
        )

    served = _list_apps(address)
    if served != [spec.agent_name]:
        findings.append(
            Finding(
                "serves_wrong_app",
                spec.agent_name,
                f"/list-apps returned {served!r}. The container serves a different "
                "agent, or predates the one-package-per-container image.",
            )
        )
    return findings


def _address(runtime: ContainerRuntime, spec: AgentSpec) -> str | None:
    if not runtime.is_running(spec.container_name):
        return None
    try:
        return runtime.ipv4_address(spec.container_name)
    except ContainerError:
        return None


def _list_apps(address: str) -> object:
    try:
        response = httpx.get(
            f"http://{address}:{EXECUTOR_PORT}/list-apps", timeout=_REQUEST_TIMEOUT_SECONDS
        )
    except httpx.HTTPError as exc:
        return f"unreachable: {exc}"
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    try:
        return response.json()
    except ValueError:
        return "unparseable response"


def render(findings: tuple[Finding, ...]) -> str:
    if not findings:
        return "Every agent in fleet.yaml has a container, a row and a matching app name."
    width = max(len(finding.subject) for finding in findings)
    lines = [
        f"{finding.subject.ljust(width)}  {finding.code}: {finding.meaning}"
        for finding in findings
    ]
    return "\n".join(lines)
