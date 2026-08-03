"""Unit coverage for the milestone-scoped issue read.

Two things this file exists to pin down, and they are the two the plan calls
non-negotiable. The eligibility predicates live in Python and take no argument,
so a test can prove there is nothing to pass; and the skip counts are produced
by the bucketing rather than by the query, so a test can prove the rows were
seen and counted rather than filtered away.

Nothing here makes a network call. The clock is faked by swapping the module's
``time`` reference, the same trick ``test_linear_milestone_service.py`` uses,
so TTL and cooldown windows are exercised without sleeping.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging

import httpx
import pytest
from agent_control_models.linear import MilestonesStatus
from agent_control_server.services import linear_issues
from agent_control_server.services.linear_client import LinearError
from agent_control_server.services.linear_issues import (
    ELIGIBLE_STATE_TYPES,
    PAGE_CAP,
    HttpLinearIssueClient,
    LinearIssue,
    LinearMilestoneIssuesService,
    bucket_issues,
)

NS = "ns-one"
KEY = "OPS"
MILESTONE = "milestone-1"

SENTINEL_KEY = "lin_api_ISSUESERVICESENTINEL0123456789"
FAKE_LINEAR_URL = "https://linear.test/graphql"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(linear_issues, "time", fake)
    return fake


class FakeIssueClient:
    """Replays a scripted sequence of results and records every call."""

    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def fetch_milestone_issues(
        self, *, milestone_id: str, team_key: str
    ) -> tuple[list[LinearIssue], int, bool]:
        self.calls.append((milestone_id, team_key))
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, tuple)
        return result

    async def aclose(self) -> None:
        self.closed = True


def _issue(
    ref: str = "i1",
    *,
    state_type: str | None = "backlog",
    assignee_id: str | None = None,
) -> LinearIssue:
    return LinearIssue(
        ref=ref,
        identifier=f"OPS-{ref}",
        title="Write the deck",
        description="Body",
        url=f"https://linear.app/acme/issue/OPS-{ref}",
        created_at=None,
        updated_at=None,
        state_type=state_type,
        assignee_id=assignee_id,
        creator_id="u1",
        creator_display_name="paul",
    )


def _run(coro):
    return asyncio.run(coro)


# =============================================================================
# The two predicates, and the fact that nothing can turn either off
# =============================================================================


def test_only_backlog_and_unstarted_are_eligible() -> None:
    assert ELIGIBLE_STATE_TYPES == frozenset({"backlog", "unstarted"})


@pytest.mark.parametrize("state", ["started", "completed", "canceled", "triage"])
def test_work_a_human_has_started_is_never_offered(state: str) -> None:
    buckets = bucket_issues(
        [_issue(state_type=state)], other_team_count=0, beyond_page_cap=False
    )

    assert buckets.eligible == []
    assert buckets.skipped_started == 1


def test_an_unreadable_state_is_treated_as_started_rather_than_eligible() -> None:
    buckets = bucket_issues(
        [_issue(state_type=None)], other_team_count=0, beyond_page_cap=False
    )

    assert buckets.eligible == []
    assert buckets.skipped_started == 1


def test_an_assigned_issue_stays_that_persons() -> None:
    buckets = bucket_issues(
        [_issue(assignee_id="u9")], other_team_count=0, beyond_page_cap=False
    )

    assert buckets.eligible == []
    assert buckets.skipped_assigned == 1


def test_neither_predicate_takes_an_argument_that_could_loosen_it() -> None:
    """There is no flag to pass, so there is nothing for a request body to set."""

    params = set(inspect.signature(bucket_issues).parameters) - {"issues"}

    assert params == {"other_team_count", "beyond_page_cap"}


def test_skip_reasons_are_disjoint_and_started_wins() -> None:
    buckets = bucket_issues(
        [_issue(state_type="started", assignee_id="u9")],
        other_team_count=0,
        beyond_page_cap=False,
    )

    assert (buckets.skipped_started, buckets.skipped_assigned) == (1, 0)


def test_the_counts_account_for_every_row_the_read_saw() -> None:
    issues = [
        _issue("a"),
        _issue("b", state_type="started"),
        _issue("c", assignee_id="u9"),
    ]

    buckets = bucket_issues(issues, other_team_count=4, beyond_page_cap=True)

    assert buckets.fetched == 3
    assert len(buckets.eligible) + buckets.skipped_started + buckets.skipped_assigned == 3
    assert buckets.skipped_other_team == 4
    assert buckets.beyond_page_cap is True


def test_the_page_cap_is_a_constant_and_not_a_parameter() -> None:
    assert PAGE_CAP == 100
    assert "first" not in inspect.signature(
        HttpLinearIssueClient.fetch_milestone_issues
    ).parameters


# =============================================================================
# Caching, single-flight and back-off
# =============================================================================


def _service(client: object, **kwargs) -> LinearMilestoneIssuesService:
    return LinearMilestoneIssuesService(client=client, **kwargs)  # type: ignore[arg-type]


def test_no_api_key_reports_not_configured_without_calling_linear() -> None:
    service = _service(None)

    result = _run(
        service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
    )

    assert result.status is MilestonesStatus.NOT_CONFIGURED
    assert result.buckets.eligible == []


def test_a_second_read_inside_the_ttl_is_served_from_cache(clock: FakeClock) -> None:
    fake = FakeIssueClient(([_issue()], 0, False))
    service = _service(fake, ttl_seconds=60.0)

    async def scenario() -> tuple[bool, bool]:
        first = await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        second = await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        return first.cached, second.cached

    first_cached, second_cached = _run(scenario())

    assert (first_cached, second_cached) == (False, True)
    assert len(fake.calls) == 1


def test_the_read_happens_again_once_the_ttl_expires(clock: FakeClock) -> None:
    fake = FakeIssueClient(([_issue()], 0, False))
    service = _service(fake, ttl_seconds=60.0)

    async def scenario() -> None:
        await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        clock.advance(61.0)
        await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )

    _run(scenario())

    assert len(fake.calls) == 2


def test_concurrent_readers_of_one_milestone_make_one_request(clock: FakeClock) -> None:
    fake = FakeIssueClient(([_issue()], 0, False))
    service = _service(fake)

    async def scenario() -> None:
        await asyncio.gather(
            *(
                service.get_milestone_issues(
                    namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
                )
                for _ in range(5)
            )
        )

    _run(scenario())

    assert len(fake.calls) == 1


def test_a_failed_read_starts_a_cooldown_rather_than_calling_again(
    clock: FakeClock,
) -> None:
    fake = FakeIssueClient(LinearError("Linear could not be reached."))
    service = _service(fake, error_cooldown_seconds=30.0)

    async def scenario() -> tuple[MilestonesStatus, MilestonesStatus]:
        first = await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        second = await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        return first.status, second.status

    first_status, second_status = _run(scenario())

    assert first_status is MilestonesStatus.ERROR
    assert second_status is MilestonesStatus.ERROR
    assert len(fake.calls) == 1


def test_the_cooldown_covers_every_milestone_of_the_same_team(clock: FakeClock) -> None:
    fake = FakeIssueClient(LinearError("Linear could not be reached."))
    service = _service(fake)

    async def scenario() -> None:
        await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id="m-one"
        )
        await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id="m-two"
        )

    _run(scenario())

    assert len(fake.calls) == 1


def test_a_rate_limit_reports_the_wait_linear_asked_for(clock: FakeClock) -> None:
    fake = FakeIssueClient(
        LinearError("Linear is rate-limiting this server.", retry_after_seconds=12)
    )
    service = _service(fake)

    result = _run(
        service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
    )

    assert result.status is MilestonesStatus.ERROR
    assert result.retry_after_seconds == 12


def test_a_stale_set_is_never_served_in_place_of_an_error(clock: FakeClock) -> None:
    """The one place this differs from the milestone panel, deliberately.

    An hour-old board beats an error panel. An hour-old list of work to start
    does not: the person acting on it would be authorising a set that may
    already have moved.
    """

    fake = FakeIssueClient(
        ([_issue()], 0, False), LinearError("Linear could not be reached.")
    )
    service = _service(fake, ttl_seconds=60.0)

    async def scenario() -> linear_issues.MilestoneIssuesResult:
        await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        clock.advance(61.0)
        return await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )

    result = _run(scenario())

    assert result.status is MilestonesStatus.ERROR
    assert result.buckets.eligible == []


def test_a_successful_read_clears_an_expired_cooldown(clock: FakeClock) -> None:
    fake = FakeIssueClient(
        LinearError("Linear could not be reached."), ([_issue()], 0, False)
    )
    service = _service(fake, error_cooldown_seconds=30.0)

    async def scenario() -> MilestonesStatus:
        await service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
        clock.advance(31.0)
        return (
            await service.get_milestone_issues(
                namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
            )
        ).status

    assert _run(scenario()) is MilestonesStatus.OK


def test_a_read_with_nothing_eligible_is_empty_rather_than_ok(clock: FakeClock) -> None:
    fake = FakeIssueClient(([], 3, False))
    service = _service(fake)

    result = _run(
        service.get_milestone_issues(
            namespace_key=NS, linear_team_key=KEY, milestone_id=MILESTONE
        )
    )

    assert result.status is MilestonesStatus.EMPTY
    assert result.buckets.skipped_other_team == 3


# =============================================================================
# The transport: scope, cross-team counting, and the credential
# =============================================================================


def _transport_client(handler) -> HttpLinearIssueClient:
    return HttpLinearIssueClient(
        api_key=SENTINEL_KEY,
        api_url=FAKE_LINEAR_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _issue_node(ref: str, *, state: str = "backlog", assignee: dict | None = None) -> dict:
    return {
        "id": ref,
        "identifier": f"OPS-{ref}",
        "title": "Write the deck",
        "description": "Body",
        "url": "https://linear.app/acme/issue/OPS-1",
        "createdAt": "2026-08-01T14:56:49.290Z",
        "updatedAt": "2026-08-01T15:05:08.924Z",
        "state": {"type": state},
        "assignee": assignee,
        "creator": {"id": "u1", "displayName": "paul"},
        "labels": {"nodes": [{"name": "agent-ready"}]},
    }


def _handler_for(scoped_nodes: list[dict], team_nodes: list[dict]):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if "AgentControlMilestoneIssueTeams" in payload["query"]:
            return httpx.Response(200, json={"data": {"issues": {"nodes": team_nodes}}})
        return httpx.Response(200, json={"data": {"issues": {"nodes": scoped_nodes}}})

    return handler, seen


def test_both_the_milestone_and_the_team_key_are_sent_as_scope() -> None:
    handler, seen = _handler_for([_issue_node("a")], [{"team": {"key": KEY}}])
    client = _transport_client(handler)

    _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    scoped = next(p for p in seen if "AgentControlMilestoneIssueTeams" not in p["query"])
    assert scoped["variables"] == {
        "milestoneId": MILESTONE,
        "teamKey": KEY,
        "first": PAGE_CAP,
    }


def test_neither_state_nor_assignee_appears_in_the_graphql_filter() -> None:
    """You cannot count rows a filter removed, so they are not in the filter."""

    handler, seen = _handler_for([_issue_node("a")], [])
    client = _transport_client(handler)

    _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    scoped = next(p for p in seen if "AgentControlMilestoneIssueTeams" not in p["query"])
    # Everything before the selection set is argument and filter. Both fields
    # are selected, so finding either one ahead of `nodes {` would mean it had
    # become a filter and the rows it removes had become uncountable.
    arguments = scoped["query"].split("nodes {", 1)[0]
    assert "state" not in arguments
    assert "assignee" not in arguments
    assert "state { type }" in scoped["query"]
    assert "assignee { id }" in scoped["query"]


def test_issues_belonging_to_another_team_are_counted_and_never_read() -> None:
    handler, _ = _handler_for(
        [_issue_node("a")],
        [{"team": {"key": KEY}}, {"team": {"key": "ENG"}}, {"team": {"key": "EAR"}}],
    )
    client = _transport_client(handler)

    issues, other_team, at_cap = _run(
        client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY)
    )

    assert [issue.ref for issue in issues] == ["a"]
    assert other_team == 2
    assert at_cap is False


def test_a_row_whose_team_is_unreadable_counts_as_another_teams() -> None:
    handler, _ = _handler_for([], [{"team": None}])
    client = _transport_client(handler)

    _, other_team, _ = _run(
        client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY)
    )

    assert other_team == 1


def test_a_full_page_is_reported_rather_than_silently_truncated() -> None:
    handler, _ = _handler_for(
        [_issue_node(str(index)) for index in range(PAGE_CAP)], []
    )
    client = _transport_client(handler)

    _, _, at_cap = _run(
        client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY)
    )

    assert at_cap is True


def test_one_unreadable_row_is_skipped_rather_than_failing_the_page() -> None:
    handler, _ = _handler_for(
        [_issue_node("a"), {"title": "no id here"}, _issue_node("c")], []
    )
    client = _transport_client(handler)

    issues, _, _ = _run(
        client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY)
    )

    assert [issue.ref for issue in issues] == ["a", "c"]


def test_the_api_key_is_sent_once_and_never_lands_in_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == SENTINEL_KEY
        return httpx.Response(
            401, json={"errors": [{"message": f"bad key {SENTINEL_KEY}"}]}
        )

    client = _transport_client(handler)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LinearError) as excinfo:
            _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert SENTINEL_KEY not in str(excinfo.value)
    assert SENTINEL_KEY not in caplog.text


def test_an_upstream_graphql_error_becomes_hand_written_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errors": [{"message": "your query said milestoneId=secret"}]}
        )

    client = _transport_client(handler)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LinearError) as excinfo:
            _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert "secret" not in str(excinfo.value)
    assert "secret" not in caplog.text


def test_a_failure_on_either_request_fails_the_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "AgentControlMilestoneIssueTeams" in json.loads(request.content)["query"]:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"data": {"issues": {"nodes": []}}})

    client = _transport_client(handler)

    with pytest.raises(LinearError):
        _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))


# =============================================================================
# The failure taxonomy: typed, hand-written, and carrying nothing from upstream
# =============================================================================


def _always(response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    return handler


UPSTREAM_BODY = {
    "errors": [
        {
            "message": (
                f"authentication failed for key {SENTINEL_KEY} while querying "
                "milestone 3dcd106d for team OPS"
            )
        }
    ]
}
"""What an upstream failure might say. None of it may reach a caller or a log:
it quotes the request back, and the request carries the credential."""


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "Linear rejected the configured API key."),
        (403, "Linear rejected the configured API key."),
        (429, "Linear is rate-limiting this server."),
        (400, "Linear rejected the request."),
        (404, "Linear rejected the request."),
        (500, "Linear reported an internal error."),
        (503, "Linear reported an internal error."),
    ],
)
def test_every_upstream_status_becomes_hand_written_text(
    caplog: pytest.LogCaptureFixture, status: int, expected: str
) -> None:
    client = _transport_client(_always(httpx.Response(status, json=UPSTREAM_BODY)))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LinearError) as excinfo:
            _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert excinfo.value.message == expected
    assert SENTINEL_KEY not in str(excinfo.value)
    assert SENTINEL_KEY not in caplog.text
    assert "authentication failed" not in caplog.text
    assert "authentication failed" not in str(excinfo.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("no route to host"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_a_transport_failure_is_typed_and_names_nothing(
    caplog: pytest.LogCaptureFixture, failure: httpx.HTTPError
) -> None:
    """``str(exc)`` on an httpx error can carry the request URL, so it is dropped.

    Only the exception class name is logged. That rule keeps holding whatever
    later moves into the URL, which is the reason for stating it as a rule
    rather than as a judgement about today's URL.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        failure.request = request  # type: ignore[attr-defined]
        raise failure

    client = _transport_client(handler)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LinearError) as excinfo:
            _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert excinfo.value.message == "Linear could not be reached."
    assert excinfo.value.retry_after_seconds is None
    assert "linear.test" not in caplog.text
    assert "linear.test" not in str(excinfo.value)
    assert type(failure).__name__ in caplog.text


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "45"}, 45),
        ({"Retry-After": "45.9"}, 45),
        ({"Retry-After": " 12 "}, 12),
        ({"Retry-After": "next Tuesday"}, None),
        ({}, None),
    ],
)
def test_a_rate_limit_reports_only_a_number_it_could_read(
    headers: dict[str, str], expected: int | None
) -> None:
    client = _transport_client(
        _always(httpx.Response(429, json=UPSTREAM_BODY, headers=headers))
    )

    with pytest.raises(LinearError) as excinfo:
        _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert excinfo.value.retry_after_seconds == expected


