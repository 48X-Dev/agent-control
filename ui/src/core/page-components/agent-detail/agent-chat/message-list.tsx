import {
  Alert,
  Box,
  Center,
  Collapse,
  Group,
  ScrollArea,
  Stack,
  Text,
  UnstyledButton,
} from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { IconChevronRight, IconInfoCircle } from '@tabler/icons-react';
import { useEffect, useRef, useState } from 'react';

import type { SessionMessage, SessionMessagePart } from '@/core/api/types';

import classes from './agent-chat.module.css';
import type { TranscriptAnnotation } from './transcript-annotations';
import { weaveTranscript } from './transcript-annotations';

/**
 * Ceiling on how much of a tool payload is rendered.
 *
 * A tool result is agent-controlled and unbounded; pasting a megabyte of JSON
 * into the DOM is a hang. The rest is one round trip away in the trace.
 */
const MAX_JSON_CHARS = 20000;

/** How close to the bottom counts as "following the conversation". */
const AUTOSCROLL_THRESHOLD_PX = 120;

function formatJson(value: unknown): string {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return 'This payload could not be displayed.';
  }
  if (text.length <= MAX_JSON_CHARS) return text;
  return `${text.slice(0, MAX_JSON_CHARS)}\n… truncated, ${
    text.length - MAX_JSON_CHARS
  } more characters`;
}

function roleLabel(message: SessionMessage): string {
  if (message.role === 'user') return 'You';
  if (message.role === 'system') return 'System';
  return message.author ?? 'Agent';
}

function formatTime(timestamp: string | null | undefined): string | null {
  if (!timestamp) return null;
  const at = new Date(timestamp);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleTimeString();
}

type ToolPartProps = {
  label: string;
  toolName: string | null | undefined;
  payload: Record<string, unknown> | null | undefined;
  testId: string;
};

/**
 * A tool call or its result, collapsed by default and expandable to raw JSON.
 *
 * Collapsed because a transcript is a conversation and a tool payload is
 * evidence: it should be one click away, not in the middle of the sentence.
 * Expanded, it is the payload as it arrived, rendered as text.
 */
function ToolPart({ label, toolName, payload, testId }: ToolPartProps) {
  const [open, setOpen] = useState(false);

  return (
    <Box className={classes.toolBlock} data-testid={testId}>
      <UnstyledButton
        className={classes.toolHeader}
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        data-testid={`${testId}-toggle`}
      >
        <Group gap="xs" wrap="nowrap" align="center">
          <IconChevronRight
            size={14}
            stroke={2}
            className={`${classes.chevron} ${open ? classes.chevronOpen : ''}`}
          />
          <Text size="xs" c="dimmed" style={{ flexShrink: 0 }}>
            {label}
          </Text>
          <Text size="xs" fw={500} className={classes.toolName} lineClamp={1}>
            {toolName ?? 'unnamed tool'}
          </Text>
        </Group>
      </UnstyledButton>

      <Collapse in={open}>
        {/* Mounted only while open. Collapse keeps its children in the DOM,
            and a window of 200 messages each carrying a capped-but-large tool
            payload is exactly the hang the cap exists to prevent.
            Plain text. No markdown, no HTML: this is agent-controlled content
            in an authenticated operator console. */}
        {open ? (
          <pre className={classes.toolJson} data-testid={`${testId}-json`}>
            {payload === null || payload === undefined
              ? 'No payload.'
              : formatJson(payload)}
          </pre>
        ) : null}
      </Collapse>
    </Box>
  );
}

