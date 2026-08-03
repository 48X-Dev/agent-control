"""The ceilings and the stop levels, driven concurrently and over a real socket.

``TestClient`` serializes requests, so the sibling file
``test_agent_dispatch_ceilings.py`` can only prove these controls one request at
a time. Every property here is one that a serialized test cannot express, and in
two cases the serialized version has to poke a database column to stand in for a
turn that is genuinely running.

**The budget is atomic.** A read-then-write budget passes every serialized test
its author writes and then lets six dispatch turns arriving together all read
"under the ceiling" and all spend. Six real requests, in flight at the same
instant, against a ceiling of three: exactly three succeed, exactly three are
refused, and the counter afterwards is three rather than six.

**Per-agent concurrency is real concurrency.** The sibling test writes
``in_flight_trace_id`` by hand because it cannot hold a turn open. Here the fake
executor blocks inside ``run``, so the first turn is genuinely mid-flight when
the second arrives, which is the only version of that test that would notice the
marker being written after the check instead of before it.

**A stop stops the *next* turn, not the one already running.** Levels 1 and 3
are refusals on the way in. The copy beside both buttons says running turns keep
running and that stopping those is level 2, which is a request. Proving that
needs a turn that is actually running while the button is pressed, and both
tests below press it in the middle of one.

**A fleet stop is bounded by the namespace.** Live turns in two namespaces at
once, a stop in one, and the assertion that the other namespace's live turns got
nothing - proved by the absence of any halt row against them, not by a count.

Nothing here asserts an implementation detail. Every refusal is provoked by an
ordinary ``POST /turns`` or an ordinary press of a documented button.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from agent_control_server.auth_framework import set_authorizer
from agent_control_server.config import dispatch_settings, executor_settings
from agent_control_server.services.executor_factory import get_executor_client_factory
from agent_control_server.services.turn_quota import reset_turn_quota

from .conftest import TEST_ADMIN_API_KEY, LiveServer, LiveServerFactory
from .test_agent_session_turns import FakeTurnExecutorFactory
from .test_agent_sessions_endpoints import HeaderNamespaceAuthorizer

DISPATCH_URL = "/api/v1/agent-dispatch"
SESSIONS_URL = "/api/v1/agent-sessions"
TASKS_URL = "/api/v1/agent-tasks"
RUNTIMES_URL = "/api/v1/agent-runtimes"

_EXECUTOR_BASE_URL = "http://agent-executor:8080"
_EXECUTOR_APP = "my_agent"

_GATE_TIMEOUT = 5.0
"""How long a test waits for the fake executor to report it is inside a turn.
Generous for a loopback socket, and short enough that a turn which never
arrives fails the test rather than hanging the suite."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_turn_quota() -> Iterator[None]:
    """The per-process chat quota is thirty a minute and these tests burst.

    Nothing here is about that quota - it is the ceiling on *human* chat and the
    whole reason the dispatch budget had to be a row instead. Resetting it keeps
    a burst in one test from refusing a turn in the next one for a reason no
    assertion in this file is about.
    """
    reset_turn_quota()
    yield
    reset_turn_quota()


@pytest.fixture()
def executor_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor_settings, "enabled", True)


@pytest.fixture()
def fake_executor(app: FastAPI) -> Iterator[FakeTurnExecutorFactory]:
    factory = FakeTurnExecutorFactory()
    app.dependency_overrides[get_executor_client_factory] = lambda: factory
    yield factory
    app.dependency_overrides.pop(get_executor_client_factory, None)


@pytest.fixture()
async def live(live_server_factory: LiveServerFactory, app: FastAPI) -> LiveServer:
    return await live_server_factory(app)


