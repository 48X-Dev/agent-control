import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  CancelNudgeResponse,
  CreateNudgeResponse,
  ListNudgesResponse,
  Nudge,
} from '@/core/api/types';

/** Ceiling on one nudge, matching `NUDGE_BODY_MAX_LENGTH` on the server. */
export const NUDGE_BODY_MAX_LENGTH = 2000;

/** How many nudges the agent is shown per model call, oldest first. */
export const NUDGE_MAX_PER_MODEL_CALL = 3;

/**
 * How often the queue is re-read while something is happening.
 *
 * The queue changes without this panel doing anything: an executor claims and
 * applies nudges on its own schedule, so a static list would show "queued"
 * long after the agent read it.
 */
export const NUDGE_POLL_INTERVAL_MS = 2000;

/** Statuses that are still on their way to a model. */
export const NUDGE_LIVE_STATUSES = new Set<Nudge['status']>([
  'pending',
  'claimed',
]);

export function nudgesQueryKey(sessionKey: string) {
  return ['agent-sessions', 'nudges', sessionKey] as const;
}

export function isNudgeLive(nudge: Nudge): boolean {
  return NUDGE_LIVE_STATUSES.has(nudge.status);
}

/**
 * The nudges queued for one session.
 *
 * Polls while anything is live or a turn is running, and stops otherwise. A
 * queue with nothing in it does not need a request every two seconds for the
 * rest of the session.
 */
export function useNudges(
  sessionKey: string | null,
  options?: { isTurnActive?: boolean }
) {
  const isTurnActive = options?.isTurnActive ?? false;

  return useQuery<ListNudgesResponse>({
    queryKey: nudgesQueryKey(sessionKey ?? ''),
    queryFn: async () => {
      const { data, error, response } = await api.agentSessions.listNudges(
        sessionKey!
      );
      if (error) {
        throw parseApiError(error, 'Failed to load nudges', response?.status);
      }
      return data!;
    },
    enabled: Boolean(sessionKey),
    refetchInterval: (query) => {
      const nudges = query.state.data?.nudges ?? [];
      const live = nudges.some(isNudgeLive);
      return live || isTurnActive ? NUDGE_POLL_INTERVAL_MS : false;
    },
  });
}

/**
 * Queue one piece of guidance.
 *
 * It does not arrive now, and the panel says so rather than implying the agent
 * stopped to read it: delivery happens at the agent's next model call, which
 * is after whatever tool it is currently running finishes.
 */
export function useCreateNudge(sessionKey: string | null) {
  const queryClient = useQueryClient();

  return useMutation<CreateNudgeResponse, Error, string>({
    mutationFn: async (body: string) => {
      const { data, error, response } = await api.agentSessions.createNudge(
        sessionKey!,
        { body }
      );
      if (error) {
        throw parseApiError(
          error,
          'The nudge was not queued',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: () => {
      if (!sessionKey) return;
      void queryClient.invalidateQueries({
        queryKey: nudgesQueryKey(sessionKey),
      });
    },
  });
}

/**
 * Withdraw a nudge nobody has claimed yet.
 *
 * A claimed nudge answers 409, and the panel shows that refusal rather than
 * hiding the row: the text may already be inside a model request, and a
 * withdrawal that did not happen is worse than one that was refused.
 */
export function useCancelNudge(sessionKey: string | null) {
  const queryClient = useQueryClient();

  return useMutation<CancelNudgeResponse, Error, number>({
    mutationFn: async (nudgeId: number) => {
      const { data, error, response } = await api.agentSessions.cancelNudge(
        sessionKey!,
        nudgeId
      );
      if (error) {
        throw parseApiError(
          error,
          'The nudge could not be withdrawn',
          response?.status
        );
      }
      return data!;
    },
    onSettled: () => {
      if (!sessionKey) return;
      void queryClient.invalidateQueries({
        queryKey: nudgesQueryKey(sessionKey),
      });
    },
  });
}
