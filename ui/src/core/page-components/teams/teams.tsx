import { CodeHighlight } from '@mantine/code-highlight';
import {
  Alert,
  Anchor,
  Box,
  Center,
  Group,
  SimpleGrid,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import { IconAlertCircle, IconExternalLink } from '@tabler/icons-react';
import { motion, useReducedMotion } from 'motion/react';

import { useTeamsWithMembers } from '@/core/hooks/query-hooks/use-teams';

import { TeamCard, TeamCardSkeleton } from './team-card';

const SKELETON_CARD_COUNT = 6;
const GRID_COLS = { base: 1, sm: 2, lg: 3 };

const CREATE_TEAM_SNIPPET = `# Create a team (admin API key required)
curl -X PUT http://localhost:8000/api/v1/teams \\
  -H "X-API-Key: $AGENT_CONTROL_ADMIN_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"display_name": "Sales & Outreach"}'

# Assign an agent to it. The slug comes from the display name
curl -X POST \\
  http://localhost:8000/api/v1/teams/sales-outreach/members/support-agent-v1 \\
  -H "X-API-Key: $AGENT_CONTROL_ADMIN_API_KEY"`;

function EmptyTeamsState() {
  return (
    <Center h={400}>
      <Stack align="center" gap="md" maw={600}>
        <Title order={3} fw={600}>
          No teams yet
        </Title>
        <Text size="sm" c="dimmed" ta="center">
          Teams group your agents into departments. Create one with an admin API
          key, then add agents to it. They show up here as a map of your
          organization.
        </Text>
        <Box w="100%">
          <CodeHighlight language="bash" code={CREATE_TEAM_SNIPPET} />
        </Box>
        <Anchor
          href="https://github.com/agentcontrol/agent-control/blob/main/README.md"
          target="_blank"
          size="sm"
          c="blue"
          underline="hover"
        >
          <Group gap={4} align="center">
            <Text size="sm">View docs</Text>
            <IconExternalLink size={14} />
          </Group>
        </Anchor>
      </Stack>
    </Center>
  );
}

const TeamsPage = () => {
  const { teams, isLoading, error } = useTeamsWithMembers();
  const reduceMotion = useReducedMotion();

  return (
    <Stack p="xl" maw={1400} mx="auto" my={0} gap={0}>
      <Group justify="space-between" mb="lg">
        <Stack gap={4}>
          <Title order={2} fw={600}>
            Teams
          </Title>
          <Text size="sm" c="dimmed">
            Every department in your namespace. Pick one to see the agents that
            belong to it.
          </Text>
        </Stack>
      </Group>

      {isLoading ? (
        <SimpleGrid cols={GRID_COLS} spacing="lg" data-testid="teams-loading">
          {Array.from({ length: SKELETON_CARD_COUNT }, (_, index) => (
            <TeamCardSkeleton key={index} />
          ))}
        </SimpleGrid>
      ) : error ? (
        <Alert
          icon={<IconAlertCircle size={16} />}
          title="Error loading teams"
          color="red"
        >
          Failed to fetch teams. Please try again later.
        </Alert>
      ) : teams.length === 0 ? (
        <Box mt="xl">
          <EmptyTeamsState />
        </Box>
      ) : (
        <SimpleGrid cols={GRID_COLS} spacing="lg" data-testid="teams-grid">
          {teams.map((team, index) => (
            <motion.div
              key={team.slug}
              initial={reduceMotion ? false : { opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.2,
                ease: 'easeOut',
                delay: reduceMotion ? 0 : Math.min(index, 8) * 0.04,
              }}
            >
              <TeamCard team={team} />
            </motion.div>
          ))}
        </SimpleGrid>
      )}
    </Stack>
  );
};

export default TeamsPage;
