"""Authorization for stopping an agent: which tier, which operation, which session.

Three separate questions, and passing one says nothing about the others.

*Which tier.* ``agent_halts.write`` is AUTHENTICATED, deliberately, because run
at AUTHENTICATED and stop at ADMIN is the one pairing that cannot be defended:
whoever can start a turn that spends money must be able to stop it. Nothing
else pins that, so a one-line relaxation to ADMIN would remove the stop button
for every non-admin key and the rest of the suite would stay green.

*Which operation.* An admin key satisfies every tier at once, so an admin-only
test cannot tell ``agent_halts.write`` from ``agent_sessions.content_read``.
The restricted authorizer refuses exactly one and allows the rest.

*Which session.* The machine side is guarded by a session-bound runtime token
under ``agent_nudges.consume`` - the same operation as the nudge claim, on
purpose, because halts ride that call at the model boundary and a separate
operation would document a boundary that does not exist. Revoking one and
keeping the other is not a state a deployment can reach, and this file is what
says so.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RUNTIME_TOKEN_BOUND_OPERATIONS,
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import LocalJwtVerifyProvider
from agent_control_server.auth_framework.providers.header import (
    DEFAULT_OPERATION_ACCESS,
    AccessLevel,
)
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .test_agent_halts_endpoints import (
    _create,
    _fresh_quota,  # noqa: F401 - fixture
    _session,
    _start_turn,
)
from .test_agent_sessions_auth import DenyingAuthorizer
from .test_agent_sessions_endpoints import (
    _agent_name,
    _bind,
    _open_session,
    _register_agent,
    executor_enabled,  # noqa: F401 - fixture
    fake_executor,  # noqa: F401 - fixture
)

_SESSIONS_URL = "/api/v1/agent-sessions"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"

pytestmark = pytest.mark.usefixtures("executor_enabled", "fake_executor")


# ---------------------------------------------------------------------------
# Which tier
# ---------------------------------------------------------------------------


def test_stopping_a_turn_sits_at_the_same_tier_as_starting_one() -> None:
    assert DEFAULT_OPERATION_ACCESS[Operation.AGENT_HALTS_WRITE] is (
        AccessLevel.AUTHENTICATED
    )
    assert (
        DEFAULT_OPERATION_ACCESS[Operation.AGENT_SESSIONS_RUN]
        is DEFAULT_OPERATION_ACCESS[Operation.AGENT_HALTS_WRITE]
    )


def test_there_is_no_separate_operation_guarding_halt_delivery() -> None:
    """One token, one session binding, one boundary decision, one operation.

    A second operation would be revocable on its own, and revoking it would
    disable halts at the model boundary while leaving them working at the tool
    boundary - which reads to an operator as "stop sometimes doesn't work".
    """
    names = {operation.value for operation in Operation}
    assert "agent_halts.consume" not in names
    assert Operation.AGENT_NUDGES_CONSUME in RUNTIME_TOKEN_BOUND_OPERATIONS


def test_a_non_admin_key_can_stop_the_turn_it_started(
    client: TestClient, non_admin_client: TestClient, db_engine: Any
) -> None:
    """AUTHENTICATED here is the feature, not a leniency.

    Binding the agent to an executor stays ADMIN in the same walk-through, so
    the split is visible in one test: deployment configuration is admin work,
    holding a conversation is not.
    """
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    denied = non_admin_client.put(
        "/api/v1/agent-runtimes/" + agent_name,
        json={"base_url": "http://agent-executor:8080", "executor_app_name": "my_agent"},
    )
    assert denied.status_code == 403, denied.text

    session_key = str(_open_session(non_admin_client, agent_name)["session_key"])
    _start_turn(db_engine, session_key)

    stopped = _create(non_admin_client, session_key)

    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["halt"]["status"] == "pending"
    assert (
        non_admin_client.get(f"{_SESSIONS_URL}/{session_key}/halts").status_code == 200
    )


# ---------------------------------------------------------------------------
# Which operation
# ---------------------------------------------------------------------------


def test_denying_the_write_leaves_the_read_alone_and_the_other_way_round(
    client: TestClient, db_engine: Any
) -> None:
    """Reads sit at ``content_read`` for one field.

    ``applied_tool_name`` names a tool the agent was about to run. Handing an
    agent's tool inventory to a caller who was refused the conversation is the
    small disclosure the content split exists to prevent.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_HALTS_WRITE))
    assert _create(client, session_key).status_code == 403
    assert client.get(f"{_SESSIONS_URL}/{session_key}/halts").status_code == 200

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSION_CONTENT_READ))
    assert client.get(f"{_SESSIONS_URL}/{session_key}/halts").status_code == 403
    assert _create(client, session_key).status_code == 200


