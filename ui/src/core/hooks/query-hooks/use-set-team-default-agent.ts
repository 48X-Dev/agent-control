import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type { PatchTeamResponse } from '@/core/api/types';

import { teamQueryKey, teamsQueryKey } from './use-teams';

/**
 * Set the agent that runs this team's dispatched steps, or clear it with null.
 *
 * Clearing is a supported end state, not a failure to configure: with no
 * default the dispatcher blocks a step that names no agent instead of picking
 * one. Writing a team is an admin operation, so a non-admin credential gets a
 * 403 and the caller is expected to say so.
 */
export function useSetTeamDefaultAgent(slug: string) {
  const queryClient = useQueryClient();

  return useMutation<PatchTeamResponse, Error, string | null>({
    mutationFn: async (agentName) => {
      const { data, error, response } = await api.teams.patch(slug, {
        default_agent_name: agentName,
      });

      if (error) {
        throw parseApiError(
          error,
          'Failed to set the default agent',
          response?.status
        );
      }

      return data!;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: teamQueryKey(slug) });
      queryClient.invalidateQueries({ queryKey: teamsQueryKey });
    },
  });
}
