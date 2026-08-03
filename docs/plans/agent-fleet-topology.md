# Agent Fleet Topology: Implementation Plan

Status: design. Nothing built.
Branch context: `feat/task-dispatcher`.
Scope: how N agents across M teams get started, addressed, credentialed and budgeted; what `docker compose up` brings up and what stays manual; and the two credential problems that only appear at fleet scale.
Depends on: the orchestration plan's Phases 1 and 2 (`agent_runtimes`, `agent_sessions`, `POST /turns`), which are shipped, and the dispatch ledger from `task-dispatcher.md` Phases 1 to 4, which is largely shipped (`agent_tasks`, `agent_task_steps`, `agent_workflows`, `agent_dispatch_state`, `endpoints/agent_dispatch.py`, `dispatcher/`).
Does not deliver: a scheduler, a supervisor, autoscaling, or any change to what authorizes work. Section 9.

**Author's note.** Every measurement below was taken from this working tree, this database and these running processes while writing. An earlier draft of this plan carried two errors that survived a first review and were caught by a second: it assumed `App(name=...)` was ADK's routing key, and it assumed `conflict_mode="strict"` alone removed the admin requirement. Both were wrong, both are corrected here in sections 3.4 and 4.3, and both corrections cost real effort that the earlier phase table did not have. Where a claim is about behaviour rather than text, the probe that produced it is named.

---

## 1. What ships, in one paragraph

Eight executor processes stop being eight terminal tabs. A checked-in `fleet.yaml` names which registered agents should have a process; a generator turns it into `compose.fleet.yml`, one service per agent, all from one image, differing only by `AGENT_CONTROL_AGENT_NAME`. Nothing publishes a host port, so the 8080-8087 range disappears rather than growing, and `agent_runtimes.base_url` becomes `http://ac-exec-<agent-name>:8000`, derived from the thing it points at rather than a number somebody typed. The image materializes exactly one ADK agent package per container at start, so `/list-apps` returns the agent's own name and a row aimed at the wrong process fails closed instead of silently running one agent's work under another's controls. A one-shot `fleet-register` job holds the only admin credential in the design: it registers the agents, syncs their step schemas, writes the runtime bindings, and exits before any agent code exists anywhere in the profile, which is the argument `docker-compose.dev.yml` already makes for `adk-db-init`. Executors then boot on an ordinary key and find every step already present, which is what takes ADMIN out of eight processes running model-driven agent code. The namespace turn budget gains a per-team companion charged in the same transaction and at the same statement, `charge_dispatch_turn`, and the plan is honest that per-team accounting bounds authorized spend without creating capacity, because all four teams draw on one consumer subscription through one unauthenticated local proxy.

**Three things it does not ship**, named here because each is what a reader supplies for themselves. Milestone scope does not become schedulable, at any layer, by any convenience this adds. The dispatcher does not become admin, and the fleet's registration credential deliberately lives in a different process with a different lifetime. And the one-agent-per-process SDK constraint is not removed; section 3.2 prices removing it and refuses.

---

## 2. Measured state

**Eight executor processes, on the host, started by hand.** `ps` shows eight `uv run --no-sync --with google-adk[extensions] adk api_server --port 808N` for N in 0..7. All eight answer 200 on `/list-apps`. None is in Docker. Only `agent_control_postgres` (host port 15432) and `agent_control_server` (host port 8000) are.

**None of them passes `--session_service_uri`.** So ADK sessions are in `InMemorySessionService` and die with the process. This matters more than it looks and section 3.5 is about it.

**They are all the same application.** `examples/google_adk_plugin/my_agent/agent.py` reads `AGENT_CONTROL_AGENT_NAME` from the environment and ends with `app = App(name="my_agent", root_agent=root_agent, plugins=[plugin])`. `marketing_researcher` and `engineering_reviewer` are the same code, the same two local tools, the same Exa toolset, differing by that environment variable and by a system prompt stored in `agent_configs`.

**`agent_runtimes`, all eight rows:**

```
namespace_key |       agent_name       | executor_kind |             base_url             | executor_app_name | enabled
--------------+------------------------+---------------+----------------------------------+-------------------+--------
default       | marketing_copywriter   | google_adk    | http://host.docker.internal:8080 | my_agent          | t
default       | sales_prospector       | google_adk    | http://host.docker.internal:8081 | my_agent          | t
default       | sales_outreach_drafter | google_adk    | http://host.docker.internal:8082 | my_agent          | t
default       | ops_runbook_agent      | google_adk    | http://host.docker.internal:8083 | my_agent          | t
default       | ops_incident_triage    | google_adk    | http://host.docker.internal:8084 | my_agent          | t
default       | marketing_researcher   | google_adk    | http://host.docker.internal:8085 | my_agent          | t
default       | engineering_reviewer   | google_adk    | http://host.docker.internal:8086 | my_agent          | t
default       | engineering_debugger   | google_adk    | http://host.docker.internal:8087 | my_agent          | t
```

Primary key `(namespace_key, agent_name)`. No unique constraint on `base_url`, which orchestration 9.6 already noted. `executor_app_name` is the same literal string on every row.

**Nine agents, eight runtimes.** `google-adk-plugin` is registered and unbound.

**Four teams, and a live misconfiguration:**

```
      slug      |   display_name   | linear_team_key
----------------+------------------+-----------------
 engineering    | Engineering      | ENG
 marketing      | Marketing        | OPS
 operations     | Operations       | OPS
 sales-outreach | Sales & Outreach |
```

`marketing` and `operations` both point at Linear team OPS. `linear_team_key` bounds which team's issues a dispatcher may read, so two Agent Control teams currently see one Linear team's work. `sales-outreach` has no key at all.

**The budget is namespace-wide.** `agent_dispatch_state` has `namespace_key` as its entire primary key, `max_tasks_per_hour` 20 and `max_turns_per_hour` 60. All four teams draw on that one row, which is why every dispatcher run prints one budget line for the whole fleet.

**A workflow already crosses team lines.** The one row in `agent_workflows` is `plan-critique-execute`, `team_slug = marketing`, and its second step names `engineering_reviewer`, a member of `engineering`. Not hypothetical; it is the shipped example.

**Dispatch sessions carry no team.** All five rows in `agent_sessions` with `agent_task_id IS NOT NULL` have `team_id` NULL, because `DispatchClient.create_session` sends `agent_name`, `title` and `task_key` and nothing else. `agent_tasks.team_slug` is populated for the Linear-sourced tasks and NULL for the file-sourced one.

**Only `once` exists.** `dispatcher/src/agent_control_dispatcher/cli.py` defines one subparser. `serve`, `claim` and `preflight` are specced in `task-dispatcher.md` section 4 and are not written.

**No compose file uses `profiles:` or `include:`.** Grepped both `docker-compose.yml` and `docker-compose.dev.yml`. The `agent-dispatcher` service in `docker-compose.yml` carries a comment claiming "a profile meets the same intent", but no profile key exists, and the service also carries a `build:` block, so `docker compose build` builds it today. Local Compose is v5.1.4, well past the 2.20 that `include:` needs.

**The proxy.** Executors reach an OpenAI-compatible endpoint at `http://127.0.0.1:10531/v1` (`npx openai-oauth`, no API key, fronting a consumer subscription). Probed: `/v1/models` answers 200, `/v1/files` answers 404. Measured earlier and taken as given: it silently drops an inline file block (HTTP 200, the model reports no attachment) and rejects image `data:` URIs with a 500 naming the URL scheme. Text is the only transport it accepts.

**Containers can reach it.** Probed from inside `agent_control_server` with `urllib`: `http://host.docker.internal:10531/v1/models` returns 200, `http://host.docker.internal:8085/list-apps` returns 200. Docker Desktop for macOS. Section 11 flags what that does not prove.

---

## 3. Fleet topology

### 3.1 The constraint, stated once

One process serves one agent. `_state.py:25` holds `self.current_agent: Agent | None`, one per module, alongside one `api_key`, one `server_url`, one `server_controls` list and one `agent_config` snapshot. `AgentControlPlugin.__init__` raises `ValueError` at `plugin.py:147` when its `agent_name` does not match the process's initialized agent. `models/src/agent_control_models/agent_runtimes.py` says the same in its module docstring, and the example agent says it in a comment above `AGENT_NAME`.

