"""HTTP endpoints for chat sessions with an agent.

Open a session, list them, read one, retitle or archive it, delete it, read its
transcript back from the executor, and run one blocking turn against it.

Two things about the shape of this module are worth stating rather than
inferring.

Routes that talk to an executor do not depend on ``get_async_db``. That
dependency yields one connection for the whole request, and the pool is five
plus ten overflow, so a handler that holds one across a call to another service
can starve policy evaluation for every unrelated agent in the process. Those
handlers delegate to ``services.agent_sessions``, which opens short-lived
sessions around each database step instead. Routes that only touch the database
use the dependency like everything else in this codebase.

Metadata and content are read through different operations.
``agent_sessions.read`` covers titles, timestamps and status - the same class of
read as observability. ``agent_sessions.content_read`` covers the transcript,
which carries raw human prompts, model output, and tool results that in this
repo can hold third-party data fetched with a server-held key. Splitting them
costs one enum member and cannot be retrofitted without a wire change.
"""

from __future__ import annotations

from agent_control_models.errors import ErrorCode
from agent_control_models.sessions import (
    MESSAGE_PAGE_DEFAULT_LIMIT,
    MESSAGE_PAGE_MAX_LIMIT,
    AgentSessionStatus,
    CreateAgentSessionRequest,
    CreateAgentSessionResponse,
    DeleteAgentSessionResponse,
    ExecutorHealthResponse,
    GetAgentSessionResponse,
    ListAgentSessionsResponse,
    ListSessionMessagesResponse,
    PatchAgentSessionRequest,
    PatchAgentSessionResponse,
    StartTurnRequest,
    TurnResponse,
)
from agent_control_models.teams import TEAM_SLUG_MAX_LENGTH
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework import Operation, Principal, require_operation
from ..config import executor_settings
from ..db import get_async_db
from ..errors import BadRequestError
from ..services.agent_names import normalize_agent_name_or_422
from ..services.agent_sessions import (
    AgentSessionsService,
    delete_session,
    open_session,
    probe_executor_health,
    read_transcript,
)
from ..services.agent_turns import run_turn
from ..services.caller_identity import hash_caller_id
from ..services.executor_factory import (
    HttpExecutorClientFactory,
    get_executor_client_factory,
)

router = APIRouter(prefix="/agent-sessions", tags=["agent-sessions"])

_DEFAULT_LIST_LIMIT = 20
_MAX_LIST_LIMIT = 100


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
    "",
    response_model=CreateAgentSessionResponse,
    summary="Open a chat session with an agent",
    response_description="The created session",
)
async def create_agent_session(
    request: CreateAgentSessionRequest,
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_WRITE)),
    factory: HttpExecutorClientFactory = Depends(get_executor_client_factory),
) -> CreateAgentSessionResponse:
    """Create a conversation on the agent's executor and the row that maps to it.

    Refusals come in order of specificity, and each one says something
    different. An agent that is not registered is a 404. A registered agent with
    no enabled executor binding is a 409, before the executor is contacted at
    all. An executor that cannot be reached is a 503 with a written message,
    never a 500 and never anything the executor itself said. A namespace already
    at its session ceiling is a 429.

    The identifiers this session is addressed by are minted here. A client
    cannot supply or influence any of them: the request model forbids unknown
    fields and has no executor field to begin with, because a caller who could
    choose executor coordinates could point a row in their own namespace at
    somebody else's conversation.

    Creating this session also mints a short-lived token bound to it and seeds
    it into the executor's session state, which is how the agent will later be
    able to write progress back for this session and no other. The token is not
    in this response and is not readable through any endpoint.
    """
    session = await open_session(
        namespace_key=principal.namespace_key,
        created_by_hash=hash_caller_id(principal.caller_id),
        agent_name=request.agent_name,
        title=request.title,
        team_slug=request.team_slug,
        factory=factory,
        settings=executor_settings,
        task_key=request.task_key,
    )
    return CreateAgentSessionResponse(session=session)


