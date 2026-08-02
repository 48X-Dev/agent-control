"""Authorization for the chat registry: which tier, which operation, which session.

Three separate questions, and passing one of them says nothing about the others.

*Which tier* is the access level each new operation carries in
``DEFAULT_OPERATION_ACCESS``. Nothing pins those today - ``test_auth_framework``
asserts every operation has an entry, not what the entry says - so a tier could
be relaxed from ADMIN to AUTHENTICATED in a one-line diff and every test in the
suite would still pass.

*Which operation* is what each route actually depends on. A key that happens to
be admin satisfies every tier at once, so an admin-only test cannot tell
``agent_sessions.read`` from ``agent_sessions.content_read``. The restricted
authorizer here refuses exactly one operation and allows the rest, which is the
pattern ``test_init_agent_conflict_mode.py`` established.

*Which session* is the machine-side half. ``agent_nudges.consume`` and
``agent_plans.write`` are not reachable from any Phase 1 route - the endpoints
that use them arrive in Phases 5 and 6 - but the token that authorizes them is
minted here, in Phase 1, and handed to a process running arbitrary agent code.
So the binding it claims to have is worth proving now, against the provider
section 6.2 designates, rather than after three more phases are built on the
assumption.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from agent_control_models.errors import ErrorCode
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from agent_control_server.auth_framework import (
    Operation,
    Principal,
    get_authorizer,
    require_operation,
    set_authorizer,
)
from agent_control_server.auth_framework.config import (
    RUNTIME_TOKEN_BOUND_OPERATIONS,
    RuntimeAuthConfig,
    configure_auth_from_env,
    set_runtime_auth_config,
    teardown_auth,
)
from agent_control_server.auth_framework.providers import (
    HeaderAuthProvider,
    LocalJwtVerifyProvider,
)
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.config import executor_settings
from agent_control_server.errors import ForbiddenError
from agent_control_server.services.agent_sessions import (
    RUNTIME_TOKEN_TARGET_TYPE,
    mint_session_runtime_token,
)
from agent_control_server.services.executor_factory import get_executor_client_factory

from .test_agent_sessions_endpoints import FakeExecutorFactory

_RUNTIMES_URL = "/api/v1/agent-runtimes"
_SESSIONS_URL = "/api/v1/agent-sessions"
_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_EXECUTOR_APP = "my_agent"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"


# ---------------------------------------------------------------------------
# Fixtures and helpers
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


class DenyingAuthorizer:
    """Admin for every operation except the ones it was told to refuse."""

    def __init__(self, *denied: Operation) -> None:
        self._denied = frozenset(denied)
        self.seen: list[Operation] = []

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del request, context
        self.seen.append(operation)
        if operation in self._denied:
            raise ForbiddenError(
                error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
                detail=f"{operation.value} denied",
            )
        return Principal(namespace_key="default", is_admin=True)


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


def _bind(client: TestClient, agent_name: str) -> None:
    resp = client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert resp.status_code == 200, resp.text


def _open_session(client: TestClient, agent_name: str) -> str:
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["session"]["session_key"])


# ---------------------------------------------------------------------------
# Which tier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        # Section 6.1: chat is per-caller working state, not org configuration.
        (Operation.AGENT_SESSIONS_READ, AccessLevel.AUTHENTICATED),
        (Operation.AGENT_SESSION_CONTENT_READ, AccessLevel.AUTHENTICATED),
        (Operation.AGENT_SESSIONS_WRITE, AccessLevel.AUTHENTICATED),
        (Operation.AGENT_SESSIONS_RUN, AccessLevel.AUTHENTICATED),
        (Operation.AGENT_NUDGES_WRITE, AccessLevel.AUTHENTICATED),
        # Binding an agent to a URL this server will call is deployment
        # configuration, the same tier as CONTROL_BINDINGS_WRITE.
        (Operation.AGENT_RUNTIMES_WRITE, AccessLevel.ADMIN),
        # The machine-side pair fails closed. These entries only apply when no
        # runtime secret is configured, and AUTHENTICATED here would let any key
        # in the namespace claim any session's nudges - the exact hole the token
        # binding exists to close.
        (Operation.AGENT_NUDGES_CONSUME, AccessLevel.ADMIN),
        (Operation.AGENT_PLANS_WRITE, AccessLevel.ADMIN),
    ],
)
def test_each_new_operation_carries_the_documented_tier(
    operation: Operation, expected: AccessLevel
) -> None:
    assert DEFAULT_OPERATION_ACCESS[operation] is expected


def test_the_machine_side_pair_is_routed_to_the_runtime_provider() -> None:
    """Membership of this tuple is what makes the token the decision.

    Without it the two operations fall through to the default authorizer, where
    a session-bound token means nothing and an admin key means everything.
    """
    assert Operation.AGENT_NUDGES_CONSUME in RUNTIME_TOKEN_BOUND_OPERATIONS
    assert Operation.AGENT_PLANS_WRITE in RUNTIME_TOKEN_BOUND_OPERATIONS
    assert Operation.AGENT_SESSIONS_WRITE not in RUNTIME_TOKEN_BOUND_OPERATIONS
    assert Operation.AGENT_SESSION_CONTENT_READ not in RUNTIME_TOKEN_BOUND_OPERATIONS


async def test_a_configured_runtime_secret_installs_the_jwt_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, not the intent: startup really does hand these two over."""
    monkeypatch.setenv("AGENT_CONTROL_RUNTIME_TOKEN_SECRET", _RUNTIME_SECRET)
    monkeypatch.delenv("AGENT_CONTROL_RUNTIME_AUTH_MODE", raising=False)
    monkeypatch.setenv("AGENT_CONTROL_AUTH_MODE", "none")

    configure_auth_from_env()
    try:
        assert isinstance(
            get_authorizer(Operation.AGENT_NUDGES_CONSUME), LocalJwtVerifyProvider
        )
        assert isinstance(
            get_authorizer(Operation.AGENT_PLANS_WRITE), LocalJwtVerifyProvider
        )
        # Human-side operations keep the default authorizer.
        assert not isinstance(
            get_authorizer(Operation.AGENT_SESSIONS_WRITE), LocalJwtVerifyProvider
        )
    finally:
        await teardown_auth()


