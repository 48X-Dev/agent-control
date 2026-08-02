# Shared Agent Memory Controls: Implementation Plan

**Status:** design. Nothing built.
**Branch context:** `feat/agent-teams`.
**Scope:** a policy decision point between "a model proposed this fact" and "this is now what everyone's agents believe", on both the write path and the recall path, for a memory store Agent Control does not own and does not add.
**Dependencies:** none on the orchestration plan, the executor, teams, or a model key. Phase 5's UI reuses the agent detail page and the existing event query API. Phase 4 needs `google-adk` installed, which is the only external dependency in the document.
**Verification note:** every claim about this repo was checked against the working tree while writing this, and every line reference below was read rather than recalled. Claims about Google ADK's `BaseMemoryService` were **not** verified: `import google` fails in this environment, there is no ADK on the path, and this document does not plan against a signature read from memory. Those are flagged A1 through A5 and Phase 0 exists to settle them.

**Revision note.** This is the second revision. A security review of the first found three blockers, five majors and two minors. All ten were reproduced against the code and all ten are resolved here. Section 17 lists each one, what the code actually said, and where the fix lives, because the three blockers were not judgement calls: two of them made the design's headline guarantee false, and the third defended a field the model never reads while leaving the field it does read unguarded.

---

## 0. What ships

A fact cannot enter shared agent memory without a control seeing it, and a fact cannot leave shared agent memory into a system prompt without a control seeing it either. Both paths run through machinery this repo already has: a `Step`, a `ControlScope`, an evaluator, an action. No new engine, no new evaluator in the shipping path, no memory store.

The write gate stops the obvious attack. The recall gate is the one that matters more, and section 3.2 argues it at length, because a write-only gate is a gate on facts written after you installed it by systems you happen to control, which is not the population of facts your agent believes.

Phase 1 through 4 ship the gate. Phase 5 ships the review queue. Phase 6 ships the templates and a fixture bench. Phases 1 and 2 are useful alone: they make memory decisions visible in the event stream, which today they are not, because a memory step is currently recorded as an LLM call (section 2.2).

**Section 4 says exactly what is still unguarded after Phase 1 ships, and it is most of it.** That section exists because a partial rollout of a security feature is the state a deployment sits in longest.

---

## 1. Naming

**The entity is a fact, the operation is a memory write or a memory recall, and the stored artifact is a held fact.**

`memory` is nearly free as an identifier. `grep -rniE "\bmemory\b"` across `models/src`, `server/src`, `sdks/python/src` and `ui/src` returns docstring prose, `InMemoryEventStore`-style class names under `server/src/agent_control_server/observability/store/`, and nothing that is an entity, a table, an operation id, a route or a wire field. `InMemory*` is a storage-backend adjective and does not collide with a domain noun.

`fact` is entirely free. Zero matches as an identifier anywhere in those four trees.

Names fixed now, before anything is built, because the orchestration plan's own rule applies: wire-level identifiers are public contract and renaming them later is expensive.

- Step type: `"memory"`. Step names: `"memory.write"` and `"memory.recall"`.
- `applies_to` values: `"memory_write"` and `"memory_recall"`.
- Table: `memory_quarantine`. There is no `memory_facts` table and section 3.5 is why.
- Operations: `memory_quarantine.submit`, `memory_quarantine.read`, `memory_quarantine.write`, `memory_admissions.read`.
- Models module: `models/src/agent_control_models/memory.py`.
- SDK package: `sdks/python/src/agent_control/memory/`.
- ADK decorator: `sdks/python/src/agent_control/integrations/google_adk/memory_service.py`.
- Routes under `/api/v1/memory/`.
- UI tab route `?tab=memory`.

**User-facing words do not generalise.** The tab is "Memory". The queue is "Held for review", not "quarantine", because an operator reading a queue label should not have to know our schema. Only identifiers move.

**`scope` is a collision worth naming.** `ControlScope` (`models/src/agent_control_models/controls.py:70`) already owns the word inside the control model. A memory scope is a different thing: the readership boundary of a fact. So the payload field is `memory.scope`, always qualified, never bare `scope`, and the wire model is `MemoryScopeRef` rather than `MemoryScope`.

---

## 2. Nine things to understand before anyone builds

### 2.1 `step_types` is an open string set, and the engine agrees

Confirmed, both halves.

`ControlScope.step_types` is `list[str] | None` (`models/src/agent_control_models/controls.py:73-80`), documented as "Built-in types are 'tool' and 'llm'", and its validator (`:94-107`) rejects only an empty list and non-string members. It does not check membership against `BUILTIN_STEP_TYPES` (`models/src/agent_control_models/agent.py:16`).

`Step.type` is `str` with `min_length=1` (`agent.py:145-149`), and `validate_builtin_types` (`agent.py:170-175`) constrains only the `tool` case, requiring object input. A `memory` step carrying a string input validates today, unchanged.

The engine matcher is a plain membership test:

```python
if scope.step_types and step_type not in scope.step_types:
    continue
```

`engine/src/agent_control_engine/core.py:582-583`, inside `get_applicable_controls`. No enum, no normalization, no lookup table.

`StepSchema.type` is likewise `str` with a validator that rejects only the empty string (`agent.py:88-92`, `:134-139`), so a memory step registers through the existing `_ensure_step_known` path with no schema change.

So `scope: {step_types: ["memory"], step_names: ["memory.write"], stages: ["pre"]}` is a control an operator can author the day this lands, with **no migration, no enum widening and no schema change**. That is the whole reason this design does not invent a parallel mechanism. It also means the first thing to build is not code, it is the payload contract in section 3.1, because the mechanism is already there and only the data a control sees is missing.

`ControlSelector.validate_path` restricts roots to `{input, output, name, type, context, *}` (`controls.py:46`). Everything a memory control reads must live under `input`, `output` or `context`. That constraint, plus the absence of list-index syntax in `select_data` (`engine/src/agent_control_engine/selectors.py`, which walks dotted parts by dict access then `getattr` and returns `None` on anything else), dictates the payload shape entirely.

### 2.2 The event record is closed, and today it silently mislabels memory

`ControlExecutionEvent.applies_to` is `Literal["llm_call", "tool_call"]` (`models/src/agent_control_models/observability.py:93-95`). `EventQueryRequest.applies_to` carries the same Literal (`:313-315`).

Events are stored whole in a JSONB `data` column (`server/src/agent_control_server/models.py:804-806`), and every query reads through `data->>'applies_to'` (`observability/store/postgres.py:463-465`). So this is a model widening and a filter widening. **No migration.** The `ix_events_data_control_id` expression index (`models.py:816`) is on `control_id` and is unaffected.

The part that is not merely cosmetic:

```python
def map_applies_to(step_type: str) -> Literal["llm_call", "tool_call"]:
    """Map Agent Control step types to observability applies_to values."""
    return "tool_call" if step_type == "tool" else "llm_call"
```

`sdks/python/src/agent_control/evaluation_events.py:65-67`. The fallback is `llm_call`, and `_build_events_for_matches` calls it as `map_applies_to(request.step.type)` (`:106`) for every event it builds. Ship a memory step without widening this and every memory decision is recorded as an LLM call: it lands in LLM totals, it is invisible to an `applies_to=["llm_call"]` filter written to mean "model calls", and an operator investigating a spike in denials on the LLM path finds memory writes. That is a data-integrity bug, not a missing feature, and it is why the widening sits in Phase 1 rather than in the phase that needs the filter.

### 2.3 The evaluated content already flows into the event stream, unconditionally, through two channels

This is the fact that decides question 6, and it is not hypothetical. There are **two** channels, and the first revision of this document only found one.

**Channel one, the preview.** `RuleEngine._evaluate_leaf` sets, with no flag guarding it:

```python
metadata = dict(result.metadata or {})
if self.include_raw_selected_data:
    metadata["engine_selected_data"] = data
metadata["engine_selected_data_preview"] = _selected_data_preview(data)
metadata["condition_trace"] = trace
```

`engine/src/agent_control_engine/core.py:357-361`. The raw value is gated behind `AGENT_CONTROL_INCLUDE_RAW_SELECTED_DATA`; the preview is not. `_selected_data_preview_value` (`:101-146`) truncates strings at `SELECTED_DATA_PREVIEW_MAX_CHARS`, default 500, and redacts dict values whose **key** matches `_SENSITIVE_KEY_PARTS` (`:67-75`). A bare string selected at `input` is not a dict, so nothing redacts it. Five hundred characters of it travel verbatim.

Then, on the SDK side:

```python
_DEBUG_METADATA_KEYS = frozenset({
    "selected_data", "selected_data_preview",
    "engine_selected_data", "engine_selected_data_preview",
})

def _safe_event_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Drop raw/debug metadata that should not be exported as observability data."""
    safe_metadata = {k: v for k, v in metadata.items() if k not in _DEBUG_METADATA_KEYS}
    if "input" not in safe_metadata:
        for preview_key in ("engine_selected_data_preview", "selected_data_preview"):
            preview = metadata.get(preview_key)
            if isinstance(preview, dict) and "value" in preview:
                safe_metadata["input"] = preview["value"]
                break
    return safe_metadata
```

`sdks/python/src/agent_control/evaluation_events.py:25-44`. The function whose docstring says it drops data that should not be exported drops the raw keys and then **promotes the preview into `metadata["input"]`**, which `_build_events_for_matches` passes straight to `ControlExecutionEvent(metadata=event_metadata)` (`:111`, `:136`).

**Channel two, everything the denylist does not name.** `metadata["condition_trace"]` (`core.py:361`) is a dict carrying `message`, `error`, `selector_path` and `evaluator_name`. It is not in `_DEBUG_METADATA_KEYS`. Neither is any evaluator's own metadata, which `_evaluate_leaf` copies in wholesale at `core.py:358`. And the `json` evaluator, which this design recommends for typed conditions, echoes values verbatim:

```python
errors.append(f"{field_path}: value '{value}' not in allowed values: {allowed}")
...
return EvaluatorResult(
    matched=True, confidence=1.0,
    message=f"Constraint validation failed: {'; '.join(errors[:3])}",
    metadata={"error_count": len(errors), "errors": errors},
)
```

`evaluators/builtin/src/agent_control_evaluators/json/evaluator.py:344-400`. `errors` is the full, untruncated list, and it lands in `result.metadata`, which `_evaluate_leaf` copies and `_safe_event_metadata` passes through untouched. A `json` constraint pointed at `input` to length-check a fact writes the fact's value into the event.

`ControlExecutionEvent` is readable at `OBSERVABILITY_READ: AccessLevel.AUTHENTICATED` (`server/src/agent_control_server/auth_framework/providers/header.py:53`). So a control that denies a fact for containing a credential writes that credential into a store every key in the namespace can read, by at least two independent routes.

**This is why section 3.6's redaction is an allowlist and not a denylist.** A denylist over a metadata dict that three separate components contribute to is a list that is wrong the next time anybody adds a key.

### 2.4 The SDK fails closed on evaluation *failure*, but it does not fail closed on evaluation *absence*

This is the single most important correction in this revision, and it invalidates the naive version of the fail-closed table.

The first half is true and is a real precedent. `_evaluate_and_enforce` (`sdks/python/src/agent_control/integrations/_core.py:48`) is documented as enforcing "fail-closed blocking semantics"; any `result.errors` raises `RuntimeError`, and in the ADK plugin every exception out of that call reaches `_handle_llm_exception` (`plugin.py:334`), whose `else` branch returns `build_blocked_llm_response(...)` with "Agent Control could not evaluate the request safely" (`plugin.py:983-988`).

The second half is where a naive memory gate would have a hole wide enough to drive the entire product through.

```python
resolved_controls = state.server_controls or []
```

`sdks/python/src/agent_control/evaluation.py:557`, inside `evaluate_controls`. And the network call is conditional:

```python
if _has_applicable_prefiltered_server_controls(server_control_payloads, request):
    ...  # the only POST to /api/v1/evaluate
```

`evaluation.py:461`. `_has_applicable_prefiltered_server_controls` (`:117-155`) returns `False` outright when `parsed_server_controls` is empty. With no local controls either, `check_evaluation_with_local` falls off the end and returns:

```python
return _with_parse_errors(EvaluationResult(is_safe=True, confidence=1.0))
```

`evaluation.py:514`. No exception, no `result.errors`, no network traffic. A clean pass.

Now the state that produces it. `init()` catches every connection failure:

```python
except Exception as e:
    logger.error("Could not connect to server: %s", e, exc_info=True)
    logger.info("Will use local controls if available")
published_controls = _publish_server_controls(server_controls)
```

`sdks/python/src/agent_control/__init__.py:698-703`, with `server_controls` still `None` from its initialiser at `:619`, and `_publish_server_controls(None)` setting `state.server_controls = None` (`:194-200`). The refresh worker is no better: on a failed fetch it logs and `continue`s (`:301-306`), keeping whatever is cached, which after a failed boot is nothing.

**So an agent that booted while the server was down, or any agent before its first successful refresh, would admit every proposed fact and return every recalled candidate, silently, with no event and no error.** That is not an edge case. `orchestration-plan.md` section 9.6 makes a restart a shipped operator action, and the default `policy_refresh_interval_seconds` is 60 (`__init__.py:458`), so there is a window on every boot.

The design's response is section 3.4, and its shape is: **never infer "evaluated" from the absence of an exception.** The gate demands positive proof that a control ran, and treats its absence exactly as it treats a timeout.

The structural-refusal precedent still holds and is still borrowed. `_refuse_unevaluatable_parts` (`plugin.py:908-926`) blocks file parts no control could evaluate, and its docstring is explicit that this is "an SDK-level structural refusal, not a guardrail verdict", so no `on_violation_callback` fires and no `blocked_message_template` applies, because no control made the decision. Every fail-closed refusal in section 3.4 is that category and is reported that way.

### 2.5 ADK's memory boundary is coarser than a fact, and for the only implementation that persists, the write path cannot see facts at all

Flagged A1 through A4 because ADK is not installed here.

**A1.** `BaseMemoryService` has two methods: `add_session_to_memory(session)` and `search_memory(*, app_name, user_id, query) -> SearchMemoryResponse`, where the response carries `memories: list[MemoryEntry]` and a `MemoryEntry` carries `content`, `author` and `timestamp`.

**A2.** `InMemoryMemoryService` stores session events verbatim, keyed by app and user, and loses everything on process restart.

**A3.** `VertexAiMemoryBankService` performs **extraction remotely**: the caller hands it a session, and Memory Bank decides what facts to distil from it. The extracted facts are never returned to the caller on the write path.

**A4.** `VertexAiRagMemoryService` chunks and indexes session content into a RAG corpus, again without returning per-fact artifacts to the caller.

Only the two Vertex-backed services persist across restarts, both need Google Cloud, and this user has a Gemini API key only. In practice their ADK memory is in-process today and evaporates on the restart that is already a shipped operator action.

Three consequences, and the third is load-bearing.

*The write gate's granularity is a session commit, not a fact.* `add_session_to_memory(session)` hands over everything new. The decorator can describe that material, hash it, size it and evaluate its text, but it cannot enumerate "the three facts this will become" because nobody has decided them yet. Section 3.1 therefore specifies a payload that is honest about granularity: `fact.count` is `1` when the runtime proposes a discrete fact and `null` when it hands over a session, and controls written against per-fact fields fail closed on `null` rather than passing vacuously.

*A runtime that does its own extraction gets fact granularity, and should.* QM's shape, a burst buffer that fires an extractor once per burst and proposes discrete facts, is exactly the shape this gate wants. So the framework-neutral API in section 3.5 takes a fact, and the ADK decorator is the adapter that degrades to session granularity when the inner service extracts remotely.

*For Memory Bank, the write gate is structurally incapable of seeing the facts it is supposed to gate.* Not slow, not lossy: incapable. The facts are produced inside Google's service from material we already handed over. The only place those facts become visible to anything we control is `search_memory`, on the way back. **That is the strongest argument in this document for gating recall,** and it is an argument from the deployment target rather than from a threat model.

### 2.6 Recall reaches the model by two paths with two completely different visibilities

Unverified here, flagged **A5**, and the most valuable thing Phase 0 can measure.

ADK exposes memory to an agent two ways. `load_memory` is an ordinary tool: the agent calls it, and the result comes back as a `function_response` part. `preload_memory` is a tool that implements `process_llm_request` and appends recalled memory to the request's instructions, which is to say into `config.system_instruction`.

Those two land on opposite sides of a line this repo has already drawn twice.

A `function_response` part **is** read by the control layer today. `_extract_structured_part` (`sdks/python/src/agent_control/integrations/google_adk/_extractors.py:76-90`) explicitly serializes `function_call`, `function_response`, `executable_code` and `code_execution_result`. So a deployment using `load_memory` can write a post-stage tool control scoped to that step name and see recalled text **right now**, with nothing from this plan. That is a same-day mitigation and belongs in the docs.

`system_instruction` is read by nothing. `extract_request_text` (`_extractors.py:87-94`) reads `llm_request.contents[-1].parts` and never touches it, which is precisely hole number one from `docs/plans/agent-system-prompts.md` section 2.1. A `preload_memory` deployment injects recalled facts into the field no control evaluates, on every model call, forever.

So the third hole is not a new class of bug. It is hole number one reached through a different door, with the aggravating factor that the content arrives from a store rather than from an admin's editor. Which is why gating at the memory service boundary, before the tool ever returns, is the right attachment point: it covers both doors with one decorator, and it does not depend on which recall tool the deployment happens to use.

### 2.7 The attribution that matters is inside the fact body, not beside it

