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

``agent_tasks.approve`` is declared and has no route here. It belongs to the
accept path, where a human agrees that an agent's claim to have finished may
change a tracker their team plans against, and it is separate precisely so
nobody folds it into ``write`` later.

What no route accepts, at any tier: an agent name on an import, a trace id, a
lease length, or a deadline. Agent selection is server-side configuration, the
chain trace is minted by the server at claim time, and the two ceilings are
read from deployment settings. A caller that could set its own lease could
reclaim live work; a caller that could set its own deadline would have no
deadline.
"""

from __future__ import annotations

from agent_control_models.errors import ErrorCode
from agent_control_models.server import PaginationInfo
from agent_control_models.tasks import (
    AgentTaskResponse,
    AgentTaskStatus,
    AgentTaskStepResponse,
    CancelAgentTaskRequest,
    ClaimAgentTaskRequest,
    ClaimAgentTaskResponse,
    FinishAgentTaskRequest,
    FinishAgentTaskStepRequest,
    GetAgentTaskResponse,
    HeartbeatAgentTaskRequest,
    HeartbeatAgentTaskResponse,
    ImportAgentTasksRequest,
    ImportAgentTasksResponse,
    ListAgentTasksResponse,
    ResolveAgentTaskRequest,
    StartAgentTaskStepRequest,
)
from agent_control_models.teams import TEAM_SLUG_MAX_LENGTH
from agent_control_models.workflows import (
    GetAgentTaskChainResponse,
    GetAgentTaskPlanResponse,
)
from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import dispatch_settings
from ..db import get_async_db
from ..errors import BadRequestError
from ..services.agent_tasks import AgentTasksService
from ..services.agent_workflows import AgentWorkflowsService
from ..services.caller_identity import hash_caller_id

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
    await db.commit()
    return AgentTaskStepResponse(step=step, task=task)


@router.post(
    "/{task_key}/steps/{step_index}/finish",
    response_model=AgentTaskStepResponse,
    summary="Close a step out and move the task in one transaction",
    response_description="The finished step, and the task after it",
)
async def finish_agent_task_step(
    request: FinishAgentTaskStepRequest,
    task_key: str = TASK_KEY_PATH,
    step_index: int = Path(..., ge=0),
    db: AsyncSession = Depends(get_async_db),
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
    await db.commit()
    return AgentTaskStepResponse(step=step, task=task)


@router.post(
    "/{task_key}/finish",
    response_model=AgentTaskResponse,
    summary="Record how a claimed task ended",
    response_description="The task after the transition",
)
async def finish_agent_task(
    request: FinishAgentTaskRequest,
    task_key: str = TASK_KEY_PATH,
    db: AsyncSession = Depends(get_async_db),
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
    await db.commit()
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
