"""Chat sessions: the namespace-scoped mapping onto an executor conversation.

Two rules shape everything below.

**No database session is held across an executor call.** ``get_async_db`` yields
one connection for the whole request, and the pool is five plus ten overflow. An
executor call can take the full configured timeout, so a handler that holds a
connection across one is a handler that can starve policy evaluation for every
unrelated agent in the process. The orchestration functions here therefore open
short-lived sessions of their own around each database step, close them, and
only then talk to the executor. The plan states this rule for the turn routes;
it is applied to every route that leaves the process, because the reason for it
is the connection hold, not the endpoint.

**Two writes across two systems can half-succeed, and the losing half must be
cleaned up or named.** Creating an executor session and then failing to write
the local row leaves a conversation nothing can address, so the executor session
is deleted in compensation. Deleting a local row and then failing to delete the
executor session would do the same in reverse, so the executor side goes first,
and a failure there parks the row in ``orphaned_pending_delete`` rather than
reporting a success that did not happen.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from agent_control_models.agent_runtimes import ExecutorKind
from agent_control_models.errors import ErrorCode, ErrorReason
from agent_control_models.server import PaginationInfo
from agent_control_models.sessions import (
    AgentSessionDetail,
    AgentSessionStatus,
    AgentSessionSummary,
    ExecutorHealthEntry,
    ExecutorHealthResponse,
    ListAgentSessionsResponse,
    ListSessionMessagesResponse,
    SessionMessage,
    SessionMessagePart,
    SessionMessagePartKind,
    SessionMessageRole,
)
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth_framework.config import runtime_auth_config
from ..auth_framework.core import Operation
from ..auth_framework.runtime_token import RuntimeTokenError, mint_runtime_token
from ..config import ExecutorSettings, dispatch_settings
from ..db import AsyncSessionLocal
from ..errors import (
    APIError,
    DatabaseError,
    ForbiddenError,
    NotFoundError,
    executor_api_error,
)
from ..models import AgentSession, Team
from .agent_dispatch_state import (
    require_dispatch_not_paused,
    require_executors_not_halted,
)
from .agent_runtimes import AgentRuntimesService
from .agent_tasks import AgentTasksService
from .executor_client import (
    EXECUTOR_DISABLED_MESSAGE,
    ExecutorClient,
    ExecutorClientFactory,
    ExecutorError,
    ExecutorMessage,
    ExecutorSessionNotFoundError,
)
from .executor_metrics import (
    EXECUTOR_PROBE_REACHABLE,
    EXECUTOR_PROBE_UNREACHABLE,
    EXECUTOR_PROBES,
    SESSIONS_STUCK_IN_FLIGHT,
)

_logger = logging.getLogger(__name__)

SESSION_STATE_KEY = "agent_control"
"""Key the seeded state hangs under in the executor's own session state.

One namespaced key rather than several loose ones, so an agent's own state
cannot collide with it and a reader can tell at a glance which half of the
state belongs to the control plane.
"""

RUNTIME_TOKEN_TARGET_TYPE = "agent_session"
"""Target kind the session-bound runtime token is minted against.

The token *is* the session identity. A token minted for session A cannot claim
session B's nudges or rewrite its plan, because the verifier compares the
token's target against the request's, and the request carries the session key
in its path.
"""

SESSION_TOKEN_SCOPES: tuple[str, ...] = (
    Operation.AGENT_NUDGES_CONSUME.value,
    Operation.AGENT_PLANS_WRITE.value,
    Operation.AGENT_TRACKER_COMMENT.value,
    Operation.COMPANY_KNOWLEDGE_SEARCH.value,
    Operation.AGENT_ATTACHMENTS_WRITE.value,
)
"""What the executor may do with its session token: drain nudges written for
this session, report progress on this session, comment on the tracker issue
this session's task came from, read the company-knowledge mirror, and store a
file it produced against this session. Notably not ``runtime.use``, so this
token cannot be used for control resolution, and not anything that reads
another session.

The tracker entry is the only one that leaves this system, and it is the
narrowest: the issue is resolved from the session's own task, so an agent
cannot name a target, and it posts a comment rather than closing anything.
Closing stays ``agent_tasks.approve``, which no token carries.

Knowledge search is a read against a database this token's holder cannot write
and the control plane itself only reads. It is here rather than on an API key
because the per-session search ceiling has to be keyed on something a caller
cannot pick, and because a long-lived key handed to every agent process would
make one agent's runaway loop spend every other agent's allowance. The upload
scope is on the token for the same reasons."""

_HEALTH_PROBE_LIMIT = 25
"""Ceiling on executors probed by one health call. A namespace with more agents
than this gets a partial answer rather than a request that fans out without
bound."""

SESSION_CEILING_RETRY_SECONDS = 60
"""How long the session-ceiling 429 asks a caller to wait.

