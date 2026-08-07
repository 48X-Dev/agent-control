"""Governance tests for MCP tools under the Google ADK plugin.

These settle, offline, what the live Exa spike settled against a real remote
server: that ``before_tool_callback`` sees an MCP tool the same way it sees a
local function, and that denying one stops the call before it leaves the
process.

Two things make this file different from its siblings. It runs against the
real, installed ``google-adk`` rather than the hand-written fakes the other
Google ADK test files inject, because the question is about ADK's own dispatch
path - a fake dispatcher would only prove that the fake calls the callback.
And it drives ``handle_function_calls_async`` rather than calling the plugin
hook directly, so the callback fires because ADK fired it.

Nothing here touches the network. The MCP boundary is a recording fake session
whose ``call_tool`` appends to a list, which is what makes the central claim
falsifiable: a blocked call is proved by the *absence* of a recorded
``tools/call``, and the allow-path test proves the recorder would have caught
one. No Exa API key exists in this environment and none is needed; the tests
assert that on the way past.

Run it the way the rest of the ADK contract work runs::

    uv run --no-sync --with "google-adk[extensions]" \\
        python -m pytest sdks/python/tests/test_google_adk_mcp_tools.py -q

``python -m pytest``, not ``pytest``: the console script resolves out of the
project venv, which does not carry the ``--with`` overlay, and the whole file
would then skip while looking like it ran.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_control import ControlViolationError
from agent_control._state import state

EXAMPLE_AGENT_PATH = (
    Path(__file__).resolve().parents[3] / "examples" / "google_adk_plugin" / "my_agent" / "agent.py"
)

# The two tools the example is allowed to carry, and the schema Exa publishes
# for the first of them (trimmed). Held here so the schema assertions do not
# depend on a live handshake.
SEARCH_TOOL_NAME = "web_search_exa"
SEARCH_TOOL_DESCRIPTION = "Search the web using Exa AI - performs real-time web searches"
SEARCH_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "numResults": {"type": "number", "description": "Number of results"},
    },
    "required": ["query"],
    "additionalProperties": False,
}

_OWNED_PREFIXES = ("google", "agent_control.integrations.google_adk")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adk():
    """Import the real google-adk, evicting any fake ``google.*`` first.

    The sibling ADK test files install hand-written modules into
    ``sys.modules["google.adk.*"]`` and never remove them. Importing here
    without evicting would hand back a fake ADK, and this file would test its
    author's guess about ADK's dispatch rather than ADK.
    """

    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name.split(".")[0] == "google" or name.startswith(_OWNED_PREFIXES[1])
    }
    for name in saved:
        sys.modules.pop(name, None)

    try:
        mcp_types = importlib.import_module("mcp.types")
        modules = SimpleNamespace(
            mcp_types=mcp_types,
            types=importlib.import_module("google.genai.types"),
            Event=importlib.import_module("google.adk.events.event").Event,
            LlmAgent=importlib.import_module("google.adk.agents.llm_agent").LlmAgent,
            InvocationContext=importlib.import_module(
                "google.adk.agents.invocation_context"
            ).InvocationContext,
            functions=importlib.import_module("google.adk.flows.llm_flows.functions"),
            PluginManager=importlib.import_module(
                "google.adk.plugins.plugin_manager"
            ).PluginManager,
            InMemorySessionService=importlib.import_module(
                "google.adk.sessions.in_memory_session_service"
            ).InMemorySessionService,
            McpTool=importlib.import_module("google.adk.tools.mcp_tool.mcp_tool").McpTool,
            mcp_tool_module=importlib.import_module("google.adk.tools.mcp_tool"),
            plugin_module=importlib.import_module("agent_control.integrations.google_adk.plugin"),
        )
    except Exception:  # pragma: no cover - exercised only without google-adk
        sys.modules.update(saved)
        pytest.skip("google-adk is not installed; run with --with 'google-adk[extensions]'")

    if getattr(modules.types, "Blob", None) is None:  # pragma: no cover - fake leaked
        sys.modules.update(saved)
        pytest.skip("google.genai.types looks like a test fake, not the real package")

    try:
        yield modules
    finally:
        for name in list(sys.modules):
            if name.split(".")[0] == "google" or name.startswith(_OWNED_PREFIXES[1]):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.fixture(autouse=True)
def reset_state():
    original = (
        state.current_agent,
        state.server_url,
        state.api_key,
        state.server_controls,
    )
    state.current_agent = None
    state.server_url = None
    state.api_key = None
    state.server_controls = None
    yield
    (
        state.current_agent,
        state.server_url,
        state.api_key,
        state.server_controls,
    ) = original


# ---------------------------------------------------------------------------
# the MCP boundary, faked at the one place a real call would leave the process
# ---------------------------------------------------------------------------


class RecordingMcpSession:
    """Stands in for a live MCP session and writes down every tool call.

    This is the observable the block test turns on. ADK's McpTool reaches the
    remote server through exactly one call - ``session.call_tool`` - so an
    empty ``calls`` list after a denied turn means no ``tools/call`` was ever
    formed, which is the offline equivalent of the recording proxy the live
    spike put in front of mcp.exa.ai.
    """

    def __init__(self, result_text: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result_text = result_text

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        progress_callback: Any = None,
        meta: Any = None,
    ) -> Any:
        from mcp.types import CallToolResult, TextContent

        self.calls.append({"name": name, "arguments": arguments})
        return CallToolResult(content=[TextContent(type="text", text=self._result_text)])


class RecordingMcpServer:
    """Session manager handing out one recording session."""

    def __init__(self, result_text: str = "UNMISTAKABLE-EXA-RESULT") -> None:
        self.session = RecordingMcpSession(result_text)
        self.sessions_created = 0

    async def create_session(self, headers: dict[str, str] | None = None) -> Any:
        self.sessions_created += 1
        return self.session

    def _get_session_context(self, headers: dict[str, str] | None = None) -> None:
        # ADK falls back to awaiting the call directly when this is not a real
        # SessionContext, which is the path a mock session manager takes.
        return None

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.session.calls


def _make_mcp_tool(
    adk: Any,
    server: RecordingMcpServer,
    *,
    name: str = SEARCH_TOOL_NAME,
    description: str = SEARCH_TOOL_DESCRIPTION,
    input_schema: dict[str, Any] | None = None,
) -> Any:
    """A real ``McpTool`` over a fake session - name and schema come from 'the server'."""

    spec = adk.mcp_types.Tool(
        name=name,
        description=description,
        inputSchema=input_schema if input_schema is not None else SEARCH_TOOL_INPUT_SCHEMA,
    )
    return adk.McpTool(mcp_tool=spec, mcp_session_manager=server)


def _make_plugin(adk: Any, agent_name: str = "test-agent01") -> Any:
    """A plugin whose step syncing is captured instead of posted."""

    plugin = adk.plugin_module.AgentControlPlugin(agent_name=agent_name)
    synced: list[dict[str, Any]] = []

    async def _capture_async(steps: Any, **_: Any) -> None:
        synced.extend(steps)

    def _capture_blocking(steps: Any, **_: Any) -> None:
        synced.extend(steps)

    plugin._sync_steps_async = _capture_async
    plugin._sync_steps_blocking = _capture_blocking
    plugin.synced_steps = synced
    return plugin


def _allow_everything(recorder: list[dict[str, Any]]) -> Any:
    async def _evaluate(
        agent_name: str,
        step_name: str,
        *,
        input: Any = None,
        output: Any = None,
        context: Any = None,
        step_type: str = "llm",
        stage: str = "pre",
    ) -> Any:
        recorder.append(
            {
                "agent_name": agent_name,
                "step_name": step_name,
                "input": input,
                "output": output,
                "step_type": step_type,
                "stage": stage,
            }
        )
        return MagicMock()

    return _evaluate


def _scoped_deny(
    recorder: list[dict[str, Any]],
    *,
    step_types: list[str],
    step_names: list[str],
    control_name: str = "no-web-search",
) -> Any:
    """An operator's scoped control, as written.

    Scope matching itself is the server's job - the SDK never reads
    ``step_names`` - so this is the control as an operator would write it, not
    the server's matcher. The half under test is the half the SDK owns: the
    step name and type the plugin presents for a remotely-named MCP tool. A
    control written against a name the plugin never presents matches nothing,
    and that is the point of the pair of tests below.
    """

    async def _evaluate(
        agent_name: str,
        step_name: str,
        *,
        input: Any = None,
        output: Any = None,
        context: Any = None,
        step_type: str = "llm",
        stage: str = "pre",
    ) -> Any:
        recorder.append({"step_name": step_name, "input": input, "step_type": step_type})
        if stage == "pre" and step_type in step_types and step_name in step_names:
            raise ControlViolationError(
                control_name=control_name,
                message=f"Control '{control_name}' denied this call",
            )
        return MagicMock()

    return _evaluate


async def _dispatch_tool_call(
    adk: Any,
    plugin: Any,
    tool: Any,
    args: dict[str, Any],
    evaluate: Any,
    *,
    agent_name: str = "root_agent",
) -> Any:
    """Run one function call through ADK's own tool dispatch, with the plugin installed."""

    agent = adk.LlmAgent(name=agent_name, model="gemini-2.0-flash", tools=[tool])
    session_service = adk.InMemorySessionService()
    session = await session_service.create_session(app_name="app", user_id="u")
    invocation_context = adk.InvocationContext(
        session_service=session_service,
        invocation_id="inv-1",
        agent=agent,
        session=session,
        plugin_manager=adk.PluginManager(plugins=[plugin]),
    )
    event = adk.Event(
        invocation_id="inv-1",
        author=agent_name,
        content=adk.types.Content(
            role="model",
            parts=[
                adk.types.Part(
                    function_call=adk.types.FunctionCall(id="fc-1", name=tool.name, args=args)
                )
            ],
        ),
    )

    original = adk.plugin_module._evaluate_and_enforce
    adk.plugin_module._evaluate_and_enforce = evaluate
    try:
        return await adk.functions.handle_function_calls_async(
            invocation_context, event, {tool.name: tool}
        )
    finally:
        adk.plugin_module._evaluate_and_enforce = original
        await plugin.close()


