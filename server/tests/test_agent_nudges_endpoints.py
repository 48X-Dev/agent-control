"""Coverage for the nudge queue: ``/agent-sessions/{key}/nudges`` and its claim.

A nudge is a sentence somebody typed while an agent was working, and the whole
feature is a promise about what happened to it. So the assertions here are
about the promise rather than about the plumbing:

* a nudge queued between turns stays ``pending`` and says so - it is not lost,
  and nothing pretends the agent read it;
* ten queued nudges hand three to one model call, oldest first, and the other
  seven come back with **both** counters untouched, because a nudge nobody
  attempted must never age out;
* only a failed injection moves ``injection_attempts``, and only that counter
  can expire a row;
* a claim is a lease: an executor that dies holding one loses it, and the nudge
  is delivered again rather than silently marked delivered;
* a nudge denied by a control is terminal, names the control, and never reaches
  a model;
* the body never appears in log output above DEBUG.

The machine-side half runs with a real session-bound runtime token against the
real routes, because "a token minted for session A cannot claim session B" is
the entire authorization design and asserting it against a stub would assert
nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

import pytest
from agent_control_models.nudges import (
    MAX_PENDING_NUDGES_PER_SESSION,
    NUDGE_BODY_MAX_LENGTH,
    NUDGE_MAX_INJECTION_ATTEMPTS,
    NUDGE_MAX_PER_MODEL_CALL,
)
from agent_control_server.auth_framework import Operation, set_authorizer
from agent_control_server.auth_framework.config import (
    RuntimeAuthConfig,
    set_runtime_auth_config,
)
from agent_control_server.auth_framework.providers import LocalJwtVerifyProvider
from agent_control_server.services.agent_sessions import mint_session_runtime_token
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from .test_agent_sessions_auth import DenyingAuthorizer
from .test_agent_sessions_endpoints import (
    FakeExecutorFactory,
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
_RUNTIME_SECRET = "test-runtime-secret-that-is-long-enough-for-hs256"

pytestmark = pytest.mark.usefixtures("executor_enabled", "fake_executor")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(client: TestClient) -> str:
    agent_name = _agent_name()
    _register_agent(client, agent_name)
    _bind(client, agent_name)
    return str(_open_session(client, agent_name)["session_key"])


def _queue(client: TestClient, session_key: str, body: str) -> dict[str, Any]:
    resp = client.post(f"{_SESSIONS_URL}/{session_key}/nudges", json={"body": body})
    assert resp.status_code == 200, resp.text
    return dict(resp.json()["nudge"])


def _list(client: TestClient, session_key: str, **params: Any) -> list[dict[str, Any]]:
    resp = client.get(f"{_SESSIONS_URL}/{session_key}/nudges", params=params)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["nudges"])


def _by_id(client: TestClient, session_key: str) -> dict[int, dict[str, Any]]:
    return {int(row["id"]): row for row in _list(client, session_key)}


@pytest.fixture()
def machine(app: FastAPI) -> Any:
    """A client that authenticates the way an executor does, and nothing else.

    The runtime provider is installed for the consume operation only, so a test
    that reaches for a human route with this fixture still goes through the
    ordinary authorizer - which is what keeps the two halves of these endpoints
    honestly separate.
    """
    set_runtime_auth_config(RuntimeAuthConfig(secret=_RUNTIME_SECRET, ttl_seconds=900))
    set_authorizer(
        LocalJwtVerifyProvider(secret=_RUNTIME_SECRET),
        operation=Operation.AGENT_NUDGES_CONSUME,
    )
    yield _MachineClient(app)
    set_runtime_auth_config(None)


class _MachineClient:
    """Signs each request with a token minted for the session in the path."""

    def __init__(self, app: FastAPI) -> None:
        self._client = TestClient(app, raise_server_exceptions=True)

    def _headers(self, session_key: str, *, token_for: str | None = None) -> dict[str, str]:
        minted = mint_session_runtime_token(
            namespace_key="default",
            session_key=token_for or session_key,
            actor_id="0123456789abcdef",
        )
        assert minted is not None
        return {"Authorization": f"Bearer {minted[0]}"}

    def claim(
        self,
        session_key: str,
        *,
        max_nudges: int = NUDGE_MAX_PER_MODEL_CALL,
        token_for: str | None = None,
    ) -> Any:
        return self._client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges/claim",
            json={"max_nudges": max_nudges},
            headers=self._headers(session_key, token_for=token_for),
        )

    def ack(self, session_key: str, acks: list[dict[str, Any]]) -> Any:
        return self._client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges/ack",
            json={"acks": acks},
            headers=self._headers(session_key),
        )


def _expire_claims(db_engine: Any, session_key: str) -> None:
    """Age every live claim past its lease, as a dead executor would leave it."""
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE agent_session_nudges n "
                "   SET claim_expires_at = now() - interval '1 second' "
                "  FROM agent_sessions s "
                " WHERE n.session_id = s.id AND s.session_key = :key "
                "   AND n.status = 'claimed'"
            ),
            {"key": session_key},
        )


# ---------------------------------------------------------------------------
# Queuing, and what the queue says about itself
# ---------------------------------------------------------------------------


def test_a_nudge_queued_between_turns_stays_pending_and_keeps_its_words(
    client: TestClient,
) -> None:
    """The case the UI renders as "queued", not as a spinner.

    Nothing is in flight, and that is fine: a nudge is bound to a session, not
    to a turn, so it waits for the agent's next model call however long that
    takes. What it must not do is disappear or claim to have been delivered.
    """
    session_key = _session(client)
    body = "Check the invoice total against the PO before you reply."

    queued = _queue(client, session_key, body)

    assert queued["status"] == "pending"
    assert queued["body"] == body, "the operator's exact text, unmodified"
    assert queued["applied_at"] is None and queued["applied_trace_id"] is None
    assert queued["claim_count"] == 0 and queued["injection_attempts"] == 0

    (listed,) = _list(client, session_key)
    assert listed == queued


def test_the_queue_is_capped_and_the_refusal_is_a_typed_429(
    client: TestClient,
) -> None:
    """A queue drained three per model call is not improved by growing it."""
    session_key = _session(client)
    for index in range(MAX_PENDING_NUDGES_PER_SESSION):
        _queue(client, session_key, f"guidance {index}")

    refused = client.post(
        f"{_SESSIONS_URL}/{session_key}/nudges", json={"body": "one too many"}
    )
    assert refused.status_code == 429, refused.text
    assert refused.json()["error_code"] == "QUOTA_EXCEEDED"
    assert len(_list(client, session_key)) == MAX_PENDING_NUDGES_PER_SESSION


def test_an_over_long_body_is_refused_before_it_reaches_the_queue(
    client: TestClient,
) -> None:
    session_key = _session(client)

    resp = client.post(
        f"{_SESSIONS_URL}/{session_key}/nudges",
        json={"body": "x" * (NUDGE_BODY_MAX_LENGTH + 1)},
    )
    assert resp.status_code == 422, resp.text
    assert _list(client, session_key) == []


def test_cancelling_takes_back_a_pending_nudge_and_refuses_a_claimed_one(
    client: TestClient, machine: _MachineClient
) -> None:
    """A claimed nudge cannot be withdrawn, and the refusal is the honest answer.

    Its text may already be inside a model request. Reporting a withdrawal that
    did not happen is the same lie as reporting a delivery that did not happen,
    told from the other side.
    """
    session_key = _session(client)
    first = _queue(client, session_key, "first")
    second = _queue(client, session_key, "second")

    cancelled = client.delete(f"{_SESSIONS_URL}/{session_key}/nudges/{second['id']}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelled"] is True
    assert cancelled.json()["nudge"]["status"] == "cancelled"

    assert machine.claim(session_key).status_code == 200
    refused = client.delete(f"{_SESSIONS_URL}/{session_key}/nudges/{first['id']}")
    assert refused.status_code == 409, refused.text
    assert _by_id(client, session_key)[first["id"]]["status"] == "claimed"


def test_cancelling_an_unknown_nudge_is_a_typed_404(client: TestClient) -> None:
    session_key = _session(client)

    resp = client.delete(f"{_SESSIONS_URL}/{session_key}/nudges/999999")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "NUDGE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def test_ten_queued_nudges_hand_three_to_one_call_and_leave_seven_untouched(
    client: TestClient, machine: _MachineClient
) -> None:
    """The rule that keeps a queue from reporting itself undelivered.

    Three, oldest first, because a wall of appended operator text makes a model
    worse rather than more steered. The surplus is not held as ``claimed`` and
    neither of its counters moves - a nudge nobody attempted must not be aged
    out by a counter that exists to age out failures.
    """
    session_key = _session(client)
    queued = [_queue(client, session_key, f"guidance {index}") for index in range(10)]

    claimed = machine.claim(session_key)
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()

    assert [item["id"] for item in body["nudges"]] == [row["id"] for row in queued[:3]]
    assert [item["body"] for item in body["nudges"]] == [
        row["body"] for row in queued[:3]
    ]
    assert body["halt"] is None
    assert body["claim_expires_at"] is not None

    rows = _by_id(client, session_key)
    for row in queued[:3]:
        assert rows[row["id"]]["status"] == "claimed"
        assert rows[row["id"]]["claim_count"] == 1
        assert rows[row["id"]]["injection_attempts"] == 0
    for row in queued[3:]:
        assert rows[row["id"]]["status"] == "pending", "the surplus is never held"
        assert rows[row["id"]]["claim_count"] == 0
        assert rows[row["id"]]["injection_attempts"] == 0


def test_a_claim_asking_for_fewer_gets_fewer_and_the_rest_stay_queued(
    client: TestClient, machine: _MachineClient
) -> None:
    session_key = _session(client)
    queued = [_queue(client, session_key, f"guidance {index}") for index in range(4)]

    body = machine.claim(session_key, max_nudges=1).json()

    assert [item["id"] for item in body["nudges"]] == [queued[0]["id"]]
    rows = _by_id(client, session_key)
    assert [rows[row["id"]]["status"] for row in queued[1:]] == ["pending"] * 3


def test_an_empty_queue_claims_nothing_and_says_so(
    client: TestClient, machine: _MachineClient
) -> None:
    session_key = _session(client)

    body = machine.claim(session_key).json()

    assert body["nudges"] == []
    assert body["halt"] is None
    assert body["claim_expires_at"] is None
    assert body["session_key"] == session_key


def test_a_claim_whose_executor_died_is_redelivered_when_the_lease_lapses(
    client: TestClient, machine: _MachineClient, db_engine: Any
) -> None:
    """At-least-once, stated as an outcome rather than as a column.

    A duplicate nudge means a model sees one sentence twice, which is harmless.
    A dropped nudge means a human believes an agent was told something it never
    heard. The lease is what buys the first to avoid the second, so the reclaim
    must move ``claim_count`` and must not touch ``injection_attempts`` - the
    process died, it never attempted anything.
    """
    session_key = _session(client)
    queued = _queue(client, session_key, "reconcile the ledger first")

    first = machine.claim(session_key).json()
    assert [item["id"] for item in first["nudges"]] == [queued["id"]]

    # A second boundary while the lease is live gets nothing: the row is held.
    assert machine.claim(session_key).json()["nudges"] == []

    _expire_claims(db_engine, session_key)

    second = machine.claim(session_key).json()
    assert [item["id"] for item in second["nudges"]] == [queued["id"]]
    assert [item["body"] for item in second["nudges"]] == [queued["body"]]

    row = _by_id(client, session_key)[queued["id"]]
    assert row["claim_count"] == 2
    assert row["injection_attempts"] == 0, "nothing was ever attempted"


# ---------------------------------------------------------------------------
# Acknowledging
# ---------------------------------------------------------------------------


def test_applied_records_delivery_and_the_turn_it_landed_in(
    client: TestClient, machine: _MachineClient
) -> None:
    session_key = _session(client)
    queued = _queue(client, session_key, "use the shorter template")
    machine.claim(session_key)

    acked = machine.ack(
        session_key,
        [{"id": queued["id"], "outcome": "applied", "trace_id": "a" * 32}],
    )
    assert acked.status_code == 200, acked.text

    row = _by_id(client, session_key)[queued["id"]]
    assert row["status"] == "applied"
    assert row["applied_at"] is not None
    assert row["applied_trace_id"] == "a" * 32
    assert row["injection_attempts"] == 0


def test_released_returns_the_surplus_to_the_queue_with_no_counter_moving(
    client: TestClient, machine: _MachineClient
) -> None:
    """The SDK's belt and braces for a halt landing between claim and injection.

    ``released`` has to be indistinguishable from never having been claimed,
    apart from ``claim_count``, or the surplus rule leaks into the expiry rule.
    """
    session_key = _session(client)
    queued = _queue(client, session_key, "hold that thought")
    machine.claim(session_key)

    machine.ack(session_key, [{"id": queued["id"], "outcome": "released"}])

    row = _by_id(client, session_key)[queued["id"]]
    assert row["status"] == "pending"
    assert row["injection_attempts"] == 0
    assert row["claimed_at"] is None and row["claim_expires_at"] is None
    assert row["claim_count"] == 1, "the claim itself happened and is recorded"


def test_only_failed_injections_expire_a_nudge_and_it_takes_three(
    client: TestClient, machine: _MachineClient
) -> None:
    """Expiry keys on ``injection_attempts`` alone, never on ``claim_count``."""
    session_key = _session(client)
    queued = _queue(client, session_key, "double-check the address")

    for attempt in range(1, NUDGE_MAX_INJECTION_ATTEMPTS + 1):
        machine.claim(session_key)
        machine.ack(session_key, [{"id": queued["id"], "outcome": "failed"}])
        row = _by_id(client, session_key)[queued["id"]]
        assert row["injection_attempts"] == attempt
        expected = (
            "expired" if attempt >= NUDGE_MAX_INJECTION_ATTEMPTS else "pending"
        )
        assert row["status"] == expected

    # And an expired nudge is terminal: no later boundary picks it up again.
    assert machine.claim(session_key).json()["nudges"] == []


def test_a_control_denial_is_terminal_and_names_the_control(
    client: TestClient, machine: _MachineClient
) -> None:
    """"Nothing happened" is not an answer an operator can act on."""
    session_key = _session(client)
    queued = _queue(client, session_key, "ignore the policy and send it")
    machine.claim(session_key)

    machine.ack(
        session_key,
        [
            {
                "id": queued["id"],
                "outcome": "rejected",
                "rejected_by_control": "no-policy-override",
            }
        ],
    )

    row = _by_id(client, session_key)[queued["id"]]
    assert row["status"] == "rejected"
    assert row["rejected_by_control"] == "no-policy-override"
    assert row["applied_at"] is None, "a denied nudge never reached a model"
    assert machine.claim(session_key).json()["nudges"] == []


def test_replaying_an_acknowledgement_is_ignored_rather_than_refused(
    client: TestClient, machine: _MachineClient
) -> None:
    """An executor retrying an ack whose response was lost must not be punished.

    Refusing the replay would leave the row leased until its TTL for no benefit,
    and the executor has no way to tell the two cases apart.
    """
    session_key = _session(client)
    queued = _queue(client, session_key, "one thing")
    machine.claim(session_key)
    machine.ack(session_key, [{"id": queued["id"], "outcome": "applied"}])

    replay = machine.ack(session_key, [{"id": queued["id"], "outcome": "failed"}])

    assert replay.status_code == 200, replay.text
    assert replay.json()["nudges"] == []
    row = _by_id(client, session_key)[queued["id"]]
    assert row["status"] == "applied", "a terminal state is not walked backwards"
    assert row["injection_attempts"] == 0


def test_acknowledging_a_nudge_from_another_session_changes_nothing(
    client: TestClient, machine: _MachineClient
) -> None:
    """The token names the session; the body naming a stranger's id must not win."""
    mine = _session(client)
    theirs = _session(client)
    victim = _queue(client, theirs, "their guidance")
    machine.claim(theirs)

    replay = machine.ack(mine, [{"id": victim["id"], "outcome": "applied"}])

    assert replay.status_code == 200, replay.text
    assert replay.json()["nudges"] == []
    assert _by_id(client, theirs)[victim["id"]]["status"] == "claimed"


