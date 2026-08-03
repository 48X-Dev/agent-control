"""Reads and writes on the dispatch ledger, within one namespace.

The server never polls this table, never starts a turn on its own initiative,
never retries anything and has no background thread. It answers requests about
rows, exactly as it does for ``agent_sessions``. If a future reader finds an
``asyncio.create_task`` or a scheduler import traceable to this module, the
design has been violated.

What it does own, and what a dispatcher therefore cannot talk itself out of:

**Deduplication.** Import inserts with ``ON CONFLICT DO NOTHING`` against a
partial unique index over non-terminal statuses, and reports how many rows it
actually created. That is what answers "how does it avoid claiming the same
issue twice" for two dispatchers, two replicas and a double-clicked button at
once, in the database rather than in a handler.

**The write order for a finished step.** The step row is updated first and the
task's counters second, in one transaction. A crash between them leaves a
completed step and a stale ``current_step``, which the resume rule tolerates
exactly. A crash in the other order loses the agent's output permanently. That
ordering is a rule rather than a convention, so it lives here rather than in
the process that would have to remember it.

**The deadline.** A step may not start after ``deadline_at``, which is set on
the first claim and never extended. A dispatcher that hangs cannot outlive it,
because the check is on this side of the wire.

One thing this module is deliberately not: a place where an agent is chosen. A
task carries a title and a body written by whoever has tracker access, and no
field on it reaches a decision about which agent runs, which tools it has, or
what it may spend.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Sequence
from uuid import uuid4

from agent_control_models.errors import ErrorCode
from agent_control_models.tasks import (
    DEFAULT_WORKFLOW_KEY,
    TERMINAL_TASK_STATUSES,
    AgentTaskDetail,
    AgentTaskStatus,
    AgentTaskStep,
    AgentTaskStepStatus,
    AgentTaskSummary,
    ClaimAgentTaskResponse,
    HeartbeatAgentTaskResponse,
    ImportAgentTasksRequest,
    ImportAgentTasksResponse,
    ImportCandidate,
    ImportMode,
    ImportSkipCounts,
    ImportTaskItem,
    TaskScopeKind,
    TaskSourceKind,
)
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import DispatchSettings
from ..errors import ConflictError, NotFoundError
from ..models import AgentTask
from ..models import AgentTaskStep as AgentTaskStepRow
from .agent_dispatch_state import (
    charge_imported_tasks,
    read_snapshot,
    require_dispatch_not_paused,
)
from .turn_locks import new_trace_id

_TERMINAL_VALUES = tuple(status.value for status in TERMINAL_TASK_STATUSES)


def refs_digest(refs: Sequence[str]) -> str:
    """A sha256 over the sorted refs, prefixed so the algorithm is on the wire.

    Over the *set*, not a count, and that is the whole point: four items
    replaced by four different items has the same count and a different digest.
    A confirm bound to a count is not an authorization, because the operator
    who expected four and saw four cannot tell that one of them is new.
    """
    joined = "\n".join(sorted(refs)).encode("utf-8")
    return f"sha256:{hashlib.sha256(joined).hexdigest()}"


class AgentTasksService:
    """One namespace's dispatch ledger."""

    def __init__(self, db: AsyncSession, *, settings: DispatchSettings) -> None:
        self._db = db
        self._settings = settings

    # -- reads ------------------------------------------------------------

    async def list_tasks(
        self,
        *,
        namespace_key: str,
        status: AgentTaskStatus | None,
        team_slug: str | None,
        limit: int,
        after_id: int | None,
    ) -> tuple[list[AgentTaskSummary], int, int | None]:
        """A page of tasks, oldest first, plus the total and the next cursor.

        Oldest first because this is the claim poll as well as a console list,
        and a queue that served newest first would starve whatever arrived
        while the fleet was busy.
        """
        stmt = self._scoped(namespace_key=namespace_key, status=status, team_slug=team_slug)
        total = await self._db.scalar(
            select(func.count()).select_from(stmt.subquery())
        )
        page = stmt.order_by(AgentTask.id.asc())
        if after_id is not None:
            page = page.where(AgentTask.id > after_id)
        rows = (await self._db.execute(page.limit(limit + 1))).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = str(rows[-1].id) if has_more and rows else None
        return [self._summary(row) for row in rows], int(total or 0), (
            int(next_cursor) if next_cursor is not None else None
        )

    async def get_detail(self, *, namespace_key: str, task_key: str) -> AgentTaskDetail:
        row = await self._require_row(namespace_key=namespace_key, task_key=task_key)
        return await self._detail(row)

    async def get_row(self, *, namespace_key: str, task_key: str) -> AgentTask:
        """The ORM row, for the two readers that need the row rather than the model.

        The plan and the chain both resolve a workflow against this task's own
        ``workflow_key`` and ``team_slug``, which is a join the wire model does
        not carry and should not: re-deriving it from a serialized detail would
        be a second copy of the resolution rule, and the second copy is the one
        that goes stale.
        """
        return await self._require_row(namespace_key=namespace_key, task_key=task_key)

    async def resolve_task_id(self, *, namespace_key: str, task_key: str) -> int:
        """Turn a task key into the row id a session binds to.

        Refuses a terminal task. Binding a new session to a task that is
        already finished would create a fleet turn under a budget nobody is
        watching any more, and it is always a caller bug.
        """
        row = await self._require_row(namespace_key=namespace_key, task_key=task_key)
        if AgentTaskStatus(row.status) in TERMINAL_TASK_STATUSES:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Task {task_key} is {row.status} and cannot take another session.",
                resource="AgentTask",
                resource_id=task_key,
                hint="Sessions belong to a task that is still running.",
            )
        return row.id

    # -- import -----------------------------------------------------------

    async def import_tasks(
        self,
        *,
        namespace_key: str,
        request: ImportAgentTasksRequest,
        created_by_hash: str | None,
    ) -> ImportAgentTasksResponse:
        """Preview or commit one import. A preview writes nothing at all.

        The eligible set is what the operator is asked to agree to, so it is
        returned in full rather than counted. Refs that already have an open
        task are moved to ``skipped.already_queued`` before the digest is
        taken, which means pressing twice previews an empty set instead of
        re-queueing work.

        ``created`` can be lower than the eligible count even after the digest
        matched: the insert is ``ON CONFLICT DO NOTHING`` against the partial
        unique index, so a ref another caller committed in the same instant is
        simply not created, and the response says how many rows exist rather
        than how many were attempted.
        """
        # Every enum arriving from a request model is re-widened here. The
        # shared base model sets ``use_enum_values=True``, so a field annotated
        # ``ImportMode`` holds the plain string after validation while
        # server-side callers pass members. ``is`` against a member is silently
        # False for the string form, which is a comparison that reads correctly
        # and does the opposite - so nothing below compares an unconverted
        # field.
        mode = ImportMode(request.mode)
        scope = request.scope
        # The request model caps the list at the constant this deployment's
        # ceiling defaults to. This is the deployment's own number, which can
        # only be lower, and it is checked here rather than left as a setting
        # nothing reads - a ceiling that no code path enforces is not a
        # ceiling, however carefully it is documented.
        if len(scope.items) > self._settings.max_import_items:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=(
                    f"This import carries {len(scope.items)} items and the deployment's "
                    f"ceiling is {self._settings.max_import_items}."
                ),
                resource="AgentTask",
                hint="One import is one page. Confirm a smaller set.",
            )
        source_kind = TaskSourceKind(scope.source_kind)
        open_refs = await self._open_source_refs(
            namespace_key=namespace_key,
            source_kind=source_kind,
            refs=[item.source_ref for item in scope.items],
        )
        worked_refs = await self._terminal_source_refs(
            namespace_key=namespace_key,
            source_kind=source_kind,
            refs=[item.source_ref for item in scope.items],
        )

        # A ref whose only task is terminal is eligible again *if the caller
        # asks*: reopened issues are real and the partial unique index permits
        # it, but a loop re-reading the same source would otherwise pay for the
        # same work on every pass. Counted either way, so the operator can see
        # what the history is holding back.
        worked_only = {ref for ref in worked_refs if ref not in open_refs}
        blocked = open_refs if request.requeue_completed else open_refs | worked_only
        eligible = [item for item in scope.items if item.source_ref not in blocked]
        skipped = ImportSkipCounts(
            already_queued=len(open_refs),
            already_worked=len(worked_only),
        )
        digest = refs_digest([item.source_ref for item in eligible])
        workflow_key = request.workflow_key or DEFAULT_WORKFLOW_KEY

        if mode is ImportMode.PREVIEW:
            return await self._import_response(
                namespace_key=namespace_key,
                mode=ImportMode.PREVIEW,
                eligible=eligible,
                digest=digest,
                skipped=skipped,
                workflow_key=workflow_key,
                dry_run=request.dry_run,
                created=0,
                task_keys=[],
            )

        # A commit is new work, so both stop switches refuse it. The preview
        # above deliberately does not: it writes nothing, and an operator whose
        # namespace is paused still needs to see what would run and read the
        # banner saying why it will not.
        await require_dispatch_not_paused(
            self._db, namespace_key=namespace_key, action="Importing tasks"
        )

        if request.expected_refs_digest is None:
            raise ConflictError(
                error_code=ErrorCode.SCOPE_CHANGED,
                detail="A commit must carry the digest of the set that was previewed.",
                resource="AgentTask",
                hint="Call this route with mode=preview and send back its refs_digest.",
                extra_details={"refs_digest": digest},
            )
        if request.expected_refs_digest != digest:
            raise ConflictError(
                error_code=ErrorCode.SCOPE_CHANGED,
                detail=(
                    "The set of items changed between the preview and this commit. "
                    "Nothing was created."
                ),
                resource="AgentTask",
                hint="Read the preview again and confirm the items it now lists.",
                extra_details={"refs_digest": digest},
            )

        # Every step of the named workflow has to resolve an agent before a
        # single row is created. Four blocked tasks and four identical comments
        # on somebody's issues is the failure this avoids, and it costs one read
        # here against a whole run of them there. The implicit one-step workflow
        # is exempt: it pins no agent by construction, and the operator running
        # it names the agent on the command line.
        #
        # Checked after the digest, like the ceiling below, so an operator whose
        # set has moved is told *that* rather than being told about a
        # configuration problem for a set they are no longer committing.
        from .agent_workflows import AgentWorkflowsService

        await AgentWorkflowsService(self._db).require_resolvable_at_import(
            namespace_key=namespace_key,
            workflow_key=workflow_key,
            team_slug=request.team_slug,
        )

        # The hourly task ceiling, in the transaction that inserts. Checked
        # after the digest so an operator whose set has moved is told that
        # rather than being told about a budget for a set they are no longer
        # committing.
        await charge_imported_tasks(
            self._db,
            namespace_key=namespace_key,
            count=len(eligible),
            settings=self._settings,
        )

        task_keys = await self._insert(
            namespace_key=namespace_key,
            items=eligible,
            source_kind=source_kind,
            team_slug=request.team_slug,
            workflow_key=workflow_key,
            dry_run=request.dry_run,
            created_by_hash=created_by_hash,
        )
        return await self._import_response(
            namespace_key=namespace_key,
            mode=ImportMode.COMMIT,
            eligible=eligible,
            digest=digest,
            skipped=skipped,
            workflow_key=workflow_key,
            dry_run=request.dry_run,
            created=len(task_keys),
            task_keys=task_keys,
        )

    # -- claim and lease --------------------------------------------------

    async def claim(
        self,
        *,
        namespace_key: str,
        task_key: str,
        instance_id: str,
        caller_hash: str | None,
    ) -> ClaimAgentTaskResponse:
        """Take the task, or refuse with a conflict.

        The statement lives in :mod:`.task_claims`; this wraps its answer in
        the wire shape and turns ``None`` into the one refusal a dispatcher can
        act on. It deliberately does not explain *which* of the several
        refusals it was: a second read to find out would be stale the moment it
        landed, and every one of them means the same thing to the caller.

        A paused or halted namespace refuses before the statement runs, and that
        refusal *is* specific: it stops a row being taken out of the queue by a
        process that could not then run a turn against it. Like the dispatcher's
        own pre-check it is an optimisation. The enforcement point is the turn
        path, which is the only thing a dispatcher cannot route around.
        """
        from .task_claims import claim_task

        await require_dispatch_not_paused(
            self._db, namespace_key=namespace_key, action="Claiming a task"
        )

        outcome = await claim_task(
            self._db,
            namespace_key=namespace_key,
            task_key=task_key,
            instance_id=instance_id,
            caller_hash=caller_hash,
            chain_trace_id=new_trace_id(),
            lease_seconds=self._settings.task_lease_seconds,
            deadline_seconds=self._settings.task_deadline_seconds,
        )
        if outcome is None:
            await self._require_row(namespace_key=namespace_key, task_key=task_key)
            raise ConflictError(
                error_code=ErrorCode.TASK_ALREADY_CLAIMED,
                detail=(
                    f"Task {task_key} is not claimable: another dispatcher holds a live "
                    "lease on it, or it is in a status only a human can move."
                ),
                resource="AgentTask",
                resource_id=task_key,
                hint="Move on to the next queued task. Do not retry this one.",
            )

        row = await self._require_row(namespace_key=namespace_key, task_key=task_key)
        return ClaimAgentTaskResponse(
            task=await self._detail(row),
            prior_status=outcome.prior_status,
            resume_step_index=outcome.resume_step_index,
            reclaimed=outcome.reclaimed,
            abandoned_step_indexes=list(outcome.abandoned_step_indexes),
            lease_expires_at=outcome.heartbeat_at
            + dt.timedelta(seconds=self._settings.task_lease_seconds),
            lease_seconds=self._settings.task_lease_seconds,
        )

    async def heartbeat(
        self, *, namespace_key: str, task_key: str, instance_id: str
    ) -> HeartbeatAgentTaskResponse:
        from .task_claims import heartbeat_task

        refreshed = await heartbeat_task(
            self._db,
            namespace_key=namespace_key,
            task_key=task_key,
            instance_id=instance_id,
        )
        if refreshed is None:
            await self._require_row(namespace_key=namespace_key, task_key=task_key)
            raise self._not_the_holder(task_key)
        heartbeat_at, deadline_at, status = refreshed
        return HeartbeatAgentTaskResponse(
            task_key=task_key,
            status=status,
            heartbeat_at=heartbeat_at,
            lease_expires_at=heartbeat_at
            + dt.timedelta(seconds=self._settings.task_lease_seconds),
            deadline_at=deadline_at,
        )

    # -- steps ------------------------------------------------------------

    async def start_step(
        self,
        *,
        namespace_key: str,
        task_key: str,
        instance_id: str,
        step_index: int,
        agent_name: str,
        brief: str,
        session_key: str | None,
    ) -> tuple[AgentTaskStep, AgentTaskSummary]:
        """Open the step row before the turn, and refuse the ones that must not run.

        Four refusals, in order. Only the claim holder may write. Only a task
        that is actually ``running`` starts a step: holding the claim is not the
        same as being runnable, and the statuses that keep ``claimed_by``
        without being terminal are exactly the ones a step must not start
        under - ``blocked`` is a configuration error that a retry reproduces
        forever, ``paused_quota`` is waiting on a ceiling, and
        ``running_unknown`` is a turn nothing can prove has stopped. A
        timed-out task must never silently advance, and that is a refusal here
        rather than a discipline in the process being refused. A task past its
        deadline starts nothing, which is the ceiling that stops a hung
        dispatcher. And a step index at or beyond the per-task ceiling is
        refused, because a workflow that could keep numbering steps is a
        workflow that can loop.

        A row already at this index is reused rather than duplicated: the
        unique index is on ``(task_id, step_index)`` and a reclaimed task
        resumes at the index it abandoned. ``attempts`` is what keeps that
        visible, and a completed step is never reopened.
        """
        row = await self._require_claimed(
            namespace_key=namespace_key, task_key=task_key, instance_id=instance_id
        )
        if AgentTaskStatus(row.status) is not AgentTaskStatus.RUNNING:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=(
                    f"Task {task_key} is {row.status}, so no step may start on it. Only a "
                    "running task takes a step."
                ),
                resource="AgentTask",
                resource_id=task_key,
                hint="Claim it again. running_unknown is cleared by a human, not by a claim.",
            )
        self._require_within_deadline(row)
        if step_index >= self._settings.max_steps_per_task:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=(
                    f"Step {step_index} is at or beyond this deployment's ceiling of "
                    f"{self._settings.max_steps_per_task} steps per task."
                ),
                resource="AgentTask",
                resource_id=task_key,
                hint="A workflow is capped so it cannot loop.",
            )

        existing = await self._db.scalar(
            select(AgentTaskStepRow).where(
                AgentTaskStepRow.task_id == row.id,
                AgentTaskStepRow.step_index == step_index,
            )
        )
        if existing is None:
            step = AgentTaskStepRow(
                namespace_key=namespace_key,
                task_id=row.id,
                step_index=step_index,
                agent_name=agent_name,
                brief=brief,
                session_key=session_key,
                status=AgentTaskStepStatus.RUNNING.value,
                attempts=1,
            )
            self._db.add(step)
            await self._db.flush()
            # ``started_at`` is a server default, so it is unloaded until it is
            # read back. Every accessor below is synchronous, and a lazy load
            # from one of those is a MissingGreenlet rather than a query.
            await self._db.refresh(step)
        else:
            if existing.status == AgentTaskStepStatus.COMPLETED.value:
                raise ConflictError(
                    error_code=ErrorCode.TASK_STATUS_CONFLICT,
                    detail=f"Step {step_index} of task {task_key} already completed.",
                    resource="AgentTaskStep",
                    resource_id=str(step_index),
                    hint="Resume at the index the claim response reported.",
                )
            existing.status = AgentTaskStepStatus.RUNNING.value
            existing.agent_name = agent_name
            existing.brief = brief
            existing.session_key = session_key
            existing.turn_trace_id = None
            existing.output_text = None
            existing.output_truncated = False
            existing.ended_at = None
            existing.attempts = existing.attempts + 1
            existing.started_at = dt.datetime.now(dt.UTC)
            step = existing
            await self._db.flush()

        return self._step(step), self._summary(row)

    async def finish_step(
        self,
        *,
        namespace_key: str,
        task_key: str,
        instance_id: str,
        step_index: int,
        status: AgentTaskStepStatus,
        output_text: str | None,
        output_truncated: bool,
        session_key: str | None,
        turn_trace_id: str | None,
        failure_code: str | None,
        failure_detail: str | None,
    ) -> tuple[AgentTaskStep, AgentTaskSummary]:
        """Close a step out, and move the task's counters in the same transaction.

        Step row first, task row second. That order is the reason this is a
        server route rather than two dispatcher writes: a crash between them
        leaves a completed step and a stale ``current_step``, which the resume
        rule reads past; a crash the other way round loses the only durable
        copy of what the agent produced.
        """
        row = await self._require_claimed(
            namespace_key=namespace_key, task_key=task_key, instance_id=instance_id
        )
        step = await self._db.scalar(
            select(AgentTaskStepRow).where(
                AgentTaskStepRow.task_id == row.id,
                AgentTaskStepRow.step_index == step_index,
            )
        )
        if step is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_TASK_STEP_NOT_FOUND,
                detail=f"Task {task_key} has no step {step_index}.",
                resource="AgentTaskStep",
                resource_id=str(step_index),
                hint="Start the step before finishing it, so a crash leaves a record.",
            )
        if step.status != AgentTaskStepStatus.RUNNING.value:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Step {step_index} of task {task_key} is already {step.status}.",
                resource="AgentTaskStep",
                resource_id=str(step_index),
            )

        step_status = AgentTaskStepStatus(status)
        step.status = step_status.value
        step.output_text = output_text
        step.output_truncated = output_truncated
        step.failure_code = failure_code
        step.failure_detail = failure_detail
        step.ended_at = dt.datetime.now(dt.UTC)
        if session_key is not None:
            step.session_key = session_key
        if turn_trace_id is not None:
            step.turn_trace_id = turn_trace_id
        await self._db.flush()

        # Second, and only now. A turn that reached the executor is charged
        # whether or not it produced anything, so ``turns_used`` moves on a
        # failed step too; ``current_step`` moves only on a completed one,
        # because that is what the resume rule counts.
        row.turns_used = row.turns_used + 1
        if step_status is AgentTaskStepStatus.COMPLETED:
            row.current_step = step_index + 1
        await self._db.flush()
        # ``updated_at`` carries an ``onupdate``, which expires the attribute
        # rather than computing it here.
        await self._db.refresh(row)
        return self._step(step), self._summary(row)

    # -- task transitions --------------------------------------------------

    async def finish_task(
        self,
        *,
        namespace_key: str,
        task_key: str,
        instance_id: str,
        status: AgentTaskStatus,
        failure_code: str | None,
        failure_detail: str | None,
    ) -> AgentTaskDetail:
        """Record how the holder left the task.

        ``paused_quota`` and ``running_unknown`` are endings from the
        dispatcher's point of view and not from the ledger's: the first keeps
        its slot and is reclaimable, the second keeps its slot and is not. Both
        keep ``claimed_by``, because the row is still somebody's
        responsibility until a lease expires or a human intervenes.

        And a task already in ``running_unknown`` does not move from here at
        all. Keeping ``claimed_by`` on it is what lets a human find the holder;
        it is not permission for that holder to come back and mark its own
        timed-out task failed or completed. Only ``resolve`` moves it, because
        only a person can read the transcript and say whether the work
        happened.
        """
        task_status = AgentTaskStatus(status)
        row = await self._require_claimed(
            namespace_key=namespace_key, task_key=task_key, instance_id=instance_id
        )
        if AgentTaskStatus(row.status) is AgentTaskStatus.RUNNING_UNKNOWN:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=(
                    f"Task {task_key} is running_unknown: its turn timed out and nothing "
                    "here can prove the invocation stopped. A dispatcher does not move it."
                ),
                resource="AgentTask",
                resource_id=task_key,
                hint="A human clears this one, with resolve, after reading the transcript.",
            )
        row.status = task_status.value
        row.failure_code = failure_code
        row.failure_detail = failure_detail
        if task_status in TERMINAL_TASK_STATUSES:
            # The claim is released with the task. The source ref is free again
            # by the partial index, and a heartbeat from a straggler is refused
            # rather than silently extending a lease on finished work.
            row.claimed_by = None
            row.heartbeat_at = None
        await self._db.flush()
        await self._db.refresh(row)
        return await self._detail(row)

    async def cancel(
        self, *, namespace_key: str, task_key: str, reason: str | None
    ) -> AgentTaskDetail:
        """Take a queued task off the list, before anything ran.

        Only from ``queued``. Cancelling a running task would tell the operator
        that work had stopped when the turn is still going: stopping a turn is
        a halt, and it is a different button with a different mechanism.
        """
        row = await self._require_row(namespace_key=namespace_key, task_key=task_key)
        if AgentTaskStatus(row.status) is not AgentTaskStatus.QUEUED:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Task {task_key} is {row.status}; only a queued task can be cancelled.",
                resource="AgentTask",
                resource_id=task_key,
                hint="A running turn is stopped with a halt, not by cancelling its task.",
            )
        row.status = AgentTaskStatus.CANCELLED.value
        row.failure_code = "CANCELLED_BY_OPERATOR"
        row.failure_detail = reason
        await self._db.flush()
        await self._db.refresh(row)
        return await self._detail(row)

    async def resolve(
        self, *, namespace_key: str, task_key: str, requeue: bool, reason: str | None
    ) -> AgentTaskDetail:
        """A human clearing ``running_unknown``, which nothing else may do.

        The turn timed out and nothing here can prove the invocation stopped.
        Automatically resuming would put a second invocation on an executor
        that may still be running the first, so the row holds its slot until
        somebody has looked at the transcript and decided. That friction is the
        control.
        """
        row = await self._require_row(namespace_key=namespace_key, task_key=task_key)
        if AgentTaskStatus(row.status) is not AgentTaskStatus.RUNNING_UNKNOWN:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=(
                    f"Task {task_key} is {row.status}. Only a running_unknown task is "
                    "resolved by hand."
                ),
                resource="AgentTask",
                resource_id=task_key,
            )
        row.status = (
            AgentTaskStatus.QUEUED.value if requeue else AgentTaskStatus.FAILED.value
        )
        row.claimed_by = None
        row.heartbeat_at = None
        row.failure_code = "RESOLVED_BY_HUMAN"
        row.failure_detail = reason
        await self._db.flush()
        await self._db.refresh(row)
        return await self._detail(row)

    # -- internals ---------------------------------------------------------

    def _scoped(
        self,
        *,
        namespace_key: str,
        status: AgentTaskStatus | None,
        team_slug: str | None,
    ) -> Select[tuple[AgentTask]]:
        stmt = select(AgentTask).where(AgentTask.namespace_key == namespace_key)
        if status is not None:
            stmt = stmt.where(AgentTask.status == status.value)
        if team_slug is not None:
            stmt = stmt.where(AgentTask.team_slug == team_slug)
        return stmt

    async def _require_row(self, *, namespace_key: str, task_key: str) -> AgentTask:
        # ``populate_existing`` because the claim and the heartbeat are raw
        # statements: they move the row without the ORM noticing, so an
        # identity-mapped copy from earlier in the same transaction would read
        # back the values from before the claim.
        row = await self._db.scalar(
            select(AgentTask)
            .where(
                AgentTask.namespace_key == namespace_key,
                AgentTask.task_key == task_key,
            )
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_TASK_NOT_FOUND,
                detail=f"No task {task_key} in this namespace.",
                resource="AgentTask",
                resource_id=task_key,
            )
        return row

    async def _require_claimed(
        self, *, namespace_key: str, task_key: str, instance_id: str
    ) -> AgentTask:
        """The row, if this caller is the holder. Otherwise a conflict.

        Fenced on ``claimed_by`` rather than on the credential, because the
        credential is shared: one dispatcher key runs every instance, and two
        instances of it are exactly the case the lease exists to arbitrate.
        """
        row = await self._require_row(namespace_key=namespace_key, task_key=task_key)
        if row.claimed_by != instance_id:
            raise self._not_the_holder(task_key)
        return row

    def _require_within_deadline(self, row: AgentTask) -> None:
        deadline = row.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=dt.UTC)
        if deadline <= dt.datetime.now(dt.UTC):
            raise ConflictError(
                error_code=ErrorCode.TASK_DEADLINE_EXCEEDED,
                detail=(
                    f"Task {row.task_key} passed its deadline at {deadline.isoformat()}. "
                    "No further step may start."
                ),
                resource="AgentTask",
                resource_id=row.task_key,
                hint="Finish the task. A deadline is a ceiling, not a pause.",
            )

    def _not_the_holder(self, task_key: str) -> ConflictError:
        return ConflictError(
            error_code=ErrorCode.TASK_NOT_CLAIMED,
            detail=(
                f"This dispatcher does not hold the claim on task {task_key}. Either the "
                "lease expired and another one took it, or it was never claimed here."
            ),
            resource="AgentTask",
            resource_id=task_key,
            hint="Stop writing to this task. Claim it again, or move on.",
        )

    async def _open_source_refs(
        self, *, namespace_key: str, source_kind: TaskSourceKind, refs: Sequence[str]
    ) -> set[str]:
        if not refs:
            return set()
        rows = await self._db.execute(
            select(AgentTask.source_ref).where(
                AgentTask.namespace_key == namespace_key,
                AgentTask.source_kind == source_kind.value,
                AgentTask.source_ref.in_(list(refs)),
                AgentTask.status.notin_(_TERMINAL_VALUES),
            )
        )
        return set(rows.scalars().all())

    async def _terminal_source_refs(
        self, *, namespace_key: str, source_kind: TaskSourceKind, refs: Sequence[str]
    ) -> set[str]:
        if not refs:
            return set()
        rows = await self._db.execute(
            select(AgentTask.source_ref).where(
                AgentTask.namespace_key == namespace_key,
                AgentTask.source_kind == source_kind.value,
                AgentTask.source_ref.in_(list(refs)),
                AgentTask.status.in_(_TERMINAL_VALUES),
            )
        )
        return set(rows.scalars().all())

    async def _insert(
        self,
        *,
        namespace_key: str,
        items: Sequence[ImportTaskItem],
        source_kind: TaskSourceKind,
        team_slug: str | None,
        workflow_key: str,
        dry_run: bool,
        created_by_hash: str | None,
    ) -> list[str]:
        """Insert the eligible rows and report the keys that actually landed.

        ``ON CONFLICT DO NOTHING`` on the partial unique index. No
        ``index_where`` is passed because the conflict target is inferred from
        the index that fires, and naming a predicate that drifts from the
        index's would silently stop the clause matching - which fails loudly as
        an integrity error rather than quietly, but on somebody else's shift.
        """
        if not items:
            return []
        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=self._settings.task_deadline_seconds
        )
        payload = [
            {
                "namespace_key": namespace_key,
                "task_key": uuid4().hex,
                "source_kind": source_kind.value,
                "source_ref": item.source_ref,
                "source_url": item.source_url,
                "title": item.title,
                "body": item.body,
                "team_slug": team_slug,
                "workflow_key": workflow_key,
                "status": AgentTaskStatus.QUEUED.value,
                "dry_run": dry_run,
                "created_by_hash": created_by_hash,
                # A placeholder until the first claim resets it. NOT NULL,
                # because a task with no deadline is a task with no ceiling.
                "deadline_at": deadline,
            }
            for item in items
        ]
        stmt = (
            pg_insert(AgentTask)
            .values(payload)
            .on_conflict_do_nothing()
            .returning(AgentTask.task_key)
        )
        result = await self._db.execute(stmt)
        return [str(key) for key in result.scalars().all()]

    async def _import_response(
        self,
        *,
        namespace_key: str,
        mode: ImportMode,
        eligible: Sequence[ImportTaskItem],
        digest: str,
        skipped: ImportSkipCounts,
        workflow_key: str,
        dry_run: bool,
        created: int,
        task_keys: Sequence[str],
    ) -> ImportAgentTasksResponse:
        # Read after the insert on a commit, so ``tasks_created_this_hour``
        # includes what this call just created. A confirm that reported the
        # budget from before its own commit would show an operator an allowance
        # they have already spent.
        state = await read_snapshot(
            self._db, namespace_key=namespace_key, settings=self._settings
        )
        return ImportAgentTasksResponse(
            dispatch_state=state,
            mode=mode,
            eligible=[
                ImportCandidate(
                    source_ref=item.source_ref,
                    title=item.title,
                    source_url=item.source_url,
                )
                for item in eligible
            ],
            refs_digest=digest,
            skipped=skipped,
            workflow_key=workflow_key,
            dry_run=dry_run,
            created=created,
            task_keys=list(task_keys),
        )

    async def _detail(self, row: AgentTask) -> AgentTaskDetail:
        steps = (
            await self._db.execute(
                select(AgentTaskStepRow)
                .where(AgentTaskStepRow.task_id == row.id)
                .order_by(AgentTaskStepRow.step_index.asc())
            )
        ).scalars().all()
        return AgentTaskDetail(
            **self._summary(row).model_dump(),
            body=row.body,
            steps=[self._step(step) for step in steps],
        )

    def _summary(self, row: AgentTask) -> AgentTaskSummary:
        return AgentTaskSummary(
            task_key=row.task_key,
            source_kind=TaskSourceKind(row.source_kind),
            source_ref=row.source_ref,
            source_url=row.source_url,
            source_scope_kind=(
                TaskScopeKind(row.source_scope_kind)
                if row.source_scope_kind is not None
                else None
            ),
            source_scope_ref=row.source_scope_ref,
            source_scope_name=row.source_scope_name,
            source_team_key=row.source_team_key,
            title=row.title,
            team_slug=row.team_slug,
            workflow_key=row.workflow_key,
            status=AgentTaskStatus(row.status),
            dry_run=row.dry_run,
            current_step=row.current_step,
            turns_used=row.turns_used,
            claimed_by=row.claimed_by,
            claimed_at=row.claimed_at,
            heartbeat_at=row.heartbeat_at,
            lease_expires_at=(
                row.heartbeat_at + dt.timedelta(seconds=self._settings.task_lease_seconds)
                if row.heartbeat_at is not None
                else None
            ),
            deadline_at=row.deadline_at,
            chain_trace_id=row.chain_trace_id,
            failure_code=row.failure_code,
            failure_detail=row.failure_detail,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def _step(self, row: AgentTaskStepRow) -> AgentTaskStep:
        return AgentTaskStep(
            step_index=row.step_index,
            agent_name=row.agent_name,
            brief=row.brief,
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
