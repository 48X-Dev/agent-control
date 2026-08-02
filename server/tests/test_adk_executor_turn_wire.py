"""``AdkExecutorClient.run`` against the payloads a real ``adk api_server`` sent.

Every other test of the turn path talks to a fake ``ExecutorClient``, which is
the right default: it keeps the flow, the lock and the error mapping testable
without ADK installed. But it also means the one module that actually knows
ADK's wire format is exercised by nothing, and that module is the whole of
assumption A2. A fake cannot disagree with the parser, because the same author
wrote both.

So these run the genuine client, over a real socket, against the captures in
``server/tests/fixtures/adk/``. Those files are recorded request/response pairs,
not hand-written expectations, which is what makes them worth asserting on: when
the pinned ADK version changes shape, a recapture fails these tests instead of
silently changing what a transcript says.

Scope is deliberately narrow. What is pinned is the request this server sends,
the reading of the response, and the classification of failures - the three
things a wire correction would have to change together. Whether the executor
*acts* on the seeded state (A7) is not observable from here and is not claimed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from agent_control_server.services.adk_executor_client import AdkExecutorClient
from agent_control_server.services.executor_client import (
    EXECUTOR_TURN_TIMEOUT_MESSAGE,
    PART_KIND_TEXT,
    PART_KIND_TOOL_CALL,
    PART_KIND_TOOL_RESULT,
    ROLE_AGENT,
    ROLE_USER,
    ExecutorModelUnavailableError,
    ExecutorSessionNotFoundError,
    ExecutorTurnTimeoutError,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

_FIXTURES = Path(__file__).parent / "fixtures" / "adk"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# A stub executor on a real socket
# ---------------------------------------------------------------------------


class _RecordedUpstream:
    """Replays one captured response and keeps what was asked of it."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: Any = []
        self.stall_seconds = 0.0

    async def handle(self, request: Request) -> Response:
        self.requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(await request.body() or b"null"),
                "headers": dict(request.headers),
            }
        )
        if self.stall_seconds:
            await asyncio.sleep(self.stall_seconds)
        return JSONResponse(self.body, status_code=self.status)

    def app(self) -> Starlette:
        return Starlette(
            routes=[Route("/{path:path}", self.handle, methods=["GET", "POST", "DELETE"])]
        )


@pytest.fixture()
async def upstream(live_server_factory: Any) -> Any:
    recorder = _RecordedUpstream()
    server = await live_server_factory(recorder.app())
    recorder.base_url = server.base_url  # type: ignore[attr-defined]
    return recorder


