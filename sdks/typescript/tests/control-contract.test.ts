/**
 * End-to-end contract tests for `control()`.
 *
 * These drive a real HTTP server over a real socket rather than stubbing
 * `fetch`, because the property under test is what happens when the control
 * plane is genuinely unreachable or genuinely says "deny". Every case asserts
 * on a side effect that must be absent when the call is refused - a wrapped
 * function that "did not run" is the only thing that makes fail-closed real.
 */
import { afterEach, describe, expect, it } from "vitest";

import type { AgentControlInitOptions } from "../src/client";
import { control } from "../src/control";
import { ControlEvaluationError, ControlSteerError, ControlViolationError } from "../src/errors";
import {
  denyControl,
  evaluationResponse,
  initResponse,
  match,
  TestContext,
  type Route,
} from "./support/control-harness";

const ctx = new TestContext();
afterEach(() => ctx.cleanup());

const makeClient = (options: Partial<AgentControlInitOptions> = {}) => ctx.makeClient(options);
const withServer = (routes: Record<string, Route>) => ctx.withServer(routes);

describe("control() fail-closed contract", () => {
  it("refuses and does not run the function when the control plane is unreachable", async () => {
    let ran = false;
    // Port 1 on loopback: nothing is listening, so registration genuinely fails.
    const client = makeClient({});
    await client.ready();

    const wrapped = control(
      async (value: string) => {
        ran = true;
        return value;
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped("hello")).rejects.toMatchObject({
      name: "ControlEvaluationError",
      reason: "registration_failed",
    });
    expect(ran).toBe(false);
  });

  it("allows the call when failOpen is explicitly set, and only then", async () => {
    let ran = false;
    const client = makeClient({ failOpen: true });
    await client.ready();

    const wrapped = control(
      async (value: string) => {
        ran = true;
        return `echo:${value}`;
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped("hello")).resolves.toBe("echo:hello");
    expect(ran).toBe(true);
  });

  it("refuses when register: false, even though the server is reachable", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([]) }),
    });
    let ran = false;
    const client = makeClient({ serverUrl: harness.url, register: false });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "registration_skipped" });
    expect(ran).toBe(false);
    expect(harness.requests).toHaveLength(0);
  });

  it("refuses when the cached control set is older than the staleness bound", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
    });
    let ran = false;
    const client = makeClient({
      serverUrl: harness.url,
      controlCacheMaxAgeMs: 0,
    });
    await client.ready();
    await new Promise((resolve) => setTimeout(resolve, 5));

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "cache_stale" });
    expect(ran).toBe(false);
  });

  it("refuses when the evaluation request itself fails", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({ status: 503, json: { detail: "upstream down" } }),
    });
    let ran = false;
    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "evaluation_request_failed" });
    expect(ran).toBe(false);
  });

  it("refuses when the control plane goes away between init and the call", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({ isSafe: true }),
      }),
    });
    let ran = false;
    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    // Registration succeeded and the cache is fresh. Now the socket is gone.
    await harness.close();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "evaluation_request_failed" });
    expect(ran).toBe(false);
  });

  it("refuses when a matched control reports an evaluator error", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: true,
          errors: [match({ id: 1, name: "block-ssn", action: "deny", error: "evaluator timeout" })],
        }),
      }),
    });
    let ran = false;
    const client = makeClient({ serverUrl: harness.url, observability: false });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "evaluation_errors" });
    expect(ran).toBe(false);
  });

  it("refuses when an applicable control declares execution: sdk", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({
        status: 200,
        json: initResponse([{ ...denyControl, execution: "sdk" }]),
      }),
    });
    let ran = false;
    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "sdk_execution_unsupported" });
    expect(ran).toBe(false);
  });
});

