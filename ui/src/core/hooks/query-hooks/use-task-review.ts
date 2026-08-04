import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isConflictError, parseApiError } from '@/core/api/errors';
import type {
  AcceptAgentTaskResponse,
  ListReviewQueueResponse,
  ListTeamMilestonesResponse,
  RejectAgentTaskResponse,
} from '@/core/api/types';

import { milestoneIssuesQueryKey } from './use-milestone-issues';
import { teamMilestonesQueryKey } from './use-team-milestones';

export function reviewQueueQueryKey(
  team?: string | null,
  milestoneId?: string | null
) {
  return ['agent-tasks-review', team ?? null, milestoneId ?? null] as const;
}

/**
 * The proposals waiting for a human in one milestone's scope.
 *
 * Each entry is an agent's claim to have finished, beside the issue it would
 * close read live from Linear, and the digest an accept must echo. Reading
 * this list decides nothing and holds nothing; entries never expire out of it.
 *
 * Kept off window-focus refetch for the same reason the issues read is: every
 * fetch reads Linear per entry through the server, and alt-tab must not spend
 * a shared workspace rate limit.
 */
export function useReviewQueue(params: {
  team: string;
  milestoneId: string;
  enabled?: boolean;
}) {
  const { team, milestoneId, enabled = true } = params;

  return useQuery<ListReviewQueueResponse>({
    queryKey: reviewQueueQueryKey(team, milestoneId),
    queryFn: async () => {
      const { data, error, response } = await api.agentTasks.review({
        team,
        milestone_id: milestoneId,
        limit: 50,
      });
      if (error) {
        throw parseApiError(
          error,
          'The review queue could not be read',
          response?.status
        );
      }
      return data!;
    },
    enabled: enabled && Boolean(team) && Boolean(milestoneId),
    refetchOnWindowFocus: false,
    staleTime: 15_000,
    retry: false,
  });
}

type AcceptArgs = {
  taskKey: string;
  writebackId: number;
  /** Exactly the digest the card showed. The server refuses anything moved. */
  expectedDecisionDigest: string;
};

type ReviewScope = {
  teamSlug: string;
  milestoneId: string;
};

/**
 * A human agreeing that this task's issue may be closed. One entry, one press.
 *
 * The response carries the milestone's new progress, and it is written into
 * the milestones cache directly rather than refetched: the server's milestone
 * cache is per replica, so an immediate refetch could land on a stale one and
 * repaint the bar the reviewer just moved back to where it was. The next
 * natural refetch corrects within one TTL either way.
 *
 * A 409 means the card went stale under the reviewer: the digest, the issue's
 * team or milestone, or the approver themselves was refused. The queue is
 * re-read so what is on screen is what a second press would be bound to.
 */
export function useAcceptAgentTask({ teamSlug, milestoneId }: ReviewScope) {
  const queryClient = useQueryClient();

  return useMutation<AcceptAgentTaskResponse, Error, AcceptArgs>({
    mutationFn: async ({ taskKey, writebackId, expectedDecisionDigest }) => {
      const { data, error, response } = await api.agentTasks.accept(taskKey, {
        writeback_id: writebackId,
        expected_decision_digest: expectedDecisionDigest,
      });
      if (error) {
        throw parseApiError(error, 'Nothing was closed', response?.status);
      }
      return data!;
    },
    onSuccess: (data) => {
      if (typeof data.milestone_progress === 'number') {
        const progress = data.milestone_progress;
        queryClient.setQueryData<ListTeamMilestonesResponse>(
          teamMilestonesQueryKey(teamSlug),
          (old) =>
            old
              ? {
                  ...old,
                  milestones: old.milestones.map((milestone) =>
                    milestone.id === milestoneId
                      ? { ...milestone, progress }
                      : milestone
                  ),
                }
              : old
        );
      }
      queryClient.invalidateQueries({ queryKey: ['agent-tasks-review'] });
      queryClient.invalidateQueries({ queryKey: ['agent-tasks'] });
      // The closed issue leaves the eligible set, and the server invalidated
      // its own issues cache on the accept, so this refetch reads fresh.
      queryClient.invalidateQueries({
        queryKey: milestoneIssuesQueryKey(teamSlug, milestoneId),
      });
    },
    onError: (error) => {
      if (isConflictError(error)) {
        queryClient.invalidateQueries({ queryKey: ['agent-tasks-review'] });
      }
    },
  });
}

type RejectArgs = {
  taskKey: string;
  writebackId: number;
  /** Why not, in the reviewer's words. Recorded on the row for good. */
  reason: string;
};

/**
 * A human declining the proposal. Writes nothing to Linear, so it works with
 * the write flag off; the task stays completed and the issue stays open.
 */
export function useRejectAgentTask() {
  const queryClient = useQueryClient();

  return useMutation<RejectAgentTaskResponse, Error, RejectArgs>({
    mutationFn: async ({ taskKey, writebackId, reason }) => {
      const { data, error, response } = await api.agentTasks.reject(taskKey, {
        writeback_id: writebackId,
        reason,
      });
      if (error) {
        throw parseApiError(
          error,
          'The rejection was not recorded',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-tasks-review'] });
    },
    onError: (error) => {
      if (isConflictError(error)) {
        queryClient.invalidateQueries({ queryKey: ['agent-tasks-review'] });
      }
    },
  });
}
