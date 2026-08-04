"""HTTP routes for the dispatch ledger.

Import work, list it, claim one row, hold the lease, write what each agent
produced, and record how the task ended. That is the whole surface, and the
shape of it is the architectural line made concrete: **every route here is a
request about rows.** None of them starts a turn, opens a session, contacts an
executor, or schedules anything. The loop that does those things is a separate
process, and this module is what stops that process from being the only thing
that knows what it did.

Three operations, not one, and the split is deliberate.

``agent_tasks.write`` is importing work and operator moves on it - cancelling a
queued row, resolving one a human has to look at. ``agent_tasks.claim`` is the
dispatcher's surface: taking a row, holding the lease, and writing steps
against a row it holds. Splitting them costs two enum members and means a
future deployment can hand a scheduler a credential that runs work without one
that can queue it.

``agent_tasks.approve`` is the accept path: a human agreeing that an agent's
claim to have finished may change a tracker their team plans against. It is a
third operation precisely so nobody folds it into ``write`` later - ``write``
covers operator moves on rows (cancel, resolve, redelivering a stranded
comment), and none of those may close an issue.

What no route accepts, at any tier: an agent name on an import, a trace id, a
lease length, or a deadline. Agent selection is server-side configuration, the
chain trace is minted by the server at claim time, and the two ceilings are
read from deployment settings. A caller that could set its own lease could
reclaim live work; a caller that could set its own deadline would have no
deadline.
"""

from __future__ import annotations

import logging

