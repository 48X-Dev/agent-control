"""A stop beats the queue, and the queue is not consumed.

One rule, decided inside the claim transaction rather than in the SDK, and it
exists to protect the one promise the nudge queue cannot afford to break.

If a nudge were claimed and injected into a request whose response is about to
be replaced by a block, it would be recorded as ``applied`` while no model ever
read it. The operator would be shown their words as delivered to an agent that
was stopped before it saw them. A dropped nudge presented as delivered is the
failure the at-least-once design exists to prevent, and this is the cheapest
possible way to reintroduce it.

So the claim returns the halt, zero nudges, and moves no counter on anything.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from .test_agent_halts_endpoints import (
    _claim_at_tool,
    _create,
    _fresh_quota,  # noqa: F401 - fixture
    _halts,
    _session,
    _start_turn,
)
from .test_agent_nudges_endpoints import _MachineClient, _by_id, _queue, machine  # noqa: F401
from .test_agent_sessions_endpoints import (
    executor_enabled,  # noqa: F401 - fixture
    fake_executor,  # noqa: F401 - fixture
)

_SESSIONS_URL = "/api/v1/agent-sessions"

pytestmark = pytest.mark.usefixtures("executor_enabled", "fake_executor")


def test_a_halt_returns_alone_and_leaves_every_queued_nudge_exactly_as_it_was(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    queued = [_queue(client, session_key, f"guidance {index}") for index in range(3)]
    halt = _create(client, session_key).json()["halt"]

    claimed = machine.claim(session_key)

    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["halt"] is not None and body["halt"]["id"] == halt["id"]
    assert body["nudges"] == [], "guidance a model will never see is not delivered"
    assert body["claim_expires_at"] is None

    rows = _by_id(client, session_key)
    for row in queued:
        state = rows[row["id"]]
        assert state["status"] == "pending"
        assert state["claim_count"] == 0
        assert state["injection_attempts"] == 0
        assert state["claimed_at"] is None
        assert state["applied_at"] is None


def test_the_same_precedence_holds_when_the_stop_was_claimed_at_a_tool_boundary(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """A halt applied at a tool boundary is spent, so the queue drains again.

    Which is correct and worth pinning: the stop has landed, the SDK latches it
    for the invocation, and there is no second stop to enforce. What must not
    happen is the applied row being claimed twice and blocking two turns.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    queued = _queue(client, session_key, "and check the totals")
    _create(client, session_key)

    assert _claim_at_tool(machine, session_key).json()["halt"] is not None
    after = machine.claim(session_key).json()

    assert after["halt"] is None
    assert [item["id"] for item in after["nudges"]] == [queued["id"]]
    assert _halts(client, session_key)[0]["status"] == "applied"


def test_a_nudge_typed_after_the_stop_waits_for_the_next_turn(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """"Stop it, then tell it something else" needs no new machinery.

    Once the halt lands the turn is over, the lock is clear, and the next turn
    is an ordinary turn that drains the queue.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)
    machine.claim(session_key)

    later = _queue(client, session_key, "now do the other thing instead")
    assert _by_id(client, session_key)[later["id"]]["status"] == "pending"

    # The stopped turn ends and a new one begins.
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions "
                "   SET in_flight_since = NULL, in_flight_trace_id = NULL "
                " WHERE session_key = :key"
            ),
            {"key": session_key},
        )
    _start_turn(db_engine, session_key)

    drained = machine.claim(session_key).json()
    assert drained["halt"] is None
    assert [item["id"] for item in drained["nudges"]] == [later["id"]]


def test_a_stale_halt_does_not_suppress_the_next_turns_guidance(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """Precedence must not outlive the turn the halt was bound to.

    A halt whose executor died sits ``pending`` in the table. If precedence
    looked at halts by session rather than by turn, that row would swallow
    every later claim: the queue would silently stop draining and the operator
    would see "queued" forever with nothing to point at.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)
    queued = _queue(client, session_key, "carry on, but carefully")

    _start_turn(db_engine, session_key)  # a new turn, the old halt never landed

    drained = machine.claim(session_key).json()
    assert drained["halt"] is None
    assert [item["id"] for item in drained["nudges"]] == [queued["id"]]