def _tool_response(event: Any) -> dict[str, Any]:
    return event.content.parts[0].function_response.response


# ---------------------------------------------------------------------------
# E1 - the callback fires, with the remote name and the real arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_before_tool_callback_fires_for_an_mcp_tool(adk):
    """ADK routes an MCP tool through the plugin hook like any other tool."""

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)
    seen: list[dict[str, Any]] = []

    await _dispatch_tool_call(
        adk, plugin, tool, {"query": "who runs DeepMind"}, _allow_everything(seen)
    )

    pre = [call for call in seen if call["stage"] == "pre"]
    assert len(pre) == 1
    # The remote server's own name, agent-qualified by the plugin - not a
    # wrapper class name, and not something ADK invented.
    assert pre[0]["step_name"] == "root_agent.web_search_exa"
    assert pre[0]["step_type"] == "tool"
    assert pre[0]["input"] == {"query": "who runs DeepMind"}


@pytest.mark.asyncio
async def test_allowed_mcp_call_reaches_the_server(adk):
    """The positive control: the recorder does catch a call when one happens.

    Without this, an empty ``calls`` list in the block test would prove
    nothing - a broken harness produces the same silence as a working control.
    """

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)

    event = await _dispatch_tool_call(
        adk, plugin, tool, {"query": "current time in Tokyo"}, _allow_everything([])
    )

    assert server.calls == [
        {"name": "web_search_exa", "arguments": {"query": "current time in Tokyo"}}
    ]
    assert "UNMISTAKABLE-EXA-RESULT" in str(_tool_response(event))


