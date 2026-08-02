import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type { TurnResponse } from '@/core/api/types';

import { agentSessionQueryKey } from './use-agent-sessions';
import { planQueryKey } from './use-plan';

/**
 * Thrown when the operator stopped waiting for a turn.
 *
 * Not a failure of the turn. The request was abandoned; the agent is still
 * running, still spending, and its output will arrive in the transcript. The
 * panel says so rather than showing an error.
 */
export class TurnAbandonedError extends Error {
  constructor() {
    super('Stopped waiting for this turn. The agent is still working.');
    this.name = 'TurnAbandonedError';
  }
}

export function isTurnAbandoned(error: unknown): error is TurnAbandonedError {
  return error instanceof TurnAbandonedError;
}

/**
 * Send one message and wait for the agent to finish answering.
 *
 * A turn is a blocking request measured in tens of seconds, so this exposes
 * `abandon()`: it aborts the request and nothing else. There is no cancel to
 * call here, because nothing in this stack can stop a running invocation - the
 * turn continues on the executor and the transcript is how it comes back.
 *
 * The transcript is refreshed on every outcome, success or not. A turn that
 * timed out, was refused late, or was abandoned has usually still written
 * messages, and leaving them unread would make the panel disagree with the
 * conversation.
 */
export function useStartTurn(sessionKey: string | null) {
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);

  const refreshSession = useCallback(() => {
    if (!sessionKey) return;
    void queryClient.invalidateQueries({
      queryKey: agentSessionQueryKey(sessionKey),
    });
    void queryClient.invalidateQueries({
      queryKey: ['agent-sessions', 'messages', sessionKey],
    });
    // The plan is polled only while a turn is live, so the poll stops before
    // the steps the agent marked in the last few seconds of the turn are read.
    // Without this the rail can sit on a plan that is one mark out of date
    // until something else happens to refetch it.
    void queryClient.invalidateQueries({
      queryKey: planQueryKey(sessionKey),
    });
  }, [queryClient, sessionKey]);

  const mutation = useMutation<TurnResponse, Error, string>({
    mutationFn: async (message: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const { data, error, response } = await api.agentSessions.startTurn(
          sessionKey!,
          { message },
          { signal: controller.signal }
        );
        if (error) {
          throw parseApiError(error, 'The turn failed', response?.status);
        }
        return data!;
      } catch (caught) {
        if (controller.signal.aborted) throw new TurnAbandonedError();
        throw caught;
      } finally {
        abortRef.current = null;
      }
    },
    onSettled: refreshSession,
  });

  const abandon = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  return { ...mutation, abandon };
}
