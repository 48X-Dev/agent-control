"""Google ADK implementation of :class:`ExecutorClient`.

=============================================================================
UNVERIFIED WIRE FORMAT - assumption A2 in ``docs/plans/orchestration-plan.md``
=============================================================================

Everything in the ``Wire`` section below - route templates, request body keys,
response body keys, and the event shape ``_parse_messages`` reads - was written
from the plan's description of ``adk api_server`` rather than from a running
one, and A2 has not been signed off.

Captured request/response pairs have since appeared in
``server/tests/fixtures/adk/`` and ``test_adk_executor_turn_wire.py`` now drives
this client against them, so the turn path's request body, response reading and
failure classification are pinned to those payloads instead of to nobody's
expectations. Two caveats that keep the marker in place. The captures were not
taken by the author of this module and their provenance is not verifiable from
inside the repo, so they are treated as the best available evidence rather than
as sign-off. And they cover ``/run`` and session reads only: whether the
executor *acts* on the state seeded under ``stateDelta`` (A7) is not observable
from a captured payload and is still claimed nowhere.

So this module is the blast radius. Every ADK-specific string and every field
name lives between the two ``Wire`` markers or inside the ``_parse_*``
functions directly below them. When A2 lands, correcting this file is the whole
correction: nothing above it names a route, a key, or an event field, and the
protocol in ``executor_client.py`` is executor-neutral by construction.

What is *not* provisional is the error handling. An executor runs arbitrary
agent code, so its response bodies can carry tracebacks, tool exception text,
model errors echoing the prompt, and internal paths. This module maps every
failure onto a hand-written constant from ``executor_client`` and logs status
codes and exception class names only. That discipline holds whatever A2 turns
out to say.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .executor_client import (
    EXECUTOR_MODEL_UNAVAILABLE_MESSAGE,
    EXECUTOR_REJECTED_MESSAGE,
    EXECUTOR_SESSION_MISSING_MESSAGE,
    EXECUTOR_TIMEOUT_MESSAGE,
    EXECUTOR_TURN_TIMEOUT_MESSAGE,
    EXECUTOR_UNAUTHORIZED_MESSAGE,
    EXECUTOR_UNREACHABLE_MESSAGE,
    EXECUTOR_UNREADABLE_MESSAGE,
    EXECUTOR_UPSTREAM_FAILURE_MESSAGE,
    PART_KIND_TEXT,
    PART_KIND_TOOL_CALL,
    PART_KIND_TOOL_RESULT,
    PART_KIND_UNSUPPORTED,
    ROLE_AGENT,
    ROLE_USER,
    ExecutorMessage,
    ExecutorMessagePart,
    ExecutorModelUnavailableError,
    ExecutorRejectedError,
    ExecutorSession,
    ExecutorSessionNotFoundError,
    ExecutorTurn,
    ExecutorTurnTimeoutError,
    ExecutorUnavailableError,
)
from .executor_metrics import (
    EXECUTOR_FAILURE_MODEL_UNAVAILABLE,
    EXECUTOR_FAILURE_REJECTED,
    EXECUTOR_FAILURE_SESSION_MISSING,
    EXECUTOR_FAILURE_TIMEOUT,
    EXECUTOR_FAILURE_TURN_TIMEOUT,
    EXECUTOR_FAILURE_UNAUTHORIZED,
    EXECUTOR_FAILURE_UNREACHABLE,
    EXECUTOR_FAILURE_UNREADABLE,
    EXECUTOR_FAILURE_UPSTREAM_ERROR,
    record_executor_failure,
)

_logger = logging.getLogger(__name__)


# =============================================================================
# Wire  -  BEGIN UNVERIFIED (A2). Correct this block, and nothing else.
# =============================================================================

_SESSION_PATH_TEMPLATE = "/apps/{app_name}/users/{user_id}/sessions/{session_id}"
"""Session create (POST), read (GET) and delete (DELETE) all address this."""

_HEALTH_PATH = "/list-apps"
"""Cheapest route that proves the process is up and serving. ADK ships no
dedicated health route, so this stands in for one."""

_RUN_PATH = "/run"
"""Blocking turn. Answers once the whole invocation is finished."""

_CREATE_BODY_IS_BARE_STATE = True
"""The create body IS the state map, not {"state": {...}}.

