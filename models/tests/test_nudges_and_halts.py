"""The wire contract for the two human actions on a running agent.

These models are the shared boundary: the server validates against them, both
SDKs are generated from them, and the executor's claim and acknowledgement
bodies are checked by them. So the assertions worth having here are the ones
that hold a boundary rather than the ones that restate a field list.

Three of them are security properties, not tidiness:

* every request model forbids unknown fields, so an executor cannot smuggle a
  status change through an endpoint that only enriches a tool name;
* ``CreateHaltRequest`` has **no** fields at all, which is what keeps stopping
  an agent from becoming a free-text channel into a model's context the way a
  nudge deliberately is - under control evaluation - and a halt deliberately
  is not;
* ``HaltToolName`` is a strict identifier, because it is the single field in
  the design carrying bytes chosen by a process running arbitrary agent code
  on their way into an operator console.
"""

from __future__ import annotations

import datetime as dt

import pytest
from agent_control_models import (
    HALT_TOOL_NAME_MAX_LENGTH,
    MAX_ACKS_PER_REQUEST,
    NUDGE_BODY_MAX_LENGTH,
    NUDGE_MAX_PER_MODEL_CALL,
    AckHaltRequest,
    AckNudgesRequest,
    ClaimHaltRequest,
    ClaimNudgesRequest,
    CreateHaltRequest,
    CreateNudgeRequest,
    Halt,
    HaltBoundary,
    HaltMode,
    HaltStatus,
    Nudge,
    NudgeAck,
    NudgeAckOutcome,
    NudgeStatus,
)
from pydantic import ValidationError

NOW = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# What a caller may send
# ---------------------------------------------------------------------------


def test_a_nudge_body_has_a_floor_and_a_ceiling() -> None:
    """Empty guidance is not guidance, and the ceiling is a bill.

    Every character is billed on the model call that carries it and on every
    later call carrying the history, three at a time.
    """
    assert CreateNudgeRequest(body="x" * NUDGE_BODY_MAX_LENGTH).body

    with pytest.raises(ValidationError):
        CreateNudgeRequest(body="")
    with pytest.raises(ValidationError):
        CreateNudgeRequest(body="x" * (NUDGE_BODY_MAX_LENGTH + 1))


def test_stopping_an_agent_carries_no_operator_text() -> None:
    """The absence is the design.

    A ``reason`` field would be free text from an AUTHENTICATED caller heading
    for a model's context. What a stopped agent is shown is a constant the SDK
    authors, so a halt cannot become the unevaluated channel the nudge path
    spends its whole delivery mechanism keeping under control evaluation.
    """
    assert CreateHaltRequest().model_dump() == {}

    with pytest.raises(ValidationError):
        CreateHaltRequest(reason="because I said so")


@pytest.mark.parametrize(
    "model",
    [CreateNudgeRequest, ClaimNudgesRequest, AckNudgesRequest, ClaimHaltRequest, AckHaltRequest],
)
def test_every_request_model_refuses_fields_it_does_not_know(model: type) -> None:
    """An ignored unknown field is a caller believing something happened.

    On the machine side it is worse than that: the acknowledgement routes exist
    to write one field each, and silently dropping an extra one is how a
    forgiving parser becomes the place a status gets changed by a body that was
    never supposed to carry one.
    """
    with pytest.raises(ValidationError):
        model.model_validate({"totally_unknown": "value"})


def test_a_claim_cannot_ask_for_more_than_one_model_call_can_hold() -> None:
    """The cap is enforced in the request as well as in the service.

    Not defence in depth for its own sake: the SDK sends its remaining
    per-invocation budget here, and a request that could name a larger number
    would make the server's cap the only thing between a queue and a wall of
    appended operator text.
    """
    assert ClaimNudgesRequest().max_nudges == NUDGE_MAX_PER_MODEL_CALL
    assert ClaimNudgesRequest(max_nudges=1).max_nudges == 1

    with pytest.raises(ValidationError):
        ClaimNudgesRequest(max_nudges=NUDGE_MAX_PER_MODEL_CALL + 1)
    with pytest.raises(ValidationError):
        ClaimNudgesRequest(max_nudges=0)


def test_an_acknowledgement_list_is_bounded() -> None:
    """The server resolves one row per entry inside a transaction holding the
    session lock, so an unbounded list is an unbounded number of queries under
    a lock every model boundary needs."""
    acks = [
        NudgeAck(id=index, outcome=NudgeAckOutcome.RELEASED)
        for index in range(MAX_ACKS_PER_REQUEST)
    ]
    assert len(AckNudgesRequest(acks=acks).acks) == MAX_ACKS_PER_REQUEST

    with pytest.raises(ValidationError):
        AckNudgesRequest(acks=[*acks, NudgeAck(id=99, outcome=NudgeAckOutcome.RELEASED)])