# ---------------------------------------------------------------------------
# Which tier, with real credentials
# ---------------------------------------------------------------------------


def test_a_non_admin_key_can_hold_a_whole_conversation(
    client: TestClient,
    non_admin_client: TestClient,
    executor_enabled: None,
    fake_executor: FakeExecutorFactory,
) -> None:
    """AUTHENTICATED on the session operations is the feature, not a leniency.

    ADMIN there would mean only admin keys can open a chat, which removes the
    feature for everyone else. Binding stays admin in the same walk-through, so
    the split is visible in one test.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    denied = non_admin_client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert denied.status_code == 403, denied.text

    session_key = _open_session(non_admin_client, agent_name)
    assert non_admin_client.get(_SESSIONS_URL).status_code == 200
    assert non_admin_client.get(f"{_SESSIONS_URL}/{session_key}").status_code == 200
    assert (
        non_admin_client.get(f"{_SESSIONS_URL}/{session_key}/messages").status_code
        == 200
    )
    assert (
        non_admin_client.patch(
            f"{_SESSIONS_URL}/{session_key}", json={"title": "mine"}
        ).status_code
        == 200
    )
    assert non_admin_client.get(f"{_SESSIONS_URL}/executor-health").status_code == 200
    assert non_admin_client.delete(f"{_SESSIONS_URL}/{session_key}").status_code == 200


def test_unauthenticated_callers_reach_no_session_route(
    unauthenticated_client: TestClient,
) -> None:
    key = uuid.uuid4().hex
    assert unauthenticated_client.get(_SESSIONS_URL).status_code == 401
    assert (
        unauthenticated_client.post(
            _SESSIONS_URL, json={"agent_name": _agent_name()}
        ).status_code
        == 401
    )
    assert unauthenticated_client.get(f"{_SESSIONS_URL}/{key}").status_code == 401
    assert (
        unauthenticated_client.get(f"{_SESSIONS_URL}/{key}/messages").status_code == 401
    )
    assert (
        unauthenticated_client.patch(
            f"{_SESSIONS_URL}/{key}", json={"title": "x"}
        ).status_code
        == 401
    )
    assert unauthenticated_client.delete(f"{_SESSIONS_URL}/{key}").status_code == 401
    assert (
        unauthenticated_client.get(f"{_SESSIONS_URL}/executor-health").status_code == 401
    )


# ---------------------------------------------------------------------------
# Which operation
# ---------------------------------------------------------------------------


def test_denying_the_metadata_read_leaves_the_writes_alone(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session_key = _open_session(client, agent_name)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSIONS_READ))
    assert client.get(_SESSIONS_URL).status_code == 403
    assert client.get(f"{_SESSIONS_URL}/{session_key}").status_code == 403
    assert client.get(f"{_SESSIONS_URL}/executor-health").status_code == 403
    # Content and writes are different operations and keep working.
    assert client.get(f"{_SESSIONS_URL}/{session_key}/messages").status_code == 200
    assert (
        client.patch(f"{_SESSIONS_URL}/{session_key}", json={"title": "x"}).status_code
        == 200
    )


def test_denying_the_content_read_leaves_the_metadata_read_alone(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    """The split is the whole point of ``agent_sessions.content_read``.

    Titles and timestamps are one sensitivity class; raw prompts, model output
    and tool results are another. A caller granted the first must not get the
    second for free.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session_key = _open_session(client, agent_name)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSION_CONTENT_READ))
    denied = client.get(f"{_SESSIONS_URL}/{session_key}/messages")
    assert denied.status_code == 403, denied.text
    assert client.get(f"{_SESSIONS_URL}/{session_key}").status_code == 200
    assert client.get(_SESSIONS_URL).status_code == 200


