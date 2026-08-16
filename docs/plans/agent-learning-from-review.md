# Learning from review: the reasons a human already gives, made durable

Status: design. Nothing built.

Scope: an agent that is corrected once stops making the same mistake, without anybody editing a prompt by hand and without a model deciding what it should believe.

Depends on: `agent-system-prompts.md` for the managed prompt block this writes into, `task-dispatcher.md` section 5.6 for the accept/reject path this reads from, and `memory-controls.md` for the rule this obeys rather than re-litigates.

**Author's note on verification.** Every claim about this repository was read out of the working tree while writing: `RejectAgentTaskRequest` at `models/tasks.py:827`, `rejected_reason` at `:738`, `DecisionDigest` at `:678`, `MANAGED_PROMPT_OPEN_TAG` and the version-source enum at `models/agent_configs.py:29,45,57`, `expected_version` at `:245`, and the writeback status counts read from the live database (49 sent, 17 awaiting approval, 2 pending, zero rejected). Claims about how a model responds to accumulated guidance are **unverified** and Phase 0 measures them rather than assuming.

---

## 0. What ships, in one paragraph

The reasons a reviewer already types when rejecting a proposal are collected, shown to a human as a suggested amendment, and on that human's press become a new version of the agent's managed prompt block. Nothing is written automatically, nothing is inferred by a model without review, and the store is the versioned config table this repo already has. An agent corrected in week one carries the correction in week four, and every line of it is attributable to a person who agreed to it.

---

## 1. Two corrections to the ask, before any design

**"Learn from our comments" cannot mean Linear comments, yet.** A Linear comment is untrusted text written by anyone with tracker access, and `envelope.py` already treats the issue body that way for a reason. Promoting arbitrary comment text into a system prompt is a direct path from "somebody commented on a ticket" to "every agent in the namespace now believes this". The **rejection reason** is different: it is typed by a reviewer inside the console, on the accept/reject gate, by someone the auth layer already identified. That is the signal this plan uses. Reading general tracker comments is section 12's rejected item, with the same reasoning.

**The signal already exists and nothing reads it.** `RejectAgentTaskRequest.reason` is captured, validated and stored as `rejected_reason` on the writeback row today. So this is not instrumentation work; it is a loop that was left open. Worth stating because it changes the estimate: the expensive half is the review UI and the guardrails, not the capture.

---

## 2. The shape: one managed block, versioned, human-gated

`agent_configs` already carries a **managed region** inside an agent's system prompt, delimited by `MANAGED_PROMPT_OPEN_TAG` (`agent_control_system_prompt`), with version rows that record where a body came from and an `expected_version` for optimistic concurrency. That is the destination, and it is the whole reason this plan needs no new store.

Three properties fall out of reusing it rather than inventing something:

Learned guidance is **separable from human-authored prompt text**, because the managed block has its own delimiters. An operator can read what the system added versus what they wrote.

It is **versioned and restorable** — the config endpoints already list versions and restore one. A lesson that makes an agent worse is one press to undo, which is the property that makes accepting a suggestion cheap.

It is **attributable**. The version row records its source, so "why does this agent believe this" has an answer that is not archaeology.

`memory-controls.md` rejects owning a memory store "for the fourth time in this project". This plan does not add one and does not reopen that.

---

## 3. The decisions

### 3.1 What counts as a lesson (design question 1)

A rejection reason, and nothing else. Not an accept — an accepted proposal teaches nothing beyond "this was fine", and mining silence for approval is how a system concludes it is doing well.

**Decision: one lesson is one rejection, carrying the reason text, the agent that produced the output, the step brief, and the digest of what was rejected.** The digest matters: it binds the lesson to a specific artefact, so a reviewer reading the suggestion later can see exactly what was wrong rather than a sentence about it.

### 3.2 Nothing is promoted without a press (design question 2)

**Decision: a lesson never reaches a prompt without a human pressing accept on the amendment itself.** Two gates, not one: the reviewer rejected the work, and a second decision promotes the reason into standing guidance. They are different decisions and conflating them is how a one-off correction becomes policy.

The argument for automation is that operators will not do the second press. The argument against is the failure mode: an agent whose prompt grows a rule from every bad day, none of which anybody agreed should be permanent. The dispatcher's whole design is that a human decision is what makes a machine output real, and this is that same rule one layer up.

### 3.3 A model drafts the amendment, a human owns it (design question 3)

Raw rejection reasons are not prompt text. "Wrong, we don't sell to SMBs" is a correction; the prompt line is "This company sells to MSSPs and enterprises, not SMBs."

**Decision: a model turns collected reasons into a proposed amendment, and that proposal is a suggestion until pressed.** The model never writes the prompt; it writes a draft of a diff that a person accepts, edits or discards. The draft is generated in the control plane against the same allowlisted model the rest of the deployment uses.

**The drafting model sees only reviewer-authored text**, never agent output, and section 5 says why.

### 3.4 Which agent learns (design question 4)

A chain has three steps and two agents. A rejection lands on the task, not on a step.

**Decision: the lesson attaches to the agent that produced the rejected output, which is the last step's agent, and only that one.** A researcher whose plan was fine should not inherit a lesson about a writer's prose. Where the reviewer means the plan was wrong, they reject and say so, and the console offers the step list so they can attribute it to the step they mean. Defaulting to the last agent and letting the reviewer move it is better than asking every time.

### 3.5 The block is bounded, and old lessons expire (design question 5)

A prompt that grows a line per rejection is a prompt that eventually costs more than the task.

