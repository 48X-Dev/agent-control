"""The one route an agent can use to write outside this system.

What is pinned, and each of them is a refusal of something otherwise easy:

* **the issue is resolved from the session**, so the request body has no field
  that could name a ticket and a model cannot choose a destination;
* a session opened as a plain chat, a task from a non-tracker source, and a
  dry-run task all refuse with one code whose detail says which it was;
* a deployment with write-back off refuses rather than pretending;
* it **comments and cannot close** - no state update reaches the tracker on
  this path, whatever the agent asks for;
* saving twice posts twice, because a correction is a normal second call.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import LocalJwtVerifyProvider
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from agent_control_server.services.linear_writeback import CompletedStateResolver
from agent_control_server.services.linear_writeback_runtime import (
    WritebackRuntime,
    get_writeback_runtime,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .test_agent_sessions_endpoints import (  # noqa: F401 - fixtures are used by name
    _bind,
    _open_session,
    _register_agent,
    executor_enabled,
    fake_executor,
)
from .test_agent_task_writebacks import FakeWritebackClient, _commit_linear, _ref

_SESSIONS_URL = "/api/v1/agent-sessions"
_RUNTIME_SECRET = "a" * 32


def _agent_name() -> str:
    return f"tracker_agent_{uuid.uuid4().hex[:8]}"


def _session_on_task(client: TestClient, task_key: str | None) -> str:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    extra = {"task_key": task_key} if task_key is not None else {}
    return str(_open_session(client, agent_name, **extra)["session_key"])


@pytest.fixture()
def machine(app: FastAPI) -> Any:
    """Signs each request with a token minted for the session in the path."""

    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_TRACKER_COMMENT,
    )
    client = TestClient(app, raise_server_exceptions=True)

    def post(session_key: str, text: str) -> Any:
        minted = mint_session_runtime_token(
            namespace_key="default",
            session_key=session_key,
            actor_id="0123456789abcdef",
        )
        assert minted is not None
        return client.post(
            f"{_SESSIONS_URL}/{session_key}/tracker-comment",
            json={"text": text},
            headers={"Authorization": f"Bearer {minted[0]}"},
        )

    yield post
    set_runtime_auth_config(None)


@pytest.fixture()
def linear_on(app: Any) -> Any:
    fake = FakeWritebackClient()
    runtime = WritebackRuntime(
        client=fake, resolver=CompletedStateResolver(fake), write_enabled=True
    )
    app.dependency_overrides[get_writeback_runtime] = lambda: runtime
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


@pytest.fixture()
def linear_off(app: Any) -> Any:
    fake = FakeWritebackClient()
    runtime = WritebackRuntime(
        client=fake, resolver=CompletedStateResolver(fake), write_enabled=False
    )
    app.dependency_overrides[get_writeback_runtime] = lambda: runtime
    yield fake
    app.dependency_overrides.pop(get_writeback_runtime, None)


# ---------------------------------------------------------------------------
# The happy path, and what it may not do
# ---------------------------------------------------------------------------


def test_a_comment_reaches_the_issue_the_session_is_working_on(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    ref = _ref()
    task_key = _commit_linear(client, ref)
    session_key = _session_on_task(client, task_key)

    response = machine(session_key, "the research, saved on request")

    assert response.status_code == 200, response.text
    assert response.json()["issue_ref"] == ref
    assert [issue for issue, _ in linear_on.comments] == [ref]
    assert "saved on request" in linear_on.comments[0][1]


def test_the_body_is_attributed_and_fenced(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    """Agent text is data, and the reader is told who wrote it."""

    task_key = _commit_linear(client, _ref())
    session_key = _session_on_task(client, task_key)

    machine(session_key, "line one")

    body = linear_on.comments[0][1]
    assert "not reviewed by a human" in body
    assert "> ```" in body


def test_commenting_never_moves_the_issue(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    """Closing is agent_tasks.approve, which no session token carries."""

    task_key = _commit_linear(client, _ref())
    session_key = _session_on_task(client, task_key)

    machine(session_key, "please close this")

    assert linear_on.state_updates == []


def test_saving_twice_posts_twice(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    task_key = _commit_linear(client, _ref())
    session_key = _session_on_task(client, task_key)

    machine(session_key, "first")
    machine(session_key, "second, corrected")

    assert len(linear_on.comments) == 2


def test_the_request_cannot_name_an_issue(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    """extra="forbid" is the injection defence, so prove it is on."""

    task_key = _commit_linear(client, _ref())
    session_key = _session_on_task(client, task_key)
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    response = TestClient(client.app).post(
        f"{_SESSIONS_URL}/{session_key}/tracker-comment",
        json={"text": "x", "issue_ref": "someone-elses-issue"},
        headers={"Authorization": f"Bearer {minted[0]}"},
    )

    assert response.status_code == 422, response.text
    assert linear_on.comments == []


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_chat_with_no_task_behind_it_refuses_and_says_so(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    session_key = _session_on_task(client, None)

    response = machine(session_key, "save this")

    assert response.status_code == 409, response.text
    payload = response.json()
    assert payload["error_code"] == "SESSION_HAS_NO_TRACKER_ISSUE"
    assert "opened as a chat" in payload["detail"]
    assert linear_on.comments == []


def test_a_dry_run_refuses_because_it_did_not_happen(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    task_key = _commit_linear(client, _ref(), dry_run=True)
    session_key = _session_on_task(client, task_key)

    response = machine(session_key, "save this")

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "SESSION_HAS_NO_TRACKER_ISSUE"
    assert "dry run" in response.json()["detail"]
    assert linear_on.comments == []


def test_write_back_switched_off_refuses_rather_than_pretending(
    client: TestClient,
    machine: Any,
    linear_off: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    task_key = _commit_linear(client, _ref())
    session_key = _session_on_task(client, task_key)

    response = machine(session_key, "save this")

    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "LINEAR_WRITE_DISABLED"
    assert linear_off.comments == []


def test_empty_text_is_refused_at_the_boundary(
    client: TestClient,
    machine: Any,
    linear_on: Any,
    executor_enabled: None,
    fake_executor: Any,
) -> None:
    task_key = _commit_linear(client, _ref())
    session_key = _session_on_task(client, task_key)

    assert machine(session_key, "").status_code == 422
    assert linear_on.comments == []
