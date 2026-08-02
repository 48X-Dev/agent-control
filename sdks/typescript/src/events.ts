/**
 * Control-execution event reconstruction, ported from the Python SDK's
 * `evaluation_events.build_control_execution_events`. Field-for-field the same
 * event shape, so both SDKs land in the same observability tables.
 */
import type { ControlExecutionEvent } from "./generated/models/control-execution-event";
import type { ControlMatch } from "./generated/models/control-match";
import type { EvaluationResponse } from "./generated/models/evaluation-response";
import { observabilityIdentity, type RenderedControl, type StepDescriptor } from "./applicability";
import { FALLBACK_SPAN_ID, FALLBACK_TRACE_ID } from "./tracing";

/** Raw/debug metadata that must not leave the process as observability data. */
const DEBUG_METADATA_KEYS = new Set([
  "selected_data",
  "selected_data_preview",
  "engine_selected_data",
  "engine_selected_data_preview",
]);

function safeEventMetadata(metadata: Record<string, unknown>): Record<string, unknown> {
  const safe: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(metadata)) {
    if (!DEBUG_METADATA_KEYS.has(key)) {
      safe[key] = value;
    }
  }

  if (!("input" in safe)) {
    for (const previewKey of ["engine_selected_data_preview", "selected_data_preview"]) {
      const preview = metadata[previewKey];
      if (preview && typeof preview === "object" && "value" in (preview as object)) {
        safe["input"] = (preview as Record<string, unknown>)["value"];
        break;
      }
    }
  }

  return safe;
}

export function mapAppliesTo(stepType: string): "llm_call" | "tool_call" {
  return stepType === "tool" ? "tool_call" : "llm_call";
}

function buildEvents(
  matches: ControlMatch[] | null | undefined,
  options: {
    matched: boolean;
    includeErrorMessage: boolean;
    step: StepDescriptor;
    controlLookup: Map<number, RenderedControl>;
    traceId: string;
    spanId: string;
    agentName: string;
    timestamp: Date;
  },
): ControlExecutionEvent[] {
  if (!matches || matches.length === 0) {
    return [];
  }

  const appliesTo = mapAppliesTo(options.step.type);

  return matches.map((match) => {
    const metadata = safeEventMetadata(
      (match.result.metadata ?? {}) as Record<string, unknown>,
    );
    let selectorPath: string | null = null;
    let evaluatorName: string | null = null;

    const known = options.controlLookup.get(match.controlId);
    if (known) {
      const identity = observabilityIdentity(known.definition);
      selectorPath = identity.selectorPath;
      evaluatorName = identity.evaluatorName;
      metadata["primary_evaluator"] = identity.evaluatorName;
      metadata["primary_selector_path"] = identity.selectorPath;
      metadata["leaf_count"] = identity.leafCount;
      metadata["all_evaluators"] = identity.allEvaluators;
      metadata["all_selector_paths"] = identity.allSelectorPaths;
    }

    return {
      controlExecutionId: match.controlExecutionId,
      traceId: options.traceId,
      spanId: options.spanId,
      agentName: options.agentName,
      controlId: match.controlId,
      controlName: match.controlName,
      checkStage: options.step.stage,
      appliesTo,
      action: match.action,
      matched: options.matched,
      confidence: match.result.confidence,
      timestamp: options.timestamp,
      evaluatorName,
      selectorPath,
      errorMessage: options.includeErrorMessage ? (match.result.error ?? null) : null,
      metadata,
    } satisfies ControlExecutionEvent;
  });
}

export function buildControlExecutionEvents(params: {
  response: EvaluationResponse;
  step: StepDescriptor;
  controlLookup: Map<number, RenderedControl>;
  traceId: string | null;
  spanId: string | null;
  agentName: string;
}): ControlExecutionEvent[] {
  const traceId = params.traceId || FALLBACK_TRACE_ID;
  const spanId = params.spanId || FALLBACK_SPAN_ID;
  const timestamp = new Date();

  const shared = {
    step: params.step,
    controlLookup: params.controlLookup,
    traceId,
    spanId,
    agentName: params.agentName,
    timestamp,
  };

  return [
    ...buildEvents(params.response.matches, {
      ...shared,
      matched: true,
      includeErrorMessage: true,
    }),
    ...buildEvents(params.response.errors, {
      ...shared,
      matched: false,
      includeErrorMessage: true,
    }),
    ...buildEvents(params.response.nonMatches, {
      ...shared,
      matched: false,
      includeErrorMessage: false,
    }),
  ];
}
