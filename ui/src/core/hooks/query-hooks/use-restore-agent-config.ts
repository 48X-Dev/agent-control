import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type { SetAgentConfigResponse } from '@/core/api/types';

import { agentConfigQueryKey } from './use-agent-config';
import { agentConfigVersionsQueryKey } from './use-agent-config-versions';

type RestoreVariables = {
  versionNum: number;
  expected_version: number;
  note?: string | null;
};

/**
 * Copy an older version's fields forward as a new version.
 *
 * A restore never rewinds the counter. Version 3 restored onto version 9
 * becomes version 10, recorded with `origin: 'restored'`, so the history stays
 * a record of what happened rather than a record somebody rewrote.
 *
 * Two refusals arrive as 409s and neither partially applies: a stored body
 * format this server no longer understands, and a stored model id that has
 * since left the allowlist. For the second, the caller offers the explicit
 * alternative, which is an ordinary save carrying the old body and the model
 * currently configured.
 */
export function useRestoreAgentConfigVersion(agentName: string) {
  const queryClient = useQueryClient();

  return useMutation<SetAgentConfigResponse, Error, RestoreVariables>({
    mutationFn: async ({ versionNum, expected_version, note }) => {
      const { data, error, response } = await api.agentConfigs.restoreVersion(
        agentName,
        versionNum,
        { expected_version, note }
      );
      if (error) {
        throw parseApiError(
          error,
          'That version was not restored',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: agentConfigQueryKey(agentName),
      });
      void queryClient.invalidateQueries({
        queryKey: agentConfigVersionsQueryKey(agentName),
      });
    },
  });
}
