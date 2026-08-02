"""Unit coverage for the Linear GraphQL adapter.

Every case runs against ``httpx.MockTransport``: no network, no Linear
account, no real credential. A sentinel key stands in for a real one so the
secrecy assertions have something specific to search for.
"""

from __future__ import annotations

import datetime as dt
import logging

import httpx
import pytest

from agent_control_server.services.linear_client import (
    HttpLinearClient,
    LinearError,
    LinearTeamNotFoundError,
)

SENTINEL_KEY = "lin_api_TESTSENTINEL0123456789"
API_URL = "https://linear.test/graphql"


def _team_payload(projects: list[dict[str, object]]) -> dict[str, object]:
    team = {"id": "t1", "key": "ENG", "projects": {"nodes": projects}}
    return {"data": {"teams": {"nodes": [team]}}}


def _project(
    *,
    project_id: str = "p1",
    name: str = "Platform",
    url: str = "https://linear.app/acme/project/platform",
    milestones: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": project_id,
        "name": name,
        "url": url,
        "projectMilestones": {"nodes": milestones or []},
    }


def _client(handler, **kwargs) -> HttpLinearClient:
    transport = httpx.MockTransport(handler)
    return HttpLinearClient(
        api_key=SENTINEL_KEY,
        api_url=API_URL,
        client=httpx.AsyncClient(transport=transport),
        **kwargs,
    )


def _responder(payload: dict[str, object], *, status_code: int = 200, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload, headers=headers or {})

    return handler


