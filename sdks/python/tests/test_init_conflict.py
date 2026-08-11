"""Tests that init() surfaces server-side conflicts."""

from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import agent_control
import httpx
import pytest
from agent_control._control_registry import clear
from agent_control.client import AgentControlClient

_ADMIN_KEY = "admin-key"
_AGENT_KEY = "agent-key"

_STEP = {
    "type": "tool",
    "name": "search",
    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    "output_schema": {"type": "array"},
}


@pytest.fixture(autouse=True)
def _clean_registry() -> Generator[None, None, None]:
    """Keep decorator-registered steps from other modules out of the payload."""
    agent_control._reset_state()
    clear()
    yield
    clear()
    agent_control._reset_state()


def _make_conflict_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:8000/api/v1/agents/initAgent")
    response = httpx.Response(409, request=request)
    return httpx.HTTPStatusError(
        "Client error '409 Conflict' for url 'http://localhost:8000/api/v1/agents/initAgent'",
        request=request,
        response=response,
    )


def _stub_server(
    stored_steps: list[dict[str, Any]], sent_modes: list[str]
) -> httpx.MockTransport:
    """A server applying initAgent's two gates: mode-based admin, then step schema."""
    stored = {(step["type"], step["name"]): step for step in stored_steps}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        if request.url.path != "/api/v1/agents/initAgent":
            return httpx.Response(404, json={})

        payload = json.loads(request.content)
        sent_modes.append(payload["conflict_mode"])
        is_admin = request.headers.get("X-API-Key") == _ADMIN_KEY

        if payload["conflict_mode"] == "overwrite" and not is_admin:
            return httpx.Response(403, json={"error_code": "AUTH_INSUFFICIENT_PRIVILEGES"})

        for step in payload["steps"]:
            existing = stored.get((step["type"], step["name"]))
            if existing is None:
                if not is_admin:
                    return httpx.Response(
                        403, json={"error_code": "AUTH_INSUFFICIENT_PRIVILEGES"}
                    )
            elif (
                existing["input_schema"] != step["input_schema"]
                or existing["output_schema"] != step["output_schema"]
            ):
                return httpx.Response(409, json={"error_code": "SCHEMA_INCOMPATIBLE"})

        return httpx.Response(200, json={"created": False, "controls": []})

    return httpx.MockTransport(handle)


def _client_class(transport: httpx.MockTransport) -> type[AgentControlClient]:
    """The SDK client bound to a stub transport, for patching into init()."""

    class _StubTransportClient(AgentControlClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs, transport=transport)

    return _StubTransportClient


def test_init_surfaces_conflict_response() -> None:
    conflict = _make_conflict_error()

    with patch(
        "agent_control.__init__.AgentControlClient.health_check",
        new=AsyncMock(return_value={"status": "healthy"}),
    ), patch(
        "agent_control.__init__.agents.register_agent",
        new=AsyncMock(side_effect=conflict),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            agent_control.init(
                agent_name=f"agent-{uuid4().hex[:12]}",
                agent_description="Testing init conflict handling",
                policy_refresh_interval_seconds=0,
            )


def test_init_restart_against_unchanged_registration_needs_no_admin_key() -> None:
    # GIVEN: an agent already registered with the step it is about to send again.
    sent_modes: list[str] = []
    transport = _stub_server([_STEP], sent_modes)

    # WHEN: the process restarts on an ordinary key.
    with patch("agent_control.AgentControlClient", _client_class(transport)):
        agent = agent_control.init(
            agent_name=f"agent-{uuid4().hex[:12]}",
            api_key=_AGENT_KEY,
            steps=[_STEP],
            policy_refresh_interval_seconds=0,
        )

    # THEN: registration asked for strict and the server let it through.
    assert sent_modes == ["strict"]
    assert agent is not None


def test_init_raises_on_changed_step_schema() -> None:
    # GIVEN: a stored step whose input schema differs from the one being sent.
    stored_step = {**_STEP, "input_schema": {"type": "object", "properties": {}}}
    sent_modes: list[str] = []
    transport = _stub_server([stored_step], sent_modes)

    # WHEN/THEN: strict refuses the change with a 409 rather than replacing it.
    with patch("agent_control.AgentControlClient", _client_class(transport)):
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            agent_control.init(
                agent_name=f"agent-{uuid4().hex[:12]}",
                api_key=_AGENT_KEY,
                steps=[_STEP],
                policy_refresh_interval_seconds=0,
            )

    assert excinfo.value.response.status_code == 409
    assert excinfo.value.response.json()["error_code"] == "SCHEMA_INCOMPATIBLE"
    assert sent_modes == ["strict"]
