import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import type {
  KnowledgeSearchResponse,
  KnowledgeStatus,
} from '@/core/api/types';

/**
 * The console's two reads of the company-knowledge mirror.
 *
 * A refusal arrives as HTTP 200 carrying a code, so neither of these treats it
 * as an error: `refusal_code` is data the panel renders as a sentence. Only a
 * transport failure or an authorization refusal reaches `error`, and the panel
 * says something different for those. Collapsing the two would put "the corpus
 * is switched off on this deployment" and "your key was rejected" behind the
 * same red box.
 */

/**
 * The server's hard ceiling, not its default (5), and asked for deliberately.
 *
 * The clamp is the contract either way, so this says "as many as you are
 * willing to give" rather than pinning a number the console would have to
 * chase. A person scanning a page wants the widest one the deployment allows;
 * an agent spending context does not, which is why the default is lower. There
 * is still no cursor, and this is the only knob that moves.
 */
const PAGE_SIZE = 8;

/** What the "what changed" tab asks for. The server clamps it to its own cap. */
const RECENT_DAYS = 14;

export function knowledgeSearchQueryKey(query: string) {
  return ['company-knowledge', 'search', query] as const;
}

export const knowledgeRecentQueryKey = [
  'company-knowledge',
  'recent',
  RECENT_DAYS,
] as const;

/**
 * Ranked search, run only once a person has submitted something.
 *
 * Not keystroke-driven: every call spends from a per-caller window, and a
 * search-as-you-type box would exhaust it in one word and then answer
 * `rate_limited` to the query the person actually meant.
 */
export function useKnowledgeSearch(query: string) {
  return useQuery<KnowledgeSearchResponse>({
    queryKey: knowledgeSearchQueryKey(query),
    enabled: query.length > 0,
    // A corpus does not move between two presses of the same query, and a
    // refetch would spend the window to redraw what is already on screen.
    staleTime: 30_000,
    retry: false,
    queryFn: async () => {
      const { data, error } = await api.companyKnowledge.search({
        query,
        max_results: PAGE_SIZE,
      });
      if (error) throw error;
      return data!;
    },
  });
}

/**
 * What moved lately, and the panel's opening request.
 *
 * It runs on mount because the freshness strip is built from the `corpus`
 * block every response carries: without it the page would have to ask a person
 * to think of a query before it could tell them how far behind the mirror is.
 */
export function useKnowledgeRecent() {
  return useQuery<KnowledgeSearchResponse>({
    queryKey: knowledgeRecentQueryKey,
    staleTime: 30_000,
    retry: false,
    queryFn: async () => {
      const { data, error } = await api.companyKnowledge.recent({
        days: RECENT_DAYS,
        max_results: PAGE_SIZE,
      });
      if (error) throw error;
      return data!;
    },
  });
}

export const knowledgeStatusQueryKey = ['company-knowledge', 'status'] as const;

/**
 * How often the sources view re-reads the status, and the only reason it is
 * this slow: unlike search and recent, the route is not rate limited, so
 * nothing server-side would stop a tighter loop.
 */
const STATUS_POLL_MS = 60_000;

/**
 * What is configured to sync, asked only once somebody opens the sources view.
 *
 * The other two verbs answer "is this mirror current" from the corpus block
 * they already carry, so a page nobody has navigated into has no reason to ask
 * a third time. Somebody watching for a sync to land does want the age to move
 * on its own, hence the poll - foreground only, and no retry on error, because
 * a status that fails once will fail the same way a hundred times a minute.
 */
export function useKnowledgeStatus(enabled: boolean) {
  return useQuery<KnowledgeStatus>({
    queryKey: knowledgeStatusQueryKey,
    enabled,
    staleTime: 30_000,
    retry: false,
    refetchInterval: STATUS_POLL_MS,
    refetchIntervalInBackground: false,
    queryFn: async () => {
      const { data, error } = await api.companyKnowledge.status();
      if (error) throw error;
      return data!;
    },
  });
}
