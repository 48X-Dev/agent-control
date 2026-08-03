"""Workflows: the ordered list of agents a task is handed between.

A workflow is **server-side configuration on a team**, never anything the task
itself can express. That is the whole point of the file. An issue body, an
issue label and a YAML line all arrive from whoever has access to the source,
and none of them reaches a decision about which agent runs, how many turns it
gets, or what it is asked to do. Writing a workflow is ADMIN, at the same tier
as authoring a control, because naming agents and shaping prompts is the same
class of authority.

**Agents never talk to each other, and this module is where that stops being a
promise and becomes a shape.** A workflow is a list of steps the *dispatcher*
walks. Between two steps the dispatcher receives agent A's turn response over
HTTP, writes the text to ``agent_task_steps``, and starts a separate guarded
turn on a separate session for agent B. Nothing here is a channel: there is no
field on a step that names another step, no message an agent can address, and
no way for a step to learn that a later one exists. Everything that looks like
collaboration is the dispatcher holding both ends, and each hop is an ordinary
``POST /turns`` with the full guard stack.

Three shapes live here.

:class:`AgentWorkflow` is the stored configuration. :class:`AgentTaskPlan` is
that configuration **resolved for one task**: the same steps with each agent
name filled in from the step or from the team's default, and with the ones that
could not be resolved named rather than guessed at. :class:`AgentTaskChain` is
what actually happened, assembled from ``agent_task_steps`` and never from a
trace a caller supplied.

The plan and the chain are deliberately separate. A chain built only from the
step rows cannot say that a two-agent workflow stopped after its first agent,
because the second agent's row was never written; a chain built only from the
plan cannot say what any of them produced. Rendering the pair is what lets a
console say "the writer never ran" instead of showing a one-agent task that
looks complete.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from .agent_runtimes import AgentName
from .base import BaseModel
from .tasks import (
    FAILURE_CODE_MAX_LENGTH,
    FAILURE_DETAIL_MAX_LENGTH,
    MAX_STEPS_PER_TASK,
    MAX_TURNS_PER_STEP,
    STEP_BRIEF_MAX_LENGTH,
    STEP_OUTPUT_MAX_LENGTH,
    WORKFLOW_KEY_MAX_LENGTH,
    AgentTaskStatus,
    AgentTaskStepStatus,
    SourceRef,
    TaskKey,
    WorkflowKey,
)
from .teams import TeamSlug

WORKFLOW_DISPLAY_NAME_MAX_LENGTH = 255

WorkflowDisplayName = Annotated[
    str, StringConstraints(min_length=1, max_length=WORKFLOW_DISPLAY_NAME_MAX_LENGTH)
]

StepBrief = Annotated[str, StringConstraints(max_length=STEP_BRIEF_MAX_LENGTH)]


class RequiredOutput(StrEnum):
    """What a step has to produce for the chain to carry on.

    ``TEXT`` is the default and the only one that can be handed onward. A step
    that reports nothing fails with ``EMPTY_STEP_OUTPUT`` rather than passing an
    empty report to the next agent, because "the previous agent reported:
    (nothing)" is how the next agent invents the missing work and reports it
    confidently.

    ``NONE`` says this step is allowed to be silent, which is only ever true of
    the last step: there is nobody downstream to mislead.
    :meth:`AgentWorkflowSteps._a_silent_step_is_the_last_step` refuses the
    other arrangement at write time rather than at 3am.
    """

    TEXT = "text"
    NONE = "none"


class AgentWorkflowStep(BaseModel):
    """One hop: who runs it, what they are asked, and what bounds it.

    ``agent_name`` null means "the team's ``default_agent_name``". It is not a
    wildcard and it is not "any agent": when neither the step nor the team names
    one, the task is ``blocked`` with ``NO_AGENT_SELECTED`` and a human sets one
    of the two. Guessing here would be choosing the blast radius by accident,
    because agents differ in system prompt, in bound controls and in tools.

    ``brief`` is operator text and is the one part of the turn message that is
    not framed as data. It is written by somebody holding ADMIN, which is why it
    can be trusted at all.

    ``idempotent`` is an assertion by whoever wrote the workflow, not a proof,
    and the dispatcher currently ignores it. Section 11.3 permits one retry
    after a timeout only where cancellation has been confirmed for the executor
    kind in the deployment topology that will actually run, and that experiment
    has not been done. "If either guard cannot be evaluated, the flag is
    ignored" is the plan's own rule, and this is what ignoring it looks like:
    the field is stored and read back, and nothing acts on it.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: AgentName | None = Field(
        default=None,
        description=(
            "Agent that runs this step. Null falls back to the team's "
            "default_agent_name; when neither is set the task is blocked."
        ),
    )
    brief: StepBrief = Field(
        default="",
        description="What this step's agent is asked to do. Operator text, not data.",
    )
    max_turns: int = Field(
        default=1,
        ge=1,
        le=MAX_TURNS_PER_STEP,
        description=(
            "Ceiling on turns this step may spend. A ceiling, not a target: the "
            "dispatcher runs one turn per step today, which is inside every value."
        ),
    )
    required_output: RequiredOutput = Field(
        default=RequiredOutput.TEXT,
        description="Whether this step must produce text. 'none' is only valid on the last step.",
    )
    idempotent: bool = Field(
        default=False,
        description=(
            "An operator's assertion that re-running this step is harmless. "
            "Recorded and deliberately not acted on; see the class docstring."
        ),
    )


