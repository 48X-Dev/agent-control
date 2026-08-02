"""HTTP-level coverage for the executor-binding registry, ``/agent-runtimes``.

``test_agent_sessions_endpoints.py`` exercises bindings only as far as opening a
session needs them. This file is about the registry as a resource in its own
right: what a binding accepts, what it refuses, who may write one, and what
happens to a live conversation when its agent's binding goes away.

The last of those is the part worth having tests for. A binding is the only
record of *where* an agent's conversations live, and the module docstrings make
three separate promises about removing one - new sessions stop, existing rows
survive, and the transcript explains itself rather than erroring. Each promise
is a different code path and none of them is visible from a passing happy path.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_models.errors import ErrorCode
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.config import executor_settings
from agent_control_server.errors import ForbiddenError
from agent_control_server.services import agent_sessions as agent_sessions_service
from agent_control_server.services.executor_factory import get_executor_client_factory

from .test_agent_sessions_endpoints import FakeExecutorFactory

_RUNTIMES_URL = "/api/v1/agent-runtimes"
_SESSIONS_URL = "/api/v1/agent-sessions"
_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_EXECUTOR_APP = "my_agent"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def executor_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)


class RecordingExecutorFactory(FakeExecutorFactory):
    """A fake executor that also remembers which base URL it was handed.

    Which binding a call followed is invisible in the call log otherwise: a
    session's executor triple stays the same when its executor moves, so the
    base URL is the only thing that changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.base_urls: list[str] = []

    def client_for(self, *, executor_kind: str, base_url: str) -> Any:
        self.base_urls.append(base_url)
        return super().client_for(executor_kind=executor_kind, base_url=base_url)


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    factory = RecordingExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


class DenyingAuthorizer:
    """Authorizes everything except the operations it was told to refuse.

    The pattern comes from ``test_init_agent_conflict_mode.CreateOnlyAuthorizer``:
    an admin principal for every operation but one, so a 403 pins the *specific*
    operation a route depends on rather than the tier its key happens to hold.
    """

    def __init__(self, *denied: Operation) -> None:
        self._denied = frozenset(denied)

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del request, context
        if operation in self._denied:
            raise ForbiddenError(
                error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
                detail=f"{operation.value} denied",
            )
        return Principal(namespace_key="default", is_admin=True)


class HeaderNamespaceAuthorizer:
    """Maps ``X-Test-Namespace`` onto the principal's namespace."""

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
                "agent_name": agent_name.lower(),
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
# Reading the registry
# ---------------------------------------------------------------------------


def test_bindings_list_in_agent_name_order_and_reads_claim_no_authorship(
    client: TestClient,
) -> None:
    """``created`` answers "did this call make it", so a read must not answer."""
    names = sorted(_agent_name() for _ in range(3))
    for name in reversed(names):
        _register_agent(client, name)
        _bind(client, name)

    listed = client.get(_RUNTIMES_URL)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["runtimes"]
    assert [row["agent_name"] for row in rows] == names
    assert all(row["created"] is None for row in rows)
    assert all(row["namespace_key"] == "default" for row in rows)


def test_a_binding_is_addressed_by_the_normalized_agent_name(
    client: TestClient,
) -> None:
    """Every other agent-bearing route normalizes; this one must agree.

    A path segment that only differs in case has to reach the same row, or an
    operator ends up with a binding they cannot find and a second one they did
    not mean to create.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    written = _bind(client, agent_name.upper())
    assert written["agent_name"] == agent_name
    assert written["created"] is True

    again = _bind(client, agent_name.upper(), base_url="http://elsewhere:9000")
    assert again["created"] is False

    found = client.get(_RUNTIMES_URL, params={"agent": agent_name.upper()}).json()
    assert [row["base_url"] for row in found["runtimes"]] == ["http://elsewhere:9000"]


def test_filtering_for_an_agent_with_no_binding_is_an_empty_list(
    client: TestClient,
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.get(_RUNTIMES_URL, params={"agent": agent_name})
    assert resp.status_code == 200, resp.text
    assert resp.json()["runtimes"] == []


@pytest.mark.parametrize("bad_name", ["short", "has spaces here", "Ünicode-name!!"])
def test_an_agent_name_that_cannot_be_normalized_is_422(
    client: TestClient, bad_name: str
) -> None:
    assert client.get(_RUNTIMES_URL, params={"agent": bad_name}).status_code == 422
    assert (
        client.put(
            f"{_RUNTIMES_URL}/{bad_name}",
            json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
        ).status_code
        == 422
    )
    assert client.delete(f"{_RUNTIMES_URL}/{bad_name}").status_code == 422


# ---------------------------------------------------------------------------
# Writing the registry
# ---------------------------------------------------------------------------


def test_the_body_refuses_fields_it_does_not_understand(client: TestClient) -> None:
    """``extra="forbid"`` is what stops a misspelled field failing silently."""
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={
            "base_url": _EXECUTOR_BASE_URL,
            "executor_app_name": _EXECUTOR_APP,
            "excutor_kind": "google_adk",
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "body",
    [
        {"executor_app_name": _EXECUTOR_APP},
        {"base_url": _EXECUTOR_BASE_URL},
        {"base_url": _EXECUTOR_BASE_URL, "executor_app_name": ""},
        {"base_url": "", "executor_app_name": _EXECUTOR_APP},
        {
            "base_url": _EXECUTOR_BASE_URL,
            "executor_app_name": _EXECUTOR_APP,
            "executor_kind": "langgraph",
        },
        {
            "base_url": "http://executor:8080/" + "a" * 600,
            "executor_app_name": _EXECUTOR_APP,
        },
    ],
)
def test_an_unusable_binding_body_is_422(
    client: TestClient, body: dict[str, Any]
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.put(f"{_RUNTIMES_URL}/{agent_name}", json=body)
    assert resp.status_code == 422, resp.text


def test_a_path_that_is_a_query_or_fragment_is_rejected_not_dropped(
    client: TestClient,
) -> None:
    """A caller who thinks they configured ``?key=`` must be told they did not.

    The alternative is silently stripping it, which reads as success and then
    fails on the first request the executor rejects for want of the key.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    rejected = client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={
            "base_url": "http://executor:8080/adk?key=secret",
            "executor_app_name": _EXECUTOR_APP,
        },
    )
    assert rejected.status_code == 422, rejected.text
    # A path on its own is fine, and keeps its shape apart from the trailing slash.
    kept = _bind(client, agent_name, base_url="http://executor:8080/adk/")
    assert kept["base_url"] == "http://executor:8080/adk"


