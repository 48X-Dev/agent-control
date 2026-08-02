"""Coverage for stopping a turn: ``/agent-sessions/{key}/halts`` and its claim.

A stop button that can report success without a stop is worse than no stop
button, so almost every assertion here is about the difference between "the
executor said it blocked" and "this server saw the turn end".

What is pinned:

* creation binds to the session's **liveness marker**, so the button still
  works in the window after a 504 - the single most likely moment for somebody
  to reach for it - and refuses with a typed 409 when nothing is running;
* one turn has one halt, so a second press is the same row and answers 200;
* claim and apply are one statement at both boundaries, and a second claim
  finds nothing left to apply;
* **applying does not end the turn**: the session stays in flight and
  ``turn_ended_at`` stays null until this server observes the ending itself;
* a halt is unclaimable outside its own turn, which is what stops a stale stop
  from killing a turn somebody deliberately started afterwards;
* replica loss ages the old halt out at the next acquire and leaves the new
  turn unhalted.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.config import executor_settings
from agent_control_server.db import AsyncSessionLocal
from agent_control_server.services.turn_locks import (
    acquire_turn_lock,
    new_trace_id,
    release_turn_lock,
)
from agent_control_server.services.turn_quota import reset_turn_quota
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from .test_agent_nudges_endpoints import _MachineClient, machine  # noqa: F401
from .test_agent_sessions_endpoints import (
    HeaderNamespaceAuthorizer,
    _agent_name,
    _bind,
    _namespace_client,
    _open_session,
    _register_agent,
    executor_enabled,  # noqa: F401 - fixture
    fake_executor,  # noqa: F401 - fixture
)

_SESSIONS_URL = "/api/v1/agent-sessions"

pytestmark = pytest.mark.usefixtures("executor_enabled", "fake_executor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_quota() -> Any:
    """Stops share the turn ceiling, so one test must not spend another's."""
    reset_turn_quota()
    yield
    reset_turn_quota()


def _session(client: TestClient) -> str:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    return str(_open_session(client, agent_name)["session_key"])


def _start_turn(db_engine: Any, session_key: str, *, locked: bool = True) -> str:
    """Put a turn in flight the way the turn handler does.

    ``locked=False`` is the 504 exit: the handler gave up waiting and released
    the lock, and the liveness marker stays set because the invocation did not
    stop when this server stopped listening.
    """
    trace = new_trace_id()
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions "
                "   SET in_flight_trace_id = :trace, "
                "       in_flight_since = CASE WHEN :locked THEN now() ELSE NULL END "
                " WHERE session_key = :key"
            ),
            {"key": session_key, "trace": trace, "locked": locked},
        )
    return trace


def _session_row(db_engine: Any, session_key: str) -> Any:
    with db_engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT id, in_flight_since, in_flight_trace_id, last_trace_id "
                "  FROM agent_sessions WHERE session_key = :key"
            ),
            {"key": session_key},
        ).one()


def _create(client: TestClient, session_key: str) -> Any:
    return client.post(f"{_SESSIONS_URL}/{session_key}/halts", json={})


def _halts(client: TestClient, session_key: str) -> list[dict[str, Any]]:
    resp = client.get(f"{_SESSIONS_URL}/{session_key}/halts")
    assert resp.status_code == 200, resp.text
    return list(resp.json()["halts"])


def _claim_at_tool(
    machine: _MachineClient, session_key: str, *, tool_name: str | None = "send_email"
) -> Any:
    body: dict[str, Any] = {"boundary": "tool"}
    if tool_name is not None:
        body["tool_name"] = tool_name
    return machine._client.post(  # noqa: SLF001 - the fixture's own transport
        f"{_SESSIONS_URL}/{session_key}/halts/claim",
        json=body,
        headers=machine._headers(session_key),  # noqa: SLF001
    )


def _ack(machine: _MachineClient, session_key: str, body: dict[str, Any]) -> Any:
    return machine._client.post(  # noqa: SLF001
        f"{_SESSIONS_URL}/{session_key}/halts/ack",
        json=body,
        headers=machine._headers(session_key),  # noqa: SLF001
    )


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_a_halt_binds_to_the_turn_that_is_running(
    client: TestClient, db_engine: Any
) -> None:
    session_key = _session(client)
    trace = _start_turn(db_engine, session_key)

    resp = _create(client, session_key)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] is True
    halt = body["halt"]
    assert halt["target_trace_id"] == trace
    assert halt["status"] == "pending"
    assert halt["mode"] == "graceful"
    assert halt["applied_at"] is None
    assert halt["applied_at_boundary"] is None
    assert halt["turn_ended_at"] is None, "nothing has stopped yet"