The first revision put provenance in a metadata field and made "never touch the body" a structural guarantee. That was the wrong boundary, and reasoning it through is worth the paragraph because the same mistake is easy to make twice.

The body is what gets injected into the system prompt on recall. Metadata is not. A body of

```
Paul confirmed the prod DB password rotation is deferred (said in #eng)
```

arrives with `claimed_source_status = "absent"` because nobody populated the metadata field, sails past a control that denies on `status == "forged"`, sails past a control that requires `speaker_attributed`, is stored verbatim, and from then on the model reads an in-body attribution as though the operator had said it. Every attribution the controls guarded was one the model never read; every attribution the model read was unguarded.

QM's design does not have this problem, because in QM the provenance tag **is** in the text and the rewrite to `[claimed source: X]` operates on the text. Moving it out to metadata removed the defence rather than strengthening it.

**So the body is inspected and, in exactly one deterministic way, weakened.** Section 3.3 defines what that is allowed to mean, and the guarantee it replaces is stated as a property rather than as an absence: no transformation of a fact body takes any input from any control, and no transformation can strengthen a claim.

The precedent is already in the repo and is argued the same way. `_sanitize.py`'s module docstring says the transcript marker is "Forgeable by anyone who can put text in front of the model, which is why controls key on `context.agent_control.*` instead and why every occurrence in text we did not author is neutralized below", and `_MARKER_RE` rewrites `[agent-control:` to a non-breaking hyphen form that "reads the same, matches nothing" (`_sanitize.py:16-21`). Neutralizing a forged attribution suffix is the same operation on the same grounds.

### 2.8 The multi-tenant case that matters is not `namespace_key`

`_state.py:38` is a module-level singleton and `AgentControlPlugin.__init__` raises when `agent_name` disagrees with the initialised agent (`plugin.py:84-90`). Both confirmed. A process therefore cannot straddle namespaces, and the first revision leaned on that as the tenancy story.

It is true and it answers the wrong question. The deployment with a tenancy problem is **one agent process serving many end users**, where the separation is `user_id` inside `search_memory(*, app_name, user_id, query)`, not `namespace_key`. A `user_id` mix-up inside a custom `BaseMemoryService` serves one end user's private facts into another's system prompt, inside one namespace, inside one process, and nothing about `_state.py` notices.

The recall payload therefore carries requester identity (section 3.1) and the design ships a speaker-mismatch template. Section 3.7 states what this detects and what it cannot enforce.

### 2.9 Agent Control has no memory store, this does not add one, and here is what the customer must supply

The boundary has been defended three times in this project and it is defended again here, with the cost stated rather than glossed.

**What Agent Control provides:** the decision point, the payload contract, the evaluation, the verdict, the event record, the review queue for held facts, and a one-time admission that lets an operator release something that was held.

**What the customer must supply, and there is no way around any of it:**

1. **A memory store.** ADK's `InMemoryMemoryService` for development, a Vertex-backed service or their own implementation for production. Agent Control does not persist facts and will not.
2. **Two call sites, or one decorator.** Either wrap their `BaseMemoryService` in `AgentControlMemoryService`, or call `propose_fact()` and `filter_recall()` themselves.
3. **An extractor, if they want fact granularity.** The gate evaluates what it is handed. Handing it a session gets session-granular decisions.
4. **A scope resolver, which also supplies requester identity and the trust signals.** `proposer.trusted`, `scope.channel_trusted` and `requester.id_sha256` are asserted by the runtime, because only the runtime knows who is in the room and who asked. Agent Control cannot compute them; both booleans default to `false` and the identity defaults to absent, which is the fail-closed direction and will make the shipped templates hold more than an operator expects. That is deliberate and the UI says so.
5. **Enforcement of the release.** When an operator releases a held fact, the runtime is what writes it. Agent Control marks it admissible once.

**What Agent Control cannot promise, stated plainly because a control plane that overclaims is worse than one that underclaims:** it cannot enforce that a fact never entered the store by another route, it cannot delete a fact, it cannot re-evaluate the store's existing contents at rest, and it cannot prove that a recall gate is attached on every call. Section 3.7 answers the third with recall-time re-evaluation, which is a real answer. The rest are limitations and appear in sections 15 and 16.

---

## 3. The decisions

### 3.1 The payload: what a control actually sees (question 1)

**Decision: the fact text goes in `input` on write and `output` on recall. Everything a control needs to reason about trust, provenance, requester and scope goes in a server-reserved `context.agent_control.memory` block, pre-aggregated into scalars, always emitted, validated at the boundary.**

Three constraints fix this shape and none are negotiable.

`ControlSelector.validate_path` (`controls.py:36-54`) permits five roots. `select_data` walks dotted paths by dict access then attribute access and **has no list-index syntax**, so `context.agent_control.memory.recall.candidates.0.scope_id` resolves to `None`. And a missing path resolves to `None`, which most evaluators treat as a non-match, which is fail-open. That last one is why the block ships even when every value is a zero or a null: `_safe_context`'s docstring already states the rule, that the block is emitted "even when there are no attachments so that `attachment_summary.count` is a selectable zero rather than a missing path a threshold control would read as absent" (`plugin.py:788-791`). Same rule, same reason.

**The `agent_control` key is reserved and server-authored.** `_safe_context` (`plugin.py:807-808`) already pops any `agent_control` key a deployment's own `context_extractor` supplies, because "the audited party must not author its own audit record". The memory payload builder does the same.

#### Write payload

```
Step.type    = "memory"
Step.name    = "memory.write"
Step.input   = <the proposed fact text, or the session material; neutralized per 3.3>
Step.output  = null
Step.context = { "agent_control": { "memory": { ... } } }
stage        = "pre"
```

```
context.agent_control.memory.op                              "write"
context.agent_control.memory.schema_version                  1
context.agent_control.memory.granularity                     "fact" | "session" | "unknown"

context.agent_control.memory.scope.kind                      "private"|"shared"|"org"|"unknown"
context.agent_control.memory.scope.id                        str|null   # opaque, tenant-local, <=255
context.agent_control.memory.scope.resolver_present          bool       # false => nothing below is meaningful
context.agent_control.memory.scope.readers                   int        # -1 when unknown
context.agent_control.memory.scope.channel_trusted           bool       # runtime-asserted, default false
context.agent_control.memory.scope.first_gated_at            str|null   # ISO8601, see 3.7

context.agent_control.memory.proposer.kind                   "human"|"model"|"tool"|"import"|"unknown"
context.agent_control.memory.proposer.id_sha256              str|null   # never the raw id
context.agent_control.memory.proposer.trusted                bool       # runtime-asserted, default false

context.agent_control.memory.provenance.basis                "observed"|"inferred"|"unknown"
context.agent_control.memory.provenance.claimed_source       str|null   # normalized, bounded, neutralized
context.agent_control.memory.provenance.claimed_source_status "verified"|"unverified"|"forged"|"absent"
context.agent_control.memory.provenance.speaker_scope_id     str|null
context.agent_control.memory.provenance.speaker_attributed   bool

context.agent_control.memory.fact.count                      int|null   # null when granularity != "fact"
context.agent_control.memory.fact.chars                      int
context.agent_control.memory.fact.sha256                     str
context.agent_control.memory.fact.truncated                  bool
context.agent_control.memory.fact.repeat_count               int
context.agent_control.memory.fact.duplicate_of_sha256        str|null
context.agent_control.memory.fact.prior_denials              int        # server-sourced, see 3.9
context.agent_control.memory.fact.body_attribution_claims    int        # see 2.7 / 3.3
context.agent_control.memory.fact.body_attribution_verified  bool
context.agent_control.memory.fact.body_marker_forgeries      int

context.agent_control.memory.turn.invocation_id              str|null
context.agent_control.memory.turn.trace_id                   str|null
context.agent_control.memory.turn.halted                     bool
context.agent_control.memory.turn.buffered_seconds           int
```

`fact.body_attribution_claims` and `fact.body_attribution_verified` are the fix for section 2.7 and they are the two fields a control author most needs. They describe attribution shapes found **inside the fact text**, which is the text the model will read.

#### Recall payload

```
Step.type    = "memory"
Step.name    = "memory.recall"
Step.input   = <the recall query text>
Step.output  = <admitted-so-far candidate texts, index-prefixed and joined>
Step.context = { "agent_control": { "memory": { ... } } }
stage        = "post"
```

Recall runs at stage `post` and write at stage `pre`, which is not a convenience. Pre means "this has not happened yet and you may prevent it". Post means "this exists and you may prevent it reaching the next thing". Both are the existing `Literal["pre","post"]` (`controls.py:89`), so **no stage widening is needed**, and an operator's intuition about the words carries over intact.

The recall block repeats `op`, `schema_version` and the scope sub-object, adds requester identity, and replaces the fact and provenance sub-objects with pre-aggregated counts over the candidate set:

```
context.agent_control.memory.requester.id_sha256             str|null
context.agent_control.memory.requester.kind                  "human"|"agent"|"unknown"
context.agent_control.memory.requester.scope_id_count         int        # scopes the requester may read

context.agent_control.memory.recall.candidate_count           int
context.agent_control.memory.recall.total_chars               int
context.agent_control.memory.recall.oldest_age_days           int|null
context.agent_control.memory.recall.pre_gate_count            int
context.agent_control.memory.recall.foreign_scope_count       int
context.agent_control.memory.recall.speaker_mismatch_count    int
context.agent_control.memory.recall.unverified_source_count   int
context.agent_control.memory.recall.forged_source_count       int
context.agent_control.memory.recall.body_attribution_count    int
context.agent_control.memory.recall.inferred_count            int
context.agent_control.memory.recall.unattributed_count        int
context.agent_control.memory.recall.held_count                int
context.agent_control.memory.recall.truncated_count           int
context.agent_control.memory.recall.structurally_dropped      int        # see 3.3
context.agent_control.memory.recall.over_budget               bool       # see 3.10
context.agent_control.memory.recall.candidates                [ {...}, ... ]
```

`candidates` exists for the `json` evaluator only, whose `_parse_json` passes `dict` and `list` straight through (`evaluators/builtin/src/agent_control_evaluators/json/evaluator.py:150-157`), so a schema over an array is writable with an evaluator already in the repo. Every threshold a control would want is also a scalar above it, because `select_data` cannot index a list. Each candidate object carries `index`, `sha256`, `chars`, `scope_id`, `age_days`, `basis`, `claimed_source_status`, `speaker_attributed`, `speaker_matches_requester`, `body_attribution_claims`, `pre_gate` and `truncated`. **Never the text**: the text is in `output`, whole, once.

`requester.*` is what makes "deny when the requesting user is not the speaker of this candidate" writable, which is the one recall control a shared multi-user deployment actually needs (section 2.8). With `scope_resolver=None`, `requester.id_sha256` is `null`, `speaker_mismatch_count` is the full candidate count, and `scope.resolver_present` is `false`.

#### The selector paths a control author actually writes

Deny facts about credentials, on write:

```json
{
  "scope": {"step_types": ["memory"], "step_names": ["memory.write"], "stages": ["pre"]},
  "condition": {
    "selector": {"path": "input"},
    "evaluator": {"name": "regex",
                  "config": {"pattern": "(?i)(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|\\bpassword\\s*[:=])"}}
  },
  "action": {"decision": "deny"}
}
```

Hold facts that assert an attribution inside their own text that the runtime did not vouch for:

```json
{
  "scope": {"step_types": ["memory"], "stages": ["pre", "post"]},
  "condition": {
    "selector": {"path": "context.agent_control.memory"},
    "evaluator": {"name": "json", "config": {"schema": {
      "type": "object",
      "properties": {"fact": {"type": "object", "properties": {
        "body_attribution_claims": {"type": "integer", "minimum": 1},
        "body_attribution_verified": {"const": false}
      }, "required": ["body_attribution_claims", "body_attribution_verified"]}},
      "required": ["fact"]}}}
  },
  "action": {"decision": "steer",
             "steering_context": {"message": "Held: the fact text attributes itself to a source the runtime did not vouch for."}}
}
```

Hold facts proposed in an untrusted shared channel:

```json
{
  "scope": {"step_types": ["memory"], "step_names": ["memory.write"], "stages": ["pre"]},
  "condition": {
    "and": [
      {"selector": {"path": "context.agent_control.memory.scope.kind"},
       "evaluator": {"name": "list", "config": {"values": ["shared", "org", "unknown"], "logic": "any"}}},
      {"selector": {"path": "context.agent_control.memory"},
       "evaluator": {"name": "json", "config": {"schema": {
         "type": "object",
         "properties": {"scope": {"type": "object",
                                  "properties": {"channel_trusted": {"const": false}},
                                  "required": ["channel_trusted"]}},
         "required": ["scope"]}}}}
    ]
  },
  "action": {"decision": "steer",
             "steering_context": {"message": "Held: proposed in a shared channel the runtime did not vouch for."}}
}
```

The boolean goes through the `json` evaluator rather than `list`, because a `list` evaluator comparing against `["false"]` would depend on how a Python `False` stringifies through the selector, and that is the kind of coupling that breaks on a library bump. A `json` schema with `{"const": false}` is typed.

**A warning that belongs beside every one of these examples and in the template descriptions:** a `json` evaluator's failure metadata echoes the offending value (`json/evaluator.py:397-400`). Section 3.6's allowlist stops that reaching the event store on memory steps, and no memory template ever points a `json` constraint at `input`.

#### Two rules that keep the payload from failing open

**The block is mandatory and the server validates it.** When `step.type == "memory"`, the `/evaluate` handler validates `step.context["agent_control"]["memory"]` against `MemoryStepContext` and returns `400 MEMORY_PAYLOAD_INVALID` when it is missing, malformed, or carries an unknown `schema_version` above the one it understands. A step type is open; a contract is not. The alternative is every control author writing defensive conditions for absent paths.

**Absent means unknown means hostile.** `proposer.trusted` and `scope.channel_trusted` default to `false`. `basis` and `claimed_source_status` default to `"unknown"` and `"absent"`. `readers` is `-1` when unknown. `body_attribution_verified` defaults to `false`. Every shipped template treats `unknown` alongside the bad value, never alongside the good one.

### 3.2 Write, recall, or both (question 2)

**Decision: both, and recall is the one that carries the guarantee.**

A write-only gate covers facts written after installation, through a path the gate is attached to, by a runtime that calls it. It does not cover:

- Facts already in the store when the gate was attached. On any real deployment that is most of them.
- Facts written by a system nobody wrapped: a second agent, a batch import, an ops script, a colleague's process pointed at the same store.
- Facts a control plane outage let through. Section 3.4 closes the outage hole on the write path, but "the gate was misconfigured for six hours" is a state that has to be recoverable, and a write-only design has no recovery.
- Facts that were fine under Tuesday's controls and are not fine under Thursday's. **A write gate evaluates once, at write time, forever.** Tightening a control changes nothing about what the agent already believes.
- For a `VertexAiMemoryBankService` deployment, the facts themselves, at all, because extraction is remote and the write path never sees them (A3).

That last one converts this from a defence-in-depth argument into a correctness argument for the deployment target the customer reaches for when they outgrow in-process memory.

**So the recall gate is what makes the guarantee true.** With it, the guarantee is: *no fact reaches a model through the gated memory service without a control evaluating it on that call*. Without it, the guarantee is: *no fact was written through this path without evaluation*, which is a statement about our plumbing rather than about the agent's beliefs.

Recall gating also answers the "controls tightened later" edge case with no store rewrite, no backfill, and no enumeration of a store we do not own. Thursday's control bites on Thursday's first recall.

#### What it costs on the hot path, honestly

Capture is not a hot path. QM's burst buffer, 180 seconds of quiet or 10 turns, is borrowed wholesale in section 7.1 precisely so the write gate's latency is invisible.

**Recall is a hot path** and its cost is a real number this design does not get to hand-wave.

- **`execution: "sdk"` controls cost no network** in the normal case: `check_evaluation_with_local` evaluates local controls in-process and only POSTs when `_has_applicable_prefiltered_server_controls` finds a matching server control (`evaluation.py:437-461`). A regex or list evaluator over a bounded batch is microseconds. **Recall controls default to `execution: "sdk"`** and the SDK logs a warning at attach time when a memory recall control is server-executed, naming the added round trip.
- **`execution: "server"` controls cost one HTTP round trip per recall.** On a co-located deployment that is single-digit milliseconds; over a network it is whatever that network is. No number is asserted here that was not measured. **Phase 0 measures it** against the deployment's own server and the result goes in the docs beside the setting that turns it on.
- One evaluation per recall, not one per candidate. A recall returning twelve memories is one `Step` whose `output` is the joined text and whose context carries twelve pre-aggregated counts plus a `candidates` array.
- The batch is **bounded** before the payload is built (section 3.10), so the evaluation cost has a ceiling that an attacker who can post in a shared channel cannot raise.
- **The freshness check in section 3.4 costs nothing on the hot path.** It reads a monotonic timestamp the refresh loop already had to set. It does not add a network call.
- `recall_timeout_ms` defaults to **400**, well under any model call, and a timeout is a drop rather than a turn failure.

#### The asymmetry in defaults

Write controls default to `execution: "server"` and the write gate **always** performs the round trip (section 3.4), because a write is off the response path, server execution picks up the operator's newest control immediately rather than after a refresh interval, and a forced round trip converts an outage into an exception instead of a silent pass. Recall controls default to `execution: "sdk"`, because a recall is in front of a model call. Both are defaults on the shipped templates, not restrictions.

### 3.3 Actions: what steer means for a fact (question 3)

