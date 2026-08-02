import { Anchor, Box, Group, Stack, Text } from '@mantine/core';

import { traceUrl } from '@/core/api/client';
import type { Plan, PlanStep, PlanStepStatus } from '@/core/api/types';
import { PLAN_STALE_AFTER_MS } from '@/core/hooks/query-hooks/use-plan';

import classes from './agent-chat.module.css';
import { useNow } from './use-now';

/**
 * The label. It is exact, and it is not softened anywhere in this file.
 *
 * Everything below it is the agent's account of its own work. An agent that
 * lies about its progress is not something this panel can detect, so the panel
 * does the two things it can: it says who the claim came from, and it puts the
 * trace beside it so a person can go and check.
 */
const RAIL_LABEL = 'Plan reported by the agent';

const STATUS_LABEL: Record<PlanStepStatus, string> = {
  pending: 'not marked',
  active: 'in progress',
  done: 'done',
  skipped: 'skipped',
  failed: 'failed',
};

const STATUS_MARK: Record<PlanStepStatus, string> = {
  pending: '○',
  active: '◐',
  done: '●',
  skipped: '–',
  failed: '×',
};

function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function ageOf(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return null;
  return formatDuration(now - at);
}

/**
 * A tally of what the agent has marked, and deliberately not a ratio.
 *
 * "3 of 5" invites the reader to divide, and the number that falls out is a
 * completion percentage nobody measured. Counting each status separately says
 * the same true things - three done, one failed, one untouched - without
 * offering the arithmetic.
 */
function tally(steps: PlanStep[]): string {
  const counts = new Map<PlanStepStatus, number>();
  for (const step of steps) {
    counts.set(step.status, (counts.get(step.status) ?? 0) + 1);
  }
  const order: PlanStepStatus[] = [
    'done',
    'active',
    'failed',
    'skipped',
    'pending',
  ];
  return order
    .filter((status) => (counts.get(status) ?? 0) > 0)
    .map((status) => `${counts.get(status)} ${STATUS_LABEL[status]}`)
    .join(' · ');
}

function TraceLink({ traceId }: { traceId: string }) {
  return (
    <Anchor
      href={traceUrl(traceId)}
      target="_blank"
      rel="noreferrer"
      size="xs"
      data-testid="chat-progress-trace-link"
    >
      Open this turn’s trace
    </Anchor>
  );
}

function StepRow({ step, now }: { step: PlanStep; now: number }) {
  const age = ageOf(step.updated_at, now);
  return (
    <Group
      gap={8}
      wrap="nowrap"
      align="flex-start"
      data-testid="chat-progress-step"
      data-step-status={step.status}
    >
      <Text size="xs" c="dimmed" aria-hidden="true" w={12} ta="center">
        {STATUS_MARK[step.status]}
      </Text>
      <Stack gap={0} style={{ minWidth: 0 }}>
        {/* Model-authored text, rendered as text. Same rule as every other
            body in this panel: no markdown, no markup. */}
        <Text component="div" size="xs" className={classes.messageText}>
          {step.title}
        </Text>
        <Text size="xs" c="dimmed">
          {STATUS_LABEL[step.status]}
          {step.status !== 'pending' && age ? ` · marked ${age} ago` : ''}
          {step.note ? ` · ${step.note}` : ''}
        </Text>
      </Stack>
    </Group>
  );
}

type NoPlanProps = {
  turnCount: number;
  turnCountIsExact: boolean;
  sessionStartedAt: string | null;
  lastActivityAt: string | null;
  traceId: string | null;
  now: number;
};

/**
 * What is shown when the agent never declared a plan, which is most of the time.
 *
 * Three facts this console actually knows - how many turns have run, how long
 * the chat has been open, when it last did anything - plus the trace of the last
 * turn. No bar, no estimate, and nothing that implies the agent is partway
 * through something it never described.
 */
function NoPlan({
  turnCount,
  turnCountIsExact,
  sessionStartedAt,
  lastActivityAt,
  traceId,
  now,
}: NoPlanProps) {
  const open = ageOf(sessionStartedAt, now);
  const idle = ageOf(lastActivityAt, now);
  const turns = turnCountIsExact
    ? `${turnCount} ${turnCount === 1 ? 'turn' : 'turns'}`
    : `at least ${turnCount} ${turnCount === 1 ? 'turn' : 'turns'}`;

  return (
    <Stack gap={4} data-testid="chat-progress-fallback">
      <Text size="xs" c="dimmed">
        This agent has not reported a plan, so there is nothing to show but what
        this console can see for itself.
      </Text>
      <Text size="xs">
        {turns}
        {open ? ` · open ${open}` : ''}
        {idle ? ` · last activity ${idle} ago` : ''}
      </Text>
      {traceId ? <TraceLink traceId={traceId} /> : null}
    </Stack>
  );
}

