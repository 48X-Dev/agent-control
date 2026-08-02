"""Coverage for ``POST /agent-sessions/{session_key}/turns``.

Phase 2's whole deliverable is a lock and a set of exits, and neither is
visible from a passing happy path. What is asserted here, one rule per test:

* a turn's trace id reaches the executor as per-turn state and comes back on
  the response, and the session records it as the last turn that ended;
* the 504 exit clears the lock and **keeps** the liveness marker, because the
  invocation did not stop, and the next turn is accepted anyway;
* an executor refusal is a typed 502 and an unreachable executor a typed 503,
  and in neither case does anything the executor said reach the caller;
* a second turn on a busy session is a 409, a lock older than the staleness
  window is reclaimed, and a late release from a reclaimed turn cannot clear
  its successor's lock;
* the per-credential quota answers 429;
* content scoping and namespace isolation apply to running a turn exactly as
  they apply to reading the transcript.

The concurrency and pool assertions run on the ``live_server`` fixture, per the
plan's rule: ``TestClient`` serializes requests and buffers responses, so two
turns racing on one session is not a thing it can express.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from agent_control_server.config import executor_settings
from agent_control_server.db import AsyncSessionLocal, async_engine
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.services.executor_client import (
    EXECUTOR_MODEL_UNAVAILABLE_MESSAGE,
    EXECUTOR_REJECTED_MESSAGE,
    EXECUTOR_TURN_TIMEOUT_MESSAGE,
    EXECUTOR_UNREACHABLE_MESSAGE,
    ExecutorMessage,
    ExecutorMessagePart,
    ExecutorModelUnavailableError,
    ExecutorRejectedError,
    ExecutorSession,
    ExecutorTurn,
    ExecutorTurnTimeoutError,
    ExecutorUnavailableError,
)
from agent_control_server.services.executor_factory import get_executor_client_factory
from agent_control_server.services.turn_locks import (
    acquire_turn_lock,
    new_trace_id,
    release_turn_lock,
)
from agent_control_server.services.turn_quota import reset_turn_quota
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from .conftest import TEST_ADMIN_API_KEY
from .test_agent_sessions_auth import DenyingAuthorizer
from .test_agent_sessions_endpoints import (
    HeaderNamespaceAuthorizer,
    _namespace_client,
)

_RUNTIMES_URL = "/api/v1/agent-runtimes"
_SESSIONS_URL = "/api/v1/agent-sessions"
_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_EXECUTOR_APP = "my_agent"

_LEAKY_UPSTREAM_TEXT = (
    "Traceback (most recent call last): tool raised at /srv/agent/secrets.py"
)


# ---------------------------------------------------------------------------
# Fake executor, with a turn
# ---------------------------------------------------------------------------


class FakeTurnExecutorClient:
    """Answers session CRUD from memory and a turn from whatever is configured."""

    def __init__(self, backend: FakeTurnExecutorFactory) -> None:
        self._backend = backend

    async def create_session(
        self, *, app_name: str, user_id: str, session_id: str, state: Any
    ) -> ExecutorSession:
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
        return self._backend.sessions[(app_name, user_id, session_id)]

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        self._backend.sessions.pop((app_name, user_id, session_id), None)

    async def run(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        message: str,
        state_delta: Any = None,
        timeout_seconds: float,
    ) -> ExecutorTurn:
        self._backend.runs.append(
            {
                "app_name": app_name,
                "user_id": user_id,
                "session_id": session_id,
                "message": message,
                "state_delta": dict(state_delta or {}),
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._backend.gate is not None:
            self._backend.entered.set()
            await self._backend.gate.wait()
        if self._backend.run_error is not None:
            raise self._backend.run_error
        return ExecutorTurn(messages=self._backend.turn_messages)

    async def health(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FakeTurnExecutorFactory:
    """Hands out :class:`FakeTurnExecutorClient` and owns the shared state."""

    def __init__(self) -> None:
        self.sessions: dict[tuple[str, str, str], ExecutorSession] = {}
        self.runs: list[dict[str, Any]] = []
        self.run_error: Exception | None = None
        self.client_error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.turn_messages: tuple[ExecutorMessage, ...] = (
            ExecutorMessage(
                role="agent",
                author="my_agent",
                timestamp=dt.datetime.now(tz=dt.UTC),
                parts=(ExecutorMessagePart(kind="text", text="Hello back."),),
            ),
        )

    def client_for(
        self, *, executor_kind: str, base_url: str
    ) -> FakeTurnExecutorClient:
        del executor_kind, base_url
        if self.client_error is not None:
            raise self.client_error
        return FakeTurnExecutorClient(self)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_quota() -> None:
    """One test's turns never count against the next one's ceiling."""
    reset_turn_quota()
    yield
    reset_turn_quota()


@pytest.fixture()
def executor_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)


@pytest.fixture()
def fake_executor(app: FastAPI) -> Any:
    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


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


def _bound_session(client: TestClient) -> dict[str, Any]:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    resp = client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 200, resp.text
    return resp.json()["session"]


def _turn(client: TestClient, session_key: str, message: str = "Hello") -> Any:
    return client.post(
        f"{_SESSIONS_URL}/{session_key}/turns", json={"message": message}
    )


def _session_row(db_engine: Any, session_key: str) -> Any:
    with db_engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT id, in_flight_since, in_flight_trace_id, last_trace_id "
                "  FROM agent_sessions WHERE session_key = :key"
            ),
            {"key": session_key},
        ).one()


def _age_lock(db_engine: Any, session_key: str, *, seconds: float) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions "
                "   SET in_flight_since = now() - (:secs * interval '1 second'), "
                "       in_flight_trace_id = :trace "
                " WHERE session_key = :key"
            ),
            {"key": session_key, "secs": seconds, "trace": "held-by-somebody-else"},
        )


# ---------------------------------------------------------------------------
# The happy path, and what it writes
# ---------------------------------------------------------------------------


def test_turn_returns_messages_and_seeds_its_trace(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    session = _bound_session(client)

    resp = _turn(client, session["session_key"], "What is the status?")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["session_key"] == session["session_key"]
    assert body["trace_id"]
    assert body["duration_seconds"] >= 0
    assert [part["text"] for part in body["messages"][0]["parts"]] == ["Hello back."]
    # Turn-relative, as the model documents.
    assert body["messages"][0]["index"] == 0

    (call,) = fake_executor.runs
    assert call["message"] == "What is the status?"
    assert call["state_delta"]["agent_control_turn"]["trace_id"] == body["trace_id"]
    assert call["timeout_seconds"] == executor_settings.turn_timeout_seconds

    row = _session_row(db_engine, session["session_key"])
    assert row.in_flight_since is None
    assert row.in_flight_trace_id is None
    assert row.last_trace_id == body["trace_id"]

    detail = client.get(f"{_SESSIONS_URL}/{session['session_key']}").json()["session"]
    assert detail["in_flight_since"] is None
    assert detail["in_flight_trace_id"] is None
    assert detail["last_trace_id"] == body["trace_id"]


def test_a_blocked_model_call_is_an_ordinary_completed_turn(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """A guardrail block arrives as model output, not as an error."""
    fake_executor.turn_messages = (
        ExecutorMessage(
            role="agent",
            parts=(
                ExecutorMessagePart(
                    kind="text", text="Blocked by policy: no outbound email."
                ),
            ),
        ),
    )
    session = _bound_session(client)

    resp = _turn(client, session["session_key"])
    assert resp.status_code == 200, resp.text
    assert "Blocked by policy" in resp.json()["messages"][0]["parts"][0]["text"]


# ---------------------------------------------------------------------------
# The two exits
# ---------------------------------------------------------------------------


def test_timeout_is_504_keeps_the_liveness_marker_and_unblocks_the_session(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    session = _bound_session(client)
    fake_executor.run_error = ExecutorTurnTimeoutError(EXECUTOR_TURN_TIMEOUT_MESSAGE)

    resp = _turn(client, session["session_key"])
    assert resp.status_code == 504, resp.text
    body = resp.json()
    assert body["error_code"] == "EXECUTOR_UNAVAILABLE"
    # The one 5xx detail that has to survive sanitization, because "we stopped
    # waiting and it is still running" is not the same sentence as "it is down".
    assert body["detail"] == EXECUTOR_TURN_TIMEOUT_MESSAGE

    row = _session_row(db_engine, session["session_key"])
    assert row.in_flight_since is None, "the lock must release so the caller is free"
    assert row.in_flight_trace_id is not None, "the invocation did not stop"
    assert row.last_trace_id is None, "a turn that did not end is not the last one"

    detail = client.get(f"{_SESSIONS_URL}/{session['session_key']}").json()["session"]
    assert detail["in_flight_since"] is None
    assert detail["in_flight_trace_id"] == row.in_flight_trace_id

    # And the session takes another turn straight away.
    fake_executor.run_error = None
    assert _turn(client, session["session_key"]).status_code == 200


def test_executor_refusal_and_unreachability_are_typed_and_leak_nothing(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    session = _bound_session(client)

    fake_executor.run_error = ExecutorModelUnavailableError(
        EXECUTOR_MODEL_UNAVAILABLE_MESSAGE
    )
    rejected = _turn(client, session["session_key"])
    assert rejected.status_code == 502, rejected.text
    assert rejected.json()["error_code"] == "EXECUTOR_REJECTED"
    assert rejected.json()["detail"] == EXECUTOR_MODEL_UNAVAILABLE_MESSAGE

    # A turn that ended in a refusal really ended: both columns clear.
    row = _session_row(db_engine, session["session_key"])
    assert row.in_flight_since is None and row.in_flight_trace_id is None

    fake_executor.run_error = ExecutorUnavailableError(_LEAKY_UPSTREAM_TEXT)
    unreachable = _turn(client, session["session_key"])
    assert unreachable.status_code == 503, unreachable.text
    assert unreachable.json()["error_code"] == "EXECUTOR_UNAVAILABLE"
    # Anything not written as a literal in ``executor_client`` is replaced.
    assert _LEAKY_UPSTREAM_TEXT not in unreachable.text
    assert "secrets.py" not in unreachable.text


def test_unsupported_executor_kind_is_typed_not_a_500(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    """Resolving the client is inside the mapped region, like the call itself.

    A binding whose kind this server has no implementation for is a deployment
    fault, and it has to arrive as a 503 rather than as an unhandled exception.
    It also has to leave the session clean: nothing ran, so advertising a live
    invocation afterwards would make the stop control appear for a turn that
    never started.
    """
    session = _bound_session(client)
    fake_executor.client_error = ExecutorUnavailableError(
        "This server has no client for the executor kind configured for this "
        "agent. Rebind the agent, or upgrade the server."
    )

    resp = _turn(client, session["session_key"])
    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "EXECUTOR_UNAVAILABLE"

    row = _session_row(db_engine, session["session_key"])
    assert row.in_flight_since is None
    assert row.in_flight_trace_id is None


# ---------------------------------------------------------------------------
# The lock
# ---------------------------------------------------------------------------


def test_a_second_turn_on_a_busy_session_is_409(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    session = _bound_session(client)
    _age_lock(db_engine, session["session_key"], seconds=1)

    resp = _turn(client, session["session_key"])
    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "TURN_IN_FLIGHT"
    assert fake_executor.runs == [], "a refused turn must not reach the executor"

    # The refusal left the incumbent's lock exactly as it found it.
    row = _session_row(db_engine, session["session_key"])
    assert row.in_flight_trace_id == "held-by-somebody-else"


def test_the_metrics_endpoint_names_no_namespace_and_no_agent(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """``/metrics`` is served without a credential dependency.

    So nothing written from a namespace-scoped, authorized route may become a
    label on it. The health probe is the one path that knows agent names, which
    makes it the one that could publish them.
    """
    session = _bound_session(client)
    assert client.get(f"{_SESSIONS_URL}/executor-health").status_code == 200
    assert _turn(client, session["session_key"]).status_code == 200

    metrics = client.get("/metrics")
    assert metrics.status_code == 200, metrics.text
    assert session["agent_name"] not in metrics.text
    assert "namespace_key=" not in metrics.text
    assert "agent_control_server_executor_probes_total" in metrics.text


def test_a_lock_older_than_the_window_is_reclaimed(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    session = _bound_session(client)
    _age_lock(
        db_engine,
        session["session_key"],
        seconds=executor_settings.turn_stale_after_seconds + 60,
    )

    resp = _turn(client, session["session_key"])
    assert resp.status_code == 200, resp.text
    row = _session_row(db_engine, session["session_key"])
    assert row.last_trace_id == resp.json()["trace_id"]


async def test_a_reclaimed_turns_late_release_leaves_the_successor_alone(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    """The fence, which is the reason the release carries a trace at all."""
    session = _bound_session(client)
    session_key = session["session_key"]

    first_trace = new_trace_id()
    second_trace = new_trace_id()

    async with AsyncSessionLocal() as db:
        session_id = await acquire_turn_lock(
            db,
            namespace_key="default",
            session_key=session_key,
            trace_id=first_trace,
            stale_after_seconds=0.0,
        )
        await db.commit()
    assert session_id is not None

    # A successor reclaims the lock while the first turn is still running.
    async with AsyncSessionLocal() as db:
        reclaimed = await acquire_turn_lock(
            db,
            namespace_key="default",
            session_key=session_key,
            trace_id=second_trace,
            stale_after_seconds=0.0,
        )
        await db.commit()
    assert reclaimed == session_id

    # The first turn now finishes and cleans up after itself.
    await release_turn_lock(
        session_id=session_id,
        namespace_key="default",
        trace_id=first_trace,
        turn_ended=True,
    )

    row = _session_row(db_engine, session_key)
    assert row.in_flight_trace_id == second_trace, "the successor still holds it"
    assert row.in_flight_since is not None
    assert row.last_trace_id is None, "the loser does not get to record the last turn"

    # And the successor's own release does land.
    await release_turn_lock(
        session_id=session_id,
        namespace_key="default",
        trace_id=second_trace,
        turn_ended=True,
    )
    cleared = _session_row(db_engine, session_key)
    assert cleared.in_flight_since is None
    assert cleared.in_flight_trace_id is None
    assert cleared.last_trace_id == second_trace


# ---------------------------------------------------------------------------
# Quota, scoping, isolation
# ---------------------------------------------------------------------------


def test_the_per_credential_quota_answers_429(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(executor_settings, "max_turns_per_minute", 1)
    reset_turn_quota()
    session = _bound_session(client)

    assert _turn(client, session["session_key"]).status_code == 200
    refused = _turn(client, session["session_key"])
    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "QUOTA_EXCEEDED"
    # Refused before the lock, so the session is not left holding one.
    assert len(fake_executor.runs) == 1


def test_running_a_turn_is_scoped_to_the_caller_who_opened_the_session(
    client: TestClient,
    non_admin_client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    session = _bound_session(client)

    denied = _turn(non_admin_client, session["session_key"])
    assert denied.status_code == 403, denied.text
    assert fake_executor.runs == []
    # Metadata stays readable; only the content-bearing routes are scoped.
    assert (
        non_admin_client.get(f"{_SESSIONS_URL}/{session['session_key']}").status_code
        == 200
    )


def test_a_turn_on_an_unknown_session_is_404(
    client: TestClient, executor_enabled: None, fake_executor: FakeTurnExecutorFactory
) -> None:
    resp = _turn(client, uuid.uuid4().hex)
    assert resp.status_code == 404, resp.text
    assert fake_executor.runs == []


def test_a_turn_needs_the_feature_switched_on(
    client: TestClient, fake_executor: FakeTurnExecutorFactory
) -> None:
    """Without ``executor_enabled`` the route answers 503, never a 500."""
    resp = client.post(f"{_SESSIONS_URL}/{uuid.uuid4().hex}/turns", json={"message": "x"})
    assert resp.status_code == 503, resp.text
    assert resp.json()["error_code"] == "EXECUTOR_UNAVAILABLE"


@pytest.mark.parametrize("message", ["", "x" * 16001])
def test_turn_message_bounds_are_enforced(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    message: str,
) -> None:
    session = _bound_session(client)
    resp = _turn(client, session["session_key"], message)
    assert resp.status_code == 422, resp.text
    assert fake_executor.runs == []


def test_turn_request_forbids_unknown_fields(
    client: TestClient, executor_enabled: None, fake_executor: FakeTurnExecutorFactory
) -> None:
    session = _bound_session(client)
    resp = client.post(
        f"{_SESSIONS_URL}/{session['session_key']}/turns",
        json={"message": "hi", "executor_session_id": "somebody-elses"},
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Concurrency, on a real socket
#
# ``TestClient`` runs one request at a time, so neither of these is expressible
# through it.
# ---------------------------------------------------------------------------


async def _live_setup(
    live: httpx.AsyncClient, fake: FakeTurnExecutorFactory
) -> dict[str, Any]:
    agent_name = _agent_name()
    resp = await live.post(
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
    resp = await live.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert resp.status_code == 200, resp.text
    resp = await live.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert resp.status_code == 200, resp.text
    del fake
    return resp.json()["session"]


async def test_two_turns_racing_on_one_session_yield_one_200_and_one_409(
    live_server: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    live = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session = await _live_setup(live, fake_executor)
    fake_executor.gate = asyncio.Event()

    first = asyncio.create_task(
        live.post(
            f"{_SESSIONS_URL}/{session['session_key']}/turns", json={"message": "one"}
        )
    )
    await asyncio.wait_for(fake_executor.entered.wait(), timeout=5)
    second = await live.post(
        f"{_SESSIONS_URL}/{session['session_key']}/turns", json={"message": "two"}
    )
    fake_executor.gate.set()
    first_response = await asyncio.wait_for(first, timeout=10)

    assert sorted([first_response.status_code, second.status_code]) == [200, 409]
    assert second.json()["error_code"] == "TURN_IN_FLIGHT"
    assert len(fake_executor.runs) == 1, "the refused turn never reached the executor"


async def test_concurrent_turns_hold_no_database_connections(
    live_server: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The rule that keeps a chat from starving policy evaluation.

    A turn can last minutes and the pool is five plus ten overflow, so a
    handler that held its connection across the executor call would take the
    process down at a dozen concurrent chats. Three turns are parked inside the
    executor call at once here, and the pool must look exactly as it did idle.
    """
    live = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    sessions = [await _live_setup(live, fake_executor) for _ in range(3)]
    fake_executor.gate = asyncio.Event()

    baseline = async_engine.sync_engine.pool.checkedout()
    turns = [
        asyncio.create_task(
            live.post(
                f"{_SESSIONS_URL}/{session['session_key']}/turns",
                json={"message": "hold"},
            )
        )
        for session in sessions
    ]
    await asyncio.wait_for(fake_executor.entered.wait(), timeout=5)
    while len(fake_executor.runs) < 3:
        await asyncio.sleep(0.02)

    assert async_engine.sync_engine.pool.checkedout() == baseline

    fake_executor.gate.set()
    responses = await asyncio.wait_for(asyncio.gather(*turns), timeout=15)
    assert [response.status_code for response in responses] == [200, 200, 200]


