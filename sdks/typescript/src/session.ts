/**
 * Session state for a control-plane connection: agent registration, the
 * control cache and its staleness bound, and the background refresh.
 *
 * This is the TypeScript analogue of the Python SDK's module-level `state`
 * plus `init()` / `refresh_controls_async()` (sdks/python/src/agent_control/__init__.py),
 * with one deliberate divergence, called out again where it is enforced:
 *
 *   Python's `init()` swallows connection failures, leaves
 *   `state.server_controls` as None, and `evaluate_controls` then resolves
 *   `state.server_controls or []` - an empty control set that makes every
 *   subsequent call return `is_safe=True` without contacting the server. That
 *   is a fail-open guardrail. Here, "no cache" and "stale cache" are distinct
 *   from "a fetched control set that happens to be empty", and only the last
 *   one can allow a call.
 */
import type { AgentControlSDK } from "./generated/sdk/sdk";
import { agentsInit } from "./generated/funcs/agents-init";
import { agentsListControls } from "./generated/funcs/agents-list-controls";
import type { Control } from "./generated/models/control";
import type { StepSchema as GeneratedStepSchema } from "./generated/models/step-schema";
import { ControlEvaluationError } from "./errors";
import { consoleLogger, type ControlLogger } from "./logging";
import { settle } from "./transport";

/** Default staleness bound for the cached control set: 5 minutes. */
export const DEFAULT_CONTROL_CACHE_MAX_AGE_MS = 5 * 60 * 1000;
/** Default background refresh interval: 60 seconds. */
export const DEFAULT_CONTROL_REFRESH_INTERVAL_MS = 60 * 1000;

export interface StepSchema {
  name: string;
  /** Step type; defaults to `"llm"` when omitted. */
  type?: string;
  /** Input JSON schema. `schema` is accepted as an alias for backwards compatibility. */
  schema?: Record<string, unknown>;
  inputSchema?: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
  description?: string;
}

export interface ControlSessionOptions {
  agentName: string;
  agentDescription?: string;
  agentVersion?: string;
  agentMetadata?: Record<string, unknown>;
  steps?: StepSchema[];
  targetType?: string;
  targetId?: string;
  /**
   * Register the agent and fetch its control set on `init()`. Default `true`.
   * With `false` the SDK never holds a control set, so every `control()` call
   * refuses unless `failOpen` is also set.
   */
  register?: boolean;
  /**
   * Allow calls the SDK could not evaluate. Default `false` (fail closed).
   * This never relaxes an actual control decision - a `deny` still denies.
   */
  failOpen?: boolean;
  /** Maximum age of the cached control set before it stops being a valid basis for allowing calls. */
  controlCacheMaxAgeMs?: number;
  /** Background refresh interval; `0` disables the refresh loop. */
  controlRefreshIntervalMs?: number;
  /** Emit control-execution events to the server. Default `true`. */
  observability?: boolean;
  logger?: ControlLogger;
}

export interface ControlPlaneSnapshot {
  agentName: string;
  controls: Control[];
  fetchedAt: number;
  ageMs: number;
  targetType?: string;
  targetId?: string;
}

function toGeneratedStep(step: StepSchema): GeneratedStepSchema {
  return {
    name: step.name,
    type: step.type ?? "llm",
    description: step.description,
    inputSchema: step.inputSchema ?? step.schema,
    outputSchema: step.outputSchema,
  };
}

export class ControlSession {
  readonly options: Required<
    Pick<
      ControlSessionOptions,
      "agentName" | "register" | "failOpen" | "controlCacheMaxAgeMs" | "controlRefreshIntervalMs" | "observability"
    >
  > &
    ControlSessionOptions;

  readonly logger: ControlLogger;

  private readonly sdk: AgentControlSDK;
  private controls: Control[] | null = null;
  private fetchedAt: number | null = null;
  private registrationError: unknown = null;
  private readyPromise: Promise<void>;
  private refreshTimer: ReturnType<typeof setInterval> | null = null;
  private closed = false;

  constructor(sdk: AgentControlSDK, options: ControlSessionOptions) {
    this.sdk = sdk;
    this.logger = options.logger ?? consoleLogger;
    this.options = {
      ...options,
      register: options.register ?? true,
      failOpen: options.failOpen ?? false,
      controlCacheMaxAgeMs: options.controlCacheMaxAgeMs ?? DEFAULT_CONTROL_CACHE_MAX_AGE_MS,
      controlRefreshIntervalMs:
        options.controlRefreshIntervalMs ?? DEFAULT_CONTROL_REFRESH_INTERVAL_MS,
      observability: options.observability ?? true,
    };

    if (this.options.failOpen) {
      this.logger.warn(
        `failOpen is enabled for agent '${this.options.agentName}'. Calls that cannot be ` +
          `evaluated will be ALLOWED. Controls that do evaluate are still enforced.`,
      );
    }

    this.readyPromise = this.options.register
      ? this.register().then(() => this.startRefreshLoop())
      : Promise.resolve();
    // An unawaited init() must never surface as an unhandled rejection; the
    // failure is recorded and re-surfaced at the call site as a refusal.
    void this.readyPromise.catch(() => undefined);
  }

  /** Resolves once the initial registration attempt has finished, successfully or not. */
  ready(): Promise<void> {
    return this.readyPromise;
  }

