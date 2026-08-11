# Per-Team Budgets: Implementation Plan

Status: design. Nothing built.
Split out of `agent-fleet-topology.md` on 11 August, where it was section 5.
Scope: a per-team companion to the namespace turn budget, charged in the same transaction and at the same statement.
Depends on: the dispatch ledger from `task-dispatcher.md` Phases 1 to 4, largely shipped (`agent_tasks`, `agent_dispatch_state`, `charge_dispatch_turn`).
Does not depend on: the fleet topology, containers, images, or any packaging work. **This is why it was split out.** It sat behind three weeks of fleet packaging in the parent plan while having no dependency on it, and it is the only part of that document that changes what the system does rather than where it runs.

---

## 1. What ships

`agent_dispatch_state` has `namespace_key` as its entire primary key, `max_tasks_per_hour` 20 and `max_turns_per_hour` 60. All four teams draw on that one row, so every dispatcher run prints one budget line for the whole fleet and no team can be given a share.

This adds a per-team row charged in the same transaction as the namespace row, resolves the team from the task rather than the session, and is honest that per-team accounting bounds authorized spend without creating capacity.

---

## 2. Where it is enforced

Same place as the namespace budget, for the same reason, and moving it would repeat a mistake `task-dispatcher.md` 12.1 already corrected once. The enforcement point is `charge_dispatch_turn` in `services/agent_dispatch_state.py`, called from `_acquire_turn` in `services/agent_turns.py`, inside the one short transaction that takes the session row, and only for sessions with `agent_task_id IS NOT NULL`.

```sql
CREATE TABLE agent_team_dispatch_budgets (
    namespace_key        TEXT    NOT NULL,
    team_slug            TEXT    NOT NULL,
    max_tasks_per_hour   INTEGER NOT NULL DEFAULT 5,
    max_turns_per_hour   INTEGER NOT NULL DEFAULT 15,
    max_concurrent_turns INTEGER NOT NULL DEFAULT 1,
    turns_window_start   TIMESTAMPTZ NOT NULL DEFAULT now(),
    turns_in_window      INTEGER NOT NULL DEFAULT 0,
    paused_at            TIMESTAMPTZ,
    paused_by            VARCHAR(64),
    paused_reason        VARCHAR(500),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace_key, team_slug)
);
```

`charge_dispatch_turn` grows a second `INSERT ... ON CONFLICT DO UPDATE ... WHERE ... RETURNING` against this table, written from the same `_WINDOW_EXPIRED` fragment so the roll condition and the charge condition cannot drift. It runs after the namespace charge and before the per-agent concurrency count.

**Mirror the halt columns.** `agent_dispatch_state` has since grown `executors_halted_at`, `executors_halted_by` and `executors_halted_reason` (`models.py:1806-1810`), and `_acquire_turn` calls `require_executors_not_halted` before charging. The table above carries pause columns and not halt columns, which is deliberate: halting executors is a namespace-level operator action and a per-team halt is not a feature anybody asked for. Stated here so the asymmetry is a decision rather than an oversight.

**`max_turns_per_hour: 15` is not a spend ceiling, and the row has to say so where somebody reads it.** One charge is one `POST /run`, and one `POST /run` is an entry into a loop that can call the model and its tools an unbounded number of times before returning. The only bound on what happens inside is `turn_timeout_seconds`, default 300. A team with one turn left can outspend a team with fifteen.

---

## 3. Lock ordering, and what the review found underneath it

The argument is sound. The namespace charge takes an exclusive lock on the one state row via `INSERT ... ON CONFLICT DO UPDATE` and holds it to commit; the per-agent count runs afterwards with no lock of its own; the import path takes the same row first via `_LOCK_ROW ... FOR UPDATE`. Every dispatch turn therefore takes the namespace row first, and a per-team row taken second yields a total order and cannot deadlock across teams.

Four things the parent plan did not say, all found in review on 11 August:

**Deadlocks on this path are not retried.** `_RETRYABLE_SQLSTATES` is `{"40001", "40P01"}` at `services/turn_locks.py:47` and it wraps `release_turn_lock` only. Any new deadlock surface on the charge path surfaces as a 500, not a retry. The lock-order argument says there should not be one; the point is that if the argument is wrong, the failure is loud and ugly rather than absorbed.

