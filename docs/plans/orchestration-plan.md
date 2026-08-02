# Chat, Steering and Progress: Implementation Plan

Status: plan, partially built. **Phase 1 has landed on this branch** (`agent_runtimes` and `agent_sessions` ORM plus migration, `AdkExecutorClient`, `ExecutorSettings`, the new `Operation` members, both startup refusals). Phase 2 onward is still prose: `grep -rn "asyncio.shield" server/src` returns nothing, there is no `/turns` route, and `in_flight_since` exists only as a column and a serialized field. Check the working tree before asserting that anything below does or does not exist. This line previously read "no implementation code exists for anything below" and stayed there after Phase 1 shipped, which is how a later amendment came to cite a `finally` block that has never been written.
Branch context: `feat/agent-teams` (phases 1 to 5 of the teams work already built, tests green).
Author's note: every codebase claim in this document was verified against the working tree before it was written. Claims about Google ADK's own API surface were **not**, and are flagged as assumptions with a spike that settles them.

---

## 1. Architecture decision

### The fork

**Option A.** Agent Control stays a control plane. Adopt an existing agent runtime, and Agent Control becomes the human-facing surface over it: chat, steering and progress, read and written through a proxy layer.

**Option B.** Agent Control grows orchestration of its own: task entities, a run loop, agent processes with inboxes, pause and resume.

### The choice: Option A, with Google ADK as the first executor

Three pieces of evidence, all from this repo.

The deployment model cannot host Option B. Agent Control is a stateless FastAPI app. Every endpoint takes a `Principal`, filters on `namespace_key`, answers, and forgets. Nothing pins state to an instance. Steering a running agent requires that something be running and addressable. Option B means process affinity, an inbox with delivery guarantees, crash recovery, and cancellation semantics, inside an app whose whole shape is request-scoped. That is a different application wearing the same repo.

The steering primitive already exists. `sdks/python/src/agent_control/integrations/google_adk/plugin.py` hooks `before_model_callback`, `after_model_callback`, `before_tool_callback` and `after_tool_callback`, and `_inject_steering_guidance` at line 533 mutates a live model request. Today the guidance comes from a control match. Sourcing it from a human is a change to where the string comes from, not new machinery.

A server-to-agent channel already exists and ships. `_policy_refresh_worker` at `sdks/python/src/agent_control/__init__.py:294` is a daemon thread polling the server. Option B would rebuild a worse version of it.

Against that, ADK, LangGraph and claude-flow all already have persistence, streaming and suspend/resume. Building a fourth is a permanent tax with no upside, because the differentiator here is the policy engine, not the loop.

### What changed from the original Option A framing

Review turned up thirteen blockers. Two of them materially change the shape of Option A, and both are recorded here rather than buried in a phase.

**One agent per executor process, enforced at the SDK level.** `sdks/python/src/agent_control/_state.py` holds a module-level singleton with exactly one `current_agent`, one `api_key`, one `server_url`, one `server_controls` list. `AgentControlPlugin.__init__` raises `ValueError` outright when `agent_name` does not match the process's initialized agent (plugin.py, lines 84 to 90). `_cached_server_control_lookup` keys its cache on `state.current_agent.agent_name` (evaluation.py:203). So a single shared `adk api_server` hosting many agents is not a topology this SDK supports. It is not a deployment footnote, it is a hard constraint, and it is per-agent rather than per-namespace.

Resolution: **process per agent** is the shipped topology, and an `agent_runtimes` registry maps each agent to its own executor base URL from Phase 1 onward. A team of five agents means five executor processes. That is honest and it works. The alternative, moving `_StateContainer` onto `ContextVar`s and relaxing the plugin guard, is a breaking SDK change costed separately in section 12 and deliberately not on the critical path.

**Human guidance must arrive as a user turn, not as a system-instruction append.** `extract_request_text` in `sdks/python/src/agent_control/integrations/google_adk/_extractors.py` reads `llm_request.contents[-1].parts` and nothing else. It never touches `system_instruction`. So text injected the way `_inject_steering_guidance` injects it is invisible to every control in the deployment. Adding an `AUTHENTICATED`-tier endpoint whose free-text body lands verbatim in the model's highest-trust field, unevaluated, would hand any valid key a control bypass. Creating a control is `ADMIN` (`CONTROLS_CREATE: AccessLevel.ADMIN`, header.py). Nudging would be `AUTHENTICATED`. That gap is not defensible.

Resolution: a nudge is delivered as a **synthetic user-role content part appended to `contents`**, delimited and labelled as operator input. It lands in `contents[-1]`, so every existing control evaluates it with no new evaluation plumbing. It is also the form models weight most heavily, which happens to de-risk the single biggest product assumption in the whole plan. This was originally sketched as a fallback. It is now the default, and the system-instruction append is not used for human text at all.

### Option B: rejected, with reasons

Three to six months minimum, and it never ends. Task entity, run entity, run loop, worker lifecycle, inbox with at-least-once delivery, cancellation that actually cancels, crash recovery, backpressure, and a UI on top. Then it is maintained against ADK and LangGraph shipping the same features faster with more people. Every blocker that stops Option A (DB pool exhaustion under long requests, no streaming precedent, `control_execution_events` being useless as a progress source) also stops Option B, plus everything else Option B needs. The maintenance surface is permanent and the competition is well funded.

### What this plan does not deliver

It delivers single-agent chat, steering and progress. **It does not deliver agents working as a team with linear hand-off.** That was half the original ask and it is not in any phase below. The teams work already on this branch stays descriptive: it records who belongs to what, and the chat panel talks to exactly one agent in one executor process. Hand-off would be Phase 8, most plausibly as an ADK `SequentialAgent` inside one app so a shared session carries the hand-off, with Agent Control's teams becoming the source of truth for the sequence. Rough size: 2 to 3 weeks on top of everything below. Flagged here so the sequencing can be reconsidered before work starts, not discovered in week ten.

---

## 2. Three corrections that must land before anyone builds

**Steering is a nudge that arrives at the next model call.** A tool that runs for 40 seconds cannot be interrupted. Not by ADK, not by LangGraph, not by anything short of killing the process. The UI must show `queued`, then `delivered at turn N`, and must render the exact text that was handed to the model. If the panel implies the agent stopped and read the message, it is lying, and the first long tool call will expose it.

**A human action arrives at the next model call or the next tool call, whichever comes first.** The plugin blocks on both paths already. `before_model_callback` (plugin.py:144) can return a substitute `LlmResponse` built by `build_blocked_llm_response` (`_extractors.py:149`), and `before_tool_callback` (plugin.py:258) can return a substitute result dict built by `build_blocked_tool_response` (`_extractors.py:176`) that stops a tool before it executes. So stopping an agent is finer-grained than "next model call", and specifically it can stop the agent before it sends the email rather than after. A tool that is *already running* still cannot be interrupted, which is the first correction restated and not softened. The only thing that ends a running tool is ending the process, and that is not an interruption, it is a kill with no rollback of anything the tool already did.

**There is no progress percentage in this stack.** ADK emits events. Events are not progress. The only honest number comes from the agent declaring a plan and marking steps, which Phase 6 builds. Anything derived from event counts is a number that moves and means nothing.

---

## 3. Naming

Three collisions, all resolved before a line is written.

`steer` is already an `ActionDecision`, a control outcome, a `ControlSteerError`, a `steering_context` field, and an example directory. Human guidance is called a **nudge**: tables `agent_session_nudges`, models `models/src/agent_control_models/nudges.py`, operations `agent_nudges.*`, UI label "Nudge the agent".

`runtime` is already taken, and the original draft walked straight into it. `Operation.RUNTIME_TOKEN_EXCHANGE` and `Operation.RUNTIME_USE` exist in `auth_framework/core.py`, alongside `auth_framework/runtime_token.py`, `RuntimeAuthConfig`, `LocalJwtVerifyProvider`, and the env vars `AGENT_CONTROL_RUNTIME_AUTH_MODE`, `AGENT_CONTROL_RUNTIME_TOKEN_SECRET`, `AGENT_CONTROL_RUNTIME_TOKEN_TTL_SECONDS`. Everything in this plan that means "the process running the agent" is called an **executor**: `ExecutorClient`, `AdkExecutorClient`, `ExecutorSettings` with `env_prefix="AGENT_CONTROL_EXECUTOR_"`, columns `executor_kind` / `executor_app_name` / `executor_user_id` / `executor_session_id`, error codes `EXECUTOR_UNAVAILABLE` and `EXECUTOR_REJECTED`, compose service `agent-executor`. Env var names and wire-level error codes are public contract, so this rename is free now and expensive later.

`stop` is not safe as a wire term inside this SDK either: `_refresh_stop_event`, `stop_event` and `_stop_policy_refresh_loop` are ordinary control-flow vocabulary in `sdks/python/src/agent_control/__init__.py`, and an `agent_control.stop` module beside them reads as "shut the SDK down". `cancel` is taken twice over, once as the `cancelled` nudge status (section 7.3) and once as Phase 3's cancel button, which means "abandon the HTTP request, the turn keeps running". `pause` promises a resume nothing here can deliver. `interrupt` and `abort` promise immediacy that section 2 and section 14 both deny. Human-initiated stopping of a turn is a **halt**: table `agent_session_halts`, models `models/src/agent_control_models/halts.py`, operation `agent_halts.write`, UI label "Stop responding". Zero occurrences of `halt` exist in `models/`, `server/`, `sdks/python/` or `ui/src`; the only hit in the repo is `haltIterator`, a Speakeasy-generated pagination helper at `sdks/typescript/src/generated/types/operations.ts:44`, which is a free function in a generated module rather than an operation id.

And a halt is not the same thing as killing the executor, so they do not share a name. Killing the process is an **executor restart**, it lives on the agent's runtime binding rather than on a session, and it reuses `agent_runtimes.write`. Section 9.6 explains why calling it a "force halt" on a session route was wrong about both its blast radius and its resource.

---

## 4. Ownership of state

| State | Owner | Lives in |
|---|---|---|
| Conversation events (user text, model text, tool calls, tool results) | ADK | ADK's own tables, `DatabaseSessionService` |
| Session identity mapped into namespace / agent / team | Agent Control | `agent_sessions` (new) |
| Agent to executor binding | Agent Control | `agent_runtimes` (new) |
| Queued nudges and delivery state | Agent Control | `agent_session_nudges` (new) |
| Operator halt requests and where they landed | Agent Control | `agent_session_halts` (new) |
| Agent-declared plan and step status | Agent Control | `agent_session_plan_steps` (new) |
| Guardrail decisions | Agent Control | `control_execution_events` (existing, unchanged) |
| Agents, controls, policies, teams | Agent Control | existing 12 tables |
| The agent loop, model calls, tool execution | ADK | executor process memory |

**ADK tables must not land in the Agent Control database.** `server/tests/conftest.py:33` truncates every table returned by `inspect(conn).get_table_names("public")` between tests, so ADK session tables in the same database would be wiped mid-suite, and Alembic autogenerate would try to drop them. ADK gets a separate logical database, `adk_runtime`, on the same Postgres instance.

Creating it is not a docker-compose init-script job. `docker-compose.yml` mounts a named `pgdata` volume, and the Postgres image runs `/docker-entrypoint-initdb.d` only against an empty data directory, so every existing developer and deployment would silently never get the database. Instead: the executor container's entrypoint runs a preflight that connects to `postgres` and issues `CREATE DATABASE adk_runtime` swallowing "already exists", plus a one-shot `adk-db-init` compose service gated on `depends_on: postgres: condition: service_healthy`. The manual `createdb` is documented in `server/.env.example` for anyone upgrading.

Separately, `adk_runtime` gets a **dedicated Postgres role** that is not `agent_control`. The compose file's `POSTGRES_USER: agent_control` owns the control-plane database, and an executor connecting as that role could read and rewrite `controls`, `policies` and `control_bindings`, which is reachable through ordinary prompt injection into a tool result. The init path creates an `adk` role owning only `adk_runtime` and issues `REVOKE CONNECT ON DATABASE agent_control FROM adk`. The control-plane database credentials never appear in the executor service's environment, and neither does anything else the executor does not need.

---

## 5. Where the executor runs

A separate process per agent, on a private network, never mounted into the Agent Control FastAPI app and never driven in-process via `Runner`.

Running agent code inside the control plane breaks the one sentence that defines this product, and a tool that blocks or segfaults would take down policy evaluation for every other agent in the deployment. ADK also moves fast, and a separate unit can be pinned, rolled and rolled back on its own.

`adk api_server` ships with **no authentication**. The only real control is that its port is never published. It goes on the internal compose network with no `ports:` mapping, and `server/.env.example` states plainly that exposing it is equivalent to publishing an unauthenticated model-spending endpoint. A shared-secret header is worth adding for defence in depth, but ADK will not check it, so it is not presented as the control.

One more thing the threat model has to say out loud: an agent with any HTTP-egress tool is an SSRF pivot onto the network its own executor sits on, and from inside, ADK's unauthenticated session CRUD is directly reachable. Process per agent shrinks that blast radius to one agent's own sessions, which is a real benefit of the topology forced by the SDK constraint. Where an agent has web access and more than one tenant exists, the executor network segment must be unreachable from tool egress, or per-namespace executors become a requirement rather than a deferral.

For Phases 1 through 6 the executor lives in `docker-compose.dev.yml`. The published `docker-compose.yml`, which pulls `galileoai/agent-control-server:latest` and is the documented quick start, is untouched until the feature is real. Adding a service that requires a `GOOGLE_API_KEY` and a second image to the out-of-box experience is not something to do behind a feature flag.

The escape hatch, explicitly not built now: `google.adk.runners.Runner` can be driven in-process behind the same `ExecutorClient` Protocol. The Protocol is the seam that makes that a one-file change.

---

## 6. Authorization

### 6.1 Human-side operations

