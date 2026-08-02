import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  AgentSessionDetail,
  AgentSessionSummary,
  CreateAgentSessionResponse,
  GetAgentSessionResponse,
  ListAgentSessionsResponse,
} from '@/core/api/types';

const SESSIONS_PAGE_SIZE = 50;

/** How often live turn state is re-read while a turn is running. */
export const SESSION_POLL_INTERVAL_MS = 2000;

export function agentSessionsQueryKey(agentName: string) {
  return ['agent-sessions', 'list', agentName] as const;
}

export function agentSessionQueryKey(sessionKey: string) {
  return ['agent-sessions', 'detail', sessionKey] as const;
}

/**
 * True while the server is waiting on a turn for this session.
 *
 * This is the lock, not the liveness marker. A session whose lock has cleared
 * accepts another turn even when the executor is still working on the previous
 * one, which is a real state after a timeout.
 */
export function isTurnInFlight(
  session: AgentSessionDetail | undefined | null
): boolean {
  return Boolean(session?.in_flight_since);
}

/**
 * True when a previous invocation may still be running on the executor even
 * though this server has stopped waiting for it.
 *
 * Worth surfacing rather than hiding: the agent is still spending, and its
 * output will land in the transcript without anyone asking for it.
 */
export function isExecutorStillWorking(
  session: AgentSessionDetail | undefined | null
): boolean {
  return Boolean(session?.in_flight_trace_id) && !isTurnInFlight(session);
}

/**
 * Chat sessions for one agent, newest first.
 *
 * One page covers any realistic agent, and the switcher is a list rather than
 * an archive browser, so this deliberately does not paginate.
 */
export function useAgentSessions(agentName: string) {
  return useQuery<ListAgentSessionsResponse>({
    queryKey: agentSessionsQueryKey(agentName),
    queryFn: async () => {
      const { data, error, response } = await api.agentSessions.list({
        agent: agentName,
        limit: SESSIONS_PAGE_SIZE,
      });
      if (error) {
        throw parseApiError(error, 'Failed to load chats', response?.status);
      }
      return data!;
    },
    enabled: Boolean(agentName),
  });
}

/**
 * One session's metadata and live turn state.
 *
 * Polls only while something is happening: a turn this panel started
 * (`forcePoll`), a turn holding the lock, or an invocation the executor has
 * not finished. Idle sessions are read once. `staleTime` is `Infinity`
 * globally, so nothing else re-reads this behind the panel's back.
 */
export function useAgentSession(
  sessionKey: string | null,
  options?: { forcePoll?: boolean }
) {
  const forcePoll = options?.forcePoll ?? false;

  return useQuery<GetAgentSessionResponse>({
    queryKey: agentSessionQueryKey(sessionKey ?? ''),
    queryFn: async () => {
      const { data, error, response } = await api.agentSessions.get(
        sessionKey!
      );
      if (error) {
        throw parseApiError(
          error,
          'Failed to load this chat',
          response?.status
        );
      }
      return data!;
    },
    enabled: Boolean(sessionKey),
    refetchInterval: (query) => {
      const session = query.state.data?.session;
      const active =
        forcePoll || isTurnInFlight(session) || isExecutorStillWorking(session);
      return active ? SESSION_POLL_INTERVAL_MS : false;
    },
  });
}

/**
 * Open a chat with an agent.
 *
 * Refusals are worth passing through verbatim rather than flattening: an agent
 * with no executor binding is a 409 with an explanation, and an executor that
 * is switched off or unreachable is a 503. Both are configuration answers, not
 * failures the caller can retry away.
 */
export function useCreateAgentSession(agentName: string) {
  const queryClient = useQueryClient();

  return useMutation<CreateAgentSessionResponse, Error, void>({
    mutationFn: async () => {
      const { data, error, response } = await api.agentSessions.create({
        agent_name: agentName,
      });
      if (error) {
        throw parseApiError(error, 'Failed to open a chat', response?.status);
      }
      return data!;
    },
    onSuccess: (created) => {
      queryClient.setQueryData(
        agentSessionQueryKey(created.session.session_key),
        { session: created.session } satisfies GetAgentSessionResponse
      );
      // Put the new chat at the head of the list before the refetch lands.
      // The panel picks its active session from this list, so without it the
      // chat that was just opened is not the chat that opens.
      queryClient.setQueryData<ListAgentSessionsResponse>(
        agentSessionsQueryKey(agentName),
        (previous) =>
          previous === undefined
            ? previous
            : {
                ...previous,
                sessions: [created.session, ...(previous.sessions ?? [])],
              }
      );
      void queryClient.invalidateQueries({
        queryKey: agentSessionsQueryKey(agentName),
      });
    },
  });
}

/** Label for a session that was never given a title. */
export function sessionLabel(session: AgentSessionSummary): string {
  if (session.title) return session.title;
  const started = new Date(session.created_at);
  if (Number.isNaN(started.getTime())) return 'Untitled chat';
  return `Chat · ${started.toLocaleString()}`;
}
