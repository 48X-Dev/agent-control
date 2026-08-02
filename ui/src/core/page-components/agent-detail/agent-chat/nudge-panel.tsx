import { Box, Group, Stack, Text, Textarea, Tooltip } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import type { KeyboardEvent } from 'react';
import { useState } from 'react';

import type { Nudge } from '@/core/api/types';
import {
  NUDGE_BODY_MAX_LENGTH,
  NUDGE_MAX_PER_MODEL_CALL,
} from '@/core/hooks/query-hooks/use-nudges';

import classes from './agent-chat.module.css';
import { useNow } from './use-now';

const COUNTER_VISIBLE_FROM = NUDGE_BODY_MAX_LENGTH - 400;

function formatAge(createdAt: string, now: number): string {
  const at = new Date(createdAt).getTime();
  if (Number.isNaN(at)) return '';
  const seconds = Math.max(0, Math.floor((now - at) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

/**
 * What each status means, in words a person can act on.
 *
 * "Queued" is the one that matters. A nudge waits for the agent to reach its
 * next model call, which is after whatever tool it is running finishes, and
 * saying so is the difference between a queue and a panel that appears broken.
 */
function statusLine(nudge: Nudge): string {
  switch (nudge.status) {
    case 'pending':
      return 'Queued. It will be added at the agent’s next model call.';
    case 'claimed':
      return 'Being delivered now.';
    case 'applied':
      return 'Delivered. It appears in the transcript where it landed.';
    case 'rejected':
      return nudge.rejected_by_control
        ? `Refused by ${nudge.rejected_by_control}. The agent never saw it.`
        : 'Could not be evaluated, so it was not delivered.';
    case 'expired':
      return 'Could not be delivered after several attempts.';
    case 'cancelled':
      return 'Withdrawn before the agent saw it.';
    default:
      return '';
  }
}

type QueuedNudgeProps = {
  nudge: Nudge;
  now: number;
  onCancel: (nudgeId: number) => void;
  isCancelling: boolean;
};

function QueuedNudge({ nudge, now, onCancel, isCancelling }: QueuedNudgeProps) {
  const canCancel = nudge.status === 'pending';

  return (
    <Box
      className={classes.queuedNudge}
      data-testid="chat-queued-nudge"
      data-nudge-status={nudge.status}
    >
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="sm">
        <Stack gap={2} style={{ minWidth: 0 }}>
          {/* Plain text, like every other body in this panel. */}
          <Text
            component="div"
            size="sm"
            className={classes.queuedNudgeBody}
            lineClamp={3}
          >
            {nudge.body}
          </Text>
          <Text size="xs" c="dimmed">
            {statusLine(nudge)} · {formatAge(nudge.created_at, now)}
          </Text>
        </Stack>
        {canCancel ? (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onCancel(nudge.id)}
            disabled={isCancelling}
            data-testid="chat-nudge-cancel"
          >
            Withdraw
          </Button>
        ) : null}
      </Group>
    </Box>
  );
}

type NudgePanelProps = {
  nudges: Nudge[];
  onSend: (body: string) => void;
  onCancel: (nudgeId: number) => void;
  isSending: boolean;
  isCancelling: boolean;
  disabled: boolean;
  agentName: string;
};

/**
 * Guidance for an agent that is already working, plus the queue of it.
 *
 * Separate from the message composer because it is a different act. A message
 * starts a turn; a nudge joins one that is already running, or waits for the
 * next. The copy never implies immediacy: delivery happens at the agent's next
 * model call, a tool that is already running finishes first, and at most three
 * nudges are shown per call because a wall of appended text makes a model
 * worse rather than more steered.
 */
export function NudgePanel({
  nudges,
  onSend,
  onCancel,
  isSending,
  isCancelling,
  disabled,
  agentName,
}: NudgePanelProps) {
  const [draft, setDraft] = useState('');
  // Ages tick only while something is still waiting to be delivered.
  const hasLive = nudges.some(
    (nudge) => nudge.status === 'pending' || nudge.status === 'claimed'
  );
  const now = useNow(hasLive, 5000);

  const trimmed = draft.trim();
  const canSend = !disabled && !isSending && trimmed.length > 0;
  // Newest first in the list, but the agent reads them oldest first, which the
  // hint below says outright rather than leaving to be inferred from order.
  const live = nudges.filter(
    (nudge) => nudge.status === 'pending' || nudge.status === 'claimed'
  );
  const recent = nudges.slice(0, 5);

  const send = () => {
    if (!canSend) return;
    onSend(trimmed.slice(0, NUDGE_BODY_MAX_LENGTH));
    setDraft('');
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    send();
  };

  return (
    <Box className={classes.nudgePanel} data-testid="chat-nudge-panel">
      <Stack gap="xs">
        <Group justify="space-between" align="center" wrap="nowrap">
          <Text size="xs" fw={600}>
            Nudge the agent
          </Text>
          <Text size="xs" c="dimmed">
            {live.length > 0
              ? `${live.length} waiting · up to ${NUDGE_MAX_PER_MODEL_CALL} per model call, oldest first`
              : `Delivered at ${agentName}’s next model call`}
          </Text>
        </Group>

        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={handleKeyDown}
          placeholder="Tell the agent something while it works"
          autosize
          minRows={1}
          maxRows={4}
          maxLength={NUDGE_BODY_MAX_LENGTH}
          disabled={disabled}
          data-testid="chat-nudge-input"
        />

        <Group justify="space-between" align="center" wrap="nowrap">
          <Text size="xs" c="dimmed">
            Arrives at the agent’s next model call. A tool it has already
            started will finish first.
            {draft.length >= COUNTER_VISIBLE_FROM
              ? ` ${draft.length} / ${NUDGE_BODY_MAX_LENGTH} characters.`
              : ''}
          </Text>
          <Tooltip
            label="Your controls evaluate this text before the agent sees it."
            withArrow
          >
            <Button
              variant="outline"
              size="sm"
              onClick={send}
              disabled={!canSend}
              loading={isSending}
              data-testid="chat-nudge-send"
            >
              Queue nudge
            </Button>
          </Tooltip>
        </Group>

        {recent.length > 0 ? (
          <Stack gap={6} data-testid="chat-nudge-queue">
            {recent.map((nudge) => (
              <QueuedNudge
                key={nudge.id}
                nudge={nudge}
                now={now}
                onCancel={onCancel}
                isCancelling={isCancelling}
              />
            ))}
          </Stack>
        ) : null}
      </Stack>
    </Box>
  );
}