def test_a_rate_limit_falls_back_to_linears_own_reset_header() -> None:
    import datetime as dt

    reset_at = dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=90)
    client = _transport_client(
        _always(
            httpx.Response(
                429,
                json={},
                headers={
                    "X-RateLimit-Requests-Reset": str(reset_at.timestamp() * 1000)
                },
            )
        )
    )

    with pytest.raises(LinearError) as excinfo:
        _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert excinfo.value.retry_after_seconds is not None
    assert 80 <= excinfo.value.retry_after_seconds <= 90


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>gateway</html>"),
        httpx.Response(200, json=["not", "a", "mapping"]),
    ],
)
def test_a_body_this_server_cannot_read_is_said_so_rather_than_guessed(
    response: httpx.Response,
) -> None:
    client = _transport_client(_always(response))

    with pytest.raises(LinearError) as excinfo:
        _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    assert excinfo.value.message == "Linear returned a response this server could not read."


def test_the_outbound_budget_cannot_be_raised_past_the_ceiling() -> None:
    """This runs while a database session is open. Ten seconds is the ceiling."""

    client = HttpLinearIssueClient(
        api_key=SENTINEL_KEY, api_url=FAKE_LINEAR_URL, timeout_seconds=600.0
    )

    assert client._timeout_seconds == linear_issues.MAX_OUTBOUND_SECONDS
    assert linear_issues.MAX_OUTBOUND_SECONDS == 10.0


