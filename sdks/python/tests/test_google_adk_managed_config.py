"""Applying a server-managed prompt and model to a live ADK request.

Three writers share ``before_model_callback``: the managed prompt, control
steering guidance, and the model swap. Two of them write the same field of the
same object, and the third has to agree with the client that actually serves the
call. Nothing structural keeps them in order, so these tests are the ordering.

**The one that matters most is guidance survival.** ``_inject_steering_guidance``
appends control-authored text to ``config.system_instruction`` and then returns
``None``, which makes ADK re-issue the request - so the callback re-enters
against the same ``LlmRequest`` with the guidance already in the field. A rule
that assigned that field wholesale would delete a control's steering text on the
retry pass, silently, with no exception and no log line. The control would
appear to have fired and would have had no effect.

**Phase 0 changed one thing and it is asserted rather than described.**
``config.system_instruction`` is composite: it carries the framework's assembled
preamble, including the transfer block that enumerates the sub-agents reachable
through ``transfer_to_agent``. So the managed block is appended after the
captured baseline instead of replacing it. Replacing would have broken
multi-agent routing.

The rules under test live in ``_agent_config.py``, which imports nothing from
ADK at module scope - the ``Gemini`` and ``LiteLlm`` imports are lazy. So the
prompt and model rules are driven directly here, against small stand-ins for the
two objects ADK hands the callback, with no ``sys.modules`` surgery.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import pytest

from agent_control.integrations.google_adk._agent_config import (
    ManagedConfigApplier,
    resolve_model_base_url,
    wrap_guidance,
    wrap_managed_prompt,
)

#: What the framework assembles before any callback runs: a global instruction,
#: the agent's own, the identity block, and the transfer preamble. The last one
#: is functionally load-bearing - delete it and ``transfer_to_agent`` stops
#: working - which is why the baseline is preserved rather than replaced.
BASELINE = (
    "GLOBAL INSTRUCTION\n\n"
    "You are a city guide. Answer questions about cities.\n\n"
    'You are an agent. Your internal name is "marketing_copywriter".\n\n'
    "You have a list of other agents to transfer to:\n"
    "Agent name: researcher\nAgent description: finds things"
)

MANAGED_BODY = "Write like a marketing copywriter. Never use emoji."
GUIDANCE = "The previous request tripped a control. Rewrite it safely."


class FakeConfig:
    def __init__(self, system_instruction: Any = None) -> None:
        self.system_instruction = system_instruction


class FakeLlmRequest:
    """Stands in for the object ADK hands the callback.

    ``model`` is populated by the framework's request-processor phase from the
    agent's model *before* the callback runs, which is why the model rule has to
    correct it.
    """

    def __init__(self, system_instruction: Any = None, model: str = "old-model") -> None:
        self.config = FakeConfig(system_instruction)
        self.model = model


class FakeAgent:
    """Stands in for an ``LlmAgent``. ``model`` is a plain mutable field."""

    def __init__(self, model: Any = "code-declared-model") -> None:
        self.model = model


class FakeConstructedModel:
    """What a provider branch would have built, with the ``model`` attribute read."""

    def __init__(self, model: str) -> None:
        self.model = model


@pytest.fixture()
def applier() -> ManagedConfigApplier:
    return ManagedConfigApplier()


def _inject_guidance(request: FakeLlmRequest, guidance: str = GUIDANCE) -> None:
    """What ``_inject_steering_guidance`` does, in the same shape."""
    current = request.config.system_instruction
    fenced = wrap_guidance(guidance)
    request.config.system_instruction = (
        f"{current}\n\n{fenced}" if current else fenced
    )


# ---------------------------------------------------------------------------
# The prompt rule
# ---------------------------------------------------------------------------


class TestTheManagedPrompt:
    def test_a_saved_prompt_reaches_the_request_fenced_and_after_the_baseline(
        self, applier: ManagedConfigApplier
    ) -> None:
        """The whole feature, in one assertion pair.

        The operator's text arrives in the field the model reads, inside a fence
        that tells the model what it is, and the framework's own preamble is
        still in front of it byte for byte.
        """
        request = FakeLlmRequest(BASELINE)

        applier.apply_prompt(request, MANAGED_BODY)

        instruction = request.config.system_instruction
        assert instruction.startswith(BASELINE)
        assert MANAGED_BODY in instruction
        assert instruction.endswith(f"</{'agent_control_system_prompt'}>")

    def test_the_framework_preamble_survives_including_the_transfer_block(
        self, applier: ManagedConfigApplier
    ) -> None:
        """Phase 0's finding, asserted rather than trusted.

        Wholesale replacement would have deleted the block enumerating transfer
        targets and broken multi-agent routing, with the only symptom being an
        agent that quietly stopped handing work over.
        """
        request = FakeLlmRequest(BASELINE)

        applier.apply_prompt(request, MANAGED_BODY)

        assert "You have a list of other agents to transfer to:" in (
            request.config.system_instruction
        )
        assert "Agent name: researcher" in request.config.system_instruction

    def test_the_block_states_its_own_precedence(
        self, applier: ManagedConfigApplier
    ) -> None:
        """The only lever left once the code's instruction stays in the field.

        With both texts present and the operator's second, the wrapper has to
        say which one wins - otherwise the dashboard shows one prompt and the
        model follows another.
        """
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)

        block = wrap_managed_prompt(MANAGED_BODY)
        assert block in request.config.system_instruction
        assert "follow this block" in block.lower() or "precedence" in block.lower()

    def test_an_unmanaged_agent_leaves_the_field_byte_identical(
        self, applier: ManagedConfigApplier
    ) -> None:
        """Nothing changes until somebody deliberately saves something."""
        request = FakeLlmRequest(BASELINE)

        applier.apply_prompt(request, None)

        assert request.config.system_instruction == BASELINE

    def test_clearing_a_prompt_restores_the_code_declared_instruction(
        self, applier: ManagedConfigApplier
    ) -> None:
        """The reversal that makes the first save safe to make.

        No restart, no redeploy: the next model call sees the baseline again,
        exactly as it was before anything was applied.
        """
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)
        assert MANAGED_BODY in request.config.system_instruction

        applier.apply_prompt(request, None)

        assert request.config.system_instruction == BASELINE

    def test_re_entry_with_no_change_is_byte_identical(
        self, applier: ManagedConfigApplier
    ) -> None:
        """Idempotent, so a retry cannot stack the block."""
        request = FakeLlmRequest(BASELINE)

        applier.apply_prompt(request, MANAGED_BODY)
        first = request.config.system_instruction
        for _ in range(4):
            applier.apply_prompt(request, MANAGED_BODY)

        assert request.config.system_instruction == first
        assert request.config.system_instruction.count("<agent_control_system_prompt>") == 1

    def test_editing_the_body_between_entries_replaces_rather_than_appends(
        self, applier: ManagedConfigApplier
    ) -> None:
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, "First body.")

        applier.apply_prompt(request, "Second body.")

        instruction = request.config.system_instruction
        assert "Second body." in instruction
        assert "First body." not in instruction
        assert instruction.count("<agent_control_system_prompt>") == 1

    def test_a_request_with_no_instruction_at_all_gets_only_the_block(
        self, applier: ManagedConfigApplier
    ) -> None:
        """An agent that declares nothing assembles to ``None``, not to ``""``."""
        request = FakeLlmRequest(None)

        applier.apply_prompt(request, MANAGED_BODY)

        assert request.config.system_instruction == wrap_managed_prompt(MANAGED_BODY)

    def test_wrapping_can_be_switched_off_without_breaking_the_rule(self) -> None:
        """The rule never depends on parsing delimiters back out of the string."""
        applier = ManagedConfigApplier(wrap_managed_prompt_block=False)
        request = FakeLlmRequest(BASELINE)

        applier.apply_prompt(request, MANAGED_BODY)
        assert request.config.system_instruction == f"{BASELINE}\n\n{MANAGED_BODY}"

        applier.apply_prompt(request, None)
        assert request.config.system_instruction == BASELINE

    def test_a_foreign_mutation_is_left_alone(
        self, applier: ManagedConfigApplier, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Failing to apply is recoverable; corrupting a field we do not model is not."""
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)

        request.config.system_instruction = "something else entirely"
        with caplog.at_level(logging.DEBUG):
            applier.apply_prompt(request, "A different body.")

        assert request.config.system_instruction == "something else entirely"

    def test_a_non_string_instruction_is_left_alone(
        self, applier: ManagedConfigApplier
    ) -> None:
        """ADK permits ``Content``/``Part`` forms here; this rule reasons about text."""
        sentinel = object()
        request = FakeLlmRequest(sentinel)

        applier.apply_prompt(request, MANAGED_BODY)

        assert request.config.system_instruction is sentinel

    def test_two_requests_keep_separate_baselines(
        self, applier: ManagedConfigApplier
    ) -> None:
        """State is per request object, so one call cannot leak into the next."""
        first = FakeLlmRequest("BASELINE ONE")
        second = FakeLlmRequest("BASELINE TWO")

        applier.apply_prompt(first, MANAGED_BODY)
        applier.apply_prompt(second, MANAGED_BODY)
        applier.apply_prompt(first, None)
        applier.apply_prompt(second, None)

        assert first.config.system_instruction == "BASELINE ONE"
        assert second.config.system_instruction == "BASELINE TWO"


