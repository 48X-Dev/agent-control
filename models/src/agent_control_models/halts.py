"""Stopping a running agent, and the wire models for it.

A *halt* is a human pressing stop. The word is chosen because every obvious
alternative is already taken or already a lie in this codebase: ``stop`` is
ordinary control flow in the Python SDK, ``cancel`` is both a nudge status and
the chat panel's abandon-the-request button, ``pause`` promises a resume
nothing here can deliver, and ``interrupt`` and ``abort`` promise immediacy.

What a halt actually does, stated once and repeated in every piece of copy
built on these models: it lands at the agent's **next boundary**. Before the
next model call, or before the next tool runs, whichever comes first. A tool
that has already started runs to completion and its side effect happens. That
is not a limitation of this implementation, it is a property of the executor,
and a stop button that implied otherwise would be discovered to be lying by
the first person who pressed it during a long tool call.

Two design decisions here differ from the nudge queue next door, in opposite
directions, which is why halts are their own table and their own module.

**A halt is bound to one turn.** ``target_trace_id`` is copied from the
session's live turn at creation and every read of the halt joins on it still
being the live turn. A halt with no bound turn would leak a stranger's stop
into somebody else's later turn, under a transcript marker blaming an operator
for stopping something they never saw.

**A halt is a latch, not a queue.** One row per turn, unconditionally, so
double-clicking stop is idempotent by construction rather than by service
logic. There is no claim state: claim and apply are one transaction, because
the alternative - the agent genuinely stopped, the acknowledgement lost, the
row swept to ``expired`` - tells an operator the stop never landed on an agent
that is already stopped, whose rational next move is to reach for something
blunter.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from .base import BaseModel

HALT_TOOL_NAME_MAX_LENGTH = 64
"""Ceiling on the executor-supplied tool name recorded against a halt."""

HaltToolName = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=HALT_TOOL_NAME_MAX_LENGTH,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$",
    ),
]
"""The one field in this design carrying executor-chosen bytes.