**The un-charge guarantee is mostly implicit.** Only the `acquire_turn_lock` failure path calls `db.rollback()` explicitly (`services/agent_turns.py:488`); the other six refusal paths rely on the context manager. One test covers the property, `test_a_turn_refused_after_the_charge_leaves_the_counter_where_it_was`. Charging two rows doubles what that single test protects, so this plan adds an explicit companion for the team counter rather than trusting the same implicit unwind.

**Window rolling has no concurrent coverage.** Both existing roll tests are sequential and age `turns_window_start` by hand. Two rows rolling independently across a boundary is new state space with nothing behind it. Named as a required test below.

**Turns are billed before the executor is reached.** The commit happens before `_prepare_attachments` and `factory.client_for` can fail, so a team's allowance burns on attachment refusals. Deliberate, because a refund is a write on a failure path and it double-refunds under exactly the retry storm that produces it. Worth writing down because "budget exhausted after zero successful turns" is otherwise filed as a bug.

---

## 4. How the turn path learns the team

Not from the session. Measured on 11 August: 33 `agent_sessions` rows, 16 with `agent_task_id`, all 16 with `team_id` NULL, because `DispatchClient.create_session` posts only `agent_name`, `title` and `task_key`.

**The team is resolved from `agent_tasks.team_slug` by joining on `agent_sessions.agent_task_id`,** in the same transaction and the same statement that charges. That is the value the import handler committed against a scope the operator confirmed, and a session edit cannot re-point it afterwards. Passing `team_slug` on session creation would be a value chosen by the process being budgeted.

**Indexing.** `agent_tasks.team_slug` is nullable and carries no foreign key (`models.py:1524`), matching `agent_workflows`. The per-turn resolution is a primary-key lookup by `agent_task_id` and is cheap. The per-team `max_tasks_per_hour` count at import is not, and section 6 proposes exactly that count, so it needs an index on `(namespace_key, team_slug)` over `agent_tasks` in the same migration. `agent_workflows` already has the equivalent at `models.py:1419`.

The dispatcher should also start sending `team_slug` on `POST /agent-sessions` so the console can filter by team. Display, not enforcement, said here and again in the code, because a field on the session row is exactly the field a later reader simplifies the join into.

---

## 5. Which team pays for a cross-team workflow

**The task's team pays. Not the agent's team.** The team that pressed play authorized the spend; the team whose agent appears in a step did not.

**The justification changed and shrank.** The parent plan argued from a shipped example: `plan-critique-execute` naming `engineering_reviewer` from a marketing workflow, with the threat that any team could drain any other team's budget by naming its agent in a step. Commit `701f7e5` closed that: the workflow upsert now refuses a step naming a non-member with `AGENT_NOT_IN_TEAM`, and the live workflow names three marketing agents.

**What remains is narrower and still real.** Workflows with `team_slug IS NULL` may pin any agent, and rows written before the refusal landed may still name non-members whose membership has since changed. So the decision stands and the reasoning is now about those two cases rather than about the shipped example.

The cost is that a team's budget does not reflect work its agent did. These numbers are a spend-authorization ledger, not a utilization report, and the console labels them "authorized by" rather than "used by".

---

## 6. The rest of the surface

**A task with no team charges the namespace pool only.** The file source refuses `--team` outright rather than merely not requiring it (`dispatcher/.../cli.py:86-87`), and `sales-outreach` has no `linear_team_key`, so a teamless task is the normal state of two supported paths. `REQUIRE_TEAM=true` turns it into a refusal, which does not add attribution to the file source so much as disable it. Default false.

**Exhaustion is a 429** with `retry_after_seconds`, the same shape the namespace ceiling produces, with a distinct error code so the message can say which ceiling and whose. The dispatcher already handles it: `paused_quota` is in the reclaim predicate, the task keeps heartbeating, and it resumes after the window rolls.

**Two additions.** `GET /agent-dispatch` grows a `teams` block so the import preview and the dispatcher's opening lines can report a team's remaining allowance; advisory, enforcement stays in `charge_dispatch_turn`. And `POST /agent-tasks/import` counts `max_tasks_per_hour` per team in the same inserting transaction it already uses for the namespace ceiling. That one is enforcement, and it belongs there because tasks are created only by import.

