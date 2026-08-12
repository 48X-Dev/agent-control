# Agent Fleet Topology: Implementation Plan

Status: design, revision 3. `fleet/` partially built and currently unable to start; see 4.4.1.
Runtime target: **Apple `container` only.** Revision 1 was written for Docker Compose; section 3.3 records why that was wrong and what it cost.
Branch context: `build/container-context-and-corpus-migrations`.
Scope: how N agents get started, addressed and credentialed under the runtime this stack actually uses; and the credential problem that has to be solved before any of it is worth building.
Depends on: the orchestration plan's Phases 1 and 2 (`agent_runtimes`, `agent_sessions`, `POST /turns`), shipped; the dispatch ledger from `task-dispatcher.md`, largely shipped.
Split out of this plan: per-team budgets, now `docs/plans/per-team-budgets.md`. It has no dependency on anything here and was holding a 1.5-week server change behind three weeks of packaging.
Does not deliver: a scheduler, a supervisor, autoscaling, or any change to what authorizes work. Section 9.

**Author's note on this revision.** Revision 1 claimed every measurement came from a live tree. Most did, and then it sat unedited from 3 August while roughly 2,900 lines changed in the files it cited. A four-reviewer audit on 11 August found the decay, and found two things that were wrong when written rather than merely stale. Both are corrected below and both are load-bearing:

- **Probe A, the plan's own entry gate, asserted a failure that does not happen.** Measured today against the running executor: `POST /apps/marketing_researcher/users/u/sessions/probe1` on the process serving `my_agent` returns **200**, with `"appName":"marketing_researcher"` echoed in the body. Session creation never consults the agent loader. Section 3.4 is rewritten around where the failure actually lands.
- **Executors cannot boot on an ordinary key, and revision 1 never examined the path that stops them.** It enumerated registration and stopped. The nudge and halt path is ADMIN, fires at every model and tool boundary, and has no configured alternative in this repo. Section 4.4 is new and it is a blocker, not a caveat.

Where a claim here is about behaviour rather than text, the probe that produced it is named and was run on 11 August.

**Author's note on revision 3, 12 August.** Two changes, one of which the plan did not choose.

- **Section 4.4's option 2 was implemented on 12 August**, as a side effect of fixing agent-side knowledge search rather than as a decision on this plan. The credential gate has therefore moved without being consulted, and the trap 4.4 predicted is now armed: `fleet up` refuses to start anything, measured, not inferred. Section 4.4.1 is new and it is the first thing to fix.
- **Revision 2 asserted one container per agent by inheriting 3.2's conclusion about processes.** The SDK constraint binds a process, not a container, so the two questions have different answers. Section 3.2.1 re-derives it, 3.3 makes the grouping operator configuration, and 3.4 restates its guarantee as per-process. Disk cost measured on 12 August: roughly 2 GB per container on this runtime, so the topology choice is worth about 14 GB across eight agents.

---

## 1. What ships, in one paragraph

Executor processes stop being terminal tabs. An operator-supplied `fleet.yaml` names which registered agents should have a process; a new `fleet/` workspace package reads it and drives Apple `container` directly, one container per agent, all from one image, differing only by `AGENT_CONTROL_AGENT_NAME`. Nothing publishes a host port, so the 8080-8087 range disappears rather than growing. The image materializes exactly one ADK agent package per container at start, so `/list-apps` returns the agent's own name, which is what lets `fleet doctor` tell a mis-bound row from a healthy one. Admin credentials live in two one-shot jobs that exit; executors run on an ordinary key. **That last clause is conditional on section 4.4, which is unsolved.** Until it is, this plan buys addressing and lifecycle and does not buy the credential reduction that is its entire justification.

**Three things it does not ship.** Milestone scope does not become schedulable, by any convenience added here. The dispatcher does not become admin. And the one-agent-per-process SDK constraint is not removed; section 3.2 prices removing it and refuses.

---

## 2. Measured state, 11 August

**One executor process, not eight.** `adk api_server --host 0.0.0.0 --port 8080 .` serving app `my_agent`. Ports 8081 to 8087 refuse connections. Revision 1 described eight and sized several arguments against that number; every "eight crash loops" and "restart the eight" claim in it was fiction by the time it was read.

Note `--host 0.0.0.0`. The one executor currently binds every interface on the host, which is the exposure section 3.3 exists to remove.

**The stack runs on Apple `container`, not Docker.** Live: `ac-postgres`, `ac-server`, `ac-dispatcher`, `ac-knowledge` on a 192.168.64.0/24 network, started by `scripts/apple-container-up.sh`. The Docker daemon is not running. This is the single fact that invalidated revision 1's delivery mechanism.

**`agent_runtimes`, all eight rows**, after a deliberate repair on 11 August that moved them off `host.docker.internal`, which does not resolve inside these VMs:

```
agent_name             | base_url                   | executor_app_name
-----------------------+----------------------------+------------------
marketing_copywriter   | http://192.168.64.1:8080   | my_agent
sales_prospector       | http://192.168.64.1:8081   | my_agent
sales_outreach_drafter | http://192.168.64.1:8082   | my_agent
ops_runbook_agent      | http://192.168.64.1:8083   | my_agent
ops_incident_triage    | http://192.168.64.1:8084   | my_agent
marketing_researcher   | http://192.168.64.1:8085   | my_agent
engineering_reviewer   | http://192.168.64.1:8086   | my_agent
engineering_debugger   | http://192.168.64.1:8087   | my_agent
```

`192.168.64.1` is the network gateway, which from inside a VM is the host. Seven of the eight point at nothing listening. `executor_app_name` is the same literal on every row.

**Nine agents, eight runtimes.** `google-adk-plugin` is registered and unbound.

**33 `agent_sessions` rows, 16 with `agent_task_id`, all 16 with `team_id` NULL.** Revision 1 said five and sized its adoption migration against five.

**`serve` exists.** `dispatcher/src/agent_control_dispatcher/cli.py` defines `once` and `serve`; `loop.py` and `test_serve.py` are landed. `claim` and `preflight` are still unwritten. Revision 1's claim that only `once` exists, and its proposal to hide the dispatcher behind a profile, would undo a deliberate decision documented at `docker-compose.yml:114-138`.

**The workflow no longer crosses team lines.** `plan-critique-execute` now names three marketing agents. Commit `701f7e5` made the upsert refuse a step naming a non-member with `AGENT_NOT_IN_TEAM`. Revision 1 built an argument on the cross-team case as shipped fact; that argument moved to the budgets plan and shrank.

**The proxy.** An OpenAI-compatible endpoint at `http://127.0.0.1:10531/v1`, no API key, fronting a consumer subscription. Loopback-bound, which section 5.4 requires. Models include `gpt-5.6-sol`, which is what the example agent defaults to when a base URL is set.

### 2.1 What Apple `container` does not have

Measured by `container run --help` and by probe:

| Compose feature revision 1 used | Apple `container` |
|---|---|
| `profiles:` | absent |
| `include:` | absent |
| `depends_on: service_healthy` | absent |
| `depends_on: service_completed_successfully` | absent |
| `healthcheck:` | absent |
| `restart: unless-stopped` | **absent. No restart policy of any kind.** |
| inter-container DNS by service name | **absent.** `socket.gethostbyname("ac-postgres")` from `ac-server` raises `gaierror` |