function MessagePartView({
  part,
  messageIndex,
  partIndex,
}: {
  part: SessionMessagePart;
  messageIndex: number;
  partIndex: number;
}) {
  const testId = `chat-part-${messageIndex}-${partIndex}`;

  if (part.kind === 'text') {
    return (
      <Text
        component="div"
        size="sm"
        className={classes.messageText}
        data-testid={testId}
      >
        {part.text ?? ''}
      </Text>
    );
  }

  if (part.kind === 'tool_call') {
    return (
      <ToolPart
        label="Called"
        toolName={part.tool_name}
        payload={part.arguments}
        testId={testId}
      />
    );
  }

  if (part.kind === 'tool_result') {
    return (
      <ToolPart
        label="Returned"
        toolName={part.tool_name}
        payload={part.result}
        testId={testId}
      />
    );
  }

  return (
    <Text size="xs" c="dimmed" fs="italic" data-testid={testId}>
      This message contained something this panel cannot display.
    </Text>
  );
}

function MessageBubble({ message }: { message: SessionMessage }) {
  const time = formatTime(message.timestamp);
  const parts = message.parts ?? [];
  const roleClass =
    message.role === 'user'
      ? classes.messageUser
      : message.role === 'system'
        ? classes.messageSystem
        : classes.messageAgent;

  return (
    <Box
      className={`${classes.message} ${roleClass}`}
      data-testid="chat-message"
      data-role={message.role}
      data-index={message.index}
    >
      <Stack gap={6}>
        <Group gap="xs" align="baseline" wrap="nowrap">
          <Text size="xs" fw={600} c="dimmed" lineClamp={1}>
            {roleLabel(message)}
          </Text>
          {time ? (
            <Text size="xs" c="dimmed">
              {time}
            </Text>
          ) : null}
        </Group>

        {parts.length === 0 ? (
          <Text size="xs" c="dimmed" fs="italic">
            Empty message.
          </Text>
        ) : (
          parts.map((part, partIndex) => (
            <MessagePartView
              key={`${message.index}-${partIndex}`}
              part={part}
              messageIndex={message.index}
              partIndex={partIndex}
            />
          ))
        )}
      </Stack>
    </Box>
  );
}

/**
 * A nudge, rendered where it landed, showing the exact text the model saw.
 *
 * Showing the body verbatim is the point of this component. Operator guidance
 * is a text box whose whole value rests on the person believing the agent was
 * told what they typed, and the only way to earn that is to show them what was
 * handed over. It renders as plain text under the same rule as everything else
 * here: the operator half of a conversation is no more trusted than the model
 * half in a console where every admin endpoint is same-origin.
 */
function NudgeAnnotation({
  annotation,
}: {
  annotation: Extract<TranscriptAnnotation, { kind: 'nudge' }>;
}) {
  const rejected = annotation.status === 'rejected';

  return (
    <Box
      className={`${classes.message} ${classes.messageOperator}`}
      data-testid="chat-nudge-marker"
      data-nudge-status={annotation.status}
    >
      <Stack gap={6}>
        <Text size="xs" fw={600} c="dimmed">
          {rejected
            ? 'Nudge refused by a control'
            : 'Nudge delivered to the agent'}
        </Text>
        <Text
          component="div"
          size="sm"
          className={classes.messageText}
          data-testid="chat-nudge-body"
        >
          {annotation.body}
        </Text>
        {rejected ? (
          <Text size="xs" c="dimmed">
            {annotation.rejectedByControl
              ? `Refused by ${annotation.rejectedByControl}. The agent never saw it.`
              : 'This nudge could not be evaluated, so it was not delivered.'}
          </Text>
        ) : (
          <Text size="xs" c="dimmed">
            Added to the agent&apos;s next model call as an operator message.
          </Text>
        )}
      </Stack>
    </Box>
  );
}

/**
 * A stop, rendered from Agent Control's own record.
 *
 * Never inferred from transcript text: the executor cannot distinguish a
 * blocked response from ordinary model output, so an agent that wrote
 * "Stopped by an operator" would otherwise forge one of these.
 */