New `Operation` members in `auth_framework/core.py`, each registered in `DEFAULT_OPERATION_ACCESS` in `providers/header.py`. Missing entries are rejected, which `test_auth_framework.py` already enforces.

| Operation | Access | Reasoning |
|---|---|---|
| `AGENT_SESSIONS_READ = "agent_sessions.read"` | `AUTHENTICATED` | Session list and metadata. Same class of read as `OBSERVABILITY_READ`. |
| `AGENT_SESSION_CONTENT_READ = "agent_sessions.content_read"` | `AUTHENTICATED` | Message bodies, nudge bodies, plan notes. Split from metadata because this is a different sensitivity class: raw human prompts, model output, and tool results that in this repo can contain Linear project data pulled with a server-held key. |
| `AGENT_SESSIONS_WRITE = "agent_sessions.write"` | `AUTHENTICATED` | Creating and archiving a chat is per-caller working state, not org configuration. `ADMIN` would mean only admin keys can open a chat, which removes the feature. Precedent: `AGENTS_CREATE` is `AUTHENTICATED`. |
| `AGENT_SESSIONS_RUN = "agent_sessions.run"` | `AUTHENTICATED` | Split from write because running a turn spends money and calls a model. Splitting later is a wire-contract change; splitting now costs one enum line. |
| `AGENT_NUDGES_WRITE = "agent_nudges.write"` | `AUTHENTICATED` | Queues human guidance. |
| `AGENT_RUNTIMES_WRITE = "agent_runtimes.write"` | `ADMIN` | Binding an agent to an executor URL is deployment configuration. Admin, same tier as `CONTROL_BINDINGS_WRITE`. Phase 5's executor restart reuses this rather than minting a second ADMIN operation with identical semantics, because restarting the process an agent runs in *is* an act on that binding. |
| `AGENT_HALTS_WRITE = "agent_halts.write"` | `AUTHENTICATED` | Stops the caller's own in-flight turn. Same tier as `AGENT_SESSIONS_RUN`, because run at `AUTHENTICATED` and stop at `ADMIN` is the one combination that cannot be defended: whoever can start a turn that spends money must be able to stop it. **Scoped to the session's creator**, admin excepted, using `_require_content_access` (`services/agent_sessions.py:946`), which is the same predicate transcript reads already use at line 817. Without that scoping the tier means any valid key in the namespace can stop every turn everyone else starts, indefinitely, at negligible cost, which is the availability twin of the confidentiality hole 6.3 closed. It also shares Phase 2's `(namespace_key, created_by_hash)` quota bucket and returns the same typed 429. |

Only one new `ADMIN` operation, and it already exists. Nothing here changes policy, controls or bindings.

### 6.2 Machine-side operations use the runtime-token path that already exists

The original draft invented `agent_nudges.consume` and `agent_plans.write` as flat `AUTHENTICATED` operations with a long-lived API key in the executor. That is wrong twice: any authenticated key in the namespace could claim and silently swallow nudges for any session (claim flips status to `claimed`, and cancelling a claimed nudge is a 409, so the human sees "delivered" for a message nobody read), and the executor would hold a key that can also register agents and read every transcript.

The repo already ships the right primitive and it was walked past. `POST /auth/runtime-token-exchange` mints a short-lived HS256 token bound to `(target_type, target_id)`. `endpoints/auth.py` documents `target_type` as an opaque kind, with `session` as its own example. `LocalJwtVerifyProvider` hard-fails when the request target does not match the token's target. `require_operation` accepts a `context_builder`, `set_authorizer` accepts a per-operation override, and `_StateContainer` in the SDK already carries `target_type`, `target_id` and a `RuntimeTokenCache`.

So:

| Operation | Authorizer | Binding |
|---|---|---|
| `AGENT_NUDGES_CONSUME = "agent_nudges.consume"` | `LocalJwtVerifyProvider` via `set_authorizer(..., operation=...)` | `target_type="agent_session"`, `target_id=<session_key>` |
| `AGENT_PLANS_WRITE = "agent_plans.write"` | same | same |

**`agent_nudges.consume` covers halt delivery too, and there is deliberately no `agent_halts.consume`.** A separate operation would document a boundary that does not exist. Halts ride the same claim call as nudges at the model boundary (section 9.4), so the operation actually guarding that request is `agent_nudges.consume`; a deployment that restricted a separate halt operation would still have halts delivered under the nudge one, and revoking the nudge operation would silently disable half of halt delivery, which reads to an operator as "stop sometimes doesn't work". One token, one session binding, one boundary decision, one operation. Its comment in `providers/header.py` is widened to say so; the `ADMIN` fallback in `DEFAULT_OPERATION_ACCESS` and the reasoning above it are unchanged and now cover both.

**A halt carries no operator text.** That is a constraint, not an oversight. What the model sees is a constant authored by the SDK, so a halt cannot become the unevaluated free-text channel into a high-trust field that section 1 spends four paragraphs closing. It also means no control evaluation of a halt body (unlike section 9.2), no `content_read` split on the halt body because there is none, and no log-hygiene exposure. If a `reason` field is ever wanted it is audit-only, it never reaches the model, and the moment it does it inherits every argument in section 1.

At session creation, Agent Control mints a session-bound runtime token and seeds it into the ADK session `state`. The plugin and the progress tools read it from `CallbackContext.state` / `ToolContext.state`. A token for session A physically cannot claim session B or write session B's plan. The claim body no longer needs to carry executor coordinates at all, because the token *is* the session identity. `agent_session_nudges` also gets a `claimed_by` column, surfaced in the UI, so a swallowed nudge is distinguishable from a delivered one.

Token lifetime is bounded by `AGENT_CONTROL_RUNTIME_TOKEN_TTL_SECONDS`; the plugin refreshes through the existing `RuntimeTokenCache` path rather than caching a raw token in session state past expiry. Long sessions therefore need a refresh mechanism, and the concrete shape of that is Phase 0 question A1b.

### 6.3 The honest limitation

`HeaderAuthProvider._resolve_namespace_key` is literally `del request; return self._default_namespace_key`. Under the shipped default provider every caller is in `default`, so `namespace_key` is enforcement plumbing, not a tenancy boundary. Only `HttpUpstreamAuthProvider` returns a caller-derived namespace. `AccessLevel` has exactly three values, so "grant this key drain-only" is not expressible under the default provider either.

For teams this was tolerable, because teams are configuration. For chat transcripts it is not. Two things follow, both required:

1. Content reads are scoped to the creating principal by default. `agent_sessions` carries `created_by_hash`, the service filters on it, and `is_admin` overrides for operator visibility. This is a one-line service rule if decided now and a migration plus contract change if decided later.
2. `server/.env.example` and this plan state plainly that under `HeaderAuthProvider`, per-user isolation of transcripts is not enforceable, and that a deployment with more than one real tenant needs `HttpUpstreamAuthProvider` plus per-namespace executors.

### 6.4 Two startup refusals

`server/.env.example:15` sets `AGENT_CONTROL_API_KEY_ENABLED=false`, and `docker-compose.yml` defaults it false too. With auth disabled, `_validate_api_key` returns an `AuthLevel.NONE` client and every operation including admin ones succeeds. Today the worst an unauthenticated caller on the published port can do is tamper with configuration. After this plan they could start turns that spend the operator's Gemini quota and inject text into running agents.

So, enforced in the lifespan, in Phase 1, before `agent_sessions.run` exists:

- Refuse to start when `executor.enabled` is true and `api_key_enabled` is false, with an explicit opt-out env var for local dev.
- Refuse to start when `executor.enabled` is true and `cors_origins == ["*"]` while `allow_credentials` is on. The `.env.example` guidance that `"*"` is usually fine in production is removed and replaced with a named origin.

Both shipped in Phase 1 (`check_executor_startup_requirements`, `config.py`). Phase 5 adds a third refusal, at request time rather than at startup, because it changes what the first hatch is worth.

**`AGENT_CONTROL_EXECUTOR_ALLOW_INSECURE_LOCAL_DEV=true` disables the startup refusal. It must never disable the executor restart path.** The hatch was sized once, against a specific blast radius: configuration tampering plus model spend. Executor restart adds remote process termination reachable by anything that can open a TCP connection to the server port, which on a laptop on a café network is a stranger killing your agents and in the inevitable misconfigured staging environment is worse. With `api_key_enabled` false every operation succeeds including `ADMIN` ones, so the tier that is supposed to protect restart evaporates exactly where the hatch is set. `POST /agent-runtimes/{agent_name}/restart` therefore refuses at request time with a written 503 naming the setting whenever `auth.api_key_enabled` is false, regardless of `allow_insecure_local_dev`. `.env.example`'s note on the hatch says this in the same block.

---

## 7. Schema

### 7.1 `agent_runtimes` (Phase 1)

The agent-to-executor binding. Without it, nothing can answer "which process serves this agent", and `POST /agent-sessions` is unimplementable.

```
namespace_key    VARCHAR(255) NOT NULL DEFAULT 'default'
agent_name       VARCHAR(255) NOT NULL       -- normalized via normalize_agent_name
executor_kind    VARCHAR(32)  NOT NULL DEFAULT 'google_adk'
base_url         VARCHAR(512) NOT NULL
executor_app_name VARCHAR(255) NOT NULL      -- the ADK App(name=...) served by that process
enabled          BOOLEAN      NOT NULL DEFAULT TRUE
created_at       TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
updated_at       TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP

PRIMARY KEY (namespace_key, agent_name)
FOREIGN KEY (namespace_key, agent_name) REFERENCES agents(namespace_key, name) ON DELETE CASCADE
```

Populated by an admin write (`agent_runtimes.write`), or optionally by the SDK at `agent_control.init()` time when an ADK integration is active. `POST /agent-sessions` for an agent with no enabled binding returns 409 with a written message, not a 500.

**Phase 5 adds no column here, deliberately.** An earlier draft of the restart mechanism proposed `supervisor_url VARCHAR(512)`, a second admin-writable outbound sink alongside `base_url`, carrying the credential that kills a process. Two free-form columns that must agree about where one process lives will eventually disagree, and the failure mode is a restart aimed at a host that is not the executor. The supervisor endpoint is derived instead: the host of `base_url`, which `validate_executor_base_url` already constrains (`models/src/agent_control_models/agent_runtimes.py:47`), plus `ExecutorSettings.supervisor_port`. One source of truth for where an agent's process is. "Restart unavailable" then means the port is unset, which still gives a written 409 with no migration column behind it.

`base_url` carries no uniqueness constraint and gets none. The primary key is `(namespace_key, agent_name)`, so two namespaces or two agent names can point at the same executor, and today that is a legitimate deployment: section 14 defers per-namespace executors precisely because sharing one is currently normal. A unique index would break existing installs at migration time to enforce an invariant that only one endpoint needs. The restart endpoint enforces it at request time instead, in section 9.6.

### 7.2 `agent_sessions` (Phase 1)

```
id                    BIGSERIAL PRIMARY KEY
namespace_key         VARCHAR(255) NOT NULL DEFAULT 'default'
session_key           VARCHAR(64)  NOT NULL      -- uuid4().hex, the only id a browser sees
agent_name            VARCHAR(255) NOT NULL
team_id               INTEGER NULL               -- plain column, see below
executor_kind         VARCHAR(32)  NOT NULL DEFAULT 'google_adk'
executor_app_name     VARCHAR(255) NOT NULL      -- server-copied from agent_runtimes
executor_user_id      VARCHAR(255) NOT NULL      -- server-minted, namespace-prefixed
executor_session_id   VARCHAR(255) NOT NULL      -- server-minted uuid4().hex
title                 VARCHAR(255) NULL
status                VARCHAR(32)  NOT NULL DEFAULT 'active'
                          -- active | archived | orphaned | orphaned_pending_delete
created_by_hash       VARCHAR(64) NULL           -- sha256(caller_id)[:16], never serialized
last_trace_id         VARCHAR(64)  NULL
in_flight_since       TIMESTAMPTZ  NULL
in_flight_trace_id    VARCHAR(64)  NULL
last_activity_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
created_at            TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
updated_at            TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP

UNIQUE (namespace_key, session_key)                          uq_agent_sessions_namespace_key
UNIQUE (executor_app_name, executor_user_id, executor_session_id)
                                                             uq_agent_sessions_executor_global
UNIQUE (namespace_key, id)                                   uq_agent_sessions_namespace_id
INDEX (namespace_key, agent_name, last_activity_at DESC)     idx_agent_sessions_agent_recent
INDEX (namespace_key, status, in_flight_since)               idx_agent_sessions_in_flight
```

Four things here are deliberate and each fixes a specific defect in the original draft.

**The executor-triple uniqueness constraint is global, with no `namespace_key`.** Scoped per namespace, it would have permitted namespace B to hold a mapping row pointing at exactly the same ADK session as a namespace-A row, and then read A's entire transcript through a lookup that passes every namespace filter in the service layer. ADK's session store has no namespace concept, so this mapping table is the only boundary, and the constraint has to prevent adoption rather than permit it. Belt and braces: `executor_user_id` is minted server-side as `f"{namespace_key}:{uuid4().hex}"`, and `CreateAgentSessionRequest` declares `model_config = ConfigDict(extra="forbid")` with no `executor_*` fields at all, so no client can supply or influence any part of the triple. A test inserts the same triple under two namespaces and asserts an `IntegrityError`.

**`team_id` is a plain nullable column, not a composite FK.** The draft specified `FOREIGN KEY (namespace_key, team_id) REFERENCES teams(namespace_key, id) ON DELETE SET NULL`. Postgres nulls every referencing column on a multi-column FK unless the PG15+ column-list form is used, so it would have tried to write `namespace_key = NULL` and aborted on the NOT NULL constraint. The stated behaviour ("sessions survive with no team") would instead have been `DELETE /teams/{slug}` returning a 500 the moment any session referenced the team, breaking an endpoint that is green today. `team_members` sidesteps this by using `CASCADE`, so copying that pattern does not cover this case. The PG15+ column-list form is available (`postgres:16-alpine` is pinned), but it needs explicit DDL and narrows portability for one nullable column. Simpler and version-portable: no FK, same-namespace enforced in `services/agent_sessions.py`, and the team-delete service path nulls `team_id` for affected sessions. A test deletes a team with a live session and asserts the session survives with `team_id IS NULL`.

