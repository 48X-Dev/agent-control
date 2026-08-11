"""The three endpoints, the failure table, and the deny-event correlation.

Faked one layer below :class:`StubClient`, at ``httpx.MockTransport``, because
what is worth pinning here is the client's own behaviour: which refusals it
retries, which it refuses to retry, and how it decides a deny belongs to the
turn it just ran.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from agent_control_dispatcher import client as client_module
from agent_control_dispatcher.client import (
    DENY_SETTLE_SECONDS,
    DispatchClient,
    DispatchHTTPError,
    Disposition,
    _BoundedIdSet,
    classify,
)
from agent_control_models.sessions import TurnResponse
from conftest import blocked_turn_payload, deny_event_payload

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler, **kwargs: Any) -> DispatchClient:
    return DispatchClient(
        base_url="http://localhost:8000",
        api_key="local-agent-key",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _turn() -> TurnResponse:
    return TurnResponse.model_validate(blocked_turn_payload())


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Backoff, recorded rather than waited out."""

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    return slept


# --- the three endpoints ----------------------------------------------------


async def test_the_three_endpoints_are_the_ones_that_already_exist() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["X-API-Key"] == "local-agent-key"
        if request.url.path.endswith("/turns"):
            return httpx.Response(200, json=blocked_turn_payload(session_key="sk-1"))
        if request.method == "DELETE":
            return httpx.Response(204, json={})
        return httpx.Response(201, json={"session": {"session_key": "sk-1"}})

    async with _client(handler) as client:
        key = await client.create_session(agent_name="marketing_researcher", title="dispatch t1")
        turn = await client.start_turn(session_key=key, message="hello")
        await client.delete_session(session_key=key)

    assert key == "sk-1"
    assert turn.session_key == "sk-1"
    assert seen == [
        ("POST", "/api/v1/agent-sessions"),
        ("POST", "/api/v1/agent-sessions/sk-1/turns"),
        ("DELETE", "/api/v1/agent-sessions/sk-1"),
    ]


async def test_the_turn_carries_the_envelope_as_the_message() -> None:
    """Section 9.2: the envelope has to arrive as ``message`` so it lands in
    ``contents[-1]`` where every control already reads."""

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=blocked_turn_payload())

    async with _client(handler) as client:
        await client.start_turn(session_key="sk", message="<<<TASK_BEGIN>>>")

    assert bodies == [{"message": "<<<TASK_BEGIN>>>", "attachment_keys": []}]


# --- section 11.3 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "error_code", "expected"),
    [
        (504, None, Disposition.RUNNING_UNKNOWN),
        (502, "EXECUTOR_REJECTED", Disposition.FAILED),
        (503, "EXECUTOR_UNAVAILABLE", Disposition.RETRY),
        (429, "QUOTA_EXCEEDED", Disposition.PAUSED_QUOTA),
        (409, "TURN_IN_FLIGHT", Disposition.FAILED),
        (409, "AGENT_RUNTIME_NOT_BOUND", Disposition.BLOCKED),
        # An unlinked team is blocked rather than retried: nothing the
        # dispatcher can do makes the read succeed, and a person has to set
        # linear_team_key before it ever will.
        (409, "TEAM_NOT_LINKED", Disposition.BLOCKED),
        (403, "AUTH_INSUFFICIENT_PRIVILEGES", Disposition.BLOCKED),
        # Not in section 11.3's table, which covers the turn path. On a poll
        # loop `FAILED` means a process that looks alive and does nothing while
        # every queued row waits, so a key the server rejects is blocked.
        (401, "AUTH_INVALID_KEY", Disposition.BLOCKED),
        (401, "AUTH_MISSING_KEY", Disposition.BLOCKED),
    ],
)
def test_the_failure_table_row_for_row(
    status_code: int, error_code: str | None, expected: Disposition
) -> None:
    assert classify(status_code, error_code) is expected


@pytest.mark.parametrize(
    ("status_code", "error_code"),
    [(500, None), (400, "VALIDATION_ERROR"), (404, None), (502, "SOMETHING_NEW")],
)
def test_an_unlisted_refusal_is_failed_rather_than_guessed_retryable(
    status_code: int, error_code: str | None
) -> None:
    assert classify(status_code, error_code) is Disposition.FAILED


async def test_executor_unavailable_is_retried_three_times_and_then_gives_up(
    no_sleep: list[float],
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(
            503, json={"error_code": "EXECUTOR_UNAVAILABLE", "detail": "nothing reached it"}
        )

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError) as caught:
            await client.start_turn(session_key="sk", message="m")

    assert len(attempts) == 3
    assert caught.value.disposition is Disposition.RETRY
    assert no_sleep == [2.0, 4.0], "bounded backoff between attempts"


async def test_a_retry_that_succeeds_returns_the_turn(no_sleep: list[float]) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, json={"error_code": "EXECUTOR_UNAVAILABLE"})
        return httpx.Response(200, json=blocked_turn_payload())

    async with _client(handler) as client:
        turn = await client.start_turn(session_key="sk", message="m")

    assert len(attempts) == 2
    assert turn.duration_seconds == 3.0


