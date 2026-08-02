/**
 * `control()` - evaluate server-defined controls around a function call.
 *
 * Ported from the Python SDK's `@agent_control.control()` decorator
 * (sdks/python/src/agent_control/control_decorators.py) and the shared
 * enforcement helper in `integrations/_core.py`. Same stages, same action
 * resolution, same error taxonomy, same observability events.
 *
 * One deliberate divergence from Python, and it is the whole point of this
 * module: THIS SDK FAILS CLOSED. Python's `init()` swallows connection errors,
 * leaves its control cache empty, and `evaluate_controls` then returns
 * `is_safe=True` without ever contacting the server. Here, anything that stops
 * the SDK from establishing a decision refuses the call. Fail-open is available
 * as `failOpen: true` at init, it is never the default, and it never relaxes an
 * actual control decision.
 */
import { prefilterControls, type RenderedControl, type StepDescriptor } from "./applicability";
import { AgentControlClient, defaultClient } from "./client";
import {
  ControlEvaluationError,
  ControlSteerError,
  ControlViolationError,
  type ControlAction,
  type ControlMatchSummary,
  type ControlStage,
  type EvaluationResult,
} from "./errors";
import { buildControlExecutionEvents } from "./events";
import type { ControlMatch } from "./generated/models/control-match";
import type { EvaluationResponse } from "./generated/models/evaluation-response";
import type { Step } from "./generated/models/step";
import { consoleLogger, type ControlLogger } from "./logging";
import type { ControlPlaneSnapshot, ControlSession } from "./session";
import { generateSpanId, generateTraceId } from "./tracing";

export interface ControlOptions {
  /** Documentation only, matching the Python decorator's `policy` argument. */
  policy?: string;
  /** Step name used for control scoping. Defaults to the wrapped function's name. */
  stepName?: string;
  /** Step type used for control scoping. Defaults to `"llm"`. */
  stepType?: string;
  /** Client whose session backs this control site. Defaults to the package default client. */
  client?: AgentControlClient;
  /** Override how the evaluated input is derived from the call arguments. */
  getInput?: (args: unknown[]) => unknown;
  /** Override how the evaluated output is derived from the return value. */
  getOutput?: (result: unknown) => unknown;
  /** Extra context forwarded on the evaluation step payload. */
  context?: Record<string, unknown>;
}

export type AsyncFn<TArgs extends unknown[], TResult> = (...args: TArgs) => Promise<TResult>;

const INPUT_KEYS = [
  "input",
  "message",
  "query",
  "text",
  "prompt",
  "content",
  "userInput",
  "user_input",
];

const OBSERVE_ALIASES = new Set(["allow", "observe", "warn", "log"]);