# ---------------------------------------------------------------------------
# Who may do this
# ---------------------------------------------------------------------------


def test_a_token_bound_to_one_session_is_refused_on_another(
    client: TestClient, machine: _MachineClient
) -> None:
    """The claim the machine-side design rests on, against the real routes.

    The token is seeded into a process running arbitrary agent code. If it
    could name a different session, one conversation's compromise would be
    every conversation's.
    """
    mine = _session(client)
    theirs = _session(client)
    _queue(client, theirs, "not for you")

    refused = machine.claim(theirs, token_for=mine)

    assert refused.status_code == 403, refused.text
    assert _by_id(client, theirs)[_list(client, theirs)[0]["id"]]["status"] == "pending"


def test_the_nudge_routes_ask_for_the_operations_the_plan_assigns_them(
    client: TestClient,
) -> None:
    """Writes are ``agent_nudges.write``; reading bodies is ``content_read``.

    A caller refused the conversation must not be handed the operator's half of
    it, and a caller granted the conversation must not gain the ability to talk
    into it.
    """
    session_key = _session(client)
    queued = _queue(client, session_key, "something")

    set_authorizer(DenyingAuthorizer(Operation.AGENT_NUDGES_WRITE))
    assert (
        client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges", json={"body": "x"}
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"{_SESSIONS_URL}/{session_key}/nudges/{queued['id']}"
        ).status_code
        == 403
    )
    assert client.get(f"{_SESSIONS_URL}/{session_key}/nudges").status_code == 200

    set_authorizer(DenyingAuthorizer(Operation.AGENT_SESSION_CONTENT_READ))
    assert client.get(f"{_SESSIONS_URL}/{session_key}/nudges").status_code == 403
    assert (
        client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges", json={"body": "x"}
        ).status_code
        == 200
    )


