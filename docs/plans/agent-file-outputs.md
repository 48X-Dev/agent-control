# Agent file outputs: a deliverable the agent writes, and a human opens from the ticket

Status: design. Nothing built.

Branch context: `feat/dispatcher-goal-scaffold` and the five PRs open behind it.

Scope: an agent produces a file, the file survives the turn, and it arrives on the Linear issue as a real attachment with a name that says what it is. One destination. No Drive, no email, no link nobody can open.

Depends on: `agent-file-inputs.md` for the attachment store this reuses, `task-dispatcher.md` section 5.6 for the write-back path this extends, and `agent-drive.md` for the destination this deliberately does not use yet.

**Author's note on verification.** Every claim about this repository was read out of the working tree while writing: `linear_writeback.py` in full, `attachment_retention.py`, `attachment_quota.py`, `attachment_binding.py`, `agent_attachments.py`'s five routes, `SESSION_TOKEN_SCOPES` at `services/agent_sessions.py:105`, `WRITEBACK_BODY_MAX_LENGTH` at `models/tasks.py:862`, `auth_framework/providers/header.py:78`, and the tool list at `examples/google_adk_plugin/my_agent/agent.py:340`. Claims about the Linear API were checked against Linear's developer documentation on 2026-08-13 and are marked verified or **unverified** individually; the two unverified ones are Phase 0 probes with a named branch per outcome. One premise of the original ask was wrong and section 1 says so.

---

## 0. What ships, in one paragraph

An executor tool writes a real `.xlsx`, `.docx` or `.pptx` with the library that owns that format, uploads it to its own session's attachment store under a name the agent had to choose, and marks it as the step's output. The write-back then does what it already does for text, plus three Linear mutations: `fileUpload` for a signed URL, a `PUT` of the bytes, and `attachmentCreate` to hang the asset on the issue. The comment becomes a short summary and a pointer instead of four thousand truncated characters. Nothing new holds a credential, nothing new is allowed to mutate, and the only process that talks to Linear is the one that already does.

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

**Unverified, and both are Phase 0 probes.** The exact GraphQL argument types and nullability of `fileUpload` are not stated in the prose documentation, only the client-library call shape; the query is written against the live schema, not from this document. And whether `size` must match the uploaded byte count exactly, or is advisory, is unstated. **P1** posts a 12-byte file with a correct size and with a deliberately wrong one; if a wrong size is accepted the client still sends the true length, and if it is rejected the error is surfaced rather than retried.

---

## 4. The decisions

### 4.1 Which credential the executor uses (design question 1)

`AGENT_ATTACHMENTS_WRITE` is `AccessLevel.AUTHENTICATED` (`auth_framework/providers/header.py:78`), so the executor's fleet API key could call the upload route today. **It must not.**

`SESSION_TOKEN_SCOPES` (`services/agent_sessions.py:105`) currently grants three operations and its docstring gives the reason this plan follows: *"a long-lived key handed to every agent process would make one agent's runaway loop spend every other agent's allowance."* That is exactly the failure mode here, with disk instead of model spend, and `attachment_quota.py` is a per-credential bucket, so a fleet key means one agent's loop exhausts the upload rate for every other agent sharing it.

**Decision: `AGENT_ATTACHMENTS_WRITE` joins `SESSION_TOKEN_SCOPES`.** The token is already session-bound and the route is `/{session_key}/attachments`, so the existing verifier, which compares the token's target against the request's path, confines an agent to attaching files to its own session with no new mechanism. The quota keys on the caller, and each session token is a different caller, so one runaway agent spends its own allowance.

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

`openpyxl` for `.xlsx`, `python-docx` for `.docx`, `python-pptx` for `.pptx`. Three libraries, each the one that owns its format, all pure-Python and all already transitively present in the executor image through the `text-extraction` extra.

**Not a generic write-any-bytes tool.** A tool that writes arbitrary files is a tool that writes `.sh`, and the executor image is the one place in this system running untrusted model output. Three typed builders, each taking structured data rather than a byte string, means the model chooses content and the tool chooses encoding.

---

## 5. Wiring: the same commit ships every half

The lesson `company-knowledge.md` section 12 makes normative applies here unchanged. Every flag ships its compose passthrough, its Apple-script line and its `server/.env.example` entry in the same commit, and every half-on state logs one line naming itself at startup.