from agent_control_models.attachments import StepFilesSummary
from agent_control_models.errors import ErrorCode
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.server import PaginationInfo
from agent_control_models.tasks import (
    AcceptAgentTaskRequest,
    AcceptAgentTaskResponse,
    AgentTaskResponse,
    AgentTaskStatus,
    AgentTaskStepResponse,
    AgentTaskStepStatus,
    CancelAgentTaskRequest,
    ClaimAgentTaskRequest,
    ClaimAgentTaskResponse,
    DeliverAgentTaskWritebackResponse,
    FinishAgentTaskRequest,
    FinishAgentTaskStepRequest,
    GetAgentTaskResponse,
    HeartbeatAgentTaskRequest,
    HeartbeatAgentTaskResponse,
    ImportAgentTasksRequest,
    ImportAgentTasksResponse,
    ListAgentTasksResponse,
    ListReviewQueueResponse,
    RejectAgentTaskRequest,
    RejectAgentTaskResponse,
    ResolveAgentTaskRequest,
    StartAgentTaskStepRequest,
)
from agent_control_models.teams import TEAM_SLUG_MAX_LENGTH
from agent_control_models.workflows import (
    GetAgentTaskChainResponse,
    GetAgentTaskPlanResponse,
)
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import dispatch_settings
from ..db import get_async_db
from ..errors import BadRequestError
from ..models import AgentTaskStep as AgentTaskStepRow
from ..services.agent_task_review import TaskReviewService
from ..services.agent_task_writeback_queue import (
    EventEmitter,
    WritebackQueueService,
    wire_writeback,
)
from ..services.agent_tasks import AgentTasksService
from ..services.agent_workflows import AgentWorkflowsService
from ..services.caller_identity import hash_caller_id
from ..services.linear_issues import get_milestone_issues_service
from ..services.linear_milestones import get_milestone_service
from ..services.linear_writeback_runtime import WritebackRuntime, get_writeback_runtime
from ..services.step_attachment_conversions import settle_step_conversions
from ..services.step_attachments import (
    fetch_step_files,
    plan_step_files,
    record_step_summary,
    store_step_files,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent-tasks", tags=["agent-tasks"])

_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 100

TASK_KEY_PATH = Path(
    ...,
    min_length=32,
    max_length=32,
    pattern=r"^[0-9a-f]{32}$",
    description="The task key returned by import.",
)


def _service(db: AsyncSession) -> AgentTasksService:
    return AgentTasksService(db, settings=dispatch_settings)


def _queue_service(db: AsyncSession, runtime: WritebackRuntime) -> WritebackQueueService:
    return WritebackQueueService(db, settings=dispatch_settings, runtime=runtime)


def _review_service(db: AsyncSession, runtime: WritebackRuntime) -> TaskReviewService:
    return TaskReviewService(db, settings=dispatch_settings, runtime=runtime)


def _event_emitter(request: Request, namespace_key: str) -> EventEmitter | None:
    """Bind the process ingestor, so a write-back deny lands on the chain trace."""
    ingestor = getattr(request.app.state, "event_ingestor", None)
    if ingestor is None:
        return None

    async def emit(events: list[ControlExecutionEvent]) -> None:
        await ingestor.ingest(events, namespace_key=namespace_key)

    return emit


def _parse_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return None
    try:
        return int(cursor)
    except ValueError as exc:
        raise BadRequestError(
            error_code=ErrorCode.VALIDATION_ERROR,
            detail="cursor must be a value returned by next_cursor.",
            hint="Pass the cursor returned in the previous response unchanged.",
        ) from exc


@router.post(
    "/import",
    response_model=ImportAgentTasksResponse,
    summary="Preview or commit a set of tasks",
    response_description="The eligible set, its digest, and what was created",
)
async def import_agent_tasks(
    request: ImportAgentTasksRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_WRITE)),
) -> ImportAgentTasksResponse:
    """Two modes on one route, and the preview is what makes the commit safe.

    ``mode=preview`` does every read and every duplicate check and inserts
    nothing, so it is safe to call on every render of a confirm. It returns
    **the items themselves**, not a count. That is the difference between an
    authorization and a gesture: an attacker with tracker access can file an
    item into a targeted scope, and an operator who expected four and is shown
    "5 items" presses anyway, because 5 and 4 look the same at a glance.

    ``mode=commit`` requires ``expected_refs_digest``, a sha256 over the sorted
    refs of the set that was previewed, and refuses with 409 ``SCOPE_CHANGED``
    carrying a fresh digest when it no longer matches. Over the set rather than
    the count, so four items swapped for four different items fails too.

    Nothing here selects an agent. The body has no field that could, and that
    is the property the whole injection argument rests on.
    """
    service = _service(db)
    response = await service.import_tasks(
        namespace_key=principal.namespace_key,
        request=request,
        created_by_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    return response


@router.get(
    "",
    response_model=ListAgentTasksResponse,
    summary="List tasks in this namespace",
    response_description="A page of tasks, oldest first",
)
async def list_agent_tasks(
    status: AgentTaskStatus | None = Query(
        None, description="Filter by status. The claim poll passes 'queued'."
    ),
    team: str | None = Query(
        None,
        min_length=1,
        max_length=TEAM_SLUG_MAX_LENGTH,
        description="Filter by the Agent Control team the task runs under.",
    ),
    limit: int = Query(_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    cursor: str | None = Query(None, description="Value of next_cursor from a previous page."),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_READ)),
) -> ListAgentTasksResponse:
    """A page of tasks. Bodies are not included; titles are.

    Oldest first, which makes this both the console list and the claim poll.
    Two dispatchers polling it get the same page in the same order, so both
    attempt the head of the queue and one wins every race: safe, and not
    faster. That is a stated property rather than an oversight - making a
    second dispatcher useful needs an offset on this route and a shuffled claim
    order, and neither is built until somebody has a backlog that needs it.
    """
    service = _service(db)
    tasks, total, next_cursor = await service.list_tasks(
        namespace_key=principal.namespace_key,
        status=status,
        team_slug=team,
        limit=limit,
        after_id=_parse_cursor(cursor),
    )
    return ListAgentTasksResponse(
        tasks=tasks,
        pagination=PaginationInfo(
            limit=limit,
            total=total,
            next_cursor=str(next_cursor) if next_cursor is not None else None,
            has_more=next_cursor is not None,
        ),
    )