Captured against google-adk 2.6.1: the wrapped form is accepted with a 200 but
stores the map one level down, so seeded keys land at state["state"][k] and
every reader looking for state[k] finds nothing. See
server/tests/fixtures/adk/create_session_with_id.json (bare, flat) against
create_session_wrapped_state_GUESS.json (wrapped, nested)."""

_RUN_APP_NAME_KEY = "appName"
_RUN_USER_ID_KEY = "userId"
_RUN_SESSION_ID_KEY = "sessionId"
_RUN_NEW_MESSAGE_KEY = "newMessage"
_RUN_STREAMING_KEY = "streaming"
_RUN_STATE_DELTA_KEY = "stateDelta"
"""Per-turn state merged before the invocation runs.

This is the channel the per-turn trace id travels on, and whether the pinned
version accepts it at all is assumption A7. If it does not, the field is simply
ignored by the executor and the turn still runs: the trace id stays a
server-side identifier for the turn and the deep link into the agent's own
guardrail decisions is the thing that does not work. Nothing here fails closed
on it, because refusing to run a turn over a missing telemetry hop would be a
worse trade than a link that is not yet offered."""

_MODEL_UNAVAILABLE_STATUS = 429
"""The one upstream status a turn reads differently from every other call.

A 429 from a session-CRUD call is the executor itself pushing back. A 429 from a
turn is, in practice, an exhausted model quota arriving through it, and telling
an operator "the executor reported an internal error" would send them to look at
the wrong process. Which status codes and bodies ADK actually produces for a
missing or rejected model key is part of A2; everything not recognised here
falls through to the ordinary mapping, so a wrong guess degrades to a generic
message rather than to a wrong one."""

_SESSION_ID_KEY = "id"
_SESSION_APP_NAME_KEY = "appName"
_SESSION_USER_ID_KEY = "userId"
_SESSION_STATE_KEY = "state"
_SESSION_EVENTS_KEY = "events"

_EVENT_AUTHOR_KEY = "author"
_EVENT_TIMESTAMP_KEY = "timestamp"
_EVENT_CONTENT_KEY = "content"
_CONTENT_ROLE_KEY = "role"
_CONTENT_PARTS_KEY = "parts"
_PART_TEXT_KEY = "text"
_PART_FUNCTION_CALL_KEY = "functionCall"
_PART_FUNCTION_RESPONSE_KEY = "functionResponse"
_FUNCTION_NAME_KEY = "name"
_FUNCTION_ID_KEY = "id"
_FUNCTION_ARGS_KEY = "args"
_FUNCTION_RESPONSE_KEY = "response"

_ROLE_USER_VALUE = "user"
"""Content role an executor stamps on human turns. Anything else is treated as
agent output, which is the safe direction: a message wrongly attributed to the
agent is confusing, one wrongly attributed to the human looks like the operator
said something they did not.

**The role alone does not identify a human turn**, and the captured payloads in
``server/tests/fixtures/adk/`` are why. A tool *result* arrives as an event with
``role: "user"`` too, because that is how tool output is fed back to a model.
Reading role by itself therefore renders every tool result in the transcript as
something the operator typed, which is precisely the misattribution the
paragraph above calls the worse direction. So a message counts as the human's
only when its role says user *and* it carries nothing but text: tool traffic is
the agent's, whoever the wire says it came from."""

# =============================================================================
# Wire  -  END UNVERIFIED (A2).
# =============================================================================


@dataclass(frozen=True)
class _TimeoutKind:
    """Which sentence a timeout on this call deserves.

    Running out of time on a session read means a request achieved nothing.
    Running out of time on a turn means a model is still being called and the
    bill is still growing. Both are ``httpx.TimeoutException``; only the caller
    knows which one it asked for, so the caller says.
    """

    metric_kind: str
    message: str
    error_type: type[ExecutorUnavailableError]

    def build(self) -> ExecutorUnavailableError:
        return self.error_type(self.message)


_CALL_TIMEOUT = _TimeoutKind(
    metric_kind=EXECUTOR_FAILURE_TIMEOUT,
    message=EXECUTOR_TIMEOUT_MESSAGE,
    error_type=ExecutorUnavailableError,
)
_TURN_TIMEOUT = _TimeoutKind(
    metric_kind=EXECUTOR_FAILURE_TURN_TIMEOUT,
    message=EXECUTOR_TURN_TIMEOUT_MESSAGE,
    error_type=ExecutorTurnTimeoutError,
)