**`in_flight_since` and `in_flight_trace_id` are in the Phase 1 migration**, even though only Phase 2 uses them. The draft introduced them in Phase 2 prose with no migration in Phase 2's build list, which quietly broke its own "one migration at most per phase" rule.

**`created_by_hash`, not `created_by`.** `HeaderAuthProvider` sets `caller_id = client.key_id`, and `AuthenticatedClient.key_id` returns `api_key[:8] + "..."`, the first eight characters of a live API key. `Principal`'s own docstring says never echo `caller_id` back to clients. Storing it raw on two tables and then serializing it would be a credential disclosure. It is hashed with the `_log_hash` helper already in `endpoints/auth.py:38`, named for what it is, and excluded from every response model.

And a limitation that has to be said rather than implied: for browser callers, `_authenticate_via_cookie` builds `AuthenticatedClient(api_key="")`, so `key_id` is the literal string `"***"`. The session JWT carries only `is_admin`, with no subject claim. **Nudge attribution is key-level, not user-level, and for UI callers it is effectively nothing.** Either a subject claim is added to the session JWT in Phase 1, or the plan and the UI both state that "who sent this nudge" cannot be answered. Do not ship a column that looks like it answers it.

### 7.3 `agent_session_nudges` (Phase 5)

```
id                  BIGSERIAL PRIMARY KEY
namespace_key       VARCHAR(255) NOT NULL DEFAULT 'default'
session_id          BIGINT       NOT NULL
body                TEXT         NOT NULL        -- max 2000 chars, enforced in Pydantic
status              VARCHAR(16)  NOT NULL DEFAULT 'pending'
                        -- pending | claimed | applied | expired | cancelled | rejected
created_by_hash     VARCHAR(64) NULL
created_at          TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
claimed_at          TIMESTAMPTZ  NULL
claimed_by          VARCHAR(64)  NULL            -- executor identity from the runtime token
claim_expires_at    TIMESTAMPTZ  NULL
applied_at          TIMESTAMPTZ  NULL
applied_trace_id    VARCHAR(64)  NULL
claim_count         SMALLINT     NOT NULL DEFAULT 0
injection_attempts  SMALLINT     NOT NULL DEFAULT 0

FOREIGN KEY (namespace_key, session_id) REFERENCES agent_sessions(namespace_key, id) ON DELETE CASCADE
INDEX (namespace_key, session_id, status, created_at)   idx_agent_session_nudges_drain
```

Delivery is at-least-once. A duplicate nudge means the model sees the same sentence twice, which is harmless. A dropped nudge means the human believes the agent was told something it never heard, which is the failure that destroys trust in a steering feature.

The two counters are separate on purpose. The draft had a single `attempts` incremented on every TTL reclaim, with expiry at `attempts >= 3`, while also capping injection at three per model call and holding the surplus as `claimed`. Queue ten nudges and seven get marked "undelivered" in the UI after three claim cycles without ever having been attempted, which reintroduces exactly the failure the at-least-once design exists to prevent. So: `claim_count` increments on every claim, `injection_attempts` increments only when an injection was attempted and failed, and expiry keys on `injection_attempts >= 3` alone. Surplus nudges are released back to `pending` without touching either counter. They are never held as `claimed`.

`rejected` is a terminal status for a nudge the control engine denied (see section 9.2).

### 7.4 `agent_session_halts` (Phase 5)

```
id                  BIGSERIAL PRIMARY KEY
namespace_key       VARCHAR(255) NOT NULL DEFAULT 'default'
session_id          BIGINT       NOT NULL
target_trace_id     VARCHAR(64)  NOT NULL    -- the one turn this halt is bound to
mode                VARCHAR(16)  NOT NULL DEFAULT 'graceful'  -- graceful | restart
status              VARCHAR(16)  NOT NULL DEFAULT 'pending'   -- pending | applied | expired
created_by_hash     VARCHAR(64) NULL
created_at          TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP
applied_at          TIMESTAMPTZ  NULL
applied_at_boundary VARCHAR(8)   NULL        -- model | tool | process
applied_tool_name   VARCHAR(64)  NULL        -- executor-supplied, pattern-checked at the boundary
turn_ended_at       TIMESTAMPTZ  NULL        -- when in_flight_trace_id actually cleared

FOREIGN KEY (namespace_key, session_id) REFERENCES agent_sessions(namespace_key, id) ON DELETE CASCADE
UNIQUE (namespace_key, session_id, target_trace_id)      uq_agent_session_halts_turn
INDEX (namespace_key, session_id, status, created_at)    idx_agent_session_halts_drain
```

**One row per turn, unconditionally.** Not a partial index on live statuses. A halt is a latch, so two halts against one turn are the same event, and a full unique constraint makes double-clicking idempotent by construction rather than by service logic. It also means a restart that lands on a turn with a graceful halt already recorded updates that row instead of adding a second, so the transcript can never show two markers for one stop. There is no `superseded` status because there is nothing to supersede.

**Statuses are `pending`, `applied`, `expired`, and there is no `claimed`.** Claim and apply are one transaction for a halt (section 9.4), so the window a `claimed` state would describe does not exist. `claimed_by` is dropped for the same reason and for a second one: the runtime token's subject is `(target_type="agent_session", target_id=session_key)`, so it identifies the session, not the process, and would have been constant for every claim on that session. Section 7.3 describes the nudge table's `claimed_by` as "executor identity from the runtime token" and surfaces it in the UI to distinguish a swallowed nudge from a delivered one. Under the session-bound token it cannot do that, and that description needs fixing in Phase 5 whether or not anything else here ships.

#### Why this is not a `kind` column on `agent_session_nudges`

The two look alike and behave in opposite directions on every mechanism that table has.

**Scope.** A nudge is bound to a session and lands at whatever model call happens next, possibly hours later. A halt is bound to **one turn**, `target_trace_id`, copied from `agent_sessions.in_flight_trace_id` (models.py:589) at creation and enforced on every read of it. A halt with no bound turn is exactly the design that leaks a stranger's stop into somebody else's next turn, which is the class of failure section 9.3 rejects for agent-scoped nudge delivery.

**Cardinality.** A nudge queue is a list of strings with an ordering, a three-per-model-call injection cap and a surplus-release rule. A halt is a latch, which the unique constraint states directly.

**Counters.** `injection_attempts` is meaningless for a halt, because a halt never mutates a request, it replaces a response. And expiry keyed on `injection_attempts >= 3` would be actively wrong: a halt must never be aged out by a counter, it must land or die with its turn. `claim_count` and `claim_expires_at` are absent because the TTL-reclaim story that justifies them for nudges is inverted here. A nudge whose claiming process died must be redelivered. A halt whose claiming process died **already got what the human wanted**, because the turn ended when the process did, and redelivering it would stop a turn the human deliberately started afterwards.

Sharing a table would mean six columns meaning the reverse of their names for half the rows in it.

#### Binding, and the window that matters

Creation binds to `agent_sessions.in_flight_trace_id`, **not** to `in_flight_since`. Those two columns stop being synonyms in Phase 5, and section 12's Phase 2 revision is where that lands: `in_flight_since` is the concurrency lock, `in_flight_trace_id` is the liveness marker for an invocation the executor may still be running.

The distinction is the whole feature. Phase 2 returns 504 at `timeout_seconds` and says explicitly that the invocation is still running, and its `finally` clears the in-flight state on the way out. Key halt creation on `in_flight_since` and the button becomes impossible to press at exactly T+60s, which is the single most likely moment for a person to reach for it: the UI would hide the stop control because nothing looks in flight, while tokens burn. So the two exits are separated. A turn that genuinely ended clears both columns. A handler that gave up while the invocation continues, meaning the 504 timeout and the client-abort path, clears `in_flight_since` only and leaves `in_flight_trace_id` set. No third column: an `executor_busy_trace_id` beside `in_flight_trace_id` would be a near-synonym nobody could keep straight.

Creation is one conditional statement, because read-then-write is the defect section 12 already calls out in Phase 2's guard and the same race applies here, a turn ending between the read and the insert.

```sql
INSERT INTO agent_session_halts
       (namespace_key, session_id, target_trace_id, mode, status, created_by_hash)
SELECT s.namespace_key, s.id, s.in_flight_trace_id, 'graceful', 'pending', :hash
  FROM agent_sessions s
 WHERE s.namespace_key = :ns
   AND s.session_key = :key
   AND s.in_flight_trace_id IS NOT NULL
ON CONFLICT DO NOTHING
RETURNING id
```

Zero rows is ambiguous between "nothing running" and "a halt already exists", so the service disambiguates with a read in the same transaction: an existing row returns 200 with it, and no live trace returns 409 `TURN_NOT_IN_FLIGHT`. Creator scoping from section 6.1 is applied to the session row before this runs.

`created_by_hash` uses `services/caller_identity.hash_caller_id`, and section 7.2's limitation carries over unchanged: under `HeaderAuthProvider` a browser caller hashes the literal `"***"`, so "who stopped it" is not answerable and the UI must not imply it is. Which is also why an availability-affecting action cannot rely on this row as its audit trail. Every halt creation and every executor restart emits a structured WARNING carrying namespace, agent, mode, caller hash and affected sessions, following the `actor_id_hash` line already at `endpoints/auth.py:189`. Section 11's log-hygiene rule exempts it explicitly, because it carries no content.

### 7.5 `agent_session_plan_steps` (Phase 6)

```
namespace_key   VARCHAR(255) NOT NULL DEFAULT 'default'
session_id      BIGINT       NOT NULL
plan_revision   SMALLINT     NOT NULL DEFAULT 1
step_index      SMALLINT     NOT NULL          -- 0-based, dense
title           VARCHAR(255) NOT NULL
status          VARCHAR(16)  NOT NULL DEFAULT 'pending'   -- pending|active|done|skipped|failed
note            TEXT         NULL
updated_at      TIMESTAMPTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP

PRIMARY KEY (namespace_key, session_id, plan_revision, step_index)
FOREIGN KEY (namespace_key, session_id) REFERENCES agent_sessions(namespace_key, id) ON DELETE CASCADE
```

Agents replan, so a re-declared plan writes a new revision and the UI renders the highest one. Which means `plan_revision` has to be part of the step-update contract, not inferred. If `declare_plan` creates revision 2 while a `mark_step` for revision 1 is in flight, guessing "latest" silently marks a step of the new plan done because an old plan's step finished. The revision is therefore required in `UpdatePlanStepRequest`, and a stale revision is a 409.

### 7.6 Migration hygiene

Three migrations across three phases, stacked on a base whose two most recent revisions (`d3a5c81f7b42_agent_teams.py`, `b6f1c92d4a07_team_linear_team_key.py`) are still uncommitted on this branch. That is how a branched revision head happens, and the symptom is `alembic upgrade head` failing at deploy rather than in CI.

Add `server/tests/test_alembic_single_head.py` asserting `len(ScriptDirectory.from_config(cfg).get_heads()) == 1`. About ten lines, catches the entire class, and it goes in before Phase 1. Confirm the actual head with `alembic heads` before writing each migration file rather than trusting a revision id written here.

---

## 8. Transport

Polling for everything durable, SSE for the live turn only. Websockets are rejected.

Session list, transcript history, plan state and nudge state are all React Query with `refetchInterval`, which `ui/src/core/hooks/query-hooks/` already does. SSE covers the live turn, because a 2-second poll on a streaming response is visibly wrong. It is one-directional, rides plain HTTP/1.1, and passes through the existing CORS config and the `require_operation` dependency chain without a new auth path.

Websockets are rejected for reasons specific to this codebase, not on principle. `require_operation` is built around `fastapi.Request` and would need a parallel `WebSocket` authorization path. `CORSMiddleware` does not apply to the WS handshake, so origin checking becomes hand-rolled. `PrometheusMiddleware` and `attach_version_header` do not run for WS, so every WS route silently leaves the observability and versioning stack. All of that to gain bidirectionality nobody needs, since nudges travel as ordinary POSTs.

### 8.1 Corrections to the original transport reasoning

Two of the stated blockers were wrong, and repeating them would send someone chasing infrastructure that does not exist.

**Static export is not unconditional and deep links do not need a host rewrite.** `ui/next.config.ts:3` gates `output: 'export'` on `process.env.AGENT_CONTROL_STATIC_EXPORT === 'true'`. And `resolve_ui_asset_path` in `server/src/agent_control_server/ui_assets.py` already falls back to the root `index.html` for any extensionless path with no matching file. So there is no 404 and no rewrite to configure.

There is, however, a real and separate defect hiding under that. The exported bundle contains a literal `ui/out/teams/[slug]/` directory, `ui/src/pages/teams/[slug].tsx` has no `getStaticPaths`, and the fallback serves the root `index.html`, which hydrates as the home page. So `/teams/my-team` returns 200 with the wrong content, silently, and `test_ui_assets.py` has no case covering it. That is pre-existing, it is not caused by this work, and this plan sidesteps it entirely by not adding a second dynamic route (section 8.2). Worth a small separate fix: add a `test_ui_assets.py` case pinning what `/teams/some-slug` actually resolves to.

**The EventSource rejection rationale was wrong.** The stated reason was that `EventSource` cannot send `X-API-Key`. The UI never sends `X-API-Key` at all: `ui/src/core/api/client.ts` sets `credentials: 'include'` and no key header, and grepping `ui/src` finds `X-API-Key` only inside a documentation snippet. Auth is cookie-based, which `EventSource` carries fine with `withCredentials`. The real reasons to use `fetch` plus `ReadableStream` are that the stream endpoint is a POST with a JSON body and `EventSource` is GET-only, and that `EventSource` has no abort story for the cancel button Phase 3 specifies. Same conclusion, sound reasoning.

