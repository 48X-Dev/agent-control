import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isNotFoundError } from '@/core/api/errors';
import type { ListMilestoneIssuesResponse } from '@/core/api/types';

export function milestoneIssuesQueryKey(slug: string, milestoneId: string) {
  return ['team', slug, 'milestones', milestoneId, 'issues'] as const;
}

/**
 * The issues in one milestone that this team's agents could be pointed at.
 *
 * Read only, and scoped on the server to the milestone plus the team's own
 * Linear key. Nothing here claims an issue or starts anything: it answers what
 * a press would cover, which is what the confirm has to show before anybody
 * agrees to it.
 *
 * A Linear outage arrives as a 200 carrying `status: 'error'`, exactly as the
 * milestone panel does, so branch on `data.status` rather than on `isError`.
 * Unlike that panel, the server never serves a stale cached set in place of an
 * error here: an hour-old board beats an error, and an hour-old list of work to
 * start does not.
 *
 * Fetched lazily. The request reaches a third party, so it fires when an
 * operator opens the scope panel and not on every render of the milestone list.
 */
export function useMilestoneIssues(
  slug: string,
  milestoneId: string | null,
  options?: { enabled?: boolean }
) {
  const enabled =
    Boolean(slug) && Boolean(milestoneId) && options?.enabled !== false;

  return useQuery<ListMilestoneIssuesResponse>({
    queryKey: milestoneIssuesQueryKey(slug, milestoneId ?? ''),
    queryFn: async () => {
      const { data, error } = await api.teams.getMilestoneIssues(
        slug,
        milestoneId!
      );
      if (error) throw error;
      return data!;
    },
    enabled,
    // One outbound read per open. Refetching on window focus would spend a
    // shared workspace rate limit every time somebody alt-tabs back.
    refetchOnWindowFocus: false,
    staleTime: 30_000,
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}