What it does have, and revision 1 never used: `--uid` and `--gid`, `--cap-drop`, `--read-only`, `--tmpfs`, `--env-file`, `--rm`.

Two of those absences are design-changing and get their own sections: no DNS (3.3) and no restart policy (5.3).

---

## 3. Fleet topology

### 3.1 The constraint, stated once

One process serves one agent. `_state.py:25` holds `self.current_agent: Agent | None`, one per module, alongside one `api_key`, one `server_url`, one `server_controls` list and one `agent_config` snapshot. `AgentControlPlugin.__init__` raises `ValueError` at `plugin.py:147` when its `agent_name` does not match the process's initialized agent.

N agents means N processes. Everything below has to fit that shape.

### 3.2 Should the constraint be changed instead? No

The tempting move is a per-agent registry in the SDK, so one `adk api_server` hosts many agents. Three costs, and the third decides it.

Note what this section is and is not about. It asks whether one *process* may hold many agents. It is not the question of how many processes share a *container*, which revision 2 never asked and 3.2.1 now answers differently.

The mechanical cost is ordinary: `state` becomes a per-agent map, the plugin's constructor refusal becomes a lookup, the refresh loop fans out. Two to three weeks on the most safety-critical file in the SDK, plus a compatibility story for every consumer holding the module-level API.

The credential cost is worse. A process holding eight agents holds the union of eight agents' keys, model endpoints and tool secrets, and one prompt injection stops costing one agent's sessions.

And the third cost inverts the argument the topology was chosen for. Orchestration section 5 defends process-per-agent partly because "an agent with any HTTP-egress tool is an SSRF pivot onto the network its own executor sits on". The example ships `web_fetch_exa` on by default. Merging processes spends a property this deployment gets for free to save memory.

**Decision: keep one process per agent.** What the fleet design owes in return is that starting the eighth costs the same as starting the second.

### 3.2.1 How many containers those processes need is a separate question, and revision 2 got it wrong by inheritance

New in revision 3. Revision 2 carried 3.2's conclusion across to containers without re-deriving it, and stated one container per agent as though the SDK constraint required it. It does not. `_state.py:56` is a module-level singleton, so the constraint binds a **process**. Eight processes in one container hold eight separate copies of it. Nothing in the SDK, ADK or this repo prevents N executor processes sharing a container today, with no code change at all.

**Re-derived against the three costs of 3.2, which do not survive the transfer.** The mechanical cost is zero, because nothing changes. The credential cost is close to zero: eight processes still hold one agent's credentials each, so no process holds the union, which was the whole of that argument. Only the third survives. Processes in one container share a network and PID namespace, so an injected agent holding `web_fetch_exa` can reach a sibling on localhost and read `/proc/<pid>/environ`. That is real and it is the only cost that transfers.

**Measured cost of the boundary, 12 August.** Apple `container` gives every container its own VM root filesystem, so layers are not shared the way they are on Linux. An image costs 1.4 to 2.4 GB of expanded snapshot and each running container costs roughly 2 GB more. Restarting three containers moved the store from 25 GB to 31 GB. So per-agent is about 16 GB for eight, team-per-container about 8 GB for four, and a single container about 2 GB.

**The status quo is the thing to compare against, and it is worse than all three.** Section 2 measured one process serving all eight agents, on the host, bound to `0.0.0.0`. Eight processes in one container with no published ports is a strict improvement on that in isolation, exposure and diagnosability, at the cheapest price of the three. Per-agent containers are the best end state; they are not the only step that improves on today.

**What changed the balance on 12 August.** Before runtime tokens were configured, every executor authenticated with the same admin key, so separating them into containers protected nothing: an attacker injecting any agent already held that key from its own environment. With `AGENT_CONTROL_RUNTIME_TOKEN_SECRET` set and verified (4.4.1), each session now carries a distinct scoped bearer, so isolation finally protects something real. The argument for separate containers is stronger today than it was when revision 2 asserted it without one.

**Decision: the grouping is operator configuration, not a constant in the design.** `fleet.yaml` declares which agents share a container. Per-agent, per-team and all-in-one are the same code path with a different file. Section 3.3 specifies it.

**Why this and not a fixed choice.** Team is already a trust boundary in this codebase rather than a new one: `701f7e5` made the workflow upsert refuse a step naming a non-member with `AGENT_NOT_IN_TEAM`, and teams are the unit the budgets plan meters. All four teams currently hold exactly two agents, and none mixes trust levels. When one does, splitting it is a config edit rather than a rewrite of `fleet/`.

### 3.3 Containers grouped by `fleet.yaml`, driven from Python, addressed by IP

Revision 1 specified a generator emitting `compose.fleet.yml`, a committed generated file, `include:` in `docker-compose.yml`, profiles, and `depends_on` conditions. None of that exists on this runtime. **Deleting it is a scope reduction, not a loss:** the generator, its golden-file test, the committed-artefact drift check and the `include:` blast radius into the quick start all disappear together.

**What replaces it.** A `fleet/` workspace package reads `fleet.yaml` and invokes `container run` directly, the same way `apple-container-up.sh` already invokes it for the four existing services. Ordering is imperative and explicit rather than declared and inferred, which on a runtime with no `depends_on` is the only option and is also easier to read.

**Why not bash.** The up script already reaches for inline `python3` to parse `container inspect` JSON, and CLAUDE.md requires boundary validation with sanitized paths. A YAML schema parsed in shell is the wrong answer to both. The package validates in Python with named error codes, modelled on `knowledge_sync/.../allowlist.py`, and shells out only to `container`.

**`fleet.yaml.example`, checked in; the real `fleet.yaml` is operator-supplied and gitignored.** This follows `knowledge.yaml.example` exactly, including that the path is an environment variable with a default. Revision 1 wanted the real file committed so fleet changes are reviewable in a diff. That is a genuine loss and it is the cost of matching the precedent this repo already set for operator-supplied allowlists.

**Grouping is declared, not assumed.** Per 3.2.1, a container holds one or more agent processes. `groups` is a list; each entry becomes one container running one `adk api_server` process per listed agent. A file listing one agent per group reproduces revision 2's per-agent topology exactly, so this is a generalization rather than a change of default.

```yaml
version: 1
image: agent-control-executor:local
defaults:
  web_tools: true
groups:
  - name: marketing
    agents:
      - agent_name: marketing_researcher
      - agent_name: marketing_copywriter
  - name: sales
    agents:
      - agent_name: sales_outreach_drafter
        web_tools: false
```

**A group is a trust boundary and the schema says so.** Agents in one group share a network namespace, a PID namespace and a tmpfs, so `web_tools` differing within a group is close to meaningless: an injected sibling reaches the same network the web-enabled process does. Validation therefore **refuses a group whose members disagree on `web_tools`** with `fleet_group_mixed_egress`, naming the group and the disagreeing agents. Lowercase snake, matching every other code in `config.py`; revision 3 first wrote it uppercase, which matched nothing in this repo. Splitting them into two groups is the fix and it is one line. This is the one place the config is opinionated, and it is opinionated about the only cost that survived 3.2.1.

