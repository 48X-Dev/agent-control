import {
  ActionIcon,
  Badge,
  Box,
  Collapse,
  Group,
  Loader,
  Progress,
  Skeleton,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import {
  IconCalendarEvent,
  IconExternalLink,
  IconFlag,
  IconPlayerPlay,
  IconPlugConnected,
  IconRefresh,
  IconSettings,
  IconX,
} from '@tabler/icons-react';
import { useCallback, useMemo, useState } from 'react';

import type {
  AgentTaskSummary,
  DispatchStateSnapshot,
  Milestone,
} from '@/core/api/types';
import {
  isLiveStatus,
  useAgentTasks,
} from '@/core/hooks/query-hooks/use-agent-tasks';
import { useDispatchState } from '@/core/hooks/query-hooks/use-dispatch-state';
import { useTeamMilestones } from '@/core/hooks/query-hooks/use-team-milestones';
import { useNow } from '@/core/page-components/agent-detail/agent-chat/use-now';

import { AgentStepProgress } from './dispatch/agent-step-progress';
import { safeHttpUrl } from './dispatch/formatting';
import type { MilestoneScope } from './dispatch/milestone-work';
import { MilestoneWork } from './dispatch/milestone-work';
import { tallySteps } from './dispatch/tally';
import { LinkLinearTeam } from './link-linear-team';
import classes from './team-detail.module.css';

const SKELETON_ROW_COUNT = 3;

/**
 * How old a cached milestone list has to be before the play control refuses.
 *
 * The server's own cache lives for a minute; past that, a list still marked
 * cached is being served from the degraded path while Linear is in a cooldown.
 * The eligible set cannot be recomputed against that, and a press that fails
 * after the operator has committed to the gesture is worse than a control that
 * says why it is off.
 */
const STALE_LIST_AFTER_MS = 120_000;

const CLOCK_INTERVAL_MS = 30_000;

const DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  year: 'numeric',
  month: 'short',
  day: 'numeric',
  // Target dates are plain calendar dates. Formatting them in the viewer's
  // zone would slide them a day backwards west of UTC.
  timeZone: 'UTC',
});

const TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
});

const STATUS_COLORS: Record<string, string> = {
  done: 'teal',
  overdue: 'red',
  next: 'blue',
  unstarted: 'gray',
};

function formatTargetDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : DATE_FORMATTER.format(parsed);
}

function formatFetchedAt(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : TIME_FORMATTER.format(parsed);
}

/**
 * Whether a cached milestone list is the degraded one.
 *
 * `cached` on its own is an ordinary TTL hit and means nothing is wrong. A
 * cached read older than the server's own TTL means Linear is in a cooldown and
 * the last good copy is being served, which is fine for reading a board and not
 * fine for deciding what to start.
 */
function isStaleRead(
  cached: boolean | undefined,
  fetchedAt: string | null | undefined,
  now: number
): boolean {
  if (!cached || !fetchedAt) return false;
  const readAt = new Date(fetchedAt).getTime();
  if (Number.isNaN(readAt)) return false;
  return now - readAt > STALE_LIST_AFTER_MS;
}

type MilestoneRowProps = {
  milestone: Milestone;
  teamSlug: string;
  linearTeamKey: string | null;
  open: boolean;
  onToggle: () => void;
  scope: MilestoneScope | undefined;
  teamTasks: AgentTaskSummary[];
  dispatchState: DispatchStateSnapshot | null | undefined;
  listIsStale: boolean;
  listFetchedAt: string | null;
  now: number;
  onScopeResolved: (scope: MilestoneScope) => void;
};

/**
 * Why the play control is off, in the words the tooltip uses.
 *
 * Returns null when it should be live. A disabled control with no explanation
 * is a control somebody clicks four times and then files a bug about.
 */
function playDisabledReason({
  linearTeamKey,
  dispatchState,
  listIsStale,
  listFetchedAt,
  scope,
}: {
  linearTeamKey: string | null;
  dispatchState: DispatchStateSnapshot | null | undefined;
  listIsStale: boolean;
  listFetchedAt: string | null;
  scope: MilestoneScope | undefined;
}): string | null {
  // An unlinked team has no milestone list and therefore no rows, so this is
  // defensive rather than reachable. It stays because the alternative is a
  // control that opens an empty panel.
  if (!linearTeamKey) {
    return 'This team is not linked to a Linear team.';
  }
  if (dispatchState?.paused) {
    return 'New agent work is paused in this namespace.';
  }
  if (dispatchState?.executors_halted) {
    return 'Executors are halted in this namespace.';
  }
  if (listIsStale) {
    const when = formatFetchedAt(listFetchedAt);
    return `This milestone list was last read${when ? ` at ${when}` : ''} and could not be refreshed, so the set a press would cover cannot be worked out.`;
  }
  if (scope && scope.eligibleCount === 0) {
    return 'Nothing to work on in this milestone.';
  }
  return null;
}

