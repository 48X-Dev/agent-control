"""Google ADK extraction helpers for Agent Control."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from google.adk.models import LlmResponse

from ._attachments import (
    AttachmentDescriptor,
    AttachmentScanner,
    build_attachment_summary,
)
from ._sanitize import neutralize_marker

try:
    from google.genai import types  # type: ignore[import-not-found]
except Exception as exc:  # pragma: no cover - optional dependency
    raise RuntimeError(
        "Google ADK integration requires google-adk. "
        "Install with: agent-control-sdk[google-adk]."
    ) from exc


def _extract_text_from_parts(parts: Any, placeholders: Mapping[int, str] | None = None) -> str:
    """Extract text blocks from an ADK parts collection.

    ``placeholders`` maps a part index to the transcript marker for a binary
    part at that position, so an attachment lands in part order inside the same
    string rather than at the end of it.

    Every chunk this did not author is marker-neutralized before assembly. For
    any text that does not contain the marker - which is all of it, outside an
    attack - the output is byte-identical to what this returned before.
    """

    if not isinstance(parts, list):
        return ""

    chunks: list[str] = []
    for index, part in enumerate(parts):
        chunk = _extract_text_from_part(part)
        if chunk is not None:
            chunks.append(neutralize_marker(chunk))

        placeholder = placeholders.get(index) if placeholders is not None else None
        if placeholder is not None:
            chunks.append(placeholder)

    return "\n".join(chunks).strip()


def _extract_text_from_part(part: Any) -> str | None:
    """Return the text a single part contributes, or ``None`` when it has none."""

    text = getattr(part, "text", None)
    if isinstance(text, str) and text:
        return text

    structured = _extract_structured_part(part)
    if structured is not None:
        return structured

    if isinstance(part, dict):
        dict_text = part.get("text")
        if isinstance(dict_text, str) and dict_text:
            return dict_text
        json_value = part.get("json")
        if json_value is not None:
            return _json_dumps(json_value)

    return None


def _extract_structured_part(part: Any) -> str | None:
    """Serialize non-text ADK part payloads that controls may still need to inspect."""

    structured_fields = (
        "function_call",
        "function_response",
        "executable_code",
        "code_execution_result",
    )
    for field_name in structured_fields:
        value = part.get(field_name) if isinstance(part, dict) else getattr(part, field_name, None)
        if value is not None:
            return _json_dumps(_to_jsonable(value))

    return None


def _to_jsonable(value: Any) -> Any:
    """Convert ADK/genai payload objects into JSON-serializable structures."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")

    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return value

    value_dict = getattr(value, "__dict__", None)
    if isinstance(value_dict, dict):
        return value_dict

    return str(value)


def _json_dumps(value: Any) -> str:
    """Serialize structured content deterministically for evaluator input."""

    return json.dumps(value, sort_keys=True)


@dataclass(frozen=True)
class ExtractedPayload:
    """Both halves of what a control sees for one model call.

    ``text`` keeps the string contract ``Step.input`` has always had, because
    ``ListEvaluator`` compiles its values into a regex and matches against
    ``str(data)``: a structured input would silently start matching a Python
    repr and every list and regex control already written would change meaning.

    ``attachments`` is the half that is actually load-bearing. It is
    server-authored, it never round-trips through a model, and it is what
    ``context.agent_control`` is built from.
    """

    text: str
    attachments: tuple[AttachmentDescriptor, ...] = ()

    def context_block(self) -> dict[str, Any]:
        """Build the ``agent_control`` block for ``Step.context``."""

        return {
            "attachments": [descriptor.to_dict() for descriptor in self.attachments],
            "attachment_summary": build_attachment_summary(self.attachments),
        }


def extract_request_payload(
    llm_request: Any,
    *,
    scanner: AttachmentScanner | None = None,
    placeholder_text: bool = True,
    describe_attachments: bool = True,
) -> ExtractedPayload:
    """Extract the control payload for one ADK model request.

    Text comes from ``contents[-1]``, exactly as it always has. Descriptors come
    from **every** ``Content``, because a file attached at turn 1 is still in
    the request at turn 40 - re-sent, re-read by the model, and invisible to
    every control on every call in between if the walk stops at the tail.

    Placeholder lines are emitted only for parts of the final ``Content``, since
    that is the only ``Content`` whose text is in ``Step.input`` at all. The
    numbering references the full descriptor list, so "attachment 2 of 5" says
    what the request actually carries rather than what its last message does.
    """

    contents = getattr(llm_request, "contents", None)
    if not isinstance(contents, list) or not contents:
        return ExtractedPayload(text="")

    working_scanner = scanner if scanner is not None else AttachmentScanner()
    descriptors: tuple[AttachmentDescriptor, ...] = ()
    if describe_attachments:
        descriptors = working_scanner.describe_contents(contents)

    last_index = len(contents) - 1
    placeholders = _build_placeholders(descriptors, last_index) if placeholder_text else None
    text = _extract_text_from_parts(getattr(contents[last_index], "parts", None), placeholders)
    return ExtractedPayload(text=text, attachments=descriptors)


