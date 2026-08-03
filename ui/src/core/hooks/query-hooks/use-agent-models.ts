import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isForbiddenError, parseApiError } from '@/core/api/errors';
import type { ListAgentModelsResponse } from '@/core/api/types';

export const agentModelsQueryKey = ['agent-models'] as const;

/**
 * The models this server offers.
 *
 * Server configuration, not a live query against a vendor: an endpoint's own
 * `/v1/models` lists what it serves rather than what this product can use, and
 * the one this feature was built against advertises an image model that would
 * save cleanly and then fail three layers away.
 *
 * The route takes the *write* operation, so a 403 here is the honest answer to
 * two questions at once: there is no list to populate, and this credential
 * cannot save either. A refusal is not retried.
 */
export function useAgentModels() {
  return useQuery<ListAgentModelsResponse>({
    queryKey: agentModelsQueryKey,
    queryFn: async () => {
      const { data, error, response } = await api.agentModels.list();
      if (error) {
        throw parseApiError(
          error,
          'Failed to load the model allowlist',
          response?.status
        );
      }
      return data!;
    },
    retry: (failureCount, error) =>
      !isForbiddenError(error) && failureCount < 1,
  });
}

/**
 * Whether this credential may write an agent's configuration.
 *
 * Derived from the allowlist route rather than from the login response,
 * because a session resumed from a cookie reports `isAdmin: false` whatever
 * the key behind it actually is. `GET /agent-models` is gated on the same
 * operation as every write on this tab, so its answer is the one that matters:
 * a 403 means Save would be refused too.
 *
 * Undefined while the probe is in flight, so callers can hold off on
 * disabling controls they are about to enable.
 */
export function useCanWriteAgentConfig(): {
  canWrite: boolean | undefined;
  isLoading: boolean;
} {
  const query = useAgentModels();

  if (query.isPending) return { canWrite: undefined, isLoading: true };
  if (isForbiddenError(query.error))
    return { canWrite: false, isLoading: false };
  // Any other failure is a server or network problem rather than a statement
  // about this credential. Leaving the controls enabled lets the save produce
  // the real error instead of a guess made here.
  return { canWrite: true, isLoading: false };
}