N agents means N processes. Everything below has to fit that shape.

### 3.2 Should the constraint be changed instead? No, and here is the bill

The tempting move is a per-agent registry in the SDK, so one `adk api_server` hosts many agents and the fleet becomes one container. Three costs, and the third decides it.

The mechanical cost is ordinary. `state` becomes a per-agent map; `_cached_server_control_lookup` rekeys (orchestration section 1 records it keying on `state.current_agent.agent_name` at `evaluation.py:203`); the plugin's constructor refusal becomes a lookup; the refresh loop fans out; `RuntimeTokenCache`, `agent_config`, `model_max_staleness_seconds` and `server_controls` all become per-agent. Two to three weeks of surgery on the most safety-critical file in the SDK, plus a compatibility story for every consumer holding the module-level API.

The credential cost is worse. One process holding one agent holds one agent's key, one agent's model endpoint and one agent's tool secrets. A process holding eight holds the union, and the blast radius of one prompt injection stops being one agent's sessions and becomes every agent sharing the process.

And the third cost inverts the argument the topology was chosen for. Orchestration section 5 defends process-per-agent partly because "an agent with any HTTP-egress tool is an SSRF pivot onto the network its own executor sits on", and says process per agent "shrinks that blast radius to one agent's own sessions, which is a real benefit of the topology forced by the SDK constraint". The example agent ships `web_fetch_exa` on by default. Merging processes spends a property this deployment gets for free in order to save memory.

**Decision: keep one process per agent.** What the fleet design owes in return is that starting the eighth process costs the same as starting the second.

### 3.3 One container per agent, generated, no published ports

*One container serving many agents behind a router.* Ruled out by 3.2. A router in front of eight processes is fine; a router inside one process is the thing that does not exist.

*A host process supervisor.* Stays documented as the fallback, because the consumer-subscription proxy binds loopback and some operators will not want executors in Docker at all. Not the default: it reintroduces the port range, and "supervised" on a laptop means a shell script nobody wrote.

*One container per agent.* Chosen. Same image, listening on 8000 inside its own network namespace, publishing nothing.

**`docker compose up --scale` is not the mechanism, and the reason matters.** Scaled replicas of one service share an environment. Every executor needs a distinct `AGENT_CONTROL_AGENT_NAME`, and the SDK refuses a second agent in a process, so eight identical replicas would be eight copies of one agent racing on one binding. Compose has no per-replica environment, so the services are generated, not scaled.

**`fleet.yaml`, checked in, is the source of what should be running:**

```yaml
version: 1
image: agent-control-executor:local
defaults:
  web_tools: true
  restart: unless-stopped
agents:
  - agent_name: marketing_researcher
  - agent_name: marketing_copywriter
  - agent_name: engineering_reviewer
  - agent_name: engineering_debugger
  - agent_name: ops_runbook_agent
  - agent_name: ops_incident_triage
  - agent_name: sales_prospector
  - agent_name: sales_outreach_drafter
    web_tools: false
```

**The database is not the source.** Generating services from `agent_runtimes` is circular: the row records where a process is, so a process started from the row cannot be what creates it, and a stale row becomes self-perpetuating. `fleet.yaml` declares intent, `agent_runtimes` records fact, and 7.4 is the reconciliation. `google-adk-plugin` is registered with no runtime today and is absent from `fleet.yaml`, which is the correct expression of "this agent has no process and that is deliberate".

**`scripts/gen_fleet_compose.py` emits `compose.fleet.yml`**, one service per entry:

```yaml
  ac-exec-marketing-researcher:
    image: agent-control-executor:local          # image only, never build:
    container_name: ac-exec-marketing-researcher
    profiles: ["fleet"]
    environment:
      AGENT_CONTROL_AGENT_NAME: marketing_researcher
      AGENT_CONTROL_URL: http://server:8000
      AGENT_CONTROL_API_KEY: ${AGENT_CONTROL_FLEET_API_KEY:?fleet key required}
      AGENT_CONTROL_MODEL_BASE_URL: ${AGENT_CONTROL_FLEET_MODEL_BASE_URL:-http://host.docker.internal:10531/v1}
      AGENT_CONTROL_WEB_TOOLS: "1"
      ADK_SESSION_SERVICE_URI: postgresql://adk:${ADK_DB_PASSWORD:-adk_local}@postgres:5432/adk_runtime
      EXA_API_KEY: ${EXA_API_KEY:-}
    extra_hosts: ["host.docker.internal:host-gateway"]
    depends_on:
      server: {condition: service_healthy}
      postgres: {condition: service_healthy}
      fleet-register: {condition: service_completed_successfully}
    healthcheck:
      test: ["CMD", "/usr/local/bin/exec-health", "marketing_researcher"]
      interval: 15s
      timeout: 5s
      retries: 4
      start_period: 45s
    restart: unless-stopped
```

Five things in that fragment are decisions rather than boilerplate.

**No `ports:`.** This is what retires 8080-8087 instead of extending it. Orchestration section 5 already requires it in writing: `adk api_server` ships with no authentication, and the only real control is that its port is never published. Today's eight published ports violate that sentence, tolerated because the server is in Docker and the executors are not. Containerising both removes the reason.

**`image:` and never `build:`.** Anything with a `build:` block participates in `docker compose build` regardless of profile, which is how a fleet profile leaks into everybody's build. The fleet image is built by an explicit `make fleet-image` target. The existing `agent-dispatcher` service has this bug today, and fixing it is a two-line change that rides along in Phase 1.

**`AGENT_CONTROL_API_KEY` has no default and the interpolation fails loudly.** An executor that starts without a key registers nothing and runs anyway on local controls, which is the quiet failure this design cannot afford at eight copies.

**`depends_on: fleet-register: service_completed_successfully`.** Load-bearing, not tidiness. Section 4.3 is the whole argument.

**The healthcheck asserts identity, not just liveness.** `/list-apps` is what the server's own client uses (`adk_executor_client.py:97`, `_HEALTH_PATH = "/list-apps"`), and having two definitions of executor health is how they disagree. But once 3.4 lands, that endpoint returns the agent's own name, so the probe can assert the body equals `["marketing_researcher"]` rather than merely 200. That converts a liveness check into a weak identity check for free. It still cannot see a wedged control cache; section 8.

### 3.4 The app name is the folder name, and this is what makes a stale row fail closed

An earlier draft proposed setting `App(name=os.getenv("AGENT_CONTROL_EXECUTOR_APP_NAME", AGENT_NAME))` in the example, on the theory that a row pointing at the wrong process would then 404 on the session path. **That does not work, and shipping it would have converted a rare silent mis-execution into a total fleet outage.**

Measured. `adk_web_server.py` resolves a route through `self.agent_loader.load_agent(app_name)`. `AgentLoader._validate_agent_name` requires `^[a-zA-Z0-9_]+$` **and a matching directory or module on disk under `agents_dir`**; `_perform_load` then does `importlib.import_module(f"{agent_name}.agent")`. `list_agents()` is `os.listdir(agents_dir)` filtered to directories. `App(name=...)` is never consulted for routing: `_record_origin_metadata` stamps `_adk_origin_app_name` from the *folder*, not from the `App`. Probed: `curl http://127.0.0.1:8080/list-apps` returns `["my_agent"]`, which is the directory name, on every one of the eight.

So `executor_app_name` in `agent_runtimes` has to name a directory that exists in the executor's image, or `POST /apps/{app_name}/users/{u}/sessions/{sid}` 404s against a perfectly healthy process.

**The fix is to make the folder real, and to materialize exactly one of them.** The image installs the example as an ordinary Python package. The entrypoint creates `/agents/${AGENT_CONTROL_AGENT_NAME}/agent.py`, a shim that imports the shared module and exposes `root_agent` and `app`, and then runs `adk api_server /agents`. One directory, so `/list-apps` returns exactly `["marketing_researcher"]`, `executor_app_name` equals `agent_name`, and a row aimed at the wrong container 404s into the `ExecutorSessionNotFoundError` path that already exists (`adk_executor_client.py:410`) instead of running one agent's work under another agent's controls.

**Materializing all eight packages into the image would undo the whole point.** `list_agents()` enumerates directories, so an image carrying eight packages would let every executor advertise and route all eight names, and the SDK would then refuse the second one at plugin construction, or worse, load it before the refusal and leave a half-initialized module. One folder per container, created at start from the one environment variable that already decides everything else.