class TestRequestShape:
    async def test_sends_the_key_in_the_authorization_header_only(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json=_team_payload([]))

        client = _client(handler)
        await client.fetch_milestones("ENG")

        request = seen[0]
        assert request.headers["Authorization"] == SENTINEL_KEY
        assert SENTINEL_KEY not in str(request.url)
        body = request.content.decode()
        assert SENTINEL_KEY not in body
        # Only the one header carries it.
        carrying = [name for name, value in request.headers.items() if SENTINEL_KEY in value]
        assert carrying == ["authorization"]

    async def test_sends_the_team_key_and_fan_out_limits_as_variables(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.append(json.loads(request.content))
            return httpx.Response(200, json=_team_payload([]))

        client = _client(handler, max_projects=7, max_milestones_per_project=3)
        await client.fetch_milestones("ENG")

        assert seen[0]["variables"] == {"key": "ENG", "projectLimit": 7, "milestoneLimit": 3}

    def test_rejects_construction_without_a_key(self) -> None:
        with pytest.raises(ValueError):
            HttpLinearClient(api_key="")

    async def test_aclose_does_not_close_a_borrowed_client(self) -> None:
        borrowed = httpx.AsyncClient(transport=httpx.MockTransport(_responder(_team_payload([]))))
        client = HttpLinearClient(api_key=SENTINEL_KEY, api_url=API_URL, client=borrowed)

        await client.aclose()

        assert borrowed.is_closed is False
        await borrowed.aclose()

    async def test_aclose_closes_a_client_it_created(self) -> None:
        client = HttpLinearClient(api_key=SENTINEL_KEY, api_url=API_URL)
        await client.aclose()
        assert client._client.is_closed is True


class TestParsing:
    async def test_flattens_milestones_onto_their_project(self) -> None:
        payload = _team_payload(
            [
                _project(
                    milestones=[
                        {
                            "id": "m1",
                            "name": "Beta",
                            "description": "Ship it",
                            "targetDate": "2026-09-01",
                            "status": "unstarted",
                            "progress": 0.5,
                        }
                    ]
                )
            ]
        )
        client = _client(_responder(payload))

        milestones = await client.fetch_milestones("ENG")

        assert len(milestones) == 1
        row = milestones[0]
        assert row.id == "m1"
        assert row.name == "Beta"
        assert row.description == "Ship it"
        assert row.target_date == dt.date(2026, 9, 1)
        assert row.status == "unstarted"
        assert row.progress == 0.5
        assert row.project_id == "p1"
        assert row.project_name == "Platform"
        assert row.project_url == "https://linear.app/acme/project/platform"

    async def test_returns_an_empty_list_when_the_team_has_no_projects(self) -> None:
        client = _client(_responder(_team_payload([])))
        assert await client.fetch_milestones("ENG") == []

    async def test_returns_an_empty_list_when_projects_have_no_milestones(self) -> None:
        payload = _team_payload([_project(), _project(project_id="p2", name="Infra")])
        client = _client(_responder(payload))
        assert await client.fetch_milestones("ENG") == []

    async def test_unions_milestones_across_projects(self) -> None:
        payload = _team_payload(
            [
                _project(milestones=[{"id": "m1", "name": "Beta", "targetDate": "2026-09-01"}]),
                _project(
                    project_id="p2",
                    name="Infra",
                    milestones=[{"id": "m2", "name": "GA", "targetDate": "2026-10-01"}],
                ),
            ]
        )
        client = _client(_responder(payload))

        milestones = await client.fetch_milestones("ENG")

        assert [m.id for m in milestones] == ["m1", "m2"]

    async def test_orders_by_target_date_with_undated_last(self) -> None:
        payload = _team_payload(
            [
                _project(
                    milestones=[
                        {"id": "m-none", "name": "Someday"},
                        {"id": "m-late", "name": "GA", "targetDate": "2026-12-01"},
                        {"id": "m-early", "name": "Beta", "targetDate": "2026-09-01"},
                    ]
                )
            ]
        )
        client = _client(_responder(payload))

        milestones = await client.fetch_milestones("ENG")

        assert [m.id for m in milestones] == ["m-early", "m-late", "m-none"]

    async def test_breaks_ties_by_project_name_then_milestone_name(self) -> None:
        payload = _team_payload(
            [
                _project(
                    project_id="p2",
                    name="Zeta",
                    milestones=[{"id": "z", "name": "Alpha", "targetDate": "2026-09-01"}],
                ),
                _project(
                    project_id="p1",
                    name="Alpha",
                    milestones=[
                        {"id": "a2", "name": "Second", "targetDate": "2026-09-01"},
                        {"id": "a1", "name": "First", "targetDate": "2026-09-01"},
                    ],
                ),
            ]
        )
        client = _client(_responder(payload))

        milestones = await client.fetch_milestones("ENG")

        assert [m.id for m in milestones] == ["a1", "a2", "z"]

    async def test_skips_rows_missing_an_id_or_a_name(self) -> None:
        payload = _team_payload(
            [
                _project(
                    milestones=[
                        {"id": "m1", "name": "Beta"},
                        {"name": "No id"},
                        {"id": "m3"},
                        {"id": "", "name": "Blank id"},
                        "not-a-dict",
                    ]
                )
            ]
        )
        client = _client(_responder(payload))

        milestones = await client.fetch_milestones("ENG")

        assert [m.id for m in milestones] == ["m1"]

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-09-01", dt.date(2026, 9, 1)),
            ("2026-09-01T00:00:00.000Z", dt.date(2026, 9, 1)),
            ("not-a-date", None),
            ("", None),
            (None, None),
            (12345, None),
        ],
    )
    async def test_parses_or_drops_the_target_date(self, raw, expected) -> None:
        payload = _team_payload(
            [_project(milestones=[{"id": "m1", "name": "Beta", "targetDate": raw}])]
        )
        client = _client(_responder(payload))

        assert (await client.fetch_milestones("ENG"))[0].target_date == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (0, 0.0),
            (0.5, 0.5),
            (1, 1.0),
            (1.4, 1.0),
            (-0.2, 0.0),
            ("0.5", None),
            (True, None),
            (None, None),
        ],
    )
    async def test_clamps_or_drops_progress(self, raw, expected) -> None:
        payload = _team_payload(
            [_project(milestones=[{"id": "m1", "name": "Beta", "progress": raw}])]
        )
        client = _client(_responder(payload))

        assert (await client.fetch_milestones("ENG"))[0].progress == expected

    async def test_tolerates_missing_project_metadata(self) -> None:
        payload = {
            "data": {
                "teams": {
                    "nodes": [
                        {
                            "projects": {
                                "nodes": [
                                    {"projectMilestones": {"nodes": [{"id": "m1", "name": "Beta"}]}}
                                ]
                            }
                        }
                    ]
                }
            }
        }
        client = _client(_responder(payload))

        row = (await client.fetch_milestones("ENG"))[0]

        assert row.project_id is None
        assert row.project_name is None
        assert row.project_url is None


