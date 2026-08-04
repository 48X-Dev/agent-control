"""Every endpoint this tool calls, and the failure table behind them.

Three session routes - ``POST /api/v1/agent-sessions``, ``POST
/api/v1/agent-sessions/{key}/turns``, ``DELETE /api/v1/agent-sessions/{key}`` -
plus ``POST /api/v1/observability/events/query``, which is how a block is
detected and where :data:`TRACE_CORRELATION_NOTE` explains why the obvious
correlation key does not work.

And the dispatch ledger under ``/api/v1/agent-tasks``: import, list, claim,
heartbeat, steps, plan, finish. Those are the ones that make a claim mean
something when a second dispatcher exists, and every one of them is a request
about rows. None of them starts anything on the server.

``get_task_plan`` is the one that decides nothing here. It reads which agent
runs which hop, already resolved from server-side configuration, and reports
the hops that resolved to nobody rather than filling them in. This process does
not choose an agent.

Failure handling is section 11.3, in one table, in one place. The line that
matters most is the 504: **the invocation did not stop.** A retry there buys a
second concurrent invocation on an executor whose plugin has never been shown
to be concurrency-safe, on top of a first one that is still spending money.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import deque
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx
from agent_control_models.attachments import StepFilesSummary
from agent_control_models.dispatch import (
    DispatchStateSnapshot,
    GetDispatchStateResponse,
)
from agent_control_models.linear import ListMilestoneIssuesResponse
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.sessions import TurnResponse
from agent_control_models.tasks import (
    AgentTaskDetail,
    AgentTaskStepResponse,
    ClaimAgentTaskResponse,
    GetAgentTaskResponse,
    ImportAgentTasksResponse,
    ListAgentTasksResponse,
)
from agent_control_models.workflows import AgentTaskPlan, GetAgentTaskPlanResponse

from .sources.base import SourceItem

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

DEFAULT_STEP_TIMEOUT_SECONDS = 90.0
"""How long ``POST /agent-tasks/{key}/steps`` may take.

Longer than the 30-second client default because that route now fetches the
issue's files before it answers, under a server-side per-step budget that
defaults to 25 seconds across all of them. Shorter than a turn by a wide
margin: nothing behind this call runs a model, so a step that has not answered
in a minute and a half is a fault rather than a long job."""

DEFAULT_TURN_TIMEOUT_SECONDS = 300.0
DEFAULT_RETRY_AFTER_SECONDS = 60.0
"""Used only when the server sends no machine-readable delay.

It sends one now: every 429 on this path carries `retry_after_seconds` in the
problem body's `details` block and repeats it on the `Retry-After` header, so
this fallback covers an older server or a proxy that rewrote the response. It is
never a parsed hint - regexing a hand-written English sentence breaks the first
time somebody rewords it, and hints in that repo do get reworded."""

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
    FLEET_STOPPED = "fleet_stopped"
    """An operator threw one of the fleet switches, or the namespace has spent
    its hour. Distinct from ``PAUSED_QUOTA`` only in what the operator is told:
    both keep the task's slot and resume at the same step, and neither is a
    failure of the task. Reporting "quota exceeded" when somebody has pressed
    stop would send the next person looking at the wrong dial."""


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
    """Section 11.3, plus 401.

    Anything unlisted is ``FAILED``. Guessing that an unrecognised refusal is
    retryable is how a dispatcher hammers a server that has already said no.

    401 is the one addition, and it is an addition rather than a reading:
    section 11.3's table covers the turn path, where a bad credential fails at
    the first call and the operator is watching. On a poll loop nobody is
    watching, and ``FAILED`` there means one line at startup and then a process
    that looks alive and polls forever while every press of play sits in the
    queue. A key the server does not recognise is not going to start being
    recognised, which is what ``BLOCKED`` means.
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
        # The three fleet ceilings. All of them keep the task's slot and none is
        # the task's fault, so none is a failure: an operator who paused the
        # namespace expects to resume it and find the queue where they left it.
        case (429, "DISPATCH_BUDGET_EXCEEDED"):
            return Disposition.FLEET_STOPPED
        case (409, "DISPATCH_PAUSED") | (409, "EXECUTORS_HALTED"):
            return Disposition.FLEET_STOPPED
        # Another invocation is live against this agent. Not blocked, because
        # the configuration is fine, and not failed, because nothing was
        # attempted: waiting is the whole remedy.
        case (409, "AGENT_CONCURRENCY_EXCEEDED"):
            return Disposition.FLEET_STOPPED
        case (409, "TURN_IN_FLIGHT"):
            return Disposition.FAILED
        case (409, "AGENT_RUNTIME_NOT_BOUND"):
            return Disposition.BLOCKED
        case (409, "TEAM_NOT_LINKED"):
            return Disposition.BLOCKED
        case (403, "AUTH_INSUFFICIENT_PRIVILEGES"):
            return Disposition.BLOCKED
        case (401, _):
            return Disposition.BLOCKED
        case (429, _):
            return Disposition.PAUSED_QUOTA
        case (503, _):
            return Disposition.RETRY
        case _:
            return Disposition.FAILED


