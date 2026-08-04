import {
  Alert,
  Badge,
  Box,
  Divider,
  Group,
  Stack,
  Switch,
  Text,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import {
  IconAlertTriangle,
  IconExternalLink,
  IconPlayerPlay,
} from '@tabler/icons-react';

import { getErrorCode } from '@/core/api/errors';
import type {
  AgentWorkflow,
  DispatchStateSnapshot,
  ImportAgentTasksResponse,
  MilestoneIssue,
  MilestoneIssueCounts,
} from '@/core/api/types';

import classes from './dispatch.module.css';
import {
  formatDateTime,
  formatTime,
  isNewWithinHour,
  pluralize,
  safeHttpUrl,
} from './formatting';

export type ScopeConfirmProps = {
  milestoneName: string;
  linearTeamKey: string;
  issues: MilestoneIssue[];
  counts: MilestoneIssueCounts;
  fetchedAt?: string | null;
  /** Refs whose title or body was cut to fit the ledger's limits. */
  truncatedRefs: ReadonlySet<string>;
  preview: ImportAgentTasksResponse | undefined;
  previewLoading: boolean;
  previewError: unknown;
  workflow: AgentWorkflow | undefined;
  dispatchState: DispatchStateSnapshot | null | undefined;
  dryRun: boolean;
  onDryRunChange: (value: boolean) => void;
  onCommit: () => void;
  committing: boolean;
  commitError: unknown;
  now: number;
};

function IssueRow({
  candidateTitle,
  issue,
  flagged,
  flags,
}: {
  candidateTitle: string;
  issue: MilestoneIssue | undefined;
  flagged: boolean;
  flags: string[];
}) {
  const created = formatDateTime(issue?.created_at);
  const updated = formatDateTime(issue?.updated_at);
  const creator = issue?.creator_display_name?.trim();
  const href = safeHttpUrl(issue?.url);

  return (
    <Box
      className={`${classes.issueRow} ${flagged ? classes.issueRowFlagged : ''}`}
      data-testid="scope-issue-row"
    >
      <Stack gap={4}>
        <Group gap="xs" align="baseline" wrap="nowrap">
          <Text
            className={classes.identifier}
            c="dimmed"
            data-testid="scope-issue-identifier"
          >
            {issue?.identifier ?? 'unknown id'}
          </Text>
          {/* Untrusted: this is a title somebody wrote in a tracker. It is a
              text node, and it is not a link target. */}
          <Text size="sm" lineClamp={2} data-testid="scope-issue-title">
            {candidateTitle}
          </Text>
        </Group>

        <Group gap="sm" wrap="wrap">
          <Text size="xs" c="dimmed" data-testid="scope-issue-creator">
            Filed by {creator && creator.length > 0 ? creator : 'unknown'}
          </Text>
          {created ? (
            <Text size="xs" c="dimmed" data-testid="scope-issue-created">
              created {created}
            </Text>
          ) : null}
          {updated ? (
            <Text size="xs" c="dimmed">
              updated {updated}
            </Text>
          ) : null}
          {href ? (
            <Text
              size="xs"
              component="a"
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              c="dimmed"
            >
              Open in Linear
              <IconExternalLink
                size={11}
                style={{ marginLeft: 4, verticalAlign: 'middle' }}
              />
            </Text>
          ) : null}
        </Group>

        {flags.length > 0 ? (
          <Group gap={6} wrap="wrap" data-testid="scope-issue-flags">
            {flags.map((flag) => (
              <Badge key={flag} size="xs" variant="light" color="yellow">
                {flag}
              </Badge>
            ))}
          </Group>
        ) : null}
      </Stack>
    </Box>
  );
}

function SkippedCounts({
  counts,
  preview,
}: {
  counts: MilestoneIssueCounts;
  preview: ImportAgentTasksResponse | undefined;
}) {
  const lines: string[] = [];
  if (counts.skipped.other_team > 0) {
    lines.push(
      `${counts.skipped.other_team} ${pluralize(counts.skipped.other_team, 'issue')} in this milestone ${pluralize(counts.skipped.other_team, 'belongs', 'belong')} to another Linear team. Cross-team work needs that team's own run.`
    );
  }
  if (counts.skipped.assigned > 0) {
    lines.push(
      `${counts.skipped.assigned} ${pluralize(counts.skipped.assigned, 'issue is', 'issues are')} assigned to a person and stay theirs.`
    );
  }
  if (counts.skipped.started > 0) {
    lines.push(
      `${counts.skipped.started} ${pluralize(counts.skipped.started, 'issue has', 'issues have')} already been started by a human.`
    );
  }
  if (preview && preview.skipped.already_queued > 0) {
    lines.push(
      `${preview.skipped.already_queued} already ${pluralize(preview.skipped.already_queued, 'has', 'have')} an open task and will not be queued twice.`
    );
  }
  if (preview && preview.skipped.already_worked > 0) {
    lines.push(
      `${preview.skipped.already_worked} ${pluralize(preview.skipped.already_worked, 'has', 'have')} been worked before. Re-running finished work is a separate decision and is not offered here.`
    );
  }
  if (counts.beyond_page_cap) {
    lines.push(
      'The read came back at its cap of 100 issues, so this milestone may hold work nobody here has seen.'
    );
  }

  if (lines.length === 0) return null;

  return (
    <Stack gap={2} data-testid="scope-skipped-counts">
      {lines.map((line) => (
        <Text key={line} size="xs" c="dimmed">
          {line}
        </Text>
      ))}
    </Stack>
  );
}

function WorkflowSummary({
  workflow,
  workflowKey,
  eligibleCount,
}: {
  workflow: AgentWorkflow | undefined;
  workflowKey: string;
  eligibleCount: number;
}) {
  const steps = workflow?.steps ?? [];
  const turnsPerTask =
    steps.length > 0
      ? steps.reduce((total, step) => total + step.max_turns, 0)
      : 1;

  return (
    <Stack gap={4} data-testid="scope-workflow">
      <Text size="xs" fw={600}>
        Workflow: {workflow?.display_name ?? workflowKey}
      </Text>
      {steps.length > 0 ? (
        steps.map((step, index) => (
          <Text key={`${workflowKey}-${index}`} size="xs" c="dimmed">
            Step {index + 1}: {step.agent_name ?? "the team's default agent"} ·
            up to {step.max_turns} {pluralize(step.max_turns, 'turn')}
          </Text>
        ))
      ) : (
        <Text size="xs" c="dimmed">
          One step, run by the team&apos;s default agent. No workflow named{' '}
          {workflowKey} is configured, so the implicit one applies.
        </Text>
      )}
      <Text size="xs" c="dimmed" data-testid="scope-turn-ceiling">
        Turn ceiling: {turnsPerTask} per issue, {turnsPerTask * eligibleCount}{' '}
        across this set. A ceiling, not an estimate.
      </Text>
    </Stack>
  );
}

function BudgetSummary({
  state,
}: {
  state: DispatchStateSnapshot | null | undefined;
}) {
  if (!state) return null;

  return (
    <Stack gap={2} data-testid="scope-budget">
      <Text size="xs" c="dimmed">
        Left this hour: {state.budget.turns_remaining_this_hour} of{' '}
        {state.budget.max_turns_per_hour} turns,{' '}
        {state.budget.tasks_remaining_this_hour} of{' '}
        {state.budget.max_tasks_per_hour} tasks.
      </Text>
      <Text size="xs" c="dimmed">
        Shown so you can see what is left. The ceiling itself is enforced by the
        server on every turn, not by this screen.
      </Text>
    </Stack>
  );
}

/**
 * The confirm. Pressing play opened this; pressing the button at the bottom is
 * what creates anything.
 *
 * It renders the rows and not a number, and that is the whole point. Anyone
 * with tracker access can file an issue into a milestone, so a set the operator
 * cannot see is a set they cannot judge: "5 issues" and "4 issues" look the
 * same at a glance and neither says which one is new.
 */
export function ScopeConfirm({
  milestoneName,
  linearTeamKey,
  issues,
  counts,
  fetchedAt,
  truncatedRefs,
  preview,
  previewLoading,
  previewError,
  workflow,
  dispatchState,
  dryRun,
  onDryRunChange,
  onCommit,
  committing,
  commitError,
  now,
}: ScopeConfirmProps) {
  const issuesByRef = new Map(issues.map((issue) => [issue.ref, issue]));
  const eligible = preview?.eligible ?? [];
  const paused = Boolean(dispatchState?.paused);
  const halted = Boolean(dispatchState?.executors_halted);
  const commitBlocked =
    paused || halted || eligible.length === 0 || previewLoading || !preview;

  const flagsFor = (ref: string, serverFlags: string[]): string[] => {
    const issue = issuesByRef.get(ref);
    const flags = [...serverFlags];
    if (isNewWithinHour(issue?.created_at, now))
      flags.push('new within the hour');
    if (truncatedRefs.has(ref)) flags.push('description truncated');
    return flags;
  };

  const scopeChanged = getErrorCode(commitError) === 'SCOPE_CHANGED';

  return (
    <Stack
      gap="sm"
      className={classes.panel}
      data-testid="milestone-work-scope"
    >
      <Stack gap={2}>
        <Text size="sm" fw={600}>
          Start work on {milestoneName}
        </Text>
        <Text size="xs" c="dimmed">
          Nothing has started. This panel is what a press would cover, read from
          Linear and scoped to {linearTeamKey}.
          {fetchedAt ? ` Read at ${formatTime(fetchedAt)}.` : ''}
        </Text>
      </Stack>

      {previewError ? (
        <Alert
          variant="light"
          color="red"
          title="Could not work out what this would start"
          data-testid="scope-preview-error"
        >
          <Text size="xs">
            Nothing was created. The ledger did not answer, so the set below
            cannot be checked against work that is already queued.
          </Text>
        </Alert>
      ) : null}

      {eligible.length === 0 && !previewLoading ? (
        <Text size="sm" data-testid="scope-nothing-eligible">
          Nothing to work on. Every issue in this milestone is assigned, already
          started, owned by another team, or already has a task.
        </Text>
      ) : (
        <Stack gap={6}>
          <Text size="xs" fw={600} data-testid="scope-eligible-count">
            {eligible.length} {pluralize(eligible.length, 'issue')} would be
            queued
          </Text>
          <Stack gap={6} className={classes.scroller}>
            {eligible.map((candidate) => {
              const flags = flagsFor(candidate.source_ref, candidate.flags);
              return (
                <IssueRow
                  key={candidate.source_ref}
                  candidateTitle={candidate.title}
                  issue={issuesByRef.get(candidate.source_ref)}
                  flagged={flags.length > 0}
                  flags={flags}
                />
              );
            })}
          </Stack>
          <Text size="xs" c="dimmed">
            A flagged row is a hint to look twice, not a verdict. An issue filed
            minutes ago may be ordinary and an old one may not be.
          </Text>
        </Stack>
      )}

      <SkippedCounts counts={counts} preview={preview} />

      <Divider />

      <WorkflowSummary
        workflow={workflow}
        workflowKey={preview?.workflow_key ?? 'default'}
        eligibleCount={eligible.length}
      />

      <BudgetSummary state={dispatchState} />

      <Divider />

      <Switch
        size="xs"
        checked={dryRun}
        onChange={(event) => onDryRunChange(event.currentTarget.checked)}
        label="Dry run"
        description="Recorded on every row this press creates and never changed afterwards. Turning it off means agents may act for real."
        data-testid="scope-dry-run"
      />

      {commitError ? (
        <Alert
          variant="light"
          color={scopeChanged ? 'yellow' : 'red'}
          icon={<IconAlertTriangle size={16} />}
          title={scopeChanged ? 'The set changed' : 'Nothing was started'}
          data-testid="scope-commit-error"
        >
          <Text size="xs">
            {scopeChanged
              ? 'The issues in this milestone are not the ones displayed a moment ago. Nothing was created. Read the refreshed list above and press again if it is still what you want.'
              : (commitError as Error).message}
          </Text>
        </Alert>
      ) : null}

      <Group justify="space-between" align="center" wrap="wrap">
        <Text size="xs" c="dimmed">
          {paused
            ? 'This namespace is paused, so nothing can be queued.'
            : halted
              ? 'Executors are halted, so nothing can be queued.'
              : 'This queues the work. The dispatcher claims it within a few seconds.'}
        </Text>
        <Button
          size="sm"
          onClick={onCommit}
          disabled={commitBlocked}
          loading={committing}
          leftSection={<IconPlayerPlay size={14} />}
          data-testid="milestone-commit-work"
        >
          {eligible.length > 0
            ? `Start work on ${eligible.length} ${pluralize(eligible.length, 'issue')}`
            : 'Nothing to start'}
        </Button>
      </Group>
    </Stack>
  );
}