def test_an_empty_api_key_is_refused_rather_than_sent_as_a_header() -> None:
    with pytest.raises(ValueError):
        HttpLinearIssueClient(api_key="", api_url=FAKE_LINEAR_URL)


def test_repeated_reads_of_unchanged_data_agree_on_order_and_digest() -> None:
    """``orderBy: updatedAt`` is what makes a digest over the refs worth having.

    Section 5.2 wants the same set to hash the same twice, so that a later phase
    can refuse a press against a set that moved. The read has to be stable for
    that to mean anything, so it is asserted here rather than assumed.
    """

    import hashlib

    nodes = [_issue_node(ref) for ref in ("c", "a", "b")]
    client = _transport_client(_handler_for(nodes, [{"team": {"key": KEY}}])[0])

    first, _, _ = _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))
    second, _, _ = _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))

    refs = [issue.ref for issue in first]
    assert refs == [issue.ref for issue in second] == ["c", "a", "b"]
    assert hashlib.sha256("\n".join(refs).encode()).hexdigest() == hashlib.sha256(
        "\n".join(issue.ref for issue in second).encode()
    ).hexdigest()


def test_a_page_at_the_cap_on_the_count_query_alone_still_warns() -> None:
    """The other-team query is unscoped, so it hits the cap first on a shared project."""

    client = _transport_client(
        _handler_for(
            [_issue_node("a")],
            [{"team": {"key": "ENG"}} for _ in range(PAGE_CAP)],
        )[0]
    )

    issues, other_team, at_cap = _run(
        client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY)
    )

    assert [issue.ref for issue in issues] == ["a"]
    assert other_team == PAGE_CAP
    assert at_cap is True


