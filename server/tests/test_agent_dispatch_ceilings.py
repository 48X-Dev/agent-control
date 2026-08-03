"""The fleet's ceilings and its stop levels, exercised on the paths that enforce them.

Phase 3 shipped a budget, a pause, a kill switch, a per-agent concurrency limit
and a set-based fleet halt, and none of them had a test. That is the worst thing
an untested module can be: every one of these is a control an operator will
believe during an incident, and a control nobody has watched refuse anything is
a control nobody knows refuses anything.

One rule per test, and each is written against the property the copy promises
rather than against the statement that implements it.

**The hourly turn ceiling is enforced on the turn path, not in the dispatcher.**
The dispatcher is not in this file at all: every refusal here is provoked by an
ordinary ``POST /turns``, which is exactly what a dispatcher in a retry loop, a
second dispatcher, or any holder of an ordinary key would send.

**A human chat turn is not charged against it**, because the whole reason the
budget can be a row is that it is only touched by fleet turns.

**The kill switch reaches human chat.** Its own error hint, the field
description on ``DispatchStateSnapshot`` and the copy beside the button all say
it refuses every new session *and every new turn* in the namespace. A chat
session opened before somebody pressed it is the case that makes that sentence
true or false, so it is asserted directly.

**A pause does not.** New dispatch work stops; an operator opening the console
to look at what happened still gets a session.

**Nothing crosses a namespace.** Every statement in the service is keyed on
``namespace_key``, and a stop in one namespace must be invisible from another.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent_control_server.auth_framework import set_authorizer
from agent_control_server.config import dispatch_settings, executor_settings

from .test_agent_session_turns import (  # noqa: F401 - fixtures
    FakeTurnExecutorFactory,
    _agent_name,
    _bind,
    _register_agent,
    executor_enabled,
    fake_executor,
)
from .test_agent_sessions_endpoints import (
    HeaderNamespaceAuthorizer,
    _namespace_client,
)

DISPATCH_URL = "/api/v1/agent-dispatch"
SESSIONS_URL = "/api/v1/agent-sessions"
TASKS_URL = "/api/v1/agent-tasks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ref(prefix: str = "ceil") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _import_tasks(client: TestClient, refs: list[str]) -> Any:
    """Preview then commit against the digest, which is the only accepted shape."""
    body: dict[str, Any] = {
        "scope": {
            "kind": "items",
            "source_kind": "file",
            "items": [{"source_ref": ref, "title": ref} for ref in refs],
        },
        "mode": "preview",
    }
    preview = client.post(f"{TASKS_URL}/import", json=body)
    if preview.status_code != 200:
        return preview
    body["mode"] = "commit"
    body["expected_refs_digest"] = preview.json()["refs_digest"]
    return client.post(f"{TASKS_URL}/import", json=body)


def _claimed_task(client: TestClient, ref: str | None = None) -> str:
    committed = _import_tasks(client, [ref or _ref()])
    assert committed.status_code == 200, committed.text
    key = str(committed.json()["task_keys"][0])
    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    assert claimed.status_code == 200, claimed.text
    return key


def _bound_agent(client: TestClient) -> str:
    agent = _agent_name()
    _register_agent(client, agent)
    _bind(client, agent)
    return agent


def _session(client: TestClient, agent: str, *, task_key: str | None = None) -> str:
    body: dict[str, Any] = {"agent_name": agent}
    if task_key is not None:
        body["task_key"] = task_key
    resp = client.post(SESSIONS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["session"]["session_key"])


def _dispatch_session(client: TestClient, agent: str) -> str:
    """A session the turn path will recognise as a fleet turn."""
    return _session(client, agent, task_key=_claimed_task(client))


def _turn(client: TestClient, session_key: str) -> Any:
    return client.post(f"{SESSIONS_URL}/{session_key}/turns", json={"message": "go"})


def _counter(db_engine: Any, namespace_key: str = "default") -> int | None:
    with db_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT turns_in_window FROM agent_dispatch_state "
                " WHERE namespace_key = :ns"
            ),
            {"ns": namespace_key},
        ).first()
    return None if row is None else int(row.turns_in_window)


def _mark_in_flight(db_engine: Any, session_key: str, *, trace: str) -> None:
    """Leave a live turn marker behind without holding a real turn open.

    ``TestClient`` serializes requests, so a genuinely concurrent turn is not
    something this file can express. What the per-agent ceiling actually reads
    is the marker, so the marker is what the test sets - and it sets
    ``in_flight_since`` too, because the staleness bound is part of the
    predicate rather than decoration.
    """
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions "
                "   SET in_flight_trace_id = :trace, in_flight_since = now(), "
                "       last_activity_at = now() "
                " WHERE session_key = :key"
            ),
            {"key": session_key, "trace": trace},
        )


@pytest.fixture()
def tiny_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two turns an hour, so the ceiling is reachable inside one test."""
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 2)