# ---------------------------------------------------------------------------
# E2 - blocking prevents the call, proved by absence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_mcp_call_never_reaches_the_server(adk):
    """A denied MCP call forms no ``tools/call`` at all.

    The assertion is the absence, not the returned dict. A blocked turn and a
    turn that ran and was then reported as blocked look identical from the
    response payload, which is exactly the trap the live spike had to avoid.
    """

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)

    event = await _dispatch_tool_call(
        adk,
        plugin,
        tool,
        {"query": "who runs DeepMind"},
        _scoped_deny([], step_types=["tool"], step_names=["root_agent.web_search_exa"]),
    )

    assert server.calls == []
    assert server.sessions_created == 0
    # Nothing the fake server could have returned is anywhere in the response.
    assert "UNMISTAKABLE-EXA-RESULT" not in str(_tool_response(event))
    assert _tool_response(event)["status"] == "blocked"


@pytest.mark.asyncio
async def test_denied_mcp_call_blocks_every_tool_in_the_allowlist(adk):
    """The fetch tool is governed on the same path as the search tool.

    ``web_fetch_exa`` is the wider injection channel of the two and it was the
    one the live spike did not spend a turn on, so it is pinned here.
    """

    server = RecordingMcpServer()
    tool = _make_mcp_tool(
        adk,
        server,
        name="web_fetch_exa",
        description="Extract content from a specific URL",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
    )
    plugin = _make_plugin(adk)

    await _dispatch_tool_call(
        adk,
        plugin,
        tool,
        {"url": "https://attacker.example/page"},
        _scoped_deny([], step_types=["tool"], step_names=["root_agent.web_fetch_exa"]),
    )

    assert server.calls == []