Two consequences worth writing down. ADK calls `envs.load_dotenv_for_agent(agent_name, agents_dir)`, which looks for `agents_dir/<agent_name>/.env`; the generated folder has no `.env`, so all configuration arrives through the container environment, which is where it should be anyway. And agent names in `agent_runtimes` are already underscore-only Python identifiers (`marketing_researcher`), so nothing needs renaming to satisfy `_VALID_AGENT_NAME_RE`. The container and service names use hyphens, because DNS labels do; only the app name uses underscores.

**Honest cost.** This is generator plus Dockerfile plus entrypoint work in Phase 3, not a one-line edit to the example. And Phase 0 probes it before anything writes `executor_app_name`, because the entire mis-execution defence rests on `/list-apps` returning the per-agent name.

### 3.5 The session backend, decided rather than inherited

Today's executors run with no `--session_service_uri`, so ADK sessions live in memory. With `restart: unless-stopped` and a design that makes restarts routine (wedged-executor recovery, image updates, daemon restarts), in-memory means every restart silently invalidates every `agent_sessions.executor_session_id` bound to that container. The next turn 404s and the row moves to `orphaned`, which `models/src/agent_control_models/sessions.py:88` already defines as "the executor lost the state".

`docker-compose.dev.yml` already provisions the answer and nobody wired it up. `adk-db-init` creates `adk_runtime` owned by a dedicated `adk` role and closes the control-plane database to that role, with a comment giving exactly the right reason: the executor runs model-driven agent code, and connecting as `agent_control` would let it rewrite the controls that govern it.

**Fleet executors run `--session_service_uri` against that role, with `depends_on: postgres: service_healthy`.** So a restart is a restart rather than a mass orphaning, the mid-chain restart runbook in section 8 becomes survivable, and the `adk` role finally does the job it was created for.

The host-supervisor fallback keeps in-memory sessions unless the operator passes the flag, and the runbook says what that costs in one sentence rather than leaving it to be discovered.

---

## 4. The admin-on-every-executor problem

### 4.1 The mechanism, verified

`agent_control.init()` always sends `conflict_mode="overwrite"` (default on the signature, `sdks/python/src/agent_control/__init__.py:527`), and nothing in the example overrides it.

`endpoints/agents.py:742` is the gate:

```python
if request.force_replace or request.conflict_mode == ConflictMode.OVERWRITE:
    await _authorize_existing_agent_overwrite(http_request, principal)
```

That runs before a single field is compared. `_authorize_existing_agent_overwrite` calls `get_authorizer(Operation.AGENTS_UPDATE).authorize(...)`, and `providers/header.py:50` maps `AGENTS_UPDATE` to `AccessLevel.ADMIN`. So re-registering an unchanged agent on an ordinary key is a 403. The example's `.env` records this in a comment, calls it "ADMIN key, and that is not a typo", says it is "wrong for a real deployment", and tracks it as a follow-up. This plan is that follow-up.

At eight processes it reads like this: eight copies of a credential that can create and unbind controls, held by processes running model-driven agent code with a web-fetch tool on by default. Each can rewrite the controls that govern it. The dispatcher, by contrast, is required in writing not to be admin, and `dispatch preflight` is specced to refuse an admin key, because "its credential must not be the credential that approves its output".

### 4.2 Option A, gate overwrite on the computed diff. Rejected

The obvious fix is to move the `AGENTS_UPDATE` check below the diff, so a no-op overwrite needs no admin. `test_init_agent_overwrite_existing_agent_requires_update_auth` exists to stop exactly that, and reading it before proposing the change is the point: it registers an agent, installs a `CreateOnlyAuthorizer` that refuses only `AGENTS_UPDATE`, re-sends the *same* payload with `conflict_mode="overwrite"`, and asserts 403.

The test is right and the instinct is wrong. Overwrite is destructive by semantics, not by outcome. `test_init_agent_overwrite_warns_on_removed_referenced_evaluator` in the same file shows what the mode does: an evaluator absent from the incoming payload is removed even when an active control references it, and the response carries `control_ids` and `control_names` so somebody can see what they just broke. Authorizing that mode on the strength of *this* run's diff authorizes a mode whose *next* run's diff is unknown, from a process whose payload is whatever its code says at the moment it restarts. A developer edits `tools=[...]`, the container restarts, and a request authorized as a no-op yesterday removes two steps today.

There is a smaller point in the same direction: `test_init_agent_overwrite_noop_reports_not_applied` already asserts that a no-op overwrite returns `overwrite_applied: false` with every change collection empty. The server can already tell the difference. It refuses anyway, on purpose.

**Rejected. The test stays exactly as it is.**

### 4.3 Option B, ask for the mode that already has the diff gate. Chosen, and it is only half the fix

`init(conflict_mode=...)` default flips from `"overwrite"` to `"strict"`. Under strict, `endpoints/agents.py:981` is the gate:

```python
if (
    not request.force_replace
    and request.conflict_mode != ConflictMode.OVERWRITE
    and (steps_changed or evaluators_changed or metadata_changed)
):
    await _authorize_existing_agent_overwrite(http_request, principal)
```

That is the diff-based gate, and it already exists. So Option B is not "add a diff gate", it is "stop asking for the mode that skips the one already there". The restart case is already handled deliberately: the metadata comparison at `agents.py:786` is preceded by a comment explaining that `agent_created_at` is preserved so that "merely restarting an agent is not seen as a metadata change (which would otherwise demand an admin credential on every restart)". Somebody has already solved part of this.

**What Option B does not fix, and an earlier draft claimed it did.** `steps_changed` is set for any step key not already stored (`agents.py:915`), so under strict a new step still demands `AGENTS_UPDATE`. There is a deliberate test: `test_init_agent_strict_existing_agent_mutation_requires_update_auth` at `server/tests/test_init_agent_conflict_mode.py:215` sends one new step under strict against `CreateOnlyAuthorizer` and expects 403.

And the SDK already reaches that path. `plugin.py:1590` sends `conflict_mode="strict"` for step sync; `plugin.py:209` calls `_sync_steps_blocking(steps, raise_on_error=True)` from `bind()`; the example wraps `bind()` and re-raises as `RuntimeError`. Meanwhile `init()` sends no steps at all in the example (`agent.py:264`, three keyword arguments, none of them `steps`). So on a non-admin fleet key the sequence for a **new** agent is: init creates it with zero steps, bind discovers the ADK tools, register under strict, `steps_changed`, 403, `RuntimeError` at module import, crash loop. And adding one tool to the shared example does the same thing to all eight processes at once. Flipping `AGENT_CONTROL_WEB_TOOLS` from 0 to 1 is also a step addition, because `bind()` registers the toolset object as a step.

**Why the earlier draft's Phase 0 could not have caught this.** `_sync_steps_async` short-circuits when every discovered step key already exists on the server (`plugin.py:1579-1584`) and returns without calling `register_agent`. Restarting the eight already-synced executors therefore never touches the register path, so the experiment passes and the defect ships behind a green result. A gate that can only pass is not a gate.

**So step registration is named as an admin act and routed through the credential holder the design already has.** `fleet-register` (section 7.1) runs the executor image with a sync-only entrypoint: for each agent in `fleet.yaml` it imports the agent module with that agent's environment, lets `init()` and `bind()` register the agent and its steps under its admin key, and exits. Executors then boot on an ordinary key, find every step present, short-circuit by construction, and never reach a gate.

That is a better answer than relaxing the gate, for the same reason 4.2 gives. A step is the unit the control engine binds to. A process that can add steps at will can add a step whose name a control does not cover and call it, which is `12.3`'s escalation argument arriving by a different door. Registration is deployment configuration; it belongs in the job that already holds deployment configuration and dies before any model runs.

**What the default flip still breaks, stated plainly.** Under strict, a step whose `input_schema` or `output_schema` changed raises `ConflictError` with `ErrorCode.SCHEMA_INCOMPATIBLE`, a 409, rather than silently replacing the registration. A developer who edits a tool signature and reruns `fleet-register` gets a refusal instead of a quiet update. That is the correct behaviour: replacing a registration that active controls are written against is an administrative act and should require `conflict_mode="overwrite"` on purpose.

