import { Alert, Box, Center, Loader, Stack, Text } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';
import { IconAlertCircle, IconInfoCircle } from '@tabler/icons-react';
import { useCallback, useMemo, useState } from 'react';

import { isApiError } from '@/core/api/errors';
import type { ProblemDetail } from '@/core/api/types';
import {
  isExecutorStillWorking,
  isTurnInFlight,
  useAgentSession,
  useAgentSessions,
  useCreateAgentSession,
} from '@/core/hooks/query-hooks/use-agent-sessions';
import {
  haltForTurn,
  useCreateHalt,
  useHalts,
} from '@/core/hooks/query-hooks/use-halts';
import {
  useCancelNudge,
  useCreateNudge,
  useNudges,
} from '@/core/hooks/query-hooks/use-nudges';
import { usePlan } from '@/core/hooks/query-hooks/use-plan';
import {
  TRANSCRIPT_WINDOW,
  type TranscriptWindowStart,
  useSessionMessages,
} from '@/core/hooks/query-hooks/use-session-messages';
import {
  isTurnAbandoned,
  useStartTurn,
} from '@/core/hooks/query-hooks/use-start-turn';

import classes from './agent-chat.module.css';
import { MessageComposer } from './message-composer';
import { MessageList } from './message-list';
import { NudgePanel } from './nudge-panel';
import { ProgressRail } from './progress-rail';
import { SessionSwitcher } from './session-switcher';
import { haltAnnotations, nudgeAnnotations } from './transcript-annotations';
import { useHaltState } from './use-halt-state';

function problemOf(error: unknown): ProblemDetail | null {
  if (isApiError(error)) return error.problemDetail;
  const candidate = error as Partial<ProblemDetail> | null | undefined;
  if (candidate?.detail && candidate?.error_code) {
    return candidate as ProblemDetail;
  }
  return null;
}

/**
 * A failure, stated in the words the server used.
 *
 * The server writes every one of these by hand and never echoes an executor's
 * own response body, so passing its text through is both safer and more
 * accurate than paraphrasing it here. This renders inline, above the composer,
 * rather than as a toast: a 503 that scrolls away is a 503 nobody read.
 */
function ProblemBanner({
  error,
  fallbackTitle,
  fallbackMessage,
  testId,
}: {
  error: unknown;
  fallbackTitle: string;
  fallbackMessage: string;
  testId: string;
}) {
  const problem = problemOf(error);
  return (
    <Alert
      icon={<IconAlertCircle size={16} />}
      color="red"
      variant="light"
      title={problem?.title ?? fallbackTitle}
      data-testid={testId}
      data-error-code={problem?.error_code ?? 'UNKNOWN_ERROR'}
    >
      <Stack gap={4}>
        <Text size="sm">{problem?.detail ?? fallbackMessage}</Text>
        {problem?.hint ? (
          <Text size="xs" c="dimmed">
            {problem.hint}
          </Text>
        ) : null}
      </Stack>
    </Alert>
  );
}

/**
 * Chat with one agent.
 *
 * What this panel can honestly claim is narrow, and the copy sticks to it. A
 * turn is a blocking request: the panel waits and shows how long it has been
 * waiting. Everything the agent produced is read back from the transcript,
 * which is the authoritative record.
 *
 * Two things a person can do to an agent that is already working, and they are
 * different acts with different promises. A **nudge** adds guidance to the
 * agent's next model call and the agent carries on. A **stop** ends the turn
 * at the agent's next boundary. Neither is immediate: a tool that has already
 * started finishes, and whatever it was doing has already happened.
 *
 * Both are rendered from Agent Control's own records rather than from
 * transcript text. The executor keeps no event for either - a nudge is
 * appended to a request, and a blocked response is indistinguishable from
 * ordinary model output - so reading them out of the transcript would also
 * mean an agent could forge either one by saying the right sentence.
 */
