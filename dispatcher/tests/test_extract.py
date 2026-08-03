"""Section 9.3, including the two things slice 1 observed rather than assumed."""

from __future__ import annotations

import datetime as dt
from typing import Any

from agent_control_dispatcher.extract import (
    StepOutputCode,
    extract_step_output,
    join_agent_text,
)
from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.sessions import SessionMessage, TurnResponse
from conftest import BLOCKED_TURN_TEXT, blocked_turn_payload, deny_event_payload


def _message(role: str, *, author: str | None = None, text: str | None = "hi") -> SessionMessage:
    parts = [{"kind": "text", "text": text}] if text is not None else []
    return SessionMessage.model_validate(
        {"index": 0, "role": role, "author": author, "parts": parts}
    )


def _deny(**overrides: Any) -> ControlExecutionEvent:
    payload: dict[str, Any] = {
        "control_execution_id": "ce-1",
        "trace_id": "4a6a4583",
        "span_id": "s1",
        "timestamp": dt.datetime.now(dt.UTC),
        "agent_name": "marketing_researcher",
        "control_id": 1,
        "control_name": "block-ssn",
        "check_stage": "post",
        "applies_to": "llm_call",
        "action": "deny",
        "matched": True,
        "confidence": 1.0,
    }
    payload.update(overrides)
    return ControlExecutionEvent.model_validate(payload)


def test_enum_valued_fields_arrive_as_plain_strings_and_still_match() -> None:
    """``use_enum_values=True`` on the shared BaseModel makes ``is`` comparison
    always false. This test is the guard on that trap, which cost a whole run."""

    message = _message("agent", author="root_agent", text="the answer")
    assert type(message.role) is str
    assert type(message.parts[0].kind) is str
    assert join_agent_text([message]) == "the answer"


def test_a_user_authored_message_is_not_the_agent_s_output() -> None:
    """Spike A9: a skip_summarization halt renders with role 'user'. Key off
    author, and never let a user-authored message become the report."""

    messages = [
        _message("user", author="user", text="the envelope"),
        _message("user", author="root_agent", text="the real answer"),
    ]
    assert join_agent_text(messages) == "the real answer"


def test_empty_output_fails_the_step_rather_than_passing_nothing_onward() -> None:
    output = extract_step_output([_message("agent", author="root_agent", text="   ")])
    assert output.code is StepOutputCode.EMPTY_STEP_OUTPUT
    assert output.text == ""
    assert not output.is_usable


def test_a_deny_event_blocks_the_step_even_though_the_payload_looks_ordinary() -> None:
    """The observed shape: one agent message whose text is the control's own
    verdict string. Nothing in the payload says it was blocked."""

    messages = [_message("agent", author="root_agent", text="Pattern '...' found")]
    output = extract_step_output(messages, deny_events=[_deny()])

    assert output.code is StepOutputCode.BLOCKED_BY_CONTROL
    assert output.control_name == "block-ssn"
    assert not output.is_usable
    assert output.text == "Pattern '...' found"


def test_no_deny_event_means_ordinary_output() -> None:
    output = extract_step_output([_message("agent", author="root_agent", text="three causes")])
    assert output.code is StepOutputCode.OK
    assert output.is_usable


def test_a_whole_blocked_turn_response_as_it_actually_arrived() -> None:
    """The captured shape, parsed the way the dispatcher parses it.

    One message, role agent, author root_agent, one text part carrying the
    control's own verdict string. Nothing in the payload distinguishes it from
    an answer, which is why the deny event is what decides.
    """

    turn = TurnResponse.model_validate(blocked_turn_payload())
    assert len(turn.messages) == 1
    assert join_agent_text(turn.messages) == BLOCKED_TURN_TEXT

    unaided = extract_step_output(turn.messages)
    assert unaided.code is StepOutputCode.OK, "the payload alone cannot tell; this is the point"

    output = extract_step_output(turn.messages, deny_events=[_deny()])
    assert output.code is StepOutputCode.BLOCKED_BY_CONTROL
    assert output.control_name == "block-ssn"
    assert output.detail is not None
    assert "post" in output.detail and "llm_call" in output.detail
    assert not output.is_usable


def test_an_ordinary_turn_response_is_extracted_whole_and_in_order() -> None:
    payload = blocked_turn_payload()
    payload["messages"] = [
        {
            "index": 0,
            "role": "agent",
            "author": "root_agent",
            "parts": [
                {"kind": "text", "text": "Three common causes:"},
                {"kind": "tool_call", "name": "get_weather"},
                {"kind": "text", "text": "1. deploy timing"},
            ],
        }
    ]
    turn = TurnResponse.model_validate(payload)
    output = extract_step_output(turn.messages)

    assert output.is_usable
    assert output.text == "Three common causes:\n1. deploy timing"
    assert output.control_name is None


def test_the_verdict_string_is_corroboration_and_never_the_test() -> None:
    """When the event carries the message the plugin substituted, the match is
    noted. It is not what makes the step blocked: the string is operator-edited
    free text in the console."""

    deny = _deny(metadata={"condition_trace": {"message": BLOCKED_TURN_TEXT}})
    messages = TurnResponse.model_validate(blocked_turn_payload()).messages

    matched = extract_step_output(messages, deny_events=[deny])
    assert matched.text_matched_deny_message is True

    bare = extract_step_output(messages, deny_events=[_deny()])
    assert bare.code is StepOutputCode.BLOCKED_BY_CONTROL
    assert bare.text_matched_deny_message is False, "absence of the string proves nothing"


def test_a_deny_blocks_the_step_even_when_the_agent_also_said_something_useful() -> None:
    messages = [_message("agent", author="root_agent", text="Here are the three causes...")]
    output = extract_step_output(messages, deny_events=[_deny()])

    assert output.code is StepOutputCode.BLOCKED_BY_CONTROL
    assert not output.is_usable
    shown_not_forwarded = "shown to the operator, forwarded to nobody"
    assert output.text == "Here are the three causes...", shown_not_forwarded


def test_the_deny_event_payload_the_query_returns_parses_as_written() -> None:
    event = ControlExecutionEvent.model_validate(deny_event_payload())
    assert event.action == "deny"
    assert event.matched is True
