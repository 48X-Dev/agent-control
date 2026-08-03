import { Alert, Group, Loader, Stack, Text } from '@mantine/core';
import { useEffect, useMemo, useState } from 'react';

import type {
  AgentTaskSummary,
  DispatchStateSnapshot,
  ImportTaskItem,
} from '@/core/api/types';
import {
  useCommitImport,
  useImportPreview,
} from '@/core/hooks/query-hooks/use-agent-task-import';
import { useAgentWorkflows } from '@/core/hooks/query-hooks/use-agent-workflows';
import { useMilestoneIssues } from '@/core/hooks/query-hooks/use-milestone-issues';

import { DispatchBanners } from './dispatch-banners';
import { safeHttpUrl, truncateWithMarker } from './formatting';
import { ResultsForReview } from './results-for-review';
import { ScopeConfirm } from './scope-confirm';
import { TaskRunList } from './task-runs';

/** The ledger's own limits. Anything longer is cut here, visibly. */
const TASK_TITLE_MAX_LENGTH = 500;
const TASK_BODY_MAX_LENGTH = 20000;

/**
 * What one milestone's scope turned out to be, reported to the row above.
 *
 * The row needs it for two things it cannot work out on its own: whether the
 * play control has anything to offer, and which ledger rows belong to this
 * milestone so the agent-step bar counts the right ones.
 */
export type MilestoneScope = {
  milestoneId: string;
  refs: string[];
  identifiersByRef: Map<string, string>;
  urlsByRef: Map<string, string>;
  /** Eligible after the ledger's own duplicate check, or null while unknown. */
  eligibleCount: number | null;
  stepsPerTask: number;
};

export type MilestoneWorkProps = {
  teamSlug: string;
  milestoneId: string;
  milestoneName: string;
  linearTeamKey: string;
  /** Ledger rows for this team, fetched once by the panel and shared. */
  teamTasks: AgentTaskSummary[];
  dispatchState: DispatchStateSnapshot | null | undefined;
  now: number;
  onScopeResolved: (scope: MilestoneScope) => void;
};

/** Ledger rows whose issue is in this milestone's eligible-or-queued set. */
function tasksForRefs(tasks: AgentTaskSummary[], refs: Set<string>) {
  return tasks.filter(
    (task) => task.source_kind === 'linear' && refs.has(task.source_ref)
  );
}

const SETTLED_STATUSES = new Set(['completed']);

/**
 * The panel under a milestone row: what a press would cover, and what previous
 * presses produced.
 *
 * Opening it starts nothing. It reads Linear through the server, asks the
 * ledger what of that is already queued, and shows both. The only call in here
 * that writes anything is the commit, and it carries the digest of the set on
 * screen.
 */
