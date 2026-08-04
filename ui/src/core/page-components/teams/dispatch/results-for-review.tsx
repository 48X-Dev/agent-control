import { Box, Group, Loader, Stack, Text, TextInput } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { IconExternalLink } from '@tabler/icons-react';
import { useState } from 'react';

import { isApiError } from '@/core/api/errors';
import type { AgentTaskSummary, ReviewQueueEntry } from '@/core/api/types';
import {
  useAcceptAgentTask,
  useRejectAgentTask,
} from '@/core/hooks/query-hooks/use-task-review';

import classes from './dispatch.module.css';
import { formatAge, pluralize, safeHttpUrl } from './formatting';
import { TaskRunList } from './task-runs';

/** After this long unread, a dry-run result is stale rather than pending. */
const STALE_AFTER_MS = 48 * 60 * 60 * 1000;

/**
 * What one press decided, kept locally so the confirmation outlives the queue
 * entry it answers: the refetched queue no longer carries a decided row, and a
 * confirmation that vanished with it would read as the press not landing.
 */
type Decision =
  | {
      kind: 'accepted';
      issueName: string;
      url: string | null;
      alreadyCompleted: boolean;
      progressPercent: number | null;
    }
  | { kind: 'rejected'; issueName: string; reason: string };

/**
 * Finished work, waiting on a person.
 *
 * An agent never changes an issue's state on the strength of its own claim.
 * What an agent's completion produces is a proposal row, and each card here is
 * one of them: the claim, the live target it would close, and two controls. An
 * accept closes the issue through Agent Control, bound by a digest to exactly
 * the text and target on this card; a reject records why and leaves the issue
 * open. Dry runs propose nothing, so their results keep a plain link into
 * Linear and a person closes those by hand if the work should count.
 *
 * There is no accept-all here and there will not be one. Bulk-accepting eight
 * claims nobody read is the thing this queue exists to prevent, and it would
 * leave an audit trail saying the opposite.
 */
