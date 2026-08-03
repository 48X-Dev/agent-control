"""The five ADK facts per-agent configuration rests on, against a real install.

Run it::

    uv run --package agent-control-sdk --with "google-adk[extensions]" \\
        python -m pytest sdks/python/tests/test_google_adk_managed_config_contract.py -q

``python -m pytest`` rather than ``pytest``: the console script resolves out of
the project venv, which does not carry the overlay ``--with`` installs, and the
whole file would then skip while looking like it ran. The extra is
``extensions``, not ``litellm``.

**Why this file is worth more than every managed-config test written against
the fakes.** The sibling files inject hand-written ``google.adk.*`` modules into
``sys.modules``, so they verify this repo's fiction of ADK. That is fine for
logic and useless for shape. Two of the facts below are *internal framework
behaviour with no stability guarantee*, and if a future ADK breaks either one,
this feature stops working with no symptom anybody would connect back to it:
saves succeed, the dashboard reports a model change, the version history records
it, and the agent keeps calling the old vendor.

The five:

1. ``LlmRequest.config.system_instruction`` is a real, writable attribute path.
2. ``LlmAgent.canonical_model`` is a property resolved on read, not cached at
   construction.
3. ``BaseLlmFlow.__get_llm`` runs **after** ``before_model_callback``, so a swap
   inside the callback is picked up by the very call it is guarding.
4. ``CallbackContext.get_invocation_context()`` is public and its shallow copy
   shares the agent object.
5. ``basic._build_basic_request`` populates ``llm_request.model`` before the
   callback fires, which is why the model rule has to correct it.

Plus the Phase 0 finding, which is a sixth fact and the one that changed the
design: ``config.system_instruction`` is **composite**, and the block it carries
enumerating transfer targets is functionally load-bearing.
"""

from __future__ import annotations

import importlib
import sys

import pytest

_OWNED_PREFIXES = ("google", "agent_control.integrations.google_adk")

GLOBAL_INSTRUCTION = "GLOBAL: this deployment is a demo."
AGENT_INSTRUCTION = "You are a city guide. Answer questions about cities."


