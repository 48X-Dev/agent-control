"""Human guidance injected into a running agent, and its wire models."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from .base import BaseModel

NUDGE_BODY_MAX_LENGTH = 2000
"""Ceiling on one nudge. Long enough for a paragraph of redirection, short
enough that three of them appended to a request do not rewrite the
conversation. Every character is billed on this model call and on every later
one that carries the history."""

NUDGE_MAX_PER_MODEL_CALL = 3
"""How many nudges may be injected into one model call, oldest first.

The surplus goes back to ``pending`` untouched rather than being held as
``claimed``: a wall of appended operator text makes a model worse rather than
more steered, and a nudge nobody has attempted must not be aged out by a
counter."""

NUDGE_MAX_INJECTION_ATTEMPTS = 3
"""Injection attempts before a nudge is given up on. Counted only when an
injection was attempted and failed."""

NUDGE_CLAIM_TTL_SECONDS = 120
"""How long a claim holds a nudge before another claim may take it.

This is the redelivery window for an executor that died between claiming a
nudge and applying it. Shorter than a long tool call on purpose: the claim is
held across one model boundary, not across the turn."""

MAX_ACKS_PER_REQUEST = 20
"""Ceiling on one acknowledgement request.

A claim hands back at most ``NUDGE_MAX_PER_MODEL_CALL``, so an honest executor
never approaches this. It is here because the server resolves one row per
acknowledgement, and an unbounded list is an unbounded number of queries inside
one transaction that holds the session lock."""

MAX_PENDING_NUDGES_PER_SESSION = 20
"""Ceiling on the queue for one session.

Not a safety control - the control engine is - but a bound on a queue that any
authenticated caller can grow. Past this the answer is 429, because a queue
nobody can drain faster than three per model call is not a queue, it is a
backlog with a bill attached."""

NudgeBody = Annotated[
    str,
    StringConstraints(min_length=1, max_length=NUDGE_BODY_MAX_LENGTH),
]


class NudgeStatus(StrEnum):
    """Where one nudge is on its way to a model."""

    PENDING = "pending"
    CLAIMED = "claimed"
    APPLIED = "applied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


CANCELLABLE_NUDGE_STATUSES: frozenset[NudgeStatus] = frozenset({NudgeStatus.PENDING})
"""Only a nudge nobody has claimed can be withdrawn. Cancelling a claimed one
would report a withdrawal while the text may already be inside a model request,
which is a lie in the direction that matters."""


class NudgeAckOutcome(StrEnum):
    """What the executor did with a nudge it claimed."""

    APPLIED = "applied"
    RELEASED = "released"
    FAILED = "failed"
    REJECTED = "rejected"


class Nudge(BaseModel):
    """One queued piece of human guidance."""

    id: int = Field(..., description="Identifier, unique within the namespace.")
    session_key: str = Field(..., description="Session the nudge was queued for.")
    body: str = Field(..., description="The operator's exact text.")
    status: NudgeStatus = Field(..., description="Where the nudge is on its way.")
    created_at: dt.datetime = Field(..., description="When it was queued.")
    claimed_at: dt.datetime | None = Field(
        default=None, description="When an executor last took it for a model call."
    )
    claim_expires_at: dt.datetime | None = Field(
        default=None,
        description=("When an unacknowledged claim lapses and the nudge becomes claimable again."),
    )
    applied_at: dt.datetime | None = Field(
        default=None, description="When the text was handed to a model."
    )
    applied_trace_id: str | None = Field(
        default=None,
        description=(
            "Trace of the turn it landed in. The transcript marker aligns on "
            "this, and it is the deep link into that turn's guardrail "
            "decisions."
        ),
    )
    claim_count: int = Field(..., ge=0, description="How many times a claim has taken this nudge.")
    injection_attempts: int = Field(
        ...,
        ge=0,
        description=(
            "Injections that were attempted and failed. Expiry keys on this "
            "alone, never on claim_count."
        ),
    )
    rejected_by_control: str | None = Field(
        default=None,
        description=(
            "Control that denied this nudge, when the status is 'rejected'. "
            "Named so the refusal is something an operator can go and look at."
        ),
    )


class CreateNudgeRequest(BaseModel):
    """Queue one piece of guidance for the agent's next model call."""

    model_config = ConfigDict(extra="forbid")

    body: NudgeBody = Field(
        ...,
        description=(
            "What to tell the agent. Delivered as a user-role turn, evaluated "
            "by this deployment's controls on the way in."
        ),
    )