**The second cost goes in the docstring.** Strict merges rather than replaces, so a step deleted from the agent's code stays in the registry and the registered step list is monotonic across restarts. Pruning needs an admin overwrite. Worse for tidiness, better for safety.

**And one quiet failure to name.** `_ensure_step_known` (`plugin.py:1368`) syncs a step discovered at runtime with `raise_on_error=False`. On a non-admin key that call 403s and is swallowed, so the server never learns the schema and the step never appears in the registry. At bind time a missing credential is a crash loop; at runtime it is silence. Pre-syncing in `fleet-register` is what keeps the runtime path from having anything left to register.

**This is a default flip on a public API, so it is a major-version change** in `sdks/python`, with a release note naming the 409 and the merge semantics, and a one-line migration for anyone who genuinely wants latest-init-wins.

### 4.4 Option C, a registration-only credential tier. Deferred, and honestly

The clean answer is a key that may register an agent and its steps and may not touch controls. It is not expressible. `AccessLevel` has three values, `DEFAULT_OPERATION_ACCESS` maps them per operation, and there is no per-key operation allowlist; `task-dispatcher.md` section 4 establishes this and section 15 prices the allowlist at 3 days. `Principal.scopes` exists but is populated by providers surfacing a runtime-token grant, not by the header path.

With 4.3, the fleet's *executor* key needs only `agents.create` at `AUTHENTICATED`, which every ordinary key has, so the tier is no longer load-bearing for the executors. It remains what would close the residual on `fleet-register`, which today needs full admin to write a step. Sized where it already is, not re-sized here.

### 4.5 What all of this is worth when `api_key_enabled` is false

`config.py:74` sets `api_key_enabled: bool = False`, `docker-compose.yml` defaults it false, and with it off the authorizer is `NoAuthProvider`, whose entire `authorize` returns a `Principal` with the default namespace and no scopes. Every operation succeeds, including ADMIN ones, and `caller_id` is never set. In that state the whole of section 4 is theatre: the executors do not need an admin key because nothing needs any key, and every credential-hash separation in the dispatch design (`claimed_by_hash`, `created_by_hash`, the self-approval refusal) compares values that are either None or identical.

**The refusal, and where it can actually be enforced.** Compose has no pre-profile hook, and `check_executor_startup_requirements` (`config.py:699`) governs the server process, not the fleet, so "the profile refuses to start" is not a thing a compose file can say. The refusal goes in `fleet-register`, which runs first, holds a credential, and blocks every executor through `service_completed_successfully`. It probes the server for an ADMIN-tier read **with no credential at all** and exits non-zero if that succeeds, which detects the condition behaviourally rather than by trusting a config field it cannot see. The generator emits a matching check, and the plan says that one is advisory, because a stale generated file bypasses it.

Not because eight processes are more dangerous each than one, but because eight is the number at which "I will turn auth on later" stops being recoverable in an afternoon. `AGENT_CONTROL_EXECUTOR_ALLOW_INSECURE_LOCAL_DEV` is honoured for the single-executor dev path and is not honoured here, which is the same asymmetry orchestration 6.4 applies to executor restart.

---

## 5. Per-team budgets

### 5.1 Where it is enforced, and what a turn actually is

Same place as the namespace budget, for the same reason, and moving it would repeat a mistake `task-dispatcher.md` 12.1 already corrected once. The enforcement point is `charge_dispatch_turn` in `services/agent_dispatch_state.py`, called from `_acquire_turn` in `services/agent_turns.py` inside the one short transaction that takes the session row, and only for sessions with `agent_task_id IS NOT NULL`.

```sql
CREATE TABLE agent_team_dispatch_budgets (
    namespace_key       TEXT    NOT NULL,
    team_slug           TEXT    NOT NULL,
    max_tasks_per_hour  INTEGER NOT NULL DEFAULT 5,
    max_turns_per_hour  INTEGER NOT NULL DEFAULT 15,
    max_concurrent_turns INTEGER NOT NULL DEFAULT 1,
    turns_window_start  TIMESTAMPTZ NOT NULL DEFAULT now(),
    turns_in_window     INTEGER NOT NULL DEFAULT 0,
    paused_at           TIMESTAMPTZ,
    paused_by           VARCHAR(64),
    paused_reason       VARCHAR(500),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_key, team_slug)
);
```

`charge_dispatch_turn` grows a second `INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING` against this table, written from the same `_WINDOW_EXPIRED` fragment so the roll condition and the charge condition cannot drift, exactly as the existing statement is. It runs after the namespace charge and before the per-agent concurrency count. Every refusal in `_acquire_turn` unwinds without committing, which un-charges both counters, and that is what makes charging two rows safe rather than merely convenient.

**`max_turns_per_hour: 15` is not a spend ceiling, and the row has to say so where somebody reads it.** One charge is one `POST /run`, and one `POST /run` is an entry into a loop that can call the model and its tools an unbounded number of times before returning. The only bound on what happens inside is `turn_timeout_seconds`, default 300. So a team with one turn left can outspend a team with fifteen, and the per-team numbers do not order teams by cost even approximately. `task-dispatcher.md` 12.1 makes this point about the namespace row; it is exactly as true one level down, and repeating it is cheaper than letting an operator rediscover it from a bill.

**`max_concurrent_turns` is the one ceiling here that bounds instantaneous pressure**, and it is why the column exists. It is counted in the same statement as the existing per-agent in-flight count, over the same `in_flight_trace_id` predicate, filtered to the team's tasks. `max_concurrent_tasks_per_agent` is pinned `le=1` in `config.py:614`, so today it bounds one agent; four teams with two agents each still means the namespace can have eight turns in flight and one team can own four of them. Default 1, which is the honest starting value for a deployment whose upstream is one subscription.

Settings, `AGENT_CONTROL_DISPATCH_` prefixed like the rest of `DispatchSettings`:

- `DEFAULT_TEAM_MAX_TASKS_PER_HOUR` (5), `DEFAULT_TEAM_MAX_TURNS_PER_HOUR` (15), `DEFAULT_TEAM_MAX_CONCURRENT_TURNS` (1), seeded on first insert. The row is authoritative afterwards, matching `default_max_tasks_per_hour`.
- `REQUIRE_TEAM` (default false). Section 5.4.
- A model validator refusing a configuration where the seeded team defaults times the number of teams undershoots the namespace ceiling by more than a stated margin, because a namespace ceiling nobody can reach reads as protection and is not. Same class of refusal as `_fleet_must_not_squeeze_human_chat_out_of_the_session_ceiling` (`config.py:623`), which is the precedent for putting arithmetic like this in `config.py` rather than in a runbook.

**Lock ordering, stated so a reviewer does not have to reconstruct it.** The docstring on `charge_dispatch_turn` already explains that the namespace charge "takes an exclusive lock on this namespace's one state row and holds it to commit, so every concurrent dispatch turn in the namespace is serialized behind it", and that the per-agent count runs afterwards for exactly that reason. Every dispatch turn therefore takes the namespace row first. A per-team row taken second yields a total lock order and cannot deadlock across teams. The cost is one extra lock acquisition behind a lock that already serializes everything, which is a much smaller claim than "two hot rows".

**Rejected: one row per `(namespace, team)` with the namespace ceiling derived as a sum.** It looks cheaper on the lock and it is, which is the problem: removing the single namespace row removes the serialization the per-agent in-flight count depends on, turning a documented invariant into a read-then-write race. The extra lock is the feature.

### 5.2 How the turn path learns the team

Not from the session. Measured: all five dispatch sessions have `team_id` NULL, because `DispatchClient.create_session` posts only `agent_name`, `title` and `task_key`.

**The team is resolved from `agent_tasks.team_slug` by joining on `agent_sessions.agent_task_id`,** in the same transaction, in the same statement that charges. That is the value the import handler committed against a scope the operator confirmed, and a session edit cannot re-point it afterwards. Passing `team_slug` on session creation would be a value chosen by the process being budgeted.

The dispatcher should *also* start sending `team_slug` on `POST /agent-sessions` so the console can filter the step rail by team. This is display and not enforcement, said here and again in the code, because a field on the session row is exactly the field a later reader simplifies the join into.

### 5.3 Which team pays for a cross-team workflow

Measured: `plan-critique-execute` has `team_slug = marketing` and its second step names `engineering_reviewer`, who belongs to `engineering`.

