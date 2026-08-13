# Agent file outputs: a deliverable the agent writes, and a human opens from the ticket

Status: design. Nothing built.

Branch context: `feat/dispatcher-goal-scaffold` and the five PRs open behind it.

Scope: an agent produces a file, the file survives the turn, and it arrives on the Linear issue as a real attachment with a name that says what it is. One destination. No Drive, no email, no link nobody can open.

Depends on: `agent-file-inputs.md` for the attachment store this reuses, `task-dispatcher.md` section 5.6 for the write-back path this extends, and `agent-drive.md` for the destination this deliberately does not use yet.

**Author's note on verification.** Every claim about this repository was read out of the working tree while writing: `linear_writeback.py` in full, `attachment_retention.py`, `attachment_quota.py`, `attachment_binding.py`, `agent_attachments.py`'s five routes, `SESSION_TOKEN_SCOPES` at `services/agent_sessions.py:105`, `WRITEBACK_BODY_MAX_LENGTH` at `models/tasks.py:862`, `auth_framework/providers/header.py:78`, and the tool list at `examples/google_adk_plugin/my_agent/agent.py:340`. Claims about the Linear API were checked against Linear's developer documentation on 2026-08-13 and are marked verified or **unverified** individually; the two unverified ones are Phase 0 probes with a named branch per outcome. One premise of the original ask was wrong and section 1 says so.

---

## 0. What ships, in one paragraph

An executor tool writes a real `.xlsx`, `.docx` or `.pptx` with the library that owns that format, uploads it to its own session's attachment store under a name the agent had to choose, and marks it `draft` or `final`. A draft never leaves this system: it is delivered back into the next turn of the same step so an agent that ran out of room continues instead of starting again, and a step that reports incomplete coverage re-runs within the `max_turns` ceiling that already exists on the step and that nothing currently enforces. A final goes to Linear through the write-back, which gains three mutations: `fileUpload` for a signed URL, a `PUT` of the bytes, and `attachmentCreate` to hang the asset on the issue. The comment becomes a short summary and a pointer instead of four thousand truncated characters. Nothing new holds a credential, nothing new is allowed to mutate, no tool gains the ability to read, and the only process that talks to Linear is the one that already does.

---

## 1. One correction to the ask, before any design

**"Raise the write-back cap" was the wrong fix and it is now withdrawn.** `WRITEBACK_BODY_MAX_LENGTH` is 4000 (`models/tasks.py:862`) and a real investor shortlist truncated against it, which is how this started. Raising it treats the symptom: the content wanted to be a spreadsheet, and a spreadsheet pasted into a comment as a markdown table is a spreadsheet nobody can sort, filter or open in Excel. With a file as the destination the comment is a pointer, and 4000 characters is generous rather than tight. **The cap does not move in this plan.** If it ever moves it should be for a reason about comments, not because a table did not fit.

**A second thing the ask assumed and the code does not support.** "Attach it to Drive" is reasonable and it is already designed, in `agent-drive.md`. That plan's status line reads "design, nothing built", and its section 14 is unambiguous: without a Google Workspace OU with external sharing off and publish-to-web off, "this capability should not ship at all in its current form. Not a reduced version." Those are admin-console prerequisites in somebody else's org, not engineering tasks. Linear needs no new credential, no new identity and no new organizational decision, so Linear is the way in and Drive is section 9's later phase.

---

## 2. The shape: one new tool, one new destination, nothing new that mutates

Three processes are already in this path and their credentials stay disjoint.

The **executor** holds a model key and a session token. It gains one tool, which writes bytes and posts them to the control plane. It never talks to Linear, and it must not: the executor is the process running untrusted model output, and Linear's write credential is the one thing in this system that changes a customer-visible record.

The **control plane** holds the attachment store and the Linear write key. `linear_writeback.py` opens by stating that the only two mutations in the server, `commentCreate` and `issueUpdate`, live in that module and nowhere else. This plan adds a third and a fourth to the same module rather than a second place that can write.