# ---------------------------------------------------------------------------
# The abort path
#
# Closing a tab mid-answer is ordinary behaviour, and the symptom of getting it
# wrong ("I can't send another message") looks nothing like its cause. Both
# tests below drive a real cancellation rather than asserting that a ``shield``
# appears in the source: the first through a socket the client hangs up on, the
# second by cancelling the handler task itself.
# ---------------------------------------------------------------------------


async def _await_lock_release(
    db_engine: Any, session_key: str, *, timeout: float = 10.0
) -> Any:
    """Wait for the shielded cleanup to land, which outlives its handler.

    Polling rather than awaiting anything: the whole point of the shield is
    that the cleanup is a task nobody is holding a reference to, so there is
    nothing to await and the database is the only observer.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        row = _session_row(db_engine, session_key)
        if row.in_flight_since is None:
            return row
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(
                "The turn lock was never released after the client hung up. "
                "The session is stuck until the staleness window expires and "
                "every turn until then is a 409."
            )
        await asyncio.sleep(0.05)


async def test_a_client_hanging_up_does_not_end_the_turn_and_does_not_wedge_it(
    live_server: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    """A real socket, closed while the executor still holds the turn.

    Two things are true here and both are worth pinning, because the obvious
    guess about each is wrong.

    Hanging up does **not** stop the turn. uvicorn marks the connection gone
    and lets the handler run to completion, so the model keeps being called and
    the lock keeps being held. That is the behaviour Phase 3's cancel button
    promises in writing ("the turn keeps running server-side"), and it is why a
    second turn in this window is a 409 rather than a 200 - the session really
    is busy, and answering otherwise would put two invocations on one
    conversation.

    And it does not wedge the session either. Whether the handler is cancelled
    or merely abandoned, the lock is cleared on the way out, so the session is
    free again the moment the turn it was actually running is over.
    """
    live = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session = await _live_setup(live, fake_executor)
    session_key = session["session_key"]
    fake_executor.gate = asyncio.Event()

    abandoned = asyncio.create_task(
        live.post(f"{_SESSIONS_URL}/{session_key}/turns", json={"message": "wait"})
    )
    await asyncio.wait_for(fake_executor.entered.wait(), timeout=5)
    assert _session_row(db_engine, session_key).in_flight_since is not None

    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned

    # Still running, still locked, and honest about both.
    busy = await live.post(
        f"{_SESSIONS_URL}/{session_key}/turns", json={"message": "meanwhile"}
    )
    assert busy.status_code == 409, busy.text
    assert busy.json()["error_code"] == "TURN_IN_FLIGHT"
    assert len(fake_executor.runs) == 1

    # The turn the client walked away from finishes on its own.
    fake_executor.gate.set()
    row = await _await_lock_release(db_engine, session_key)
    assert row.in_flight_trace_id is None
    assert row.last_trace_id is not None, "it ran to the end, so it ended"

    fake_executor.entered.clear()
    nxt = await live.post(
        f"{_SESSIONS_URL}/{session_key}/turns", json={"message": "again"}
    )
    assert nxt.status_code == 200, nxt.text


async def test_a_cancelled_handler_still_releases_the_lock(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    """The shield itself, driven by cancelling the coroutine that owns it.

    The socket-level test above depends on the server noticing a dropped
    connection. This one removes that variable: the turn task is cancelled
    outright while parked in the executor call, which is the exact exception
    the ``finally`` has to survive.
    """
    from agent_control_server.services.agent_turns import run_turn

    session = _bound_session(client)
    session_key = session["session_key"]
    fake_executor.gate = asyncio.Event()

    turn = asyncio.create_task(
        run_turn(
            namespace_key="default",
            session_key=session_key,
            caller_hash=None,
            is_admin=True,
            message="hold the line",
            factory=fake_executor,
            settings=executor_settings,
        )
    )
    await asyncio.wait_for(fake_executor.entered.wait(), timeout=5)
    assert _session_row(db_engine, session_key).in_flight_since is not None

    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    row = await _await_lock_release(db_engine, session_key)
    assert row.in_flight_trace_id is not None
    assert row.last_trace_id is None


# ---------------------------------------------------------------------------
# Authorization: which operation, which namespace
# ---------------------------------------------------------------------------


def test_running_a_turn_asks_for_the_run_operation_and_nothing_else(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Section 6.1 splits ``run`` from ``write`` because a turn spends money.

    A route that guarded ``agent_sessions.write`` instead would pass every
    other test in this file, because an admin key satisfies both.
    """
    session = _bound_session(client)

    authorizer = DenyingAuthorizer()
    set_authorizer(authorizer)
    _turn(client, session["session_key"])
    assert authorizer.seen == [Operation.AGENT_SESSIONS_RUN]