**The task's team pays. Not the agent's team.** The team that pressed play authorized the spend; the team whose agent appears in a step did not. Charging the agent's home team would let any team drain any other team's hourly budget by naming its agent in a workflow step, which turns a budget into an attack surface reachable by anyone holding `agent_workflows.write`.

The cost is that `engineering`'s budget does not reflect work its agent actually did. The per-team numbers are a spend-authorization ledger, not a utilization report, and the console labels them "authorized by" rather than "used by". Put it on the screen, because the number will otherwise be read as the other thing.

### 5.4 A task with no team

Measured: the file-sourced task has `team_slug` NULL. `once --source file://tasks.yaml` does not require `--team`, and `sales-outreach` has no `linear_team_key`, so a teamless task is the normal state of two supported paths.

**A NULL team charges the namespace pool only.** `REQUIRE_TEAM=true` turns it into a refusal, with a written error naming the setting, for a deployment that wants every turn attributable. Default false so nothing that works today stops working.

### 5.5 What happens when a team exhausts its share

429 with `retry_after_seconds`, the same shape the namespace ceiling produces, with a distinct error code so the message can say which ceiling and whose. The dispatcher already handles it: `paused_quota` is in the reclaim predicate, the task keeps heartbeating, and it resumes after the window rolls. Nothing new in the loop.

Two additions, labelled as optimisations in the code and here. `GET /agent-dispatch` grows a `teams` block so the import preview and the dispatcher's opening lines can report a team's remaining allowance rather than only the namespace's; advisory, enforcement stays in `charge_dispatch_turn`. And `POST /agent-tasks/import` counts `max_tasks_per_hour` per team in the same inserting transaction it already uses for the namespace ceiling, refusing with 429 and inserting nothing. That one is enforcement, and it belongs there for the same reason the namespace version does: tasks are created only by import.

### 5.6 Per-team accounting does not create per-team capacity

This has to be the last word, because everything above will otherwise read as a fix.

All four teams reach one model endpoint: `http://127.0.0.1:10531/v1`, one `npx openai-oauth` process, one consumer subscription. That hop has no per-caller identity, no API key, and no way to attribute a request to a team. Probed: `/v1/models` answers 200 and `/v1/files` answers 404, so it is not a service with per-tenant surface hiding behind an unused feature. It is one queue.

So `marketing` burning through the upstream rate limit returns errors to `engineering`, and no arrangement of rows in `agent_team_dispatch_budgets` changes that. Per-team budgets bound what each team is *authorized* to spend from a shared pot. `max_concurrent_turns` bounds how much of the queue one team can occupy at an instant, which is the only fairness lever available on this side of the hop, and it is a lever on contention rather than on capacity.

The only thing that creates per-team capacity is a per-team upstream credential with its own quota. This deployment does not have one and cannot have one on a consumer subscription. That is a purchasing decision, not an engineering one, and the runbook says so in those words rather than implying the budget table solved it.

---

## 6. Compose lifecycle

### 6.1 What comes up on `docker compose up`

Postgres and the server. Unchanged. That is the published quick start, it pulls one image from Docker Hub, and adding eight containers that need a model endpoint to the out-of-box experience is exactly what orchestration section 5 refuses for one.

`agent-dispatcher` stays as it is: present, `restart: "no"`, default command `--help`, exits 0 having done nothing. The comment block explaining why is correct and stays. Two small corrections ride along in Phase 1: it gains an explicit profile so the comment's claim becomes true, and it loses its `build:` block in favour of a make target, so it stops participating in every `docker compose build`.

### 6.2 How the fleet profile is actually wired

`docker compose --profile fleet up -d` will not start a generated file that compose has never heard of. Compose loads `docker-compose.yml` plus `docker-compose.override.yml` and nothing else, and a profile matching no service prints no warning, so the failure is silent. So `docker-compose.yml` gains:

```yaml
include:
  - compose.fleet.yml
```

`include:` needs Compose 2.20 or newer; local is v5.1.4. The generated file is committed, because an included file that does not exist is a hard error on every `docker compose up`, including the quick start. A committed generated file also means the generator's output is reviewable in a diff, which is how a fleet change gets noticed.

**One CI assertion in Phase 1, because the claim that the default path is unchanged has to be checkable:** `docker compose config --services` with no profile lists exactly `postgres`, `server`, `agent-dispatcher`; with `--profile fleet` it lists those plus one service per `fleet.yaml` entry plus `fleet-register`. And `docker compose build --dry-run` builds nothing from the fleet profile, which is what the image-only rule buys.

### 6.3 The proxy constraint, resolved

The constraint as usually stated is that a container cannot reach `127.0.0.1:10531`, so it needs `host.docker.internal`. True and measured: from inside `agent_control_server`, `http://host.docker.internal:10531/v1/models` returned 200.

The sharper question is how to do that without putting a consumer-subscription credential into eight containers. **There is no credential to put there.** The proxy holds the OAuth session in the host process and accepts requests with no API key; the example agent's own comment says the local proxy "authenticates upstream itself" and passes `api_key="not-used-by-local-proxy"`. What the containers get is an address.

**That is the good news and it is also the whole risk.** The address *is* the credential. Anything that can open a TCP connection to `host.docker.internal:10531` spends the subscription, with no per-caller identity and therefore no per-team accounting at that hop, which is 5.6 arriving from the other side. Three consequences, all configuration:

The proxy binds loopback only. Bound to `0.0.0.0` it lets every host on the operator's network spend the subscription, and the fleet's `extra_hosts` entry does not cause that but does make it easy to stop noticing.

`AGENT_CONTROL_FLEET_MODEL_BASE_URL` is set once, in `.env`, and interpolated into every generated service. The generator refuses a value whose host is `127.0.0.1` or `localhost`, with an error naming `host.docker.internal`, because that mistake otherwise surfaces at runtime as a connection error nobody attributes correctly.

`EXA_API_KEY` is the only real secret copied N times, it is optional, and `AGENT_CONTROL_WEB_TOOLS=0` means the toolset is never constructed. `sales_outreach_drafter` is set that way in 3.3 as the worked example, since its product is drafting text.

### 6.4 Health checks, restart policy, ordering

*Health.* The `/list-apps` identity assertion from 3.3. Liveness plus a name, never a control-cache check; section 8 says what it cannot see.

*Restart.* `unless-stopped`, not `always`. `always` restarts a container the operator stopped, which is level 4 of the fleet stop being quietly undone by the orchestrator. `unless-stopped` survives a daemon restart and respects a deliberate `docker compose stop`.

*Ordering.* Executors depend on `server: service_healthy`, `postgres: service_healthy`, and `fleet-register: service_completed_successfully`. The server dependency is load-bearing rather than tidy: the example calls `agent_control.init()` at module import and then `plugin.bind(root_agent)`, and eight containers racing an unready server is eight crash loops with a message about running `setup_controls.py`.

**The `server` service has no healthcheck today.** Adding one is a prerequisite and a four-line config change against the existing `/health` route (`main.py:581`):

```yaml
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"]
      interval: 10s
      timeout: 5s
      retries: 6
      start_period: 30s
```

`depends_on` plus a healthcheck does not remove the need for the bounded retry around `_sync_steps_blocking` that orchestration 9.6 asks Phase 1 for. Compose orders startup; it does not keep the server up, and a control plane that blips for two seconds should not leave eight agents down.

---

## 7. Runtime registration

### 7.1 One job, one admin credential, one lifetime

Not the executor. Self-registration needs `agent_runtimes.write`, which orchestration 6.1 puts at ADMIN because binding an agent to an executor URL is deployment configuration, and handing that to the process running model-driven agent code is the escalation section 4 just spent a page removing.

**`fleet-register` does three things and exits.** It registers each agent in `fleet.yaml` and syncs its step schemas by importing the agent module under that agent's environment (4.3); it writes each binding with `PUT /agent-runtimes/{agent_name}`; and it refuses to proceed when the server accepts an uncredentialed ADMIN read (4.5). Then it terminates, before any agent code that will handle a turn exists anywhere in the profile.

That is not a novel pattern here. `docker-compose.dev.yml` already ships `adk-db-init` with the argument written out: "Superuser credentials for provisioning only. This container exits before any agent code exists; the executor service never gets them."