The **agent's file** therefore travels executor → store → Linear, and at no point do the bytes and the Linear credential sit in the same process for a reason other than posting them.

Meeting point: the session's attachment store, which already holds inbound files with exactly the metadata the Linear upload needs.

---

## 3. The Linear API, verified

**Verified 2026-08-13** against Linear's developer documentation.

Upload is three steps, not one:

```graphql
mutation ($contentType: String!, $filename: String!, $size: Int!) {
  fileUpload(contentType: $contentType, filename: $filename, size: $size) {
    success
    uploadFile { uploadUrl assetUrl headers { key value } }
  }
}
```

then a `PUT` of the bytes to `uploadUrl` carrying `Content-Type`, `Cache-Control: public, max-age=31536000`, and every header the mutation returned. Then `assetUrl` is the durable reference.

Two properties decide the implementation.

**`headers` is an array of `{key, value}`, not a map.** Linear's own documentation calls this out because it is the step people get wrong. The client converts it once, in one place, with a test that a two-element array becomes a two-key mapping.

**The `PUT` must be server-side.** Linear's CSP blocks client-side uploads. The write-back already runs in the server, so this lands where it needs to, and it independently rules out the executor uploading to Linear directly.

Attaching is a separate mutation:

```graphql
mutation ($issueId: String!, $title: String!, $url: String!, $subtitle: String) {
  attachmentCreate(input: { issueId: $issueId, title: $title, url: $url, subtitle: $subtitle }) {
    success
    attachment { id }
  }
}
```

**`attachmentCreate` is idempotent on `(issueId, url)`.** Linear's documentation: re-creating an attachment with the same URL on the same issue "updates the original instead". This is the single most useful fact in this section. `linear_writeback.py` reads up to `_MARKER_COMMENT_PAGE` (100) comments per write to dedupe, and its own docstring accepts a duplicate comment as the residual failure. Attachments need none of that machinery: a retried write-back updates in place.

**Phase 0 is closed. Both probes ran live and both passed.**

**P1, by schema introspection.** `fileUpload(size: Int!, contentType: String!, filename: String!, metaData: JSON, makePublic: Boolean)` and `attachmentCreate(input: AttachmentCreateInput!)`, where `AttachmentCreateInput` carries `title: String!`, `url: String!`, `issueId: String!` and optional `subtitle`, `iconUrl`, `metadata: JSONObject`, `commentBody`. The mutations above are correct as written.

**`makePublic` is deliberately not set.** It would make the asset readable by anyone holding the URL rather than by the workspace. The default is what this feature wants and the argument stays absent rather than being passed as `false`, so nobody reads an explicit `false` as a thing worth toggling.

**`size` is enforced by storage, not by Linear.** The `headers` array came back with two real entries, `x-goog-content-length-range` and `Content-Disposition`. A wrong `size` therefore fails at the `PUT` with a GCS error rather than at the mutation, which is a confusing place to land. `LinearFileWriter.file_upload` sends `len(content)` and says so at the line that computes it; anyone who later lets a caller declare the size inherits a storage-layer failure with no obvious cause.

**The `PUT` carried no `Authorization` header and returned 200**, confirming that credentials must not reach the signed storage URL.

**P2, by a live round trip against OPS-2**, since cleaned up. `attachmentCreate` with the same `url` and a *changed* `title` returned the same attachment id and updated the title in place; the issue ended with one attachment, not two. `subtitle` updated too, so a retry can correct the agent name and step index rather than leaving them stale. Skipping comment-style dedupe is confirmed correct.

Not covered by either probe: whether `assetUrl` is stable over months. Section 10 still carries that as an assumption.

---

## 4. The decisions

### 4.1 Which credential the executor uses (design question 1)

`AGENT_ATTACHMENTS_WRITE` is `AccessLevel.AUTHENTICATED` (`auth_framework/providers/header.py:78`), so the executor's fleet API key could call the upload route today. **It must not.**