def test_denying_the_run_operation_leaves_reading_and_writing_alone(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    session = _bound_session(client)
    session_key = session["session_key"]

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSIONS_RUN))

    denied = _turn(client, session_key)
    assert denied.status_code == 403, denied.text
    assert fake_executor.runs == [], "a 403 must not have spent anything"

    assert client.get(f"{_SESSIONS_URL}/{session_key}").status_code == 200
    assert (
        client.patch(f"{_SESSIONS_URL}/{session_key}", json={"title": "x"}).status_code
        == 200
    )


def test_a_turn_cannot_be_started_on_another_namespaces_session(
    app: FastAPI,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    db_engine: Any,
) -> None:
    """A session key is unguessable, and it is still not a capability.

    ``executor_user_id`` is namespace-prefixed and the executor triple is
    globally unique precisely so one namespace cannot address another's
    conversation. The route has to refuse before any of that matters, and it
    has to refuse as a 404: telling namespace B that this key exists somewhere
    is itself the disclosure.
    """
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")

    session = _bound_session(alpha)
    session_key = session["session_key"]

    denied = _turn(beta, session_key)
    assert denied.status_code == 404, denied.text
    assert fake_executor.runs == [], "nothing may reach the executor"

    row = _session_row(db_engine, session_key)
    assert row.in_flight_since is None, "the refused turn took no lock"

    # And the owning namespace is unaffected by the attempt.
    assert _turn(alpha, session_key).status_code == 200