def test_the_credential_is_never_carried_on_a_result_either() -> None:
    """A successful read is checked too, not only the failures."""

    client = _transport_client(
        _handler_for([_issue_node("a")], [{"team": {"key": KEY}}])[0]
    )

    issues, _, _ = _run(
        client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY)
    )

    assert SENTINEL_KEY not in repr(issues)


def test_a_loop_over_invented_milestone_ids_does_not_grow_forever(
    clock: FakeClock,
) -> None:
    """``milestone_id`` is a path parameter, unlike the panel's cache key.

    A failing read never reaches the cache, so the cache's own eviction never
    runs, and the single-flight locks would be the thing that grew. The ceiling
    is the same one the cache uses.
    """

    fake = FakeIssueClient(LinearError("Linear could not be reached."))
    service = _service(fake, error_cooldown_seconds=0.0)

    async def scenario() -> None:
        for index in range(linear_issues._MAX_CACHE_ENTRIES * 2):
            await service.get_milestone_issues(
                namespace_key=NS, linear_team_key=KEY, milestone_id=f"m-{index}"
            )

    _run(scenario())

    assert len(service._locks) <= linear_issues._MAX_CACHE_ENTRIES
    assert len(service._cache) == 0


def test_a_flood_of_cached_reads_is_bounded_too(clock: FakeClock) -> None:
    fake = FakeIssueClient(([_issue()], 0, False))
    service = _service(fake, ttl_seconds=600.0)

    async def scenario() -> None:
        for index in range(linear_issues._MAX_CACHE_ENTRIES + 50):
            await service.get_milestone_issues(
                namespace_key=NS, linear_team_key=KEY, milestone_id=f"m-{index}"
            )

    _run(scenario())

    assert len(service._cache) <= linear_issues._MAX_CACHE_ENTRIES
    assert len(service._locks) <= linear_issues._MAX_CACHE_ENTRIES