`SESSION_TOKEN_SCOPES` (`services/agent_sessions.py:105`) currently grants three operations and its docstring gives the reason this plan follows: *"a long-lived key handed to every agent process would make one agent's runaway loop spend every other agent's allowance."* That is exactly the failure mode here, with disk instead of model spend, and `attachment_quota.py` is a per-credential bucket, so a fleet key means one agent's loop exhausts the upload rate for every other agent sharing it.

**Decision, as revised after Phase 1 shipped: a *second* operation, `agent_attachments.write_self`, on its own route.** The first cut put `AGENT_ATTACHMENTS_WRITE` into `SESSION_TOKEN_SCOPES` and stopped there, and nothing worked: that operation is served by the default header authorizer, which never reads a Bearer token, so an agent got 401 without an API key and 403 with one. A route selects exactly one operation, and these two need different authorizers, a person with a cookie on one, a session-bound runtime token on the other - so they cannot be the same route. `agent_tracker.comment` had already established the shape and this copies it: new operation in `auth_framework/core.py`, into `RUNTIME_TOKEN_BOUND_OPERATIONS`, `AccessLevel.ADMIN` in the header provider's map so it fails closed, into `SESSION_TOKEN_SCOPES`, and `POST /agent-sessions/{session_key}/attachments/agent-output` sharing the handler body with the console's route.

The invariant this violated is now asserted: every member of `SESSION_TOKEN_SCOPES` is also in `RUNTIME_TOKEN_BOUND_OPERATIONS`.

**Correction: being on the session token does not by itself isolate the quota.** The paragraph above claimed "each session token is a different caller". It is not. `mint_session_runtime_token` stamps `actor_id` with the hash of whoever *created* the session, so every session one dispatcher opens presents the same `Principal.caller_id` and shares one bucket. The agent's route therefore meters on `Principal.target_id`, the session key the verifier already refused to let the caller choose, while `created_by_hash` keeps the creator. Knowledge search has the same shape and is *not* corrected here: its ceiling is per creator, and saying so is the point of writing this down.

### 4.2 Why upload alone is not enough (design question 2)

`attachment_retention.py`'s orphan sweep removes "an upload that never became part of a conversation", bytes and metadata together, `attachment_orphan_ttl_hours` after the row was last written. An agent that uploads a workbook and stops has created precisely that row. **Uploading without binding moves where the file dies rather than stopping it**, and it dies quietly, on somebody else's upload, hours later.

**Decision: the upload path records a binding in the same transaction**, reusing `attachment_binding.record_bindings`' notion of "carried by a turn". An agent-authored file is bound to the step that produced it at the moment it is stored, not later and not by the write-back, because the write-back may never run: a task can fail after the file is written, and the file is still the most valuable thing that turn produced.

A consequence worth stating: this makes the file survive a failed task. That is intended. A half-finished workbook attached to a ticket that then failed is evidence, and section 7 covers what the comment says about it.

### 4.3 A new origin, because provenance is the point (design question 3)

`StepAttachmentSummary` carries `origin` and `AttachmentOrigin.LINEAR` is the inbound case. Agent-authored files get **`AttachmentOrigin.AGENT`**, and it is not cosmetic. Three things read it:

- the delivery renderer, which must never hand an agent its own previous output as though a person attached it, since that is the laundering failure `company-knowledge.md` section 11 refuses in the corpus and the same argument applies one layer down;
- retention, per 4.4;
- the console, so a reviewer can see at a glance which files on a ticket came from a model.

### 4.4 Retention, which is a real question and not a default (design question 4)

Inbound files are copies: the original is in Linear and the blob sweep reclaiming it after `blob_ttl_days` loses nothing. **An agent-authored file is the only copy until the write-back pushes it to Linear**, and after that Linear holds it and our blob is again a copy.

**Decision: agent-authored blobs are exempt from the orphan sweep by virtue of 4.2's binding, and follow the ordinary blob TTL once `linear_asset_url` is set on the row.** Before that column is set the row is the only copy and the blob sweep must not touch it. This is one predicate, not a second retention system.

