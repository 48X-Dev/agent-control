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
import math
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

from ..config import ExecutorSettings, dispatch_settings
from ..db import AsyncSessionLocal
from ..errors import APIError, executor_api_error
from .agent_dispatch_state import charge_dispatch_turn, require_executors_not_halted
from .agent_runtimes import AgentRuntimesService
from .agent_sessions import (
    AgentSessionsService,
    mint_session_runtime_token,
    require_content_access,
    require_executor_enabled,
)
from .attachment_binding import load_for_turn, record_bindings, unique_keys
from .attachment_delivery import DeliveredTurn, build_turn_message
from .executor_client import (
    ExecutorClientFactory,
    ExecutorError,
    ExecutorMessage,
    ExecutorTurnTimeoutError,
)
from .executor_metrics import (
    TURN_DURATION,
    TURN_OUTCOME_ABANDONED,
    TURN_OUTCOME_ATTACHMENT_REFUSED,
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
    attachment_keys: list[str] | None = None,
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
            delivery = await _prepare_attachments(
                namespace_key=namespace_key,
                session_id=target.session_id,
                trace_id=trace_id,
                message=message,
                attachment_keys=attachment_keys or [],
                settings=settings,
            )
        except APIError:
            # A named file that cannot be sent refuses the turn rather than
            # running without it. Nothing left the process, so the turn did not
            # start and both lock columns clear.
            #
            # Labelled rather than left at the initialiser: an unknown key, a
            # file that is not ready and an oversize set are all refusals this
            # deployment made, and recording them as ``abandoned`` would file a
            # per-turn cap set too low under "people are giving up on this
            # agent".
            outcome = TURN_OUTCOME_ATTACHMENT_REFUSED
            turn_ended = True
            raise
        message = delivery.message

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


async def _prepare_attachments(
    *,
    namespace_key: str,
    session_id: int,
    trace_id: str,
    message: str,
    attachment_keys: list[str],
    settings: ExecutorSettings,
) -> DeliveredTurn:
    """Resolve this turn's files and fold them into the message it will send.

    One short-lived session, committed before the executor is contacted, for
    the same reason every other database step in this module is: the pool is
    five plus ten and a turn can last minutes.

    The bindings are written **before** the call rather than after it, because
    they record what this turn carried and that is true the moment the request
    is built. Writing them afterwards would leave a turn that timed out with no
    record of the documents it had already put in front of a model.
    """
    if not attachment_keys:
        return DeliveredTurn(
            message=message,
            included_keys=(),
            named_keys=(),
            overflowed=False,
            render_failed=False,
        )
    # Deduplicated once, here, so the renderer and the ledger are handed the
    # same list. Doing it in only one of them is how a message that carried two
    # copies of one file gets recorded as having carried one.
    attachment_keys = unique_keys(attachment_keys)

    async with AsyncSessionLocal() as db:
        deliverables = await load_for_turn(
            db,
            namespace_key=namespace_key,
            session_id=session_id,
            attachment_keys=attachment_keys,
            settings=settings,
        )
        delivery = build_turn_message(
            message, deliverables, ceiling=settings.attachment_delivery_max_chars
        )
        # Only a genuine overflow refuses. ``render_failed`` is a bug in this
        # server, and answering it with "shorten your message" would send the
        # operator round a loop that cannot end: the turn runs instead, with
        # every file named as not included.
        if delivery.overflowed:
            raise APIError(
                status_code=413,
                error_code=ErrorCode.ATTACHMENT_TOO_LARGE,
                reason=ErrorReason.INVALID,
                detail=(
                    "This message is too long to carry its files as well. "
                    "Nothing was sent to the agent."
                ),
                hint="Shorten the message, or send the files on their own.",
            )
        await record_bindings(
            db,
            namespace_key=namespace_key,
            session_id=session_id,
            trace_id=trace_id,
            attachment_keys=attachment_keys,
            included_keys=delivery.included_keys,
        )
        await db.commit()
    return delivery


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
    """Refuse a caller who is starting turns faster than the configured rate.

    The delay goes out as a number as well as in the sentence. Prose is for the
    person reading the response; ``retry_after_seconds`` is for the process that
    has to decide when to come back, and regexing an English hint breaks the
    first time somebody rewords it - which hints in this repo do get. Without
    the number, the likely implementation is a hardcoded sleep that ignores the
    server, and under a shared bucket the fleet then oscillates between
    hammering and idling.
    """
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
        extra_details={"retry_after_seconds": math.ceil(retry_after)},
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

    **Between the binding and the lock sit the fleet ceilings.** This is where
    the executor kill switch, the namespace budget and the dispatch pause are
    enforced, because it is the one place every turn passes through regardless
    of which process started it. The dispatcher checks the same things before
    it claims, and that is an optimisation so it does not open sessions it
    cannot use; it is not the enforcement point and must not be mistaken for
    one.

    The kill switch applies to **every** turn, human chat included, because
    that is what its copy promises and because a chat session opened before it
    was pressed would otherwise keep running. It is a primary-key read of a row
    most namespaces have never created.

    The budget, the pause and the per-agent concurrency ceiling apply only to a
    session that belongs to a dispatch task. Human chat keeps the per-process
    ``TurnQuota`` above and never charges the dispatch row, which is what stops
    the *write* on this path from reaching every turn in the deployment.

    Every refusal below unwinds this transaction without committing, so a turn
    that does not start is not charged.
    """
    async with AsyncSessionLocal() as db:
        sessions = AgentSessionsService(db)
        row = await sessions.get_row_or_404(
            namespace_key=namespace_key, session_key=session_key
        )
        require_content_access(row, caller_hash=caller_hash, is_admin=is_admin, for_turn=True)
        _require_runnable_status(row.status, session_key=session_key)
        binding = await AgentRuntimesService(db).require_enabled_binding(
            namespace_key=namespace_key, agent_name=row.agent_name
        )

        # Level 3 is consulted for **every** turn, not only a task's. The copy
        # beside that button, the error hint it raises and the field
        # description on ``DispatchStateSnapshot`` all say it refuses every new
        # turn in the namespace, human chat included; checking it only on the
        # dispatch branch would leave every chat session that was already open
        # when somebody pressed it running turns, which is the one case an
        # operator reaching for the authoritative stop is trying to end. It is
        # a primary-key read of a row most namespaces do not have, and it takes
        # no lock - the charge below is what is expensive, and that stays
        # conditional.
        await require_executors_not_halted(
            db, namespace_key=namespace_key, action="Starting a turn"
        )

        if row.agent_task_id is not None:
            await charge_dispatch_turn(
                db,
                namespace_key=namespace_key,
                agent_name=row.agent_name,
                session_key=session_key,
                dispatch=dispatch_settings,
                executor=settings,
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
