"""Milestone-scoped issue reads, with the bucket counts a human decides on.

Plan section 5.2. Same shape as :mod:`.linear_client`, deliberately: the API
key is passed to a constructor, held in one attribute and written into exactly
one request header; error text raised from here is written by hand rather than
lifted from the upstream response; and an individual row this module cannot
read is skipped rather than failing the whole page.

**This module only reads.** There is no mutation, no comment, no state change
and no code path that could become one. Slice 2 has no write of any kind.

Two predicates are hard-coded here and no request field can turn either off:

* ``state.type in ('backlog', 'unstarted')``, so work a human has already
  started is never taken; and
* ``assignee is null``, so an issue assigned to a person stays theirs, which
  makes assigning an issue to yourself the cheapest possible override of
  anything this system would otherwise do to it.

Both are applied **in Python**, in :func:`bucket_issues`, and neither is in the
GraphQL filter. That is not an oversight. The confirm a person reads before
starting anything has to be able to say *"2 issues are assigned to a person and
were skipped"*, and you cannot count rows a filter removed.

Milestone id and team key *are* in the filter, because both are scope. That
leaves one thing the scoped read cannot see - how many issues in this milestone
belong to some other team - so a second, deliberately minimal request counts
them without pulling their text into this process. A Linear project shared
across three teams is the ordinary case, not the exotic one, and the count is
what the confirm names. Cross-team work needs that team's own run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from agent_control_models.linear import MilestonesStatus

from ..config import linear_settings
from .linear_client import LinearError

_logger = logging.getLogger(__name__)

PAGE_CAP = 100
"""Hard ceiling on rows per request, section 5.2. Not settable by a caller. A
read that comes back at the cap reports ``beyond_page_cap`` rather than
pretending it saw the whole milestone."""

ELIGIBLE_STATE_TYPES = frozenset({"backlog", "unstarted"})
"""Linear state types an issue may sit in and still be offered. Started,
completed, canceled and triage are all somebody's business already."""

MAX_OUTBOUND_SECONDS = 10.0
"""Hard budget on the outbound call. This runs on a request path holding a
database session, so a hanging Linear must not be able to hold that session for
a full request timeout. The two requests are issued together, so the budget is
the wall clock rather than the sum."""

_MAX_CACHE_ENTRIES = 512

# Verbatim from plan section 5.2. `state.type` and `assignee` are selected and
# bucketed in Python rather than filtered here; `creator` and `createdAt` are
# selected because an operator deciding whether to start cannot weigh a set
# whose provenance is hidden. `orderBy: updatedAt` gives a stable page, so
# repeated reads of unchanged data produce the same set in the same order.
_MILESTONE_ISSUES_QUERY = """
query AgentControlMilestoneIssues($milestoneId: ID!, $teamKey: String!, $first: Int!) {
  issues(
    first: $first
    orderBy: updatedAt
    filter: {
      projectMilestone: { id:  { eq: $milestoneId } }
      team:             { key: { eq: $teamKey } }
    }
  ) {
    nodes { id identifier title description url createdAt updatedAt
            state { type } assignee { id }
            creator { id displayName }
            labels { nodes { name } } }
  }
}
"""

# The other-team count, and nothing else. Selecting only `team { key }` means
# an issue this team is not allowed to work never has its title, body or author
# copied into this process, let alone into a response. The count is all the
# confirm needs to say "6 issues in this milestone belong to other teams".
_MILESTONE_ISSUE_TEAMS_QUERY = """
query AgentControlMilestoneIssueTeams($milestoneId: ID!, $first: Int!) {
  issues(
    first: $first
    orderBy: updatedAt
    filter: { projectMilestone: { id: { eq: $milestoneId } } }
  ) {
    nodes { team { key } }
  }
}
"""

_UNREACHABLE_MESSAGE = "Linear could not be reached."
_UNEXPECTED_SHAPE_MESSAGE = "Linear returned a response this server could not read."
_REJECTED_MESSAGE = "Linear rejected the request."
_UNAUTHORIZED_MESSAGE = "Linear rejected the configured API key."
_RATE_LIMITED_MESSAGE = "Linear is rate-limiting this server."
_UPSTREAM_FAILURE_MESSAGE = "Linear reported an internal error."


