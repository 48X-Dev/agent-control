import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type { PlanResponse } from '@/core/api/types';

/** How often the plan is re-read while the agent is working. */
export const PLAN_POLL_INTERVAL_MS = 3000;

/**
 * How long a plan may go untouched before the rail calls it stale.
 *
 * Two minutes is well past the pace of an agent that is marking its own steps
 * and well short of an idle chat looking broken. What the rail does with it is
 * say when the last update was; it never infers that work continues, and it
 * never infers that it stopped.
 */
export const PLAN_STALE_AFTER_MS = 120000;

export function planQueryKey(sessionKey: string) {
  return ['agent-sessions', 'plan', sessionKey] as const;
}

/**
 * The plan the agent declared for one session, if it declared one.
 *
 * Read-only, and there is no mutation beside it. Plans are written by the agent
 * with a session-bound runtime token; a console that could edit one would be
 * showing something other than what the agent said, under a label that claims
 * otherwise.
 *
 * Polled only while a turn is live. A finished conversation's plan does not
 * change, so re-reading it every three seconds would be a request per user per
 * idle tab for nothing.
 */
export function usePlan(
  sessionKey: string | null,
  options?: { isTurnActive?: boolean }
) {
  const isTurnActive = options?.isTurnActive ?? false;

  return useQuery<PlanResponse>({
    queryKey: planQueryKey(sessionKey ?? ''),
    queryFn: async () => {
      const { data, error, response } = await api.agentSessions.getPlan(
        sessionKey!
      );
      if (error) {
        throw parseApiError(error, 'Failed to load the plan', response?.status);
      }
      return data!;
    },
    enabled: Boolean(sessionKey),
    refetchInterval: isTurnActive ? PLAN_POLL_INTERVAL_MS : false,
  });
}