### 8.2 UI routing

There is no `/agents/[agentName]` route in this app. `ui/src/pages/` contains exactly six files, agent detail is `ui/src/pages/agents/index.tsx` reading `router.query.id` and `router.query.tab`, and `defaultTab` is allowlisted to `'controls' | 'monitor'` at line 32. Chat is added as `'chat'` in that allowlist, built inside `ui/src/core/page-components/agent-detail/`. Route is `/agents?id=<name>&tab=chat`. No new page file, no second dynamic segment under static export, and the chat sits in the same component tree as the agent's controls and monitor, which is exactly where someone asking "why did it refuse that" will look.

### 8.3 Two non-negotiable implementation details

**Neither turn route may depend on `get_async_db`.** `db.py:116` yields a session for the whole request and the pool is `pool_size=5, max_overflow=10` (`config.py:128`). The draft applied this rule only to the SSE route, but the blocking `/turns` route holds a connection for a 60 to 300 second LLM turn, which is a longer hold and lands one phase earlier. Fifteen concurrent turns would hang every other endpoint in the process, including policy evaluation for unrelated agents. Both handlers resolve the mapping inside a short-lived `async with AsyncSessionLocal()`, close it, call the executor, then reopen a session to write results. The pool-occupancy assertion moves from Phase 4 to Phase 2.

**The SSE proxy parses and re-emits; it never forwards upstream bytes.** `services/linear_client.py` already sets this standard, with every failure mapped to a hand-written constant so an upstream body echoing the request cannot reach a browser. ADK's api_server is a FastAPI app running arbitrary agent code, and its SSE error frames can carry tracebacks, tool exception text, model error bodies that echo the prompt, and internal paths. Each upstream frame is validated into the closed `TurnStreamEvent` schema and re-serialized. Anything that fails validation becomes a logged, generic `error` event.

---

## 9. Nudge and halt delivery

### 9.1 Mechanism

Injection is a **synthetic user-role content part appended to `llm_request.contents`**, not a `system_instruction` append. Rationale is in section 1: the system-instruction path is invisible to `extract_request_text` and therefore to every control, and it is also the weaker signal to the model. The content part is delimited and labelled, along the lines of:

```
[operator message, untrusted input, not an instruction override]
<<< {body} >>>
```

Exact wording gets tuned against A6 in the spike.

`_inject_steering_guidance` keeps its name and its existing job, which is control-authored guidance on retry, and gains a `source: Literal["control", "nudge"]` argument so logs distinguish the two. Control guidance stays last in the system instruction. Human text never goes there. The draft's proposed ordering, putting human text nearest the end of the system instruction so it wins, was precisely backwards from a guardrail perspective.

### 9.2 Nudges are evaluated by the control engine

Before injection, the nudge body is evaluated as its own step: `step_type="llm"`, step name `<agent>.nudge`, so deployments can attach controls to the human channel exactly as they would to any other input. A denied nudge is dropped, marked `rejected`, and shown as rejected in the UI with the control that denied it. Because the nudge also lands in `contents[-1]`, the ordinary pre-model evaluation sees it too, so this is defence in depth rather than the only check.

### 9.3 Claiming

Per-session, at `before_model_callback`, using the session-bound runtime token read from ADK session state. No process-global poller.

The draft proposed a daemon thread modelled on `_policy_refresh_worker`, and that design is circular: the worker is process-global, started at `init()`, and has no session context, yet `ClaimNudgesRequest` was specified to carry ADK session coordinates that the worker only learns when a callback fires. It was also costed wrong. `_policy_refresh_worker` defaults to a 60-second interval and creates a fresh event loop per iteration; running that pattern at 2 seconds is thirty times the loop churn and request rate, per agent process, forever, against a deliberately small pool, with no error signal because empty polls succeed.

Claiming at `before_model_callback` resolves all of it. The session identity is known, the token is bound to that session, and a nudge is never injected into a session it was not written for. The claim-time cost is one HTTP call per model step, on a path that already makes a server call in the default server-execution mode (`evaluate_controls` posts per step), guarded by a short in-process negative cache so a session that claimed nothing a moment ago does not re-claim immediately.

Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED` so two processes serving the same agent never claim the same row.

The A1 failure mode is a hard gate, not a degradation. The draft's fallback was agent-scoped delivery, meaning a sentence typed into session A gets injected into a stranger's session B on the same agent. That is cross-conversation leakage of user-authored text presented as an acceptable outcome. **If session identity is unavailable at `before_model_callback`, nudges and graceful halts do not ship.** Executor restart (9.6) is unaffected, because it touches no callback and no session state.

**Lock order, stated once and obeyed everywhere.** Every transaction that touches both `agent_sessions` and `agent_session_halts` locks the `agent_sessions` row first. Claiming already takes `SELECT ... FOR UPDATE` on the session row and then writes the child rows; the turn-end cleanup writes both from inside a shielded `finally`. Reverse the order on one of them and Postgres deadlocks in exactly the race the design exists for, a halt claimed at the instant its turn ends, and the abort would raise inside the one code path that guarantees `in_flight_since` gets cleared, reintroducing the stuck-in-flight 409 the shield was added to prevent. The shielded cleanup body also carries a bounded retry on `DeadlockDetected` so the clear cannot be lost, and a test runs a claim and a cleanup concurrently against one session.

### 9.4 Halt delivery

Claimed at both `before_model_callback` and `before_tool_callback`, using the same session-bound runtime token as a nudge, and gated on A1 for the same reason.

**Model boundary.** `POST /agent-sessions/{key}/nudges/claim` already runs here, so `ClaimNudgesResponse` gains a `halt: ClaimedHalt | None` field rather than the SDK making a second call. Section 9.3's cost argument is about per-step HTTP volume against a deliberately small pool, and two calls for one decision at one instant contradicts it. The token, the session binding and the boundary are identical in both cases, so splitting the request separates nothing. Section 6.2 covers why there is no second operation guarding it.

If a halt comes back, the SDK returns `build_blocked_llm_response(...)` (`_extractors.py:149`). That is the identical path a control deny already takes through `_handle_llm_exception` (plugin.py, returning at :474), and Phase 2 already commits to the behaviour: the plugin returns a blocked `LlmResponse`, ADK completes the turn normally, and the transcript shows the block. A halt reuses that path rather than adding machinery.

**Tool boundary.** `POST /agent-sessions/{key}/halts/claim` returns only a halt. There is no model request to mutate at a tool boundary, so nudges are not claimed there and the call stays one per boundary.

That call is load-bearing and it is worth spelling out why, because it is the only reason a halt does what a person means by "stop". Timeline: the SDK claims nothing at t0, the model runs for thirty seconds, the human clicks stop at t15, the model returns a function call, `before_tool_callback` fires at t30. With no check there, the tool executes and only the *following* model call is blocked. The email is already sent. With the check, `build_blocked_tool_response(...)` (`_extractors.py:176`) substitutes for the result and the tool never runs.

**Claim and apply are one transaction.** The claim itself moves the row `pending -> applied`, with `applied_at_boundary` supplied in the claim request. Splitting them the way nudges do would create a failure worse than the one section 7.3 names as trust-destroying: the agent genuinely stopped, possibly before sending that email, a lost ack leaves the row to be swept `expired`, and the UI tells the operator the stop never landed, whose rational response is to reach for the restart button on an agent that is already stopped. There is no useful window between claim and apply here anyway, because the SDK returns the blocked response synchronously in the same callback, and one-agent-per-process (`_state.py` singleton, plugin.py:84 to :90) means no second consumer can race for the row. Ack survives only as optional enrichment carrying `applied_tool_name`, and losing it costs one word of transcript copy rather than the truth of the record.

`applied_tool_name` is the one field in this design carrying executor-chosen bytes, arriving from a process running arbitrary agent code and landing in an operator console. It is pattern-checked server-side against a strict identifier and capped at 64 characters, following the hand-written-constant discipline in `services/executor_client.py`, and it renders under Phase 3's plain-text rule.

**The latch is per invocation, not per process.** A blocked tool result does not end an invocation; the agent receives `{"status": "blocked", ...}` and calls the model again. So a halt claimed at a tool boundary is held in process memory until the next model callback, which then returns the blocked `LlmResponse` with no second network call. That memory is a dict keyed by ADK invocation id, following `_current_llm_call_ids` and the other invocation-keyed dicts in `plugin.__init__`, evicted the moment the model-boundary block fires, and bounded so an abandoned invocation cannot leak an entry. A flag on the plugin instance would be process-global, and one executor process serves one agent across many concurrent sessions, so a process-global latch turns one `AUTHENTICATED` halt on one session into a halt of every concurrent session that agent is running: the cheapest possible cross-user denial of service, needing no admin key and no restart path. It would also never clear when the halting invocation ends at a tool boundary and the process is reused, leaving the agent blocked until somebody restarts it.

Cost of the sticky latch: one extra model call after a tool-boundary halt. That is the price of ADK exposing no cancel primitive, it is bounded and visible, and Phase 0 question A9 may remove it.

**The halt check sits at the top of each callback, immediately after the `enabled_hooks` guard, and does not go through `_handle_llm_exception` or `_handle_tool_exception`.** Both of those run `_invoke_callback`, which fires a deployment's `on_violation_callback` with a deny action, and both push the message through `blocked_message_template`. They exist to describe guardrail decisions. Reporting a human pressing stop as a control violation would corrupt whatever a deployment wired into that callback and would render the halt to the model in the deployment's denial voice. The halt path calls the builders directly with its own constant. Placing it first also means no `_register_llm_request` bookkeeping has happened yet, so there is no `_clear_pending_llm_state` cleanup to remember.

Delivery requires `before_model` and `before_tool` in `enabled_hooks`. A deployment that disabled either silently narrows where a stop can land, so `bind()` logs a warning naming the boundaries that will not stop.

### 9.5 Precedence: a halt beats a queued nudge, and the nudge is not consumed

Enforced server-side in the claim transaction. A halt for the in-flight trace means the claim returns the halt and **zero nudges**, and neither `claim_count` nor `injection_attempts` moves on anything.

The alternative breaks the one promise the nudge design exists to keep. A nudge claimed and injected into a request whose response is about to be replaced by a halt block gets marked `applied` while no model ever read it, and section 7.3 is explicit that a dropped nudge presented as delivered is the failure that destroys trust in the feature.

Belt and braces on the SDK side, for a halt created between the claim and the injection: any nudge already claimed in that request is acked as `released`, the surplus-release path 7.3 already specifies, counters untouched.

A nudge created after the halt lands stays `pending` and applies to the next turn. That is correct, and it is how "stop it, then tell it something else" works without new machinery: once a halt lands, the turn is over, `in_flight_since` is clear, and the next `POST /turns` is an ordinary turn.

**A halt is unclaimable outside its own turn, by construction.** The claim query joins the session row and requires `agent_session_halts.target_trace_id = agent_sessions.in_flight_trace_id`. Prose alone would not be enough: a halt whose replica died sits in the table, and without that predicate the next turn's first model callback would claim it, silently killing a turn the human deliberately started afterwards, under a transcript marker blaming an operator. Under the default provider that would also be cross-principal, since halt creation is creator-scoped but delivery is not.

**Expiry is an event, and the event is the next acquire.** When the turn's in-flight state clears, the halt for that `target_trace_id` is stamped `turn_ended_at` and, if still `pending`, moved to `expired`, from the same shielded `finally`, fenced on the trace the handler owns. That covers the ordinary case. It does not cover replica death, and an earlier draft claimed it did by asserting that "Phase 2's staleness predicate reclaims the session and the same sweep expires the halt". There is no sweep. Phase 2's staleness handling is a `WHERE` clause inside the acquire `UPDATE`, not a background job, and it touches no halt row. So halt expiry also runs inside the acquire transaction, which is the one statement guaranteed to execute before any subsequent turn:

```sql
UPDATE agent_session_halts
   SET status = 'expired', turn_ended_at = COALESCE(turn_ended_at, now())
 WHERE namespace_key = :ns AND session_id = :id
   AND status = 'pending'
   AND target_trace_id <> :new_trace
