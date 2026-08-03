import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isNotFoundError, parseApiError } from '@/core/api/errors';
import type {
  GetAgentConfigVersionResponse,
  ListAgentConfigVersionsResponse,
} from '@/core/api/types';

/** One page of history. Enough for the panel without a scroll-to-load. */
export const CONFIG_VERSIONS_PAGE_SIZE = 50;

export function agentConfigVersionsQueryKey(agentName: string) {
  return ['agent-config', agentName, 'versions'] as const;
}

export function agentConfigVersionQueryKey(
  agentName: string,
  versionNum: number
) {
  return ['agent-config', agentName, 'versions', versionNum] as const;
}

/**
 * The version history for one agent's configuration, newest first.
 *
 * Same operation and same tier as reading the current config: anyone who can
 * see what an agent runs today can see what it ran before, including versions
 * whose prompt has since been cleared. Clearing is a state, not a deletion,
 * which is the point of having a history and also the exposure it accepts.
 *
 * Summaries carry no body. The panel fetches a body only when somebody opens
 * a diff, so a history of forty long prompts is one small response.
 */
export function useAgentConfigVersions(agentName: string) {
  return useQuery<ListAgentConfigVersionsResponse>({
    queryKey: agentConfigVersionsQueryKey(agentName),
    queryFn: async () => {
      const { data, error, response } = await api.agentConfigs.listVersions(
        agentName,
        { limit: CONFIG_VERSIONS_PAGE_SIZE }
      );
      if (error) {
        throw parseApiError(
          error,
          'Failed to load the configuration history',
          response?.status
        );
      }
      return data!;
    },
    enabled: Boolean(agentName),
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}

/**
 * One version in full, including its body.
 *
 * Fetched on demand, when a row is opened for a diff or a restore. Version
 * rows are immutable once written, so this is cached indefinitely by key and
 * never invalidated by a save.
 */
export function useAgentConfigVersion(
  agentName: string,
  versionNum: number | null
) {
  return useQuery<GetAgentConfigVersionResponse>({
    queryKey: agentConfigVersionQueryKey(agentName, versionNum ?? -1),
    queryFn: async () => {
      const { data, error, response } = await api.agentConfigs.getVersion(
        agentName,
        versionNum!
      );
      if (error) {
        throw parseApiError(
          error,
          'Failed to load that version',
          response?.status
        );
      }
      return data!;
    },
    enabled: Boolean(agentName) && versionNum !== null,
    staleTime: Infinity,
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}
