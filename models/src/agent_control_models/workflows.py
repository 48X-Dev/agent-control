"""Workflows: the ordered list of agents a task is handed between."""

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
    """What a step has to produce for the chain to carry on."""

    TEXT = "text"
    NONE = "none"


class AgentWorkflowStep(BaseModel):
    """One hop: who runs it, what they are asked, and what bounds it."""

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
    """An ordered list of steps, stored against a namespace and maybe a team."""

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
    """Refuse ``required_output: none`` anywhere but the end of the chain."""
    for index, step in enumerate(steps[:-1]):
        if RequiredOutput(step.required_output) is RequiredOutput.NONE:
            raise ValueError(
                f"Step {index} has required_output 'none' but step {index + 1} follows it. "
                "Only the last step of a workflow may be silent: an empty report handed "
                "to the next agent is how that agent invents the work it was not given."
            )
    return steps


class UpsertAgentWorkflowRequest(BaseModel):
    """Create or replace one workflow, keyed by ``workflow_key`` in the path."""

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
    def _silent_step_must_be_last(cls, steps: list[AgentWorkflowStep]) -> list[AgentWorkflowStep]:
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
    """What a delete did."""

    success: bool = Field(...)
    workflow_key: str = Field(...)
    open_task_count: int = Field(
        ..., ge=0, description="Non-terminal tasks that named this workflow."
    )


# =============================================================================
# The plan: one workflow resolved for one task
# =============================================================================


class ResolvedWorkflowStep(BaseModel):
    """One planned step with its agent decided, or with nobody to run it."""

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
    """What is supposed to run on one task, before any of it has."""

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
    """One position in the chain, planned and recorded together."""

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
    """One task as a sequence of hops, assembled from its own step rows."""

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
        """More hops ran than the workflow now has steps."""
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