def _client(upstream: Any, **kwargs: Any) -> AdkExecutorClient:
    return AdkExecutorClient(
        base_url=upstream.base_url,
        client=httpx.AsyncClient(timeout=httpx.Timeout(10.0)),
        owns_client=True,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The request this server sends
# ---------------------------------------------------------------------------


async def test_the_run_request_matches_the_captured_one_key_for_key(
    upstream: Any,
) -> None:
    """The body ADK actually accepted, compared against the body we send.

    Captured from a live ``POST /run``. A missing or renamed key here is the
    single most likely way this integration breaks, and it breaks as a 422 that
    a user reads as "the agent didn't answer".
    """
    captured = _fixture("run_response")
    expected_body = captured["request"]["body"]
    upstream.body = captured["response"]["body"]

    client = _client(upstream)
    try:
        await client.run(
            app_name=expected_body["appName"],
            user_id=expected_body["userId"],
            session_id=expected_body["sessionId"],
            message=expected_body["newMessage"]["parts"][0]["text"],
            state_delta=expected_body["stateDelta"],
            timeout_seconds=10.0,
        )
    finally:
        await client.aclose()

    (sent,) = upstream.requests
    assert sent["method"] == "POST"
    assert sent["path"] == captured["request"]["path"]
    assert set(sent["body"]) == set(expected_body)
    assert sent["body"] == expected_body


async def test_the_per_turn_state_delta_is_omitted_rather_than_sent_empty(
    upstream: Any,
) -> None:
    """An empty delta is not the same request as no delta.

    ADK merges ``stateDelta`` into the session before the invocation runs, so
    sending an empty object on every turn is a write where none was asked for.
    """
    upstream.body = []
    client = _client(upstream)
    try:
        await client.run(
            app_name="spike_app",
            user_id="ns:u",
            session_id="s",
            message="hello",
            state_delta=None,
            timeout_seconds=10.0,
        )
    finally:
        await client.aclose()

    (sent,) = upstream.requests
    assert "stateDelta" not in sent["body"]


async def test_the_shared_secret_is_the_only_header_added(upstream: Any) -> None:
    """Defence in depth, and nothing else rides along with it."""
    upstream.body = []
    client = _client(upstream, shared_secret="s3cret-value")
    try:
        await client.run(
            app_name="spike_app",
            user_id="ns:u",
            session_id="s",
            message="hello",
            timeout_seconds=10.0,
        )
    finally:
        await client.aclose()

    (sent,) = upstream.requests
    assert sent["headers"]["x-agent-control-executor-secret"] == "s3cret-value"


# ---------------------------------------------------------------------------
# Reading the response
# ---------------------------------------------------------------------------


async def test_a_captured_turn_reads_as_text_a_tool_call_and_a_tool_result(
    upstream: Any,
) -> None:
    """The bare array ADK answers with, flattened into transcript messages.

    Three events, in order: the model asking for a tool, the tool's result, and
    the model's closing text. All three belong in a transcript, and the middle
    one is the case the next test exists for.
    """
    captured = _fixture("run_response")
    upstream.body = captured["response"]["body"]

    client = _client(upstream)
    try:
        turn = await client.run(
            app_name="spike_app",
            user_id="ns:u",
            session_id="s",
            message="Report the phrase HELLO_API.",
            timeout_seconds=10.0,
        )
    finally:
        await client.aclose()

    kinds = [[part.kind for part in message.parts] for message in turn.messages]
    assert kinds == [[PART_KIND_TOOL_CALL], [PART_KIND_TOOL_RESULT], [PART_KIND_TEXT]]

    call = turn.messages[0].parts[0]
    assert call.tool_name == "send_report"
    assert call.arguments == {"text": "HELLO_API"}

    result = turn.messages[1].parts[0]
    assert result.tool_name == "send_report"
    assert result.tool_call_id == call.tool_call_id, "the result names its own call"
    assert result.result == {"status": "sent", "text": "HELLO_API"}

    assert turn.messages[2].parts[0].text == "Done."
    assert all(message.timestamp is not None for message in turn.messages)


async def test_a_tool_result_is_never_attributed_to_the_human(upstream: Any) -> None:
    """The misattribution the captured payloads exposed.

    ADK stamps ``role: "user"`` on a tool-result event, because that is how tool
    output is handed back to a model. Read literally, the transcript then shows
    the tool's output as a message the operator typed - and in a console whose
    whole purpose is deciding whether an agent behaved, "who said this" is not
    a detail. Every part of this turn is the agent's.
    """
    captured = _fixture("run_response")
    upstream.body = captured["response"]["body"]
    raw_roles = [
        event["content"]["role"] for event in captured["response"]["body"]
    ]
    assert "user" in raw_roles, "fixture must still contain the trap"

    client = _client(upstream)
    try:
        turn = await client.run(
            app_name="spike_app",
            user_id="ns:u",
            session_id="s",
            message="Report the phrase HELLO_API.",
            timeout_seconds=10.0,
        )
    finally:
        await client.aclose()

    assert [message.role for message in turn.messages] == [ROLE_AGENT] * 3


async def test_a_real_human_turn_is_still_read_as_the_humans(upstream: Any) -> None:
    """The other side of that rule, from the captured transcript read.

    Narrowing "who is the human" must not narrow it to nobody: the operator's
    own message carries role ``user`` and text, and it has to survive.
    """
    captured = _fixture("get_session_after_turn")
    upstream.body = captured["response"]["body"]

    client = _client(upstream)
    try:
        session = await client.get_session(
            app_name="spike_app", user_id="ns:u", session_id="s"
        )
    finally:
        await client.aclose()

    roles = [message.role for message in session.messages]
    assert roles[0] == ROLE_USER, "the person's own message is theirs"
    assert roles[1:] == [ROLE_AGENT] * (len(roles) - 1)


async def test_events_carrying_no_content_do_not_become_empty_bubbles(
    upstream: Any,
) -> None:
    """Executors emit bookkeeping events. A transcript is not a log."""
    upstream.body = [
        {"author": "root_agent", "actions": {"stateDelta": {}}},
        {"author": "root_agent", "content": {"role": "model", "parts": []}},
        {"author": "root_agent", "content": {"role": "model", "parts": [{"text": "hi"}]}},
    ]
    client = _client(upstream)
    try:
        turn = await client.run(
            app_name="a", user_id="u", session_id="s", message="x", timeout_seconds=10.0
        )
    finally:
        await client.aclose()

    assert len(turn.messages) == 1
    assert turn.messages[0].parts[0].text == "hi"


# ---------------------------------------------------------------------------
# Classifying failures
# ---------------------------------------------------------------------------


async def test_a_stalled_executor_is_a_turn_timeout_and_says_the_turn_goes_on(
    upstream: Any,
) -> None:
    """The distinction the whole two-exit design rests on.

    A turn that ran out of time is not a turn that failed: the executor is
    still calling a model and still spending. This is the only place that
    difference is decided, and it is decided by which exception class comes out
    of here.
    """
    upstream.stall_seconds = 5.0
    client = _client(upstream)
    try:
        with pytest.raises(ExecutorTurnTimeoutError) as raised:
            await client.run(
                app_name="a",
                user_id="u",
                session_id="s",
                message="x",
                timeout_seconds=0.25,
            )
    finally:
        await client.aclose()

    assert str(raised.value) == EXECUTOR_TURN_TIMEOUT_MESSAGE


async def test_the_turn_timeout_is_per_call_not_the_transports(upstream: Any) -> None:
    """Session CRUD gets seconds and a turn gets minutes, on one transport.

    Proven by giving the transport a short timeout and the turn a longer one:
    if the transport's value won, this would raise.
    """
    captured = _fixture("run_response")
    upstream.body = captured["response"]["body"]
    upstream.stall_seconds = 0.5

    client = AdkExecutorClient(
        base_url=upstream.base_url,
        client=httpx.AsyncClient(timeout=httpx.Timeout(0.1)),
        owns_client=True,
    )
    try:
        turn = await client.run(
            app_name="a", user_id="u", session_id="s", message="x", timeout_seconds=10.0
        )
    finally:
        await client.aclose()
    assert turn.messages


async def test_an_exhausted_model_quota_is_told_apart_from_a_busy_executor(
    upstream: Any,
) -> None:
    """429 on a turn points at the model's credentials, not at the process.

    Sending an operator to restart a healthy executor because its model key ran
    out is the wrong half-hour, so the two get different sentences.
    """
    upstream.status = 429
    upstream.body = {"detail": "Quota exceeded for project agent-control-spike"}

    client = _client(upstream)
    try:
        with pytest.raises(ExecutorModelUnavailableError) as raised:
            await client.run(
                app_name="a",
                user_id="u",
                session_id="s",
                message="x",
                timeout_seconds=10.0,
            )
    finally:
        await client.aclose()

    assert "agent-control-spike" not in str(raised.value)
    assert "Quota exceeded for project" not in str(raised.value)


async def test_no_upstream_body_survives_any_turn_failure(upstream: Any) -> None:
    """Captured 4xx bodies, replayed. None of them may reach an exception.

    ``run_missing_session`` carries an executor-side identifier and ``run_422``
    echoes the request back field by field. Both are ordinary ADK behaviour and
    both are somebody else's business.
    """
    for name, expected in (
        ("run_missing_session", ExecutorSessionNotFoundError),
        ("run_422", Exception),
    ):
        captured = _fixture(name)
        upstream.requests.clear()
        upstream.status = captured["response"]["status"]
        upstream.body = captured["response"]["body"]

        client = _client(upstream)
        try:
            with pytest.raises(expected) as raised:
                await client.run(
                    app_name="a",
                    user_id="u",
                    session_id="s",
                    message="x",
                    timeout_seconds=10.0,
                )
        finally:
            await client.aclose()

        leaked = json.dumps(captured["response"]["body"])
        message = str(raised.value)
        for fragment in ("does-not-exist", "Field required", "spike_app"):
            assert fragment not in message, f"{name} leaked {fragment!r}"
        assert message not in leaked


async def test_the_seeded_state_is_sent_bare_not_wrapped(upstream: Any) -> None:
    """The create body IS the state map.

    ADK answers 200 to the wrapped form too, which is what makes this worth
    pinning: the wrapper is accepted and then stored one level down, so every
    reader looking for ``state["agent_control.session_key"]`` finds nothing and
    the failure surfaces far from its cause. The two captures disagree on
    exactly this, and the bare one is the one that round-trips flat.
    """
    captured = _fixture("create_session_with_id")
    seeded = captured["request"]["body"]
    upstream.body = captured["response"]["body"]

    client = _client(upstream)
    try:
        await client.create_session(
            app_name="spike_app",
            user_id="ns-demo:u-spike",
            session_id="spike-session-1",
            state=seeded,
        )
    finally:
        await client.aclose()

    sent = upstream.requests[-1]["body"]
    assert sent == seeded
    assert "state" not in sent, "seeded state must not be nested under a 'state' key"