@dataclass(frozen=True)
class LinearIssue:
    """One issue as this server read it.

    ``labels`` is read and kept for a later phase's optional narrowing filter.
    It is a filter and never a selector: nothing in this system lets a label
    choose an agent, a workflow or a tool, because anybody who can file an
    issue can attach one.
    """

    ref: str
    identifier: str
    title: str
    description: str | None
    url: str | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None
    state_type: str | None
    assignee_id: str | None
    creator_id: str | None
    creator_display_name: str | None
    labels: tuple[str, ...] = ()
    assignee_unreadable: bool = False
    """An ``assignee`` object came back that this module could not read an id
    from. It counts as assigned, the same way an unreadable ``state`` counts as
    started: an unreadable row is never an eligible one, and assigning an issue
    to yourself has to keep working as the override even when the row around it
    is malformed."""


@dataclass(frozen=True)
class IssueBuckets:
    """What the read saw, split the way a person has to be shown it."""

    eligible: list[LinearIssue] = field(default_factory=list)
    fetched: int = 0
    skipped_started: int = 0
    skipped_assigned: int = 0
    skipped_other_team: int = 0
    beyond_page_cap: bool = False


@dataclass(frozen=True)
class MilestoneIssuesResult:
    """A scope read, including why it produced nothing."""

    status: MilestonesStatus
    buckets: IssueBuckets = field(default_factory=IssueBuckets)
    error: str | None = None
    retry_after_seconds: int | None = None
    cached: bool = False
    fetched_at: dt.datetime | None = None


def bucket_issues(
    issues: list[LinearIssue],
    *,
    other_team_count: int,
    beyond_page_cap: bool,
) -> IssueBuckets:
    """Split a scoped read into what may be worked and what was skipped, by reason.

    The two eligibility predicates live here, in Python, and nowhere else. They
    take no arguments on purpose: there is no parameter to pass, so there is
    nothing for a request body, a config file or a later refactor to loosen.

    Reasons are disjoint and counted once each. An issue that is both started
    and assigned counts under ``started``, because "a human is already working
    on this" is the stronger of the two statements and the confirm reads better
    for saying it.
    """

    eligible: list[LinearIssue] = []
    started = 0
    assigned = 0
    for issue in issues:
        if issue.state_type not in ELIGIBLE_STATE_TYPES:
            started += 1
            continue
        if issue.assignee_id is not None or issue.assignee_unreadable:
            assigned += 1
            continue
        eligible.append(issue)
    return IssueBuckets(
        eligible=eligible,
        fetched=len(issues),
        skipped_started=started,
        skipped_assigned=assigned,
        skipped_other_team=other_team_count,
        beyond_page_cap=beyond_page_cap,
    )


class LinearIssueClient(Protocol):
    """The narrow surface the service depends on, so tests fake an object."""

    async def fetch_milestone_issues(
        self, *, milestone_id: str, team_key: str
    ) -> tuple[list[LinearIssue], int, bool]:
        """Return this team's issues in the milestone, the other-team count, and
        whether either read came back at the page cap.

        Raises :class:`~.linear_client.LinearError` when the read fails.
        """
        ...

    async def aclose(self) -> None:
        """Release any transport the client owns."""
        ...