```yaml
  fleet-register:
    image: agent-control-executor:local        # the executor image: it must import agent modules
    profiles: ["fleet"]
    entrypoint: ["python", "-m", "agent_control_fleet.register"]
    command: ["--fleet-file", "/app/fleet.yaml"]
    environment:
      AGENT_CONTROL_URL: http://server:8000
      AGENT_CONTROL_API_KEY: ${AGENT_CONTROL_FLEET_REGISTER_API_KEY:?one admin key required}
    volumes: [./fleet.yaml:/app/fleet.yaml:ro]
    depends_on:
      server: {condition: service_healthy}
    restart: "no"
```

**It runs the executor image, not the dispatcher image.** Syncing steps means importing the agent module and letting `bind()` discover the tools, which needs the agent's dependencies. An earlier draft put it in the dispatcher image on the grounds that both are thin HTTP clients; that was true of the binding write and false of the step sync.

**It is not the dispatcher and must never be folded into it.** If the two ever share a *process*, the dispatcher has acquired an admin credential by convenience, which is the failure section 9 names. Sharing an image with the executor is not sharing a credential: the register job gets the admin key and dies, the executors get an ordinary key and live.

**`AGENT_CONTROL_FLEET_REGISTER_API_KEY`, and not `AGENT_CONTROL_ADMIN_API_KEY`.** The server's real setting is `AGENT_CONTROL_ADMIN_API_KEYS`, plural, comma-separated (`config.py:82`, parsed at `config.py:98`). A slot one character off from it, sitting in the same `.env`, gets a comma-joined list pasted into it and produces a 401 that reads as a server fault. The job refuses a value containing a comma and documents that it takes exactly one key drawn from the admin list. The dispatcher has the same bug today at `docker-compose.yml:116`, `${AGENT_CONTROL_DISPATCHER_API_KEY:-${AGENT_CONTROL_API_KEYS:-}}`, which degrades to a joined list whenever more than one ordinary key is configured; it is fixed in the same change, with a smaller blast radius.

### 7.2 The URL and the app name the job writes

`base_url` is `http://ac-exec-<agent-name-with-hyphens>:8000`, derived from the same generator input that named the container. `validate_executor_base_url` (`models/src/agent_control_models/agent_runtimes.py:47`) accepts it: `http` scheme, a host, no credentials, no query, no fragment. `executor_app_name` is the agent name verbatim, which 3.4 made routable.

Nobody types a port again. Adding a team adds entries to `fleet.yaml`, and "which port is `ops_incident_triage` on" stops having an answer.

### 7.3 Adoption: the first run rewrites eight rows and does not refuse

All eight rows today carry a hand-written `http://host.docker.internal:80NN` URL and `executor_app_name = 'my_agent'`. Every one of them is a mismatch against what the generator would write. A register job that aborts on any mismatch it did not itself write therefore aborts on row one of its own first run, writes nothing, and produces an error implying the fleet is contested when it is simply new. The obvious operator response is to hand-edit `agent_runtimes` to get past it, which is exactly the drift the abort exists to prevent.

**So adoption is an explicit one-time gesture.** `fleet-register --adopt` rewrites the rows for agents named in `fleet.yaml` regardless of their current value. Without `--adopt`, a row whose `base_url` differs from the generated one aborts the whole run and names the row. After adoption every row matches, so any later mismatch is genuinely foreign, and the abort means what it says. Phase 3 carries adoption as a named migration whose stated input is the eight host-port rows.

The one-time reset also covers the app-name rename. The five existing `agent_sessions` rows carry `executor_app_name = 'my_agent'` and point at ADK sessions that live only in process memory (2, 3.5), so they are already no more durable than a restart. They are marked `orphaned` by the migration rather than left to fail at the next turn, and `_require_runnable_status` already knows that status. Note the direction on the constraint: `uq_agent_sessions_executor_global` is `(executor_app_name, executor_user_id, executor_session_id)`, so per-agent app names make it strictly easier to satisfy.

### 7.4 Reconciliation: `fleet doctor`

A read-only subcommand comparing three sources, which refuses to fix anything:

| Finding | Meaning |
|---|---|
| Agent in `fleet.yaml`, no row in `agent_runtimes` | The register job did not run, or failed. Session open answers 409 `AGENT_RUNTIME_NOT_BOUND`. |
| Row in `agent_runtimes`, agent absent from `fleet.yaml` | A binding with no intended process. The shape of a stale row. |
| Row whose `base_url` does not match the generated name | Hand-edited, or pre-adoption. All eight rows are in this state today. |
| Registered agent in neither | `google-adk-plugin`. Informational, not a fault. |
| `executor_app_name` not equal to `agent_name` | Pre-3.4 row. Session open will 404 against a fleet container. |
| `/list-apps` on a bound executor not equal to `[agent_name]` | The container is serving a different agent, or a pre-3.4 image. |
| Two teams sharing one `linear_team_key` | `marketing` and `operations` on OPS. Warning, never an error. |
| Team with no `linear_team_key` | `sales-outreach`. Informational: the milestone path is unavailable to it. |

It refuses to fix anything because the fix for half of these is to delete a binding, and a tool that deletes bindings needs `agent_runtimes.write`, which is where 7.1 just decided not to put a long-running process.

---

## 8. Edge cases, each with its decided behaviour