# ---------------------------------------------------------------------------
# Guidance survival - the case the mutation rule exists for
# ---------------------------------------------------------------------------


class TestGuidanceSurvival:
    def test_steering_guidance_survives_a_managed_prompt_on_the_retry_pass(
        self, applier: ManagedConfigApplier
    ) -> None:
        """A control that steers must still be steering after the re-entry.

        This is the exact sequence: apply the prompt, a control steers, the
        guidance is appended, ADK re-issues the request, and the callback runs
        again against the same object. If the guidance is gone at the end, the
        control fired and had no effect, and nothing anywhere says so.
        """
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)

        _inject_guidance(request)
        applier.apply_prompt(request, MANAGED_BODY)

        instruction = request.config.system_instruction
        assert instruction.count(f"<{'agent_control_guidance'}>") == 1
        assert GUIDANCE in instruction

    def test_the_guidance_fence_stays_the_trailing_element(
        self, applier: ManagedConfigApplier
    ) -> None:
        """Closest to the model, and a managed prompt can never displace it."""
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)
        _inject_guidance(request)

        applier.apply_prompt(request, MANAGED_BODY)

        assert request.config.system_instruction.endswith(wrap_guidance(GUIDANCE))

    def test_the_managed_block_precedes_the_guidance(
        self, applier: ManagedConfigApplier
    ) -> None:
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)
        _inject_guidance(request)
        applier.apply_prompt(request, MANAGED_BODY)

        instruction = request.config.system_instruction
        assert instruction.index("<agent_control_system_prompt>") < instruction.index(
            "<agent_control_guidance>"
        )

    def test_guidance_survives_the_prompt_being_cleared_mid_request(
        self, applier: ManagedConfigApplier
    ) -> None:
        """``HEAD`` flips; ``TAIL`` is preserved either way."""
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)
        _inject_guidance(request)

        applier.apply_prompt(request, None)

        assert request.config.system_instruction == (
            f"{BASELINE}\n\n{wrap_guidance(GUIDANCE)}"
        )

    def test_guidance_survives_the_prompt_being_enabled_mid_request(
        self, applier: ManagedConfigApplier
    ) -> None:
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, None)
        _inject_guidance(request)

        applier.apply_prompt(request, MANAGED_BODY)

        instruction = request.config.system_instruction
        assert instruction.endswith(wrap_guidance(GUIDANCE))
        assert MANAGED_BODY in instruction

    def test_two_rounds_of_steering_each_survive(
        self, applier: ManagedConfigApplier
    ) -> None:
        """A control may steer more than once against one request."""
        request = FakeLlmRequest(BASELINE)
        applier.apply_prompt(request, MANAGED_BODY)
        _inject_guidance(request, "First steer.")
        applier.apply_prompt(request, MANAGED_BODY)
        _inject_guidance(request, "Second steer.")

        applier.apply_prompt(request, MANAGED_BODY)

        instruction = request.config.system_instruction
        assert "First steer." in instruction
        assert instruction.endswith(wrap_guidance("Second steer."))

    def test_both_fences_exist_so_neither_side_can_forge_the_other(self) -> None:
        """A one-sided fence is not a provenance boundary.

        With guidance unfenced, a saved body containing "Agent Control guidance:
        ..." would be indistinguishable to the model from real control output,
        which defeats the purpose of fencing the other one.
        """
        assert wrap_guidance("x").startswith("<agent_control_guidance>")
        assert wrap_guidance("x").endswith("</agent_control_guidance>")
        assert wrap_managed_prompt("x").startswith("<agent_control_system_prompt>")
        assert wrap_managed_prompt("x").endswith("</agent_control_system_prompt>")