def test_a_drained_binding_is_kept_and_can_be_re_enabled(client: TestClient) -> None:
    """Draining is a decision, so it must not need the configuration retyping."""
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    drained = _bind(client, agent_name, enabled=False)
    assert drained["enabled"] is False
    assert drained["base_url"] == _EXECUTOR_BASE_URL

    # ``enabled`` defaults to true, and this is replace semantics, so an omitted
    # flag brings the binding back rather than leaving it drained.
    revived = _bind(client, agent_name)
    assert revived["enabled"] is True


# ---------------------------------------------------------------------------
# Authorization: both tiers, and the specific operation each route needs
# ---------------------------------------------------------------------------


def test_unauthenticated_callers_reach_none_of_it(
    unauthenticated_client: TestClient,
) -> None:
    assert unauthenticated_client.get(_RUNTIMES_URL).status_code == 401
    assert (
        unauthenticated_client.put(
            f"{_RUNTIMES_URL}/{_agent_name()}",
            json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
        ).status_code
        == 401
    )
    assert (
        unauthenticated_client.delete(f"{_RUNTIMES_URL}/{_agent_name()}").status_code
        == 401
    )


def test_unbinding_is_admin_only_while_listing_is_not(
    client: TestClient, non_admin_client: TestClient
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    denied = non_admin_client.delete(f"{_RUNTIMES_URL}/{agent_name}")
    assert denied.status_code == 403, denied.text
    assert non_admin_client.get(_RUNTIMES_URL).status_code == 200
    # The refusal was real: the binding is still there.
    assert len(client.get(_RUNTIMES_URL).json()["runtimes"]) == 1


def test_only_agent_runtimes_write_gates_the_writes(client: TestClient) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_RUNTIMES_WRITE))
    assert (
        client.put(
            f"{_RUNTIMES_URL}/{agent_name}",
            json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
        ).status_code
        == 403
    )
    assert client.delete(f"{_RUNTIMES_URL}/{agent_name}").status_code == 403
    assert client.get(_RUNTIMES_URL).status_code == 200