# ---------------------------------------------------------------------------
# Nothing the executor says reaches a caller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status", "code", "fallback"),
    [
        (
            ExecutorModelUnavailableError,
            502,
            "EXECUTOR_REJECTED",
            "The executor that runs this agent refused the request.",
        ),
        (
            ExecutorRejectedError,
            502,
            "EXECUTOR_REJECTED",
            "The executor that runs this agent refused the request.",
        ),
        (
            ExecutorUnavailableError,
            503,
            "EXECUTOR_UNAVAILABLE",
            "The executor that runs this agent is unavailable.",
        ),
        (
            ExecutorTurnTimeoutError,
            504,
            "EXECUTOR_UNAVAILABLE",
            "The executor that runs this agent is unavailable.",
        ),
    ],
)
def test_no_executor_failure_carries_upstream_bytes_to_the_caller(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    error: type[Exception],
    status: int,
    code: str,
    fallback: str,
) -> None:
    """The rule ``linear_client`` set: a proxy answers in its own words.

    An executor runs arbitrary agent code, so its error text can carry
    tracebacks, filesystem paths, tool exception text and model bodies that
    echo the prompt back. Every failure here is raised carrying exactly that,
    and what a caller gets is the generic sentence for the status instead.

    Which is the part worth pinning: the specific sentences that *do* survive
    are allowed through by exact match against a closed set of literals, so a
    message assembled from anything an executor said cannot reach a browser
    even when it is raised on a path whose constants are public.
    """
    session = _bound_session(client)
    fake_executor.run_error = error(_LEAKY_UPSTREAM_TEXT)

    resp = _turn(client, session["session_key"])
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error_code"] == code
    assert body["detail"] == fallback
    assert _LEAKY_UPSTREAM_TEXT not in resp.text
    assert "Traceback" not in resp.text
    assert "secrets.py" not in resp.text


