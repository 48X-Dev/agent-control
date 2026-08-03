import type { AgentTaskSummary } from '@/core/api/types';

import type { AgentStepTally } from './agent-step-progress';

const FAILED_STATUSES = new Set(['failed', 'blocked', 'running_unknown']);

/**
 * Steps finished, from the task rows alone.
 *
 * `current_step` is advanced in the same transaction that records a finished
 * step, so it counts hops the ledger accepted rather than hops an agent
 * mentioned. It is still the agent's own account of its work, which is why the
 * bar built from it is labelled as such and kept apart from Linear's.
 *
 * A failed task contributes the one position it died at; nothing here pretends
 * to know how far into that step it got.
 */
export function tallySteps(
  tasks: AgentTaskSummary[],
  stepsPerTask: number
): AgentStepTally {
  const perTask = Math.max(1, stepsPerTask);

  let completed = 0;
  let running = 0;
  let failed = 0;

  for (const task of tasks) {
    completed += Math.min(Math.max(task.current_step, 0), perTask);
    if (task.status === 'running') running += 1;
    if (FAILED_STATUSES.has(task.status)) failed += 1;
  }

  return { planned: tasks.length * perTask, completed, running, failed };
}
