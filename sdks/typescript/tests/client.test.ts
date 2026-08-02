import { describe, expect, it } from "vitest";

import { AgentControlClient } from "../src/client";

describe("AgentControlClient", () => {
  it("stores init config", () => {
    const client = new AgentControlClient();

    client.init({
      agentName: "test-agent",
      register: false,
      serverUrl: "http://localhost:8000",
      apiKey: "test-key",
    });

    expect(client.initialized).toBe(true);
    expect(client.config?.agentName).toBe("test-agent");
  });
});
