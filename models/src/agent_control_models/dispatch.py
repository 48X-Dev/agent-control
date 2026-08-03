"""The ceilings that bound the dispatch loop, and the switches that stop it.

The loop runs outside this server. Everything in this module is inside it, and
that split is the whole point: **a budget enforced by the process being budgeted
is not a control.** A dispatcher in a retry loop, a second dispatcher started by
a different operator, a bad release, or any holder of an ordinary key calling
``POST /turns`` directly all spend without consulting a limit that lives in the
dispatcher's own memory.

So the numbers live in one row per namespace, and the refusal that reads them
sits on the turn path, in the transaction that already takes the session row.

Three things here are load-bearing rather than shape.

**"Budget" means turns, never money.** ``POST /run`` returns no token usage in
any shape this repo reads, so nothing in this stack can meter dollars. A turn
ceiling is a proxy whose error bars are the difference between a one-tool answer
and a twenty-tool agentic loop. Showing a turn count is fine; deriving a
currency figure from it would be a fabricated measurement, which is the same
mistake ``plans.py`` refuses to make with progress percentages.

**Every field on :class:`DispatchStateSnapshot` that a preview renders is
advisory.** The preview reports the budget so a confirm can say "the namespace
is paused" instead of queueing four rows that will never run. It is not the
enforcement point, and this sentence is here twice on purpose so nobody later
simplifies the server-side check away in favour of the number the console
already had.

**The four stop levels answer different questions and only one is
authoritative.** Level 1 (pause) stops new work and does not depend on the
dispatcher cooperating. Level 2 (fleet halt) is a *request* that lands only when
the executor can still reach us, and the console must not render it as a stop.
Level 3 (halt executors) is the authoritative one: one flag refuses every new
session and every new turn in the namespace, human chat included. Level 4 is a
runbook that kills processes, because nothing in an API stops a tool that is
already executing.
"""

from __future__ import annotations

import datetime as dt

from pydantic import ConfigDict, Field

from .base import BaseModel

DEFAULT_MAX_TASKS_PER_HOUR = 20
"""Imported tasks per namespace per hour. Enforced in the transaction that
inserts the rows, because that is the only place tasks are created."""

DEFAULT_MAX_TURNS_PER_HOUR = 60
"""Dispatch-origin turns per namespace per hour. Enforced inside
``_acquire_turn``. Human chat is not counted against it and keeps the existing
in-process quota, which is what keeps this from being a hot row on every turn in
the deployment."""

DISPATCH_WINDOW_SECONDS = 3600
"""The window both ceilings are counted over. One hour, fixed: a configurable
window is a second number an operator has to reason about to predict what the
first one means."""

STOP_REASON_MAX_LENGTH = 500
"""Free text an operator types into a pause or a stop. Rendered in the banner
beside who set it and when, because a stop nobody can see the state of is a stop
somebody presses twice and then works around."""


class DispatchBudget(BaseModel):
    """What is left of this namespace's hour, counted in Postgres.

    ``turns_used_this_hour`` is read off the counter the turn path increments,
    so it is exact rather than sampled. ``tasks_created_this_hour`` is counted
    from ``agent_tasks`` directly: tasks are created only by import, and a
    counter column for something already recorded as rows is a second source of
    truth waiting to disagree with the first.

    Both remaining figures are floored at zero. A ceiling lowered while a window
    is open would otherwise report a negative allowance, which reads as a bug
    rather than as "you are over".
    """

    max_turns_per_hour: int = Field(..., ge=0)
    turns_used_this_hour: int = Field(..., ge=0)
    turns_remaining_this_hour: int = Field(..., ge=0)
    max_tasks_per_hour: int = Field(..., ge=0)
    tasks_created_this_hour: int = Field(..., ge=0)
    tasks_remaining_this_hour: int = Field(..., ge=0)
    window_started_at: dt.datetime = Field(
        ..., description="When the current turn window opened."
    )
    window_resets_at: dt.datetime = Field(
        ..., description="When the turn counter next rolls back to zero."
    )


class DispatchStateSnapshot(BaseModel):
    """One namespace's dispatch ceilings, and whether either switch is thrown.

    ``paused_by_hash`` and ``halted_by_hash`` identify a *credential*, not a
    person: browser callers all hash identically because the session token
    carries no subject, so these answer "which key" and never "which human".
    Surfaced anyway, because an incident banner naming the API key that stopped
    the fleet is more useful than one naming nobody.
    """

    paused: bool = Field(..., description="Level 1. New work is refused; running turns are not.")
    paused_at: dt.datetime | None = Field(default=None)
    paused_by_hash: str | None = Field(
        default=None, description="Credential tag of whoever paused. Not a person."
    )
    paused_reason: str | None = Field(default=None)
    executors_halted: bool = Field(
        ...,
        description=(
            "Level 3, the authoritative stop. Every new session and every new "
            "turn in this namespace is refused, human chat included."
        ),
    )
    executors_halted_at: dt.datetime | None = Field(default=None)
    executors_halted_by_hash: str | None = Field(default=None)
    executors_halted_reason: str | None = Field(default=None)
    budget: DispatchBudget = Field(...)
    updated_at: dt.datetime = Field(...)


class GetDispatchStateResponse(BaseModel):
    """The state, for a banner and for a preview that wants to warn early."""

    state: DispatchStateSnapshot = Field(...)


class PauseDispatchRequest(BaseModel):
    """Level 1. Stop new work, and say why on the banner."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        max_length=STOP_REASON_MAX_LENGTH,
        description="Shown in the banner. A stop with no reason gets un-set by the next person.",
    )


class HaltExecutorsRequest(BaseModel):
    """Level 3. Refuse every new session and every new turn in this namespace.

    Separate from :class:`PauseDispatchRequest` only so the two cannot be
    confused at the call site: the request bodies are identical and the
    consequences are not. This one stops human chat as well, and the UI copy has
    to say so where the button is.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=STOP_REASON_MAX_LENGTH)


class DispatchStateResponse(BaseModel):
    """The state after a pause, a resume, a halt or a release."""

    state: DispatchStateSnapshot = Field(...)


class HaltFleetResponse(BaseModel):
    """Level 2, and its honest limits are in these three numbers.

    ``sessions_in_flight`` is what the statement looked at, ``halts_created`` is
    what it wrote, and the gap is sessions that already had a halt bound to the
    turn they are running. None of the three says a turn stopped: halt delivery
    is best-effort at the executor, which swallows a failed post and runs the
    tool anyway, so a halt lands only when the executor can still reach us.

    The console must render this as *requested*, never as stopped.
    """

    sessions_in_flight: int = Field(
        ..., ge=0, description="Sessions running a turn when the statement ran."
    )
    halts_created: int = Field(..., ge=0, description="Halt rows this call wrote.")
    already_halted: int = Field(
        ..., ge=0, description="Turns that already had a stop bound to them."
    )
    dispatch_sessions_in_flight: int = Field(
        ...,
        ge=0,
        description=(
            "How many of sessions_in_flight belong to a dispatch task. The rest "
            "are human chats, which a fleet stop also reaches."
        ),
    )


__all__ = [
    "DEFAULT_MAX_TASKS_PER_HOUR",
    "DEFAULT_MAX_TURNS_PER_HOUR",
    "DISPATCH_WINDOW_SECONDS",
    "STOP_REASON_MAX_LENGTH",
    "DispatchBudget",
    "DispatchStateResponse",
    "DispatchStateSnapshot",
    "GetDispatchStateResponse",
    "HaltExecutorsRequest",
    "HaltFleetResponse",
    "PauseDispatchRequest",
]
