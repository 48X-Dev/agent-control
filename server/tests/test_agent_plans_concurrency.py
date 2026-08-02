"""The plan races, driven concurrently over a real socket.

``TestClient`` serializes requests, so it cannot express either of the two
races this resource was designed around. Both go over real sockets against a
real server here.

**Two declarations at once.** Revision numbers are allocated by reading the
maximum and adding one, which is a read-modify-write and therefore a lost
update waiting to happen. Without the row lock both transactions compute the
same next revision, one dies on the primary key, and an agent that replanned
twice quickly gets a 500 - a stack trace for behaviour that is entirely
legitimate.

**A step update racing the replan it is about to be superseded by.** This is
the race the whole revision-in-the-path design exists for. Whichever order the
database picks, exactly one outcome is acceptable for each update: it landed on
the revision it *named*, or it was refused with a 409. What must never happen
is a step of the new plan carrying a mark that was made about the old one.
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
_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"

pytestmark = pytest.mark.usefixtures("fake_executor")


@pytest.fixture(autouse=True)
def _runtime_auth(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(executor_settings, "enabled", True)
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_PLANS_WRITE,
    )
    yield
    set_runtime_auth_config(None)


def _auth(session_key: str) -> dict[str, str]:
    minted = mint_session_runtime_token(
        namespace_key="default", session_key=session_key, actor_id="0123456789abcdef"
    )
    assert minted is not None
    return {"Authorization": f"Bearer {minted[0]}"}


async def _open_session(client: httpx.AsyncClient, suffix: str) -> str:
    agent_name = f"agent-plan-race-{suffix}"
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
    opened = await client.post(_SESSIONS_URL, json={"agent_name": agent_name})
    assert opened.status_code == 200, opened.text
    return str(opened.json()["session"]["session_key"])


def _plan_rows(db_engine: Any, session_key: str) -> list[tuple[Any, ...]]:
    with db_engine.begin() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT p.plan_revision, p.step_index, p.title, p.status "
                    "  FROM agent_session_plan_steps p "
                    "  JOIN agent_sessions s ON s.id = p.session_id "
                    "   AND s.namespace_key = p.namespace_key "
                    " WHERE s.session_key = :key "
                    " ORDER BY p.plan_revision, p.step_index"
                ),
                {"key": session_key},
            ).fetchall()
        )


async def test_eight_declarations_at_once_produce_eight_distinct_revisions(
    live_server: LiveServer, db_engine: Any
) -> None:
    """No 500, no lost revision, and no two plans sharing a number.

    An agent replanning under load is ordinary behaviour, and the read-modify-
    write that allocates the revision is only safe because the session row is
    locked first. Without the lock this fails as a primary key violation
    surfacing to the agent as a server error.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session_key = await _open_session(client, "declare")
    headers = _auth(session_key)

    async def declare(n: int) -> httpx.Response:
        return await client.put(
            f"{_SESSIONS_URL}/{session_key}/plan",
            json={"steps": [f"plan {n} step 0", f"plan {n} step 1"]},
            headers=headers,
        )

    responses = await asyncio.gather(*(declare(n) for n in range(8)))

    assert [r.status_code for r in responses] == [200] * 8, [r.text for r in responses]
    revisions = sorted(r.json()["plan"]["revision"] for r in responses)
    assert revisions == [1, 2, 3, 4, 5, 6, 7, 8], (
        "each declaration must get a revision of its own; a repeat means two "
        "transactions read the same maximum"
    )

    stored = {row[0] for row in _plan_rows(db_engine, session_key)}
    assert stored == set(revisions)

    # And the read settles on the highest, with a count that matches.
    read = await client.get(f"{_SESSIONS_URL}/{session_key}/plan")
    assert read.status_code == 200, read.text
    plan = read.json()["plan"]
    assert plan["revision"] == 8
    assert plan["revision_count"] == 8


