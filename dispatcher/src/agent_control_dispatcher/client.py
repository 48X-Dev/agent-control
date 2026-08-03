"""The three endpoints this slice calls, and the failure table behind them.

``POST /api/v1/agent-sessions``, ``POST /api/v1/agent-sessions/{key}/turns``,
``DELETE /api/v1/agent-sessions/{key}``. All three already exist and none of
them needed a server change, which is most of why slice 1 is a week rather than
a phase.

A fourth read, ``POST /api/v1/observability/events/query``, is how a block is
detected. It is not a new endpoint either, and :data:`TRACE_CORRELATION_NOTE`
says why the obvious correlation key does not work.

Failure handling is section 11.3, in one table, in one place. The line that
matters most is the 504: **the invocation did not stop.** A retry there buys a
second concurrent invocation on an executor whose plugin has never been shown
to be concurrency-safe, on top of a first one that is still spending money.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from enum import StrEnum
from typing import Any

import httpx
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.sessions import TurnResponse

TRACE_CORRELATION_NOTE = """\
`TurnResponse.trace_id` does not correlate with control-execution events.
Observed 2026-08-02: a turn returned trace ac554b66..., its deny was recorded
under trace 4a6a4583..., and GET /observability/traces/<turn trace> answered
404 "has no recorded control executions". The server mints the turn's trace and
the executor mints its own; the TurnResponse docstring already calls the
carry-over unverified, and it does not happen here.

Deny evidence is therefore correlated by (agent_name, time window) using the
turn's own started_at/completed_at. That is sound for one turn at a time
against one agent, which is the concurrency this slice runs at
(max_concurrent_tasks_per_agent is 1, section 9.1), and it is not sound for
anything wider. Whatever replaces it wants a real correlation key.\
"""

DENY_INGESTION_LAG_NOTE = """\
Deny events are not readable at the moment the turn returns, and the lag is
seconds rather than milliseconds. Observed 2026-08-02: a blocked turn returned
at 23:43:51 with its deny carrying timestamp 23:43:47, and the row was still
invisible to a query four seconds later - it surfaced only in the next task's
query. The cause is not a mystery: the SDK batches events and ships them on a
timer, `AGENT_CONTROL_FLUSH_INTERVAL`, default 5.0 seconds
(`sdks/python/src/agent_control/observability.py`), so the write is behind the
HTTP response by up to a flush plus a round trip.

Two consequences the dispatcher has to live with. It waits out the flush before
concluding a turn was not blocked, which costs the settle window on every clean
task. And a deny that lands late gets attributed to whichever turn's window is
open when it finally appears, which is why events already attributed to an
earlier turn in this run are never attributed to a later one.

Absence still proves nothing. This is how a step is *classified*, never how
anything is enforced.\
"""

DENY_SETTLE_SECONDS = 10.0
"""How long to wait for a deny to become visible before concluding none is.

