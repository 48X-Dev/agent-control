import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  CreateHaltResponse,
  Halt,
  ListHaltsResponse,
} from '@/core/api/types';

import { agentSessionQueryKey } from './use-agent-sessions';

/** How often stops are re-read while one is outstanding. */
export const HALT_POLL_INTERVAL_MS = 2000;

/**
 * How long a stop may sit acknowledged-but-not-ended before the panel warns.
 *
 * The acknowledgement comes from the process being stopped. When it says it
 * blocked and the turn has still not ended, something is wrong in the
 * direction that matters, and a person watching deserves to be told rather
 * than shown a spinner.
 */
export const HALT_STALL_WARNING_MS = 20000;

export function haltsQueryKey(sessionKey: string) {
  return ['agent-sessions', 'halts', sessionKey] as const;
}

/** A stop that has been asked for but whose turn has not ended. */
export function isHaltOutstanding(halt: Halt): boolean {
  return halt.turn_ended_at == null && halt.status !== 'expired';
}

/**
 * The stop bound to the turn now in flight, if there is one.
 *
 * Matched on the turn's trace rather than on recency: a stop belongs to
 * exactly one turn and is never carried into another, which is what stops a
 * stale record from being rendered against work nobody asked to stop.
 */
export function haltForTurn(
  halts: Halt[] | undefined,
  traceId: string | null | undefined
): Halt | null {
  if (!traceId) return null;
  return halts?.find((halt) => halt.target_trace_id === traceId) ?? null;
}

export function useHalts(
  sessionKey: string | null,
  options?: { isTurnActive?: boolean }
) {
  const isTurnActive = options?.isTurnActive ?? false;

  return useQuery<ListHaltsResponse>({
    queryKey: haltsQueryKey(sessionKey ?? ''),
    queryFn: async () => {
      const { data, error, response } = await api.agentSessions.listHalts(
        sessionKey!
      );
      if (error) {
        throw parseApiError(error, 'Failed to load stops', response?.status);
      }
      return data!;
    },
    enabled: Boolean(sessionKey),
    refetchInterval: (query) => {
      const halts = query.state.data?.halts ?? [];
      const outstanding = halts.some(isHaltOutstanding);
      return outstanding || isTurnActive ? HALT_POLL_INTERVAL_MS : false;
    },
  });
}

/**
 * Stop the turn this session is running.
 *
 * What this does, exactly: the agent stops at its next boundary, before its
 * next model call or before its next tool runs. A tool that has already
 * started finishes, and whatever it was doing has already happened. Every
 * piece of copy around this mutation says so before the click, not after.
 *
 * Pressing stop twice is one stop and answers 200 both times.
 */
export function useCreateHalt(sessionKey: string | null) {
  const queryClient = useQueryClient();

  return useMutation<CreateHaltResponse, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await api.agentSessions.createHalt(
        sessionKey!
      );
      if (error) {
        throw parseApiError(
          error,
          'The agent could not be stopped',
          response?.status
        );
      }
      return data!;
    },
    onSettled: () => {
      if (!sessionKey) return;
      void queryClient.invalidateQueries({
        queryKey: haltsQueryKey(sessionKey),
      });
      // The turn's own state is what says whether the stop landed, so it is
      // re-read too. "Applied" is the executor's word; the turn ending is the
      // server's.
      void queryClient.invalidateQueries({
        queryKey: agentSessionQueryKey(sessionKey),
      });
    },
  });
}
