# Agent Control TypeScript SDK

TypeScript SDK for Agent Control. Wraps your agent's model and tool calls, evaluates the controls
your control plane defines for that agent, and enforces the decision.

Full documentation: [Agent Control TypeScript SDK](https://docs.agentcontrol.dev/sdk/typescript-sdk)

## Install

```bash
npm install agent-control
```

## Quick start

```ts
import agentControl, { control, ControlViolationError, ControlSteerError } from "agent-control";

agentControl.init({
  agentName: "support-bot",
  serverUrl: process.env.AGENT_CONTROL_URL!,
  apiKey: process.env.AGENT_CONTROL_API_KEY,
});

// Registration runs in the background. Awaiting it is optional but recommended:
// without it, the first controlled call blocks until registration settles.
await agentControl.ready();

const chat = control(async function chat(message: string) {
  return assistant.respond(message);
});

try {
  const reply = await chat("my SSN is 123-45-6789");
} catch (err) {
  if (err instanceof ControlViolationError) {
    // A control matched with action "deny".
  } else if (err instanceof ControlSteerError) {
    // A control matched with action "steer". err.steeringContext says what to change.
  }
}
```

`control()` runs two evaluation stages around your function: `pre` on the derived input before the
call, and `post` on the return value after it. Controls are defined and scoped on the server; the
wrapper only marks where they are evaluated. This mirrors the Python SDK's `@agent_control.control()`
decorator, and both SDKs produce the same decisions, the same error taxonomy, and the same
observability events.

## Fail-closed by default

This is a guardrail. When it cannot establish a decision, it refuses the call. It does not pass the
call through and hope.

Every refusal throws `ControlEvaluationError`, which carries a `reason` you can switch on. On a
`pre`-stage refusal your function never runs. On a `post`-stage refusal it has already run, but its
output is withheld rather than returned.

| Situation | `reason` | Default behaviour | With `failOpen: true` |
| --- | --- | --- | --- |
| `init()` never called | `not_initialized` | Refuse | **Still refuses** — opting into fail-open requires an `init()` call to opt in from |
| `init({ register: false })`, so no control set was ever requested | `registration_skipped` | Refuse | Allow, warn |
| Registration failed: control plane unreachable, DNS failure, timeout, 401, 5xx | `registration_failed` | Refuse | Allow, warn |
| No control set has been cached yet (e.g. call raced ahead of registration and registration then failed) | `cache_missing` | Refuse | Allow, warn |
| Cached control set is older than `controlCacheMaxAgeMs` | `cache_stale` | Refuse | Allow, warn |
| An applicable control declares `execution: "sdk"` | `sdk_execution_unsupported` | Refuse | Skip that control, warn |
| An applicable control cannot be interpreted (e.g. a `step_name_regex` this runtime rejects) | `control_unreadable` | Refuse | Skip that control, warn |
| The evaluation request failed: network error, timeout, non-2xx, unparseable response | `evaluation_request_failed` | Refuse | Allow, warn |
| The evaluation returned nothing | `evaluation_empty` | Refuse | Allow, warn |
| The evaluation completed but one or more controls errored, or matched with an action this SDK does not recognise | `evaluation_errors` | Refuse | Allow, warn |

Two cases are **not** in that table, because they are decisions rather than failures to decide:

- A control set was fetched and **nothing in it applies** to this step and stage. The call proceeds.
  That is a real answer, not an absent one.
- A control matched. `deny` throws `ControlViolationError`, `steer` throws `ControlSteerError`,
  `observe` is recorded and the call proceeds. **`failOpen` does not touch any of this.** It only
  covers the case where no decision could be reached at all.

One more case sits between the two tables. If the evaluation comes back `is_safe: false` but no
match carries a `deny` or `steer` action, this SDK throws `ControlViolationError` anyway. The Python
decorator lets that combination through; `agent_control.integrations._core` blocks it. We follow
`_core`, because "unsafe, and nobody said what to do about it" is not a reason to run the call.

### Staleness

A cached control set that is too old is not evidence of anything, so it cannot be the basis for
allowing a call. The cache is refreshed every `controlRefreshIntervalMs` (default 60s) and expires
after `controlCacheMaxAgeMs` (default 5 minutes). When a refresh fails the previous cache is kept —
but it keeps ageing, so a control plane that stays unreachable stops calls within the staleness
bound rather than serving a frozen policy indefinitely.

The bound is checked before each stage, not once per call. A function that runs longer than
`controlCacheMaxAgeMs` will pass its `pre` stage and then be refused at `post` with `cache_stale`,
because deciding to hand its output back is a decision made at that later moment and the control set
justifying it has expired by then. If your steps are legitimately long, raise the bound.

Tighten or loosen it per deployment:

```ts
agentControl.init({
  agentName: "support-bot",
  serverUrl: process.env.AGENT_CONTROL_URL!,
  controlCacheMaxAgeMs: 60_000,
  controlRefreshIntervalMs: 15_000,
});
```

### Controls this SDK cannot run

Controls carry an `execution` field. `execution: "server"` controls are evaluated by the control
plane, which is what this SDK does. `execution: "sdk"` controls are meant to run in-process, and
this SDK has no local evaluator engine — so when one of them applies to a step, the honest answer is
that the decision is unknown, and the call is refused. Set those controls to `execution: "server"`,
or run that step through the Python SDK.

### Opting into fail-open

```ts
agentControl.init({
  agentName: "support-bot",
  serverUrl: process.env.AGENT_CONTROL_URL!,
  failOpen: true, // logs a warning at init and on every allowed-but-unevaluated call
});
```

Use it knowingly. With `failOpen: true` an unreachable control plane means your agent runs
unprotected.

## Options

`agentControl.init(options)`:

| Option | Default | Meaning |
| --- | --- | --- |
| `agentName` | required | Agent identity registered with the control plane |
| `serverUrl` | required | Control plane base URL |
| `apiKey` | — | API key, or an async function returning one |
| `steps` | `[]` | Step schemas registered alongside the agent |
| `agentDescription`, `agentVersion`, `agentMetadata` | — | Registration metadata |
| `targetType` / `targetId` | — | Target context; supply both or neither. Controls bound to the target are merged into the effective set |
| `register` | `true` | Register the agent and fetch its controls on init |
| `failOpen` | `false` | Allow calls that could not be evaluated |
| `controlCacheMaxAgeMs` | `300000` | Staleness bound for the cached control set |
| `controlRefreshIntervalMs` | `60000` | Background refresh interval; `0` disables refresh |
| `observability` | `true` | Emit control-execution events |
| `logger` | console | SDK diagnostics sink (`debug`/`info`/`warn`/`error`) |
| `timeoutMs`, `userAgent`, `debugLogger` | — | Transport settings for the generated client |

`control(fn, options)`:

| Option | Default | Meaning |
| --- | --- | --- |
| `stepName` | `fn.name` | Step name used for control scoping |
| `stepType` | `"llm"` | Step type used for control scoping (`"llm"` or `"tool"`) |
| `client` | package default | Which client's session backs this control site |
| `getInput` | derived | Override how the evaluated input is taken from the call arguments |
| `getOutput` | identity | Override how the evaluated output is taken from the return value |
| `context` | — | Extra context sent on the evaluation payload |
| `policy` | — | Documentation only, matching the Python decorator |

By default the evaluated input for an `llm` step is: the single string argument if there is one;
otherwise the first of `input`, `message`, `query`, `text`, `prompt`, `content`, `userInput`,
`user_input` found on a single object argument; otherwise the first string among the arguments;
otherwise the JSON-encoded arguments. For a `tool` step it is the single object argument, or
`{ args }`. Pass `getInput` when your call shape does not fit that.

Arguments that JSON cannot represent are degraded, not rejected: a circular reference serializes as
`"[Circular]"` on the second visit and a `BigInt` as its decimal string, which matches the Python
SDK's `str()` based extraction. Content cannot hide behind a cycle, since every object is still
serialized the first time it is reached. A *return value* the transport cannot encode is different:
the post-stage evaluation genuinely cannot be performed, so it refuses with
`evaluation_request_failed` and the output is withheld.

Refusal messages never quote the control plane's response. `ControlEvaluationError.message` names
the failure class and the HTTP status; the underlying transport error, body included, is on the
error's `cause` so it goes wherever you already send exception causes rather than into every log
line that mentions a refusal.

## Lifecycle

```ts
agentControl.init({ /* ... */ });   // synchronous; transport ready immediately
await agentControl.ready();          // resolves when the first registration attempt settles
await agentControl.refreshControls(); // force a refresh; rejects if the fetch fails
agentControl.shutdown();             // stop the refresh loop, drop the cached control set
```

`init()` never throws on a connectivity failure — it records it, and the failure surfaces at the
call site as a refusal. `ready()` resolves either way; check `agentControl.session?.lastRegistrationError`
if you want to fail your own startup on it.

## Direct API access

The generated OpenAPI client is available on the same object for everything outside the control
path:

```ts
await agentControl.controls.list();
await agentControl.policies.list();
await agentControl.observability.queryEvents({ agentName: "support-bot" });
```

If you only want the API client and no control session, pass `register: false`.

One caveat on those direct calls. The generated `APIPromise` derives a second promise in its
constructor and only attaches a rejection handler to it if you use `.catch()` or `.finally()`. When
the request pipeline throws rather than returning an error result, which a response declaring
`content-type: application/json` and carrying a truncated or empty body will do, that second promise
rejects with nobody listening, and Node's default policy for an unhandled rejection is to kill the
process. The control path (`control()`, registration, refresh, event ingest) contains this
internally. Direct generated-client calls do not, so wrap them in `try`/`catch` *and* keep a
`process.on("unhandledRejection")` guard if you call them on a hot path. The real fix belongs in the
generator.

## Credits

Agent Control's TypeScript SDK is generated with [Speakeasy](https://www.speakeasy.com/product/sdk-generation).
