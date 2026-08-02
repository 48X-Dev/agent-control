"""The two tools an agent uses to say what it is doing.

Driven against a **real HTTP server** on a real socket rather than a patched
client. The three things worth checking about these tools are the path they
address, the credential they carry and the fact that nothing they do can take a
turn down, and a stubbed transport would let all three be wrong while the tests
stayed green.

What is pinned:

* identity comes from ADK session state and never from an argument, so a model
  cannot name a session it should not write to - it never names one at all;
* the token on the wire is the session-bound one, and the per-turn token wins
  over the one seeded at session creation;
* **nothing raises, ever** - no state, no token, no server, a server that hangs
  up, a server answering nonsense, and a refusal all come back as ordinary tool
  results the model can read;
* a refusal carries Agent Control's own hand-written ``detail`` and ``hint``
  through to the model, because that text is what teaches an agent which
  revision is current and how many steps its plan actually has;
* the revision is remembered in session state, so a model that omits it marks a
  step of the plan it declared rather than of whatever is newest;
* a plan the tool can already tell is invalid costs a sentence back to the
  model rather than a round trip, and no request leaves the process.

The ADK wiring at the bottom runs against a real ``google-adk`` and skips
otherwise. The skip is the honest outcome: the rest of this file proves the
tool bodies, and only the real package can prove that ``tool_context`` is kept
out of the declaration a model sees.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from agent_control._state import state
from agent_control.integrations.google_adk import progress_tools

SESSION_KEY = "sess-refunds"
SEEDED_TOKEN = "tok-seeded-at-creation"
TURN_TOKEN = "tok-minted-for-this-turn"


# ---------------------------------------------------------------------------
# A real control plane on a real socket
# ---------------------------------------------------------------------------


class _Recorder:
    """Every request the tools made, and the canned answers they got."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.body: dict[str, Any] = {}
        self.raw: bytes | None = None

    def plan(self, revision: int, steps: list[str]) -> None:
        self.status = 200
        self.raw = None
        self.body = {
            "session_key": SESSION_KEY,
            "plan": {
                "session_key": SESSION_KEY,
                "revision": revision,
                "revision_count": revision,
                "steps": [
                    {
                        "index": i,
                        "title": title,
                        "status": "pending",
                        "note": None,
                        "updated_at": "2026-08-02T12:00:00Z",
                    }
                    for i, title in enumerate(steps)
                ],
                "declared_at": "2026-08-02T12:00:00Z",
                "last_updated_at": "2026-08-02T12:00:00Z",
            },
        }

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
    recorder.plan(1, ["a"])

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(payload or b"{}")
            except ValueError:
                parsed = None
            recorder.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": parsed,
                }
            )
            if recorder.raw is not None:
                self.send_response(recorder.status)
                self.send_header("Content-Type", "application/json")
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

        do_PUT = _handle  # noqa: N815 - BaseHTTPRequestHandler's naming
        do_PATCH = _handle  # noqa: N815
        do_POST = _handle  # noqa: N815

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
    saved = (state.current_agent, state.server_url, state.api_key)
    state.current_agent = None
    state.server_url = None
    state.api_key = None
    yield
    (state.current_agent, state.server_url, state.api_key) = saved


@pytest.fixture
def wired(plane: Any) -> Any:
    state.server_url = plane.url
    return plane


