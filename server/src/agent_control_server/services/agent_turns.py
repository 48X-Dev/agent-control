"""Running one blocking turn.

The lock protocol - one atomic acquire, a fenced release, and the two columns
that clear on different events - lives next door in ``turn_locks``. This module
is the flow around it: what is refused before anything leaves the process, what
gets seeded into the executor state, and which of the two release exits each
failure takes.

Two properties of the flow are load-bearing.

**No database connection is held across the executor call.** The pool is five
plus ten overflow and a turn can last minutes, so a handler that held one would
starve policy evaluation for every unrelated agent in the process after a dozen
concurrent chats. Every database step opens and closes its own short-lived
session.

**The cleanup is shielded.** A browser tab closing mid-turn cancels this task,
and an unshielded clear never lands: the session stays locked and refuses every
later turn until the staleness window expires, with a symptom ("I can't send
another message") that looks nothing like its cause. The shield turns the
cleanup into a task of its own that finishes whatever happens to this one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass

from agent_control_models.errors import ErrorCode, ErrorReason
from agent_control_models.sessions import (
    AgentSessionStatus,
    SessionMessage,
    SessionMessagePart,
    SessionMessagePartKind,
    SessionMessageRole,
    TurnResponse,
)

from ..config import ExecutorSettings
from ..db import AsyncSessionLocal
from ..errors import APIError, executor_api_error
from .agent_runtimes import AgentRuntimesService
from .agent_sessions import (
    AgentSessionsService,
    mint_session_runtime_token,
    require_content_access,
    require_executor_enabled,
)
from .executor_client import (
    ExecutorClientFactory,
    ExecutorError,
    ExecutorMessage,
    ExecutorTurnTimeoutError,
)
from .executor_metrics import (
    TURN_DURATION,
    TURN_OUTCOME_ABANDONED,
    TURN_OUTCOME_COMPLETED,
    TURN_OUTCOME_EXECUTOR_ERROR,
    TURN_OUTCOME_TIMEOUT,
    TURN_REJECT_IN_FLIGHT,
    TURN_REJECT_QUOTA,
    TURNS_REJECTED,
)
from .turn_locks import acquire_turn_lock, new_trace_id, release_turn_lock
from .turn_quota import get_turn_quota

_logger = logging.getLogger(__name__)

TURN_STATE_KEY = "agent_control_turn"
"""Key the per-turn state delta hangs under in the executor's session state.