class AgentWorkflow(BaseModel):
    """An ordered list of steps, stored against a namespace and maybe a team.

    ``team_slug`` scopes which team's tasks may use it and supplies the default
    agent when a step does not pin one. A workflow with no team is usable from
    any team in the namespace and can only run steps that name their own agent.
    """

    workflow_key: WorkflowKey = Field(..., description="Stable key, unique in the namespace.")
    display_name: WorkflowDisplayName = Field(..., description="What a console calls it.")
    team_slug: TeamSlug | None = Field(
        default=None, description="Team this workflow belongs to, when it belongs to one."
    )
    steps: list[AgentWorkflowStep] = Field(
        ..., min_length=1, max_length=MAX_STEPS_PER_TASK, description="Ordered. Index is position."
    )
    created_at: dt.datetime = Field(...)
    updated_at: dt.datetime = Field(...)


def _a_silent_step_is_the_last_step(steps: list[AgentWorkflowStep]) -> list[AgentWorkflowStep]:
    """Refuse ``required_output: none`` anywhere but the end of the chain.

    A step that is permitted to say nothing, followed by a step that would be
    handed its report, is a chain with a hole in the middle. The next agent
    receives an empty prior-report block, has nothing to work from, and answers
    anyway - which is the failure the envelope's untrusted framing cannot help
    with, because there is no text to distrust.

    Refused at write time rather than at run time, because the run-time version
    of this refusal costs a claimed task and a turn nobody needed to pay for.
    """
    for index, step in enumerate(steps[:-1]):
        if RequiredOutput(step.required_output) is RequiredOutput.NONE:
            raise ValueError(
                f"Step {index} has required_output 'none' but step {index + 1} follows it. "
                "Only the last step of a workflow may be silent: an empty report handed "
                "to the next agent is how that agent invents the work it was not given."
            )
    return steps


