"""Wire-model coverage for per-agent runtime configuration.

Two fields on one row, and the validation on each of them is load-bearing for a
different reason.

The **body** may not carry either fence delimiter. Those tags are the only thing
telling a model which text is operator configuration and which is
control-authored steering guidance, so a body that can spell one can forge the
other. Both fences or neither, opening tags as well as closing ones.

The **model id** may not contain ``/`` or ``://``. A slash prefix re-selects the
underlying provider and a configured ``api_base`` is ignored for routing, so a
slashed id is a destination selector wearing a name field. This module is the
first of four layers that refuse one; the others are the server's write
boundary, a database check constraint, and the SDK before it constructs a
client.
"""

from __future__ import annotations

import datetime as dt

import pytest
from agent_control_models import (
    BODY_MAX_LENGTH,
    MODEL_ID_MAX_LENGTH,
    AgentConfigVersionDetail,
    AgentConfigVersionSummary,
    AgentModelOption,
    BodyFormat,
    ClearAgentConfigFieldRequest,
    ConfigEventType,
    ConfigOrigin,
    DeliveryState,
    GetAgentConfigResponse,
    ListAgentModelsResponse,
    ModelCostTier,
    ModelProvider,
    ModelSource,
    PromptSource,
    RestoreAgentConfigVersionRequest,
    ScanFinding,
    SetAgentConfigRequest,
    SetAgentConfigResponse,
    SetPromptEnabledRequest,
    contains_fence_delimiter,
    validate_model_id_shape,
    validate_prompt_body,
)
from pydantic import ValidationError

NOW = dt.datetime(2026, 8, 2, 12, 0, tzinfo=dt.UTC)


def _set_request(**overrides: object) -> SetAgentConfigRequest:
    payload: dict[str, object] = {"body": "Be concise.", "expected_version": 0}
    payload.update(overrides)
    return SetAgentConfigRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# The body
# ---------------------------------------------------------------------------


class TestPromptBody:
    def test_a_body_at_the_cap_is_accepted_and_one_character_past_it_is_not(self) -> None:
        assert len(_set_request(body="x" * BODY_MAX_LENGTH).body or "") == BODY_MAX_LENGTH

        with pytest.raises(ValidationError):
            _set_request(body="x" * (BODY_MAX_LENGTH + 1))

    @pytest.mark.parametrize("blank", ["", " ", "\n", "\t  \n ", " "])
    def test_a_blank_body_is_refused_and_the_message_points_at_the_clear_route(
        self, blank: str
    ) -> None:
        """An empty string and "clear this" are different intents.

        Saving an empty prompt must not mean "send an empty system instruction
        to the model", which is never what anybody meant. Where the body is
        whitespace rather than empty the refusal also has to name the route that
        expresses the other intent, or the caller types a space to make the
        error go away and ships a system instruction that is one space.
        """
        with pytest.raises(ValidationError) as excinfo:
            _set_request(body=blank)
        if blank:
            assert "clear" in str(excinfo.value)

    @pytest.mark.parametrize(
        "body",
        [
            "<agent_control_system_prompt>",
            "</agent_control_system_prompt>",
            "<agent_control_guidance>",
            "</agent_control_guidance>",
            "prefix <AGENT_CONTROL_SYSTEM_PROMPT version='9'> suffix",
            "prefix </ AGENT_CONTROL_GUIDANCE > suffix",
            "< agent_control_guidance >",
            "<\t/\tagent_control_system_prompt>",
            "line one\n<agent_control_guidance>\nline three",
        ],
    )
    def test_every_fence_delimiter_is_refused_including_openers_and_case_variants(
        self, body: str
    ) -> None:
        """A body that can spell a fence can forge a provenance boundary.

        Closing tags alone are not enough: without the opening check a nested
        tag makes the field's structure ambiguous, and without the guidance
        check a saved body can put words in Agent Control's mouth that the model
        reads as real control output.
        """
        assert contains_fence_delimiter(body) is True
        with pytest.raises(ValidationError):
            _set_request(body=body)

    @pytest.mark.parametrize(
        "body",
        [
            "Talk about agent control guidance in prose.",
            "Mention <agent_control_something_else> which is not a fence.",
            "A less-than sign < on its own, and the word agent_control_guidance.",
            "<agent_controlled_guidance>",
        ],
    )
    def test_prose_that_merely_mentions_the_words_is_not_a_fence(self, body: str) -> None:
        """The check is on the tag, not on the vocabulary.

        A prompt is prose an operator writes. Refusing every body that contains
        the phrase would make the editor unusable for writing about the product
        the editor is part of.
        """
        assert contains_fence_delimiter(body) is False
        assert validate_prompt_body(body) == body


