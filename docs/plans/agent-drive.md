# Agent Drive: a folder structure the agent owns, and nothing else

Status: design. Nothing built. **Section 14 recommends that three parts of this do not ship, and section 4.1 names four Google-side prerequisites that are not engineering tasks and that block everything.**

Branch context: `feat/task-dispatcher`.

Scope: one Google Drive capability for the dedicated identity `agent.control@earlycore.dev`. The agent gets a folder tree it owns, the ability to create folders inside it, and the ability to write files into it. It cannot share, cannot publish, cannot change a permission, cannot read anything a human put in Drive, and cannot permanently delete. No Gmail. No send of any kind. No recipient allowlist appears anywhere in this document, because nothing here has a recipient.

Depends on: `task-dispatcher.md` sections 12 and 13 for the safety machinery this reuses, and on `agent-file-inputs.md`'s own section 3.3 for the converter sidecar that a later phase would need and this plan does not build. **Every bare "section N" reference below points inside this document. References to another plan always name the file.**

**Author's note on verification.** Every claim about this repository was read out of the working tree while writing, with file and line references. Claims about the Google Drive API are marked as verified against Google's public reference or as **unverified**, and every unverified one that decides a design question is an experiment in Phase 0 with a named branch for each outcome. Two of the brief's premises were wrong and section 1 says how. Two critique findings were wrong or incomplete and section 1.3 says which and why.

---

## 0. What ships, in one paragraph

`agent.control@earlycore.dev` gets a Drive capability with one shape. A bootstrap script run by a human creates a root folder using the agent's own OAuth client, so the folder is app-created and inherits nothing. Under that root the agent writes files and creates one level of folders, through two tools in the first slice and six in total, in a purpose-built MCP server in this repo. The OAuth scope is `drive.file` and nothing else. Sharing is refused at six layers, of which only the top two are Google-enforced and the other four are ours. A canary asserts, continuously, both that no file has been shared with the agent and that no file the agent owns has been shared outward, and it latches every tool call off when either assertion fails. Four controls in valid schema form the policy surface, and the server's own refusals become `control_execution_events` rows by riding the post-tool stage rather than by calling the control plane. Section 3 states which of `task-dispatcher.md` 13.5's four conditions this satisfies: two of four, and section 3 says what each unmet one blocks.

---

## 1. Corrections, because a plan that opens with a wrong file path spends credibility it needs later

### 1.1 Two corrections to the brief

**The file the brief names does not exist.** There is no `sdks/python/src/agent_control/integrations/google_adk/_schema_derivation.py`. The directory holds `_agent_config.py`, `_attachments.py`, `_descriptors.py`, `_extractors.py`, `_sanitize.py`, `_session_state.py`, `nudges.py`, `plugin.py` and `progress_tools.py`. The flattening the brief means is `plugin.py:1357`:

```python
def _iter_tools(self, agent: Any) -> Iterable[Any]:
    tools = getattr(agent, "tools", None)
    if isinstance(tools, (list, tuple, set)):
        return tools
    return []
```

Its only caller is `_discover_steps` (`plugin.py:1305`). Anyone assigned the fix by the brief's file name will not find it.

**The qualified-name behaviour is confirmed, but the fail-open is not in `_iter_tools`.** Verified at `plugin.py:745`:

```python
default_name = (
    f"{resolved_agent_step_name}.{raw_name}" if resolved_agent_step_name else raw_name
)
```

`_extractors.py:271`'s `resolve_tool_agent_name` walks to `resolve_agent_name`, which returns `"root_agent"` as its fallback (`_extractors.py:256`). So the resolved step name is `root_agent.drive_write_file`, and a control naming `drive_write_file` matches nothing. That much is exactly as the brief reports.

The fail-open itself lives in two other places. `get_applicable_controls` (`engine/src/agent_control_engine/core.py:561`) filters on name only inside `if scope.step_names or scope.step_name_regex:` (line 590), and skips a non-matching control with no warning. Then `evaluation.py:462` contacts the server only when `_has_applicable_prefiltered_server_controls` returns true, and falls through to `EvaluationResult(is_safe=True, confidence=1.0)` at `evaluation.py:514` when it does not. **A control that names anything can name it wrong. A control that names nothing cannot.** Section 5.1 builds on the second, and section 6 argues that `_iter_tools` is still a prerequisite, for a different reason.

### 1.2 What `_iter_tools` actually breaks

Two defects, related, different severities, both verified.

**Defect A: toolsets are not resolved.** `agent.tools` for an MCP agent holds `McpToolset` objects, not `McpTool` objects. ADK resolves them through `LlmAgent.canonical_tools`, which `_discover_steps` never calls. At `bind()` time the plugin registers one tool step whose name comes from `resolve_tool_name`'s class-name fallback (`_extractors.py:265`), so the console shows a step called `MCPToolset` and shows no Drive tool names at all. The real names arrive later, one at a time, through `_ensure_step_known` (`plugin.py:1368`), which schedules a sync and does not await it. The server's step registry is a post-hoc record of tools that have already run, exactly as `task-dispatcher.md` 12.3 already found.

**Defect B: schemas are uninformative.** `_resolve_schema_source` (`plugin.py:1295`) tries `run_async`, `run`, `func`, `callback` in order and gets the ADK wrapper's signature, producing `{"type": "object", "additionalProperties": true}`. The real schema is on `tool._mcp_tool.inputSchema`.

Both are prerequisites, argued in section 6.

### 1.3 Two critique findings this design does not accept as written

Both critiques were largely right and this plan adopts almost all of them. Two need correcting rather than adopting, and saying so is cheaper now than discovering it in Phase 3.

**Rejected: "Workspace app access control is a Google-enforced preventer of scope widening."** One critique proposes restricting API access to trusted OAuth client IDs so that an operator who edits the scope string to `drive` is refused by Google at the consent step. That is not how app access control works. Trusting a client ID trusts *that client*, not a scope list on it: a trusted client may request any scope it likes. The setting is real and worth having for a different reason (it stops a *different* OAuth client from ever being consented against the account, which is the shadow-app and stolen-client-secret case), but it does not constrain our own client. The critique's premise, that the canary is a detector where a preventer was available, is right. Its preventer is wrong.

**The preventer that does work, and it is free.** Google's token endpoint returns the granted `scope` string in every token response, including every refresh. The MCP server asserts on startup and on every refresh that the granted scope set equals exactly `{https://www.googleapis.com/auth/drive.file}`, and refuses to start or latches off if it does not. That closes the widening window to zero on the re-consent path, which a fifteen-minute canary cannot. The canary stays for the paths the assertion cannot see, which section 4.4 names. Adopting a weaker control because a stronger one was proposed under a wrong justification would be the wrong trade, so this design takes both, for their real reasons.

**Rejected as primary: the single whole-dict regex form of the permission control.** One critique proposes one leaf selecting `input` and regexing the serialised argument dict for `role|type|emailAddress|...`. It survives nesting, which the per-field form does not, and it costs one leaf. It also matches those words appearing as *values*, and `drive_write_file` takes a free-text `content` argument, so a document about Drive permissions would be denied. A deny control with that false-positive profile gets disabled by the first operator it inconveniences, which is worse than a narrower control that stays on. So the per-field `or` is primary (section 5.3), and the nesting concern is closed by a different mechanism: a unit test asserting every shipped tool's argument schema is flat and scalar-valued, so a tool with a nested argument fails CI rather than quietly escaping the control.

---

## 2. The primitive, restated for Drive

Removing send removes the obvious exfiltration tool and leaves quieter ones. There are three, not one.

**Permission change.** `permissions.create` with `type: "anyone", role: "reader"` publishes a document to the open internet, notifies nobody, sends nothing, and looks in every log like a successful API call by an authorised principal. `type: "user", emailAddress: <attacker>` is the same move with a smaller blast radius and a better story. Neither is a transfer. Both are exfiltration.

**Publish to the web, which is not a permission at all.** Verified against Google's reference: `revisions.update` lists `https://www.googleapis.com/auth/drive.file` among its scopes, and the Revision resource for Docs Editors files carries `published`, `publishAuto`, `publishedOutsideDomain` and `publishedLink`. Setting `published: true, publishedOutsideDomain: true` puts a document on the open internet at a URL, notifies nobody, and never touches `permissions.create`. Any control keying on `role`, `type` or `emailAddress` is blind to it. It is inert against this design's shipped surface only because the agent writes `text/plain` and never creates a Docs Editors file, and the first obvious follow-up request ("write a proper Doc, not a .txt") makes it live. Section 5.3 covers it by name and section 4.5 refuses format conversion for this reason among others.

**Writing into something already shared.** No API call changes a permission and the data leaves anyway. This is the one the design's own happy path creates, and section 4.4 is about it.

Three second-order paths, each verified.

**Inheritance is real and asymmetric.** Drive's sharing guide states that permission lists propagate downward to all child files and folders, and then states the part that matters: inherited permissions cannot be removed or reduced on any item. A file created inside a folder a human already shared outward is shared outward at the moment of creation, and neither the agent nor an operator can un-share it from the child. The fix is on the parent, always. Section 4.3's root design is a consequence of this and nothing else.

**Revision history outlives the current version.** `revisions.list` is authorised by `drive.file`. Overwriting a document does not remove what was there. "The agent overwrote it, so the sensitive text is gone" is false, and nothing in this design says otherwise. The operator-facing form of that goes in the runbook, because somebody will otherwise believe it.

**A share is not an event we see.** `control_execution_events` records what our controls evaluated. A successful permission change that no control matched produces a clean audit trail, which is the same shape `task-dispatcher.md` 13.2 describes for a connector call with ordinary arguments.

---

## 3. The four conditions from 13.5: two satisfied, two not

The brief asks for this plainly, so here it is plainly, before the design that follows.

