"""The wire models for workflows, plans and chains.

Most of this file is about what the models **refuse**, because each refusal is a
run-time failure moved to a place where it costs nothing. A silent step in the
middle of a chain refused at write time is a validation error; the same mistake
caught at run time is a claimed task, a paid turn and an agent handed an empty
prior report that it answers anyway.

``AgentTaskChain.plan_changed`` is the one piece of behaviour rather than
validation, and it is here rather than on the server because it is the model's
own answer to "somebody rewrote the workflow while the dispatcher was walking
it".
"""

from __future__ import annotations

import datetime as dt

import pytest
from agent_control_models.tasks import MAX_STEPS_PER_TASK, MAX_TURNS_PER_STEP
from agent_control_models.workflows import (
    AgentTaskChain,
    AgentTaskChainHop,
    AgentTaskPlan,
    AgentWorkflow,
    AgentWorkflowStep,
    RequiredOutput,
    ResolvedWorkflowStep,
    UpsertAgentWorkflowRequest,
)
from pydantic import ValidationError

NOW = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.UTC)
TASK_KEY = "a" * 32
AGENT = "marketing_researcher"
OTHER_AGENT = "marketing_writer"


def _upsert(**overrides: object) -> UpsertAgentWorkflowRequest:
    payload: dict[str, object] = {
        "display_name": "Research then write",
        "steps": [{"agent_name": AGENT, "brief": "research it"}],
    }
    payload.update(overrides)
    return UpsertAgentWorkflowRequest.model_validate(payload)


def _hop(index: int, **overrides: object) -> AgentTaskChainHop:
    payload: dict[str, object] = {"step_index": index, "ran": True, "status": "completed"}
    payload.update(overrides)
    return AgentTaskChainHop.model_validate(payload)


def _chain(hops: list[AgentTaskChainHop], *, planned: int) -> AgentTaskChain:
    return AgentTaskChain(
        task_key=TASK_KEY,
        source_kind="linear",
        source_ref="ENG-1",
        title="Whatever somebody typed",
        workflow_key="marketing",
        workflow_display_name="Marketing chain",
        status="running",
        dry_run=True,
        hops=hops,
        hops_planned=planned,
        hops_ran=sum(1 for hop in hops if hop.ran),
    )


class TestAgentWorkflowStep:
    def test_defaults_are_one_turn_of_text_from_the_teams_agent(self) -> None:
        """The shape a step takes when its author wrote only a brief."""
        step = AgentWorkflowStep.model_validate({"brief": "do it"})

        assert step.agent_name is None
        assert step.max_turns == 1
        assert RequiredOutput(step.required_output) is RequiredOutput.TEXT
        assert step.idempotent is False

    def test_no_field_can_name_another_step(self) -> None:
        """Asserted as an absence, because it is the property section 9 rests
        on. Extras are forbidden, so a field invented to address a later step
        is refused rather than stored and ignored."""
        with pytest.raises(ValidationError):
            AgentWorkflowStep.model_validate({"brief": "x", "then": 1})

        assert "then" not in AgentWorkflowStep.model_fields
        assert not any(
            "step" in name
            for name in AgentWorkflowStep.model_fields
            if name not in {"agent_name", "brief", "max_turns", "required_output", "idempotent"}
        )

    @pytest.mark.parametrize("turns", [0, MAX_TURNS_PER_STEP + 1])
    def test_a_turn_ceiling_outside_the_range_is_refused(self, turns: int) -> None:
        with pytest.raises(ValidationError):
            AgentWorkflowStep.model_validate({"brief": "x", "max_turns": turns})

    def test_the_ceiling_itself_is_allowed(self) -> None:
        assert (
            AgentWorkflowStep.model_validate({"max_turns": MAX_TURNS_PER_STEP}).max_turns
            == MAX_TURNS_PER_STEP
        )

    def test_a_brief_past_the_cap_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            AgentWorkflowStep.model_validate({"brief": "b" * 2001})