class FakeToolContext:
    """Just enough of ADK's ``ToolContext``: a state mapping the tools can read.

    Deliberately a plain dict subclass rather than a mock, because the tools
    write back into it and a mock would record the write instead of storing it.
    """

    def __init__(self, session_state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = dict(session_state or {})


def _identity_state(
    *, session_key: str = SESSION_KEY, turn_token: str | None = TURN_TOKEN
) -> dict[str, Any]:
    seeded: dict[str, Any] = {
        "agent_control": {
            "session_key": session_key,
            "namespace_key": "default",
            "agent_name": "support-bot",
            "trace_id": "trace-1",
            "runtime_token": SEEDED_TOKEN,
        }
    }
    if turn_token is not None:
        seeded["agent_control_turn"] = {
            "session_key": session_key,
            "trace_id": "trace-2",
            "runtime_token": turn_token,
        }
    return seeded


# ---------------------------------------------------------------------------
# Declaring
# ---------------------------------------------------------------------------


async def test_declaring_a_plan_addresses_the_session_from_state_under_its_own_token(
    wired: Any,
) -> None:
    """The path and the credential both come from state, not from the model.

    A tool taking ``session_key`` as an argument would hand a model the ability
    to write another conversation's plan, and the rail's "reported by the
    agent" label would be naming the wrong agent.
    """
    wired.plan(1, ["Read the ticket", "Draft the reply"])
    context = FakeToolContext(_identity_state())

    result = await progress_tools.declare_plan(
        ["Read the ticket", "Draft the reply"], tool_context=context
    )

    assert result["status"] == "recorded"
    assert result["plan_revision"] == 1
    assert result["step_count"] == 2
    (request,) = wired.requests
    assert request["method"] == "PUT"
    assert request["path"] == f"/api/v1/agent-sessions/{SESSION_KEY}/plan"
    assert request["body"] == {"steps": ["Read the ticket", "Draft the reply"]}
    # The per-turn token, not the one seeded at session creation: that one is
    # bound to the runtime TTL and an ADK session outlives it.
    assert request["authorization"] == f"Bearer {TURN_TOKEN}"


async def test_the_seeded_token_is_used_when_no_turn_token_has_arrived_yet(
    wired: Any,
) -> None:
    context = FakeToolContext(_identity_state(turn_token=None))

    await progress_tools.declare_plan(["A"], tool_context=context)

    assert wired.requests[0]["authorization"] == f"Bearer {SEEDED_TOKEN}"


async def test_blank_steps_are_dropped_and_a_plan_of_nothing_is_refused_locally(
    wired: Any,
) -> None:
    """A refusal the tool can make itself costs a sentence, not a round trip."""
    context = FakeToolContext(_identity_state())

    result = await progress_tools.declare_plan(["  ", ""], tool_context=context)

    assert result["status"] == "rejected"
    assert "at least one step" in result["message"]
    assert wired.requests == [], "nothing invalid should reach the control plane"


async def test_an_over_long_plan_is_refused_whole_and_never_sent(wired: Any) -> None:
    """Refused rather than truncated, and the message says how many it had.

    A silently shortened plan is a plan whose last step can never be marked,
    and the agent would have no way to discover that.
    """
    context = FakeToolContext(_identity_state())

    result = await progress_tools.declare_plan(
        [f"step {i}" for i in range(41)], tool_context=context
    )

    assert result["status"] == "rejected"
    assert "41" in result["message"]
    assert wired.requests == []


async def test_declaring_records_the_revision_in_session_state(wired: Any) -> None:
    wired.plan(3, ["A"])
    context = FakeToolContext(_identity_state())

    await progress_tools.declare_plan(["A"], tool_context=context)

    assert context.state[progress_tools.PLAN_REVISION_STATE_KEY] == 3
    # And the bookkeeping key is its own flat key, so it cannot shadow the
    # identity block the server seeded.
    assert context.state["agent_control"]["session_key"] == SESSION_KEY


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


async def test_marking_a_step_names_the_revision_and_the_index_in_the_path(
    wired: Any,
) -> None:
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(
        2, 1, "done", note="  found it  ", tool_context=context
    )

    assert result["status"] == "recorded"
    (request,) = wired.requests
    assert request["method"] == "PATCH"
    assert request["path"] == (
        f"/api/v1/agent-sessions/{SESSION_KEY}/plan/revisions/2/steps/1"
    )
    assert request["body"] == {"status": "done", "note": "found it"}


async def test_an_omitted_revision_falls_back_to_the_one_this_session_declared(
    wired: Any,
) -> None:
    """Zero means "the plan I declared", never "whatever is newest".

    Resolving it server-side against the latest revision is the exact bug the
    revision exists to prevent: a step of the new plan marked done because a
    step of the old one finished.
    """
    wired.plan(4, ["A", "B"])
    context = FakeToolContext(_identity_state())
    await progress_tools.declare_plan(["A", "B"], tool_context=context)

    await progress_tools.mark_step(0, 1, "active", tool_context=context)

    assert wired.paths[-1].endswith("/plan/revisions/4/steps/1")


async def test_marking_before_any_plan_exists_is_refused_without_a_request(
    wired: Any,
) -> None:
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(0, 0, "done", tool_context=context)

    assert result["status"] == "rejected"
    assert "Declare a plan first" in result["message"]
    assert wired.requests == []


async def test_an_omitted_note_is_left_out_of_the_body_rather_than_sent_empty(
    wired: Any,
) -> None:
    """Absent and empty are different: one leaves the note alone, one blanks it."""
    context = FakeToolContext(_identity_state())

    await progress_tools.mark_step(1, 0, "done", tool_context=context)

    assert wired.requests[0]["body"] == {"status": "done"}


@pytest.mark.parametrize("status", ["nearly-done", "", "DONE!", "complete"])
async def test_an_invented_status_costs_a_sentence_not_a_round_trip(
    wired: Any, status: str
) -> None:
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(1, 0, status, tool_context=context)

    assert result["status"] == "rejected"
    assert "pending, active, done, skipped, failed" in result["message"]
    assert wired.requests == []


async def test_a_status_the_model_shouted_is_still_understood(wired: Any) -> None:
    """Case and whitespace are the model's habit, not a different claim."""
    context = FakeToolContext(_identity_state())

    await progress_tools.mark_step(1, 0, "  Done  ", tool_context=context)

    assert wired.requests[0]["body"]["status"] == "done"


async def test_a_negative_step_index_is_refused_locally(wired: Any) -> None:
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(1, -1, "done", tool_context=context)

    assert result["status"] == "rejected"
    assert wired.requests == []


# ---------------------------------------------------------------------------
# Refusals from the server reach the model in the server's own words
# ---------------------------------------------------------------------------


async def test_a_stale_revision_reaches_the_model_with_the_current_one_named(
    wired: Any,
) -> None:
    """The refusal text is the correction mechanism, so it is passed through.

    An agent told only "409" retries the same wrong revision. Told "this
    session's plan is at revision 2", it marks the right one.
    """
    wired.refuse(
        409,
        error_code="PLAN_REVISION_STALE",
        detail="This session's plan is at revision 2, and the update named revision 1.",
        hint="Re-read the plan and mark steps of revision 2.",
    )
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(1, 0, "done", tool_context=context)

    assert result["status"] == "refused"
    assert result["error_code"] == "PLAN_REVISION_STALE"
    assert "revision 2" in result["message"]
    assert "Re-read the plan" in result["message"]


async def test_an_out_of_range_step_reaches_the_model_with_the_plans_real_shape(
    wired: Any,
) -> None:
    wired.refuse(
        422,
        error_code="PLAN_STEP_OUT_OF_RANGE",
        detail="Revision 1 of this plan has 3 steps, indexed 0 to 2.",
        hint="Mark a step the declared plan actually has.",
    )
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(1, 7, "done", tool_context=context)

    assert result["status"] == "refused"
    assert "3 steps" in result["message"]


async def test_a_refusal_with_no_readable_body_still_returns_a_usable_sentence(
    wired: Any,
) -> None:
    """A proxy's HTML error page must not become the model's instructions."""
    wired.status = 502
    wired.raw = b"<html>Bad Gateway</html>"
    context = FakeToolContext(_identity_state())

    result = await progress_tools.mark_step(1, 0, "done", tool_context=context)

    assert result["status"] == "refused"
    assert "Bad Gateway" not in result["message"]
    assert "Carry on with the work" in result["message"]


# ---------------------------------------------------------------------------
# Nothing here may take a turn down
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "broken_state",
    [
        pytest.param(None, id="no tool context at all"),
        pytest.param({}, id="empty session state"),
        pytest.param({"agent_control": {}}, id="identity block with nothing in it"),
        pytest.param(
            {"agent_control": {"session_key": SESSION_KEY}},
            id="a session but no credential to write with",
        ),
        pytest.param(
            {"agent_control": {"runtime_token": TURN_TOKEN}},
            id="a credential but no session",
        ),
    ],
)
async def test_missing_identity_degrades_to_a_result_and_never_an_exception(
    wired: Any, broken_state: dict[str, Any] | None
) -> None:
    """Progress reporting is an enhancement to work that is happening anyway.

    A tool that raised would take the turn with it, so an agent would lose a
    conversation because a rail could not be drawn. The absence of a token is
    handled the same way as the absence of a session: reporting under the
    process's own API key would let one agent rewrite another session's plan,
    which is the whole reason the token is session-bound.
    """
    context = None if broken_state is None else FakeToolContext(broken_state)

    declared = await progress_tools.declare_plan(["A"], tool_context=context)
    marked = await progress_tools.mark_step(1, 0, "done", tool_context=context)

    assert declared["status"] == "unavailable"
    assert marked["status"] in {"unavailable", "rejected"}
    assert wired.requests == [], "no credential means no request"