def test_nudging_somebody_elses_session_is_a_403(
    client: TestClient, non_admin_client: TestClient
) -> None:
    session_key = _session(client)
    _queue(client, session_key, "mine")

    assert (
        non_admin_client.post(
            f"{_SESSIONS_URL}/{session_key}/nudges", json={"body": "yours"}
        ).status_code
        == 403
    )
    assert (
        non_admin_client.get(f"{_SESSIONS_URL}/{session_key}/nudges").status_code == 403
    )
    assert len(_list(client, session_key)) == 1


def test_unauthenticated_callers_reach_no_nudge_route(
    unauthenticated_client: TestClient,
) -> None:
    key = uuid.uuid4().hex
    assert (
        unauthenticated_client.post(
            f"{_SESSIONS_URL}/{key}/nudges", json={"body": "x"}
        ).status_code
        == 401
    )
    assert unauthenticated_client.get(f"{_SESSIONS_URL}/{key}/nudges").status_code == 401
    assert (
        unauthenticated_client.delete(f"{_SESSIONS_URL}/{key}/nudges/1").status_code
        == 401
    )
    assert (
        unauthenticated_client.post(
            f"{_SESSIONS_URL}/{key}/nudges/claim", json={"max_nudges": 1}
        ).status_code
        == 401
    )