# ---------------------------------------------------------------------------
# The model id
# ---------------------------------------------------------------------------


class TestModelIdShape:
    @pytest.mark.parametrize(
        "model_id",
        [
            "bedrock/anthropic.claude-v2",
            "openai/gpt-5.5",
            "vertex_ai/gemini-2.5-flash",
            "http://127.0.0.1:10531/v1",
            "https://evil.example.com/v1",
            "/gpt-5.4-mini",
            "gpt-5.4-mini/",
        ],
    )
    def test_a_slashed_or_url_shaped_id_is_refused(self, model_id: str) -> None:
        """A slash re-selects the provider and the configured endpoint is ignored.

        Verified upstream: ``get_llm_provider('bedrock/anthropic.claude-v2',
        api_base='http://127.0.0.1:10531/v1')`` routes to bedrock. So an id like
        this is a per-agent endpoint by another name, in a field the UI labels
        with the word "model".
        """
        with pytest.raises(ValueError):
            validate_model_id_shape(model_id)
        with pytest.raises(ValidationError):
            _set_request(model_id=model_id)

    def test_a_url_is_told_it_is_a_url_rather_than_told_about_slashes(self) -> None:
        """Order of the two messages matters to whoever pasted the URL."""
        with pytest.raises(ValueError, match="'://'"):
            validate_model_id_shape("https://api.openai.com/v1")

    @pytest.mark.parametrize("model_id", [" gpt-5.4-mini", "gpt-5.4-mini ", "\tgpt-5"])
    def test_surrounding_whitespace_is_refused(self, model_id: str) -> None:
        """Two ids differing only in whitespace would be two picker rows."""
        with pytest.raises(ValueError):
            validate_model_id_shape(model_id)

    def test_the_length_bound_matches_the_column(self) -> None:
        assert _set_request(model_id="m" * MODEL_ID_MAX_LENGTH).model_id is not None
        with pytest.raises(ValidationError):
            _set_request(model_id="m" * (MODEL_ID_MAX_LENGTH + 1))

    @pytest.mark.parametrize(
        "model_id", ["gpt-5.4-mini", "gemini-2.5-flash", "gemma-3-1b", "gpt-5.6-sol"]
    )
    def test_ordinary_ids_pass(self, model_id: str) -> None:
        assert validate_model_id_shape(model_id) == model_id


class TestAgentModelOption:
    def test_an_allowlist_entry_round_trips(self) -> None:
        entry = AgentModelOption.model_validate(
            {
                "id": "gpt-5.4-mini",
                "label": "GPT 5.4 mini",
                "provider": "openai_compatible",
                "cost_tier": "economy",
                "recommended": True,
            }
        )
        assert entry.provider == ModelProvider.OPENAI_COMPATIBLE
        assert entry.cost_tier == ModelCostTier.ECONOMY
        assert AgentModelOption.model_validate(entry.model_dump(mode="json")) == entry

    def test_an_allowlist_entry_may_not_carry_a_slashed_id(self) -> None:
        """The picker is the other place a bad id could enter the system."""
        with pytest.raises(ValidationError):
            AgentModelOption.model_validate(
                {
                    "id": "bedrock/anthropic.claude-v2",
                    "label": "Claude",
                    "provider": "openai_compatible",
                    "cost_tier": "premium",
                }
            )

    def test_an_empty_allowlist_is_a_valid_response(self) -> None:
        """Empty by default, so the model half is inert on existing deployments."""
        assert ListAgentModelsResponse().models == []