@router.get(
    "/review",
    response_model=ListReviewQueueResponse,
    summary="List completed tasks waiting for a human decision",
    response_description="One entry per proposal, oldest first, targets read live",
)
async def list_review_queue(
    team: str | None = Query(
        None,
        min_length=1,
        max_length=TEAM_SLUG_MAX_LENGTH,
        description="Filter by the Agent Control team the task ran under.",
    ),
    milestone_id: str | None = Query(
        None,
        min_length=1,
        max_length=255,
        description="Filter by the milestone the task was imported from.",
    ),
    limit: int = Query(_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_READ)),
) -> ListReviewQueueResponse:
    """The review queue of 5.7: an agent's completion claim, waiting.

    Each entry shows the target as well as the claim - the issue's identifier,
    title and state are read live from Linear at render time - and carries the
    ``decision_digest`` the accept must echo back. There is deliberately no
    accept-all anywhere in this API: bulk-accepting N claims would be one
    recorded decision covering work nobody read.

    Entries never expire out of this list. ``stale`` starts rendering true
    after the deployment's ``review_stale_after_hours`` so age is visible,
    and that is all it does.
    """
    service = _review_service(db, runtime)
    return await service.review_queue(
        namespace_key=principal.namespace_key,
        team_slug=team,
        milestone_id=milestone_id,
        limit=limit,
    )


@router.get(
    "/{task_key}",
    response_model=GetAgentTaskResponse,
    summary="Read one task and its steps",
    response_description="The task, its body, and every step recorded against it",
)
async def get_agent_task(
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_READ)),
) -> GetAgentTaskResponse:
    """The task, with the chain assembled from its own step rows.

    The chain is these rows and not a trace rollup. A rollup is built from
    control-execution events, so an agent with no bound control that fired
    contributes no hops and vanishes from it entirely - a three-agent chain
    where two have no controls renders as one agent with nothing saying the
    rest is missing. These rows show every step whether or not a control fired,
    and each one carries its own trace for whoever wants the forensic view.

    ``body`` is untrusted input written by whoever has access to the source.
    Nothing that renders it may treat it as instructions.
    """
    service = _service(db)
    task = await service.get_detail(
        namespace_key=principal.namespace_key, task_key=task_key
    )
    return GetAgentTaskResponse(task=task)


@router.get(
    "/{task_key}/plan",
    response_model=GetAgentTaskPlanResponse,
    summary="Read the resolved chain of agents this task is supposed to run",
    response_description="One step per hop, with each agent resolved or reported unresolved",
)
async def get_agent_task_plan(
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_READ)),
) -> GetAgentTaskPlanResponse:
    """Which agent runs which step, decided here and nowhere else.

    Two sources, both server-side configuration: the workflow step's own
    ``agent_name``, then the team's ``default_agent_name``. There is no third,
    and in particular **nothing on the task reaches this decision** - not its
    title, not its body, not its labels, not which source it came from. Anyone
    who can file an issue in a tracker can label it, so a label that chose the
    agent would let whoever filed the issue choose the executor, and agents
    differ in system prompt, in bound controls and in tools.

    A step neither source can answer comes back with ``agent_name`` null and its
    index in ``unresolved_step_indexes``. A dispatcher reading that blocks the
    task with ``NO_AGENT_SELECTED`` rather than choosing one; a human then pins
    the agent on the step or sets the team default.

    A task whose ``workflow_key`` names a workflow that has since been deleted
    resolves to the implicit one-step plan, rather than 404ing - the task row
    still exists, and taking its page away is the wrong answer at the moment
    somebody is trying to work out what went wrong with it.
    """
    task = await _service(db).get_row(
        namespace_key=principal.namespace_key, task_key=task_key
    )
    plan = await AgentWorkflowsService(db).plan_for_task(
        namespace_key=principal.namespace_key, task=task
    )
    return GetAgentTaskPlanResponse(plan=plan)


