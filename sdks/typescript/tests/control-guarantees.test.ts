/**
 * The guarantees a guardrail SDK lives or dies by.
 *
 * `control-contract.test.ts` proves each refusal reason fires. This file proves
 * the surrounding properties: that `failOpen` is the *only* thing that turns a
 * refusal into a pass, that it never softens a real decision, that the post
 * stage actually sees the function's output, that an observe is recorded rather
 * than merely tolerated, and that nothing thrown at the caller carries the API
 * key, the server URL, or an upstream response body.
 *
 * Every refusal case asserts on a side-effect flag rather than a return value.
 * A wrapped function that "did not run" is the only evidence that fail-closed
 * is real.
 */
import { afterEach, describe, expect, it } from "vitest";

import type { AgentControlClient } from "../src/client";
import { control } from "../src/control";
import {
  ControlEvaluationError,
  ControlSteerError,
  ControlViolationError,
  type ControlEvaluationFailureReason,
} from "../src/errors";
import { silentLogger } from "../src/logging";
import { ControlSession } from "../src/session";
import {
  denyControl,
  evaluationResponse,
  initResponse,
  match,
  TestContext,
  capture,
  type Harness,
} from "./support/control-harness";

const ctx = new TestContext();
afterEach(() => ctx.cleanup());

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

interface Tracker {
  ran: boolean;
  call: (value?: string) => Promise<string>;
}

/** A wrapped function whose only job is to record whether it was allowed to run. */
function tracked(client: AgentControlClient, stepName = "chat"): Tracker {
  const tracker: Tracker = {
    ran: false,
    call: async () => "unused",
  };
  const wrapped = control(
    async (value: string) => {
      tracker.ran = true;
      return `ran:${value}`;
    },
    { client, stepName },
  );
  tracker.call = (value = "hello") => wrapped(value);
  return tracker;
}

interface RefusalScenario {
  name: string;
  reason: ControlEvaluationFailureReason;
  /** Builds a client in the failing state, with fail-open on or off. */
  setup: (failOpen: boolean) => Promise<AgentControlClient>;
}

const okInit = (controls = [denyControl]) => ({
  "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse(controls) }),
});

const scenarios: RefusalScenario[] = [
  {
    name: "control plane unreachable at init",
    reason: "registration_failed",
    // Port 1 on loopback is the harness default: nothing is listening there.
    setup: async (failOpen) => {
      const client = ctx.makeClient({ failOpen });
      await client.ready();
      return client;
    },
  },
  {
    name: "registration skipped",
    reason: "registration_skipped",
    setup: async (failOpen) => {
      const harness = await ctx.withServer(okInit());
      const client = ctx.makeClient({ serverUrl: harness.url, register: false, failOpen });
      await client.ready();
      return client;
    },
  },
  {
    name: "cached control set past the staleness bound",
    reason: "cache_stale",
    setup: async (failOpen) => {
      const harness = await ctx.withServer(okInit());
      const client = ctx.makeClient({
        serverUrl: harness.url,
        controlCacheMaxAgeMs: 20,
        failOpen,
      });
      await client.ready();
      await sleep(45);
      return client;
    },
  },
  {
    name: "evaluation rejected by the server",
    reason: "evaluation_request_failed",
    setup: async (failOpen) => {
      const harness = await ctx.withServer({
        ...okInit(),
        "/api/v1/evaluation": () => ({ status: 503, json: { detail: "upstream down" } }),
      });
      const client = ctx.makeClient({ serverUrl: harness.url, failOpen });
      await client.ready();
      return client;
    },
  },
  {
    name: "evaluation answered with something unparseable",
    reason: "evaluation_request_failed",
    setup: async (failOpen) => {
      // A captive proxy or a misrouted ingress answering 200 with HTML is the
      // realistic shape of this: a success status carrying no decision.
      const harness = await ctx.withServer({
        ...okInit(),
        "/api/v1/evaluation": () => ({ status: 200, raw: "<html>gateway</html>" }),
      });
      const client = ctx.makeClient({ serverUrl: harness.url, failOpen });
      await client.ready();
      return client;
    },
  },
  {
    name: "applicable control requires local execution",
    reason: "sdk_execution_unsupported",
    setup: async (failOpen) => {
      const harness = await ctx.withServer(okInit([{ ...denyControl, execution: "sdk" }]));
      const client = ctx.makeClient({ serverUrl: harness.url, failOpen });
      await client.ready();
      return client;
    },
  },
  {
    name: "applicable control has an uninterpretable scope",
    reason: "control_unreadable",
    setup: async (failOpen) => {
      const harness = await ctx.withServer(
        okInit([{ ...denyControl, scope: { step_name_regex: "([a-z" } }]),
      );
      const client = ctx.makeClient({ serverUrl: harness.url, failOpen });
      await client.ready();
      return client;
    },
  },
  {
    name: "evaluation reported an evaluator error",
    reason: "evaluation_errors",
    setup: async (failOpen) => {
      const harness = await ctx.withServer({
        ...okInit(),
        "/api/v1/evaluation": () => ({
          status: 200,
          json: evaluationResponse({
            isSafe: true,
            errors: [
              match({ id: 1, name: "block-ssn", action: "deny", error: "evaluator timeout" }),
            ],
          }),
        }),
        "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 0 } }),
      });
      const client = ctx.makeClient({ serverUrl: harness.url, failOpen });
      await client.ready();
      return client;
    },
  },
];

