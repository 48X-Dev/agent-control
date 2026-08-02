import { defaultClient } from "./client";

export { AgentControlClient, defaultClient } from "./client";
export { control } from "./control";
export type { AsyncFn, ControlOptions } from "./control";
export {
  ControlEvaluationError,
  ControlSteerError,
  ControlViolationError,
} from "./errors";
export type {
  ControlAction,
  ControlEvaluationFailureReason,
  ControlMatchSummary,
  ControlStage,
  EvaluationResult,
} from "./errors";
export {
  ControlSession,
  DEFAULT_CONTROL_CACHE_MAX_AGE_MS,
  DEFAULT_CONTROL_REFRESH_INTERVAL_MS,
} from "./session";
export type { ControlPlaneSnapshot, ControlSessionOptions } from "./session";
export type { ControlLogger } from "./logging";
export type {
  AgentControlInitOptions,
  AgentsApi,
  ControlsApi,
  EvaluationApi,
  EvaluatorsApi,
  ObservabilityApi,
  PoliciesApi,
  StepSchema,
  SystemApi,
} from "./client";
export type { JsonObject, JsonPrimitive, JsonValue } from "./types";
export * from "./types";

const agentControl = defaultClient;

export default agentControl;