`deny` and `observe` map cleanly. On write, `deny` means the fact is not persisted and the runtime is told so. On recall, `deny` means the candidate batch does not reach the model. `observe` records and lets it through, on both.

**`steer` means hold, and no control ever supplies a byte of a fact.**

The brief's constraint is the right one and it survives intact, but it is now stated as a property rather than as "the body is never touched", because section 2.7 showed that never touching the body means never defending it.

**The two properties, and they are what the tests assert:**

1. **No transformation of a fact body takes any input from any control.** Not from `steering_context`, not from `result.metadata`, not from a control name, not from a template parameter. The transformation functions live in `sdks/python/src/agent_control/memory/_provenance.py` and their signatures accept a string and configuration constants, and nothing else.
2. **Every transformation only weakens.** Each one replaces a claim of authority with a marked, inert claim of the same claim. None adds a proposition, removes a proposition, or changes a proposition's truth value.

**The three permitted transformations, all deterministic, all pre-evaluation, all applied to text the runtime did not author:**

- **Marker neutralization.** `[agent-control:` becomes the non-breaking-hyphen form, exactly as `neutralize_marker` already does (`_sanitize.py:18-21`). Counted in `fact.body_marker_forgeries`.
- **Attribution-suffix neutralization.** A trailing or inline attribution shape the runtime did not author, such as `(said in #eng)` or `— per Paul`, is rewritten to `[claimed source: X]`. Counted in `fact.body_attribution_claims`, with `fact.body_attribution_verified` true only when the runtime marked the attribution as its own. This is QM's rewrite, applied where QM applies it: in the text.
- **Provenance-field normalization.** The metadata `claimed_source` gets the same treatment and yields `claimed_source_status`.

The attribution detector is a bounded set of compiled patterns in one module with one test corpus. It is not a model, not a heuristic score, and not extensible by configuration, because an operator-supplied pattern is operator-supplied input into a body transformation and rule 1 does not have an exception for operators either.

**On write, `steer` means:**

1. The fact is **not** written to the runtime's memory store.
2. A `memory_quarantine` row is created holding the fact text (post-neutralization), its hash, the scope, the proposer metadata and the deciding control.
3. The runtime receives `MemoryWriteHeld` carrying the quarantine id and the control's `steering_context.message`.
4. An operator reviews it and either discards it or releases it (section 3.8).

**On recall, `steer` means drop the whole batch**, and this is a correction of the first revision rather than a simplification for its own sake. That revision specified per-candidate dropping driven by `result.metadata["flagged_sha256"]`. No shipped evaluator produces that key: `regex` emits `{"pattern": ...}` (`regex/evaluator.py:72`), `json` emits `{"error_count", "errors"}` (`json/evaluator.py:397-400`), and `list` emits nothing candidate-shaped. There is also no contract by which an evaluator could learn candidate boundaries, since it sees only the joined `Step.output`. The feature was unimplementable and every recall steer would have silently taken the documented degradation.

**Per-candidate filtering still exists, and it belongs to the SDK rather than to a control.** `_gate.py` runs a structural pass before evaluation, using only fields it computed itself: `pre_gate`, `foreign_scope`, `speaker_mismatch`, `forged_source`, `body_attribution_claims` and `truncated`. With `filter_structural=True` on the decorator (default `False`), candidates matching an enabled structural signal are dropped before the payload is built, counted in `recall.structurally_dropped`, and never evaluated. This is not a control decision and is not recorded as one; it is the same category as `_refuse_unevaluatable_parts`. Controls remain batch-granular and the docs say so in one sentence.

**`steer` on write requires somewhere to put the fact, and the fallback is discard.** Quarantine needs a server round trip. If submission fails, is rate limited, or is disabled, a write steer becomes a **discard with a warning**, never a write. Losing a fact is recoverable, because it can be proposed again. Writing an unreviewed fact is not.

**The structural guarantee, and the test that holds it.** The steer path does not read text out of the control's response at any point. `steering_context.message` reaches the runtime's log and the quarantine row's `reason` column. It cannot reach the fact body, because the code that writes the fact body holds the neutralized original string. A test asserts that the body persisted after a steer is byte-identical to `neutralize(proposed_body)` or absent, for every action, including one where the control's steering message is itself a plausible fact sentence, and including one where the control name is a plausible fact sentence.

### 3.4 Fail-closed (question 4)

**Decision: a fact that cannot be shown to have been evaluated is not persisted, and a candidate that cannot be shown to have been evaluated is not returned. "Cannot be shown" is the operative phrase and it is not the same as "raised an exception".**

#### Positive proof of evaluation

Section 2.4 established that `evaluate_controls` returns `EvaluationResult(is_safe=True, confidence=1.0)` with no network call and no error when the control cache is empty, which after a failed boot or before the first refresh is the normal state. So the gate does not call `evaluate_controls` and hope. It requires three things, all of which it checks itself:

1. **A fresh policy.** `state.controls_published_at: float | None` is a new field on `_StateContainer` (`sdks/python/src/agent_control/_state.py`), set to `time.monotonic()` **only** inside `_publish_server_controls` when `controls is not None` (`__init__.py:194-204`), and left unset when `init()` swallowed a connection failure. The gate refuses when it is unset, or when `now - controls_published_at > max_policy_staleness_seconds`, default `2 * policy_refresh_interval_seconds` and therefore 120 by default (`__init__.py:458`).
2. **Proof a control ran.** `EvaluationResponse` carries `matches`, `errors` and `non_matches` (`models/src/agent_control_models/evaluation.py:153-164`). The gate treats `len(matches) + len(non_matches) + len(errors) == 0` as **unevaluated**, not as safe. This is the check that turns "no applicable memory control exists" into a refusal rather than a pass. It is governed by `allow_when_no_memory_controls: bool = False`, and a deployment that turns it on gets a warning at attach time naming what it just disabled.
3. **On the write path, a real round trip.** `_gate.py` does not go through the prefilter. It builds the `EvaluationRequest` and POSTs `/api/v1/evaluate` unconditionally for `memory.write`, because writes are off the response path so the round trip is free and because a transport failure must be an exception rather than an inferred pass. Recall keeps the prefilter for latency, and relies on checks 1 and 2 instead.

#### The table

| Failure | Write path | Recall path |
|---|---|---|
| Control plane unreachable | Refuse. `MemoryWriteRefused(retryable=True)`. Buffer retries 3 times with backoff, then dead-letters locally with a warning. | Return zero memories. Log once per interval. **The turn continues.** |
| Control cache never populated (boot-time outage) | Refuse. Check 1. | Return zero memories. Check 1. |
| Control cache stale past the window | Refuse. Check 1. | Return zero memories. Check 1. |
| No applicable memory control | Refuse, unless `allow_when_no_memory_controls`. Check 2. | Return zero memories, same setting. Check 2. |
| Evaluation timeout | Refuse. `write_timeout_ms` default 2000. | Return zero memories. `recall_timeout_ms` default 400. |
| Evaluator error (`result.errors` non-empty) | Refuse, matching `_evaluate_and_enforce`'s existing behaviour. | Return zero memories. |
| Payload cannot be built (missing scope, unreadable entry) | Refuse. `_refuse_unevaluatable_parts`'s category: a structural refusal, not a verdict. | Drop that candidate, evaluate the rest, count it in `truncated_count`. |
| Recall batch over budget (3.10) | n/a | Truncate to budget, set `recall.over_budget`, evaluate the truncated batch. A template denies on the flag. |
| Inner memory service raises | **Propagate unchanged.** Not our decision, not reported as one. | **Propagate unchanged.** |

#### The availability trade, stated rather than implied

A control plane outage means the agent stops learning and stops remembering. It does not stop working. An agent that suddenly has no memory is degraded, and degraded in a way a user notices and can work around by repeating themselves. An agent that raises on every recall is down, and the outage propagates out of a component whose whole job is to be optional.

That is a deliberate break from the LLM path, where an unreachable control plane blocks the model call (section 2.4, `plugin.py:983-988`). The difference is what the unevaluated content would do. On the LLM path, letting the call through means the model acts on unevaluated input. On the recall path, dropping the candidates means the model acts on **less** input, and the safety property is fully preserved: nothing unevaluated reached it. Failing closed and failing available are the same choice here, which is a piece of luck worth taking.

`on_recall_unavailable: Literal["empty", "raise"] = "empty"` exists for the deployment that would rather know. It is a setting, it is documented, and the default is `"empty"`.

**The predictable operational consequence, said out loud.** Checks 1 and 2 mean a deployment that attaches the decorator and writes no memory controls gets **no memory at all**, in both directions, immediately. That will read as a broken product to somebody. It is the correct default and the mitigations are: the gate logs the reason once per interval with the exact setting to change; the gate-status card in the UI renders "no memory controls configured, everything is being refused" as a distinct state with a link to the templates; and Phase 6 ships six templates so that turning the feature on is enabling templates rather than authoring conditions.

#### What fail-closed does not mean

It does not mean the fact is gone. A refused write returns `MemoryWriteRefused` with `retryable: bool`, and the burst buffer holds the material. It does not mean the store is consistent: if the inner service already wrote something before the decorator got involved, we cannot un-write it, which is exactly why the decorator evaluates **before** delegating on write and never after.

**Fail-closed on the write path is worth almost nothing on its own, and the design says so.** A refused write leaves the fact out of the store; a store we do not own can still be written by anything else. This is a control on a path, not a property of the data. The property comes from recall.

### 3.5 The attachment point (question 5)

**Decision: a `MemoryGate` Protocol in framework-neutral code, an `AgentControlMemoryService` decorator over `BaseMemoryService` for ADK, and two explicit functions for everyone else. Protocol plus decorator, following `linear_client.py` and `adk_executor_client.py`.**

The pattern is established twice already. `LinearClient` is "a two-method protocol, so tests substitute a fake object instead of a fake transport", and the ADK executor client concentrates every framework-specific string between two `Wire` markers so that correcting one file is the whole correction. Memory needs it more, because A1 through A5 are all unverified.

#### Layers

```
sdks/python/src/agent_control/memory/__init__.py       # public: propose_fact, filter_recall, MemoryGate
sdks/python/src/agent_control/memory/_gate.py          # decision logic, no framework imports
sdks/python/src/agent_control/memory/_payload.py       # builds context.agent_control.memory
sdks/python/src/agent_control/memory/_provenance.py    # body + field neutralization, forgery detection
sdks/python/src/agent_control/memory/_buffer.py        # burst buffer, snapshots, dedup LRU, dead letter
sdks/python/src/agent_control/memory/_admissions.py    # release cache, refreshed on the policy loop
sdks/python/src/agent_control/integrations/google_adk/memory_service.py   # the ADK blast radius
```

`_gate.py` imports nothing from `google`. Everything ADK knows how to say lives in `memory_service.py` between two `Wire` markers. When A1 through A5 land, correcting that one file is the whole correction.

#### The framework-neutral surface

```python
class MemoryGate(Protocol):
    async def propose_fact(self, proposal: FactProposal) -> GateDecision: ...
    async def filter_recall(self, recall: RecallBatch) -> RecallDecision: ...
```

`GateDecision` is `{admitted: bool, action: "allow"|"deny"|"steer"|"observe", quarantine_id: str|None, reason: str|None, retryable: bool, unevaluated_reason: str|None}`. `RecallDecision` is `{admitted_indices: list[int], structurally_dropped: int, action, reason, unevaluated_reason}`. Neither carries text a control authored. `unevaluated_reason` is populated for the section 3.4 refusals and is what the UI's gate-status card and the SDK's rate-limited log line both read.

#### The ADK decorator

```python
class AgentControlMemoryService(BaseMemoryService):
    def __init__(self, inner: BaseMemoryService, *, agent_name: str,
                 scope_resolver: ScopeResolver | None = None,
                 gate_writes: bool = True, gate_recall: bool = True,
                 on_recall_unavailable: Literal["empty", "raise"] = "empty",
                 oversize_facts: Literal["refuse", "truncate"] = "refuse",
                 filter_structural: bool = False,
                 allow_when_no_memory_controls: bool = False) -> None: ...
```

`add_session_to_memory` snapshots the session's new events (section 7.5, and it is a snapshot for a reason), enqueues on the burst buffer, and on flush runs the gate and delegates only on `admitted=True`. `search_memory` delegates to `inner` first, bounds the batch, runs the structural pass, gates the remainder, and returns a `SearchMemoryResponse` containing only admitted entries. **Evaluate before delegating on write, after delegating on recall.** Both orders are the one where a refusal is still possible.

`scope_resolver` is how the deployment says what scope a session belongs to, who can read it, and who is asking. Without one, `scope.resolver_present` is `false`, `scope.kind` is `"unknown"`, `readers` is `-1`, `requester.id_sha256` is `null` and both trust booleans are `false`, so the shipped templates hold or observe nearly everything. The docs say that in the first paragraph, because an operator who attaches the decorator with no resolver and sees everything held will otherwise file a bug.

#### Runtimes with no hook

**What they get:** the two functions, called by hand, plus the documented `POST /api/v1/evaluate` contract and `POST /api/v1/memory/quarantine`. Everything here is reachable over HTTP with a `Step` a caller constructs. A LangChain, Letta or bespoke store integrates in two call sites.

**What they must add:** those two call sites. There is no ambient interception, no monkeypatching, no import hook, and none is coming. A store the SDK does not sit in front of is a store this feature does not cover.

**What the product must therefore never claim.** The agent row gains `memory_gate_first_seen_at`, set the first time a memory step is evaluated for that agent, and the UI reads it. Four states, and only the first is a claim:

- **Guarded**, both paths seen within the staleness window.
- **Write-only** or **recall-only**, one path seen. Warning banner naming the open path.
- **Refusing**, the gate has been seen but its recent decisions carry `unevaluated_reason`. Distinct from guarded because "nothing is getting through" and "everything is being checked" look identical from a decision count.
- **Unguarded**, never seen. The Memory tab says so and links to the integration doc. It does **not** say "no memory in use", because it cannot know that.

`memory_gate_first_seen_at` is also the anchor for `recall.pre_gate_count` (section 3.7), so it earns a column rather than a scan of the event store.

### 3.6 Event recording: what to store (question 6)

**Decision: the fact text never enters the event stream. Memory-step event metadata is built from an allowlist, not filtered by a denylist, in the SDK and again on the server. The event carries the fact's hash, its length, the scope, the proposer class, the provenance status and the verdict. The text exists in exactly one place a human can read it, the `memory_quarantine` row, which is ADMIN-read and not stored at all on a credential-less server.**

Section 2.3 established that this is not a precaution against a future mistake. The mechanisms that would leak the fact are built, unconditional, shipping, and there are at least two of them.

**One: `map_applies_to` becomes step-name aware.**

```python
def map_applies_to(step_type: str, step_name: str = "") -> AppliesTo:
    if step_type == "tool":
        return "tool_call"
    if step_type == "memory":
        return "memory_recall" if step_name == "memory.recall" else "memory_write"
    return "llm_call"
```

Two values, not one. A single `memory_op` would force every dashboard query and every runbook to spell `applies_to=memory_op AND check_stage=pre` and would teach operators that "pre" means "write", which is a convention rather than a meaning. The Literal is being opened once; opening it to the right vocabulary costs one line. Defaulting to `memory_write` on an unrecognised memory step name is the conservative direction: a write event is the one an operator must not miss. The call site is `_build_events_for_matches` (`evaluation_events.py:106`), which already has `request.step` in hand.

**Two: `_safe_event_metadata` becomes step-type aware and builds from an allowlist.**

This is the change that matters, and denylisting is what the first revision got wrong. For a memory step the function does not filter the metadata dict; it constructs a fresh one:

```
metadata.memory.fact_sha256              str
metadata.memory.fact_chars               int
metadata.memory.scope_kind               str
metadata.memory.scope_id_sha256          str|null
metadata.memory.proposer_kind            str
metadata.memory.claimed_source_status    str
metadata.memory.basis                    str
metadata.memory.body_attribution_claims  int
metadata.memory.quarantine_id            str|null
metadata.memory.unevaluated_reason       str|null
metadata.memory.candidate_count          int|null   # recall only
metadata.memory.dropped_count            int|null   # recall only
metadata.memory.requester_id_sha256      str|null   # recall only
```

plus the three identity keys `observability_metadata` already contributes: `primary_evaluator`, `primary_selector_path`, `leaf_count`. **Everything else is discarded**, including `condition_trace`, `errors`, `message`, `engine_selected_data`, `engine_selected_data_preview`, `input`, and any key an evaluator invents next year. `error_message` on the event is set to a fixed string on memory steps rather than `match.result.error`, because an evaluator's error text can quote its input.

An allowlist costs one thing: an operator who opens a memory denial in the Monitor tab sees no input field and no condition trace. That is the point, it will look like a bug, and section 16 says who is likely to "fix" it.

**Three: the server strips it again on ingest.** `ingest_events` (`server/src/agent_control_server/endpoints/observability.py:87`) applies the same allowlist to any event whose `applies_to` starts with `memory_`. The SDK is the audited party and an SDK a version behind will send the preview. This is the argument the system-prompts plan makes for the reserved `agent_control.*` prefix: a server that trusts a client's redaction has not redacted anything.

**Four: `EventQueryRequest.applies_to` widens to the same four-value Literal.** One line, no migration, and `postgres.py:463-465` filters on `data->>'applies_to'` unchanged.

**The residue, named rather than hidden.** For `execution: "server"` controls, the fact text is in the `/evaluate` request body and in `match.result.metadata` on the response. Both are transient and neither is stored by this design. `RUNTIME_USE` is AUTHENTICATED (`header.py:59`), so any key in the namespace can already send an evaluation request; that is not new exposure created here. What would be new is persisting it, and nothing persists it. If a deployment enables `AGENT_CONTROL_INCLUDE_RAW_SELECTED_DATA`, the raw value returns to the SDK and the allowlist discards it, so the flag does not reopen this. A test asserts that with the flag on.