@pytest.mark.parametrize(
    "tool_name",
    [
        "send_email",
        "_private",
        "tools.send-email",
        "x" * HALT_TOOL_NAME_MAX_LENGTH,
    ],
)
def test_a_plausible_tool_name_is_accepted(tool_name: str) -> None:
    assert ClaimHaltRequest(boundary="tool", tool_name=tool_name).tool_name == tool_name


@pytest.mark.parametrize(
    "tool_name",
    [
        "",
        "send email",
        "9lives",
        "-leading-dash",
        "send_email; DROP TABLE agents",
        "<script>alert(1)</script>",
        "sénd_email",
        "x" * (HALT_TOOL_NAME_MAX_LENGTH + 1),
    ],
)
def test_a_tool_name_that_is_not_a_strict_identifier_is_refused(tool_name: str) -> None:
    """The one field carrying executor-chosen bytes into an operator console."""
    with pytest.raises(ValidationError):
        ClaimHaltRequest(boundary="tool", tool_name=tool_name)
    with pytest.raises(ValidationError):
        AckHaltRequest(id=1, applied_tool_name=tool_name)


def test_a_boundary_is_one_of_three_named_places() -> None:
    assert ClaimHaltRequest(boundary="model").tool_name is None

    with pytest.raises(ValidationError):
        ClaimHaltRequest(boundary="whenever")


# ---------------------------------------------------------------------------
# What a caller is told
# ---------------------------------------------------------------------------


def _nudge(**overrides: object) -> Nudge:
    payload: dict[str, object] = {
        "id": 1,
        "session_key": "sess-abc",
        "body": "check the totals",
        "status": NudgeStatus.PENDING,
        "created_at": NOW,
        "claim_count": 0,
        "injection_attempts": 0,
    }
    payload.update(overrides)
    return Nudge.model_validate(payload)


def _halt(**overrides: object) -> Halt:
    payload: dict[str, object] = {
        "id": 1,
        "session_key": "sess-abc",
        "target_trace_id": "a" * 32,
        "mode": HaltMode.GRACEFUL,
        "status": HaltStatus.PENDING,
        "created_at": NOW,
    }
    payload.update(overrides)
    return Halt.model_validate(payload)


def test_a_queued_nudge_says_nothing_about_delivery() -> None:
    """A panel reading these fields must not be able to render "delivered"."""
    nudge = _nudge()

    assert nudge.applied_at is None
    assert nudge.applied_trace_id is None
    assert nudge.claimed_at is None
    assert nudge.rejected_by_control is None


def test_the_counters_cannot_go_backwards_past_zero() -> None:
    with pytest.raises(ValidationError):
        _nudge(claim_count=-1)
    with pytest.raises(ValidationError):
        _nudge(injection_attempts=-1)


def test_an_applied_halt_is_not_yet_a_stopped_turn() -> None:
    """``applied`` is the executor's own word for it, and the executor is the
    party being stopped. The state a console may render as stopped is
    ``turn_ended_at``, which the server observes for itself."""
    halt = _halt(
        status=HaltStatus.APPLIED,
        applied_at=NOW,
        applied_at_boundary=HaltBoundary.TOOL,
        applied_tool_name="send_email",
    )

    # This package's base model serializes enums to their values, so a console
    # reading the JSON and a test reading the object see the same string.
    assert halt.status == HaltStatus.APPLIED.value
    assert halt.turn_ended_at is None


def test_the_statuses_are_exactly_the_ones_the_ui_has_copy_for() -> None:
    """A status nobody wrote copy for renders as a raw enum value in a console.

    The halt set has no ``claimed`` on purpose: claim and apply are one
    transaction, so the window it would describe does not exist.
    """
    assert {status.value for status in NudgeStatus} == {
        "pending",
        "claimed",
        "applied",
        "expired",
        "cancelled",
        "rejected",
    }
    assert {status.value for status in HaltStatus} == {"pending", "applied", "expired"}
    assert {boundary.value for boundary in HaltBoundary} == {"model", "tool", "process"}
    assert {mode.value for mode in HaltMode} == {"graceful", "restart"}


def test_only_one_acknowledgement_outcome_means_a_model_saw_the_text() -> None:
    assert {outcome.value for outcome in NudgeAckOutcome} == {
        "applied",
        "released",
        "failed",
        "rejected",
    }
    assert NudgeAckOutcome.APPLIED.value == NudgeStatus.APPLIED.value
    # ``released`` has no matching status: it puts the row back on the queue.
    assert "released" not in {status.value for status in NudgeStatus}