**Ports inside a group.** One `adk api_server` per agent means one port per agent, allocated from 8000 within the container and never published. `base_url` becomes `http://<ipv4Address>:<port>`, so the port is per-agent where the IP is per-group.

**The database is not the source.** Generating containers from `agent_runtimes` is circular: the row records where a process is, so a process started from the row cannot be what creates it, and a stale row becomes self-perpetuating. `fleet.yaml` declares intent, `agent_runtimes` records fact, 6.4 is the reconciliation.

**No published ports.** This retires 8080-8087 instead of extending it. Orchestration section 5 requires it in writing: `adk api_server` ships with no authentication, and the only real control is that its port is never published. Today's one executor binds `0.0.0.0` on the host, which is worse than the eight published ports it replaced.

**Addressing is by container IP, read back after start.** There is no DNS, so `base_url` cannot be a name. It is `http://<ipv4Address>:8000`, read from `container inspect` exactly as `ip_of()` already does for postgres and the server. `validate_executor_base_url` (`models/.../agent_runtimes.py:47`) accepts it.

**The consequence is the important part: an IP is not knowable until the container is running, so the binding write happens after executors start, not before.** That inverts revision 1's single-job ordering and forces two one-shot jobs. Section 6.1.

**Hardening the compose version did not have.** The executor runs `--uid 10003 --gid 10003`, following the uid 10001 and 10002 precedent in `dispatcher/Dockerfile` and `knowledge_sync/Dockerfile`. It runs `--read-only` with `--tmpfs /agents`, because the one thing the entrypoint writes is the materialized agent package and a tmpfs is the correct home for a file regenerated at every start. Revision 1 specified no user at all for the one container that both writes to disk and runs model-driven code with web fetch on.

### 3.4 The app name is the folder name, and the mis-execution defence is weaker than revision 1 claimed

Revision 1 correctly established that `App(name=...)` is never ADK's routing key. Verified again against google-adk 1.37.0: `_validate_agent_name` requires `^[a-zA-Z0-9_]+$` and a matching directory under `agents_dir`; `list_agents()` is `os.listdir` filtered to directories; `_record_origin_metadata` stamps the origin from the folder. `/list-apps` on the running executor returns `["my_agent"]`. All confirmed.

**What was wrong is the failure mode it inferred.** Revision 1 said a row naming the wrong app 404s at session creation, and made that the pass criterion of Probe A. Measured today:

```
POST /apps/marketing_researcher/users/u/sessions/probe1  ->  200
{"id":"probe1","appName":"marketing_researcher","userId":"u","state":{},...}
```

That process serves `my_agent`. Session creation goes straight to the session service and never consults the loader, so a foreign app name is accepted and echoed back. The failure lands one hop later at `POST /run`, where `load_agent` raises `ValueError`, nothing in `google/adk/cli/` catches it, and FastAPI returns **500**. Our client maps `status >= 500` to `ExecutorUnavailableError`, not `ExecutorSessionNotFoundError`.

**So the defence still fails closed, and everything downstream of it changes.** A mis-bound row produces a 500 at first turn rather than a 404 at session open. It reads to an operator as "the executor is broken", not "this row points at the wrong process", and `agent_sessions` has a live row for a session that can never run. Section 8's edge case is rewritten accordingly, and `fleet doctor` becomes the only thing that can tell the two apart, which raises its priority from convenience to necessary.

**The fix is unchanged: make the folder real, and materialize exactly one.** The entrypoint creates `/agents/${AGENT_CONTROL_AGENT_NAME}/agent.py`, a shim importing the shared module, then runs `adk api_server /agents`. One directory, so `/list-apps` returns exactly the agent's name and `executor_app_name` equals `agent_name`.

Materializing all eight into the image would undo the point: `list_agents()` enumerates directories, so every executor would advertise all eight names and the SDK would refuse the second at plugin construction.

**Under 3.2.1's grouping this is a per-process guarantee, not a per-container one, and the distinction is load-bearing.** `list_agents()` enumerates directories under the root a process was given, so a group container gives **each process its own agents root holding exactly one directory**: `/agents/<agent_name>/<agent_name>/agent.py`, with `adk api_server /agents/<agent_name>`. A single shared root holding both agents would make every process in the group advertise both names, which is precisely the mis-binding 3.4 exists to prevent, and the SDK would refuse the second at plugin construction anyway. One root per process, one directory per root.

Agent names in `agent_runtimes` are already underscore-only identifiers, so nothing needs renaming. Container names use hyphens.

### 3.5 The session backend, and the two defects revision 1 shipped in its wiring

Executors run with no `--session_service_uri`. **Both revisions said that means `InMemorySessionService`, and both were wrong.** Measured against google-adk 1.37.0: `create_session_service_from_options` (`cli/utils/service_factory.py:219-224`) defaults to `create_local_session_service(per_agent=True)`, which is `PerAgentDatabaseSessionService` writing SQLite to `<agents_root>/<app_name>/.adk/session.db`. In-memory is only the `OSError`/`PermissionError` fallback.

The conclusion survives and the reason changes. Under 3.3's `--read-only` with `--tmpfs /agents`, that SQLite file lands on tmpfs and dies with the container, so restarts still invalidate every `executor_session_id` and rows still move to `orphaned`. But note the new failure mode the correction exposes: if `/agents` is not writable and there is no tmpfs, ADK does not crash, it silently falls back to in-memory. A strict read-only container without the tmpfs is a quiet behaviour change rather than a startup error, which is exactly the class of thing this plan exists to refuse.

`adk-db-init` already provisions `adk_runtime` owned by a dedicated `adk` role, and `apple-container-up.sh:93` runs the same SQL, so the role exists on this runtime today. Nobody wired it up.

Revision 1's wiring had two defects, both caught in review:

- **Wrong driver.** It wrote `postgresql://adk:...`. `docs/plans/spike-findings.md:350` measured that exact form failing with `ValueError: Database related module not found (no psycopg2)` and says in bold that an explicit driver is mandatory. The working form is `postgresql+asyncpg://`. A sibling plan in the same directory had already answered this.
- **Wrong transport.** `--session_service_uri` is a click option with no `envvar` binding, so setting `ADK_SESSION_SERVICE_URI` does nothing. The entrypoint has to read the variable and pass the flag.

Both are one-line fixes in the entrypoint and both would have cost a day of confusion each.

---

## 4. The admin-on-every-executor problem

### 4.1 Registration: the mechanism, verified

`agent_control.init()` sends `conflict_mode="overwrite"` by default (`sdks/python/src/agent_control/__init__.py:527`), and the example does not override it. `endpoints/agents.py:741` gates that mode on `_authorize_existing_agent_overwrite` before any field is compared, and `providers/header.py:50` maps `AGENTS_UPDATE` to ADMIN.

