"""What an agent says it is going to do, and how far along it says it is.

This is the only honest progress signal in this stack, and the word *says* is
doing all the work in both sentences above.

An executor emits events. Events are not progress: counting them produces a
number that moves, looks like completion, and means nothing. There is no
percentage anywhere in this module and none may be derived from it downstream -
not from event counts, not from steps done over steps declared. A five-step
plan with two steps done is "2 of 5 steps marked done by the agent", which is a
claim with an author, and it is not "40% complete", which is a measurement
nobody took.

So the models here describe a **declaration**. The agent calls ``declare_plan``
and later marks its own steps. Everything a console renders from this is the
agent's account of its own work, which is why the rail built on it is labelled
"Plan reported by the agent" and sits next to a link to the turn's trace. An
agent that lies about its progress is not something a schema can fix; what a
schema can do is refuse to launder the claim into a measurement, and put the
independent evidence beside it.

Two consequences show up in the shapes below.

**Revisions are explicit, never inferred.** Agents replan, and a re-declared
plan is a new revision rather than an edit of the old one. A step update
therefore names the revision it belongs to: if ``declare_plan`` writes revision
2 while a ``mark_step`` for revision 1 is still in flight, guessing "the latest
one" marks a step of the *new* plan done because a step of the *old* one
finished. A stale revision is refused instead.

**An abandoned plan stays visibly abandoned.** Steps keep their own
``updated_at`` and a plan carries ``last_updated_at``, so a console shows
staleness by time since the last update. Nothing here decays, completes itself,
or infers that work continues because a plan exists.
"""

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
    """Where the agent says one step is.

    ``skipped`` and ``failed`` are separate from ``done`` on purpose. Collapsing
    them would let a plan read as finished when a third of it was abandoned,
    which is the same failure as a percentage by a slower route.
    """

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
    status: PlanStepStatus = Field(
        ..., description="Where the agent says this step is."
    )
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
    """The plan an agent most recently declared for one session.

    Only the current revision's steps are carried. Earlier revisions are kept in
    the database because a replan is a thing that happened and the record should
    say so, but a console renders the highest revision and a count of how many
    there have been.
    """

    session_key: str = Field(..., description="Session this plan belongs to.")
    revision: int = Field(
        ..., ge=1, description="Revision these steps belong to. Highest wins."
    )
    revision_count: int = Field(
        ...,
        ge=1,
        description=(
            "How many plans this agent has declared for this session. More than "
            "one means it replanned, which is worth showing rather than hiding."
        ),
    )
    steps: list[PlanStep] = Field(
        default_factory=list, description="Steps in declared order."
    )
    declared_at: dt.datetime = Field(
        ..., description="When this revision was declared."
    )
    last_updated_at: dt.datetime = Field(
        ...,
        description=(
            "Most recent write to any step of this revision. Equal to "
            "declared_at until the agent marks something."
        ),
    )


class PlanResponse(BaseModel):
    """The plan for one session, or the plain fact that there is not one.

    ``plan`` is null when the agent never declared one, which is an ordinary
    state and not an error. A console showing this has to fall back to what is
    actually known - how many turns have run, how long the session has been
    open, and the trace of the last turn - rather than to a progress bar with no
    plan behind it.
    """

    session_key: str = Field(..., description="Session this answer is about.")
    plan: Plan | None = Field(
        default=None,
        description="The current revision, or null when no plan was declared.",
    )


# =============================================================================
# Machine side: the agent's own writes
# =============================================================================


class DeclarePlanRequest(BaseModel):
    """Declare, or re-declare, the plan for this session.

    Every call writes a **new revision**. There is no partial edit of a declared
    plan and no way to append a step to one: a plan that changed is a new plan,
    and recording it as one is what lets a console say "revised" instead of
    quietly showing different steps than the ones a person read a minute ago.
    """

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
    """Mark one step of one revision.

    The revision and the index are path parameters rather than body fields, so a
    stale update is refused by the resource it addressed rather than by a field
    somebody could omit. Marking a step of a superseded plan is a 409; marking a
    step that plan does not have is a 422, and neither writes anything.
    """

    model_config = ConfigDict(extra="forbid")

    status: PlanStepStatus = Field(..., description="Where this step now is.")
    note: PlanStepNote | None = Field(
        default=None,
        description=(
            "Optional note. Replaces whatever note the step carried; omitting "
            "it leaves the existing one alone."
        ),
    )