| Condition | Verdict | What the unmet ones block |
|---|---|---|
| 1. Egress control does not exist | **Not satisfied.** Narrowing makes destinations finite and enumerable for one API. It does not build an egress control. | Blocks a second connector, and blocks any Drive tool that takes a free-form destination-shaped argument. Phase 8's `args_hash` approval is the named precondition for both. |
| 2. Per-namespace executor isolation | **Not satisfied.** The dedicated account removes the workspace-wide grant. It does not create a tenant boundary. | Blocks a multi-agent Drive deployment on one OAuth client until D6 returns. Does not block the single-agent first slice. |
| 3. A credential broker is a vendor decision | **Satisfied, by deciding there is no broker.** | Nothing. The risk is named and owned in 3.3. |
| 4. E5b as a recurring shipping gate | **Satisfied, mechanised as a red build.** | Nothing. |

The rest of this section is the argument for each.

### 3.1 Condition 1: egress control still does not exist

What narrowing does is make the word "destination" finite enough to enumerate, which is the only reason a name-based allowlist gets anywhere near sufficient here.

For Drive, a destination is one of exactly four things:

1. **A permission grant.** `permissions.create` on any file. The recipient is an `emailAddress`, a `domain`, or the literal `anyone`.
2. **A published revision.** `revisions.update` with `published: true`. There is no recipient at all, which is why it is easy to miss.
3. **A parent folder.** `files.create` with `parents: [<id>]`, or `files.update` with `addParents`. If the parent is outside the agent's tree, the file lands wherever that folder's sharing already reaches.
4. **A share scope already in force.** Writing content into a file that is already readable by someone. No API call changes anything. The data leaves.

Destinations 1 and 2 are refused by not exposing the tools, by the argument controls in 5.3, and above both by the Workspace OU settings in 4.1. Destination 3 is refused in the tool: no tool takes a parent argument, and the server resolves ancestry before every write from its own configuration. Destination 4 is **not** refused. It is bounded, to one named internal Google Group, by 4.4, and that is the honest answer rather than a refusal.

That is not a general egress control. It is a closed enumeration that happens to be complete for one API, and the moment a second connector arrives the enumeration is no longer complete. Nobody should read this section as evidence that condition 1 was satisfied.

### 3.2 Condition 2: isolation moves, and there is only one namespace to isolate

13.5 says one executor process holding a workspace-wide Drive grant while serving more than one namespace is a cross-tenant read with no policy violation anywhere.

**A dedicated account with `drive.file` removes the workspace-wide half and leaves a smaller problem.** There is no pre-existing corpus to read across, because the app can only see files it created. What survives: two agents write into one account's Drive, under one root, through processes holding one OAuth client's grant. Agent A asking for a file id belonging to agent B is a request Drive will happily serve, because to Drive both are the same user and the same app.

**And the level that matters today is the agent, not the namespace.** Verified: `HeaderAuthProvider._resolve_namespace_key` (`server/src/agent_control_server/auth_framework/providers/header.py:207`) is `del request; return self._default_namespace_key`, and `NoAuthProvider.authorize` returns `Principal(namespace_key=self._default_namespace_key)`. Neither shipped provider can produce a second namespace. So `<namespace_key>` is a constant string in every deployment reachable today, the cross-namespace refusal in `test_ancestry.py` can only ever be exercised against a fake, and `dispatch preflight` asserting that the executor's namespace matches its Drive subtree would compare two constants. The namespace level stays in the tree as forward compatibility, and this document says that is what it is. The same caveat `orchestration-plan.md` section 15 makes about the ADK fakes applies here and is not re-derived.

The reachable boundary is the `<agent_name>` segment, and it is enforceable, because one executor process serves exactly one agent: a module-level singleton in `sdks/python/src/agent_control/_state.py` and the `ValueError` at `plugin.py:147`. One agent per process makes the agent name a deployment constant the MCP server can be handed in its environment.

**That enforcement is our code, and this design refuses our-code-only enforcement for sharing.** Applying two standards to two risks of comparable size is the inconsistency a reviewer should not let through, and one experiment settles it. `drive.file` is documented as per-app access, which points at a *separate OAuth client per executor* giving Google-enforced 404s between agents. Google does not state per-client isolation in the negative, so it is exactly D1's shape: one afternoon, two branches. **D6** in Phase 0. If it returns 404, one OAuth client per executor becomes the shipped deployment once more than one agent has Drive, and condition 2's answer at the agent level becomes Google-enforced rather than ours. If it returns the file, the answer downgrades to "there is no isolation boundary except our ancestry check", and the multi-agent Drive deployment holds until Phase 8's argument-hash approval exists.

### 3.3 Condition 3: satisfied, by deciding there is no broker

**No Composio, no Zapier MCP, no Pipedream.** The OAuth client lives in a Google Cloud project owned by whoever operates this deployment. The client id and refresh token live in the executor process environment as `AGENT_CONTROL_EXECUTOR_DRIVE_*` under 13.6's rule, and the token is exchanged by our own MCP server against Google's token endpoint. No third party sits between the policy and the action.

Who can revoke it, and what that costs, is section 4.9. It is not the free safety valve an earlier framing made it out to be.

The honest cost of owning it: somebody on this team now maintains an OAuth client, a refresh loop, and a token rotation story, and a refresh token that leaks is a full compromise of the agent's Drive with no revocation signal until a human notices. A broker would have owned that and charged for it. We are choosing the risk we can see over the risk we cannot, and this repo has not yet had the secrets-management conversation that choice implies (section 9).

### 3.4 Condition 4: satisfied, as a red build

Choosing our own MCP server changes the frequency, not the requirement. Our tool list changes when we change it, in a commit, in this repo. That makes the gate mechanisable rather than aspirational.

**The gate is a test, not a process.** `mcp-servers/drive/tests/test_tool_inventory.py` holds a frozen list of tool names and their argument keys, compares it against the server's live `tools/list` output, and fails on any difference including an addition. Changing the frozen list is a diff a reviewer sees, in the same PR as the tool, next to the allowlist regex. A second test asserts the shipped allowlist regex matches every frozen name and nothing else. A third asserts every frozen argument schema is flat and scalar-valued, which is what keeps 5.3 sound (see 1.3).

That is how a real team runs a recurring gate: by making it a red build. The version that becomes a rule nobody follows is the one that lives in a checklist. E5b itself, exposing a never-before-seen tool name and asserting both the deny and the server round trip, runs per release in the wire tier (W4), not per catalogue change, because there is no catalogue.

---

## 4. The decisions

### 4.1 Can it share? No. Here is what enforces that, and what does not

Six layers, listed in order of how much they are worth, which is the reverse of the order they are usually listed in. Only the first is outside our own code.

**Layer 1, Google-enforced, and it is a prerequisite rather than a mitigation.** `agent.control@earlycore.dev` sits in its own Google Workspace organizational unit with, in the Admin console under Drive and Docs sharing settings for that OU:

- **External sharing off.** Google's admin documentation confirms this can be set per OU and that it blocks both invitations to Docs, Sheets and Slides items and links to files stored in Drive.
- **Publish to the web off.** A distinct setting in the same place, and the one that closes destination 2 from 3.1.

Both are enforced by Google, server-side, against every API call, and both survive every bug in every line of code this design describes.

This is layer 1 because of a fact the brief did not have and that I verified against Google's reference: **`drive.file` authorises `permissions.create`.** The scope list for that method is exactly `.../auth/drive` and `.../auth/drive.file`. The same is true of `revisions.update`. So choosing the narrow scope does not remove the sharing primitive or the publishing primitive. It confines them to files the app created, which under section 4.3 is every file the agent has. **The scope choice, which the brief framed as the crux, does not answer this question at all.** The OU does.

**The capability does not ship into an account that is not in such an OU.** If `earlycore.dev` is not a Workspace domain, or the account cannot be placed in an OU with both settings off, that is a blocker to be resolved before Phase 2, not a risk to be accepted. Section 14 says so without softening.

**Layer 2, also Google-enforced, and it is not what one critique thought it was.** App access control, restricting third-party API access to named OAuth clients. Worth having: it stops a *different* client being consented against this account, which is the shadow-app case and part of the stolen-client-secret case. **It does not constrain our own client's scopes**, per 1.3. Two honest costs: whether it can be scoped to an OU rather than the whole domain is **unverified**, and the domain-wide version is a change affecting every user in `earlycore.dev`, which makes it somebody's decision rather than an afternoon. **D7** settles the OU question in Phase 0 and this layer is optional if it turns out to be domain-wide only.

**Layer 3, the granted-scope assertion, which is ours but is checkable rather than trusted.** Google's token endpoint returns the granted `scope` on every token response. The MCP server asserts it equals exactly `drive.file` at startup and after every refresh, and refuses to start or latches off otherwise. This is the preventer for the "somebody widened the scope to make a demo work" path, and it closes that window to zero rather than to fifteen minutes.

**Layer 4, the tool surface.** No permission tool. Not a filtered one, not a read-only one. No revision tool. `permissions.create`, `permissions.update`, `permissions.delete`, `files.delete`, `addParents`, `removeParents` and `revisions.update` have no path to the wire because no handler calls them, and `test_tool_inventory.py`'s grep test fails if one appears in the handler package. The grep bans the *write* methods in `mcp-servers/drive/handlers/`; the read-only outbound audit that 4.4 needs lives in `mcp-servers/drive/audit/` with a test asserting no handler imports it. The guarantee wanted is "no tool can reach a permission write", not "the word never appears", and the earlier phrasing traded the strongest detector for a weaker one.

**Layer 5, the deny-by-default control from 12.2**, mechanism unchanged, Drive-shaped allowlist, scope naming no steps so it is applicable to every tool call. Section 5.1, with the qualified-name correction it forces.

**Layer 6, the argument controls.** 5.2 and 5.3. They exist because layers 4 and 5 are name-based and share one failure: a tool that keeps its name and grows a parameter.

#### 4.1.1 What survives with `api_key_enabled=False`, and why that is a ship prerequisite too

Verified: `server/src/agent_control_server/config.py:73` is `api_key_enabled: bool = False`, and `auth_framework/providers/no_auth.py` documents `NoAuthProvider` as allowing every operation and returning the default namespace, with `authorize` discarding the request and returning a `Principal` unconditionally. In that state anyone who can open a TCP connection to the server port can disable, edit or unbind `drive-deny-unlisted-tool` and both argument controls, because `controls.update` is ADMIN and ADMIN is granted to everyone.