The honest limitation, stated here rather than discovered: retention runs only from the upload path (`attachment_retention.py` says so plainly, "there is no sweeper daemon in this codebase and this is not the place to invent one"), so a namespace that stops uploading stops reclaiming. That is already true and this plan does not make it worse.

### 4.5 Representative names, enforced rather than requested (design question 5)

The ask was that agents use representative names. A tool argument with a default gets the default.

**Decision: the tool's `filename` argument is required, has no default, and is validated against a refusal list** (`output`, `file`, `result`, `untitled`, `document`, `sheet1`, and bare extensions), returning `status=blocked` with a message naming the rule. The agent's instruction already says a blocked tool is explained and not retried, so the failure is legible rather than a loop.

The name threads through unchanged: the tool's `filename` becomes `declared_name` on the upload route, which normalises it server-side and never stores it verbatim, and that becomes `title` on `attachmentCreate`. One name chosen once by the party that knows what the file contains.

`subtitle` carries the agent name and step index, so a ticket with three attachments from a three-step chain reads as three deliverables rather than three mystery files.

### 4.6 What the tool can produce (design question 6)

`openpyxl` for `.xlsx`, `python-docx` for `.docx`, `python-pptx` for `.pptx`. Three libraries, each the one that owns its format.

**Correction: they are not free, and the `text-extraction` extra is the server's, not the executor's.** This section originally claimed all three arrive transitively through that extra. They do not, so the example declares them explicitly. Measured 2026-08-13: the example's site-packages goes 482MB to 520MB, so +38MB and +8 packages. Two of the eight are compiled rather than pure Python: `python-docx` and `python-pptx` pull `lxml` (19MB), and `python-pptx` pulls Pillow (13MB). Neither was already in the `google-adk` tree.

**Not a generic write-any-bytes tool.** A tool that writes arbitrary files is a tool that writes `.sh`, and the executor image is the one place in this system running untrusted model output. Three typed builders, each taking structured data rather than a byte string, means the model chooses content and the tool chooses encoding.

### 4.7 Drafts, so a step can resume rather than restart (design question 7)

An agent that runs out of turns mid-workbook currently loses the workbook. **Decision: a file is attached as a `draft` or as `final`, and the difference is what happens to it.**

A `final` file is the deliverable: pushed to Linear per section 3, kept per 4.4. A `draft` is working state: bound to the step, **never pushed to Linear**, and delivered back to the next turn of the same step so the agent continues from where it stopped.

The resume path needs no read tool, and that is the whole point of doing it this way. The agent does not fetch, list or open anything. The server delivers the draft's converted text into the next turn's envelope using `attachment_delivery`, exactly as it already delivers an inbound file. Section 6's refusal stands unamended: the executor still has no tool that reads.

**A draft is untrusted when it comes back.** `envelope.py` already treats a prior agent's report as DATA carrying the same warning as the issue body, because "A's output can carry B's injection". A draft is the same text one turn later and gets the same fence and the same warning. It is delivered under its own heading, never inside the human-attached files section, so 4.3's rule that an agent is never handed its own output as though a person attached it holds.

**Drafts are superseded, not accumulated.** One live draft per step: attaching a new one tombstones the previous. Otherwise a step with a five-turn ceiling leaves five near-identical workbooks bound to a ticket. When the step produces a `final`, its drafts are tombstoned in the same transaction.

### 4.8 Completing the work, by honouring a field that already exists (design question 8)

`AgentWorkflowStep.max_turns` exists, is validated `ge=1, le=MAX_TURNS_PER_STEP`, and its own description records that "the dispatcher runs one turn per step today". The field is a declared ceiling nothing enforces, which makes it the right home for iteration rather than a new concept.

**Decision: a step whose report declares incomplete work re-runs, within `max_turns`, carrying its draft forward.** The trigger is the `## Coverage` section from the goal scaffold: a step reporting `partial` or `not determined` against any part has not finished, and the next turn is handed its own draft plus the coverage lines that were not `done`.

