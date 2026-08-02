import { AgentControlSDK } from "./generated/sdk/sdk";
import { evaluationEvaluate } from "./generated/funcs/evaluation-evaluate";
import { observabilityIngestEvents } from "./generated/funcs/observability-ingest-events";
import type { Logger } from "./generated/lib/logger";
import type { BatchEventsRequest } from "./generated/models/batch-events-request";
import type { BatchEventsResponse } from "./generated/models/batch-events-response";
import type { Control } from "./generated/models/control";
import type { EvaluationRequest } from "./generated/models/evaluation-request";
import type { EvaluationResponse } from "./generated/models/evaluation-response";
import { ControlSession, type ControlSessionOptions, type StepSchema } from "./session";
import type { ControlLogger } from "./logging";
import { settle } from "./transport";

export type { StepSchema } from "./session";

export type APIKeyProvider = string | (() => Promise<string>);

export interface AgentControlInitOptions
  extends Omit<ControlSessionOptions, "agentName" | "steps"> {
  agentName: string;
  agentId?: string;
  serverUrl: string;
  apiKey?: APIKeyProvider;
  steps?: StepSchema[];
  timeoutMs?: number;
  userAgent?: string;
  /** Transport-level debug logger passed to the generated client. */
  debugLogger?: Logger;
  /** SDK diagnostics logger (registration, refusals, refresh failures). */
  logger?: ControlLogger;
}

export type AgentsApi = AgentControlSDK["agents"];
export type AuthApi = AgentControlSDK["auth"];
export type ControlBindingsApi = AgentControlSDK["controlBindings"];
export type ControlsApi = AgentControlSDK["controls"];
export type EvaluationApi = AgentControlSDK["evaluation"];
export type EvaluatorsApi = AgentControlSDK["evaluators"];
export type ObservabilityApi = AgentControlSDK["observability"];
export type PoliciesApi = AgentControlSDK["policies"];
export type SystemApi = AgentControlSDK["system"];

export class AgentControlClient {
  private options: AgentControlInitOptions | null = null;
  private sdk: AgentControlSDK | null = null;
  private controlSession: ControlSession | null = null;

  /**
   * Configure the client and start agent registration.
   *
   * The transport is ready synchronously, so direct API access works
   * immediately. Registration and the initial control fetch run in the
   * background; `control()` awaits them internally, and `ready()` exposes them
   * to callers. A registration failure is recorded rather than thrown - it
   * surfaces at the call site as a refusal, not as a silent pass.
   */
  init(options: AgentControlInitOptions): void {
    this.controlSession?.shutdown();
    this.options = { ...options };
    this.sdk = new AgentControlSDK({
      serverURL: options.serverUrl,
      apiKeyHeader: options.apiKey,
      timeoutMs: options.timeoutMs,
      userAgent: options.userAgent,
      debugLogger: options.debugLogger,
    });
    this.controlSession = new ControlSession(this.sdk, options);
  }

  /** Resolves once the initial registration attempt has settled. */
  async ready(): Promise<void> {
    await this.controlSession?.ready();
  }

  /** Re-fetch the effective control set now. Rejects if the fetch fails. */
  async refreshControls(): Promise<Control[] | null> {
    if (!this.controlSession) {
      return null;
    }
    return this.controlSession.refreshControls();
  }

  /**
   * Evaluate one step against the control plane.
   *
   * `control()` goes through here rather than through `client.evaluation` so
   * every hot-path request gets the `settle()` containment described in
   * transport.ts. Behaviour is otherwise identical to `evaluation.evaluate`.
   */
  async evaluate(request: EvaluationRequest): Promise<EvaluationResponse> {
    return settle(evaluationEvaluate(this.evaluation, request));
  }

  /** Ingest control-execution events. Diagnostics only; never changes a decision. */
  async ingestEvents(request: BatchEventsRequest): Promise<BatchEventsResponse> {
    return settle(observabilityIngestEvents(this.observability, request));
  }

  /** Stop the background refresh loop and drop the cached control set. */
  shutdown(): void {
    this.controlSession?.shutdown();
    this.controlSession = null;
  }

  get initialized(): boolean {
    return this.sdk !== null;
  }

  get config(): AgentControlInitOptions | null {
    return this.options;
  }

  /** The active control session, or `null` before `init()`. */
  get session(): ControlSession | null {
    return this.controlSession;
  }

  get agents(): AgentsApi {
    return this.requireSDK().agents;
  }

  get auth(): AuthApi {
    return this.requireSDK().auth;
  }

  get controlBindings(): ControlBindingsApi {
    return this.requireSDK().controlBindings;
  }

  get controls(): ControlsApi {
    return this.requireSDK().controls;
  }

  get evaluation(): EvaluationApi {
    return this.requireSDK().evaluation;
  }

  get evaluators(): EvaluatorsApi {
    return this.requireSDK().evaluators;
  }

  get observability(): ObservabilityApi {
    return this.requireSDK().observability;
  }

  get policies(): PoliciesApi {
    return this.requireSDK().policies;
  }

  get system(): SystemApi {
    return this.requireSDK().system;
  }

  private requireSDK(): AgentControlSDK {
    if (!this.sdk) {
      throw new Error(
        "AgentControlClient is not initialized. Call init(...) before making API calls.",
      );
    }

    return this.sdk;
  }
}

/** Process-wide default client, exported as the package default. */
export const defaultClient = new AgentControlClient();