class UpsertAgentWorkflowRequest(BaseModel):
    """Create or replace one workflow, keyed by ``workflow_key`` in the path.

    Replace semantics, deliberately. A workflow is a short list read in order,
    and a PATCH that could move one step in the middle is a way to change who
    runs step 2 without the reviewer of the change seeing steps 1 and 3. The
    whole list is written or none of it is.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: WorkflowDisplayName = Field(...)
    team_slug: TeamSlug | None = Field(
        default=None,
        description=(
            "Team this workflow runs for. Supplies the default agent for steps "
            "that do not name one."
        ),
    )
    steps: list[AgentWorkflowStep] = Field(..., min_length=1, max_length=MAX_STEPS_PER_TASK)

    @field_validator("steps")
    @classmethod
    def _silent_step_must_be_last(
        cls, steps: list[AgentWorkflowStep]
    ) -> list[AgentWorkflowStep]:
        return _a_silent_step_is_the_last_step(steps)


class AgentWorkflowResponse(BaseModel):
    """One workflow."""

    workflow: AgentWorkflow = Field(...)


class UpsertAgentWorkflowResponse(BaseModel):
    """The stored workflow, and whether this call created it."""

    workflow: AgentWorkflow = Field(...)
    created: bool = Field(..., description="True when the row did not exist before.")


class ListAgentWorkflowsResponse(BaseModel):
    """Every workflow in the namespace, ordered by key."""

    workflows: list[AgentWorkflow] = Field(default_factory=list)


class DeleteAgentWorkflowResponse(BaseModel):
    """What a delete did.

    Tasks already queued against a deleted workflow keep their ``workflow_key``
    and stop resolving, which shows up as ``blocked`` rather than as a task that
    quietly runs somebody else's steps. ``open_task_count`` is what the console
    warns with before the button is pressed.
    """

    success: bool = Field(...)
    workflow_key: str = Field(...)
    open_task_count: int = Field(
        ..., ge=0, description="Non-terminal tasks that named this workflow."
    )


# =============================================================================
# The plan: one workflow resolved for one task
# =============================================================================


class ResolvedWorkflowStep(BaseModel):
    """One planned step with its agent decided, or with nobody to run it.

    ``agent_name`` is null exactly when neither the step nor the team named an
    agent. It is reported rather than defaulted: a plan that silently filled the
    gap would be agent selection happening somewhere nobody reviewed.
    """

    step_index: int = Field(..., ge=0, lt=MAX_STEPS_PER_TASK)
    agent_name: AgentName | None = Field(
        default=None, description="Resolved agent, or null when nothing server-side names one."
    )
    agent_source: str = Field(
        ...,
        description=(
            "Where the agent came from: 'workflow_step', 'team_default', or "
            "'unresolved'. Never anything the task's source could express."
        ),
    )
    brief: StepBrief = Field(default="")
    max_turns: int = Field(..., ge=1, le=MAX_TURNS_PER_STEP)
    required_output: RequiredOutput = Field(default=RequiredOutput.TEXT)
    idempotent: bool = Field(default=False)


class AgentTaskPlan(BaseModel):
    """What is supposed to run on one task, before any of it has.

    ``implicit`` is true for the one-step fallback a team with no workflow gets.
    That step pins no agent and carries an empty brief, so a deployment that has
    configured nothing still runs one agent doing one thing - which is most of
    the value, and a design that demands a workflow before anything works does
    not get used.
    """

    task_key: TaskKey = Field(...)
    workflow_key: str = Field(..., max_length=WORKFLOW_KEY_MAX_LENGTH)
    display_name: str = Field(..., max_length=WORKFLOW_DISPLAY_NAME_MAX_LENGTH)
    implicit: bool = Field(
        ..., description="True when no stored workflow matched and the one-step fallback applies."
    )
    team_slug: TeamSlug | None = Field(default=None)
    steps: list[ResolvedWorkflowStep] = Field(default_factory=list)
    unresolved_step_indexes: list[int] = Field(
        default_factory=list,
        description=(
            "Steps with no agent. A dispatcher treats a plan with any of these "
            "as blocked with NO_AGENT_SELECTED rather than choosing one."
        ),
    )


class GetAgentTaskPlanResponse(BaseModel):
    """The resolved plan for one task."""

    plan: AgentTaskPlan = Field(...)


# =============================================================================
# The chain: what actually ran
# =============================================================================


class AgentTaskChainHop(BaseModel):
    """One position in the chain, planned and recorded together.

    ``ran`` false is a step that never started: either the chain stopped before
    reaching it, or it is still ahead of the dispatcher. That distinction is why
    this model carries the plan's fields as well as the step row's - a hop with
    no row is invisible in ``agent_task_steps``, and a chain that showed only
    the rows would render a stopped two-agent workflow as a finished one-agent
    task with nothing saying otherwise.

    ``turn_trace_id`` is this hop's own trace, minted by the server for its
    turn. It is a link to a forensic view and not the identity of the chain: the
    chain is these rows. A caller has never been able to supply it.
    """

    step_index: int = Field(..., ge=0)
    agent_name: AgentName | None = Field(default=None)
    brief: StepBrief = Field(default="")
    ran: bool = Field(..., description="Whether a step row exists for this position.")
    status: AgentTaskStepStatus | None = Field(
        default=None, description="Null when this hop never started."
    )
    session_key: str | None = Field(
        default=None,
        max_length=64,
        description="Null once the session is deleted, which is the ordinary end state.",
    )
    turn_trace_id: str | None = Field(
        default=None, max_length=64, description="This hop's own trace, for the rollup link."
    )
    output_text: str | None = Field(default=None, max_length=STEP_OUTPUT_MAX_LENGTH)
    output_truncated: bool = Field(default=False)
    attempts: int = Field(default=0, ge=0)
    failure_code: str | None = Field(default=None, max_length=FAILURE_CODE_MAX_LENGTH)
    failure_detail: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)
    started_at: dt.datetime | None = Field(default=None)
    ended_at: dt.datetime | None = Field(default=None)


class AgentTaskChain(BaseModel):
    """One task as a sequence of hops, assembled from its own step rows.

    Not from a trace. The existing rollup at ``GET /observability/traces/{id}``
    builds hops exclusively from control-execution events, so an agent with no
    bound control that fired contributes zero hops and vanishes from it: a
    three-agent chain where two have no controls renders as one agent, with
    nothing indicating the rest is missing. These rows show every hop whether or
    not a control fired, cannot 404, and carry a per-hop trace id for whoever
    wants the forensic view of one of them.

    ``chain_trace_id`` is minted by the server at claim time and is here for
    correlation only. It is not what this view is built from, and it is not
    accepted from a caller: the audited party does not author its own audit
    record.
    """

    task_key: TaskKey = Field(...)
    source_kind: str = Field(..., max_length=32)
    source_ref: SourceRef = Field(...)
    source_url: str | None = Field(default=None)
    title: str = Field(..., description="Untrusted. Written by whoever filed it.")
    team_slug: TeamSlug | None = Field(default=None)
    workflow_key: str = Field(..., max_length=WORKFLOW_KEY_MAX_LENGTH)
    workflow_display_name: str = Field(..., max_length=WORKFLOW_DISPLAY_NAME_MAX_LENGTH)
    status: AgentTaskStatus = Field(...)
    dry_run: bool = Field(...)
    chain_trace_id: str | None = Field(default=None, max_length=64)
    hops: list[AgentTaskChainHop] = Field(default_factory=list)
    hops_planned: int = Field(
        ...,
        ge=0,
        description="How many steps the workflow has *now*, which is not always how many ran.",
    )
    hops_ran: int = Field(..., ge=0, description="How many positions have a step row.")

    @property
    def plan_changed(self) -> bool:
        """More hops ran than the workflow now has steps.

        Somebody rewrote or deleted the workflow while the dispatcher was
        walking it. The chain still renders, and it renders **every** hop that
        ran: the step rows are the record of what the agents actually did, and
        trimming them to fit the current configuration would show an operator a
        shorter chain than the one they paid for.

        A flag rather than a refusal, because this is a read. A model that
        raised here would turn "the workflow was edited" into a 500 on the one
        page somebody opens to find out what happened.
        """
        return self.hops_ran > self.hops_planned

    failure_code: str | None = Field(default=None, max_length=FAILURE_CODE_MAX_LENGTH)
    failure_detail: str | None = Field(default=None, max_length=FAILURE_DETAIL_MAX_LENGTH)


class GetAgentTaskChainResponse(BaseModel):
    """One task's chain."""

    chain: AgentTaskChain = Field(...)


__all__ = [
    "WORKFLOW_DISPLAY_NAME_MAX_LENGTH",
    "AgentTaskChain",
    "AgentTaskChainHop",
    "AgentTaskPlan",
    "AgentWorkflow",
    "AgentWorkflowResponse",
    "AgentWorkflowStep",
    "DeleteAgentWorkflowResponse",
    "GetAgentTaskChainResponse",
    "GetAgentTaskPlanResponse",
    "ListAgentWorkflowsResponse",
    "RequiredOutput",
    "ResolvedWorkflowStep",
    "StepBrief",
    "UpsertAgentWorkflowRequest",
    "UpsertAgentWorkflowResponse",
    "WorkflowDisplayName",
]
