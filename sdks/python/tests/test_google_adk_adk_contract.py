"""Contract tests against a real, pinned google-adk.

Every other Google ADK test file in this suite injects hand-written fakes into
``sys.modules["google.adk.*"]``, so it verifies this repo's *fiction* of ADK.
That is fine for logic and useless for shape. If ``Blob.data`` were base64 text
rather than ``bytes``, every hash the SDK computes would be over the wrong
input, the manifest could never hit, ``source`` would be permanently
``unknown`` - and the fakes would pass.

This file settles the shape questions the plan flags, against the installed
package:

    uv run --package agent-control-sdk --with "google-adk[extensions]" \\
        python -m pytest sdks/python/tests/test_google_adk_adk_contract.py -q

``python -m pytest`` rather than ``pytest``: the console script resolves out of
the project venv, which does not carry the overlay ``--with`` installs, and the
whole file would then skip while looking like it ran. The extra is
``extensions``, not ``litellm``.

It skips when google-adk is not installed, which is the default for
``pytest sdks/python/tests``. The skip is the honest outcome, not a pass:
without this job the fakes prove only that they match their author's guess.
"""

from __future__ import annotations

import hashlib
import importlib
import sys

import pytest

PDF_BYTES = b"%PDF-1.7\n" + b"a" * 64
PDF_SHA = hashlib.sha256(PDF_BYTES).hexdigest()

_OWNED_PREFIXES = ("google", "agent_control.integrations.google_adk")


@pytest.fixture
def adk():
    """Evict any fake ``google.*`` modules and import the real package.

    The fake-installing fixtures in the sibling test files never remove what
    they put in ``sys.modules``, and pytest collects this file after some of
    them, so importing ``google.genai`` here without evicting first would hand
    back a fake and this file would silently test nothing.
    """

    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name.split(".")[0] == "google" or name.startswith(_OWNED_PREFIXES[1])
    }
    for name in saved:
        sys.modules.pop(name, None)

    try:
        types = importlib.import_module("google.genai.types")
    except Exception:  # pragma: no cover - exercised only without google-adk
        sys.modules.update(saved)
        pytest.skip("google-adk is not installed; run with --with 'google-adk[extensions]'")

    if getattr(types, "Blob", None) is None:  # pragma: no cover - fake leaked through
        sys.modules.update(saved)
        pytest.skip("google.genai.types looks like a test fake, not the real package")

    try:
        yield types
    finally:
        for name in list(sys.modules):
            if name.split(".")[0] == "google" or name.startswith(_OWNED_PREFIXES[1]):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.fixture
def scanner_module(adk):
    return importlib.import_module("agent_control.integrations.google_adk._attachments")


@pytest.fixture
def extractors(adk):
    return importlib.import_module("agent_control.integrations.google_adk._extractors")


# --------------------------------------------------------------------------
# the three shape assumptions the plan names
# --------------------------------------------------------------------------


def test_part_exposes_inline_data_and_file_data(adk):
    fields = set(adk.Part.model_fields)

    assert "inline_data" in fields
    assert "file_data" in fields
    # snake_case is the attribute name; camelCase is only the wire alias.
    assert "inlineData" not in fields


def test_blob_exposes_mime_type_and_data_and_display_name(adk):
    fields = set(adk.Blob.model_fields)

    assert {"mime_type", "data"} <= fields
    assert "display_name" in fields


def test_blob_data_is_bytes_not_base64_text(adk):
    blob = adk.Blob(data=PDF_BYTES, mime_type="application/pdf", display_name="deck.pdf")

    assert isinstance(blob.data, bytes)
    assert blob.data == PDF_BYTES
    assert hashlib.sha256(blob.data).hexdigest() == PDF_SHA


def test_file_data_exposes_file_uri_and_mime_type(adk):
    fields = set(adk.FileData.model_fields)

    assert {"file_uri", "mime_type"} <= fields


def test_model_dump_base64s_the_whole_file_which_is_why_it_is_not_used(adk):
    """Justifies reading named fields instead of reusing the generic serializer.

    ``_to_jsonable`` calls ``model_dump(mode="json")``. On a ``Blob`` that
    inlines the entire file as base64 into whatever string the controls
    evaluate. A 20MB PDF would become a 27MB ``Step.input``.
    """

    dumped = adk.Blob(data=PDF_BYTES, mime_type="application/pdf").model_dump(mode="json")

    assert isinstance(dumped["data"], str)
    assert len(dumped["data"]) > len(PDF_BYTES)