export function AgentChat({ agentName }: { agentName: string }) {
  const [selectedSessionKey, setSelectedSessionKey] = useState<string | null>(
    null
  );
  // Both of these belong to one conversation, so they carry the session they
  // were set for. Switching chats then resets them by derivation rather than
  // by an effect that fires a render late.
  const [transcriptWindow, setTranscriptWindow] = useState<{
    sessionKey: string;
    start: number;
  } | null>(null);
  const [localTurnStart, setLocalTurnStart] = useState<{
    sessionKey: string;
    at: number;
  } | null>(null);

  const sessionsQuery = useAgentSessions(agentName);
  const sessions = useMemo(
    () => sessionsQuery.data?.sessions ?? [],
    [sessionsQuery.data?.sessions]
  );
  const createSession = useCreateAgentSession(agentName);

  // The list is newest first, so the first entry is the chat someone last
  // used. A selection that no longer exists falls back to it rather than
  // leaving the panel pointed at nothing.
  const activeSessionKey = useMemo(() => {
    const stillThere = sessions.some(
      (session) => session.session_key === selectedSessionKey
    );
    if (selectedSessionKey && stillThere) return selectedSessionKey;
    return sessions[0]?.session_key ?? null;
  }, [sessions, selectedSessionKey]);

  const windowStart: TranscriptWindowStart =
    transcriptWindow && transcriptWindow.sessionKey === activeSessionKey
      ? transcriptWindow.start
      : null;

  const startTurn = useStartTurn(activeSessionKey);

  // One mutation serves the whole panel, so its pending and error state has to
  // be attributed to the chat it was started in. Without this, switching chats
  // while a turn runs shows the new chat as busy and replays the old chat's
  // failure banner underneath it.
  const turnStartedHere =
    localTurnStart !== null && localTurnStart.sessionKey === activeSessionKey;
  const waitingHere = startTurn.isPending && turnStartedHere;

  const sessionQuery = useAgentSession(activeSessionKey, {
    forcePoll: waitingHere,
  });
  const session = sessionQuery.data?.session;

  const turnRunning = waitingHere || isTurnInFlight(session);
  const executorStillWorking = isExecutorStillWorking(session);

  const transcript = useSessionMessages({
    sessionKey: activeSessionKey,
    windowStart,
    isTurnActive: turnRunning || executorStillWorking,
  });

  const nudgesQuery = useNudges(activeSessionKey, {
    isTurnActive: turnRunning || executorStillWorking,
  });
  const nudges = useMemo(
    () => nudgesQuery.data?.nudges ?? [],
    [nudgesQuery.data?.nudges]
  );
  const createNudge = useCreateNudge(activeSessionKey);
  const cancelNudge = useCancelNudge(activeSessionKey);

  const haltsQuery = useHalts(activeSessionKey, {
    isTurnActive: turnRunning || executorStillWorking,
  });
  const halts = useMemo(
    () => haltsQuery.data?.halts ?? [],
    [haltsQuery.data?.halts]
  );
  const createHalt = useCreateHalt(activeSessionKey);

  // The stop button follows the *liveness marker*, not the turn lock. After a
  // timeout the lock is clear and the agent is still working, which is exactly
  // when a person reaches for stop; keying on the lock would hide the control
  // at that moment.
  const liveTraceId = session?.in_flight_trace_id ?? null;
  const liveHalt = haltForTurn(halts, liveTraceId);
  const haltState = useHaltState(liveHalt, createHalt.isPending);

  const planQuery = usePlan(activeSessionKey, {
    isTurnActive: turnRunning || executorStillWorking,
  });

  // Turns counted from the transcript, because that is the only place this
  // console can count them: the session row holds no turn counter and inventing
  // one from message totals would count the agent's replies as turns too. A
  // window that does not reach the start of the conversation makes the count a
  // floor, which the rail says rather than rounding over.
  const turnCount = useMemo(
    () =>
      (transcript.data?.messages ?? []).filter(
        (message) => message.role === 'user'
      ).length,
    [transcript.data?.messages]
  );
  const turnCountIsExact = !(transcript.data?.hasEarlier ?? false);

  const annotations = useMemo(
    () => [...nudgeAnnotations(nudges), ...haltAnnotations(halts)],
    [nudges, halts]
  );

  const handleNudge = useCallback(
    (body: string) => {
      createNudge.mutate(body);
    },
    [createNudge]
  );

  const handleCancelNudge = useCallback(
    (nudgeId: number) => {
      cancelNudge.mutate(nudgeId);
    },
    [cancelNudge]
  );

  const handleStop = useCallback(() => {
    createHalt.mutate();
  }, [createHalt]);

  // `localTurnStart` is deliberately not cleared here. It records which chat
  // the in-flight mutation belongs to, and that is what keeps a turn started
  // in one chat from being reported in another.
  const handleSelectSession = useCallback((sessionKey: string) => {
    setSelectedSessionKey(sessionKey);
    setTranscriptWindow(null);
  }, []);

  const handleNewChat = useCallback(() => {
    createSession.mutate(undefined, {
      onSuccess: (created) => handleSelectSession(created.session.session_key),
    });
  }, [createSession, handleSelectSession]);

  const handleSend = useCallback(
    (message: string, attachmentKeys: string[]) => {
      if (!activeSessionKey) return;
      setTranscriptWindow(null);
      setLocalTurnStart({ sessionKey: activeSessionKey, at: Date.now() });
      startTurn.mutate({ message, attachmentKeys });
    },
    [activeSessionKey, startTurn]
  );

  const handleLoadEarlier = useCallback(() => {
    if (!activeSessionKey) return;
    const firstIndex = transcript.data?.firstIndex ?? 0;
    setTranscriptWindow({
      sessionKey: activeSessionKey,
      start: Math.max(0, firstIndex - TRANSCRIPT_WINDOW),
    });
  }, [activeSessionKey, transcript.data?.firstIndex]);

  const serverTurnStartedAt = session?.in_flight_since
    ? new Date(session.in_flight_since).getTime()
    : null;
  const localTurnStartedAt =
    localTurnStart && localTurnStart.sessionKey === activeSessionKey
      ? localTurnStart.at
      : null;
  const turnStartedAt =
    serverTurnStartedAt !== null && !Number.isNaN(serverTurnStartedAt)
      ? serverTurnStartedAt
      : localTurnStartedAt;

  const turnError =
    turnStartedHere && startTurn.error && !isTurnAbandoned(startTurn.error)
      ? startTurn.error
      : null;
  const abandoned = turnStartedHere && isTurnAbandoned(startTurn.error);

  const renderBody = () => {
    if (sessionsQuery.isLoading) {
      return (
        <Center className={classes.transcript}>
          <Stack align="center" gap="sm">
            <Loader size="sm" />
            <Text size="sm" c="dimmed">
              Loading chats…
            </Text>
          </Stack>
        </Center>
      );
    }

    if (sessionsQuery.error) {
      return (
        <Box className={classes.transcriptBody}>
          <ProblemBanner
            error={sessionsQuery.error}
            fallbackTitle="Chats could not be loaded"
            fallbackMessage="Failed to load this agent's chats. Please try again later."
            testId="chat-sessions-error"
          />
        </Box>
      );
    }

    if (!activeSessionKey) {
      return (
        <Center className={classes.transcript} p="xl">
          <Stack align="center" gap="sm" maw={460}>
            <Text size="sm" fw={500}>
              No chats with this agent yet
            </Text>
            <Text size="xs" c="dimmed" ta="center">
              Opening a chat creates a conversation on the executor this agent
              is bound to. Nothing is sent to a model until you send a message.
            </Text>
            <Button
              variant="filled"
              size="sm"
              onClick={handleNewChat}
              loading={createSession.isPending}
              data-testid="chat-start-first-session"
            >
              Start a chat
            </Button>
            {createSession.error ? (
              <ProblemBanner
                error={createSession.error}
                fallbackTitle="The chat could not be opened"
                fallbackMessage="Failed to open a chat with this agent."
                testId="chat-create-error"
              />
            ) : null}
          </Stack>
        </Center>
      );
    }

    return (
      <MessageList
        messages={transcript.data?.messages ?? []}
        annotations={annotations}
        hasEarlier={transcript.data?.hasEarlier ?? false}
        onLoadEarlier={handleLoadEarlier}
        onJumpToLatest={() => setTranscriptWindow(null)}
        isTailing={windowStart === null}
        isLoading={transcript.isFetching}
        notice={transcript.data?.notice ?? null}
      />
    );
  };

  return (
    <Box className={classes.panel} data-testid="agent-chat-panel">
      <Box className={classes.panelHeader}>
        <SessionSwitcher
          sessions={sessions}
          activeSessionKey={activeSessionKey}
          onSelect={handleSelectSession}
          onNewChat={handleNewChat}
          isLoading={sessionsQuery.isLoading}
          isCreating={createSession.isPending}
          canCreate={!sessionsQuery.isLoading}
        />
      </Box>

      {renderBody()}

      {activeSessionKey ? (
        <>
          {transcript.error ? (
            <Box px="md" pt="md">
              <ProblemBanner
                error={transcript.error}
                fallbackTitle="The transcript could not be read"
                fallbackMessage="Failed to read this conversation. Please try again later."
                testId="chat-transcript-error"
              />
            </Box>
          ) : null}

          {turnError ? (
            <Box px="md" pt="md">
              <ProblemBanner
                error={turnError}
                fallbackTitle="The turn failed"
                fallbackMessage="The agent could not be reached. Please try again later."
                testId="chat-turn-error"
              />
            </Box>
          ) : null}

          {createSession.error && sessions.length > 0 ? (
            <Box px="md" pt="md">
              <ProblemBanner
                error={createSession.error}
                fallbackTitle="The chat could not be opened"
                fallbackMessage="Failed to open a chat with this agent."
                testId="chat-create-error"
              />
            </Box>
          ) : null}

          {abandoned && !turnRunning && !executorStillWorking ? (
            <Box px="md" pt="md">
              <Alert
                icon={<IconInfoCircle size={16} />}
                color="blue"
                variant="light"
                title="Stopped waiting"
                data-testid="chat-abandoned-notice"
              >
                <Text size="sm">
                  This panel stopped waiting for that turn. The agent was not
                  stopped: anything it produces will appear in this transcript.
                </Text>
              </Alert>
            </Box>
          ) : null}

          {executorStillWorking ? (
            <Box px="md" pt="md">
              <Alert
                icon={<IconInfoCircle size={16} />}
                color="yellow"
                variant="light"
                title="An earlier turn was not seen through to the end"
                data-testid="chat-executor-busy"
              >
                <Text size="sm">
                  This server stopped waiting for an earlier turn, so its
                  outcome was never recorded here. The executor ends an
                  invocation when the request it arrived on is dropped, so that
                  turn most likely stopped there; whatever it managed to write
                  appears in this transcript. Sending another message starts a
                  fresh turn.
                </Text>
              </Alert>
            </Box>
          ) : null}

          {createHalt.error ? (
            <Box px="md" pt="md">
              <ProblemBanner
                error={createHalt.error}
                fallbackTitle="The agent could not be stopped"
                fallbackMessage="The stop was not recorded. The turn is still running."
                testId="chat-halt-error"
              />
            </Box>
          ) : null}

          {createNudge.error ? (
            <Box px="md" pt="md">
              <ProblemBanner
                error={createNudge.error}
                fallbackTitle="The nudge was not queued"
                fallbackMessage="The guidance was not queued, so the agent will not see it."
                testId="chat-nudge-error"
              />
            </Box>
          ) : null}

          {cancelNudge.error ? (
            <Box px="md" pt="md">
              <ProblemBanner
                error={cancelNudge.error}
                fallbackTitle="The nudge could not be withdrawn"
                fallbackMessage="This nudge could not be withdrawn."
                testId="chat-nudge-cancel-error"
              />
            </Box>
          ) : null}

          {/* The agent's own account of its work, and never this panel's
              inference about it. Placed above the two composers because it is
              context for what to say next, not a control. */}
          <ProgressRail
            plan={planQuery.data?.plan ?? null}
            readState={
              planQuery.error
                ? 'error'
                : planQuery.data === undefined
                  ? 'loading'
                  : 'ready'
            }
            turnCount={turnCount}
            turnCountIsExact={turnCountIsExact}
            sessionStartedAt={session?.created_at ?? null}
            lastActivityAt={session?.last_activity_at ?? null}
            traceId={liveTraceId ?? session?.last_trace_id ?? null}
            isTurnActive={turnRunning || executorStillWorking}
          />

          <NudgePanel
            nudges={nudges}
            onSend={handleNudge}
            onCancel={handleCancelNudge}
            isSending={createNudge.isPending}
            isCancelling={cancelNudge.isPending}
            disabled={!activeSessionKey}
            agentName={agentName}
          />

          {/* Keyed on the session, so switching chats starts a fresh composer.
              Its files are the load-bearing part: a chip holds an attachment
              key that only its own session can resolve, and carrying one across
              a switch would post it against a session that 404s it - refusing
              the turn after the draft had already been cleared. The draft goes
              with it, which is the same rule the transcript already follows. */}
          <MessageComposer
            key={activeSessionKey}
            onSend={handleSend}
            sessionKey={activeSessionKey}
            onStopWaiting={startTurn.abandon}
            onStop={handleStop}
            canStop={Boolean(liveTraceId)}
            isStopping={createHalt.isPending}
            haltState={haltState}
            haltToolName={liveHalt?.applied_tool_name ?? null}
            isTurnInFlight={turnRunning}
            isWaitingHere={waitingHere}
            turnStartedAt={turnStartedAt}
            disabled={!activeSessionKey}
            hasStoppedWaiting={abandoned}
            agentName={agentName}
          />
        </>
      ) : null}
    </Box>
  );
}
