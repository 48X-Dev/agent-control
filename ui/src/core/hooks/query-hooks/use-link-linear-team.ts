import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type { PatchTeamResponse } from '@/core/api/types';

import { teamMilestonesQueryKey } from './use-team-milestones';
import { teamQueryKey, teamsQueryKey } from './use-teams';

/**
 * Point a team at a Linear team, or unlink it by passing null.
 *
 * The key is a Linear team identifier such as `ENG`, not a credential; the
 * Linear API key stays on the server and is never part of this exchange.
 * Writing a team is an admin operation, so a non-admin caller gets a 403 and
 * the caller is expected to surface it.
 */
export function useLinkLinearTeam(slug: string) {
  const queryClient = useQueryClient();

  return useMutation<PatchTeamResponse, Error, string | null>({
    mutationFn: async (linearTeamKey) => {
      const { data, error, response } = await api.teams.patch(slug, {
        linear_team_key: linearTeamKey,
      });

      if (error) {
        throw parseApiError(
          error,
          'Failed to link the Linear team',
          response?.status
        );
      }

      return data!;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: teamQueryKey(slug) });
      queryClient.invalidateQueries({ queryKey: teamMilestonesQueryKey(slug) });
      queryClient.invalidateQueries({ queryKey: teamsQueryKey });
    },
  });
}
