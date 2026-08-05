"""The review gate: the queue of proposals, and the human decision. Plan 5.7.

Rows arrive here from :mod:`.agent_task_writeback_queue`: a ``status_change``
row is created in ``awaiting_approval`` when the task completes and does
nothing until a human presses accept or reject - **an agent never changes an
issue's state on the strength of its own claim. Ever, at any tier, behind any
flag.**

The task itself reaches ``completed`` and stays there whether or not anyone
accepts. ``AgentTaskStatus.AWAITING_APPROVAL`` is a *task* status reserved for
Phase 8's suspended tool calls and nothing here sets it; the ``awaiting``
state of this phase lives on the write-back row, precisely so that "the agent
is done" and "the tracker was changed" never become one fact.

The self-approval refusal is the invariant the accept path exists to hold: a
credential that ran the agents may not also accept their work. It is a
server-side comparison rather than an access tier because the local-credential
path has three tiers and no per-key operation allowlist. Under
``NoAuthProvider`` no caller has an identity at all (``caller_hash`` is
``None`` for everyone), so the comparison cannot bind and is skipped with a
warning rather than refusing every accept: with credential checks disabled the
deployment has opted out of separating principals everywhere, and a refusal
here would only disable the review queue while claiming a protection the rest
of the surface no longer has.
"""

from __future__ import annotations

import datetime as dt
import logging