function HaltAnnotation({
  annotation,
}: {
  annotation: Extract<TranscriptAnnotation, { kind: 'halt' }>;
}) {
  const text = (() => {
    if (annotation.mode === 'restart') {
      return 'Executor restarted by an operator. The agent’s last step may be missing from this transcript.';
    }
    if (annotation.boundary === 'tool') {
      return annotation.toolName
        ? `Stopped by an operator before running ${annotation.toolName}.`
        : 'Stopped by an operator before its next step.';
    }
    return 'Stopped by an operator before the next model call.';
  })();

  return (
    <Box
      className={`${classes.message} ${classes.messageSystem}`}
      data-testid="chat-halt-marker"
      data-halt-boundary={annotation.boundary ?? 'unknown'}
    >
      <Stack gap={4}>
        <Text component="div" size="sm" className={classes.messageText}>
          {text}
        </Text>
        {annotation.ended ? null : (
          <Text size="xs" c="dimmed">
            The executor acknowledged the stop; this turn has not been recorded
            as ended yet.
          </Text>
        )}
      </Stack>
    </Box>
  );
}

type MessageListProps = {
  messages: SessionMessage[];
  /**
   * Nudges that reached a model and stops that landed. Rendered inline among
   * the messages, from Agent Control's rows rather than from transcript text.
   */
  annotations?: TranscriptAnnotation[];
  hasEarlier: boolean;
  onLoadEarlier: () => void;
  onJumpToLatest: () => void;
  isTailing: boolean;
  isLoading: boolean;
  notice: string | null;
};

/**
 * The transcript.
 *
 * Every message part is rendered as text. There is no markdown renderer and no
 * HTML sink anywhere in this tree, deliberately: an agent that fetched an
 * attacker's page and echoed it would otherwise become stored XSS in a console
 * where every admin endpoint is same-origin.
 */
export function MessageList({
  messages,
  annotations,
  hasEarlier,
  onLoadEarlier,
  onJumpToLatest,
  isTailing,
  isLoading,
  notice,
}: MessageListProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const pinnedToBottom = useRef(true);
  const lastIndex = messages[messages.length - 1]?.index ?? -1;
  const items = weaveTranscript(messages, annotations ?? []);

  // Follow the conversation only while the reader is at the bottom. Yanking
  // someone back down while they read an earlier answer is worse than not
  // scrolling at all.
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport || !pinnedToBottom.current) return;
    viewport.scrollTop = viewport.scrollHeight;
  }, [lastIndex, messages.length, isTailing]);

  const handleScrollPositionChange = () => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const distanceFromBottom =
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    pinnedToBottom.current = distanceFromBottom <= AUTOSCROLL_THRESHOLD_PX;
  };

  return (
    <ScrollArea
      className={classes.transcript}
      viewportRef={viewportRef}
      onScrollPositionChange={handleScrollPositionChange}
      data-testid="chat-transcript"
    >
      <Stack className={classes.transcriptBody} gap="sm">
        {notice ? (
          <Alert
            icon={<IconInfoCircle size={16} />}
            color="yellow"
            variant="light"
            data-testid="chat-transcript-notice"
          >
            {notice}
          </Alert>
        ) : null}

        {hasEarlier ? (
          <Center>
            <Button
              variant="outline"
              size="sm"
              onClick={onLoadEarlier}
              disabled={isLoading}
              data-testid="chat-load-earlier"
            >
              Load earlier messages
            </Button>
          </Center>
        ) : null}

        {!isTailing ? (
          <Center>
            <Button
              variant="ghost"
              size="sm"
              onClick={onJumpToLatest}
              data-testid="chat-jump-to-latest"
            >
              Jump to the latest messages
            </Button>
          </Center>
        ) : null}

        {messages.length === 0 && !isLoading ? (
          <Center py="xl">
            <Text size="sm" c="dimmed" data-testid="chat-transcript-empty">
              No messages yet. Say something to start.
            </Text>
          </Center>
        ) : null}

        {items.map((item) => {
          if (item.kind === 'message') {
            return <MessageBubble key={item.key} message={item.message} />;
          }
          return item.annotation.kind === 'nudge' ? (
            <NudgeAnnotation key={item.key} annotation={item.annotation} />
          ) : (
            <HaltAnnotation key={item.key} annotation={item.annotation} />
          );
        })}
      </Stack>
    </ScrollArea>
  );
}