def extract_response_payload(
    llm_response: Any,
    *,
    scanner: AttachmentScanner | None = None,
    placeholder_text: bool = True,
    describe_attachments: bool = True,
) -> ExtractedPayload:
    """Extract the control payload for one ADK model response.

    A model can emit ``inline_data`` of its own, and ``after_model`` controls
    see nothing for it otherwise. Those descriptors carry ``source="agent"``:
    the request-side manifest describes what an operator attached and says
    nothing about what the model produced.
    """

    content = getattr(llm_response, "content", None)
    if content is None:
        return ExtractedPayload(text="")

    parts = getattr(content, "parts", None)
    working_scanner = scanner if scanner is not None else AttachmentScanner()
    descriptors: tuple[AttachmentDescriptor, ...] = ()
    if describe_attachments:
        descriptors = working_scanner.describe_parts(parts, default_source="agent")

    placeholders = _build_placeholders(descriptors, 0) if placeholder_text else None
    text = _extract_text_from_parts(parts, placeholders)
    return ExtractedPayload(text=text, attachments=descriptors)


def _build_placeholders(
    descriptors: tuple[AttachmentDescriptor, ...],
    content_index: int,
) -> dict[int, str]:
    total = len(descriptors)
    return {
        descriptor.part_index: descriptor.placeholder_line(position, total)
        for position, descriptor in enumerate(descriptors, start=1)
        if descriptor.content_index == content_index
    }


def extract_request_text(llm_request: Any) -> str:
    """Extract the most recent text payload from an ADK LLM request.

    Retained as public SDK surface, and deliberately identical to what it always
    returned: no descriptor walk, no placeholder, no hashing cost. Callers that
    want the file half call :func:`extract_request_payload`.
    """

    return extract_request_payload(
        llm_request,
        placeholder_text=False,
        describe_attachments=False,
    ).text


def extract_response_text(llm_response: Any) -> str:
    """Extract text from an ADK LLM response."""

    return extract_response_payload(
        llm_response,
        placeholder_text=False,
        describe_attachments=False,
    ).text


def resolve_agent_name(callback_context: Any) -> str:
    """Resolve the currently executing ADK agent name."""

    callback_agent = getattr(callback_context, "agent", None)
    agent_name = getattr(callback_agent, "name", None)
    if isinstance(agent_name, str) and agent_name:
        return agent_name

    fallback = getattr(callback_context, "agent_name", None)
    if isinstance(fallback, str) and fallback:
        return fallback

    return "root_agent"


def resolve_tool_name(tool: Any) -> str:
    """Resolve an ADK tool name."""

    tool_name = getattr(tool, "name", None)
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    class_name = getattr(tool.__class__, "__name__", None)
    if isinstance(class_name, str) and class_name:
        return class_name
    return "tool"


def resolve_tool_agent_name(tool_context: Any) -> str | None:
    """Resolve the currently executing ADK agent name for a tool callback."""

    callback_context = getattr(tool_context, "callback_context", None)
    if callback_context is not None:
        agent_name = resolve_agent_name(callback_context)
        if agent_name:
            return agent_name

    fallback = getattr(tool_context, "agent_name", None)
    if isinstance(fallback, str) and fallback:
        return fallback

    return None


def build_blocked_llm_response(message: str) -> LlmResponse:
    """Create a replacement model response when a request is blocked."""

    content = types.Content(role="model", parts=[types.Part(text=message)])
    return _build_llm_response(content)


def _build_llm_response(content: Any) -> LlmResponse:
    """Construct an LLM response from a content payload."""

    response_type = _resolve_llm_response_type()
    return cast("LlmResponse", response_type(content=content))


def _resolve_llm_response_type() -> type[Any]:
    """Resolve the google.adk.models.LlmResponse class lazily."""

    try:
        from google.adk.models import LlmResponse  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Google ADK integration requires google-adk. "
            "Install with: agent-control-sdk[google-adk]."
        ) from exc
    return cast(type[Any], LlmResponse)


def append_operator_turn(llm_request: Any, text: str) -> bool:
    """Append a synthetic user-role ``Content`` to a live model request.

    This is how operator guidance reaches a model, and the role is the point.
    The same text appended to ``config.system_instruction`` would be invisible
    to this module and therefore to every control - an unevaluated channel into
    the model's highest-trust field, reachable by anyone who can queue a nudge.

    Note what this does **not** do for evaluation. The appended ``Content``
    becomes the new ``contents[-1]``, which is the only one
    :func:`extract_request_payload` takes text from, so calling this before
    extraction replaces the call's real input rather than adding to it. The
    caller is responsible for folding this text into the input it evaluates;
    the plugin appends after extraction and concatenates, for that reason.

    Returns whether the append happened. A request shaped in a way this cannot
    read is a nudge that is not delivered, never a model call that fails.
    """

    contents = getattr(llm_request, "contents", None)
    if not isinstance(contents, list):
        return False
    try:
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
    except Exception:  # pragma: no cover - defensive against ADK type changes
        return False
    return True


def build_blocked_tool_response(message: str) -> dict[str, str]:
    """Create a replacement tool response when a call is blocked."""

    return {
        "status": "blocked",
        "message": message,
    }