@pytest.fixture()
async def api(live: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    client = live.client(headers={"X-API-Key": TEST_ADMIN_API_KEY})
    yield client


# ---------------------------------------------------------------------------
# Helpers. Each is one documented call; nothing here reaches into the service.
# ---------------------------------------------------------------------------


def _ref(prefix: str = "conc") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def _bound_agent(api: httpx.AsyncClient) -> str:
    agent = f"agent-{uuid.uuid4().hex[:12]}"
    registered = await api.post(
        "/api/v1/agents/initAgent",
        json={
            "agent": {
                "agent_name": agent,
                "agent_description": "test agent",
                "agent_version": "1.0",
            },
            "steps": [],
        },
    )
    assert registered.status_code == 200, registered.text
    bound = await api.put(
        f"{RUNTIMES_URL}/{agent}",
        json={"base_url": _EXECUTOR_BASE_URL, "executor_app_name": _EXECUTOR_APP},
    )
    assert bound.status_code == 200, bound.text
    return agent


async def _claimed_task(api: httpx.AsyncClient) -> str:
    """Import one item and claim it, which is what a dispatcher does."""
    scope = {
        "kind": "items",
        "source_kind": "file",
        "items": [{"source_ref": (ref := _ref()), "title": ref}],
    }
    preview = await api.post(f"{TASKS_URL}/import", json={"scope": scope, "mode": "preview"})
    assert preview.status_code == 200, preview.text
    committed = await api.post(
        f"{TASKS_URL}/import",
        json={
            "scope": scope,
            "mode": "commit",
            "expected_refs_digest": preview.json()["refs_digest"],
        },
    )
    assert committed.status_code == 200, committed.text
    key = str(committed.json()["task_keys"][0])
    claimed = await api.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    assert claimed.status_code == 200, claimed.text
    return key


async def _session(
    api: httpx.AsyncClient, agent: str, *, task_key: str | None = None
) -> str:
    body: dict[str, Any] = {"agent_name": agent}
    if task_key is not None:
        body["task_key"] = task_key
    resp = await api.post(SESSIONS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["session"]["session_key"])


async def _dispatch_session(api: httpx.AsyncClient, agent: str) -> str:
    return await _session(api, agent, task_key=await _claimed_task(api))


async def _turn(api: httpx.AsyncClient, session_key: str) -> httpx.Response:
    return await api.post(
        f"{SESSIONS_URL}/{session_key}/turns",
        json={"message": "go"},
        timeout=httpx.Timeout(30.0),
    )


async def _hold_a_turn_open(
    api: httpx.AsyncClient, session_key: str, executor: FakeTurnExecutorFactory
) -> asyncio.Task[httpx.Response]:
    """Start a turn and return once it is genuinely inside the executor call.

    The gate is what makes "while a turn is running" a real state rather than a
    column somebody wrote. The caller sets the gate and is responsible for
    releasing it.
    """
    started = asyncio.create_task(_turn(api, session_key))
    await asyncio.wait_for(executor.entered.wait(), _GATE_TIMEOUT)
    executor.entered.clear()
    return started


def _counter(db_engine: Any, namespace_key: str = "default") -> int | None:
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT turns_in_window FROM agent_dispatch_state WHERE namespace_key = :ns"
            ),
            {"ns": namespace_key},
        ).first()
    return None if row is None else int(row.turns_in_window)


def _halted_session_keys(db_engine: Any) -> set[str]:
    """Which sessions have a halt bound to them, by session key.

    Read back through the join rather than by counting rows, because the claim
    under test is about *which* sessions were reached and a count cannot say
    that a namespace was left alone.
    """
    with db_engine.connect() as conn:
        return {
            str(row.session_key)
            for row in conn.execute(
                text(
                    "SELECT s.session_key FROM agent_session_halts h "
                    "  JOIN agent_sessions s ON s.id = h.session_id"
                )
            )
        }


# ---------------------------------------------------------------------------
# The budget, under genuine concurrency
# ---------------------------------------------------------------------------