- `AGENT_CONTROL_LINEAR_ATTACHMENTS_WRITE_ENABLED`, default false, gating the two new mutations only. It is separate from `AGENT_CONTROL_LINEAR_WRITE_ENABLED` because posting a comment and uploading a file are different blast radii, and an operator who has accepted one has not thereby accepted the other.
- `AGENT_CONTROL_AGENT_FILE_OUTPUTS_ENABLED`, default false, gating the executor tool.
- Half-on line, when the tool is on and the Linear half is off: `agent file outputs enabled but Linear attachment write is off; files will be stored and never reach the ticket`.

`scripts/check_knowledge_env_parity.py` is the model for the parity check and its second direction is the one that matters: every variable read anywhere under the new code appears in all three files. Extend it rather than write a second one.

---

## 6. What this refuses to do

**No generic file-write tool**, per 4.6.

**No Drive**, per 1, until `agent-drive.md`'s prerequisites are somebody's completed admin task rather than a plan's assumption.

**No email, no send, no recipient of any kind.** The word "recipient" does not appear in this design.

**No agent-authored file re-enters the corpus.** `company-knowledge.md` section 11 settled this: agent output reaches company knowledge only through a reviewed folder moved by a human hand. A Linear attachment is not in the corpus and this plan adds no path that puts it there.

**No reading of its own outputs.** The tool writes and uploads. It does not list, fetch or modify. `agent-drive.md` section 14 holds the equivalent read tools until a phase that may never come, for the reason that a read tool converts a capability with no injection surface into one with the same surface as a fetched web page. Same argument, same answer.

---

## 7. Edge cases, each with its decided behaviour

| Case | Behaviour |
|---|---|
| Agent writes a file, task then fails | File is bound and kept. Write-back posts the failure and the attachment, because a partial deliverable is evidence. |
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
- the parity check fails when a new variable reaches only two of the three files.

---

## 9. Phases and effort

**Phase 0, probes, 2 days.** P1: `fileUpload` argument types and whether `size` is enforced, against the live schema. P2: whether `attachmentCreate` idempotency holds across a changed `title` with a stable `url`.

**Phase 1, the store half, 1 week.** Scope on the session token, the `AGENT` origin, binding at upload, the retention predicate, the tests in section 8 that need no Linear.

**Phase 2, the Linear half, 1 week.** Two mutations on `LinearWritebackClient`, the header transform, the `PUT`, the comment that renders a pointer. Behind its own flag, default off.

**Phase 3, the executor tool, 1 week.** Three typed builders, the required-name validation, the upload call, the wiring in both runtimes.

**Phase 4, Drive, unscheduled and blocked.** Only if `agent-drive.md`'s section 4.1 prerequisites are completed by whoever owns the Workspace. Not an engineering decision.

Roughly three weeks plus probes, of which about four days is configuration and parity plumbing and the rest is real work.

---

## 10. Riskiest assumptions

**That the executor image can gain three libraries without a size problem.** `server/Dockerfile`'s comments record the OCR extra taking an image to 19.3GB, so this project has been bitten. `openpyxl`, `python-docx` and `python-pptx` are pure Python and small, but the number goes in the PR rather than being assumed.

**That `assetUrl` is durable.** The plan treats it as a stable identifier for idempotency. If Linear rotates it, the idempotency argument in 3 and the retention predicate in 4.4 both weaken. P2 checks stability across an update; nothing checks it across months.

**That a model asked for structured data will produce structured data.** The three builders take rows and cells, not prose. An agent that returns a paragraph where a list belongs produces an empty workbook. `## Coverage` from the goal scaffold is the existing signal for this class of failure, and PR #12 has not been tested against real traffic yet.

---

## 11. Open questions a reviewer should push on

1. Should the file also be attached to the **session** in the console, or only to the Linear issue? The store makes the first free; nobody has asked for it.
2. Is `subtitle` the right home for agent name and step index, or should that be `metadata`, which is queryable?
3. Should a failed task's partial deliverable really be attached (7, row 1)? The argument is that evidence beats silence. The counter-argument is that a ticket acquires a half-finished spreadsheet nobody asked for, and a reviewer may reasonably prefer the failure alone.
4. Does anything need to stop an agent attaching a file to a **completed** task's issue? The write-back path already has a review gate; the upload path does not.