**Decision: the managed block has a hard character ceiling and a stated eviction rule.** When a new lesson would exceed it, the amendment view shows what would be dropped and the reviewer decides. Nothing evicts silently. The ceiling is a number in config, and its first value should be small enough to force the conversation early — a block that has never hit its ceiling has never been curated.

**Lessons carry the date they were accepted.** A rule accepted eight months ago about a product that has since changed is worse than no rule, and a dated line is one a reviewer can judge.

---

## 4. What this refuses to do

**No learning from agent output.** `company-knowledge.md` section 11 refuses to let agent reports re-enter the corpus, because a speculation becomes an organizational fact with nobody agreeing. Promoting an agent's own text into its own prompt is that failure with a shorter loop.

**No learning from Linear comments**, per section 1.

**No automatic promotion**, per 3.2, including "after N similar rejections". A threshold is a policy decision wearing arithmetic.

**No cross-namespace learning.** A lesson is scoped to the agent and namespace it came from. There is no fleet-wide "agents have learned" surface, because the authorization model has three access levels and cannot express who owns a lesson.

**No deletion of history.** Discarding a suggested amendment records that it was discarded. A prompt that quietly forgot a rejected suggestion cannot be audited.

---

## 5. Edge cases, each with its decided behaviour

| Case | Behaviour |
|---|---|
| Rejection reason contains prompt injection | The drafting model receives it as fenced untrusted data under the same warning `envelope.py` uses; the human reads the draft before it lands. Two barriers, neither sufficient alone. |
| Reviewer rejects with an empty reason | No lesson. A rejection with no reason is a rejection, not a correction, and inventing the reason is the failure this plan exists to avoid. |
| Two lessons contradict each other | Both are shown in the amendment view, adjacent. The system does not resolve it; a person does, because "we changed our mind" and "one of these is wrong" look identical to a model. |
| Lesson is about the task, not the agent | Reviewer moves it to the step, or discards. 3.4's default is a starting point, not a claim. |
| The same mistake is rejected five times | Five lessons, one amendment view, one press. Deduplication is by exact reason text only; "the same correction rephrased" needs similarity scoring that `memory-controls.md` already rejected for the same reason. |
| Accepted lesson makes the agent worse | Restore the previous config version. This is the property 2 pays for, and the amendment view links to it. |
| Rejection on a dry-run task | No lesson. Nothing left the process, so the output was never real work and the reviewer is judging a rehearsal. |
| Managed block at its ceiling | The amendment shows what would be evicted; no silent drop. |
| Two reviewers press at once | `expected_version` already refuses the second with a 409 carrying the real version. Reused, not reimplemented. |
| Agent has no config row yet | Created on first accepted lesson, with the managed block as its only content. |

---

## 6. Testing

The tests that would catch the failures this plan is most likely to have:

- a rejection with no reason produces no lesson;
- a lesson never reaches a config version without an explicit promote call;
- the drafting model's input contains no agent-authored text, asserted on the assembled payload rather than on intent;
- a promoted amendment lands inside the managed delimiters and leaves human-authored prompt text byte-identical;
- a second concurrent promote gets a 409 rather than silently overwriting;
- an amendment that would exceed the ceiling refuses and names what it would evict;
- a dry-run rejection produces no lesson;
- restoring the previous version removes the lesson from the assembled prompt.

---

## 7. Phases and effort

**Phase 0, measurement, 3 days.** Does an accumulated block of guidance actually change output? Take the rejection reasons this deployment produces, hand-write the block they imply, and run the same tasks with and without it. **If the difference is not visible, this plan is not worth building** and that is a real outcome. Nothing else here is worth doing before this answers.

**Phase 1, the lesson record, 1 week.** Capture on reject, a table keyed to agent and writeback, the API to list them. No prompt writing. Useful alone: it makes "what have reviewers been telling this agent" answerable.

**Phase 2, the amendment view, 1.5 weeks.** The console surface: lessons for an agent, the drafted amendment, accept/edit/discard, the eviction preview, the link to version restore.

**Phase 3, drafting, 1 week.** The model call that turns reasons into a proposed block, with the untrusted framing and the payload assertion from section 6.

**Phase 4, the loop closed, 3 days.** Promote writes a config version; the executor already reads the assembled prompt, so nothing changes on that side.

Roughly four weeks, of which Phase 0 may end it.

---

## 8. The riskiest assumptions

**That accumulated guidance improves output at all.** Unverified, and Phase 0 exists to settle it. A long prompt of accumulated rules can make a model worse — more constrained, more hedged, more likely to recite the rules instead of doing the work. Nothing in this repository measures that today.

**That reviewers will type useful reasons.** The live table shows 49 sent and zero rejected, so **the rejection path has never been exercised on this deployment**. A feature whose input nobody has produced yet is a feature designed against an imagined signal. Phase 1 being useful alone is the hedge.

**That the last step's agent is the right owner.** 3.4 defaults there and lets a reviewer move it, which is a guess about attribution wearing a default.

---

## 9. Open questions a reviewer should push on

1. Should an **accepted** proposal with an edit teach anything? The reviewer changed the text before accepting, and the diff is a correction nobody typed a reason for. It is the richest untapped signal here and it is not in this plan.
2. Is the managed block the right home, or should lessons be a separate retrieval the agent queries? A block is always in context and always costs tokens; a retrieval costs a call and can be missed.
3. Does a lesson belong to an agent or to a team? Two agents on one team making the same mistake is common, and 3.4 makes each learn separately.
4. What happens to lessons when an agent is renamed or retired?