describe.each(scenarios)("$name", (scenario) => {
  it(`refuses the call by default, with reason ${scenario.reason}`, async () => {
    const client = await scenario.setup(false);
    const fn = tracked(client);

    const error = await capture(fn.call());

    expect(error).toBeInstanceOf(ControlEvaluationError);
    expect((error as ControlEvaluationError).reason).toBe(scenario.reason);
    expect(fn.ran).toBe(false);
  });

  it("passes only because failOpen was explicitly opted into", async () => {
    const client = await scenario.setup(true);
    const fn = tracked(client);

    await expect(fn.call()).resolves.toBe("ran:hello");
    expect(fn.ran).toBe(true);
  });
});

describe("failOpen never softens an actual control decision", () => {
  async function denyingServer(): Promise<Harness> {
    return ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          reason: "SSN detected",
          matches: [match({ id: 1, name: "block-ssn", action: "deny", message: "SSN in input" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });
  }

  it("still denies with failOpen: true", async () => {
    const harness = await denyingServer();
    const client = ctx.makeClient({ serverUrl: harness.url, failOpen: true });
    await client.ready();
    const fn = tracked(client);

    const error = await capture(fn.call());

    expect(error).toBeInstanceOf(ControlViolationError);
    expect(fn.ran).toBe(false);
  });

  it("still steers with failOpen: true", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          matches: [
            match({
              id: 1,
              name: "tone",
              action: "steer",
              steeringContext: "Ask for consent first",
            }),
          ],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url, failOpen: true });
    await client.ready();
    const fn = tracked(client);

    const error = await capture(fn.call());

    expect(error).toBeInstanceOf(ControlSteerError);
    expect((error as ControlSteerError).steeringContext).toBe("Ask for consent first");
    expect(fn.ran).toBe(false);
  });
});

describe("an uninitialized SDK cannot be talked into passing", () => {
  it("refuses on the package default client, which is what the README example uses", async () => {
    let ran = false;
    const wrapped = control(async (value: string) => {
      ran = true;
      return value;
    });

    const error = await capture(wrapped("hello"));

    expect(error).toBeInstanceOf(ControlEvaluationError);
    expect((error as ControlEvaluationError).reason).toBe("not_initialized");
    expect(ran).toBe(false);
  });

  it("has no failOpen to consult before init(), so there is no opt-out", async () => {
    // failOpen lives on the session, and the session is created by init(). This
    // is the one refusal that cannot be configured away.
    const client = ctx.makeClient({ failOpen: true });
    client.shutdown();
    const fn = tracked(client);

    const error = await capture(fn.call());

    expect((error as ControlEvaluationError).reason).toBe("not_initialized");
    expect(fn.ran).toBe(false);
  });
});