async def test_a_replan_racing_step_updates_never_marks_a_step_of_the_new_plan(
    live_server: LiveServer, db_engine: Any
) -> None:
    """The race the revision-in-the-path exists to make unambiguous.

    Two waves. The first is a genuine race: six updates naming revision 1 go
    out beside a replan, some land before it and some after, and the server
    decides which. Both answers are correct there, so the assertion is the
    invariant that holds either way.

    The second wave removes the timing luck. It is fired *after* the replan has
    returned, so every one of those updates is unambiguously stale, and the
    refusal is no longer something a fast machine could skip past.

    The invariant across both: **every step of revision 2 is still pending**. A
    mark that crossed the replan would be a tick against work the agent never
    claimed to have done under this plan.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session_key = await _open_session(client, "replan")
    headers = _auth(session_key)

    first = await client.put(
        f"{_SESSIONS_URL}/{session_key}/plan",
        json={"steps": [f"old {i}" for i in range(6)]},
        headers=headers,
    )
    assert first.status_code == 200, first.text

    async def mark(index: int) -> httpx.Response:
        return await client.patch(
            f"{_SESSIONS_URL}/{session_key}/plan/revisions/1/steps/{index}",
            json={"status": "done"},
            headers=headers,
        )

    async def replan() -> httpx.Response:
        return await client.put(
            f"{_SESSIONS_URL}/{session_key}/plan",
            json={"steps": [f"new {i}" for i in range(6)]},
            headers=headers,
        )

    results = await asyncio.gather(*(mark(i) for i in range(6)), replan())
    *marks, replanned = results

    assert replanned.status_code == 200, replanned.text
    assert replanned.json()["plan"]["revision"] == 2
    # Every update either landed on the revision it named or was told the plan
    # moved. Nothing else is an acceptable answer, and in particular no 500.
    assert {m.status_code for m in marks} <= {200, 409}
    for refused in (m for m in marks if m.status_code == 409):
        assert refused.json()["error_code"] == "PLAN_REVISION_STALE"

    rows = _plan_rows(db_engine, session_key)
    new_statuses = {row[3] for row in rows if row[0] == 2}
    assert new_statuses == {"pending"}, (
        "a mark made about revision 1 must never appear on revision 2"
    )
    # The marks that did land are on the old revision, where they belong.
    landed = sum(1 for m in marks if m.status_code == 200)
    assert sum(1 for row in rows if row[0] == 1 and row[3] == "done") == landed

    # Second wave: unambiguously after the replan, so every one is stale.
    late = await asyncio.gather(*(mark(i) for i in range(6)))

    assert [r.status_code for r in late] == [409] * 6, [r.text for r in late]
    assert {r.json()["error_code"] for r in late} == {"PLAN_REVISION_STALE"}
    after = _plan_rows(db_engine, session_key)
    assert {row[3] for row in after if row[0] == 2} == {"pending"}
    assert after == rows, "a refused update writes nothing, under load as well"


async def test_two_updates_to_the_same_step_at_once_leave_one_coherent_answer(
    live_server: LiveServer, db_engine: Any
) -> None:
    """Concurrent marks of one step settle on one of the two, not on a blend.

    Both name a real revision and a real step, so both are legitimate; the last
    writer wins. What must not happen is a row carrying one call's status and
    the other's note, which is what a non-atomic read-modify-write of the two
    columns would produce.
    """
    client = live_server.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    session_key = await _open_session(client, "samestep")
    headers = _auth(session_key)
    await client.put(
        f"{_SESSIONS_URL}/{session_key}/plan",
        json={"steps": ["the only step"]},
        headers=headers,
    )

    async def mark(status: str, note: str) -> httpx.Response:
        return await client.patch(
            f"{_SESSIONS_URL}/{session_key}/plan/revisions/1/steps/0",
            json={"status": status, "note": note},
            headers=headers,
        )

    done, failed = await asyncio.gather(
        mark("done", "it worked"), mark("failed", "it did not")
    )

    assert done.status_code == 200, done.text
    assert failed.status_code == 200, failed.text

    with db_engine.begin() as conn:
        status, note = conn.execute(
            text(
                "SELECT p.status, p.note FROM agent_session_plan_steps p "
                "  JOIN agent_sessions s ON s.id = p.session_id "
                " WHERE s.session_key = :key"
            ),
            {"key": session_key},
        ).one()
    assert (status, note) in {("done", "it worked"), ("failed", "it did not")}
