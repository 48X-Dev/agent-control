"""Queueing and sending step comments. The 5.6 half of write-back.

A ``comment`` row is created in the same transaction as the completed step it
reports, so the queue entry is durable before any network is tried, and it is
sent by the server on that same request - behind the write flag, after the
body passes controls evaluation as an explicit ``dispatch.writeback`` tool
step. Rows that attempt leaves ``pending`` or ``failed`` are retried when the
task finishes, and an operator can attempt one explicitly through the deliver
route; the marker dedupe is what makes every retry safe. A ``status_change``
row is also created here, when the task completes, and then never touched
again by anything in this module: deciding it belongs to
:mod:`.agent_task_review`, because **an agent never changes an issue's state
on the strength of its own claim.**

The task itself reaches ``completed`` and stays there whether or not its
write-back landed. Conflating "the work is done" with "the ticket was
updated" makes a Linear outage look like failed work, and the operator
response to those two is completely different.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

from agent_control_engine.core import ControlEngine
from agent_control_models import ControlMatch, EvaluationRequest, Step
from agent_control_models.errors import ErrorCode
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.tasks import (
    AgentTaskStepStatus,
    AgentTaskWriteback,
    TaskSourceKind,
    WritebackKind,
    WritebackStatus,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import DispatchSettings
from ..errors import ConflictError, NotFoundError
from ..models import AgentTask
from ..models import AgentTaskStep as AgentTaskStepRow
from ..models import AgentTaskWriteback as WritebackRow
from .controls import ControlService
from .linear_client import LinearError
from .linear_writeback import (
    WRITEBACK_STEP_NAME,
    comment_marker,
    compose_comment_body,
)
from .linear_writeback_runtime import WritebackRuntime

_logger = logging.getLogger(__name__)

EventEmitter = Callable[[list[ControlExecutionEvent]], Awaitable[None]]
"""How a deny leaves its audit artefact. The endpoint binds this to the
process ingestor; ``None`` in tests that are not about events."""


def wire_writeback(row: WritebackRow, *, task_key: str) -> AgentTaskWriteback:
    """One row as a caller may see it. Shared by the review gate, the task
    detail and the deliver route, so the three never drift apart."""
    return AgentTaskWriteback(
        writeback_id=row.id,
        task_key=task_key,
        kind=WritebackKind(row.kind),
        status=WritebackStatus(row.status),
        step_index=row.step_index,
        body=row.body,
        target_state_id=row.target_state_id,
        decision_digest=row.decision_digest,
        approved_by_hash=row.approved_by_hash,
        approved_at=row.approved_at,
        rejected_reason=row.rejected_reason,
        attempts=row.attempts,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def evaluate_writeback_body(
    db: AsyncSession, *, namespace_key: str, agent_name: str, body: str
) -> tuple[bool, list[ControlMatch]]:
    """Run the body through the agent's controls as a ``dispatch.writeback`` step.

    Mitigation 3 of 5.6: the one action outside the executor still passes the
    same controls as one inside it. An agent with no registration and no
    controls evaluates to allowed - the write-back is the dispatcher's action
    under the workspace credential, and an unregistered agent name must not
    make the comment silently undeliverable forever.
    """
    controls = await ControlService(db).list_runtime_controls_for_agent(
        agent_name,
        namespace_key=namespace_key,
        allow_invalid_step_name_regex=True,
    )
    if not controls:
        return True, []
    engine = ControlEngine(controls)
    response = await engine.process(
        EvaluationRequest(
            agent_name=agent_name,
            # Tool steps require object input; the body is the one field.
            step=Step(
                type="tool",
                name=WRITEBACK_STEP_NAME,
                input={"body": body},
                output=None,
                context=None,
            ),
            stage="pre",
        )
    )
    matches = list(response.matches or [])
    denied = any(match.action == "deny" for match in matches)
    return not denied, matches


def _deny_events(
    *, trace_id: str, agent_name: str, matches: list[ControlMatch]
) -> list[ControlExecutionEvent]:
    """The audit rows a refused write-back leaves on the chain trace."""
    return [
        ControlExecutionEvent(
            control_execution_id=match.control_execution_id,
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:16],
            agent_name=agent_name,
            control_id=match.control_id,
            control_name=match.control_name,
            check_stage="pre",
            applies_to="tool_call",
            action=match.action,
            matched=match.result.matched,
            confidence=match.result.confidence,
            evaluator_name=None,
            error_message=None,
            metadata={"step_name": WRITEBACK_STEP_NAME},
        )
        for match in matches
    ]


class WritebackQueueService:
    """Queueing and sending write-backs, within one namespace."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        settings: DispatchSettings,
        runtime: WritebackRuntime,
    ) -> None:
        self._db = db
        self._settings = settings
        self._runtime = runtime

    # -- queueing ----------------------------------------------------------

    def writeback_applies(self, task: AgentTask) -> bool:
        """Whether this task writes back at all.

        Dry run suppresses it entirely (5.6 mitigation 4): a dry run that
        still comments records work that never happened. Non-Linear sources
        have no tracker to write to.
        """
        return (
            not task.dry_run
            and task.source_kind == TaskSourceKind.LINEAR.value
        )

    async def enqueue_step_comment(
        self, *, task: AgentTask, step: AgentTaskStepRow, total_steps: int
    ) -> WritebackRow | None:
        """Queue the comment for one completed step. Idempotent per step.

        The row is written in the caller's transaction, beside the step it
        reports, so the queue entry is durable before any network is tried.
        The body is composed - and therefore sanitized - here, so what sits in
        the queue is byte-for-byte what would be posted.
        """
        if not self.writeback_applies(task):
            return None
        if step.status != AgentTaskStepStatus.COMPLETED.value:
            return None
        body = compose_comment_body(
            task_key=task.task_key,
            step_index=step.step_index,
            total_steps=max(total_steps, step.step_index + 1),
            agent_name=step.agent_name,
            output_text=step.output_text or "",
        )
        return await self._upsert_row(
            task=task,
            step_index=step.step_index,
            kind=WritebackKind.COMMENT,
            status=WritebackStatus.PENDING,
            body=body,
        )

    async def create_status_change_proposal(self, *, task: AgentTask) -> WritebackRow | None:
        """The task's final output becomes a proposal row in ``awaiting_approval``.

        Created when the task reaches ``completed`` and never sent by anything
        but a human's accept. The body is the final step's output because the
        session dies with the task and this text is the durable record.
        """
        if not self.writeback_applies(task):
            return None
        final = await self._db.scalar(
            select(AgentTaskStepRow)
            .where(
                AgentTaskStepRow.task_id == task.id,
                AgentTaskStepRow.status == AgentTaskStepStatus.COMPLETED.value,
            )
            .order_by(AgentTaskStepRow.step_index.desc())
            .limit(1)
        )
        return await self._upsert_row(
            task=task,
            step_index=final.step_index if final is not None else 0,
            kind=WritebackKind.STATUS_CHANGE,
            status=WritebackStatus.AWAITING_APPROVAL,
            body=(final.output_text or "") if final is not None else "",
        )

    async def _upsert_row(
        self,
        *,
        task: AgentTask,
        step_index: int,
        kind: WritebackKind,
        status: WritebackStatus,
        body: str,
    ) -> WritebackRow | None:
        stmt = (
            pg_insert(WritebackRow)
            .values(
                namespace_key=task.namespace_key,
                task_id=task.id,
                step_index=step_index,
                kind=kind.value,
                status=status.value,
                body=body,
            )
            .on_conflict_do_nothing(
                constraint="ux_agent_task_writebacks_step_kind"
            )
        )
        await self._db.execute(stmt)
        row: WritebackRow | None = await self._db.scalar(
            select(WritebackRow).where(
                WritebackRow.task_id == task.id,
                WritebackRow.step_index == step_index,
                WritebackRow.kind == kind.value,
            )
        )
        return row

    # -- sending comments --------------------------------------------------

    async def deliver_comment(
        self,
        *,
        row: WritebackRow,
        task: AgentTask,
        agent_name: str,
        emit_events: EventEmitter | None,
    ) -> WritebackRow:
        """One attempt to post one queued comment. Never raises for Linear.

        With the write flag off this returns the row untouched and still
        ``pending``, which is the shipped default: the queue fills, nothing
        reaches the tracker, and the console can show "done here, not written
        there" as two separate facts.

        A control deny is terminal (``denied``): the same body reproduces the
        same refusal, and the deny leaves ``control_execution_events`` rows on
        the chain trace so the one action outside the executor carries the
        same audit artefact as one inside it.
        """
        if row.status not in (WritebackStatus.PENDING.value, WritebackStatus.FAILED.value):
            return row
        if not self._runtime.can_write:
            return row

        allowed, matches = await evaluate_writeback_body(
            self._db,
            namespace_key=task.namespace_key,
            agent_name=agent_name,
            body=row.body,
        )
        if not allowed:
            row.status = WritebackStatus.DENIED.value
            row.last_error = "A control denied this write-back."
            await self._db.flush()
            if emit_events is not None and task.chain_trace_id:
                deny = [m for m in matches if m.action == "deny"]
                try:
                    await emit_events(
                        _deny_events(
                            trace_id=task.chain_trace_id,
                            agent_name=agent_name,
                            matches=deny,
                        )
                    )
                except Exception:
                    _logger.exception("Recording a write-back deny failed.")
            return row

        client = self._runtime.client
        assert client is not None  # can_write checked above
        marker = comment_marker(task.task_key, row.step_index)
        row.attempts = row.attempts + 1
        try:
            if await client.issue_has_marker(
                issue_id=task.source_ref, marker=marker
            ):
                # Found means already written, by this process or another.
                row.status = WritebackStatus.SENT.value
                row.last_error = None
            else:
                await client.create_comment(issue_id=task.source_ref, body=row.body)
                row.status = WritebackStatus.SENT.value
                row.last_error = None
        except LinearError as exc:
            row.status = WritebackStatus.FAILED.value
            row.last_error = exc.message
        await self._db.flush()
        return row

    async def deliver_pending_comments(
        self, *, task: AgentTask, emit_events: EventEmitter | None
    ) -> list[WritebackRow]:
        """One more attempt at every comment row this task still holds.

        This is 5.6's "retried independently of the task" made real: a row
        left ``failed`` by a Linear outage, or ``pending`` by a crash between
        the enqueue and the send, gets its retry when the task finishes rather
        than never. The re-finish routes 409 on completed work, so without
        this call the finish-step attempt would be the only one a row ever
        gets. The marker dedupe makes a second attempt safe, and rows already
        ``sent`` or ``denied`` are not selected at all.
        """
        if not self.writeback_applies(task) or not self._runtime.can_write:
            return []
        rows = (
            (
                await self._db.execute(
                    select(WritebackRow)
                    .where(
                        WritebackRow.task_id == task.id,
                        WritebackRow.kind == WritebackKind.COMMENT.value,
                        WritebackRow.status.in_(
                            (
                                WritebackStatus.PENDING.value,
                                WritebackStatus.FAILED.value,
                            )
                        ),
                    )
                    .order_by(WritebackRow.step_index.asc())
                )
            )
            .scalars()
            .all()
        )
        attempted: list[WritebackRow] = []
        for row in rows:
            agent_name = await self._agent_for_step(
                task_id=task.id, step_index=row.step_index
            )
            if agent_name is None:
                continue
            attempted.append(
                await self.deliver_comment(
                    row=row, task=task, agent_name=agent_name, emit_events=emit_events
                )
            )
        return attempted

    async def require_comment_row(
        self, *, namespace_key: str, task_key: str, writeback_id: int
    ) -> tuple[AgentTask, WritebackRow, str]:
        """Load one deliverable comment row, or refuse with the reason.

        The refusals are the route's contract. A ``status_change`` row is
        refused no matter its state, because redelivery must never become a
        second door past the review gate; ``sent`` and ``denied`` are refused
        because one is finished and the other reproduces the same refusal;
        and a disabled write flag is named outright rather than silently
        attempting nothing.
        """
        task = await self._db.scalar(
            select(AgentTask).where(
                AgentTask.namespace_key == namespace_key,
                AgentTask.task_key == task_key,
            )
        )
        if task is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_TASK_NOT_FOUND,
                detail=f"No task {task_key} in this namespace.",
                resource="AgentTask",
                resource_id=task_key,
            )
        row = await self._db.scalar(
            select(WritebackRow).where(
                WritebackRow.id == writeback_id,
                WritebackRow.task_id == task.id,
                WritebackRow.namespace_key == namespace_key,
            )
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_TASK_WRITEBACK_NOT_FOUND,
                detail=f"Task {task_key} has no write-back {writeback_id}.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        if row.kind != WritebackKind.COMMENT.value:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail="Only comment rows can be delivered here. A status "
                "change moves exclusively by a human's accept or reject.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        if row.status not in (
            WritebackStatus.PENDING.value,
            WritebackStatus.FAILED.value,
        ):
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Write-back {writeback_id} is {row.status}. Only a "
                "pending or failed comment has a delivery left to attempt.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        if not self._runtime.can_write:
            raise ConflictError(
                error_code=ErrorCode.LINEAR_WRITE_DISABLED,
                detail="Write-back to Linear is disabled on this deployment.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
                hint="Set AGENT_CONTROL_LINEAR_WRITE_ENABLED=true and restart.",
            )
        agent_name = await self._agent_for_step(
            task_id=task.id, step_index=row.step_index
        )
        if agent_name is None:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail="The step this row reports has no recorded agent, so "
                "there are no controls to evaluate the body against.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        return task, row, agent_name

    async def _agent_for_step(
        self, *, task_id: int, step_index: int
    ) -> str | None:
        agent_name: str | None = await self._db.scalar(
            select(AgentTaskStepRow.agent_name).where(
                AgentTaskStepRow.task_id == task_id,
                AgentTaskStepRow.step_index == step_index,
            )
        )
        return agent_name