@router.get(
    "/{task_key}/chain",
    response_model=GetAgentTaskChainResponse,
    summary="Read what actually ran, hop by hop",
    response_description="The planned positions merged with the recorded step rows",
)
async def get_agent_task_chain(
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_READ)),
) -> GetAgentTaskChainResponse:
    """The chain, assembled from ``agent_task_steps`` and from the plan.

    **Never from a trace.** ``GET /observability/traces/{trace_id}`` builds hops
    exclusively from control-execution events, which only the SDK writes, so an
    agent with no bound control that fired contributes zero hops and vanishes:
    a three-agent chain where two have no controls renders there as a one-agent
    trace with nothing saying the rest is missing, and a trace with no events at
    all 404s outright. These rows show every hop whether or not a control fired,
    and each carries its own ``turn_trace_id`` for whoever wants the forensic
    view of one of them.

    **Never from a caller-supplied id, either.** ``chain_trace_id`` is minted by
    the server at claim time and is on this response for correlation only. The
    audited party does not author its own audit record: a caller-chosen trace
    could attach one team's hops into another team's chain, or make a chain read
    as fewer hops than actually happened.

    Merging the plan in is what lets a hop say ``ran: false``. A step row is
    only written when its turn starts, so a two-agent workflow that stopped
    after its researcher has exactly one row - and a view built from rows alone
    would render that as a finished one-agent task. The difference between "the
    writer found nothing" and "the writer never ran" is the whole reason to
    render a chain rather than a list.
    """
    task = await _service(db).get_row(
        namespace_key=principal.namespace_key, task_key=task_key
    )
    chain = await AgentWorkflowsService(db).chain_for_task(
        namespace_key=principal.namespace_key, task=task
    )
    return GetAgentTaskChainResponse(chain=chain)


@router.post(
    "/{task_key}/claim",
    response_model=ClaimAgentTaskResponse,
    summary="Claim a task for one dispatcher",
    response_description="The claimed task, where to resume, and the lease",
)
async def claim_agent_task(
    request: ClaimAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_CLAIM)),
) -> ClaimAgentTaskResponse:
    """One statement decides this, and a 409 means somebody else won.

    A refusal is not a reason to retry: the queue poll will hand out a
    different task next time round, and hammering a row another dispatcher
    holds produces a wall of conflict logs and no extra throughput.

    A claim that reclaims a task from an expired lease marks every step still
    ``running`` as ``abandoned`` and reports their indexes. Resume position
    comes back as ``resume_step_index``, read from the completed steps rather
    than from any counter, and ``prior_status`` says what re-running that index
    costs: nothing for a ``queued`` task, nothing for one that stopped on
    quota, and a possible repeat of whatever the abandoned step already did for
    one that was mid-turn.
    """
    service = _service(db)
    response = await service.claim(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        instance_id=request.instance_id,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    return response


@router.post(
    "/{task_key}/heartbeat",
    response_model=HeartbeatAgentTaskResponse,
    summary="Refresh the lease on a claimed task",
    response_description="The refreshed lease and the deadline it sits under",
)
async def heartbeat_agent_task(
    request: HeartbeatAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_CLAIM)),
) -> HeartbeatAgentTaskResponse:
    """Sent between steps, and during a quota backoff, which is between steps.

    Fenced on the instance that holds the claim. A heartbeat from a dispatcher
    whose lease already expired and whose task was taken by another one is
    refused with 409 rather than honoured, because honouring it would extend
    the *successor's* lease and leave two processes believing they held one
    task.
    """
    service = _service(db)
    response = await service.heartbeat(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        instance_id=request.instance_id,
    )
    await db.commit()
    return response


