"""HTTP coverage for ``GET /api/v1/teams/{slug}/milestones/{id}/issues``.

Runs the real route against real Postgres with a substituted issue service.
Nothing here makes a network call, and nothing here writes: the route is a GET
and the module behind it has no mutation.

The refusals are the point of the file. A team with no ``linear_team_key`` is a
409 rather than an empty 200, because an empty list reads as "nothing to do";
and no request field reaches either eligibility predicate, which is checked by
throwing the obvious ones at the route and watching the counts not move.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from agent_control_models.linear import MilestonesStatus
from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.services.linear_issues import (
    IssueBuckets,
    LinearIssue,
    LinearMilestoneIssuesService,
    MilestoneIssuesResult,
    get_milestone_issues_service,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_TEAMS_URL = "/api/v1/teams"
MILESTONE = "3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6"


class FakeIssuesService:
    """Returns a scripted result and records the scope it was asked for."""

    def __init__(self, result: MilestoneIssuesResult) -> None:
        self._result = result
        self.calls: list[tuple[str, str, str]] = []

    async def get_milestone_issues(
        self, *, namespace_key: str, linear_team_key: str, milestone_id: str
    ) -> MilestoneIssuesResult:
        self.calls.append((namespace_key, linear_team_key, milestone_id))
        return self._result


@pytest.fixture
def use_service(app: FastAPI) -> Iterator[Callable[[object], None]]:
    def _install(service: object) -> None:
        app.dependency_overrides[get_milestone_issues_service] = lambda: service

    yield _install
    app.dependency_overrides.pop(get_milestone_issues_service, None)


def _issue(ref: str = "uuid-1", identifier: str = "OPS-2") -> LinearIssue:
    return LinearIssue(
        ref=ref,
        identifier=identifier,
        title="Clive to review the deck",
        description="Owner noted in request: Clive.",
        url=f"https://linear.app/acme/issue/{identifier}",
        created_at=dt.datetime(2026, 8, 1, 14, 56, tzinfo=dt.UTC),
        updated_at=dt.datetime(2026, 8, 1, 15, 5, tzinfo=dt.UTC),
        state_type="backlog",
        assignee_id=None,
        creator_id="c087560f",
        creator_display_name="paul",
        labels=("agent-ready",),
    )


def _ok_result(
    *issues: LinearIssue,
    started: int = 0,
    assigned: int = 0,
    other_team: int = 0,
    beyond_page_cap: bool = False,
) -> MilestoneIssuesResult:
    return MilestoneIssuesResult(
        status=MilestonesStatus.OK if issues else MilestonesStatus.EMPTY,
        buckets=IssueBuckets(
            eligible=list(issues),
            fetched=len(issues) + started + assigned,
            skipped_started=started,
            skipped_assigned=assigned,
            skipped_other_team=other_team,
            beyond_page_cap=beyond_page_cap,
        ),
        fetched_at=dt.datetime(2026, 8, 3, 8, 19, tzinfo=dt.UTC),
    )


def _create_team(
    client: TestClient,
    *,
    display_name: str = "Operations",
    linear_team_key: str | None = None,
) -> str:
    body: dict[str, Any] = {"display_name": display_name}
    if linear_team_key is not None:
        body["linear_team_key"] = linear_team_key
    resp = client.put(_TEAMS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["slug"])


def _issues(client: TestClient, slug: str, query: str = "") -> httpx.Response:
    return client.get(f"{_TEAMS_URL}/{slug}/milestones/{MILESTONE}/issues{query}")


# =============================================================================
# The refusal
# =============================================================================


def test_a_team_with_no_linear_key_is_refused_rather_than_answered_empty(
    client: TestClient, use_service
) -> None:
    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)
    slug = _create_team(client, display_name="Marketing")

    resp = _issues(client, slug)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["error_code"] == "TEAM_NOT_LINKED"
    assert slug in body["detail"]
    assert "linear_team_key" in body["hint"]
    # And Linear was never asked, because there was no scope to ask about.
    assert fake.calls == []


def test_an_unknown_team_is_a_404_before_any_read(
    client: TestClient, use_service
) -> None:
    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)

    resp = _issues(client, "no-such-team")

    assert resp.status_code == 404, resp.text
    assert fake.calls == []


# =============================================================================
# Scope, and what a caller cannot change about it
# =============================================================================


def test_the_read_is_scoped_to_the_teams_own_linear_key(
    client: TestClient, use_service
) -> None:
    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)
    slug = _create_team(client, linear_team_key="OPS")

    body = _issues(client, slug).json()

    assert body["linear_team_key"] == "OPS"
    assert [call[1] for call in fake.calls] == ["OPS"]
    assert [call[2] for call in fake.calls] == [MILESTONE]


@pytest.mark.parametrize(
    "query",
    [
        "?include_started=true",
        "?state=started",
        "?assignee=any",
        "?include_assigned=true",
        "?first=1000",
        "?linear_team_key=ENG",
        "?team_key=ENG",
    ],
)
def test_no_request_field_loosens_the_scope_or_the_predicates(
    client: TestClient, use_service, query: str
) -> None:
    fake = FakeIssuesService(_ok_result(_issue(), started=1, assigned=2))
    use_service(fake)
    slug = _create_team(client, linear_team_key="OPS")

    body = _issues(client, slug, query).json()

    assert body["counts"]["eligible"] == 1
    assert body["counts"]["skipped"] == {"started": 1, "assigned": 2, "other_team": 0}
    assert [call[1] for call in fake.calls] == ["OPS"]


@pytest.mark.parametrize("method", ["post", "patch", "put", "delete"])
def test_the_route_is_a_read_and_nothing_else(
    client: TestClient, use_service, method: str
) -> None:
    use_service(FakeIssuesService(_ok_result(_issue())))
    slug = _create_team(client, linear_team_key="OPS")

    resp = getattr(client, method)(f"{_TEAMS_URL}/{slug}/milestones/{MILESTONE}/issues")

    assert resp.status_code == 405


# =============================================================================
# What the counts say
# =============================================================================


def test_cross_team_issues_are_counted_and_named_but_never_listed(
    client: TestClient, use_service
) -> None:
    use_service(FakeIssuesService(_ok_result(_issue(), other_team=6)))
    slug = _create_team(client, linear_team_key="OPS")

    body = _issues(client, slug).json()

    assert body["counts"]["skipped"]["other_team"] == 6
    assert len(body["issues"]) == 1
    assert body["issues"][0]["identifier"] == "OPS-2"


def test_a_full_page_is_reported_rather_than_hidden(
    client: TestClient, use_service
) -> None:
    use_service(FakeIssuesService(_ok_result(_issue(), beyond_page_cap=True)))
    slug = _create_team(client, linear_team_key="OPS")

    assert _issues(client, slug).json()["counts"]["beyond_page_cap"] is True


def test_nothing_eligible_is_empty_rather_than_an_error(
    client: TestClient, use_service
) -> None:
    use_service(FakeIssuesService(_ok_result(started=3)))
    slug = _create_team(client, linear_team_key="OPS")

    body = _issues(client, slug).json()

    assert body["status"] == "empty"
    assert body["issues"] == []
    assert body["counts"]["skipped"]["started"] == 3


def test_an_unreachable_linear_is_a_200_carrying_an_error_status(
    client: TestClient, use_service
) -> None:
    use_service(
        FakeIssuesService(
            MilestoneIssuesResult(
                status=MilestonesStatus.ERROR,
                error="Linear could not be reached.",
                retry_after_seconds=12,
            )
        )
    )
    slug = _create_team(client, linear_team_key="OPS")

    resp = _issues(client, slug)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "Linear could not be reached."
    assert body["retry_after_seconds"] == 12
    assert body["issues"] == []


def test_an_unconfigured_server_says_so_without_pretending_to_have_read(
    client: TestClient, use_service
) -> None:
    use_service(
        FakeIssuesService(MilestoneIssuesResult(status=MilestonesStatus.NOT_CONFIGURED))
    )
    slug = _create_team(client, linear_team_key="OPS")

    body = _issues(client, slug).json()

    assert body["status"] == "not_configured"
    assert body["counts"]["fetched"] == 0


# =============================================================================
# The credential
# =============================================================================


def test_the_response_never_carries_anything_credential_shaped(
    client: TestClient, use_service
) -> None:
    use_service(FakeIssuesService(_ok_result(_issue())))
    slug = _create_team(client, linear_team_key="OPS")

    raw = _issues(client, slug).text

    assert "lin_api" not in raw
    assert "Authorization" not in raw


def test_the_route_takes_no_query_parameters_at_all(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    path = schema["paths"]["/api/v1/teams/{slug}/milestones/{milestone_id}/issues"]

    assert set(path) == {"get"}
    assert {p["name"] for p in path["get"]["parameters"]} == {"slug", "milestone_id"}


def test_the_service_protocol_the_route_depends_on_has_no_write(
    use_service,
) -> None:
    """A read-only surface, asserted rather than assumed."""

    public = {name for name in dir(LinearMilestoneIssuesService) if not name.startswith("_")}

    assert public == {"aclose", "get_milestone_issues", "invalidate"}


# =============================================================================
# Whose scope it is
# =============================================================================


class HeaderNamespaceAuthorizer:
    """Maps ``X-Test-Namespace`` onto the principal, as the teams tests do."""

    async def authorize(
        self,
        request: Request,
        operation: Operation,
        context: dict[str, Any] | None = None,
    ) -> Principal:
        del operation, context
        return Principal(
            namespace_key=request.headers.get("X-Test-Namespace", "default"),
            is_admin=True,
        )


def _namespace_client(app: FastAPI, namespace_key: str) -> TestClient:
    return TestClient(
        app, raise_server_exceptions=True, headers={"X-Test-Namespace": namespace_key}
    )


def test_the_read_runs_under_the_callers_own_namespace(
    app: FastAPI, use_service
) -> None:
    """The cache is keyed on it, so handing the service the wrong one would let
    one namespace serve another's cached set."""

    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)
    set_authorizer(HeaderNamespaceAuthorizer())
    client = _namespace_client(app, "ns-a")
    slug = _create_team(client, linear_team_key="OPS")

    _issues(client, slug)

    assert [call[0] for call in fake.calls] == ["ns-a"]


