import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isNotFoundError } from '@/core/api/errors';
import type { ListTeamMilestonesResponse } from '@/core/api/types';

export function teamMilestonesQueryKey(slug: string) {
  return ['team', slug, 'milestones'] as const;
}

/**
 * Linear milestones for one team.
 *
 * A Linear outage is not a failed query: the server turns it into a 200
 * carrying `status: 'error'`, so this hook rejects only when Agent Control
 * itself could not answer. Branch on `data.status`, not on `isError`.
 */
export function useTeamMilestones(slug: string) {
  return useQuery<ListTeamMilestonesResponse>({
    queryKey: teamMilestonesQueryKey(slug),
    queryFn: async () => {
      const { data, error } = await api.teams.getMilestones(slug);
      if (error) throw error;
      return data!;
    },
    enabled: Boolean(slug),
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}
