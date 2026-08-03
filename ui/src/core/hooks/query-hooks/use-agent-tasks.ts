import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isNotFoundError } from '@/core/api/errors';
import type {
  AgentTaskStatus,
  GetAgentTaskChainResponse,
  ListAgentTasksResponse,
} from '@/core/api/types';

/** Statuses that can still move on their own. Anything else is settled. */
const LIVE_STATUSES: ReadonlySet<AgentTaskStatus> = new Set<AgentTaskStatus>([
  'queued',
  'running',
  'paused_quota',
  'awaiting_approval',
]);

const LIVE_POLL_MS = 5_000;

export function agentTasksQueryKey(params?: {
  team?: string;
  status?: AgentTaskStatus;
}) {
  return ['agent-tasks', params?.team ?? null, params?.status ?? null] as const;
}

export function agentTaskChainQueryKey(taskKey: string) {
  return ['agent-tasks', taskKey, 'chain'] as const;
}

/** True when at least one task in the page can still change without a person. */
export function hasLiveTask(response: ListAgentTasksResponse | undefined) {
  return Boolean(
    response?.tasks.some((task) => LIVE_STATUSES.has(task.status))
  );
}

export function isLiveStatus(status: AgentTaskStatus) {
  return LIVE_STATUSES.has(status);
}

/**
 * A page of the dispatch ledger, oldest first.
 *
 * Polls while anything in the page is still moving and stops when everything is
 * settled. There is no push channel here and a task that finishes silently
 * reads as a task that hung, which is the failure this poll exists to avoid.
 */
export function useAgentTasks(params: {
  team?: string;
  status?: AgentTaskStatus;
  limit?: number;
  enabled?: boolean;
}) {
  const { team, status, limit = 100, enabled = true } = params;

  return useQuery<ListAgentTasksResponse>({
    queryKey: agentTasksQueryKey({ team, status }),
    queryFn: async () => {
      const { data, error } = await api.agentTasks.list({
        team,
        status,
        limit,
      });
      if (error) throw error;
      return data!;
    },
    enabled,
    refetchInterval: (query) =>
      hasLiveTask(query.state.data) ? LIVE_POLL_MS : false,
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}

/**
 * What actually ran on one task, hop by hop.
 *
 * The planned positions are merged with the recorded step rows, so a hop can
 * say it never ran. "The writer found nothing" and "the writer never started"
 * are different answers and a list of rows alone cannot tell them apart.
 */
export function useAgentTaskChain(
  taskKey: string | null,
  options?: { enabled?: boolean; live?: boolean }
) {
  const enabled = Boolean(taskKey) && options?.enabled !== false;

  return useQuery<GetAgentTaskChainResponse>({
    queryKey: agentTaskChainQueryKey(taskKey ?? ''),
    queryFn: async () => {
      const { data, error } = await api.agentTasks.getChain(taskKey!);
      if (error) throw error;
      return data!;
    },
    enabled,
    refetchInterval: options?.live ? LIVE_POLL_MS : false,
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}
