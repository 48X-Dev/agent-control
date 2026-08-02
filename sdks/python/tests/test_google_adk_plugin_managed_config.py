"""End to end through the plugin: a saved prompt reaching a running agent.

``test_google_adk_managed_config.py`` drives the two mutation rules directly.
This file checks the wiring around them - that ``before_model_callback``
actually reads the refreshed snapshot, actually applies it to the request it was
handed, and actually resolves the agent object the flow will read.

The wiring is where a feature like this dies quietly. Every rule can be correct
and the operator's prompt can still never reach a model because the callback
reads a stale cache, resolves the wrong agent object, or applies the prompt
after the controls have already looked at the request.

Same ``sys.modules`` fake pattern as ``test_google_adk_plugin.py``, extended so
the callback context can hand back an invocation context whose ``.agent`` is a
mutable object - which is what the real shallow ``model_copy`` gives the plugin.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_control import ControlSteerError
from agent_control._state import state
from agent_control.agent_config import AgentConfigSnapshot

MANAGED_BODY = "Write like a marketing copywriter. Never use emoji."
BASELINE = 'You are a city guide.\n\nYou are an agent. Your internal name is "x".'


class MockBasePlugin:
    def __init__(self, name: str | None = None):
        self.base_name = name


class MockPart:
    def __init__(self, text: str | None = None):
        self.text = text


class MockContent:
    def __init__(self, role: str = "user", parts: list[object] | None = None):
        self.role = role
        self.parts = parts or []


class MockConfig:
    def __init__(self, system_instruction: str | None = None):
        self.system_instruction = system_instruction


class MockLlmRequest:
    def __init__(
        self,
        text: str = "hello",
        config: object | None = None,
        request_id: str | None = None,
        model: str = "code-declared-model",
    ):
        self.contents = [SimpleNamespace(parts=[MockPart(text)])]
        self.config = config if config is not None else MockConfig()
        self.request_id = request_id
        self.model = model


class MockLlmResponse:
    def __init__(self, content: object, request_id: str | None = None):
        self.content = content
        self.request_id = request_id


class MockTool:
    def __init__(self, name: str, description: str = ""):
        self.name = name
        self.description = description


class MockToolContext:
    def __init__(self, agent_name=None, callback_context=None):
        self.agent_name = agent_name
        self.callback_context = callback_context


class MockAgent:
    """An ``LlmAgent`` stand-in whose ``model`` is a plain mutable field."""

    def __init__(self, name: str, model: object = "code-declared-model"):
        self.name = name
        self.description = f"{name} desc"
        self.model = model


class MockCallbackContext:
    """Exposes ``get_invocation_context`` the way the real surface does.

    The real method returns a ``model_copy`` of the invocation context. The copy
    is shallow, so ``.agent`` on it is the *same* object the flow will read when
    it resolves the model - which is the only reason assigning to it works.
    """

    def __init__(self, agent_name: str, invocation_id: str | None = None):
        self.agent_name = agent_name
        self.invocation_id = invocation_id
        self.agent = MockAgent(agent_name)

    def get_invocation_context(self) -> object:
        return SimpleNamespace(agent=self.agent)


def _install_google_modules() -> None:
    google_mod = ModuleType("google")
    adk_mod = ModuleType("google.adk")
    callback_context_mod = ModuleType("google.adk.agents.callback_context")
    models_mod = ModuleType("google.adk.models")
    plugins_mod = ModuleType("google.adk.plugins")
    tools_mod = ModuleType("google.adk.tools")
    tool_context_mod = ModuleType("google.adk.tools.tool_context")
    genai_mod = ModuleType("google.genai")
    types_mod = ModuleType("google.genai.types")

    callback_context_mod.CallbackContext = MockCallbackContext
    models_mod.LlmRequest = MockLlmRequest
    models_mod.LlmResponse = MockLlmResponse
    plugins_mod.BasePlugin = MockBasePlugin
    tools_mod.BaseTool = MockTool
    tool_context_mod.ToolContext = MockToolContext
    types_mod.Content = MockContent
    types_mod.Part = MockPart
    genai_mod.types = types_mod

    sys.modules["google"] = google_mod
    sys.modules["google.adk"] = adk_mod
    sys.modules["google.adk.agents.callback_context"] = callback_context_mod
    sys.modules["google.adk.models"] = models_mod
    sys.modules["google.adk.plugins"] = plugins_mod
    sys.modules["google.adk.tools"] = tools_mod
    sys.modules["google.adk.tools.tool_context"] = tool_context_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = types_mod


@pytest.fixture
def plugin_module():
    _install_google_modules()
    for name in (
        "agent_control.integrations.google_adk._extractors",
        "agent_control.integrations.google_adk.plugin",
    ):
        sys.modules.pop(name, None)
    module = importlib.import_module("agent_control.integrations.google_adk.plugin")
    yield module
    for name in (
        "agent_control.integrations.google_adk._extractors",
        "agent_control.integrations.google_adk.plugin",
    ):
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def reset_state():
    saved = (
        state.current_agent,
        state.server_url,
        state.api_key,
        state.server_controls,
        state.agent_config,
        state.model_max_staleness_seconds,
    )
    state.current_agent = None
    state.server_url = None
    state.api_key = None
    state.server_controls = None
    state.agent_config = None
    state.model_max_staleness_seconds = None
    yield
    (
        state.current_agent,
        state.server_url,
        state.api_key,
        state.server_controls,
        state.agent_config,
        state.model_max_staleness_seconds,
    ) = saved


def _publish(**overrides: object) -> AgentConfigSnapshot:
    payload: dict[str, object] = {
        "body": MANAGED_BODY,
        "prompt_enabled": True,
        "prompt_source": "managed",
        "model_id": None,
        "model_provider": None,
        "model_source": "code",
        "delivery_state": "active",
        "etag": "v1-aabbccddeeff",
        "current_version": 1,
    }
    payload.update(overrides)
    snapshot = AgentConfigSnapshot.from_response(
        payload, fetched_at=dt.datetime.now(dt.UTC)
    )
    state.agent_config = snapshot
    return snapshot


async def _enter(plugin, context, request):
    with patch.object(
        sys.modules["agent_control.integrations.google_adk.plugin"],
        "_evaluate_and_enforce",
        AsyncMock(return_value=MagicMock()),
    ), patch.object(plugin, "_schedule_step_sync"):
        return await plugin.before_model_callback(
            callback_context=context, llm_request=request
        )


# ---------------------------------------------------------------------------
# The prompt reaching a live request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_saved_prompt_reaches_the_request_through_the_callback(
    plugin_module,
) -> None:
    """The user's sentence, end to end on the delivery side.

    An admin saved a prompt, the refresh loop published it, and the next model
    call carries it - with no restart and no redeploy.
    """
    _publish()
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = MockLlmRequest("hello", config=MockConfig(BASELINE))

    await _enter(plugin, MockCallbackContext("marketing-copywriter"), request)

    assert MANAGED_BODY in request.config.system_instruction
    assert request.config.system_instruction.startswith(BASELINE)


@pytest.mark.asyncio
async def test_an_agent_with_no_saved_prompt_is_left_exactly_as_it_was(
    plugin_module,
) -> None:
    """Nothing changes on any deployment until somebody deliberately saves."""
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = MockLlmRequest("hello", config=MockConfig(BASELINE))

    await _enter(plugin, MockCallbackContext("marketing-copywriter"), request)

    assert request.config.system_instruction == BASELINE


@pytest.mark.asyncio
async def test_a_gated_server_delivers_nothing_even_with_a_body_on_the_wire(
    plugin_module,
) -> None:
    """The startup gate is enforced server-side and obeyed here without re-deriving.

    The body is still in the payload - storage is never gated - so an SDK that
    decided for itself would apply it. It reads ``prompt_source`` instead.
    """
    _publish(prompt_source="code", delivery_state="blocked_insecure_auth")
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = MockLlmRequest("hello", config=MockConfig(BASELINE))

    await _enter(plugin, MockCallbackContext("marketing-copywriter"), request)

    assert request.config.system_instruction == BASELINE


@pytest.mark.asyncio
async def test_clearing_the_prompt_puts_the_code_declaration_back(
    plugin_module,
) -> None:
    """Live, on the next model call, with no restart."""
    _publish()
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("marketing-copywriter")
    request = MockLlmRequest("hello", config=MockConfig(BASELINE))

    await _enter(plugin, context, request)
    assert MANAGED_BODY in request.config.system_instruction

    _publish(body=None, prompt_source="none")
    await _enter(plugin, context, request)

    assert request.config.system_instruction == BASELINE


@pytest.mark.asyncio
async def test_steering_guidance_survives_a_managed_prompt_through_the_real_steer_path(
    plugin_module,
) -> None:
    """The whole callback, the real steer, and the real re-entry.

    A control raising a steer makes the plugin append guidance and return
    ``None``, which is what makes ADK re-issue the request against the same
    object. If the managed prompt rule ran wholesale, the guidance would be gone
    on that second pass and the control would silently have had no effect.
    """
    _publish()
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("marketing-copywriter", invocation_id="inv-1")
    request = MockLlmRequest(
        "hello", config=MockConfig(BASELINE), request_id="call-1"
    )

    with patch.object(
        plugin_module,
        "_evaluate_and_enforce",
        AsyncMock(
            side_effect=ControlSteerError(
                control_name="c1",
                message="Steer",
                steering_context="Rewrite safely",
            )
        ),
    ):
        steered = await plugin.before_model_callback(
            callback_context=context, llm_request=request
        )
    assert steered is None
    assert "Rewrite safely" in request.config.system_instruction

    # ADK re-issues the request: the callback runs again on the same object.
    await _enter(plugin, context, request)

    instruction = request.config.system_instruction
    assert instruction.count("<agent_control_guidance>") == 1
    assert instruction.rstrip().endswith("</agent_control_guidance>")
    assert MANAGED_BODY in instruction
    assert instruction.index("agent_control_system_prompt") < instruction.index(
        "agent_control_guidance"
    )


# ---------------------------------------------------------------------------
# The model reaching a live request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_callback_resolves_the_agent_object_the_flow_will_read(
    plugin_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the public invocation context, not by reaching into privates.

    The copy is shallow, so mutating ``.agent`` on it mutates the object the
    flow resolves the model from a moment later. Reading the wrong object is the
    failure mode where every save succeeds, the UI reports a model change, and
    the agent keeps calling the old vendor.
    """
    monkeypatch.setenv("AGENT_CONTROL_MODEL_BASE_URL", "http://127.0.0.1:10531/v1")
    _publish(
        model_id="gpt-5.4-mini",
        model_provider="openai_compatible",
        model_source="managed",
    )
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("marketing-copywriter")
    constructed = SimpleNamespace(model="openai/gpt-5.4-mini")
    plugin._managed_config._model_cache[
        ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
    ] = constructed
    request = MockLlmRequest("hello")

    await _enter(plugin, context, request)

    assert context.agent.model is constructed
    assert request.model == "openai/gpt-5.4-mini"


@pytest.mark.asyncio
async def test_an_unmanaged_model_leaves_the_agent_on_what_its_code_declares(
    plugin_module,
) -> None:
    _publish()
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("marketing-copywriter")
    request = MockLlmRequest("hello")

    await _enter(plugin, context, request)

    assert context.agent.model == "code-declared-model"
    assert request.model == "code-declared-model"


@pytest.mark.asyncio
async def test_a_broken_snapshot_never_takes_a_model_call_down(
    plugin_module,
) -> None:
    """A configuration feature that can break an agent is worse than one that does nothing.

    So the applier is called inside its own guard, and a snapshot the SDK cannot
    make sense of costs a log line rather than a turn.
    """
    _publish()
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = MockLlmRequest("hello", config=MockConfig(BASELINE))

    with patch.object(
        plugin._managed_config,
        "apply_prompt",
        side_effect=RuntimeError("something in the applier broke"),
    ):
        result = await _enter(plugin, MockCallbackContext("x-agent-name"), request)

    assert result is None
    assert request.config.system_instruction == BASELINE