export function ResultsForReview({
  teamSlug,
  milestoneId,
  tasks,
  entries,
  queueLoading,
  queueError,
  identifiersByRef,
  urlsByRef,
  now,
}: {
  teamSlug: string;
  milestoneId: string;
  /** Completed ledger rows in this milestone's scope, dry runs included. */
  tasks: AgentTaskSummary[];
  /** The review queue for this scope. Empty while loading or unreadable. */
  entries: ReviewQueueEntry[];
  queueLoading: boolean;
  queueError: Error | null;
  identifiersByRef: Map<string, string>;
  urlsByRef: Map<string, string>;
  now: number;
}) {
  const [decisions, setDecisions] = useState<ReadonlyMap<number, Decision>>(
    new Map()
  );
  const recordDecision = (writebackId: number, decision: Decision) =>
    setDecisions((previous) => new Map(previous).set(writebackId, decision));

  const waiting = entries.filter((entry) => !decisions.has(entry.writeback_id));

  // Dry runs are excluded from write-back on the server, so no queue entry
  // will ever arrive for them. The link is their whole human path.
  const dryRunTasks = tasks.filter((task) => task.dry_run);

  if (tasks.length === 0 && waiting.length === 0 && decisions.size === 0) {
    return null;
  }

  const staleCount =
    waiting.filter((entry) => entry.stale).length +
    dryRunTasks.filter((task) => {
      const updated = new Date(task.updated_at).getTime();
      return !Number.isNaN(updated) && now - updated > STALE_AFTER_MS;
    }).length;

  const waitingCount = waiting.length + dryRunTasks.length;

  return (
    <Stack gap="xs" data-testid="task-review-queue">
      <Text size="sm" fw={600}>
        {waitingCount > 0
          ? `${waitingCount} ${pluralize(waitingCount, 'result')} waiting for you`
          : 'Nothing else is waiting for you here'}
      </Text>

      {waiting.length > 0 ? (
        <Text size="xs" c="dimmed">
          The agents finished and commented on their issues. No issue closes on
          an agent&apos;s word: read each result, then accept it to close the
          issue, or reject it and say why. One decision per issue, and there is
          no accept-all.
        </Text>
      ) : dryRunTasks.length > 0 ? (
        <Text size="xs" c="dimmed">
          The agents finished. Nothing has been written to the tracker and no
          issue has been closed. Read what each agent wrote, then close the
          issue in Linear yourself; the milestone bar moves because a person
          agreed, not because an agent reported.
        </Text>
      ) : null}

      {staleCount > 0 ? (
        <Text size="xs" c="dimmed" data-testid="task-review-stale">
          {staleCount} of these {pluralize(staleCount, 'has', 'have')} been
          waiting more than two days. Nothing here expires into approval.
        </Text>
      ) : null}

      {queueError ? (
        <Text size="xs" c="red" data-testid="task-review-queue-error">
          The review queue could not be read, so results waiting for a decision
          are not shown. Nothing was decided for you; the runs below are
          unaffected.
        </Text>
      ) : null}

      {queueLoading && entries.length === 0 && !queueError ? (
        <Group gap="xs" data-testid="task-review-queue-loading">
          <Loader size="xs" />
          <Text size="xs" c="dimmed">
            Reading what is waiting for a decision
          </Text>
        </Group>
      ) : null}

      <TaskRunList
        tasks={tasks}
        identifiersByRef={identifiersByRef}
        now={now}
      />

      {[...decisions.entries()].map(([writebackId, decision]) => (
        <DecisionRow key={writebackId} decision={decision} />
      ))}

      {waiting.map((entry) => (
        <ReviewCard
          key={entry.writeback_id}
          entry={entry}
          teamSlug={teamSlug}
          milestoneId={milestoneId}
          issueName={
            identifiersByRef.get(entry.source_ref) ??
            entry.issue?.identifier ??
            'this issue'
          }
          url={
            urlsByRef.get(entry.source_ref) ??
            safeHttpUrl(entry.source_url) ??
            null
          }
          now={now}
          onDecided={recordDecision}
        />
      ))}

      {dryRunTasks.length > 0 && waiting.length > 0 ? (
        <Text size="xs" c="dimmed">
          The rest were dry runs. A dry run writes nothing and proposes nothing,
          so close these in Linear yourself if the work should count.
        </Text>
      ) : null}

      <Stack gap={2}>
        {dryRunTasks.map((task) => {
          // The milestone read carries only issues that are still eligible, so
          // an issue a person has already picked up drops out of that map. The
          // url the task recorded at import is the one that survives, and this
          // link is the whole human-accept path for a dry run: losing it
          // silently would leave a result nobody can act on.
          const url =
            urlsByRef.get(task.source_ref) ?? safeHttpUrl(task.source_url);
          if (!url) return null;
          const identifier = identifiersByRef.get(task.source_ref);
          const age = formatAge(task.updated_at, now);
          return (
            <Box key={task.task_key}>
              <Group gap="xs" wrap="nowrap">
                <Text
                  size="xs"
                  component="a"
                  href={url}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid="task-review-link"
                >
                  Close {identifier ?? 'this issue'} in Linear
                  <IconExternalLink
                    size={11}
                    style={{ marginLeft: 4, verticalAlign: 'middle' }}
                  />
                </Text>
                {age ? (
                  <Text size="xs" c="dimmed" className={classes.identifier}>
                    finished {age}
                  </Text>
                ) : null}
              </Group>
            </Box>
          );
        })}
      </Stack>
    </Stack>
  );
}

/**
 * One proposal: the claim, the target, and the two controls.
 *
 * The card renders the issue as Linear answered at render time, not only the
 * agent's account of it, and the digest it holds covers the text, the target
 * and the resolved completed state together. A press over a card that moved is
 * refused by the server, and the refusal is shown rather than retried.
 */