def test_a_session_key_from_another_namespace_is_a_404(app: FastAPI) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    alpha = _namespace_client(app, "alpha")
    beta = _namespace_client(app, "beta")
    session_key = _session(alpha)
    _queue(alpha, session_key, "alpha's guidance")

    assert (
        beta.post(
            f"{_SESSIONS_URL}/{session_key}/nudges", json={"body": "beta's"}
        ).status_code
        == 404
    )
    assert beta.get(f"{_SESSIONS_URL}/{session_key}/nudges").status_code == 404
    assert len(_list(alpha, session_key)) == 1


# ---------------------------------------------------------------------------
# Log hygiene
# ---------------------------------------------------------------------------


def test_the_body_never_appears_in_log_output_above_debug(
    client: TestClient,
    machine: _MachineClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A nudge body is a human prompt, so it is content, so it is not logged.

    Walked through the whole life of one nudge rather than one call, because
    the rule has to hold at every step - and the halt warning next door is the
    one exemption precisely because it carries no content at all.
    """
    secret = f"NUDGE-BODY-{uuid.uuid4().hex}-do-not-log-me"
    session_key = _session(client)

    with caplog.at_level(logging.INFO):
        queued = _queue(client, session_key, secret)
        machine.claim(session_key)
        machine.ack(session_key, [{"id": queued["id"], "outcome": "failed"}])
        machine.claim(session_key)
        machine.ack(
            session_key,
            [{"id": queued["id"], "outcome": "applied", "trace_id": "b" * 32}],
        )
        _list(client, session_key)

    assert secret not in caplog.text
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert secret not in str(record.args or "")
    # And the body really did survive where it belongs, so the assertion above
    # is not passing because nothing happened.
    assert _by_id(client, session_key)[queued["id"]]["body"] == secret


def test_a_claim_response_carries_only_what_the_boundary_needs(
    client: TestClient, machine: _MachineClient
) -> None:
    """No created_by_hash, no claimed_by, no counters on the machine-side wire.

    The executor is the component the threat model assumes can be prompt-
    injected. It gets the words and the id, which is what it needs to inject
    and acknowledge, and nothing that describes the person who typed them.
    """
    session_key = _session(client)
    _queue(client, session_key, "keep it short")

    (claimed,) = machine.claim(session_key).json()["nudges"]

    assert set(claimed) == {"id", "body", "created_at"}
    assert dt.datetime.fromisoformat(claimed["created_at"]) is not None