@router.post(
    "/{task_key}/steps",
    response_model=AgentTaskStepResponse,
    summary="Open a step row before its turn starts",
    response_description="The step as recorded, and the task it belongs to",
)
async def start_agent_task_step(
    request: StartAgentTaskStepRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_CLAIM)),
) -> AgentTaskStepResponse:
    """Called before ``POST /turns``, never after.

    A step row that only exists once its turn succeeded cannot record the case
    this table was added for: a hop that reached the executor, spent money,
    possibly acted through a tool, and never came back. Opening the row first
    is what makes a dispatcher's death visible instead of invisible.

    Refused past the task's deadline. That ceiling is checked here rather than
    in the dispatcher precisely because a hung dispatcher is the thing it
    bounds.

    **And this is where the tracker's own files are fetched**, in three parts
    around one commit, which is the reason the route reads the way it does.
    The row is opened and committed first, so the connection goes back to the
    pool; the fetch then runs with no session in hand at all, under a single
    wall-clock budget across every file on the step; a second short write
    stores what arrived. Twenty-five seconds of network wait inside the first
    transaction would hold a pooled connection for the whole of it, against a
    pool of five with ten overflow.

    ``files`` on the response is what lets the dispatcher's envelope say "2 of
    3 files were delivered". ``None`` means no fetch ran - the deployment has
    the source off, or this task did not come from one - which is a different
    answer from a fetch that found nothing.
    """
    service = _service(db)
    step, task = await service.start_step(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        instance_id=request.instance_id,
        step_index=request.step_index,
        agent_name=request.agent_name,
        brief=request.brief,
        session_key=request.session_key,
    )
    plan = await plan_step_files(
        db,
        namespace_key=principal.namespace_key,
        task=task,
        step_index=request.step_index,
        session_key=request.session_key,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()
    if plan is None:
        return AgentTaskStepResponse(step=step, task=task)

    try:
        files = await fetch_step_files(plan)
        stored = await store_step_files(db, plan=plan, files=files)
        await db.commit()
        # After the commit, deliberately. The bytes have to be visible to the
        # background converter's own session before there is anything to wait
        # for, and this wait holds no connection of its own.
        summary = await settle_step_conversions(plan=plan, stored=stored)
        await record_step_summary(db, plan=plan, summary=summary)
        await db.commit()
    except Exception:
        # The step row is already committed. Answering 500 here would tell the
        # dispatcher the step never opened, so it would close out the task and
        # leave a row running until a reclaim swept it - a worse outcome than
        # a step that ran with no files. Logged as an exception because unlike
        # every refusal above, reaching this is a defect in this server.
        await db.rollback()
        logger.exception("Attaching this step's tracker files failed.")
        # Not ``files=None``, which renders nothing at all, and not a zero
        # count, which asserts the issue carries no files. This server tried to
        # list them and could not, and an agent that is told there is nothing
        # to read will confidently answer from the title - the exact failure
        # this route exists to remove.
        return AgentTaskStepResponse(
            step=step,
            task=task,
            files=StepFilesSummary(found=0, delivered=0, files=[], read_failed=True),
        )
    return AgentTaskStepResponse(step=step, task=task, files=summary)


@router.post(
    "/{task_key}/steps/{step_index}/finish",
    response_model=AgentTaskStepResponse,
    summary="Close a step out and move the task in one transaction",
    response_description="The finished step, and the task after it",
)
async def finish_agent_task_step(
    request: FinishAgentTaskStepRequest,
    http_request: Request,
    task_key: str = TASK_KEY_PATH,
    step_index: int = Path(..., ge=0),
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_CLAIM)),
) -> AgentTaskStepResponse:
    """The write order lives here so nobody has to remember it.

    Step row first, task counters second, one transaction. A crash between them
    leaves a completed step and a stale ``current_step``, which the resume rule
    reads past exactly. A crash in the other order loses the agent's output
    permanently, and that output is the durable record: the session is deleted
    when the task ends, so there is no transcript to go back to.

    ``abandoned`` is not accepted here. It is what the server writes when it
    reclaims a step from an expired lease, and a dispatcher claiming it for
    itself would be reporting somebody else's failure as its own.

    **A completed step also queues its Linear comment here**, in the same
    transaction as the step, so the queue entry is durable before any network
    is tried. The send happens after the commit with no transaction open,
    exactly like the file fetch on the start route, and a Linear failure marks
    the row and never fails the step: "the work is done" and "the ticket was
    updated" are two facts, kept separately.
    """
    service = _service(db)
    step, task = await service.finish_step(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        instance_id=request.instance_id,
        step_index=step_index,
        status=request.status,
        output_text=request.output_text,
        output_truncated=request.output_truncated,
        session_key=request.session_key,
        turn_trace_id=request.turn_trace_id,
        failure_code=request.failure_code,
        failure_detail=request.failure_detail,
    )
    writebacks = _queue_service(db, runtime)
    queued = None
    task_row = None
    step_row = None
    # Re-widened before comparing: the shared base model serializes enums to
    # their values, so ``step.status`` holds the plain string here.
    if AgentTaskStepStatus(step.status) is AgentTaskStepStatus.COMPLETED:
        task_row = await service.get_row(
            namespace_key=principal.namespace_key, task_key=task_key
        )
        if writebacks.writeback_applies(task_row):
            step_row = await db.scalar(
                select(AgentTaskStepRow).where(
                    AgentTaskStepRow.task_id == task_row.id,
                    AgentTaskStepRow.step_index == step_index,
                )
            )
            plan = await AgentWorkflowsService(db).plan_for_task(
                namespace_key=principal.namespace_key, task=task_row
            )
            if step_row is not None:
                queued = await writebacks.enqueue_step_comment(
                    task=task_row,
                    step=step_row,
                    total_steps=len(plan.steps),
                )
    await db.commit()

    if queued is not None and task_row is not None and step_row is not None:
        try:
            await writebacks.deliver_comment(
                row=queued,
                task=task_row,
                agent_name=step_row.agent_name,
                emit_events=_event_emitter(http_request, principal.namespace_key),
            )
            await db.commit()
        except Exception:
            # The step is already committed. A write-back failure is a row in
            # the queue, never a failed step; reaching this is a defect here.
            await db.rollback()
            logger.exception("Sending this step's Linear comment failed.")
    return AgentTaskStepResponse(step=step, task=task)


