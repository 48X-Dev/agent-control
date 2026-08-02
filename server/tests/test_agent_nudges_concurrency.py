"""Two executors draining one queue, driven concurrently.

The plan's reason for ``FOR UPDATE SKIP LOCKED`` is two processes serving one
agent, and the failure it prevents is not slowness: it is the same sentence
being handed to two model calls and counted once, or handed to neither.

``TestClient`` serializes requests, so it cannot express this at all. These run
over real sockets against a real server, and they assert the outcome rather
than the mechanism: whatever order the database picks, no nudge is delivered
twice while it is still leased, and no nudge is skipped.
"""

from __future__ import annotations

import asyncio
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
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from sqlalchemy import text

from .conftest import TEST_ADMIN_API_KEY, LiveServer
from .test_agent_sessions_endpoints import fake_executor  # noqa: F401 - fixture

_SESSIONS_URL = "/api/v1/agent-sessions"
_RUNTIMES_URL = "/api/v1/agent-runtimes"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"

pytestmark = pytest.mark.usefixtures("fake_executor")


@pytest.fixture(autouse=True)
def _runtime_auth(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(executor_settings, "enabled", True)
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    yield
    set_runtime_auth_config(None)


def _token(session_key: str) -> str:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    return str(minted[0])


async def _open_session(client: httpx.AsyncClient) -> str:
    agent_name = f"agent-drain-{id(client) & 0xFFFF:x}"
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
        json={"base_url": "http://agent-executor:8080", "executor_app_name": "my_agent"},
    )
    assert bound.status_code == 200, bound.text
    opened = await client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert opened.status_code == 200, opened.text
    return str(opened.json()["session"]["session_key"])


async def test_concurrent_claims_on_one_session_never_overlap(
    live_server: LiveServer, db_engine: Any
) -> None:
    """Six queued nudges, four boundaries firing at once, no sentence twice.

    Delivering one nudge to two model calls is the harmless half of
    at-least-once, but only once its lease has lapsed and the row has been
    counted. Two *live* claims returning the same row is the other thing: a
    duplicate nobody chose, with one ``claim_count`` covering two deliveries.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session_key = await _open_session(client)

    queued: list[int] = []
    for index in range(6):
        created = await client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges",
            json={"body": f"guidance {index}"},
        )
        assert created.status_code == 200, created.text
        queued.append(int(created.json()["nudge"]["id"]))

    headers = {"Authorization": f"Bearer {_token(session_key)}"}
    responses = await asyncio.gather(
        *(
            client.post(
                f"{_SESSIONS_URL}/{session_key}/nudges/claim",
                json={"max_nudges": 3},
                headers=headers,
            )
            for _ in range(4)
        )
    )

    batches: list[list[int]] = []
    for response in responses:
        assert response.status_code == 200, response.text
        batches.append([int(item["id"]) for item in response.json()["nudges"]])

    delivered = [nudge_id for batch in batches for nudge_id in batch]
    assert len(delivered) == len(set(delivered)), (
        f"one nudge went to two live claims: {batches}"
    )
    assert set(delivered) <= set(queued)
    assert len(delivered) == 6, "four boundaries drain the whole queue between them"

    with db_engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, status, claim_count, injection_attempts "
                "  FROM agent_session_nudges ORDER BY id"
            )
        ).all()
    assert [row.status for row in rows] == ["claimed"] * 6
    assert [row.claim_count for row in rows] == [1] * 6, (
        "one claim each, so the counter still means what it says"
    )
    assert [row.injection_attempts for row in rows] == [0] * 6


async def test_concurrent_claims_across_sessions_stay_in_their_own_session(
    live_server: LiveServer,
) -> None:
    """Two sessions drained at once, each token seeing only its own queue.

    Worth asserting under concurrency and not only in isolation: the session
    binding is enforced by a token comparison in one request's authorization,
    and a claim that filtered by anything process-wide would pass every serial
    test and cross the streams here.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    first = await _open_session(client)
    second = await _open_session(client)

    async def queue(session_key: str, label: str) -> set[int]:
        ids: set[int] = set()
        for index in range(3):
            created = await client.post(
                f"{_SESSIONS_URL}/{session_key}/nudges",
                json={"body": f"{label} {index}"},
            )
            assert created.status_code == 200, created.text
            ids.add(int(created.json()["nudge"]["id"]))
        return ids

    first_ids = await queue(first, "first")
    second_ids = await queue(second, "second")

    first_claim, second_claim = await asyncio.gather(
        client.post(
            f"{_SESSIONS_URL}/{first}/nudges/claim",
            json={"max_nudges": 3},
            headers={"Authorization": f"Bearer {_token(first)}"},
        ),
        client.post(
            f"{_SESSIONS_URL}/{second}/nudges/claim",
            json={"max_nudges": 3},
            headers={"Authorization": f"Bearer {_token(second)}"},
        ),
    )

    assert {int(item["id"]) for item in first_claim.json()["nudges"]} == first_ids
    assert {int(item["id"]) for item in second_claim.json()["nudges"]} == second_ids
    for item in first_claim.json()["nudges"]:
        assert item["body"].startswith("first ")
    for item in second_claim.json()["nudges"]:
        assert item["body"].startswith("second ")