describe("control() decision enforcement", () => {
  it("deny prevents the wrapped function from running at all", async () => {
    let ran = false;
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          reason: "SSN detected",
          matches: [
            match({ id: 1, name: "block-ssn", action: "deny", message: "SSN detected in input" }),
          ],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });

    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(
      async (value: string) => {
        ran = true;
        return `leaked:${value}`;
      },
      { client, stepName: "chat" },
    );

    const error = await wrapped("my ssn is 123-45-6789").then(
      () => null,
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(ControlViolationError);
    expect((error as ControlViolationError).controlName).toBe("block-ssn");
    expect((error as ControlViolationError).stage).toBe("pre");
    // The load-bearing assertion: the model call never happened.
    expect(ran).toBe(false);
    const evaluations = harness.requests.filter((r) => r.path === "/api/v1/evaluation");
    expect(evaluations).toHaveLength(1);
  });

  it("steer raises ControlSteerError carrying the steering context", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          matches: [
            match({
              id: 1,
              name: "tone",
              action: "steer",
              message: "too blunt",
              steeringContext: "Rephrase more politely",
            }),
          ],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });

    let ran = false;
    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    const error = await wrapped().then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ControlSteerError);
    expect((error as ControlSteerError).steeringContext).toBe("Rephrase more politely");
    expect(ran).toBe(false);
  });

  it("observe records and passes the call through", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: true,
          matches: [match({ id: 1, name: "pii-watch", action: "observe", message: "noted" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });

    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(async (value: string) => `echo:${value}`, {
      client,
      stepName: "chat",
    });

    await expect(wrapped("hi")).resolves.toBe("echo:hi");
  });

  it("evaluates the post stage and withholds the output on a post-stage deny", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({
        status: 200,
        json: initResponse([{ ...denyControl, scope: { stages: ["post"] } }]),
      }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          matches: [match({ id: 1, name: "block-ssn", action: "deny", message: "SSN in output" })],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });

    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(async () => "123-45-6789", { client, stepName: "chat" });

    const error = await wrapped().then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ControlViolationError);
    expect((error as ControlViolationError).stage).toBe("post");

    const evaluations = harness.requests.filter((r) => r.path === "/api/v1/evaluation");
    expect(evaluations).toHaveLength(1);
    expect((evaluations[0]?.body as { stage: string }).stage).toBe("post");
  });

  it("reports a deny as a violation even when a sibling match has an unknown action", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: false,
          matches: [
            {
              ...match({ id: 2, name: "future-action", action: "observe" }),
              action: "quarantine",
            },
            match({ id: 1, name: "block-ssn", action: "deny", message: "SSN detected" }),
          ],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });

    let ran = false;
    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    const error = await wrapped().then(
      () => null,
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ControlViolationError);
    expect((error as ControlViolationError).controlName).toBe("block-ssn");
    expect(ran).toBe(false);
  });

  it("refuses a safe verdict whose matched control carries an action it cannot read", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({
        status: 200,
        json: evaluationResponse({
          isSafe: true,
          matches: [
            {
              ...match({ id: 2, name: "future-action", action: "observe" }),
              action: "quarantine",
            },
          ],
        }),
      }),
      "/api/v1/observability/events": () => ({ status: 200, json: { ingested: 1 } }),
    });

    let ran = false;
    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(
      async () => {
        ran = true;
        return "ok";
      },
      { client, stepName: "chat" },
    );

    await expect(wrapped()).rejects.toMatchObject({ reason: "evaluation_errors" });
    expect(ran).toBe(false);
  });

  it("does not call the server when a fetched control set has nothing applicable", async () => {
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({
        status: 200,
        json: initResponse([{ ...denyControl, scope: { step_names: ["some-other-step"] } }]),
      }),
    });

    const client = makeClient({ serverUrl: harness.url });
    await client.ready();

    const wrapped = control(async (value: string) => `echo:${value}`, {
      client,
      stepName: "chat",
    });

    await expect(wrapped("hi")).resolves.toBe("echo:hi");
    expect(harness.requests.filter((r) => r.path === "/api/v1/evaluation")).toHaveLength(0);
  });
});

describe("control() error hygiene", () => {
  it("does not leak the api key, server url or response body into refusal messages", async () => {
    const secretBody = { detail: "internal: db password is hunter2" };
    const harness = await withServer({
      "/api/v1/agents/initAgent": () => ({ status: 200, json: initResponse([denyControl]) }),
      "/api/v1/evaluation": () => ({ status: 500, json: secretBody }),
    });

    const client = makeClient({ serverUrl: harness.url, apiKey: "sk-super-secret-key" });
    await client.ready();

    const wrapped = control(async () => "ok", { client, stepName: "chat" });
    const error = (await wrapped().then(
      () => null,
      (e: unknown) => e,
    )) as ControlEvaluationError;

    expect(error).toBeInstanceOf(ControlEvaluationError);
    expect(error.message).not.toContain("sk-super-secret-key");
    expect(error.message).not.toContain("hunter2");
    expect(error.message).not.toContain(harness.url);
  });
});