describe("a control cache that was never populated", () => {
  /** A control plane that accepts the connection and then never answers. */
  const hangingSdk = {
    agents: {
      init: () => new Promise<never>(() => undefined),
      listControls: () => new Promise<never>(() => undefined),
    },
  };

  it("refuses with cache_missing rather than behaving like an empty control set", () => {
    // Reached directly: `control()` awaits registration, so an in-flight fetch
    // makes the call wait rather than proceed. What must never happen is the
    // Python defect - "no cache" collapsing into "no controls apply".
    const session = new ControlSession(hangingSdk as never, {
      agentName: "pending-agent",
      logger: silentLogger,
      controlRefreshIntervalMs: 0,
    });

    expect(() => session.requireSnapshot("chat")).toThrowError(ControlEvaluationError);
    try {
      session.requireSnapshot("chat");
    } catch (error) {
      expect((error as ControlEvaluationError).reason).toBe("cache_missing");
    }
    expect(session.cachedControls).toBeNull();
    expect(session.cacheAgeMs).toBeNull();
    session.shutdown();
  });

  it("refuses again after shutdown drops a cache that had been populated", async () => {
    const harness = await ctx.withServer(okInit());
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();

    const session = client.session;
    expect(session?.cachedControls).toHaveLength(1);
    session?.shutdown();

    expect(() => session?.requireSnapshot("chat")).toThrowError(/No control set has been fetched/);
  });
});

describe("an evaluation that returns nothing", () => {
  it("refuses with evaluation_empty", async () => {
    const harness = await ctx.withServer(okInit());
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();

    // Defensive branch: `evaluate()` is typed as always resolving to a
    // response, so this state is only reachable by injection. It is still worth
    // pinning - "no decision" must not read as "safe".
    client.evaluate = async () => undefined as never;

    const fn = tracked(client);
    const error = await capture(fn.call());

    expect((error as ControlEvaluationError).reason).toBe("evaluation_empty");
    expect(fn.ran).toBe(false);
  });
});

describe("the staleness bound is checked at each stage", () => {
  it("withholds the output when the cache expires while the function is running", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({ status: 200, json: evaluationResponse({ isSafe: true }) }),
    });
    const client = ctx.makeClient({
      serverUrl: harness.url,
      controlCacheMaxAgeMs: 40,
      observability: false,
    });
    await client.ready();

    let ran = false;
    const wrapped = control(
      async () => {
        ran = true;
        await sleep(90);
        return "the answer";
      },
      { client, stepName: "chat" },
    );

    const error = await capture(wrapped());

    // The pre stage passed on a fresh cache, so the function did run. What must
    // not happen is its output being returned on the strength of a control set
    // that expired mid-call.
    expect(ran).toBe(true);
    expect(error).toBeInstanceOf(ControlEvaluationError);
    expect((error as ControlEvaluationError).reason).toBe("cache_stale");

    const stages = harness.requests
      .filter((request) => request.path === "/api/v1/evaluation")
      .map((request) => (request.body as { stage: string }).stage);
    expect(stages).toEqual(["pre"]);
  });
});

describe("a refusal must not take the host process down with it", () => {
  /**
   * Regression test for the generated `APIPromise` defect contained in
   * src/transport.ts. A response that declares JSON and delivers a truncated
   * body makes the request pipeline throw; before the containment that left a
   * promise rejecting with no handler, and Node's default policy for an
   * unhandled rejection is to terminate the process. Verified separately
   * against a built dist in a real node process, where it exited 1.
   */
  it("refuses a truncated response body without a dangling rejection", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({ status: 200, raw: '{"is_safe": tru' }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();
    const fn = tracked(client);

    const dangling: unknown[] = [];
    const listener = (reason: unknown) => dangling.push(reason);
    process.on("unhandledRejection", listener);
    try {
      const error = await capture(fn.call());
      expect((error as ControlEvaluationError).reason).toBe("evaluation_request_failed");
      expect(fn.ran).toBe(false);
      await sleep(50);
      expect(dangling).toEqual([]);
    } finally {
      process.off("unhandledRejection", listener);
    }
  });

  it("refuses an empty response body without a dangling rejection", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({ status: 200, raw: "" }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();
    const fn = tracked(client);

    const dangling: unknown[] = [];
    const listener = (reason: unknown) => dangling.push(reason);
    process.on("unhandledRejection", listener);
    try {
      const error = await capture(fn.call());
      expect((error as ControlEvaluationError).reason).toBe("evaluation_request_failed");
      expect(fn.ran).toBe(false);
      await sleep(50);
      expect(dangling).toEqual([]);
    } finally {
      process.off("unhandledRejection", listener);
    }
  });
});