```

No new sweeper process, and the replica-death case closes by construction rather than by assertion.

**`applied` alone does not render as stopped.** The ack is attested by the process being stopped, which is section 6.2's whole worry about machine-side actors marking work delivered, and here the consequence is the side effect the human was trying to prevent. So the UI's terminal state derives from the turn actually ending, which the server independently knows: `turn_ended_at` set, meaning `in_flight_trace_id` cleared for that trace. `applied` with the turn still live renders "stop acknowledged, waiting for the turn to end", and after a bounded interval renders a warning. `agent_control_halts_applied_still_in_flight_total` counts the gap, and a test asserts that acking a halt does not by itself move the session out of in-flight. A stop button that can report success without a stop is worse than no stop button.

### 9.6 Executor restart

Graceful halt is gated on A1. Restart is gated on nothing: no callback, no session state, no runtime token.

**It is not a session operation and it does not live on a session route.** An earlier draft put it at `POST /agent-sessions/{key}/halts/force` under a new `AGENT_HALTS_FORCE` ADMIN operation. Both were wrong. The blast radius is every in-flight turn in that process while the resource identifier named one session, which misrepresents what the endpoint does, and the stated justification for the new operation ("it acts on deployment infrastructure, so it sits at the `AGENT_RUNTIMES_WRITE` tier") is an argument for *reusing* `agent_runtimes.write`, not for minting a second ADMIN operation with identical semantics. So: `POST /agent-runtimes/{agent_name}/restart`, `agent_runtimes.write`, resource matching radius, no new operation.

**Mechanism.** The executor container's entrypoint already runs a DB preflight (section 4). It becomes a supervisor that execs `adk api_server` as a child and serves one route on `ExecutorSettings.supervisor_port`, unpublished, on the internal network: `POST /_supervisor/restart`. Because the SDK holds one agent per process, that ends exactly one agent and touches nobody else's controls, caches or sessions.

**The credential is per executor and derived, never the deployment-wide executor secret.** `ExecutorSettings.shared_secret` is a single `SecretStr` (`config.py`) sent on every executor request (`services/adk_executor_client.py`, `self._headers`). Verifying it at the supervisor would mean every executor container holds it, and the executor is the process running model-driven agent code. `server/.env.example` already states in writing that an agent with any HTTP-egress tool is an SSRF pivot onto the network its own executor sits on. A prompt-injected agent that reads its own environment could then POST `/_supervisor/restart` at every sibling executor on that segment, in any namespace, and kill them all: agent-initiated denial of service across the whole deployment, with no operator key and no ADMIN gate crossed. That inverts section 5's argument for the topology, and it hands the executor a privilege it does not have today, where the worst it can do with that secret is impersonate the server to another ADK port that is unauthenticated anyway.

So a second, separate root secret, `AGENT_CONTROL_EXECUTOR_SUPERVISOR_SECRET`, never sent on ordinary executor calls. Each executor is provisioned with `HMAC(root, f"{namespace_key}:{agent_name}")` and nothing else; the root stays on the server, which can derive any agent's value on demand. The supervisor constant-time compares against its own single value. An injected agent can therefore restart itself, which is equivalent to crashing itself and is acceptable, and cannot restart anyone else. `.env.example` says exactly that, adds executor-to-executor traffic on the internal segment to the deny list beside the existing SSRF note, and states that the supervisor port is reachable only from the server. A test asserts the supervisor secret never appears in `AdkExecutorClient._headers`.

**Exclusive binding is required and checked at request time.** `agent_runtimes` is keyed `(namespace_key, agent_name)` with no uniqueness on `base_url` (models.py:469), and `validate_executor_base_url` checks scheme, host, credentials and query, not exclusivity. So two agents or two namespaces can legitimately share an executor, and restarting on behalf of one silently ends the other's turns with no halt row, no transcript marker and no explanation, performed by a principal who may not be able to read that namespace at all. Before dispatching, the endpoint counts runtime rows sharing the resolved `base_url` **across all namespaces** and refuses with a written 409 `EXECUTOR_SHARED` when there is more than one, naming exclusive binding as the requirement. `PUT /agent-runtimes/{agent_name}` warns at write time on the same condition rather than only at restart. Derived per-agent secrets make a mis-aimed restart fail closed on top of that, since one agent's value is not accepted by another agent's supervisor.

**Rejected: mounting the Docker socket into the server container.** That gives a stateless control-plane app root-equivalent control of the host, reachable from every endpoint in it, to implement one button.

**Rejected: orchestrator APIs.** Deployment-specific, needs a service account with delete rights, turns the control plane into a cluster actor. The supervisor behaves identically under compose and Kubernetes.

**SIGKILL, not graceful shutdown.** SIGTERM to `adk api_server` starts uvicorn's graceful shutdown, which waits for in-flight requests to finish, and the in-flight request is the turn being killed. Any sane-looking grace window is then spent waiting for precisely the thing the operator asked to stop, and a 300-second tool makes the whole window dead time before the SIGKILL that was always coming, with a stop button that appears to hang. The child gets SIGKILL after a short flush window whose only job is letting `DatabaseSessionService` write what it has. That window is a named constant tied to the Phase 0 A3b finding, not a number somebody picked.

**Recovery of Phase 2 in-flight state, which is the part people get wrong.** The restart endpoint **does not clear `in_flight_since`**. The turn handler owns that lock. Killing the executor makes the handler's in-flight `httpx` call fail, which raises inside its `try`, and the shielded `finally` clears the session's in-flight state from its own short-lived session, fenced on its own trace. A second writer clearing the lock from the restart endpoint would let a new turn start while the dying handler is still writing results for the old one. "The stop button should clear the stuck flag" is the obvious instinct and it is wrong. This holds across replicas with no coordination: the handler fails wherever it runs, and if that replica also dies, the acquire's staleness predicate reclaims the row. Note this is a constraint Phase 5 places on Phase 2's `finally`, not a description of code that exists; section 12's Phase 2 entry carries it.

**Halt rows written by a restart are terminal on insert**, `mode='restart'`, `status='applied'`, `applied_at_boundary='process'`, upserted on the one-row-per-turn constraint so a graceful halt already recorded for that turn is overwritten rather than duplicated. Insert them `pending` and the expiry rule marks them `expired`, whose copy is "the turn ended before the stop was applied": the one mechanism that unambiguously worked would render as the one that did not, and the operator would be invited to press it again. Enumerating affected sessions is racy by nature, since a turn can start between enumeration and kill, so those rows are best-effort annotation. The authoritative record of an abnormally ended turn is the turn's own outcome, written by the handler's fenced `finally`, so a session missed by the enumeration still renders correctly.

**The transcript records nothing on the executor side.** A graceful halt leaves a model-role message in ADK's session store. A restart truncates the invocation: whatever `DatabaseSessionService` persisted survives, the final model response does not exist, and the last event may be a function call with no response. Two consequences. The UI renders halt markers from `agent_session_halts` and never by pattern-matching transcript text, which is also true for graceful halts because ADK cannot distinguish a blocked `LlmResponse` from ordinary model output. And the first turn after a restart resumes from a session whose last event may be a dangling function call, which nobody has observed, so it goes into the spike as A2's extension.

**Restart window.** The executor is down for its cold start, and `plugin.bind()` runs `_sync_steps_blocking(raise_on_error=True)`. Phase 1's bounded retry around it is load-bearing here: without it, restarting while the control plane is momentarily slow leaves the agent down instead of restarted.

**The supervisor client is built like `AdkExecutorClient`.** Explicit `httpx.Limits` and timeout, redirects off, and no upstream bytes in any response, per section 8.3. Unreachable or non-2xx is `EXECUTOR_UNAVAILABLE` 503 with hand-written text.

---

## 10. Endpoints

All under `/api/v1`. Routers registered in `main.py` with `dependencies=[Depends(get_api_key_from_header)]`, matching `main.py:287`.

### `endpoints/agent_runtimes.py`

```
GET    /agent-runtimes?agent=                     -> ListAgentRuntimesResponse    agent_sessions.read
PUT    /agent-runtimes/{agent_name}   UpsertAgentRuntimeRequest -> AgentRuntimeResponse   agent_runtimes.write
DELETE /agent-runtimes/{agent_name}               -> DeleteAgentRuntimeResponse   agent_runtimes.write
POST   /agent-runtimes/{agent_name}/restart  RestartExecutorRequest -> RestartExecutorResponse   agent_runtimes.write   (Phase 5)
```

`restart` returns `affected_session_keys`, so the blast radius appears in the payload rather than in a docstring. Its refusals: 409 `EXECUTOR_NO_SUPERVISOR` when `supervisor_port` is unset, 409 `EXECUTOR_SHARED` when more than one runtime row across all namespaces resolves to the same `base_url`, 503 when `api_key_enabled` is false (section 6.4), and 503 `EXECUTOR_UNAVAILABLE` with no upstream bytes when the supervisor does not answer. Restarting an executor with nothing in flight is allowed, because a wedged process is a legitimate thing to restart; it writes no halt rows and says so.

### `endpoints/agent_sessions.py`

```
POST   /agent-sessions                            CreateAgentSessionRequest -> CreateAgentSessionResponse   agent_sessions.write
GET    /agent-sessions?agent=&team=&status=&limit=&cursor=  -> ListAgentSessionsResponse                    agent_sessions.read
GET    /agent-sessions/{session_key}              -> GetAgentSessionResponse                                agent_sessions.read
PATCH  /agent-sessions/{session_key}              PatchAgentSessionRequest -> PatchAgentSessionResponse     agent_sessions.write
DELETE /agent-sessions/{session_key}              -> DeleteAgentSessionResponse                             agent_sessions.write
GET    /agent-sessions/{session_key}/messages?after_index=&limit=  -> ListSessionMessagesResponse           agent_sessions.content_read
POST   /agent-sessions/{session_key}/turns        StartTurnRequest -> TurnResponse                          agent_sessions.run
POST   /agent-sessions/{session_key}/turns/stream StartTurnRequest -> text/event-stream                     agent_sessions.run
GET    /agent-sessions/executor-health            -> ExecutorHealthResponse                                 agent_sessions.read
```

`?team=` reuses the slug filter already shipped on `GET /agents`. `DELETE` is a hard delete: local row plus `ExecutorClient.delete_session`. When the executor delete fails, the row moves to `orphaned_pending_delete` and is retried, never silently reported as success.

### `endpoints/agent_nudges.py`

```
POST   /agent-sessions/{session_key}/nudges       CreateNudgeRequest -> CreateNudgeResponse                 agent_nudges.write
GET    /agent-sessions/{session_key}/nudges?status=  -> ListNudgesResponse                                  agent_sessions.content_read
DELETE /agent-sessions/{session_key}/nudges/{nudge_id}  -> CancelNudgeResponse                              agent_nudges.write
POST   /agent-sessions/{session_key}/nudges/claim ClaimNudgesRequest -> ClaimNudgesResponse                 agent_nudges.consume  (token-bound)
POST   /agent-sessions/{session_key}/nudges/ack   AckNudgesRequest -> AckNudgesResponse                     agent_nudges.consume  (token-bound)
```

Claim and ack sit under the session path so the `context_builder` can pluck `session_key` straight from path params, mirroring `_exchange_context` in `endpoints/auth.py`. `DELETE` cancels a `pending` nudge only; cancelling a `claimed` one is a 409, because the guidance may already be inside a model request and pretending otherwise would be a lie. `ClaimNudgesResponse` also carries `halt: ClaimedHalt | None` (section 9.4).

### `endpoints/agent_halts.py`

```
POST   /agent-sessions/{session_key}/halts        CreateHaltRequest -> CreateHaltResponse                  agent_halts.write
GET    /agent-sessions/{session_key}/halts?status=  -> ListHaltsResponse                                   agent_sessions.content_read
POST   /agent-sessions/{session_key}/halts/claim  ClaimHaltRequest -> ClaimHaltResponse                    agent_nudges.consume  (token-bound)
POST   /agent-sessions/{session_key}/halts/ack    AckHaltRequest -> AckHaltResponse                        agent_nudges.consume  (token-bound)
```

Reads are `content_read` rather than `agent_sessions.read` because `applied_tool_name` names a tool the agent was about to run, and handing that to a caller who was refused content access is the small inventory disclosure the split in 6.1 exists to prevent. `POST /halts` is 409 `TURN_NOT_IN_FLIGHT` when no live trace exists, 200 with the existing row when one is already recorded for that turn, 403 for a session opened by another caller, 404 across namespaces, and 429 on the shared quota bucket. There is no force route here; restart lives on the runtime binding, for the reasons in 9.6.

### `endpoints/agent_plans.py`

```
GET    /agent-sessions/{session_key}/plan         -> PlanResponse                                           agent_sessions.content_read
PUT    /agent-sessions/{session_key}/plan         DeclarePlanRequest -> PlanResponse                        agent_plans.write  (token-bound)
PATCH  /agent-sessions/{session_key}/plan/revisions/{plan_revision}/steps/{step_index}
                                                  UpdatePlanStepRequest -> PlanResponse                     agent_plans.write  (token-bound)
```

The agent's tools get `session_key` and the runtime token without a lookup: Agent Control seeds the ADK session `state` at creation with `{"agent_control": {"session_key": ..., "namespace_key": ..., "agent_name": ..., "trace_id": ..., "runtime_token": ...}}`, and `tool_context.state` / `callback_context.state` read it back. That single design detail carries nudges, plans and trace propagation, using public ADK surface rather than reaching into `_invocation_context`. It is Assumption A1 and it gates three phases.

---

## 11. Shared models, services, config

**Pydantic** in `models/src/agent_control_models/`: `sessions.py` (`AgentSessionSummary`, `AgentSessionDetail`, `SessionMessage`, `SessionMessagePart`, `TurnRequest`, `TurnResponse`, `TurnStreamEvent`), `nudges.py`, `plans.py`, `halts.py`, `agent_runtimes.py`. All request models use `ConfigDict(extra="forbid")`. Exported from `__init__.py`. New `ErrorCode` members in `errors.py`: `AGENT_SESSION_NOT_FOUND`, `AGENT_RUNTIME_NOT_BOUND`, `NUDGE_NOT_FOUND`, `EXECUTOR_UNAVAILABLE`, `EXECUTOR_REJECTED`, `TURN_IN_FLIGHT`, `QUOTA_EXCEEDED`, and in Phase 5 `TURN_NOT_IN_FLIGHT`, `EXECUTOR_NO_SUPERVISOR`, `EXECUTOR_SHARED`, each with a title in the `_ERROR_TITLES` map. Several of these already exist on this branch from Phase 1; confirm before adding.

**Services** in `server/src/agent_control_server/services/`:

- `executor_client.py`: `ExecutorClient` Protocol (`create_session`, `get_session`, `delete_session`, `run`, `run_stream`, `health`) plus `AdkExecutorClient`, taking a per-agent `base_url` resolved from `agent_runtimes`. Modelled on `linear_client.py`: the only module that knows ADK is an HTTP service, secrets in one attribute and one header, error text written by hand. Explicit `httpx.Limits` configured, unlike `HttpLinearClient` which sets none.
- `agent_sessions.py`, `nudges.py`, `plans.py`, `halts.py`, `agent_runtimes.py`. Every method takes `namespace_key`.

**Config**: `ExecutorSettings` with `env_prefix="AGENT_CONTROL_EXECUTOR_"`, mirroring `LinearSettings`: `enabled: bool = False`, `shared_secret: SecretStr`, `timeout_seconds`, `stream_idle_timeout_seconds`, `max_stream_seconds`, `max_concurrent_streams`, `max_turns_per_minute`, `max_concurrent_sessions`, and in Phase 5 `supervisor_port: int | None = None`, `supervisor_secret: SecretStr` (the root from which per-agent values are derived, see 9.6) and `supervisor_kill_grace_seconds`. Documented in `server/.env.example` under a new `# Agent executor #` block, immediately above the auth block, with the auth dependency from section 6.4 spelled out. Default off, so every phase is inert for existing deployments until they opt in.

