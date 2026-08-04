"""The shipped ledger: ``agent_tasks``, over HTTP, and why it is not local.

The SQLite ledger in :mod:`.ledger` records a claim one process believes in.
This one records a claim **two processes contend for**, and the difference is
not a refinement - it is the difference between a dispatcher that can run
unattended and one that cannot. Three properties come from the server and
cannot be had on this side of the wire at all:

*The claim is atomic.* One ``UPDATE ... WHERE ... RETURNING`` inside Postgres.
Zero rows back means somebody else won, which is a 409 and a signal to move on.
A read-then-write here would pass every test its author wrote and fail under
exactly the concurrency it was added to prevent.

*The claim expires.* A lease, refreshed by a heartbeat, so a dispatcher that is
killed mid-task does not hold its row forever. Nothing in a local file can
notice that this process has stopped existing.

*A dead holder's work is recoverable and visible.* On reclaim the server marks
steps still ``running`` as ``abandoned``, and the console shows the gap rather
than papering over it.

**What this module still does not do**, said plainly because the shape invites
the assumption: it does not enforce anything. The pause, the executor kill
switch and the namespace turn budget are refusals on the turn path inside the
server, and the checks a dispatcher makes are optimisations so it does not open
sessions it cannot use. A ceiling enforced by the process being budgeted is not
a ceiling.

Section 14 promised the CLI signature would not change when the ledger landed.
It did not: ``dispatch once --source ... --agent ... --max-tasks N --dry-run``
is unchanged, and the only new thing is that ``--ledger`` is now how you ask
for the *old* behaviour rather than how you configure the new one.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Sequence
from uuid import uuid4

from agent_control_models.attachments import StepFilesSummary
from agent_control_models.tasks import AgentTaskStepStatus

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
    """A stable-enough name for this process, or the operator's own.

    Host plus pid plus four random characters. The random half matters: two
    containers can share a hostname and a pid namespace, and two instances that
    hash to one name would each be able to write to the other's tasks.
    """
    override = os.environ.get(INSTANCE_ID_ENV, "").strip()
    if override:
        return override[:64]
    return f"{socket.gethostname()[:24]}-{os.getpid()}-{uuid4().hex[:4]}"[:64]


class ServerTaskLedger:
    """``agent_tasks`` behind the :class:`~.ledger.TaskLedger` protocol.

    Holds one thing of its own: the map from a source ref to the task key the
    server minted for it. That map is built by reading the queue rather than by
    trusting what an import returned, because the queue is the authority on
    what is still claimable and an import's answer is a snapshot that another
    dispatcher may already have acted on.
    """

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
        """Import the set, then read the queue back to learn which key is which.

        The import is preview-then-commit against the digest of the preview, so
        what is created is the set that was read a moment earlier and not a set
        that moved in between. Items that already have an open task are
        reported as ``already_queued`` and are not re-created; the queue read
        that follows picks them up anyway, which is what makes re-running this
        over the same source resumable rather than duplicative.
        """
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
            page = await self._client.list_tasks(
                status=status, limit=self._queue_page_size
            )
            for task in page.tasks:
                if task.source_kind == source_kind and task.source_ref in wanted:
                    self._task_keys[(source_kind, task.source_ref)] = task.task_key

    async def claim(
        self, *, source_kind: str, ref: str, agent_name: str, dry_run: bool
    ) -> bool:
        """Take one task, or report that this dispatcher cannot have it.

        ``dry_run`` is not passed: it was fixed on the row at import and is not
        a per-claim choice. A dispatcher that could flip it would be able to
        turn a dry run into a live one after a human had agreed to the former.
        """
        del dry_run
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return False
        try:
            claimed = await self._client.claim_task(
                task_key=key, instance_id=self._instance_id
            )
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
        return True

    def resume_step_index(self, *, source_kind: str, ref: str) -> int:
        """Where the chain starts, as the server decided at claim time.

        ``MAX(step_index) WHERE status='completed'`` plus one, computed from the
        step rows rather than from ``current_step``. The two disagree exactly
        when a dispatcher died between a 200 from ``POST /turns`` and its own
        bookkeeping, and the counter is the half that is allowed to be wrong.
        Recomputing it here would be a second implementation of the resume rule,
        and the second implementation is the one that re-runs a step that
        already spent money.
        """
        return self._step_index.get((source_kind, ref), 0)

    async def prior_report(
        self, *, source_kind: str, ref: str, step_index: int
    ) -> PriorReport | None:
        """The completed step immediately before ``step_index``, from the ledger.

        Read over the wire rather than from this process's memory, and that is
        the point of the method existing. A reclaimed task resumes mid-chain in
        a *different* dispatcher: the one that ran the earlier steps is gone,
        and holding the previous report in a local dict would mean the
        successor either invents an empty one or fails a step that actually
        succeeded. ``agent_task_steps`` is where that text survives, which is
        the whole reason the table is there.

        Only a ``completed`` step at exactly ``step_index - 1`` answers. An
        abandoned or failed one is not a report, and neither is a step two
        positions back: skipping the gap would hand the next agent a report
        whose place in the chain is not the place it thinks it is.
        """
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
            return PriorReport(
                agent_name=step.agent_name, brief=step.brief, text=text
            )
        return None

    def session_task_key(self, *, source_kind: str, ref: str) -> str | None:
        """The claimed task this item's session belongs to.

        Sent when the session is opened rather than recorded afterwards,
        because the column it lands in is what the turn path reads: a session
        that reaches ``POST /turns`` without it is a fleet turn the server
        cannot tell from a human's chat, and every ceiling that keys off it -
        the namespace budget, the dispatch pause, the kill switch - silently
        does not apply to it. It is also what lets an operator without an admin
        key read and halt this task's session.
        """
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
        """Open the step row, carrying the session, before the turn starts.

        This is also where the server fetches whatever the tracker has attached
        to the item, which is why it now returns something. It has to happen
        here and not earlier: the envelope is built from the answer, and an
        envelope built before the fetch could not describe it.

        The heartbeat immediately before it is not ceremony, and on a chain it
        is what the lease depends on. The claim's lease started when the claim
        did; a four-step chain of five-minute turns runs for twenty minutes
        under one claim, so the refresh between hops is the thing that stops a
        second dispatcher taking the task out from under a running one.
        Section 5.4 puts the heartbeat *between* steps for exactly this reason:
        a step can legitimately take five minutes, so it cannot be inside one.

        ``step_index`` is the position about to run, and ``None`` means the one
        the claim reported - which is where a single-step task starts and where
        a reclaimed one resumes. A chain passes each position explicitly, so a
        later ``finish`` closes the hop that actually ran rather than the index
        the claim named several hops ago.
        """
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return None
        index = (
            step_index
            if step_index is not None
            else self._step_index.get((source_kind, ref), 0)
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
        """Close a hop that another hop follows. The task stays running.

        The task-level write is deliberately absent. A chain's task is finished
        once, by whatever ends it: the last hop's success, or the first hop's
        failure. Writing a task status between hops would mean a two-agent task
        reaching ``completed`` when its researcher finished, and an operator
        reading that would believe the writer had run.
        """
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
        """Close the step, then the task. Never the task alone when a step ran.

        The step write carries the agent's output, and that output is the
        durable record: the session is deleted when the task ends, so there is
        no transcript to go back to and a task row that recorded only a status
        would have thrown the work away.

        A failure before the session existed has no step to close, which is
        the one case where only the task is written. On a chain, the hops before
        this one are already closed by :meth:`complete_step` and are not touched
        again: this call ends the hop that was open and the task with it.
        """
        key = self._task_keys.get((source_kind, ref))
        if key is None:
            return
        index = step_index if step_index is not None else self._step_index.get(
            (source_kind, ref)
        )
        if index is not None and (source_kind, ref) in self._open_steps:
            await self._client.finish_task_step(
                task_key=key,
                instance_id=self._instance_id,
                step_index=index,
                status=(
                    "completed" if status is ClaimStatus.COMPLETED else "failed"
                ),
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
