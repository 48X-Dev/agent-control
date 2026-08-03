"""Workflows, the plan they resolve to for one task, and the chain that ran.

Three jobs, one namespace at a time.

**Store the configuration.** ``agent_workflows`` is an ordered list of steps,
written at ADMIN because it names agents and shapes prompts. Replace semantics:
the whole list is written or none of it is, because a partial update is a way to
change who runs step 2 without whoever reviews it seeing steps 1 and 3.

**Resolve it for a task.** :meth:`AgentWorkflowsService.plan_for_task` fills in
each step's agent from exactly two server-side sources, in order: the step's own
``agent_name``, then the team's ``default_agent_name``. There is no third
source, and in particular nothing on the task - not its title, not its body, not
its labels, not its source - reaches this decision. **Anyone who can file an
issue in a tracker can label it**, so a label that chose the agent would let an
attacker choose the executor and therefore the blast radius: agents differ in
system prompt, in bound controls and in tools. A step neither source can answer
comes back with ``agent_name`` null, and the dispatcher blocks the task rather
than picking one.

**Assemble the chain.** :meth:`AgentWorkflowsService.chain_for_task` merges the
plan with the rows in ``agent_task_steps``. Not with a trace: the rollup at
``GET /observability/traces/{id}`` builds hops exclusively from
control-execution events, so an agent with no bound control that fired
contributes zero hops and disappears from it. Merging plan with rows is also
what lets the view say a two-agent workflow stopped after its first agent, which
neither source can say alone - the second agent's row was never written, and the
plan alone knows nothing about what the first one produced.

Nothing in this module is a channel between agents. There is no field naming
another step, no message an agent can address, and no way for a step to learn a
later one exists.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, cast

from agent_control_models.errors import ErrorCode
from agent_control_models.tasks import (
    DEFAULT_WORKFLOW_KEY,
    TERMINAL_TASK_STATUSES,
    AgentTaskStatus,
    AgentTaskStepStatus,
)
from agent_control_models.workflows import (
    AgentTaskChain,
    AgentTaskChainHop,
    AgentTaskPlan,
    AgentWorkflow,
    AgentWorkflowStep,
    RequiredOutput,
    ResolvedWorkflowStep,
)
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import ConflictError, NotFoundError
from ..models import AgentTask, Team
from ..models import AgentTaskStep as AgentTaskStepRow
from ..models import AgentWorkflow as AgentWorkflowRow

IMPLICIT_WORKFLOW_DISPLAY_NAME = "One step, no workflow configured"
"""What the fallback is called wherever it is rendered.