Separate from the ``agent_control`` key seeded at session creation, which holds
the session identity and the runtime token and does not change. Merging per-turn
facts into that dict would mean a stale trace id surviving into the next turn if
a delta is ever dropped, and a reader could not tell which turn a value belonged
to."""

@dataclass(frozen=True)
class _TurnTarget:
    """Everything one turn needs, read out under the acquire's lock.

    A plain value rather than an ORM row, because the database session it was
    read through is closed before the executor is called and touching an
    expired attribute afterwards would be lazy IO on a dead connection.
    """

    session_id: int
    executor_kind: str
    base_url: str
    app_name: str
    user_id: str
    session_id_remote: str


async def run_turn(
    *,
    namespace_key: str,
    session_key: str,
    caller_hash: str | None,
    is_admin: bool,
    message: str,
    factory: ExecutorClientFactory,
    settings: ExecutorSettings,
) -> TurnResponse:
    """Run one turn to completion and answer with what it produced.

    Refusals happen in the cheapest order that is also the most specific: the
    feature must be on, the caller must be under its quota, the session must
    exist and belong to them, its agent must still be bound to an executor, and
    the session must not already be running a turn. Only then does anything
    leave this process.

    No database connection is held across the executor call. The pool is five
    plus ten overflow, and a turn can last minutes, so a handler that held one
    would starve policy evaluation for every unrelated agent in the process
    after a dozen concurrent chats.
    """
    require_executor_enabled(settings)
    _enforce_quota(
        namespace_key=namespace_key, caller_hash=caller_hash, settings=settings
    )

    trace_id = new_trace_id()
    target = await _acquire_turn(
        namespace_key=namespace_key,
        session_key=session_key,
        caller_hash=caller_hash,
        is_admin=is_admin,
        trace_id=trace_id,
        settings=settings,
    )

    started_at = dt.datetime.now(tz=dt.UTC)
    started_monotonic = asyncio.get_running_loop().time()
    outcome = TURN_OUTCOME_ABANDONED
    turn_ended = False
    try:
        try:
            client = factory.client_for(
                executor_kind=target.executor_kind, base_url=target.base_url
            )
        except ExecutorError as exc:
            # No client for this binding's kind. Nothing left the process, so
            # the turn genuinely did not start and both columns clear: leaving
            # the liveness marker set here would advertise an invocation that
            # was never made. Mapped rather than raised raw, because an
            # unmapped ExecutorError reaches the client as a 500.
            outcome = TURN_OUTCOME_EXECUTOR_ERROR
            turn_ended = True
            raise executor_api_error(exc) from exc
        try:
            turn = await client.run(
                app_name=target.app_name,
                user_id=target.user_id,
                session_id=target.session_id_remote,
                message=message,
                state_delta={
                    TURN_STATE_KEY: _turn_state(
                        namespace_key=namespace_key,
                        session_key=session_key,
                        caller_hash=caller_hash,
                        trace_id=trace_id,
                    )
                },
                timeout_seconds=settings.turn_timeout_seconds,
            )
        except ExecutorTurnTimeoutError as exc:
            # The one failure that is not an ending. The executor is still
            # working, so ``turn_ended`` stays false: the lock is released and
            # the liveness marker is not. The 504 itself comes from the shared
            # mapper, so this route and the streaming one cannot disagree about
            # what a still-running turn looks like on the wire.
            outcome = TURN_OUTCOME_TIMEOUT
            raise executor_api_error(exc) from exc
        except ExecutorError as exc:
            # Everything else did end the turn, one way or another: the
            # executor refused it, lost the session, or failed internally.
            outcome = TURN_OUTCOME_EXECUTOR_ERROR
            turn_ended = True
            raise executor_api_error(exc) from exc

        outcome = TURN_OUTCOME_COMPLETED
        turn_ended = True
        completed_at = dt.datetime.now(tz=dt.UTC)
        return TurnResponse(
            session_key=session_key,
            trace_id=trace_id,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=max(
                0.0, asyncio.get_running_loop().time() - started_monotonic
            ),
            messages=[
                _message_of(executor_message, index=index)
                for index, executor_message in enumerate(turn.messages)
            ],
        )
    finally:
        TURN_DURATION.labels(outcome=outcome).observe(
            max(0.0, asyncio.get_running_loop().time() - started_monotonic)
        )
        # Shielded so a client hanging up cannot leave the lock set. The shield
        # wraps a task that keeps running after this coroutine is torn down;
        # ``CancelledError`` is re-raised rather than swallowed, because a
        # handler that eats its own cancellation is a handler that never dies.
        release = asyncio.shield(
            release_turn_lock(
                session_id=target.session_id,
                namespace_key=namespace_key,
                trace_id=trace_id,
                turn_ended=turn_ended,
            )
        )
        try:
            await release
        except asyncio.CancelledError:
            _logger.info(
                "Client hung up mid-turn. The shielded cleanup is a task of its "
                "own and finishes without this handler. namespace=%s "
                "session_id=%s",
                namespace_key,
                target.session_id,
            )
            raise


def _turn_state(
    *,
    namespace_key: str,
    session_key: str,
    caller_hash: str | None,
    trace_id: str,
) -> dict[str, str]:
    """Build the per-turn state the executor reads back inside the invocation.

    The trace id is what connects this turn to its guardrail decisions.

    The runtime token beside it is minted fresh **per turn**, and that is not
    belt and braces. The token seeded at session creation is bound to the
    configured runtime TTL - five minutes by default - while an ADK session
    lives for hours, and a runtime token cannot renew itself: the exchange
    endpoint sits behind the API-key authorizer and grants only ``runtime.use``
    anyway, so a re-exchange would come back without the session-write scopes.
    Minting one per turn costs an HS256 signature on a path that is about to
    make a model call, and it means the credential the agent claims nudges and
    halts with is never older than the turn it is being claimed in.

    Absent when runtime auth is not configured, which is a supported
    deployment: the turn runs, and the machine-side writes simply have no
    credential to make.
    """
    state: dict[str, str] = {"trace_id": trace_id}
    minted = mint_session_runtime_token(
        namespace_key=namespace_key,
        session_key=session_key,
        actor_id=caller_hash or "anonymous",
    )
    if minted is not None:
        token, expires_at = minted
        state["runtime_token"] = token
        state["runtime_token_expires_at"] = expires_at.isoformat()
    return state


def _enforce_quota(
    *, namespace_key: str, caller_hash: str | None, settings: ExecutorSettings
) -> None:
    """Refuse a caller who is starting turns faster than the configured rate."""
    quota = get_turn_quota(max_per_minute=settings.max_turns_per_minute)
    retry_after = quota.try_acquire(
        namespace_key=namespace_key, caller_hash=caller_hash
    )
    if retry_after is None:
        return
    TURNS_REJECTED.labels(reason=TURN_REJECT_QUOTA).inc()
    raise APIError(
        status_code=429,
        error_code=ErrorCode.QUOTA_EXCEEDED,
        reason=ErrorReason.CONFLICT,
        detail=(
            f"This credential has started {settings.max_turns_per_minute} turns "
            f"in the last minute, which is its configured ceiling."
        ),
        hint=(
            f"Retry in about {retry_after:.0f} seconds, or raise "
            f"AGENT_CONTROL_EXECUTOR_MAX_TURNS_PER_MINUTE."
        ),
    )


async def _acquire_turn(
    *,
    namespace_key: str,
    session_key: str,
    caller_hash: str | None,
    is_admin: bool,
    trace_id: str,
    settings: ExecutorSettings,
) -> _TurnTarget:
    """Take the session's turn lock, or explain why it could not be taken.

    Everything here runs in one short-lived transaction that is committed before
    the executor is contacted, so no connection is held across the turn itself.

    The refusals are ordered cheapest and most specific first, and each says
    something different: an unknown session is a 404, somebody else's session is
    a 403, a session the executor has lost is a 409 naming that, an agent with
    no enabled binding is a different 409, and only then does the lock decide.
    """
    async with AsyncSessionLocal() as db:
        sessions = AgentSessionsService(db)
        row = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(row, caller_hash=caller_hash, is_admin=is_admin)
        _require_runnable_status(row.status, session_key=session_key)
        binding = await AgentRuntimesService(db).require_enabled_binding(
            namespace_key=namespace_key, agent_name=row.agent_name
        )

        session_id = await acquire_turn_lock(
            db,
            namespace_key=namespace_key,
            session_key=session_key,
            trace_id=trace_id,
            stale_after_seconds=settings.turn_stale_after_seconds,
        )
        if session_id is None:
            await db.rollback()
            TURNS_REJECTED.labels(reason=TURN_REJECT_IN_FLIGHT).inc()
            raise APIError(
                status_code=409,
                error_code=ErrorCode.TURN_IN_FLIGHT,
                reason=ErrorReason.CONFLICT,
                detail=(
                    "This session is already running a turn. A session answers "
                    "one turn at a time."
                ),
                hint=(
                    "Wait for the turn to finish, or open a second session to "
                    "ask something in parallel."
                ),
            )
        target = _TurnTarget(
            session_id=int(session_id),
            executor_kind=row.executor_kind,
            base_url=binding.base_url,
            app_name=row.executor_app_name,
            user_id=row.executor_user_id,
            session_id_remote=row.executor_session_id,
        )
        await db.commit()
        return target


def _require_runnable_status(status: str, *, session_key: str) -> None:
    """Refuse a session whose executor-side conversation is known to be gone."""
    if status not in (
        AgentSessionStatus.ORPHANED.value,
        AgentSessionStatus.ORPHANED_PENDING_DELETE.value,
    ):
        return
    raise APIError(
        status_code=409,
        error_code=ErrorCode.AGENT_SESSION_NOT_FOUND,
        reason=ErrorReason.CONFLICT,
        detail=(
            "The executor no longer holds this conversation, so there is "
            "nothing for a turn to continue."
        ),
        hint="Open a new session with the agent.",
        resource="AgentSession",
        resource_id=session_key,
    )


def _message_of(message: ExecutorMessage, *, index: int) -> SessionMessage:
    """Map one executor message onto the wire shape.

    ``index`` is turn-relative, which ``TurnResponse`` says in writing. The
    transcript route is where indexes are conversation-absolute; this handler
    never read the conversation, so it has nothing to count from and does not
    pretend otherwise.
    """
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