Three bounds, because a loop that spends model budget needs all three:

- **`max_turns` is a ceiling and the default stays 1.** A workflow that has not asked for iteration does not get it.
- **Progress, or stop.** Two consecutive turns whose coverage improves on nothing end the step at `partial`. A model that reports the same three gaps forever is not converging, and paying for the fourth round is a decision nobody made.
- **The namespace hourly turn budget is unchanged and is the real ceiling.** It is a refusal on the server's turn path, so an iterating step spends the same pool as everything else and cannot widen it. A five-turn step is five turns of somebody's hour.

**The honest weakness, stated here rather than found later: the trigger is self-reported.** `## Coverage` is written by the agent being asked whether it finished. It can claim `done` to stop early or `partial` to keep going, and nothing in this plan verifies either. That is the same gap the goal scaffold's own PR names, and it is the argument for binding coverage to an evaluator. Until that exists, `max_turns` is a spend ceiling protecting against a dishonest `partial`, and there is no protection at all against a dishonest `done` beyond a human reading the ticket.

---

## 5. Wiring: the same commit ships every half

The lesson `company-knowledge.md` section 12 makes normative applies here, but the three-file rule it states is the *server's* rule and only server-read flags obey it. Every half-on state still logs one line naming itself at startup.

**A flag the executor reads belongs in `examples/google_adk_plugin/.env.example` and in the fleet's `PASSTHROUGH_ENV`, and nowhere else.** The shipped `AGENT_CONTROL_KNOWLEDGE_TOOLS` is wired exactly that way: it appears in neither `server/.env.example`, nor `docker-compose.yml`, nor `scripts/apple-container-up.sh`, because the server never reads it and the fleet is what puts it inside an executor container. Putting an executor flag on the server's own container makes a feature read as wired when it is not.

- `AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED`, default false, gating the two new mutations only. It is separate from `AGENT_CONTROL_LINEAR_WRITE_ENABLED` because posting a comment and uploading a file are different blast radii, and an operator who has accepted one has not thereby accepted the other.
- `AGENT_CONTROL_AGENT_FILE_OUTPUTS_ENABLED`, default false, gating the executor tool. Executor-read, so: the example's `.env.example` and `fleet/src/agent_control_fleet/settings.py`.
- Half-on line, when the tool is on and the Linear half is off: `agent file outputs enabled but Linear attachment write is off; files will be stored and never reach the ticket`.

**Do not extend `scripts/check_knowledge_env_parity.py`.** Its second direction, every variable read anywhere under the new code appearing in the wiring files, is structurally unimplementable for pydantic-settings variables: the name is never a string literal in the source, it is derived from a field name and a prefix at class-construction time, so there is nothing for a static scan to find. The check that does hold is per-flag and lives beside the flag: one test reading the wiring files as text and asserting each line is present, which is what both flags here now carry.

---

## 6. What this refuses to do

**No generic file-write tool**, per 4.6.

**No Drive**, per 1, until `agent-drive.md`'s prerequisites are somebody's completed admin task rather than a plan's assumption.

**No email, no send, no recipient of any kind.** The word "recipient" does not appear in this design.

**No agent-authored file re-enters the corpus.** `company-knowledge.md` section 11 settled this: agent output reaches company knowledge only through a reviewed folder moved by a human hand. A Linear attachment is not in the corpus and this plan adds no path that puts it there.

**No reading of its own outputs.** The tool writes and uploads. It does not list, fetch or modify. `agent-drive.md` section 14 holds the equivalent read tools until a phase that may never come, for the reason that a read tool converts a capability with no injection surface into one with the same surface as a fetched web page. Same argument, same answer.

This survives 4.7's resume path intact, and the distinction is worth being precise about because it is the one a reviewer should test. A draft returns to the agent because **the server put it in the turn message**, on the same path that delivers a file a person attached. The agent never named it, never asked for it and cannot ask for a different one. A read tool would let the model choose what to open, which is the surface being refused; delivery does not, and the executor's tool list is unchanged by 4.7.

