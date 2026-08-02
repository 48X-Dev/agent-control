import { useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  ListSessionMessagesResponse,
  SessionMessage,
} from '@/core/api/types';

/** How many messages the panel shows at once. */
export const TRANSCRIPT_WINDOW = 200;

/**
 * How many messages one request asks for.
 *
 * Larger than the window on purpose. The tail request is anchored at the end
 * of the transcript as it was on the previous poll, so the slack absorbs
 * everything a running turn appends between polls and keeps a poll to a single
 * round trip.
 */
const FETCH_LIMIT = 500;

/** How often the transcript is re-read while a turn is running. */
export const TRANSCRIPT_POLL_INTERVAL_MS = 2000;

/**
 * `null` means "the end of the transcript". A number is the absolute index the
 * window starts at, which is what "load earlier" sets.
 */
export type TranscriptWindowStart = number | null;

export type TranscriptPage = {
  /** At most {@link TRANSCRIPT_WINDOW} messages, oldest first. */
  messages: SessionMessage[];
  /** Absolute index of the first message shown, or 0 when there are none. */
  firstIndex: number;
  /** Messages the whole transcript holds. */
  total: number;
  /** Whether anything precedes the window. */
  hasEarlier: boolean;
  status: ListSessionMessagesResponse['status'];
  notice: string | null;
};

export function sessionMessagesQueryKey(
  sessionKey: string,
  windowStart: TranscriptWindowStart
) {
  return ['agent-sessions', 'messages', sessionKey, windowStart] as const;
}

async function fetchPage(
  sessionKey: string,
  start: number | null
): Promise<ListSessionMessagesResponse> {
  // `after_index` is exclusive and rejects negatives, so the first message of
  // the transcript is addressed by omitting it.
  const afterIndex = start !== null && start > 0 ? start - 1 : undefined;
  const { data, error, response } = await api.agentSessions.messages(
    sessionKey,
    { after_index: afterIndex, limit: FETCH_LIMIT }
  );
  if (error) {
    throw parseApiError(error, 'Failed to load this chat', response?.status);
  }
  return data!;
}

function toPage(
  raw: ListSessionMessagesResponse,
  windowStart: TranscriptWindowStart
): TranscriptPage {
  const fetched = raw.messages ?? [];
  // Tailing shows the last window of what came back; an explicitly requested
  // window shows its first. Either way the panel renders one window and never
  // grows without bound.
  const messages =
    windowStart === null
      ? fetched.slice(-TRANSCRIPT_WINDOW)
      : fetched.slice(0, TRANSCRIPT_WINDOW);
  const firstIndex = messages[0]?.index ?? 0;
  return {
    messages,
    firstIndex,
    total: raw.total,
    hasEarlier: firstIndex > 0,
    status: raw.status,
    notice: raw.notice ?? null,
  };
}

type UseSessionMessagesArgs = {
  sessionKey: string | null;
  windowStart: TranscriptWindowStart;
  /** Poll while a turn is running. Off otherwise, per the transport decision. */
  isTurnActive: boolean;
};

/**
 * One window of a session's transcript.
 *
 * Reads are capped and explicit. There is no infinite scroll: the panel shows
 * the last {@link TRANSCRIPT_WINDOW} messages and moves the window on request,
 * because a chat that silently loads ten thousand messages is a hang.
 *
 * Tailing needs to know where the end is, and the endpoint pages forward from
 * an index. So the tail request anchors at the end as of the previous poll,
 * taken from this query's own cache. A poll is one request; it costs a second
 * one only when the transcript has outgrown the anchor, which the response
 * says outright via `has_more`.
 */
export function useSessionMessages({
  sessionKey,
  windowStart,
  isTurnActive,
}: UseSessionMessagesArgs) {
  const queryClient = useQueryClient();
  const queryKey = sessionMessagesQueryKey(sessionKey ?? '', windowStart);

  return useQuery<TranscriptPage>({
    queryKey,
    queryFn: async () => {
      const key = sessionKey!;

      if (windowStart !== null) {
        return toPage(await fetchPage(key, windowStart), windowStart);
      }

      const known = queryClient.getQueryData<TranscriptPage>(queryKey);
      const anchor =
        known === undefined
          ? null
          : Math.max(0, known.total - TRANSCRIPT_WINDOW);

      let raw = await fetchPage(key, anchor);
      if (raw.has_more) {
        raw = await fetchPage(key, Math.max(0, raw.total - TRANSCRIPT_WINDOW));
      }
      return toPage(raw, windowStart);
    },
    enabled: Boolean(sessionKey),
    // Only the tail changes under a running turn. Someone reading history is
    // not dragged back to the bottom every two seconds.
    refetchInterval:
      isTurnActive && windowStart === null
        ? TRANSCRIPT_POLL_INTERVAL_MS
        : false,
  });
}