async def test_an_unconfigured_server_url_is_a_result_rather_than_a_crash() -> None:
    state.server_url = None
    context = FakeToolContext(_identity_state())

    result = await progress_tools.declare_plan(["A"], tool_context=context)

    assert result["status"] == "unavailable"
    assert "Carry on with the work" in result["message"]


async def test_an_unreachable_control_plane_is_a_result_rather_than_a_crash(
    plane: Any,
) -> None:
    """Pointed at a port nothing is listening on, so the socket really is refused."""
    del plane
    state.server_url = f"http://127.0.0.1:{_closed_port()}"
    context = FakeToolContext(_identity_state())

    declared = await progress_tools.declare_plan(["A"], tool_context=context)
    marked = await progress_tools.mark_step(1, 0, "done", tool_context=context)

    assert declared["status"] == "unavailable"
    assert marked["status"] == "unavailable"
    assert "Carry on with the work" in declared["message"]


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_session_state_that_refuses_to_be_written_does_not_break_the_call(
    wired: Any,
) -> None:
    """ADK state is somebody else's object, and it may not cooperate.

    Losing the remembered revision costs the model one argument it has to pass
    itself; losing the turn costs a conversation.
    """

    class HostileState(dict):
        def __setitem__(self, key: Any, value: Any) -> None:
            raise RuntimeError("no writes here")

    context = FakeToolContext()
    context.state = HostileState(_identity_state())  # type: ignore[assignment]

    result = await progress_tools.declare_plan(["A"], tool_context=context)

    assert result["status"] == "recorded"