export function MilestoneWork({
  teamSlug,
  milestoneId,
  milestoneName,
  linearTeamKey,
  teamTasks,
  dispatchState,
  now,
  onScopeResolved,
}: MilestoneWorkProps) {
  const [dryRun, setDryRun] = useState(true);
  const [committedRefs, setCommittedRefs] = useState<string[]>([]);

  const issuesQuery = useMilestoneIssues(teamSlug, milestoneId);
  const workflowsQuery = useAgentWorkflows();

  const issues = useMemo(
    () => issuesQuery.data?.issues ?? [],
    [issuesQuery.data]
  );

  // The ledger's limits are the server's, and a body that overruns them would
  // be refused with a validation error the operator cannot act on. Cut here,
  // with the cut named in the text and flagged on the row.
  const { items, truncatedRefs } = useMemo(() => {
    const refs = new Set<string>();
    const built: ImportTaskItem[] = issues.map((issue) => {
      const title = truncateWithMarker(issue.title, TASK_TITLE_MAX_LENGTH);
      const body = truncateWithMarker(
        issue.description ?? '',
        TASK_BODY_MAX_LENGTH
      );
      if (title.truncated || body.truncated) refs.add(issue.ref);
      return {
        source_ref: issue.ref,
        title: title.text,
        body: body.text,
        // The ledger refuses anything but http(s) here, and dropping it now
        // beats a 422 the operator cannot act on.
        source_url: safeHttpUrl(issue.url),
      };
    });
    return { items: built, truncatedRefs: refs };
  }, [issues]);

  const previewQuery = useImportPreview({
    teamSlug,
    scopeRef: milestoneId,
    items,
    dryRun,
    enabled: issuesQuery.data?.status === 'ok',
  });

  const workflow = useMemo(() => {
    const key = previewQuery.data?.workflow_key;
    if (!key) return undefined;
    return workflowsQuery.data?.workflows.find(
      (candidate) => candidate.workflow_key === key
    );
  }, [previewQuery.data?.workflow_key, workflowsQuery.data]);

  const stepsPerTask = workflow?.steps.length ?? 1;

  const commit = useCommitImport(teamSlug);

  const refSet = useMemo(
    () => new Set([...items.map((item) => item.source_ref), ...committedRefs]),
    [items, committedRefs]
  );

  const identifiersByRef = useMemo(
    () => new Map(issues.map((issue) => [issue.ref, issue.identifier])),
    [issues]
  );
  const urlsByRef = useMemo(() => {
    const map = new Map<string, string>();
    for (const issue of issues) {
      const url = safeHttpUrl(issue.url);
      if (url) map.set(issue.ref, url);
    }
    return map;
  }, [issues]);

  // Null means "not worked out yet", which is different from zero. A milestone
  // the server answered for with no issues at all is a known zero and the play
  // control above says so, rather than opening a panel to report it.
  const eligibleCount =
    previewQuery.data?.eligible.length ??
    (issuesQuery.data && items.length === 0 ? 0 : null);

  useEffect(() => {
    onScopeResolved({
      milestoneId,
      refs: [...refSet],
      identifiersByRef,
      urlsByRef,
      eligibleCount,
      stepsPerTask,
    });
    // `onScopeResolved` is a setter on the panel above and is stable enough to
    // leave out; including it would re-report on every render of the parent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    milestoneId,
    refSet,
    identifiersByRef,
    urlsByRef,
    eligibleCount,
    stepsPerTask,
  ]);

  const milestoneTasks = useMemo(
    () => tasksForRefs(teamTasks, refSet),
    [teamTasks, refSet]
  );
  const runningTasks = milestoneTasks.filter(
    (task) => !SETTLED_STATUSES.has(task.status)
  );
  const finishedTasks = milestoneTasks.filter((task) =>
    SETTLED_STATUSES.has(task.status)
  );

  const onCommit = () => {
    const digest = previewQuery.data?.refs_digest;
    const eligible = previewQuery.data?.eligible ?? [];
    if (!digest || eligible.length === 0) return;

    const eligibleRefs = new Set(
      eligible.map((candidate) => candidate.source_ref)
    );
    // Only the rows that were displayed. The digest the server checks is over
    // this set, so anything else here would be refused rather than sneak in.
    const committing = items.filter((item) =>
      eligibleRefs.has(item.source_ref)
    );

    commit.mutate(
      {
        items: committing,
        expectedRefsDigest: digest,
        workflowKey: previewQuery.data?.workflow_key ?? null,
        dryRun,
      },
      {
        onSuccess: () => {
          setCommittedRefs((previous) => [
            ...new Set([...previous, ...eligibleRefs]),
          ]);
          void previewQuery.refetch();
        },
        onError: () => {
          // A refused commit is usually a moved set. Re-read it so what is on
          // screen is what a second press would be bound to.
          void previewQuery.refetch();
        },
      }
    );
  };

  if (issuesQuery.isLoading) {
    return (
      <Group gap="xs" py="sm" data-testid="milestone-work-loading">
        <Loader size="xs" />
        <Text size="xs" c="dimmed">
          Reading this milestone&apos;s issues
        </Text>
      </Group>
    );
  }

  if (issuesQuery.error || !issuesQuery.data) {
    return (
      <Alert
        variant="light"
        color="red"
        title="Could not read this milestone"
        data-testid="milestone-work-error"
      >
        <Text size="xs">
          Agent Control did not answer. Nothing has started, and the set a press
          would cover is unknown.
        </Text>
      </Alert>
    );
  }

  const issuesData = issuesQuery.data;

  if (issuesData.status === 'error') {
    return (
      <Alert
        variant="light"
        color="yellow"
        title="Could not reach Linear"
        data-testid="milestone-work-linear-error"
      >
        <Text size="xs">
          {issuesData.error ?? 'Linear did not answer.'}
          {issuesData.retry_after_seconds
            ? ` Linear asked us to wait ${issuesData.retry_after_seconds}s.`
            : ''}{' '}
          No stale list is shown here on purpose: an old board is better than an
          error, and an old list of work to start is not.
        </Text>
      </Alert>
    );
  }

  if (issuesData.status === 'not_configured') {
    return (
      <Alert variant="light" color="gray" title="Linear is not configured">
        <Text size="xs">
          This server has no Linear key, so there is nothing to read and nothing
          to start.
        </Text>
      </Alert>
    );
  }

  return (
    <Stack gap="sm">
      <DispatchBanners state={dispatchState} />

      {runningTasks.length > 0 ? (
        <Stack gap={6} data-testid="milestone-work-runs">
          <Text size="sm" fw={600}>
            Work under way
          </Text>
          <TaskRunList
            tasks={runningTasks}
            identifiersByRef={identifiersByRef}
            now={now}
          />
        </Stack>
      ) : null}

      <ResultsForReview
        tasks={finishedTasks}
        identifiersByRef={identifiersByRef}
        urlsByRef={urlsByRef}
        now={now}
      />

      <ScopeConfirm
        milestoneName={milestoneName}
        linearTeamKey={linearTeamKey}
        issues={issues}
        counts={issuesData.counts}
        fetchedAt={issuesData.fetched_at}
        truncatedRefs={truncatedRefs}
        preview={previewQuery.data}
        previewLoading={previewQuery.isLoading}
        previewError={previewQuery.error}
        workflow={workflow}
        dispatchState={dispatchState}
        dryRun={dryRun}
        onDryRunChange={setDryRun}
        onCommit={onCommit}
        committing={commit.isPending}
        commitError={commit.error}
        now={now}
      />
    </Stack>
  );
}
