import { describe, expect, it } from "vitest";

import { control } from "../src/control";
import { AgentControlClient } from "../src/client";
import { ControlEvaluationError } from "../src/errors";

describe("control", () => {
  it("refuses to run when the SDK was never initialized", async () => {
    const wrapped = control(async (value: string) => `echo:${value}`, {
      client: new AgentControlClient(),
    });

    await expect(wrapped("hello")).rejects.toBeInstanceOf(ControlEvaluationError);
  });
});