# ---------------------------------------------------------------------------
# E3 - what step registration actually does with a remotely-derived schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_step_registers_without_raising(adk):
    """``_ensure_step_known`` handles an MCP tool: it neither raises nor skips."""

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)

    await _dispatch_tool_call(adk, plugin, tool, {"query": "q"}, _allow_everything([]))

    steps = [step for step in plugin.synced_steps if step["type"] == "tool"]
    assert [step["name"] for step in steps] == ["root_agent.web_search_exa"]
    # The description does survive the round trip - it comes off the MCP tool
    # spec, so an operator does at least learn what the tool claims to do.
    assert steps[0]["description"] == SEARCH_TOOL_DESCRIPTION


@pytest.mark.asyncio
async def test_mcp_step_schema_is_a_fallback_not_the_published_one(adk):
    """Known gap, pinned deliberately: the registered schema says nothing.

    Exa publishes a usable JSON Schema and ADK keeps it on the tool, but
    ``_resolve_schema_source`` reads the wrapper's ``run_async`` signature -
    ``(*, args, tool_context)`` - which pydantic cannot model, so derivation
    falls back to a permissive object. Evaluation is unaffected: selectors read
    the live arguments, which the deny tests above exercise.

    If this test fails because the schema is now Exa's, that is the fix landing
    and this assertion is what should change.
    """

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)

    await _dispatch_tool_call(adk, plugin, tool, {"query": "q"}, _allow_everything([]))

    step = next(step for step in plugin.synced_steps if step["type"] == "tool")
    assert step["input_schema"] == {"type": "object", "additionalProperties": True}
    # The real schema was there the whole time, one attribute away.
    assert tool._mcp_tool.inputSchema == SEARCH_TOOL_INPUT_SCHEMA
    assert "query" not in str(step["input_schema"])


def test_bind_registers_the_toolset_object_not_its_tools(adk, monkeypatch):
    """Another known artefact: bind cannot see inside a toolset.

    ``_iter_tools`` walks ``agent.tools`` without expanding toolsets, so a
    toolset registers under its class name and the real tool names appear only
    when each tool first runs. This runs against the example's own agent
    because the README tells an operator exactly which names bind produces, and
    that claim is the reason the qualified name has to be typed rather than
    copied off a console.
    """

    # Loading the example stubs bind so the module's own bind call cannot reach
    # the server. Put the real one back before exercising it here.
    real_bind = adk.plugin_module.AgentControlPlugin.bind
    module = _load_example_agent(adk, monkeypatch, "example_agent_bind")
    monkeypatch.setattr(adk.plugin_module.AgentControlPlugin, "bind", real_bind)
    plugin = _make_plugin(adk)

    plugin.bind(module.root_agent)

    names = {step["name"] for step in plugin.synced_steps}
    assert "root_agent.WebToolset" in names
    assert "root_agent.function" in names  # both local functions, collapsed
    assert not any(name.endswith("web_search_exa") for name in names)
    assert not any(name.endswith("web_fetch_exa") for name in names)