def test_a_session_running_nothing_cannot_be_stopped(client: TestClient) -> None:
    """There is no such thing as a stop queued for a future turn."""
    session_key = _session(client)

    resp = _create(client, session_key)

    assert resp.status_code == 409, resp.text
    assert resp.json()["error_code"] == "TURN_NOT_IN_FLIGHT"
    assert _halts(client, session_key) == []


def test_a_halt_can_still_be_created_after_a_timeout_released_the_lock(
    client: TestClient, db_engine: Any
) -> None:
    """The window the whole trace binding exists for.

    At T+timeout the handler has given up and cleared ``in_flight_since``,
    which is exactly when a person reaches for stop. Binding to the lock rather
    than to the liveness marker would hide the button at that moment while the
    panel still showed an agent working.

    What such a halt is worth is a separate and smaller question - the executor
    ends an invocation whose request was dropped - and nothing here claims it
    lands. It is created, recorded, and aged out by the next turn.
    """
    session_key = _session(client)
    trace = _start_turn(db_engine, session_key, locked=False)
    row = _session_row(db_engine, session_key)
    assert row.in_flight_since is None and row.in_flight_trace_id == trace

    resp = _create(client, session_key)

    assert resp.status_code == 200, resp.text
    assert resp.json()["halt"]["target_trace_id"] == trace


def test_pressing_stop_twice_is_one_halt(client: TestClient, db_engine: Any) -> None:
    """Idempotent by constraint rather than by service logic.

    Telling somebody their second click failed invites a third, and a second
    transcript marker for one stop would be a second event that never happened.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    first = _create(client, session_key)
    second = _create(client, session_key)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["halt"]["id"] == first.json()["halt"]["id"]
    assert len(_halts(client, session_key)) == 1


def test_a_halt_carries_no_operator_text(client: TestClient, db_engine: Any) -> None:
    """The body is closed, and that is a security property, not tidiness.

    A reason field would be free text from an AUTHENTICATED caller heading for
    the model's context, which is the exact channel the nudge design spends its
    whole delivery mechanism keeping under control evaluation.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    resp = client.post(
        f"{_SESSIONS_URL}/{session_key}/halts",
        json={"reason": "because I said so"},
    )

    assert resp.status_code == 422, resp.text
    assert _halts(client, session_key) == []


# ---------------------------------------------------------------------------
# Claiming, at both boundaries
# ---------------------------------------------------------------------------