class AdkExecutorClient:
    """:class:`ExecutorClient` backed by one ``adk api_server`` process.

    One instance per agent binding, all sharing the connection pool of the
    ``httpx.AsyncClient`` handed to the constructor. The base URL is per-agent
    because the topology is one agent per executor process.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        shared_secret: str | None = None,
        shared_secret_header: str = "X-Agent-Control-Executor-Secret",
        owns_client: bool = False,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must not be empty")
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._owns_client = owns_client
        self._headers: dict[str, str] = {"Accept": "application/json"}
        if shared_secret:
            # Defence in depth only. ADK will not check this header; the
            # control that protects an executor is that its port is unpublished.
            self._headers[shared_secret_header] = shared_secret

    async def aclose(self) -> None:
        """Close the transport, if this client owns it."""
        if self._owns_client:
            await self._client.aclose()

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        state: Mapping[str, Any],
    ) -> ExecutorSession:
        """Create the executor-side conversation with seeded state."""
        response = await self._request(
            "POST",
            self._session_path(app_name, user_id, session_id),
            json=dict(state),
        )
        return _parse_session(
            _decode(response),
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )

    async def get_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> ExecutorSession:
        """Read the executor-side conversation and its messages."""
        response = await self._request(
            "GET", self._session_path(app_name, user_id, session_id)
        )
        return _parse_session(
            _decode(response),
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )

    async def delete_session(
        self, *, app_name: str, user_id: str, session_id: str
    ) -> None:
        """Delete the executor-side conversation.

        A session the executor has already lost is a success: the caller's
        intent was for it to be gone, and it is.
        """
        try:
            await self._request(
                "DELETE", self._session_path(app_name, user_id, session_id)
            )
        except ExecutorSessionNotFoundError:
            return

    async def run(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        message: str,
        state_delta: Mapping[str, Any] | None = None,
        timeout_seconds: float,
    ) -> ExecutorTurn:
        """Run one blocking turn and return only what it produced.

        The timeout is passed per call rather than taken from the shared client,
        because a turn is allowed minutes and session CRUD is allowed seconds,
        and one transport serves both.

        Two failures are told apart here that are indistinguishable further up.
        Running out of time means the invocation is *still running*, so it
        raises :class:`ExecutorTurnTimeoutError` and the caller must not treat
        the session as idle. A 429 is read as the executor's model refusing to
        answer rather than as the executor itself pushing back; see
        ``_MODEL_UNAVAILABLE_STATUS``.
        """
        body: dict[str, Any] = {
            _RUN_APP_NAME_KEY: app_name,
            _RUN_USER_ID_KEY: user_id,
            _RUN_SESSION_ID_KEY: session_id,
            _RUN_NEW_MESSAGE_KEY: {
                _CONTENT_ROLE_KEY: _ROLE_USER_VALUE,
                _CONTENT_PARTS_KEY: [{_PART_TEXT_KEY: message}],
            },
            _RUN_STREAMING_KEY: False,
        }
        if state_delta:
            body[_RUN_STATE_DELTA_KEY] = dict(state_delta)

        response = await self._request(
            "POST",
            _RUN_PATH,
            json=body,
            timeout=timeout_seconds,
            timeout_error=_TURN_TIMEOUT,
            model_unavailable_status=_MODEL_UNAVAILABLE_STATUS,
        )
        return ExecutorTurn(messages=_parse_messages(_decode_events(response)))

    async def health(self) -> None:
        """Probe the executor process.

        A 404 here is not a missing session - there is no session in a health
        probe. It means the process answered but does not serve this route,
        which is what a wrong or upgraded executor version looks like, so it is
        reported as a refusal rather than as a lost conversation.
        """
        try:
            await self._request("GET", _HEALTH_PATH)
        except ExecutorSessionNotFoundError as exc:
            raise ExecutorRejectedError(EXECUTOR_REJECTED_MESSAGE) from exc

    @staticmethod
    def _session_path(app_name: str, user_id: str, session_id: str) -> str:
        # Every segment is server-minted or admin-configured, but they are
        # quoted anyway: a colon in a namespace-prefixed user id has no
        # business being interpreted as anything but a literal.
        return _SESSION_PATH_TEMPLATE.format(
            app_name=quote(app_name, safe=""),
            user_id=quote(user_id, safe=""),
            session_id=quote(session_id, safe=""),
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
        timeout_error: _TimeoutKind = _CALL_TIMEOUT,
        model_unavailable_status: int | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        request_kwargs: dict[str, Any] = {"json": json, "headers": self._headers}
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        try:
            response = await self._client.request(method, url, **request_kwargs)
        except httpx.TimeoutException as exc:
            _logger.warning("Executor request timed out: %s", type(exc).__name__)
            record_executor_failure(timeout_error.metric_kind)
            raise timeout_error.build() from exc
        except httpx.HTTPError as exc:
            # Only the exception class is logged. ``str(exc)`` carries the URL,
            # and keeping executor text out of this server's logs entirely is
            # the rule that stays true when the URL later carries something
            # that matters.
            _logger.warning("Executor request failed: %s", type(exc).__name__)
            record_executor_failure(EXECUTOR_FAILURE_UNREACHABLE)
            raise ExecutorUnavailableError(EXECUTOR_UNREACHABLE_MESSAGE) from exc

        self._raise_for_status(
            response,
            method=method,
            path=path,
            model_unavailable_status=model_unavailable_status,
        )
        return response

    @staticmethod
    def _raise_for_status(
        response: httpx.Response,
        *,
        method: str,
        path: str,
        model_unavailable_status: int | None = None,
    ) -> None:
        status = response.status_code
        if status < 400:
            return
        if status == 404:
            record_executor_failure(EXECUTOR_FAILURE_SESSION_MISSING)
            raise ExecutorSessionNotFoundError(EXECUTOR_SESSION_MISSING_MESSAGE)
        if status in (401, 403):
            _logger.warning("Executor rejected this server's credentials (%s).", status)
            record_executor_failure(EXECUTOR_FAILURE_UNAUTHORIZED)
            raise ExecutorRejectedError(EXECUTOR_UNAUTHORIZED_MESSAGE)
        if model_unavailable_status is not None and status == model_unavailable_status:
            _logger.warning(
                "Executor could not call its model (HTTP %s for %s %s).",
                status,
                method,
                path,
            )
            record_executor_failure(EXECUTOR_FAILURE_MODEL_UNAVAILABLE)
            raise ExecutorModelUnavailableError(EXECUTOR_MODEL_UNAVAILABLE_MESSAGE)
        if status == 429 or status >= 500:
            _logger.warning(
                "Executor returned HTTP %s for %s %s.", status, method, path
            )
            record_executor_failure(EXECUTOR_FAILURE_UPSTREAM_ERROR)
            raise ExecutorUnavailableError(EXECUTOR_UPSTREAM_FAILURE_MESSAGE)
        _logger.warning("Executor returned HTTP %s for %s %s.", status, method, path)
        record_executor_failure(EXECUTOR_FAILURE_REJECTED)
        raise ExecutorRejectedError(EXECUTOR_REJECTED_MESSAGE)


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Read a JSON object body, or fail with fixed text."""
    if not response.content:
        return {}
    body = _decode_json(response)
    if not isinstance(body, dict):
        record_executor_failure(EXECUTOR_FAILURE_UNREADABLE)
        raise ExecutorRejectedError(EXECUTOR_UNREADABLE_MESSAGE)
    return body


