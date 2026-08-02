/**
 * Shared fixtures for the `control()` contract tests.
 *
 * The tests drive a real HTTP server over a real loopback socket rather than
 * stubbing `fetch`. The property under test is what the SDK does when the
 * control plane is genuinely unreachable, genuinely slow to be believed, or
 * genuinely says "deny", and a stubbed transport cannot tell you that.
 */
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import { AgentControlClient, type AgentControlInitOptions } from "../../src/client";
import { silentLogger } from "../../src/logging";

export interface ControlFixture {
  id: number;
  name: string;
  decision: "deny" | "steer" | "observe";
  execution?: "server" | "sdk";
  enabled?: boolean;
  scope?: Record<string, unknown>;
}

export function controlPayload(fixture: ControlFixture): Record<string, unknown> {
  return {
    id: fixture.id,
    name: fixture.name,
    control: {
      action: { decision: fixture.decision, steering_context: null },
      condition: {
        selector: { path: "input" },
        evaluator: { name: "regex", config: { pattern: "\\d{3}-\\d{2}-\\d{4}" } },
      },
      description: null,
      enabled: fixture.enabled ?? true,
      execution: fixture.execution ?? "server",
      // The generated inbound schemas treat these keys as required-but-nullable,
      // so a realistic payload has to carry them.
      scope: {
        stages: null,
        step_name_regex: null,
        step_names: null,
        step_types: null,
        ...(fixture.scope ?? {}),
      },
      tags: [],
      template: null,
      template_values: null,
    },
  };
}

export const denyControl: ControlFixture = { id: 1, name: "block-ssn", decision: "deny" };

export interface RecordedRequest {
  path: string;
  body: unknown;
}

export interface Harness {
  url: string;
  requests: RecordedRequest[];
  close: () => Promise<void>;
}

export type Route = (body: unknown) => { status: number; json: unknown } | { status: number; raw: string };

export async function startServer(routes: Record<string, Route>): Promise<Harness> {
  const requests: RecordedRequest[] = [];

  const server: Server = createServer((req: IncomingMessage, res: ServerResponse) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8");
      const body: unknown = raw ? JSON.parse(raw) : null;
      const path = (req.url ?? "").split("?")[0] ?? "";
      requests.push({ path, body });

      const route = routes[path];
      if (!route) {
        res.writeHead(404, { "content-type": "application/json" });
        res.end(JSON.stringify({ detail: "no route" }));
        return;
      }

      const outcome = route(body);
      res.writeHead(outcome.status, { "content-type": "application/json" });
      res.end("raw" in outcome ? outcome.raw : JSON.stringify(outcome.json));
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;

  return {
    url: `http://127.0.0.1:${port}`,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
      }),
  };
}

export function initResponse(controls: ControlFixture[]): Record<string, unknown> {
  return {
    created: true,
    overwrite_applied: false,
    overwrite_changes: null,
    controls: controls.map(controlPayload),
  };
}

export function evaluationResponse(params: {
  isSafe: boolean;
  matches?: unknown[];
  errors?: unknown[];
  reason?: string;
}): Record<string, unknown> {
  return {
    is_safe: params.isSafe,
    confidence: 1,
    reason: params.reason ?? null,
    matches: params.matches ?? [],
    errors: params.errors ?? [],
    non_matches: [],
  };
}

export function match(params: {
  id: number;
  name: string;
  action: "deny" | "steer" | "observe";
  message?: string;
  steeringContext?: string;
  error?: string;
}): Record<string, unknown> {
  return {
    control_id: params.id,
    control_name: params.name,
    action: params.action,
    control_execution_id: `exec-${params.id}`,
    steering_context: params.steeringContext ? { message: params.steeringContext } : null,
    result: {
      matched: params.error === undefined,
      confidence: 0.99,
      message: params.message ?? null,
      error: params.error ?? null,
      metadata: { detector: "regex" },
    },
  };
}

/**
 * Per-file bookkeeping for the clients and servers a test opens. Call
 * `cleanup()` from `afterEach` so a refused test never leaks a refresh timer
 * or a listening socket into the next one.
 */
export class TestContext {
  private readonly clients: AgentControlClient[] = [];
  private readonly servers: Harness[] = [];

  async withServer(routes: Record<string, Route>): Promise<Harness> {
    const harness = await startServer(routes);
    this.servers.push(harness);
    return harness;
  }

  /**
   * A client pointed at 127.0.0.1:1 by default - a port nothing listens on, so
   * "unreachable control plane" is a real socket failure and not a mock.
   */
  makeClient(options: Partial<AgentControlInitOptions> = {}): AgentControlClient {
    const client = new AgentControlClient();
    client.init({
      agentName: "contract-agent",
      serverUrl: "http://127.0.0.1:1",
      logger: silentLogger,
      controlRefreshIntervalMs: 0,
      ...options,
    });
    this.clients.push(client);
    return client;
  }

  async cleanup(): Promise<void> {
    while (this.clients.length > 0) {
      this.clients.pop()?.shutdown();
    }
    while (this.servers.length > 0) {
      await this.servers.pop()?.close();
    }
  }
}

/** Await a rejection without letting a resolved promise masquerade as one. */
export async function capture(promise: Promise<unknown>): Promise<unknown> {
  return promise.then(
    (value) => {
      throw new Error(`expected a rejection, got a resolved value: ${JSON.stringify(value)}`);
    },
    (error: unknown) => error,
  );
}