def test_the_model_boundary_claim_applies_the_halt_and_returns_no_nudges(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """The model boundary claims halts on the nudge call, not on a second one."""
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    created = _create(client, session_key).json()["halt"]

    claimed = machine.claim(session_key)

    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["halt"] is not None
    assert body["halt"]["id"] == created["id"]
    assert body["halt"]["target_trace_id"] == created["target_trace_id"]
    assert body["nudges"] == []

    (row,) = _halts(client, session_key)
    assert row["status"] == "applied"
    assert row["applied_at"] is not None
    assert row["applied_at_boundary"] == "model"


def test_the_tool_boundary_claim_records_the_tool_it_caught(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """This is the difference between stopping before and after the email."""
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    created = _create(client, session_key).json()["halt"]

    claimed = _claim_at_tool(machine, session_key, tool_name="send_email")

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["halt"]["id"] == created["id"]

    (row,) = _halts(client, session_key)
    assert row["status"] == "applied"
    assert row["applied_at_boundary"] == "tool"
    assert row["applied_tool_name"] == "send_email"


def test_claim_and_apply_are_one_step_so_a_second_claim_finds_nothing(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """No window between claimed and applied, so nothing can strand in one.

    Splitting them would let a lost response sweep an applied row to expired
    after the agent genuinely stopped, and the console would then report that
    the stop never landed on an agent that is already stopped.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)

    assert _claim_at_tool(machine, session_key).json()["halt"] is not None
    second = _claim_at_tool(machine, session_key)

    assert second.status_code == 200, second.text
    assert second.json()["halt"] is None
    assert len(_halts(client, session_key)) == 1


def test_a_boundary_with_no_halt_pending_claims_nothing(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    assert _claim_at_tool(machine, session_key).json()["halt"] is None
    assert machine.claim(session_key).json()["halt"] is None


def test_a_halt_is_unclaimable_outside_the_turn_it_was_bound_to(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """The join, asserted as behaviour rather than read off the SQL.

    Without it, a halt whose executor died sits in the table and the next
    turn's first model call claims it, silently killing a turn the human
    deliberately started afterwards under a marker blaming an operator.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    _create(client, session_key)

    # A different turn is now running on the same session.
    _start_turn(db_engine, session_key)

    assert machine.claim(session_key).json()["halt"] is None
    assert _claim_at_tool(machine, session_key).json()["halt"] is None
    (row,) = _halts(client, session_key)
    assert row["status"] == "pending", "still bound to its own dead turn"


def test_applying_a_halt_does_not_by_itself_end_the_turn(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """``applied`` is the executor's word for it, and it is being stopped.

    The state a console may render as stopped is the turn actually ending,
    which this server observes for itself. Until then the honest copy is "stop
    acknowledged, waiting for the turn to end".
    """
    session_key = _session(client)
    trace = _start_turn(db_engine, session_key)
    _create(client, session_key)

    machine.claim(session_key)

    row = _session_row(db_engine, session_key)
    assert row.in_flight_trace_id == trace, "the invocation is not this server's to end"
    assert row.in_flight_since is not None
    (halt,) = _halts(client, session_key)
    assert halt["status"] == "applied"
    assert halt["turn_ended_at"] is None


# ---------------------------------------------------------------------------
# Acknowledging
# ---------------------------------------------------------------------------


def test_the_acknowledgement_enriches_the_tool_name_and_nothing_else(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    created = _create(client, session_key).json()["halt"]
    machine.claim(session_key)

    acked = _ack(
        machine,
        session_key,
        {"id": created["id"], "applied_tool_name": "send_email"},
    )

    assert acked.status_code == 200, acked.text
    halt = acked.json()["halt"]
    assert halt["applied_tool_name"] == "send_email"
    assert halt["status"] == "applied"
    assert halt["applied_at_boundary"] == "model", "unchanged by the enrichment"


@pytest.mark.parametrize(
    "tool_name",
    [
        "send email",
        "9lives",
        "send_email; DROP TABLE agents",
        "<script>alert(1)</script>",
        "x" * 65,
    ],
)
def test_a_tool_name_that_is_not_an_identifier_is_refused(
    client: TestClient, db_engine: Any, machine: _MachineClient, tool_name: str
) -> None:
    """The one field carrying bytes chosen by a process running agent code.

    It lands in an operator console, so it is pattern-checked here rather than
    trusted and escaped later.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)
    created = _create(client, session_key).json()["halt"]
    machine.claim(session_key)

    refused = _ack(
        machine, session_key, {"id": created["id"], "applied_tool_name": tool_name}
    )

    assert refused.status_code == 422, refused.text
    (row,) = _halts(client, session_key)
    assert row["applied_tool_name"] is None


def test_acknowledging_a_halt_from_another_session_is_a_404(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    mine = _session(client)
    theirs = _session(client)
    _start_turn(db_engine, mine)
    _start_turn(db_engine, theirs)
    _create(client, mine)
    victim = _create(client, theirs).json()["halt"]
    machine.claim(theirs)

    refused = _ack(
        machine, mine, {"id": victim["id"], "applied_tool_name": "send_email"}
    )

    assert refused.status_code == 404, refused.text
    assert _halts(client, theirs)[0]["applied_tool_name"] is None


# ---------------------------------------------------------------------------
# The turn ending, and what that does to the row
# ---------------------------------------------------------------------------


async def test_the_turn_ending_stamps_an_applied_halt_as_really_stopped(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    session_key = _session(client)
    trace = new_trace_id()
    async with AsyncSessionLocal() as db:
        session_id = await acquire_turn_lock(
            db,
            namespace_key="default",
            session_key=session_key,
            trace_id=trace,
            stale_after_seconds=60.0,
        )
        await db.commit()
    assert session_id is not None
    _create(client, session_key)
    machine.claim(session_key)

    await release_turn_lock(
        session_id=session_id,
        namespace_key="default",
        trace_id=trace,
        turn_ended=True,
    )

    (halt,) = _halts(client, session_key)
    assert halt["status"] == "applied", "it landed, and it stays landed"
    assert halt["turn_ended_at"] is not None


async def test_a_turn_that_finished_before_the_stop_reached_a_boundary_expires_it(
    client: TestClient, db_engine: Any
) -> None:
    """A different outcome from being stopped, and it reads differently."""
    session_key = _session(client)
    trace = new_trace_id()
    async with AsyncSessionLocal() as db:
        session_id = await acquire_turn_lock(
            db,
            namespace_key="default",
            session_key=session_key,
            trace_id=trace,
            stale_after_seconds=60.0,
        )
        await db.commit()
    assert session_id is not None
    _create(client, session_key)

    await release_turn_lock(
        session_id=session_id,
        namespace_key="default",
        trace_id=trace,
        turn_ended=True,
    )

    (halt,) = _halts(client, session_key)
    assert halt["status"] == "expired"
    assert halt["turn_ended_at"] is not None
    assert halt["applied_at"] is None


async def test_replica_loss_leaves_the_new_turn_unhalted_and_ages_the_old_stop_out(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    """Nobody stamps a halt when the process holding its turn dies.

    There is no sweeper in this codebase, so the expiry has to happen inside
    the one statement guaranteed to run before any later turn exists. Getting
    this wrong does not lose a row, it stops a turn somebody deliberately
    started afterwards.
    """
    session_key = _session(client)
    dead_trace = _start_turn(db_engine, session_key)
    _create(client, session_key)
    # The replica dies here: no release, no cleanup, the lock is left behind.
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_sessions "
                "   SET in_flight_since = now() - interval '1 hour' "
                " WHERE session_key = :key"
            ),
            {"key": session_key},
        )

    new_trace = new_trace_id()
    async with AsyncSessionLocal() as db:
        reclaimed = await acquire_turn_lock(
            db,
            namespace_key="default",
            session_key=session_key,
            trace_id=new_trace,
            stale_after_seconds=60.0,
        )
        await db.commit()
    assert reclaimed is not None

    (halt,) = _halts(client, session_key)
    assert halt["target_trace_id"] == dead_trace
    assert halt["status"] == "expired"
    assert halt["turn_ended_at"] is not None
    # And the new turn runs unmolested.
    assert machine.claim(session_key).json()["halt"] is None
    assert _claim_at_tool(machine, session_key).json()["halt"] is None
    assert _session_row(db_engine, session_key).in_flight_trace_id == new_trace


# ---------------------------------------------------------------------------
# Who may stop a turn
# ---------------------------------------------------------------------------


def test_stopping_somebody_elses_session_is_a_403(
    client: TestClient, non_admin_client: TestClient, db_engine: Any
) -> None:
    """Run at AUTHENTICATED with stop unscoped would be the availability twin
    of handing every key everyone else's transcript."""
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    refused = _create(non_admin_client, session_key)

    assert refused.status_code == 403, refused.text
    assert _halts(client, session_key) == []
    assert (
        non_admin_client.get(f"{_SESSIONS_URL}/{session_key}/halts").status_code == 403
    )


def test_the_stop_shares_the_turn_ceiling(
    client: TestClient, db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start-stop-start is one loop, so two ceilings would let half of it run
    at a rate the other half is refused at."""
    monkeypatch.setattr(executor_settings, "max_turns_per_minute", 1)
    reset_turn_quota()
    first = _session(client)
    second = _session(client)
    _start_turn(db_engine, first)
    _start_turn(db_engine, second)

    assert _create(client, first).status_code == 200
    refused = _create(client, second)

    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "QUOTA_EXCEEDED"
    assert _halts(client, second) == []


def test_a_token_bound_to_one_session_cannot_stop_another(
    client: TestClient, db_engine: Any, machine: _MachineClient
) -> None:
    mine = _session(client)
    theirs = _session(client)
    _start_turn(db_engine, theirs)
    _create(client, theirs)

    refused = machine._client.post(  # noqa: SLF001
        f"{_SESSIONS_URL}/{theirs}/halts/claim",
        json={"boundary": "tool"},
        headers=machine._headers(theirs, token_for=mine),  # noqa: SLF001
    )

    assert refused.status_code == 403, refused.text
    assert _halts(client, theirs)[0]["status"] == "pending"


def test_unauthenticated_callers_reach_no_halt_route(
    unauthenticated_client: TestClient,
) -> None:
    key = uuid.uuid4().hex
    assert (
        unauthenticated_client.post(f"{_SESSIONS_URL}/{key}/halts", json={}).status_code
        == 401
    )
    assert unauthenticated_client.get(f"{_SESSIONS_URL}/{key}/halts").status_code == 401
    assert (
        unauthenticated_client.post(
            f"{_SESSIONS_URL}/{key}/halts/claim", json={"boundary": "model"}
        ).status_code
        == 401
    )


def test_a_session_key_from_another_namespace_is_a_404(
    app: FastAPI, db_engine: Any
) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")
    session_key = _session(alpha)
    _start_turn(db_engine, session_key)

    assert _create(beta, session_key).status_code == 404
    assert beta.get(f"{_SESSIONS_URL}/{session_key}/halts").status_code == 404
    assert _halts(alpha, session_key) == []


# ---------------------------------------------------------------------------
# What the record says out loud
# ---------------------------------------------------------------------------


def test_the_audit_line_names_the_action_and_carries_no_content(
    client: TestClient, db_engine: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The one exemption to "content is never logged above DEBUG".

    It earns the exemption by having no content to leak: an availability-
    affecting action whose only actor field hashes to the same value for every
    browser caller has no audit trail otherwise.
    """
    session_key = _session(client)
    _start_turn(db_engine, session_key)

    with caplog.at_level("WARNING"):
        _create(client, session_key)

    lines = [
        record.getMessage()
        for record in caplog.records
        if "Operator halt" in record.getMessage()
    ]
    assert lines, "an operator stopping an agent is not a silent event"
    assert session_key not in lines[0], "only a prefix, never the whole key"
    assert "graceful" in lines[0]