**A correction worth stating, because it changes what Phase 1 buys.** The route itself is not admin: `Operation.AGENTS_CREATE` is `AUTHENTICATED` at `header.py:49`. The 403 an ordinary key gets from `initAgent` comes from the conflict mode, not the endpoint. This was diagnosed the other way round during live debugging on 11 August and the distinction matters: flipping the default to strict is precisely the fix, which is why it sequences first.

The example's `.env` records the current state in a comment, calls it "ADMIN key, and that is not a typo", and tracks it as a follow-up. This plan is that follow-up.

### 4.2 Option A, gate overwrite on the computed diff. Rejected

Moving the `AGENTS_UPDATE` check below the diff would make a no-op overwrite need no admin. `test_init_agent_overwrite_existing_agent_requires_update_auth` exists to stop exactly that.

The test is right. Overwrite is destructive by semantics, not by outcome: `test_init_agent_overwrite_warns_on_removed_referenced_evaluator` shows an evaluator absent from the payload being removed even when an active control references it. Authorizing that mode on the strength of this run's diff authorizes a mode whose next run's diff is unknown, from a process whose payload is whatever its code says when it restarts.

**Rejected. The test stays as it is.**

### 4.3 Option B, ask for the mode that already has the diff gate. Chosen, and it is a third of the fix

`init(conflict_mode=...)` default flips from `"overwrite"` to `"strict"`. Under strict, `agents.py:980-985` is the diff-based gate, and it already exists. So this is not "add a gate", it is "stop asking for the mode that skips the one already there". Restart-unchanged is already handled: `agents.py:786` preserves `agent_created_at` so a restart is not a metadata change.

**What it does not fix.** `steps_changed` is set for any step key not already stored (`agents.py:920`), so a new step still demands `AGENTS_UPDATE` under strict, and `test_init_agent_strict_existing_agent_mutation_requires_update_auth` pins that deliberately. `plugin.py:209` calls `_sync_steps_blocking(..., raise_on_error=True)` from `bind()`, so a new agent's first boot on an ordinary key is a 403 and a crash at import.

**So step registration is named as an admin act and routed through a one-shot job.** Executors then boot, find every step present, and short-circuit at `plugin.py:1579-1584`.

**And a residual revision 1 asserted away, now measured rather than reasoned.** It claimed pre-syncing "keeps the runtime path from having anything left to register". Probe B on 11 August established what is actually true, and it is not what either revision said.

*The name mismatch is real and it is not MCP-specific.* `_discover_steps` sees a toolset as one entry and `resolve_tool_name` falls through to the class name, while `before_tool_callback` resolves the individual tool. The same thing happens to every plain Python callable: `LlmAgent` stores raw functions, `resolve_tool_name` (`_extractors.py:259`) finds no `.name` and returns `"function"`, so `get_current_time` and `get_weather` collapse into a single step `tool:root_agent.function`. The live registries confirm it: `google-adk-plugin`, `marketing_researcher` and `marketing_copywriter` all carry `tool:root_agent.function` and none carries `root_agent.get_weather`.

*The consequence both revisions attached to it was wrong.* A control scoped to an unregistered step name does **not** fail open. Control scoping is a pure string comparison against the `step_name` in the evaluation request (`engine/src/agent_control_engine/core.py:589-596`); the step registry is never consulted. Proven live: control `block-web-fetch-private-addresses` is scoped to `root_agent.web_fetch_exa` and `root_agent.web_search_exa` and bound to `marketing_copywriter`, whose registry contains neither name, and it matches anyway. The registry drives console discoverability, not enforcement. **Delete the fail-open claim; it was a security assertion that was not true.**

*A pre-sync does close the mismatch,* verified end to end rather than inferred. `await toolset.get_tools(None)` yields MCPTool objects whose `.name` gives `root_agent.web_search_exa`, byte-identical to the runtime key, with matching schemas, so a pre-sync cannot cause a later 409.

*The unpriced cost nobody named.* The `_ensure_step_known` failure is not cached. `_synced_step_keys` updates only on success and `_step_sync_tasks` pops on completion, so it retries on every single tool call with no backoff. Measured: three calls for one unregistered name produced three GET plus POST pairs, 403 each time. At eight executors with web tools on, that is a 403 per tool invocation, forever.

So the real cost is console discoverability and a permanent 403 storm, not an enforcement hole. Either the register job enumerates tool names by connecting to the toolset, or the residual is accepted with the retry storm written into the runbook. **Decide before Phase 2, on the corrected grounds.**

**This is a default flip on a public API, so it is a major-version change** in `sdks/python`, with a release note naming the 409 on schema change and the monotonic registry.

### 4.4 The nudge and halt path is ADMIN, and this blocks the whole plan

New in revision 2. Revision 1 enumerated registration and stopped, and this is what it missed.

Measured:

- `nudges/claim`, `nudges/ack` and `halts/claim` all require `Operation.AGENT_NUDGES_CONSUME`.
- `header.py:116` maps `AGENT_NUDGES_CONSUME` to `AccessLevel.ADMIN`, deliberately, with a comment arguing that failing closed is right when no session binding is available.
- These are hot path. `nudges/claim` fires at every model boundary (`plugin.py:293-295`), `halts/claim` at every tool boundary (`plugin.py:459`).
- The SDK prefers a session bearer token and falls back to the process API key when none was seeded (`nudges.py:486-491`).
- That token is minted only when `AGENT_CONTROL_RUNTIME_TOKEN_SECRET` is set. **Probed: it is absent from `docker-compose.yml`, `docker-compose.dev.yml`, `server/.env.example` and `apple-container-up.sh`.**
- On an ordinary key the result is a 403, one warning, and a 300-second per-session backoff.

**So an executor on an ordinary key silently loses the operator STOP button, while the console still shows the halt recorded.** That is a worse failure than the credential it removes.

**The obvious fix is a trap.** Setting `RUNTIME_TOKEN_SECRET` routes `Operation.RUNTIME_USE` to the JWT provider too. The example calls `init()` with no target, so `POST /evaluation` goes out with no bearer, `LocalJwtVerifyProvider` 401s, and the SDK fails closed on every turn. The session-minted token cannot rescue it: `SESSION_TOKEN_SCOPES` deliberately excludes `runtime.use`.

Three ways out, none free, and the choice is not this plan's to make alone:

1. **Map `AGENT_NUDGES_CONSUME` to `AUTHENTICATED`.** Smallest change, and it argues directly against a comment somebody wrote on purpose. Whoever owns that decision has to reverse it in writing.
2. **Configure runtime tokens properly**, which means giving the example a target so the JWT path has a bearer, and re-checking every operation that moves to the JWT provider. Larger, and it is the only option that ends with a per-session credential rather than a per-process one, which is the better end state.
3. **Accept executors keep an admin key for the nudge path.** Honest, and it guts the plan: section 4 exists to take ADMIN out of processes running model-driven code.

**This is Phase 0's real gate.** Until it is decided, the fleet buys addressing and lifecycle, and the credential reduction that justifies three weeks of work is not available. Sizing the rest of the plan as though option 1 is free would repeat exactly the error revision 1 made.