def test_only_agent_sessions_read_gates_the_read(client: TestClient) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSIONS_READ))
    assert client.get(_RUNTIMES_URL).status_code == 403
    assert (
        client.put(
            f"{_RUNTIMES_URL}/{agent_name}",
            json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


def test_two_namespaces_bind_the_same_agent_name_independently(app: FastAPI) -> None:
    """The primary key is (namespace_key, agent_name), and it has to behave so.

    Two deployments sharing a database and an agent naming convention must not
    be able to see, overwrite or delete each other's executor coordinates.
    """
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")

    agent_name = _agent_name()
    _register_agent(alpha, agent_name)
    _register_agent(beta, agent_name)
    _bind(alpha, agent_name, base_url="http://alpha-executor:8080")
    _bind(beta, agent_name, base_url="http://beta-executor:8080")

    alpha_rows = alpha.get(_RUNTIMES_URL).json()["runtimes"]
    beta_rows = beta.get(_RUNTIMES_URL).json()["runtimes"]
    assert [row["base_url"] for row in alpha_rows] == ["http://alpha-executor:8080"]
    assert [row["base_url"] for row in beta_rows] == ["http://beta-executor:8080"]
    assert alpha_rows[0]["namespace_key"] == "alpha"

    # Deleting beta's binding leaves alpha's alone.
    assert beta.delete(f"{_RUNTIMES_URL}/{agent_name}").json()["deleted"] is True
    assert alpha.get(_RUNTIMES_URL).json()["runtimes"] == alpha_rows
    assert beta.get(_RUNTIMES_URL).json()["runtimes"] == []


def test_binding_an_agent_registered_in_another_namespace_is_404(
    app: FastAPI,
) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")

    agent_name = _agent_name()
    _register_agent(alpha, agent_name)

    resp = beta.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "AGENT_NOT_FOUND"


def test_deleting_a_binding_that_was_never_there_is_not_an_error(
    client: TestClient,
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    resp = client.delete(f"{_RUNTIMES_URL}/{agent_name}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is False


# ---------------------------------------------------------------------------
# What unbinding does to conversations that already exist
# ---------------------------------------------------------------------------


def test_unbinding_stops_new_sessions_and_leaves_the_old_ones_readable(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    """The three promises the delete docstring makes, one assertion each.

    Removing a binding is a drain step: new conversations stop, existing rows
    survive, and a transcript that can no longer be fetched says why instead of
    erroring. The executor is never contacted for any of it.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name)

    assert client.delete(f"{_RUNTIMES_URL}/{agent_name}").json()["deleted"] is True
    fake_executor.calls.clear()

    refused = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "AGENT_RUNTIME_NOT_BOUND"

    survivor = client.get(f"{_SESSIONS_URL}/{session['session_key']}")
    assert survivor.status_code == 200, survivor.text
    assert survivor.json()["session"]["status"] == "active"

    transcript = client.get(f"{_SESSIONS_URL}/{session['session_key']}/messages")
    assert transcript.status_code == 200, transcript.text
    body = transcript.json()
    assert body["messages"] == []
    assert "not bound to an executor" in body["notice"]

    assert fake_executor.calls == []


def test_deleting_an_unbound_session_parks_it_rather_than_lying(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    """A delete that cannot reach the executor must not report success.

    The executor still holds the conversation, and the local row is the only
    thing that knows where. So the row is parked for retry and the caller is
    told, which is the same contract as an executor that is merely down.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name)
    client.delete(f"{_RUNTIMES_URL}/{agent_name}")

    refused = client.delete(f"{_SESSIONS_URL}/{session['session_key']}")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "AGENT_RUNTIME_NOT_BOUND"

    parked = client.get(f"{_SESSIONS_URL}/{session['session_key']}").json()
    assert parked["session"]["status"] == "orphaned_pending_delete"

    # Re-binding is the documented way out, and it finishes the job.
    _bind(client, agent_name)
    assert client.delete(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 200
    assert client.get(f"{_SESSIONS_URL}/{session['session_key']}").status_code == 404
    assert fake_executor.sessions == {}


def test_a_replaced_binding_moves_where_the_next_call_goes(
    client: TestClient, executor_enabled: None, fake_executor: RecordingExecutorFactory
) -> None:
    """Moving an executor is one PUT, including for conversations already open.

    Sessions keep their own executor triple but carry no base URL, so the
    binding is what a later read follows. If it did not, an executor move would
    silently strand every open conversation.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session = _open_session(client, agent_name)

    _bind(client, agent_name, base_url="http://moved-executor:9090")
    fake_executor.base_urls.clear()
    assert (
        client.get(f"{_SESSIONS_URL}/{session['session_key']}/messages").status_code
        == 200
    )
    assert fake_executor.base_urls == ["http://moved-executor:9090"]


# ---------------------------------------------------------------------------
# Health probing is bounded by the registry, not by the namespace's ambition
# ---------------------------------------------------------------------------


def test_health_probes_are_capped(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeExecutorFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace with more bound agents than the cap gets a partial answer.

    One health call fans out to every binding at once. Without a ceiling, the
    size of that fan-out is whatever the registry happens to hold, which is not
    a number this server chose.
    """
    monkeypatch.setattr(agent_sessions_service, "_HEALTH_PROBE_LIMIT", 2)
    for _ in range(3):
        name = _agent_name()
        _register_agent(client, name)
        _bind(client, name)

    body = client.get(f"{_SESSIONS_URL}/executor-health").json()
    assert len(body["executors"]) == 2
    assert body["healthy"] is True


def test_binding_with_an_explicit_executor_kind_is_accepted(client: TestClient) -> None:
    """Sending the field must behave exactly like omitting it.

    ``_bind`` leaves ``executor_kind`` out, so every other test in this file
    exercises the default. That path keeps the enum member, while a supplied
    value validates to a plain ``str`` because the shared BaseModel sets
    ``use_enum_values=True``. The endpoint used to call ``.value`` on it, so the
    documented request body 500'd while the undocumented one worked.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)

    omitted = _bind(client, agent_name)
    explicit = _bind(client, agent_name, executor_kind="google_adk")

    assert explicit["executor_kind"] == omitted["executor_kind"] == "google_adk"
