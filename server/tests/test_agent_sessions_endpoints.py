"""HTTP-level coverage for ``/agent-runtimes`` and ``/agent-sessions``.

Runs the real routers against real Postgres, with the executor replaced by a
fake :class:`ExecutorClient` in the style ``test_linear_milestones_endpoint.py``
uses for Linear. The point of the fake is that every assertion about refusal
ordering, namespace isolation and error mapping is made without a second
process being involved, which is also why those assertions are cheap enough to
keep.

What is deliberately checked here, because each one is a rule the plan states
and none of them is visible from a passing happy path:

* refusals in order - unknown agent 404, unbound agent 409, and neither of them
  reaching the executor;
* no executor coordinate on any response, and no request able to supply one;
* a session key from namespace A reading as 404 under namespace B;
* an executor that has lost the conversation rendering as a 200 with a banner
  and flipping the row to ``orphaned``, not as an error page;
* a failed executor delete leaving the row in ``orphaned_pending_delete``
  rather than reporting a success that did not happen;
* deleting a team leaving its sessions alive with no team.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.config import executor_settings
from agent_control_server.services.executor_client import (
    ExecutorMessage,
    ExecutorMessagePart,
    ExecutorSession,
    ExecutorSessionNotFoundError,
    ExecutorUnavailableError,
)
from agent_control_server.services.executor_factory import get_executor_client_factory

_RUNTIMES_URL = "/api/v1/agent-runtimes"
_SESSIONS_URL = "/api/v1/agent-sessions"
_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_EXECUTOR_APP = "my_agent"


# ---------------------------------------------------------------------------
# Fake executor
# ---------------------------------------------------------------------------


class FakeExecutorClient:
    """Records every call and answers from in-memory state."""

    def __init__(self, backend: FakeExecutorFactory, base_url: str) -> None:
        self._backend = backend
        self._base_url = base_url

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        state: Any,
    ) -> ExecutorSession:
        self._backend.calls.append(("create", app_name, user_id, session_id))
        if self._backend.create_error is not None:
            raise self._backend.create_error
        session = ExecutorSession(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            messages=(),
            state=dict(state),
        )
        self._backend.sessions[(app_name, user_id, session_id)] = session
        return session

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> ExecutorSession:
        self._backend.calls.append(("get", app_name, user_id, session_id))
        if self._backend.get_error is not None:
            raise self._backend.get_error
        session = self._backend.sessions.get((app_name, user_id, session_id))
        if session is None:
            raise ExecutorSessionNotFoundError("gone")
        return ExecutorSession(
            app_name=session.app_name,
            user_id=session.user_id,
            session_id=session.session_id,
            messages=self._backend.messages,
            state=session.state,
        )

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        self._backend.calls.append(("delete", app_name, user_id, session_id))
        if self._backend.delete_error is not None:
            raise self._backend.delete_error
        self._backend.sessions.pop((app_name, user_id, session_id), None)

    async def health(self) -> None:
        self._backend.calls.append(("health", self._base_url, "", ""))
        if self._backend.health_error is not None:
            raise self._backend.health_error

    async def aclose(self) -> None:
        return None


class FakeExecutorFactory:
    """Hands out :class:`FakeExecutorClient` and owns the shared state."""

    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str, str], ExecutorSession] = {}
        self.messages: tuple[ExecutorMessage, ...] = ()
        self.calls: list[tuple[str, str, str, str]] = []
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.health_error: Exception | None = None

    def client_for(self, *, executor_kind: str, base_url: str) -> FakeExecutorClient:
        del executor_kind
        return FakeExecutorClient(self, base_url)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    factory = FakeExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


class HeaderNamespaceAuthorizer:
    """Test authorizer mapping ``X-Test-Namespace`` onto the principal."""

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del operation, context
        return Principal(
            namespace_key=request.headers.get("X-Test-Namespace", "default"),
            is_admin=True,
        )


def _namespace_client(app: FastAPI, namespace_key: str) -> TestClient:
    return TestClient(
        app,
        raise_server_exceptions=True,
        headers={"X-Test-Namespace": namespace_key},
    )


def _agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:12]}"


def _register_agent(client: TestClient, agent_name: str) -> None:
    resp = client.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent_name,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert resp.status_code == 200, resp.text


def _bind(client: TestClient, agent_name: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "base_url": _EXECUTOR_BASE_URL,
        "executor_app_name": _EXECUTOR_APP,
    }
    body.update(overrides)
    resp = client.put(f"{_RUNTIMES_URL}/{agent_name}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _open_session(client: TestClient, agent_name: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"agent_name": agent_name}
    body.update(extra)
    resp = client.post(_SESSIONS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["session"]


# ---------------------------------------------------------------------------
# Runtime bindings
# ---------------------------------------------------------------------------


def test_bind_agent_is_idempotent_and_replaces(client: TestClient) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    created = _bind(client, agent_name)
    assert created["created"] is True
    assert created["base_url"] == _EXECUTOR_BASE_URL
    assert created["executor_kind"] == "google_adk"
    assert created["enabled"] is True

    replaced = _bind(client, agent_name, base_url="https://elsewhere:9000/", enabled=False)
    assert replaced["created"] is False
    # The trailing slash is normalized away at the model boundary.
    assert replaced["base_url"] == "https://elsewhere:9000"
    assert replaced["enabled"] is False

    listed = client.get(_RUNTIMES_URL, params={"agent": agent_name}).json()
    assert [row["agent_name"] for row in listed["runtimes"]] == [agent_name]


def test_bind_unknown_agent_is_404(client: TestClient) -> None:
    resp = client.put(
        f"{_RUNTIMES_URL}/{_agent_name()}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://executor:8080",
        "http://user:pass@executor:8080",
        "http://executor:8080/?key=secret",
        "http://executor:8080/#frag",
        "executor:8080",
    ],
)
def test_bind_rejects_unusable_base_urls(client: TestClient, base_url: str) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": base_url, "executor_app_name": _EXECUTOR_APP},
    )
    assert resp.status_code == 422, resp.text


def test_binding_write_requires_admin(
    client: TestClient, non_admin_client: TestClient
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = non_admin_client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert resp.status_code == 403, resp.text
    # Reading bindings is not admin-gated.
    assert non_admin_client.get(_RUNTIMES_URL).status_code == 200


def test_delete_binding_is_idempotent(client: TestClient) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    first = client.delete(f"{_RUNTIMES_URL}/{agent_name}")
    assert first.status_code == 200 and first.json()["deleted"] is True
    second = client.delete(f"{_RUNTIMES_URL}/{agent_name}")
    assert second.status_code == 200 and second.json()["deleted"] is False


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_open_read_and_delete_a_session(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    session = _open_session(client, agent_name, title="First chat")
    assert session["agent_name"] == agent_name
    assert session["status"] == "active"
    assert session["title"] == "First chat"
    # No executor coordinate and no runtime token reaches a client.
    assert not any(
        key.startswith("executor_") and key != "executor_kind" for key in session
    )
    assert "created_by_hash" not in session
    assert "runtime_token" not in session

    # The session-bound state really is seeded on the executor side.
    (created,) = list(fake_executor.sessions.values())
    assert created.state["agent_control"]["session_key"] == session["session_key"]
    assert created.user_id.startswith("default:")

    listed = client.get(_SESSIONS_URL, params={"agent": agent_name}).json()
    assert [row["session_key"] for row in listed["sessions"]] == [session["session_key"]]
    assert listed["pagination"]["total"] == 1

    fetched = client.get(f"{_SESSIONS_URL}/{session['session_key']}")
    assert fetched.status_code == 200
    assert fetched.json()["session"]["session_key"] == session["session_key"]

    messages = client.get(f"{_SESSIONS_URL}/{session['session_key']}/messages")
    assert messages.status_code == 200, messages.text
    assert messages.json()["messages"] == []

    deleted = client.delete(f"{_SESSIONS_URL}/{session['session_key']}")
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert fake_executor.sessions == {}
    assert client.get(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 404


def test_transcript_maps_executor_events(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name)

    fake_executor.messages = (
        ExecutorMessage(
            role="user",
            author="user",
            parts=(ExecutorMessagePart(kind="text", text="hello"),),
        ),
        ExecutorMessage(
            role="agent",
            author=agent_name,
            parts=(
                ExecutorMessagePart(kind="text", text="thinking"),
                ExecutorMessagePart(
                    kind="tool_call", tool_name="search", arguments={"q": "x"}
                ),
            ),
        ),
    )

    page = client.get(f"{_SESSIONS_URL}/{session['session_key']}/messages").json()
    assert page["total"] == 2
    assert page["has_more"] is False
    assert [m["index"] for m in page["messages"]] == [0, 1]
    assert page["messages"][0]["role"] == "user"
    assert page["messages"][1]["parts"][1]["kind"] == "tool_call"

    # Paging keeps whole-transcript indexes.
    first = client.get(
        f"{_SESSIONS_URL}/{session['session_key']}/messages", params={"limit": 1}
    ).json()
    assert first["has_more"] is True and first["next_index"] == 0
    second = client.get(
        f"{_SESSIONS_URL}/{session['session_key']}/messages",
        params={"after_index": first["next_index"]},
    ).json()
    assert [m["index"] for m in second["messages"]] == [1]


def test_patch_retitles_archives_and_refuses_server_owned_status(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name, title="Before")
    url = f"{_SESSIONS_URL}/{session['session_key']}"

    renamed = client.patch(url, json={"title": "After"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["session"]["title"] == "After"

    archived = client.patch(url, json={"status": "archived"})
    assert archived.status_code == 200, archived.text
    assert archived.json()["session"]["status"] == "archived"
    # A patch that omits the title leaves it alone.
    assert archived.json()["session"]["title"] == "After"

    cleared = client.patch(url, json={"title": None})
    assert cleared.json()["session"]["title"] is None

    refused = client.patch(url, json={"status": "orphaned"})
    assert refused.status_code == 422, refused.text

    filtered = client.get(_SESSIONS_URL, params={"status": "archived"}).json()
    assert [row["session_key"] for row in filtered["sessions"]] == [
        session["session_key"]
    ]


def test_create_forbids_executor_fields(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    resp = client.post(
        _SESSIONS_URL,
        json={"agent_name": agent_name, "executor_app_name": "somebody-elses-app"},
    )
    assert resp.status_code == 422, resp.text
    assert fake_executor.calls == []


# ---------------------------------------------------------------------------
# Refusal ordering
# ---------------------------------------------------------------------------


def test_unknown_agent_is_404_before_the_executor(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    resp = client.post(_SESSIONS_URL, json={"agent_name": _agent_name()})
    assert resp.status_code == 404, resp.text
    assert fake_executor.calls == []


def test_unbound_agent_is_409_before_the_executor(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "AGENT_RUNTIME_NOT_BOUND"
    assert fake_executor.calls == []


def test_disabled_binding_is_409(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name, enabled=False)
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 409, resp.text
    assert fake_executor.calls == []


def test_executor_disabled_is_a_typed_503(
    client: TestClient, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "EXECUTOR_UNAVAILABLE"
    assert "AGENT_CONTROL_EXECUTOR_ENABLED" in resp.json()["detail"]


def test_unreachable_executor_is_a_503_with_its_own_sentence(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    fake_executor.create_error = ExecutorUnavailableError(
        "The executor that runs this agent did not answer in time. The request "
        "was abandoned by this server; the executor may still be working."
    )
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "EXECUTOR_UNAVAILABLE"
    # The specific sentence survives 5xx sanitization; the generic one does not
    # replace it.
    assert "did not answer in time" in resp.json()["detail"]


def test_upstream_body_never_reaches_the_client(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    fake_executor.create_error = ExecutorUnavailableError(
        "Traceback: /srv/agent/tool.py line 12: API_KEY=sk-live-secret"
    )
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 503
    assert "sk-live-secret" not in resp.text
    assert "Traceback" not in resp.text


# ---------------------------------------------------------------------------
# Orphaned sessions
# ---------------------------------------------------------------------------


def test_lost_executor_session_renders_as_a_banner_and_flips_status(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name)
    fake_executor.sessions.clear()

    page = client.get(f"{_SESSIONS_URL}/{session['session_key']}/messages")
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["messages"] == []
    assert body["status"] == "orphaned"
    assert body["notice"]

    after = client.get(f"{_SESSIONS_URL}/{session['session_key']}").json()
    assert after["session"]["status"] == "orphaned"


def test_failed_executor_delete_parks_the_row_for_retry(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name)
    fake_executor.delete_error = ExecutorUnavailableError(
        "The executor that runs this agent could not be reached. The agent's "
        "process may be down, restarting, or unreachable from this server."
    )

    failed = client.delete(f"{_SESSIONS_URL}/{session['session_key']}")
    assert failed.status_code == 503, failed.text
    still_there = client.get(f"{_SESSIONS_URL}/{session['session_key']}").json()
    assert still_there["session"]["status"] == "orphaned_pending_delete"

    # Retrying once the executor is back finishes the job.
    fake_executor.delete_error = None
    assert client.delete(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 200
    assert client.get(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 404


# ---------------------------------------------------------------------------
# Namespaces and teams
# ---------------------------------------------------------------------------


def test_a_session_key_from_another_namespace_is_404(
    app: FastAPI, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")

    agent_name = _agent_name()
    _register_agent(alpha, agent_name)
    _bind(alpha, agent_name)
    session = _open_session(alpha, agent_name)

    assert beta.get(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 404
    assert (
        beta.get(f"{_SESSIONS_URL}/{session['session_key']}/messages").status_code == 404
    )
    assert beta.delete(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 404
    assert beta.get(_SESSIONS_URL).json()["sessions"] == []
    assert beta.get(_RUNTIMES_URL).json()["runtimes"] == []


def test_deleting_a_team_leaves_its_sessions_alive(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    created = client.put(
        "/api/v1/teams", json={"display_name": f"Platform {uuid.uuid4().hex[:8]}"}
    )
    assert created.status_code == 200, created.text
    team = created.json()
    session = _open_session(client, agent_name, team_slug=team["slug"])
    assert session["team_slug"] == team["slug"]

    filtered = client.get(_SESSIONS_URL, params={"team": team["slug"]}).json()
    assert [row["session_key"] for row in filtered["sessions"]] == [
        session["session_key"]
    ]
    # An unknown slug is an empty page, not a 404, matching GET /agents.
    assert client.get(_SESSIONS_URL, params={"team": "no-such-team"}).json()[
        "sessions"
    ] == []

    assert client.delete(f"/api/v1/teams/{team['slug']}").status_code == 200
    survivor = client.get(f"{_SESSIONS_URL}/{session['session_key']}")
    assert survivor.status_code == 200, survivor.text
    assert survivor.json()["session"]["team_slug"] is None


def test_opening_a_session_under_an_unknown_team_is_404(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    resp = client.post(
        _SESSIONS_URL, json={"agent_name": agent_name, "team_slug": "no-such-team"}
    )
    assert resp.status_code == 404, resp.text
    assert fake_executor.calls == []


# ---------------------------------------------------------------------------
# Health and quota
# ---------------------------------------------------------------------------


def test_executor_health_reports_per_binding(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    reachable = _agent_name()
    drained = _agent_name()
    _register_agent(client, reachable)
    _register_agent(client, drained)
    _bind(client, reachable)
    _bind(client, drained, enabled=False)

    body = client.get(f"{_SESSIONS_URL}/executor-health").json()
    assert body["enabled"] is True
    assert body["healthy"] is True
    by_agent = {row["agent_name"]: row for row in body["executors"]}
    assert by_agent[reachable]["reachable"] is True
    assert by_agent[drained]["enabled"] is False
    # A disabled binding is reported, never probed.
    assert ("health", _EXECUTOR_BASE_URL, "", "") in fake_executor.calls

    fake_executor.health_error = ExecutorUnavailableError(
        "The executor that runs this agent could not be reached. The agent's "
        "process may be down, restarting, or unreachable from this server."
    )
    unhealthy = client.get(f"{_SESSIONS_URL}/executor-health").json()
    assert unhealthy["healthy"] is False


def test_executor_health_is_inert_while_disabled(client: TestClient) -> None:
    body = client.get(f"{_SESSIONS_URL}/executor-health").json()
    assert body["enabled"] is False
    assert body["executors"] == []


def test_a_transcript_is_scoped_to_the_caller_who_opened_it(
    client: TestClient,
    non_admin_client: TestClient,
    executor_enabled: None,
    fake_executor: FakeExecutorFactory,
) -> None:
    """Content reads are per-credential, with admins exempt.

    Worth being clear-eyed about what this buys: under the default provider
    every browser caller resolves to one identity, so this separates API keys
    from each other and from the console, and separates nothing inside it.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    admin_session = _open_session(client, agent_name)
    denied = non_admin_client.get(
        f"{_SESSIONS_URL}/{admin_session['session_key']}/messages"
    )
    assert denied.status_code == 403, denied.text
    # Metadata is a different, unscoped operation.
    assert (
        non_admin_client.get(f"{_SESSIONS_URL}/{admin_session['session_key']}").status_code
        == 200
    )

    other_session = _open_session(non_admin_client, agent_name)
    allowed = client.get(f"{_SESSIONS_URL}/{other_session['session_key']}/messages")
    assert allowed.status_code == 200, allowed.text
    own = non_admin_client.get(
        f"{_SESSIONS_URL}/{other_session['session_key']}/messages"
    )
    assert own.status_code == 200, own.text


def test_session_ceiling_is_a_429(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeExecutorFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_settings, "max_concurrent_sessions", 1)
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    _open_session(client, agent_name)

    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 429, resp.text
    assert resp.json()["error_code"] == "QUOTA_EXCEEDED"