**So in the default configuration, two of the six layers above become advisory: 5 and 6, the ones stored in the control plane.** What survives is layer 1 (Google), layer 2 (Google), layer 3 (the process asserts its own granted scope) and layer 4 (the binary has no handler to reach). That is not a catastrophe, and it is exactly why layers 1 to 4 are ordered above them, but a reader must be able to price it.

**`api_key_enabled=true` is a named ship prerequisite, in the same breath as the OU.** The repo already has the precedent and this design copies it rather than inventing one: `config.py:639` refuses to start when `executor.enabled` is true and auth is off, unless `executor.allow_insecure_local_dev` is set, and `check_agent_config_startup_requirements` (`config.py:778`) gates delivery of saved agent configuration on the same fact. A Drive-shaped equivalent goes in beside them, refusing to start an executor with `AGENT_CONTROL_EXECUTOR_DRIVE_*` configured while auth is off. `dispatch preflight` (12.6) grows the same assertion for the operator-facing path.

### 4.2 Which OAuth scope

**`drive.file`. One scope, nothing else.**

Verified against Google's reference, method by method, because asserting scope semantics is how designs acquire holes:

| Operation | `drive.file` authorised | Note |
|---|---|---|
| `files.create`, including `mimeType: application/vnd.google-apps.folder` | yes | With no `parents`, the file lands at the top of My Drive and is app-created |
| `files.update` | yes | Content and `name`. This design ships no tool for it in the first slice |
| `files.delete` | yes, and it is **permanent**, and on a folder it deletes all descendants owned by the user | No tool. Tier 1 |
| `permissions.create` | **yes** | The reason layer 1 exists |
| `revisions.update` | **yes**, including `published` | The reason 4.1's second OU setting exists |
| `revisions.list` | yes | No tool. Named so nobody thinks overwriting is redaction |

Google classifies `drive.file` as **non-sensitive** and `drive`, `drive.readonly`, `drive.metadata` and `drive.metadata.readonly` as **restricted**. That is a real difference: restricted scopes drag in an annual third-party security assessment for a published app, and non-sensitive ones do not. It is also what makes the consent-screen prerequisite in 4.9 a settings change rather than a project.

**What `drive.file` buys.** The scope grants per-file access, and the documented paths by which a file becomes app-accessible are three: the app created it, the user selected it through the Google Picker, or the user chose the app from Drive's "Open with" menu.

- The **Picker** path needs a browser and a signed-in user. A headless agent has none.
- The **"Open with"** path is gated on the OAuth client having a configured **Drive UI integration** in the Cloud console. Ours will not have one. That is a console setting, not a property of being headless, and a future reader who enables a Drive UI integration for an unrelated reason silently reopens the path. It goes on the Phase 0 admin checklist with the other three Google-side items, and D1 covers it.

So under `drive.file`, with no Drive UI integration configured, the set of files this agent can touch is exactly the set it created, and it stays that way with no policy change anywhere. That is a *dynamic* property, holding tomorrow for shares nobody has made yet, which is worth more than the static read confinement.

**What is lost, plainly.** The agent cannot read a design doc. It cannot read a spec. It cannot read a folder a human shares with it, which is the single most natural thing a person will try in week one and the thing this design will be judged on. 13.2 already gave that up under Linear-only and this does not give it back. The difference is that under `drive` it would work, and somebody will notice. Section 14 is about resisting that.

**One load-bearing claim I will not assert.** Whether a file shared with `agent.control@earlycore.dev` by a human becomes readable by an OAuth client holding only `drive.file`. Google's scope description ("Create new Drive files, or modify existing files, that you open with an app") points at no, and the Picker docs point at no. It is not stated anywhere in the negative. It decides question 4 outright, so it is **D1** in Phase 0 and Phase 2 does not start until it returns.

### 4.3 Folder structure, and who creates the root

```
My Drive of agent.control@earlycore.dev
└── Agent Control                              <- app-created by drive_bootstrap.py. No parent.
    └── <namespace_key>/                       <- app-created by bootstrap. Forward compatibility only (3.2)
        └── <agent_name>/                      <- app-created by bootstrap, one per agent. THE boundary today
            └── <work_folder>/                 <- created by the agent, one level
                └── <files>
```

**The root is created by the OAuth client itself, and this corrects the previous draft.** An earlier version had a human create the root in the Drive UI and had `drive_bootstrap.py` call `permissions.list` on the intended parent before creating it. Both are impossible under `drive.file`: a human-created folder is not app-created, so `files.create` with that folder as `parents` returns 404, and `permissions.list` on it returns 404 as well. The likely field fix, made under deadline pressure, would have been to widen the scope to `drive`, which is exactly what section 14 refuses permanently. So:

`scripts/drive_bootstrap.py` runs the OAuth device flow as a human, calls `files.create({name: "Agent Control", mimeType: "application/vnd.google-apps.folder"})` with **no `parents` field**, and prints the id. A folder at the top of My Drive has no parent whose sharing state could matter, because My Drive itself cannot be shared, so there is no inheritance to check and nothing to refuse. The script takes no `--parent` flag and rejects one if passed. It then asserts, on the folder it just created (which *is* app-created and therefore readable), that the permission set is exactly one entry: the account, role `owner`. That assertion is expressible where the old one was not, and it runs in the `audit/` module rather than in a handler (4.1 layer 4). D1 carries a second assertion proving the old approach's impossibility, so the correction is recorded as a result rather than as an argument.

Levels 2 and 3 are created by the same script from the namespace and agent lists, and are not creatable at runtime by anything. A new agent means re-running the script, which is a deliberate human action taking ten seconds. Root id and agent segment go into the executor environment as `AGENT_CONTROL_EXECUTOR_DRIVE_ROOT_ID` and `AGENT_CONTROL_EXECUTOR_DRIVE_AGENT_SEGMENT`.

#### 4.3.1 The work folder, and the branch that decides its guarantee

The brief asked for "its own folder structure and a sense of folder creations". Level 4 is that, and it is the only creation the agent does. `drive_create_folder(name)` creates a direct child of the agent's own subtree root. One level. **It takes no parent argument at all**, because a parent argument is destination 3 from section 3.1, and a tool that cannot express a destination cannot be talked into a bad one.

**Two branches, with different guarantees, and the previous draft described the strong one and deferred the weak one as if nothing changed.**

*Branch A, per-task, the strong one.* If the session key reaches the MCP server, the server resolves the task from the session and creates the task folder itself, lazily, on the first write of a turn. The agent never chooses a name or a parent for its first write, so the very first file cannot land outside the tree even if every control fails. `drive_read_file` on an id from a previous task is refused.

*Branch B, per-agent fallback, the shipped one if A fails.* The write root is `<root>/<namespace_key>/<agent_name>/`, a deployment constant from the executor environment. The agent creates its own work folders under it by name. The compensating property is that no tool takes a parent argument and the root is not model-chosen, so the worst case is a file in the wrong sibling folder inside the agent's own subtree. Bounded. **Not the same claim.** An id from a previous piece of work resolves and is allowed.

**The previous draft's fallback needed the dispatcher to create the task folder at claim time, and that is refused.** The dispatcher is a separate process (`dispatcher/src/agent_control_dispatcher/`) that runs as a compose service, as a `once` CLI under cron, or as a library in tests, and it authenticates with an ordinary `AUTHENTICATED` key. Having it create a Drive folder means it holds the Google refresh token, which doubles the number of processes holding the only credential that can write to Drive and puts the second one in the least controlled place. A refresh token in a cron container on somebody's laptop is precisely the leak path 3.3 names as the price of not using a broker. It also breaks the executor-to-agent binding that 3.2 rests on, because the dispatcher serves every agent by construction. **Exactly one process holds the grant, and it is the executor.**

Which branch ships is **decided in Phase 0, not "before writing the server"**, because it decides both the tree and the guarantee. It is answerable in an afternoon by reading `header_provider`'s `ReadonlyContext` against the stdio transport, and if the clean answer needs a localhost HTTP MCP server instead of stdio, that is a Phase 2 shape decision rather than a Phase 5 surprise. **D8.**

#### 4.3.2 What stops unbounded creation

Three ceilings, and the previous draft's third one is deleted rather than moved.

- `drive_max_folders_per_task`, default 8, counted in the MCP server against the subtree's child list.
- `drive_max_files_per_task`, default 64, same.
- `drive_max_writes_per_turn`, default 8, **process-local in the MCP server**, needing no credential and no network call.

The deleted one is a per-namespace hourly Drive write ceiling in `agent_dispatch_state`, checked by the MCP server against the control plane. It needed an authenticated call from the tool process to the API on every write, at an access level nobody had chosen. ADMIN would hand a process whose entire job is handling model-chosen arguments an ADMIN key, which is the escalation `task-dispatcher.md` 12.3 refuses in its closing paragraph. AUTHENTICATED would make the counter resettable by anything holding an ordinary key. Either way it adds a second control-plane round trip per Drive call, on a different path with different auth and different failure semantics.

**Turns already bound it.** `max_turns_per_hour` is enforced in `_acquire_turn` against a Postgres row, at a point 12.1's own reasoning says a dispatcher loop cannot bypass. So the hard bound is `max_turns_per_hour x drive_max_writes_per_turn`, which at the defaults is 60 x 8 = **480 writes per hour per namespace**, enforced entirely at points that already exist. The arithmetic is in this document so an operator can see the ceiling without a new table. If a Drive-specific ceiling is genuinely wanted later, it belongs inside `_acquire_turn` beside the turn count, not in a tool.

12.1's rule that an unenforced ceiling in a safety table is worse than none is why the row was deleted rather than left in with a `TODO`.

**Naming.** Folder and file names are normalised the way `agent-file-inputs.md` 3.1 normalises filenames: bidi overrides stripped, path separators refused, length capped. A folder name is a string an injected document can choose, and it renders in a UI a human reads.

### 4.4 What a human sharing a folder with the agent does, and the much likelier thing

Two directions, and the previous draft only covered one.