@router.post(
    "/{task_key}/finish",
    response_model=AgentTaskResponse,
    summary="Record how a claimed task ended",
    response_description="The task after the transition",
)
async def finish_agent_task(
    request: FinishAgentTaskRequest,
    http_request: Request,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_CLAIM)),
) -> AgentTaskResponse:
    """``blocked`` and ``failed`` are not synonyms, and the ledger keeps them apart.

    ``failed`` means the work was attempted and did not work. ``blocked`` means
    it was never attempted because the configuration is wrong - no enabled
    binding, no agent resolvable, a content-access refusal - and retrying it on
    a timer produces the same result forever, so a dispatcher never does.

    ``paused_quota`` and ``running_unknown`` are endings for the dispatcher and
    not for the ledger. Both keep the task's slot. The first is reclaimable and
    resumes at the same step, which is provably safe because the quota check
    runs before anything leaves the process. The second is not reclaimable by
    any machine, because nothing here can prove the invocation stopped.

    **A task finishing as ``completed`` additionally leaves a proposal row in
    the review queue**, in this same transaction. The task's status is and
    stays ``completed``: the proposal waiting is a fact about the write-back,
    never about the task, so a Linear outage or an unread queue cannot make
    finished work look unfinished.

    **Undelivered step comments get one more attempt here**, after the commit,
    whatever status the task finished with. The finish-step send is otherwise
    the only attempt a comment row ever gets, because re-finishing a completed
    step is a 409: without this pass, a row left ``failed`` by a Linear blip
    would be stranded for good. The marker dedupe makes the retry safe, and a
    failure here still only marks rows, exactly like the first attempt.
    """
    service = _service(db)
    task = await service.finish_task(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        instance_id=request.instance_id,
        status=request.status,
        failure_code=request.failure_code,
        failure_detail=request.failure_detail,
    )
    task_row = await service.get_row(
        namespace_key=principal.namespace_key, task_key=task_key
    )
    writebacks = _queue_service(db, runtime)
    if AgentTaskStatus(task.status) is AgentTaskStatus.COMPLETED:
        await writebacks.create_status_change_proposal(task=task_row)
    await db.commit()

    try:
        await writebacks.deliver_pending_comments(
            task=task_row,
            emit_events=_event_emitter(http_request, principal.namespace_key),
        )
        await db.commit()
    except Exception:
        # The finish is already committed; a redelivery defect costs the
        # retry and nothing else.
        await db.rollback()
        logger.exception("Redelivering this task's Linear comments failed.")
    return AgentTaskResponse(task=task)