def test_no_halt_or_nudge_route_asks_for_an_operation_it_does_not_need(
    client: TestClient, db_engine: Any
) -> None:
    """Each route consults exactly one operation, and it is the documented one.

    Catches the failure a tier test cannot: a handler that guards the right
    operation and also drags in another, which quietly narrows who can use it.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    expected = {
        ("post", f"{_SESSIONS_URL}/{session_key}/halts", "{}"): (
            Operation.AGENT_HALTS_WRITE
        ),
        ("get", f"{_SESSIONS_URL}/{session_key}/halts", None): (
            Operation.AGENT_SESSION_CONTENT_READ
        ),
        (
            "post",
            f"{_SESSIONS_URL}/{session_key}/halts/claim",
            json.dumps({"boundary": "model"}),
        ): Operation.AGENT_NUDGES_CONSUME,
        (
            "post",
            f"{_SESSIONS_URL}/{session_key}/halts/ack",
            json.dumps({"id": 1}),
        ): Operation.AGENT_NUDGES_CONSUME,
        (
            "post",
            f"{_SESSIONS_URL}/{session_key}/nudges",
            json.dumps({"body": "x"}),
        ): Operation.AGENT_NUDGES_WRITE,
        ("get", f"{_SESSIONS_URL}/{session_key}/nudges", None): (
            Operation.AGENT_SESSION_CONTENT_READ
        ),
        ("delete", f"{_SESSIONS_URL}/{session_key}/nudges/1", None): (
            Operation.AGENT_NUDGES_WRITE
        ),
        (
            "post",
            f"{_SESSIONS_URL}/{session_key}/nudges/claim",
            json.dumps({"max_nudges": 1}),
        ): Operation.AGENT_NUDGES_CONSUME,
        (
            "post",
            f"{_SESSIONS_URL}/{session_key}/nudges/ack",
            json.dumps({"acks": []}),
        ): Operation.AGENT_NUDGES_CONSUME,
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


def test_denying_the_consume_operation_closes_delivery_and_nothing_else(
    client: TestClient, db_engine: Any
) -> None:
    """A deployment that revokes the machine credential loses delivery only.

    The human half keeps working, which is the honest degradation: a person can
    still press stop and still see that it is queued, and the panel is not
    telling them it landed.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)

    set_authorizer(DenyingAuthorizer(Operation.AGENT_NUDGES_CONSUME))
    assert (
        client.post(
            f"{_SESSIONS_URL}/{session_key}/halts/claim", json={"boundary": "model"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges/claim", json={"max_nudges": 1}
        ).status_code
        == 403
    )
    assert client.get(f"{_SESSIONS_URL}/{session_key}/halts").status_code == 200
    assert _create(client, session_key).status_code == 200


# ---------------------------------------------------------------------------
# Which session
# ---------------------------------------------------------------------------


@pytest.fixture()
def runtime_auth() -> Any:
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    yield
    set_runtime_auth_config(None)


def _token(session_key: str, *, namespace_key: str = "default") -> str:
    minted = mint_session_runtime_token(
        namespace_key=namespace_key,
        session_key=session_key,
        actor_id="0123456789abcdef",
    )
    assert minted is not None
    return str(minted[0])


def test_an_api_key_alone_cannot_claim_a_halt_once_runtime_auth_is_configured(
    client: TestClient, db_engine: Any, runtime_auth: None
) -> None:
    """The whole point of routing consume through the token provider.

    Without it, any authenticated key in the namespace could apply and swallow
    stops for sessions it has nothing to do with, and the operator who pressed
    the button would be shown "applied" for a stop nobody acted on.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)

    refused = client.post(
        f"{_SESSIONS_URL}/{session_key}/halts/claim", json={"boundary": "model"}
    )

    assert refused.status_code == 401, refused.text
    listed = client.get(f"{_SESSIONS_URL}/{session_key}/halts").json()["halts"]
    assert listed[0]["status"] == "pending"


def test_a_token_for_one_session_cannot_claim_or_acknowledge_another(
    app: FastAPI, client: TestClient, db_engine: Any, runtime_auth: None
) -> None:
    mine = _session(client)
    theirs = _session(client)
    _start_turn(db_engine, theirs)
    created = _create(client, theirs).json()["halt"]

    machine = TestClient(app, raise_server_exceptions=True)
    wrong = {"Authorization": f"Bearer {_token(mine)}"}

    assert (
        machine.post(
            f"{_SESSIONS_URL}/{theirs}/halts/claim",
            json={"boundary": "model"},
            headers=wrong,
        ).status_code
        == 403
    )
    assert (
        machine.post(
            f"{_SESSIONS_URL}/{theirs}/halts/ack",
            json={"id": created["id"], "applied_tool_name": "send_email"},
            headers=wrong,
        ).status_code
        == 403
    )
    assert (
        machine.post(
            f"{_SESSIONS_URL}/{theirs}/nudges/claim",
            json={"max_nudges": 1},
            headers=wrong,
        ).status_code
        == 403
    )

    listed = client.get(f"{_SESSIONS_URL}/{theirs}/halts").json()["halts"]
    assert listed[0]["status"] == "pending"
    assert listed[0]["applied_tool_name"] is None


def test_a_forged_or_missing_token_reaches_no_machine_route(
    app: FastAPI, client: TestClient, db_engine: Any, runtime_auth: None
) -> None:
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)
    machine = TestClient(app, raise_server_exceptions=True)
    url = f"{_SESSIONS_URL}/{session_key}/halts/claim"

    assert machine.post(url, json={"boundary": "model"}).status_code == 401
    assert (
        machine.post(
            url,
            json={"boundary": "model"},
            headers={"Authorization": "Bearer not-a-token"},
        ).status_code
        == 401
    )
    assert (
        machine.post(
            url,
            json={"boundary": "model"},
            # A valid token for this session, presented without the scheme.
            headers={"Authorization": _token(session_key)},
        ).status_code
        == 401
    )

    listed = client.get(f"{_SESSIONS_URL}/{session_key}/halts").json()["halts"]
    assert listed[0]["status"] == "pending"
