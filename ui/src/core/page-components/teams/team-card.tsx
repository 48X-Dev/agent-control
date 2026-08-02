import {
  Box,
  Group,
  Skeleton,
  Stack,
  Text,
  UnstyledButton,
} from '@mantine/core';
import { IconChevronRight, IconUsers } from '@tabler/icons-react';
import Link from 'next/link';
import type { CSSProperties } from 'react';

import { getTeamAccent, getTeamInitials } from '@/core/constants/team-colors';
import { getTeamRoute } from '@/core/constants/team-routes';
import type { TeamWithMembers } from '@/core/hooks/query-hooks/use-teams';

import classes from './teams.module.css';

const MEMBER_PREVIEW_LIMIT = 4;

function memberCountLabel(count: number): string {
  if (count === 0) return 'No agents';
  return count === 1 ? '1 agent' : `${count} agents`;
}

function MemberPreview({ team }: { team: TeamWithMembers }) {
  if (team.member_count === 0) {
    return (
      <Text size="xs" c="dimmed" fs="italic">
        No agents assigned yet
      </Text>
    );
  }

  if (team.isLoadingMembers) {
    return (
      <div className={classes.members}>
        <Skeleton height={20} width={88} radius="sm" />
        <Skeleton height={20} width={64} radius="sm" />
      </div>
    );
  }

  // Names are absent when the per-team read failed. The count above still tells
  // the real story, so the card drops the pills rather than holding a skeleton
  // that will never resolve.
  if (!team.memberNames || team.memberNames.length === 0) {
    return null;
  }

  const preview = team.memberNames.slice(0, MEMBER_PREVIEW_LIMIT);
  const remaining = team.memberNames.length - preview.length;

  return (
    <div className={classes.members}>
      {preview.map((name) => (
        <span key={name} className={classes.memberPill}>
          {name}
        </span>
      ))}
      {remaining > 0 ? (
        <span className={`${classes.memberPill} ${classes.overflowPill}`}>
          +{remaining} more
        </span>
      ) : null}
    </div>
  );
}

export function TeamCard({ team }: { team: TeamWithMembers }) {
  const accent = getTeamAccent(team.slug);
  const countLabel = memberCountLabel(team.member_count);

  const accentStyle = {
    '--team-accent-solid': accent.solid,
    '--team-accent-surface': accent.surface,
    '--team-accent-foreground': accent.foreground,
  } as CSSProperties;

  return (
    <UnstyledButton
      component={Link}
      href={getTeamRoute(team.slug)}
      className={classes.card}
      style={accentStyle}
      aria-label={`${team.display_name} team, ${countLabel}`}
      data-testid="team-card"
      data-team-slug={team.slug}
    >
      <Stack gap="md">
        <Group justify="space-between" align="flex-start" wrap="nowrap">
          <Group gap="sm" align="center" wrap="nowrap">
            <Box className={classes.marker} aria-hidden="true">
              {getTeamInitials(team.display_name)}
            </Box>
            <Stack gap={2}>
              <Text size="md" fw={600} className={classes.title} lineClamp={1}>
                {team.display_name}
              </Text>
              <Text size="xs" c="dimmed" lineClamp={1}>
                {team.slug}
              </Text>
            </Stack>
          </Group>
          <IconChevronRight size={18} stroke={2} className={classes.chevron} />
        </Group>

        {team.description ? (
          <Text size="sm" c="dimmed" lineClamp={2}>
            {team.description}
          </Text>
        ) : null}

        <Group gap={6} align="center">
          <IconUsers size={14} stroke={2} color="var(--mantine-color-dimmed)" />
          <Text size="sm" fw={500} data-testid="team-member-count">
            {countLabel}
          </Text>
        </Group>

        <MemberPreview team={team} />
      </Stack>
    </UnstyledButton>
  );
}

export function TeamCardSkeleton() {
  return (
    <div className={classes.skeletonCard}>
      <Stack gap="md">
        <Group gap="sm" align="center" wrap="nowrap">
          <Skeleton height={40} width={40} radius="md" />
          <Stack gap={6} style={{ flex: 1 }}>
            <Skeleton height={14} width="55%" radius="sm" />
            <Skeleton height={10} width="35%" radius="sm" />
          </Stack>
        </Group>
        <Skeleton height={12} width="40%" radius="sm" />
        <Group gap={6}>
          <Skeleton height={20} width={88} radius="sm" />
          <Skeleton height={20} width={64} radius="sm" />
        </Group>
      </Stack>
    </div>
  );
}