# --------------------------------------------------------------------------
# the walker, driven by real objects
# --------------------------------------------------------------------------


def test_scanner_describes_a_real_part(adk, scanner_module):
    part = adk.Part(
        inline_data=adk.Blob(data=PDF_BYTES, mime_type="application/pdf", display_name="deck.pdf")
    )

    (descriptor,) = scanner_module.AttachmentScanner().describe_contents(
        [adk.Content(role="user", parts=[part])]
    )

    assert descriptor.display_name == "deck.pdf"
    assert descriptor.declared_mime == "application/pdf"
    assert descriptor.sniffed_mime == "application/pdf"
    assert descriptor.size_bytes == len(PDF_BYTES)
    assert descriptor.sha256 == PDF_SHA


def test_blocker_one_against_real_adk_types(adk, scanner_module):
    """The regression, on the real classes rather than on the fakes."""

    contents = [
        adk.Content(
            role="user",
            parts=[
                adk.Part(text="summarise this deck"),
                adk.Part(inline_data=adk.Blob(data=PDF_BYTES, mime_type="application/pdf")),
            ],
        ),
        adk.Content(
            role="model",
            parts=[adk.Part(function_call=adk.FunctionCall(name="send_report", args={}))],
        ),
        adk.Content(
            role="user",
            parts=[
                adk.Part(
                    function_response=adk.FunctionResponse(
                        name="send_report", response={"status": "sent"}
                    )
                )
            ],
        ),
    ]

    descriptors = scanner_module.AttachmentScanner().describe_contents(contents)

    assert len(descriptors) == 1
    assert descriptors[0].content_index == 0
    assert descriptors[0].part_index == 1
    assert descriptors[0].sha256 == PDF_SHA


def test_scanner_describes_a_real_file_data_part(adk, scanner_module):
    uri = "https://generativelanguage.googleapis.com/v1beta/files/x?token=SECRET"
    part = adk.Part(file_data=adk.FileData(file_uri=uri, mime_type="application/pdf"))

    (descriptor,) = scanner_module.AttachmentScanner().describe_contents(
        [adk.Content(role="user", parts=[part])]
    )

    assert descriptor.is_file_data is True
    assert descriptor.uri_scheme == "https"
    assert descriptor.uri_host == "generativelanguage.googleapis.com"
    assert "SECRET" not in repr(descriptor.to_dict())


def test_from_bytes_helper_produces_a_describable_part(adk, scanner_module):
    """``Part.from_bytes`` is how an ADK app actually attaches a file."""

    part = adk.Part.from_bytes(data=PDF_BYTES, mime_type="application/pdf")

    (descriptor,) = scanner_module.AttachmentScanner().describe_contents(
        [adk.Content(role="user", parts=[part])]
    )

    assert descriptor.sha256 == PDF_SHA
    assert descriptor.declared_mime == "application/pdf"


# --------------------------------------------------------------------------
# the extractor and the request object, driven by real objects
# --------------------------------------------------------------------------


def test_extract_request_payload_on_a_real_llm_request(adk, extractors):
    llm_request_module = importlib.import_module("google.adk.models")
    llm_request = llm_request_module.LlmRequest(
        contents=[
            adk.Content(
                role="user",
                parts=[
                    adk.Part(text="what does this say?"),
                    adk.Part(
                        inline_data=adk.Blob(
                            data=PDF_BYTES,
                            mime_type="application/pdf",
                            display_name="deck.pdf",
                        )
                    ),
                ],
            )
        ]
    )

    payload = extractors.extract_request_payload(llm_request)

    assert payload.text.startswith("what does this say?\n[agent-control: attachment 1 of 1")
    assert "%PDF" not in payload.text
    assert payload.attachments[0].sha256 == PDF_SHA
    assert payload.context_block()["attachment_summary"]["unextracted_count"] == 1