**A second residue that belongs in the UI copy, not just here.** `OBSERVABILITY_WRITE` is `AccessLevel.AUTHENTICATED` (`header.py:54`). Any key in the namespace can POST fabricated events, including `memory_write` events with `action: "allow"`. The decisions table therefore renders **what was reported**, not what happened, and the panel says so in one line. That is a pre-existing property of the event stream rather than something this feature introduces, but a memory-decisions table is the first place an operator would read it as a security record.

**The quarantine body and the credential-less server.** `api_key_enabled` defaults to `False` (`server/src/agent_control_server/config.py:37`), and `_build_default_provider` resolves the mode to `"none"` and installs `NoAuthProvider` when it is unset (`auth_framework/config.py:220-226`), which authorizes every operation including ADMIN. So an ADMIN-only quarantine read is not a boundary on a default-configured server, and a quarantine table on that server is a public archive of exactly the content a control refused.

The system-prompts plan's answer was a startup gate on delivery. The equivalent here is a gate on **storage of the body**, not on the API:

`check_memory_startup_requirements(*, auth: AuthSettings)` resolves whether the default authorizer will be `NoAuthProvider` using the same rule as `_build_default_provider`. When it will be, and `AGENT_CONTROL_MEMORY_ALLOW_INSECURE_LOCAL_DEV` is not `true`, the module flag `MEMORY_QUARANTINE_BODY_ALLOWED = False`. Quarantine rows are still created, counted, reviewable and discardable. `body` is `NULL` and `body_redacted_reason` is `'insecure_auth'`. The UI shows the hash, the length, the reason and the scope, and says in one sentence that the text is not stored because credential enforcement is off.

The same function sets `MEMORY_RELEASE_ALLOWED = False` under the same condition, and section 3.9 is why.

### 3.7 Scope and tenancy (question 7)

**Decision: a memory scope is `(namespace_key, scope_kind, scope_id)`, `namespace_key` comes from the Principal on every query without exception, and the honest limit is that Agent Control cannot enforce isolation inside a store it does not own. What it can do is make a cross-scope or cross-user recall a first-class control-writable signal, and ship the templates.**

**On the write path.** The proposal carries `scope.kind` and `scope.id`. The server never resolves a scope, never joins across namespaces, and never treats `scope_id` as a key into anything. `memory_quarantine` is keyed `(namespace_key, quarantine_id)`, mirroring `control_execution_events` (`server/src/agent_control_server/models.py:810-814`), with `namespace_key` leading every index. Every service method takes `namespace_key=principal.namespace_key`. Namespace A cannot read, release, discard, consume or count namespace B's held facts, and `server/tests/test_namespace_isolation.py` gains cases for all of them.

**On the recall path, two independent signals.**

*Scope.* The decorator declares which scope ids the requester is entitled to and every candidate carries its own. `recall.foreign_scope_count` counts the mismatches; a candidate whose scope id cannot be determined counts as foreign, which is fail-closed.

*User.* `requester.id_sha256` and per-candidate `speaker_matches_requester` produce `recall.speaker_mismatch_count`. This is the signal that covers section 2.8's case, which `foreign_scope_count` structurally cannot: a `user_id` mix-up inside a custom `BaseMemoryService` returns candidates whose scope is entirely correct and whose speaker is somebody else.

**Both templates ship at `observe`, not `deny`, and that is a correction.** With `scope_resolver=None`, every candidate has an undeterminable scope and no requester, so both counts equal the candidate count and a denying default would deny every recall on every default install. The predictable operator response to a gate returning zero memories on day one is to disable the gate, not to write a resolver. The pre-gate template already had this reasoning applied to it; applying it to one template and not the other two was inconsistent. All three ship at `observe`, the gate-status card renders "no scope resolver configured, scope and speaker detection are not meaningful" as its own state rather than letting it present as a denial, and the UI offers a one-click promotion to `deny` that is enabled only once recent recalls have carried a non-null `scope.id` and a non-null `requester.id_sha256`.

**Detection is not enforcement, and the difference is stated in the doc, the endpoint docstring and the UI helper text:** *if your memory service serves one tenant's facts to another tenant's query, Agent Control will refuse the recall and record it, and it will not have prevented the store from doing it.* Preventing that is the store's job, and a control plane that claimed otherwise would be claiming to own something it deliberately does not.

**Pre-gate facts.** `agents.memory_gate_first_seen_at` is set once, on the first memory evaluation for that agent, and returned on the config-shaped read the decorator already polls. A candidate whose `timestamp` predates it, or whose timestamp is missing, is `pre_gate: true` and counts into `recall.pre_gate_count`. This is the concrete answer to hole number three: a fact written before controls existed is not silently trusted, it is labelled, counted and deniable. The UI reports how many pre-gate facts were recalled this week and offers the same one-click promotion once the number stops mattering.

### 3.8 UI (question 8)

**Where.** A fourth tab on the agent detail page, after Controls, Monitor and Chat, labelled "Memory". Tab list at `ui/src/core/page-components/agent-detail/agent-detail.tsx:302`, shallow-push routing at `:274-297`. Route `/agents?id=<name>&tab=memory`.

**New files.**

```
ui/src/core/page-components/agent-detail/memory/memory-tab.tsx
ui/src/core/page-components/agent-detail/memory/gate-status-card.tsx
ui/src/core/page-components/agent-detail/memory/decisions-table.tsx
ui/src/core/page-components/agent-detail/memory/quarantine-table.tsx
ui/src/core/page-components/agent-detail/memory/quarantine-detail.tsx
ui/src/core/page-components/agent-detail/memory/release-modal.tsx
ui/src/core/page-components/agent-detail/memory/memory-tab.module.css
ui/src/core/hooks/query-hooks/use-memory-gate-status.ts
ui/src/core/hooks/query-hooks/use-memory-quarantine.ts
ui/src/core/hooks/query-hooks/use-memory-quarantine-action.ts
```

Hooks follow `use-teams.ts`: exported `*QueryKey` helpers, `useQuery` unwrapping `{data, error}` and throwing, `retry: (n, error) => !isNotFoundError(error) && n < 1`. Client methods go into `ui/src/core/api/client.ts`.

**Three panels, top to bottom.**

*Gate status.* Guarded, write-only, recall-only, refusing, or unguarded, per section 3.5, with the last decision timestamp on each path. Two conditional alerts sit here: one when body storage is off, naming the env var; one when release is off, naming the same env var and saying that hold and discard still work. When `scope.resolver_present` has been false on recent decisions, a third line explains that scope and speaker detection are inert.

*Recent decisions.* A table over the existing event query API filtered to `applies_to in ("memory_write","memory_recall")`. Columns: time, path, action, control, scope kind, proposer kind, provenance status, in-body attribution count, fact hash prefix, candidates and dropped for recalls, and an "unevaluated" badge when `unevaluated_reason` is set. **No fact text, because the events do not contain any.** Deep link into the existing trace view by `trace_id`. One line under the table: these are events agents reported, and any key in the namespace can report one.

*Held for review.* The queue. Rows carry scope, proposer kind, claimed-source status, in-body attribution count, deciding control, reason, age, repeat count and hash prefix. Filters on state, scope kind and control. When a scope has hit the flood ceiling (section 3.9), a banner names the scope.

**How an operator reviews a held fact.** The row expands to `quarantine-detail.tsx`. The text is **not** rendered on expand. It sits behind an explicit "Reveal fact text" button whose helper line reads: this text was written by whoever was talking to the agent and has not been evaluated as safe to read. Reveal calls `GET /api/v1/memory/quarantine/{id}/body`, which is a separate route at `MEMORY_QUARANTINE_READ`, records `revealed_at` and `revealed_by_hash`, and returns 404 when the body was never stored. One write on a read, and it is worth it: "who looked at the credential we intercepted" is a question a regulated customer will ask.

**Two actions, and the asymmetry is deliberate.**

- **Discard** is the primary button. It sets `state='discarded'` and nothing else happens. No confirm modal, because discarding a held fact is the safe direction and a modal on the safe action trains people to click through the modal on the unsafe one.
- **Release** opens `release-modal.tsx`, which shows the fact text (revealing it if not yet revealed, and recording that), the deciding control, and the most important sentence in this UI: **releasing does not write this fact. It permits the agent to write it once, within the next 24 hours, if the agent proposes it again, and only if the same control still holds it.** Confirm sets `state='released'`, `release_expires_at`, `released_against_control_id` and `released_at`.

That wording is the mechanism, not softening. Agent Control has no memory store, so "release" cannot mean "insert". Section 3.9 defines exactly what it does mean and what it deliberately cannot override.

When `MEMORY_RELEASE_ALLOWED` is false, the Release button is disabled with a tooltip naming the env var, and the API returns 403 regardless of the button.

**Rendering untrusted text.** Phase 3 of the orchestration plan settled this and it is inherited unchanged: plain text, `white-space: pre-wrap`, no markdown renderer, no HTML string assembly, no `dangerouslySetInnerHTML`, no sanitizer because none is needed when nothing renders as markup (`docs/plans/orchestration-plan.md:688`). Concretely:

- Fact text renders inside a `<pre>` as a React text node.
- Bodies arrive already marker-neutralized and attribution-neutralized from the write path, and the display layer neutralizes again for pre-gate and imported rows, so a fact cannot impersonate a system marker in the operator's own console.
- Display names, scope ids and claimed sources are bounded at 128 characters on render, matching `MAX_DISPLAY_NAME_CHARS` (`_sanitize.py:23`).
- A CI grep bans `dangerouslySetInnerHTML`, `innerHTML` and `react-markdown` under the memory directory.

The escalation this prevents is specific: the operator console's session cookie is a valid credential on this API, because `_validate_api_key` falls back to the session JWT (`server/src/agent_control_server/auth.py:196-205`). Stored XSS in a queue of attacker-authored text renders straight to ADMIN.

**Non-admin.** The queue, the reveal route and release are ADMIN. A non-admin sees the gate status and the decisions table, which need only `OBSERVABILITY_READ`, and the queue panel renders an inline alert reading "Requires an admin key" via a new `isForbiddenError` in `ui/src/core/api/errors.ts`, beside the existing `isNotFoundError` (`:80`) and `getErrorStatus` (`:72`).

### 3.9 Release, and what an admission is allowed to override

This section exists because release is the only mechanism in the design that turns a refusal into a write, and the first revision made it a full bypass.

**The problem, reproduced.** `MEMORY_QUARANTINE_WRITE: AccessLevel.ADMIN` is not a boundary when `api_key_enabled` is `False`, which is the default (`config.py:37`) and installs `NoAuthProvider` (`auth_framework/config.py:220-226`). Checking the admission **before** evaluating, as the first revision specified, then made the sequence: propose a credential-bearing fact, watch it be held, call `:release` unauthenticated, propose the same hash again, admitted with no control evaluating it. And `released_by_hash` uses `hash_caller_id`, whose own module docstring says that under the default provider "for UI traffic every caller therefore hashes to the same value" (`services/caller_identity.py:11-16`). Full bypass with an audit trail that says an admin approved it. An audit record that actively lies is worse than none.

**Four changes, and each closes a different half of it.**

1. **Release is gated at startup, like body storage.** `check_memory_startup_requirements` sets `MEMORY_RELEASE_ALLOWED = False` on the same condition as `MEMORY_QUARANTINE_BODY_ALLOWED`. `:release` returns `403 MEMORY_RELEASE_DISABLED` naming `AGENT_CONTROL_MEMORY_ALLOW_INSECURE_LOCAL_DEV`. Hold, list, detail and discard keep working, so the feature still demos on a laptop and the queue is still useful there; the one verb that converts a deny into an allow does not exist on a server where everyone is an admin.
2. **The admission is checked after evaluation, never before.** `_gate.py` evaluates first, always. Only if the verdict is `steer` does it consult the admissions cache. An admission cannot rescue a `deny`, cannot rescue a `result.errors`, cannot rescue a timeout, and cannot rescue any of section 3.4's unevaluated refusals. This is the change that matters most, because it means the worst a release can do is skip a hold, and it removes the "check something cheap, then skip the check" shape entirely.
3. **An admission is bound to the control that held the fact.** The row stores `released_against_control_id` and `released_at`. The admission applies only when the current `steer` verdict names the same control id. A different control holding the same fact is a different decision and gets its own review.
4. **An admission is invalidated by a policy change.** The admissions response carries `control_set_fingerprint`, a stable hash the server computes over the ids and version numbers of the agent's applicable memory controls at release time. The gate compares it to the fingerprint of the control set it just evaluated against, and declines the admission on a mismatch, logging why. So tightening a control on Thursday invalidates Tuesday's outstanding releases, which is the same property the recall gate gives for stored facts and it should not be weaker for pending ones.

**What release therefore means, precisely:** the next `propose_fact` carrying that hash in that scope, within 24 hours, whose evaluation produces a `steer` from the same control under an unchanged control set, is admitted once and the admission is consumed. Anything else is refused normally. If the runtime never proposes it again the row expires to `state='expired'` and the UI says "released but never written back".

**`released_by_hash` is labelled "credential", not "user", everywhere it appears**, matching `agent_config_versions.changed_by_hash` and for the reason `caller_identity.py`'s docstring already gives.

### 3.10 Bounding the recall path

The recall gate sits in front of a model call under a 400ms budget, and its input is the contents of a store this design explicitly cannot limit. `MAX_FACT_CHARS` is enforced at the write gate only, so pre-gate facts, facts from an ungated writer and imported facts have no size at all, and the candidate count has no ceiling.

Left unbounded, an attacker who can post in a shared channel controls both the number and the size of candidates every later recall returns. Flood a scope to thousands of facts and every recall for every colleague evaluates megabytes, the timeout fires, section 3.4 returns zero memories, and flooding a scope reliably disables memory for everyone in it while burning CPU on every turn. With `execution: "server"` controls it is also an amplifier into `/evaluate`, which is AUTHENTICATED.

**The bounds, applied in `_payload.py` before anything is evaluated:**

- `max_recall_candidates`, default 50. Overflow is dropped, counted in `recall.truncated_count`.
- `max_recall_total_chars`, default 64000, applied across the batch after per-candidate capping.
- Per-candidate text capped at `MAX_FACT_CHARS` (32000), setting that candidate's `truncated` flag.
- `recall.over_budget: bool` is set when either batch bound bit, so a control can deny outright rather than reason about a partial view. The shipped `memory-recall-over-budget` template denies on it, because a truncated view of a flooded scope is a view an attacker chose.
- Server side, `/evaluate` rejects a `memory_recall` step whose serialized body exceeds `max_recall_body_bytes` (default 256 KiB) with `400 MEMORY_PAYLOAD_TOO_LARGE`, so a server-executed recall control cannot be used as an amplifier.

Gate metrics count timeouts, over-budget batches and structural drops per scope, which is what makes a flood visible rather than merely survivable.

---

## 4. What is still unguarded after each phase

This section is here because a partial rollout is the state a deployment sits in longest, and because "we shipped memory controls" is a sentence somebody will say after Phase 1.

**After Phase 1** (models, event widening, payload contract, `/evaluate` branch):

- **Nothing is gated.** No SDK code calls the memory path yet. Every fact still enters and leaves the store exactly as it does today.
- What is fixed: a memory step, if hand-constructed by a caller, evaluates correctly and is recorded as `memory_write` or `memory_recall` rather than as an LLM call, and no fact text can reach the event store.
- What an operator can honestly say: memory decisions have a vocabulary and a redaction guarantee. Not that any memory decision is being made.

**After Phase 2** (quarantine store, endpoints, operations, startup gates): still nothing is gated. There is now somewhere for a held fact to live and an API to review it. No agent produces one.

**After Phase 3** (framework-neutral gate): a runtime that adds two call sites is fully covered on both paths. A runtime that does not is not covered at all. ADK deployments are in the second group.

**After Phase 4** (ADK decorator): an ADK deployment that wraps its `BaseMemoryService` is covered on both paths, subject to five standing limits that no phase removes:

1. **A recall that does not go through `search_memory` is not gated.** A custom tool that reads the store directly, a separately wired RAG retriever, anything bypassing `BaseMemoryService`. The gate does not detect this and cannot.
2. **A write that does not go through `add_session_to_memory` is not gated.** Another agent, an import, an ops script.
3. **For a Memory Bank deployment, the write gate sees material, not facts** (A3). Only recall sees facts.
4. **`save_artifact` is not covered at all.** It goes through none of the plugin callbacks and `_warn_on_artifact_service` (`plugin.py:811`) already says so out loud. An agent can write bytes no control sees.
5. **Nothing re-evaluates the store at rest.** Tightened controls bite on the next recall, not on the store.

**After Phase 5**: an operator can review and release. **After Phase 6**: turning the feature on is enabling templates rather than authoring conditions, and a fixture bench holds the templates to a floor.

`gate_state` in the UI reports whether the gate has **ever** been called, which is not the same as whether it is called **every** time, and no honest mechanism gives us the second. The UI reports what was seen, never what was not.

---

## 5. Operations

Added to `Operation` (`server/src/agent_control_server/auth_framework/core.py:34`):

