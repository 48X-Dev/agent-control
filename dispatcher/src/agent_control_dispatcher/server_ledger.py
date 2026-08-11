"""The shipped ledger: ``agent_tasks`` over HTTP, claimed atomically and held by lease.

One ``UPDATE ... WHERE ... RETURNING``; a read-then-write fails under concurrency.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from uuid import uuid4

from agent_control_models.attachments import StepFilesSummary
from agent_control_models.tasks import AgentTaskDetail, AgentTaskStepStatus

from .client import DispatchClient, DispatchHTTPError, Disposition
from .envelope import PriorReport
from .ledger import Claim, ClaimStatus
from .sources.base import SourceItem

INSTANCE_ID_ENV = "AGENT_CONTROL_DISPATCH_INSTANCE_ID"
"""Names this dispatcher on every write it makes.

The server fences step and finish writes on it, so two instances sharing one
id would be able to write to each other's claimed tasks - which is the exact
thing the lease exists to arbitrate. It is deliberately *not* derived from the
API key: one key runs every instance, and two instances of one key is the case
that has to be distinguishable."""

_TASK_STATUS_FOR: dict[ClaimStatus, str] = {
    ClaimStatus.COMPLETED: "completed",
    ClaimStatus.FAILED: "failed",
    ClaimStatus.BLOCKED: "blocked",
    ClaimStatus.PAUSED_QUOTA: "paused_quota",
    ClaimStatus.RUNNING_UNKNOWN: "running_unknown",
}
"""How a run's outcome maps onto the ledger's status machine.