def test_text_only_real_request_is_unchanged_by_phase_one(adk, extractors):
    llm_request_module = importlib.import_module("google.adk.models")
    llm_request = llm_request_module.LlmRequest(
        contents=[adk.Content(role="user", parts=[adk.Part(text="plain hello")])]
    )

    assert extractors.extract_request_text(llm_request) == "plain hello"
    assert extractors.extract_request_payload(llm_request).text == "plain hello"


def test_callback_context_exposes_get_invocation_context(adk):
    """The artifact-service notice depends on this being public API."""

    callback_context = importlib.import_module("google.adk.agents.callback_context")

    assert hasattr(callback_context.CallbackContext, "get_invocation_context")


def test_llm_agent_has_no_artifact_service_so_a_bind_time_check_cannot_work(adk):
    """Why the notice reads the invocation context rather than the agent.

    In ADK the artifact service is a ``Runner`` constructor argument. ``bind()``
    is handed the root agent, so a bind-time probe finds nothing even when a
    service is configured, and a notice that never fires is worse than none.
    """

    agents = importlib.import_module("google.adk.agents")

    assert "artifact_service" not in set(agents.LlmAgent.model_fields)


# --------------------------------------------------------------------------
# behavioural contracts: the two facts halting rests on
#
# Everything above checks shapes. These check behaviour, because the two
# claims Phase 5 is built on are not attribute checks: that an ``LlmResponse``
# returned from ``before_model_callback`` ends the invocation, and that a dict
# returned from ``before_tool_callback`` prevents the tool body running. The
# plugin tests next door assert those against hand-written fakes, which proves
# only that the fakes behave as their author wrote them.
#
# The model is a stub ``BaseLlm``, so these need no API key, no network and no
# spend - but the agent, the runner, the plugin dispatch and the tool
# invocation are all the real package. The spike measured the same three
# outcomes against a live ``adk api_server``; these reproduce them in-process
# so they can run on every change instead of once.
# --------------------------------------------------------------------------


class _RunOutcome:
    """What one invocation did, as plain data."""

    def __init__(self) -> None:
        self.events: list[object] = []
        self.model_calls = 0
        self.callbacks: list[str] = []
        self.tool_results: list[object] = []
        self.seen_state: dict[str, object] = {}
        self.tool_body_ran = False


async def _run_agent(adk, mode: str, *, seeded_state: dict | None = None) -> _RunOutcome:
    """Drive one real ADK invocation whose plugin behaves like a halt.

    ``mode`` is ``"none"`` (the control run), ``"block_model"`` or
    ``"block_tool"``. The control run exists so a negative result cannot be a
    broken harness: if the tool never runs even when nothing blocks it, the
    absence proves nothing.
    """
    agents = importlib.import_module("google.adk.agents")
    base_llm = importlib.import_module("google.adk.models.base_llm")
    models = importlib.import_module("google.adk.models")
    plugins = importlib.import_module("google.adk.plugins")
    runners = importlib.import_module("google.adk.runners")
    sessions = importlib.import_module("google.adk.sessions")

    outcome = _RunOutcome()

    def send_email(to: str) -> dict:
        outcome.tool_body_ran = True
        return {"status": "sent", "to": to}

    class _StubLlm(base_llm.BaseLlm):
        model: str = "stub-model"

        async def generate_content_async(self, llm_request, stream=False):
            outcome.model_calls += 1
            if outcome.model_calls == 1:
                yield models.LlmResponse(
                    content=adk.Content(
                        role="model",
                        parts=[
                            adk.Part(
                                function_call=adk.FunctionCall(
                                    name="send_email", args={"to": "board@example.com"}
                                )
                            )
                        ],
                    )
                )
            else:
                yield models.LlmResponse(
                    content=adk.Content(role="model", parts=[adk.Part(text="Done.")])
                )

    class _HaltingPlugin(plugins.BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="halt-contract")

        async def before_model_callback(self, *, callback_context, llm_request):
            outcome.callbacks.append("before_model")
            outcome.seen_state["model"] = callback_context.state.get("agent_control")
            if mode == "block_model":
                return models.LlmResponse(
                    content=adk.Content(
                        role="model", parts=[adk.Part(text="BLOCKED_BY_HALT")]
                    )
                )
            return None

        async def after_model_callback(self, *, callback_context, llm_response):
            outcome.callbacks.append("after_model")
            return None

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            outcome.callbacks.append("before_tool")
            outcome.seen_state["tool"] = tool_context.state.get("agent_control")
            if mode == "block_tool":
                return {"status": "blocked", "message": "BLOCKED_BY_HALT_TOOL"}
            return None

        async def after_tool_callback(self, *, tool, tool_args, tool_context, result):
            outcome.callbacks.append("after_tool")
            outcome.tool_results.append(result)
            return None

    agent = agents.LlmAgent(name="contract_agent", model=_StubLlm(), tools=[send_email])
    session_service = sessions.InMemorySessionService()
    runner = runners.Runner(
        app_name="contract_app",
        agent=agent,
        session_service=session_service,
        plugins=[_HaltingPlugin()],
    )
    await session_service.create_session(
        app_name="contract_app",
        user_id="u",
        session_id="s",
        state=seeded_state or {},
    )
    async for event in runner.run_async(
        user_id="u",
        session_id="s",
        new_message=adk.Content(role="user", parts=[adk.Part(text="send it")]),
    ):
        outcome.events.append(event)
    return outcome


