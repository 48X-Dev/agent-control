import { Box, Group, Stack, Text, Textarea } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import type { KeyboardEvent } from 'react';
import { useEffect, useState } from 'react';

import classes from './agent-chat.module.css';

/**
 * Ceiling on one turn's text, matching `TURN_MESSAGE_MAX_LENGTH` on the
 * server. Every character is billed on the way in and again on every later
 * turn that carries the history.
 */
export const TURN_MESSAGE_MAX_LENGTH = 16000;

/** Show the counter only once the limit is close enough to matter. */
const COUNTER_VISIBLE_FROM = TURN_MESSAGE_MAX_LENGTH - 2000;

function useElapsedSeconds(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (startedAt === null) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);

  if (startedAt === null) return 0;
  // Clamped rather than seeded on start: the clock only runs while a turn is
  // in flight, so the first reading can lag the real start by up to the tick.
  // Seeding it would mean setState in an effect, which this repo's lint bans.
  return Math.max(0, Math.floor((now - startedAt) / 1000));
}

function formatElapsed(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${String(seconds % 60).padStart(2, '0')}s`;
}

/**
 * Where a stop has got to, from the panel's point of view.
 *
 * `acknowledged` is deliberately not `stopped`. It means the executor said it
 * blocked, which is an assertion by the party being stopped; the turn ending is
 * what the server observes for itself, and that is what clears this state.
 * `stalled` is `acknowledged` that has outstayed its welcome, and `unlanded` is
 * `requested` that has - a stop nobody has picked up, which is what a long tool
 * call looks like from here, and also what a turn that ended on the executor
 * without this server hearing about it looks like.
 */
export type HaltState =
  | 'none'
  | 'requested'
  | 'unlanded'
  | 'acknowledged'
  | 'stalled';

type MessageComposerProps = {
  onSend: (message: string) => void;
  onStopWaiting: () => void;
  /** Ask the agent to stop at its next boundary. */
  onStop: () => void;
  /**
   * True when there is a live invocation to stop. Follows the session's
   * liveness marker rather than its turn lock, so the control does not vanish
   * the instant this server stops waiting for a turn. Note that the marker
   * outliving the turn lock does not prove the agent is still running: the
   * executor ends an invocation when the request it arrived on is dropped, so
   * a stop pressed in that window may find nothing left to land on and will
   * age out with the turn. Nothing here says otherwise.
   */
  canStop: boolean;
  isStopping: boolean;
  haltState: HaltState;
  /** Tool the stop caught, when it landed at a tool boundary. */
  haltToolName: string | null;
  /** True while a turn holds this session, whoever started it. */
  isTurnInFlight: boolean;
  /**
   * True only while *this* panel holds the request. A turn started in another
   * tab, or by another caller, still blocks the composer, but there is no
   * request here to abandon and offering to stop waiting for one would be a
   * button that does nothing.
   */
  isWaitingHere: boolean;
  /** When the turn started, for the elapsed counter. */
  turnStartedAt: number | null;
  /** Disabled for reasons other than a running turn, e.g. no session yet. */
  disabled: boolean;
  /**
   * The operator already stopped waiting for the turn that is still holding
   * this session. The composer stays disabled, because the server refuses a
   * second turn while one is in flight, but it stops claiming to be waiting.
   */
  hasStoppedWaiting: boolean;
  agentName: string;
};

/**
 * The input, and everything the panel says while a turn is running.
 *
 * A turn blocks for as long as the agent takes, so the composer is disabled
 * and the elapsed time is shown rather than a spinner that says nothing.
 *
 * Two controls, and the difference between them is the whole point.
 * **Stop responding** stops the agent: it lands at the next model call or the
 * next tool, the turn ends, and the transcript shows the block. **Stop
 * waiting** abandons this request and nothing else - the turn carries on. The
 * second is a genuinely useful thing to want, and it is also a lie if it is
 * labelled "stop", so it keeps its own name and appears only once the real
 * stop is visibly not landing.
 *
 * Nothing here claims immediacy. A tool that has already started finishes, and
 * whatever it was doing has already happened; the copy says so before the
 * click rather than after it.
 */
export function MessageComposer({
  onSend,
  onStopWaiting,
  onStop,
  canStop,
  isStopping,
  haltState,
  haltToolName,
  isTurnInFlight,
  isWaitingHere,
  turnStartedAt,
  disabled,
  hasStoppedWaiting,
  agentName,
}: MessageComposerProps) {
  const [draft, setDraft] = useState('');
  const elapsed = useElapsedSeconds(isTurnInFlight ? turnStartedAt : null);

  const trimmed = draft.trim();
  const canSend = !disabled && !isTurnInFlight && trimmed.length > 0;

  const send = () => {
    if (!canSend) return;
    onSend(trimmed.slice(0, TURN_MESSAGE_MAX_LENGTH));
    setDraft('');
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    send();
  };

  return (
    <Box className={classes.composer} data-testid="chat-composer">
      <Stack gap="xs">
        <Textarea
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
          onKeyDown={handleKeyDown}
          placeholder={
            isTurnInFlight
              ? 'Waiting for the agent to finish…'
              : `Message ${agentName}`
          }
          autosize
          minRows={2}
          maxRows={8}
          maxLength={TURN_MESSAGE_MAX_LENGTH}
          disabled={disabled || isTurnInFlight}
          data-testid="chat-composer-input"
        />

        <Group justify="space-between" align="center" wrap="nowrap">
          <Box style={{ minWidth: 0 }}>
            {haltState !== 'none' ? (
              <Stack gap={2}>
                <Text size="xs" fw={500} data-testid="chat-halt-state">
                  {haltState === 'requested'
                    ? `Stopping ${agentName}…`
                    : haltState === 'unlanded'
                      ? 'The stop has not reached the agent yet'
                      : haltState === 'acknowledged'
                        ? 'Stop acknowledged, waiting for the turn to end'
                        : 'Stop acknowledged, but the turn has not ended'}
                </Text>
                <Text size="xs" c="dimmed">
                  {haltState === 'requested'
                    ? 'Waiting for the agent to reach its next step. A tool that is already running will finish first, and whatever it was doing will have happened.'
                    : haltState === 'unlanded'
                      ? 'It lands at the next model call or the next tool, so a long-running tool holds it up. If this server had already stopped waiting for the turn, it may have ended on the executor and there is nothing left to stop.'
                      : haltState === 'acknowledged'
                        ? haltToolName
                          ? `The agent stopped before running ${haltToolName}. This turn will close shortly.`
                          : 'The agent has blocked at a boundary. This turn will close shortly.'
                        : 'The executor says it stopped, but this server has not seen the turn end. It may still be spending.'}
                </Text>
              </Stack>
            ) : isTurnInFlight ? (
              <Stack gap={2}>
                <Text size="xs" fw={500} data-testid="chat-turn-elapsed">
                  {hasStoppedWaiting
                    ? `Stopped waiting · ${agentName} has been running for ${formatElapsed(elapsed)}`
                    : isWaitingHere
                      ? `Waiting for ${agentName} · ${formatElapsed(elapsed)}`
                      : `${agentName} has been running for ${formatElapsed(elapsed)}`}
                </Text>
                <Text size="xs" c="dimmed">
                  {hasStoppedWaiting
                    ? 'The turn is still running on the executor, so this session will not accept another message until it ends. Its messages will appear here.'
                    : isWaitingHere
                      ? 'Stopping the agent ends the turn at its next step. Stopping the wait does not: the turn keeps running and its messages appear here.'
                      : 'This turn was not started here, so there is nothing for this panel to stop waiting for. Its messages will appear here when it finishes.'}
                </Text>
              </Stack>
            ) : (
              <Text size="xs" c="dimmed">
                Enter to send, Shift + Enter for a new line.
                {draft.length >= COUNTER_VISIBLE_FROM
                  ? ` ${draft.length} / ${TURN_MESSAGE_MAX_LENGTH} characters.`
                  : ''}
              </Text>
            )}
          </Box>

          <Group gap="xs" wrap="nowrap">
            {/* "Stop waiting" is the secondary control and it appears late on
                purpose. Two buttons where one of them cannot stop the agent is
                confusing while the real stop is landing; it earns its place
                once the stop is visibly not landing, which is exactly when
                someone needs a way off this screen.

                It also appears when there is nothing to stop yet - the turn
                has been sent but this server has not recorded an invocation
                against it - because in that window the alternative is a panel
                with no control on it at all. */}
            {isWaitingHere &&
            !hasStoppedWaiting &&
            (haltState === 'stalled' || haltState === 'unlanded' || !canStop) ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={onStopWaiting}
                data-testid="chat-stop-waiting"
              >
                Stop waiting
              </Button>
            ) : null}
            {canStop && haltState === 'none' ? (
              <Button
                variant="outline"
                size="sm"
                onClick={onStop}
                loading={isStopping}
                data-testid="chat-stop-responding"
              >
                Stop responding
              </Button>
            ) : null}
            <Button
              variant="filled"
              size="sm"
              onClick={send}
              disabled={!canSend}
              data-testid="chat-send"
            >
              Send
            </Button>
          </Group>
        </Group>
      </Stack>
    </Box>
  );
}