```python
    # A held fact is the exact content a control just refused: a credential, a
    # forged attribution, an injected instruction. Submit is separated from
    # read because the agent process must be able to deposit one and must
    # never be able to enumerate the archive.
    MEMORY_QUARANTINE_SUBMIT = "memory_quarantine.submit"
    MEMORY_QUARANTINE_READ = "memory_quarantine.read"
    MEMORY_QUARANTINE_WRITE = "memory_quarantine.write"
    # Read-only list of one-time admissions an admin granted. Held apart from
    # the quarantine read tier because the agent process polls it and must not
    # thereby gain the archive.
    MEMORY_ADMISSIONS_READ = "memory_admissions.read"
```

Added to `DEFAULT_OPERATION_ACCESS` (`auth_framework/providers/header.py:38`). Every member must be present or `HeaderAuthProvider.authorize` raises `RuntimeError` on the first request, and `server/tests/test_auth_framework.py` already asserts full enum coverage, so a missing entry fails CI rather than production.

```python
    # Write-only deposit by an agent process, same tier as the evaluation it
    # follows (RUNTIME_USE is AUTHENTICATED). Rate limited per principal and
    # ceiling-capped per scope in the service.
    Operation.MEMORY_QUARANTINE_SUBMIT: AccessLevel.AUTHENTICATED,
    # The archive holds refused content. Same tier as CONTROLS_CREATE, and
    # note this tier only binds when credential enforcement is on, which is
    # why the body is not stored at all when it is off. See
    # check_memory_startup_requirements.
    Operation.MEMORY_QUARANTINE_READ: AccessLevel.ADMIN,
    # Releasing a held fact permits an agent to write something a control
    # held. Strictly a policy override, strictly ADMIN, and additionally
    # refused outright when the ADMIN tier is not a real boundary.
    Operation.MEMORY_QUARANTINE_WRITE: AccessLevel.ADMIN,
    # Polled by every agent process on the refresh loop. Returns hashes,
    # scope ids, control bindings and a fingerprint. Never bodies.
    Operation.MEMORY_ADMISSIONS_READ: AccessLevel.AUTHENTICATED,
```

**Why four and not two.** Folding submit into read would put an ADMIN key in every agent process, which is the mistake `AGENT_CONFIGS_READ` was set to AUTHENTICATED to avoid. Folding admissions into `RUNTIME_USE` would grant "which refused facts did an admin release" to anything that can evaluate, which is a surprise hiding inside a name. Folding release into read is the genuinely tempting fold and it is wrong for the ordinary reason: reading an archive and overriding a control decision are different privileges even when the same people hold both.

**The ADMIN tier is necessary and not sufficient**, which is the lesson from section 3.9. `MEMORY_QUARANTINE_WRITE: ADMIN` plus `MEMORY_RELEASE_ALLOWED` plus the post-evaluation, control-bound, fingerprint-checked admission are four independent things, and the design needs all four because the first is inert on a default server.

Nothing goes into `RUNTIME_TOKEN_BOUND_OPERATIONS` (`auth_framework/config.py:80`). That tuple installs the runtime provider **instead of** the default authorizer for those operations for every caller, so a deployment running `AGENT_CONTROL_RUNTIME_AUTH_MODE=jwt` would reject an ordinary API key and break every standalone SDK agent, which have no runtime token.

---

## 6. Model widenings

All additive, all in `models/src/agent_control_models/`, none requiring a migration.

**`observability.py`:**

```python
type AppliesTo = Literal["llm_call", "tool_call", "memory_write", "memory_recall"]
```

Replacing the inline Literal at `:93` and `:313`. A named alias rather than two inline Literals, because they must never drift.

**`agent.py`:**

```python
STEP_TYPE_MEMORY = "memory"
MEMORY_STEP_NAME_WRITE = "memory.write"
MEMORY_STEP_NAME_RECALL = "memory.recall"
```

`BUILTIN_STEP_TYPES` (`:16`) is **not** widened. It is a `tuple[str, str]` used for documentation and defaults, and widening it would be the beginning of treating step types as closed, which is the property section 2.1 depends on. A comment says so at the definition.

`Step.validate_builtin_types` (`:170-175`) gains a memory branch requiring `input` to be a string, matching the tool branch's requirement that tool input be an object. Cheap, and it catches the integration that passes a dict of the whole session.

**`memory.py`, new:** `MemoryStepContext`, `MemoryScopeRef`, `MemoryProposer`, `MemoryProvenance`, `MemoryFactRef`, `MemoryTurnRef`, `MemoryRequesterRef`, `MemoryRecallSummary`, `MemoryCandidate`, plus the quarantine and admission wire models in section 9. Re-exported from `models/src/agent_control_models/__init__.py`.

**`errors.py`:**

```
MEMORY_PAYLOAD_INVALID        # 400, the memory context block is missing or malformed
MEMORY_PAYLOAD_TOO_LARGE      # 400, a recall step body over max_recall_body_bytes
MEMORY_QUARANTINE_NOT_FOUND   # 404
MEMORY_QUARANTINE_STATE       # 409, release of a discarded row, discard of a consumed one
MEMORY_BODY_UNAVAILABLE       # 404 on the body route when it was never stored
MEMORY_RELEASE_DISABLED       # 403, release refused because ADMIN is not a boundary here
```

`MEMORY_PAYLOAD_INVALID` and `MEMORY_PAYLOAD_TOO_LARGE` go in the validation block beside `INVALID_CONFIG` (`:95`). `VALIDATION_ERROR` (`:94`), `AGENT_NOT_FOUND` (`:61`) and `QUOTA_EXCEEDED` (`:104`) already exist and are reused, the last one for both the submit rate limit and the per-scope ceiling.

**SDK Literals that must widen or the memory path cannot compile:**

- `evaluate_controls(..., step_type: Literal["tool", "llm"])` (`sdks/python/src/agent_control/evaluation.py:524`)
- `_evaluate_and_enforce(..., step_type: Literal["tool", "llm"])` (`integrations/_core.py:55`)
- `_safe_context(..., step_type: Literal["llm", "tool"])` (`google_adk/plugin.py:777`)

All three become `Literal["tool", "llm", "memory"]`. `evaluate_controls`'s `default_value` line (`evaluation.py:544`) also needs a memory branch: `{}` for tool, `""` for everything else is currently right by accident and should be right on purpose.

**One new field on `_StateContainer`** (`sdks/python/src/agent_control/_state.py:38`): `controls_published_at: float | None = None`, set only inside `_publish_server_controls` on a non-`None` publish (`__init__.py:194-204`). It is the freshness anchor for section 3.4 check 1, and it is deliberately a monotonic clock reading rather than a wall clock so a clock adjustment cannot make a stale cache look fresh.

---

## 7. SDK

### 7.1 The burst buffer

Borrowed from QM and sized identically: extraction fires once per burst, after **180 seconds of quiet or 10 turns**, whichever first. Both configurable, defaults stated in the docs.

`_buffer.py` holds:

- A per-scope pending list of **snapshots**, never framework objects (section 7.5), bounded at `max_pending_facts` (default 200). Overflow drops oldest with a warning, because an unbounded buffer in a long-lived agent process is a memory leak wearing a feature's name.
- A dedup LRU of 1024 `(scope_id, sha256)` entries per process, feeding `fact.repeat_count` and `fact.duplicate_of_sha256`.
- A denial backoff: after `repeat_denial_threshold` (default 3) refusals of the same hash in the same scope, that hash is not proposed again for `repeat_backoff_seconds` (default 3600). A control in a deny loop against an agent in a propose loop is a denial-of-service against the control plane that shows up in the customer's own logs as their agent hammering their own server. Note what this does **not** cover: an attacker proposing distinct facts defeats a hash-keyed backoff entirely, which is why the server-side ceiling in section 9 exists and is not optional.
- A dead-letter list, bounded, holding facts refused for transport reasons after their retries. Readable via `agent_control.memory.dead_letter()` for a developer, never sent anywhere.

**Flush is off the response path**, on a background task, the same way `_policy_refresh_worker` (`__init__.py:294`) already runs. A write refusal never delays a turn.

**A halted turn's buffer is dropped, not flushed**, and section 10 covers why.

### 7.2 The gate

`_gate.py` builds the payload and evaluates, following section 3.4 exactly:

1. Check policy freshness against `state.controls_published_at`. Unset or stale is a refusal with `unevaluated_reason="stale_policy"` and no evaluation is attempted.
2. Evaluate. **Write always POSTs** `/api/v1/evaluate` with the constructed `EvaluationRequest`, bypassing the prefilter. Recall goes through `check_evaluation_with_local` for latency.
3. Check that a control ran: `len(matches) + len(non_matches) + len(errors) > 0`. Zero is `unevaluated_reason="no_applicable_controls"` and a refusal unless `allow_when_no_memory_controls`.
4. Map the verdict. Any `deny` or `is_safe=False`: refuse. Any `steer`: consult admissions (section 3.9), and if no admission applies, submit to quarantine and return `MemoryWriteHeld`. `result.errors` non-empty, timeout or transport failure: refuse on write, empty on recall.
5. Otherwise admit.

It does **not** go through `_evaluate_and_enforce`. That helper raises `ControlViolationError` and `ControlSteerError`, which the ADK plugin translates into blocked model responses and injected steering guidance. Neither is right for a memory decision: there is no model response to block and no request to steer. Reusing it would mean catching the exceptions it raises purely to discard the framework semantics attached to them. A comment at the top of `_gate.py` says which helper it deliberately does not use and why.

### 7.3 Provenance and body neutralization

`_provenance.py` exports two functions and one constant set.

```python
def neutralize_body(text: str, *, runtime_authored: bool) -> tuple[str, BodySignals]: ...
def normalize_claimed_source(text: str | None, *, runtime_authored: bool) -> tuple[str | None, str]: ...
```

`BodySignals` is `{attribution_claims: int, attribution_verified: bool, marker_forgeries: int}` and feeds the three `fact.body_*` payload fields.

Rules, all deterministic, no model in the loop, no configuration:

- `[agent-control:` in text the runtime did not author is neutralized to the non-breaking-hyphen form, exactly as `_sanitize.py:18-21` does, and counted.
- An attribution shape in text the runtime did not author is rewritten to `[claimed source: X]` and counted. A runtime-authored attribution, marked as such by the caller, is left alone and sets `attribution_verified=True`.
- The metadata `claimed_source` field gets the corresponding treatment and returns `"verified"`, `"unverified"`, `"forged"` or `"absent"`.

The module docstring inherits `_sanitize.py`'s framing verbatim in spirit: a suffix anyone can type is not a provenance boundary, which is why controls key on `context.agent_control.memory.*` and why the text form is rewritten rather than trusted.

**The same functions run over recall candidates**, so a pre-gate fact or one written by an ungated writer gets its in-body attribution counted and neutralized on the way to the model, not merely on the way in. This is the recall half of section 2.7 and it is the only reason the defence covers facts that predate the gate.

**The speaker's-notebook property, without becoming a datastore.** QM writes a shared-room fact into the speaker's personal notebook tagged with the room. Agent Control cannot write anything, so it does the next useful thing: `provenance.speaker_scope_id` and `provenance.speaker_attributed` are required fields, and the shipped `memory-write-requires-attribution` template holds a `shared` or `org` write whose `speaker_attributed` is false. The product behaviour becomes a control the operator enforces, on a runtime that must supply the attribution to pass it. That is the boundary held and the property kept.

### 7.4 Admissions

`_admissions.py` fetches `GET /api/v1/memory/admissions` on the existing policy refresh tick, **in its own error boundary**, following the rule the system-prompts plan established for exactly this hazard: the loop body must not let a low-value new endpoint's failure stop control delivery.

```
1. try: controls = fetch() / except: log; continue        (unchanged, __init__.py:301-306)
2. _publish_server_controls(controls)                      (unchanged, sets controls_published_at)
3. try: admissions = fetch_admissions() / except: log; keep previous
```

Nothing in step 3 runs before step 2 and step 3 never `continue`s. An SDK test makes the admissions fetch raise and asserts controls were still published, and `controls_published_at` still advanced, that iteration.

An admission is `{scope_id, fact_sha256, quarantine_id, control_id, control_set_fingerprint, expires_at}`. The gate checks it **after** evaluating and only against a `steer` from `control_id` under a matching fingerprint (section 3.9), then reports consumption via `POST /api/v1/memory/quarantine/{id}:consume`. Admissions are one-shot in the process and one-shot on the server; a race between two processes resolves at the server, and the loser evaluates normally, which is to say the fact stays held.

### 7.5 The ADK decorator

`memory_service.py`, everything ADK-specific between two `Wire` markers, mirroring `adk_executor_client.py`'s structure and its module docstring's honesty about what is and is not verified. It imports `google.adk` lazily inside a `try`, raising the same `RuntimeError` the plugin raises when the extra is missing (`plugin.py:37-41`).

`add_session_to_memory(session)`:

1. **Snapshot.** Extract the new event text since the last commit for this session, marker- and attribution-neutralize it, record the event ids, and hash it. **The `Session` object itself is never retained.**
2. Resolve scope and requester via `scope_resolver`, or fall back to unknown-and-untrusted.
3. Enqueue the snapshot on the burst buffer. Return. The caller is not blocked.
4. On flush: build the payload from the snapshot, run the gate, and on `admitted` commit **only the snapshotted content**.

**Step 1 is a snapshot for a security reason, not a hygiene one.** Holding a live `Session` across a buffer window of up to 180 seconds means the controls evaluate the text at enqueue time and the inner service commits whatever the session contains at flush time. Everything accumulated in that window enters shared memory unevaluated, including turns added after a halt, and including content added by somebody who has worked out that a quiet period triggers the flush. It also breaks the halt guarantee: dropping an invocation's pending entries on halt achieves nothing if a later invocation's flush commits a session object that still contains the halted invocation's events.

Committing only the snapshot needs `inner.add_session_to_memory` to accept something we constructed. Three cases, and the third is the honest one:

- If a projection restricted to the snapshotted event ids is constructible, build one and pass it.
- If the inner service accepts nothing but the live object, **the decorator refuses to buffer for it and gates synchronously on the caller's thread**, which writes tolerate. `gate_writes` still works; the burst buffer does not apply. The decorator logs this at attach time naming the inner class.
- Whether case one or case two applies is **A6** and Phase 0 answers it.

Step 4 is also where A3 bites. For an inner service that extracts remotely, what we admitted is the material, not the facts. The decorator logs that distinction once per process at attach time and `granularity` on the payload says `"session"` so a control can tell.

`search_memory(*, app_name, user_id, query)`:

1. `response = await inner.search_memory(...)`. An exception here propagates unchanged.
2. Bound the batch per section 3.10.
3. Neutralize each candidate body and compute its structural signals.
4. Run the structural pass if `filter_structural`, counting drops.
5. Build the recall payload; text joined into `Step.output`, per-candidate metadata in the context block.
6. Run the gate with `recall_timeout_ms`.
7. Return a `SearchMemoryResponse` containing only admitted entries, in the original order, with neutralized bodies.

**Order is preserved and bodies are only ever weakened.** The returned response is the input filtered and neutralized, never reordered and never augmented.

---

## 8. Schema

New migration `server/alembic/versions/<rev>_memory_quarantine.py`, `down_revision = "f4c7a2b9e310"`. That is the current head: walking every `down_revision` under `server/alembic/versions/`, `f4c7a2b9e310_agent_session_nudges_and_halts` is the only revision no file names as its parent. Confirm with `alembic heads` before writing the file anyway; `server/tests/test_alembic_single_head.py` guards a branched head.

**There is no fact table, and the migration's module docstring says so in words**, with the reason, so the next person who wants one finds the argument before they write it. Agent Control gates a store the runtime owns. A table of admitted facts would be that store, arriving by the back door, with no eviction policy, no consistency story with the real store, and a second place for a customer's data to live.

### `memory_quarantine`

```
namespace_key             varchar(255)  NOT NULL  server_default 'default'
quarantine_id             varchar(36)   NOT NULL
agent_name                varchar(255)  NOT NULL
scope_kind                varchar(16)   NOT NULL
scope_id                  varchar(255)  NOT NULL
fact_sha256               varchar(64)   NOT NULL
fact_chars                integer       NOT NULL
body                      text          NULL      -- NULL when redacted; never NULL and empty
body_redacted_reason      varchar(32)   NULL      -- 'insecure_auth' | 'oversize' | 'operator'
proposer_kind             varchar(16)   NOT NULL
proposer_id_sha256        varchar(64)   NULL
provenance_basis          varchar(16)   NOT NULL  server_default 'unknown'
claimed_source            varchar(255)  NULL      -- normalized, never the forged form
claimed_source_status     varchar(16)   NOT NULL  server_default 'absent'
body_attribution_claims   integer       NOT NULL  server_default 0
control_id                integer       NULL      -- deciding control; NULL for structural refusals
control_name              varchar(255)  NULL
reason                    text          NULL      -- steering_context.message; never enters a fact
trace_id                  varchar(64)   NULL
state                     varchar(16)   NOT NULL  server_default 'held'
repeat_count              integer       NOT NULL  server_default 1
denial_count              integer       NOT NULL  server_default 0
released_by_hash          varchar(64)   NULL
released_at               timestamptz   NULL
released_against_control_id integer     NULL
release_fingerprint       varchar(64)   NULL
release_expires_at        timestamptz   NULL
revealed_at               timestamptz   NULL
revealed_by_hash          varchar(64)   NULL
created_at                timestamptz   NOT NULL  server_default CURRENT_TIMESTAMP
updated_at                timestamptz   NOT NULL  server_default CURRENT_TIMESTAMP, onupdate CURRENT_TIMESTAMP

PRIMARY KEY (namespace_key, quarantine_id)                       -- memory_quarantine_pkey
FOREIGN KEY (namespace_key, agent_name)
    REFERENCES agents (namespace_key, name) ON DELETE CASCADE    -- memory_quarantine_agent_fkey
UNIQUE (namespace_key, agent_name, scope_id, fact_sha256)
    WHERE state = 'held'                                         -- uq_memory_quarantine_held
INDEX (namespace_key, agent_name, state, created_at DESC)        -- idx_memory_quarantine_queue
INDEX (namespace_key, agent_name, fact_sha256)                   -- idx_memory_quarantine_hash
INDEX (namespace_key, agent_name, scope_id, state)               -- idx_memory_quarantine_scope
CHECK (state IN ('held','released','discarded','expired','consumed'))
CHECK (scope_kind IN ('private','shared','org','unknown'))
CHECK (claimed_source_status IN ('verified','unverified','forged','absent'))
CHECK (provenance_basis IN ('observed','inferred','unknown'))
CHECK (body IS NULL OR char_length(body) <= 32000)               -- ck_memory_quarantine_body_max
CHECK (fact_chars >= 0)
CHECK (body IS NOT NULL OR body_redacted_reason IS NOT NULL)     -- ck_memory_quarantine_body_reason
CHECK (state <> 'released' OR released_against_control_id IS NOT NULL)
                                                                 -- ck_memory_quarantine_release_bound
```