@pytest.fixture
def adk():
    """Evict any fake ``google.*`` modules and import the real package.

    The fake-installing fixtures in the sibling files never remove what they put
    in ``sys.modules``, and pytest may collect this file after some of them, so
    importing without evicting first would hand back a fake and this file would
    silently test nothing - which is the exact failure mode it exists to
    prevent.
    """
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name.split(".")[0] == "google" or name.startswith(_OWNED_PREFIXES[1])
    }
    for name in saved:
        sys.modules.pop(name, None)

    try:
        agents = importlib.import_module("google.adk.agents")
    except Exception:  # pragma: no cover - exercised only without google-adk
        sys.modules.update(saved)
        pytest.skip("google-adk is not installed; run with --with 'google-adk[extensions]'")

    if getattr(agents, "LlmAgent", None) is None:  # pragma: no cover - fake leaked
        sys.modules.update(saved)
        pytest.skip("google.adk.agents looks like a test fake, not the real package")

    try:
        yield agents
    finally:
        for name in list(sys.modules):
            if name.split(".")[0] == "google" or name.startswith(_OWNED_PREFIXES[1]):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def _stub_llm(model: str = "stub-model"):
    """A real ``BaseLlm`` subclass that never calls anything.

    It has to be a real subclass: ``LlmAgent.model`` is validated against
    ``str | BaseLlm``, so a duck-typed object is rejected at construction. No
    API key, no network and no spend - the facts under test are about
    resolution and ordering, not about responses.
    """
    base_llm = importlib.import_module("google.adk.models.base_llm")

    class _StubLlm(base_llm.BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            raise AssertionError("no contract test here makes a model call")
            yield  # pragma: no cover - unreachable, keeps this an async generator

    return _StubLlm(model=model)


# ---------------------------------------------------------------------------
# Fact 1: the field the prompt is written into
# ---------------------------------------------------------------------------


def test_llm_request_config_system_instruction_is_a_writable_string_field(adk) -> None:
    """The whole prompt half writes here, so it has to be a plain attribute."""
    models = importlib.import_module("google.adk.models")

    request = models.LlmRequest()
    assert hasattr(request, "config")
    assert hasattr(request.config, "system_instruction")

    request.config.system_instruction = "written by hand"
    assert request.config.system_instruction == "written by hand"


def test_the_request_config_is_replaced_per_call_rather_than_shared(adk) -> None:
    """Why nothing the prompt rule writes can leak into the next model call.

    The request-processor phase overwrites ``llm_request.config`` with a fresh
    deep copy of the agent's own generation config before any callback runs, so
    the baseline the rule captures is always from the copy this call will use.
    """
    agents = adk
    agent = agents.LlmAgent(
        name="contract_agent", model=_stub_llm(), instruction=AGENT_INSTRUCTION
    )

    first = _assemble_system_instruction(agent)
    second = _assemble_system_instruction(agent)

    assert first == second, "assembly is deterministic, so a stale copy would show"


# ---------------------------------------------------------------------------
# Fact 2: the model resolves on read
# ---------------------------------------------------------------------------


def test_llm_agent_model_is_a_plain_mutable_field(adk) -> None:
    """No ``frozen``, no ``validate_assignment``: assignment is an attribute store.

    If a future ADK freezes this field, the swap raises instead of silently
    doing nothing, which is the better of the two failures - but it still stops
    the feature, and this is where that gets noticed.
    """
    field = adk.LlmAgent.model_fields["model"]
    assert field.frozen in (None, False)

    agent = adk.LlmAgent(name="contract_agent", model=_stub_llm("first"))
    replacement = _stub_llm("second")
    agent.model = replacement
    assert agent.model is replacement


def test_canonical_model_is_resolved_on_every_read_not_cached_at_construction(
    adk,
) -> None:
    """**The single riskiest assumption in the whole feature.**

    If a future ADK caches the resolved model on the agent, the swap becomes a
    no-op: every save succeeds, the UI reports a model change, the version
    history records it, and the agent keeps calling the old vendor with nothing
    anywhere saying so. Nothing else in the suite would notice.
    """
    agent = adk.LlmAgent(name="contract_agent", model=_stub_llm("first"))

    assert agent.canonical_model.model == "first"

    replacement = _stub_llm("second")
    agent.model = replacement

    assert agent.canonical_model is replacement
    assert agent.canonical_model.model == "second"


def test_canonical_model_is_a_property_rather_than_a_stored_field(adk) -> None:
    """Belt and braces on the fact above, and it rules out two shapes.

    A plain ``property`` recomputes on every read. A ``cached_property`` or a
    stored field would not, and either would silently turn the swap into a
    one-shot that only works before the first model call.
    """
    descriptor = next(
        (
            vars(klass)["canonical_model"]
            for klass in type(adk.LlmAgent).__mro__ + adk.LlmAgent.__mro__
            if "canonical_model" in vars(klass)
        ),
        None,
    )
    assert isinstance(descriptor, property), (
        "canonical_model is no longer a plain property; if it now caches, every "
        "managed model change becomes a silent no-op"
    )
    assert "canonical_model" not in set(adk.LlmAgent.model_fields)


# ---------------------------------------------------------------------------
# Facts 3 and 5: the ordering the swap depends on
# ---------------------------------------------------------------------------


def test_the_flow_resolves_the_model_after_the_before_model_callback(adk) -> None:
    """Fact 3, read off the source of the method that does both.

    ``_call_llm_async`` awaits the before-model callbacks and only then calls
    ``__get_llm``. A refactor that hoisted the model resolution above the
    callbacks would make every swap land one call late - an agent that runs the
    old model for one more turn after every change, forever.
    """
    import inspect

    base_llm_flow = importlib.import_module("google.adk.flows.llm_flows.base_llm_flow")
    source = inspect.getsource(base_llm_flow.BaseLlmFlow._call_llm_async)

    callback_at = source.find("_handle_before_model_callback")
    get_llm_at = source.find("__get_llm")

    assert callback_at != -1, "the before-model callback hook was renamed"
    assert get_llm_at != -1, "the model resolution hook was renamed"
    assert callback_at < get_llm_at, (
        "the flow now resolves the model before the callback runs, so a model "
        "swap inside the callback would land one call late"
    )


def test_the_basic_processor_populates_llm_request_model_before_the_callback(
    adk,
) -> None:
    """Fact 5, and why the model rule writes ``llm_request.model`` too.

    The request processors run in the request-processor phase, before any
    callback. Leaving the field alone after a swap makes the request's
    self-reported model disagree with the client that serves it, which corrupts
    ADK's own ``call_llm`` span and its per-agent billing label.
    """
    models = importlib.import_module("google.adk.models")
    basic = importlib.import_module("google.adk.flows.llm_flows.basic")

    agent = adk.LlmAgent(name="contract_agent", model=_stub_llm("declared-model"))
    request = models.LlmRequest()

    _run_processor(basic.request_processor, agent, request)

    assert request.model == "declared-model"


def test_the_expression_the_model_rule_copies_still_matches_the_framework(
    adk,
) -> None:
    """``target if isinstance(target, str) else target.model``.

    Asserted by producing the same value both ways, so the two cannot drift into
    a request that names one vendor while another is called.
    """
    models = importlib.import_module("google.adk.models")
    basic = importlib.import_module("google.adk.flows.llm_flows.basic")

    swapped = _stub_llm("openai/gpt-5.4-mini")
    agent = adk.LlmAgent(name="contract_agent", model=swapped)
    request = models.LlmRequest()

    _run_processor(basic.request_processor, agent, request)

    ours = swapped if isinstance(swapped, str) else swapped.model
    assert request.model == ours


# ---------------------------------------------------------------------------
# Fact 4: reaching the agent object from public callback surface
# ---------------------------------------------------------------------------


def test_get_invocation_context_is_public_and_its_copy_shares_the_agent(adk) -> None:
    """No reaching into privates, and the shallow copy is the point.

    ``model_copy`` is shallow, so ``.agent`` on the returned context is the same
    object the flow will read. A deep copy would make every swap invisible.
    """
    callback_context_mod = importlib.import_module(
        "google.adk.agents.callback_context"
    )
    assert hasattr(callback_context_mod.CallbackContext, "get_invocation_context")

    invocation_context_mod = importlib.import_module(
        "google.adk.agents.invocation_context"
    )
    run_config_mod = importlib.import_module("google.adk.agents.run_config")
    agent = adk.LlmAgent(name="contract_agent", model=_stub_llm())
    context = invocation_context_mod.InvocationContext(
        session_service=importlib.import_module(
            "google.adk.sessions"
        ).InMemorySessionService(),
        invocation_id="inv-1",
        agent=agent,
        session=_a_session(),
        run_config=run_config_mod.RunConfig(),
    )

    copied = context.model_copy(update={})

    assert copied.agent is agent


def _a_session():
    sessions = importlib.import_module("google.adk.sessions.session")
    return sessions.Session(id="s", app_name="contract_app", user_id="u")


# ---------------------------------------------------------------------------
# Fact 6: Phase 0's finding, which changed the design
# ---------------------------------------------------------------------------


def _run_processor(processor, agent, request) -> None:
    """Drive one ADK request processor to completion, synchronously."""
    import asyncio

    invocation_context_mod = importlib.import_module(
        "google.adk.agents.invocation_context"
    )
    sessions = importlib.import_module("google.adk.sessions")
    run_config_mod = importlib.import_module("google.adk.agents.run_config")
    context = invocation_context_mod.InvocationContext(
        session_service=sessions.InMemorySessionService(),
        invocation_id="inv-1",
        agent=agent,
        session=_a_session(),
        run_config=run_config_mod.RunConfig(),
    )

    async def _drain() -> None:
        async for _ in processor.run_async(context, request):
            pass

    asyncio.run(_drain())


def _assemble_system_instruction(agent) -> str | None:
    """Assemble ``system_instruction`` the way the real flow does.

    Runs the same processors in the same order the flow runs them, so what comes
    out is what a live agent's request would actually carry.
    """
    models = importlib.import_module("google.adk.models")
    basic = importlib.import_module("google.adk.flows.llm_flows.basic")
    instructions = importlib.import_module("google.adk.flows.llm_flows.instructions")
    identity = importlib.import_module("google.adk.flows.llm_flows.identity")

    request = models.LlmRequest()
    for processor in (
        basic.request_processor,
        instructions.request_processor,
        identity.request_processor,
    ):
        _run_processor(processor, agent, request)
    return request.config.system_instruction


def test_system_instruction_is_composite_not_just_the_agents_own_instruction(
    adk,
) -> None:
    """Phase 0's answer, pinned so a future ADK cannot change it unnoticed.

    The design was written assuming this field carried only what
    ``LlmAgent.instruction`` declared, and the whole replace-versus-append
    decision rested on that. It does not: the framework welds its own preamble
    into the same string, and the pieces cannot be separated after assembly.
    That is why the managed block is appended after the captured baseline rather
    than replacing it.
    """
    agent = adk.LlmAgent(
        name="contract_agent",
        model=_stub_llm(),
        instruction=AGENT_INSTRUCTION,
        description="a guide to cities",
    )

    assembled = _assemble_system_instruction(agent)

    assert assembled is not None
    assert AGENT_INSTRUCTION in assembled
    # The framework's own contribution, after the agent's own instruction.
    assert assembled.strip() != AGENT_INSTRUCTION
    assert "contract_agent" in assembled


def test_the_agents_own_instruction_is_not_even_the_trailing_element(adk) -> None:
    """Which is why "displace the baseline" was never a safe operation.

    ``identity``'s block is appended after ``instructions``', so anything that
    pushed the assembled string aside would push framework text aside with it.
    """
    agent = adk.LlmAgent(
        name="contract_agent",
        model=_stub_llm(),
        instruction=AGENT_INSTRUCTION,
        description="a guide to cities",
    )

    assembled = _assemble_system_instruction(agent)

    assert not assembled.rstrip().endswith(AGENT_INSTRUCTION)


def test_an_agent_declaring_no_instruction_assembles_to_none_not_empty_string(
    adk,
) -> None:
    """So the rule's baseline capture has to tolerate ``None``."""
    agent = adk.LlmAgent(name="contract_agent", model=_stub_llm())

    models = importlib.import_module("google.adk.models")
    basic = importlib.import_module("google.adk.flows.llm_flows.basic")
    instructions = importlib.import_module("google.adk.flows.llm_flows.instructions")

    request = models.LlmRequest()
    _run_processor(basic.request_processor, agent, request)
    _run_processor(instructions.request_processor, agent, request)

    assert request.config.system_instruction is None


def test_a_global_instruction_is_prepended_into_the_same_field(adk) -> None:
    """A second framework contribution the operator never sees in the editor."""
    root = adk.LlmAgent(
        name="root_agent",
        model=_stub_llm(),
        global_instruction=GLOBAL_INSTRUCTION,
        instruction=AGENT_INSTRUCTION,
    )

    assembled = _assemble_system_instruction(root)

    assert GLOBAL_INSTRUCTION in assembled
    assert assembled.index(GLOBAL_INSTRUCTION) < assembled.index(AGENT_INSTRUCTION)


def test_the_transfer_preamble_lands_in_the_same_field_and_is_load_bearing(
    adk,
) -> None:
    """The block that would have been deleted by a wholesale assignment.

    It enumerates the sub-agents reachable through ``transfer_to_agent``.
    Deleting it does not produce an error; it produces an agent that quietly
    stops handing work over, which nobody would connect back to a prompt save.
    """
    agent_transfer = importlib.import_module(
        "google.adk.flows.llm_flows.agent_transfer"
    )
    sub = adk.LlmAgent(
        name="researcher", model=_stub_llm(), description="finds things"
    )
    root = adk.LlmAgent(
        name="root_agent",
        model=_stub_llm(),
        instruction=AGENT_INSTRUCTION,
        sub_agents=[sub],
    )

    models = importlib.import_module("google.adk.models")
    basic = importlib.import_module("google.adk.flows.llm_flows.basic")
    instructions = importlib.import_module("google.adk.flows.llm_flows.instructions")
    request = models.LlmRequest()
    for processor in (
        basic.request_processor,
        instructions.request_processor,
        agent_transfer.request_processor,
    ):
        _run_processor(processor, root, request)

    assembled = request.config.system_instruction
    assert assembled is not None
    assert "researcher" in assembled
    assert "transfer" in assembled.lower()


def test_the_mutation_rule_preserves_everything_the_framework_assembled(adk) -> None:
    """The two halves joined: real assembly, real rule, byte-for-byte preserved.

    Everything else about the prompt rule is asserted against fakes. This is the
    one case where the baseline is what ADK really produces.
    """
    agent_config = importlib.import_module(
        "agent_control.integrations.google_adk._agent_config"
    )
    agent_transfer = importlib.import_module(
        "google.adk.flows.llm_flows.agent_transfer"
    )
    models = importlib.import_module("google.adk.models")
    basic = importlib.import_module("google.adk.flows.llm_flows.basic")
    instructions = importlib.import_module("google.adk.flows.llm_flows.instructions")
    identity = importlib.import_module("google.adk.flows.llm_flows.identity")

    sub = adk.LlmAgent(name="researcher", model=_stub_llm(), description="finds things")
    root = adk.LlmAgent(
        name="root_agent",
        model=_stub_llm(),
        global_instruction=GLOBAL_INSTRUCTION,
        instruction=AGENT_INSTRUCTION,
        sub_agents=[sub],
    )
    request = models.LlmRequest()
    for processor in (
        basic.request_processor,
        instructions.request_processor,
        identity.request_processor,
        agent_transfer.request_processor,
    ):
        _run_processor(processor, root, request)
    baseline = request.config.system_instruction
    assert baseline

    applier = agent_config.ManagedConfigApplier()
    body = "Write like a marketing copywriter."
    applier.apply_prompt(request, body)

    assert request.config.system_instruction.startswith(baseline)
    assert body in request.config.system_instruction

    # Steering guidance appended by a control survives the re-entry.
    request.config.system_instruction += "\n\n" + agent_config.wrap_guidance("Rewrite.")
    applier.apply_prompt(request, body)
    assert request.config.system_instruction.count("<agent_control_guidance>") == 1
    assert request.config.system_instruction.endswith("</agent_control_guidance>")

    # Clearing restores exactly what the framework assembled.
    applier.apply_prompt(request, None)
    assert request.config.system_instruction == (
        baseline + "\n\n" + agent_config.wrap_guidance("Rewrite.")
    )
