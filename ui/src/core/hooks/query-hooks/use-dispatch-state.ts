import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isForbiddenError, isNotFoundError } from '@/core/api/errors';
import type { GetDispatchStateResponse } from '@/core/api/types';

export function dispatchStateQueryKey() {
  return ['agent-dispatch', 'state'] as const;
}

/**
 * The namespace's two stop switches and its hourly budget.
 *
 * Rendered, never enforced here. Both refusals live on the turn path inside the
 * server, which is the only place a process cannot route around its own budget.
 * What this read buys is a banner: a paused namespace that says so beats four
 * tasks queued at a fleet that will not run them.
 */
export function useDispatchState(options?: { enabled?: boolean }) {
  return useQuery<GetDispatchStateResponse>({
    queryKey: dispatchStateQueryKey(),
    queryFn: async () => {
      const { data, error } = await api.agentDispatch.get();
      if (error) throw error;
      return data!;
    },
    enabled: options?.enabled !== false,
    staleTime: 15_000,
    retry: (failureCount, error) =>
      !isNotFoundError(error) && !isForbiddenError(error) && failureCount < 1,
  });
}
