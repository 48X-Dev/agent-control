import {
  Badge,
  Box,
  Collapse,
  Group,
  Loader,
  Stack,
  Text,
  UnstyledButton,
} from '@mantine/core';
import {
  IconChevronDown,
  IconChevronRight,
  IconExternalLink,
} from '@tabler/icons-react';
import { useState } from 'react';

import { traceUrl } from '@/core/api/client';
import type {
  AgentTaskChainHop,
  AgentTaskStatus,
  AgentTaskSummary,
} from '@/core/api/types';
import {
  isLiveStatus,
  useAgentTaskChain,
} from '@/core/hooks/query-hooks/use-agent-tasks';

import classes from './dispatch.module.css';
import { formatAge, formatDateTime, pluralize } from './formatting';

const STATUS_COLORS: Record<AgentTaskStatus, string> = {
  queued: 'gray',
  running: 'blue',
  completed: 'teal',
  failed: 'red',
  blocked: 'orange',
  paused_quota: 'yellow',
  running_unknown: 'orange',
  awaiting_approval: 'violet',
  cancelled: 'gray',
};

/** What each status means to the person reading it, in one line. */
const STATUS_COPY: Record<AgentTaskStatus, string> = {
  queued: 'Waiting for a dispatcher to pick it up.',
  running: 'An agent is working on it now.',
  completed: 'The agents finished. The issue is untouched.',
  failed: 'The work was attempted and did not work.',
  blocked:
    'Never attempted: the configuration is wrong, and retrying it on a timer would fail the same way forever.',
  paused_quota:
    'Out of quota. It keeps its place and resumes at the same step, which is safe because the refusal happened before anything left the process.',
  running_unknown:
    'A turn timed out and nothing proves the work stopped. It is not retried automatically; a person clears it.',
  awaiting_approval: 'Suspended mid-chain, waiting on a human decision.',
  cancelled: 'Cancelled by an operator.',
};

function StepRail({ taskKey, live }: { taskKey: string; live: boolean }) {
  const { data, isLoading, error } = useAgentTaskChain(taskKey, { live });

  if (isLoading) {
    return (
      <Group gap="xs" py="xs">
        <Loader size="xs" />
        <Text size="xs" c="dimmed">
          Reading the chain
        </Text>
      </Group>
    );
  }

  if (error || !data) {
    return (
      <Text size="xs" c="dimmed" py="xs">
        The chain for this task could not be read.
      </Text>
    );
  }

  const { chain } = data;

  return (
    <Stack gap="xs" className={classes.stepRail} data-testid="task-step-rail">
      {chain.hops.map((hop) => (
        <HopRow key={hop.step_index} hop={hop} />
      ))}
      <Text size="xs" c="dimmed">
        {chain.hops_ran} of {chain.hops_planned}{' '}
        {pluralize(chain.hops_planned, 'step')} started. Assembled from the
        ledger&apos;s own step rows, so a step that never ran still appears.
      </Text>
    </Stack>
  );
}

function HopRow({ hop }: { hop: AgentTaskChainHop }) {
  const agent = hop.agent_name ?? 'no agent resolved';
  const started = formatDateTime(hop.started_at);

  return (
    <Stack gap={2} data-testid="task-step">
      <Group gap="xs" wrap="wrap" align="baseline">
        <Text size="xs" fw={600}>
          Step {hop.step_index + 1}: {agent}
        </Text>
        {hop.ran ? (
          <Badge
            size="xs"
            variant="light"
            color={
              hop.status === 'completed'
                ? 'teal'
                : hop.status === 'running'
                  ? 'blue'
                  : 'red'
            }
          >
            {hop.status ?? 'unknown'}
          </Badge>
        ) : (
          <Badge size="xs" variant="light" color="gray">
            never ran
          </Badge>
        )}
        {started ? (
          <Text size="xs" c="dimmed">
            {started}
          </Text>
        ) : null}
        {hop.turn_trace_id ? (
          <Text
            size="xs"
            c="dimmed"
            component="a"
            href={traceUrl(hop.turn_trace_id)}
            target="_blank"
            rel="noopener noreferrer"
          >
            Trace
            <IconExternalLink
              size={11}
              style={{ marginLeft: 4, verticalAlign: 'middle' }}
            />
          </Text>
        ) : null}
      </Group>

      {hop.brief ? (
        <Text size="xs" c="dimmed">
          Asked to: {hop.brief}
        </Text>
      ) : null}

      {hop.failure_code ? (
        <Text size="xs" c="red" data-testid="task-step-failure">
          {hop.failure_code}
          {hop.failure_detail ? `: ${hop.failure_detail}` : ''}
        </Text>
      ) : null}

      {hop.output_text ? (
        <Box>
          {/* The agent's own words, verbatim, as text. Never markup: an agent
              that swallowed an injection writes this string. */}
          <Text
            className={classes.outputText}
            component="pre"
            data-testid="task-step-output"
          >
            {hop.output_text}
          </Text>
          {hop.output_truncated ? (
            <Text size="xs" c="dimmed">
              This output was truncated when it was recorded.
            </Text>
          ) : null}
        </Box>
      ) : null}
    </Stack>
  );
}