class TestFailures:
    async def test_unknown_team_raises_team_not_found(self) -> None:
        client = _client(_responder({"data": {"teams": {"nodes": []}}}))

        with pytest.raises(LinearTeamNotFoundError) as exc_info:
            await client.fetch_milestones("NOPE")

        assert "NOPE" in str(exc_info.value)

    async def test_missing_data_key_raises_team_not_found(self) -> None:
        client = _client(_responder({}))

        with pytest.raises(LinearTeamNotFoundError):
            await client.fetch_milestones("ENG")

    async def test_401_reports_a_rejected_key_without_quoting_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # An upstream that echoes the request back at us.
            return httpx.Response(
                401,
                json={"error": "invalid token", "sent": request.headers["Authorization"]},
            )

        client = _client(handler)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LinearError) as exc_info:
                await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear rejected the configured API key."
        assert exc_info.value.retry_after_seconds is None
        assert SENTINEL_KEY not in str(exc_info.value)
        assert SENTINEL_KEY not in caplog.text

    async def test_403_is_treated_like_401(self) -> None:
        client = _client(_responder({"error": "forbidden"}, status_code=403))

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear rejected the configured API key."

    async def test_429_reports_the_retry_after_header(self) -> None:
        client = _client(
            _responder({"error": "slow down"}, status_code=429, headers={"Retry-After": "42"})
        )

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear is rate-limiting this server."
        assert exc_info.value.retry_after_seconds == 42

    async def test_429_falls_back_to_the_rate_limit_reset_header(self) -> None:
        reset_at_ms = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=90)).timestamp() * 1000
        client = _client(
            _responder(
                {"error": "slow down"},
                status_code=429,
                headers={"X-RateLimit-Requests-Reset": str(int(reset_at_ms))},
            )
        )

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.retry_after_seconds is not None
        assert 80 <= exc_info.value.retry_after_seconds <= 90

    @pytest.mark.parametrize(
        "headers",
        [{}, {"Retry-After": "soon"}, {"X-RateLimit-Requests-Reset": "nope"}],
    )
    async def test_429_without_a_usable_hint_reports_no_retry_after(self, headers) -> None:
        client = _client(_responder({"error": "slow down"}, status_code=429, headers=headers))

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.retry_after_seconds is None

    async def test_500_reports_an_upstream_failure(self) -> None:
        client = _client(_responder({"error": "boom"}, status_code=500))

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear reported an internal error."

    async def test_503_reports_an_upstream_failure(self) -> None:
        client = _client(_responder({"error": "unavailable"}, status_code=503))

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear reported an internal error."

    async def test_400_reports_a_rejected_request(self) -> None:
        client = _client(_responder({"error": "bad query"}, status_code=400))

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear rejected the request."

    async def test_connection_failure_reports_unreachable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = _client(handler)

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LinearError) as exc_info:
                await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear could not be reached."
        assert SENTINEL_KEY not in caplog.text

    async def test_timeout_reports_unreachable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        client = _client(handler)

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear could not be reached."
        assert exc_info.value.retry_after_seconds is None

    async def test_non_json_body_reports_an_unreadable_response(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>maintenance</html>")

        client = _client(handler)

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear returned a response this server could not read."

    async def test_json_array_body_reports_an_unreadable_response(self) -> None:
        client = _client(_responder([]))  # type: ignore[arg-type]

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear returned a response this server could not read."

    async def test_graphql_errors_are_reported_without_the_upstream_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = {
            "errors": [
                {
                    "message": (
                        f"Authentication failed for header Authorization: {SENTINEL_KEY}"
                    )
                }
            ]
        }
        client = _client(_responder(payload))

        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LinearError) as exc_info:
                await client.fetch_milestones("ENG")

        assert exc_info.value.message == "Linear rejected the request."
        assert SENTINEL_KEY not in str(exc_info.value)
        assert SENTINEL_KEY not in caplog.text

    async def test_graphql_errors_win_over_a_partial_data_payload(self) -> None:
        payload = {"errors": [{"message": "partial"}], "data": {"teams": {"nodes": []}}}
        client = _client(_responder(payload))

        with pytest.raises(LinearError) as exc_info:
            await client.fetch_milestones("ENG")

        assert not isinstance(exc_info.value, LinearTeamNotFoundError)