**A trap in the shipped refusal, found reviewing the built code on 11 August.** `ServerCalls.refuse_when_executor_credential_cannot_halt` probes `halts/claim` with the executor's `X-API-Key` and refuses on 401 or 403. That is correct today and it inverts under option 2. Once `AGENT_CONTROL_RUNTIME_TOKEN_SECRET` is set, `AGENT_NUDGES_CONSUME` moves to the JWT provider, the executor claims halts with a session-bound bearer, and its API key is *supposed* to be refused on that route. The refusal would then block the very fix it exists to demand.

So the probe has to become mode-aware before option 2 lands: under JWT mode the question is not "can this key claim a halt" but "can this key exchange for a runtime token, and does session creation mint a session token carrying `agent_nudges.consume`". Whoever implements option 2 owns that change, and shipping the secret without it turns a working fleet into one that refuses to start.

### 4.4.1 Option 2 was taken on 12 August, and the trap 4.4 predicted is now armed

New in revision 3. Option 2 was implemented, not as a decision on this plan but as the fix for an unrelated user-visible bug, which is worth recording because it means the gate moved without the plan being consulted.

**What was done.** `company_knowledge_search` returned nothing for every agent while the console read the corpus fine. The cause was not the corpus: `knowledge_tools._identity()` returns `None` with no session-bound token and refuses to fall back to the process key by design, so with `AGENT_CONTROL_RUNTIME_TOKEN_SECRET` unset the tool could never work. Fixing it required exactly option 2: give the example a target so the SDK can exchange for a `runtime.use` bearer, set the secret, and pass it to both runtimes.

**Verified after the change.** A turn completes under JWT mode, `company_knowledge_search` returns real content and the agent cites the source document. A queued nudge reaches `applied` with `claimed_at`, `applied_at` and an `applied_trace_id`, with `nudges/claim` and `nudges/ack` both 200. So the nudge path works on a session bearer, which is what 4.4 said option 2 would buy.

**The trap is live, and it is measured rather than predicted.** 4.4 warned that `ServerCalls.refuse_when_executor_credential_cannot_halt` inverts under option 2. It does. The probe at `fleet/src/agent_control_fleet/server.py:96` posts to `halts/claim` with `X-API-Key` and refuses on 401 or 403. Run against the server as configured on 12 August:

```
POST /api/v1/agent-sessions/__agent_control_fleet_credential_probe__/halts/claim
X-API-Key: <executor key>   ->  HTTP 401  AUTH_MISSING_KEY  "Missing Authorization header."
```

So `fleet up` now raises `executor_credential_cannot_halt` and starts nothing. The refusal blocks the fix it exists to demand, exactly as written. **This is a blocker on the fleet package, not on the runtime tokens, and it is the first thing any implementation must fix.**

**What the probe has to become.** Under JWT mode the question is not whether the key can claim a halt. It is whether the key can exchange for a runtime token, and whether session creation mints a session token carrying `agent_nudges.consume`. The probe must detect which mode the server is in rather than assume, because both modes are now reachable configurations of this repo.

**What is still owed from 4.4.** Option 2's second clause, re-checking every operation that moved to the JWT provider, was not done. Only the knowledge, evaluation, nudge and halt paths were exercised. Any operation that routes through `RUNTIME_USE` and was not hit by that testing is unverified, and the fleet should not be sized as though it is.

### 4.5 Option C, a registration-only credential tier. Deferred

A key that may register an agent and its steps and may not touch controls is not expressible: `AccessLevel` has three values and there is no per-key operation allowlist. `task-dispatcher.md` section 15 prices the allowlist at 3 days. It remains what would close the residual on the register job. Not re-sized here.

### 4.6 What all of this is worth when `api_key_enabled` is false

`config.py:74` defaults it false, and with it off `NoAuthProvider` returns a principal for whom every operation succeeds. In that state all of section 4 is theatre.

**Where the refusal can be enforced.** Revision 1 put it in a compose one-shot blocked by `service_completed_successfully`. There is no such mechanism here, and the replacement is better: the fleet `up` command is a single Python process that runs the register job, checks its exit code, and refuses to start any executor unless it is zero. An exit code checked in the same process is a stronger gate than a declared dependency.

The check itself stays behavioural: probe the server for an ADMIN-tier read with no credential at all and refuse if it succeeds. That detects the condition rather than trusting a config field the fleet cannot see. `AGENT_CONTROL_EXECUTOR_ALLOW_INSECURE_LOCAL_DEV` is honoured for the single-executor dev path and is not honoured here.

---

## 5. Lifecycle under Apple `container`

### 5.1 What comes up, and what stays opt-in

`scripts/apple-container-up.sh` continues to bring up postgres, the server, the dispatcher and the knowledge sync. Unchanged.

The fleet is opt-in by the same mechanism the knowledge sync already uses: **the script skips it when the config is absent.** Knowledge skips when the Drive credentials are unset and says so on stdout. The fleet skips when no `fleet.yaml` exists and says so. No profiles needed, and the precedent is three lines away in the same file.

### 5.2 Ordering, without `depends_on`

Imperative, in `agent-control-fleet up`, each step gated on the previous one's result:

1. Server reachable. Poll `/health` until 200 or refuse. Replaces `depends_on: service_healthy`.
2. **`fleet register`** with the admin key. Registers each agent in `fleet.yaml` and syncs its steps. Runs to completion; a non-zero exit refuses everything below.
3. Start N executors on the `agent-control` network, no published ports, ordinary key, `--uid 10003`, `--read-only`, `--tmpfs /agents`.
4. For each, read `ipv4Address` from `container inspect` and poll `/list-apps` until `agent_name` is present. This is where revision 1's healthcheck identity assertion goes, since the runtime has no healthchecks.

**The assertion is "contains", not "equals", and that is a concession forced by measurement.** Opening a session materializes the app directory: `PerAgentDatabaseSessionService` creates `<agents_root>/<app_name>/.adk/` and `list_agents()` is `os.listdir` filtered to directories. So one session opened against a wrong app name permanently adds that name to `/list-apps`, on a container that still 500s at `/run` because no `agent.py` exists there. Observed live on 11 August: a single probe against `marketing_researcher` on the executor serving `my_agent` left `/list-apps` returning both names until the directory was deleted by hand.

Equality therefore passes at start and can stop being true later, through no fault of the fleet. The identity check keeps its value at startup and loses it as a steady-state invariant, so `fleet doctor` reports an unexpected extra name as a warning naming the likely cause rather than as a fault.
5. **`fleet bind`** with the admin key. Writes `PUT /agent-runtimes/{agent_name}` with the observed IP and `executor_app_name = agent_name`. Exits.

**Two admin jobs, not one, and it is the no-DNS tax.** An IP is not knowable before start, so the binding cannot be written before the container exists. Revision 1's property was "the admin credential exits before any agent code exists". That is no longer literally true: at step 5 the executors are alive.

**The property that actually holds, stated precisely.** No executor can be given work before step 5 completes, because an agent with no runtime row answers 409 `AGENT_RUNTIME_NOT_BOUND` at session open. So the window in which an admin credential coexists with live executors is a window in which no executor can receive a turn. That is weaker than revision 1's claim and it is the true one. Both jobs still exit, and no long-running process holds admin.