function MilestoneRow({
  milestone,
  teamSlug,
  linearTeamKey,
  open,
  onToggle,
  scope,
  teamTasks,
  dispatchState,
  listIsStale,
  listFetchedAt,
  now,
  onScopeResolved,
}: MilestoneRowProps) {
  const targetDate = formatTargetDate(milestone.target_date);
  const percent =
    typeof milestone.progress === 'number'
      ? Math.round(milestone.progress * 100)
      : null;

  // Same treatment the issue links get: a url read out of a third party is not
  // a scheme the browser should be handed unchecked.
  const projectUrl = safeHttpUrl(milestone.project_url);

  const refs = useMemo(() => new Set(scope?.refs ?? []), [scope]);
  const milestoneTasks = useMemo(
    () =>
      teamTasks.filter(
        (task) => task.source_kind === 'linear' && refs.has(task.source_ref)
      ),
    [teamTasks, refs]
  );
  const tally = tallySteps(milestoneTasks, scope?.stepsPerTask ?? 1);
  const liveTasks = milestoneTasks.filter((task) => isLiveStatus(task.status));

  const disabledReason = playDisabledReason({
    linearTeamKey,
    dispatchState,
    listIsStale,
    listFetchedAt,
    scope,
  });
  // Closing is always allowed: a panel that cannot be shut because the
  // namespace paused while it was open is a trap, not a safeguard.
  const playDisabled = !open && disabledReason !== null;

  const control = (
    <ActionIcon
      variant="subtle"
      size="sm"
      color="gray"
      onClick={onToggle}
      disabled={playDisabled}
      aria-label={
        open
          ? `Close the work scope for ${milestone.name}`
          : `Start work on ${milestone.name}`
      }
      data-testid="milestone-start-work"
    >
      {open ? <IconX size={16} /> : <IconPlayerPlay size={16} />}
    </ActionIcon>
  );

  return (
    <Box className={classes.milestone} data-testid="milestone-row">
      <Stack gap={8}>
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Text size="sm" fw={500} lineClamp={2}>
            {milestone.name}
          </Text>
          <Group gap={6} align="center" wrap="nowrap">
            {liveTasks.length > 0 ? (
              <Group gap={4} align="center" wrap="nowrap">
                <Loader size={12} data-testid="milestone-running-spinner" />
                <Text
                  size="xs"
                  c="dimmed"
                  data-testid="milestone-running-count"
                >
                  {liveTasks.length} running
                </Text>
              </Group>
            ) : null}
            {milestone.status ? (
              <Badge
                size="sm"
                variant="light"
                color={STATUS_COLORS[milestone.status] ?? 'gray'}
                data-testid="milestone-status"
              >
                {milestone.status}
              </Badge>
            ) : null}
            {/* Pressing this starts nothing. It opens the panel below, which
                shows the issues a run would cover; a second, labelled button
                in there is what creates anything. */}
            {disabledReason && !open ? (
              <Tooltip label={disabledReason} withArrow multiline maw={280}>
                <Box>{control}</Box>
              </Tooltip>
            ) : (
              control
            )}
          </Group>
        </Group>

        <Group gap="md" align="center" wrap="wrap">
          {targetDate ? (
            <Group gap={4} align="center" wrap="nowrap">
              <IconCalendarEvent
                size={14}
                stroke={2}
                color="var(--mantine-color-dimmed)"
              />
              <Text size="xs" c="dimmed" data-testid="milestone-target-date">
                {targetDate}
              </Text>
            </Group>
          ) : (
            <Text size="xs" c="dimmed" fs="italic">
              No target date
            </Text>
          )}

          {milestone.project_name ? (
            <Text
              size="xs"
              c="dimmed"
              component={projectUrl ? 'a' : 'span'}
              href={projectUrl ?? undefined}
              target={projectUrl ? '_blank' : undefined}
              rel={projectUrl ? 'noopener noreferrer' : undefined}
              className={projectUrl ? classes.projectLink : undefined}
              lineClamp={1}
            >
              {milestone.project_name}
              {projectUrl ? (
                <IconExternalLink
                  size={11}
                  style={{ marginLeft: 4, verticalAlign: 'middle' }}
                />
              ) : null}
            </Text>
          ) : null}
        </Group>

        {percent === null ? null : (
          <Group gap="xs" align="center" wrap="nowrap">
            <Progress
              value={percent}
              size="sm"
              radius="xl"
              style={{ flex: 1 }}
              aria-label={`${milestone.name} issue completion`}
            />
            <Text size="xs" c="dimmed" data-testid="milestone-progress">
              {percent}%
            </Text>
          </Group>
        )}

        {/* Under Linear's bar, never merged with it. That one is issue
            completion in the tracker; this one counts steps agents say they
            finished. */}
        <AgentStepProgress tally={tally} />

        <Collapse in={open}>
          {open && linearTeamKey ? (
            <MilestoneWork
              teamSlug={teamSlug}
              milestoneId={milestone.id}
              milestoneName={milestone.name}
              linearTeamKey={linearTeamKey}
              teamTasks={teamTasks}
              dispatchState={dispatchState}
              now={now}
              onScopeResolved={onScopeResolved}
            />
          ) : null}
        </Collapse>
      </Stack>
    </Box>
  );
}