@router.get(
    "",
    response_model=ListAgentSessionsResponse,
    summary="List chat sessions",
    response_description="Sessions in the request namespace",
)
async def list_agent_sessions(
    agent: str | None = Query(None, description="Optional agent name filter."),
    team: str | None = Query(
        None,
        min_length=1,
        max_length=TEAM_SLUG_MAX_LENGTH,
        description=(
            "Optional team slug. Matches the team a session was opened under, "
            "not the teams its agent belongs to now. A slug with no team in "
            "this namespace yields an empty page rather than an error."
        ),
    ),
    status: AgentSessionStatus | None = Query(
        None, description="Optional lifecycle filter."
    ),
    cursor: str | None = Query(
        None,
        description=(
            "Opaque cursor returned as ``next_cursor`` on the previous page. "
            "Pass it back unchanged to fetch the next page."
        ),
    ),
    limit: int = Query(
        _DEFAULT_LIST_LIMIT,
        ge=1,
        le=_MAX_LIST_LIMIT,
        description="Maximum sessions to return (default 20, max 100).",
    ),
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_READ)),
) -> ListAgentSessionsResponse:
    """Return sessions in this namespace, newest first, with cursor pagination.

    Metadata only. No message ever appears in this response, whatever the
    filters say: reading a conversation is a different operation.
    """
    agent_name = normalize_agent_name_or_422(agent, field_name="agent") if agent else None
    service = AgentSessionsService(db)
    return await service.list_sessions(
        namespace_key=principal.namespace_key,
        agent_name=agent_name,
        team_slug=team,
        status=status,
        cursor=_parse_cursor(cursor),
        limit=limit,
    )


@router.get(
    "/executor-health",
    response_model=ExecutorHealthResponse,
    summary="Check the executors behind this namespace's agents",
    response_description="Per-binding reachability",
)
async def get_executor_health(
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_READ)),
    factory: HttpExecutorClientFactory = Depends(get_executor_client_factory),
) -> ExecutorHealthResponse:
    """Probe every enabled executor binding in this namespace.

    ``/health`` reports on this process and deliberately checks nothing else,
    not even the database. Chat adds a hard dependency on a second process that
    ships no health signal of its own, so this route is that signal: without it,
    the first symptom of an executor outage is a person hitting send.

    Declared before the ``/{session_key}`` route so the literal path wins. A
    session key is 32 hex characters and could never collide with it, but route
    order is not something to leave to the shape of an identifier.
    """
    return await probe_executor_health(
        namespace_key=principal.namespace_key,
        factory=factory,
        settings=executor_settings,
    )


@router.get(
    "/{session_key}",
    response_model=GetAgentSessionResponse,
    summary="Get one chat session",
    response_description="The requested session",
)
async def get_agent_session(
    session_key: str,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_READ)),
) -> GetAgentSessionResponse:
    """Read one session's metadata.

    A key that exists in another namespace is a 404 here, exactly as an unknown
    key is. The two are indistinguishable to the caller on purpose.
    """
    service = AgentSessionsService(db)
    row = await service.get_row_or_404(
        namespace_key=principal.namespace_key, session_key=session_key
    )
    return GetAgentSessionResponse(session=await service.to_detail(row))


@router.patch(
    "/{session_key}",
    response_model=PatchAgentSessionResponse,
    summary="Retitle, re-team or archive a session",
    response_description="The updated session",
)
async def patch_agent_session(
    session_key: str,
    request: PatchAgentSessionRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_WRITE)),
) -> PatchAgentSessionResponse:
    """Update the mutable fields of a session.

    Omitted fields are left alone, so an explicit ``"title": null`` is the only
    way to clear a title, and the same holds for ``team_slug``. Only ``active``
    and ``archived`` may be set: the orphaned statuses describe the executor
    disagreeing with this server, which is an observation to be made rather than
    a claim to be accepted.
    """
    service = AgentSessionsService(db)
    row = await service.get_row_or_404(
        namespace_key=principal.namespace_key, session_key=session_key
    )
    update_team = "team_slug" in request.model_fields_set
    team_id = (
        await service.resolve_team_id(
            namespace_key=principal.namespace_key, slug=request.team_slug
        )
        if request.team_slug is not None
        else None
    )
    row = await service.update_session(
        row=row,
        title=request.title,
        update_title="title" in request.model_fields_set,
        team_id=team_id,
        update_team=update_team,
        status=request.status,
    )
    detail = await service.to_detail(row)
    await db.commit()
    return PatchAgentSessionResponse(session=detail)


@router.delete(
    "/{session_key}",
    response_model=DeleteAgentSessionResponse,
    summary="Delete a chat session",
    response_description="Deletion confirmation",
)
async def delete_agent_session(
    session_key: str,
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_WRITE)),
    factory: HttpExecutorClientFactory = Depends(get_executor_client_factory),
) -> DeleteAgentSessionResponse:
    """Delete both halves of a session: the executor's copy and this one.

    A hard delete, and executor-side first, because the local row is the only
    record of where the conversation lives. When the executor delete fails the
    row is kept and moved to ``orphaned_pending_delete``, and this returns an
    error rather than a success, so repeating the call finishes the job. A
    success here means both sides are gone.
    """
    await delete_session(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        factory=factory,
        settings=executor_settings,
    )
    return DeleteAgentSessionResponse(deleted=True)