function ReviewCard({
  entry,
  teamSlug,
  milestoneId,
  issueName,
  url,
  now,
  onDecided,
}: {
  entry: ReviewQueueEntry;
  teamSlug: string;
  milestoneId: string;
  issueName: string;
  url: string | null;
  now: number;
  onDecided: (writebackId: number, decision: Decision) => void;
}) {
  const accept = useAcceptAgentTask({ teamSlug, milestoneId });
  const reject = useRejectAgentTask();
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');

  const digest = entry.decision_digest ?? null;
  const issue = entry.issue ?? null;
  const age = formatAge(entry.created_at, now);
  const busy = accept.isPending || reject.isPending;
  const error = accept.error ?? reject.error;

  const onAccept = () => {
    if (!digest) return;
    accept.mutate(
      {
        taskKey: entry.task_key,
        writebackId: entry.writeback_id,
        expectedDecisionDigest: digest,
      },
      {
        onSuccess: (data) => {
          onDecided(entry.writeback_id, {
            kind: 'accepted',
            issueName,
            url,
            alreadyCompleted: data.note === 'ALREADY_COMPLETED',
            progressPercent:
              typeof data.milestone_progress === 'number'
                ? Math.round(data.milestone_progress * 100)
                : null,
          });
        },
      }
    );
  };

  const onReject = () => {
    const trimmed = reason.trim();
    if (!trimmed) return;
    reject.mutate(
      {
        taskKey: entry.task_key,
        writebackId: entry.writeback_id,
        reason: trimmed,
      },
      {
        onSuccess: () =>
          onDecided(entry.writeback_id, {
            kind: 'rejected',
            issueName,
            reason: trimmed,
          }),
      }
    );
  };

  return (
    <Box className={classes.taskRow} data-testid="review-card">
      <Stack gap={6}>
        <Group gap="xs" wrap="wrap" align="baseline">
          <Text className={classes.identifier} c="dimmed">
            {issueName}
          </Text>
          {issue?.title ? (
            /* Untrusted: the title as it stands in the tracker right now. */
            <Text size="sm" lineClamp={1} data-testid="review-card-title">
              {issue.title}
            </Text>
          ) : null}
        </Group>

        <Text size="xs" c="dimmed">
          {entry.agent_name
            ? `Agent ${entry.agent_name} proposes closing this issue`
            : 'An agent proposes closing this issue'}
          {age ? `, finished ${age}` : ''}
          {issue?.state_name ? `. In Linear it is "${issue.state_name}".` : '.'}
        </Text>

        {issue?.read_failed ? (
          <Text size="xs" c="dimmed" data-testid="review-card-unreadable">
            The issue could not be read from Linear just now. Accepting needs
            that read, so this waits; nothing expires while it does.
          </Text>
        ) : null}

        {/* The agent's own words, verbatim, as text. Never markup: an agent
            that swallowed an injection writes this string. */}
        <Text
          className={classes.outputText}
          component="pre"
          data-testid="review-card-summary"
        >
          {entry.summary}
        </Text>

        {!rejecting ? (
          <Group gap="xs">
            <Button
              size="sm"
              variant="filled"
              onClick={onAccept}
              disabled={!digest || busy}
              loading={accept.isPending}
              data-testid="review-accept"
            >
              Accept and close {issueName}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setRejecting(true)}
              disabled={busy}
              data-testid="review-reject"
            >
              Reject
            </Button>
          </Group>
        ) : (
          <Stack gap={6}>
            <TextInput
              size="xs"
              value={reason}
              onChange={(event) => setReason(event.currentTarget.value)}
              placeholder="Why this result should not close the issue"
              aria-label={`Why ${issueName} should stay open`}
              maxLength={2000}
              data-testid="review-reject-reason"
            />
            <Group gap="xs">
              <Button
                size="sm"
                variant="secondary"
                onClick={onReject}
                disabled={reason.trim().length === 0 || busy}
                loading={reject.isPending}
                data-testid="review-reject-confirm"
              >
                Record rejection
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setRejecting(false);
                  setReason('');
                }}
                disabled={busy}
                data-testid="review-reject-cancel"
              >
                Keep deciding
              </Button>
            </Group>
          </Stack>
        )}

        {/* A null digest means the server could not resolve this team's
            completed workflow state, not that the write flag is off: a
            write-disabled deployment still serves the digest and refuses the
            press instead, with the refusal rendered below. */}
        {!digest && !issue?.read_failed ? (
          <Text size="xs" c="dimmed" data-testid="review-card-no-digest">
            Accepting is unavailable: the state this issue would move to could
            not be read from Linear. That happens while the server has no Linear
            key or Linear is unreachable. The proposal keeps waiting; nothing
            expires while it does.
          </Text>
        ) : null}

        {error ? (
          <Text size="xs" c="red" data-testid="review-card-error">
            {error.message}
            {isApiError(error) && error.problemDetail.hint
              ? ` ${error.problemDetail.hint}`
              : ''}
          </Text>
        ) : null}
      </Stack>
    </Box>
  );
}

/** The confirmation a decision leaves behind, in the queue's own place. */
function DecisionRow({ decision }: { decision: Decision }) {
  if (decision.kind === 'accepted') {
    return (
      <Box data-testid="review-decided">
        <Stack gap={2}>
          <Text size="xs">
            Accepted.{' '}
            {decision.alreadyCompleted
              ? `${decision.issueName} had already been closed by a person, so nothing was changed.`
              : `${decision.issueName} is now closed in Linear.`}
            {decision.progressPercent !== null
              ? ` Milestone issue completion is now ${decision.progressPercent}%.`
              : ''}
          </Text>
          {decision.url ? (
            <Text
              size="xs"
              component="a"
              href={decision.url}
              target="_blank"
              rel="noopener noreferrer"
              data-testid="review-accepted-link"
            >
              See the agent&apos;s comment on {decision.issueName}
              <IconExternalLink
                size={11}
                style={{ marginLeft: 4, verticalAlign: 'middle' }}
              />
            </Text>
          ) : null}
        </Stack>
      </Box>
    );
  }
  return (
    <Box data-testid="review-decided">
      <Text size="xs">
        Rejected. {decision.issueName} stays open and the task stays completed.
        Reason recorded: {decision.reason}
      </Text>
    </Box>
  );
}