def _decode_events(response: httpx.Response) -> list[Any]:
    """Read a turn's event list.

    A turn answers with a bare JSON array rather than an object, which is the
    one place these two shapes differ; an empty body is an empty turn rather
    than a fault, because an agent that produced nothing is a real and boring
    outcome.
    """
    if not response.content:
        return []
    body = _decode_json(response)
    if not isinstance(body, list):
        record_executor_failure(EXECUTOR_FAILURE_UNREADABLE)
        raise ExecutorRejectedError(EXECUTOR_UNREADABLE_MESSAGE)
    return body


def _decode_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        record_executor_failure(EXECUTOR_FAILURE_UNREADABLE)
        raise ExecutorRejectedError(EXECUTOR_UNREADABLE_MESSAGE) from exc


def _parse_session(
    body: dict[str, Any], *, app_name: str, user_id: str, session_id: str
) -> ExecutorSession:
    """Map an executor session body onto the neutral shape.

    The identifiers this server asked for win over the ones the body reports.
    They are the keys the local mapping row is written with, so trusting the
    response to rename them would be trusting an external service to decide
    which row it maps to.
    """
    del user_id  # Retained in the signature for symmetry with the call sites.
    state = body.get(_SESSION_STATE_KEY)
    return ExecutorSession(
        app_name=_optional_str(body.get(_SESSION_APP_NAME_KEY)) or app_name,
        user_id=_optional_str(body.get(_SESSION_USER_ID_KEY)) or "",
        session_id=_optional_str(body.get(_SESSION_ID_KEY)) or session_id,
        messages=_parse_messages(body.get(_SESSION_EVENTS_KEY)),
        state=state if isinstance(state, dict) else {},
    )