**Rate limiting has to exist before the endpoint is reachable, not after the first bill.** Grepping `server/src` finds no rate limiting anywhere, only handling of being rate-limited by Linear. `POST /turns` is the first endpoint in this product that costs money per call. Per-session 409 does not help, because you can create N sessions and run N turns. Per-principal quotas keyed on `(namespace_key, created_by_hash)` returning 429 with a typed error, in Phase 2.

**Content is never logged above DEBUG.** Nudge bodies, turn text and model output do not appear in server or SDK logs at INFO. A test asserts a nudge body is absent from captured log output, mirroring the discipline in `linear_client.py`. One named exemption: the halt-creation and executor-restart WARNING lines from section 7.4 carry namespace, agent, mode, caller hash and affected session keys, and no content at all. They exist because an availability-affecting action whose only actor field hashes to `"***"` for every browser caller has no audit trail otherwise.

---

## 12. Phases

Each phase is one branch and at most one migration. Every phase that touches routes must also regenerate three artifacts, not one:

```
make openapi-spec
cd ui && npm run fetch-api-types          # ui/src/core/api/generated/api-types.ts
make sdk-ts-generate                       # sdks/typescript/src/generated
# then add naming-overlay rules and verify:
make sdk-ts-generate-check                 # gated in CI: sdk-ts-ci job
make sdk-ts-name-check                     # gated in CI: fails on Get/Post/Put/Patch/Delete suffixes
```

The draft mentioned only the UI types, which is the one target CI does *not* gate. `.github/workflows/ci.yml` runs `make sdk-ts-generate-check` and `make sdk-ts-name-check` in a `sdk-ts-ci` job, and regeneration needs the pinned Speakeasy CLI plus `SPEAKEASY_API_KEY`, so whoever hits the red build cannot fix it without setup. Budget two days per gated phase for overlay and name-check churn.

---

### Phase 0: spike and decision gate
**1 week. No shippable artifact. Blocks everything.**

Install `google-adk` into a scratch venv (it is an optional extra, `sdks/python/pyproject.toml:40`, and `import google.adk` currently fails in this environment). Stand up `adk api_server` against Postgres, point `examples/google_adk_plugin/my_agent` at it with a Gemini API key, and answer in writing:

- **A0** Confirm the one-agent-per-process constraint end to end, and confirm two executor processes for two agents against one Agent Control server behave correctly (separate control caches, no cross-talk). This is the topology the whole plan now rests on.
- **A1** Does `CallbackContext.state` expose session state seeded at session creation? Does `ToolContext.state`? If neither, **Phase 5 and Phase 6 do not ship** and the nudge design is re-costed from scratch.
- **A1b** How is a session-bound runtime token refreshed inside a long-running ADK session? Confirm the plugin can re-exchange through `RuntimeTokenCache` when the seeded token expires.
- **A2** Exact request and response shapes of `POST /run`, `POST /run_sse` and the session CRUD routes on the pinned version. Capture real payloads into `server/tests/fixtures/adk/`. Also capture what ADK does when a session's most recent event is a function call with no response, since that is the exact state a restart-truncated session is resumed from.
- **A3** Does `DatabaseSessionService` accept `postgresql+asyncpg://`, or does it need the sync `psycopg` driver this repo already uses? Record the exact URL string that worked. Section 5's framing treats persistent sessions as settled, and it is the one thing that makes Option A workable without Vertex, so it gets confirmed rather than assumed.
- **A3b** At what granularity does `DatabaseSessionService` persist events during an invocation, per event or at invocation end? This decides how much of a killed turn survives in the transcript, therefore what the UI is allowed to claim after an executor restart, and therefore the flush window before SIGKILL in section 9.6. If it flushes only at the end, a restart loses the whole turn and the copy has to say so.
- **A4** Do the four plugin callbacks still fire correctly under `adk api_server`? They are currently exercised only in-process. **Extended for Phase 5**: confirm under a real `adk api_server`, not in-process, that returning an `LlmResponse` from `before_model_callback` ends the invocation, and that returning a dict from `before_tool_callback` prevents the tool from executing. Phase 2 already leans on the first. Halt delivery leans on both, and they are behavioural contracts rather than attribute existence, so no amount of reading signatures settles them.
- **A5 (rewritten)** Streaming behaviour, measured against a **real uvicorn server**, not `TestClient`. Three checks: does a client abort mid-stream cancel the upstream httpx request and release the task, with no leaked task and flat `pool.checkedout()`; does the stream path need adding to `PROMETHEUS_SKIP_PATHS`; and buffering, which is the cheap one. The original A5 asked only about buffering and pre-committed a fix (converting `attach_version_header` off `BaseHTTPMiddleware`) for a failure that measurement suggests does not occur on the installed starlette 1.3.1 / fastapi 0.141.1. Chasing a phantom for three days while the disconnect-leak and metrics-pollution problems go unspiked is how the lowest-confidence phase eats a week.
- **A6** Behavioural test by hand: mid-conversation, append an operator sentence as a user-role content part and see whether the model actually changes course. Compare against the system-instruction append for reference. This is the product risk, not an engineering one, and it is the reason the delivery mechanism changed.
- **A7** Trace propagation. Confirm the pinned ADK run request accepts `state_delta`, or that session `state` seeded at creation is readable from `CallbackContext` inside the same invocation (same experiment as A1). The plugin then opens `with_trace(...)` per invocation, or passes `trace_id` explicitly, both of which are public SDK surface: `set_trace_context` is exported from `tracing.py:179` and `evaluate_controls` takes `trace_id` and `span_id` parameters. If no channel exists, Phase 2 drops the turn-to-trace deep link from its deliverable rather than shipping a link that resolves to a single hop.
- **A9** Does the pinned version expose an invocation-ending signal reachable from `ToolContext` or `CallbackContext`, an escalate or end-invocation flag on the actions object? If yes, a tool-boundary halt ends the turn without the extra model call in 9.4. If no, the sticky latch is the shipped behaviour and stopping an agent at a tool boundary costs one round trip of spend. Half a day, and it decides that.
- **A8** Generate a throwaway `text/event-stream` route into the OpenAPI spec and run `make sdk-ts-generate` plus `make sdk-ts-name-check` against it. Nothing in `gen.yaml` configures event streaming. If Speakeasy cannot represent it, mark the stream route `include_in_schema=False` and document it as a UI-only route. Decide this in week one, not in week eight with the server work already done.

**Also built in Phase 0, because later phases cannot be tested without them:**

- A `live_server` fixture in `server/tests/conftest.py`: `uvicorn.Server` on an ephemeral port in a background asyncio task, plus `httpx.AsyncClient`. Both dependencies are already present (uvicorn is a server runtime dep; `asyncio_mode = "auto"` is already set; tests run serially with no xdist so port allocation is safe). **Rule for the whole plan: no streaming assertion may use `TestClient` or `httpx.ASGITransport`.** Both buffer the entire response, so frame ordering, heartbeats, idle timeout, terminal error frames, client abort and pool occupancy are all unassertable through them, and running A5 through `TestClient` would report a false failure. The same fixture doubles as the fake ADK server for proxy-level failure injection.
- `server/tests/test_alembic_single_head.py`.

**Deliverable:** a findings note, the captured ADK fixtures, the `live_server` fixture, and the single-head test.

---

### Phase 1: session registry, executor adapter, runtime bindings
**2 weeks. Depends on Phase 0.**

Build: `AgentRuntime` and `AgentSession` ORM plus one migration; `models/.../sessions.py` and `agent_runtimes.py`; `ExecutorClient` Protocol and `AdkExecutorClient` (session CRUD, history and health, no run); `services/agent_sessions.py` and `services/agent_runtimes.py`; `endpoints/agent_sessions.py` (everything except `/turns*`) and `endpoints/agent_runtimes.py`; six `Operation` members registered (`AGENT_SESSIONS_READ`, `AGENT_SESSION_CONTENT_READ`, `AGENT_SESSIONS_WRITE`, `AGENT_SESSIONS_RUN`, `AGENT_RUNTIMES_WRITE`, and the token-bound `AGENT_NUDGES_CONSUME` and `AGENT_PLANS_WRITE` wired through `set_authorizer(..., operation=...)`); session-bound runtime token minting at session creation; `ExecutorSettings` and the `.env.example` block; both startup refusals from section 6.4; `docker-compose.dev.yml` executor service with the `adk` Postgres role, `REVOKE CONNECT`, the DB preflight, a `healthcheck` on the `server` service and `depends_on: condition: service_healthy` on the executor; a bounded retry around `plugin.bind()` in the example agent so a cold start does not crash-loop against `_sync_steps_blocking(raise_on_error=True)`; all three generated artifacts regenerated.

**Shippable as:** `curl` binds an agent to an executor, creates a session, reads back an empty transcript. No UI.

Failure and edge cases. Executor disabled or unreachable returns a typed `EXECUTOR_UNAVAILABLE` 503 with a hand-written message, never a 500 and never an upstream body, following `linear_milestones.py`. Agent with no enabled runtime binding is a 409 before touching the executor. Agent not registered in `agents` is a 404 before that. ADK session created but the local INSERT failed compensates by calling delete on the executor, and if that fails too it logs and leaves an orphan that nothing can address. Local row exists with the ADK session gone marks the row `orphaned` and renders an empty transcript with an explicit banner, not an error page. A `session_key` from namespace A read under namespace B is a 404, matching teams. Deleting a team with live sessions nulls `team_id` in the service and the sessions survive.

Tests: `test_agent_sessions_endpoints.py` (CRUD, pagination, `?agent=` and `?team=`, against a fake `ExecutorClient` in the `LinearClient` fake style), `test_agent_sessions_models.py` (cascade, namespace uniqueness, and the global executor-triple constraint asserting `IntegrityError` across namespaces), `test_agent_sessions_alembic_migration.py` mirroring `test_teams_alembic_migration.py` (upgrade, downgrade, residue sweep, upgrade-downgrade-upgrade), `test_agent_sessions_auth.py` (restricted-authorizer 403 per operation plus a token-bound cross-session 403), `test_agent_runtimes_endpoints.py`, and `test_namespace_isolation.py` extended. `test_auth_framework.py` passing unchanged proves the new operations are registered.

---

### Phase 2: blocking turn execution
**2 weeks. Depends on Phase 1. No migration.**

Build: `ExecutorClient.run()` proxying `POST /run`; `POST /agent-sessions/{session_key}/turns` under `agent_sessions.run`; per-turn trace id minted server-side, seeded into ADK session state, picked up by the plugin via `with_trace` / `set_trace_context` (subject to A7), written to `last_trace_id` and returned in `TurnResponse` so the UI can deep-link to the existing `GET /observability/traces/{trace_id}`; per-principal quotas; the short-lived-session rule; the metrics from section 13; all three generated artifacts.

The concurrency guard is a single atomic statement, not a read then a write:

```sql
UPDATE agent_sessions
   SET in_flight_since = now(), in_flight_trace_id = :trace
 WHERE namespace_key = :ns
   AND session_key = :key
   AND (in_flight_since IS NULL OR in_flight_since < now() - :stale)
RETURNING id
```

Zero rows means 409. The draft's SELECT-then-UPDATE version fails precisely under the concurrency it exists for. A timestamp plus a staleness heuristic is still an unfenced lock across replicas, so the update runs inside a transaction that also takes `SELECT ... FOR UPDATE` on the session row, which is the same tool the nudge claim uses.

Clearing the in-flight state runs in a `finally` wrapped in `asyncio.shield` inside `try/except CancelledError`, opening its own short-lived session. Without the shield, a client closing the tab mid-turn raises `CancelledError` before the clear lands, and the session sits stuck in flight, 409-ing every subsequent turn until the staleness heuristic expires. Closing a tab mid-response is normal behaviour, not an edge case, and the user-visible symptom ("I can't send another message") looks nothing like the cause.

**Three properties of that cleanup are load-bearing for Phase 5 and have to be built here, not retrofitted.**

*Fenced on the trace the handler owns.* `... WHERE namespace_key = :ns AND id = :id AND in_flight_trace_id = :my_trace`. The acquire above deliberately permits stale takeover, so turn A can be reclaimed while still running and turn B can acquire; an unfenced clear in A's late `finally` then releases B's lock and turn C starts concurrently with B, defeating the single guarantee this statement exists to provide. Phase 5's executor restart makes that common rather than theoretical, because it kills handlers mid-turn on purpose. A test asserts that a late cleanup from a reclaimed turn does not clear a successor's lock.

*Two exits, not one.* A turn that genuinely ended clears `in_flight_since` **and** `in_flight_trace_id`. A handler that gave up while the invocation continues, the 504 timeout and the client-abort path, clears `in_flight_since` only and leaves `in_flight_trace_id` set. The lock releases so the caller is not stuck; the liveness marker stays so the system still knows an invocation is running. Section 7.4 explains why: bind Phase 5's stop button to `in_flight_since` and it is disabled at exactly T+60s, the moment a person most wants it, while tokens burn behind a UI showing nothing in flight.

*Retryable and ordered.* The cleanup body is structured so a bounded retry can wrap it, and it locks `agent_sessions` before any child table. Phase 5 adds `agent_session_halts` writes to both this `finally` and the acquire transaction, and section 9 fixes the lock order globally; a deadlock abort inside a shielded `finally` is the worst possible place to raise, because that is the code path guaranteeing the lock gets released.