class HttpLinearIssueClient:
    """:class:`LinearIssueClient` over Linear's public GraphQL endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        timeout_seconds: float = MAX_OUTBOUND_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must not be empty")
        self._api_key = api_key
        self._api_url = api_url
        self._timeout_seconds = min(timeout_seconds, MAX_OUTBOUND_SECONDS)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self._timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_milestone_issues(
        self, *, milestone_id: str, team_key: str
    ) -> tuple[list[LinearIssue], int, bool]:
        """One scoped read plus one count, issued together.

        Together rather than in sequence so the 10-second budget is wall clock:
        this runs while a database session is open, and two sequential reads
        against a slow Linear would hold it for twenty.
        """

        scoped_task = asyncio.create_task(
            self._post(
                {
                    "query": _MILESTONE_ISSUES_QUERY,
                    "variables": {
                        "milestoneId": milestone_id,
                        "teamKey": team_key,
                        "first": PAGE_CAP,
                    },
                }
            )
        )
        teams_task = asyncio.create_task(
            self._post(
                {
                    "query": _MILESTONE_ISSUE_TEAMS_QUERY,
                    "variables": {"milestoneId": milestone_id, "first": PAGE_CAP},
                }
            )
        )
        await asyncio.wait({scoped_task, teams_task})
        # Both exceptions are retrieved before either is raised. Raising the
        # first one straight out of .result() would leave the other task's
        # exception unretrieved, which asyncio complains about at collection
        # time in a log line nobody can trace back to here.
        failures = [task.exception() for task in (scoped_task, teams_task)]
        for failure in failures:
            if failure is not None:
                raise failure
        scoped = scoped_task.result()
        teams = teams_task.result()

        issue_nodes = _nodes(_mapping(_mapping(scoped, "data"), "issues"))
        team_nodes = _nodes(_mapping(_mapping(teams, "data"), "issues"))

        issues = [issue for node in issue_nodes if (issue := _parse_issue(node)) is not None]
        other_team = sum(
            1
            for node in team_nodes
            if _optional_str(_mapping(node, "team").get("key")) != team_key
        )
        at_cap = len(issue_nodes) >= PAGE_CAP or len(team_nodes) >= PAGE_CAP
        return issues, other_team, at_cap

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._api_url,
                json=payload,
                headers={
                    # Personal API keys go in Authorization with no scheme
                    # prefix, the same contract linear_client.py documents.
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.HTTPError as exc:
            # Only the exception class is logged: httpx puts the request URL in
            # str(exc), and keeping upstream text out of our logs entirely is
            # the rule that stays true whatever moves into that URL later.
            _logger.warning("Linear issue read failed: %s", type(exc).__name__)
            raise LinearError(_UNREACHABLE_MESSAGE) from exc

        if response.status_code == 429:
            raise LinearError(
                _RATE_LIMITED_MESSAGE,
                retry_after_seconds=_retry_after_seconds(response),
            )
        if response.status_code in (401, 403):
            _logger.warning("Linear rejected the configured API key (%s).", response.status_code)
            raise LinearError(_UNAUTHORIZED_MESSAGE)
        if response.status_code >= 500:
            raise LinearError(_UPSTREAM_FAILURE_MESSAGE)
        if response.status_code >= 400:
            _logger.warning("Linear returned HTTP %s for an issue read.", response.status_code)
            raise LinearError(_REJECTED_MESSAGE)

        try:
            body = response.json()
        except ValueError as exc:
            raise LinearError(_UNEXPECTED_SHAPE_MESSAGE) from exc
        if not isinstance(body, dict):
            raise LinearError(_UNEXPECTED_SHAPE_MESSAGE)
        # GraphQL reports failures with HTTP 200, so this is what catches a
        # malformed query or a key without the scope to read issues. Upstream
        # messages are counted for an operator and replaced with fixed text,
        # because they can quote parts of the request back.
        errors = body.get("errors")
        if errors:
            if isinstance(errors, list):
                _logger.warning("Linear returned %d GraphQL error(s).", len(errors))
            raise LinearError(_REJECTED_MESSAGE)
        decoded: dict[str, Any] = body
        return decoded


@dataclass
class _CacheEntry:
    buckets: IssueBuckets
    stored_at_monotonic: float
    fetched_at: dt.datetime


@dataclass
class _Cooldown:
    until_monotonic: float
    message: str
    upstream_retry_after_seconds: int | None


class LinearMilestoneIssuesService:
    """Reads one milestone's issues, with caching, single-flight and back-off.

    The three protections are the ones ``LinearMilestoneService`` documents, for
    the same reason: an issue read with none of them, fired on every expand of
    every row, is an authenticated caller loop against a shared workspace rate
    limit, on a request path holding a database session.

    **One thing 5.2 asks for is not here.** The cooldown is keyed on
    ``(namespace_key, linear_team_key)`` but is this service's own, not shared
    with ``LinearMilestoneService``. Sharing it needs a change to that module,
    and until it happens a 429 earned by one reader does not back the other off.

    ``client`` is ``None`` when no API key is configured, which is how
    ``not_configured`` is reported without this class knowing what a credential
    looks like.
    """

    def __init__(
        self,
        *,
        client: LinearIssueClient | None,
        ttl_seconds: float = 60.0,
        error_cooldown_seconds: float = 30.0,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._error_cooldown_seconds = error_cooldown_seconds
        self._cache: dict[tuple[str, str, str], _CacheEntry] = {}
        self._cooldowns: dict[tuple[str, str], _Cooldown] = {}
        self._locks: dict[tuple[str, str, str], asyncio.Lock] = {}

    async def aclose(self) -> None:
        self._cache.clear()
        self._cooldowns.clear()
        self._locks.clear()
        if self._client is not None:
            await self._client.aclose()

    def invalidate(self, *, namespace_key: str, linear_team_key: str) -> None:
        """Drop every cached milestone read for one team.

        Nothing in slice 2 calls this: there is no write, so nothing this
        server does can make a cached read wrong. It exists because the first
        thing that mutates an issue has to be able to say so.
        """

        for key in [k for k in self._cache if k[0] == namespace_key and k[1] == linear_team_key]:
            del self._cache[key]

    async def get_milestone_issues(
        self, *, namespace_key: str, linear_team_key: str, milestone_id: str
    ) -> MilestoneIssuesResult:
        """Read one milestone's eligible issues for one team.

        A stale set is never served in place of an error here, which is the one
        place this differs from the milestone panel. An hour-old board beats an
        error panel; an hour-old set of work to start does not, because the
        person pressing would be authorising a list that may already have moved.
        """

        if self._client is None:
            return MilestoneIssuesResult(status=MilestonesStatus.NOT_CONFIGURED)

        key = (namespace_key, linear_team_key, milestone_id)
        fresh = self._fresh_entry(key)
        if fresh is not None:
            return _from_entry(fresh, cached=True)

        async with self._lock_for(key):
            fresh = self._fresh_entry(key)
            if fresh is not None:
                return _from_entry(fresh, cached=True)

            cooldown = self._active_cooldown((namespace_key, linear_team_key))
            if cooldown is not None:
                remaining = int(cooldown.until_monotonic - time.monotonic()) + 1
                reported = remaining if cooldown.upstream_retry_after_seconds is not None else None
                return MilestoneIssuesResult(
                    status=MilestonesStatus.ERROR,
                    error=cooldown.message,
                    retry_after_seconds=reported,
                )

            try:
                issues, other_team, at_cap = await self._client.fetch_milestone_issues(
                    milestone_id=milestone_id, team_key=linear_team_key
                )
            except LinearError as exc:
                self._start_cooldown((namespace_key, linear_team_key), exc)
                return MilestoneIssuesResult(
                    status=MilestonesStatus.ERROR,
                    error=exc.message,
                    retry_after_seconds=exc.retry_after_seconds,
                )

            buckets = bucket_issues(
                issues, other_team_count=other_team, beyond_page_cap=at_cap
            )
            return _from_entry(self._store(key, buckets), cached=False)

    def _fresh_entry(self, key: tuple[str, str, str]) -> _CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() - entry.stored_at_monotonic > self._ttl_seconds:
            del self._cache[key]
            return None
        return entry

    def _start_cooldown(self, key: tuple[str, str], exc: LinearError) -> None:
        """Stop calling Linear for this team for a while after a failed read.

        A 429 sets the wait; every other failure gets a fixed one, because an
        unreachable or hanging Linear otherwise costs a full outbound budget on
        every single request.
        """

        upstream = exc.retry_after_seconds
        seconds = float(upstream) if upstream is not None else self._error_cooldown_seconds
        self._cooldowns[key] = _Cooldown(
            until_monotonic=time.monotonic() + seconds,
            message=exc.message,
            upstream_retry_after_seconds=upstream,
        )

    def _active_cooldown(self, key: tuple[str, str]) -> _Cooldown | None:
        cooldown = self._cooldowns.get(key)
        if cooldown is None:
            return None
        if time.monotonic() >= cooldown.until_monotonic:
            del self._cooldowns[key]
            return None
        return cooldown

    def _store(self, key: tuple[str, str, str], buckets: IssueBuckets) -> _CacheEntry:
        self._cooldowns.pop((key[0], key[1]), None)
        entry = _CacheEntry(
            buckets=buckets,
            stored_at_monotonic=time.monotonic(),
            fetched_at=dt.datetime.now(tz=dt.UTC),
        )
        self._cache[key] = entry
        self._evict_if_oversized()
        return entry

    def _evict_if_oversized(self) -> None:
        while len(self._cache) > _MAX_CACHE_ENTRIES:
            oldest = min(self._cache, key=lambda k: self._cache[k].stored_at_monotonic)
            del self._cache[oldest]
            # A held lock is somebody's in-flight read; dropping it would let
            # the next caller start a second request for the same milestone.
            lock = self._locks.get(oldest)
            if lock is not None and not lock.locked():
                del self._locks[oldest]

    def _lock_for(self, key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            if len(self._locks) >= _MAX_CACHE_ENTRIES:
                self._drop_idle_locks()
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _drop_idle_locks(self) -> None:
        """Forget the locks nobody is holding.

        The milestone panel's service keys its locks on
        ``(namespace_key, linear_team_key)``, and both come from a real team, so
        that dictionary is bounded by how many teams exist. This one carries a
        ``milestone_id`` as well, and that is a path parameter: a caller asking
        for invented ids in a loop would otherwise add a lock per id and never
        give one back. The reads that grow it fastest are the ones that *fail*,
        because those never reach the cache eviction that cleans up alongside an
        entry.

        A lock with a waiter is a held lock, so ``locked()`` is the whole test:
        anything false here is an in-flight read of nobody's.
        """

        for key in [k for k, lock in self._locks.items() if not lock.locked()]:
            del self._locks[key]


def _from_entry(entry: _CacheEntry, *, cached: bool) -> MilestoneIssuesResult:
    status = MilestonesStatus.OK if entry.buckets.eligible else MilestonesStatus.EMPTY
    return MilestoneIssuesResult(
        status=status,
        buckets=entry.buckets,
        cached=cached,
        fetched_at=entry.fetched_at,
    )


def _parse_issue(node: dict[str, Any]) -> LinearIssue | None:
    """Read one row, or skip it.

    A row missing its id or identifier is dropped rather than failing the page:
    one issue with a surprising shape should not stop an operator seeing the
    other twelve. A row whose ``state`` is unreadable keeps ``state_type`` as
    ``None``, which buckets it as started - unknown state is not eligibility.
    An ``assignee`` that is present but unreadable is treated the same way, for
    the same reason: the query asks for ``assignee { id }`` and Linear's own
    schema makes that id non-null, so a row without one is a row this module
    does not understand, and it is not going to hand it to an agent.
    """

    ref = _optional_str(node.get("id"))
    identifier = _optional_str(node.get("identifier"))
    if ref is None or identifier is None:
        return None
    creator = _mapping(node, "creator")
    assignee = node.get("assignee")
    assignee_id = _optional_str(_mapping(node, "assignee").get("id"))
    return LinearIssue(
        ref=ref,
        identifier=identifier,
        title=_optional_str(node.get("title")) or identifier,
        description=_optional_str(node.get("description")),
        url=_optional_str(node.get("url")),
        created_at=_parse_datetime(node.get("createdAt")),
        updated_at=_parse_datetime(node.get("updatedAt")),
        state_type=_optional_str(_mapping(node, "state").get("type")),
        assignee_id=assignee_id,
        assignee_unreadable=assignee is not None and assignee_id is None,
        creator_id=_optional_str(creator.get("id")),
        creator_display_name=_optional_str(creator.get("displayName")),
        labels=tuple(
            name
            for label in _nodes(_mapping(node, "labels"))
            if (name := _optional_str(label.get("name"))) is not None
        ),
    )


def _mapping(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return nested if isinstance(nested, dict) else {}


def _nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _retry_after_seconds(response: httpx.Response) -> int | None:
    """How long Linear wants the server to wait, in seconds.

    Prefers ``Retry-After``; falls back to Linear's own reset header, which
    carries an absolute epoch timestamp in milliseconds.
    """

    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(0, int(float(raw.strip())))
        except ValueError:
            pass

    reset = response.headers.get("X-RateLimit-Requests-Reset")
    if reset is not None:
        try:
            reset_at = dt.datetime.fromtimestamp(float(reset) / 1000, tz=dt.UTC)
        except (ValueError, OSError, OverflowError):
            return None
        return max(0, int((reset_at - dt.datetime.now(tz=dt.UTC)).total_seconds()))
    return None


_service: LinearMilestoneIssuesService | None = None
_service_lock = threading.Lock()


def build_milestone_issues_service() -> LinearMilestoneIssuesService:
    """Construct a service from the process settings.

    An absent API key yields a service with no client rather than a failure,
    which is what makes ``not_configured`` an ordinary answer.
    """

    api_key = linear_settings.get_api_key()
    client = (
        HttpLinearIssueClient(
            api_key=api_key,
            api_url=linear_settings.api_url,
            timeout_seconds=linear_settings.timeout_seconds,
        )
        if api_key is not None
        else None
    )
    return LinearMilestoneIssuesService(
        client=client,
        ttl_seconds=linear_settings.cache_ttl_seconds,
        error_cooldown_seconds=linear_settings.error_cooldown_seconds,
    )


def get_milestone_issues_service() -> LinearMilestoneIssuesService:
    """FastAPI dependency returning the process-wide issue service.

    Built on first use, under a lock: FastAPI runs this in a worker thread, so
    two first requests can land here at once and the second must not build a
    second HTTP client that nothing would ever close.
    """

    global _service
    with _service_lock:
        if _service is None:
            _service = build_milestone_issues_service()
        return _service


async def shutdown_milestone_issues_service() -> None:
    """Close the process-wide service, if one was ever built."""

    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        await service.aclose()