def _parse_messages(raw_events: Any) -> tuple[ExecutorMessage, ...]:
    """Flatten executor events into messages, in the order they arrived.

    An event that carries no content is skipped: executors emit bookkeeping
    events with no message in them, and rendering those as empty bubbles would
    make every transcript look broken.
    """
    if not isinstance(raw_events, list):
        return ()
    messages: list[ExecutorMessage] = []
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        content = event.get(_EVENT_CONTENT_KEY)
        if not isinstance(content, dict):
            continue
        parts = _parse_parts(content.get(_CONTENT_PARTS_KEY))
        if not parts:
            continue
        role = _role_of(_optional_str(content.get(_CONTENT_ROLE_KEY)), parts)
        messages.append(
            ExecutorMessage(
                role=role,
                author=_optional_str(event.get(_EVENT_AUTHOR_KEY)),
                timestamp=_parse_timestamp(event.get(_EVENT_TIMESTAMP_KEY)),
                parts=parts,
            )
        )
    return tuple(messages)


def _role_of(raw_role: str | None, parts: tuple[ExecutorMessagePart, ...]) -> str:
    """Decide whether a message is the human's, given its role and its parts.

    See ``_ROLE_USER_VALUE``: an executor stamps ``user`` on tool results as
    well as on human turns, so the role is necessary and not sufficient. Any
    tool part in the message makes it the agent's.
    """
    if raw_role != _ROLE_USER_VALUE:
        return ROLE_AGENT
    if any(part.kind != PART_KIND_TEXT for part in parts):
        return ROLE_AGENT
    return ROLE_USER


def _parse_parts(raw_parts: Any) -> tuple[ExecutorMessagePart, ...]:
    """Map content parts, keeping a placeholder for shapes this does not model."""
    if not isinstance(raw_parts, list):
        return ()
    parts: list[ExecutorMessagePart] = []
    for raw in raw_parts:
        if not isinstance(raw, dict):
            continue
        text = raw.get(_PART_TEXT_KEY)
        if isinstance(text, str) and text:
            parts.append(ExecutorMessagePart(kind=PART_KIND_TEXT, text=text))
            continue

        call = raw.get(_PART_FUNCTION_CALL_KEY)
        if isinstance(call, dict):
            args = call.get(_FUNCTION_ARGS_KEY)
            parts.append(
                ExecutorMessagePart(
                    kind=PART_KIND_TOOL_CALL,
                    tool_name=_optional_str(call.get(_FUNCTION_NAME_KEY)),
                    tool_call_id=_optional_str(call.get(_FUNCTION_ID_KEY)),
                    arguments=args if isinstance(args, dict) else None,
                )
            )
            continue

        result = raw.get(_PART_FUNCTION_RESPONSE_KEY)
        if isinstance(result, dict):
            payload = result.get(_FUNCTION_RESPONSE_KEY)
            parts.append(
                ExecutorMessagePart(
                    kind=PART_KIND_TOOL_RESULT,
                    tool_name=_optional_str(result.get(_FUNCTION_NAME_KEY)),
                    tool_call_id=_optional_str(result.get(_FUNCTION_ID_KEY)),
                    result=payload if isinstance(payload, dict) else None,
                )
            )
            continue

        parts.append(ExecutorMessagePart(kind=PART_KIND_UNSUPPORTED))
    return tuple(parts)


def _parse_timestamp(value: Any) -> dt.datetime | None:
    """Read an event timestamp, tolerating epoch seconds or an ISO string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
        except (ValueError, OSError, OverflowError):
            return None
    if isinstance(value, str) and value:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