async def test_the_hourly_ceiling_holds_when_every_turn_arrives_at_once(
    api: httpx.AsyncClient,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Six dispatch turns at the same instant against a ceiling of three.

    This is the failure the whole module was written in one statement to
    prevent, and it is invisible to a serialized test: a read-then-write budget
    lets all six read "under the ceiling" and all six spend. Six *different*
    agents, because the per-agent ceiling of one would otherwise arbitrate this
    before the budget ever got the chance.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 3)

    sessions = [await _dispatch_session(api, await _bound_agent(api)) for _ in range(6)]
    responses = await asyncio.gather(*(_turn(api, key) for key in sessions))

    allowed = [r for r in responses if r.status_code == 200]
    refused = [r for r in responses if r.status_code == 429]
    assert len(allowed) == 3, [r.status_code for r in responses]
    assert len(refused) == 3, [r.status_code for r in responses]
    assert {r.json()["error_code"] for r in refused} == {"DISPATCH_BUDGET_EXCEEDED"}

    assert len(fake_executor.runs) == 3, "a refused turn must never reach an executor"
    assert _counter(db_engine) == 3, "three turns happened, so three were billed"


async def test_a_burst_of_human_chat_does_not_touch_the_fleet_budget(
    api: httpx.AsyncClient,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The bound on the hot row, asserted under the load that would break it.

    The budget is allowed to be one row per namespace only because human chat
    never writes to it. Five simultaneous chat turns against a ceiling of one
    all succeed, and the row is not even created - so the reasoning that made a
    single row acceptable still holds under concurrency and not merely in a
    sequence.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 1)

    agent = await _bound_agent(api)
    chats = [await _session(api, agent) for _ in range(5)]
    responses = await asyncio.gather(*(_turn(api, key) for key in chats))

    assert [r.status_code for r in responses] == [200] * 5
    assert _counter(db_engine) is None


# ---------------------------------------------------------------------------
# Per-agent concurrency, with a turn genuinely in flight
# ---------------------------------------------------------------------------


