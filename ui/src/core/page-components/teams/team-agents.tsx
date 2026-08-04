import {
  Alert,
  Badge,
  Box,
  Center,
  Group,
  Loader,
  Skeleton,
  Stack,
  Text,
  UnstyledButton,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import {
  IconAlertCircle,
  IconChevronRight,
  IconMessage,
  IconRobot,
} from '@tabler/icons-react';
import Link from 'next/link';
import { useMemo } from 'react';

import type { AgentSummary } from '@/core/api/types';
import { getAgentRoute } from '@/core/constants/agent-routes';
import { useTeamAgents } from '@/core/hooks/query-hooks/use-team-agents';
import { useInfiniteScroll } from '@/core/hooks/use-infinite-scroll';

import { TeamDefaultAgent } from './team-default-agent';
import classes from './team-detail.module.css';

const SKELETON_ROW_COUNT = 4;

/**
 * One agent, with a direct route into a conversation with it.
 *
 * The chat link is a sibling of the row rather than a child: the row is
 * already a link, and an anchor inside an anchor is not something a browser
 * agrees to render predictably.
 */
function AgentRow({ agent }: { agent: AgentSummary }) {
  return (
    <Group gap="xs" align="center" wrap="nowrap">
      <UnstyledButton
        component={Link}
        href={getAgentRoute(agent.agent_name)}
        className={classes.agentRow}
        style={{ flex: 1, minWidth: 0 }}
        data-testid="team-agent-row"
        data-agent-name={agent.agent_name}
      >
        <Group justify="space-between" align="center" wrap="nowrap">
          <Group gap="sm" align="center" wrap="nowrap" style={{ minWidth: 0 }}>
            <IconRobot
              size={16}
              stroke={2}
              color="var(--mantine-color-dimmed)"
              style={{ flexShrink: 0 }}
            />
            <Text size="sm" fw={500} lineClamp={1}>
              {agent.agent_name}
            </Text>
          </Group>
          <Group gap="sm" align="center" wrap="nowrap">
            <Badge size="sm" variant="light" color="gray">
              {agent.active_controls_count === 1
                ? '1 control'
                : `${agent.active_controls_count} controls`}
            </Badge>
            <IconChevronRight
              size={16}
              stroke={2}
              className={classes.chevron}
            />
          </Group>
        </Group>
      </UnstyledButton>

      <Button
        component={Link}
        href={getAgentRoute(agent.agent_name, { tab: 'chat' })}
        variant="outline"
        size="sm"
        leftSection={<IconMessage size={14} />}
        data-testid="team-agent-chat"
        data-agent-name={agent.agent_name}
      >
        Chat
      </Button>
    </Group>
  );
}

export function TeamAgents({ slug }: { slug: string }) {
  const {
    data,
    error,
    isLoading,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    refetch,
  } = useTeamAgents(slug);

  const { sentinelRef } = useInfiniteScroll({
    hasNextPage: hasNextPage ?? false,
    isFetchingNextPage,
    fetchNextPage,
  });

  const agents = useMemo(
    () => data?.pages.flatMap((page) => page.agents) ?? [],
    [data]
  );

  const renderBody = () => {
    if (isLoading) {
      return (
        <Stack gap="xs" data-testid="team-agents-loading">
          {Array.from({ length: SKELETON_ROW_COUNT }, (_, index) => (
            <Skeleton key={index} height={46} radius="sm" />
          ))}
        </Stack>
      );
    }

    if (error) {
      return (
        <Alert
          icon={<IconAlertCircle size={16} />}
          title="Error loading agents"
          color="red"
          data-testid="team-agents-error"
        >
          <Stack gap="sm" align="flex-start">
            <Text size="sm">
              Failed to fetch the agents in this team. Please try again later.
            </Text>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void refetch()}
              data-testid="team-agents-retry"
            >
              Try again
            </Button>
          </Stack>
        </Alert>
      );
    }

    if (agents.length === 0) {
      return (
        <Stack gap={4} py="md" align="center" data-testid="team-agents-empty">
          <Text size="sm" fw={500}>
            No agents in this team
          </Text>
          <Text size="xs" c="dimmed" ta="center">
            {`Add one with an admin API key: POST /api/v1/teams/${slug}/members/<agent-name>`}
          </Text>
        </Stack>
      );
    }

    return (
      <>
        <Stack gap="xs" data-testid="team-agents-list">
          {agents.map((agent) => (
            <AgentRow key={agent.agent_name} agent={agent} />
          ))}
        </Stack>

        <div ref={sentinelRef} style={{ height: 1 }} />

        {isFetchingNextPage ? (
          <Center p="sm">
            <Loader size="sm" />
          </Center>
        ) : null}
      </>
    );
  };

  return (
    <Box className={classes.panel} data-testid="team-agents-panel">
      <Stack gap="md">
        <Group gap={8} align="center">
          <Text size="sm" fw={600}>
            Agents
          </Text>
          {agents.length > 0 ? (
            <Text size="xs" c="dimmed" data-testid="team-agents-count">
              {agents.length} shown
            </Text>
          ) : null}
        </Group>

        {/* Above the list, not inside it: the default names one of these
            agents, and which one it is decides the controls a dispatched
            task runs under. */}
        <TeamDefaultAgent slug={slug} />

        {renderBody()}
      </Stack>
    </Box>
  );
}