It arrives from a process running arbitrary agent code and lands in an operator
console, so it is pattern-checked against a strict identifier server-side and
capped, and it renders under the chat panel's plain-text rule like everything
else that came out of a model."""


class HaltMode(StrEnum):
    """How the stop was carried out.

    ``graceful`` is the one this module implements end to end: the SDK
    substitutes a blocked response at the next boundary and the turn ends
    normally, with the block visible in the transcript.

    ``restart`` describes a halt row written when an operator restarted the
    executor process instead. It is a record, not a request: those rows are
    inserted already applied, because a restart that unambiguously worked must
    not render as the one mechanism that did not.
    """

    GRACEFUL = "graceful"
    RESTART = "restart"


class HaltStatus(StrEnum):
    """Where one halt is.

    There is deliberately no ``claimed``. Claim and apply are one transaction,
    so the window a claimed state would describe does not exist.

    ``expired`` means the turn ended before the stop reached a boundary - most
    often because the turn ended on its own, sometimes because the executor
    died, which is the same outcome the operator wanted by a different route.
    A halt is never carried into a later turn.
    """

    PENDING = "pending"
    APPLIED = "applied"
    EXPIRED = "expired"


class HaltBoundary(StrEnum):
    """Where a halt landed.

    ``model`` ends the invocation outright and costs no model call. ``tool``
    prevents a tool body from running, which is the difference between
    stopping the agent before it sends the email and stopping it after.
    ``process`` means the executor was restarted under it.
    """

    MODEL = "model"
    TOOL = "tool"
    PROCESS = "process"


class Halt(BaseModel):
    """One operator stop, bound to one turn.

    ``applied`` on its own is **not** "stopped". The acknowledgement comes from
    the process being stopped, which is the party with the incentive to say it
    worked. The state a UI may render as stopped is ``turn_ended_at`` being
    set, which this server observes independently when the turn's liveness
    marker clears. Until then the honest copy is "stop acknowledged, waiting
    for the turn to end".
    """

    id: int = Field(..., description="Identifier, unique within the namespace.")
    session_key: str = Field(..., description="Session the halt belongs to.")
    target_trace_id: str = Field(
        ...,
        description=(
            "The one turn this halt can ever apply to. Copied from the "
            "session's live turn at creation."
        ),
    )
    mode: HaltMode = Field(..., description="How the stop was carried out.")
    status: HaltStatus = Field(..., description="Where the halt is.")
    created_at: dt.datetime = Field(..., description="When stop was pressed.")
    applied_at: dt.datetime | None = Field(
        default=None, description="When the executor blocked at a boundary."
    )
    applied_at_boundary: HaltBoundary | None = Field(
        default=None, description="Which boundary it landed at."
    )
    applied_tool_name: str | None = Field(
        default=None,
        description=(
            "Tool the agent was about to run, for a halt that landed at a tool "
            "boundary. Executor-supplied, pattern-checked, rendered as plain "
            "text."
        ),
    )
    turn_ended_at: dt.datetime | None = Field(
        default=None,
        description=(
            "When this server observed the turn actually end. This, not "
            "'applied', is what a UI may render as stopped."
        ),
    )


class CreateHaltRequest(BaseModel):
    """Stop the turn this session is running.

    No fields, and that is a constraint rather than an oversight. A halt
    carries no operator text: what the model is shown is a constant authored by
    the SDK, so stopping an agent cannot become the unevaluated free-text
    channel into a high-trust field that the nudge design spends its whole
    delivery mechanism closing. If a reason is ever wanted it is audit-only and
    never reaches a model.
    """

    model_config = ConfigDict(extra="forbid")


class CreateHaltResponse(BaseModel):
    """The halt now recorded against the live turn.

    ``created`` distinguishes the first press from a repeat. Both answer 200
    with the same row, because one turn has one halt and telling somebody their
    second click failed would invite a third.
    """

    halt: Halt = Field(..., description="The halt bound to the live turn.")
    created: bool = Field(
        ..., description="False when a halt was already recorded for this turn."
    )


class ListHaltsResponse(BaseModel):
    """Halts recorded against one session, newest first."""

    session_key: str = Field(..., description="Session these halts belong to.")
    halts: list[Halt] = Field(default_factory=list)


# =============================================================================
# Machine side: claim and acknowledge
# =============================================================================


class ClaimHaltRequest(BaseModel):
    """Ask whether this boundary should stop.

    Claim and apply are one statement, so this request carries where the stop
    would land. There is no separate apply call and no window between the two.
    """

    model_config = ConfigDict(extra="forbid")

    boundary: HaltBoundary = Field(
        ..., description="Boundary this claim is running at: 'model' or 'tool'."
    )
    tool_name: HaltToolName | None = Field(
        default=None,
        description="Tool about to run, at a tool boundary.",
    )
    invocation_id: str | None = Field(
        default=None,
        max_length=128,
        description="Executor-supplied invocation identifier, for logs only.",
    )


class ClaimHaltResponse(BaseModel):
    """Whether to block here."""

    session_key: str = Field(..., description="Session the claim ran against.")
    halt: Halt | None = Field(
        default=None,
        description="The halt that was applied, or null when there is nothing to do.",
    )


class AckHaltRequest(BaseModel):
    """Enrich an applied halt after the fact.

    Optional by design. The claim already moved the row to ``applied``, so
    losing this call costs one word of transcript copy rather than the truth of
    the record.
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., description="Halt to enrich.")
    applied_tool_name: HaltToolName | None = Field(
        default=None, description="Tool the agent was about to run."
    )


class AckHaltResponse(BaseModel):
    """The halt after the acknowledgement."""

    session_key: str = Field(..., description="Session the acknowledgement ran against.")
    halt: Halt = Field(..., description="The halt as it now stands.")