function TaskRow({
  task,
  now,
  issueIdentifier,
}: {
  task: AgentTaskSummary;
  now: number;
  issueIdentifier: string | undefined;
}) {
  const [open, setOpen] = useState(false);
  const live = isLiveStatus(task.status);
  const age = formatAge(task.updated_at, now);

  return (
    <Box className={classes.taskRow} data-testid="task-run-row">
      <Stack gap={4}>
        <UnstyledButton
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          data-testid="task-run-toggle"
        >
          <Group gap="xs" wrap="nowrap" align="flex-start">
            {open ? (
              <IconChevronDown size={14} style={{ marginTop: 3 }} />
            ) : (
              <IconChevronRight size={14} style={{ marginTop: 3 }} />
            )}
            <Stack gap={2} style={{ minWidth: 0, flex: 1 }}>
              <Group gap="xs" align="baseline" wrap="nowrap">
                {issueIdentifier ? (
                  <Text className={classes.identifier} c="dimmed">
                    {issueIdentifier}
                  </Text>
                ) : null}
                {/* Untrusted: the title as filed in the tracker. */}
                <Text size="sm" lineClamp={1} data-testid="task-run-title">
                  {task.title}
                </Text>
              </Group>
              <Group gap="xs" wrap="wrap">
                <Badge
                  size="xs"
                  variant="light"
                  color={STATUS_COLORS[task.status]}
                  data-testid="task-run-status"
                >
                  {task.status}
                </Badge>
                {task.dry_run ? (
                  <Badge size="xs" variant="light" color="gray">
                    dry run
                  </Badge>
                ) : null}
                <Text size="xs" c="dimmed">
                  step {task.current_step + 1} · {task.turns_used}{' '}
                  {pluralize(task.turns_used, 'turn')} spent
                </Text>
                {age ? (
                  <Text size="xs" c="dimmed">
                    updated {age}
                  </Text>
                ) : null}
              </Group>
            </Stack>
          </Group>
        </UnstyledButton>

        <Text size="xs" c="dimmed">
          {STATUS_COPY[task.status]}
        </Text>

        {task.failure_code ? (
          <Text size="xs" c="red" data-testid="task-run-failure">
            {task.failure_code}
            {task.failure_detail ? `: ${task.failure_detail}` : ''}
          </Text>
        ) : null}

        <Collapse in={open}>
          {open ? <StepRail taskKey={task.task_key} live={live} /> : null}
        </Collapse>
      </Stack>
    </Box>
  );
}

export type TaskRunListProps = {
  tasks: AgentTaskSummary[];
  /** Linear identifiers by source ref, so a row can say OPS-114 rather than a uuid. */
  identifiersByRef: Map<string, string>;
  now: number;
};

/**
 * What the agents did, per issue, with the step rail underneath.
 *
 * The rail is built from the ledger's step rows rather than from a trace
 * rollup. A rollup is assembled from control-execution events, so an agent with
 * no control that fired contributes nothing to it and disappears; each step
 * still links to its own trace for whoever wants the forensic view of one hop.
 */
export function TaskRunList({
  tasks,
  identifiersByRef,
  now,
}: TaskRunListProps) {
  if (tasks.length === 0) return null;

  return (
    <Stack gap={6} data-testid="task-run-list">
      {tasks.map((task) => (
        <TaskRow
          key={task.task_key}
          task={task}
          now={now}
          issueIdentifier={identifiersByRef.get(task.source_ref)}
        />
      ))}
    </Stack>
  );
}