# ---------------------------------------------------------------------------
# Request shapes
# ---------------------------------------------------------------------------


class TestSetAgentConfigRequest:
    def test_a_model_only_save_does_not_require_a_body(self) -> None:
        """Otherwise a dropdown change round-trips 32000 characters."""
        request = SetAgentConfigRequest.model_validate(
            {"model_id": "gpt-5.4-mini", "expected_version": 3}
        )
        assert request.body is None
        assert request.model_id == "gpt-5.4-mini"

    def test_a_prompt_only_save_does_not_have_to_restate_the_model(self) -> None:
        request = _set_request()
        assert request.model_id is None

    def test_a_request_carrying_neither_field_is_refused(self) -> None:
        """A no-op write that burns a version number is worse than a rejection."""
        with pytest.raises(ValidationError):
            SetAgentConfigRequest.model_validate({"expected_version": 1})

    def test_expected_version_is_required_and_may_not_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            SetAgentConfigRequest.model_validate({"body": "hi"})
        with pytest.raises(ValidationError):
            _set_request(expected_version=-1)

    @pytest.mark.parametrize("origin", ["authored", "copied_from_reported"])
    def test_both_authoring_origins_are_writable(self, origin: str) -> None:
        assert _set_request(origin=origin).origin == origin

    def test_a_caller_may_not_label_an_ordinary_save_as_a_restore(self) -> None:
        """``restored`` is stamped by the restore route, never accepted from a client.

        History is only worth having if the rows mean what they say. A save that
        could call itself a restore would let somebody hide an authored change
        inside what reads as a rollback.
        """
        with pytest.raises(ValidationError):
            _set_request(origin="restored")

    def test_a_note_past_the_cap_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _set_request(note="n" * 501)

    def test_the_request_refuses_fields_it_does_not_understand(self) -> None:
        """No ``base_url``, no ``api_base``, no ``api_key`` - not now, not by typo.

        There is no per-agent endpoint anywhere in this feature, and a request
        model that silently swallowed one would make the absence of the column
        the only thing standing between a writer and an outbound sink.
        """
        for smuggled in ("base_url", "api_base", "endpoint", "api_key"):
            with pytest.raises(ValidationError):
                _set_request(**{smuggled: "http://evil.example.com/v1"})