A hint rather than a promise, and the difference matters: nothing expires a
session, so this ceiling clears only when somebody deletes one. Sent anyway,
because the alternative is a caller inventing its own interval - and a fleet
whose whole retry behaviour is decided in the process being limited is the
failure this phase exists to remove."""


def _status_value(status: AgentSessionStatus | str) -> str:
    """Return the stored form of a status, from either an enum or its value.

    Both forms genuinely arrive. Shared request models are configured with
    ``use_enum_values=True``, so a field annotated ``AgentSessionStatus`` holds
    the plain string after validation, while a query parameter of the same
    annotation stays an enum and server-side code passes members directly.
    Normalizing in one place beats being surprised by it at each call site -
    which is what ``.value`` on an already-plain string does, loudly and at
    runtime.
    """
    return AgentSessionStatus(status).value


@dataclass(frozen=True)
class _ExecutorCoordinates:
    """Everything needed to talk to one session's executor.

    Read out of the database inside a short-lived session and carried as a
    plain value, so the executor call that follows holds no connection.
    """

    executor_kind: str
    base_url: str
    app_name: str
    user_id: str
    session_id: str


# =============================================================================
# Persistence
# =============================================================================


class AgentSessionsService:
    """Reads and writes ``agent_sessions``. Never calls an executor.

    The response models it returns carry no executor coordinates. That is not
    an oversight to be corrected later: ``session_key`` is the only session
    identifier a client is given, and the executor triple is the one thing that
    would let a caller in one namespace address another namespace's
    conversation.
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_row_or_404(
        self, *, namespace_key: str, session_key: str
    ) -> AgentSession:
        """Load one session by key within ``namespace_key``, or raise 404.

        A key from another namespace is a 404 rather than a 403: whether a
        session exists elsewhere is not this caller's business, and the answer
        is the same either way.
        """
        stmt = select(AgentSession).where(
            AgentSession.namespace_key == namespace_key,
            AgentSession.session_key == session_key,
        )
        result = await self._db.execute(stmt)
        row = cast(AgentSession | None, result.scalars().first())
        if row is None:
            raise NotFoundError(
                error_code=ErrorCode.AGENT_SESSION_NOT_FOUND,
                detail=f"Session '{session_key}' not found",
                resource="AgentSession",
                resource_id=session_key,
                hint="Verify the session key and that it belongs to this namespace.",
            )
        return row

    async def to_detail(self, row: AgentSession) -> AgentSessionDetail:
        """Build the detail view of one row, resolving its team slug."""
        slugs = await self._team_slugs(
            namespace_key=row.namespace_key,
            team_ids=[row.team_id] if row.team_id is not None else [],
        )
        return _detail_of(row, team_slug=slugs.get(row.team_id or -1))

    async def list_sessions(
        self,
        *,
        namespace_key: str,
        agent_name: str | None = None,
        team_slug: str | None = None,
        status: AgentSessionStatus | str | None = None,
        cursor: int | None = None,
        limit: int = 20,
    ) -> ListAgentSessionsResponse:
        """One page of sessions, newest first, filtered as asked.

        ``team_slug`` matches the team the *session* was opened under, not the
        teams its agent happens to belong to now. The column exists precisely so
        a conversation keeps the context it was started in; resolving it through
        current membership would move old sessions between teams whenever an
        agent joins one.

        An unknown slug yields an empty page rather than a 404, matching the
        filter on ``GET /agents``: a filter that matches nothing is not an
        error.
        """
        filters = [AgentSession.namespace_key == namespace_key]
        if agent_name is not None:
            filters.append(AgentSession.agent_name == agent_name)
        if status is not None:
            filters.append(AgentSession.status == _status_value(status))
        if team_slug is not None:
            team_id = await self._find_team_id(
                namespace_key=namespace_key, slug=team_slug
            )
            if team_id is None:
                return ListAgentSessionsResponse(
                    sessions=[],
                    pagination=PaginationInfo(
                        limit=limit, total=0, next_cursor=None, has_more=False
                    ),
                )
            filters.append(AgentSession.team_id == team_id)

        page_stmt = (
            select(AgentSession).where(*filters).order_by(AgentSession.id.desc())
        )
        if cursor is not None:
            page_stmt = page_stmt.where(AgentSession.id < cursor)
        result = await self._db.execute(page_stmt.limit(limit + 1))
        rows = list(result.scalars().all())
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        next_cursor = str(rows[-1].id) if has_more and rows else None

        total_result = await self._db.execute(
            select(func.count()).select_from(AgentSession).where(*filters)
        )
        total = int(total_result.scalar_one())

        slugs = await self._team_slugs(
            namespace_key=namespace_key,
            team_ids=[row.team_id for row in rows if row.team_id is not None],
        )
        return ListAgentSessionsResponse(
            sessions=[
                _summary_of(row, team_slug=slugs.get(row.team_id or -1)) for row in rows
            ],
            pagination=PaginationInfo(
                limit=limit,
                total=total,
                next_cursor=next_cursor,
                has_more=has_more,
            ),
        )

    async def count_open_sessions(self, *, namespace_key: str) -> int:
        """Count sessions that still hold executor-side state.

        Archived sessions count: archiving is a UI gesture, and the executor
        still holds the conversation.
        """
        stmt = (
            select(func.count())
            .select_from(AgentSession)
            .where(
                AgentSession.namespace_key == namespace_key,
                AgentSession.status.in_(
                    [
                        AgentSessionStatus.ACTIVE.value,
                        AgentSessionStatus.ARCHIVED.value,
                    ]
                ),
            )
        )
        result = await self._db.execute(stmt)
        return int(result.scalar_one())

    async def create_row(
        self,
        *,
        namespace_key: str,
        session_key: str,
        agent_name: str,
        team_id: int | None,
        executor_kind: str,
        executor_app_name: str,
        executor_user_id: str,
        executor_session_id: str,
        title: str | None,
        created_by_hash: str | None,
        agent_task_id: int | None = None,
    ) -> AgentSession:
        """Insert the mapping row for an already-created executor session."""
        row = AgentSession(
            namespace_key=namespace_key,
            session_key=session_key,
            agent_name=agent_name,
            team_id=team_id,
            executor_kind=executor_kind,
            executor_app_name=executor_app_name,
            executor_user_id=executor_user_id,
            executor_session_id=executor_session_id,
            title=title,
            status=AgentSessionStatus.ACTIVE.value,
            created_by_hash=created_by_hash,
            agent_task_id=agent_task_id,
        )
        self._db.add(row)
        await self._db.flush()
        return row

    async def update_session(
        self,
        *,
        row: AgentSession,
        title: str | None = None,
        update_title: bool = False,
        team_id: int | None = None,
        update_team: bool = False,
        status: AgentSessionStatus | str | None = None,
    ) -> AgentSession:
        """Apply a partial update.

        ``title`` and ``team_id`` are both nullable, so each takes a separate
        flag distinguishing "clear it" from "leave it alone".
        """
        if update_title:
            row.title = title
        if update_team:
            row.team_id = team_id
        if status is not None:
            row.status = _status_value(status)
        row.last_activity_at = dt.datetime.now(tz=dt.UTC)
        await self._db.flush()
        # ``updated_at`` is maintained by a server-side ``onupdate``, so the
        # flush leaves it expired and the next read of it would be lazy IO -
        # which under asyncio is not a slow read, it is a MissingGreenlet. The
        # caller renders this row, so the refresh happens here rather than
        # waiting to surprise them.
        await self._db.refresh(row)
        return row

    async def set_status(
        self, *, row: AgentSession, status: AgentSessionStatus | str
    ) -> AgentSession:
        """Record a status this server observed, e.g. the executor lost the session."""
        row.status = _status_value(status)
        await self._db.flush()
        return row

    async def delete_row(self, *, row: AgentSession) -> None:
        """Remove the mapping row. The executor side is the caller's business."""
        await self._db.delete(row)
        await self._db.flush()

    async def resolve_team_id(self, *, namespace_key: str, slug: str) -> int:
        """Resolve a team slug within this namespace, or raise 404.

        This is where same-namespace membership is enforced. There is no
        foreign key doing it: a composite ``ON DELETE SET NULL`` would try to
        null ``namespace_key`` too and abort the team delete outright.
        """
        team_id = await self._find_team_id(namespace_key=namespace_key, slug=slug)
        if team_id is None:
            raise NotFoundError(
                error_code=ErrorCode.TEAM_NOT_FOUND,
                detail=f"Team with slug '{slug}' not found",
                resource="Team",
                resource_id=slug,
                hint="Verify the slug and that the team belongs to this namespace.",
            )
        return team_id

    async def clear_team(self, *, namespace_key: str, team_id: int) -> None:
        """Detach every session in this namespace from one team.

        Called by the team-delete path, and living here rather than there
        because this is the module that owns the column. Sessions outlive their
        team: the conversation happened, and losing it because a grouping was
        deleted would be data loss nobody asked for.

        This is also the reason ``team_id`` carries no foreign key. A composite
        ``ON DELETE SET NULL`` nulls every referencing column, ``namespace_key``
        included, and would abort against its NOT NULL constraint - so deleting
        a team that had ever been used for a chat would 500 instead.
        """
        await self._db.execute(
            update(AgentSession)
            .where(
                AgentSession.namespace_key == namespace_key,
                AgentSession.team_id == team_id,
            )
            .values(team_id=None)
        )

    async def _find_team_id(self, *, namespace_key: str, slug: str) -> int | None:
        stmt = select(Team.id).where(
            Team.namespace_key == namespace_key,
            Team.slug == slug,
        )
        result = await self._db.execute(stmt)
        row = result.first()
        return int(row[0]) if row is not None else None

    async def _team_slugs(
        self, *, namespace_key: str, team_ids: list[int]
    ) -> dict[int, str]:
        """Map team ids to slugs in one query over the page."""
        wanted = {team_id for team_id in team_ids}
        if not wanted:
            return {}
        stmt = select(Team.id, Team.slug).where(
            Team.namespace_key == namespace_key,
            Team.id.in_(wanted),
        )
        result = await self._db.execute(stmt)
        return {int(team_id): slug for team_id, slug in result.all()}