---

## 7. Edge cases, each with its decided behaviour

| Case | Behaviour |
|---|---|
| Agent writes a file, task then fails | The draft is bound and kept, and is not pushed to Linear. A reclaimed task resumes from it (4.7). The comment names the failure and says a draft exists. |
| Step exhausts `max_turns` still `partial` | Its last draft is promoted to `final` and attached, with `subtitle` recording that it is incomplete. Losing four turns of work to a ceiling is the worse outcome. |
| Coverage says `done` but the workbook is empty | Not detected. 4.8 states this plainly; the trigger is self-reported and nothing here verifies it. |
| Two turns of a step both attach a draft | The second tombstones the first. One live draft per step. |
| Write-back retried after a partial success | `attachmentCreate` is idempotent on `(issueId, url)`; the second call updates. No dedupe read needed. |
| `fileUpload` succeeds, `PUT` fails | No `assetUrl` recorded, blob stays (4.4), write-back posts the comment with one line saying the file was produced and could not be delivered. Never silent. |
| `PUT` succeeds, `attachmentCreate` fails | `linear_asset_url` is set, so the blob may be reclaimed later while the ticket has no attachment. Retry on the next write-back attempt; the asset URL is stable and the mutation idempotent. |
| Agent produces a 200MB workbook | Refused by the existing per-file ceiling before any Linear call. The refusal names the limit. |
| Agent produces twelve files in one turn | The existing per-turn attachment ceiling applies unchanged. |
| Two chain steps produce the same filename | Different attachment keys, different `assetUrl`s, two Linear attachments. `subtitle` carries the step index, which is what distinguishes them. |
| Linear attachment write disabled | Files are stored and bound; comment says so via the half-on line. Nothing is lost, nothing is claimed. |

---

## 8. Testing

The tests that would have caught the failures this plan exists to fix:

- an agent-authored attachment is **not** removed by `sweep_orphaned_attachments`, and an unbound one still is;
- the blob sweep leaves a row whose `linear_asset_url` is null, and reclaims it once set;
- a session token carrying `AGENT_ATTACHMENTS_WRITE` cannot upload to a different session's key;
- the header array from `fileUpload` becomes a mapping with every key preserved;
- `filename="output.xlsx"` is refused, and the refusal names the rule;
- the delivery renderer never renders an `AttachmentOrigin.AGENT` file as though a person attached it;
- a write-back run twice produces one Linear attachment, not two;
- a `draft` is never sent to Linear, asserted on the write-back path rather than by inspecting the caller;
- a draft delivered into turn two arrives under its own heading, fenced and carrying the untrusted warning, and **not** inside the human-attached files section;
- attaching a second draft to a step tombstones the first, and a `final` tombstones every draft of that step;
- a step whose coverage is all `done` runs once even when `max_turns` is five;
- two consecutive turns with no coverage improvement end the step rather than spending the ceiling;
- the executor's tool list is unchanged by the resume path, asserted by name, because that is what keeps section 6 true;
- the parity check fails when a new variable reaches only two of the three files.

---

## 9. Phases and effort