Other failure cases. A turn exceeding `timeout_seconds` returns 504 with `EXECUTOR_UNAVAILABLE` and says explicitly that the invocation is still running and its events will appear in the next history read; once Phase 5 ships, that copy also says the turn can still be stopped. A control denying the first model call means the plugin returns a blocked `LlmResponse`, ADK completes the turn normally, and the transcript shows the block; that is correct and needs a test asserting it. Missing or quota-exhausted model key on the executor surfaces as `EXECUTOR_REJECTED` 502 with a written message.

Tests: `test_agent_session_turns.py` (happy path, timeout, `EXECUTOR_REJECTED` mapping, trace round-trip, quota 429, control-block transcript), a concurrency test firing two turns at one session asserting exactly one 200 and one 409, an abort test asserting the next turn returns 200 rather than 409, a fencing test where a reclaimed turn's late cleanup leaves the successor's lock intact, a test that the timeout path leaves `in_flight_trace_id` set while clearing `in_flight_since`, and a pool test on the `live_server` fixture asserting `async_engine.sync_engine.pool.checkedout()` is unchanged from idle baseline while three concurrent turns are in flight. One integration test, marked and skipped by default, runs against a real `adk api_server` using the Phase 0 fixtures as its baseline.

---

### Phase 3: UI chat panel
**1.5 weeks. Depends on Phase 2.**

Build: `'chat'` added to the `defaultTab` allowlist in `ui/src/pages/agents/index.tsx`; `ui/src/core/page-components/agent-detail/agent-chat/` with `agent-chat.tsx`, `message-list.tsx`, `message-composer.tsx`, `session-switcher.tsx` and a CSS module, Mantine 7.17 matching the teams look; `use-agent-sessions.ts`, `use-session-messages.ts` (`refetchInterval: 2000` while a turn is in flight, off otherwise), `use-start-turn.ts`; a chat affordance per agent on `/teams/[slug]`.

**Shippable as:** the feature the user asked for, minus streaming and steering. A human talks to an agent and sees the reply.

**Message parts render as plain text with `white-space: pre-wrap`. No markdown, no HTML, in the first version.** This is the first untrusted content this UI has ever rendered. Grepping `ui/src` and `ui/package.json` finds no `dangerouslySetInnerHTML`, no `react-markdown`, no `marked`, no `DOMPurify`; everything rendered today is server-controlled configuration. An agent that fetches an attacker-controlled page and echoes it becomes stored XSS in an authenticated operator console where every admin endpoint is same-origin and reachable from script. If markdown is added later: allowlist sanitizer, raw HTML disabled, non-http(s) URL schemes blocked, `rel="noopener noreferrer"` on links, and no auto-loading remote images (which beacon transcript-view events to a third party).

Other cases. A 60-second turn disables the composer, shows elapsed time, and offers a cancel that abandons the request while saying plainly that the turn keeps running server-side. When Phase 5 ships, that control is **relabelled rather than removed**: "Stop responding" creates a halt, "Stop waiting" abandons the request and still says the turn continues. Deleting the second one on the grounds that two buttons where one of them lies is worse than either alone gets the diagnosis right and the treatment wrong. The cure for a misleading label is a label. A halt cannot land while a 300-second tool is executing, which section 2 concedes is real, and in that window an operator with only a halt button watches a control that visibly does nothing with no way off the screen. "Stop waiting" appears only once a halt has been pending longer than a short threshold, so it shows up exactly when the halt is visibly not landing. A 503 renders an inline banner in the panel, not a toast that scrolls away. Transcripts cap at the last 200 messages with an explicit "load earlier" control, no infinite scroll. Tool calls render collapsed, expandable to raw JSON, reusing `ui/src/components/json-editor-shared/`.

Tests: `ui/tests/agent-chat.spec.ts` with route mocks added to `ui/tests/fixtures.ts` (transcript render, send, in-flight state, error banner, session switching), component tests under `ui/tests/ct/` for text, tool-call and tool-result parts, and an XSS case feeding a script tag and an `onerror` image through the mocked transcript asserting nothing executes.

---

### Phase 4: SSE streaming
**1.5 weeks. Depends on Phase 3. Independently revertable behind a flag.**

Build: `ExecutorClient.run_stream()` as an async generator over ADK's `POST /run_sse`; `POST /agent-sessions/{session_key}/turns/stream` returning `StreamingResponse(media_type="text/event-stream")` with `Cache-Control: no-cache`, `X-Accel-Buffering: no` and a 15-second heartbeat; frame-by-frame validation into `TurnStreamEvent` with no upstream bytes forwarded; the stream path added to `PROMETHEUS_SKIP_PATHS` plus dedicated stream metrics; a semaphore bounding concurrent streams per process returning 503 when exhausted; `ui/src/core/api/sse.ts`, a `fetch` plus `ReadableStream` reader with abort support; `use-turn-stream.ts` writing incremental text into the React Query cache; feature flag `NEXT_PUBLIC_AGENT_CHAT_STREAMING`, default on, flip off to fall back to Phase 3.

**A stream must not outlive its own authorization.** `require_operation` runs once at request start, and this is the first long-lived connection in a codebase whose auth framework has only ever made request-scoped decisions. A revoked key, an expired cookie or an upstream deauthorization should stop the flow of transcript content, and nothing in the draft made that happen. `Principal.grant_expires_at` exists precisely to bound downstream lifetime and the draft never referenced it. The handler computes `deadline = min(now + stream_idle_timeout, principal.grant_expires_at or +inf, now + max_stream_seconds)` and closes with a terminal `error` frame at the deadline.

Other cases. A client disconnect cancels the upstream request and closes cleanly, with the `asyncio.shield` cleanup from Phase 2. An upstream stall hits `stream_idle_timeout_seconds` and closes with a terminal error so the UI does not spin. A malformed upstream frame is skipped and logged once per stream; one bad chunk must not kill a turn. A backgrounded tab keeps streaming and reconciles against the polled transcript on focus, which is the authoritative record. `.env.example` notes `X-Accel-Buffering` for anyone fronting the server with nginx.

Tests, all on the `live_server` fixture: frame ordering, heartbeat, idle timeout, terminal error frame, client abort with no leaked task, a stream terminating when `grant_expires_at` passes, the concurrent-stream cap boundary, and pool occupancy flat across three concurrent streams. UI-side, incremental render is a **component test** under `ui/tests/ct/` driving `ui/src/core/api/sse.ts` from a hand-built `ReadableStream`, because `ui/tests/fixtures.ts` mocks exclusively through `page.route()` plus `route.fulfill()`, which sends a complete body and cannot emit frames over time. `playwright-ct.config.ts` and `ui/tests/ct/` both already exist.

---

### Phase 5: nudges and halts
**2.5 weeks. Depends on Phase 3 (not Phase 4). The nudge and graceful-halt paths are hard-gated on A1; executor restart is not gated on anything.**

Two human actions, not one. A nudge injects guidance and the agent carries on. A halt stops the agent at its next boundary, the turn ends, and the transcript shows the block. "Stop it, then give it new input" needs nothing beyond those two, because once a halt lands the turn is over, the lock is clear, and the next `POST /turns` is an ordinary turn.

The draft claimed a Phase 2 dependency while its own deliverable includes rendering injected text inline in the transcript, which is the Phase 3 renderer.

Build, nudge side: `agent_session_nudges` table, `models/.../nudges.py`; `services/nudges.py` with `FOR UPDATE SKIP LOCKED` claiming, TTL reclaim, the split counters and `injection_attempts >= 3` expiry; `endpoints/agent_nudges.py` with `AGENT_NUDGES_WRITE` human-side and token-bound `AGENT_NUDGES_CONSUME` machine-side; SDK `sdks/python/src/agent_control/nudges.py` with per-session claiming at `before_model_callback` reading identity and token from `callback_context.state`, a negative-result cache with a floor interval, and control evaluation of the nudge body before injection; plugin injection as a synthetic user-role content part with `_inject_steering_guidance` gaining its `source` argument for the control path; UI nudge composer, queue list with per-nudge status, and **the exact injected text rendered inline in the transcript at the turn it landed**. Section 7.3's description of `claimed_by` as executor identity is corrected here, because under the session-bound token it identifies the session and is constant per session.

Build, halt side: `agent_session_halts` in the same migration; `models/.../halts.py`; `services/halts.py` with the conditional-insert creation, creator scoping via `_require_content_access`, the shared quota bucket, halt-beats-nudge precedence inside the claim transaction, claim-and-apply as one statement, and expiry in both the shielded cleanup and the acquire transaction; `endpoints/agent_halts.py`; one `Operation` member, `AGENT_HALTS_WRITE`, at `AUTHENTICATED`; SDK halt claiming at `before_model_callback` and `before_tool_callback` with the invocation-keyed latch; `ClaimNudgesResponse.halt`; `POST /agent-runtimes/{agent_name}/restart` reusing `agent_runtimes.write`, the executor supervisor with derived per-agent secrets, `supervisor_port` / `supervisor_secret` / kill-grace settings and their compose and `.env.example` wiring; error codes `TURN_NOT_IN_FLIGHT`, `EXECUTOR_NO_SUPERVISOR`, `EXECUTOR_SHARED` with `_ERROR_TITLES` entries; the structured audit WARNING; UI as below; all three generated artifacts.