# ---------------------------------------------------------------------------
# The model rule
# ---------------------------------------------------------------------------


@pytest.fixture()
def openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CONTROL_MODEL_BASE_URL", "http://127.0.0.1:10531/v1")


class TestTheManagedModel:
    @staticmethod
    def _apply(
        applier: ManagedConfigApplier,
        request: FakeLlmRequest,
        agent: FakeAgent,
        managed: tuple[str, str] | None,
        *,
        fetched_at: dt.datetime | None = None,
        max_staleness_seconds: float | None = None,
    ) -> None:
        applier.apply_model(
            request,
            agent,
            managed,
            fetched_at=fetched_at or dt.datetime.now(dt.UTC),
            max_staleness_seconds=max_staleness_seconds,
        )

    def test_a_managed_model_is_assigned_and_the_request_is_corrected_to_match(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        """The request's self-reported model must never name a different vendor.

        The framework populated ``llm_request.model`` from the old value before
        this callback ran. Leaving it stale corrupts ADK's own span and its
        per-agent billing label, so the two would disagree about which vendor
        served the call.
        """
        applier._model_cache[
            ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
        ] = FakeConstructedModel("openai/gpt-5.4-mini")
        request, agent = FakeLlmRequest(), FakeAgent()

        self._apply(applier, request, agent, ("gpt-5.4-mini", "openai_compatible"))

        assert isinstance(agent.model, FakeConstructedModel)
        assert request.model == agent.model.model == "openai/gpt-5.4-mini"

    def test_clearing_restores_the_agents_own_model_by_identity(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        """Not an equal object - the same object the code declared."""
        baseline = FakeConstructedModel("code-declared")
        agent = FakeAgent(baseline)
        applier._model_cache[
            ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
        ] = FakeConstructedModel("openai/gpt-5.4-mini")

        self._apply(applier, FakeLlmRequest(), agent, ("gpt-5.4-mini", "openai_compatible"))
        assert agent.model is not baseline

        self._apply(applier, FakeLlmRequest(), agent, None)
        assert agent.model is baseline

    def test_an_unmanaged_model_is_never_assigned(
        self, applier: ManagedConfigApplier
    ) -> None:
        agent = FakeAgent("code-declared-model")
        request = FakeLlmRequest(model="code-declared-model")

        self._apply(applier, request, agent, None)

        assert agent.model == "code-declared-model"
        assert request.model == "code-declared-model"

    def test_re_application_with_unchanged_state_assigns_nothing(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        """Assigning the same object each call keeps ADK's stateless assumption."""
        constructed = FakeConstructedModel("openai/gpt-5.4-mini")
        applier._model_cache[
            ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
        ] = constructed
        agent = FakeAgent()

        for _ in range(5):
            self._apply(
                applier, FakeLlmRequest(), agent, ("gpt-5.4-mini", "openai_compatible")
            )

        assert agent.model is constructed

    def test_two_agent_objects_each_get_their_own_baseline_back(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        """Sub-agents are covered per object, not skipped.

        A sub-agent with a deliberate model of its own is replaced while a
        managed model is in effect and gets *its* choice back when the field is
        cleared. Skipping sub-agents that declare a model would mean the
        dashboard shows one model while half the calls use another.
        """
        applier._model_cache[
            ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
        ] = FakeConstructedModel("openai/gpt-5.4-mini")
        root, sub = FakeAgent("root-model"), FakeAgent("sub-model")

        for agent in (root, sub):
            self._apply(
                applier, FakeLlmRequest(), agent, ("gpt-5.4-mini", "openai_compatible")
            )
        for agent in (root, sub):
            self._apply(applier, FakeLlmRequest(), agent, None)

        assert root.model == "root-model"
        assert sub.model == "sub-model"

    def test_an_object_with_no_model_attribute_is_left_alone(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        request = FakeLlmRequest(model="untouched")

        self._apply(
            applier, request, object(), ("gpt-5.4-mini", "openai_compatible")
        )

        assert request.model == "untouched"


class TestTheFourRefusals:
    """Each keeps the code-declared model and logs once. None of them guesses."""

    @staticmethod
    def _apply(
        applier: ManagedConfigApplier,
        agent: FakeAgent,
        managed: tuple[str, str] | None,
        **kwargs: Any,
    ) -> FakeLlmRequest:
        request = FakeLlmRequest(model="code-declared-model")
        kwargs.setdefault("fetched_at", dt.datetime.now(dt.UTC))
        kwargs.setdefault("max_staleness_seconds", None)
        applier.apply_model(request, agent, managed, **kwargs)
        return request

    def test_an_unrecognised_provider_is_refused_without_inferring_one(
        self, applier: ManagedConfigApplier, openai_env: None,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """The forward-compatibility path, and the exfiltration path it closes.

        Inferring the provider from the id string is exactly how a name in a
        dropdown becomes a destination nobody chose: the framework's registry
        resolves a bare ``gpt-*`` to a client whose factory takes no base URL and
        falls back to the vendor's own endpoint. An older SDK meeting a newer
        server does nothing rather than guessing.
        """
        agent = FakeAgent("code-declared-model")

        with caplog.at_level(logging.WARNING):
            self._apply(applier, agent, ("gpt-5.4-mini", "some_future_provider"))

        assert agent.model == "code-declared-model"
        assert "never inferred" in caplog.text

    def test_a_missing_provider_is_refused(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        agent = FakeAgent("code-declared-model")
        self._apply(applier, agent, ("gpt-5.4-mini", ""))
        assert agent.model == "code-declared-model"

    def test_a_slashed_id_is_refused_even_this_far_down(
        self, applier: ManagedConfigApplier, openai_env: None,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """Three upstream layers should make this unreachable. It costs one line.

        A slash re-selects the provider and the configured endpoint is ignored
        for routing, so this is the last place to catch a bypass of the other
        three.
        """
        agent = FakeAgent("code-declared-model")

        with caplog.at_level(logging.WARNING):
            self._apply(
                applier, agent, ("bedrock/anthropic.claude-v2", "openai_compatible")
            )

        assert agent.model == "code-declared-model"
        assert "re-selects the provider" in caplog.text

    def test_an_openai_compatible_model_with_no_base_url_is_refused(
        self, applier: ManagedConfigApplier, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """Applying without a base URL is how traffic reaches a vendor nobody chose."""
        monkeypatch.delenv("AGENT_CONTROL_MODEL_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        agent = FakeAgent("code-declared-model")

        with caplog.at_level(logging.WARNING):
            self._apply(applier, agent, ("gpt-5.4-mini", "openai_compatible"))

        assert agent.model == "code-declared-model"
        assert "AGENT_CONTROL_MODEL_BASE_URL" in caplog.text
        assert "OPENAI_BASE_URL" in caplog.text

    def test_a_stale_configuration_drops_the_managed_model_and_restores_the_baseline(
        self, applier: ManagedConfigApplier, openai_env: None,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """The ceiling the prompt deliberately does not get.

        An indefinitely retained managed model is unbounded spend on the
        operator's quota that the control plane cannot revoke: the process that
        would pick up a clear is the one that cannot reach the server.
        """
        applier._model_cache[
            ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
        ] = FakeConstructedModel("openai/gpt-5.4-mini")
        agent = FakeAgent("code-declared-model")
        fresh = dt.datetime.now(dt.UTC)

        applier.apply_model(
            FakeLlmRequest(), agent, ("gpt-5.4-mini", "openai_compatible"),
            fetched_at=fresh, max_staleness_seconds=300,
        )
        assert isinstance(agent.model, FakeConstructedModel)

        stale = fresh - dt.timedelta(seconds=600)
        with caplog.at_level(logging.WARNING):
            applier.apply_model(
                FakeLlmRequest(), agent, ("gpt-5.4-mini", "openai_compatible"),
                fetched_at=stale, max_staleness_seconds=300,
            )

        assert agent.model == "code-declared-model"
        assert "system prompt is not affected" in caplog.text.lower()

    def test_a_stale_configuration_does_not_drop_the_managed_prompt(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        """Stale text is a behaviour issue whose fallback is a working agent.

        Applying the same ceiling to the prompt would take an agent's authored
        instruction away during a control-plane outage, for no safety gain.
        """
        request = FakeLlmRequest(BASELINE)
        agent = FakeAgent("code-declared-model")
        stale = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=600)

        applier.apply_prompt(request, MANAGED_BODY)
        applier.apply_model(
            request, agent, ("gpt-5.4-mini", "openai_compatible"),
            fetched_at=stale, max_staleness_seconds=300,
        )

        assert MANAGED_BODY in request.config.system_instruction
        assert agent.model == "code-declared-model"

    def test_a_never_fetched_configuration_applies_no_model(
        self, applier: ManagedConfigApplier, openai_env: None
    ) -> None:
        agent = FakeAgent("code-declared-model")
        self._apply(
            applier,
            agent,
            ("gpt-5.4-mini", "openai_compatible"),
            fetched_at=None,
            max_staleness_seconds=300,
        )
        assert agent.model == "code-declared-model"

    def test_a_refusal_logs_once_rather_than_once_per_model_call(
        self, applier: ManagedConfigApplier, openai_env: None,
        caplog: pytest.LogCaptureFixture
    ) -> None:
        """These fire on every call; unthrottled they would drown the one line."""
        agent = FakeAgent("code-declared-model")

        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                self._apply(applier, agent, ("gpt-5.4-mini", "unknown_provider"))

        assert caplog.text.count("never inferred") == 1


class TestTheBaseUrl:
    def test_either_environment_name_satisfies_the_requirement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Co-equal, not primary and fallback.

        Demoting ``OPENAI_BASE_URL`` would create deployments that set only the
        new name, leaving it unset - which is precisely the state in which a
        stray client reaches the vendor's own endpoint.
        """
        monkeypatch.delenv("AGENT_CONTROL_MODEL_BASE_URL", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "http://from-openai-name/v1")
        assert resolve_model_base_url() == "http://from-openai-name/v1"

        monkeypatch.setenv("AGENT_CONTROL_MODEL_BASE_URL", "http://from-ac-name/v1")
        assert resolve_model_base_url() == "http://from-ac-name/v1"

    def test_neither_set_resolves_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AGENT_CONTROL_MODEL_BASE_URL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert resolve_model_base_url() is None

    def test_a_whitespace_only_value_counts_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_CONTROL_MODEL_BASE_URL", "   ")
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert resolve_model_base_url() is None


# ---------------------------------------------------------------------------
# Both rules in one callback entry
# ---------------------------------------------------------------------------


def test_both_fields_settle_together_and_agree_with_each_other(
    applier: ManagedConfigApplier, openai_env: None
) -> None:
    """One entry, both rules, and the pair asserted together on purpose.

    A reordering refactor that moved the model rule before the prompt rule, or
    that stopped correcting ``llm_request.model``, would otherwise produce a
    request whose self-reported model names one vendor while the client serves
    another - and every single-rule test would still pass.
    """
    constructed = FakeConstructedModel("openai/gpt-5.4-mini")
    applier._model_cache[
        ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
    ] = constructed
    request, agent = FakeLlmRequest(BASELINE), FakeAgent()

    applier.apply_prompt(request, MANAGED_BODY)
    applier.apply_model(
        request,
        agent,
        ("gpt-5.4-mini", "openai_compatible"),
        fetched_at=dt.datetime.now(dt.UTC),
        max_staleness_seconds=None,
    )

    assert request.config.system_instruction.startswith(BASELINE)
    assert MANAGED_BODY in request.config.system_instruction
    assert agent.model is constructed
    assert request.model == (
        constructed if isinstance(constructed, str) else constructed.model
    )


def test_guidance_survives_with_a_managed_model_in_effect(
    applier: ManagedConfigApplier, openai_env: None
) -> None:
    """The steer path and the model swap must not interfere with each other."""
    applier._model_cache[
        ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
    ] = FakeConstructedModel("openai/gpt-5.4-mini")
    request, agent = FakeLlmRequest(BASELINE), FakeAgent()
    managed_model = ("gpt-5.4-mini", "openai_compatible")

    applier.apply_prompt(request, MANAGED_BODY)
    applier.apply_model(
        request, agent, managed_model,
        fetched_at=dt.datetime.now(dt.UTC), max_staleness_seconds=None,
    )
    _inject_guidance(request)
    applier.apply_prompt(request, MANAGED_BODY)
    applier.apply_model(
        request, agent, managed_model,
        fetched_at=dt.datetime.now(dt.UTC), max_staleness_seconds=None,
    )

    instruction = request.config.system_instruction
    assert instruction.count("<agent_control_guidance>") == 1
    assert instruction.endswith(wrap_guidance(GUIDANCE))
    assert isinstance(agent.model, FakeConstructedModel)


def test_closing_the_plugin_forgets_every_baseline(
    applier: ManagedConfigApplier, openai_env: None
) -> None:
    """Per-request and per-agent state is bounded by the plugin's lifetime."""
    applier._model_cache[
        ("openai_compatible", "gpt-5.4-mini", "http://127.0.0.1:10531/v1")
    ] = FakeConstructedModel("openai/gpt-5.4-mini")
    request, agent = FakeLlmRequest(BASELINE), FakeAgent()
    applier.apply_prompt(request, MANAGED_BODY)

    applier.clear()

    assert applier._prompt_state == {}
    assert applier._baseline_models == {}
    assert applier._model_cache == {}


def test_forgetting_one_request_does_not_forget_the_others(
    applier: ManagedConfigApplier
) -> None:
    first, second = FakeLlmRequest("ONE"), FakeLlmRequest("TWO")
    applier.apply_prompt(first, MANAGED_BODY)
    applier.apply_prompt(second, MANAGED_BODY)

    applier.forget_request(first)

    applier.apply_prompt(second, None)
    assert second.config.system_instruction == "TWO"