ATTRIBUTED_DENY_ID_CAP = 2048
"""How many already-attributed deny ids one client remembers.

The set only has to answer "did an earlier turn already claim this event", and
an event is only ever offered inside its own turn's time window, so the useful
memory is a handful of recent turns rather than the whole run. ``once`` exits
and never noticed the difference; ``serve`` runs for weeks, and an unbounded
set is one id per deny, forever, consulted on every deny query."""


class _BoundedIdSet:
    """A set that forgets its oldest entries rather than growing without end."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._order: deque[str] = deque()
        self._seen: set[str] = set()

    def __contains__(self, value: object) -> bool:
        return value in self._seen

    def update(self, values: Iterable[str]) -> None:
        for value in values:
            if value in self._seen:
                continue
            self._seen.add(value)
            self._order.append(value)
        while len(self._order) > self._cap:
            self._seen.discard(self._order.popleft())


class DispatchClient:
    """A thin, typed client over the session and ledger routes.

    One instance per run. It holds an ``httpx.AsyncClient`` and a set of
    already-attributed deny ids, and nothing else: the state two dispatchers
    contend for is a row in ``agent_tasks``, arbitrated by one statement inside
    Postgres, which is the only place it can be arbitrated at all.
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
        self._step_timeout = DEFAULT_STEP_TIMEOUT_SECONDS
        self._attributed_deny_ids: _BoundedIdSet = _BoundedIdSet(ATTRIBUTED_DENY_ID_CAP)
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

    async def create_session(
        self, *, agent_name: str, title: str, task_key: str | None = None
    ) -> str:
        """Open a session and return its key.

        ``task_key`` binds the session to the task's row, and it is not
        bookkeeping. That column is what lets the turn path tell a fleet turn
        from a human chat turn, so it is where a namespace budget, a dispatch
        pause and a kill switch become refusals *on the turn* rather than
        checks inside the process being budgeted. It is also what opens the
        session to oversight: a task's session has no human owner, so without
        it every non-admin operator is 403'd out of the step rail and out of
        halting one runaway task. Sending it is therefore part of the claim,
        not a nicety - a session that omits it is a fleet turn the control
        plane cannot recognise as one.
        """

        body: dict[str, Any] = {"agent_name": agent_name, "title": title}
        if task_key is not None:
            body["task_key"] = task_key
        payload = await self._request("POST", "/agent-sessions", json=body)
        session = payload["session"]
        return str(session["session_key"])

    async def start_turn(
        self,
        *,
        session_key: str,
        message: str,
        attachment_keys: Sequence[str] = (),
    ) -> TurnResponse:
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
                    json={
                        "message": message,
                        "attachment_keys": list(attachment_keys),
                    },
                    timeout=self._turn_timeout,
                )
            except DispatchHTTPError as exc:
                if exc.disposition is Disposition.RETRY and attempt < _UNAVAILABLE_RETRIES:
                    await asyncio.sleep(min(2.0**attempt, 8.0))
                    continue
                raise
            return TurnResponse.model_validate(payload)

    async def fetch_milestone_issues(
        self, *, team_slug: str, milestone_id: str
    ) -> ListMilestoneIssuesResponse:
        """Read one milestone's eligible issues and its skip counts.

        A GET, and the only route slice 2 adds. The server holds the Linear
        credential, applies the team scope and applies both eligibility
        predicates; this side cannot widen any of the three, which is the point
        of the read living there rather than here.

        Both segments are quoted rather than interpolated raw. httpx resolves
        ``..`` in a path before sending, so an id carrying dot segments would
        otherwise send this GET to a different route than the one named here.
        """

        payload = await self._request(
            "GET",
            f"/teams/{quote(team_slug, safe='')}"
            f"/milestones/{quote(milestone_id, safe='')}/issues",
        )
        return ListMilestoneIssuesResponse.model_validate(payload)

    async def delete_session(self, *, session_key: str) -> None:
        """Delete both halves of the session. Sessions are one per step."""

        await self._request("DELETE", f"/agent-sessions/{session_key}")

    # -- the dispatch ledger ---------------------------------------------
    #
    # Every method below is a request about rows. None of them starts
    # anything: the server has no loop, and the claim these calls contend for
    # is a single ``UPDATE ... RETURNING`` inside Postgres, which is the only
    # thing two dispatchers share.

    async def read_dispatch_state(self) -> DispatchStateSnapshot:
        """The namespace's stop switches and what is left of its hour.

        **Advisory, and this method exists to be advisory.** Reading it before a
        run stops the dispatcher opening sessions it cannot use and importing
        rows nobody will ever see move. It is not the ceiling: the ceiling is a
        refusal inside the server's ``_acquire_turn`` that this process cannot
        reach, which is the entire reason it was put there. A future change that
        makes this call the thing deciding whether work happens has moved the
        budget back into the process being budgeted.
        """

        payload = await self._request("GET", "/agent-dispatch")
        return GetDispatchStateResponse.model_validate(payload).state

    async def import_tasks(
        self,
        *,
        items: Sequence[SourceItem],
        source_kind: str,
        dry_run: bool,
        team_slug: str | None = None,
        workflow_key: str | None = None,
    ) -> ImportAgentTasksResponse:
        """Preview the set, then commit it against the digest of what came back.

        Two calls rather than one, and the second quotes the first. That is
        what makes the commit an agreement to a *set* rather than to a count:
        four items swapped for four different items between the two calls has
        the same count, a different digest, and is refused with 409
        ``SCOPE_CHANGED``.

        A ref that already has an open task is reported under
        ``skipped.already_queued`` and never re-created, and a ref whose task
        already finished is reported under ``skipped.already_worked`` and left
        alone. So running this twice over the same YAML file queues nothing the
        second time and spends nothing - re-running finished work is a decision
        somebody makes on purpose, not what a loop does by default.
        """

        scope = {
            "kind": "items",
            "source_kind": source_kind,
            "items": [
                {
                    "source_ref": item.ref,
                    "title": item.title,
                    "body": item.body,
                    "source_url": item.url,
                }
                for item in items
            ],
        }
        body: dict[str, Any] = {
            "scope": scope,
            "team_slug": team_slug,
            "workflow_key": workflow_key,
            "dry_run": dry_run,
            "mode": "preview",
        }
        preview = ImportAgentTasksResponse.model_validate(
            await self._request("POST", "/agent-tasks/import", json=body)
        )
        if not preview.eligible:
            return preview
        body["mode"] = "commit"
        body["expected_refs_digest"] = preview.refs_digest
        return ImportAgentTasksResponse.model_validate(
            await self._request("POST", "/agent-tasks/import", json=body)
        )

    async def list_tasks(
        self, *, status: str | None = None, limit: int = 100
    ) -> ListAgentTasksResponse:
        """A page of tasks, oldest first.

        This is the queue. Two dispatchers polling it get the same page in the
        same order, so both attempt the head and one wins every race: safe, and
        no faster.
        """

        params = [f"limit={limit}"]
        if status is not None:
            params.append(f"status={quote(status, safe='')}")
        payload = await self._request("GET", "/agent-tasks?" + "&".join(params))
        return ListAgentTasksResponse.model_validate(payload)

    async def get_task(self, *, task_key: str) -> AgentTaskDetail:
        payload = await self._request("GET", f"/agent-tasks/{quote(task_key, safe='')}")
        return GetAgentTaskResponse.model_validate(payload).task

    async def get_task_plan(self, *, task_key: str) -> AgentTaskPlan:
        """Which agents run this task's steps, resolved by the server.

        **This process does not choose an agent, and this method is why.** The
        plan comes back with each step's agent already decided from server-side
        configuration - the workflow step, then the team's default - and with
        the steps that resolved to nothing named in
        ``unresolved_step_indexes`` rather than filled in. A dispatcher that
        picked one would be putting agent selection in the process an operator
        started, which is one argument away from putting it somewhere an issue
        label can reach.

        The read is per claimed task rather than cached per workflow: a
        workflow rewritten between two tasks of one run should affect the
        second one, and a plan held in memory across a run is a plan that
        outlives the configuration it copied.
        """

        payload = await self._request(
            "GET", f"/agent-tasks/{quote(task_key, safe='')}/plan"
        )
        return GetAgentTaskPlanResponse.model_validate(payload).plan

    async def claim_task(
        self, *, task_key: str, instance_id: str
    ) -> ClaimAgentTaskResponse:
        """Take one task. A 409 means somebody else won; move on, do not retry."""

        payload = await self._request(
            "POST",
            f"/agent-tasks/{quote(task_key, safe='')}/claim",
            json={"instance_id": instance_id},
        )
        return ClaimAgentTaskResponse.model_validate(payload)

    async def heartbeat_task(self, *, task_key: str, instance_id: str) -> None:
        """Refresh the lease. Sent between steps, never during one."""

        await self._request(
            "POST",
            f"/agent-tasks/{quote(task_key, safe='')}/heartbeat",
            json={"instance_id": instance_id},
        )

    async def start_task_step(
        self,
        *,
        task_key: str,
        instance_id: str,
        step_index: int,
        agent_name: str,
        brief: str,
        session_key: str | None,
    ) -> StepFilesSummary | None:
        """Open the step row before the turn, so a death leaves a mark.

        The server fetches the issue's files inside this call and answers with
        what it found against what it stored. ``None`` means no fetch ran at
        all, which is not the same as a fetch that found nothing: this side
        renders the second and says nothing about the first.

        No URL crosses this boundary in either direction. What comes back is
        attachment keys and server-authored refusal codes, which is the whole
        reason the fetch is on that side.
        """

        payload = await self._request(
            "POST",
            f"/agent-tasks/{quote(task_key, safe='')}/steps",
            json={
                "instance_id": instance_id,
                "step_index": step_index,
                "agent_name": agent_name,
                "brief": brief,
                "session_key": session_key,
            },
            timeout=self._step_timeout,
        )
        return AgentTaskStepResponse.model_validate(payload).files

    async def finish_task_step(
        self,
        *,
        task_key: str,
        instance_id: str,
        step_index: int,
        status: str,
        output_text: str | None = None,
        turn_trace_id: str | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        """Close the step and move the task's counters, in one server transaction.

        The ordering rule - step row first, task row second - is enforced on
        that side rather than here, which is the point of the route existing.
        Two calls from this process could not have that property.
        """

        await self._request(
            "POST",
            f"/agent-tasks/{quote(task_key, safe='')}/steps/{step_index}/finish",
            json={
                "instance_id": instance_id,
                "status": status,
                "output_text": output_text,
                "turn_trace_id": turn_trace_id,
                "failure_code": failure_code,
                "failure_detail": failure_detail,
            },
        )

    async def finish_task(
        self,
        *,
        task_key: str,
        instance_id: str,
        status: str,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        await self._request(
            "POST",
            f"/agent-tasks/{quote(task_key, safe='')}/finish",
            json={
                "instance_id": instance_id,
                "status": status,
                "failure_code": failure_code,
                "failure_detail": failure_detail,
            },
        )

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