async def test_two_tools_reporting_at_once_do_not_interfere(wired: Any) -> None:
    """One agent, several steps finishing together. Each write stands alone.

    The tools share no client and no mutable module state, so this is really a
    check that nothing was quietly added that they do.
    """
    context = FakeToolContext(_identity_state())

    results = await asyncio.gather(
        *(progress_tools.mark_step(1, i, "done", tool_context=context) for i in range(5))
    )

    assert [r["status"] for r in results] == ["recorded"] * 5
    marked = sorted(int(p.rsplit("/", 1)[1]) for p in wired.paths)
    assert marked == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# ADK wiring, against the real package
# ---------------------------------------------------------------------------


@pytest.fixture
def real_adk() -> Any:
    """The real ``google.adk``, with any fake evicted first.

    Sibling files inject fakes into ``sys.modules`` and never remove them, so
    importing here without evicting would hand back a fake and this section
    would silently test nothing.
    """
    import importlib
    import sys

    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name.split(".")[0] == "google"
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        module = importlib.import_module("google.adk.tools")
    except Exception:  # pragma: no cover - the default environment
        sys.modules.update(saved)
        pytest.skip("google-adk is not installed; run with --with 'google-adk[extensions]'")
    try:
        yield module
    finally:
        for name in list(sys.modules):
            if name.split(".")[0] == "google":
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def _declared_properties(tool: Any) -> set[str]:
    """What a model is shown for one tool, under either declaration shape.

    ADK emits ``parameters`` on some versions and ``parameters_json_schema`` on
    others; which one this build uses is not the thing under test, and pinning
    it would make this assertion break on an ADK upgrade that changed nothing
    about the contract.
    """
    declaration = tool._get_declaration()  # noqa: SLF001 - the shape under test
    assert declaration is not None
    if getattr(declaration, "parameters", None) is not None:
        return set(declaration.parameters.properties or {})
    return set((declaration.parameters_json_schema or {}).get("properties") or {})


def test_the_factory_builds_two_tools_a_model_can_actually_call(real_adk: Any) -> None:
    """And ``tool_context`` is kept out of what the model is shown.

    ADK injects it at call time. Leaving it in the declaration would invite a
    model to invent one, and the tool would then be reading the session
    identity out of a string the model made up - which is exactly the argument
    these tools refuse to take.
    """
    tools = progress_tools.build_progress_tools()

    assert [tool.name for tool in tools] == ["declare_plan", "mark_step"]
    declare, mark = tools
    assert _declared_properties(declare) == {"steps"}
    assert _declared_properties(mark) == {
        "plan_revision",
        "step_index",
        "status",
        "note",
    }
    for tool in tools:
        assert "tool_context" not in _declared_properties(tool)
        assert "session_key" not in _declared_properties(tool)


async def test_the_real_function_tool_injects_the_context_the_model_never_names(
    real_adk: Any, wired: Any
) -> None:
    """End to end through ADK's own call path, not through the bare function."""
    declare, _mark = progress_tools.build_progress_tools()
    context = FakeToolContext(_identity_state())

    result = await declare.run_async(
        args={"steps": ["A", "B"]}, tool_context=context  # type: ignore[arg-type]
    )

    assert result["status"] == "recorded"
    assert wired.requests[0]["body"] == {"steps": ["A", "B"]}