### 5.3 There is no restart policy, and that cuts both ways

`container run` has no `--restart`. A crashed executor stays down.

**What that removes.** Revision 1's crash-loop storm is gone. Eight containers re-POSTing `initAgent` on every restart cycle against the server was a named risk; it cannot happen here. The edge case in section 8 changes from "eight crash loops, loud and correct" to "N containers exit and nothing tells you", which is quieter and worse.

**What it costs.** No recovery from a transient failure. A server blip during startup leaves executors dead until an operator re-runs `up`. Revision 1 leaned on `restart: unless-stopped` plus a healthcheck for this; neither exists.

**The mitigation, and it is deliberately not a supervisor.** `up` is idempotent and re-running it restarts what is missing, exactly as the existing script reuses running containers and finishes partial starts. `fleet doctor` reports which agents have no container. Writing a supervisor is orchestration Phase 5's job and section 9 refuses it here.

### 5.4 The proxy constraint, resolved for this runtime

Revision 1 specified `extra_hosts: ["host.docker.internal:host-gateway"]` and defaulted the model base URL to `http://host.docker.internal:10531/v1`. **Both are wrong here.** `host.docker.internal` does not resolve inside these VMs; probed on 11 August, `getent hosts host.docker.internal` returns nothing. The up script has said so in its closing banner all along.

The correct value is the network gateway, which `gateway_of()` at `apple-container-up.sh:51` already computes and whose comment already says it is "the address executor runtime rows must use in place of host.docker.internal".

**So the model base URL is computed, not configured.** `AGENT_CONTROL_FLEET_MODEL_BASE_URL` may override it, but the default is derived from the live gateway at `up` time. Revision 1's generator-side refusal of `127.0.0.1` and `localhost` moves to where the value is read, because a generator cannot validate a value that did not exist yet.

**The address is the credential.** The proxy holds the OAuth session in the host process and accepts requests with no API key. Anything that can open a TCP connection to gateway:10531 spends the subscription. So the proxy stays loopback-bound on the host, and the fleet reaches it through the gateway, which is the VM-to-host path and not a LAN path.

---

## 6. Runtime registration

### 6.1 Two one-shot jobs, one credential, both exit

Not the executor. Self-registration needs `agent_runtimes.write`, which orchestration 6.1 puts at ADMIN because binding an agent to an executor URL is deployment configuration.

**`fleet register`** runs the executor image, because syncing steps means importing the agent module and letting `bind()` discover the tools, which needs the agent's dependencies. It registers each agent, syncs step schemas under the admin key, and refuses to proceed when the server accepts an uncredentialed ADMIN read.

**`fleet bind`** writes the runtime rows once IPs are observable. It is a thin HTTP client and could run from any image; it runs from the same one to avoid a second image for four requests.

**Neither is the dispatcher and neither may be folded into it.** If they ever share a process, the dispatcher has acquired an admin credential by convenience, which is the failure section 9 names.

**One process cannot register N agents by importing the module N times, and revision 1 assumed it could.** `my_agent/agent.py` calls `init()`, constructs the plugin and calls `bind()` at import. Python caches the module in `sys.modules`, so eight imports run it once, under whichever `AGENT_CONTROL_AGENT_NAME` was set first. Making it work in-process needs `importlib.reload` plus environment mutation plus tearing down each MCP toolset between iterations. **`fleet register` runs one subprocess per agent instead.** That is the honest shape and it was unpriced in revision 1.

**The register job's environment must reproduce each executor's exactly.** `AGENT_NAME` defaults to `google-adk-plugin` when unset, which is the one agent deliberately left unbound. `AGENT_CONTROL_WEB_TOOLS` defaults to `"1"`, so a register job with a bare environment would build a toolset for the agent whose `fleet.yaml` entry turns it off. And `_build_web_toolset` catches every construction failure and returns `None` with a warning, so a register job that cannot reach the Exa endpoint registers a smaller step set, exits 0, and leaves every executor to fail on a pending step. **The subprocess inherits the same environment the container would get, computed once, and this is asserted by a test rather than by care.**

**`AGENT_CONTROL_FLEET_REGISTER_API_KEY`, not `AGENT_CONTROL_ADMIN_API_KEY`.** The server's real setting is `AGENT_CONTROL_ADMIN_API_KEYS`, plural and comma-separated. A slot one character off gets a comma-joined list pasted into it and produces a 401 that reads as a server fault. The job refuses a value containing a comma.

### 6.2 What gets written

`base_url` is `http://<observed-ip>:8000`. `executor_app_name` is the agent name verbatim, which 3.4 made routable. Nobody types a port again.

**IPs are not stable across recreation**, so `bind` runs on every `up`, and a row is rewritten whenever the observed IP differs. This is the one place the fleet is chattier than the compose design would have been, and it is a direct consequence of no DNS.

### 6.3 Adoption

All eight rows carry a hand-written host-gateway URL and `executor_app_name = 'my_agent'`. Every one is a mismatch against what `bind` would write. A job that aborts on any mismatch aborts on row one of its first run.

**So adoption is an explicit one-time gesture.** `fleet bind --adopt` rewrites rows for agents named in `fleet.yaml` regardless of current value. Without it, a row differing from the generated one aborts and names the row. After adoption any later mismatch is genuinely foreign.

The 33 existing `agent_sessions` rows carry `executor_app_name = 'my_agent'` and point at ADK sessions that live only in process memory, so they are already no more durable than a restart. The migration marks them `orphaned` rather than leaving them to fail at the next turn. Revision 1 sized this against 5 rows.

### 6.4 Reconciliation: `fleet doctor`

Read-only, refuses to fix anything, and 3.4 raised its priority: with a mis-bound row now surfacing as a 500 rather than a 404, this is the only thing that distinguishes a binding error from a broken executor.

| Finding | Meaning |
|---|---|
| Agent in `fleet.yaml`, no row in `agent_runtimes` | Bind did not run. Session open answers 409 `AGENT_RUNTIME_NOT_BOUND`. |
| Agent in `fleet.yaml`, no running container | Crashed, and nothing restarted it. 5.3. |
| Row in `agent_runtimes`, agent absent from `fleet.yaml` | A binding with no intended process. |
| Row whose `base_url` is not the observed IP | Stale after recreation, or hand-edited. |
| `executor_app_name` not equal to `agent_name` | Pre-3.4 row. First turn will 500. |
| `/list-apps` missing `agent_name` | The container serves a different agent, or a pre-3.4 image. A fault. |
| `/list-apps` carries an extra name | Somebody opened a session against a wrong app name and ADK materialized the directory. A warning naming that cause, not a fault. |
| Registered agent in neither | `google-adk-plugin`. Informational. |

It refuses to fix anything because the fix for half of these is to delete a binding, and that needs `agent_runtimes.write`, which 6.1 just decided not to give a long-running process.

---

## 7. Artefact placement and conventions

Revision 1 put its code in `scripts/` and named a package it never gave a home. Both fail this repo's own rules.