async def test_a_504_is_never_retried_because_the_invocation_did_not_stop(
    no_sleep: list[float],
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(504, json={"detail": "gateway timeout"})

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError) as caught:
            await client.start_turn(session_key="sk", message="m")

    assert len(attempts) == 1
    assert caught.value.disposition is Disposition.RUNNING_UNKNOWN
    assert no_sleep == []


async def test_a_local_read_timeout_looks_like_a_504_and_is_treated_like_one(
    no_sleep: list[float],
) -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("too slow", request=request)

    async with _client(handler, turn_timeout_seconds=1.0) as client:
        with pytest.raises(DispatchHTTPError) as caught:
            await client.start_turn(session_key="sk", message="m")

    assert len(attempts) == 1
    assert caught.value.disposition is Disposition.RUNNING_UNKNOWN
    assert caught.value.status_code == 504
    assert "still spending" in caught.value.detail


async def test_an_unreachable_server_is_retryable_rather_than_terminal(
    no_sleep: list[float],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError) as caught:
            await client.create_session(agent_name="a", title="t")

    assert caught.value.disposition is Disposition.RETRY
    assert "localhost:8000" in caught.value.detail


@pytest.mark.parametrize(
    ("payload", "headers", "expected"),
    [
        ({"extra_details": {"retry_after_seconds": 42}}, {}, 42.0),
        ({"details": {"retry_after_seconds": 7.5}}, {}, 7.5),
        ({}, {"Retry-After": "30"}, 30.0),
        ({"detail": "Retry in about 60 seconds"}, {}, None),
        ({}, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
    ],
)
async def test_retry_after_is_read_from_a_number_never_from_prose(
    payload: dict[str, Any], headers: dict[str, str], expected: float | None
) -> None:
    """Section 11.4: the server does not send a machine-readable delay today.
    The field is read when it appears and the English sentence is never parsed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error_code": "QUOTA_EXCEEDED", **payload}, headers=headers
        )

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError) as caught:
            await client.create_session(agent_name="a", title="t")

    assert caught.value.retry_after_seconds == expected
    assert caught.value.disposition is Disposition.PAUSED_QUOTA


async def test_a_refusal_with_no_json_body_still_produces_a_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>Internal Server Error</html>")

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError) as caught:
            await client.create_session(agent_name="a", title="t")

    assert caught.value.disposition is Disposition.FAILED
    assert "Internal Server Error" in str(caught.value)


# --- deny events ------------------------------------------------------------


async def test_the_deny_query_asks_only_for_matched_denies_in_this_turn_s_window() -> None:
    bodies: list[dict[str, Any]] = []
    turn = _turn()

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"events": [deny_event_payload()]})

    async with _client(handler) as client:
        events = await client.deny_events_for_turn(agent_name="marketing_researcher", turn=turn)

    assert [event.control_name for event in events] == ["block-ssn"]
    body = bodies[0]
    assert body["agent_name"] == "marketing_researcher"
    assert body["actions"] == ["deny"]
    assert body["matched"] is True
    assert dt.datetime.fromisoformat(body["start_time"]) < turn.started_at
    assert dt.datetime.fromisoformat(body["end_time"]) > turn.completed_at


async def test_the_query_waits_out_the_flush_rather_than_asking_once() -> None:
    """Observed: the deny is written after the turn answers, because the SDK
    batches events on a 5 second timer. Asking once, immediately, finds nothing
    and would report a refusal as a finding."""

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(200, json={"events": []})
        return httpx.Response(200, json={"events": [deny_event_payload()]})

    async with _client(handler) as client:
        events = await client.deny_events_for_turn(
            agent_name="marketing_researcher",
            turn=_turn(),
            settle_seconds=5.0,
            poll_interval_seconds=0.001,
        )

    assert len(calls) == 3
    assert len(events) == 1


async def test_an_empty_result_after_the_window_is_not_proof_of_anything() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"events": []})

    async with _client(handler) as client:
        events = await client.deny_events_for_turn(
            agent_name="a", turn=_turn(), settle_seconds=0.0, poll_interval_seconds=0.001
        )

    assert events == []
    assert len(calls) == 1
    assert "Absence still proves nothing" in client_module.DENY_INGESTION_LAG_NOTE


async def test_a_deny_already_given_to_an_earlier_turn_is_not_given_to_a_later_one() -> None:
    """A late deny lands inside the next task's window. Attributing it twice
    would blame a task that was never blocked."""

    def handler(request: httpx.Request) -> httpx.Response:
        event = deny_event_payload(control_execution_id="ce-9")
        return httpx.Response(200, json={"events": [event]})

    async with _client(handler) as client:
        first = await client.deny_events_for_turn(agent_name="a", turn=_turn())
        second = await client.deny_events_for_turn(
            agent_name="a", turn=_turn(), settle_seconds=0.0, poll_interval_seconds=0.001
        )

    assert [event.control_execution_id for event in first] == ["ce-9"]
    assert second == []


async def test_the_attributed_deny_ids_do_not_grow_without_end() -> None:
    """One client now outlives thousands of turns, which ``once`` never did.

    An unbounded set is one id per deny for the life of the process, consulted
    on every deny query. The oldest are the ones it is safe to forget: an event
    is only ever offered inside its own turn's time window.
    """
    seen = _BoundedIdSet(3)
    seen.update(str(index) for index in range(10))

    assert "9" in seen
    assert "0" not in seen
    assert len(seen._seen) == 3


async def test_the_settle_window_is_wider_than_the_sdk_flush_interval() -> None:
    """``AGENT_CONTROL_FLUSH_INTERVAL`` defaults to 5.0 seconds and the write is
    behind the HTTP response by up to a flush plus a round trip."""

    assert DENY_SETTLE_SECONDS >= 10.0


def test_the_two_observations_are_recorded_where_the_code_uses_them() -> None:
    assert "does not correlate" in client_module.TRACE_CORRELATION_NOTE
    assert "2026-08-02" in client_module.TRACE_CORRELATION_NOTE
    assert "AGENT_CONTROL_FLUSH_INTERVAL" in client_module.DENY_INGESTION_LAG_NOTE


# --- the scope read, which is the only route slice 2 adds -------------------


def _issues_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "ok",
        "slug": "operations",
        "linear_team_key": "OPS",
        "milestone_id": "3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6",
        "issues": [
            {
                "ref": "uuid-1",
                "identifier": "OPS-2",
                "title": "Review the deck",
                "description": "Owner noted in request: Clive.",
                "url": "https://linear.app/acme/issue/OPS-2",
                "created_at": "2026-08-01T14:56:49.290000Z",
                "updated_at": "2026-08-01T15:05:08.924000Z",
                "creator_id": "c087560f",
                "creator_display_name": "paul",
            }
        ],
        "counts": {
            "fetched": 3,
            "eligible": 1,
            "skipped": {"started": 1, "assigned": 1, "other_team": 0},
            "beyond_page_cap": False,
        },
        "cached": False,
        "fetched_at": "2026-08-03T08:19:00Z",
    }
    payload.update(overrides)
    return payload


async def test_the_scope_read_is_a_get_and_carries_the_key() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        assert request.headers["X-API-Key"] == "local-agent-key"
        return httpx.Response(200, json=_issues_payload())

    async with _client(handler) as client:
        response = await client.fetch_milestone_issues(
            team_slug="operations", milestone_id="3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6"
        )

    assert seen == [
        (
            "GET",
            "/api/v1/teams/operations/milestones/3dcd106d-e00a-4f32-a3b6-27b9fd64c6d6/issues",
        )
    ]
    assert [issue.identifier for issue in response.issues] == ["OPS-2"]
    assert response.counts.skipped.started == 1


async def test_the_scope_read_never_sends_a_body_or_a_query_string() -> None:
    """There is no field to widen the scope with, over the wire either."""

    seen: list[tuple[bytes, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.content, request.url.query.decode()))
        return httpx.Response(200, json=_issues_payload())

    async with _client(handler) as client:
        await client.fetch_milestone_issues(team_slug="operations", milestone_id="m-1")

    assert seen == [(b"", "")]


async def test_a_path_shaped_team_cannot_retarget_the_request_either() -> None:
    """``--team`` is interpolated too, and is not shape-checked before it is.

    Quoting is what holds here: the server answers 404 for a slug that is not a
    slug, which is the right answer, and the request never becomes a GET
    against some other route.
    """

    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        return httpx.Response(404, json={"detail": "not found"})

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError):
            await client.fetch_milestone_issues(
                team_slug="../../agent-sessions", milestone_id="m-1"
            )

    assert seen == [b"/api/v1/teams/..%2F..%2Fagent-sessions/milestones/m-1/issues"]


async def test_a_path_shaped_id_cannot_retarget_the_request() -> None:
    """httpx resolves ``..`` in a path, so the segments are quoted.

    ``build_source`` refuses an id like this before it ever reaches the client,
    and this is the second of the two: the request that gets sent still names
    the route this method documents.
    """

    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # ``raw_path`` rather than ``path``: the latter percent-decodes, so it
        # shows what the id said rather than what went down the wire.
        seen.append(request.url.raw_path)
        return httpx.Response(200, json=_issues_payload())

    async with _client(handler) as client:
        await client.fetch_milestone_issues(
            team_slug="operations", milestone_id="../../../agent-sessions"
        )

    assert seen == [b"/api/v1/teams/operations/milestones/..%2F..%2F..%2Fagent-sessions/issues"]


async def test_an_unlinked_team_is_a_blocked_refusal_carrying_the_servers_words() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error_code": "TEAM_NOT_LINKED",
                "detail": "Team 'marketing' is not linked to a Linear team.",
            },
        )

    async with _client(handler) as client:
        with pytest.raises(DispatchHTTPError) as excinfo:
            await client.fetch_milestone_issues(team_slug="marketing", milestone_id="m-1")

    assert excinfo.value.disposition is Disposition.BLOCKED
    assert excinfo.value.error_code == "TEAM_NOT_LINKED"
    assert "not linked" in excinfo.value.detail
