"""The race the halt design exists for, driven concurrently.

A stop is pressed at the instant its turn ends. Two transactions then touch
``agent_sessions`` and ``agent_session_halts`` at once: the claim, which reads
the session and applies the halt, and the turn's cleanup, which clears the lock
and stamps the halt. Both must take the session row first. Reverse either and
Postgres deadlocks, and the abort lands inside the one code path that
guarantees ``in_flight_since`` gets cleared - which is how a stop button leaves
a session wedged at 409 for the whole staleness window.

``TestClient`` cannot express this: it serializes requests. So the claims go
over a real socket against a real server while the cleanups run beside them,
and the assertions are about the two outcomes that must hold whichever order
the database picks:

* the lock is always cleared, and no cleanup reports having lost;
* the halt never ends up ``applied`` with the turn recorded as never having
  ended, and never ``pending`` after its turn is over.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import LocalJwtVerifyProvider
from agent_control_server.config import executor_settings
from agent_control_server.db import AsyncSessionLocal
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from agent_control_server.services.turn_locks import (
    acquire_turn_lock,
    new_trace_id,
    release_turn_lock,
)
from agent_control_server.services.turn_quota import reset_turn_quota
from sqlalchemy import text

from .conftest import TEST_ADMIN_API_KEY, LiveServer
from .test_agent_sessions_endpoints import fake_executor  # noqa: F401 - fixture

_SESSIONS_URL = "/api/v1/agent-sessions"
_RUNTIMES_URL = "/api/v1/agent-runtimes"
_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"
_SESSION_COUNT = 8

pytestmark = pytest.mark.usefixtures("fake_executor")


@pytest.fixture(autouse=True)
def _executor_and_quota(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(executor_settings, "enabled", True)
    reset_turn_quota()
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    yield
    set_runtime_auth_config(None)
    reset_turn_quota()


def _token(session_key: str) -> str:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    return str(minted[0])


async def _open_sessions(client: httpx.AsyncClient, count: int) -> list[str]:
    """Register one agent, bind it, and open ``count`` chats against it."""
    agent_name = f"agent-race-{id(client) & 0xFFFF:x}"
    registered = await client.post(
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
    assert registered.status_code == 200, registered.text
    bound = await client.put(
        f"{_RUNTIMES_URL}/{agent_name}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": "my_agent"},
    )
    assert bound.status_code == 200, bound.text

    keys: list[str] = []
    for _ in range(count):
        opened = await client.post(_SESSIONS_URL, json={"agent_name": agent_name})
        assert opened.status_code == 200, opened.text
        keys.append(str(opened.json()["session"]["session_key"]))
    return keys


async def test_a_claim_racing_its_own_turns_cleanup_never_wedges_the_session(
    live_server: LiveServer,
    db_engine: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session_keys = await _open_sessions(client, _SESSION_COUNT)

    traces: dict[str, str] = {}
    session_ids: dict[str, int] = {}
    for session_key in session_keys:
        trace = new_trace_id()
        async with AsyncSessionLocal() as db:
            session_id = await acquire_turn_lock(
                db,
                namespace_key="default",
                session_key=session_key,
                trace_id=trace,
                stale_after_seconds=600.0,
            )
            await db.commit()
        assert session_id is not None
        traces[session_key] = trace
        session_ids[session_key] = session_id

        created = await client.post(f"{_SESSIONS_URL}/{session_key}/halts", json={})
        assert created.status_code == 200, created.text

    async def claim(session_key: str) -> httpx.Response:
        return await client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges/claim",
            json={"max_nudges": 3},
            headers={"Authorization": f"Bearer {_token(session_key)}"},
        )

    async def cleanup(session_key: str) -> None:
        await release_turn_lock(
            session_id=session_ids[session_key],
            namespace_key="default",
            trace_id=traces[session_key],
            turn_ended=True,
        )

    with caplog.at_level(logging.ERROR):
        results = await asyncio.gather(
            *(
                coro
                for session_key in session_keys
                for coro in (claim(session_key), cleanup(session_key))
            )
        )

    for result in results:
        if isinstance(result, httpx.Response):
            assert result.status_code == 200, result.text

    assert "Could not clear turn state" not in caplog.text, (
        "a deadlock escaping the cleanup is how a session stays locked for the "
        "whole staleness window"
    )

    with db_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT s.session_key, s.in_flight_since, s.in_flight_trace_id, "
                "       h.status, h.turn_ended_at "
                "  FROM agent_sessions s "
                "  JOIN agent_session_halts h ON h.session_id = s.id "
                " WHERE s.session_key = ANY(:keys)"
            ),
            {"keys": session_keys},
        ).all()

    assert len(rows) == _SESSION_COUNT
    for row in rows:
        assert row.in_flight_since is None, "the lock is always cleared"
        assert row.in_flight_trace_id is None
        assert row.status in {"applied", "expired"}, "never left mid-flight"
        assert row.turn_ended_at is not None, (
            "whichever order won, the server saw this turn end"
        )


async def test_a_halt_created_as_its_turn_ends_never_leaks_into_the_next_one(
    live_server: LiveServer, db_engine: Any
) -> None:
    """Creation races the ending too, and the loser must not bind to nothing.

    Creation reads the live trace inside the insert rather than before it, so
    the two outcomes are "a halt bound to the turn that was running" and "409,
    nothing was running". A read-then-write would produce a third: a row bound
    to a turn that had already finished, which the next turn's claim would then
    have to be trusted not to pick up.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    (session_key,) = await _open_sessions(client, 1)

    for _ in range(6):
        trace = new_trace_id()
        async with AsyncSessionLocal() as db:
            session_id = await acquire_turn_lock(
                db,
                namespace_key="default",
                session_key=session_key,
                trace_id=trace,
                stale_after_seconds=600.0,
            )
            await db.commit()
        assert session_id is not None

        created, _ = await asyncio.gather(
            client.post(f"{_SESSIONS_URL}/{session_key}/halts", json={}),
            release_turn_lock(
                session_id=session_id,
                namespace_key="default",
                trace_id=trace,
                turn_ended=True,
            ),
        )
        assert created.status_code in (200, 409), created.text
        if created.status_code == 200:
            assert created.json()["halt"]["target_trace_id"] == trace

    listed = await client.get(f"{_SESSIONS_URL}/{session_key}/halts")
    assert listed.status_code == 200, listed.text
    for halt in listed.json()["halts"]:
        assert halt["status"] in {"applied", "expired"}
        assert halt["turn_ended_at"] is not None, (
            "a halt bound to a finished turn must not sit pending, waiting for "
            "a turn that will never come"
        )
