"""HTTP coverage for ``GET /api/v1/teams/{slug}/milestones``.

Runs the real route against real Postgres with a substituted milestone
service, so every one of the five documented states is exercised over the
wire. A sentinel API key stands in for a real Linear credential: no test here
makes a network call, and several assert the sentinel never reaches a response
body, a log line, or the OpenAPI schema.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_control_server.auth_framework import Operation, Principal, set_authorizer
from agent_control_server.config import linear_settings
from agent_control_server.services import linear_milestones
from agent_control_server.services.linear_client import (
    HttpLinearClient,
    LinearError,
    LinearMilestone,
)
from agent_control_server.services.linear_milestones import (
    LinearMilestoneService,
    get_milestone_service,
)

_TEAMS_URL = "/api/v1/teams"

SENTINEL_KEY = "lin_api_ENDPOINTSENTINEL0123456789"
FAKE_LINEAR_URL = "https://linear.test/graphql"


class HeaderNamespaceAuthorizer:
    """Test authorizer mapping ``X-Test-Namespace`` to ``Principal.namespace_key``."""

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


class FakeLinearClient:
    """Replays a scripted result without touching the network."""

    def __init__(self, result: list[LinearMilestone] | Exception) -> None:
        self._result = result
        self.calls: list[str] = []
        self.closed = False

    async def fetch_milestones(self, team_key: str) -> list[LinearMilestone]:
        self.calls.append(team_key)
        if isinstance(self._result, Exception):
            raise self._result
        return list(self._result)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def use_service(app: FastAPI) -> Iterator[Callable[[LinearMilestoneService], None]]:
    """Swap the process-wide milestone service for the duration of one test."""

    def _install(service: LinearMilestoneService) -> None:
        app.dependency_overrides[get_milestone_service] = lambda: service

    yield _install
    app.dependency_overrides.pop(get_milestone_service, None)


def _milestone(
    identifier: str = "m1",
    name: str = "Beta",
    target_date: dt.date | None = dt.date(2026, 9, 1),
) -> LinearMilestone:
    return LinearMilestone(
        id=identifier,
        name=name,
        description="Ship the beta",
        target_date=target_date,
        status="unstarted",
        progress=0.5,
        project_id="p1",
        project_name="Platform",
        project_url="https://linear.app/acme/project/platform",
    )


def _create_team(
    client: TestClient,
    *,
    display_name: str = "Engineering",
    linear_team_key: str | None = None,
) -> str:
    body: dict[str, Any] = {"display_name": display_name}
    if linear_team_key is not None:
        body["linear_team_key"] = linear_team_key
    resp = client.put(_TEAMS_URL, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["slug"]


def _milestones(client: TestClient, slug: str) -> httpx.Response:
    return client.get(f"{_TEAMS_URL}/{slug}/milestones")


def _http_service(handler, **kwargs: Any) -> LinearMilestoneService:
    """A service wired to a real ``HttpLinearClient`` over a mock transport."""
    client = HttpLinearClient(
        api_key=SENTINEL_KEY,
        api_url=FAKE_LINEAR_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return LinearMilestoneService(client=client, **kwargs)


# =============================================================================
# The five states
# =============================================================================


def test_unconfigured_server_reports_not_configured(
    client: TestClient, use_service
) -> None:
    use_service(LinearMilestoneService(client=None))
    slug = _create_team(client, linear_team_key="ENG")

    resp = _milestones(client, slug)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_configured"
    assert body["slug"] == slug
    assert body["linear_team_key"] == "ENG"
    assert body["milestones"] == []
    assert body["error"] is None


def test_unconfigured_server_with_unlinked_team_still_reports_not_configured(
    client: TestClient, use_service
) -> None:
    use_service(LinearMilestoneService(client=None))
    slug = _create_team(client)

    body = _milestones(client, slug).json()

    assert body["status"] == "not_configured"
    assert body["linear_team_key"] is None


def test_team_without_a_linear_key_reports_not_linked(
    client: TestClient, use_service
) -> None:
    fake = FakeLinearClient([_milestone()])
    use_service(LinearMilestoneService(client=fake))
    slug = _create_team(client)

    resp = _milestones(client, slug)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "not_linked"
    assert body["linear_team_key"] is None
    assert body["milestones"] == []
    assert fake.calls == []


def test_linked_team_with_no_milestones_reports_empty(
    client: TestClient, use_service
) -> None:
    use_service(LinearMilestoneService(client=FakeLinearClient([])))
    slug = _create_team(client, linear_team_key="ENG")

    body = _milestones(client, slug).json()

    assert body["status"] == "empty"
    assert body["milestones"] == []
    assert body["error"] is None
    assert body["fetched_at"] is not None


def test_linked_team_returns_its_milestones(client: TestClient, use_service) -> None:
    fake = FakeLinearClient([_milestone()])
    use_service(LinearMilestoneService(client=fake))
    slug = _create_team(client, linear_team_key="eng")

    body = _milestones(client, slug).json()

    assert body["status"] == "ok"
    assert body["linear_team_key"] == "ENG"
    assert fake.calls == ["ENG"]
    assert body["milestones"] == [
        {
            "id": "m1",
            "name": "Beta",
            "description": "Ship the beta",
            "target_date": "2026-09-01",
            "status": "unstarted",
            "progress": 0.5,
            "project_id": "p1",
            "project_name": "Platform",
            "project_url": "https://linear.app/acme/project/platform",
        }
    ]
    assert body["cached"] is False


def test_second_read_is_served_from_cache(client: TestClient, use_service) -> None:
    fake = FakeLinearClient([_milestone()])
    use_service(LinearMilestoneService(client=fake, ttl_seconds=60))
    slug = _create_team(client, linear_team_key="ENG")

    first = _milestones(client, slug).json()
    second = _milestones(client, slug).json()

    assert fake.calls == ["ENG"]
    assert second["cached"] is True
    assert second["fetched_at"] == first["fetched_at"]


def test_unreachable_linear_is_a_200_with_an_error_status(
    client: TestClient, use_service
) -> None:
    use_service(
        LinearMilestoneService(client=FakeLinearClient(LinearError("Linear could not be reached.")))
    )
    slug = _create_team(client, linear_team_key="ENG")

    resp = _milestones(client, slug)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == "Linear could not be reached."
    assert body["milestones"] == []


# =============================================================================
# Upstream failures, over a real HTTP client with a mock transport
# =============================================================================


@pytest.mark.parametrize(
    ("status_code", "expected_error"),
    [
        (401, "Linear rejected the configured API key."),
        (403, "Linear rejected the configured API key."),
        (400, "Linear rejected the request."),
        (500, "Linear reported an internal error."),
        (503, "Linear reported an internal error."),
    ],
)
def test_upstream_status_codes_surface_as_error_without_leaking_the_key(
    client: TestClient,
    use_service,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
    expected_error: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Worst case: an upstream that echoes our credential straight back.
        return httpx.Response(
            status_code,
            json={"error": "denied", "echo": request.headers["Authorization"]},
        )

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    with caplog.at_level(logging.DEBUG):
        resp = _milestones(client, slug)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"] == expected_error
    assert SENTINEL_KEY not in resp.text
    assert SENTINEL_KEY not in caplog.text


def test_rate_limited_linear_reports_the_retry_after(
    client: TestClient, use_service, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "90"})

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    with caplog.at_level(logging.DEBUG):
        body = _milestones(client, slug).json()

    assert body["status"] == "error"
    assert body["error"] == "Linear is rate-limiting this server."
    assert body["retry_after_seconds"] == 90
    assert SENTINEL_KEY not in caplog.text


def test_rate_limit_cooldown_stops_a_second_upstream_call(
    client: TestClient, use_service
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "90"})

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    _milestones(client, slug)
    second = _milestones(client, slug).json()

    assert len(calls) == 1
    assert second["status"] == "error"
    assert second["retry_after_seconds"] is not None


def test_connection_failure_surfaces_as_error(client: TestClient, use_service) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    body = _milestones(client, slug).json()

    assert body["status"] == "error"
    assert body["error"] == "Linear could not be reached."
    assert body["retry_after_seconds"] is None


def test_timeout_surfaces_as_error(client: TestClient, use_service) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    body = _milestones(client, slug).json()

    assert body["status"] == "error"
    assert body["error"] == "Linear could not be reached."


def test_error_cooldown_spares_a_hanging_linear_a_second_request(
    client: TestClient, use_service
) -> None:
    """An unreachable Linear must not cost a request timeout on every page view."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    use_service(_http_service(handler, error_cooldown_seconds=30))
    slug = _create_team(client, linear_team_key="ENG")

    _milestones(client, slug)
    second = _milestones(client, slug).json()

    assert len(calls) == 1
    assert second["status"] == "error"
    # The wait is this server's own choice, so no figure is quoted to the client.
    assert second["retry_after_seconds"] is None