function SetupHint() {
  return (
    <Box className={classes.hint} data-testid="milestones-not-configured">
      <Group gap="sm" align="flex-start" wrap="nowrap">
        <IconSettings
          size={18}
          stroke={2}
          color="var(--mantine-color-dimmed)"
          style={{ flexShrink: 0, marginTop: 2 }}
        />
        <Stack gap={4}>
          <Text size="sm" fw={500}>
            Milestones are turned off
          </Text>
          <Text size="xs" c="dimmed">
            Set{' '}
            <span className={classes.envVar}>AGENT_CONTROL_LINEAR_API_KEY</span>{' '}
            on the Agent Control server and restart it. The key stays on the
            server; it is never sent to this page.
          </Text>
        </Stack>
      </Group>
    </Box>
  );
}

function NotLinkedPrompt({ slug }: { slug: string }) {
  return (
    <Box className={classes.hint} data-testid="milestones-not-linked">
      <Stack gap="sm">
        <Group gap="sm" align="flex-start" wrap="nowrap">
          <IconPlugConnected
            size={18}
            stroke={2}
            color="var(--mantine-color-dimmed)"
            style={{ flexShrink: 0, marginTop: 2 }}
          />
          <Stack gap={4}>
            <Text size="sm" fw={500}>
              Not linked to Linear yet
            </Text>
            <Text size="xs" c="dimmed">
              Point this team at a Linear team to pull its milestones in.
            </Text>
          </Stack>
        </Group>
        <LinkLinearTeam slug={slug} />
      </Stack>
    </Box>
  );
}

type InlineErrorProps = {
  title: string;
  message: string;
  retryAfterSeconds?: number | null;
  onRetry: () => void;
  isRetrying: boolean;
};

/** Linear being unavailable is reported, not alarmed about. */
function InlineError({
  title,
  message,
  retryAfterSeconds,
  onRetry,
  isRetrying,
}: InlineErrorProps) {
  return (
    <Box className={classes.hint} data-testid="milestones-error">
      <Stack gap="xs">
        <Text size="sm" fw={500}>
          {title}
        </Text>
        <Text size="xs" c="dimmed">
          {message}
          {retryAfterSeconds
            ? ` Linear asked us to wait ${retryAfterSeconds}s before trying again.`
            : ''}
        </Text>
        <Group>
          <Button
            variant="outline"
            size="sm"
            onClick={onRetry}
            loading={isRetrying}
            leftSection={<IconRefresh size={14} />}
            data-testid="milestones-retry"
          >
            Try again
          </Button>
        </Group>
      </Stack>
    </Box>
  );
}

