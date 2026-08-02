"""Plugin-level tests for Phase 1 file-part handling.

Same ``sys.modules`` fake pattern as ``test_google_adk_plugin.py``, extended so
a ``Part`` can actually carry a file. The two structural refusals, the reserved
``agent_control`` context key and the per-invocation scanner all live in the
plugin, so they cannot be tested at the extractor level.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_control import ControlViolationError
from agent_control._state import state

PDF_BYTES = b"%PDF-1.7\n" + b"a" * 64
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()


class MockBasePlugin:
    def __init__(self, name: str | None = None):
        self.base_name = name


class MockBlob:
    def __init__(self, data=PDF_BYTES, mime_type="application/pdf", display_name="deck.pdf"):
        self.data = data
        self.mime_type = mime_type
        self.display_name = display_name


class MockFileData:
    def __init__(self, file_uri, mime_type="application/pdf", display_name="remote.pdf"):
        self.file_uri = file_uri
        self.mime_type = mime_type
        self.display_name = display_name


class MockPart:
    def __init__(self, text=None, inline_data=None, file_data=None, function_response=None):
        self.text = text
        self.inline_data = inline_data
        self.file_data = file_data
        self.function_response = function_response


class MockContent:
    def __init__(self, role: str = "user", parts: list[object] | None = None):
        self.role = role
        self.parts = parts or []


class MockConfig:
    def __init__(self, system_instruction: str | None = None):
        self.system_instruction = system_instruction


class MockLlmRequest:
    def __init__(self, contents=None, request_id: str | None = None):
        self.contents = (
            contents if contents is not None else [MockContent(parts=[MockPart("hello")])]
        )
        self.config = MockConfig()
        self.request_id = request_id


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


class MockCallbackContext:
    def __init__(self, agent_name: str, invocation_id: str | None = None, session_state=None):
        self.agent_name = agent_name
        self.invocation_id = invocation_id
        self.agent = SimpleNamespace(name=agent_name, description=f"{agent_name} desc")
        if session_state is not None:
            self.state = session_state


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
    types_mod.Blob = MockBlob
    types_mod.FileData = MockFileData
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


_MODULES = (
    "agent_control.integrations.google_adk._extractors",
    "agent_control.integrations.google_adk.plugin",
)


@pytest.fixture
def plugin_module():
    _install_google_modules()
    for name in _MODULES:
        sys.modules.pop(name, None)
    module = importlib.import_module("agent_control.integrations.google_adk.plugin")
    yield module
    for name in _MODULES:
        sys.modules.pop(name, None)


@pytest.fixture(autouse=True)
def reset_state():
    saved = (state.current_agent, state.server_url, state.api_key, state.server_controls)
    state.current_agent = None
    state.server_url = None
    state.api_key = None
    state.server_controls = None
    yield
    (state.current_agent, state.server_url, state.api_key, state.server_controls) = saved


def file_request(blob=None, file_data=None, tail_text="summarise this", request_id=None):
    """A request whose file sits at contents[0] and whose tail is a tool result."""

    parts = [MockPart("here it is")]
    if blob is not None:
        parts.append(MockPart(inline_data=blob))
    if file_data is not None:
        parts.append(MockPart(file_data=file_data))
    return MockLlmRequest(
        contents=[
            MockContent(role="user", parts=parts),
            MockContent(role="model", parts=[MockPart("calling a tool")]),
            MockContent(
                role="user",
                parts=[MockPart(function_response={"name": "t", "response": {"ok": True}})],
            ),
            MockContent(role="user", parts=[MockPart(tail_text)]),
        ],
        request_id=request_id,
    )


def no_eval(plugin_module):
    """Patch the engine round trip and assert on whether it was reached."""

    return patch.object(plugin_module, "_evaluate_and_enforce", AsyncMock(return_value=MagicMock()))


# --------------------------------------------------------------------------
# file_data: structurally unevaluatable, refused by default
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_data_is_blocked_by_default_without_reaching_the_engine(plugin_module):
    violations = []
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01",
        blocked_message_template="POLICY[{reason}]",
        on_violation_callback=lambda *args: violations.append(args),
    )
    context = MockCallbackContext("writer", invocation_id="inv-1")
    request = file_request(
        file_data=MockFileData("https://files.example.test/v1/x?token=BEARERSECRET"),
        request_id="call-1",
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(callback_context=context, llm_request=request)

    assert isinstance(result, MockLlmResponse)
    text = result.content.parts[0].text
    assert "file reference" in text
    # A structural SDK refusal is not a control verdict, so neither the
    # deployment's template nor its violation hook is involved.
    assert "POLICY[" not in text
    assert violations == []
    mock_eval.assert_not_awaited()
    # And no pending-call bookkeeping is left behind for after_model to trip on.
    assert plugin._request_text_by_call_key == {}
    assert plugin._current_llm_call_ids == {}


@pytest.mark.asyncio
async def test_file_data_is_blocked_even_when_it_is_not_at_the_tail(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = MockLlmRequest(
        contents=[
            MockContent(parts=[MockPart(file_data=MockFileData("gs://bucket/object"))]),
            MockContent(role="user", parts=[MockPart("what does it say?")]),
        ]
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=request,
        )

    assert isinstance(result, MockLlmResponse)
    mock_eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_data_refusal_leaks_neither_the_uri_nor_the_token(plugin_module, caplog):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = file_request(
        file_data=MockFileData("https://files.example.test/v1/blobs/abc?token=BEARERSECRET")
    )

    with (
        caplog.at_level(logging.DEBUG),
        no_eval(plugin_module),
        patch.object(plugin, "_schedule_step_sync"),
    ):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=request,
        )

    rendered = result.content.parts[0].text + caplog.text
    assert "BEARERSECRET" not in rendered
    assert "/v1/blobs/abc" not in rendered
    assert "remote.pdf" not in rendered


@pytest.mark.asyncio
async def test_file_data_can_be_allowed_explicitly(plugin_module):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01", file_data_parts="allow", unminted_file_parts="allow"
    )
    request = file_request(file_data=MockFileData("https://files.example.test/v1/x"))

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=request,
        )

    assert result is None
    mock_eval.assert_awaited_once()
    summary = mock_eval.await_args.kwargs["context"]["agent_control"]["attachment_summary"]
    assert summary["file_data_count"] == 1


# --------------------------------------------------------------------------
# unminted inline data
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unminted_inline_data_warns_by_default_and_still_evaluates(plugin_module, caplog):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    request = file_request(blob=MockBlob())

    with (
        caplog.at_level(logging.WARNING),
        no_eval(plugin_module) as mock_eval,
        patch.object(plugin, "_schedule_step_sync"),
    ):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=request,
        )

    assert result is None
    mock_eval.assert_awaited_once()
    assert "did not issue" in caplog.text
    assert "deck.pdf" not in caplog.text


@pytest.mark.asyncio
async def test_unminted_warning_is_deduplicated_within_an_invocation(plugin_module, caplog):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("writer", invocation_id="inv-1")
    blob = MockBlob()

    with (
        caplog.at_level(logging.WARNING),
        no_eval(plugin_module),
        patch.object(plugin, "_schedule_step_sync"),
    ):
        for _ in range(3):
            await plugin.before_model_callback(
                callback_context=context, llm_request=file_request(blob=blob)
            )

    assert caplog.text.count("did not issue") == 1


@pytest.mark.asyncio
async def test_unminted_block_refuses_without_a_control_verdict(plugin_module):
    violations = []
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01",
        unminted_file_parts="block",
        blocked_message_template="POLICY[{reason}]",
        on_violation_callback=lambda *args: violations.append(args),
    )
    request = file_request(blob=MockBlob(), request_id="call-1")

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=request,
        )

    assert isinstance(result, MockLlmResponse)
    assert "did not issue" in result.content.parts[0].text
    assert "POLICY[" not in result.content.parts[0].text
    assert violations == []
    mock_eval.assert_not_awaited()
    assert plugin._request_text_by_call_key == {}


@pytest.mark.asyncio
async def test_unminted_allow_emits_no_warning(plugin_module, caplog):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01", unminted_file_parts="allow"
    )

    with (
        caplog.at_level(logging.WARNING),
        no_eval(plugin_module),
        patch.object(plugin, "_schedule_step_sync"),
    ):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=file_request(blob=MockBlob()),
        )

    assert result is None
    assert "did not issue" not in caplog.text


# --------------------------------------------------------------------------
# the manifest, read from ADK session state
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "session_state",
    [
        {"agent_control": {"attachment_manifest": {PDF_SHA: "att_1"}}},
        {"agent_control.attachment_manifest": {PDF_SHA: "att_1"}},
    ],
    ids=["nested-block", "flat-dotted-key"],
)
@pytest.mark.asyncio
async def test_a_manifest_hit_marks_the_part_operator_minted(plugin_module, session_state):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01", unminted_file_parts="block"
    )
    context = MockCallbackContext("writer", invocation_id="inv-1", session_state=session_state)

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(
            callback_context=context, llm_request=file_request(blob=MockBlob())
        )

    assert result is None
    block = mock_eval.await_args.kwargs["context"]["agent_control"]
    assert block["attachments"][0]["source"] == "operator"
    assert block["attachments"][0]["attachment_id"] == "att_1"
    assert block["attachment_summary"]["unminted_count"] == 0


@pytest.mark.parametrize(
    "session_state",
    [
        {},
        {"agent_control": {}},
        {"agent_control": "not-a-dict"},
        {"agent_control": {"attachment_manifest": "not-a-dict"}},
        {"agent_control.attachment_manifest": {"0" * 64: "att_stale"}},
    ],
    ids=["empty", "no-manifest", "block-wrong-type", "manifest-wrong-type", "stale"],
)
@pytest.mark.asyncio
async def test_a_missing_or_stale_manifest_fails_closed(plugin_module, session_state):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("writer", invocation_id="inv-1", session_state=session_state)

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=context, llm_request=file_request(blob=MockBlob())
        )

    summary = mock_eval.await_args.kwargs["context"]["agent_control"]["attachment_summary"]
    assert summary["unminted_count"] == summary["count"] == 1


@pytest.mark.asyncio
async def test_a_state_object_that_raises_does_not_fail_the_model_call(plugin_module):
    class ExplodingState:
        def get(self, key):
            raise RuntimeError("state backend is down")

    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("writer", invocation_id="inv-1", session_state=ExplodingState())

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(
            callback_context=context, llm_request=file_request(blob=MockBlob())
        )

    assert result is None
    summary = mock_eval.await_args.kwargs["context"]["agent_control"]["attachment_summary"]
    assert summary["unminted_count"] == 1


# --------------------------------------------------------------------------
# per-invocation scanner: carried-over accounting and hash memoization
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_model_calls_in_one_invocation_hash_once_and_report_carry_over(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("writer", invocation_id="inv-1")
    blob = MockBlob()
    summaries = []

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        for _ in range(3):
            await plugin.before_model_callback(
                callback_context=context, llm_request=file_request(blob=blob)
            )
            summaries.append(
                mock_eval.await_args.kwargs["context"]["agent_control"]["attachment_summary"]
            )

    assert [s["count"] for s in summaries] == [1, 1, 1]
    assert [s["new_count"] for s in summaries] == [1, 0, 0]
    assert [s["carried_over_count"] for s in summaries] == [0, 1, 1]
    assert plugin._attachment_scanners["inv-1"].hash_cache.hashes_computed == 1


@pytest.mark.asyncio
async def test_a_second_invocation_gets_its_own_scanner(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    blob = MockBlob()

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=file_request(blob=blob),
        )
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-2"),
            llm_request=file_request(blob=blob),
        )

    summary = mock_eval.await_args.kwargs["context"]["agent_control"]["attachment_summary"]
    assert summary["new_count"] == 1
    assert set(plugin._attachment_scanners) == {"inv-1", "inv-2"}


@pytest.mark.asyncio
async def test_scanners_are_bounded_and_cleared_on_close(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")

    with no_eval(plugin_module), patch.object(plugin, "_schedule_step_sync"):
        for index in range(40):
            await plugin.before_model_callback(
                callback_context=MockCallbackContext("writer", invocation_id=f"inv-{index}"),
                llm_request=file_request(blob=MockBlob()),
            )

    assert len(plugin._attachment_scanners) <= 8

    await plugin.close()

    assert plugin._attachment_scanners == {}
    assert plugin._warned_attachments == set()


# --------------------------------------------------------------------------
# Step.input and Step.context
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_text_only_request_evaluates_the_same_string_as_before(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(),
        )

    assert mock_eval.await_args.kwargs["input"] == "hello"


@pytest.mark.asyncio
async def test_a_clean_request_still_carries_a_zeroed_agent_control_block(plugin_module):
    """``count max 0`` has to be evaluable on a request with no files in it."""

    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(),
        )

    block = mock_eval.await_args.kwargs["context"]["agent_control"]
    assert block["attachments"] == []
    assert block["attachment_summary"]["count"] == 0


@pytest.mark.asyncio
async def test_the_placeholder_reaches_step_input_but_the_bytes_do_not(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(
                contents=[MockContent(parts=[MockPart("look"), MockPart(inline_data=MockBlob())])]
            ),
        )

    step_input = mock_eval.await_args.kwargs["input"]
    assert isinstance(step_input, str)
    assert step_input.startswith("look\n[agent-control: attachment 1 of 1")
    assert "%PDF" not in step_input


@pytest.mark.asyncio
async def test_the_placeholder_can_be_switched_off(plugin_module):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01", attachment_placeholder_text=False
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(
                contents=[MockContent(parts=[MockPart("look"), MockPart(inline_data=MockBlob())])]
            ),
        )

    assert mock_eval.await_args.kwargs["input"] == "look"
    block = mock_eval.await_args.kwargs["context"]["agent_control"]
    assert block["attachment_summary"]["count"] == 1


@pytest.mark.asyncio
async def test_a_part_above_the_hash_cap_fails_closed_at_the_plugin(plugin_module):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01", attachment_hash_max_bytes=32
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=file_request(blob=MockBlob(data=b"%PDF-" + b"x" * 4096)),
        )

    block = mock_eval.await_args.kwargs["context"]["agent_control"]
    assert block["attachments"][0]["sha256"] is None
    assert block["attachments"][0]["size_bytes"] == 4101
    assert block["attachment_summary"]["unminted_count"] == 1
    assert plugin._attachment_scanners["inv-1"].hash_cache.hashes_computed == 0


# --------------------------------------------------------------------------
# the reserved context key
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extractor_context_is_merged_under_the_agent_control_block(plugin_module):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01",
        context_extractor=lambda **kwargs: {"tenant": "acme"},
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(),
        )

    context = mock_eval.await_args.kwargs["context"]
    assert context["tenant"] == "acme"
    assert context["agent_control"]["attachment_summary"]["count"] == 0


@pytest.mark.asyncio
async def test_an_extractor_supplied_agent_control_key_is_dropped(plugin_module):
    """The audited party must not author its own audit record."""

    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01",
        context_extractor=lambda **kwargs: {
            "agent_control": {"attachment_summary": {"count": 0, "unminted_count": 0}},
            "kept": True,
        },
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=file_request(blob=MockBlob()),
        )

    context = mock_eval.await_args.kwargs["context"]
    assert context["kept"] is True
    assert context["agent_control"]["attachment_summary"]["count"] == 1
    assert context["agent_control"]["attachment_summary"]["unminted_count"] == 1


@pytest.mark.asyncio
async def test_a_failing_extractor_does_not_take_the_agent_control_block_down(plugin_module):
    def boom(**kwargs):
        raise RuntimeError("extractor is broken")

    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01", context_extractor=boom)

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=file_request(blob=MockBlob()),
        )

    block = mock_eval.await_args.kwargs["context"]["agent_control"]
    assert block["attachment_summary"]["count"] == 1


@pytest.mark.asyncio
async def test_no_extractor_and_no_files_still_yields_a_selectable_block(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(),
        )

    assert set(mock_eval.await_args.kwargs["context"]) == {"agent_control"}


# --------------------------------------------------------------------------
# the response side, end to end through the plugin
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_after_model_describes_a_model_emitted_file(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("writer", invocation_id="inv-1")
    request = MockLlmRequest(request_id="call-1")
    response = MockLlmResponse(
        MockContent(role="model", parts=[MockPart("chart:"), MockPart(inline_data=MockBlob())]),
        request_id="call-1",
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(callback_context=context, llm_request=request)
        result = await plugin.after_model_callback(callback_context=context, llm_response=response)

    assert result is None
    block = mock_eval.await_args.kwargs["context"]["agent_control"]
    assert block["attachments"][0]["source"] == "agent"
    assert mock_eval.await_args.kwargs["input"] == "hello"
    assert "%PDF" not in mock_eval.await_args.kwargs["output"]


@pytest.mark.parametrize(
    "contents",
    [
        [MockContent(parts=[None])],
        [MockContent(parts=[MockPart(inline_data=SimpleNamespace())])],
        [MockContent(parts=[MockPart(inline_data=MockBlob(data=None, display_name=None))])],
        [MockContent(parts=[MockPart(inline_data=MockBlob(data="not-base64!!"))])],
        [MockContent(parts=[MockPart(file_data=MockFileData(""))])],
        [MockContent(parts="not-a-list")],
        [SimpleNamespace()],
    ],
    ids=[
        "null-part",
        "empty-blob",
        "blob-without-data",
        "blob-with-garbage-data",
        "file-data-empty-uri",
        "parts-not-a-list",
        "content-without-parts",
    ],
)
@pytest.mark.asyncio
async def test_malformed_parts_never_raise_into_the_callback(plugin_module, contents):
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01", file_data_parts="allow", unminted_file_parts="allow"
    )

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=MockLlmRequest(contents=contents),
        )

    assert result is None
    mock_eval.assert_awaited_once()
    assert isinstance(mock_eval.await_args.kwargs["input"], str)


@pytest.mark.asyncio
async def test_a_malformed_response_never_raises_into_after_model(plugin_module):
    plugin = plugin_module.AgentControlPlugin(agent_name="test-agent01")
    context = MockCallbackContext("writer", invocation_id="inv-1")

    with no_eval(plugin_module) as mock_eval, patch.object(plugin, "_schedule_step_sync"):
        await plugin.before_model_callback(
            callback_context=context, llm_request=MockLlmRequest(request_id="call-1")
        )
        result = await plugin.after_model_callback(
            callback_context=context,
            llm_response=MockLlmResponse(MockContent(parts=[None]), request_id="call-1"),
        )

    assert result is None
    assert mock_eval.await_args.kwargs["output"] == ""


@pytest.mark.asyncio
async def test_a_genuine_control_deny_still_uses_the_template_and_the_hook(plugin_module):
    """The structural refusals must not have broken the ordinary deny path."""

    violations = []
    plugin = plugin_module.AgentControlPlugin(
        agent_name="test-agent01",
        blocked_message_template="POLICY[{reason}]",
        on_violation_callback=lambda *args: violations.append(args),
    )

    with (
        patch.object(
            plugin_module,
            "_evaluate_and_enforce",
            AsyncMock(side_effect=ControlViolationError(control_name="c1", message="Denied")),
        ),
        patch.object(plugin, "_schedule_step_sync"),
    ):
        result = await plugin.before_model_callback(
            callback_context=MockCallbackContext("writer", invocation_id="inv-1"),
            llm_request=file_request(blob=MockBlob(), request_id="call-1"),
        )

    assert result.content.parts[0].text == "POLICY[Denied]"
    assert len(violations) == 1