describe("post-stage evaluation", () => {
  it("sends the function's actual output, and the pre stage sends none", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({ status: 200, json: evaluationResponse({ isSafe: true }) }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url, observability: false });
    await client.ready();

    const wrapped = control(async (prompt: string) => ({ reply: `answer to ${prompt}` }), {
      client,
      stepName: "chat",
    });

    await expect(wrapped("what is my ssn")).resolves.toEqual({
      reply: "answer to what is my ssn",
    });

    const evaluations = harness.requests
      .filter((request) => request.path === "/api/v1/evaluation")
      .map((request) => request.body as { stage: string; step: { input: unknown; output: unknown } });

    expect(evaluations.map((body) => body.stage)).toEqual(["pre", "post"]);
    expect(evaluations[0]?.step.input).toBe("what is my ssn");
    expect(evaluations[0]?.step.output).toBeNull();
    expect(evaluations[1]?.step.input).toBe("what is my ssn");
    expect(evaluations[1]?.step.output).toEqual({ reply: "answer to what is my ssn" });
  });

  it("never reaches the post stage when the pre stage denied", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          matches: [match({ id: 1, name: "block-ssn", action: "deny", message: "SSN in input" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();
    const fn = tracked(client);

    await capture(fn.call());

    const stages = harness.requests
      .filter((request) => request.path === "/api/v1/evaluation")
      .map((request) => (request.body as { stage: string }).stage);
    expect(stages).toEqual(["pre"]);
    expect(fn.ran).toBe(false);
  });
});

describe("observe", () => {
  it("records a control execution event and lets the call through", async () => {
    const harness = await ctx.withServer({
      ...okInit([{ ...denyControl, scope: { stages: ["pre"] } }]),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: true,
          matches: [match({ id: 1, name: "pii-watch", action: "observe", message: "noted" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(async (value: string) => `echo:${value}`, {
      client,
      stepName: "chat",
    });
    await expect(wrapped("hi")).resolves.toBe("echo:hi");

    // Ingestion is fire-and-forget by design, so give it a turn to land.
    await sleep(50);

    const ingests = harness.requests.filter(
      (request) => request.path === "/api/v1/observability/events",
    );
    expect(ingests).toHaveLength(1);
    const events = (ingests[0]?.body as { events: Record<string, unknown>[] }).events;
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      action: "observe",
      agent_name: "contract-agent",
      applies_to: "llm_call",
      check_stage: "pre",
      control_id: 1,
      control_name: "pii-watch",
      matched: true,
    });
    expect(events[0]?.["trace_id"]).toMatch(/^[0-9a-f]{32}$/);
    expect(events[0]?.["span_id"]).toMatch(/^[0-9a-f]{16}$/);
  });

  it("keeps enforcing when event ingestion itself fails", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          matches: [match({ id: 1, name: "block-ssn", action: "deny", message: "SSN in input" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 500, json: { detail: "sink down" } }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url });
    await client.ready();
    const fn = tracked(client);

    const error = await capture(fn.call());

    expect(error).toBeInstanceOf(ControlViolationError);
    expect(fn.ran).toBe(false);
    await sleep(50);
  });
});

describe("inputs that JSON cannot represent", () => {
  it("still gets evaluated instead of crashing the wrapper", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({ status: 200, json: evaluationResponse({ isSafe: true }) }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url, observability: false });
    await client.ready();

    const payload: Record<string, unknown> = { marker: "visible", tokens: 12n };
    payload["self"] = payload;

    const wrapped = control(
      async (arg: Record<string, unknown>) => (arg["marker"] === "visible" ? "ok" : "unexpected"),
      { client, stepName: "chat" },
    );

    await expect(wrapped(payload)).resolves.toBe("ok");

    const evaluations = harness.requests.filter((r) => r.path === "/api/v1/evaluation");
    expect(evaluations).toHaveLength(2);
    const sent = (evaluations[0]?.body as { step: { input: string } }).step.input;
    // The content survives; only the back-reference degrades.
    expect(sent).toContain('"marker":"visible"');
    expect(sent).toContain('"tokens":"12"');
    expect(sent).toContain("[Circular]");
  });

  it("refuses rather than passes when the output cannot be sent for evaluation", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": (body) => {
        const stage = (body as { stage: string }).stage;
        return { status: 200, json: evaluationResponse({ isSafe: stage === "pre" }) };
      },
    });
    const client = ctx.makeClient({ serverUrl: harness.url, observability: false });
    await client.ready();

    const circular: Record<string, unknown> = {};
    circular["self"] = circular;
    const wrapped = control(async () => circular, { client, stepName: "chat" });

    const error = await capture(wrapped());

    // The post stage could not be performed, so the output is withheld.
    expect(error).toBeInstanceOf(ControlEvaluationError);
    expect((error as ControlEvaluationError).reason).toBe("evaluation_request_failed");
    expect((error as ControlEvaluationError).stage).toBe("post");
  });
});