def test_unknown_linear_team_surfaces_as_error(client: TestClient, use_service) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"teams": {"nodes": []}}})

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="NOPE")

    body = _milestones(client, slug).json()

    assert body["status"] == "error"
    assert body["milestones"] == []


def test_graphql_errors_do_not_reach_the_client(
    client: TestClient, use_service, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": f"bad key {SENTINEL_KEY}", "path": ["teams"]}]},
        )

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    with caplog.at_level(logging.DEBUG):
        resp = _milestones(client, slug)

    assert resp.json()["error"] == "Linear rejected the request."
    assert SENTINEL_KEY not in resp.text
    assert "bad key" not in resp.text
    assert SENTINEL_KEY not in caplog.text


def test_a_full_read_never_writes_the_key_into_a_response_or_a_log(
    client: TestClient, use_service, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == SENTINEL_KEY
        return httpx.Response(
            200,
            json={
                "data": {
                    "teams": {
                        "nodes": [
                            {
                                "id": "t1",
                                "key": "ENG",
                                "projects": {
                                    "nodes": [
                                        {
                                            "id": "p1",
                                            "name": "Platform",
                                            "url": "https://linear.app/acme/p",
                                            "projectMilestones": {
                                                "nodes": [
                                                    {
                                                        "id": "m1",
                                                        "name": "Beta",
                                                        "targetDate": "2026-09-01",
                                                        "status": "unstarted",
                                                        "progress": 0.5,
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            },
        )

    use_service(_http_service(handler))
    slug = _create_team(client, linear_team_key="ENG")

    with caplog.at_level(logging.DEBUG):
        resp = _milestones(client, slug)
        team = client.get(f"{_TEAMS_URL}/{slug}")
        listing = client.get(_TEAMS_URL)
        schema = client.get("/openapi.json")

    assert resp.json()["status"] == "ok"
    for response in (resp, team, listing, schema):
        assert SENTINEL_KEY not in response.text
        assert "lin_api_" not in response.text
    assert SENTINEL_KEY not in caplog.text


# =============================================================================
# Routing, authorization and namespace scoping
# =============================================================================


def test_unknown_slug_returns_404(client: TestClient, use_service) -> None:
    use_service(LinearMilestoneService(client=FakeLinearClient([_milestone()])))

    resp = _milestones(client, "no-such-team")

    assert resp.status_code == 404, resp.text
    assert resp.json()["error_code"] == "TEAM_NOT_FOUND"


def test_authenticated_non_admin_can_read_milestones(
    client: TestClient, non_admin_client: TestClient, use_service
) -> None:
    use_service(LinearMilestoneService(client=FakeLinearClient([_milestone()])))
    slug = _create_team(client, linear_team_key="ENG")

    resp = _milestones(non_admin_client, slug)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"


def test_unauthenticated_client_cannot_read_milestones(
    client: TestClient, unauthenticated_client: TestClient, use_service
) -> None:
    use_service(LinearMilestoneService(client=FakeLinearClient([_milestone()])))
    slug = _create_team(client, linear_team_key="ENG")

    assert _milestones(unauthenticated_client, slug).status_code == 401


def test_milestones_of_a_team_in_another_namespace_are_a_404(
    app: FastAPI, use_service
) -> None:
    set_authorizer(HeaderNamespaceAuthorizer())
    use_service(LinearMilestoneService(client=FakeLinearClient([_milestone()])))

    owner = TestClient(app, headers={"X-Test-Namespace": "ns-one"})
    stranger = TestClient(app, headers={"X-Test-Namespace": "ns-two"})
    slug = _create_team(owner, linear_team_key="ENG")

    assert _milestones(owner, slug).status_code == 200
    assert _milestones(stranger, slug).status_code == 404


def test_real_wiring_with_no_api_key_configured_reports_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dependency override: the route builds its own service from settings."""
    monkeypatch.setattr(linear_settings, "api_key", SecretStr(""))
    monkeypatch.setattr(linear_milestones, "_service", None)
    slug = _create_team(client, linear_team_key="ENG")

    resp = _milestones(client, slug)

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "not_configured"
    assert linear_milestones._service is not None
    assert linear_milestones._service._client is None


def test_openapi_documents_the_route_and_its_five_states(client: TestClient) -> None:
    """The UI generates its types from this schema."""
    schema = client.get("/openapi.json").json()

    path = schema["paths"]["/api/v1/teams/{slug}/milestones"]
    assert "get" in path
    status_enum = schema["components"]["schemas"]["MilestonesStatus"]["enum"]
    assert set(status_enum) == {"not_configured", "not_linked", "error", "empty", "ok"}


def test_two_namespaces_sharing_a_linear_key_each_get_a_read(
    app: FastAPI, use_service
) -> None:
    """A cooldown or cache entry in one namespace must not silence another."""
    set_authorizer(HeaderNamespaceAuthorizer())
    fake = FakeLinearClient([_milestone()])
    use_service(LinearMilestoneService(client=fake, ttl_seconds=60))

    first = TestClient(app, headers={"X-Test-Namespace": "ns-one"})
    second = TestClient(app, headers={"X-Test-Namespace": "ns-two"})
    slug_one = _create_team(first, linear_team_key="ENG")
    slug_two = _create_team(second, linear_team_key="ENG")

    assert _milestones(first, slug_one).json()["status"] == "ok"
    assert _milestones(second, slug_two).json()["status"] == "ok"
    assert fake.calls == ["ENG", "ENG"]