class TestTheSilentStepRule:
    def test_a_silent_step_before_another_step_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="Only the last step"):
            _upsert(
                steps=[
                    {"agent_name": AGENT, "brief": "a", "required_output": "none"},
                    {"agent_name": OTHER_AGENT, "brief": "b"},
                ]
            )

    def test_a_silent_last_step_is_allowed(self) -> None:
        request = _upsert(
            steps=[
                {"agent_name": AGENT, "brief": "a"},
                {"agent_name": OTHER_AGENT, "brief": "b", "required_output": "none"},
            ]
        )

        assert RequiredOutput(request.steps[1].required_output) is RequiredOutput.NONE

    def test_a_single_silent_step_is_allowed(self) -> None:
        """One step is the last step. There is nobody downstream to mislead."""
        request = _upsert(steps=[{"agent_name": AGENT, "required_output": "none"}])

        assert len(request.steps) == 1

    def test_the_refusal_names_the_step_that_follows_the_silent_one(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _upsert(
                steps=[
                    {"agent_name": AGENT, "required_output": "none"},
                    {"agent_name": OTHER_AGENT},
                ]
            )

        assert "Step 0" in str(caught.value)
        assert "step 1 follows it" in str(caught.value)


class TestUpsertAgentWorkflowRequest:
    def test_a_workflow_must_have_at_least_one_step(self) -> None:
        with pytest.raises(ValidationError):
            _upsert(steps=[])

    def test_a_workflow_longer_than_the_cap_is_refused(self) -> None:
        """The cap is a ceiling on chain length, so a workflow cannot loop."""
        with pytest.raises(ValidationError):
            _upsert(steps=[{"agent_name": AGENT}] * (MAX_STEPS_PER_TASK + 1))

    def test_the_cap_itself_is_allowed(self) -> None:
        assert len(_upsert(steps=[{"agent_name": AGENT}] * MAX_STEPS_PER_TASK).steps) == (
            MAX_STEPS_PER_TASK
        )

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _upsert(workflow_key="smuggled")

    def test_a_workflow_may_belong_to_no_team(self) -> None:
        """It is then usable from any team, and can only run steps that name
        their own agent."""
        assert _upsert().team_slug is None


class TestAgentWorkflow:
    def test_it_round_trips_its_stored_shape(self) -> None:
        workflow = AgentWorkflow.model_validate(
            {
                "workflow_key": "triage-and-fix",
                "display_name": "Triage then fix",
                "team_slug": "marketing",
                "steps": [{"agent_name": AGENT, "brief": "a"}],
                "created_at": NOW,
                "updated_at": NOW,
            }
        )

        assert workflow.steps[0].agent_name == AGENT
        assert workflow.team_slug == "marketing"


class TestAgentTaskPlan:
    def test_an_unresolved_step_is_reported_rather_than_defaulted(self) -> None:
        """A plan that silently filled the gap would be agent selection
        happening somewhere nobody reviewed."""
        plan = AgentTaskPlan(
            task_key=TASK_KEY,
            workflow_key="default",
            display_name="One step, no workflow configured",
            implicit=True,
            steps=[
                ResolvedWorkflowStep(
                    step_index=0, agent_name=None, agent_source="unresolved", max_turns=1
                )
            ],
            unresolved_step_indexes=[0],
        )

        assert plan.steps[0].agent_name is None
        assert plan.unresolved_step_indexes == [0]

    def test_a_resolved_step_says_which_of_the_two_sources_answered(self) -> None:
        step = ResolvedWorkflowStep(
            step_index=1, agent_name=AGENT, agent_source="team_default", max_turns=1
        )

        assert step.agent_source == "team_default"

    def test_a_step_index_past_the_workflow_cap_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ResolvedWorkflowStep(
                step_index=MAX_STEPS_PER_TASK,
                agent_name=AGENT,
                agent_source="workflow_step",
                max_turns=1,
            )


class TestAgentTaskChain:
    def test_a_hop_that_never_ran_carries_no_status(self) -> None:
        """The difference between "the writer found nothing" and "the writer
        never ran"."""
        hop = AgentTaskChainHop(step_index=1, agent_name=OTHER_AGENT, ran=False)

        assert hop.status is None
        assert hop.output_text is None

    def test_a_deleted_session_leaves_the_hop_readable(self) -> None:
        """Deleting the session is the ordinary end state, and the step row is
        the durable record of what the agent produced."""
        hop = _hop(0, agent_name=AGENT, session_key=None, output_text="what I found")

        assert hop.session_key is None
        assert hop.output_text == "what I found"

    def test_plan_changed_is_false_when_the_rows_fit_the_workflow(self) -> None:
        chain = _chain([_hop(0), _hop(1)], planned=2)

        assert chain.plan_changed is False

    def test_plan_changed_is_true_when_more_hops_ran_than_the_workflow_has(
        self,
    ) -> None:
        """Somebody rewrote or deleted the workflow while the dispatcher was
        walking it. A flag rather than a refusal, because this is a read and a
        model that raised would turn "the workflow was edited" into a 500 on
        the one page somebody opens to find out what happened."""
        chain = _chain([_hop(0), _hop(1)], planned=1)

        assert chain.plan_changed is True

    def test_a_chain_still_ahead_of_the_dispatcher_is_not_a_changed_plan(self) -> None:
        chain = _chain([_hop(0), AgentTaskChainHop(step_index=1, ran=False)], planned=2)

        assert chain.plan_changed is False
        assert chain.hops_ran == 1
