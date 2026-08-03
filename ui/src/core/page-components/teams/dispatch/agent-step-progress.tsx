import { Group, Progress, Stack, Text } from '@mantine/core';

import classes from './dispatch.module.css';
import { pluralize } from './formatting';

export type AgentStepTally = {
  /** Positions in every claimed task's plan, whether or not they have run. */
  planned: number;
  /** Positions with a step row the agent finished. */
  completed: number;
  /** Positions with a step row still running. */
  running: number;
  /** Positions whose step row failed or was abandoned on reclaim. */
  failed: number;
};

/**
 * The second bar, and it is deliberately not Linear's.
 *
 * Linear's bar is issue completion: a fact about the tracker, moved by a human
 * closing something. This one counts steps an agent says it finished, which is
 * the same self-report the chat panel labels "reported by the agent". Merging
 * the two would launder a claim into a measurement, so they are two bars with
 * two labels and they never become one.
 *
 * It exists at all because a run with no visible progress reads as a run that
 * has broken.
 */
export function AgentStepProgress({ tally }: { tally: AgentStepTally }) {
  if (tally.planned === 0) return null;

  const done = Math.min(tally.completed, tally.planned);
  const percent = Math.round((done / tally.planned) * 100);
  const failedPercent = Math.round(
    (Math.min(tally.failed, tally.planned - done) / tally.planned) * 100
  );

  return (
    <Stack
      gap={2}
      className={classes.agentProgress}
      data-testid="agent-step-progress"
    >
      <Group gap="xs" align="center" wrap="nowrap">
        <Progress.Root
          size="xs"
          radius="xl"
          style={{ flex: 1 }}
          aria-label={`Agent steps finished: ${done} of ${tally.planned}`}
        >
          <Progress.Section value={percent} color="violet" />
          {failedPercent > 0 ? (
            <Progress.Section value={failedPercent} color="red" />
          ) : null}
        </Progress.Root>
        <Text size="xs" c="dimmed" data-testid="agent-step-progress-count">
          agents: {done} of {tally.planned} {pluralize(tally.planned, 'step')}
        </Text>
      </Group>

      <Text size="xs" c="dimmed">
        {tally.running > 0
          ? `${tally.running} ${pluralize(tally.running, 'step')} running. `
          : ''}
        {tally.failed > 0
          ? `${tally.failed} ${pluralize(tally.failed, 'step')} failed or was abandoned. `
          : ''}
        Reported by the agents, not by Linear. The bar above this one is issue
        completion and only a person moves it.
      </Text>
    </Stack>
  );
}