# ---------------------------------------------------------------------------
# E4 - the step name a control has to be scoped to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_scoped_to_the_qualified_name_blocks_the_call(adk):
    """``root_agent.web_search_exa`` is expressible and it works."""

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)
    seen: list[dict[str, Any]] = []

    await _dispatch_tool_call(
        adk,
        plugin,
        tool,
        {"query": "anything"},
        _scoped_deny(seen, step_types=["tool"], step_names=["root_agent.web_search_exa"]),
    )

    assert server.calls == []
    assert seen[0]["step_name"] == "root_agent.web_search_exa"


@pytest.mark.asyncio
async def test_control_scoped_to_the_bare_remote_name_fails_open(adk):
    """The trap, pinned so nobody discovers it in production.

    ``web_search_exa`` is the name the transcript shows, the name the remote
    server reports, and the name an operator will type. The plugin never
    presents it: the step is agent-qualified. A control scoped to the bare name
    therefore matches nothing, logs nothing, and the search runs.

    This test asserts the current behaviour, not the desired one. If the SDK
    ever learns to warn on an unmatched step name, or the console starts
    offering the qualified name, this is the test that should change.
    """

    server = RecordingMcpServer()
    tool = _make_mcp_tool(adk, server)
    plugin = _make_plugin(adk)
    seen: list[dict[str, Any]] = []

    await _dispatch_tool_call(
        adk,
        plugin,
        tool,
        {"query": "anything"},
        _scoped_deny(seen, step_types=["tool"], step_names=["web_search_exa"]),
    )

    assert server.calls == [{"name": "web_search_exa", "arguments": {"query": "anything"}}]
    assert seen[0]["step_name"] != "web_search_exa"


# ---------------------------------------------------------------------------
# the example's wiring: what is attached, what is not, and what happens when
# the remote server is not there
# ---------------------------------------------------------------------------


def _load_example_agent(adk: Any, monkeypatch: Any, module_name: str) -> Any:
    """Execute examples/google_adk_plugin/my_agent/agent.py in isolation.

    ``agent_control.init`` and ``plugin.bind`` are the module's two calls that
    would reach the Agent Control server; both are stubbed. Nothing here
    reaches Exa either - the toolset is constructed but never connected.
    """

    import agent_control

    monkeypatch.setattr(agent_control, "init", MagicMock())
    monkeypatch.setattr(adk.plugin_module.AgentControlPlugin, "bind", MagicMock())
    # A LiteLLM route would try to build a real client; the plain-model branch
    # is what the wiring under test needs.
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("AGENT_CONTROL_AGENT_NAME", "test-agent01")
    monkeypatch.setenv("EXA_MCP_URL", "http://127.0.0.1:9/mcp")

    spec = importlib.util.spec_from_file_location(module_name, EXAMPLE_AGENT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_example_attaches_web_tools_alongside_the_local_ones(adk, monkeypatch):
    module = _load_example_agent(adk, monkeypatch, "example_agent_web_on")

    tools = module.root_agent.tools
    assert module.get_current_time in tools
    assert module.get_weather in tools

    toolset = tools[-1]
    assert isinstance(toolset, adk.mcp_tool_module.McpToolset)
    # An allowlist, not a convenience: whatever Exa publishes next does not
    # arrive on its own.
    assert toolset.tool_filter == ["web_search_exa", "web_fetch_exa"]
    assert module.EXA_TOOL_ALLOWLIST == ["web_search_exa", "web_fetch_exa"]


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "FALSE", " Off "])
def test_example_omits_web_tools_when_opted_out(adk, monkeypatch, value):
    monkeypatch.setenv("AGENT_CONTROL_WEB_TOOLS", value)

    module = _load_example_agent(adk, monkeypatch, f"example_agent_off_{abs(hash(value))}")

    assert module.root_agent.tools == [module.get_current_time, module.get_weather]
    assert module._build_web_toolset() is None