@router.post(
    "/{task_key}/cancel",
    response_model=AgentTaskResponse,
    summary="Take a queued task off the list",
    response_description="The cancelled task",
)
async def cancel_agent_task(
    request: CancelAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_WRITE)),
) -> AgentTaskResponse:
    """Only from ``queued``, and that restriction is honesty rather than caution.

    Cancelling a running task would tell an operator that work had stopped
    while the turn carries on spending. Stopping a turn is a halt: a different
    button, a different mechanism, and one that reaches the executor.
    """
    service = _service(db)
    task = await service.cancel(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        reason=request.reason,
    )
    await db.commit()
    return AgentTaskResponse(task=task)


@router.post(
    "/{task_key}/resolve",
    response_model=AgentTaskResponse,
    summary="Clear a task whose turn timed out with no proof it stopped",
    response_description="The task after a human decided",
)
async def resolve_agent_task(
    request: ResolveAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_WRITE)),
) -> AgentTaskResponse:
    """The one transition no dispatcher can make, and that is the point.

    A 504 says this server stopped waiting. It does not say the invocation
    stopped: whether it did depends on the topology between the control plane
    and the executor and on the executor kind, and neither is knowable from
    here. So the task holds its slot until a person has read the transcript and
    decided whether the work happened.

    ``requeue`` puts it back on the queue and anything else records it failed.
    Nothing expires into either. A queue that times out into a decision is not
    a queue.
    """
    service = _service(db)
    task = await service.resolve(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        requeue=request.requeue,
        reason=request.reason,
    )
    await db.commit()
    return AgentTaskResponse(task=task)