class CreateNudgeResponse(BaseModel):
    """The queued nudge."""

    nudge: Nudge = Field(..., description="The nudge as it was queued.")


class ListNudgesResponse(BaseModel):
    """Every nudge queued for one session, newest first."""

    session_key: str = Field(..., description="Session these nudges belong to.")
    nudges: list[Nudge] = Field(default_factory=list)


class CancelNudgeResponse(BaseModel):
    """Result of withdrawing a nudge."""

    cancelled: bool = Field(
        ..., description="Whether the nudge was withdrawn before anyone claimed it."
    )
    nudge: Nudge = Field(..., description="The nudge after the attempt.")


# =============================================================================
# Machine side: claim and acknowledge
# =============================================================================


class ClaimedNudge(BaseModel):
    """One nudge handed to an executor for a single model call."""

    id: int = Field(..., description="Identifier to acknowledge under.")
    body: str = Field(..., description="The operator's exact text.")
    created_at: dt.datetime = Field(..., description="When it was queued.")


class ClaimedHalt(BaseModel):
    """A stop that beat the nudge queue to this boundary."""

    id: int = Field(..., description="Identifier of the halt that was applied.")
    target_trace_id: str = Field(
        ..., description="Turn this halt was bound to when it was created."
    )
    mode: str = Field(..., description="How the stop was requested.")


class ClaimNudgesRequest(BaseModel):
    """Ask for the guidance and the stop waiting at this boundary."""

    model_config = ConfigDict(extra="forbid")

    max_nudges: int = Field(
        default=NUDGE_MAX_PER_MODEL_CALL,
        ge=1,
        le=NUDGE_MAX_PER_MODEL_CALL,
        description=(
            "How many nudges to claim, oldest first. Capped server-side at "
            f"{NUDGE_MAX_PER_MODEL_CALL}."
        ),
    )
    invocation_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Executor-supplied invocation identifier, for logs only. Never "
            "used as an authorization input."
        ),
    )


class ClaimNudgesResponse(BaseModel):
    """What this boundary should do."""

    session_key: str = Field(..., description="Session the claim ran against.")
    nudges: list[ClaimedNudge] = Field(default_factory=list)
    halt: ClaimedHalt | None = Field(
        default=None,
        description=(
            "A stop bound to the turn now in flight. When present, act on it and ignore the queue."
        ),
    )
    claim_expires_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When an unacknowledged claim lapses and these nudges become claimable again."
        ),
    )


class NudgeAck(BaseModel):
    """What happened to one claimed nudge."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Nudge being acknowledged.")
    outcome: NudgeAckOutcome = Field(..., description="What the executor did with it.")
    trace_id: str | None = Field(
        default=None,
        max_length=64,
        description="Trace of the turn it landed in, for an applied nudge.",
    )
    rejected_by_control: str | None = Field(
        default=None,
        max_length=255,
        description="Control that denied it, for a rejected nudge.",
    )


class AckNudgesRequest(BaseModel):
    """Report what became of claimed nudges."""

    model_config = ConfigDict(extra="forbid")

    acks: list[NudgeAck] = Field(
        default_factory=list,
        max_length=MAX_ACKS_PER_REQUEST,
        description="One entry per claimed nudge.",
    )


class AckNudgesResponse(BaseModel):
    """Nudges as they stand after the acknowledgement."""

    session_key: str = Field(..., description="Session the acknowledgement ran against.")
    nudges: list[Nudge] = Field(default_factory=list)