def test_example_leaves_the_knowledge_tools_off_until_asked(adk, monkeypatch):
    """The egress pair is never a default, and this is where that is enforced.

    This example ships the web tools on. Attaching retrieval beside them makes
    a channel where an injected instruction can pull corpus text into a query
    somebody else composed, so the pairing has to be an operator's written
    decision rather than what happens when nobody chooses.
    """
    monkeypatch.delenv("AGENT_CONTROL_KNOWLEDGE_TOOLS", raising=False)

    module = _load_example_agent(adk, monkeypatch, "example_agent_knowledge_default")

    names = [getattr(tool, "__name__", "") for tool in module.root_agent.tools]
    assert "company_knowledge_search" not in names
    assert module._build_knowledge_tools() == []
    assert "company_knowledge" not in module.root_agent.instruction


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", "TRUE", " On "])
def test_example_attaches_both_knowledge_tools_when_asked(adk, monkeypatch, value):
    monkeypatch.setenv("AGENT_CONTROL_KNOWLEDGE_TOOLS", value)
    monkeypatch.setenv("AGENT_CONTROL_WEB_TOOLS", "0")

    module = _load_example_agent(adk, monkeypatch, f"example_agent_kn_{abs(hash(value))}")

    tools = module.root_agent.tools
    assert [getattr(tool, "func", tool).__name__ for tool in tools[-2:]] == [
        "company_knowledge_search",
        "company_knowledge_recent",
    ]
    # The brief the model reads has to name the tools it now holds, or they sit
    # there unused and the agent answers company questions from its weights.
    assert "company_knowledge_search" in module.root_agent.instruction
    assert "cite the path" in module.root_agent.instruction.lower()


def test_example_needs_no_exa_credentials(adk, monkeypatch):
    """No key is configured, and a key in the environment is not picked up.

    There is no Exa key in this environment and the free endpoint does not want
    one. A future change that starts reading one should fail here rather than
    quietly start sending it.
    """

    monkeypatch.setenv("EXA_API_KEY", "sentinel-key-must-not-be-used")

    module = _load_example_agent(adk, monkeypatch, "example_agent_no_key")

    toolset = module.root_agent.tools[-1]
    params = toolset._connection_params
    assert params.headers is None
    assert getattr(toolset, "_auth_scheme", None) is None
    assert getattr(toolset, "_auth_credential", None) is None
    assert "sentinel-key-must-not-be-used" not in repr(params)


def test_example_survives_a_toolset_that_cannot_be_built(adk, monkeypatch, caplog):
    """A missing extension or a bad URL costs the web tools, not the process."""

    def _explode(*_: Any, **__: Any) -> Any:
        raise RuntimeError("mcp extension not installed")

    monkeypatch.setattr(adk.mcp_tool_module, "McpToolset", _explode)

    with caplog.at_level("WARNING"):
        module = _load_example_agent(adk, monkeypatch, "example_agent_build_fails")

    assert module.root_agent.tools == [module.get_current_time, module.get_weather]
    assert "Web tools are off" in caplog.text


@pytest.mark.asyncio
async def test_example_degrades_when_the_mcp_server_is_unreachable(adk, monkeypatch, caplog):
    """An unreachable server yields no tools, not a failed turn.

    ADK asks the toolset for its tools while assembling the tool list for a
    turn, and an unguarded ``McpToolset`` raises ``ConnectionError`` there -
    which fails the whole turn, including the questions ``get_weather`` could
    have answered without any web access at all.
    """

    module = _load_example_agent(adk, monkeypatch, "example_agent_unreachable")
    toolset = module.root_agent.tools[-1]

    async def _refuse(*_: Any, **__: Any) -> Any:
        raise ConnectionError("Failed to create MCP session: All connection attempts failed")

    # Patch the parent's method: the example's subclass is the thing under
    # test, and it delegates to this.
    monkeypatch.setattr(adk.mcp_tool_module.McpToolset, "get_tools", _refuse)

    with caplog.at_level("WARNING"):
        tools = await toolset.get_tools()

    assert tools == []
    assert "Web tools unavailable" in caplog.text
