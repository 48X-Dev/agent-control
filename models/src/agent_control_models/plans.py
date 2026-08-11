"""What an agent says it is going to do, and how far along it says it is."""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from .base import BaseModel

PLAN_MAX_STEPS = 40
"""Ceiling on one declared plan.

Not a guardrail, a bound: these rows are written by a model-driven process, and
a plan longer than this is not a plan a person can read in a rail beside a
transcript. A longer declaration is refused rather than truncated, because a
silently shortened plan is a plan whose last step can never be marked."""

PLAN_MAX_REVISIONS = 20
"""Ceiling on how many times one session's plan may be re-declared.

An agent that replans in a loop would otherwise write unbounded rows against a
session under a credential that is meant only to report progress. Past this the
answer is 429 and the existing plan stands."""

PLAN_STEP_TITLE_MAX_LENGTH = 255
PLAN_NOTE_MAX_LENGTH = 2000

PlanStepTitle = Annotated[
    str,
    StringConstraints(min_length=1, max_length=PLAN_STEP_TITLE_MAX_LENGTH),
]
"""One step, as the agent worded it.

Model-authored text on its way to an operator console, so it renders as plain
text like every other body in the chat panel: no markdown, no markup."""

PlanStepNote = Annotated[
    str,
    StringConstraints(min_length=1, max_length=PLAN_NOTE_MAX_LENGTH),
]
"""Whatever the agent wanted to say about how a step went. Also plain text."""


class PlanStepStatus(StrEnum):
    """Where the agent says one step is."""

    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class PlanStep(BaseModel):
    """One step of one revision, with the agent's own status for it."""

    index: int = Field(
        ...,
        ge=0,
        description="0-based position in the plan, dense within a revision.",
    )
    title: str = Field(..., description="The step as the agent declared it.")
    status: PlanStepStatus = Field(..., description="Where the agent says this step is.")
    note: str | None = Field(
        default=None,
        description="The agent's note on this step, if it left one.",
    )
    updated_at: dt.datetime = Field(
        ...,
        description=(
            "When this step was last written. The basis for showing staleness: "
            "a plan nobody has touched for an hour is an old plan, not a plan "
            "that is 40% done."
        ),
    )


class Plan(BaseModel):
    """The plan an agent most recently declared for one session."""

    session_key: str = Field(..., description="Session this plan belongs to.")
    revision: int = Field(..., ge=1, description="Revision these steps belong to. Highest wins.")
    revision_count: int = Field(
        ...,
        ge=1,
        description=(
            "How many plans this agent has declared for this session. More than "
            "one means it replanned, which is worth showing rather than hiding."
        ),
    )
    steps: list[PlanStep] = Field(default_factory=list, description="Steps in declared order.")
    declared_at: dt.datetime = Field(..., description="When this revision was declared.")
    last_updated_at: dt.datetime = Field(
        ...,
        description=(
            "Most recent write to any step of this revision. Equal to "
            "declared_at until the agent marks something."
        ),
    )


class PlanResponse(BaseModel):
    """The plan for one session, or the plain fact that there is not one."""

    session_key: str = Field(..., description="Session this answer is about.")
    plan: Plan | None = Field(
        default=None,
        description="The current revision, or null when no plan was declared.",
    )


# =============================================================================
# Machine side: the agent's own writes
# =============================================================================


class DeclarePlanRequest(BaseModel):
    """Declare, or re-declare, the plan for this session."""

    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStepTitle] = Field(
        ...,
        min_length=1,
        max_length=PLAN_MAX_STEPS,
        description=(
            "The steps, in order. Each becomes a step at its own 0-based index, "
            "starting at 'pending'."
        ),
    )


class UpdatePlanStepRequest(BaseModel):
    """Mark one step of one revision."""

    model_config = ConfigDict(extra="forbid")

    status: PlanStepStatus = Field(..., description="Where this step now is.")
    note: PlanStepNote | None = Field(
        default=None,
        description=(
            "Optional note. Replaces whatever note the step carried; omitting "
            "it leaves the existing one alone."
        ),
    )
