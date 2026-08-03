"""Extracting a step's output, section 9.3.

Read ``TurnResponse.messages``, keep the agent's own messages, keep the text
parts, join them. Then three things can be true of the result, and only one of
them is a usable report.

**Empty after stripping** fails the step with ``EMPTY_STEP_OUTPUT``. An empty
report is never passed onward, because B receiving "the previous agent
reported: (nothing)" is how B invents the missing work and reports it
confidently.

**The text is a control block.** Section 9.3 says the dispatcher must recognise
the plugin's blocked-response shape. Slice 1 observed one, and *there is no
shape to recognise*: see :data:`BLOCK_DETECTION_NOTE`. Block detection here
therefore takes deny evidence from the observability store as an argument, and
the payload text is only ever corroboration.

**Spike A9's rendering wrinkle applies.** A ``skip_summarization`` halt's
terminal event has ``content.role == "user"`` and carries raw JSON. Key off
``author``, never ``role``. The spike says it twice; this is the third place it
matters, so :func:`_is_agent_output` prefers ``author`` and falls back to
``role`` only when the executor reported no author at all.

One trap, paid for once already. ``agent_control_models.base.BaseModel`` sets
``use_enum_values=True``, so ``message.role`` and ``part.kind`` arrive as plain
strings even though they are *declared* as ``StrEnum``. ``part.kind is
SessionMessagePartKind.TEXT`` is therefore always false, mypy cannot see it
because the declared type is the enum, and the symptom is every step reporting
``EMPTY_STEP_OUTPUT`` while the transcript plainly holds the answer. Compare
with ``==``, which works in both directions because these are ``StrEnum``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent_control_models.observability import ControlExecutionEvent
from agent_control_models.sessions import (
    SessionMessage,
    SessionMessagePartKind,
    SessionMessageRole,
)

BLOCK_DETECTION_NOTE = """\
A blocked turn is indistinguishable from ordinary model output in the turn
payload. Observed 2026-08-02 against a live executor: a post-stage deny from
the `block-ssn` control produced exactly one message, role `agent`, author
`root_agent`, one text part whose text was the control's own verdict string
("Pattern '...' found") and nothing else. No marker, no status field, no
distinguishing structure - the plugin substitutes the violation message
verbatim (`plugin._handle_llm_exception` -> `build_blocked_llm_response`), and
that message is control-authored free text. Matching on it would mean matching
on a string an operator edits in the console.

So the deny is read from the control-execution events instead, which is where
the server records it with a control name, a stage and a matched flag.\
"""


class StepOutputCode(StrEnum):
    """What came back from a step."""

    OK = "ok"
    EMPTY_STEP_OUTPUT = "EMPTY_STEP_OUTPUT"
    BLOCKED_BY_CONTROL = "BLOCKED_BY_CONTROL"


@dataclass(frozen=True, slots=True)
class StepOutput:
    """The step's report, and whether it is one.

    ``text`` is populated even when ``code`` is ``BLOCKED_BY_CONTROL``, because
    an operator watching the terminal wants to see what the model was made to
    say. It is never forwarded to another step: forwarding a refusal downstream
    as if it were a finding is the worst-quality failure available here.
    """

    text: str
    code: StepOutputCode
    control_name: str | None = None
    detail: str | None = None
    text_matched_deny_message: bool = False

    @property
    def is_usable(self) -> bool:
        return self.code is StepOutputCode.OK


def extract_step_output(
    messages: Sequence[SessionMessage],
    *,
    deny_events: Sequence[ControlExecutionEvent] = (),
    required_output: str = "text",
) -> StepOutput:
    """Turn one turn's messages into a step result.

    ``deny_events`` are control executions the caller has already narrowed to
    this turn (see ``client.deny_events_for_turn``). Any of them makes the step
    terminal: a turn the guardrails blocked is a completed turn with a
    substituted answer, so nothing in the payload will say so.
    """

    text = join_agent_text(messages)

    if deny_events:
        event = deny_events[0]
        return StepOutput(
            text=text,
            code=StepOutputCode.BLOCKED_BY_CONTROL,
            control_name=event.control_name,
            detail=_deny_detail(event),
            text_matched_deny_message=any(
                _deny_message(candidate) == text.strip() for candidate in deny_events
            ),
        )

    if required_output == "text" and not text.strip():
        return StepOutput(
            text="",
            code=StepOutputCode.EMPTY_STEP_OUTPUT,
            detail="The agent produced no text. Nothing is passed onward.",
        )

    return StepOutput(text=text, code=StepOutputCode.OK)


def join_agent_text(messages: Sequence[SessionMessage]) -> str:
    """Agent-authored text parts, in order, joined."""

    chunks: list[str] = []
    for message in messages:
        if not _is_agent_output(message):
            continue
        for part in message.parts:
            if part.kind == SessionMessagePartKind.TEXT and part.text:
                chunks.append(part.text)
    return "\n".join(chunks).strip()


def _is_agent_output(message: SessionMessage) -> bool:
    if message.author:
        return message.author != "user"
    return message.role == SessionMessageRole.AGENT


def _deny_detail(event: ControlExecutionEvent) -> str:
    return (
        f"control '{event.control_name}' denied at the {event.check_stage} stage "
        f"of a {event.applies_to}"
    )


def _deny_message(event: ControlExecutionEvent) -> str | None:
    """The verdict string the plugin substitutes, when the event carries one.

    Corroboration only. Its absence proves nothing and its presence is not what
    makes the step blocked.
    """

    metadata: dict[str, Any] | None = event.metadata
    if not isinstance(metadata, dict):
        return None
    trace = metadata.get("condition_trace")
    if not isinstance(trace, dict):
        return None
    message = trace.get("message")
    return message if isinstance(message, str) else None