@router.get(
    "/{session_key}/messages",
    response_model=ListSessionMessagesResponse,
    summary="Read a session's transcript",
    response_description="One page of messages",
)
async def list_session_messages(
    session_key: str,
    after_index: int | None = Query(
        None,
        ge=0,
        description=(
            "Return messages after this index. Pass back the ``next_index`` "
            "from the previous page."
        ),
    ),
    limit: int = Query(
        MESSAGE_PAGE_DEFAULT_LIMIT,
        ge=1,
        le=MESSAGE_PAGE_MAX_LIMIT,
        description="Maximum messages to return.",
    ),
    principal: Principal = Depends(
        require_operation(Operation.AGENT_SESSION_CONTENT_READ)
    ),
    factory: HttpExecutorClientFactory = Depends(get_executor_client_factory),
) -> ListSessionMessagesResponse:
    """Read the conversation back from the executor that holds it.

    A session whose executor-side conversation has disappeared answers 200 with
    an empty transcript and a notice, not an error. The row is marked orphaned
    on the way through so the next read does not have to ask again. An error
    page in place of a chat panel tells a person less than an empty panel that
    explains itself.

    Transcripts are scoped to the caller who opened the session, admins
    excepted. Be clear-eyed about how much that buys under the default
    credential provider: every browser caller resolves to the same identity, so
    this separates API keys from each other and from the console, and separates
    nothing between two people using the console. Real per-user isolation needs
    a provider that resolves callers, which is stated in ``.env.example`` too.
    """
    return await read_transcript(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        after_index=after_index,
        limit=limit,
        factory=factory,
        settings=executor_settings,
    )


@router.post(
    "/{session_key}/turns",
    response_model=TurnResponse,
    summary="Say something to the agent and wait for its answer",
    response_description="What the turn produced",
)
async def start_turn(
    session_key: str,
    request: StartTurnRequest,
    principal: Principal = Depends(require_operation(Operation.AGENT_SESSIONS_RUN)),
    factory: HttpExecutorClientFactory = Depends(get_executor_client_factory),
) -> TurnResponse:
    """Run one turn to completion and answer with the messages it produced.

    This is the first endpoint in this product that spends money every time it
    is called, which shapes almost everything about it.

    It runs under ``agent_sessions.run`` rather than ``agent_sessions.write``,
    split for exactly that reason: opening a chat and paying for a model call
    are different privileges, and separating them later would be a wire-contract
    change rather than an enum line. It is also scoped to the caller who opened
    the session, admins excepted, on the same predicate transcript reads use -
    the response carries model output, and the request appends to somebody
    else's conversation. And it is bounded by a per-credential rate limit, since
    the one-turn-per-session guard does nothing against a caller who opens more
    sessions.

    The failures are worth reading before writing a client:

    * **409** - a turn is already in flight on this session. Sessions answer one
      at a time. Also: a named attachment is not ``ready``, in which case
      **nothing was sent** - a turn that ran without the file somebody attached
      on purpose is the half-done job this refusal exists to prevent.
    * **413** - the named files are larger together than one turn carries, or
      the message leaves no room for them.
    * **429** - this credential has started too many turns this minute.
    * **502** - the executor answered and refused, which includes its model
      credentials being missing, rejected or out of quota. Retrying will not
      help.
    * **503** - the executor could not be reached, or the feature is switched
      off on this server.
    * **504** - the turn outlived this server's patience. Read this one
      carefully: **the invocation did not stop.** The agent is still running and
      still spending, its work will appear in the next transcript read, and the
      session accepts another turn immediately even though the previous one has
      not finished.

    A turn the guardrails blocked is **not** in that list. The plugin
    substitutes a blocked response, the executor completes the turn normally,
    and the block arrives here as ordinary model output in ``messages``. A
    refused request and a refused *model call* are different events and only one
    of them is an error.
    """
    return await run_turn(
        namespace_key=principal.namespace_key,
        session_key=session_key,
        caller_hash=hash_caller_id(principal.caller_id),
        is_admin=principal.is_admin,
        message=request.message,
        factory=factory,
        settings=executor_settings,
        attachment_keys=list(request.attachment_keys),
    )