# ---------------------------------------------------------------------------
# The hourly turn ceiling
# ---------------------------------------------------------------------------


def test_the_hourly_ceiling_refuses_a_dispatch_turn_and_says_when_to_come_back(
    client: TestClient,
    db_engine: Any,
    tiny_budget: None,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    agent = _bound_agent(client)
    first = _dispatch_session(client, agent)
    second = _dispatch_session(client, agent)
    third = _dispatch_session(client, agent)

    assert _turn(client, first).status_code == 200
    assert _turn(client, second).status_code == 200
    assert _counter(db_engine) == 2

    refused = _turn(client, third)
    assert refused.status_code == 429, refused.text
    body = refused.json()
    assert body["error_code"] == "DISPATCH_BUDGET_EXCEEDED"

    # Section 11.4: the delay has to be machine readable in both places a
    # client might look, and the two come from one value.
    delay = body["details"]["retry_after_seconds"]
    assert delay > 0
    assert refused.headers["Retry-After"] == str(delay)

    assert len(fake_executor.runs) == 2, "the refused turn must not reach an executor"
    assert _counter(db_engine) == 2, "a turn that did not start must not be billed"


def test_a_human_chat_turn_is_never_charged_against_the_fleet_budget(
    client: TestClient,
    db_engine: Any,
    tiny_budget: None,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """The bound on the hot row, asserted rather than assumed.

    Three turns against a two-turn ceiling, all of them fine, because none of
    them belongs to a task. If this ever fails, the budget has become a write
    on every turn in the deployment and the reasoning that let it be a single
    row no longer holds.
    """
    agent = _bound_agent(client)
    chat = _session(client, agent)

    for _ in range(3):
        assert _turn(client, chat).status_code == 200

    assert _counter(db_engine) is None, "human chat must not even create the row"


def test_the_ceiling_is_enforced_from_the_row_not_from_the_deployment_default(
    client: TestClient,
    db_engine: Any,
    tiny_budget: None,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """A namespace that has dispatched keeps its own number.

    The settings seed the row on first use and the row is authoritative
    afterwards, which is what stops an environment variable edited at 3am from
    retroactively widening a namespace already running.
    """
    agent = _bound_agent(client)
    assert _turn(client, _dispatch_session(client, agent)).status_code == 200

    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_dispatch_state SET max_turns_per_hour = 1 "
                " WHERE namespace_key = 'default'"
            )
        )

    refused = _turn(client, _dispatch_session(client, agent))
    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "DISPATCH_BUDGET_EXCEEDED"


def test_a_ceiling_of_zero_is_zero_turns_and_not_one(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """Zero is a legal setting, so zero has to mean zero.

    The window-roll branch of the charge re-checks the ceiling for exactly this
    reason. The insert branch cannot: a ``WHERE`` that stopped the statement
    proposing a row would also stop it updating namespaces whose own ceiling is
    fine. What makes that harmless is an ordering nobody has written down and
    this test pins - a dispatch turn needs a task, a task needs an import, and
    the import seeds the row. Delete the row by hand and the first turn after
    it is charged against a ceiling of zero.
    """
    monkeypatch.setattr(dispatch_settings, "default_max_turns_per_hour", 0)
    agent = _bound_agent(client)

    refused = _turn(client, _dispatch_session(client, agent))
    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "DISPATCH_BUDGET_EXCEEDED"
    assert fake_executor.runs == []


# ---------------------------------------------------------------------------
# Level 1, the pause
# ---------------------------------------------------------------------------


def test_a_pause_refuses_dispatch_turns_and_leaves_the_console_usable(
    client: TestClient,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    agent = _bound_agent(client)
    fleet = _dispatch_session(client, agent)

    paused = client.post(f"{DISPATCH_URL}/pause", json={"reason": "incident 4"})
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"]["paused"] is True

    refused = _turn(client, fleet)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "DISPATCH_PAUSED"
    assert "incident 4" in refused.json()["detail"]

    # A pause stops new dispatch work. It does not lock an operator out of the
    # console while they go and look at what the fleet did.
    chat = _session(client, agent)
    assert _turn(client, chat).status_code == 200

    resumed = client.post(f"{DISPATCH_URL}/resume")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"]["paused"] is False
    assert _turn(client, fleet).status_code == 200


def test_a_pause_refuses_the_import_and_the_claim(client: TestClient) -> None:
    assert client.post(f"{DISPATCH_URL}/pause", json={}).status_code == 200

    refused = _import_tasks(client, [_ref()])
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "DISPATCH_PAUSED"


# ---------------------------------------------------------------------------
# Level 3, the kill switch
# ---------------------------------------------------------------------------


def test_the_kill_switch_refuses_every_new_turn_including_a_human_chat(
    client: TestClient,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """The regression test for the sentence beside the button.

    A chat session opened *before* the halt is the only case that distinguishes
    "refuses every new turn in this namespace, human chat included" from
    "refuses every new dispatch turn and every new session". The first is what
    the hint, the field description and the endpoint docstring all say.
    """
    agent = _bound_agent(client)
    chat = _session(client, agent)
    fleet = _dispatch_session(client, agent)
    assert _turn(client, chat).status_code == 200

    halted = client.post(f"{DISPATCH_URL}/halt-executors", json={"reason": "runaway"})
    assert halted.status_code == 200, halted.text
    assert halted.json()["state"]["executors_halted"] is True

    for session_key in (chat, fleet):
        refused = _turn(client, session_key)
        assert refused.status_code == 409, refused.text
        assert refused.json()["error_code"] == "EXECUTORS_HALTED"
        assert "runaway" in refused.json()["detail"]

    blocked = client.post(SESSIONS_URL, json={"agent_name": agent})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error_code"] == "EXECUTORS_HALTED"

    released = client.post(f"{DISPATCH_URL}/release-executors")
    assert released.status_code == 200, released.text
    assert _turn(client, chat).status_code == 200


def test_releasing_the_halt_does_not_silently_clear_a_pause(
    client: TestClient,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """Two flags rather than one enum, so stepping back down one level of
    escalation does not take the level underneath it with you."""
    assert client.post(f"{DISPATCH_URL}/pause", json={"reason": "first"}).status_code == 200
    assert client.post(f"{DISPATCH_URL}/halt-executors", json={}).status_code == 200

    released = client.post(f"{DISPATCH_URL}/release-executors")
    state = released.json()["state"]
    assert state["executors_halted"] is False
    assert state["paused"] is True
    assert state["paused_reason"] == "first"


# ---------------------------------------------------------------------------
# Per-agent concurrency
# ---------------------------------------------------------------------------


def test_one_agent_runs_one_dispatch_turn_at_a_time(
    client: TestClient,
    db_engine: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """One executor process per agent, and the plugin's concurrent-invocation
    safety has never been demonstrated. This is a ceiling, not a queue."""
    agent = _bound_agent(client)
    busy = _dispatch_session(client, agent)
    waiting = _dispatch_session(client, agent)
    _mark_in_flight(db_engine, busy, trace="live-elsewhere")

    refused = _turn(client, waiting)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "AGENT_CONCURRENCY_EXCEEDED"
    assert fake_executor.runs == []

    # A different agent in the same namespace is unaffected: the ceiling is per
    # agent because the thing it protects is one process.
    other = _bound_agent(client)
    assert _turn(client, _dispatch_session(client, other)).status_code == 200


def test_a_human_chat_does_not_block_its_agents_dispatch_turn(
    client: TestClient,
    db_engine: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """The count is over sessions belonging to a task.

    Counting chats too would let one operator with a panel open stall the whole
    fleet, and the ceiling exists for the plugin rather than for the process.
    """
    agent = _bound_agent(client)
    chat = _session(client, agent)
    _mark_in_flight(db_engine, chat, trace="human-is-typing")

    assert _turn(client, _dispatch_session(client, agent)).status_code == 200


def test_a_marker_older_than_the_staleness_window_does_not_block_forever(
    client: TestClient,
    db_engine: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """A 504 keeps the liveness marker deliberately. Past the window in which
    the server already lets that session's *own* lock be taken over, refusing a
    different session on the strength of the same marker would be the stricter
    half of an inconsistent pair - and nothing would ever clear it."""
    agent = _bound_agent(client)
    stale = _dispatch_session(client, agent)
    _mark_in_flight(db_engine, stale, trace="timed-out-ages-ago")
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions "
                "   SET in_flight_since = now() - (:secs * interval '1 second'), "
                "       last_activity_at = now() - (:secs * interval '1 second') "
                " WHERE session_key = :key"
            ),
            {"key": stale, "secs": executor_settings.turn_stale_after_seconds + 60},
        )

    assert _turn(client, _dispatch_session(client, agent)).status_code == 200


# ---------------------------------------------------------------------------
# The import ceiling
# ---------------------------------------------------------------------------


def test_an_import_that_would_cross_the_hourly_task_ceiling_inserts_nothing(
    client: TestClient,
    db_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatch_settings, "default_max_tasks_per_hour", 3)

    assert _import_tasks(client, [_ref(), _ref()]).status_code == 200

    refused = _import_tasks(client, [_ref(), _ref()])
    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "DISPATCH_BUDGET_EXCEEDED"
    assert refused.headers["Retry-After"]

    with db_engine.connect() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM agent_tasks WHERE namespace_key = 'default'")
        ).scalar_one()
    assert total == 2, "a refused commit must not insert a partial set"

    # And the remaining allowance is still spendable, because nothing was taken.
    assert _import_tasks(client, [_ref()]).status_code == 200


# ---------------------------------------------------------------------------
# Level 2, the fleet halt
# ---------------------------------------------------------------------------


def test_the_fleet_halt_binds_one_stop_per_live_turn_and_reports_honestly(
    client: TestClient,
    db_engine: Any,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    agent = _bound_agent(client)
    fleet = _dispatch_session(client, agent)
    chat = _session(client, agent)
    idle = _session(client, agent)
    _mark_in_flight(db_engine, fleet, trace="trace-fleet")
    _mark_in_flight(db_engine, chat, trace="trace-chat")

    stopped = client.post(f"{DISPATCH_URL}/halt-fleet")
    assert stopped.status_code == 200, stopped.text
    body = stopped.json()
    assert body["sessions_in_flight"] == 2
    assert body["halts_created"] == 2
    assert body["already_halted"] == 0
    # Human chats are reached too, and the console has to be able to say so.
    assert body["dispatch_sessions_in_flight"] == 1

    with db_engine.connect() as conn:
        traces = {
            row.target_trace_id
            for row in conn.execute(
                text("SELECT target_trace_id FROM agent_session_halts")
            )
        }
    assert traces == {"trace-fleet", "trace-chat"}
    assert idle  # named for the reader: a session with no live turn gets nothing

    # Idempotent. Pressing it twice is one stop per turn, not two.
    again = client.post(f"{DISPATCH_URL}/halt-fleet").json()
    assert again["halts_created"] == 0
    assert again["already_halted"] == 2


# ---------------------------------------------------------------------------
# Reads, and the namespace boundary
# ---------------------------------------------------------------------------


def test_reading_the_state_reports_defaults_without_creating_a_row(
    client: TestClient, db_engine: Any
) -> None:
    """A console polls this, and a read that writes cannot be served from a
    replica."""
    resp = client.get(DISPATCH_URL)
    assert resp.status_code == 200, resp.text
    state = resp.json()["state"]

    assert state["paused"] is False
    assert state["executors_halted"] is False
    assert state["budget"]["max_turns_per_hour"] == dispatch_settings.default_max_turns_per_hour
    assert state["budget"]["turns_used_this_hour"] == 0

    with db_engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM agent_dispatch_state")).scalar_one()
    assert rows == 0


def test_a_stop_in_one_namespace_is_invisible_from_another(
    app: FastAPI,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")

    agent = _bound_agent(beta)
    beta_chat = _session(beta, agent)

    assert alpha.post(f"{DISPATCH_URL}/halt-executors", json={}).status_code == 200

    assert alpha.get(DISPATCH_URL).json()["state"]["executors_halted"] is True
    assert beta.get(DISPATCH_URL).json()["state"]["executors_halted"] is False
    assert _turn(beta, beta_chat).status_code == 200
    assert beta.post(f"{DISPATCH_URL}/halt-fleet").json()["sessions_in_flight"] == 0


# ---------------------------------------------------------------------------
# The window, which is fixed rather than sliding
# ---------------------------------------------------------------------------


def _age_the_window(db_engine: Any, *, seconds: float) -> None:
    """Push the window's start back, which is what the clock does for free."""
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_dispatch_state "
                "   SET turns_window_start = now() - (:secs * interval '1 second') "
                " WHERE namespace_key = 'default'"
            ),
            {"secs": seconds},
        )


def test_the_allowance_comes_back_when_the_window_rolls(
    client: TestClient,
    db_engine: Any,
    tiny_budget: None,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """A ceiling that never refills is an off switch with a delay on it.

    The refusal's own hint promises the counter rolls at the top of the window,
    and that promise is the only reason a dispatcher told to wait is told
    anything useful. The window is fixed rather than sliding - two integers and
    one statement - so what "rolls" means is that the next charge after the
    window expires starts a new one at exactly one.
    """
    agent = _bound_agent(client)
    assert _turn(client, _dispatch_session(client, agent)).status_code == 200
    assert _turn(client, _dispatch_session(client, agent)).status_code == 200
    assert _turn(client, _dispatch_session(client, agent)).status_code == 429

    _age_the_window(db_engine, seconds=3601)

    assert _turn(client, _dispatch_session(client, agent)).status_code == 200
    assert _counter(db_engine) == 1, "a rolled window starts again at one, not at three"


def test_a_read_reports_a_rolled_window_as_fresh_and_does_not_write_to_it(
    client: TestClient,
    db_engine: Any,
    tiny_budget: None,
    executor_enabled: None,  # noqa: F811 - fixture
    fake_executor: FakeTurnExecutorFactory,  # noqa: F811 - fixture
) -> None:
    """The banner must not tell an operator the hour is spent when it is not.

    A window nobody has charged a turn in since it expired still holds the old
    count in the row, because the counter is moved by the charge statement and
    by nothing else. So the *reported* figure has to roll on its own - and it
    has to do that without writing, because a read that writes cannot be served
    from a replica and a console polls this one.
    """
    agent = _bound_agent(client)
    assert _turn(client, _dispatch_session(client, agent)).status_code == 200
    assert _turn(client, _dispatch_session(client, agent)).status_code == 200
    assert client.get(DISPATCH_URL).json()["state"]["budget"]["turns_used_this_hour"] == 2

    _age_the_window(db_engine, seconds=3601)

    budget = client.get(DISPATCH_URL).json()["state"]["budget"]
    assert budget["turns_used_this_hour"] == 0
    assert budget["turns_remaining_this_hour"] == 2
    assert _counter(db_engine) == 2, "the read reported a roll it must not perform"


# ---------------------------------------------------------------------------
# Level 1, on the claim
# ---------------------------------------------------------------------------


def test_a_pause_refuses_a_claim_of_a_task_that_was_already_queued(
    client: TestClient,
) -> None:
    """The other half of the sentence the import test's name promises.

    Refusing the import stops new rows appearing. Refusing the claim stops a row
    that is already queued being taken out of the queue by a process that could
    not then run a turn against it, which is the case a pause pressed *after* a
    successful import produces. Like the import refusal it is an optimisation
    and not the enforcement point, and it still has to happen or the first thing
    a paused namespace does is strand a task at ``claimed``.
    """
    committed = _import_tasks(client, [_ref()])
    assert committed.status_code == 200, committed.text
    key = str(committed.json()["task_keys"][0])

    assert client.post(f"{DISPATCH_URL}/pause", json={"reason": "hold"}).status_code == 200

    refused = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "DISPATCH_PAUSED"
    assert "hold" in refused.json()["detail"]

    # And the row is untouched, so a resume picks it straight back up.
    assert client.post(f"{DISPATCH_URL}/resume").status_code == 200
    claimed = client.post(f"{TASKS_URL}/{key}/claim", json={"instance_id": "inst"})
    assert claimed.status_code == 200, claimed.text
