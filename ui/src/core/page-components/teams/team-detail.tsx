import {
  Alert,
  Box,
  Center,
  Grid,
  Group,
  Loader,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { IconAlertCircle, IconArrowLeft, IconUsers } from '@tabler/icons-react';
import Link from 'next/link';
import type { CSSProperties, ReactNode } from 'react';

import { ErrorBoundary } from '@/components/error-boundary';
import { isNotFoundError } from '@/core/api/errors';
import { getTeamAccent, getTeamInitials } from '@/core/constants/team-colors';
import { useTeam } from '@/core/hooks/query-hooks/use-teams';

import { TeamAgents } from './team-agents';
import classes from './team-detail.module.css';
import { TeamMilestones } from './team-milestones';

const TEAMS_ROUTE = '/teams';

function memberCountLabel(count: number): string {
  if (count === 0) return 'No agents';
  return count === 1 ? '1 agent' : `${count} agents`;
}

function PageShell({ children }: { children: ReactNode }) {
  return (
    <Stack p="xl" maw={1400} mx="auto" my={0} gap="lg">
      {children}
    </Stack>
  );
}

function BackToTeams() {
  return (
    <Text
      component={Link}
      href={TEAMS_ROUTE}
      size="sm"
      className={classes.backLink}
      data-testid="back-to-teams"
    >
      <IconArrowLeft size={14} stroke={2} />
      Teams
    </Text>
  );
}

function TeamNotFound({ slug }: { slug: string }) {
  return (
    <PageShell>
      <BackToTeams />
      <Center h={360}>
        <Stack align="center" gap="md" maw={520}>
          <Title order={3} fw={600}>
            Team not found
          </Title>
          <Text size="sm" c="dimmed" ta="center" data-testid="team-not-found">
            No team with the slug &quot;{slug}&quot; exists in this namespace.
            It may have been deleted, or the link may be out of date.
          </Text>
          <Button
            component={Link}
            href={TEAMS_ROUTE}
            variant="outline"
            size="sm"
            data-testid="team-not-found-back"
          >
            Back to teams
          </Button>
        </Stack>
      </Center>
    </PageShell>
  );
}

const TeamDetailPage = ({ slug }: { slug: string }) => {
  const { data: team, isLoading, error } = useTeam(slug);

  if (isLoading) {
    return (
      <PageShell>
        <Center h={400}>
          <Stack align="center" gap="md">
            <Loader size="lg" />
            <Text c="dimmed">Loading team...</Text>
          </Stack>
        </Center>
      </PageShell>
    );
  }

  if (isNotFoundError(error)) {
    return <TeamNotFound slug={slug} />;
  }

  if (error || !team) {
    return (
      <PageShell>
        <BackToTeams />
        <Alert
          icon={<IconAlertCircle size={16} />}
          title="Error loading team"
          color="red"
          data-testid="team-detail-error"
        >
          Failed to fetch this team. Please try again later.
        </Alert>
      </PageShell>
    );
  }

  const accent = getTeamAccent(team.slug);
  const accentStyle = {
    '--team-accent-solid': accent.solid,
    '--team-accent-surface': accent.surface,
    '--team-accent-foreground': accent.foreground,
  } as CSSProperties;

  return (
    <Box style={accentStyle}>
      <PageShell>
        <BackToTeams />

        <Box className={classes.header} data-testid="team-detail-header">
          <Group gap="md" align="flex-start" wrap="nowrap">
            <Box className={classes.marker} aria-hidden="true">
              {getTeamInitials(team.display_name)}
            </Box>
            <Stack gap={6} style={{ minWidth: 0 }}>
              <Title
                order={2}
                fw={600}
                className={classes.teamName}
                data-testid="team-display-name"
              >
                {team.display_name}
              </Title>
              {team.description ? (
                <Text size="sm" c="dimmed" data-testid="team-description">
                  {team.description}
                </Text>
              ) : null}
              <Group gap={6} align="center">
                <IconUsers
                  size={14}
                  stroke={2}
                  color="var(--mantine-color-dimmed)"
                />
                <Text size="sm" fw={500} data-testid="team-member-count">
                  {memberCountLabel(team.member_count)}
                </Text>
                <Text size="sm" c="dimmed">
                  ·
                </Text>
                <Text size="sm" c="dimmed" data-testid="team-slug">
                  {team.slug}
                </Text>
              </Group>
            </Stack>
          </Group>
        </Box>

        {/* Stacked, not side by side. Milestones carry the scope preview, the
            per-issue result cards and two progress bars, and at lg:5 that all
            wrapped into a column narrow enough that issue titles truncated and
            the agent output needed scrolling to read one line at a time. The
            agent list is a short list of names and loses nothing by going full
            width above it. */}
        <Grid gutter="lg">
          <Grid.Col span={12}>
            <TeamAgents slug={team.slug} />
          </Grid.Col>
          <Grid.Col span={12}>
            {/* Linear is a third party. A failure in this panel must not take
                the agent list down with it. Keyed on the slug so moving
                between teams resets both a caught error and any half-finished
                link form. */}
            <ErrorBoundary key={team.slug} variant="content">
              <TeamMilestones slug={team.slug} />
            </ErrorBoundary>
          </Grid.Col>
        </Grid>
      </PageShell>
    </Box>
  );
};

export default TeamDetailPage;