**`scripts/` is outside every quality gate.** `make lint` and `make typecheck` enumerate `models/src`, `server/src`, `sdks/python/src`, `dispatcher/src`, `knowledge_sync/src`. Root mypy sets `disallow_untyped_defs = true` and would never see a file in `scripts/`. Today `scripts/` holds one 83-line regex checker; revision 1 proposed putting 150 lines of schema parsing and deployment-config emission there.

**So the fleet is a workspace member: `fleet/`.** A `pyproject.toml`, a `[project.scripts]` console entry `agent-control-fleet`, a `__main__.py` alias, a `tests/` directory, and entries in the root workspace members list and the Makefile's lint, typecheck and test targets. `scripts/tests/test_workspace_test_coverage.py:65` fails the build for a member `make test` does not reach, and the `HOW_TO_FIX` block at `:23` names exactly what is needed.

**The schema gets a typed parser that refuses every ambiguity**, modelled on `knowledge_sync/src/agent_control_knowledge_sync/allowlist.py`. That file declares its key sets as frozensets, refuses an unknown key with the reason attached, refuses a duplicate naming the first occurrence, and refuses a bool-shaped string because "a string here would read as true and turn a channel on by accident". Every one of those has a fleet analogue, and the last is not hypothetical: `web_tools: "false"` would read as true and turn Exa on in the one container the example deliberately turns it off for. At 257 lines for a simpler schema, revision 1's "roughly 150 lines" was light.

**The image follows the three that exist**, which is not free: `examples/google_adk_plugin` is **not** a workspace member, carries its own `uv.lock`, and installs five packages by editable path. All three existing Dockerfiles copy the root lock and run `uv sync --package <name>`. Either the example is promoted to a workspace member, which drags `google-adk` into the root lock that CI resolves for every package, or the image builds from a second lock. **Neither is free and revision 1 priced this at zero.** Recommendation: promote it, accept the lockfile growth, and get lint, typecheck and test coverage over the example for the first time.

**Tests, and the one that matters most.** The plan's core claim is credential separation and revision 1 gave it no test, despite `server/tests/test_knowledge_provisioning_wiring.py` already carrying the template in two halves: `test_the_server_is_never_handed_the_sync_credential` at `:223`, which asserts per container that nothing but the intended holder carries it, and `test_the_sync_container_is_handed_the_credential_it_needs` at `:257`, so nobody satisfies the first by deletion. Note that both are parametrized over `RUNTIMES`, which already includes the Apple container path, so the fleet analogue inherits runtime parity for free. The fleet needs exactly that, plus:

- Env-reaches-container parity, extending `scripts/check_knowledge_env_parity.py`. The example reads twelve environment variables; a fleet container that gets seven has a feature that reads as available and is off. `AGENT_CONTROL_KNOWLEDGE_TOOLS` is precisely this class.
- Schema refusal tests per ambiguity, mirroring the allowlist's.
- The register-environment parity assertion from 6.1.

These are file facts and pure functions. They must fail on a laptop with nothing running, which is the discipline `test_knowledge_provisioning_wiring.py:19-21` states outright and is why revision 1's `docker compose config` CI assertions were the wrong mechanism even before the runtime changed.

---

## 8. Edge cases, each with its decided behaviour

| Case | Behaviour |
|---|---|
| Executor up but wedged | `/list-apps` answers from the ADK app registry and says nothing about the plugin's control cache. The refresh loop logs "Failed to refresh controls; keeping previous cache" and runs the old control set indefinitely. Nothing in the runtime catches it. The dry-run canary per task catches it; the real fix is the control-set generation counter in `task-dispatcher.md` Phase 8. |
| Stale row, wrong agent | **Revised.** Not a 404 at session open. Session creation returns 200 against any app name, the first `POST /run` returns 500, and the client maps it to `ExecutorUnavailableError`. Reads as a broken executor, is a binding error, and `fleet doctor` is the only thing that distinguishes them. |
| Stale row, dead IP | 503 `EXECUTOR_UNAVAILABLE`. An outage, correctly reported. |
| Executor crashes | **Revised.** No restart policy, so it stays down and nothing announces it. `fleet doctor` reports the missing container; re-running `up` restarts it. |
| Executor whose agent row was deleted | Absent from revision 1. `init()` recreates the agent with zero steps on an ordinary key, `bind()` then finds every step pending and 403s at import. Deleting one agent row takes one executor down until `fleet register` is re-run. |
| Register job fails halfway | Some agents registered, some not. Idempotent, so re-running completes it. No executor starts, because `up` checks the exit code. |
| A tool is added to the shared example | Every executor's `bind()` would need `AGENTS_UPDATE`. It never gets there if `fleet register` ran first. Forgetting to re-run it means N containers exit at import, and under 5.3 they stay exited. |
| A tool name is first seen at runtime | **The residual from 4.3, corrected.** `_ensure_step_known` 403s silently on an ordinary key and the step never registers. Controls still enforce, because scoping is a string comparison and never reads the registry. What is lost is console discoverability, plus a 403 on every subsequent tool call because the failure is not cached. Applies to plain callables as much as MCP tools. |
| A developer changes a tool signature | 409 `SCHEMA_INCOMPATIBLE` from `fleet register` under strict. Deliberate. The fix is an explicit admin overwrite. |
| A developer deletes a tool | The step stays in the registry. Strict merges. Pruning needs an admin overwrite. |
| Fleet restarted mid-chain while a dispatcher holds a lease | The lease is 1800s and restarting executors does not touch it, so a task can sit `running` for up to thirty minutes before reclaim. With the Postgres session backend the ADK session survives. The runbook orders the sequence rather than offering one command: pause dispatch, drain or accept losing in-flight steps, restart, unpause. |
| Executor restarts against an unchanged registration | Strict, no new step keys, `agent_created_at` preserved, so no admin path. Succeeds on an ordinary key. |
| `api_key_enabled` false | Every operation succeeds unauthenticated and every credential separation here is inert. `fleet register` detects it behaviourally and exits non-zero, which stops `up` before any executor starts. |
| Fleet executor key leaked | It is an ordinary key: it can register agents, open sessions and spend within the namespace budget. It cannot create or unbind controls. **Subject to 4.4:** if executors keep an admin key for the nudge path, this row is false and the plan has not delivered its point. |
| Two fleets against one server | One `agent_runtimes` table keyed `(namespace_key, agent_name)`, so the second fleet silently repoints the first's bindings. After adoption, any `base_url` that is not the one this fleet would write aborts the run and names the row. |

---

## 9. What this refuses to do

**It does not make milestone scope schedulable.** The property lives in the endpoint: `POST /agent-tasks/import` accepts `scope.kind == "linear_milestone"` only under `mode: "commit"` carrying an `expected_refs_digest` over a preview the caller was shown. No non-interactive caller can construct that.

The fleet-specific clause: **no long-running process started by this plan holds a credential carrying `agent_tasks.write`.** Both admin jobs exit. Nothing here adds a fleet-wide play button or a container with a milestone id in its arguments. If a future reviewer finds any of those traceable to this plan, the plan has been violated.