def test_denying_the_session_write_leaves_the_reads_alone(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session_key = _open_session(client, agent_name)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSIONS_WRITE))
    assert client.post(_SESSIONS_URL, json={"agent_name": agent_name}).status_code == 403
    assert (
        client.patch(f"{_SESSIONS_URL}/{session_key}", json={"title": "x"}).status_code
        == 403
    )
    assert client.delete(f"{_SESSIONS_URL}/{session_key}").status_code == 403
    assert client.get(f"{_SESSIONS_URL}/{session_key}").status_code == 200


def test_the_refusal_happens_before_the_executor_is_touched(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    """A 403 must not have already created a conversation somewhere."""
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSIONS_WRITE))
    assert client.post(_SESSIONS_URL, json={"agent_name": agent_name}).status_code == 403
    assert fake_executor.calls == []
    assert fake_executor.sessions == {}


def test_no_session_route_asks_for_an_operation_it_does_not_need(
    client: TestClient, executor_enabled: None, fake_executor: FakeExecutorFactory
) -> None:
    """Each route consults exactly one operation, and it is the documented one.

    Cheap to assert and it catches the copy-paste failure a tier test cannot:
    a handler that guards the right operation but also drags in another.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    session_key = _open_session(client, agent_name)

    expected = {
        ("get", _SESSIONS_URL, None): Operation.AGENT_SESSIONS_READ,
        ("get", f"{_SESSIONS_URL}/{session_key}", None): Operation.AGENT_SESSIONS_READ,
        (
            "get",
            f"{_SESSIONS_URL}/executor-health",
            None,
        ): Operation.AGENT_SESSIONS_READ,
        (
            "get",
            f"{_SESSIONS_URL}/{session_key}/messages",
            None,
        ): Operation.AGENT_SESSION_CONTENT_READ,
        (
            "post",
            _SESSIONS_URL,
            json.dumps({"agent_name": agent_name}),
        ): Operation.AGENT_SESSIONS_WRITE,
        (
            "patch",
            f"{_SESSIONS_URL}/{session_key}",
            json.dumps({"title": "x"}),
        ): Operation.AGENT_SESSIONS_WRITE,
        ("delete", f"{_SESSIONS_URL}/{session_key}", None): Operation.AGENT_SESSIONS_WRITE,
        ("get", _RUNTIMES_URL, None): Operation.AGENT_SESSIONS_READ,
        (
            "put",
            f"{_RUNTIMES_URL}/{agent_name}",
            json.dumps(
                {
                    "base_url": _EXECUTOR_BASE_URL,
                    "executor_app_name": _EXECUTOR_APP,
                }
            ),
        ): Operation.AGENT_RUNTIMES_WRITE,
        ("delete", f"{_RUNTIMES_URL}/{agent_name}", None): Operation.AGENT_RUNTIMES_WRITE,
    }

    for (method, url, body), operation in expected.items():
        authorizer = DenyingAuthorizer()
        set_authorizer(authorizer)
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["content"] = body
            kwargs["headers"] = {"Content-Type": "application/json"}
        getattr(client, method)(url, **kwargs)
        assert authorizer.seen == [operation], f"{method.upper()} {url}"


# ---------------------------------------------------------------------------
# Which session: the token minted at creation
# ---------------------------------------------------------------------------


def _machine_app() -> FastAPI:
    """A stand-in for the Phase 5 and 6 machine-side routes.

    Same shape as the endpoints section 10 specifies - the session key in the
    path, plucked into the authorization context exactly as ``_exchange_context``
    plucks the target out of the exchange body - so what is under test is the
    token binding and the provider, not a route that does not exist yet.
    """

    def session_context(request: Request) -> dict[str, Any]:
        return {
            "target_type": RUNTIME_TOKEN_TARGET_TYPE,
            "target_id": request.path_params.get("session_key"),
        }

    machine_app = FastAPI()

    @machine_app.post("/agent-sessions/{session_key}/nudges/claim")
    async def claim(
        session_key: str,
        principal: Principal = Depends(
            require_operation(
                Operation.AGENT_NUDGES_CONSUME, context_builder=session_context
            )
        ),
    ) -> dict[str, Any]:
        return {
            "session_key": session_key,
            "namespace_key": principal.namespace_key,
            "target_id": principal.target_id,
        }

    @machine_app.get("/agent-sessions/{session_key}/anything")
    async def anything(
        session_key: str,
        principal: Principal = Depends(
            require_operation(
                Operation.RUNTIME_USE, context_builder=session_context
            )
        ),
    ) -> dict[str, Any]:
        del principal
        return {"session_key": session_key}

    return machine_app


@pytest.fixture()
def runtime_auth() -> Any:
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    yield
    set_runtime_auth_config(None)


def _session_token(session_key: str, *, namespace_key: str = "default") -> str:
    minted = mint_session_runtime_token(
        namespace_key=namespace_key,
        session_key=session_key,
        actor_id="0123456789abcdef",
    )
    assert minted is not None
    return minted[0]


def test_a_session_token_opens_its_own_session_and_no_other(runtime_auth: None) -> None:
    """The claim the whole machine-side design rests on.

    A token seeded into an executor's session state is handed to a process that
    runs arbitrary agent code and can be prompt-injected. If it could name a
    different session, one conversation's compromise would be every
    conversation's.
    """
    session_a = uuid.uuid4().hex
    session_b = uuid.uuid4().hex
    token = _session_token(session_a)

    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    machine = TestClient(_machine_app(), raise_server_exceptions=True)
    headers = {"Authorization": f"Bearer {token}"}

    own = machine.post(f"/agent-sessions/{session_a}/nudges/claim", headers=headers)
    assert own.status_code == 200, own.text
    assert own.json()["target_id"] == session_a

    other = machine.post(f"/agent-sessions/{session_b}/nudges/claim", headers=headers)
    assert other.status_code == 403, other.text
    # This app registers no error handlers, so the body is FastAPI's default
    # shape rather than the server's typed one. The refusal is the assertion.
    assert "target_id" in other.text


def test_a_session_token_is_useless_for_anything_it_was_not_scoped_to(
    runtime_auth: None,
) -> None:
    """Two session scopes and nothing else - notably not ``runtime.use``."""
    session_key = uuid.uuid4().hex
    token = _session_token(session_key)

    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET), operation=Operation.RUNTIME_USE
    )
    machine = TestClient(_machine_app(), raise_server_exceptions=True)
    resp = machine.get(
        f"/agent-sessions/{session_key}/anything",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403, resp.text
    assert "runtime.use" in resp.json()["detail"]


def test_a_missing_or_forged_token_is_rejected(runtime_auth: None) -> None:
    session_key = uuid.uuid4().hex
    forged = _session_token(session_key)

    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    machine = TestClient(_machine_app(), raise_server_exceptions=True)
    url = f"/agent-sessions/{session_key}/nudges/claim"

    assert machine.post(url).status_code == 401
    assert machine.post(url, headers={"Authorization": forged}).status_code == 401
    assert (
        machine.post(
            url, headers={"Authorization": f"Bearer {forged[:-4]}beef"}
        ).status_code
        == 401
    )


def test_the_token_carries_the_minting_namespace_not_the_callers(
    runtime_auth: None,
) -> None:
    """A namespace-alpha session token resolves as alpha wherever it is used."""
    session_key = uuid.uuid4().hex
    token = _session_token(session_key, namespace_key="alpha")

    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    machine = TestClient(_machine_app(), raise_server_exceptions=True)
    resp = machine.post(
        f"/agent-sessions/{session_key}/nudges/claim",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["namespace_key"] == "alpha"


def test_the_default_authorizer_refuses_the_machine_operations_to_a_plain_key(
    runtime_auth: None,
) -> None:
    """The fallback when no runtime secret is configured must fail closed.

    With no JWT override installed these two operations land on the default
    authorizer, where they are ADMIN. A non-admin key reaching them would be
    able to claim - and therefore silently swallow - another caller's nudges.
    """
    from .conftest import TEST_ADMIN_API_KEY, TEST_API_KEY

    session_key = uuid.uuid4().hex
    set_authorizer(HeaderAuthProvider())
    machine = TestClient(_machine_app(), raise_server_exceptions=True)
    url = f"/agent-sessions/{session_key}/nudges/claim"

    assert machine.post(url, headers={"X-API-Key": TEST_API_KEY}).status_code == 403
    assert machine.post(url, headers={"X-API-Key": TEST_ADMIN_API_KEY}).status_code == 200