export function TeamMilestones({ slug }: { slug: string }) {
  const { data, error, isLoading, isFetching, refetch } =
    useTeamMilestones(slug);
  const [changingLink, setChangingLink] = useState(false);
  const [openMilestoneId, setOpenMilestoneId] = useState<string | null>(null);
  const [scopes, setScopes] = useState<Record<string, MilestoneScope>>({});

  const linkedKey = data?.linear_team_key ?? null;
  const canChangeLink = Boolean(linkedKey) && data?.status !== 'not_configured';
  const fetchedAt = formatFetchedAt(data?.fetched_at);
  const linked = data?.status === 'ok' && Boolean(linkedKey);

  // One ledger read for the whole panel rather than one per row, and one
  // dispatch-state read for every banner and every tooltip on this screen.
  const tasksQuery = useAgentTasks({ team: slug, enabled: linked });
  const dispatchQuery = useDispatchState({ enabled: linked });
  const teamTasks = useMemo(
    () => tasksQuery.data?.tasks ?? [],
    [tasksQuery.data]
  );
  const dispatchState = dispatchQuery.data?.state ?? null;

  const now = useNow(linked, CLOCK_INTERVAL_MS);

  // `cached` alone is an ordinary TTL hit and means nothing is wrong. A cached
  // list whose read is older than the server's own TTL is the degraded path:
  // Linear is in a cooldown and the eligible set cannot be recomputed.
  const listIsStale = isStaleRead(data?.cached, data?.fetched_at, now);

  const onScopeResolved = useCallback((scope: MilestoneScope) => {
    setScopes((previous) => ({ ...previous, [scope.milestoneId]: scope }));
  }, []);

  const renderBody = () => {
    if (isLoading) {
      return (
        <Stack gap="sm" data-testid="milestones-loading">
          {Array.from({ length: SKELETON_ROW_COUNT }, (_, index) => (
            <Skeleton key={index} height={64} radius="sm" />
          ))}
        </Stack>
      );
    }

    // The endpoint answers 200 even when Linear is down, so reaching here means
    // Agent Control itself did not answer. Worded so a failure on our side is
    // not reported to the user as a Linear outage.
    if (error || !data) {
      return (
        <InlineError
          title="Could not load milestones"
          message="The milestones request did not complete."
          onRetry={() => void refetch()}
          isRetrying={isFetching}
        />
      );
    }

    if (changingLink) {
      return (
        <LinkLinearTeam
          slug={slug}
          currentKey={linkedKey}
          onLinked={() => setChangingLink(false)}
          onCancel={() => setChangingLink(false)}
        />
      );
    }

    switch (data.status) {
      case 'not_configured':
        return <SetupHint />;
      case 'not_linked':
        return <NotLinkedPrompt slug={slug} />;
      case 'error':
        return (
          <InlineError
            title="Could not reach Linear"
            message={data.error ?? 'Linear did not answer.'}
            retryAfterSeconds={data.retry_after_seconds}
            onRetry={() => void refetch()}
            isRetrying={isFetching}
          />
        );
      case 'empty':
        return (
          <Stack gap={4} py="md" align="center" data-testid="milestones-empty">
            <Text size="sm" fw={500}>
              No milestones yet
            </Text>
            <Text size="xs" c="dimmed" ta="center">
              Nothing is scheduled in {linkedKey ?? 'Linear'} for this team.
            </Text>
          </Stack>
        );
      case 'ok':
        return (
          <Stack gap="sm" data-testid="milestones-list">
            {data.milestones.map((milestone) => (
              <MilestoneRow
                key={milestone.id}
                milestone={milestone}
                teamSlug={slug}
                linearTeamKey={linkedKey}
                open={openMilestoneId === milestone.id}
                onToggle={() =>
                  setOpenMilestoneId((current) =>
                    current === milestone.id ? null : milestone.id
                  )
                }
                scope={scopes[milestone.id]}
                teamTasks={teamTasks}
                dispatchState={dispatchState}
                listIsStale={listIsStale}
                listFetchedAt={data.fetched_at ?? null}
                now={now}
                onScopeResolved={onScopeResolved}
              />
            ))}
          </Stack>
        );
    }
  };

  return (
    <Box className={classes.panel} data-testid="milestones-panel">
      <Stack gap="md">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Group gap={8} align="center" wrap="nowrap">
            <IconFlag
              size={16}
              stroke={2}
              color="var(--mantine-color-dimmed)"
            />
            <Text size="sm" fw={600}>
              Milestones
            </Text>
            {linkedKey ? (
              <Badge
                size="sm"
                variant="light"
                color="gray"
                data-testid="linear-team-key-badge"
              >
                {linkedKey}
              </Badge>
            ) : null}
          </Group>

          {canChangeLink && !changingLink ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setChangingLink(true)}
              data-testid="milestones-change-link"
            >
              Change
            </Button>
          ) : null}
        </Group>

        {renderBody()}

        {fetchedAt && data?.status === 'ok' ? (
          <Text size="xs" c="dimmed" data-testid="milestones-fetched-at">
            {data.cached ? 'Cached from' : 'Updated'} {fetchedAt}
          </Text>
        ) : null}
      </Stack>
    </Box>
  );
}
