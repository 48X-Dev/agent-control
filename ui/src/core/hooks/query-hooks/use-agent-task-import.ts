import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  ImportAgentTasksResponse,
  ImportTaskItem,
} from '@/core/api/types';

import { agentTasksQueryKey } from './use-agent-tasks';
import { dispatchStateQueryKey } from './use-dispatch-state';

export function importPreviewQueryKey(
  teamSlug: string,
  scopeRef: string,
  refs: readonly string[],
  dryRun: boolean,
  requeueCompleted: boolean,
  workflowKey: string | null
) {
  // Keyed on the refs themselves rather than on their count. Two sets of the
  // same size are different sets, and a preview cached across that difference
  // would show one and commit the other.
  //
  // `dryRun` is in the key because it is in the request. A cached answer that
  // does not cover every field that produced it is a response that quietly
  // stops describing the request it is displayed next to.
  return [
    'agent-tasks',
    'import-preview',
    teamSlug,
    scopeRef,
    [...refs].sort().join(','),
    dryRun,
    // Here for the same reason as dryRun, and it matters more: this flag
    // changes which rows come back eligible, so a key without it would show a
    // preview of one set and commit another.
    requeueCompleted,
    // The workflow decides the step count and therefore the turn ceiling the
    // operator is agreeing to. A preview cached across a workflow change would
    // display one price and commit another.
    workflowKey ?? '',
  ] as const;
}

type PreviewArgs = {
  teamSlug: string;
  /** What bounded the set, used only to key the cache. */
  scopeRef: string;
  items: ImportTaskItem[];
  workflowKey?: string | null;
  dryRun: boolean;
  /** Offer refs whose only task is finished. Off unless the operator asks. */
  requeueCompleted?: boolean;
  enabled?: boolean;
};

/**
 * What a press would create, without creating any of it.
 *
 * The preview reads, counts and checks; it inserts nothing, so it is safe to
 * run whenever the scope panel is open. Its answer carries the eligible rows
 * themselves and a digest over them, and the digest is what the commit sends
 * back. That is the difference between an authorization and a gesture: a
 * confirm bound to a count cannot tell the operator that one of the four rows
 * is not the row they saw a minute ago.
 *
 * `already_queued` in the response is why pressing twice is harmless. The
 * second press previews an empty set.
 */
export function useImportPreview({
  teamSlug,
  scopeRef,
  items,
  workflowKey,
  dryRun,
  requeueCompleted = false,
  enabled = true,
}: PreviewArgs) {
  const refs = items.map((item) => item.source_ref);

  return useQuery<ImportAgentTasksResponse>({
    queryKey: importPreviewQueryKey(
      teamSlug,
      scopeRef,
      refs,
      dryRun,
      requeueCompleted,
      workflowKey ?? null
    ),
    queryFn: async () => {
      const { data, error, response } = await api.agentTasks.import({
        scope: { kind: 'items', source_kind: 'linear', items },
        team_slug: teamSlug,
        workflow_key: workflowKey ?? null,
        dry_run: dryRun,
        requeue_completed: requeueCompleted,
        mode: 'preview',
      });
      if (error) {
        throw parseApiError(
          error,
          'Could not work out what this would start',
          response?.status
        );
      }
      return data!;
    },
    enabled: enabled && items.length > 0,
    refetchOnWindowFocus: false,
    // Deliberately short. The set the operator is looking at is the set the
    // commit is bound to, and a stale preview turns a legitimate press into a
    // SCOPE_CHANGED refusal they cannot explain.
    staleTime: 10_000,
    retry: false,
  });
}

type CommitArgs = {
  items: ImportTaskItem[];
  expectedRefsDigest: string;
  workflowKey?: string | null;
  dryRun: boolean;
  requeueCompleted?: boolean;
};

/**
 * Create the rows. This is the press, and it is the only call here that writes.
 *
 * It creates rows and starts no process: `agent-control-dispatch serve` polls
 * the queue and claims what it finds, within a poll interval of the press. So a
 * namespace that is paused refuses the commit outright rather than queueing
 * work that will not move.
 *
 * `expected_refs_digest` is required, and a 409 `SCOPE_CHANGED` means the set
 * moved between the confirm and the press. Nothing was created; the caller
 * shows the fresh set and asks again.
 */
export function useCommitImport(teamSlug: string) {
  const queryClient = useQueryClient();

  return useMutation<ImportAgentTasksResponse, Error, CommitArgs>({
    mutationFn: async ({
      items,
      expectedRefsDigest,
      workflowKey,
      dryRun,
      requeueCompleted = false,
    }) => {
      const { data, error, response } = await api.agentTasks.import({
        scope: { kind: 'items', source_kind: 'linear', items },
        team_slug: teamSlug,
        workflow_key: workflowKey ?? null,
        dry_run: dryRun,
        requeue_completed: requeueCompleted,
        mode: 'commit',
        expected_refs_digest: expectedRefsDigest,
      });
      if (error) {
        throw parseApiError(error, 'Nothing was started', response?.status);
      }
      return data!;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['agent-tasks'] });
      queryClient.invalidateQueries({ queryKey: dispatchStateQueryKey() });
      queryClient.invalidateQueries({ queryKey: agentTasksQueryKey() });
    },
  });
}