Composite natural primary key leading with `namespace_key`, matching `control_execution_events` (`models.py:810-814`) and `AgentRuntime` (`:468-478`). Foreign key to `agents` so deleting the agent takes its held facts with it, and because the agent row is the tenancy anchor everywhere else in this schema.

**The partial unique index is the dedup mechanism and it is load-bearing.** The same fact proposed forty times in a shared channel produces one held row with `repeat_count = 40`, not forty rows. Partial on `state = 'held'` so a fact discarded last month and proposed again gets a fresh row rather than a constraint violation, which is what an operator wants: they discarded it once, they should be asked again rather than have it silently suppressed.

**`denial_count` is the server-side denial history**, incremented on every denied proposal of that hash in that scope even when no row is held, and returned on the admissions poll as `fact.prior_denials`. It lives here rather than only in the SDK's in-process LRU because an in-process counter reads zero on exactly the reboot an attacker would provoke, and `prior_denials` is a field controls are meant to threshold on.

**`ck_memory_quarantine_release_bound` enforces section 3.9 at the storage layer.** A row cannot be `released` without naming the control it was released against, so a code path that forgets to set it fails loudly instead of producing an unbounded admission.

**`body` is capped at 32000 characters** in the Pydantic model and again here, matching the `agent_configs` precedent. A fact longer than that is refused at the gate, not truncated into the archive.

**`consumed` is a state, not a deletion.** When the decorator uses an admission it reports back and the row moves `released -> consumed`. "An admin released this and the agent then wrote it" is the whole point of the release mechanism, and deleting the row would destroy it.

**`released_by_hash` and `revealed_by_hash` identify a credential, not a person**, per `services/caller_identity.py`'s own docstring, and every UI column is labelled "credential".

### One column on `agents`

```
memory_gate_first_seen_at   timestamptz  NULL
```

Written once, on the first memory evaluation for that agent, by the evaluation path under the row lock. It anchors `recall.pre_gate_count` (section 3.7) and the gate-status card (section 3.5). One nullable timestamp on a table that already exists, rather than a scan of the event store on every page load.

**No `memory_gate_last_seen_at`.** That would be a write per agent per recall, forever, for a freshness indicator. The last decision timestamp comes from the event stream, which is already being written.

---

## 9. Endpoints

New router `server/src/agent_control_server/endpoints/memory.py`, registered in `main.py`. Service `server/src/agent_control_server/services/memory_quarantine.py`. Wire models `models/src/agent_control_models/memory.py`.

```python
# POST /api/v1/memory/quarantine
async def submit_quarantine(
    request: SubmitQuarantineRequest,
    db: AsyncSession = Depends(get_async_db),
    principal: Principal = Depends(require_operation(Operation.MEMORY_QUARANTINE_SUBMIT)),
) -> SubmitQuarantineResponse: ...
```

`SubmitQuarantineRequest` carries `agent_name`, the full `MemoryStepContext`, `body`, `control_id`, `control_name`, `reason` and `trace_id`. The handler resolves the agent through `_get_agent_or_404`, computes `sha256(body)` itself and **rejects a mismatch with the submitted `fact.sha256`** as `400 VALIDATION_ERROR`, drops `body` to `NULL` with `body_redacted_reason='insecure_auth'` when `MEMORY_QUARANTINE_BODY_ALLOWED` is false, and upserts on the partial unique index incrementing `repeat_count`.

**Two independent limits, because they stop different things.**

- **Rate**, per principal, reusing `services/turn_quota.py`'s `TurnQuota` (`:45`, `try_acquire` at `:66`). Default 60 submissions per minute, `429 QUOTA_EXCEEDED` past it. Stops a denial loop.
- **Ceiling**, per `(namespace_key, agent_name, scope_id)` held rows. Default 500, `429 QUOTA_EXCEEDED` past it, and the gate turns that into a discard-with-warning rather than a hold. Stops a flood. The rate limit alone permits roughly 86,000 rows per key per day, and burying three real held credentials under 86,000 generated ones is cheaper than getting a fact past the controls. `idx_memory_quarantine_scope` is what makes the count cheap. The UI banners the flooded scope, because a scope that hit its ceiling is a scope whose queue is no longer trustworthy as a review surface.

**Why the server computes the hash.** The submitting party is the audited party. A submitted hash that does not match the submitted body is either a bug or an attempt to make the queue disagree with the admissions list, and both should be a 400 rather than a row.

```
GET  /api/v1/memory/quarantine?agent_name=&state=&scope_kind=&cursor=&limit=
     -> ListQuarantineResponse                          [MEMORY_QUARANTINE_READ]
GET  /api/v1/memory/quarantine/{id}
     -> QuarantineDetail                                [MEMORY_QUARANTINE_READ]
GET  /api/v1/memory/quarantine/{id}/body
     -> QuarantineBody                                  [MEMORY_QUARANTINE_READ]
POST /api/v1/memory/quarantine/{id}:release
     body: { note: str | None, ttl_hours: int = 24 }    [MEMORY_QUARANTINE_WRITE]
POST /api/v1/memory/quarantine/{id}:discard
     body: { note: str | None }                         [MEMORY_QUARANTINE_WRITE]
POST /api/v1/memory/quarantine/{id}:consume
     body: { }                                          [MEMORY_QUARANTINE_SUBMIT]
GET  /api/v1/memory/admissions?agent_name=&since=
     -> ListAdmissionsResponse                          [MEMORY_ADMISSIONS_READ]
GET  /api/v1/agents/{agent_name}/memory-status
     -> MemoryGateStatus                                [AGENTS_READ]
```

`POST` with a verb suffix rather than `DELETE`, matching `POST /control-bindings/by-key:delete` (`endpoints/control_bindings.py:342-343`), because these calls carry bodies and bodies on `DELETE` get dropped by some proxies.

**The list route never returns `body`.** `QuarantineDetail` omits it too. The body has its own route so that reading it is a distinct request that can be logged, tiered and, in a future deployment, alerted on. A list endpoint that happened to include it would put refused credentials into every page load of the queue.

`GET .../body` returns `404 MEMORY_BODY_UNAVAILABLE` when the body was never stored, sets `revealed_at` and `revealed_by_hash` when it was, and its docstring states in words that the content is unevaluated, attacker-influenceable text.

`:release` returns `403 MEMORY_RELEASE_DISABLED` when `MEMORY_RELEASE_ALLOWED` is false (section 3.9), `409 MEMORY_QUARANTINE_STATE` on a row that is not `held`, and otherwise sets `state='released'`, `released_at`, `released_by_hash`, `released_against_control_id` from the row's `control_id`, `release_fingerprint` computed over the agent's current applicable memory control set, and `release_expires_at`.

`:discard` is idempotent on an already-discarded row and `409` on a `consumed` one, because discarding something the agent already wrote is a request the operator means differently and should be told so. It is **not** gated by `MEMORY_RELEASE_ALLOWED`: discarding is the safe direction and must work on a laptop.

`:consume` takes the **submit** operation, not write, because the agent process calls it and an agent process must not hold `MEMORY_QUARANTINE_WRITE`. It is `409` on a row that is not `released` and on one whose `release_expires_at` has passed.

`GET /api/v1/memory/admissions` returns `{scope_id, fact_sha256, quarantine_id, control_id, control_set_fingerprint, expires_at, prior_denials}` and **never a body**. It filters `namespace_key` from the Principal and `agent_name` from the query, and it is the one route in this design an ordinary agent key polls. `prior_denials` is joined from `denial_count` so the SDK's payload field survives a restart.

`GET /api/v1/agents/{agent_name}/memory-status` returns `{gate_state, write_last_seen_at, recall_last_seen_at, first_gated_at, body_storage_allowed, release_allowed, resolver_seen, held_count, released_count, recent_unevaluated_count, flooded_scopes}` at `AGENTS_READ`, which is AUTHENTICATED (`header.py:48`). Counts, not content.

**The `/evaluate` change.** `endpoints/evaluation.py` gains a memory branch: when `step.type == "memory"` it validates the context block into `MemoryStepContext` and returns `400 MEMORY_PAYLOAD_INVALID` on failure, rejects a `memory.recall` step over `max_recall_body_bytes` with `400 MEMORY_PAYLOAD_TOO_LARGE`, and sets `agents.memory_gate_first_seen_at` when null. Both under the existing `RUNTIME_USE` authorization, no new operation, no change to the response shape.

---

## 10. Edge cases

| Case | Behaviour |
|---|---|
| Control plane down at agent boot | **Refused, both paths.** `state.controls_published_at` is unset because `init()` swallowed the failure (`__init__.py:698-703`) and `_publish_server_controls(None)` published nothing (`:194-200`). The gate refuses on check 1 rather than reading the empty cache as "nothing applies". Logged once per interval with the reason. |
| Control cache stale but non-empty | Refused past `max_policy_staleness_seconds` (default 120). A cache from six hours ago is a policy from six hours ago and the write path has no reason to accept one. |
| Gate attached but no memory controls authored | Refused, both paths, with `unevaluated_reason="no_applicable_controls"`, unless `allow_when_no_memory_controls=True`. The UI renders this as its own gate state with a link to the templates, because it is the most likely first-run experience and it looks identical to a bug otherwise. |
| Fact proposed during a turn that is then halted | The burst buffer is keyed by invocation. On halt, that invocation's pending **snapshots** are dropped, not flushed. Because the buffer holds snapshots rather than live `Session` objects (7.5), a later invocation's flush cannot resurrect the halted turn's content. `turn.halted` is also on the payload for facts already in flight, so a control can deny them independently, and the shipped template does. Halt delivery already exists on this branch (`services/halts.py`). |
| The same fact proposed repeatedly | Four layers. In-process LRU produces `repeat_count` and `duplicate_of_sha256`. The partial unique index collapses held rows to one with an incremented count. `denial_count` on the row survives restarts and feeds `fact.prior_denials`. The client backoff stops re-proposal for an hour after three refusals. An operator sees "proposed 40 times" on one row. |
| Many *distinct* facts proposed to flood the queue | The hash-keyed backoff does nothing here and the design says so. The per-scope held ceiling (default 500) does: past it, submission is `429` and the gate discards with a warning rather than holding, and the UI banners the scope. Without the ceiling, 60/min buys roughly 86,000 rows a day and the queue stops being a review surface. |
| An enormous fact | `MAX_FACT_CHARS` is 32000, in the Pydantic model, in the check constraint, and in the SDK before the payload is built. `oversize_facts` defaults to `"refuse"`. Truncating and evaluating the prefix would repeat the file-inputs mistake exactly: a descriptor of part of a thing is not a verdict on the thing. `"truncate"` exists for a deployment that knowingly wants it, sets `fact.truncated=true`, and every shipped template denies on that flag. |
| An enormous *recall batch* | Bounded at 50 candidates and 64000 characters before the payload is built, per-candidate capped at `MAX_FACT_CHARS`, `recall.over_budget` set, and a shipped template denies on it. Server side, an oversized `memory_recall` body is `400 MEMORY_PAYLOAD_TOO_LARGE` so a server-executed control cannot be used as an amplifier into an AUTHENTICATED endpoint. |
| A fact containing a secret | Denied by the shipped `memory-write-no-credentials` regex template. The fact text never enters the event stream (3.6 allowlist), the quarantine body is `NULL` on a credential-less server, and `_selected_data_preview`'s key-based redaction is bypassed for bare strings, which is why suppression is by step type rather than by key. Nothing here claims regex catches every secret; it catches the shaped ones, and the design's value is that the fact does not then get archived where everyone can read it. |
| A fact whose attribution is forged **inside its own text** | Neutralized to `[claimed source: X]` by `neutralize_body`, counted in `fact.body_attribution_claims`, `body_attribution_verified` false, and the shipped `memory-body-attribution` template holds on it at both stages. This is the case the first revision missed entirely; see 2.7. |
| A fact whose *metadata* claimed source is forged | `normalize_claimed_source` sets status `"forged"` and `memory-forged-provenance` denies on it at both stages. |
| Recall returning facts written before controls existed | `agents.memory_gate_first_seen_at` anchors `pre_gate`. Every such candidate is labelled and counted. Their bodies are still neutralized, so a pre-gate fact carrying a forged in-body attribution is defanged on the way to the model even though nothing gated its write. The template starts at `observe`, because on day one of an existing store every candidate is pre-gate. **This is the answer to hole number three and it works because the gate is on recall.** |
| A fact referencing another scope | `recall.foreign_scope_count`, `observe` by default. Honestly a detection, not an enforcement: Agent Control cannot stop a store it does not own from serving the wrong tenant's rows. It can refuse the recall and record it. |
| One process, many end users, `user_id` confusion inside the store | `requester.id_sha256` plus per-candidate `speaker_matches_requester` gives `recall.speaker_mismatch_count`, and `memory-recall-speaker-mismatch` is the template. With `scope_resolver=None` this is undetectable and the docs say so in the same sentence that introduces the resolver. `namespace_key` is irrelevant to this case, which is why 2.8 exists. |
| No scope resolver configured | `scope.resolver_present=false`, every scope and speaker count maxes out, all three affected templates are at `observe`, and the gate-status card renders a dedicated explanation rather than letting it read as a denial. Promotion to `deny` is offered only once a non-null `scope.id` and `requester.id_sha256` have been seen. |
| The memory service being unavailable | The inner service's exception propagates unchanged, on both paths. Not reported as a control decision, no `on_violation_callback`, no quarantine row. Conflating a store outage with a policy refusal would teach operators to distrust denials, which is the one thing this product cannot afford. |
| Evaluation timing out | Write refuses at `write_timeout_ms` (2000). Recall empties at `recall_timeout_ms` (400). Different numbers because one is off the response path and one is in front of a model call. Both counted in gate metrics. |
| Session mutates between enqueue and flush | Impossible to commit: the buffer holds a snapshot and the flush commits only the snapshot. Where the inner service will not accept a projection, the decorator gates synchronously instead of buffering (7.5, A6). |
| Already-stored facts when a control is later tightened | **Not re-evaluated at rest, and this design does not pretend to.** The recall gate evaluates on every read, so the tightened control bites on the next recall with no store rewrite, no backfill and no enumeration of a store we do not own. Bulk re-evaluation is in section 15, rejected rather than deferred. |
| A released fact after the controls were tightened | The admission carries `control_set_fingerprint` from release time. A changed control set means the fingerprint mismatches and the admission is declined, logged, and the fact stays held. Tightening a control invalidates outstanding releases, which is the same property the recall gate gives for stored facts. |
| Release attempted on a credential-less server | `403 MEMORY_RELEASE_DISABLED` naming `AGENT_CONTROL_MEMORY_ALLOW_INSECURE_LOCAL_DEV`. Hold, list, detail and discard still work, so the queue still demos. The one verb that converts a deny into an allow does not exist where ADMIN is not a boundary. |
| An admission against a `deny` verdict | Never applies. Admissions are consulted only after evaluation and only against a `steer` from the recorded control id. A `deny` is a `deny` regardless of what an admin released. |
| Quarantine submission fails after a steer | The write degrades to a discard with a warning, never to a write. A fact can be proposed again; an unreviewed fact in shared memory cannot be un-recalled. |
| Release expires before the agent proposes again | Row moves to `expired` on read or on a nightly sweep. The UI says "released but never written back". Not an error state, and the audit trail keeps both facts: an admin allowed it, the agent did not take it. |
| Two admins releasing and discarding the same row | Last write is rejected. Both verbs require `state='held'` under `SELECT ... FOR UPDATE`, and the second returns `409 MEMORY_QUARANTINE_STATE` naming the current state. |
| A `json` control pointed at `input` on a memory step | The value would land in `metadata["errors"]` (`json/evaluator.py:397-400`) and in `condition_trace.message`. The section 3.6 allowlist discards both, in the SDK and again on ingest. No shipped memory template points a `json` constraint at `input`. |
| An old SDK sending the preview on a memory event | The server rebuilds memory-event metadata from the allowlist on ingest. Client-side redaction the server trusts is not redaction. |
| `AGENT_CONTROL_INCLUDE_RAW_SELECTED_DATA` enabled | Does not reopen the leak. The allowlist never copies `engine_selected_data`, and ingest applies it again. A test asserts both with the flag on. |
| Fabricated memory events | `OBSERVABILITY_WRITE` is AUTHENTICATED (`header.py:54`), so any key in the namespace can POST a `memory_write`/`allow` event. The decisions table renders what was reported and says so in one line. Pre-existing property of the event stream; named because a memory-decisions table is the first place somebody reads it as a security record. |
| Agent registered but no memory gate ever attached | `gate_state="unguarded"`, banner, link to the integration doc. The UI does **not** say "no memory in use", because it cannot know that, and saying it would be the most dangerous sentence in the product. |
| Agent deleted | `ON DELETE CASCADE` from `agents`. Held facts go with the agent, which is right: the agent row is the tenancy anchor everywhere else. |
| Fact proposed for an unknown agent | `404 AGENT_NOT_FOUND` via `_get_agent_or_404`, before any quarantine logic runs. |
| A control author writes a memory control before any memory step exists | It validates, saves and never fires, exactly like a control scoped to a tool name that does not exist. The Controls tab's existing "never matched" signal covers it and no special case is added. |

