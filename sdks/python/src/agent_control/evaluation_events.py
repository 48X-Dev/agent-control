"""Derived control-execution event reconstruction for SDK evaluation flows."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal

from agent_control_models import (
    ControlDefinition,
    ControlDefinitionRuntime,
    ControlExecutionEvent,
    ControlMatch,
    EvaluationRequest,
    EvaluationResponse,
)

from .observability import get_logger, is_observability_enabled, write_events

_logger = get_logger(__name__)

# All-zero values are invalid trace/span IDs per OpenTelemetry and make it
# obvious that the event could not be correlated to an external trace.
_FALLBACK_TRACE_ID = "0" * 32
_FALLBACK_SPAN_ID = "0" * 16
_trace_warning_logged = False
_DEBUG_METADATA_KEYS = frozenset(
    {
        "selected_data",
        "selected_data_preview",
        "engine_selected_data",
        "engine_selected_data_preview",
    }
)


#: Prefix reserved for values the *server* authors on ingest. A client-supplied
#: key carrying it is dropped, because the audited party must not be able to
#: attest its own audit record.
_SERVER_AUTHORED_PREFIX = "agent_control."

#: Prefix for values the agent process reports about itself. Everything under it
#: is an **unverified self-report** and is trustworthy only where it agrees with
#: the server's own stamp. The divergence is the artifact worth alerting on.
_REPORTED_PREFIX = "reported."


def _safe_event_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Drop raw/debug metadata that should not be exported as observability data."""
    safe_metadata = {k: v for k, v in metadata.items() if k not in _DEBUG_METADATA_KEYS}
    if "input" not in safe_metadata:
        for preview_key in ("engine_selected_data_preview", "selected_data_preview"):
            preview = metadata.get(preview_key)
            if isinstance(preview, dict) and "value" in preview:
                safe_metadata["input"] = preview["value"]
                break
    return safe_metadata


def _reported_config_metadata(request: EvaluationRequest) -> dict[str, object]:
    """Extract this agent's self-reported configuration from the step context.

    Answers the question an operator actually asks at 2am: *which prompt and
    which model produced this decision?* Somebody moves Sales to a cheap model
    on Tuesday and denials triple on Wednesday; without these keys the event
    stream contains no field connecting the two.

    Two rules, and both matter.

    Client-supplied keys under the server-authored prefix are dropped here. The
    agent reports its own configuration, so letting it write
    ``agent_control.config_etag_current`` would let the audited party forge the
    server's view of what it should have been running.

    Everything that does survive is routed through ``_safe_event_metadata``
    rather than around it, so any key later added to ``_DEBUG_METADATA_KEYS`` is
    stripped from this path too.

    ``reported.config_etag`` is an opaque server-issued token a client cannot
    fabricate without having fetched it, which is what makes a divergence from
    the server's own stamp a usable tamper signal. ``reported.model_id`` earns
    its own key beside it because an etag answers "did the agent hold the
    current config" and cannot answer "which model produced this denial".
    """
    context = getattr(request.step, "context", None)
    if not isinstance(context, Mapping):
        return {}

    reported: dict[str, object] = {}
    for key, value in context.items():
        if not isinstance(key, str):
            continue
        if key.startswith(_SERVER_AUTHORED_PREFIX):
            _logger.debug(
                "Dropping client-supplied metadata key %r: the %r prefix is "
                "reserved for server-authored values.",
                key,
                _SERVER_AUTHORED_PREFIX,
            )
            continue
        if key.startswith(_REPORTED_PREFIX):
            reported[key] = value
    return reported


def observability_metadata(
    control_def: ControlDefinition | ControlDefinitionRuntime,
) -> tuple[str | None, str | None, dict[str, object]]:
    """Return representative event fields plus full composite context."""
    identity = control_def.observability_identity()
    return (
        identity.selector_path,
        identity.evaluator_name,
        {
            "primary_evaluator": identity.evaluator_name,
            "primary_selector_path": identity.selector_path,
            "leaf_count": identity.leaf_count,
            "all_evaluators": identity.all_evaluators,
            "all_selector_paths": identity.all_selector_paths,
        },
    )


def map_applies_to(step_type: str) -> Literal["llm_call", "tool_call"]:
    """Map Agent Control step types to observability applies_to values."""
    return "tool_call" if step_type == "tool" else "llm_call"