Two flush intervals plus room for the round trip. It costs nothing on a blocked
turn, because the loop returns the moment an event appears, and it costs the
full window on every clean one. That is the price of not silently reporting a
refusal as a finding."""

DEFAULT_TURN_TIMEOUT_SECONDS = 300.0
DEFAULT_RETRY_AFTER_SECONDS = 60.0
"""Used only when the server sends no machine-readable delay, which today is
always: section 11.4's `extra_details={"retry_after_seconds": ...}` is a Phase 2
server change that has not been made. The number is read when it appears."""

_UNAVAILABLE_RETRIES = 3
_DENY_WINDOW_SLACK = dt.timedelta(seconds=2)
"""Clock skew between the executor host and the server, nothing more. It was
five seconds and that was wide enough to pull the previous task's deny into
this task's window - observed, and the reason ``_attributed_deny_ids`` exists
as well."""


class Disposition(StrEnum):
    """What the dispatcher does about a failure, per section 11.3."""

    FAILED = "failed"
    RETRY = "retry"
    PAUSED_QUOTA = "paused_quota"
    BLOCKED = "blocked"
    RUNNING_UNKNOWN = "running_unknown"


class DispatchHTTPError(Exception):
    """A refusal from the server, classified.

    ``detail`` is the server's written message. It is shown to the operator and
    never parsed: section 11.4 is about exactly the failure mode of regexing a
    hand-written English sentence.
    """

    def __init__(
        self,
        *,
        disposition: Disposition,
        status_code: int,
        error_code: str | None,
        detail: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"HTTP {status_code} {error_code or '-'}: {detail}")
        self.disposition = disposition
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds


def classify(status_code: int, error_code: str | None) -> Disposition:
    """Section 11.3, verbatim.

    Anything unlisted is ``FAILED``. Guessing that an unrecognised refusal is
    retryable is how a dispatcher hammers a server that has already said no.
    """

    match (status_code, error_code):
        case (504, _):
            return Disposition.RUNNING_UNKNOWN
        case (502, "EXECUTOR_REJECTED"):
            return Disposition.FAILED
        case (503, "EXECUTOR_UNAVAILABLE"):
            return Disposition.RETRY
        case (429, "QUOTA_EXCEEDED"):
            return Disposition.PAUSED_QUOTA
        case (409, "TURN_IN_FLIGHT"):
            return Disposition.FAILED
        case (409, "AGENT_RUNTIME_NOT_BOUND"):
            return Disposition.BLOCKED
        case (403, "AUTH_INSUFFICIENT_PRIVILEGES"):
            return Disposition.BLOCKED
        case (429, _):
            return Disposition.PAUSED_QUOTA
        case (503, _):
            return Disposition.RETRY
        case _:
            return Disposition.FAILED


class DispatchClient:
    """A thin, typed client over the three session endpoints.

    One instance per run. It holds an ``httpx.AsyncClient`` and nothing else;
    there is no state here that a second dispatcher would contend with, because
    all the contended state is in :mod:`agent_control_dispatcher.ledger` and
    that file is honest about being local.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        turn_timeout_seconds: float = DEFAULT_TURN_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_root = base_url.rstrip("/") + "/api/v1"
        self._turn_timeout = turn_timeout_seconds
        self._attributed_deny_ids: set[str] = set()
        self._client = httpx.AsyncClient(
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0, read=turn_timeout_seconds),
            transport=transport,
        )

    async def __aenter__(self) -> DispatchClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_session(self, *, agent_name: str, title: str) -> str:
        """Open a session and return its key."""

        payload = await self._request(
            "POST", "/agent-sessions", json={"agent_name": agent_name, "title": title}
        )
        session = payload["session"]
        return str(session["session_key"])

    async def start_turn(self, *, session_key: str, message: str) -> TurnResponse:
        """Run one turn to completion.

        ``EXECUTOR_UNAVAILABLE`` is the only status retried, three attempts,
        because nothing reached the executor. A read timeout is surfaced as
        ``RUNNING_UNKNOWN`` and never retried: locally it looks the same as a
        504, and a 504 means the agent is still running.
        """

        attempt = 0
        while True:
            attempt += 1
            try:
                payload = await self._request(
                    "POST",
                    f"/agent-sessions/{session_key}/turns",
                    json={"message": message},
                    timeout=self._turn_timeout,
                )
            except DispatchHTTPError as exc:
                if exc.disposition is Disposition.RETRY and attempt < _UNAVAILABLE_RETRIES:
                    await asyncio.sleep(min(2.0**attempt, 8.0))
                    continue
                raise
            return TurnResponse.model_validate(payload)

    async def delete_session(self, *, session_key: str) -> None:
        """Delete both halves of the session. Sessions are one per step."""

        await self._request("DELETE", f"/agent-sessions/{session_key}")

    async def deny_events_for_turn(
        self,
        *,
        agent_name: str,
        turn: TurnResponse,
        settle_seconds: float = DENY_SETTLE_SECONDS,
        poll_interval_seconds: float = 0.5,
    ) -> list[ControlExecutionEvent]:
        """Deny events plausibly belonging to this turn.

        Two things make this harder than it looks, both observed rather than
        assumed. :data:`TRACE_CORRELATION_NOTE` says why it is a time window
        and not a join on ``trace_id``. :data:`DENY_INGESTION_LAG_NOTE` says
        why it polls: the deny is written to the event store *after* the turn
        response comes back, so asking once, immediately, reliably finds
        nothing.

        An empty result means "no deny was visible within ``settle_seconds``".
        It does not mean the turn was not blocked, and no caller should render
        it as if it did.
        """

        body: dict[str, Any] = {
            "agent_name": agent_name,
            "actions": ["deny"],
            "matched": True,
            "start_time": _iso(turn.started_at - _DENY_WINDOW_SLACK),
            "end_time": _iso(turn.completed_at + _DENY_WINDOW_SLACK),
            "limit": 20,
        }
        deadline = asyncio.get_running_loop().time() + settle_seconds
        while True:
            payload = await self._request("POST", "/observability/events/query", json=body)
            events = [
                event
                for row in payload.get("events", [])
                if (event := ControlExecutionEvent.model_validate(row)).control_execution_id
                not in self._attributed_deny_ids
            ]
            if events or asyncio.get_running_loop().time() >= deadline:
                self._attributed_deny_ids.update(
                    event.control_execution_id for event in events
                )
                return events
            await asyncio.sleep(poll_interval_seconds)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                f"{self._api_root}{path}",
                json=json,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.ReadTimeout as exc:
            raise DispatchHTTPError(
                disposition=Disposition.RUNNING_UNKNOWN,
                status_code=504,
                error_code=None,
                detail=(
                    f"No answer within {timeout or self._turn_timeout:.0f}s. The invocation "
                    "did not stop; it is still running and still spending. Not retried."
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise DispatchHTTPError(
                disposition=Disposition.RETRY,
                status_code=503,
                error_code="EXECUTOR_UNAVAILABLE",
                detail=f"Cannot reach {self._api_root}: {exc}",
            ) from exc

        if response.is_success:
            decoded: dict[str, Any] = response.json()
            return decoded
        raise _from_response(response)


def _from_response(response: httpx.Response) -> DispatchHTTPError:
    try:
        body: dict[str, Any] = response.json()
    except ValueError:
        body = {}
    error_code = body.get("error_code")
    detail = body.get("detail") or response.text.strip() or response.reason_phrase
    return DispatchHTTPError(
        disposition=classify(response.status_code, error_code),
        status_code=response.status_code,
        error_code=error_code,
        detail=str(detail),
        retry_after_seconds=_retry_after(response, body),
    )


def _retry_after(response: httpx.Response, body: dict[str, Any]) -> float | None:
    """Prefer a machine-readable delay; fall back to the header; never to prose."""

    for container in (body.get("extra_details"), body.get("details")):
        if isinstance(container, dict):
            value = container.get("retry_after_seconds")
            if isinstance(value, int | float):
                return float(value)
    header = response.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            return None
    return None


def _iso(moment: dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat()