A team with no workflow gets one step with no pinned agent and an empty brief.
That is deliberate rather than a gap: most of the value is one agent doing one
thing, and a design that demands a workflow before anything runs does not get
used. The name says what happened so nobody goes looking for the row."""

AGENT_SOURCE_STEP = "workflow_step"
AGENT_SOURCE_TEAM_DEFAULT = "team_default"
AGENT_SOURCE_UNRESOLVED = "unresolved"

_TERMINAL_VALUES = tuple(status.value for status in TERMINAL_TASK_STATUSES)


class AgentWorkflowsService:
    """One namespace's workflows, and what they mean for one task."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # -- configuration -----------------------------------------------------

    async def list_workflows(self, *, namespace_key: str) -> list[AgentWorkflow]:
        rows = (
            await self._db.execute(
                select(AgentWorkflowRow)
                .where(AgentWorkflowRow.namespace_key == namespace_key)
                .order_by(AgentWorkflowRow.workflow_key.asc())
            )
        ).scalars().all()
        return [self._to_model(row) for row in rows]

    async def get_workflow(self, *, namespace_key: str, workflow_key: str) -> AgentWorkflow:
        return self._to_model(
            await self._require_row(namespace_key=namespace_key, workflow_key=workflow_key)
        )

    async def upsert_workflow(
        self,
        *,
        namespace_key: str,
        workflow_key: str,
        display_name: str,
        team_slug: str | None,
        steps: Sequence[AgentWorkflowStep],
    ) -> tuple[AgentWorkflow, bool]:
        """Create or replace one workflow. Returns ``(workflow, created)``.

        The team is verified to exist when one is named. A workflow scoped to a
        team that is not there would resolve no default agent and refuse every
        step that relies on one, at claim time, on somebody else's shift.
        """
        if team_slug is not None:
            await self._require_team(namespace_key=namespace_key, team_slug=team_slug)

        payload = [step.model_dump(mode="json") for step in steps]
        existing = await self._find_row(
            namespace_key=namespace_key, workflow_key=workflow_key
        )
        if existing is not None:
            existing.display_name = display_name
            existing.team_slug = team_slug
            existing.steps = payload
            await self._db.flush()
            await self._db.refresh(existing)
            return self._to_model(existing), False

        row = AgentWorkflowRow(
            namespace_key=namespace_key,
            workflow_key=workflow_key,
            display_name=display_name,
            team_slug=team_slug,
            steps=payload,
        )
        # Savepoint for the same reason ``upsert_team`` uses one: the loser of a
        # unique-key race rolls back only its own insert and applies its values
        # as an update, leaving unrelated pending writes intact.
        try:
            async with self._db.begin_nested():
                self._db.add(row)
                await self._db.flush()
        except IntegrityError:
            existing = await self._find_row(
                namespace_key=namespace_key, workflow_key=workflow_key
            )
            if existing is None:
                raise
            existing.display_name = display_name
            existing.team_slug = team_slug
            existing.steps = payload
            await self._db.flush()
            await self._db.refresh(existing)
            return self._to_model(existing), False
        await self._db.refresh(row)
        return self._to_model(row), True

    async def delete_workflow(
        self, *, namespace_key: str, workflow_key: str
    ) -> int:
        """Delete a workflow and report how many open tasks named it.

        The count is returned rather than used to refuse. A workflow somebody
        wants gone is usually one that is going wrong, and refusing to delete it
        while its tasks are still running is refusing at exactly the moment the
        operator needs it. What the tasks then do is legible: they keep their
        ``workflow_key``, stop resolving, and show up as ``blocked`` rather than
        quietly running some other workflow's steps.
        """
        row = await self._require_row(
            namespace_key=namespace_key, workflow_key=workflow_key
        )
        open_tasks = await self._db.scalar(
            select(func.count())
            .select_from(AgentTask)
            .where(
                AgentTask.namespace_key == namespace_key,
                AgentTask.workflow_key == workflow_key,
                AgentTask.status.notin_(_TERMINAL_VALUES),
            )
        )
        await self._db.delete(row)
        await self._db.flush()
        return int(open_tasks or 0)

    # -- the plan ----------------------------------------------------------

    async def plan_for_task(self, *, namespace_key: str, task: AgentTask) -> AgentTaskPlan:
        """Resolve the task's workflow into the steps a dispatcher would run.

        Two sources for the agent and no third. When neither answers, the step
        comes back unresolved and is named in ``unresolved_step_indexes``: it is
        reported rather than filled in, because a plan that silently picked an
        agent would be agent selection happening somewhere nobody reviewed.

        A ``workflow_key`` naming a row that no longer exists resolves to the
        implicit one-step plan rather than raising. The task is a row that
        already exists and an operator reading it needs to see what is wrong
        with it; a 404 on the task's own plan would take the console page away
        at exactly the moment somebody needed to look at it.
        """
        row = (
            None
            if task.workflow_key == DEFAULT_WORKFLOW_KEY
            else await self._find_row(
                namespace_key=namespace_key, workflow_key=task.workflow_key
            )
        )
        steps = (
            [AgentWorkflowStep()]
            if row is None
            else _decode_steps(row.steps, workflow_key=task.workflow_key)
        )
        # The task's own team wins over the workflow's. A workflow shared
        # between teams still resolves each team's own default, and a task
        # carries the team it was imported under, which is the one an operator
        # pressed the button for.
        team_slug = task.team_slug or (row.team_slug if row is not None else None)
        default_agent = await self._team_default_agent(
            namespace_key=namespace_key, team_slug=team_slug
        )

        resolved: list[ResolvedWorkflowStep] = []
        unresolved: list[int] = []
        for index, step in enumerate(steps):
            agent_name = step.agent_name or default_agent
            if step.agent_name:
                source = AGENT_SOURCE_STEP
            elif default_agent:
                source = AGENT_SOURCE_TEAM_DEFAULT
            else:
                source = AGENT_SOURCE_UNRESOLVED
                unresolved.append(index)
            resolved.append(
                ResolvedWorkflowStep(
                    step_index=index,
                    agent_name=agent_name,
                    agent_source=source,
                    brief=step.brief,
                    max_turns=step.max_turns,
                    required_output=RequiredOutput(step.required_output),
                    idempotent=step.idempotent,
                )
            )

        return AgentTaskPlan(
            task_key=task.task_key,
            workflow_key=task.workflow_key,
            display_name=(
                IMPLICIT_WORKFLOW_DISPLAY_NAME if row is None else row.display_name
            ),
            implicit=row is None,
            team_slug=team_slug,
            steps=resolved,
            unresolved_step_indexes=unresolved,
        )

    async def require_resolvable_at_import(
        self, *, namespace_key: str, workflow_key: str, team_slug: str | None
    ) -> None:
        """Refuse an import whose workflow cannot name an agent for every step.

        Called before any row is created, which is the point: four blocked
        tasks and four identical comments on somebody's issues is the failure
        this avoids, and it is much cheaper to avoid at the confirm screen.

        The implicit one-step workflow is exempt and deliberately so. It pins no
        agent by construction, and the operator running the file source names
        the agent on the command line - which is the path slice 1 shipped and
        which nothing here should break. An *explicit* workflow is server-side
        configuration, so it has to be complete before work is queued against
        it.
        """
        if workflow_key == DEFAULT_WORKFLOW_KEY:
            return
        row = await self._find_row(
            namespace_key=namespace_key, workflow_key=workflow_key
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_WORKFLOW_NOT_FOUND,
                detail=f"No workflow '{workflow_key}' in this namespace.",
                resource="AgentWorkflow",
                resource_id=workflow_key,
                hint="Create it with PUT /agent-workflows/{workflow_key}, or omit the key.",
            )
        steps = _decode_steps(row.steps, workflow_key=workflow_key)
        default_agent = await self._team_default_agent(
            namespace_key=namespace_key, team_slug=team_slug or row.team_slug
        )
        missing = [
            index
            for index, step in enumerate(steps)
            if not step.agent_name and not default_agent
        ]
        if missing:
            raise ConflictError(
                error_code=ErrorCode.NO_AGENT_SELECTED,
                detail=(
                    f"Workflow '{workflow_key}' has no agent for "
                    f"step{'s' if len(missing) > 1 else ''} "
                    f"{', '.join(str(index) for index in missing)}, and "
                    + (
                        f"team '{team_slug or row.team_slug}' has no default_agent_name."
                        if (team_slug or row.team_slug)
                        else "no team was named to take a default from."
                    )
                ),
                resource="AgentWorkflow",
                resource_id=workflow_key,
                hint=(
                    "Pin an agent on the step, or set default_agent_name on the team. "
                    "Nothing on the task can choose one."
                ),
                extra_details={"unresolved_step_indexes": missing},
            )

    # -- the chain ---------------------------------------------------------

    async def chain_for_task(
        self, *, namespace_key: str, task: AgentTask
    ) -> AgentTaskChain:
        """One task as hops, from ``agent_task_steps`` merged with its plan.

        Every planned position produces a hop. A position with a step row
        carries what that agent did; a position without one carries ``ran``
        false, which is the difference between "the writer found nothing" and
        "the writer never ran". Neither source can say that alone.

        A step row past the end of the plan is still rendered, and
        ``hops_planned`` still reports what the workflow says *now*. The pair
        disagreeing is how a console learns the workflow was rewritten while the
        dispatcher was walking it (``AgentTaskChain.plan_changed``). Trimming
        the extra rows to fit the current configuration would show an operator a
        shorter chain than the one their agents actually ran, and padding the
        count to hide the gap would be the same lie with better arithmetic.
        """
        plan = await self.plan_for_task(namespace_key=namespace_key, task=task)
        rows = (
            await self._db.execute(
                select(AgentTaskStepRow)
                .where(AgentTaskStepRow.task_id == task.id)
                .order_by(AgentTaskStepRow.step_index.asc())
            )
        ).scalars().all()
        by_index = {row.step_index: row for row in rows}

        positions = sorted({step.step_index for step in plan.steps} | set(by_index))
        planned_by_index = {step.step_index: step for step in plan.steps}
        hops: list[AgentTaskChainHop] = []
        for index in positions:
            planned = planned_by_index.get(index)
            row = by_index.get(index)
            if row is None:
                hops.append(
                    AgentTaskChainHop(
                        step_index=index,
                        agent_name=planned.agent_name if planned is not None else None,
                        brief=planned.brief if planned is not None else "",
                        ran=False,
                    )
                )
                continue
            hops.append(
                AgentTaskChainHop(
                    step_index=index,
                    # The row wins over the plan. It records the agent that
                    # actually ran, and a workflow edited mid-task must not
                    # rewrite the history of what did.
                    agent_name=row.agent_name,
                    brief=row.brief,
                    ran=True,
                    status=AgentTaskStepStatus(row.status),
                    session_key=row.session_key,
                    turn_trace_id=row.turn_trace_id,
                    output_text=row.output_text,
                    output_truncated=row.output_truncated,
                    attempts=row.attempts,
                    failure_code=row.failure_code,
                    failure_detail=row.failure_detail,
                    started_at=row.started_at,
                    ended_at=row.ended_at,
                )
            )

        return AgentTaskChain(
            task_key=task.task_key,
            source_kind=task.source_kind,
            source_ref=task.source_ref,
            source_url=task.source_url,
            title=task.title,
            team_slug=task.team_slug,
            workflow_key=task.workflow_key,
            workflow_display_name=plan.display_name,
            status=AgentTaskStatus(task.status),
            dry_run=task.dry_run,
            chain_trace_id=task.chain_trace_id,
            hops=hops,
            hops_planned=len(plan.steps),
            hops_ran=len(by_index),
            failure_code=task.failure_code,
            failure_detail=task.failure_detail,
        )

    # -- internals ---------------------------------------------------------

    async def _team_default_agent(
        self, *, namespace_key: str, team_slug: str | None
    ) -> str | None:
        if team_slug is None:
            return None
        return await self._db.scalar(
            select(Team.default_agent_name).where(
                Team.namespace_key == namespace_key, Team.slug == team_slug
            )
        )

    async def _require_team(self, *, namespace_key: str, team_slug: str) -> None:
        found = await self._db.scalar(
            select(Team.id).where(
                Team.namespace_key == namespace_key, Team.slug == team_slug
            )
        )
        if found is None:
            raise NotFoundError(
                error_code=ErrorCode.TEAM_NOT_FOUND,
                detail=f"No team '{team_slug}' in this namespace.",
                resource="Team",
                resource_id=team_slug,
                hint="A workflow scoped to a missing team resolves no default agent.",
            )

    async def _find_row(
        self, *, namespace_key: str, workflow_key: str
    ) -> AgentWorkflowRow | None:
        row = await self._db.scalar(
            select(AgentWorkflowRow).where(
                AgentWorkflowRow.namespace_key == namespace_key,
                AgentWorkflowRow.workflow_key == workflow_key,
            )
        )
        return cast(AgentWorkflowRow | None, row)

    async def _require_row(
        self, *, namespace_key: str, workflow_key: str
    ) -> AgentWorkflowRow:
        row = await self._find_row(
            namespace_key=namespace_key, workflow_key=workflow_key
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_WORKFLOW_NOT_FOUND,
                detail=f"No workflow '{workflow_key}' in this namespace.",
                resource="AgentWorkflow",
                resource_id=workflow_key,
            )
        return row

    def _to_model(self, row: AgentWorkflowRow) -> AgentWorkflow:
        return AgentWorkflow(
            workflow_key=row.workflow_key,
            display_name=row.display_name,
            team_slug=row.team_slug,
            steps=_decode_steps(row.steps, workflow_key=row.workflow_key),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )


def _decode_steps(
    raw: list[dict[str, Any]] | None, *, workflow_key: str
) -> list[AgentWorkflowStep]:
    """Validate stored JSONB back into steps, and refuse a row that will not.

    Validated on the way out as well as on the way in. The column is JSONB, so
    a hand-edited row, a partially applied migration or a future field rename
    can put something in it that no longer parses - and the failure mode of
    tolerating that is a step with a silently missing ``max_turns`` running
    against whatever the default happens to be that week.
    """
    try:
        return [AgentWorkflowStep.model_validate(entry) for entry in (raw or [])]
    except ValidationError as exc:
        raise ConflictError(
            error_code=ErrorCode.CORRUPTED_DATA,
            detail=(
                f"Workflow '{workflow_key}' has steps that no longer validate: {exc}. "
                "Nothing will run against it until it is written again."
            ),
            resource="AgentWorkflow",
            resource_id=workflow_key,
            hint="Rewrite the workflow with PUT /agent-workflows/{workflow_key}.",
        ) from exc


def _aware(moment: dt.datetime) -> dt.datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)
