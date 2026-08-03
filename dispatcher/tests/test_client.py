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

    assert bodies == [{"message": "<<<TASK_BEGIN>>>"}]


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
        (403, "AUTH_INSUFFICIENT_PRIVILEGES", Disposition.BLOCKED),
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


async def test_the_settle_window_is_wider_than_the_sdk_flush_interval() -> None:
    """``AGENT_CONTROL_FLUSH_INTERVAL`` defaults to 5.0 seconds and the write is
    behind the HTTP response by up to a flush plus a round trip."""

    assert DENY_SETTLE_SECONDS >= 10.0


def test_the_two_observations_are_recorded_where_the_code_uses_them() -> None:
    assert "does not correlate" in client_module.TRACE_CORRELATION_NOTE
    assert "2026-08-02" in client_module.TRACE_CORRELATION_NOTE
    assert "AGENT_CONTROL_FLUSH_INTERVAL" in client_module.DENY_INGESTION_LAG_NOTE