class TestOtherRequestShapes:
    def test_clear_requires_a_concurrency_token(self) -> None:
        with pytest.raises(ValidationError):
            ClearAgentConfigFieldRequest.model_validate({})
        assert (
            ClearAgentConfigFieldRequest.model_validate({"expected_version": 4}).expected_version
            == 4
        )

    def test_the_enable_toggle_carries_the_flag_and_the_token(self) -> None:
        request = SetPromptEnabledRequest.model_validate(
            {"prompt_enabled": False, "expected_version": 2}
        )
        assert request.prompt_enabled is False
        with pytest.raises(ValidationError):
            SetPromptEnabledRequest.model_validate({"expected_version": 2})

    def test_restore_requires_a_concurrency_token(self) -> None:
        with pytest.raises(ValidationError):
            RestoreAgentConfigVersionRequest.model_validate({})


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class TestResponses:
    def test_an_unmanaged_agent_reads_as_unmanaged_rather_than_as_broken(self) -> None:
        """The defaults are the zero-risk rollout, expressed as a data shape."""
        response = GetAgentConfigResponse(agent_name="marketing-copywriter")
        assert response.body is None
        assert response.prompt_source == PromptSource.NONE
        assert response.model_source == ModelSource.CODE
        assert response.model_provider is None
        assert response.current_version == 0

    def test_a_managed_response_round_trips(self) -> None:
        payload = {
            "agent_name": "marketing-copywriter",
            "body": "Be concise.",
            "body_format": "text",
            "prompt_enabled": True,
            "prompt_source": "managed",
            "model_id": "gpt-5.4-mini",
            "model_provider": "openai_compatible",
            "model_allowed": True,
            "model_cost_tier": "economy",
            "model_source": "managed",
            "delivery_state": "active",
            "etag": "v3-abc123def456",
            "current_version": 3,
            "source_instruction": None,
            "source_reported_at": None,
            "updated_by_hash": "abcd1234",
            "created_at": NOW,
            "updated_at": NOW,
        }
        response = GetAgentConfigResponse.model_validate(payload)
        assert response.delivery_state == DeliveryState.ACTIVE
        assert GetAgentConfigResponse.model_validate(response.model_dump(mode="json")) == response

    def test_a_delisted_model_reads_as_not_allowed_with_no_provider(self) -> None:
        """The stored id survives; only its resolution changes.

        Nulling stored ids when server config changes would mean one mistyped
        env line silently wiped model choices across a namespace with no version
        row recording it.
        """
        response = GetAgentConfigResponse(
            agent_name="a",
            model_id="gpt-9-retired",
            model_allowed=False,
            model_provider=None,
            model_source=ModelSource.CODE,
        )
        assert response.model_id == "gpt-9-retired"
        assert response.model_provider is None

    def test_a_scan_finding_never_carries_the_matched_text(self) -> None:
        """A finding that quoted the secret would copy it into the audit log.

        The version row and every history response would then hold the very
        string the finding exists to warn about.
        """
        finding = ScanFinding(
            scanner="secret_pattern",
            code="openai_api_key",
            message="Looks like an OpenAI-style API key.",
            match_count=2,
        )
        assert "match" not in finding.model_dump()
        assert set(finding.model_dump()) == {
            "scanner",
            "severity",
            "code",
            "message",
            "match_count",
        }

    def test_a_write_response_carries_the_resolved_delivery_view(self) -> None:
        response = SetAgentConfigResponse(
            version_num=8,
            current_version=8,
            etag="v8-0011223344ff",
            prompt_source=PromptSource.MANAGED,
            model_source=ModelSource.CODE,
            delivery_state=DeliveryState.BLOCKED_INSECURE_AUTH,
        )
        assert response.success is True
        assert response.scan_findings == []


class TestVersionRows:
    def test_a_summary_omits_the_body_but_keeps_the_model(self) -> None:
        """Most history rows are about the model, and it is short."""
        summary = AgentConfigVersionSummary(
            version_num=2,
            event_type=ConfigEventType.UPDATED,
            origin=ConfigOrigin.AUTHORED,
            model_id="gpt-5.4-mini",
            has_body=True,
            created_at=NOW,
        )
        assert "body" not in summary.model_dump()
        assert summary.model_id == "gpt-5.4-mini"

    def test_a_detail_adds_the_body_and_its_format(self) -> None:
        detail = AgentConfigVersionDetail(
            version_num=2,
            event_type=ConfigEventType.RESTORED,
            origin=ConfigOrigin.RESTORED,
            body="Be concise.",
            body_format=BodyFormat.TEXT,
            etag="v2-aabbccddeeff",
            created_at=NOW,
        )
        assert detail.body == "Be concise."
        assert detail.body_format == BodyFormat.TEXT

    def test_clearing_is_two_distinct_event_types(self) -> None:
        """One ``cleared`` value on a two-field row cannot say which field went."""
        assert ConfigEventType.PROMPT_CLEARED.value == "prompt_cleared"
        assert ConfigEventType.MODEL_CLEARED.value == "model_cleared"
        assert "cleared" not in {e.value for e in ConfigEventType}

    def test_the_seven_event_types_match_the_database_constraint(self) -> None:
        assert {e.value for e in ConfigEventType} == {
            "created",
            "updated",
            "prompt_cleared",
            "model_cleared",
            "restored",
            "enabled",
            "disabled",
        }

    def test_the_three_origins_match_the_database_constraint(self) -> None:
        assert {o.value for o in ConfigOrigin} == {
            "authored",
            "copied_from_reported",
            "restored",
        }

    def test_body_format_has_exactly_one_member_today(self) -> None:
        """The seam exists so a future format fails loudly on restore."""
        assert [f.value for f in BodyFormat] == ["text"]