def test_another_namespace_cannot_read_this_teams_milestone(
    app: FastAPI, use_service
) -> None:
    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)
    set_authorizer(HeaderNamespaceAuthorizer())
    slug = _create_team(_namespace_client(app, "ns-a"), linear_team_key="OPS")

    resp = _issues(_namespace_client(app, "ns-b"), slug)

    assert resp.status_code == 404, resp.text
    assert fake.calls == [], "no scope existed to read, so nothing was read"


def test_an_unauthenticated_caller_never_reaches_the_read(
    unauthenticated_client: TestClient, use_service
) -> None:
    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)

    resp = unauthenticated_client.get(
        f"{_TEAMS_URL}/operations/milestones/{MILESTONE}/issues"
    )

    assert resp.status_code == 401
    assert fake.calls == []


def test_the_scope_read_is_not_told_anything_the_url_did_not_carry(
    client: TestClient, use_service
) -> None:
    """Three arguments reach the service and there is no fourth to add."""

    fake = FakeIssuesService(_ok_result(_issue()))
    use_service(fake)
    slug = _create_team(client, linear_team_key="OPS")

    _issues(client, slug, "?first=500&include_started=1&namespace_key=other")

    assert fake.calls == [("default", "OPS", MILESTONE)]


def test_the_issues_on_the_response_never_carry_a_label_or_a_state(
    client: TestClient, use_service
) -> None:
    """Labels are a filter for a later phase, never a selector, so they are not
    rendered; state and assignee are counts rather than fields."""

    use_service(FakeIssuesService(_ok_result(_issue())))
    slug = _create_team(client, linear_team_key="OPS")

    issue = _issues(client, slug).json()["issues"][0]

    assert "agent-ready" not in str(issue)
    assert set(issue) == {
        "ref",
        "identifier",
        "title",
        "description",
        "url",
        "created_at",
        "updated_at",
        "creator_id",
        "creator_display_name",
    }
