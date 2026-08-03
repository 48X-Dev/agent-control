import { Box, Group, Stack, Text } from '@mantine/core';
import { IconExternalLink } from '@tabler/icons-react';

import type { AgentTaskSummary } from '@/core/api/types';

import classes from './dispatch.module.css';
import { formatAge, pluralize, safeHttpUrl } from './formatting';
import { TaskRunList } from './task-runs';

/** After this long unread, a result is stale rather than pending. */
const STALE_AFTER_MS = 48 * 60 * 60 * 1000;

/**
 * Finished work, waiting on a person.
 *
 * No agent closes an issue. Not at any tier, not behind any flag, not because a
 * control approved its summary: a control is a filter on text and nothing in
 * this stack tells "I fixed it" apart from "I said I fixed it". So a completed
 * task means the agents are done and the tracker is untouched, and the
 * milestone bar above moves when a person reads this and closes the issue.
 *
 * There is no accept-all here and there will not be one. Bulk-accepting eight
 * claims nobody read is the thing this queue exists to prevent, and it would
 * leave an audit trail saying the opposite.
 */
export function ResultsForReview({
  tasks,
  identifiersByRef,
  urlsByRef,
  now,
}: {
  tasks: AgentTaskSummary[];
  identifiersByRef: Map<string, string>;
  urlsByRef: Map<string, string>;
  now: number;
}) {
  if (tasks.length === 0) return null;

  const stale = tasks.filter((task) => {
    const updated = new Date(task.updated_at).getTime();
    return !Number.isNaN(updated) && now - updated > STALE_AFTER_MS;
  });

  return (
    <Stack gap="xs" data-testid="task-review-queue">
      <Text size="sm" fw={600}>
        {tasks.length} {pluralize(tasks.length, 'result')} waiting for you
      </Text>

      <Text size="xs" c="dimmed">
        The agents finished. Nothing has been written to the tracker and no
        issue has been closed. Read what each agent wrote, then close the issue
        in Linear yourself; the milestone bar moves because a person agreed, not
        because an agent reported.
      </Text>

      {stale.length > 0 ? (
        <Text size="xs" c="dimmed" data-testid="task-review-stale">
          {stale.length} of these {pluralize(stale.length, 'has', 'have')} been
          waiting more than two days. Nothing here expires into approval.
        </Text>
      ) : null}

      <TaskRunList
        tasks={tasks}
        identifiersByRef={identifiersByRef}
        now={now}
      />

      <Stack gap={2}>
        {tasks.map((task) => {
          // The milestone read carries only issues that are still eligible, so
          // an issue a person has already picked up drops out of that map. The
          // url the task recorded at import is the one that survives, and this
          // link is the whole human-accept path: losing it silently would leave
          // a result nobody can act on.
          const url =
            urlsByRef.get(task.source_ref) ?? safeHttpUrl(task.source_url);
          if (!url) return null;
          const identifier = identifiersByRef.get(task.source_ref);
          const age = formatAge(task.updated_at, now);
          return (
            <Box key={task.task_key}>
              <Group gap="xs" wrap="nowrap">
                <Text
                  size="xs"
                  component="a"
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid="task-review-link"
                >
                  Close {identifier ?? 'this issue'} in Linear
                  <IconExternalLink
                    size={11}
                    style={{ marginLeft: 4, verticalAlign: 'middle' }}
                  />
                </Text>
                {age ? (
                  <Text size="xs" c="dimmed" className={classes.identifier}>
                    finished {age}
                  </Text>
                ) : null}
              </Group>
            </Box>
          );
        })}
      </Stack>
    </Stack>
  );
}