**UI.** The stop button lives on the composer and exists only while a turn is live, which `AgentSessionDetail` already exposes. Between click and landing the state is "stopping…" with the honest sub-line "waiting for the agent to reach its next step; a tool already running will finish", never a bare spinner, which would imply the immediacy section 2 denies. If the halt reads `applied` but the turn has not ended, the copy is "stop acknowledged, waiting for the turn to end", and after a bounded interval it warns, per 9.5. "Stop waiting" stays as the secondary control described in Phase 3. Restart sits on the agent's runtime binding under an admin-only disclosure labelled with what it does to the process rather than with an adverb, and it names its blast radius from `affected_session_keys`. In the transcript, markers reuse Phase 2's control-block renderer, at most one per `target_trace_id`, with three copy variants: "Stopped by an operator before the next model call", "Stopped by an operator before running `send_email`" (the tool name rendered under Phase 3's plain-text, no-markdown rule), and "Executor restarted by an operator. The agent's last step may be missing from this transcript." Markers align by `target_trace_id`, so Phase 2's deep link into `GET /observability/traces/{trace_id}` works straight from one. No attribution is rendered, per section 7.2.

**Failure and edge cases**, nudge side. An agent between turns leaves the nudge `pending`, and the UI says "queued, will apply on the agent's next step" rather than showing a spinner. An agent that never runs again leaves it pending indefinitely, so the UI shows age and offers cancel. A process dying after claim and before ack gets TTL reclaim and redelivery, which is what at-least-once exists for. Two processes serving one agent get `SKIP LOCKED`. Ten queued nudges inject at most three per model call, oldest first, and the surplus returns to `pending` untouched, because a wall of appended text makes the model worse rather than more steered. A nudge arriving alongside control steering means control guidance stays in the system instruction and the nudge is a user turn; both are logged. A denied nudge is marked `rejected` and shown with the control that denied it. Session identity unavailable means neither the nudge nor the graceful-halt path ships.

**Failure and edge cases**, halt side. A halt against a session with no live invocation cannot be created: the conditional insert sees `in_flight_trace_id IS NULL` and returns 409 `TURN_NOT_IN_FLIGHT`, and the button is not shown. A halt after a 504 **can** be created, because the timeout path leaves the liveness marker set, and it lands at the executor's next boundary. A halt racing a nudge resolves server-side, halt wins, nudges are not consumed and no counter moves. A halt on a session opened by another caller is 403, matching transcript reads; under `HeaderAuthProvider` every browser caller hashes to the same identity, so that separates API keys and separates nothing between two people sharing the console, which the UI and `.env.example` both say in the wording already at `server/.env.example`. A halt claimed whose process then dies takes the turn down with it, the fenced cleanup or the next acquire expires the row, status reads `expired` with "the turn ended before the stop was applied", and it is never carried into a later turn, which the `target_trace_id = in_flight_trace_id` join enforces rather than assumes. Two halts against one turn is one halt, idempotent 200, enforced by the unique constraint. A tool already mid-execution cannot be interrupted; the halt lands at the next boundary after that tool returns and its side effect has already happened, and the UI says this before the click rather than after. Under Phase 4 a halt arrives as an ordinary terminal frame and the stream closes with `reason=complete`; no new termination reason. Cross-namespace `session_key` is 404 and a token bound to session A is 403 on session B.

**Failure and edge cases**, restart. A restart with turns in flight is the intended case, recovering as 9.6 describes, and the endpoint never touches `in_flight_since`. A restart with nothing in flight is allowed and writes no halt rows. `supervisor_port` unset is a written 409, not a 500. A `base_url` shared by more than one runtime row in any namespace is `EXECUTOR_SHARED` 409. `api_key_enabled` false is 503 naming the setting, whatever `allow_insecure_local_dev` says. An unreachable or non-2xx supervisor is `EXECUTOR_UNAVAILABLE` 503 with no upstream bytes forwarded, per 8.3. A session that starts a turn between enumeration and kill gets no halt row, and renders from its own turn outcome instead.

**Tests.** `test_agent_halts_endpoints.py`: create against a live turn; 409 with nothing live; **create after a simulated 504 succeeds**, which is the case the whole binding rework exists for; double-create returning the same id; claim at each boundary; claim-and-apply in one statement with `applied_at_boundary` recorded; ack enriching `applied_tool_name` and rejecting a name failing the identifier pattern; acking `applied` not by itself moving the session out of in-flight; expiry when the turn ends; **replica loss, where a stale in-flight row plus a new turn leaves the new turn unhalted and the old halt `expired`**; creator-scoping 403; quota 429; cross-session token 403; namespace 404. `test_agent_halts_precedence.py`: one halt and three pending nudges, one claim, asserting the halt returns, zero nudges return, and all three nudges' `claim_count` and `injection_attempts` are unchanged. `test_agent_halts_concurrency.py`: a claim and a turn-end cleanup racing on one session, asserting no deadlock escapes and the lock is always cleared. `test_agent_runtimes_restart.py` on the Phase 0 `live_server` fixture standing in as the supervisor: halt rows written terminal with `mode='restart'` and `applied_at_boundary='process'`; a graceful row for the same turn upserted rather than duplicated; `affected_session_keys` covering a second live session on the same agent; `EXECUTOR_NO_SUPERVISOR` 409; `EXECUTOR_SHARED` 409 when two namespaces share a `base_url`, plus an assertion that no row is written outside the caller's namespace; 503 with no upstream body; the `api_key_enabled` false refusal; the derived supervisor secret absent from `AdkExecutorClient._headers`; and an explicit assertion that the endpoint leaves `in_flight_since` alone. `test_agent_halts_alembic_migration.py` mirroring `test_teams_alembic_migration.py` across upgrade, downgrade, residue sweep and upgrade-downgrade-upgrade. `test_agent_halts_auth.py` for restricted-authorizer 403 per operation. `sdks/python/tests/test_google_adk_plugin.py` extended: a claimed halt at `before_model_callback` returns the blocked `LlmResponse` and `_evaluate_and_enforce` is never called; at `before_tool_callback` it returns the blocked dict and the tool function is never invoked; the latch is sticky across to the next model callback with no second claim; **a halt latched for invocation A does not block a concurrent `before_model_callback` for invocation B in the same process**, and the entry is gone after the block fires; `on_violation_callback` does not fire; `blocked_message_template` is not applied. `test_namespace_isolation.py` extended. `ui/tests/agent-chat-halt.spec.ts` with route mocks in `ui/tests/fixtures.ts`, covering button visibility tied to live-turn state, the stopping state, the acknowledged-but-still-running state, all three transcript markers, and admin-only visibility of restart.

Section 15's standing warning applies with extra force here: that plugin test file injects hand-written fakes into `sys.modules["google.adk.*"]`, so it verifies this repo's fiction of ADK. The two contracts halting rests on, that an `LlmResponse` from `before_model_callback` ends the invocation and that a dict from `before_tool_callback` prevents execution, go into the pinned-ADK contract job as behavioural cases rather than attribute checks. Without that, the halt tests prove only that the mock behaves as its author wrote it.

---

### Phase 6: declared plan and progress
**1 week. Depends on Phase 3. Hard-gated on A1.**

Build: `agent_session_plan_steps` table, migration, `models/.../plans.py`; `services/plans.py` and `endpoints/agent_plans.py` with token-bound `AGENT_PLANS_WRITE`; two ADK `FunctionTool`s in `sdks/python/src/agent_control/integrations/google_adk/progress_tools.py`, `declare_plan(steps)` and `mark_step(plan_revision, step_index, status, note)`, both reading `session_key`, `plan_revision` and the runtime token from `tool_context.state`; a progress rail in the chat panel labelled **"Plan reported by the agent"**, with a revision count when the agent replans.

Fallback when no plan exists: turn count, elapsed time, and a link to the turn's trace. No percentage, ever.

Other cases. An agent that never calls `declare_plan` gets the fallback view. An abandoned plan leaves steps `pending` and the UI shows staleness by last-update time rather than pretending work is stalled at 40 percent. A replan writes a new revision and the UI says "plan revised". Marking step 7 of a 5-step plan is a 422 with no partial write. A stale `plan_revision` is a 409. An agent that lies is unfixable, which is exactly why the rail says "reported by the agent" and the trace link is the independent evidence.

Tests: `test_agent_plans_endpoints.py` (declare, revise, update, out-of-range index, stale revision 409, namespace isolation, cross-session token rejection), `sdks/python/tests/test_google_adk_progress_tools.py`, and `ui/tests/agent-chat-progress.spec.ts` including the no-plan fallback.

---

## 13. Observability of the new machinery

The draft shipped zero new metrics, which means the parts most likely to fail quietly (nudge delivery, stream lifetime, executor reachability) would be diagnosable only by reading logs.

Streaming will also poison the existing request-latency histogram if left alone. `starlette_exporter` stamps the end of a request on the last response body chunk, so a streamed turn records the full turn duration into `request_duration_seconds`, whose buckets top out at 60, dropping every long turn into `+Inf`. `requests_in_progress` stays incremented for the stream's whole lifetime. Any dashboard computing p95 over that histogram breaks on the day streaming ships, and it breaks in the direction that looks like a server-wide latency regression. The stream path goes into `PROMETHEUS_SKIP_PATHS` (the constant already exists at `main.py:71`) and gets its own instrumentation instead. Precedent for hand-rolled metrics is in `auth_framework/providers/http_upstream.py` and `db.py`.

New metrics: `agent_control_turn_duration_seconds` and `..._turn_stream_duration_seconds` with turn-appropriate buckets; `..._active_streams` gauge; `..._streams_rejected_total`; `..._stream_terminations_total{reason=complete|client_abort|idle_timeout|grant_expired|upstream_error}`; `..._nudge_delivery_lag_seconds` (created_at to applied_at); `..._nudges_pending` gauge by age bucket; `..._nudge_claims_total{result=empty|claimed}`; `..._sessions_stuck_in_flight` gauge; `..._executor_request_failures_total{kind}`; `..._executor_up` gauge.

Phase 5 adds: `..._halt_delivery_lag_seconds`, created_at to applied_at, **emitted only for `mode=graceful`**, because a restart's row is inserted already applied and would observe near zero, dragging every percentile down and hiding the graceful regressions this histogram exists to catch; it is the one number answering "how long did stop take". Then `..._halts_total{mode=graceful|restart,boundary=model|tool|process}`, `..._halts_expired_total`, `..._halts_rejected_total{reason=...}` so quota and 409 refusals are visible rather than invisible, `..._halts_applied_still_in_flight_total` for the gap in 9.5, and `..._executor_restarts_total`.

`/health` returns a static `{"status": "healthy"}` and explicitly does not check the database, let alone the executor. Adding a hard dependency on a second process with no health signal means the first symptom of any executor outage is a user hitting send. `GET /api/v1/agent-sessions/executor-health` backs the `..._executor_up` gauge and gives the dependency a probe.

---

## 14. Explicitly out of scope, with reasons

**Team hand-off between agents.** Not delivered by any phase. See section 1. This is the largest scope decision in the document and it should be reconsidered before work starts rather than discovered late.

**Long-term cross-session memory.** Only `VertexAiMemoryBankService` and `VertexAiRagMemoryService` persist, and a bare Gemini API key reaches neither. A `BaseMemoryService` over Postgres plus pgvector using Gemini embeddings is roughly 3 days and is a real option later. Out now because a chat panel needs within-session history, which `DatabaseSessionService` gives for free. Worth noting the deferral is sound for chat and silently false for hand-off: agent B needs agent A's output and there is nowhere to put it, which is part of why hand-off is its own phase.

**True mid-tool interruption.** Not possible. Named so nobody scopes it. Phase 5 is not an exception to this and must not be read as one. A halt lands at the next model or tool boundary, so a tool that has already started runs to completion and its side effect happens. Restarting the executor is not a counter-example either: it ends a running tool mid-syscall with no rollback of anything it already did, which is a different thing from interrupting it, and usually a worse one.

**Human-in-the-loop tool approval.** ADK supports it through `LongRunningFunctionTool` and `tool_context.request_confirmation()`, and it fits this product's identity well. Deferred because a turn that can suspend changes the turn lifecycle, and stacking that on an unproven streaming path doubles the debugging surface. Natural Phase 7.

**The SDK ContextVar refactor.** Moving `_StateContainer` fields onto `ContextVar`s and relaxing the plugin's agent_name guard would allow one executor process to host many agents. Breaking SDK change, 1 to 2 weeks with full `sdks/python/tests` regression, and it is not on the critical path because process-per-agent works. Revisit when the process count becomes the operational problem.

**Authentication on the ADK process.** It has none. Network isolation is the control. An auth shim in front of it is a deployment concern.

**Per-namespace executors.** Process per agent already gives partial isolation. Full per-namespace runtime separation becomes a requirement the moment a second real tenant exists, and `ExecutorSettings.base_url` resolved per agent from `agent_runtimes` makes it a configuration change rather than a refactor.

**Websockets.** Rejected with reasons in section 8.

**A second `ExecutorClient` implementation.** The Protocol exists so LangGraph or claude-flow could slot in. Building a second before the first works is designing an abstraction against one example and calling it two.

**The claude-flow / AgentDB path from the global `CLAUDE.md`.** If that stack is chosen instead, most of this plan survives because `ExecutorClient` is the seam, but the ADK-specific parts (plugin, session state, tools) need equivalents and nothing here is written against it.

---

## 15. Effort

| Phase | Estimate | Confidence |
|---|---|---|
| 0. Spike, gate, test harness | 1 week | High. Bounded by a checklist. |
| 1. Session registry, executor adapter, runtime bindings | 2 weeks | Medium. Straight CRUD, but the compose and DB-role work is a real half-week the draft priced at nothing. |
| 2. Blocking turns | 2 weeks | Medium. Atomic locking, quotas, trace propagation and cancellation cleanup are each fiddlier than they look, and the cleanup now has to be trace-fenced with two distinct exits because Phase 5 depends on its exact shape rather than on its existence. |
| 3. UI chat panel | 1.5 weeks | Medium. Tool-call rendering always overruns. |
| 4. SSE streaming | 1.5 weeks | Low. No precedent in this repo, and the auth-lifetime and metrics work is new ground. |
| 5. Nudges and halts | 2.5 weeks | Medium. Nudges and graceful halt conditional on A1; executor restart unconditional. |
| 6. Plan and progress | 1 week | High, conditional on A1. |
| TS SDK regeneration and overlay churn | 2 days x 5 gated phases | Medium. |

Phase 5's extra week splits evenly. Graceful halt is +0.5 because it reuses the migration, the claim plumbing, the endpoint module and the UI panel the nudge work already builds; the new parts are the trace binding, the precedence rule and the invocation-keyed latch. Executor restart is +0.5 and it is the likelier of the two to overrun, because the supervisor and its derived-secret provisioning are new artifacts inside the executor image, and packaging work in this repo has already been priced at zero once.

**Total: 11.5 to 14.5 weeks** of focused work. The architect's 4 to 6 and the draft's 6 to 8 both omitted the spike's real size, the executor packaging, the runtime registry, trace propagation, the TS SDK gate, and the verification load of a repo where `make check` spans eight workspace members and the UI job runs lint, prettier, typecheck, next build, Playwright integration and component tests.

**Minimum useful slice: Phases 0 through 3, roughly 7 weeks, gives a working chat panel.** That is a real stopping point if the schedule slips.

Ongoing cost: version coupling to ADK, widened from four callback signatures to four callbacks plus a session-state contract plus an HTTP surface. Which needs a mitigation, because there is currently none: `sdks/python/tests/test_google_adk_plugin.py` injects hand-written fakes into `sys.modules["google.adk.*"]`, `google-adk` is an optional extra, and `make sync` does not install extras, so all 26 existing plugin tests exercise this repo's fiction of ADK rather than ADK. Extending that file with nudge tests would re-verify A1 in perpetuity against a mock the developer wrote. Add a CI job (blocking, or nightly and release-gating) that installs `agent-control[google-adk]` at a pinned version and runs a thin contract suite: the four callbacks exist with the expected parameter names, `CallbackContext.state` and `ToolContext.state` exist and round-trip a dict, and the `LlmRequest.config.system_instruction` attribute path is real. Pin `google-adk` to `>=X,<Y` rather than the current open `>=1.0.0`.

---

## 16. The riskiest remaining assumption

Not SSE. Not the ADK API surface. Both are engineering with visible failure modes and a spike that catches them.

**It is A1: that ADK session state seeded at creation is readable from `CallbackContext.state` and `ToolContext.state` inside a live invocation.**

That single mechanism carries the nudge session binding, the halt session binding, the runtime token that authorizes every machine-side write, the `session_key` the progress tools need, and the trace id that connects a turn to its guardrail decisions. Five load-bearing things on one unverified contract. If it does not hold, Phases 5 and 6 do not ship in the form described, the trace deep-link degrades to after-the-fact reconciliation, and the machine-side authorization design needs a different token delivery channel. There is no partial version of this that is safe: the obvious workaround, agent-scoped delivery, means injecting one person's typed sentence into another person's conversation, and the halt equivalent is worse, since it means stopping a turn the person never asked to stop.

**Executor restart survives an A1 failure intact.** It needs no callback, no session state and no runtime token. So if the spike kills A1, the fallback is not "no Phase 5" but a restart-only slice of roughly one week: an admin stop control with honest copy, the supervisor with derived per-agent secrets, the recovery semantics, the trace-fenced cleanup Phase 2 owes it, and no nudges. That matters, because before this was worked out an A1 failure meant the product had no answer at all to "stop the agent", which is the more visceral of the two things a person watching an agent go wrong actually wants.

The second risk is A6, and it is a product risk rather than an engineering one: that an operator sentence delivered as a user turn actually changes agent behaviour in a way a person recognises as steering. Delivering it as a user turn rather than a system-instruction append improves the odds substantially, because models weight user content heavily and because the text then passes through the control engine on the way in. But there is no evidence for it in this repo, and if it fails, what ships is a text box that appears to do nothing, which is worse for trust than shipping no steering at all. Hence A6 by hand in week one, and hence the requirement that Phase 5 render the exact injected text inline so a human can judge for themselves whether it was heard.

---

## 17. Verification checklist before each PR

Per the repo's own conventions, and because CI enforces more of it than the draft accounted for:

```
make check                       # test + lint + typecheck across eight workspace members
make openapi-spec
cd ui && npm run fetch-api-types && npm run lint && npm run typecheck && npm run build
cd ui && npx playwright test && npx playwright test -c playwright-ct.config.ts
make sdk-ts-generate && make sdk-ts-name-check && make sdk-ts-generate-check
```

Known pre-existing and not caused by this work: ruff I001 on `server/src/agent_control_server/migrate.py`.