**Inbound: a human shares a folder with the agent.** Refused by construction, per 4.2, pending D1. There is nothing to accept or refuse because the app cannot see it. Detection in case the construction is wrong is the inbound canary below.

**Outbound: a human shares the agent's tree so they can read what it produced. This is the one that matters, and it is the design's own happy path.** The previous draft asserted that destination 4 is refused because "the root has never been shared", without ever saying how a human reads the deliverables. The first operational act after the first successful run is somebody sharing the root, or a work folder, or setting it link-viewable, so the team can see the output. From that moment `drive_write_file` is a live write channel into a readable document. Inheritance means every child gets it and it cannot be removed from a child. And an inbound-only canary stays green throughout, because `sharedWithMe` is still empty and `owners` is still the agent account.

**The decision: exactly one named internal Google Group gets `reader` on the root, granted once by the human running bootstrap, and nothing else is ever shared.**

That is a real reduction in the design's promise and it is stated as one. Destination 4 goes from zero to non-zero. What bounds it is the shape `task-dispatcher.md` 13.2 already argued for the Linear comment: **an injection influences the content of what is written, never the audience that reads it.** The audience is one internal group, fixed at bootstrap, changeable only by a human in the Drive UI, and asserted continuously. That is a different and much better position than "the reader set is whatever the last person to press Share decided", which is what happens with no decision at all.

The alternative considered and rejected for the first slice: humans read deliverables through the Agent Control console, which means the console needs a read path into Drive, which means either the server holds a Google credential (refused, 4.3.1) or the executor serves file content to the console (a new cross-process path). Real scope, not sized here, and named in section 12 as the thing that would let the group share go away.

#### 4.4.1 The canary, both directions, and it is the piece I would keep if I could keep only one

The MCP server, at startup and every `drive_scope_canary_interval_seconds` (default 900), makes two assertions.

**Inbound.** `files.list` with `q: "sharedWithMe = true"` returns empty. Under `drive.file` it is empty forever, because the app cannot see shared files. Non-empty means the scope was widened or a share path exists that 4.2 says does not.

**Amended 2026-08-06, and it stays an invariant.** `company-knowledge.md` 2.1 records an operator
decision to read the company corpus with *this same account* under a separate `drive.readonly`
client, so `sharedWithMe` is no longer empty by construction and the assertion above would fire
forever - a canary that always fires is one nobody reads. It becomes **"`sharedWithMe` is exactly
one entry, and its id is `AGENT_KNOWLEDGE_DRIVE_ROOT_FOLDER_ID`"**.

That is still a fixed assertion rather than a list to maintain, and it is `company-knowledge.md` 5.7
that keeps it so: the corpus is one shared root descended recursively, not a set of separately shared
folders. Adding knowledge happens *inside* that tree, where it changes nothing this canary looks at.
A second entry appearing in `sharedWithMe` means the same thing the original empty-set assertion
meant - a share path exists that 4.2 says should not - and it fires with the same force.

The dependency runs one way and is worth naming: if that plan ever returns to a multi-root allowlist,
this canary degrades to maintaining the same list, with the failure modes that implies. Keeping the
root singular is what keeps this cheap.

**Outbound, which the previous draft did not have.** `files.list` over the agent's subtree with `fields=files(id,name,shared,ownedByMe,permissions(id,type,emailAddress,role))`, asserting on every node that `ownedByMe == true` and that the permission set is exactly `{agent account: owner}` plus, if configured, `{<the one group>: reader}` and nothing else. `shared` is a documented File field and the `permissions` field is readable on app-created files, so this costs one extra field on a call the canary already makes. It catches a link-viewable file, an added collaborator, an external grant, and the case in 7's table where a human moves the whole root into a folder that is already shared.

Either assertion failing fires `agent_control_drive_outbound_share_detected_total` or `agent_control_drive_scope_canary_failures_total`, latches, and refuses every subsequent tool call until a human clears it.

This is proof by absence, the same method as E1's third criterion and spike H2, applied to a permission surface instead of a side effect.

### 4.5 Create, read, update, delete

| Verb | Decision | Why |
|---|---|---|
| **Create** | Yes. Files and one level of folders, inside the agent's own subtree only, no parent argument, `text/plain` and `text/markdown` only, no format conversion | The feature. No conversion means no Docs Editors file, which is what keeps `revisions.update` publishing inert against the shipped surface (section 2) |
| **Read** | Phase 5, and only files the agent created, and only as text. See 4.6 | Confined by scope, not by policy |
| **Update** | **No. Deferred, with no tool at all** | Below |
| **Delete** | **Trash only, Phase 5** | Below |

**No update tool ships, and the previous draft was internally inconsistent about this.** Its verb table said "Update: yes, content only", its tool surface had no tool that updates content, and its revision-history argument defended a capability the shipped surface did not have. `drive_write_file` refuses a name that already exists, which is create-or-refuse, not update. That is the right default for the first slice: a retrying agent is idempotent, and there is no overwrite path to reason about.

The revision-history material keeps its place because it is true independently of any tool: **overwriting is not redaction**, `revisions.list` is `drive.file`-authorised, and an operator who "removed" sensitive text by overwriting has not. That goes in the runbook, where it stays true the day an update tool arrives.

**Delete means trash, and the distinction is not pedantry.** `files.delete` permanently deletes without moving to trash, and on a folder it deletes all descendants owned by the user. One call with a work folder's id destroys its entire output with no undo. That is 12.2's tier 1, "deletes or overwrites with no undo", so there is no tool and no flag. Trash is tier 2, because trash has undo: the item is restorable and Drive purges it after 30 days. `drive_trash_file(file_id)` is `files.update` with `trashed: true`, never an HTTP DELETE, and it refuses any id whose depth is less than 5, so the work folder, the agent folder, the namespace folder and the root cannot be trashed.

**And the agent needs a remedy, which refuse-on-collision alone does not give it.** With no overwrite and no trash in the first slice, an agent that produces a wrong `report.md` writes `report-2.md`, then `report-3.md`, against the 64-file cap, and the failure is silent name inflation rather than an error a human sees. So: the collision refusal returns a **typed refusal the model can act on**, naming the colliding file, rather than returning the existing id and looking like success. And `drive_replace_file(file_id, content)` ships in Phase 5 alongside `drive_trash_file`, restricted to files inside the agent's own subtree, on the tier 2 side precisely because `revisions.list` makes the prior content retrievable.

The collision refusal is also a cheap existence oracle over the agent's own subtree. Accepted: the subtree is the agent's own output.

### 4.6 Which MCP server, and file content as untrusted input

**A purpose-built server in this repo, at `mcp-servers/drive/`, following 13.2's Linear precedent exactly.** Stdio transport in the executor image (or localhost HTTP if D8 needs it), tool list frozen by test, schemas authored here, scope enforcement in the tool rather than in a prompt or a control. A third-party catalogue is refused, and condition 3 is why: a catalogue holding the OAuth grant sits between our policy and the action, and nobody on this team has agreed to own that vendor risk by name.

The complete surface:

| Tool | Arguments | Phase | Behaviour |
|---|---|---|---|
| `drive_create_folder` | `name` | 2 | Direct child of the agent's subtree root. No parent argument. Refuses past `drive_max_folders_per_task` |
| `drive_write_file` | `name`, `content`, `folder_id` (optional) | 2 | `text/plain` or `text/markdown`, no conversion. `folder_id` must be the subtree root or a direct child, resolved server-side. Refuses a colliding name with a typed refusal. Refuses past `drive_max_files_per_task` or `drive_max_writes_per_turn` |
| `drive_list` | `folder_id` (optional) | 5 | Names, ids, sizes, modified times inside the subtree. Never outside it |
| `drive_read_file` | `file_id` | 5 | Text of a file the agent created inside its subtree |
| `drive_trash_file` | `file_id` | 5 | `files.update trashed=true`. Refuses depth below 5 |
| `drive_replace_file` | `file_id`, `content` | 5 | Content only. No metadata, no parents |

Six tools, two of them in the first slice. Every one resolves ancestry to the subtree root before doing anything, from the executor's own environment, never from a model argument. Every argument schema is flat and scalar-valued, asserted by test (1.3).

#### 4.6.1 Content as untrusted input, reusing `agent-file-inputs.md` rather than re-deriving it

A document the agent reads is untrusted in exactly the way a fetched web page is. One decision makes most of that plan not apply.

**Drive tools return text and never bytes.** `drive_read_file` returns plain text as-is and exports a Google Doc as `text/plain`. It does not return a PDF. It does not produce an `inline_data` part or a `file_data` part, which the plugin blocks unconditionally by default anyway (`plugin.py:131`, `file_data_parts: Literal["allow", "block"] = "block"`).

That retires 2.5's divergence for this feature. The model reads the same characters the controls read, because there is no rendering. There is no text layer to be a subset of a page, so `extraction_status`, `pages_with_no_text` and `max_image_area_ratio` have nothing to describe.

What still applies:

**Second-order injection through a tool result** is covered by the plugin running `before_model_callback` on every model call rather than once per invocation, for the reason its own comment gives. A document read on call 1 and needing refusal on call 3 is refused on call 3.

**Marker neutralisation, and the previous draft's claim about it was wrong.** It said text coming back from Drive goes through `_sanitize.neutralize_marker` "same as message text". Verified: `neutralize_marker` is called in exactly one place, `_extractors.py:45`, inside `_extract_text_from_parts`, which assembles text for control evaluation and transcript assembly. Nothing in `after_tool_callback` rewrites the result the model sees; the plugin returns `None` on the success path and ADK's result goes through untouched. A document containing the literal marker would reach the model verbatim and could forge a "blocked by policy" line, which is precisely the attack `_sanitize.py:69`'s own docstring names, tool results included.

**So neutralisation happens in the Drive MCP server, not in the SDK.** The server is ours, it produces the text, and doing it there needs no SDK change, no ADK-version coupling, and gives one fewer place the guarantee can be dropped. The `_MARKER_RE` substitution from `_sanitize.py:68` is ported into `mcp-servers/drive/` and applied to every string `drive_read_file` and `drive_list` return, **including file and folder names**, since 4.3.2 already concedes a name is attacker-choosable. A unit test asserts a document containing the literal marker comes back with U+2011. If somebody prefers the SDK route instead, size it honestly: `after_tool_callback` returning a rewritten result dict has blast radius across every tool in the product and is not free reuse.