``blocked`` and ``failed` stay apart on both sides of the wire. ``failed``
means the work was attempted and did not work; ``blocked`` means it was never
attempted because the configuration is wrong, and a loop retrying it produces
the same result forever."""

_CLAIM_STATUS_FOR: dict[str, ClaimStatus] = {
    "queued": ClaimStatus.CLAIMED,
    "running": ClaimStatus.CLAIMED,
    "completed": ClaimStatus.COMPLETED,
    "failed": ClaimStatus.FAILED,
    "blocked": ClaimStatus.BLOCKED,
    "paused_quota": ClaimStatus.PAUSED_QUOTA,
    "running_unknown": ClaimStatus.RUNNING_UNKNOWN,
    "cancelled": ClaimStatus.FAILED,
    "awaiting_approval": ClaimStatus.COMPLETED,
}


def default_instance_id() -> str:
    """A stable-enough name for this process, or the operator's own."""
    override = os.environ.get(INSTANCE_ID_ENV, "").strip()
    if override:
        return override[:64]
    return f"{socket.gethostname()[:24]}-{os.getpid()}-{uuid4().hex[:4]}"[:64]


class ServerTaskLedger:
    """``agent_tasks`` behind the :class:`~.ledger.TaskLedger` protocol."""

    def __init__(
        self,
        client: DispatchClient,
        *,
        instance_id: str | None = None,
        team_slug: str | None = None,
        queue_page_size: int = 100,
    ) -> None:
        self._client = client
        self._instance_id = instance_id or default_instance_id()
        self._team_slug = team_slug
        self._queue_page_size = queue_page_size
        self._task_keys: dict[tuple[str, str], str] = {}
        self._step_index: dict[tuple[str, str], int] = {}
        self._agent_names: dict[tuple[str, str], str] = {}
        self._claimed: dict[tuple[str, str], AgentTaskDetail] = {}
        self._open_steps: set[tuple[str, str]] = set()
        """Items whose step row exists. A claim alone does not put one here:
        a session that could not be opened means the turn never happened, so
        there is nothing to close and asking the server to close it would turn
        one failure into a crash."""

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def register(
        self,
        *,
        source_kind: str,
        items: Sequence[SourceItem],
        dry_run: bool,
        workflow_key: str | None = None,
    ) -> None:
        """Import the set, then read the queue back to learn which key is which."""
        if not items:
            return
        await self._client.import_tasks(
            items=items,
            source_kind=source_kind,
            dry_run=dry_run,
            team_slug=self._team_slug,
            workflow_key=workflow_key,
        )
        wanted = {item.ref for item in items}
        for status in ("queued", "running", "paused_quota"):
            page = await self._client.list_tasks(status=status, limit=self._queue_page_size)
            for task in page.tasks:
                if task.source_kind == source_kind and task.source_ref in wanted:
                    self._task_keys[(source_kind, task.source_ref)] = task.task_key

    def adopt(self, *, source_kind: str, ref: str, task_key: str) -> None:
        """Learn a task key from the queue rather than from an import."""
        self._task_keys[(source_kind, ref)] = task_key

    def forget(self, *, source_kind: str, ref: str) -> None:
        """Drop everything this ledger holds about one finished task."""
        key = (source_kind, ref)
        self._task_keys.pop(key, None)
        self._step_index.pop(key, None)
        self._agent_names.pop(key, None)
        self._claimed.pop(key, None)
        self._open_steps.discard(key)

    def claimed_task(self, *, source_kind: str, ref: str) -> AgentTaskDetail | None:
        """The row as it came back from this dispatcher's own claim."""
        return self._claimed.get((source_kind, ref))

    async def claim(self, *, source_kind: str, ref: str, agent_name: str, dry_run: bool) -> bool:
        """Take one task, or report that this dispatcher cannot have it."""
        del dry_run
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return False
        try:
            claimed = await self._client.claim_task(task_key=key, instance_id=self._instance_id)
        except DispatchHTTPError as exc:
            if exc.disposition is Disposition.FLEET_STOPPED:
                # Not about this item, and the one refusal here that is not.
                # A pause or a kill switch refuses *every* claim in the
                # namespace, so reporting it as "somebody else has it" would
                # send the run through every remaining item printing a reason
                # that is false, and finish reporting a clean pass. The caller
                # stops the run on this.
                raise
            # Every other refusal the claim predicate can express arrives as one
            # conflict, and every one of them means the same thing here: this
            # item is not ours. Distinguishing them would need a second read
            # that is stale the moment it lands.
            return False
        self._step_index[(source_kind, ref)] = claimed.resume_step_index
        self._agent_names[(source_kind, ref)] = agent_name
        self._claimed[(source_kind, ref)] = claimed.task
        return True

    def resume_step_index(self, *, source_kind: str, ref: str) -> int:
        """Where the chain starts, as the server decided at claim time."""
        return self._step_index.get((source_kind, ref), 0)

    async def prior_report(
        self, *, source_kind: str, ref: str, step_index: int
    ) -> PriorReport | None:
        """The completed step immediately before ``step_index``, from the ledger."""
        if step_index <= 0:
            return None
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return None
        task = await self._client.get_task(task_key=key)
        for step in task.steps:
            if step.step_index != step_index - 1:
                continue
            if AgentTaskStepStatus(step.status) is not AgentTaskStepStatus.COMPLETED:
                return None
            text = (step.output_text or "").strip()
            if not text:
                return None
            return PriorReport(agent_name=step.agent_name, brief=step.brief, text=text)
        return None

    def session_task_key(self, *, source_kind: str, ref: str) -> str | None:
        """The claimed task this item's session belongs to."""
        return self._task_keys.get((source_kind, ref))

    async def record_session(
        self,
        *,
        source_kind: str,
        ref: str,
        session_key: str,
        agent_name: str,
        brief: str,
        step_index: int | None = None,
    ) -> StepFilesSummary | None:
        """Open the step row, carrying the session, before the turn starts."""
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return None
        index = (
            step_index if step_index is not None else self._step_index.get((source_kind, ref), 0)
        )
        await self._client.heartbeat_task(task_key=key, instance_id=self._instance_id)
        files = await self._client.start_task_step(
            task_key=key,
            instance_id=self._instance_id,
            step_index=index,
            agent_name=agent_name,
            brief=brief,
            session_key=session_key,
        )
        self._step_index[(source_kind, ref)] = index
        self._agent_names[(source_kind, ref)] = agent_name
        self._open_steps.add((source_kind, ref))
        return files

    async def complete_step(
        self,
        *,
        source_kind: str,
        ref: str,
        step_index: int,
        output_text: str,
        turn_trace_id: str | None = None,
    ) -> None:
        """Close a hop that another hop follows. The task stays running."""
        key = self._task_keys.get((source_kind, ref))
        if key is None or (source_kind, ref) not in self._open_steps:
            return
        await self._client.finish_task_step(
            task_key=key,
            instance_id=self._instance_id,
            step_index=step_index,
            status=AgentTaskStepStatus.COMPLETED.value,
            output_text=output_text or None,
            turn_trace_id=turn_trace_id,
        )
        self._open_steps.discard((source_kind, ref))

    async def finish(
        self,
        *,
        source_kind: str,
        ref: str,
        status: ClaimStatus,
        outcome_code: str | None = None,
        detail: str | None = None,
        turn_trace_id: str | None = None,
        output_text: str | None = None,
        step_index: int | None = None,
    ) -> None:
        """Close the step, then the task. Never the task alone when a step ran."""
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return
        index = step_index if step_index is not None else self._step_index.get((source_kind, ref))
        if index is not None and (source_kind, ref) in self._open_steps:
            await self._client.finish_task_step(
                task_key=key,
                instance_id=self._instance_id,
                step_index=index,
                status=("completed" if status is ClaimStatus.COMPLETED else "failed"),
                output_text=output_text or None,
                turn_trace_id=turn_trace_id,
                failure_code=outcome_code,
                failure_detail=detail,
            )
        await self._client.finish_task(
            task_key=key,
            instance_id=self._instance_id,
            status=_TASK_STATUS_FOR.get(status, "failed"),
            failure_code=outcome_code,
            failure_detail=detail,
        )

    async def get(self, *, source_kind: str, ref: str) -> Claim | None:
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return None
        task = await self._client.get_task(task_key=key)
        steps = task.steps
        last = steps[-1] if steps else None
        return Claim(
            source_kind=source_kind,
            ref=ref,
            agent_name=(
                last.agent_name
                if last is not None
                else self._agent_names.get((source_kind, ref), "")
            ),
            status=_CLAIM_STATUS_FOR.get(str(task.status), ClaimStatus.CLAIMED),
            dry_run=task.dry_run,
            session_key=last.session_key if last is not None else None,
            turn_trace_id=last.turn_trace_id if last is not None else None,
            outcome_code=task.failure_code,
            detail=task.failure_detail,
        )

    async def aclose(self) -> None:
        """Nothing to release. The HTTP client is owned by the run, not by this."""
