import type { Halt } from '@/core/api/types';
import { getAgentRoute } from '@/core/constants/agent-routes';

import { chatAgentName, expect, mockRoutes, test } from './fixtures';

const chatUrl = getAgentRoute('agent-1', { tab: 'chat' });
const SESSION = 'sess-refunds';
const LIVE_TRACE = 'trace-live';

/**
 * A session whose liveness marker is set but whose turn lock is not.
 *
 * That is a real state and it is the one the stop control has to survive: the
 * request timed out, this server released the lock, and the marker says an
 * invocation may still be out there. Keying the control on the lock would hide
 * it at exactly that moment.
 */
const liveTurn = {
  [SESSION]: { in_flight_since: null, in_flight_trace_id: LIVE_TRACE },
};

function halt(overrides: Partial<Halt> = {}): Halt {
  return {
    id: 1,
    session_key: SESSION,
    target_trace_id: LIVE_TRACE,
    mode: 'graceful',
    status: 'pending',
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

test.describe('Agent chat: stopping the agent', () => {
  test('offers no stop when there is no invocation to stop', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);
    await expect(mockedPage.getByTestId('chat-composer')).toBeVisible();

    // An idle chat has nothing running, so a stop button would be a button
    // whose only possible outcome is a 409.
    await expect(mockedPage.getByTestId('chat-stop-responding')).toHaveCount(0);
    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveCount(0);
  });

  test('offers the stop while an invocation is live, even after the wait ended', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, { detailOverrides: liveTurn });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-stop-responding')).toBeVisible();
    // And the panel says why it thinks something is still running.
    await expect(mockedPage.getByTestId('chat-executor-busy')).toBeVisible();
  });

  test('pressing stop says what it will and will not do, and never claims it stopped', async ({
    mockedPage,
  }) => {
    const chat = await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-stop-responding').click();

    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveText(
      `Stopping ${chatAgentName}…`
    );
    // The one sentence that has to be there before the click is honoured, not
    // after: a tool that has already started is not interrupted by anything.
    await expect(
      mockedPage.getByText(
        'Waiting for the agent to reach its next step. A tool that is already running will finish first, and whatever it was doing will have happened.'
      )
    ).toBeVisible();

    // Polled, not read once: the click starts a request and this assertion is
    // about what that request carried, so reading the recorder on the very
    // next tick is a race the assertion loses whenever the suite is busy.
    await expect.poll(() => chat.haltsRequested).toEqual([SESSION]);
    expect(chat.halts[SESSION]?.[0]?.target_trace_id).toBe(LIVE_TRACE);
    // Pending is not stopped, so nothing is drawn in the transcript yet.
    await expect(mockedPage.getByTestId('chat-halt-marker')).toHaveCount(0);
  });

  test('the stop control goes once a stop is outstanding, so it cannot be pressed twice', async ({
    mockedPage,
  }) => {
    const chat = await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-stop-responding').click();
    await expect(mockedPage.getByTestId('chat-halt-state')).toBeVisible();

    await expect(mockedPage.getByTestId('chat-stop-responding')).toHaveCount(0);
    await expect.poll(() => chat.haltsRequested).toEqual([SESSION]);
  });

  test('an acknowledged stop says the executor blocked, not that the turn ended', async ({
    mockedPage,
  }) => {
    // The executor has acknowledged the stop at a tool boundary. This server
    // has not seen the turn end, and the copy must not pretend otherwise: the
    // acknowledgement comes from the party being stopped.
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
      halts: {
        [SESSION]: [
          halt({
            status: 'applied',
            applied_at: new Date().toISOString(),
            applied_at_boundary: 'tool',
            applied_tool_name: 'send_email',
            turn_ended_at: null,
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveText(
      'Stop acknowledged, waiting for the turn to end'
    );
    await expect(
      mockedPage.getByText(
        'The agent stopped before running send_email. This turn will close shortly.'
      )
    ).toBeVisible();
  });

  test('a stop that has sat unacknowledged brings back a way off the screen', async ({
    mockedPage,
  }) => {
    // Pressed long enough ago that the panel stops implying it is about to
    // land. This is what a long tool call looks like from here - and also what
    // a turn that ended on the executor without this server hearing looks
    // like. Either way, an operator left on "Stopping…" forever has no move.
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
      halts: {
        [SESSION]: [
          halt({ created_at: new Date(Date.now() - 120_000).toISOString() }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveText(
      'The stop has not reached the agent yet'
    );
    await expect(
      mockedPage.getByText(
        'It lands at the next model call or the next tool, so a long-running tool holds it up. If this server had already stopped waiting for the turn, it may have ended on the executor and there is nothing left to stop.',
        { exact: false }
      )
    ).toBeVisible();
  });

  test('an acknowledged stop that outstays its welcome is a warning, not a spinner', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
      halts: {
        [SESSION]: [
          halt({
            status: 'applied',
            applied_at: new Date(Date.now() - 120_000).toISOString(),
            applied_at_boundary: 'model',
            turn_ended_at: null,
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveText(
      'Stop acknowledged, but the turn has not ended'
    );
    await expect(
      mockedPage.getByText(
        'The executor says it stopped, but this server has not seen the turn end. It may still be spending.'
      )
    ).toBeVisible();
  });

  test('a stop the turn outran is not rendered as a stop', async ({
    mockedPage,
  }) => {
    // Expired means the turn finished before the stop reached a boundary. It
    // did not stop anything, so drawing a "stopped by an operator" marker
    // would put an event in the transcript that never happened.
    await mockRoutes.agentSessions(mockedPage, {
      halts: {
        [SESSION]: [
          halt({ status: 'expired', turn_ended_at: new Date().toISOString() }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);
    await expect(mockedPage.getByTestId('chat-transcript')).toBeVisible();

    await expect(mockedPage.getByTestId('chat-halt-marker')).toHaveCount(0);
    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveCount(0);
  });

  test('a stop bound to an earlier turn is not shown against the live one', async ({
    mockedPage,
  }) => {
    // A halt belongs to exactly one turn. Matching on recency instead of on
    // the turn's trace is how a stale record ends up rendered against work
    // nobody asked to stop.
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
      halts: {
        [SESSION]: [
          halt({ id: 2, target_trace_id: 'trace-from-an-older-turn' }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-stop-responding')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-halt-state')).toHaveCount(0);
  });

  test('renders a refused stop in the panel rather than swallowing it', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
      haltCreateError: {
        status: 409,
        errorCode: 'TURN_NOT_IN_FLIGHT',
        title: 'Nothing to stop',
        detail:
          'This session is not running a turn, so there is nothing to stop.',
        hint: 'Send the message you want instead.',
      },
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-stop-responding').click();

    const banner = mockedPage.getByTestId('chat-halt-error');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute(
      'data-error-code',
      'TURN_NOT_IN_FLIGHT'
    );
    await expect(
      mockedPage.getByText(
        'This session is not running a turn, so there is nothing to stop.'
      )
    ).toBeVisible();
  });
});

test.describe('Agent chat: stop markers in the transcript', () => {
  test('renders the model-boundary marker', async ({ mockedPage }) => {
    await mockRoutes.agentSessions(mockedPage, {
      halts: {
        [SESSION]: [
          halt({
            status: 'applied',
            applied_at: '2024-03-02T09:05:00Z',
            applied_at_boundary: 'model',
            turn_ended_at: '2024-03-02T09:05:01Z',
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    const marker = mockedPage.getByTestId('chat-halt-marker');
    await expect(marker).toHaveText(
      'Stopped by an operator before the next model call.'
    );
    await expect(marker).toHaveAttribute('data-halt-boundary', 'model');
  });

  test('renders the tool-boundary marker, naming the tool that did not run', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      halts: {
        [SESSION]: [
          halt({
            status: 'applied',
            applied_at: '2024-03-02T09:05:00Z',
            applied_at_boundary: 'tool',
            applied_tool_name: 'send_email',
            turn_ended_at: '2024-03-02T09:05:01Z',
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-halt-marker')).toHaveText(
      'Stopped by an operator before running send_email.'
    );
  });

  test('renders the restart marker and warns that the transcript is short', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      halts: {
        [SESSION]: [
          halt({
            mode: 'restart',
            status: 'applied',
            applied_at: '2024-03-02T09:05:00Z',
            applied_at_boundary: 'process',
            turn_ended_at: '2024-03-02T09:05:00Z',
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-halt-marker')).toHaveText(
      'Executor restarted by an operator. The agent’s last step may be missing from this transcript.'
    );
  });

  test('a marker whose turn has not ended says so rather than reading as done', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: liveTurn,
      halts: {
        [SESSION]: [
          halt({
            status: 'applied',
            applied_at: '2024-03-02T09:05:00Z',
            applied_at_boundary: 'model',
            turn_ended_at: null,
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(
      mockedPage.getByText(
        'The executor acknowledged the stop; this turn has not been recorded as ended yet.'
      )
    ).toBeVisible();
  });

  test('renders an executor-supplied tool name as text and executes nothing', async ({
    mockedPage,
  }) => {
    // ``applied_tool_name`` is the one field in this design carrying bytes
    // chosen by a process running arbitrary agent code. The server checks it
    // against a strict identifier; this asserts that a name which somehow got
    // past that still cannot do anything in the console.
    await mockRoutes.agentSessions(mockedPage, {
      halts: {
        [SESSION]: [
          halt({
            status: 'applied',
            applied_at: '2024-03-02T09:05:00Z',
            applied_at_boundary: 'tool',
            applied_tool_name:
              '<img src=x onerror="window.__pwned=1">send_email',
            turn_ended_at: '2024-03-02T09:05:01Z',
          }),
        ],
      },
    });
    await mockedPage.goto(chatUrl);

    const marker = mockedPage.getByTestId('chat-halt-marker');
    await expect(marker).toContainText('<img src=x onerror=');
    expect(await marker.locator('img').count()).toBe(0);
    expect(
      await mockedPage.evaluate(
        () => (window as Window & { __pwned?: number }).__pwned
      )
    ).toBeUndefined();
  });
});