**A PDF in Drive is refused, not read.** `drive_read_file` on a PDF returns a typed refusal naming the format. Reading one needs the converter sidecar from `agent-file-inputs.md` 3.3 and 8, with its `RLIMIT_AS`, its process-group kill and its 200:1 decompression cap. That is a dependency on another plan's Phase 3 and is out of scope here. When it lands, the Drive path feeds the same sidecar and inherits the same honest status names, `text_layer_extracted` rather than `ok`.

### 4.7 Server-side refusals are invisible to the control plane, and the fix costs no credential

Almost every enforcement point in this design lives inside the MCP server and produces no `control_execution_events`: ancestry refusal, the folder and file caps, the per-turn write cap, the depth guard, the collision refusal, the canary, the token latch. `task-dispatcher.md` 12.2's own rule, quoted approvingly in 4.1 layer 5, is that `McpToolset(tool_filter=[...])` is not the allowlist of record *precisely because* it produces no `control_execution_events` row. Applying that standard to the tool filter and exempting our own server from it would be the same mistake with our name on it.

The obvious fix, having the server post structured refusals to the control plane, needs a credential and an endpoint and is refused for the same reasons as the deleted hourly ceiling in 4.3.2.

**The fix that costs nothing rides the path that already exists.** Verified at `plugin.py:528`: `after_tool_callback` calls `_evaluate_and_enforce(..., input=tool_args, output=result, ..., stage="post")`. So a tool *result* is selectable at `output.<field>`, evaluated by every applicable bound control, and produces a real `control_execution_events` row carrying the control name, the agent name and the turn's trace id. This is the same shape as the `block-ssn` control already live in this deployment (post, output).

So every server-side refusal returns a result with a fixed field:

```json
{"refusal_code": "ancestry_outside_subtree", "detail": "...", "file_id": "..."}
```

with `refusal_code` drawn from a closed enum: `ancestry_outside_subtree`, `folder_cap`, `file_cap`, `writes_per_turn_cap`, `depth_guard`, `name_collision`, `latched_scope_canary`, `latched_outbound_share`, `latched_token_refresh`, `unsupported_format`, `upstream_rate_limited`. One bound observe control (`drive-observe-server-refusal`, section 5.4) matches it and every refusal lands in the same table an operator already reads, correlated by trace, with no new credential, no new endpoint and no new failure mode.

The residual, stated: the enum is authored by us, so a refusal path that forgets to set `refusal_code` is invisible. A unit test asserts every refusal return path in the handler package carries one.

### 4.8 The human-read path, decided

Restating 4.4's decision in one place because an implementer will look for it: exactly one internal Google Group holds `reader` on the root, granted by a human at bootstrap, recorded in the executor environment as `AGENT_CONTROL_EXECUTOR_DRIVE_READER_GROUP` so the canary can assert against it. No other permission on any node, ever. Console-mediated reading is the alternative and is out of scope (section 12).

### 4.9 Tokens: expiry, revocation, and the seven-day trap

Three things the previous draft got wrong or omitted, all of which an operator meets in week one or week two.

**The consent screen must be "In production", and this is a Phase 0 prerequisite.** An OAuth client whose consent screen is in **Testing** publishing status issues refresh tokens that expire after seven days, regardless of scope sensitivity. Nothing in a proactive-refresh design accounts for a hard weekly expiry: the capability dies every seventh day, the latch trips, every Drive call refuses, and a human has to notice and re-run the device flow. Weekly. The operator experiences that as the feature being broken rather than as a configuration state. And the reflex fix compounds it: Google invalidates the oldest refresh tokens once a client and user pair exceeds roughly 100 outstanding, so repeated re-bootstrapping eventually kills the running executor's token too. The payoff 4.2 already earned applies here: because `drive.file` is non-sensitive, moving to In production needs no verification review and no annual third-party assessment, so it is a settings change. `drive_bootstrap.py` refuses to complete and prints the publishing status if it receives a token whose refresh lifetime is bounded.

**Refresh, and failure.** The server refreshes proactively at 80% of the access token's lifetime. A refresh failure trips the same latch as the canary: every subsequent tool call refuses until a human clears it. Mid-turn, the tool returns a typed refusal, the model sees a tool error, and the turn completes with a report saying the tool failed. It is **not** a retry and **not** a silent skip, because a capability that half-works is worse than one that is off: the agent writes a confident report about work it did not do.

**Revocation is neither immediate nor free, and the previous draft called it "worth more than any control in this document".** Two corrections. An already-issued access token stays valid for its remaining lifetime, up to roughly an hour, so an executor mid-loop keeps writing after the click. And under `drive.file` specifically, revoking discards the app's accumulated per-file grants; re-authorizing may not restore access to files the app previously created, because access is per-file and granted at creation rather than derived from ownership. If that is so, revoke-and-reauthorize is a one-way door: every existing work folder becomes API-invisible, `drive_list`, `drive_read_file` and the collision check stop working against historical output, and the deliverables are recoverable only by a human in the Drive UI. **D6b** settles it in an afternoon.

So the runbook order is explicit and revocation is not step one: **halt executors first** via `task-dispatcher.md` 12.5's level-3 refusal on the turn path, which is immediate and does not depend on the executor cooperating; **then** revoke; and record that the residual window is bounded by access token lifetime rather than by the click. Whatever D6b returns, the mitigation that always holds is that the account owns the files, so they stay visible and movable in the Drive UI regardless of API access. The deliverable survives. The agent's reach over it does not.

---

## 5. Controls, in a schema that actually validates

**The previous draft's controls could not be authored, and neither could the example in `task-dispatcher.md` 12.2 that they were modelled on.** Verified: `ConditionNode` (`models/src/agent_control_models/controls.py:546`) is exactly one of leaf / and / or / not; a leaf requires **both** `selector` and `evaluator` (`validate_shape` raises "Leaf condition requires both selector and evaluator"); `model_config` sets `extra="ignore"`, so a sibling key like `matches:` or `exists: true` is silently dropped, leaving a node with a selector and no evaluator that fails validation; there is no `any` key; and the built-in evaluator set is exactly `regex`, `list`, `json`, `sql` (`evaluators/builtin/src/agent_control_evaluators/`). **There is no `exists` evaluator.** Every leaf in the previous draft's 5.2 and 5.3 was `{selector: {...}, exists: true}`, which is neither a valid leaf nor a supported predicate.

That is not a cosmetic defect. 5.3 is this design's stated answer to "can it share", and as written it would have failed Pydantic validation at bind time in Phase 3, after the OAuth work was already done.

**Existence is expressible today, without a new evaluator.** `select_data` returns `None` for a missing path (`engine/src/agent_control_engine/selectors.py:39`) and `RegexEvaluator.evaluate` returns `matched=False` when `data is None` (`regex/evaluator.py:57`). So `{"selector": {"path": "input.role"}, "evaluator": {"name": "regex", "config": {"pattern": "."}}}` means "present and non-empty". Two residuals, written down rather than discovered: a present-but-empty-string argument does not match, and JSON `null` does not match. Neither is a plausible way to grant a permission.

A first-class `exists` evaluator would be cleaner. It is a new evaluator package with its own registration, roughly two days, and it is a line item in section 9 rather than an assumption.

**A test constructs every shipped control below through the real Pydantic model**, so a control that cannot be authored fails CI rather than Phase 3. The same fix is flagged back to `task-dispatcher.md` 12.2 so the invalid shape stops propagating, and that is the one edit this plan asks another document's owner to make.

### 5.1 Deny by default, and the qualified name

```yaml
name: drive-deny-unlisted-tool
execution: server
scope:
  step_types: ["tool"]
  stages: ["pre"]
  # No step_names. No step_name_regex. Deliberate: naming steps is what makes it fail open,
  # and get_applicable_controls (core.py:590) filters on name ONLY inside that branch.
action:
  decision: deny
condition:
  not:
    selector:
      path: "name"
    evaluator:
      name: "regex"
      config:
        # UNANCHORED AT THE FRONT ON PURPOSE. path:"name" selects the RESOLVED step name,
        # which _resolve_tool_step_name (plugin.py:745) builds as "<agent_step>.<raw_name>",
        # e.g. "root_agent.drive_write_file". An anchored "^drive_write_file$" matches
        # nothing, denies every call, and looks like a working control until the day it does not.
        pattern: "(^|\\.)(drive_create_folder|drive_write_file|get_current_time|get_weather)$"
```

The comment is not decoration. This expression is the difference between a working allowlist and one that denies every call, and a future operator editing it in the console has no way to know the step name carries a prefix unless section 6 ships.

Phase 5 adds `drive_list|drive_read_file|drive_trash_file|drive_replace_file` to the alternation, in the same PR as the tools, enforced by the coherence test in 3.4.

### 5.2 Parent arguments, redundant on purpose

```yaml
name: drive-deny-parent-arguments
execution: server
scope: { step_types: ["tool"], stages: ["pre"] }
action: { decision: deny }
condition:
  or:
    - selector: { path: "input.parents" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.addParents" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.removeParents" }
      evaluator: { name: "regex", config: { pattern: "." } }
```

None of the six tools takes a parent argument, so against the shipped surface this can never match. That is the point. It matches the day a tool grows one, which is the failure layers 4 and 5 share.

### 5.3 Share and publish arguments, the one that answers "can it share"

```yaml
name: drive-deny-share-or-publish-arguments
execution: server
scope: { step_types: ["tool"], stages: ["pre"] }
action: { decision: deny }
condition:
  or:
    - selector: { path: "input.role" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.type" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.emailAddress" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.permissionId" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.domain" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.sendNotificationEmail" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.transferOwnership" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.published" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.publishAuto" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.publishedOutsideDomain" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.publishedLink" }
      evaluator: { name: "regex", config: { pattern: "." } }
    - selector: { path: "input.keepForever" }
      evaluator: { name: "regex", config: { pattern: "." } }
```