def _summary_of(row: AgentSession, *, team_slug: str | None) -> AgentSessionSummary:
    return AgentSessionSummary(
        session_key=row.session_key,
        namespace_key=row.namespace_key,
        agent_name=row.agent_name,
        team_slug=team_slug,
        title=row.title,
        status=AgentSessionStatus(row.status),
        executor_kind=ExecutorKind(row.executor_kind),
        last_trace_id=row.last_trace_id,
        last_activity_at=row.last_activity_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _detail_of(row: AgentSession, *, team_slug: str | None) -> AgentSessionDetail:
    """Build the detail view.

    Both live-turn columns are serialized, and they are not redundant. The lock
    (``in_flight_since``) clears whenever this server stops waiting; the liveness
    marker (``in_flight_trace_id``) clears only when a turn really ended. A UI
    that reads only the first believes an agent is idle while it is still
    spending, which is precisely the moment a person wants to act on it.
    """
    return AgentSessionDetail(
        **_summary_of(row, team_slug=team_slug).model_dump(),
        in_flight_since=row.in_flight_since,
        in_flight_trace_id=row.in_flight_trace_id,
    )


def _coordinates_of(row: AgentSession, base_url: str) -> _ExecutorCoordinates:
    return _ExecutorCoordinates(
        executor_kind=row.executor_kind,
        base_url=base_url,
        app_name=row.executor_app_name,
        user_id=row.executor_user_id,
        session_id=row.executor_session_id,
    )


# =============================================================================
# Session-bound runtime token and seeded state
# =============================================================================


def mint_session_runtime_token(
    *, namespace_key: str, session_key: str, actor_id: str
) -> tuple[str, dt.datetime] | None:
    """Mint the token the executor writes back to this session with.

    Returns ``None`` when runtime auth is not configured, which is a supported
    deployment: the session works, and the machine-side writes that need the
    token simply have no credential to make. Failing session creation over it
    would break chat for every deployment that has not enabled runtime auth
    yet, to protect endpoints that do not exist until a later phase.

    The token never appears in an HTTP response. It goes into the executor's
    own session state and nowhere else, because a caller holding it could write
    to the session as though it were the agent.

    ``actor_id`` must already be hashed. A JWT payload is base64, not
    encryption, and this one is handed to a process running arbitrary agent
    code; under the default provider ``Principal.caller_id`` is the first eight
    characters of a live API key, so putting it in this claim would publish a
    credential fragment to exactly the component the threat model assumes can
    be prompt-injected.
    """
    config = runtime_auth_config()
    if config is None:
        return None
    try:
        token, claims = mint_runtime_token(
            namespace_key=namespace_key,
            actor_id=actor_id,
            target_type=RUNTIME_TOKEN_TARGET_TYPE,
            target_id=session_key,
            scopes=SESSION_TOKEN_SCOPES,
            secret=config.secret,
            ttl_seconds=config.ttl_seconds,
        )
    except RuntimeTokenError:
        # Only the failure is logged, never the inputs. A session without a
        # token is degraded, not broken.
        _logger.warning(
            "Could not mint a session-bound runtime token; the session will be "
            "created without one.",
            exc_info=True,
        )
        return None
    return token, claims.expires_at


def build_seed_state(
    *,
    namespace_key: str,
    agent_name: str,
    session_key: str,
    runtime_token: str | None,
    token_expires_at: dt.datetime | None,
) -> dict[str, Any]:
    """Build the state seeded into the executor session at creation.

    This one dict is how an agent's callbacks and tools learn which session
    they are in and what they may write. It is public executor surface -
    ``CallbackContext.state`` and ``ToolContext.state`` - rather than anything
    reached by poking at private attributes.

    Whether those really do expose state seeded at creation is assumption A1 in
    the plan, and it is unverified here: the spike needed a model API key this
    environment does not have. Nothing in Phase 1 reads this state back, so
    nothing here depends on the answer yet.
    """
    payload: dict[str, Any] = {
        "session_key": session_key,
        "namespace_key": namespace_key,
        "agent_name": agent_name,
    }
    if runtime_token is not None:
        payload["runtime_token"] = runtime_token
        if token_expires_at is not None:
            payload["runtime_token_expires_at"] = token_expires_at.isoformat()
    return {SESSION_STATE_KEY: payload}


# =============================================================================
# Orchestration: database and executor, never at the same time
# =============================================================================


def require_executor_enabled(settings: ExecutorSettings) -> None:
    """Refuse politely when the feature is switched off.

    A 503 rather than a 404: the route exists, the deployment has not turned it
    on, and saying so is more useful than pretending the endpoint is absent.
    """
    if not settings.enabled:
        raise APIError(
            status_code=503,
            error_code=ErrorCode.EXECUTOR_UNAVAILABLE,
            reason=ErrorReason.SERVICE_UNAVAILABLE,
            detail=EXECUTOR_DISABLED_MESSAGE,
        )


async def open_session(
    *,
    namespace_key: str,
    created_by_hash: str | None,
    agent_name: str,
    title: str | None,
    team_slug: str | None,
    factory: ExecutorClientFactory,
    settings: ExecutorSettings,
    task_key: str | None = None,
) -> AgentSessionDetail:
    """Create an executor conversation and the row that maps to it.

    Order of refusals, cheapest and most specific first: the feature must be
    on, the executors must not be halted, the agent must exist, it must be
    bound to an enabled executor, the team must be real, dispatch must not be
    paused if this session belongs to a task, and the namespace must be under
    its session ceiling. Only then is anything created anywhere.

    The halt is consulted before the agent lookup and it refuses **every**
    session, human chat included. That is level 3 doing what its copy says it
    does, and it is the reason the kill switch is a flag rather than a sweep
    over ``agent_runtimes``: bindings disabled for unrelated reasons would be
    indistinguishable afterwards. The pause is narrower - it stops new dispatch
    work - so a human opening a chat while the fleet is paused still gets one,
    which is usually somebody going to look at what happened.

    Every identifier is minted here. ``executor_user_id`` carries the namespace
    as a prefix so that even a collision in the random half cannot produce a
    triple that another namespace could hold, and no request model has a field
    that could influence any part of it.
    """
    require_executor_enabled(settings)

    async with AsyncSessionLocal() as db:
        await require_executors_not_halted(
            db, namespace_key=namespace_key, action="Opening a session"
        )
        runtimes = AgentRuntimesService(db)
        binding = await runtimes.require_enabled_binding(
            namespace_key=namespace_key, agent_name=agent_name
        )
        sessions = AgentSessionsService(db)
        team_id = (
            await sessions.resolve_team_id(namespace_key=namespace_key, slug=team_slug)
            if team_slug is not None
            else None
        )
        # Resolved before the executor is contacted, so an unknown or already
        # finished task costs nothing and leaves no conversation behind.
        agent_task_id = (
            await AgentTasksService(db, settings=dispatch_settings).resolve_task_id(
                namespace_key=namespace_key, task_key=task_key
            )
            if task_key is not None
            else None
        )
        if agent_task_id is not None:
            # Only for a task's session. A pause stops new dispatch work; it
            # does not lock operators out of the console while they look at
            # what the fleet just did.
            await require_dispatch_not_paused(
                db,
                namespace_key=namespace_key,
                action="Opening a session for a dispatch task",
            )
        open_count = await sessions.count_open_sessions(namespace_key=namespace_key)
        if open_count >= settings.max_concurrent_sessions:
            raise APIError(
                status_code=429,
                error_code=ErrorCode.QUOTA_EXCEEDED,
                reason=ErrorReason.CONFLICT,
                detail=(
                    f"This namespace already holds {open_count} sessions, which "
                    f"is its configured ceiling."
                ),
                hint=(
                    "Delete sessions that are finished with, or raise "
                    "AGENT_CONTROL_EXECUTOR_MAX_CONCURRENT_SESSIONS."
                ),
                # There is no window here to count down: this ceiling clears
                # when somebody deletes a session, not when a clock rolls. The
                # number is a poll interval rather than a promise, and the
                # dispatcher's own default would otherwise be the only thing
                # deciding how hard it hammers a full namespace.
                extra_details={"retry_after_seconds": SESSION_CEILING_RETRY_SECONDS},
            )
        coordinates = _ExecutorCoordinates(
            executor_kind=binding.executor_kind,
            base_url=binding.base_url,
            app_name=binding.executor_app_name,
            user_id=f"{namespace_key}:{uuid4().hex}",
            session_id=uuid4().hex,
        )

    session_key = uuid4().hex
    minted = mint_session_runtime_token(
        namespace_key=namespace_key,
        session_key=session_key,
        actor_id=created_by_hash or "anonymous",
    )
    state = build_seed_state(
        namespace_key=namespace_key,
        agent_name=agent_name,
        session_key=session_key,
        runtime_token=minted[0] if minted else None,
        token_expires_at=minted[1] if minted else None,
    )

    client = factory.client_for(
        executor_kind=coordinates.executor_kind, base_url=coordinates.base_url
    )
    try:
        await client.create_session(
            app_name=coordinates.app_name,
            user_id=coordinates.user_id,
            session_id=coordinates.session_id,
            state=state,
        )
    except ExecutorError as exc:
        raise executor_api_error(exc) from exc

    try:
        async with AsyncSessionLocal() as db:
            sessions = AgentSessionsService(db)
            row = await sessions.create_row(
                namespace_key=namespace_key,
                session_key=session_key,
                agent_name=agent_name,
                team_id=team_id,
                executor_kind=coordinates.executor_kind,
                executor_app_name=coordinates.app_name,
                executor_user_id=coordinates.user_id,
                executor_session_id=coordinates.session_id,
                title=title,
                created_by_hash=created_by_hash,
                agent_task_id=agent_task_id,
            )
            detail = await sessions.to_detail(row)
            await db.commit()
            return detail
    except Exception as exc:
        # The executor holds a conversation this server just failed to record,
        # and nothing will ever address it again unless it is removed now.
        await _compensating_delete(client, coordinates)
        raise DatabaseError(
            "Could not record the session after the executor created it.",
            resource="AgentSession",
            operation="session creation",
        ) from exc


async def _compensating_delete(
    client: ExecutorClient, coordinates: _ExecutorCoordinates
) -> None:
    """Undo an executor-side create whose local write failed.

    Best effort by necessity. When this fails too, an executor session exists
    that no row points at - so it is logged loudly and by name, because the only
    remaining way to find it is to go and look.
    """
    try:
        await client.delete_session(
            app_name=coordinates.app_name,
            user_id=coordinates.user_id,
            session_id=coordinates.session_id,
        )
    except ExecutorError:
        _logger.error(
            "Orphaned executor session: the local row could not be written and "
            "the compensating delete failed. app_name=%s session_id=%s",
            coordinates.app_name,
            coordinates.session_id,
        )


async def delete_session(
    *,
    namespace_key: str,
    session_key: str,
    factory: ExecutorClientFactory,
    settings: ExecutorSettings,
) -> None:
    """Delete both halves of a session, executor side first.

    Executor first because the local row is the only thing that knows the
    executor coordinates. Dropping it before the executor delete succeeds would
    leave a conversation with no handle. When the executor delete fails, the row
    stays and moves to ``orphaned_pending_delete``, and the caller gets an
    error, so a retry of the same DELETE finishes the job.
    """
    require_executor_enabled(settings)

    async with AsyncSessionLocal() as db:
        sessions = AgentSessionsService(db)
        row = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        binding = await AgentRuntimesService(db).find_binding(
            namespace_key=namespace_key, agent_name=row.agent_name
        )
        # The session's own copy of the coordinates is what identifies it. The
        # binding is consulted only for a base URL, and only because the row
        # does not carry one; an unbound agent's sessions are still deletable
        # locally, which matters when the binding was removed first.
        base_url = binding.base_url if binding is not None else None
        coordinates = _coordinates_of(row, base_url or "")

    if base_url is None:
        async with AsyncSessionLocal() as db:
            sessions = AgentSessionsService(db)
            row = await sessions.get_row_or_404(
                namespace_key=namespace_key, session_key=session_key
            )
            await sessions.set_status(
                row=row, status=AgentSessionStatus.ORPHANED_PENDING_DELETE
            )
            await db.commit()
        raise APIError(
            status_code=409,
            error_code=ErrorCode.AGENT_RUNTIME_NOT_BOUND,
            reason=ErrorReason.CONFLICT,
            detail=(
                "This session's agent is no longer bound to an executor, so the "
                "executor-side conversation cannot be deleted."
            ),
            hint=(
                "Re-bind the agent to the executor that holds the session and "
                "repeat the delete."
            ),
        )

    client = factory.client_for(
        executor_kind=coordinates.executor_kind, base_url=coordinates.base_url
    )
    try:
        await client.delete_session(
            app_name=coordinates.app_name,
            user_id=coordinates.user_id,
            session_id=coordinates.session_id,
        )
    except ExecutorError as exc:
        async with AsyncSessionLocal() as db:
            sessions = AgentSessionsService(db)
            row = await sessions.get_row_or_404(
                namespace_key=namespace_key, session_key=session_key
            )
            await sessions.set_status(
                row=row, status=AgentSessionStatus.ORPHANED_PENDING_DELETE
            )
            await db.commit()
        raise executor_api_error(exc) from exc

    async with AsyncSessionLocal() as db:
        sessions = AgentSessionsService(db)
        row = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        await sessions.delete_row(row=row)
        await db.commit()


async def read_transcript(
    *,
    namespace_key: str,
    session_key: str,
    caller_hash: str | None,
    is_admin: bool,
    after_index: int | None,
    limit: int,
    factory: ExecutorClientFactory,
    settings: ExecutorSettings,
) -> ListSessionMessagesResponse:
    """Read one page of a conversation from the executor.

    A session whose executor-side state has gone is not an error here. It
    answers 200 with an empty transcript and a one-line notice, because a chat
    panel that turns into an error page tells a person less than a chat panel
    that says the conversation is gone.

    Transcript reads are scoped to the caller who opened the session, with
    admins exempt. Under the default provider that scoping is weaker than it
    looks: browser callers all resolve to the same caller identity, so it
    separates API keys from each other and from the console, and separates
    nothing within the console. That is a property of the credential model, not
    of this check.
    """
    require_executor_enabled(settings)

    async with AsyncSessionLocal() as db:
        sessions = AgentSessionsService(db)
        row = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(row, caller_hash=caller_hash, is_admin=is_admin)
        status = AgentSessionStatus(row.status)
        if status in (
            AgentSessionStatus.ORPHANED,
            AgentSessionStatus.ORPHANED_PENDING_DELETE,
        ):
            return _empty_transcript(session_key=session_key, status=status)
        binding = await AgentRuntimesService(db).find_binding(
            namespace_key=namespace_key, agent_name=row.agent_name
        )
        if binding is None:
            return _empty_transcript(
                session_key=session_key,
                status=status,
                notice=(
                    "This session's agent is not bound to an executor, so its "
                    "messages cannot be read."
                ),
            )
        coordinates = _coordinates_of(row, binding.base_url)

    client = factory.client_for(
        executor_kind=coordinates.executor_kind, base_url=coordinates.base_url
    )
    try:
        executor_session = await client.get_session(
            app_name=coordinates.app_name,
            user_id=coordinates.user_id,
            session_id=coordinates.session_id,
        )
    except ExecutorSessionNotFoundError:
        async with AsyncSessionLocal() as db:
            sessions = AgentSessionsService(db)
            row = await sessions.get_row_or_404(
                namespace_key=namespace_key, session_key=session_key
            )
            await sessions.set_status(row=row, status=AgentSessionStatus.ORPHANED)
            await db.commit()
        return _empty_transcript(
            session_key=session_key, status=AgentSessionStatus.ORPHANED
        )
    except ExecutorError as exc:
        raise executor_api_error(exc) from exc

    return _page_of(
        session_key=session_key,
        status=status,
        messages=executor_session.messages,
        after_index=after_index,
        limit=limit,
    )


async def probe_executor_health(
    *,
    namespace_key: str,
    factory: ExecutorClientFactory,
    settings: ExecutorSettings,
) -> ExecutorHealthResponse:
    """Probe every executor this namespace's agents are bound to.

    ``/health`` checks the process and nothing else, deliberately. Chat adds a
    hard dependency on a second process that has no health signal of its own,
    so this is the probe for it: a monitor that answers "is the executor up"
    before a person answers it by hitting send.

    Disabled bindings are reported without being probed. They are configuration
    that has been deliberately drained, and calling them anyway would report a
    problem where there is a decision.
    """
    checked_at = dt.datetime.now(tz=dt.UTC)
    if not settings.enabled:
        return ExecutorHealthResponse(
            enabled=False, healthy=True, executors=[], checked_at=checked_at
        )

    async with AsyncSessionLocal() as db:
        bindings = await AgentRuntimesService(db).list_runtimes(
            namespace_key=namespace_key
        )
        await _refresh_stuck_in_flight_gauge(
            db, stale_after_seconds=settings.turn_stale_after_seconds
        )
        probes = [
            (
                binding.agent_name,
                binding.executor_kind,
                binding.base_url,
                binding.enabled,
            )
            for binding in bindings[:_HEALTH_PROBE_LIMIT]
        ]

    def _mark_up(agent_name: str, up: bool) -> None:
        # Counted, not labelled by tenant: ``/metrics`` has no credential
        # dependency, so an agent name here is public. See EXECUTOR_PROBES.
        del agent_name
        EXECUTOR_PROBES.labels(
            result=EXECUTOR_PROBE_REACHABLE if up else EXECUTOR_PROBE_UNREACHABLE
        ).inc()

    async def probe(
        agent_name: str, kind: str, base_url: str, enabled: bool
    ) -> ExecutorHealthEntry:
        if not enabled:
            # A drained binding is a decision, not an outage. Leaving the gauge
            # alone rather than zeroing it keeps "we turned this off" out of the
            # series that pages someone.
            return ExecutorHealthEntry(
                agent_name=agent_name,
                executor_kind=ExecutorKind(kind),
                enabled=False,
                reachable=False,
                error="Binding is disabled and was not probed.",
            )
        try:
            client = factory.client_for(executor_kind=kind, base_url=base_url)
            await client.health()
        except ExecutorError as exc:
            _mark_up(agent_name, False)
            return ExecutorHealthEntry(
                agent_name=agent_name,
                executor_kind=ExecutorKind(kind),
                enabled=True,
                reachable=False,
                error=exc.message,
            )
        _mark_up(agent_name, True)
        return ExecutorHealthEntry(
            agent_name=agent_name,
            executor_kind=ExecutorKind(kind),
            enabled=True,
            reachable=True,
        )

    entries = list(await asyncio.gather(*(probe(*probe_args) for probe_args in probes)))
    healthy = all(entry.reachable for entry in entries if entry.enabled)
    return ExecutorHealthResponse(
        enabled=True,
        healthy=healthy,
        executors=entries,
        checked_at=checked_at,
    )


async def _refresh_stuck_in_flight_gauge(
    db: AsyncSession, *, stale_after_seconds: float
) -> None:
    """Count sessions whose turn lock has outlived the staleness window.

    Refreshed here rather than by a background job, because there is no
    background job and inventing one to move a gauge would put a periodic
    database query into the process to feed a dashboard. The health route is
    what a monitor already polls, so this is where the number is cheapest and
    freshest at the same time.

    Deliberately not namespace-scoped: it is a process-level symptom, the same
    class of number as the connection-pool gauge next to it, and slicing it by
    namespace would multiply a whole-deployment health signal into one series
    per tenant.
    """
    result = await db.execute(
        text(
            "SELECT count(*) FROM agent_sessions "
            " WHERE in_flight_since IS NOT NULL "
            "   AND in_flight_since < now() - (:stale * interval '1 second')"
        ),
        {"stale": float(stale_after_seconds)},
    )
    SESSIONS_STUCK_IN_FLIGHT.set(int(result.scalar_one()))


def require_content_access(
    row: AgentSession, *, caller_hash: str | None, is_admin: bool, for_turn: bool = False
) -> None:
    """Refuse access to a session's content by anyone but the caller who opened it.

    Public rather than private because it is the shared predicate for every
    route that touches a conversation, not just the transcript read: running a
    turn appends to somebody's conversation and hands their model output back,
    and the plan puts halt creation behind the same check for the same reason.
    One predicate, so the answer to "who may see this chat" cannot drift between
    the routes that read it and the routes that write it.

    **Three branches, and the third one is a requirement rather than a
    convenience.** A session opened for a dispatch task has no human owner: the
    dispatcher opened it with its own credential, so creator scoping would 403
    every non-admin operator out of the per-task step rail and out of halting
    one runaway task. Both workarounds are unacceptable - sharing the
    dispatcher's key lets every reviewer start turns as the dispatcher, and
    handing out admin keys hands out ``controls.create`` and
    ``agent_runtimes.write``. Oversight without admin is a requirement of the
    dispatch design, and it matters more here than in a chat panel, where the
    operator is the session owner by construction and here nobody is.

    What that branch grants, stated exactly: any caller who got far enough to
    reach this predicate may read, halt and nudge a session belonging to a
    task. Under the default local-credential provider that is every
    authenticated caller in the namespace, because ``agent_tasks.read`` sits at
    AUTHENTICATED and the header path has no per-key operation allowlist. A
    provider that can express a narrower grant should narrow it here.

    **What it does not grant is a turn**, which is why ``for_turn`` exists. This
    one predicate also gates ``run_turn``, so an unqualified task branch would
    let any authenticated caller in the namespace append to a fleet
    conversation, spend against it, and interleave with the dispatcher
    mid-chain - the plan rejects sharing the dispatcher's key precisely because
    it "lets every reviewer start turns as the dispatcher", and granting it to
    everybody would be worse than sharing it. Oversight of a task's session is
    read, halt and nudge; driving it is the holder's or an admin's.
    """
    if is_admin or row.created_by_hash is None:
        return
    if row.created_by_hash == caller_hash:
        return
    if row.agent_task_id is not None:
        if not for_turn:
            return
        raise ForbiddenError(
            error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
            detail="This session belongs to a dispatch task and is driven by its dispatcher.",
            resource="AgentSession",
            resource_id=row.session_key,
            hint="Read, halt or nudge it instead. A turn on it is the dispatcher's to start.",
        )
    raise ForbiddenError(
        error_code=ErrorCode.AUTH_INSUFFICIENT_PRIVILEGES,
        detail="This session's messages belong to a different caller.",
        resource="AgentSession",
        resource_id=row.session_key,
        hint="Read it with the credential that opened the session, or an admin key.",
    )


def _empty_transcript(
    *,
    session_key: str,
    status: AgentSessionStatus,
    notice: str | None = None,
) -> ListSessionMessagesResponse:
    default_notice = (
        "The executor no longer holds this conversation, so there is nothing "
        "left to show. The session record is kept so it can be tidied up."
    )
    return ListSessionMessagesResponse(
        session_key=session_key,
        status=status,
        messages=[],
        next_index=None,
        has_more=False,
        total=0,
        notice=notice if notice is not None else default_notice,
    )


def _page_of(
    *,
    session_key: str,
    status: AgentSessionStatus,
    messages: tuple[ExecutorMessage, ...],
    after_index: int | None,
    limit: int,
) -> ListSessionMessagesResponse:
    """Slice a transcript into one page.

    Indexes are assigned over the whole transcript before slicing, so a
    message's index does not change with the page it arrives in.
    """
    start = 0 if after_index is None else max(0, after_index + 1)
    window = messages[start : start + limit]
    has_more = start + len(window) < len(messages)
    wire = [
        _message_of(message, index=start + offset)
        for offset, message in enumerate(window)
    ]
    return ListSessionMessagesResponse(
        session_key=session_key,
        status=status,
        messages=wire,
        next_index=wire[-1].index if has_more and wire else None,
        has_more=has_more,
        total=len(messages),
    )


def _message_of(message: ExecutorMessage, *, index: int) -> SessionMessage:
    return SessionMessage(
        index=index,
        role=SessionMessageRole(message.role),
        author=message.author,
        timestamp=message.timestamp,
        parts=[
            SessionMessagePart(
                kind=SessionMessagePartKind(part.kind),
                text=part.text,
                tool_name=part.tool_name,
                tool_call_id=part.tool_call_id,
                arguments=part.arguments,
                result=part.result,
            )
            for part in message.parts
        ],
    )