def _resolve_event_trace_context(
    trace_id: str | None,
    span_id: str | None,
) -> tuple[str, str]:
    """Return event IDs, applying fallback IDs and a one-time warning if needed."""
    global _trace_warning_logged  # noqa: PLW0603

    if trace_id and span_id:
        return trace_id, span_id

    if not _trace_warning_logged:
        _logger.warning(
            "Emitting control events without trace context; events will use fallback "
            "IDs and cannot be correlated with traces. Pass trace_id/span_id for "
            "full observability."
        )
        _trace_warning_logged = True

    return trace_id or _FALLBACK_TRACE_ID, span_id or _FALLBACK_SPAN_ID


def _build_events_for_matches(
    matches: list[ControlMatch] | None,
    *,
    matched: bool,
    include_error_message: bool,
    request: EvaluationRequest,
    control_lookup: Mapping[int, ControlDefinition | ControlDefinitionRuntime],
    trace_id: str,
    span_id: str,
    agent_name: str,
    now: datetime,
) -> list[ControlExecutionEvent]:
    if not matches:
        return []

    applies_to = map_applies_to(request.step.type)
    events: list[ControlExecutionEvent] = []
    reported_config = _reported_config_metadata(request)

    for match in matches:
        control_def = control_lookup.get(match.control_id)
        raw_metadata = dict(match.result.metadata or {})
        # Merged before the safety filter, not after, so the reported keys are
        # subject to every rule that applies to evaluator metadata.
        raw_metadata.update(reported_config)
        event_metadata = _safe_event_metadata(raw_metadata)
        selector_path = None
        evaluator_name = None

        if control_def is not None:
            selector_path, evaluator_name, identity_metadata = observability_metadata(control_def)
            event_metadata.update(identity_metadata)

        events.append(
            ControlExecutionEvent(
                control_execution_id=match.control_execution_id,
                trace_id=trace_id,
                span_id=span_id,
                agent_name=agent_name,
                control_id=match.control_id,
                control_name=match.control_name,
                check_stage=request.stage,
                applies_to=applies_to,
                action=match.action,
                matched=matched,
                confidence=match.result.confidence,
                timestamp=now,
                evaluator_name=evaluator_name,
                selector_path=selector_path,
                error_message=match.result.error if include_error_message else None,
                metadata=event_metadata,
            )
        )

    return events


def build_control_execution_events(
    response: EvaluationResponse,
    request: EvaluationRequest,
    control_lookup: Mapping[int, ControlDefinition | ControlDefinitionRuntime],
    trace_id: str | None,
    span_id: str | None,
    agent_name: str | None,
) -> list[ControlExecutionEvent]:
    """Reconstruct control execution events from an evaluation response.

    This is the shared reconstruction step used by both supported event
    creation styles:
    - the default SDK observability path, where reconstructed local events are
      queued into the existing SDK batcher
    - the merged-event path, where local and server events are reconstructed in
      the SDK and queued together through the existing SDK batcher

    Args:
        response: Evaluation response containing matches, errors, and
            non-matches.
        request: Original evaluation request used to derive stage and
            ``applies_to``.
        control_lookup: Parsed controls keyed by control ID.
        trace_id: Optional trace ID for correlation.
        span_id: Optional span ID for correlation.
        agent_name: Optional override for the agent name stamped on events.

    Returns:
        A list of reconstructed ``ControlExecutionEvent`` objects.
    """
    resolved_trace_id, resolved_span_id = _resolve_event_trace_context(trace_id, span_id)
    resolved_agent_name = agent_name or request.agent_name
    now = datetime.now(UTC)

    events: list[ControlExecutionEvent] = []
    events.extend(
        _build_events_for_matches(
            response.matches,
            matched=True,
            include_error_message=True,
            request=request,
            control_lookup=control_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    events.extend(
        _build_events_for_matches(
            response.errors,
            matched=False,
            include_error_message=True,
            request=request,
            control_lookup=control_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    events.extend(
        _build_events_for_matches(
            response.non_matches,
            matched=False,
            include_error_message=False,
            request=request,
            control_lookup=control_lookup,
            trace_id=resolved_trace_id,
            span_id=resolved_span_id,
            agent_name=resolved_agent_name,
            now=now,
        )
    )
    return events


def enqueue_observability_events(events: list[ControlExecutionEvent]) -> None:
    """Enqueue reconstructed events through the existing SDK observability path.

    This preserves the built-in SDK behavior of forwarding events through the
    existing observability batcher.

    Args:
        events: Reconstructed control execution events to enqueue.

    Returns:
        None.
    """
    if not is_observability_enabled():
        return

    write_events(events)
