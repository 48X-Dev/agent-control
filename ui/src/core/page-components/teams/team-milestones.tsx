import {
  Badge,
  Box,
  Group,
  Progress,
  Skeleton,
  Stack,
  Text,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import {
  IconCalendarEvent,
  IconExternalLink,
  IconFlag,
  IconPlugConnected,
  IconRefresh,
  IconSettings,
} from '@tabler/icons-react';
import { useState } from 'react';

import type { Milestone } from '@/core/api/types';
import { useTeamMilestones } from '@/core/hooks/query-hooks/use-team-milestones';

import { LinkLinearTeam } from './link-linear-team';
import classes from './team-detail.module.css';

const SKELETON_ROW_COUNT = 3;

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

function MilestoneRow({ milestone }: { milestone: Milestone }) {
  const targetDate = formatTargetDate(milestone.target_date);
  const percent =
    typeof milestone.progress === 'number'
      ? Math.round(milestone.progress * 100)
      : null;

  return (
    <Box className={classes.milestone} data-testid="milestone-row">
      <Stack gap={8}>
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Text size="sm" fw={500} lineClamp={2}>
            {milestone.name}
          </Text>
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
              component={milestone.project_url ? 'a' : 'span'}
              href={milestone.project_url ?? undefined}
              target={milestone.project_url ? '_blank' : undefined}
              rel={milestone.project_url ? 'noopener noreferrer' : undefined}
              className={
                milestone.project_url ? classes.projectLink : undefined
              }
              lineClamp={1}
            >
              {milestone.project_name}
              {milestone.project_url ? (
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
              aria-label={`${milestone.name} progress`}
            />
            <Text size="xs" c="dimmed" data-testid="milestone-progress">
              {percent}%
            </Text>
          </Group>
        )}
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

  const linkedKey = data?.linear_team_key ?? null;
  const canChangeLink = Boolean(linkedKey) && data?.status !== 'not_configured';
  const fetchedAt = formatFetchedAt(data?.fetched_at);

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
              <MilestoneRow key={milestone.id} milestone={milestone} />
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