  get lastRegistrationError(): unknown {
    return this.registrationError;
  }

  get cachedControls(): Control[] | null {
    return this.controls;
  }

  get cacheAgeMs(): number | null {
    return this.fetchedAt === null ? null : Date.now() - this.fetchedAt;
  }

  private async register(): Promise<void> {
    try {
      const response = await settle(agentsInit(this.sdk.agents, {
        agent: {
          agentName: this.options.agentName,
          agentDescription: this.options.agentDescription,
          agentVersion: this.options.agentVersion,
          agentMetadata: this.options.agentMetadata,
        },
        steps: (this.options.steps ?? []).map(toGeneratedStep),
        targetType: this.options.targetType,
        targetId: this.options.targetId,
      }));

      this.controls = response.controls ?? [];
      this.fetchedAt = Date.now();
      this.registrationError = null;
      this.logger.info(
        `Registered agent '${this.options.agentName}' with ${this.controls.length} control(s).`,
      );
    } catch (error) {
      this.registrationError = error;
      this.logger.error(
        `Failed to register agent '${this.options.agentName}' with the control plane. ` +
          `Controlled calls will be refused until registration succeeds` +
          `${this.options.failOpen ? " (failOpen is set, so they will be allowed instead)" : ""}.`,
        error,
      );
    }
  }

  private startRefreshLoop(): void {
    if (this.closed || this.refreshTimer !== null) {
      return;
    }
    const interval = this.options.controlRefreshIntervalMs;
    if (!interval || interval <= 0) {
      return;
    }

    // The loop runs even when the initial registration failed, so a control
    // plane that comes back reachable un-refuses the agent instead of leaving
    // it permanently blocked. With no cache yet the agent may not exist server
    // side, so retry registration rather than a controls fetch.
    this.refreshTimer = setInterval(() => {
      const tick = this.controls === null ? this.register() : this.refreshControls();
      void tick.catch(() => undefined);
    }, interval);

    const timer = this.refreshTimer as { unref?: () => void };
    timer.unref?.();
  }

  /**
   * Re-fetch the effective control set. On failure the previous cache is kept
   * (as in the Python SDK) - but it keeps ageing, so a control plane that stays
   * unreachable eventually trips the staleness bound and calls start refusing.
   */
  async refreshControls(): Promise<Control[] | null> {
    if (this.closed) {
      return this.controls;
    }
    try {
      const response = await settle(agentsListControls(this.sdk.agents, {
        agentName: this.options.agentName,
        targetType: this.options.targetType,
        targetId: this.options.targetId,
      }));
      this.controls = response.controls ?? [];
      this.fetchedAt = Date.now();
      this.registrationError = null;
      return this.controls;
    } catch (error) {
      this.logger.error(
        `Failed to refresh controls for '${this.options.agentName}'; keeping the previous ` +
          `cache of ${this.controls?.length ?? 0} control(s) until it goes stale.`,
        error,
      );
      throw error;
    }
  }

  /**
   * Return a control set that is a valid basis for a decision, or throw.
   *
   * A cache that was never populated is not an empty control set, and a cache
   * older than the staleness bound is not evidence of anything. Both refuse.
   */
  requireSnapshot(stepName: string): ControlPlaneSnapshot {
    if (!this.options.register) {
      throw new ControlEvaluationError({
        reason: "registration_skipped",
        stepName,
        message:
          `Agent '${this.options.agentName}' was initialized with register: false, so no control ` +
          `set was ever fetched. Refusing the call. Re-init with registration enabled, or set ` +
          `failOpen: true to accept unevaluated calls.`,
      });
    }

    if (this.controls === null || this.fetchedAt === null) {
      if (this.registrationError !== null) {
        throw new ControlEvaluationError({
          reason: "registration_failed",
          stepName,
          cause: this.registrationError,
          message:
            `Agent '${this.options.agentName}' could not register with the control plane, so no ` +
            `controls are known. Refusing the call rather than running it unprotected.`,
        });
      }
      throw new ControlEvaluationError({
        reason: "cache_missing",
        stepName,
        message:
          `No control set has been fetched for agent '${this.options.agentName}'. Refusing the ` +
          `call. Await agentControl.ready() after init() before invoking controlled functions.`,
      });
    }

    const ageMs = Date.now() - this.fetchedAt;
    if (ageMs > this.options.controlCacheMaxAgeMs) {
      throw new ControlEvaluationError({
        reason: "cache_stale",
        stepName,
        message:
          `The cached control set for agent '${this.options.agentName}' is ${Math.round(ageMs / 1000)}s ` +
          `old, past the ${Math.round(this.options.controlCacheMaxAgeMs / 1000)}s staleness bound. ` +
          `A stale control set is not a valid basis for allowing a call, so the call is refused.`,
      });
    }

    return {
      agentName: this.options.agentName,
      controls: this.controls,
      fetchedAt: this.fetchedAt,
      ageMs,
      targetType: this.options.targetType,
      targetId: this.options.targetId,
    };
  }

  /** Stop the refresh loop and drop cached controls. */
  shutdown(): void {
    this.closed = true;
    if (this.refreshTimer !== null) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    this.controls = null;
    this.fetchedAt = null;
  }
}