---

## 11. Testing

**Models** (`models/tests/test_memory.py`, mirroring `test_teams.py`): `MemoryStepContext` round trip, every enum's rejection of an unknown value, `schema_version` forward-compatibility rejection, the body length cap, the required-field defaults being the hostile ones (`trusted=false`, `basis="unknown"`, `claimed_source_status="absent"`, `readers=-1`, `body_attribution_verified=false`, `resolver_present=false`), `MemoryCandidate` list round trip, and the six new error codes.

**Engine** (`engine/tests/`): a control scoped to `step_types: ["memory"]` matches a memory step and does not match an `llm` step; a control with no `step_types` matches a memory step, which is the existing wildcard behaviour and must not regress; `step_name_regex: "^memory\\."` matches both names.

**Server:**

- `server/tests/test_memory_quarantine_endpoints.py`, mirroring `test_agent_runtimes_endpoints.py`: submit, list with cursor pagination, detail, body, release, discard, consume, every state transition and its 409, 404 on an unknown agent, upsert incrementing `repeat_count` rather than creating a second held row, `denial_count` incrementing without a held row.
- `server/tests/test_memory_startup_gates.py`: with the resolved default authorizer as `NoAuthProvider` and the override unset, a submit carrying a body stores `body IS NULL` with `body_redacted_reason='insecure_auth'`, `GET .../body` is `404`, **and `:release` is `403 MEMORY_RELEASE_DISABLED` while `:discard` still succeeds**. With the override set, both work.
- `server/tests/test_memory_evaluation_payload.py`: a memory step with no context block is `400 MEMORY_PAYLOAD_INVALID`; malformed, same; unknown high `schema_version`, same; an oversized recall body is `400 MEMORY_PAYLOAD_TOO_LARGE`; a valid block evaluates and sets `memory_gate_first_seen_at` exactly once.
- **`server/tests/test_memory_event_redaction.py`, the highest-value test in this list.** Ingest a `memory_write` event whose metadata carries `input`, `engine_selected_data`, `engine_selected_data_preview`, `errors`, `message` and `condition_trace.message`, each holding a distinct canary string, then read it back through `query_events` and assert **no canary survives**. Repeat for `memory_recall`. Repeat with `AGENT_CONTROL_INCLUDE_RAW_SELECTED_DATA=true`. Assert the allowlisted `metadata.memory.*` fields did survive, so the test fails on over-stripping too.
- `server/tests/test_memory_quota.py`: the 60/min rate limit returns `429 QUOTA_EXCEEDED`; the per-scope held ceiling returns `429` at 501 and `memory-status` reports the scope as flooded.
- `server/tests/test_memory_auth.py`, mirroring `test_controls_auth.py`: submit succeeds with a non-admin key; list, body, release and discard are 403 with one; admissions succeeds with one; and a case asserting all four new members are present in `DEFAULT_OPERATION_ACCESS`.
- `server/tests/test_memory_alembic_migration.py`, mirroring `test_agent_sessions_alembic_migration.py`: upgrade and downgrade, every constraint name including `uq_memory_quarantine_held`, `ck_memory_quarantine_body_reason` and `ck_memory_quarantine_release_bound`, cascade from `agents`.
- Concurrency: two overlapping `:release` calls yield one 200 and one 409. `SELECT ... FOR UPDATE` is a no-op on SQLite, so this needs the Postgres path in `server/tests/conftest.py` and must **skip** rather than pass vacuously when Postgres is unavailable.
- New cases in `server/tests/test_namespace_isolation.py` for all eight routes.

**SDK** (`sdks/python/tests/test_memory_gate.py`, new; `test_google_adk_plugin.py` extended):

- **The unevaluated-refusal tests, which are the ones that would have caught blocker 1.** With `state.server_controls = None` and `controls_published_at` unset, a write is refused and a recall returns zero memories. With a populated cache older than the staleness window, same. With a fresh cache but no applicable memory control, same, and setting `allow_when_no_memory_controls=True` flips both. A write with the server unreachable raises out of the forced POST and is refused, rather than short-circuiting through the prefilter.
- **The steer-never-authors tests.** A control returns `steer` with a `steering_context.message` that reads like a plausible fact, and separately with a control *name* that reads like one. Assert the quarantine body equals `neutralize(proposed_body)` byte for byte and that neither string appears in it. Run for `deny` and `observe` too.
- **The release-cannot-bypass tests.** A released hash is still denied by a `deny`-action control. An admission for control A does not apply to a `steer` from control B. An admission whose `control_set_fingerprint` no longer matches is declined and logged. An admission does not rescue a timeout, a transport error, or a stale-policy refusal.
- **The body-attribution tests.** A body containing `(said in #eng)` in runtime-unauthored text is neutralized to `[claimed source: #eng]`, `body_attribution_claims` is 1, `body_attribution_verified` is false, and the shipped template holds it. A runtime-authored attribution is untouched and verified. The same detector runs over recall candidates and neutralizes a pre-gate fact's body.
- **The buffer-snapshot test.** Enqueue from a session, mutate the session by appending events, flush, and assert the committed content contains none of the added events. Plus: 180-second quiet flush, 10-turn flush, halt drops the invocation's snapshots, overflow drops oldest, denial backoff stops re-proposal after three refusals.
- **The recall-bound tests.** 51 candidates truncates to 50 and sets `over_budget`; a batch over 64000 characters truncates and sets it; a single 100000-character candidate is capped and flagged.
- Fail-closed: transport error on write refuses; on recall returns zero and does not raise; `on_recall_unavailable="raise"` raises; timeouts behave as their column says; an inner-service exception propagates unchanged and produces no quarantine row.
- Recall filtering: order preserved, structural drops counted, a control `steer` drops the whole batch, and a test asserting the design does **not** claim per-candidate control filtering.
- Admissions: a matching hash admits once and only once; the admissions fetch raising does not stop controls being published or `controls_published_at` advancing that iteration.
- `map_applies_to` returns `memory_write` for `memory.write`, `memory_recall` for `memory.recall`, `memory_write` for an unrecognised memory step name, and is unchanged for `tool` and `llm`.

**The pinned ADK contract job.** The existing SDK tests inject hand-written fakes into `sys.modules["google.adk.*"]`, so everything above exercises this repo's fiction of ADK rather than ADK. `agent-system-prompts.md` section 10 already argues for a pinned contract job and this feature adds five facts to it, none guaranteed across versions:

1. `BaseMemoryService` exposes `add_session_to_memory` and `search_memory` with the A1 signatures.
2. `SearchMemoryResponse.memories` is a list of entries carrying `content`, `author` and `timestamp`.
3. `VertexAiMemoryBankService.add_session_to_memory` returns nothing enumerating the facts it extracted.
4. `preload_memory` writes recalled text into the request's instructions rather than into `contents`.
5. **A `Session`-shaped projection restricted to a chosen set of event ids is constructible and accepted by `add_session_to_memory`** (A6). This is the one that decides whether the burst buffer exists for ADK at all.

Fact 4 decides whether section 2.6's argument holds; if a future ADK moves preloaded memory into `contents`, the recall gate is still correct and only the framing needs correcting.

**UI**: `ui/tests/memory.spec.ts`, mirroring `agent-detail.spec.ts`: tab loads, each of the five gate states renders its own copy, the "no memory controls configured" state is distinguishable from "guarded", the decisions table filters, the queue paginates, reveal is a separate deliberate action, release shows the "does not write this fact" sentence, release is disabled with its tooltip when the startup gate is closed, discard has no modal, non-admin sees the forbidden alert on the queue and the decisions table normally, the redacted-body state renders its explanation, a flooded scope renders its banner. Component tests under `ui/tests/ct` for a fact body containing `<script>`, `<img onerror=…>`, `[agent-control:`, `(said in #eng)` and a 40000-character line, asserting they render as text, that markers and attributions display neutralized, and that the page does not scroll horizontally. Fixtures in `ui/tests/fixtures.ts`. CI grep bans `dangerouslySetInnerHTML` under the memory directory.

**Bench** (`evaluators/tests/test_memory_bench.py`): a fixture corpus of roughly 140 hand-labelled proposals across five axes: signal-to-noise, staleness, inference-versus-observation, forged metadata provenance, and **forged in-body attribution**. The shipped templates run against it with floors asserted in CI: no false negative on the credential set, no false negative on the metadata-forgery set, **no false negative on the in-body attribution set**, and a false-positive rate on the benign set under a threshold checked into the fixture file. Deterministic evaluators only. The LLM-judged version QM runs is section 15.

---

## 12. Phases

### Phase 0: settle the ADK memory surface and measure recall, 2 to 3 days

Install `agent-control[google-adk]` at a pinned version and answer A1 through A6 by executing them. The two method signatures; what `SearchMemoryResponse` actually contains; whether `VertexAiMemoryBankService` returns anything fact-shaped on write; whether `preload_memory` writes into instructions or into `contents`; and **whether an event-id-restricted `Session` projection is constructible and accepted**, which decides whether the ADK write path buffers or gates synchronously.

Then measure: 50 recall evaluations against the deployment's own server with a two-leaf control, p50 and p95, for both `execution` modes. That number goes in the docs beside `recall_timeout_ms`.

Phases 1 and 2 do not depend on any of it and can run in parallel.

### Phase 1: models, engine, event allowlist, payload contract, 5 to 6 days

`models/src/agent_control_models/memory.py`; the `AppliesTo` alias and both Literal widenings; the step-type constants; `map_applies_to`; **the `_safe_event_metadata` allowlist for memory steps**; the server-side ingest allowlist; the `/evaluate` memory branch with `MEMORY_PAYLOAD_INVALID`, the recall body cap and the `memory_gate_first_seen_at` write; the three SDK Literal widenings; `controls_published_at` on `_StateContainer`; `make openapi-spec`; TS SDK regeneration and name check.

The estimate moved up half a phase from the first revision because an allowlist is more work than a denylist: every field an operator legitimately needs has to be enumerated, and `error_message` needs its own handling.

Exit criterion: a memory step evaluates end to end against a real control, the decision appears labelled `memory_write` or `memory_recall`, and `test_memory_event_redaction.py` passes with six canaries and the raw-data flag both on and off. **No fact text, no evaluator message and no condition trace is anywhere in the event store.**

### Phase 2: quarantine store, endpoints, operations, startup gates, 6 to 7 days

Migration and the `agents` column; ORM model; wire models; service with the row lock, the state machine and the release binding; router; four operations plus their `DEFAULT_OPERATION_ACCESS` entries; `check_memory_startup_requirements` setting **both** flags and its env var in `server/.env.example`; the submit rate limit and the per-scope ceiling; the admissions route with the fingerprint and `prior_denials`; the memory-status route; namespace isolation cases.

Exit criterion: a held fact can be submitted, listed, revealed, released, consumed and discarded through the API with correct 409s; bodies are not stored and release is refused on a credential-less server while discard still works; no route leaks a body into a list response; and the per-scope ceiling holds under a flood.

### Phase 3: the framework-neutral SDK gate, 6 to 7 days

`memory/` package: `_payload`, `_gate`, `_provenance`, `_buffer`, `_admissions`, the public `propose_fact` and `filter_recall`, the `MemoryGate` Protocol, the admissions fetch in its own error boundary, the settings. **The three positive-proof checks in section 3.4 are the core of this phase, not a detail of it.** Full SDK test suite including the unevaluated-refusal tests, the steer-never-authors tests and the release-cannot-bypass tests.

Exit criterion: a runtime with no ADK at all can gate its own memory in two call sites; every row of the section 3.4 table has a passing test; and an SDK whose server was down at boot refuses instead of admitting.

### Phase 4: the ADK decorator, 4 to 5 days

`AgentControlMemoryService` between its `Wire` markers, the scope-and-requester resolver seam, the snapshot write path with its synchronous fallback, the session-versus-fact granularity handling, the recall bounds and structural pass, the attach-time warnings, and the five contract assertions.

Exit criterion: wrap `InMemoryMemoryService`, propose a fact containing a fake credential, watch it be held; propose one whose text claims `(said in #eng)`, watch it be neutralized and held; review both in the API, release one, watch the agent write it on the next proposal and only once.

### Phase 5: UI, 5 to 6 days

Tab, gate-status card with its five states plus the resolver and startup-gate explanations, decisions table over the event API with its "reported, not observed" line, queue with filters and cursor pagination, detail with the deliberate reveal, release modal with its one important sentence and its disabled state, discard, redacted-body explanation, flood banner, non-admin path, `isForbiddenError`, the plain-text rule and its CI grep. Playwright and component tests.

Exit criterion: an operator who has never read this document can find a held fact, understand why it was held, read it knowing it is untrusted, and either discard it or release it while understanding that releasing does not write it.

### Phase 6: templates and the bench, 3 to 4 days

Eight shipped `control_templates` entries: `memory-write-no-credentials` (deny), `memory-body-attribution` (steer), `memory-forged-provenance` (deny), `memory-write-untrusted-channel` (steer), `memory-write-requires-attribution` (steer), `memory-recall-no-foreign-scope` (observe), `memory-recall-speaker-mismatch` (observe), `memory-recall-pre-gate` (observe), plus `memory-recall-over-budget` (deny). Each with its parameters, its default action, and a one-paragraph description saying what it will and will not catch. Plus the fixture bench and its CI floors.

Exit criterion: an operator turns on shared memory protection by enabling templates rather than authoring condition trees, and the section 3.4 "everything is refused" first-run state has an obvious exit.

---

## 13. Effort

| Phase | Estimate | Confidence |
|---|---|---|
| 0. Settle ADK memory, measure recall | 2 to 3 days | Medium. Cheap to run; Phase 4's shape hangs on A6. |
| 1. Models, engine, event allowlist, payload contract | 5 to 6 days | High. The step-type mechanism exists; the allowlist is the work. |
| 2. Quarantine store, endpoints, operations, startup gates | 6 to 7 days | High. The controls-and-versions pattern on a simpler table. Two startup gates, two quota mechanisms and the release binding add about a day and a half over the naive version. |
| 3. Framework-neutral SDK gate | 6 to 7 days | Medium. The buffer, the dedup LRU, the backoff, the admissions cache and the freshness tracking are five pieces of stateful in-process machinery, and stateful is where this kind of work overruns. |
| 4. ADK decorator | 4 to 5 days | Medium, conditional on Phase 0. Add two days if A6 fails and the synchronous fallback becomes the only path. |
| 5. UI | 5 to 6 days | Medium. Three panels, a five-state machine rendered honestly, and a reveal flow that has to be hard to do by accident. |
| 6. Templates and bench | 3 to 4 days | Medium. Labelling 140 fixtures well is the cost, not the code. |
| TS SDK regeneration, phases 1 and 2 | 0.5 day each | Medium. |

**Total: 6 to 7.5 weeks** of focused work. That is up from the first revision's 5 to 6, and the delta is the security work: the allowlist, the two startup gates, the positive-proof checks, the body neutralizer, the recall bounds and the snapshot write path.

**Minimum useful slice: Phases 0 through 4, roughly 4.5 to 5.5 weeks.** That is the gate working, reviewable through the API, with no UI. A real stopping point: the security property holds and an operator uses `curl`.

**Smaller still, and genuinely worth shipping alone: Phases 1 and 2, about two and a half weeks.** Memory decisions become visible in the event stream and stop being mislabelled as LLM calls, held facts get a home, no fact text or evaluator message leaks into an AUTHENTICATED-readable store, and release does not exist on a server where ADMIN is not a boundary. No SDK change ships, so nothing about a running deployment changes, and section 4 says plainly that nothing is gated yet.

The estimate includes this repo's verification load: `make check` spans eight workspace members, the UI job runs lint, prettier, typecheck, `next build`, Playwright and component tests, and the TS SDK needs generate, name-check and generate-check on any phase touching the OpenAPI surface.

**Ongoing cost.** One coupling to ADK internals, `BaseMemoryService`, which is a smaller and more stable surface than `LlmRequest.config.system_instruction` or `LlmAgent.canonical_model`. One coupling to our own engine's metadata shape, now defended by an allowlist rather than by a list of known-bad keys, which means a new evaluator metadata key cannot silently reopen the leak. And one attribution-pattern set that needs occasional additions and has a fixture corpus to hold it honest.

---

## 14. Decisions taken, and what was rejected