@pytest.mark.asyncio
async def test_the_control_run_really_executes_the_tool(adk) -> None:
    """The guard on the two negative results below.

    A test that asserts "the tool did not run" is worthless without one that
    shows the tool runs when nothing stops it.
    """
    outcome = await _run_agent(adk, "none")

    assert outcome.tool_body_ran is True
    assert outcome.tool_results == [{"status": "sent", "to": "board@example.com"}]
    assert outcome.model_calls == 2


@pytest.mark.asyncio
async def test_an_llm_response_from_before_model_ends_the_invocation(adk) -> None:
    """H1, against the real package.

    One event, no second model call, no ``after_model``, no tool. This is why
    a halt at the model boundary costs nothing and why the plugin may return
    the blocked response and stop thinking about that invocation.
    """
    outcome = await _run_agent(adk, "block_model")

    assert len(outcome.events) == 1
    assert outcome.model_calls == 0, "a blocked call is a call nobody paid for"
    assert outcome.callbacks == ["before_model"]
    assert outcome.tool_body_ran is False


@pytest.mark.asyncio
async def test_a_dict_from_before_tool_prevents_the_tool_body_running(adk) -> None:
    """H2, proven by the absence of the side effect rather than by a transcript.

    Two further facts the plugin has to live with, both measured here rather
    than assumed: ``after_tool_callback`` still fires and is handed the
    substitute dict as the tool's result, and the invocation carries on for one
    more model call. The first is why post-tool evaluation has to be skipped
    for a blocked call; the second is why the halt latches per invocation.
    """
    outcome = await _run_agent(adk, "block_tool")

    assert outcome.tool_body_ran is False
    assert outcome.callbacks.count("before_tool") == 1
    assert outcome.tool_results == [
        {"status": "blocked", "message": "BLOCKED_BY_HALT_TOOL"}
    ]
    assert outcome.callbacks.count("before_model") == 2, (
        "the blocked result goes back to the model, so the invocation continues"
    )


@pytest.mark.asyncio
async def test_state_seeded_at_session_creation_reaches_both_callbacks(adk) -> None:
    """A1, the assumption five load-bearing things hang off.

    The session key, the runtime token and the trace id all reach the executor
    this way and no other. If this ever stops holding, nudges and graceful
    halts do not ship in the form they are built in - agent-scoped delivery is
    not a fallback, it is one person's sentence in another person's chat.
    """
    seeded = {"agent_control": {"session_key": "sess-abc", "runtime_token": "tok"}}

    outcome = await _run_agent(adk, "none", seeded_state=seeded)

    assert outcome.seen_state["model"] == seeded["agent_control"]
    assert outcome.seen_state["tool"] == seeded["agent_control"]


def test_tool_context_actions_carries_the_invocation_ending_flag(adk) -> None:
    """A9's field, checked for existence rather than for effect.

    ``skip_summarization`` is best effort in this design - the latch is the
    correctness guarantee - but an attribute that quietly disappeared would
    turn a cost optimization into an ``AttributeError`` on the tool path.
    """
    event_actions = importlib.import_module("google.adk.events.event_actions")

    assert "skip_summarization" in set(event_actions.EventActions.model_fields)