This works today, verified, because `before_tool_callback` (`plugin.py:463`) passes real `tool_args` as `input` regardless of what the registered schema says. It is the concrete form of the pre-stage permission control the brief asked for, extended with the publish fields section 2 found.

**Its limits, stated.** It keys on the field names Google's API uses, so a wrapper taking `{"share_with": "..."}` passes it. It reads one level deep, so a nested argument escapes it, which is closed by the flat-schema test in 3.4 rather than by a lossier regex (1.3). Neither is a reason to skip it: the realistic accident is a thin wrapper passing Drive's own field names through, and the deliberate case is covered by layers 1 and 4. Both are reasons not to call it complete.

### 5.4 Server refusals, observed

```yaml
name: drive-observe-server-refusal
execution: server
scope: { step_types: ["tool"], stages: ["post"] }
action: { decision: observe }
condition:
  selector:
    path: "output.refusal_code"
  evaluator:
    name: "regex"
    config:
      pattern: "^(ancestry_outside_subtree|folder_cap|file_cap|writes_per_turn_cap|depth_guard|name_collision|latched_scope_canary|latched_outbound_share|latched_token_refresh|unsupported_format|upstream_rate_limited)$"
```

Section 4.7's mechanism. It turns every server-side refusal into a real event with a trace id, at the cost of one bound control.

### 5.5 Which control denied it

`ControlExecutionEvent` (`models/src/agent_control_models/observability.py:30`) carries `control_name`, `agent_name`, `applies_to`, `check_stage`, `selector_path` and `trace_id`, and carries **no step name**. So "which tool was denied" must be inferred, and for Drive that is not a diagnostic nicety: "the agent tried to read outside its folder" and "the agent tried to share something" are different incidents with different responses.

**The discriminator is the control name**, which is why 5.2 and 5.3 are separate controls that could have been one `or`, and why 5.4 exists at all rather than the refusals being counters.

- `drive-deny-share-or-publish-arguments` firing is a page.
- `drive-deny-unlisted-tool` firing is a look at the last commit.
- `drive-deny-parent-arguments` firing means a tool grew an argument nobody noticed.
- `drive-observe-server-refusal` with `ancestry_outside_subtree` is the observable signature of a model being talked into a file id it should not have, which is the closest thing this design has to an injection detector.

`selector_path` gives a second discriminator inside 5.3 when a single leaf matched. It is null for composite matches, so the control name stays primary.

Making the event carry a step name is server work that would improve every incident review in this product. Out of scope here, named in section 12.

---

## 6. The SDK prerequisite, and the argument for it

The brief asks whether fixing `_iter_tools` is a prerequisite or a follow-up. **Prerequisite, for a reason that is not the one in the brief.**

Defects A and B are in 1.2. Together they mean an operator opening the console to author the allowlist in 5.1 sees a step called `MCPToolset`, sees no Drive tool names until after those tools have run at least once, and sees no arguments ever. They will write `MCPToolset`, or they will write the bare tool name off a PR. Both fail open silently: `get_applicable_controls` skips a non-matching name with no warning, `_has_applicable_prefiltered_server_controls` returns false, and `evaluation.py:514` returns `is_safe=True`.

For Exa that costs a search. For Drive the same mistake, made the day somebody adds a seventh tool, costs whatever that tool does. Defect B specifically blocks 5.3: an operator cannot author an argument control against a field the console cannot tell them exists.

**The counter-argument, stated fairly.** 5.1 names no steps, so it does not depend on either defect, and it is the thing that actually holds the line. Both defects could ship as follow-ups without weakening it.

**Why it is still a prerequisite.** 12.2's own argument is that a control written the same week as the tool it governs is written by somebody who already knows the answer. The console is how that control gets maintained by somebody who does not. Shipping a Drive capability whose policy surface is invisible in the product means the only correct allowlist is the one in this document, and section 3.4's whole gate design exists because documents drift from deployments. Fix A and B in the phase before the first Drive tool exists. They are 1.5 weeks together and they improve the live Exa controls as a side effect.

---

## 7. Edge cases

| Case | Decided behaviour |
|---|---|
| **A file created inside a folder a human already shared outward** | Cannot happen for the agent's own writes: every parent is inside a root created with no parent at the top of My Drive, and no tool takes a parent argument. **Can** happen if a human later moves the root into a shared folder in the Drive UI, which re-parents the whole tree and inherits sharing with no API call by anyone. That is why the outbound canary (4.4.1) is continuous rather than a bootstrap-time check |
| **A human shares the tree so the team can read the output** | Expected, and it is the design's happy path. Bounded to one named internal group by 4.4, asserted by the canary on every node. Any other grant latches the server off |
| **Agent changes a permission on a file it created** | Refused at six layers (4.1). The ones that hold if our code is wrong are the two Google-side OU settings. In a deployment without them this is **not** prevented, because `drive.file` authorises `permissions.create`. Ship order reflects that |
| **Agent publishes a revision to the web** | Same shape, different API. No tool, 5.3 covers the argument names, the OU's publish-to-web setting is the Google-enforced layer, and no Docs Editors file exists to publish because 4.5 refuses format conversion. D2b proves the OU setting; W5 proves the control |
| **Folder shared with the agent mid-task** | Invisible under `drive.file` (4.2), pending D1. The inbound canary asserts `sharedWithMe` stays empty |
| **Unbounded folder creation, DoS or quota exhaustion** | Three ceilings (4.3.2), with the hard bound `max_turns_per_hour x drive_max_writes_per_turn` = 480/hour/namespace, all at points that already exist. Drive's own per-user write quota is a fourth and it is not ours: exhausting it breaks the human account too. A 403 `userRateLimitExceeded` returns a typed refusal with `Retry-After` and never an upstream body, following `services/executor_client.py`'s hand-written-constant discipline |
| **Revision history holds content the current version does not** | True. No tool exposes revisions, so the agent cannot mine history, and once `drive_replace_file` ships in Phase 5 the operator-facing consequence goes in the runbook: overwriting is not redaction |
| **Deletion into trash versus permanent** | Only trash, only Phase 5, `files.update trashed=true`, depth guard below 5. `files.delete` is tier 1 with no tool. Drive purges trash after 30 days and that is the retention story |
| **Two agents writing the same file** | Under branch A they have different task folders. Under branch B, two agents have different `<agent_name>` segments, so the collision needs one agent racing itself, which per-agent concurrency of 1 (`task-dispatcher.md` 9.1) prevents. `drive_write_file` refuses a colliding name with a typed refusal either way; Drive has no lock and no merge |
| **OAuth grant expires mid-turn** | Typed refusal, latch, human clears it. Not a retry, not a silent skip (4.9) |
| **Consent screen left in Testing** | The capability dies every seven days. Phase 0 prerequisite; `drive_bootstrap.py` refuses to complete and prints the publishing status (4.9) |
| **Repeated re-bootstrapping** | Google invalidates the oldest refresh tokens past roughly 100 outstanding per client and user, so re-running the device flow to "fix" an outage eventually kills the running executor's token. The runbook says halt first, re-bootstrap once, never in a loop |
| **Provider renames a tool so no allowlist names it** | There is no provider. Our own rename fails `test_tool_inventory.py` and the coherence test in the same PR. If both are bypassed, 5.1 denies the new name because it names no steps |
| **The qualified-name fail-open hit once** | 5.1's pattern is unanchored at the front with the reason in a comment, and W2 asserts a bare-name control matches nothing, so the failure is proven rather than remembered |
| **Document whose text layer and rendering disagree** | Does not arise: Drive tools return text and never a rendering (4.6.1). It returns the moment a PDF path is added, which is out of scope and depends on `agent-file-inputs.md`'s sidecar |
| **A document containing the literal transcript marker** | Neutralised in the MCP server, not the SDK, because the SDK path does not cover tool results (4.6.1). Applies to names as well as content |
| **Agent reads a file it was never meant to see because the scope was `drive`** | The granted-scope assertion on startup and every refresh (4.1 layer 3) refuses to run at all, which is a preventer. The canary is the detector for paths the assertion cannot see. The scope string is executor environment under 13.6's rule and is not API-settable |
| **An operator moves a work folder out of the tree in the Drive UI** | Ancestry resolution now fails for every id under it and the tool refuses with `ancestry_outside_subtree`. Visible as a `control_execution_events` row via 5.4, not as a silent failure. The runbook says move it back rather than widen the check |
| **A shortcut file whose target is outside the tree** | Refused. Covered by `test_ancestry.py` |
| **A folder with thousands of children** | `files.list` pagination in the tool, with a hard page cap. The child count is what the folder and file caps are counted against, so an unpaginated read would under-count and let a cap be exceeded. Named because it is the boring bug that breaks a ceiling |
| **The dedicated account's Drive fills up** | 15GB consumer, more on Workspace. Quota-exceeded write returns a typed refusal. `agent_control_drive_bytes_written_total` by namespace is the metric that would have warned |
| **A work folder outlives the task** | Yes, deliberately. Sessions are deleted when a task ends (`task-dispatcher.md` section 6); Drive folders are not, because the folder is the deliverable. Nothing cleans them up. That is a real cost, named in section 12 |
| **The agent writes something that should not have been written** | Nothing here prevents that. It is confined: one subtree, one account, an OU that cannot share externally or publish, readable by one named internal group. The Linear comment path from 5.6 of the dispatcher plan still has the larger fan-out. Drive's fan-out under this design is one group, and that is why it is defensible |
| **Control plane unreachable** | Drive tools keep working. There is no per-write control-plane call left in the design (4.3.2), so a control-plane outage no longer becomes a Drive outage. The turn ceiling that bounds writes is enforced on the turn path, which is already unavailable in that outage, so the bound holds by construction |

---

## 8. Testing

### 8.1 Unit, and what they cannot prove

`mcp-servers/drive/tests/test_ancestry.py`. Ancestry against a fake Drive client in the `LinearClient` fake style: an id inside the subtree resolves; an id one level above refuses; an id in another agent's subtree refuses; an id in another namespace's subtree refuses (untestable end to end, per 3.2, and the test says so in a comment); a cyclic parent chain terminates rather than recursing; a file with no parents refuses; a shortcut targeting outside refuses; a paginated child list is fully walked before a cap is evaluated. Depth guard: ids at depth 1 to 4 refuse `drive_trash_file`, depth 5 and below allow it.

