export type ControlAction = "deny" | "steer" | "observe";

/**
 * One control's contribution to an evaluation, flattened from the wire
 * `ControlMatch` model. Mirrors the Python SDK's `ControlMatch`.
 */
export interface ControlMatchSummary {
  controlId: number;
  controlName: string;
  action: ControlAction | string;
  matched: boolean;
  confidence: number;
  message?: string;
  error?: string;
  metadata?: Record<string, unknown>;
  steeringContext?: string;
  controlExecutionId?: string;
}

/**
 * SDK-facing evaluation outcome. Field names match the Python SDK's
 * `EvaluationResult` (camelCased); every field beyond `isSafe` is optional so
 * the shape stays assignable from the narrower 3.1.0 definition.
 */
export interface EvaluationResult {
  isSafe: boolean;
  reason?: string;
  confidence?: number;
  matches?: ControlMatchSummary[];
  errors?: ControlMatchSummary[];
  nonMatches?: ControlMatchSummary[];
}

/**
 * Why the SDK refused a call it could not evaluate.
 *
 * Every one of these is a fail-closed refusal: the wrapped function either did
 * not run (pre stage) or its output is withheld (post stage).
 */
export type ControlEvaluationFailureReason =
  /** `init()` was never called on the client backing this control site. */
  | "not_initialized"
  /** `init({ register: false })`, so no control set was ever fetched. */
  | "registration_skipped"
  /** Registration ran but failed (server unreachable, auth rejected, HTTP error). */
  | "registration_failed"
  /** No control set has ever been cached for this session. */
  | "cache_missing"
  /** The cached control set is older than `controlCacheMaxAgeMs`. */
  | "cache_stale"
  /** An applicable control declares `execution: "sdk"`, which this SDK cannot run. */
  | "sdk_execution_unsupported"
  /** An applicable control could not be interpreted (e.g. invalid `step_name_regex`). */
  | "control_unreadable"
  /** The evaluation request itself failed (network error, timeout, non-2xx). */
  | "evaluation_request_failed"
  /** The evaluation returned nothing usable. */
  | "evaluation_empty"
  /** The evaluation completed but one or more controls errored during evaluation. */
  | "evaluation_errors";

export type ControlStage = "pre" | "post";

/**
 * Raised when a control decision could not be established, and the call was
 * refused rather than allowed through.
 *
 * The Python SDK raises a bare `RuntimeError` in the equivalent situations
 * (see `control_decorators._run_control_check`); this is the same contract
 * with a checkable type. It is deliberately NOT a `ControlViolationError`: no
 * control decided anything, the SDK just refused to guess.
 */
export class ControlEvaluationError extends Error {
  readonly reason: ControlEvaluationFailureReason;
  readonly stage?: ControlStage;
  readonly stepName?: string;

  constructor(params: {
    reason: ControlEvaluationFailureReason;
    message: string;
    stage?: ControlStage;
    stepName?: string;
    cause?: unknown;
  }) {
    super(params.message, params.cause === undefined ? undefined : { cause: params.cause });
    this.name = "ControlEvaluationError";
    this.reason = params.reason;
    this.stage = params.stage;
    this.stepName = params.stepName;
  }
}

/** Raised when a control matched with the `deny` action. */
export class ControlViolationError extends Error {
  readonly controlName: string;
  readonly controlId: string;
  readonly action: ControlAction;
  readonly evaluationResult: EvaluationResult;
  readonly metadata: Record<string, unknown>;
  readonly stage?: ControlStage;

  constructor(params: {
    controlName: string;
    controlId: string;
    action: ControlAction;
    evaluationResult: EvaluationResult;
    message?: string;
    metadata?: Record<string, unknown>;
    stage?: ControlStage;
  }) {
    super(params.message ?? `Control violation: ${params.controlName}`);
    this.name = "ControlViolationError";
    this.controlName = params.controlName;
    this.controlId = params.controlId;
    this.action = params.action;
    this.evaluationResult = params.evaluationResult;
    this.metadata = params.metadata ?? {};
    this.stage = params.stage;
  }
}

/**
 * Raised when a control matched with the `steer` action.
 *
 * Equivalent to the Python SDK's `ControlSteerError`. Unlike a deny, this is a
 * corrective signal: `steeringContext` says what to change, and the caller may
 * adjust and retry. It is still a refusal, not a pass.
 */
export class ControlSteerError extends Error {
  readonly controlName: string;
  readonly controlId: string;
  readonly action: ControlAction = "steer";
  readonly steeringContext: string;
  readonly evaluationResult: EvaluationResult;
  readonly metadata: Record<string, unknown>;
  readonly stage?: ControlStage;

  constructor(params: {
    controlName: string;
    controlId: string;
    steeringContext: string;
    evaluationResult: EvaluationResult;
    message?: string;
    metadata?: Record<string, unknown>;
    stage?: ControlStage;
  }) {
    super(
      params.message ??
        `Control steering [${params.controlName}]: ${params.steeringContext}`,
    );
    this.name = "ControlSteerError";
    this.controlName = params.controlName;
    this.controlId = params.controlId;
    this.steeringContext = params.steeringContext;
    this.evaluationResult = params.evaluationResult;
    this.metadata = params.metadata ?? {};
    this.stage = params.stage;
  }
}