| Case | Behaviour |
|---|---|
| Executor up but wedged | `/list-apps` answers from the ADK app registry and says nothing about the plugin's control cache, so the healthcheck sees a healthy container even with the identity assertion. The refresh loop logs "Failed to refresh controls; keeping previous cache" and runs the old control set indefinitely, which `task-dispatcher.md` 12.3 documents. Compose cannot catch it. The dry-run canary per task catches it; the real fix is the control-set generation counter already sized in that plan's Phase 8. The fleet's contribution is that recovery is `docker compose restart ac-exec-<agent>` rather than finding a pid. |
| Wedged mid-turn | The turn 504s at `turn_timeout_seconds` (300), releases with `turn_ended=False`, keeps `in_flight_trace_id`, and the task goes `running_unknown` and is never retried. Unchanged and deliberately so: a retry of a step that may have acted is refused permanently. |
| Stale row, dead name | 503 `EXECUTOR_UNAVAILABLE`. An outage, correctly reported. In the containerised topology the name does not resolve, which is the same class of failure with a clearer message. |
| Stale row, reused port, different agent | The case that matters, because it is a mis-execution rather than an outage. Today: `executor_app_name` is `my_agent` on all eight rows, `AdkExecutorClient._headers` names no agent, so the request is accepted, the wrong plugin evaluates it, and `control_execution_events` records the *executing* agent faithfully and wrongly. After 3.4 the app name is the agent name, the wrong container has no such folder, and the session path 404s into `ExecutorSessionNotFoundError`. Port reuse is a property of a flat host port space, which 3.3 deletes. The residual is two containers deliberately given the same `AGENT_CONTROL_AGENT_NAME`, which the generator cannot emit and `fleet doctor` reports. |
| Two teams sharing one `linear_team_key` | Both teams' milestone panels show OPS milestones. The partial unique index `ux_agent_tasks_open_source_ref` on `(namespace_key, source_kind, source_ref)` means the issue imports once; the second press previews `already_queued` and the first team's budget pays. Not an error, because it may be a deliberate migration state. `fleet doctor` warns, and `LinkLinearTeam` gains one line naming the other team already on that key. |
| Team with no `linear_team_key` | `sales-outreach` today. No milestone rows render, so no play control exists, and import refuses independently with 409 `TEAM_NOT_LINKED`. The file source and per-team budgets both still work for it, which is why the budget is not tied to the Linear key. |
| Agent registered with no runtime | `google-adk-plugin`. 409 `AGENT_RUNTIME_NOT_BOUND` at session open, from `require_enabled_binding`, which runs the registration check first so an unknown agent never reads as a configuration gap. `fleet doctor` reports it as informational. |
| Model endpoint down for the whole fleet | Every turn fails identically and the budget is spent on failures: `_acquire_turn` commits the charge before the executor is contacted, by design, so sixty immediate failures consume the hour. **No refund path**, because a refund is a write on a failure path and it double-refunds under exactly the retry storm that produces it. Instead the dispatcher stops its pass after N consecutive `EXECUTOR_*` failures, which is advisory and lives in the process. Honest residual: the budget is a spend ceiling, not an outage detector, and an outage still burns it. |
| Fleet restarted mid-chain while a dispatcher holds a lease | The lease is `task_lease_seconds`, default 1800, and restarting executors does not touch it, so a task can sit `running` for up to thirty minutes before reclaim. With the Postgres session backend from 3.5 the ADK session survives, so a resumed step is not starting from an empty transcript. The runbook orders the sequence rather than offering one command: pause dispatch (level 1, independent of the dispatcher), let in-flight steps drain or accept losing them, restart, unpause. There is deliberately no `fleet restart` command, because a single command hides the pause and the pause is the part that matters. |
| Fleet restarted with in-memory sessions (host fallback) | Every `executor_session_id` for that process is invalid; the next turn 404s and the row moves to `orphaned`. This is the current behaviour of all eight processes and the reason 3.5 exists. |
| Per-team budget exhausted mid-chain at step 2 of 3 | 429 with `retry_after_seconds`; the task goes `paused_quota`, keeps heartbeating, is reclaimable, and resumes at the same step when the window rolls. Steps 1 and 2 are done and their write-backs stand. Mitigation, and it is an optimisation: at claim time the dispatcher compares the resolved chain's remaining turn requirement against the team's advisory remaining allowance and declines to start a chain that provably cannot finish this window. Advisory, in the process being budgeted, not enforcement. |
| Namespace budget fine, team budget zero | Refused with the team's error code and `retry_after_seconds`. The console banner names which ceiling, because "budget exhausted" against a namespace figure showing 40 remaining is the report that gets filed as a bug. |
| Team at `max_concurrent_turns` | 409, same shape as `AGENT_CONCURRENCY_EXCEEDED`, distinct code. The task is not failed; the dispatcher takes the next one. |
| Task with no team | Charges the namespace only. `REQUIRE_TEAM=true` refuses it. Default false. |
| Cross-team workflow step | The task's team pays. Section 5.3. |
| Scaling past the host port range | Does not arise: no host ports are published. On the host-supervisor fallback it does, and that path's documented ceiling is whatever range the operator allocates, stated as a limitation of the fallback rather than of the design. |
| Adding a ninth agent | One entry in `fleet.yaml`, regenerate, `up -d`. `fleet-register` registers the agent, syncs its steps under the admin key, writes the binding, and exits; the new executor boots non-admin and finds every step present. No port allocation, no hand-edited row, no restart of the other eight. **The step sync is the part that makes this work and the part an earlier draft omitted.** |
| Adding a fifth team | Teams are rows, not processes. What a team needs is agents, and each agent is one `fleet.yaml` entry, so the path is the row above, twice. The `DispatchSettings` validator refuses a per-team default that oversubscribes the namespace ceiling, which is where the operator learns that adding a team means deciding whose allowance shrinks. |
| Executor restarts against an unchanged registration | Strict, unchanged payload, no new step keys, `agent_created_at` preserved, so no `AGENTS_UPDATE` path and no admin. Succeeds on the fleet's ordinary key. |
| A tool is added to the shared example | Every executor's `bind()` would need `AGENTS_UPDATE`. It never gets there: `fleet-register` runs first with the admin key and syncs the new step for all eight, and the executors short-circuit. Forgetting to rerun the job means eight crash loops with a 403, which is loud and correct. |
| A developer changes a tool signature | 409 `SCHEMA_INCOMPATIBLE` from `fleet-register` under strict. Deliberate. The fix is an explicit admin `conflict_mode="overwrite"`, which is what replacing a registration should cost. |
| A developer deletes a tool | The step stays in the registry. Strict merges. Pruning needs an admin overwrite. Documented on the SDK docstring, not discovered. |
| `AGENT_CONTROL_WEB_TOOLS` flipped 0 to 1 in `fleet.yaml` | A step addition, because `bind()` registers the toolset object as a step. Same path as adding a tool: rerun `fleet-register`. Worth naming because it looks like a config flag and is a registration change. |
| `api_key_enabled` false | Every operation succeeds unauthenticated under `NoAuthProvider` and every credential separation in this plan is inert. `fleet-register` detects it behaviourally and exits non-zero, which blocks every executor through `service_completed_successfully`. The insecure-local-dev hatch does not apply. |
| Fleet executor key leaked | It is an ordinary key: it can register agents, open sessions and spend within the namespace budget. It cannot create or unbind controls, and it cannot add a step. That is the whole delta from today, and it is the point of the plan. |
| Register job fails halfway | Some agents registered, some not. Idempotent, so re-running completes it. Executors stay down, because the dependency is `service_completed_successfully`. `fleet doctor` names which agents lack a row. |
| Two fleets against one server | Two generators, two name prefixes, one `agent_runtimes` table keyed `(namespace_key, agent_name)`, so the second fleet would silently repoint the first's bindings. Refused: after adoption (7.3), any `base_url` that is not the one this fleet would write aborts the run and names the row. |

---

## 9. What this refuses to do

**It does not make milestone scope schedulable.** The property is not "neither `once` nor `serve` can construct one", because that is a list of two subcommand names and somebody will add a third. The property lives in the endpoint: `POST /agent-tasks/import` accepts `scope.kind == "linear_milestone"` only under `mode: "commit"` carrying an `expected_refs_digest` over a set produced by a preview the caller was shown, refusing with 409 `SCOPE_CHANGED` otherwise. No non-interactive caller can construct that regardless of what process it runs in. Cron reaches only the team-wide label source, where, in `task-dispatcher.md`'s words, "the human press is gone and the label is the sole gate".

The fleet-specific clause, and it is checkable in the generator: **no long-running service in the fleet profile holds a credential carrying `agent_tasks.write`.** Executors do not need it. `fleet-register` holds admin, which under a three-level model does carry it, and that is precisely why the job registers and dies rather than idling: a process that both holds admin and runs on a timer could preview and then commit, which is forging the press by doing it twice. Nothing here adds a "start all teams" control, a fleet-wide play button, or a container with a milestone id in its arguments. **If a future reviewer finds any of those traceable to this plan, the plan has been violated.**

**It does not dissolve the dispatcher's non-admin requirement.** The admin credential lives in exactly one place, a one-shot container that exits before any turn-handling agent code runs. The dispatcher never holds it, the executors stop holding it, and `dispatch preflight` still refuses an admin key when it is written. Sharing an image with the register job is not sharing a process.

**It does not remove the one-agent-per-process constraint.** Section 3.2 prices it at two to three weeks of SDK surgery plus a permanent widening of blast radius, and refuses.

**It does not create per-team model capacity.** Section 5.6. One subscription, one proxy, no per-caller identity at that hop.

**It does not add per-namespace executor isolation.** Multi-tenant deployments with tool egress need it; orchestration already flags it; this plan is single-namespace by construction, since `HeaderAuthProvider._resolve_namespace_key` returns the default for every caller.

**It does not build the supervisor.** Executor restart, the derived per-agent secret and the identity route are orchestration Phase 5's work. This plan folds one assertion into that work and does not claim it.

**It does not autoscale, and it does not make a second dispatcher useful.** Both are named non-goals in `task-dispatcher.md` 17 and neither becomes easier here.

**It does not put the model endpoint anywhere near the control plane.** `agent-system-prompts.md` already settled that a per-agent `api_base` is data exfiltration wearing a config field. The endpoint stays in the executor's environment, one value, interpolated.

---

## 10. Phases and effort

One engineer, including tests, in this repo's convention. Configuration and real work are separated per phase, because conflating them is how a two-day phase becomes a two-week one.

**Phase 0, two probes that can change the plan. 1 day.**

*Probe A, the app name.* Build a scratch image that materializes `/agents/marketing_researcher/agent.py` as a shim over the installed example, run it, and confirm `/list-apps` returns exactly `["marketing_researcher"]` and that `POST /apps/marketing_researcher/users/u/sessions/s` succeeds while `POST /apps/my_agent/...` 404s. Nothing writes `executor_app_name` until this passes, because the whole mis-execution defence rests on it.

*Probe B, the credential.* Not a restart of the eight already-synced executors: `_sync_steps_async` short-circuits on empty `pending_steps` and that experiment can only pass. Instead, on a non-admin key: register a **brand new** agent with tools and confirm the 403 at `bind()`; then add one tool to an existing agent and confirm the same. Then run the `fleet-register` sync under the admin key and confirm both cases boot clean afterwards. This is the entry gate for Phase 2, and it is the experiment the earlier draft got wrong.