@pytest.mark.parametrize(
    "assignee", [{}, {"name": "paul"}, {"id": None}, {"id": ""}, "not-an-object"]
)
def test_an_assignee_this_module_cannot_read_still_counts_as_assigned(
    assignee: object,
) -> None:
    """Unknown state is not eligibility, and neither is unknown assignee.

    ``assignee { id }`` is what the query asks for and Linear's schema makes
    that id non-null, so any of these is a row this module does not understand.
    Handing it to an agent would take work off somebody who had assigned it to
    themselves, which is the one override the plan promises always works.
    """

    handler, _ = _handler_for([_issue_node("a", assignee=assignee)], [])  # type: ignore[arg-type]
    client = _transport_client(handler)

    issues, _, _ = _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))
    buckets = bucket_issues(issues, other_team_count=0, beyond_page_cap=False)

    assert buckets.eligible == []
    assert buckets.skipped_assigned == 1


def test_an_absent_assignee_is_the_ordinary_eligible_case() -> None:
    handler, _ = _handler_for([_issue_node("a", assignee=None)], [])
    client = _transport_client(handler)

    issues, _, _ = _run(client.fetch_milestone_issues(milestone_id=MILESTONE, team_key=KEY))
    buckets = bucket_issues(issues, other_team_count=0, beyond_page_cap=False)

    assert [issue.ref for issue in buckets.eligible] == ["a"]
    assert buckets.skipped_assigned == 0