@router.post(
    "/{task_key}/accept",
    response_model=AcceptAgentTaskResponse,
    summary="Accept a completed task's proposal and close its issue",
    response_description="The task, the sent write-back, and the milestone's new progress",
)
async def accept_agent_task(
    request: AcceptAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_APPROVE)),
) -> AcceptAgentTaskResponse:
    """A human agreeing that an agent's claim may change the tracker.

    This is the only route in the server that closes an issue, and a person is
    the only thing that can reach it. Server-side it refuses, in order: a task
    that is not ``completed`` or a row that is not waiting; a dry-run task; the
    credential that ran the work (409 ``SELF_APPROVAL_REFUSED`` - "may run
    agents, may not accept their work" is this comparison, because the local
    credential path cannot express it as a tier); an issue that changed team or
    left its milestone (409 ``SCOPE_CHANGED``); and a digest that no longer
    matches the text, the target and the resolved completed state together
    (409 ``DECISION_CHANGED`` - re-read the review queue for the current one).

    The target state is resolved from the team's workflow server-side. Nothing
    in this request can name a state, and nothing in the agent's output is
    read to find one.

    The response carries the milestone's new progress read after the close,
    because the milestone cache is per process: the invalidation this route
    performs clears the serving replica, and any other replica corrects within
    one TTL. ``note`` says ``ALREADY_COMPLETED`` when a human closed the issue
    first, which is the system working rather than an error.
    """
    service = _review_service(db, runtime)
    task_row, row, note, team_key = await service.accept(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        writeback_id=request.writeback_id,
        expected_decision_digest=request.expected_decision_digest,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    await db.commit()

    progress: float | None = None
    if team_key is not None:
        milestones = get_milestone_service()
        milestones.invalidate(
            namespace_key=principal.namespace_key, linear_team_key=team_key
        )
        get_milestone_issues_service().invalidate(
            namespace_key=principal.namespace_key, linear_team_key=team_key
        )
        if task_row.source_scope_ref is not None:
            result = await milestones.get_milestones(
                namespace_key=principal.namespace_key, linear_team_key=team_key
            )
            for milestone in result.milestones:
                if milestone.id == task_row.source_scope_ref:
                    progress = milestone.progress
                    break

    detail = await _service(db).get_detail(
        namespace_key=principal.namespace_key, task_key=task_key
    )
    return AcceptAgentTaskResponse(
        task=detail,
        writeback=service.wire(row, task_key=task_key),
        note=note,
        milestone_progress=progress,
    )


@router.post(
    "/{task_key}/reject",
    response_model=RejectAgentTaskResponse,
    summary="Decline a completed task's proposal",
    response_description="The task and the rejected write-back",
)
async def reject_agent_task(
    request: RejectAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_APPROVE)),
) -> RejectAgentTaskResponse:
    """Records a reason; the task stays ``completed`` and the issue stays open.

    Rejection writes nothing to Linear, so it works with the write flag off.
    The self-approval refusal applies here too, because a dispatcher that can
    reject its own proposal can bury its own output before anybody reads it.
    """
    service = _review_service(db, runtime)
    task_row, row = await service.reject(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        writeback_id=request.writeback_id,
        reason=request.reason,
        caller_hash=hash_caller_id(principal.caller_id),
    )
    del task_row
    await db.commit()
    detail = await _service(db).get_detail(
        namespace_key=principal.namespace_key, task_key=task_key
    )
    return RejectAgentTaskResponse(
        task=detail,
        writeback=service.wire(row, task_key=task_key),
    )


@router.post(
    "/{task_key}/writebacks/{writeback_id}/deliver",
    response_model=DeliverAgentTaskWritebackResponse,
    summary="Attempt one undelivered comment again",
    response_description="The row after the attempt: sent, failed, or denied",
)
async def deliver_agent_task_writeback(
    http_request: Request,
    task_key: str = TASK_KEY_PATH,
    writeback_id: int = Path(..., ge=1),
    db: AsyncSession = Depends(get_async_db),
    runtime: WritebackRuntime = Depends(get_writeback_runtime),
    principal: Principal = Depends(require_operation(Operation.AGENT_TASKS_WRITE)),
) -> DeliverAgentTaskWritebackResponse:
    """An operator's retry for the long tail the automatic attempts missed.

    The finish routes already retry: this exists for rows those can no longer
    reach - a task that finished while the write flag was off, or while Linear
    was down past the last attempt. The row's own queue entry is the evidence
    something is undelivered, and the task detail shows it.

    **Comments only, and that refusal is the review gate holding.** A
    ``status_change`` row is refused whatever its state, because a deliver
    route that could send one would be an accept with no digest, no
    self-approval check and no named approver. ``denied`` rows are refused
    too: the same body reproduces the same refusal, and the controls verdict
    is not retried around. The body itself is re-evaluated against the
    step agent's controls before every attempt, exactly like the first one.
    """
    writebacks = _queue_service(db, runtime)
    task_row, row, agent_name = await writebacks.require_comment_row(
        namespace_key=principal.namespace_key,
        task_key=task_key,
        writeback_id=writeback_id,
    )
    row = await writebacks.deliver_comment(
        row=row,
        task=task_row,
        agent_name=agent_name,
        emit_events=_event_emitter(http_request, principal.namespace_key),
    )
    # ``updated_at`` carries an ``onupdate`` and the flush expired it; read it
    # back so serializing after the commit stays synchronous.
    await db.refresh(row)
    await db.commit()
    return DeliverAgentTaskWritebackResponse(
        writeback=wire_writeback(row, task_key=task_key)
    )