from agent_control_models.errors import ErrorCode
from agent_control_models.tasks import (
    AgentTaskStatus,
    AgentTaskWriteback,
    ListReviewQueueResponse,
    ReviewQueueEntry,
    ReviewQueueIssue,
    WritebackKind,
    WritebackStatus,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import DispatchSettings
from ..errors import ConflictError, NotFoundError, ServiceUnavailableError
from ..models import AgentSession, AgentTask
from ..models import AgentTaskStep as AgentTaskStepRow
from ..models import AgentTaskWriteback as WritebackRow
from .agent_task_writeback_queue import EventEmitter, wire_writeback
from .linear_client import LinearError
from .linear_writeback import IssueReviewState, decision_digest
from .linear_writeback_runtime import WritebackRuntime

_logger = logging.getLogger(__name__)

ALREADY_COMPLETED_NOTE = "ALREADY_COMPLETED"


class TaskReviewService:
    """The review queue and the human decision over it, within one namespace."""

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

    # -- the review queue --------------------------------------------------

    async def review_queue(
        self,
        *,
        namespace_key: str,
        team_slug: str | None,
        milestone_id: str | None,
        limit: int,
    ) -> ListReviewQueueResponse:
        """The proposals waiting for a human, oldest first, targets read live.

        Each card shows the target and not only the claim, and the digest is
        computed at render time over ``(output_text, source_ref,
        target_state_id)`` so the accept can refuse anything that moved.
        A Linear read failure renders the entry with ``read_failed`` and no
        digest rather than hiding it: the queue must not shrink because the
        tracker is down.
        """
        stmt = (
            select(WritebackRow, AgentTask)
            .join(AgentTask, AgentTask.id == WritebackRow.task_id)
            .where(
                WritebackRow.namespace_key == namespace_key,
                WritebackRow.kind == WritebackKind.STATUS_CHANGE.value,
                WritebackRow.status == WritebackStatus.AWAITING_APPROVAL.value,
            )
        )
        if team_slug is not None:
            stmt = stmt.where(AgentTask.team_slug == team_slug)
        if milestone_id is not None:
            stmt = stmt.where(AgentTask.source_scope_ref == milestone_id)
        rows = (
            await self._db.execute(
                stmt.order_by(WritebackRow.created_at.asc(), WritebackRow.id.asc())
            )
        ).all()
        total = len(rows)

        entries: list[ReviewQueueEntry] = []
        for row, task in rows[:limit]:
            entries.append(await self._entry(row=row, task=task))
        return ListReviewQueueResponse(entries=entries, total=total)

    async def _entry(self, *, row: WritebackRow, task: AgentTask) -> ReviewQueueEntry:
        issue: ReviewQueueIssue | None = None
        digest: str | None = None
        client = self._runtime.client
        if client is not None:
            try:
                live = await client.fetch_issue_review_state(issue_id=task.source_ref)
                issue = ReviewQueueIssue(
                    source_ref=task.source_ref,
                    identifier=live.identifier,
                    title=live.title,
                    state_name=live.state_name,
                    state_type=live.state_type,
                    team_key=live.team_key,
                    milestone_id=live.milestone_id,
                )
                digest = await self._digest_for(task=task, row=row, live=live)
            except LinearError:
                issue = ReviewQueueIssue(source_ref=task.source_ref, read_failed=True)
        final_step = await self._db.scalar(
            select(AgentTaskStepRow).where(
                AgentTaskStepRow.task_id == task.id,
                AgentTaskStepRow.step_index == row.step_index,
            )
        )
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.UTC)
        age = dt.datetime.now(dt.UTC) - created_at
        return ReviewQueueEntry(
            task_key=task.task_key,
            writeback_id=row.id,
            agent_name=final_step.agent_name if final_step is not None else None,
            summary=row.body,
            source_ref=task.source_ref,
            source_url=task.source_url,
            team_slug=task.team_slug,
            source_scope_name=task.source_scope_name,
            chain_trace_id=task.chain_trace_id,
            created_at=row.created_at,
            stale=age > dt.timedelta(hours=self._settings.review_stale_after_hours),
            decision_digest=digest,
            issue=issue,
        )

    async def _digest_for(
        self, *, task: AgentTask, row: WritebackRow, live: IssueReviewState
    ) -> str | None:
        """The digest over the live target state, or ``None`` when unresolvable."""
        team_key = task.source_team_key or live.team_key
        resolver = self._runtime.resolver
        if team_key is None or resolver is None:
            return None
        try:
            state_id = await resolver.resolve_completed_state(team_key)
        except LinearError:
            return None
        return decision_digest(row.body, task.source_ref, state_id)

    # -- accept and reject -------------------------------------------------

    async def accept(
        self,
        *,
        namespace_key: str,
        task_key: str,
        writeback_id: int,
        expected_decision_digest: str,
        caller_hash: str | None,
        emit_events: EventEmitter | None = None,
    ) -> tuple[AgentTask, WritebackRow, str | None, str | None]:
        """The seven steps of 5.7, in order. Returns (task, row, note, team_key).

        The Linear mutation runs before the row is marked ``sent``, so a crash
        between them re-offers an issue that is already closed - which the
        ``ALREADY_COMPLETED`` branch then reports as a note, because a human
        (or an earlier accept) having closed it first is the system working.
        """
        del emit_events  # Reserved: an accept currently leaves no deny events.
        task, row = await self._require_pair(
            namespace_key=namespace_key, task_key=task_key, writeback_id=writeback_id
        )

        # Step 1: only a completed task's awaiting proposal, and never dry run.
        if AgentTaskStatus(task.status) is not AgentTaskStatus.COMPLETED:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Task {task_key} is {task.status}; only a completed task's "
                "write-back can be accepted.",
                resource="AgentTask",
                resource_id=task_key,
            )
        if row.status != WritebackStatus.AWAITING_APPROVAL.value:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Write-back {writeback_id} is {row.status}, not awaiting approval.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        if task.dry_run:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail="A dry-run task proposes nothing and its issue is never closed.",
                resource="AgentTask",
                resource_id=task_key,
            )

        # Step 2: the invariant. The credential that ran this may not accept it.
        await self._refuse_self_approval(task=task, caller_hash=caller_hash)

        if not self._runtime.can_write:
            raise ConflictError(
                error_code=ErrorCode.LINEAR_WRITE_DISABLED,
                detail="Write-back to Linear is disabled on this deployment.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
                hint="Set AGENT_CONTROL_LINEAR_WRITE_ENABLED=true and restart.",
            )
        client = self._runtime.client
        resolver = self._runtime.resolver
        assert client is not None and resolver is not None

        try:
            live = await client.fetch_issue_review_state(issue_id=task.source_ref)
        except LinearError as exc:
            raise ServiceUnavailableError(
                error_code=ErrorCode.LINEAR_UNAVAILABLE,
                detail=f"The issue could not be read from Linear: {exc.message}",
                resource="AgentTaskWriteback",
                hint="Nothing was changed. Try again when Linear answers.",
            ) from exc

        # Step 3: the issue must still be where it was imported from.
        if task.source_team_key and live.team_key != task.source_team_key:
            raise ConflictError(
                error_code=ErrorCode.SCOPE_CHANGED,
                detail="The issue moved to another team since it was imported. "
                "Nothing was changed.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        if (
            task.source_scope_kind == "milestone"
            and task.source_scope_ref
            and live.milestone_id != task.source_scope_ref
        ):
            raise ConflictError(
                error_code=ErrorCode.SCOPE_CHANGED,
                detail="The issue left the milestone it was imported under. "
                "Nothing was changed.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )

        # Step 4: the target state comes from the team's workflow and nowhere
        # else. Not the request, not the agent's text.
        team_key = task.source_team_key or live.team_key
        if team_key is None:
            raise ServiceUnavailableError(
                error_code=ErrorCode.LINEAR_UNAVAILABLE,
                detail="The issue's team could not be determined.",
                resource="AgentTaskWriteback",
            )
        try:
            target_state_id = await resolver.resolve_completed_state(team_key)
        except LinearError as exc:
            raise ServiceUnavailableError(
                error_code=ErrorCode.LINEAR_UNAVAILABLE,
                detail=f"The team's completed state could not be read: {exc.message}",
                resource="AgentTaskWriteback",
                hint="Nothing was changed. Try again when Linear answers.",
            ) from exc

        # The digest binds text, target and state together. Any of the three
        # moving between render and press is a refusal; the card re-reads the
        # queue, which recomputes the digest over what is now true. The fresh
        # digest deliberately stays out of the refusal: handing it back would
        # invite a blind retry over content nobody re-read.
        current = decision_digest(row.body, task.source_ref, target_state_id)
        if expected_decision_digest != current:
            raise ConflictError(
                error_code=ErrorCode.DECISION_CHANGED,
                detail="The output, the target issue, or the completed state moved "
                "between the card you read and this accept. Nothing was changed.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )

        # Steps 5 and 7: write, or record that a human beat us to it.
        note: str | None = None
        if live.state_type == "completed":
            note = ALREADY_COMPLETED_NOTE
        else:
            try:
                await client.update_issue_state(
                    issue_id=task.source_ref, state_id=target_state_id
                )
            except LinearError as exc:
                row.attempts = row.attempts + 1
                row.last_error = exc.message
                await self._db.flush()
                raise ServiceUnavailableError(
                    error_code=ErrorCode.LINEAR_UNAVAILABLE,
                    detail=f"Linear refused the state change: {exc.message}",
                    resource="AgentTaskWriteback",
                    hint="The proposal is still waiting. Try again.",
                ) from exc

        row.status = WritebackStatus.SENT.value
        row.target_state_id = target_state_id
        row.decision_digest = current
        row.approved_by_hash = caller_hash
        row.approved_at = dt.datetime.now(dt.UTC)
        row.attempts = row.attempts + 1
        row.last_error = None
        await self._db.flush()
        # ``updated_at`` carries an ``onupdate``, which expires the attribute;
        # read it back now so serializing after commit stays synchronous.
        await self._db.refresh(row)
        return task, row, note, team_key

    async def reject(
        self,
        *,
        namespace_key: str,
        task_key: str,
        writeback_id: int,
        reason: str | None,
        caller_hash: str | None,
    ) -> tuple[AgentTask, WritebackRow]:
        """Decline the proposal. The task stays completed, the issue open.

        The self-approval refusal applies here too: a dispatcher must not be
        able to bury its own output before a human reads it, which is the
        mirror image of approving it.
        """
        task, row = await self._require_pair(
            namespace_key=namespace_key, task_key=task_key, writeback_id=writeback_id
        )
        if row.status != WritebackStatus.AWAITING_APPROVAL.value:
            raise ConflictError(
                error_code=ErrorCode.TASK_STATUS_CONFLICT,
                detail=f"Write-back {writeback_id} is {row.status}, not awaiting approval.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        await self._refuse_self_approval(task=task, caller_hash=caller_hash)
        row.status = WritebackStatus.REJECTED.value
        row.rejected_reason = reason
        await self._db.flush()
        await self._db.refresh(row)
        return task, row

    # -- internals ---------------------------------------------------------

    async def _require_pair(
        self, *, namespace_key: str, task_key: str, writeback_id: int
    ) -> tuple[AgentTask, WritebackRow]:
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
        # Both decisions are check-then-act, and accept's window spans a live
        # Linear round trip. The row lock holds until the deciding transaction
        # commits, so a concurrent decision waits here, re-reads, and refuses
        # on status instead of committing a record that contradicts the
        # mutation (an issue closed in Linear under a row marked rejected).
        row = await self._db.scalar(
            select(WritebackRow)
            .where(
                WritebackRow.id == writeback_id,
                WritebackRow.task_id == task.id,
                WritebackRow.namespace_key == namespace_key,
            )
            .with_for_update()
        )
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_TASK_WRITEBACK_NOT_FOUND,
                detail=f"Task {task_key} has no write-back {writeback_id}.",
                resource="AgentTaskWriteback",
                resource_id=str(writeback_id),
            )
        return task, row

    async def _refuse_self_approval(
        self, *, task: AgentTask, caller_hash: str | None
    ) -> None:
        """409 when the deciding credential is one that ran the work.

        Compared against ``claimed_by_hash`` and the ``created_by_hash`` of
        every session belonging to the task. Sessions are deleted after a
        retention grace, so the set can be empty; ``claimed_by_hash`` survives
        on the task row, which is why it is recorded there at claim time.

        ``caller_hash`` is ``None`` under ``NoAuthProvider``, where no caller
        anywhere has an identity. The comparison cannot bind and is skipped
        with a warning; refusing on ``None == None`` would refuse every accept
        in every unauthenticated deployment while protecting nothing, because
        the same anonymous caller already holds every other operation.
        """
        if caller_hash is None:
            _logger.warning(
                "Accepting task %s with no caller identity: credential checks are "
                "disabled, so the self-approval refusal cannot bind.",
                task.task_key,
            )
            return
        ran_it: set[str] = set()
        if task.claimed_by_hash:
            ran_it.add(task.claimed_by_hash)
        session_hashes = (
            await self._db.execute(
                select(AgentSession.created_by_hash).where(
                    AgentSession.namespace_key == task.namespace_key,
                    AgentSession.agent_task_id == task.id,
                    AgentSession.created_by_hash.is_not(None),
                )
            )
        ).scalars()
        ran_it.update(h for h in session_hashes if h)
        if caller_hash in ran_it:
            raise ConflictError(
                error_code=ErrorCode.SELF_APPROVAL_REFUSED,
                detail="This credential ran the task, so it may not accept or "
                "reject the task's work.",
                resource="AgentTask",
                resource_id=task.task_key,
                hint="A different person, with a different credential, reviews it.",
            )

    def wire(self, row: WritebackRow, *, task_key: str) -> AgentTaskWriteback:
        return wire_writeback(row, task_key=task_key)