`mcp-servers/drive/tests/test_tool_inventory.py`. The frozen list from 3.4, the allowlist-coherence assertion, the flat-schema assertion from 1.3, the grep assertion that no module under `handlers/` references `permissions.create`, `permissions.update`, `permissions.delete`, `files.delete`, `revisions.update`, `addParents` or `removeParents`, and the assertion that no handler imports `audit/`. The grep is the cheapest thing in this section.

`mcp-servers/drive/tests/test_limits.py`. Folder cap, file cap, per-turn write cap, collision returning a typed refusal rather than an id, name normalisation including a bidi override and a path separator, marker neutralisation on content and on names, oversize content refused with a typed error, every refusal path carrying a `refusal_code` from the enum.

`server/tests/test_drive_controls.py`. **First, a compile test**: every control in section 5 constructed through the real `ControlDefinitionRuntime.model_validate`. Then behaviour against synthetic `EvaluationRequest` objects: a qualified name matching 5.1's unanchored pattern; a bare name **failing** an anchored one, asserted explicitly so the trap is a test rather than a comment; `{"role": "reader"}` denying; `{"share_with": "..."}` **passing**, asserted so 5.3's limit is written down as a test rather than a caveat; `{"published": true}` denying; an output dict with `refusal_code` matching 5.4.

**What none of this proves.** These are our fakes of Google's API. They verify this repo's fiction of Drive, exactly as `orchestration-plan.md` section 15 warns about the ADK fakes.

### 8.2 Integration, real MCP server, no Google

`mcp-servers/drive/tests/test_integration.py`, marked, running the real stdio (or localhost HTTP) server against a Drive stub speaking real HTTP shapes. Full lifecycle: bootstrap creates the tree, the agent creates two folders, writes four files, and (Phase 5) lists and reads one back. Failure injection: 403 `userRateLimitExceeded` producing a typed refusal with `Retry-After` and no upstream body; token refresh failure tripping the latch and every subsequent call refusing; the inbound canary returning a non-empty `sharedWithMe`; the outbound canary finding a node with an unexpected permission; a token response whose granted scope is `drive` refusing at startup.

### 8.3 Wire level, proof by absence, the pattern this project has used twice

The load-bearing tier. Same method as E2 for Exa and H2 for ADK: a recording proxy between the MCP server and Google, and the assertion is what is **absent** from the recording.

**W1, the deny reaches the wire as nothing.** Bind 5.1 with an allowlist excluding `drive_write_file`. Run a turn the model resolves into a `drive_write_file` call. Assert: no `POST /upload/drive/v3/files` in the proxy log; a `control_execution_event` with `matched=true` carrying the real arguments; and the Drive account contains no new file, checked out of band under a separate credential. The third assertion distinguishes "blocked" from "blocked after it ran".

**W2, the qualified-name fail-open, reproduced then fixed.** Bind a control whose `step_names` is `["drive_write_file"]`, the bare name. Run the same turn. Assert the file **is** created, no `control_execution_event` exists, and no warning is logged. That is the failure, proven. Then bind 5.1, re-run, assert no file. W2 is the regression test for the whole class and must be written to fail against a naive allowlist.

**W3, permission arguments never reach the wire.** Add a temporary `drive_share_file(file_id, role, type)` in a test-only build. Run a turn calling it. Assert no `POST /drive/v3/permissions` in the proxy log, a `control_execution_event` naming `drive-deny-share-or-publish-arguments`, and the file's permission list unchanged out of band. Delete the tool.

**W4, the never-before-seen tool.** Add `drive_something_new()` doing nothing. Run a turn. Assert denied and assert a server round trip happened. Do **not** add it to the allowlist first: proving a named tool is denied proves nothing.

**W5, publish never reaches the wire.** Test-only `drive_publish_revision(file_id, published)`. Assert no `PATCH /drive/v3/files/*/revisions/*` in the proxy log, a `control_execution_event` naming 5.3, and the file's `publishedLink` still absent out of band.

**W6, a server refusal becomes an event.** Call `drive_write_file` with a `folder_id` outside the subtree. Assert no upload in the proxy log, and a `control_execution_event` for `drive-observe-server-refusal` with `matched=true` on the turn's trace. This proves 4.7's mechanism, which is the only thing making the server's own enforcement visible to the control plane.

### 8.4 What runs in CI, and what only a live Google account can prove

**In CI on every PR:** all of 8.1, all of 8.2 against the stub, the inventory freeze, the coherence test, the flat-schema test, the control compile test, and W2, W4 and W6 against a stubbed upstream. W1, W3 and W5 also run stubbed, which proves the control fires but not that Google would have accepted the call.

**Only a live Google account can prove, run manually into a throwaway Workspace before each release:**

- **D1**, the load-bearing one. Share a folder from a second account with `agent.control@earlycore.dev` through the Drive UI. Assert an app holding only `drive.file` gets 404 on `files.get` and that it does not appear in `files.list`. Second assertion: `files.create` with a human-created folder as `parents` returns 404, proving 4.3's correction. Third: with no Drive UI integration configured, no "Open with" path exists. **Phase 2 does not start until D1 returns**, and a "yes it is visible" answer changes 4.4's inbound answer from "refused by construction" to "detected only", which is a different and much weaker design.
- **D2**, the OU, sharing. With external sharing off for the agent's OU, call `permissions.create` with `type: "anyone"` directly, outside our stack, with a raw token. Assert Google refuses.
- **D2b**, the OU, publishing. Same setup, `revisions.update` with `published: true, publishedOutsideDomain: true` on a native Doc. Assert Google refuses. If it does not, publish-to-web off for that OU becomes a second hard prerequisite with the same ship-blocking status as external sharing.
- **D3**, inheritance. Create a folder, share it externally, create a file inside it via the API, assert the file is externally visible, then attempt to remove the permission from the child and assert Google refuses. Proves the sharing guide's claim in our own account and is why 4.3 puts the root at My Drive top level.
- **D4**, delete semantics. `files.delete` on a folder containing two files; assert both gone and neither in trash. Then `files.update trashed=true` on a file and assert it is restorable.
- **D5**, revision persistence. Write a file with a marker string, replace it, assert the marker is retrievable through `revisions.get`.
- **D6**, per-client isolation (3.2). Create a file with client A; attempt `files.get` with client B holding `drive.file` against the same account; assert 404. Decides whether one client per executor is a Google-enforced agent boundary.
- **D6b**, revocation (4.9). Create a file, revoke at `myaccount.google.com/permissions`, re-authorize, assert whether `files.get` on the original id returns 200 or 404. Decides whether revocation is a reversible safety valve or a one-way door.
- **D7**, app access control (4.1 layer 2). Whether it can be scoped to an OU rather than the whole domain. If domain-wide only, layer 2 is somebody's decision rather than an afternoon and the layer is optional.
- **D8**, the session key (4.3.1). Whether `header_provider`'s `ReadonlyContext` can carry the session key to the MCP server, deciding branch A or branch B, the folder tree, and the guarantee. Answerable by reading ADK source; no Google account needed, so it runs first.

Also on the Phase 0 admin checklist, not experiments but prerequisites to confirm in writing: the OU exists with external sharing off and publish-to-web off; the consent screen is In production; no Drive UI integration is configured for the client; `api_key_enabled` is true in the target deployment.

### 8.5 The recurring gate

3.4's gate is `test_tool_inventory.py` plus the coherence, flat-schema and compile tests, all in CI, all red on any tool change. W3, W4, W5 and W6 re-run per release. D1 through D8 re-run when the scope string changes, when the OAuth client changes, when a Google admin setting changes, or annually, whichever is sooner, and the runbook names who owns that.

---

## 9. Phases and effort

One engineer, including tests. Each phase is one branch, at most one migration, and regenerates all three SDK artifacts if it touches routes, per `orchestration-plan.md` section 12. Nothing here touches routes, which is why no SDK regeneration days appear.