@pytest.mark.parametrize(
    ("error", "public"),
    [
        (ExecutorTurnTimeoutError, EXECUTOR_TURN_TIMEOUT_MESSAGE),
        (ExecutorModelUnavailableError, EXECUTOR_MODEL_UNAVAILABLE_MESSAGE),
        (ExecutorUnavailableError, EXECUTOR_UNREACHABLE_MESSAGE),
        (ExecutorRejectedError, EXECUTOR_REJECTED_MESSAGE),
    ],
)
def test_the_sentences_this_codebase_wrote_do_reach_the_caller(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    error: type[Exception],
    public: str,
) -> None:
    """The other half of the allowlist, without which it is just a blanket.

    Flattening every executor 5xx into one sentence would be safe and useless:
    "we stopped waiting and your turn is still running" and "the process is
    down" need different reactions from the person reading them.
    """
    session = _bound_session(client)
    fake_executor.run_error = error(public)

    resp = _turn(client, session["session_key"])
    assert resp.json()["detail"] == public, resp.text


def test_the_quota_refusal_names_the_setting_and_not_the_credential(
    client: TestClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429 has to be actionable without echoing who was throttled.

    ``created_by_hash`` derives from ``caller_id``, which under the header
    provider is the first eight characters of a live API key. It is hashed on
    the way into the database and it has no business coming back out of an
    error body.
    """
    monkeypatch.setattr(executor_settings, "max_turns_per_minute", 1)
    reset_turn_quota()
    session = _bound_session(client)

    assert _turn(client, session["session_key"]).status_code == 200
    refused = _turn(client, session["session_key"])
    assert refused.status_code == 429, refused.text
    body = refused.json()
    assert "AGENT_CONTROL_EXECUTOR_MAX_TURNS_PER_MINUTE" in body["hint"]
    assert TEST_ADMIN_API_KEY not in refused.text
    assert TEST_ADMIN_API_KEY[:8] not in refused.text