**Phase 1, generator, profile and wiring. 3 days.** `fleet.yaml`; `scripts/gen_fleet_compose.py` with a golden-file test; `include: [compose.fleet.yml]` in `docker-compose.yml`; the `server` healthcheck; profiles and the `build:` removal on `agent-dispatcher`; the `AGENT_CONTROL_FLEET_REGISTER_API_KEY` naming and the dispatcher's key-fallback fix; the CI assertion on `docker compose config --services` under both profiles. Roughly 150 lines of generator, the rest compose YAML. **Mostly configuration.**

**Phase 2, the SDK default flip. 2 days.** `conflict_mode` default to `"strict"` in `sdks/python`; a docstring covering the 409 and the monotonic registry; tests for restart-unchanged on a non-admin key and for the 409 on a changed schema; a major-version bump and a release note; rewriting `examples/google_adk_plugin/.env.example` and the `.env` comment that currently explains why an admin key is needed. **Real work, small.** Entry gate: Probe B. On its own it does not remove admin, and the phase note says so, so nobody ships it and declares the problem solved.

**Phase 3, the executor image, registration and adoption. 1.5 weeks.** A Dockerfile for the example agent plus the entrypoint that materializes one agent package and starts `adk api_server /agents`; the `exec-health` identity probe; `agent_control_fleet.register` with agent registration, step sync, binding write, the uncredentialed-ADMIN refusal, and `--adopt`; the session-service URI wired to the `adk` role `adk-db-init` already provisions; per-agent `executor_app_name` with its orphaning migration; `fleet doctor`; the runbook covering the ordered fleet restart, the proxy binding and adoption. **Real work, and it grew from the earlier draft's one week** because 3.4 and 4.3 are both real engineering rather than one-line edits. Packaging in this repo has been priced at zero once before and orchestration section 15 says so.

**Phase 4, per-team budgets. 1.5 weeks plus 2 days.** `agent_team_dispatch_budgets` and its migration; the second charge statement and the concurrency predicate inside `charge_dispatch_turn`; the task-to-team resolution in the same transaction; per-team `max_tasks_per_hour` in the import transaction; the `teams` block on `GET /agent-dispatch`; the settings and the oversubscription validator; the console banner naming which ceiling and the "authorized by" label; the dispatcher's advisory chain-fit check and its consecutive-failure circuit break. The 2 days is TypeScript SDK regeneration, which `make sdk-ts-generate-check` gates in CI and is therefore mandatory rather than scope. **Real work, and the two-row charge on the turn path is the highest-risk code here.**

**Phase 5, deferrable. 3 days.** The identity assertion folded into orchestration's supervisor route, and `fleet doctor` promoted from a CLI subcommand to a read-only server route so the console can render it. Worth doing only once the supervisor exists.

| Phase | Effort | Kind |
|---|---|---|
| 0. Two probes | 1 d | Measurement, gates 2 and 3 |
| 1. Generator, profile, wiring | 3 d | Mostly config |
| 2. SDK default flip | 2 d | Real work, small |
| 3. Image, registration, adoption | 1.5 wk | Real work |
| 4. Per-team budgets | 1.5 wk + 2 d | Real work |
| 5. Identity and doctor route | 3 d | Deferrable |

Phases 0 to 4: **roughly 4 to 4.5 weeks.** With Phase 5, about 5.

### 10.1 The minimum useful slice

**Phase 0's Probe B plus the step-sync half of `fleet-register`, run once by hand. Roughly 2 days.** No containers, no generator, no compose changes.

Run the sync job against the eight running host processes with the admin key, flip `conflict_mode` to strict, and restart the eight with an ordinary key. That is the smallest change that removes ADMIN from eight processes running model-driven agent code, and it is the highest ratio of risk removed to effort spent in this document. Ports stay at 8080-8087, the budget stays namespace-wide, `executor_app_name` stays `my_agent`, and none of that is worse than it is today.

---

## 11. Riskiest assumptions

**That the per-agent package shim behaves like the example does today.** Probe A checks routing and session creation. It does not check that `envs.load_dotenv_for_agent` looking in a folder with no `.env` is harmless, or that ADK's hot-reload watcher is indifferent to a folder written at container start, or that `App` metadata attached by `_record_origin_metadata` from the folder name matters to anything downstream. The shim is three lines and the failure modes are all at import time, which is the good kind, but this is the plan's newest mechanism and it sits under the mis-execution defence.

**That flipping the SDK default to strict does not break real consumers.** Verified against this repo's tests and this repo's example, and not against anyone else's agent. The failure is a 403 at `bind()` for anybody whose code has grown a step since its last registration, which is silent today only because everyone is holding an admin key. Probe B measures it on the agents that exist here. It cannot measure it anywhere else, which is why the change is a major version.

**That a containerised executor reaches the proxy on the operator's platform.** Probed here, from `agent_control_server`, on Docker Desktop for macOS: 200. **Not verified on Linux**, where `host.docker.internal` needs the `host-gateway` mapping and where a proxy bound to `127.0.0.1` is genuinely unreachable from the bridge network no matter what the mapping says. The fallback for a Linux operator is the host-supervisor path from 3.3, with its port range and its in-memory sessions. Half a day to settle; it gates Phase 3's usefulness rather than its correctness.

**That two hot rows on the turn path is acceptable.** The lock-ordering argument in 5.1 says why it is one extra acquisition behind an existing serialization point rather than new contention, and the rejected alternative would break the per-agent count. But `agent_turns.py`'s docstring is proud of how little that transaction does, and the defence that dispatch turns are rate-limited by the very thing being checked is now being made twice about the same transaction. Somebody who cares about that file should say whether the second row is worth it.

**That `fleet.yaml` stays the source of truth.** The first time somebody adds an agent by hand-editing `agent_runtimes` because it is faster, 7.4 starts reporting a fault that is actually the operator's intent, and a doctor tool that cries wolf gets ignored. The mitigation is that the register job aborts on a post-adoption `base_url` it did not write, converting drift into a refusal at the next `up` rather than a warning nobody reads. Whether that is too strict for a real operator is the thing a reviewer should push on hardest, because it is the one place this plan chooses friction over convenience without a safety argument underneath it.

**That routing step registration through an admin job is a boundary and not a bottleneck.** Every tool change now needs a job run before eight processes will boot. That is correct as a security property and annoying as a development loop, and the annoyance is what gets engineered around. The mitigation is that `fleet-register` is idempotent, fast, and part of `up`; the risk is that somebody adds `--auto-register` to the executor entrypoint to skip it, which quietly restores an admin key to eight processes and undoes section 4 entirely.

---

## 12. Open questions a reviewer should push on

**Should `steps_changed` really demand ADMIN?** 4.3 accepts the existing gate and works around it. The counter-argument is that adding a step is additive: it cannot remove an evaluator, and the deny-by-default tier-1 control from `task-dispatcher.md` 12.2 already refuses any tool no allowlist names, so a newly-registered step is not thereby callable. If that argument holds, a narrower gate (admin for step *removal* or *schema change*, `AUTHENTICATED` for pure addition) would delete `fleet-register`'s step-sync responsibility and most of Phase 3's registration work. It is not proposed here because it changes a deliberate test and because `_ensure_step_known` would then let a running agent register steps at will. Somebody who owns the control engine should decide.

**Is one container per agent right at forty agents?** Eight is comfortable. Forty is forty Python processes each holding a LiteLLM client and an MCP session, on one host, and the answer is probably a scheduler rather than a bigger compose file. This plan is honest that it solves the eight-to-fifteen range and does not claim the shape survives past it.

**Is `max_concurrent_turns` per team the right fairness lever, or is it just a smaller queue?** It bounds occupancy of a shared upstream that has no notion of teams. Against one subscription that may be all anyone can do; it may also be a number that looks like fairness and delivers none, in which case the honest version is no per-team concurrency at all and a sentence in the runbook.

**Should the fleet profile exist in `docker-compose.yml` at all, or in its own file?** `include:` puts eight service definitions one command away from the published quick start. The CI assertion checks that the default path is unchanged, which is the mitigation, not a proof. A separate `docker-compose.fleet.yml` invoked with `-f` is uglier and has no blast radius into the quick start.
