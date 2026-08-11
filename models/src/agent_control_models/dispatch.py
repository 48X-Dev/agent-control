"""The ceilings that bound the dispatch loop, and the switches that stop it."""

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
    """What is left of this namespace's hour, counted in Postgres."""

    max_turns_per_hour: int = Field(..., ge=0)
    turns_used_this_hour: int = Field(..., ge=0)
    turns_remaining_this_hour: int = Field(..., ge=0)
    max_tasks_per_hour: int = Field(..., ge=0)
    tasks_created_this_hour: int = Field(..., ge=0)
    tasks_remaining_this_hour: int = Field(..., ge=0)
    window_started_at: dt.datetime = Field(..., description="When the current turn window opened.")
    window_resets_at: dt.datetime = Field(
        ..., description="When the turn counter next rolls back to zero."
    )


class DispatchStateSnapshot(BaseModel):
    """One namespace's dispatch ceilings, and whether either switch is thrown."""

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
    """Level 3. Refuse every new session and every new turn in this namespace."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=STOP_REASON_MAX_LENGTH)


class DispatchStateResponse(BaseModel):
    """The state after a pause, a resume, a halt or a release."""

    state: DispatchStateSnapshot = Field(...)


class HaltFleetResponse(BaseModel):
    """Level 2, and its honest limits are in these three numbers."""

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
