"""The one tool whose effect leaves Agent Control.

Driven against a **real HTTP server** on a real socket, for the reason
``test_google_adk_progress_tools.py`` gives: the things worth checking here are
the path addressed, the credential carried and the guarantee that nothing takes
a turn down, and a stubbed transport would let all three be wrong quietly.

What is pinned:

* **the tool cannot name a ticket** - its signature has no issue parameter, and
  the only address on the wire is the session key from ADK state, so text
  arriving from a fetched page cannot redirect the write;
* the token on the wire is the session-bound one, and the per-turn token wins;
* **nothing raises, ever** - no state, no token, no server, a hang-up, a
  nonsense body and a refusal all come back as ordinary results;
* a refusal carries the server's own ``detail`` and ``hint`` through, because
  that sentence is what the agent repeats to the person who asked;
* empty text costs no round trip;
* saving twice sends twice, because asking twice means two comments.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_control._state import state
from agent_control.integrations.google_adk import tracker_tools

SESSION_KEY = "sess-ops-19"
SEEDED_TOKEN = "tok-seeded-at-creation"
TURN_TOKEN = "tok-minted-for-this-turn"


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: dict[str, Any] = {
            "issue_ref": "issue-uuid",
            "issue_url": "https://linear.app/x/issue/OPS-19/research",
            "comment_id": "comment-1",
        }
        self.raw: bytes | None = None

    def refuse(self, status: int, **problem: Any) -> None:
        self.status = status
        self.raw = None
        self.body = problem

    @property
    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]


@pytest.fixture
def plane() -> Any:
    recorder = _Recorder()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(payload or b"{}")
            except ValueError:
                parsed = None
            recorder.requests.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": parsed,
                }
            )
            if recorder.raw is not None:
                self.send_response(recorder.status)
                self.send_header("Content-Length", str(len(recorder.raw)))
                self.end_headers()
                self.wfile.write(recorder.raw)
                return
            encoded = json.dumps(recorder.body).encode()
            self.send_response(recorder.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    recorder.url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def reset_state() -> Any:
    saved = (state.server_url, state.api_key)
    state.server_url = None
    state.api_key = None
    yield
    (state.server_url, state.api_key) = saved


@pytest.fixture
def wired(plane: Any) -> Any:
    state.server_url = plane.url
    return plane


class FakeToolContext:
    def __init__(self, session_state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = dict(session_state or {})


def _identity_state(*, turn_token: str | None = TURN_TOKEN) -> dict[str, Any]:
    seeded: dict[str, Any] = {
        "agent_control": {
            "session_key": SESSION_KEY,
            "namespace_key": "default",
            "agent_name": "marketing_researcher",
            "trace_id": "trace-1",
            "runtime_token": SEEDED_TOKEN,
        }
    }
    if turn_token is not None:
        seeded["agent_control_turn"] = {
            "session_key": SESSION_KEY,
            "trace_id": "trace-2",
            "runtime_token": turn_token,
        }
    return seeded


def _save(text: str, context: Any) -> dict[str, Any]:
    return asyncio.run(tracker_tools.save_to_tracker(text, context))


# ---------------------------------------------------------------------------
# The address is the session, never an argument
# ---------------------------------------------------------------------------


def test_the_tool_takes_no_issue_so_a_model_cannot_choose_a_ticket() -> None:
    """The injection defence is the signature, not a validation rule."""

    import inspect

    parameters = set(inspect.signature(tracker_tools.save_to_tracker).parameters)
    assert parameters == {"text", "tool_context"}


def test_the_path_carries_the_session_from_state(wired: Any) -> None:
    result = _save("research summary", FakeToolContext(_identity_state()))
    assert result["saved"] is True
    assert wired.paths == [f"/api/v1/agent-sessions/{SESSION_KEY}/tracker-comment"]
    assert wired.requests[0]["body"] == {"text": "research summary"}


def test_the_turn_token_wins_over_the_one_seeded_at_creation(wired: Any) -> None:
    _save("x", FakeToolContext(_identity_state()))
    assert wired.requests[0]["authorization"] == f"Bearer {TURN_TOKEN}"


def test_the_seeded_token_is_used_when_no_turn_token_was_minted(wired: Any) -> None:
    _save("x", FakeToolContext(_identity_state(turn_token=None)))
    assert wired.requests[0]["authorization"] == f"Bearer {SEEDED_TOKEN}"


def test_saving_twice_sends_twice(wired: Any) -> None:
    """A correction is a normal second call and must not be swallowed."""

    context = FakeToolContext(_identity_state())
    _save("first", context)
    _save("second, corrected", context)
    assert len(wired.requests) == 2
    assert [r["body"]["text"] for r in wired.requests] == ["first", "second, corrected"]


# ---------------------------------------------------------------------------
# Nothing raises
# ---------------------------------------------------------------------------


def test_no_tool_context_is_a_result_not_a_crash(wired: Any) -> None:
    result = _save("x", None)
    assert result["saved"] is False
    assert "credential" in result["message"]
    assert wired.requests == []


def test_no_runtime_token_never_falls_back_to_the_process_key(wired: Any) -> None:
    """A process key here would let one agent comment on another's ticket."""

    state.api_key = "process-key"
    context = FakeToolContext({"agent_control": {"session_key": SESSION_KEY}})
    result = _save("x", context)
    assert result["saved"] is False
    assert wired.requests == []


def test_empty_text_costs_no_round_trip(wired: Any) -> None:
    assert _save("   ", FakeToolContext(_identity_state()))["saved"] is False
    assert wired.requests == []


def test_an_unreachable_control_plane_is_a_result(plane: Any) -> None:
    state.server_url = "http://127.0.0.1:1"
    result = _save("x", FakeToolContext(_identity_state()))
    assert result["saved"] is False
    assert "could not be reached" in result["message"]


def test_a_nonsense_body_is_a_result(wired: Any) -> None:
    wired.raw = b"not json"
    result = _save("x", FakeToolContext(_identity_state()))
    assert result["saved"] is False


def test_a_refusal_carries_the_servers_own_sentence_through(wired: Any) -> None:
    """That text is what the agent repeats to whoever asked it to save."""

    wired.refuse(
        409,
        error_code="SESSION_HAS_NO_TRACKER_ISSUE",
        detail="This session was opened as a chat rather than for a task.",
        hint="Open the chat from the ticket.",
    )
    result = _save("x", FakeToolContext(_identity_state()))
    assert result["saved"] is False
    assert result["error_code"] == "SESSION_HAS_NO_TRACKER_ISSUE"
    assert "opened as a chat" in result["message"]
    assert "Open the chat from the ticket." in result["message"]


def test_a_successful_save_reports_the_issue_it_reached(wired: Any) -> None:
    result = _save("x", FakeToolContext(_identity_state()))
    assert result["issue_ref"] == "issue-uuid"
    assert result["issue_url"].endswith("/OPS-19/research")
    assert "not closed" in result["message"]