type ProgressRailProps = {
  plan: Plan | null;
  /**
   * Where the read of the plan itself got to.
   *
   * Distinguished from "no plan" on purpose. Rendering the no-plan fallback
   * while the request is still in flight, or after it failed, tells a person
   * the agent reported nothing when nobody has actually asked yet - a small
   * lie, in a component whose entire job is not telling those.
   */
  readState: 'loading' | 'error' | 'ready';
  /** Turns counted from the transcript on screen. */
  turnCount: number;
  /** False when earlier messages are outside the loaded window. */
  turnCountIsExact: boolean;
  sessionStartedAt: string | null;
  lastActivityAt: string | null;
  /** The turn in flight if there is one, otherwise the last completed turn. */
  traceId: string | null;
  isTurnActive: boolean;
};

/**
 * What the agent says it is doing, labelled as such, with the evidence beside it.
 *
 * The one thing this component will not do is produce a number for how far
 * along the work is. There is no percentage here and none can be derived from
 * what it renders: not from event counts, which measure nothing, and not from
 * steps marked over steps declared, which is a completion figure wearing a
 * fraction's clothes. A plan nobody has touched for an hour is shown as an old
 * plan, by its last update time, rather than as work frozen at 40%.
 */
export function ProgressRail({
  plan,
  readState,
  turnCount,
  turnCountIsExact,
  sessionStartedAt,
  lastActivityAt,
  traceId,
  isTurnActive,
}: ProgressRailProps) {
  // Ticks while a turn is running, and also while a plan is on screen with no
  // turn behind it - which is exactly when staleness is the thing worth saying.
  // A frozen clock would leave an hour-old plan reading "marked 4s ago".
  const now = useNow(isTurnActive || plan !== null, 5000);
  const steps = plan?.steps ?? [];
  const lastUpdate = plan ? new Date(plan.last_updated_at).getTime() : null;
  const isStale =
    lastUpdate !== null &&
    !Number.isNaN(lastUpdate) &&
    now - lastUpdate > PLAN_STALE_AFTER_MS;
  const revisedCount = plan ? Math.max(0, plan.revision_count - 1) : 0;

  return (
    <Box className={classes.progressRail} data-testid="chat-progress-rail">
      <Stack gap={6}>
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text size="xs" fw={600}>
            {RAIL_LABEL}
          </Text>
          {plan && revisedCount > 0 ? (
            <Text size="xs" c="dimmed" data-testid="chat-progress-revisions">
              Plan revised {revisedCount}{' '}
              {revisedCount === 1 ? 'time' : 'times'} · showing revision{' '}
              {plan.revision}
            </Text>
          ) : null}
        </Group>

        {readState === 'loading' && plan === null ? (
          <Text size="xs" c="dimmed" data-testid="chat-progress-loading">
            Reading what the agent has reported…
          </Text>
        ) : readState === 'error' && plan === null ? (
          <Text size="xs" c="dimmed" data-testid="chat-progress-error">
            The agent’s report could not be read, so nothing is shown here. That
            says nothing about whether the agent is working.
          </Text>
        ) : plan === null ? (
          <NoPlan
            turnCount={turnCount}
            turnCountIsExact={turnCountIsExact}
            sessionStartedAt={sessionStartedAt}
            lastActivityAt={lastActivityAt}
            traceId={traceId}
            now={now}
          />
        ) : (
          <Stack gap={6}>
            <Stack gap={4}>
              {steps.map((step) => (
                <StepRow key={step.index} step={step} now={now} />
              ))}
            </Stack>

            <Text size="xs" c="dimmed" data-testid="chat-progress-tally">
              {tally(steps)}
            </Text>

            {isStale ? (
              <Text size="xs" c="dimmed" data-testid="chat-progress-stale">
                No step has been marked for{' '}
                {ageOf(plan.last_updated_at, now) ?? 'a while'}. The agent may
                have moved on from this plan; nothing here says whether the work
                is still happening.
              </Text>
            ) : null}

            <Group gap="xs" wrap="nowrap">
              <Text size="xs" c="dimmed">
                Reported by the agent. The trace is the independent record.
              </Text>
              {traceId ? <TraceLink traceId={traceId} /> : null}
            </Group>
          </Stack>
        )}
      </Stack>
    </Box>
  );
}