**Settings**, `AGENT_CONTROL_DISPATCH_` prefixed like the rest of `DispatchSettings`: `DEFAULT_TEAM_MAX_TASKS_PER_HOUR` (5), `DEFAULT_TEAM_MAX_TURNS_PER_HOUR` (15), `DEFAULT_TEAM_MAX_CONCURRENT_TURNS` (1), `REQUIRE_TEAM` (false). Plus a model validator refusing a configuration where the seeded team defaults times the number of teams undershoots the namespace ceiling by more than a stated margin, because a namespace ceiling nobody can reach reads as protection and is not. The precedent for arithmetic like that living in `config.py` is `_fleet_must_not_squeeze_human_chat_out_of_the_session_ceiling` at `config.py:758`.

---

## 7. `max_concurrent_turns: 1` halves current capacity, and the parent plan did not say so

`max_concurrent_tasks_per_agent` is pinned `le=1` at `config.py:744`, so today it bounds one agent. Four teams with two agents each means the namespace can have eight turns in flight.

**Seeding team concurrency at 1 caps the namespace at four.** The parent plan called 1 "the honest starting value for a deployment whose upstream is one subscription" and never mentioned that adopting it halves throughput on migration day. Both things are true. The number may still be right, because the upstream is one queue and section 8 is about that, but it ships as a deliberate throughput decision with a line in the release note, not as a default nobody priced.

---

## 8. Per-team accounting does not create per-team capacity

This has to be the last word, because everything above will otherwise read as a fix.

All four teams reach one model endpoint: `http://127.0.0.1:10531/v1`, one `openai-oauth` process, one consumer subscription. That hop has no per-caller identity, no API key, and no way to attribute a request to a team. Probed: `/v1/models` answers 200 and `/v1/files` answers 404, so it is not a service with per-tenant surface hiding behind an unused feature. It is one queue.

So `marketing` burning through the upstream rate limit returns errors to `engineering`, and no arrangement of rows changes that. Per-team budgets bound what each team is authorized to spend from a shared pot. `max_concurrent_turns` bounds how much of the queue one team can occupy at an instant, which is the only fairness lever on this side of the hop, and it is a lever on contention rather than on capacity.

The only thing that creates per-team capacity is a per-team upstream credential with its own quota. This deployment does not have one and cannot have one on a consumer subscription. That is a purchasing decision, not an engineering one, and the runbook says so in those words.

---

## 9. Phases and effort

**Phase 1, the table and the charge. 1 week.** `agent_team_dispatch_budgets` and its migration, including the `agent_tasks` team index from section 4; the second charge statement and the concurrency predicate inside `charge_dispatch_turn`; the task-to-team resolution in the same transaction; the settings and the oversubscription validator.

Required tests, because the parent plan listed none for the highest-risk code in it:

- Both counters unwound on every refusal path, not just the lock-acquisition one.
- Lock order under contention, two teams, concurrent.
- Two rows rolling their windows independently across a boundary, concurrent. This is the coverage gap named in section 3.
- A NULL team charging the namespace only.
- `REQUIRE_TEAM=true` refusing.
- A cross-team workflow charging the task's team.

**Phase 2, the surface. 3 days.** Per-team `max_tasks_per_hour` in the import transaction; the `teams` block on `GET /agent-dispatch`; the console banner naming which ceiling and the "authorized by" label; the dispatcher's advisory chain-fit check and its consecutive-failure circuit break.

**Phase 3, TypeScript SDK. 2 days.** Regeneration, gated by `make sdk-ts-generate-check` in CI, so mandatory rather than scope.

**Roughly 1.5 weeks plus 2 days**, unchanged from the parent plan's estimate. The work was never the problem; its position behind three weeks of packaging was.

---

## 10. Riskiest assumptions

**That two hot rows on the turn path is acceptable.** The lock-ordering argument says it is one extra acquisition behind an existing serialization point rather than new contention, and the rejected alternative of deriving the namespace ceiling as a sum would remove the serialization the per-agent count depends on. But `agent_turns.py`'s docstring is proud of how little that transaction does, and the defence that dispatch turns are rate-limited by the very thing being checked is now made twice about the same transaction. Somebody who owns that file should say whether the second row is worth it.

**That `max_concurrent_turns` is a fairness lever and not just a smaller queue.** It bounds occupancy of a shared upstream with no notion of teams. Against one subscription that may be all anyone can do; it may also be a number that looks like fairness and delivers none, in which case the honest version is no per-team concurrency at all and a sentence in the runbook.

**That charging before the executor is reached stays acceptable at per-team granularity.** At namespace scope an outage burning the hour is annoying. At team scope it means one team's allowance can be consumed entirely by a failure mode that never reached a model, and that team's operator sees a ceiling they did not spend.