**Phase 0: experiments and the admin checklist. 1 week. Blocks everything.**
D8 first (no Google account needed, decides the server's shape). Then D1 through D7 in a throwaway Workspace, plus confirming all four Google-side prerequisites in writing. Output is nine recorded results appended to `docs/plans/spike-findings.md` in that file's format. If D1 says shared folders are visible under `drive.file`, stop and re-plan 4.4 before continuing. If D2 or D2b says the OU does not refuse, stop and read section 14.

**Phase 1: the SDK fixes from section 6. 1.5 weeks. Depends on nothing, runs in parallel with Phase 0.**
`_iter_tools` resolves toolsets through `canonical_tools` so `_discover_steps` registers real tool names at bind time. `_resolve_schema_source` prefers `_mcp_tool.inputSchema` for MCP tools. Tests in `sdks/python/tests/test_google_adk_plugin.py`, plus a pinned-ADK contract case asserting `McpTool` exposes `_mcp_tool` with an `inputSchema`, because without it the tests prove only that the fake matches its author's guess. Improves the live Exa controls as a side effect.

**Phase 2: the Drive MCP server, create and write only. 2 weeks. Depends on Phase 0.**
`mcp-servers/drive/`, `scripts/drive_bootstrap.py` with the device flow, no-parent root creation and the post-create permission assertion, token refresh with the 80% proactive refresh, the granted-scope assertion and the failure latch, ancestry resolution with pagination, `drive_create_folder`, `drive_write_file`, the three caps, the typed refusal envelope with `refusal_code`, marker neutralisation, and both canary directions. All of 8.1 and 8.2.

**Phase 3: the controls and the wire proof. 1 week. Depends on Phase 2.**
The four controls from section 5, bound, with the compile test. W1 through W6. The inventory, coherence and flat-schema tests in CI. This is where the capability becomes governed rather than merely narrow, and it ships **before** the dispatcher can reach the tools.

**Phase 4: executor integration and preflight. 0.5 weeks. Depends on Phase 3.**
The Drive environment variables in the executor image under 13.6's rule, the startup refusal when `api_key_enabled` is false (4.1.1) modelled on `config.py:639`, `dispatch preflight` asserting the executor's agent segment matches its Drive subtree and that auth is on. No dispatcher changes, because 4.3.1 removed the dispatcher from the credential path.

**Phase 5: read, replace, trash, observability, UI. 1.5 weeks. Depends on Phase 4.**
`drive_list`, `drive_read_file` with text-only export and the PDF refusal, `drive_replace_file`, `drive_trash_file` with the depth guard, the 5.1 allowlist extension in the same PR, the metrics in section 10, and a console panel showing the agent's tree and the canary state. **Section 14 says read may not be worth building even here.**

**Optional line item, not scheduled: a first-class `exists` evaluator. 2 days.** A new evaluator package with registration and tests, replacing the `pattern: "."` idiom in 5.2 and 5.3 and removing its two residuals. Everything works without it.

**Total: 6.5 weeks**, against four Google-side prerequisites that are somebody's afternoon each and that nothing can proceed without, plus the `api_key_enabled` prerequisite that is a deployment decision.

Two things that estimate omits, in the spirit of `task-dispatcher.md` 15.1. The OAuth client needs a Google Cloud project, a consent screen and the In-production switch, and `drive.file` being non-sensitive is what keeps that small rather than a verification project. And somebody has to own a refresh token in production, which is a secrets-management conversation this repo has not had and which no phase above resolves.

---

## 10. Observability

```
agent_control_drive_calls_total{tool, result=ok|denied|refused|upstream_error}
agent_control_drive_bytes_written_total              counter, by namespace
agent_control_drive_folders_created_total            counter, by namespace
agent_control_drive_ancestry_refusals_total
agent_control_drive_scope_canary_failures_total      # flat at zero forever
agent_control_drive_outbound_share_detected_total    # flat at zero forever
agent_control_drive_token_refresh_failures_total
agent_control_drive_token_age_seconds                gauge
agent_control_drive_latch_active                     gauge, 0 or 1
```

Two should be flat at zero for the life of the deployment, like `file_data_parts_total` in `agent-file-inputs.md`: the two canary counters. Any value at all is worth a look, because the only ways to move them are a scope widening, a share the design says cannot exist, or a permission somebody added by hand.

`token_age_seconds` exists because of 4.9: a shortening refresh lifetime is visible before it is an outage.

`ancestry_refusals_total` moving is the observable signature of a model being talked into a file id it should not have. Unlike the previous draft, it is **not** the only signal: 5.4 puts the same refusal in `control_execution_events` with a trace id, so incident review is a query rather than a metrics graph plus a log grep.

Content is never logged above DEBUG, extending `orchestration-plan.md` section 11 to file names, folder names and file content. A test asserts a written string is absent from captured log output at INFO.

---

## 11. The minimum defensible slice, and it is about three weeks

**The previous draft called this "what a person could run in a week" and then listed four days of experiments, a cut-down OAuth server with refresh and a latch, ancestry resolution, a canary, and bound controls. That is three to four of the seven weeks, relabelled.** This project's plans are unusually good at honest sizing and that was the one place the document softened. So, two answers.

### 11.1 What the first week alone produces

D8, D1 and D2 returning; the four Google-side prerequisites confirmed or found missing; and a decision. If D1 or D2 goes the wrong way, the first week's product is knowing not to build this in its current form, which is worth the week.

### 11.2 The three-week slice, write-only

**Write-only. The agent's Drive is an output destination, not an input source.** With no read tool at all there is no path from any document to any model, so the entire untrusted-input class in 4.6.1 is absent rather than mitigated. And it still delivers what the user asked for in their own words: a folder structure the agent owns and a sense of folder creation.

Phase 0 (or at minimum D8, D1, D2, D2b) plus a cut-down Phase 2 plus Phase 3:

1. A human runs `drive_bootstrap.py` once. It creates the root with no parent, asserts the permission set is exactly the account as owner, and prints the id.
2. Root id, agent segment, reader group and the OAuth credentials go into one executor's environment. Auth is on.
3. The MCP server ships two tools: `drive_create_folder` and `drive_write_file`. No read. No list. No trash. No replace.
4. 5.1 is bound with a four-name allowlist unanchored at the front, and 5.3 is bound alongside it.
5. Both canary directions run at startup and every 15 minutes. The granted-scope assertion runs on every token.
6. The dispatcher's existing file source, already shipped at `dispatcher/src/agent_control_dispatcher/sources/file.py`, drives one agent through one step whose brief is "write your findings into your work folder".
7. W1, W4 and W6 run against the stub before anyone presses play.

What a person sees at the end: a folder in `agent.control@earlycore.dev`'s Drive, named by the agent, containing what it wrote, readable by one internal group, shareable by nobody.

### 11.3 A genuine one-week spike, if somebody needs to see it move

Explicitly labelled a spike, on a throwaway Google account, deleted afterwards, never pointed at a real namespace: a human-created root, a hardcoded folder id, one `drive_write_file` with a manually pasted access token that expires in an hour and no refresh loop, 5.1 bound, no canary. It proves the tool path and nothing about the safety design. Do not let it become the thing that ships.

---

## 12. Out of scope, explicitly

- **Gmail, and every form of send.** Not designed, not sketched, not left as a hook. If a future capability needs mail, that is a new plan with its own safety section, and 13.2's asymmetry argument is where it starts.
- **Reading anything the agent did not create.** Refused by scope, deliberately. Section 14 argues against relaxing it.
- **PDFs and Office formats in Drive.** Depends on `agent-file-inputs.md`'s converter sidecar. Named, not built.
- **Shared drives.** Different permission model, `organizer` and `fileOrganizer` roles, and a corpus the agent did not create. Out.
- **Google Docs as a format.** The agent writes plain text and markdown, with no conversion, which is also what keeps the publish primitive inert. Creating a native Doc needs the Docs API and a second scope.
- **Any permission write.** Banned in handlers by test. The read-only outbound audit in `audit/` is in scope and is the canary's engine.
- **Console-mediated reading of deliverables.** The alternative to 4.4's group share. Needs either a Google credential in the server (refused) or an executor-to-console file path (new work). It is what would let the group share go away, and it is not sized here.
- **Retention and cleanup of work folders.** Folders outlive tasks by design and nothing deletes them. Somebody will eventually want a policy. Not this plan.
- **A step-name field on `control_execution_events`.** 5.5 works around its absence. Fixing it belongs in the dispatcher plan's Phase 7.
- **A per-namespace Drive write ceiling in `agent_dispatch_state`.** Deleted in 4.3.2, with the arithmetic that replaces it. If it comes back it belongs inside `_acquire_turn`.
- **Multi-account or per-human Drive identity.** `HeaderAuthProvider._resolve_namespace_key` is `del request; return self._default_namespace_key`, so there is no per-user identity to hang a Google grant on, exactly as `agent-file-inputs.md` 3.4 already found for Slides export.

---

## 13. Open questions a reviewer should push on

1. **Is one internal group with reader on the root the right answer to 4.4, or is the console read path worth its cost?** The group share makes destination 4 non-zero forever. The console path keeps it at zero and adds a cross-process file path nobody has sized. This design picks the group because the alternative is unsized, not because it is obviously better.
2. **Does D6 return 404?** If yes, one OAuth client per executor is nearly free and turns condition 2's agent-level answer from ours into Google's. If no, multi-agent Drive holds.
3. **Is branch B acceptable indefinitely, or does D8 need to succeed?** Branch B's guarantee is weaker in a way an implementer must not gloss over, and 4.3.1 states both.
4. **Should the `exists` evaluator ship instead of the `pattern: "."` idiom?** Two days, removes two residuals, changes nothing else.
5. **Who owns the refresh token in production?** Named in 3.3 and 9 as a conversation this repo has not had.

---

## 14. What should not ship, and I am not softening it

Three recommendations. One refusal, one hold, and one that blocks the whole capability.

**Refuse, permanently, under this design: the full `drive` scope.** Not "later", not "behind a flag". `drive` is a restricted scope granting view and manage over all of the account's files, and the account is a shared identity that humans will eventually put things into. It converts every second-order path in section 2 from theoretical to live, it makes the inbound canary meaningless because `sharedWithMe` would legitimately return content, and it deletes the single best property this design has, which is that reach cannot widen without a code change. If a use case genuinely needs it, that use case needs a different account and a different plan.

**Hold: `drive_read_file`, `drive_list` and `drive_replace_file` until Phase 5, and reconsider even then.** The slice is write-only for the reason in 11.2, and the reason to keep holding is that a read tool converts this from a capability with no injection surface into one with the same second-order surface as a fetched web page. It is survivable, it is covered by `before_model` running on every model call, and 4.6.1's text-only decision removes the worst of it. But it is not free, and it buys the agent only the ability to read what it itself wrote, which is a much smaller prize than it sounds like. If Phase 5 arrives and nobody has a concrete task that needs it, do not build it.

**And the hardest one. If the Workspace OU with external sharing off and publish-to-web off does not exist, or `earlycore.dev` is not a Workspace domain, this capability should not ship at all in its current form.** Not a reduced version. Not a "we will be careful" version. The reason is 4.1's verified fact: `drive.file` authorises both `permissions.create` and `revisions.update`, so every layer in our own stack that stops sharing or publishing is a layer written by us, tested by us, and one renamed tool or one wrong regex away from not working. Layers 1 and 2 are the only ones that are not, and layer 2 does not constrain our own client (1.3). A design whose central promise rests entirely on its own correctness is a design that has not been reviewed honestly, and this project has drawn that line five times and been right each time.

**The same sentence applies, with less force but the same shape, to `api_key_enabled=False`.** In that configuration two of the six layers are advisory and anybody who can reach the port can remove them. It is not a reason to refuse the capability, because the surviving layers are the Google-side ones and the binary's own absence of a handler. It is a reason to make auth a named ship prerequisite in the same breath as the OU, and to write down, as 4.1.1 does, exactly which layers a reader is left with if somebody ships it anyway.