async def test_a_second_dispatch_turn_on_one_agent_is_refused_while_the_first_runs(
    api: httpx.AsyncClient,
    db_engine: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """One executor process per agent, and one invocation in it at a time.

    The first turn is blocked *inside* the executor call when the second
    arrives, so this is the real overlap rather than a marker written by the
    test. The refusal must also cost nothing: the charge runs before the
    concurrency check, so a bug that committed it would bill a turn that never
    started.
    """
    agent = await _bound_agent(api)
    busy = await _dispatch_session(api, agent)
    waiting = await _dispatch_session(api, agent)

    fake_executor.gate = asyncio.Event()
    running = await _hold_a_turn_open(api, busy, fake_executor)

    refused = await _turn(api, waiting)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "AGENT_CONCURRENCY_EXCEEDED"

    fake_executor.gate.set()
    first = await asyncio.wait_for(running, _GATE_TIMEOUT)
    assert first.status_code == 200, first.text

    assert len(fake_executor.runs) == 1
    assert _counter(db_engine) == 1, "the refused turn must not have been billed"


async def test_a_turn_refused_after_the_charge_leaves_the_counter_where_it_was(
    api: httpx.AsyncClient,
    db_engine: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The furthest a refusal can travel and still be a turn that never started.

    The per-agent count excludes the session doing the asking, so a second turn
    on a session that is *already* running one gets past the charge and is
    refused by the turn lock instead - which is the one refusal that happens
    after the counter has been moved. It has to unwind, and a serialized test
    cannot produce it at all, because the session has to be genuinely busy.
    """
    agent = await _bound_agent(api)
    session = await _dispatch_session(api, agent)

    fake_executor.gate = asyncio.Event()
    running = await _hold_a_turn_open(api, session, fake_executor)
    assert _counter(db_engine) == 1

    refused = await _turn(api, session)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "TURN_IN_FLIGHT"
    assert _counter(db_engine) == 1, "the second charge was rolled back with its turn"

    fake_executor.gate.set()
    assert (await asyncio.wait_for(running, _GATE_TIMEOUT)).status_code == 200


async def test_a_turn_the_executor_refused_is_still_billed(
    api: httpx.AsyncClient,
    db_engine: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The other side of "a turn that did not start is not billed".

    This one did start. It reached the executor, the executor answered, and
    whatever the agent spent getting there is spent whether the answer was an
    essay or a refusal. A budget that only counted successes would be a budget a
    failing agent could sit under indefinitely, retrying, all night.
    """
    from agent_control_server.services.executor_client import ExecutorModelUnavailableError

    agent = await _bound_agent(api)
    session = await _dispatch_session(api, agent)
    fake_executor.run_error = ExecutorModelUnavailableError("upstream said no")

    rejected = await _turn(api, session)
    assert rejected.status_code == 502, rejected.text
    assert rejected.json()["error_code"] == "EXECUTOR_REJECTED"
    assert _counter(db_engine) == 1


async def test_a_different_agent_is_not_blocked_by_a_live_turn_elsewhere(
    api: httpx.AsyncClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The ceiling protects one process, so it is per agent and not per fleet.

    Written as a separate test from the one above because the two would pass for
    different reasons: a ceiling accidentally applied namespace-wide would still
    refuse the second turn there, and only here would anybody notice.
    """
    busy_agent = await _bound_agent(api)
    other_agent = await _bound_agent(api)
    busy = await _dispatch_session(api, busy_agent)
    other = await _dispatch_session(api, other_agent)

    fake_executor.gate = asyncio.Event()
    running = await _hold_a_turn_open(api, busy, fake_executor)

    second = asyncio.create_task(_turn(api, other))
    await asyncio.wait_for(fake_executor.entered.wait(), _GATE_TIMEOUT)

    fake_executor.gate.set()
    responses = await asyncio.wait_for(asyncio.gather(running, second), _GATE_TIMEOUT)
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]


# ---------------------------------------------------------------------------
# Level 1: a pause stops the next turn and not the one already running
# ---------------------------------------------------------------------------


async def test_a_turn_that_started_before_the_pause_runs_to_completion(
    api: httpx.AsyncClient,
    db_engine: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Level 1 is a refusal on the way in, and its copy says exactly that.

    "Running turns are not stopped by a pause; stopping those is a halt" is the
    hint the refusal itself carries. A pause pressed while a turn is inside the
    executor must therefore leave that turn alone, and must refuse the next one
    for the same agent. Both halves are asserted here, because a pause that
    killed the turn in flight would be a different and much more alarming
    control than the one described.
    """
    agent = await _bound_agent(api)
    running_session = await _dispatch_session(api, agent)
    next_session = await _dispatch_session(api, agent)

    fake_executor.gate = asyncio.Event()
    running = await _hold_a_turn_open(api, running_session, fake_executor)

    paused = await api.post(f"{DISPATCH_URL}/pause", json={"reason": "incident 9"})
    assert paused.status_code == 200, paused.text

    fake_executor.gate.set()
    finished = await asyncio.wait_for(running, _GATE_TIMEOUT)
    assert finished.status_code == 200, finished.text
    assert finished.json()["messages"], "the paused-over turn still returned its answer"
    assert len(fake_executor.runs) == 1

    refused = await _turn(api, next_session)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "DISPATCH_PAUSED"
    assert "incident 9" in refused.json()["detail"]
    assert _counter(db_engine) == 1, "only the turn that ran was billed"


# ---------------------------------------------------------------------------
# Level 3: the kill switch, pressed mid-turn
# ---------------------------------------------------------------------------


async def test_the_kill_switch_pressed_mid_turn_does_not_kill_that_turn(
    api: httpx.AsyncClient,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Level 3 refuses every *new* session and every *new* turn. That is all.

    The sentence the console has to print beside the button is that turns
    already running are not stopped by it - that is level 2, and level 2 is a
    request. An operator who believes level 3 stops what is running will not
    reach for level 4 when they need to, so the limit is asserted rather than
    left to the docstring.
    """
    agent = await _bound_agent(api)
    chat = await _session(api, agent)

    fake_executor.gate = asyncio.Event()
    running = await _hold_a_turn_open(api, chat, fake_executor)

    halted = await api.post(f"{DISPATCH_URL}/halt-executors", json={"reason": "runaway"})
    assert halted.status_code == 200, halted.text

    fake_executor.gate.set()
    finished = await asyncio.wait_for(running, _GATE_TIMEOUT)
    assert finished.status_code == 200, finished.text

    # And now everything new is refused, human chat included.
    refused = await _turn(api, chat)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "EXECUTORS_HALTED"
    blocked = await api.post(SESSIONS_URL, json={"agent_name": agent})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error_code"] == "EXECUTORS_HALTED"


# ---------------------------------------------------------------------------
# Level 2: the fleet stop, and the namespace boundary
# ---------------------------------------------------------------------------


async def test_the_fleet_stop_reaches_every_live_turn_in_its_namespace_and_no_other(
    live: LiveServer,
    db_engine: Any,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """Two namespaces with turns genuinely in flight, and a stop in one of them.

    The absence is the assertion that matters. Beta's live turns must have *no*
    halt row bound to them, checked by reading back which sessions were reached
    rather than by trusting a count in the response, because a count of two is
    equally consistent with the statement having reached the wrong two.

    The stopped turns still finish, which is the honest property of level 2: it
    binds a request that lands at the executor's next boundary, and nothing here
    stops a tool that is already executing.
    """
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = live.client(headers={"X-Test-Namespace": "alpha"})
    beta = live.client(headers={"X-Test-Namespace": "beta"})

    alpha_fleet = await _dispatch_session(alpha, await _bound_agent(alpha))
    alpha_chat = await _session(alpha, await _bound_agent(alpha))
    beta_fleet = await _dispatch_session(beta, await _bound_agent(beta))

    fake_executor.gate = asyncio.Event()
    live_turns = [
        await _hold_a_turn_open(client, key, fake_executor)
        for client, key in (
            (alpha, alpha_fleet),
            (alpha, alpha_chat),
            (beta, beta_fleet),
        )
    ]

    stopped = await alpha.post(f"{DISPATCH_URL}/halt-fleet")
    assert stopped.status_code == 200, stopped.text
    body = stopped.json()
    assert body["sessions_in_flight"] == 2
    assert body["halts_created"] == 2
    assert body["dispatch_sessions_in_flight"] == 1

    assert _halted_session_keys(db_engine) == {alpha_fleet, alpha_chat}

    fake_executor.gate.set()
    finished = await asyncio.wait_for(asyncio.gather(*live_turns), _GATE_TIMEOUT)
    assert [r.status_code for r in finished] == [200, 200, 200], [
        r.text for r in finished
    ]


async def test_a_halted_namespace_does_not_refuse_another_namespaces_turn(
    live: LiveServer,
    executor_enabled: None,
    fake_executor: FakeTurnExecutorFactory,
) -> None:
    """The authoritative stop is authoritative over one namespace.

    Pressed in alpha while beta has a turn in flight: beta's turn finishes and
    beta may start another one. A kill switch that leaked would take down every
    tenant in the deployment, which is the failure nobody would discover until
    the first incident.
    """
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = live.client(headers={"X-Test-Namespace": "alpha"})
    beta = live.client(headers={"X-Test-Namespace": "beta"})

    beta_agent = await _bound_agent(beta)
    beta_session = await _dispatch_session(beta, beta_agent)

    fake_executor.gate = asyncio.Event()
    running = await _hold_a_turn_open(beta, beta_session, fake_executor)

    assert (await alpha.post(f"{DISPATCH_URL}/halt-executors", json={})).status_code == 200

    fake_executor.gate.set()
    assert (await asyncio.wait_for(running, _GATE_TIMEOUT)).status_code == 200

    fake_executor.gate = None
    assert (await _turn(beta, beta_session)).status_code == 200
    assert (await beta.post(SESSIONS_URL, json={"agent_name": beta_agent})).status_code == 200