**It does not dissolve the dispatcher's non-admin requirement.** Note that the dispatcher now runs `serve` continuously in the default stack, which revision 1 did not account for. It holds an ordinary key and must keep holding one.

**It does not remove the one-agent-per-process constraint.** Section 3.2.

**It does not build a supervisor.** With no restart policy this is more tempting than it was under compose, and it is still orchestration Phase 5's work. Section 5.3 offers idempotent `up` and a doctor instead.

**It does not create per-team capacity, or per-team anything.** Budgets left this document entirely; see `docs/plans/per-team-budgets.md`.

**It does not add per-namespace executor isolation.** This plan is single-namespace by construction.

**It does not target Docker Compose.** Revision 1 did, against a machine that does not run it. If a Docker path is wanted later it is a second driver behind the same `fleet.yaml`, and the parity rule at `apple-container-up.sh:96` applies: a service that exists only in compose does not exist on the machine this stack runs on.

---

## 10. Phases and effort

One engineer, including tests, in this repo's convention.

**Phase 0, the decisions that can cancel the plan. 2 days.**

*Probe A is done.* Corrected and measured on 11 August: foreign app name returns 200, failure lands at `/run` as a 500. Section 3.4 is rewritten and nothing further is needed.

*Probe B, the credential path.* Not a restart of already-synced executors, which short-circuits and can only pass. On a non-admin key: register a brand new agent with tools and confirm the 403 at `bind()`; add one tool to an existing agent and confirm the same; then run the sync under the admin key and confirm both boot clean.

*Decision C, and it gates everything.* Section 4.4. Choose among mapping `AGENT_NUDGES_CONSUME` to `AUTHENTICATED`, configuring runtime tokens properly, or accepting admin on executors. **If the answer is the third, stop: the plan's justification is gone and only the addressing work is worth doing.**

*Decision D.* The MCP step-name residual from 4.3. Enumerate at register time, turn web tools off on the fleet, or accept and document.

**Phase 1, the SDK default flip. 2 days.** `conflict_mode` default to `"strict"`; a one-line docstring with the reasoning in the commit message; tests for restart-unchanged on a non-admin key and the 409 on a changed schema; a major-version bump and a release note; rewriting the example's `.env` comment that currently explains why an admin key is needed. Entry gate: Probe B. On its own it does not remove admin.

**Phase 2, the fleet package and the executor image. 2 weeks.** The `fleet/` workspace member with typed schema, `up`, `register`, `bind`, `doctor` and its subprocess-per-agent harness; the executor Dockerfile and the entrypoint that materializes one agent package into tmpfs and maps the session URI to the flag; promoting `examples/google_adk_plugin` to a workspace member; the credential-separation and env-parity tests; wiring into `apple-container-up.sh` and the down script. **Two weeks, not 1.5, and the delta is the workspace promotion plus the subprocess harness, both of which revision 1 priced at zero.**

**Phase 3, adoption and the runbook. 3 days.** `--adopt`; the orphaning migration against 33 rows; `fleet doctor`; the runbook covering ordered restart, the proxy binding, and what to do when an executor exits and nothing restarts it.

| Phase | Effort | Kind |
|---|---|---|
| 0. Probes and two decisions | 2 d | Measurement, can cancel the plan |
| 1. SDK default flip | 2 d | Real work, small |
| 2. Fleet package and image | 2 wk | Real work |
| 3. Adoption, doctor, runbook | 3 d | Real work |

**Roughly 3 weeks**, down from revision 1's 4 to 4.5, because per-team budgets left and the generator, the committed compose artefact and its drift check no longer exist.

### 10.1 The minimum useful slice

**Probe B plus the step-sync half of `fleet register`, run once by hand. Roughly 2 days.** No containers, no package, no runtime changes.

Run the sync against the running host executor with the admin key, flip `conflict_mode` to strict, and restart on an ordinary key. Ports stay where they are, `executor_app_name` stays `my_agent`, and none of that is worse than today.

**Both halves are required, which Probe B established and revision 1 obscured.** Measured in sequence on one agent: admin pre-sync under strict succeeds; the same agent then boots on an ordinary key under strict with no POST at all, because the step keys are all present and `_sync_steps_async` short-circuits; but on an ordinary key under today's shipped `overwrite` default, `init()` 403s before `bind()` is ever reached. So the pre-sync unblocks first boot and the flip unblocks every reboot. Neither alone is sufficient, and the slice is not a slice of Phase 2, it is Phase 1 and Phase 2's step-sync together.

**Revision 2 caveat, and it matters.** Revision 1 called this the highest ratio of risk removed to effort spent. Under 4.4 it is not: restarting on an ordinary key is what breaks the STOP button. **The slice is only useful after Decision C.** With Decision C resolved it remains the right first move; before it, it makes things worse in a way that is silent.

---

## 11. Riskiest assumptions

**That Decision C has an acceptable answer.** Everything about credential reduction rests on it and the comment at `header.py:116` argues the current mapping is correct. If the owner of that decision keeps it, this plan delivers addressing and lifecycle only, and should be re-scoped and re-titled rather than built as written.

**That the per-agent package shim behaves like the example does today.** Probe A now covers routing and where the failure lands. It does not cover whether `envs.load_dotenv_for_agent` on a folder with no `.env` is harmless, or whether ADK's hot-reload watcher is indifferent to a folder written into tmpfs at start. Failures are at import time, which is the good kind.

**That flipping the SDK default to strict does not break real consumers.** Verified against this repo's tests and example, not against anyone else's agent. Hence the major version.

**That no restart policy is livable.** Revision 1 assumed `unless-stopped` and a healthcheck. Neither exists, and the honest position is that this fleet has no automatic recovery at all. Whether that is acceptable at eight containers on a laptop is a real question, and the answer may be that Phase 2 should carry a small supervisor after all, which section 9 currently refuses.

**That IP-based bindings rewritten on every `up` are not a source of drift.** No DNS forces it. A row is now correct only relative to the last `up`, and anything that recreates a container behind the fleet's back leaves a stale row that surfaces as a 500 at first turn.

---

## 12. Open questions a reviewer should push on

**Should `steps_changed` really demand ADMIN?** 4.3 accepts the gate and works around it. The counter-argument is that adding a step is additive and the deny-by-default tier-1 control already refuses any tool no allowlist names, so a newly registered step is not thereby callable. If that holds, a narrower gate would delete most of the register job. Somebody who owns the control engine should decide, and Decision D depends on the same person.

**Is one container per agent right at forty agents?** Eight is comfortable. Forty is forty Python processes each holding a LiteLLM client and an MCP session on one laptop, and the answer is a scheduler. This plan solves the eight-to-fifteen range and does not claim the shape survives past it.

**Should the fleet ever have targeted compose?** Revision 1 did, for a stack that has not used it in weeks. Worth asking how that happened, because the answer is that nobody re-read the plan against the tree before building on it, and the parity rule that would have caught it was written in a shell script the plan never opened.

**Is `fleet.yaml.example` plus a gitignored real file the right call?** It matches the knowledge precedent and it gives up the reviewable-in-a-diff property revision 1 wanted. For a single-operator deployment that is probably right and it is worth one dissenting voice.
