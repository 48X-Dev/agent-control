import { useInfiniteQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import type { ListAgentsResponse } from '@/core/api/types';

const TEAM_AGENTS_PAGE_SIZE = 20;

export function teamAgentsQueryKey(slug: string) {
  return ['agents', 'team', slug] as const;
}

/**
 * The agents that belong to one team, newest first, paginated.
 *
 * Reads the agents endpoint with its `team` filter rather than the membership
 * list on the team itself, so each row carries the same fields the agents
 * overview shows and links to the same detail page.
 */
export function useTeamAgents(slug: string) {
  return useInfiniteQuery({
    queryKey: teamAgentsQueryKey(slug),
    queryFn: async ({ pageParam }: { pageParam: string | undefined }) => {
      const { data, error } = await api.agents.listByTeam({
        team: slug,
        cursor: pageParam,
        limit: TEAM_AGENTS_PAGE_SIZE,
      });
      if (error) throw error;
      return data!;
    },
    getNextPageParam: (lastPage: ListAgentsResponse) =>
      lastPage.pagination.next_cursor ?? undefined,
    initialPageParam: undefined,
    enabled: Boolean(slug),
  });
}