**Base and precedent.** This work is built on `feat/agent-fleet-topology` (merged to `main` as PR #14), not on `main` as it stood when the plan was written. That branch carries session-bound runtime tokens, the executor Dockerfile, the `fleet/` package and `save_to_tracker`, all four of which this plan needs and none of which the three phase branches saw. `save_to_tracker` / `agent_tracker.comment` is the worked example for every machine-side route here: `endpoints/agent_tracker.py` and its tests are what 4.1's operation split copies.

**ADK tool docstrings are legitimately long.** The three tools in `examples/google_adk_plugin/my_agent/file_outputs.py` run 19-21 lines each and must stay that way: an ADK tool docstring *is* the tool's description to the model, so the repository's one-to-three-line rule does not reach them. `sdks/python/src/agent_control/integrations/google_adk/knowledge_tools.py` is the shipped precedent. Every internal helper in that module is one line.


**Phase 0, probes, 2 days.** P1: `fileUpload` argument types and whether `size` is enforced, against the live schema. P2: whether `attachmentCreate` idempotency holds across a changed `title` with a stable `url`.

**Phase 1, the store half, 1 week.** Scope on the session token, the `AGENT` origin, binding at upload, the retention predicate, the tests in section 8 that need no Linear.

**Phase 2, the Linear half, 1 week.** Two mutations on `LinearWritebackClient`, the header transform, the `PUT`, the comment that renders a pointer. Behind its own flag, default off.

**Phase 3, the executor tool, 1 week.** Three typed builders, the required-name validation, the upload call, the wiring in both runtimes. Drafts are storable from here; nothing reads them back yet.

**Phase 4, resume and iterate, 1 week.** Draft delivery into the next turn, the supersede rule, and honouring `max_turns` with the progress check. Depends on Phase 3 and on PR #12's goal scaffold being merged, because the coverage section is the trigger and without it there is nothing to iterate against.

**Phase 5, Drive, unscheduled and blocked.** Only if `agent-drive.md`'s section 4.1 prerequisites are completed by whoever owns the Workspace. Not an engineering decision.

Roughly three weeks plus probes, of which about four days is configuration and parity plumbing and the rest is real work.

---

## 10. Riskiest assumptions

**That the executor image can gain three libraries without a size problem.** `server/Dockerfile`'s comments record the OCR extra taking an image to 19.3GB, so this project has been bitten. `openpyxl`, `python-docx` and `python-pptx` are pure Python and small, but the number goes in the PR rather than being assumed.

**That `assetUrl` is durable.** The plan treats it as a stable identifier for idempotency. If Linear rotates it, the idempotency argument in 3 and the retention predicate in 4.4 both weaken. P2 checks stability across an update; nothing checks it across months.

**That a model asked for structured data will produce structured data.** The three builders take rows and cells, not prose. An agent that returns a paragraph where a list belongs produces an empty workbook. `## Coverage` from the goal scaffold is the existing signal for this class of failure, and PR #12 has not been tested against real traffic yet.

---

## 11. Open questions a reviewer should push on

1. Should the file also be attached to the **session** in the console, or only to the Linear issue? The store makes the first free; nobody has asked for it.
2. ~~Is `subtitle` the right home for agent name and step index, or should that be `metadata`, which is queryable?~~ **Settled by P1:** `AttachmentCreateInput.metadata` exists and is a `JSONObject`, so both are available. `subtitle` wins because it is the field Linear *renders*. The point of the string is that a person looking at the ticket can see which agent and which step produced the file, and a queryable field nobody has asked to query is not worth a second write path. P2 confirmed `subtitle` updates in place on a retry, so a corrected name replaces a stale one.
3. ~~Should a failed task's partial deliverable be attached?~~ **Settled 2026-08-13 by the operator:** incomplete work is re-run to completion rather than shipped, and the partial is kept as a draft so the next turn resumes from it. 4.7 and 4.8 carry the design. The ticket never receives a half-finished spreadsheet unless the ceiling is exhausted, which 7 handles explicitly.
4. Does anything need to stop an agent attaching a file to a **completed** task's issue? The write-back path already has a review gate; the upload path does not.
5. **What ends a step that reports `partial` honestly and forever?** 4.8's progress check is a heuristic: no improvement across two turns. A model that adds one trivial line per turn defeats it and spends the ceiling. A stricter rule needs a definition of improvement that is not the model's own prose, which probably means the evaluator binding.
6. Should a draft be visible in the console at all? It is bound to the step, so it will appear unless something hides it. Showing an operator four superseded workbooks is noise; hiding them makes "what was the agent doing" unanswerable when a step burns its ceiling.
7. `MAX_TURNS_PER_STEP` is the schema's ceiling on the ceiling. Nobody has revisited it since it bounded a field nothing enforced, and 4.8 makes it load-bearing.