/** Port of `agent_control_models.normalize_action`; `null` means "not interpretable". */
function normalizeAction(action: string): ControlAction | null {
  if (OBSERVE_ALIASES.has(action)) {
    return "observe";
  }
  if (action === "deny" || action === "steer") {
    return action;
  }
  return null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Describe a transport failure without quoting it.
 *
 * Refusal messages are thrown at the caller and usually end up in application
 * logs, so they must not carry anything the control plane sent back. The
 * generated `AgentControlSDKError` puts the raw upstream response body in its
 * `message`, and an API key or connection string in a 500 body would ride
 * along. Only the class name and, when present, the HTTP status survive; the
 * original error is still reachable through the thrown error's `cause`.
 */
function describeTransportError(error: unknown): string {
  if (!(error instanceof Error)) {
    return "unknown error";
  }
  const status = (error as { statusCode?: unknown }).statusCode;
  if (typeof status === "number") {
    return `${error.name} (HTTP ${status})`;
  }
  return error.name;
}

/**
 * Stringify call arguments for evaluation without ever throwing.
 *
 * Python's `_extract_input_from_args` uses `str(...)`, which has no failure
 * mode. `JSON.stringify` has two: circular references and BigInt both raise a
 * TypeError. That throw would escape the wrapper as neither a control decision
 * nor a documented refusal, and it would escape under `failOpen` too. So the
 * first pass keeps the exact JSON shape, and a second pass degrades only
 * back-references. Content cannot hide behind a cycle: every object is still
 * serialized on first visit, and only repeat visits collapse.
 */
function safeStringify(value: unknown): string {
  const scalar = (candidate: unknown): unknown => {
    if (typeof candidate === "bigint") {
      return candidate.toString();
    }
    if (typeof candidate === "function" || typeof candidate === "symbol") {
      return String(candidate);
    }
    return candidate;
  };

  try {
    const direct = JSON.stringify(value, (_key, nested: unknown) => scalar(nested));
    if (typeof direct === "string") {
      return direct;
    }
  } catch {
    // Circular structure; fall through to the degrading pass.
  }

  try {
    const seen = new WeakSet<object>();
    const degraded = JSON.stringify(value, (_key, nested: unknown) => {
      const normalized = scalar(nested);
      if (typeof normalized === "object" && normalized !== null) {
        if (seen.has(normalized)) {
          return "[Circular]";
        }
        seen.add(normalized);
      }
      return normalized;
    });
    if (typeof degraded === "string") {
      return degraded;
    }
  } catch {
    // Fall through to the last resort.
  }

  try {
    return String(value);
  } catch {
    return "[unserializable]";
  }
}

function defaultInput(args: unknown[], stepType: string): unknown {
  if (stepType === "tool") {
    if (args.length === 1 && isPlainObject(args[0])) {
      return args[0];
    }
    return { args };
  }

  if (args.length === 0) {
    return "";
  }
  if (args.length === 1) {
    const only = args[0];
    if (typeof only === "string") {
      return only;
    }
    if (isPlainObject(only)) {
      for (const key of INPUT_KEYS) {
        const candidate = only[key];
        if (typeof candidate === "string") {
          return candidate;
        }
        if (candidate !== undefined && candidate !== null) {
          return safeStringify(candidate);
        }
      }
    }
    return typeof only === "undefined" ? "" : safeStringify(only);
  }

  // Python's `_extract_input_from_args` falls back to the first string argument
  // before stringifying everything. Keeping that ordering matters: it is what
  // gives a text evaluator the prompt rather than a JSON array containing it.
  const firstString = args.find((arg) => typeof arg === "string");
  if (typeof firstString === "string") {
    return firstString;
  }
  return safeStringify(args);
}

function defaultOutput(result: unknown): unknown {
  if (result === undefined) {
    return null;
  }
  const kind = typeof result;
  if (kind === "function" || kind === "symbol" || kind === "bigint") {
    return String(result);
  }
  return result;
}

function toMatchSummary(match: ControlMatch): ControlMatchSummary {
  return {
    controlId: match.controlId,
    controlName: match.controlName,
    action: match.action,
    matched: match.result.matched,
    confidence: match.result.confidence,
    message: match.result.message ?? undefined,
    error: match.result.error ?? undefined,
    metadata: (match.result.metadata ?? undefined) as Record<string, unknown> | undefined,
    steeringContext: match.steeringContext?.message ?? undefined,
    controlExecutionId: match.controlExecutionId,
  };
}

function toEvaluationResult(response: EvaluationResponse): EvaluationResult {
  return {
    isSafe: response.isSafe,
    confidence: response.confidence,
    reason: response.reason ?? undefined,
    matches: (response.matches ?? []).map(toMatchSummary),
    errors: (response.errors ?? []).map(toMatchSummary),
    nonMatches: (response.nonMatches ?? []).map(toMatchSummary),
  };
}

class StageRunner {
  constructor(
    private readonly client: AgentControlClient,
    private readonly session: ControlSession,
    /**
     * Resolved per stage, not once per call. A function that takes longer than
     * the staleness bound must not have its output waved through on the
     * strength of a control set that expired while it was running.
     */
    private readonly resolveSnapshot: () => ControlPlaneSnapshot | null,
    private readonly logger: ControlLogger,
    private readonly stepName: string,
    private readonly stepType: string,
    private readonly traceId: string,
    private readonly spanId: string,
    private readonly context: Record<string, unknown> | undefined,
  ) {}

  private get failOpen(): boolean {
    return this.session.options.failOpen;
  }

  /** Throw, unless the session explicitly opted into fail-open. */
  private refuse(error: ControlEvaluationError): void {
    if (this.failOpen) {
      this.logger.warn(
        `failOpen: allowing '${this.stepName}' despite an unevaluated control decision. ${error.message}`,
        error,
      );
      return;
    }
    throw error;
  }

  async run(stage: ControlStage, input: unknown, output: unknown): Promise<void> {
    const snapshot = this.resolveSnapshot();
    if (snapshot === null) {
      // Snapshot resolution already refused-or-warned; under failOpen there is
      // nothing to evaluate against.
      return;
    }

    const step: StepDescriptor = { name: this.stepName, type: this.stepType, stage };
    const prefiltered = prefilterControls(snapshot.controls, step);

    for (const unreadable of prefiltered.unreadableControls) {
      this.refuse(
        new ControlEvaluationError({
          reason: "control_unreadable",
          stage,
          stepName: this.stepName,
          message:
            `Control '${unreadable.control.name}' (id ${unreadable.control.id}) could not be ` +
            `interpreted, so it is unknown whether it applies to '${this.stepName}': ` +
            `${unreadable.problem}. Refusing the call.`,
        }),
      );
    }

    if (prefiltered.unsupportedLocalControls.length > 0) {
      const names = prefiltered.unsupportedLocalControls.map((c) => c.name).join(", ");
      this.refuse(
        new ControlEvaluationError({
          reason: "sdk_execution_unsupported",
          stage,
          stepName: this.stepName,
          message:
            `Control(s) [${names}] apply to '${this.stepName}' but declare execution: "sdk". ` +
            `The TypeScript SDK has no local evaluator engine and cannot run them, so the ` +
            `decision is unknown and the call is refused. Set execution: "server" on these ` +
            `controls, or run this step through the Python SDK.`,
        }),
      );
    }

    if (prefiltered.serverControls.length === 0) {
      // A control set was fetched and nothing in it applies to this step. That
      // is a real decision, not an absence of one.
      return;
    }

    const stepPayload: Step = {
      type: this.stepType,
      name: this.stepName,
      input,
      output: stage === "pre" ? null : output,
      context: this.context,
    };

    let response: EvaluationResponse | undefined;
    try {
      response = await this.client.evaluate({
        agentName: snapshot.agentName,
        stage,
        step: stepPayload,
        targetType: snapshot.targetType,
        targetId: snapshot.targetId,
      });
    } catch (error) {
      this.refuse(
        new ControlEvaluationError({
          reason: "evaluation_request_failed",
          stage,
          stepName: this.stepName,
          cause: error,
          message:
            `The ${stage}-stage evaluation of '${this.stepName}' could not be performed ` +
            `(${describeTransportError(error)}). ${prefiltered.serverControls.length} control(s) ` +
            `applied and none of them were checked, so the call is refused. See the error's ` +
            `cause for transport detail.`,
        }),
      );
      return;
    }

    if (!response) {
      this.refuse(
        new ControlEvaluationError({
          reason: "evaluation_empty",
          stage,
          stepName: this.stepName,
          message:
            `The ${stage}-stage evaluation of '${this.stepName}' returned no result. An absent ` +
            `decision is not a safe decision, so the call is refused.`,
        }),
      );
      return;
    }

    this.emitEvents(response, step, prefiltered.serverControls, snapshot.agentName);
    this.enforce(response, stage);
  }

  private emitEvents(
    response: EvaluationResponse,
    step: StepDescriptor,
    controls: RenderedControl[],
    agentName: string,
  ): void {
    if (!this.session.options.observability) {
      return;
    }

    const lookup = new Map(controls.map((control) => [control.id, control]));
    const events = buildControlExecutionEvents({
      response,
      step,
      controlLookup: lookup,
      traceId: this.traceId,
      spanId: this.spanId,
      agentName,
    });

    if (events.length === 0) {
      return;
    }

    // Events are diagnostics. A failed ingest must not change the decision, in
    // either direction.
    void this.client
      .ingestEvents({ events })
      .catch((error: unknown) =>
        this.logger.warn("Failed to ingest control execution events.", error),
      );
  }

  /**
   * Action resolution, ported from `_handle_evaluation_result` and
   * `integrations/_core._action_error`: evaluator errors block first, then
   * deny, then steer, then observe passes through.
   */
  private enforce(response: EvaluationResponse, stage: ControlStage): void {
    const result = toEvaluationResult(response);
    const matches = result.matches ?? [];

    if (result.errors && result.errors.length > 0) {
      const detail = result.errors
        .map((e) => `[${e.controlName}] ${e.error ?? e.message ?? "unknown error"}`)
        .join("; ");
      this.refuse(
        new ControlEvaluationError({
          reason: "evaluation_errors",
          stage,
          stepName: this.stepName,
          message:
            `Control evaluation reported errors for '${this.stepName}'; the call is refused ` +
            `because those controls did not produce a decision. Errors: ${detail}`,
        }),
      );
    }

    if (!result.isSafe) {
      const deny = matches.find((match) => String(match.action) === "deny");
      if (deny) {
        throw new ControlViolationError({
          controlName: deny.controlName,
          controlId: String(deny.controlId),
          action: "deny",
          evaluationResult: result,
          metadata: deny.metadata,
          stage,
          message:
            `Control violation [${deny.controlName}]: ` +
            `${deny.message ?? result.reason ?? "control triggered"}`,
        });
      }

      const steer = matches.find((match) => String(match.action) === "steer");
      if (steer) {
        const steering =
          steer.steeringContext ?? steer.message ?? result.reason ?? "No steering context provided";
        throw new ControlSteerError({
          controlName: steer.controlName,
          controlId: String(steer.controlId),
          steeringContext: steering,
          evaluationResult: result,
          metadata: steer.metadata,
          stage,
        });
      }

      // Unsafe with no actionable match: still a refusal, matching
      // `integrations/_core._evaluate_and_enforce`.
      const first = matches[0];
      throw new ControlViolationError({
        controlName: first?.controlName ?? "unknown",
        controlId: String(first?.controlId ?? "unknown"),
        action: "deny",
        evaluationResult: result,
        metadata: first?.metadata,
        stage,
        message:
          `Control violation [${first?.controlName ?? "unknown"}]: ` +
          `${result.reason ?? first?.message ?? "evaluation reported the step as unsafe"}`,
      });
    }

    // Only reachable on a safe verdict, so an unrecognised action here is the
    // dangerous case: a control matched and the SDK cannot tell what it wants.
    // Checked after deny/steer so that a real decision is reported as the
    // decision it is, rather than as an unreadable one.
    const uninterpretable = matches.filter(
      (match) => match.matched && normalizeAction(String(match.action)) === null,
    );
    if (uninterpretable.length > 0) {
      const detail = uninterpretable
        .map((match) => `${match.controlName}=${String(match.action)}`)
        .join(", ");
      this.refuse(
        new ControlEvaluationError({
          reason: "evaluation_errors",
          stage,
          stepName: this.stepName,
          message:
            `Control(s) matched with an unrecognized action (${detail}). The SDK will not guess ` +
            `what an unknown action means, so the call is refused.`,
        }),
      );
    }

    for (const match of matches) {
      if (match.matched && normalizeAction(String(match.action)) === "observe") {
        this.logger.info(
          `Control observe [${match.controlName}]: ${match.message ?? "control triggered"}`,
        );
      }
    }
  }
}

/**
 * Wrap an async function so that server-defined controls are evaluated before
 * it runs (`pre`) and after it returns (`post`).
 *
 * Throws `ControlViolationError` on `deny`, `ControlSteerError` on `steer`, and
 * `ControlEvaluationError` whenever a decision could not be established -
 * unless the session was initialized with `failOpen: true`.
 */
export function control<TArgs extends unknown[], TResult>(
  fn: AsyncFn<TArgs, TResult>,
  options?: ControlOptions,
): AsyncFn<TArgs, TResult> {
  const stepName = options?.stepName || fn.name || "anonymous";
  const stepType = options?.stepType ?? "llm";

  return async (...args: TArgs): Promise<TResult> => {
    const client = options?.client ?? defaultClient;
    const session = client.session;

    if (!session) {
      // No session means no failOpen setting to honour: opting into fail-open
      // requires calling init(), so an uninitialized SDK always refuses.
      throw new ControlEvaluationError({
        reason: "not_initialized",
        stepName,
        message:
          `agentControl.init(...) has not been called, so '${stepName}' cannot be evaluated ` +
          `against any control. Refusing the call rather than running it unprotected.`,
      });
    }

    const logger = session.logger ?? consoleLogger;
    await session.ready();

    // Re-resolved before each stage rather than captured once. The staleness
    // bound is a claim about when the control set was last known good, and the
    // post stage decides whether to hand back output at a later wall-clock time
    // than the pre stage did.
    const resolveSnapshot = (): ControlPlaneSnapshot | null => {
      try {
        return session.requireSnapshot(stepName);
      } catch (error) {
        if (!session.options.failOpen || !(error instanceof ControlEvaluationError)) {
          throw error;
        }
        logger.warn(
          `failOpen: allowing '${stepName}' with no usable control set. ${error.message}`,
          error,
        );
        return null;
      }
    };

    const runner = new StageRunner(
      client,
      session,
      resolveSnapshot,
      logger,
      stepName,
      stepType,
      generateTraceId(),
      generateSpanId(),
      options?.context,
    );

    const input = options?.getInput ? options.getInput(args) : defaultInput(args, stepType);

    await runner.run("pre", input, null);

    const result = await fn(...args);

    const output = options?.getOutput ? options.getOutput(result) : defaultOutput(result);
    await runner.run("post", input, output);

    return result;
  };
}