| # | Question | Decision | Rejected |
|---|---|---|---|
| 1 | The payload | Fact text in `input` on write and `output` on recall; everything else in a server-authored `context.agent_control.memory` block, pre-aggregated to scalars, always emitted, validated at the boundary. Includes in-body attribution counts and, on recall, requester identity | Per-candidate paths reachable by index (`select_data` has no list-index syntax); an optional block (a missing path is a non-match, which is fail-open); letting a deployment's `context_extractor` supply it (the audited party authoring its own audit record, already banned at `plugin.py:807-808`); a new selector root (`ControlSelector` roots are contract); a recall payload with no requester (makes the one control a multi-user deployment needs unwritable) |
| 2 | Write, recall, or both | **Both, and recall carries the guarantee.** Write is off the response path and always POSTs; recall defaults to SDK execution with a 400ms timeout and a bounded batch | Write-only (leaves pre-gate facts, ungated writers, and every stored fact when a control is tightened; and for Memory Bank the write path cannot see facts at all); recall-only (a fact reaching the store at all is a cost worth refusing); per-candidate evaluation (N round trips for one recall) |
| 3 | What steer means | Hold. On write: not persisted, quarantine row, operator reviews. On recall: drop the whole batch. Per-candidate filtering is an SDK structural pass over signals the SDK computed, never a control decision | Rewriting the fact body from control output (a control authoring a fact nobody said); taking replacement text from `steering_context` (same thing, one indirection); injecting guidance at recall (no model call to steer); **per-candidate filtering driven by `result.metadata["flagged_sha256"]`, which no shipped evaluator produces and no evaluator could produce, since it never sees candidate boundaries**; silently writing on a failed quarantine submit |
| 4 | Fail-closed | A fact that cannot be **shown** to have been evaluated is not persisted. Three positive-proof checks: policy freshness via `controls_published_at`, at least one control actually evaluated, and a forced round trip on write. Write refuses, recall empties, the turn continues | **Inferring "evaluated" from the absence of an exception**, which `evaluation.py:514` makes a clean pass whenever the control cache is empty, and `__init__.py:698-703` makes the cache empty after any boot-time outage; fail-open on either path; failing the turn on a recall gate error (converts a control-plane outage into an agent outage for an optional component); reusing `_evaluate_and_enforce` and its framework-shaped exceptions |
| 5 | Attachment point | `MemoryGate` Protocol in framework-neutral code, `AgentControlMemoryService` decorating any `BaseMemoryService`, every ADK string between two `Wire` markers. Others get two functions and the HTTP contract | Monkeypatching or import hooks; a single ADK-only implementation with no neutral core (A1 through A6 are unverified and the blast radius must be one file); **holding a live `Session` across the burst buffer** (commits whatever the session grew into, not what the controls saw, and breaks the halt guarantee); claiming coverage for runtimes that added no call site |
| 6 | Event recording | The fact text never enters the event stream. Memory-step metadata is built from an **allowlist** in the SDK and rebuilt from the same allowlist on ingest. Bodies live only in the quarantine row, at ADMIN, and are not stored at all on a credential-less server | Storing the text verbatim (`_safe_event_metadata` promotes the preview into `metadata["input"]` today and events are AUTHENTICATED-readable); **a denylist of known-bad keys**, which missed `condition_trace` (`core.py:361`) and evaluator metadata including the `json` evaluator's verbatim `errors` (`json/evaluator.py:397-400`), and would miss the next key anybody adds; trusting the SDK's redaction alone (the SDK is the audited party and old versions exist); disabling the queue endpoint on an insecure server (makes the feature undemonstrable on a laptop) |
| 7 | Scope and tenancy | `(namespace_key, scope_kind, scope_id)`; `namespace_key` from the Principal on every query; `foreign_scope_count` **and** `speaker_mismatch_count` as control-writable signals, both `observe` by default; the limit stated in the docs, the endpoint docstring and the UI | Claiming Agent Control enforces isolation inside a store it does not own; **resting the tenancy story on `_state.py`'s namespace singleton**, which is true and answers the wrong question, since the multi-tenant case is many end users inside one process separated by `user_id`; resolving scopes server-side (needs a scope registry, which is the memory store by another name); an unhashed `scope_id` in the event stream (channel names are org structure in an AUTHENTICATED store); shipping the foreign-scope template at `deny` (denies every recall on a default install with no resolver) |
| 8 | UI | Fourth tab on agent detail. Gate status with five states, decisions table, held-for-review queue. Reveal is a deliberate separate recorded request. Discard is primary and modal-free; release is modal-gated, says it does not write the fact, and disables itself when the startup gate is closed. Plain text only, CI grep | Rendering fact text on row expand; markdown or any HTML string assembly (the console's cookie is an ADMIN credential); a "Release and write" button (Agent Control has no store); a confirm modal on discard (trains click-through on the dangerous action); presenting the no-resolver state as a denial |
| 9 | `applies_to` widening | Two values, `memory_write` and `memory_recall`, as a named `AppliesTo` alias | One `memory_op` discriminated by `check_stage` (encodes "pre means write" into every dashboard query); leaving `map_applies_to` alone (its `llm_call` fallback files memory decisions as model calls) |
| 10 | Where quarantine rows come from | Submitted by the agent under `MEMORY_QUARANTINE_SUBMIT` at AUTHENTICATED, write-only, rate limited **and** ceiling-capped per scope, with the server recomputing the hash | Server-authored inside `/evaluate` only (produces no row for SDK-executed controls, which is the path recall uses); one operation for submit and read (an ADMIN key in every agent process, or an archive readable by every agent process); a rate limit alone (86k rows a day buries the real ones) |
| 11 | What release means | A one-time admission, checked **after** evaluation, applying only to a `steer` from the recorded control under an unchanged control-set fingerprint, within 24 hours, and refused entirely when ADMIN is not a real boundary | **Checking the admission before evaluation** (turns any unauthenticated `:release` on a default server into a full control bypass with an audit trail that says an admin approved it); an admission that can rescue a `deny`; an unbound admission that survives a policy change; relying on `MEMORY_QUARANTINE_WRITE: ADMIN` alone, which is inert when `api_key_enabled` is `False` |
| 12 | Where provenance is defended | **In the fact body**, deterministically neutralized, plus the metadata field. Two properties replace "never touch the body": no control input reaches a transformation, and no transformation strengthens a claim | **Metadata-only provenance**, which guards a field the model never reads and leaves the in-body attribution the model does read completely unguarded, and which removed QM's actual defence rather than strengthening it; an operator-configurable attribution pattern set (operator input into a body transformation is still input into a body transformation) |
| 13 | Granularity | A proposal is a fact when the runtime says so and a session when it does not, with `granularity` on the payload and per-fact fields null rather than fabricated | Pretending `add_session_to_memory` gives fact granularity; refusing to gate session-granular writes at all (would leave `InMemoryMemoryService`, the only service this user can run, entirely ungated) |
| 14 | Re-evaluating stored facts | Not at rest. The recall gate re-evaluates on every read, which is what makes a tightened control bite tomorrow, and it neutralizes pre-gate bodies on the way past | A bulk sweep endpoint (needs the runtime to volunteer its whole store, which is the store arriving by the back door for a third time) |
| 15 | Bounding recall | 50 candidates, 64000 characters, per-candidate cap, `over_budget` flag with a denying template, and a server-side body cap on `memory_recall` | An unbounded batch, which lets anyone who can post in a shared channel flood a scope until every colleague's recall times out and returns nothing, and amplifies into an AUTHENTICATED `/evaluate` |

Also rejected, not one of the fifteen: adding `memory` to `BUILTIN_STEP_TYPES` (the beginning of treating step types as closed, which is the property this whole design rests on); a `memory_gate_last_seen_at` column (a write per agent per recall for a freshness dot); deleting quarantine rows on consume (destroys the audit trail the release mechanism exists to produce); inferring `proposer.trusted` or `requester.id_sha256` from anything server-side (only the runtime knows who is in the room, and a control plane that guesses will guess wrong on the day it matters); and sourcing `fact.prior_denials` from the in-process LRU (reads zero on exactly the reboot an attacker would provoke).

---

## 15. Explicitly out of scope, with reasons

**A memory store.** Not deferred. Rejected, for the fourth time in this project. Section 2.9 lists what the customer must supply instead.

**Bulk re-evaluation of an existing store.** Rejected rather than deferred. It requires enumerating a store we do not own, which means the runtime uploading its whole memory to us, which is the store by another name. The recall gate is the answer and it is a better one, because it re-evaluates continuously rather than at whatever moment somebody pressed a button.

**Deleting a fact.** Agent Control cannot. A discarded quarantine row means "we did not admit this", not "this is gone". A customer with a deletion obligation deletes from their store, and this document says so rather than implying a capability that does not exist.

**Per-candidate control-driven recall filtering.** Would need a `memory_candidates` evaluator, a JSON-array `Step.output` that `regex` and `list` would stringify badly, and a boundary protocol by which an evaluator learns candidate identity. Roughly three days and a new builtin. The structural pass in section 3.3 covers the signals the SDK computes, which is most of the value, and controls stay batch-granular with that stated plainly.

**Semantic deduplication and staleness scoring.** `duplicate_of_sha256` is an exact-hash match. "The same fact rephrased" needs embeddings, an index and a threshold nobody has calibrated, and it would be the third component of a memory system living in a control plane. The `json` evaluator over the candidate array is the seam if a deployment wants to bring its own similarity signal in as a payload field.

**The LLM-judged bench with CI floors.** QM's version scores signal-to-noise, staleness and inference-versus-observation with a model in the loop. Real, valuable, and wrong for CI: nondeterministic scores make a red build a coin flip, and it spends model quota on every push. Phase 6 ships the deterministic fixture version with hard floors. The judged version runs on demand, out of band, into a report rather than a gate.

**Redacting a secret out of a fact and storing the remainder.** Section 3.3's transformations are neutralizations, not redactions. Removing a secret and keeping the rest means a control authored the stored fact, and the remainder is frequently still the secret in context.

**Per-scope or per-team memory policy**, such as "this team's agents may only read facts from their own scope". It needs an authorization model that can express a grant narrower than a namespace. `AccessLevel` has three values and `_resolve_namespace_key` is `del request; return self._default_namespace_key`, so the feature would be decorative under the shipped provider. Same reason `agent-system-prompts.md` puts per-team model policy out of scope.

**Automatic delivery for Strands or any non-ADK framework.** Strands is the obvious next target, `strands/plugin.py` has the right shape, and whether it exposes a memory seam at all is unverified. Two to three days once the ADK path is proven, and not specified here, because this repo does not plan against unverified framework APIs.

**Gating writes into `VertexAiMemoryBankService` at fact granularity.** Structurally impossible: extraction is remote and the extracted facts are never returned on the write path (A3). The write gate covers the material, the recall gate covers the facts, and the docs say which is which rather than letting an operator assume both.

**ADK artifacts.** `save_artifact` goes through none of the plugin callbacks and the pinned surface exposes no hook, which `_warn_on_artifact_service` (`plugin.py:811`) already says out loud. An agent can write bytes no control sees. That is the file-inputs plan's territory and it is not reopened here.

**A fact TTL or expiry policy.** The store owns lifetime. A control plane that expired facts would be writing to the store.

**Cross-agent memory graphs, shared team memory, memory inheritance.** All features of a memory product. This is a control on one.

**Rendering held facts inside a chat transcript.** Depends on the orchestration plan's Phase 3 chat panel and on whether an operator reading a transcript should see what the agent was refused. Separate work, separate argument.

**Per-agent read scoping on the quarantine queue.** Named as accepted risk: every ADMIN key in a namespace reads every held fact for every agent in it. It needs a credential model binding a key to an agent name, which exists only inside the executor's session tokens.

**Trustworthy memory-decision events.** `OBSERVABILITY_WRITE` is AUTHENTICATED, so the decisions table shows what agents reported. Making the event stream unforgeable is a change to the observability tier, not to this feature, and it would be dishonest to fix it only for memory events.

---

## 16. Verification checklist before each PR

```
make check                       # test + lint + typecheck across eight workspace members
make openapi-spec
cd ui && npm run fetch-api-types && npm run lint && npm run typecheck && npm run build
cd ui && npx playwright test && npx playwright test -c playwright-ct.config.ts
make sdk-ts-generate && make sdk-ts-name-check && make sdk-ts-generate-check
alembic heads                    # exactly one, before writing any migration file
```

---

## 17. What the security review found, and where each fix lives

Every item was reproduced against the working tree before being accepted. None was waved through and none was argued away, because all ten were right.

| Severity | Finding | Reproduced at | Resolved in |
|---|---|---|---|
| Blocker | Fail-closed was unreachable: an empty control cache produces a clean pass with no exception, and a boot-time outage guarantees an empty cache | `evaluation.py:461, 514, 557`; `evaluation.py:117-155`; `__init__.py:194-200, 301-306, 698-703` | 2.4, 3.4 (three positive-proof checks), 6 (`controls_published_at`), 7.2, 11 (the unevaluated-refusal tests) |
| Blocker | Release was a full control bypass: ADMIN is inert by default, and the admission was checked before evaluation | `config.py:37`; `auth_framework/config.py:220-226`; `caller_identity.py:11-16` | 3.6 (`MEMORY_RELEASE_ALLOWED`), 3.9 (post-evaluation, control-bound, fingerprint-checked), 8 (`ck_memory_quarantine_release_bound`), 9 (`403 MEMORY_RELEASE_DISABLED`) |
| Blocker | Provenance was defended only in metadata; the in-body attribution the model actually reads was unguarded | design, versus `_sanitize.py:16-21` as the precedent for the fix | 2.7, 3.3 (two properties replacing "never touch the body"), 7.3 (`neutralize_body`), 3.1 (`fact.body_attribution_*`), 12 (`memory-body-attribution`) |
| Major | The event suppression was a denylist and missed `condition_trace` and evaluator metadata, including the `json` evaluator's verbatim `errors` | `core.py:357-361`; `evaluation_events.py:25-44`; `json/evaluator.py:344-400` | 2.3, 3.6 (allowlist in the SDK and again on ingest), 11 (six-canary redaction test) |
| Major | No requester identity on recall, so the multi-tenant case that matters was unwritable | `_state.py:38` is true and irrelevant; `search_memory` separates by `user_id` | 2.8, 3.1 (`requester.*`, `speaker_mismatch_count`), 3.7, 12 (`memory-recall-speaker-mismatch`) |
| Major | Per-candidate filtering depended on a `flagged_sha256` no evaluator produces or could produce | `regex/evaluator.py:72`; `json/evaluator.py:397-400` | 3.3 (batch-granular controls; structural pass owned by the SDK), 15 (the evaluator, priced and out of scope) |
| Major | The recall hot path had no input bound on a store whose contents the design cannot limit | `MAX_FACT_CHARS` was write-side only | 3.10, 9 (`MEMORY_PAYLOAD_TOO_LARGE`), 10 |
| Major | Time-of-check/time-of-use: the burst buffer held a live `Session` across up to 180 seconds | design | 7.5 (snapshot, projection, synchronous fallback), A6, 11 (buffer-snapshot test) |
| Minor | `memory-recall-no-foreign-scope` shipped at `deny` while `scope_resolver` defaults to `None`, denying every recall on a default install | design, self-inconsistent with the pre-gate template's own reasoning | 3.7 (all three at `observe`, dedicated no-resolver state, gated promotion) |
| Minor | `prior_denials` was sourced from an in-process cache that resets on reboot; the denial backoff is hash-keyed and distinct facts defeat it | design | 8 (`denial_count`), 9 (per-scope ceiling, `prior_denials` on the admissions poll), 3.6 and 3.8 (the `OBSERVABILITY_WRITE` caveat in the UI) |

---

## 18. The riskiest remaining assumptions

Not the schema, not the operations, not the UI. Those are decisions with visible consequences and tests that catch them.

**First: that the recall gate is actually on the path recalled facts take to the model.** The whole guarantee in section 3.2 rests on every recall going through `search_memory`. If a deployment's agent reads its memory store directly, through a custom tool, through a separately wired RAG retriever, or through anything bypassing `BaseMemoryService`, the gate is not on that path and nothing in this design detects it. `gate_state` reports whether the gate has *ever* been called, which is not the same as whether it is called *every* time, and no honest mechanism gives us the second. Same class of gap as `save_artifact`, stated the same way: the UI reports what was seen, never what was not.

**Second: A1 through A6, five signatures and one behaviour, none of which could be executed here.** `import google` fails. A6 is the one that changes the plan rather than the estimate: if an event-id-restricted projection is not constructible, the ADK write path gates synchronously and the burst buffer becomes framework-neutral-only. If `preload_memory` writes into `contents` rather than instructions, section 2.6's framing needs correcting and the recall gate is still right. The one that would genuinely hurt is `search_memory` returning something the decorator cannot filter and return, and the pinned contract job is the only thing that would say so before a customer did.

**Third, and the one most likely to be quietly undone: the event allowlist.** Section 2.3's leak exists because several reasonable pieces of code do reasonable things. `_selected_data_preview` gives an operator the context they need to understand a denial. `_safe_event_metadata` promotes it into `input` so the UI has something to render. The `json` evaluator quotes the offending value so a schema failure is debuggable. Nobody was wrong. The memory allowlist cuts a hole in all three, and it will look like a bug to the next person who opens a memory denial in the Monitor tab and finds no input field and no condition trace. `test_memory_event_redaction.py` with its six canaries is what turns that helpful fix into a red build, and it is worth more than the code it protects. A comment at the SDK allowlist and at the ingest allowlist names the test.

**Fourth: that operators will not turn off the thing that makes it work.** `allow_when_no_memory_controls=True` is one setting away from restoring the exact hole in blocker 1, and a deployment that attaches the decorator, writes no controls, sees no memory, and reaches for the setting has done something entirely reasonable and entirely wrong. The mitigations are a distinct UI state, a log line naming the setting and its consequence, and eight templates so that the correct exit is easier than the incorrect one. That is design pressure, not enforcement, and it is the weakest guarantee in the document.