describe("nothing thrown at the caller leaks a secret or an endpoint", () => {
  const API_KEY = "sk-live-do-not-log-me";
  const SECRET_BODY = "postgres://admin:hunter2@db.internal:5432";

  /**
   * Walk the error's own enumerable properties looking for a needle.
   *
   * `cause` is skipped on purpose: it holds the original transport error and is
   * documented as carrying upstream detail for debugging. Everything the SDK
   * itself composes - the message the caller sees and prints - must be clean.
   */
  function findLeaks(error: unknown, needles: string[]): string[] {
    const hits: string[] = [];
    const seen = new WeakSet<object>();

    const visit = (value: unknown): void => {
      if (typeof value === "string") {
        for (const needle of needles) {
          if (value.includes(needle)) {
            hits.push(`${needle} in ${value}`);
          }
        }
        return;
      }
      if (typeof value !== "object" || value === null || seen.has(value)) {
        return;
      }
      seen.add(value);
      if (value instanceof Error) {
        visit(value.message);
        visit(value.name);
      }
      for (const [key, nested] of Object.entries(value)) {
        if (key === "cause") {
          continue;
        }
        visit(nested);
      }
    };

    visit(error);
    return hits;
  }

  const needlesFor = (harness: Harness): string[] => [
    API_KEY,
    SECRET_BODY,
    "hunter2",
    harness.url,
    harness.url.split("//")[1] ?? "",
  ];

  it("keeps them out of a deny, the error users see most often", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          reason: "SSN detected",
          matches: [match({ id: 1, name: "block-ssn", action: "deny", message: "SSN in input" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url, apiKey: API_KEY });
    await client.ready();

    const error = await capture(tracked(client).call());

    expect(error).toBeInstanceOf(ControlViolationError);
    expect(findLeaks(error, needlesFor(harness))).toEqual([]);
  });

  it("keeps them out of a transport refusal whose upstream body is a connection string", async () => {
    const harness = await ctx.withServer({
      ...okInit(),
      "/api/v1/evaluation": () => ({
        status: 500,
        json: { detail: `internal: ${SECRET_BODY}` },
      }),
    });
    const client = ctx.makeClient({ serverUrl: harness.url, apiKey: API_KEY });
    await client.ready();

    const error = await capture(tracked(client).call());

    expect(error).toBeInstanceOf(ControlEvaluationError);
    expect((error as ControlEvaluationError).reason).toBe("evaluation_request_failed");
    expect(findLeaks(error, needlesFor(harness))).toEqual([]);
    // The detail is still reachable for debugging, just not in what is printed.
    expect(String((error as Error).cause)).toContain("hunter2");
  });

  it("keeps them out of a registration failure against a dead port", async () => {
    const client = ctx.makeClient({ apiKey: API_KEY });
    await client.ready();

    const error = await capture(tracked(client).call());

    expect(findLeaks(error, [API_KEY, "127.0.0.1:1"])).toEqual([]);
  });
});
