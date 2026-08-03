import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isForbiddenError, isNotFoundError } from '@/core/api/errors';
import type { ListAgentWorkflowsResponse } from '@/core/api/types';

export function agentWorkflowsQueryKey() {
  return ['agent-workflows'] as const;
}

/**
 * Every workflow configured in this namespace.
 *
 * Not paginated on the server and small by construction: a workflow is capped
 * at four steps and there are as many of them as somebody configured by hand.
 * The console reads them so a confirm can say which agents a press would run
 * and how many turns they may spend, rather than naming a key and leaving the
 * operator to go and look it up.
 */
export function useAgentWorkflows(options?: { enabled?: boolean }) {
  return useQuery<ListAgentWorkflowsResponse>({
    queryKey: agentWorkflowsQueryKey(),
    queryFn: async () => {
      const { data, error } = await api.agentWorkflows.list();
      if (error) throw error;
      return data!;
    },
    enabled: options?.enabled !== false,
    staleTime: 60_000,
    retry: (failureCount, error) =>
      !isNotFoundError(error) && !isForbiddenError(error) && failureCount < 1,
  });
}
