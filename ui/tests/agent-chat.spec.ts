import type { Locator } from '@playwright/test';

import type { SessionMessage } from '@/core/api/types';
import { getAgentRoute } from '@/core/constants/agent-routes';

import { chatAgentName, expect, mockData, mockRoutes, test } from './fixtures';

const chatUrl = getAgentRoute('agent-1', { tab: 'chat' });

/** A promise a test resolves when it wants the pending turn to answer. */
function gate(): { promise: Promise<void>; release: () => void } {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

type Pwned = Window & { __pwned?: string };

test.describe('Agent chat panel', () => {
  test('shows the newest chat and renders its text verbatim', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(chatUrl);

    const panel = mockedPage.getByTestId('agent-chat-panel');
    await expect(panel).toBeVisible();

    // The list is newest first, so the panel opens on the refunds chat.
    await expect(mockedPage.getByTestId('chat-session-switcher')).toHaveValue(
      'Refund policy'
    );
    await expect(
      mockedPage.getByText('What is our refund window?')
    ).toBeVisible();
    await expect(
      mockedPage.getByText('Refunds are accepted within 30 days of delivery.')
    ).toBeVisible();
    await expect(mockedPage.getByTestId('chat-message')).toHaveCount(2);
  });

  test('renders an agent with no chats as an empty state that can start one', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      sessions: [],
      transcripts: {},
    });
    await mockedPage.goto(chatUrl);

    await expect(
      mockedPage.getByText('No chats with this agent yet', { exact: true })
    ).toBeVisible();
    // No conversation means nothing to type into: the composer is not there.
    await expect(mockedPage.getByTestId('chat-composer')).toHaveCount(0);

    await mockedPage.getByTestId('chat-start-first-session').click();

    await expect(mockedPage.getByTestId('chat-composer')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-transcript-empty')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-session-switcher')).toHaveValue(
      'New chat 1'
    );
  });

  test('sends a message and shows the reply that comes back', async ({
    mockedPage,
  }) => {
    const chat = await mockRoutes.agentSessions(mockedPage, {
      turnReply: [{ kind: 'text', text: 'The window is 30 days.' }],
    });
    await mockedPage.goto(chatUrl);
    await expect(mockedPage.getByTestId('chat-message')).toHaveCount(2);

    await mockedPage
      .getByTestId('chat-composer-input')
      .fill('  How long exactly?  ');
    await mockedPage.getByTestId('chat-send').click();

    await expect(mockedPage.getByText('The window is 30 days.')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-message')).toHaveCount(4);
    // Trimmed on the way out, and addressed to the chat that was open.
    expect(chat.turns).toEqual([
      { sessionKey: 'sess-refunds', message: 'How long exactly?' },
    ]);
    // The composer empties, so the next message is not the previous one again.
    await expect(mockedPage.getByTestId('chat-composer-input')).toHaveValue('');
  });

  test('disables the composer, counts the wait and offers to stop the agent while a turn runs', async ({
    mockedPage,
  }) => {
    const turn = gate();
    await mockRoutes.agentSessions(mockedPage, { turnGate: turn.promise });
    await mockedPage.goto(chatUrl);

    const input = mockedPage.getByTestId('chat-composer-input');
    await input.fill('Check the policy again');
    await mockedPage.getByTestId('chat-send').click();

    await expect(input).toBeDisabled();
    await expect(mockedPage.getByTestId('chat-send')).toBeDisabled();

    const elapsed = mockedPage.getByTestId('chat-turn-elapsed');
    await expect(elapsed).toHaveText(
      new RegExp(`Waiting for ${chatAgentName} · \\d+s`)
    );
    // The clock is running, not a static zero.
    await expect(elapsed).toHaveText(/· (?:[2-9]|\d\d+)s/, { timeout: 10000 });

    // Once the server reports a live invocation, the control on offer is the
    // one that actually stops the agent. "Stop waiting" is the secondary
    // control and it stays out of the way until the real stop is visibly not
    // landing, because two buttons where one of them cannot stop the agent is
    // the confusion this split exists to remove.
    await expect(mockedPage.getByTestId('chat-stop-responding')).toHaveText(
      'Stop responding'
    );
    await expect(mockedPage.getByTestId('chat-stop-waiting')).toHaveCount(0);

    // The copy separates the two acts, and says what neither of them does.
    await expect(
      mockedPage.getByText(
        'Stopping the agent ends the turn at its next step. Stopping the wait does not: the turn keeps running and its messages appear here.'
      )
    ).toBeVisible();

    turn.release();
    await expect(input).toBeEnabled();
  });

  test('stopping waiting abandons the request and keeps saying the turn is running', async ({
    mockedPage,
  }) => {
    const turn = gate();
    await mockRoutes.agentSessions(mockedPage, {
      turnGate: turn.promise,
      // The turn was sent and this server has recorded no invocation against
      // it yet. Nothing to stop, so the only control the panel can honestly
      // offer is the one that abandons this request.
      detailOverrides: { 'sess-refunds': { in_flight_trace_id: null } },
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-composer-input').fill('Take your time');
    await mockedPage.getByTestId('chat-send').click();
    await expect(mockedPage.getByTestId('chat-stop-waiting')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-stop-responding')).toHaveCount(0);

    await mockedPage.getByTestId('chat-stop-waiting').click();

    // The button goes because there is no longer a request to abandon, but the
    // session is still held, so the composer stays shut and says why.
    await expect(mockedPage.getByTestId('chat-stop-waiting')).toHaveCount(0);
    await expect(mockedPage.getByTestId('chat-turn-elapsed')).toHaveText(
      new RegExp(
        `Stopped waiting · ${chatAgentName} has been running for \\d+s`
      )
    );
    await expect(mockedPage.getByTestId('chat-composer-input')).toBeDisabled();

    turn.release();

    // Once the turn genuinely ends the panel reopens and states plainly that
    // nothing was cancelled.
    await expect(mockedPage.getByTestId('chat-abandoned-notice')).toBeVisible();
    await expect(
      mockedPage.getByText(
        'This panel stopped waiting for that turn. The agent was not stopped: anything it produces will appear in this transcript.'
      )
    ).toBeVisible();
    await expect(mockedPage.getByTestId('chat-composer-input')).toBeEnabled();
  });

  test('renders a 503 as an inline banner inside the panel, not a toast', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      turnError: {
        status: 503,
        errorCode: 'EXECUTOR_UNAVAILABLE',
        title: 'The executor is unavailable',
        detail: 'The executor for this agent did not answer.',
        hint: 'Check that the executor is running, then try again.',
      },
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-composer-input').fill('Anyone there?');
    await mockedPage.getByTestId('chat-send').click();

    const banner = mockedPage
      .getByTestId('agent-chat-panel')
      .getByTestId('chat-turn-error');
    await expect(banner).toBeVisible();
    await expect(banner).toHaveAttribute(
      'data-error-code',
      'EXECUTOR_UNAVAILABLE'
    );
    // The server's own words, not a paraphrase and not an upstream body.
    await expect(banner).toContainText(
      'The executor for this agent did not answer.'
    );
    await expect(banner).toContainText('The executor is unavailable');

    // Nothing scrolled away: no notification was raised at all.
    await expect(
      mockedPage.locator('[class*="Notification-root"]')
    ).toHaveCount(0);

    // A refusal leaves the chat usable.
    await expect(mockedPage.getByTestId('chat-composer-input')).toBeEnabled();
  });

  test('caps the transcript at 200 messages, offers to load earlier and does not scroll infinitely', async ({
    mockedPage,
  }) => {
    const long: SessionMessage[] = Array.from({ length: 250 }, (_, index) => ({
      index,
      role: index % 2 === 0 ? 'user' : 'agent',
      timestamp: '2024-03-02T09:00:00Z',
      parts: [{ kind: 'text', text: `message number ${index}` }],
    }));
    const chat = await mockRoutes.agentSessions(mockedPage, {
      sessions: [mockData.chatSessions[0]],
      transcripts: { 'sess-refunds': long },
    });
    await mockedPage.goto(chatUrl);

    const messages = mockedPage.getByTestId('chat-message');
    await expect(messages).toHaveCount(200);
    // The tail, not the head: 50 through 249.
    await expect(messages.first()).toHaveAttribute('data-index', '50');
    await expect(messages.last()).toHaveAttribute('data-index', '249');
    await expect(mockedPage.getByTestId('chat-load-earlier')).toBeVisible();

    // Scrolling to the top loads nothing on its own: the window moves on
    // request, not on scroll position.
    const readsBefore = chat.messageReads.length;
    const viewport = mockedPage
      .getByTestId('chat-transcript')
      .locator('.mantine-ScrollArea-viewport');
    const scrolled = await viewport.evaluate((element) => {
      const before = element.scrollTop;
      element.scrollTop = 0;
      return { before, after: element.scrollTop };
    });
    // Guards the guard: a transcript that never scrolled would make the
    // assertion below vacuous.
    expect(scrolled.before).toBeGreaterThan(0);
    expect(scrolled.after).toBe(0);
    await mockedPage.waitForTimeout(1000);
    await expect(messages).toHaveCount(200);
    expect(chat.messageReads.length).toBe(readsBefore);

    await mockedPage.getByTestId('chat-load-earlier').click();

    await expect(messages).toHaveCount(200);
    await expect(messages.first()).toHaveAttribute('data-index', '0');
    await expect(messages.last()).toHaveAttribute('data-index', '199');
    await expect(mockedPage.getByTestId('chat-load-earlier')).toHaveCount(0);

    // And back to the end, explicitly.
    await mockedPage.getByTestId('chat-jump-to-latest').click();
    await expect(messages.last()).toHaveAttribute('data-index', '249');
  });

  test('renders a tool call collapsed and expands it to raw JSON', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(chatUrl);

    const call = mockedPage.getByTestId('chat-part-1-0');
    await expect(call).toContainText('Called');
    await expect(call).toContainText('fetch_policy');
    // Collapsed means the payload is not in the document, not merely hidden:
    // a 200-message window of agent-controlled JSON is the hang the cap exists
    // to prevent.
    await expect(mockedPage.getByTestId('chat-part-1-0-json')).toHaveCount(0);
    await expect(call.getByTestId('chat-part-1-0-toggle')).toHaveAttribute(
      'aria-expanded',
      'false'
    );

    await call.getByTestId('chat-part-1-0-toggle').click();

    const json = mockedPage.getByTestId('chat-part-1-0-json');
    await expect(json).toBeVisible();
    const raw = (await json.textContent()) ?? '';
    expect(JSON.parse(raw)).toEqual({ topic: 'refunds', locale: 'en-GB' });

    // The result is its own block with its own toggle.
    const result = mockedPage.getByTestId('chat-part-1-1');
    await expect(result).toContainText('Returned');
    await result.getByTestId('chat-part-1-1-toggle').click();
    const resultRaw =
      (await mockedPage.getByTestId('chat-part-1-1-json').textContent()) ?? '';
    expect(JSON.parse(resultRaw)).toEqual({
      window_days: 30,
      source: 'policies/refunds.md',
    });

    await call.getByTestId('chat-part-1-0-toggle').click();
    await expect(mockedPage.getByTestId('chat-part-1-0-json')).toHaveCount(0);
  });

  test('switching chats shows that chat and switching back restores the first', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(chatUrl);
    await expect(
      mockedPage.getByText('What is our refund window?')
    ).toBeVisible();

    await mockedPage.getByTestId('chat-session-switcher').click();
    await mockedPage
      .getByRole('option', { name: 'Onboarding checklist' })
      .click();

    await expect(
      mockedPage.getByText('Draft the onboarding checklist.')
    ).toBeVisible();
    await expect(
      mockedPage.getByText('Step one: create the workspace.')
    ).toBeVisible();
    await expect(
      mockedPage.getByText('What is our refund window?')
    ).toHaveCount(0);

    await mockedPage.getByTestId('chat-session-switcher').click();
    await mockedPage.getByRole('option', { name: 'Refund policy' }).click();

    await expect(
      mockedPage.getByText('What is our refund window?')
    ).toBeVisible();
    await expect(
      mockedPage.getByText('Draft the onboarding checklist.')
    ).toHaveCount(0);
  });

  test('a turn started in one chat is not reported as running in another', async ({
    mockedPage,
  }) => {
    const turn = gate();
    await mockRoutes.agentSessions(mockedPage, { turnGate: turn.promise });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-composer-input').fill('Hold on');
    await mockedPage.getByTestId('chat-send').click();
    await expect(mockedPage.getByTestId('chat-stop-waiting')).toBeVisible();

    await mockedPage.getByTestId('chat-session-switcher').click();
    await mockedPage
      .getByRole('option', { name: 'Onboarding checklist' })
      .click();

    // The other conversation is idle and usable.
    await expect(mockedPage.getByTestId('chat-composer-input')).toBeEnabled();
    await expect(mockedPage.getByTestId('chat-turn-elapsed')).toHaveCount(0);
    await expect(mockedPage.getByTestId('chat-stop-waiting')).toHaveCount(0);

    turn.release();
  });

  test('shows a stranded invocation as a banner while still accepting messages', async ({
    mockedPage,
  }) => {
    // The lock cleared but the marker did not: the server gave up waiting and
    // the executor is still working.
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: {
        'sess-refunds': {
          in_flight_since: null,
          in_flight_trace_id: 'trace-stranded',
        },
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-executor-busy')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-composer-input')).toBeEnabled();
  });

  test('renders a failed transcript read as an inline banner', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      messagesError: {
        status: 502,
        errorCode: 'EXECUTOR_REJECTED',
        title: 'The transcript could not be read',
        detail: 'The executor rejected the read.',
      },
    });
    await mockedPage.goto(chatUrl);

    const banner = mockedPage
      .getByTestId('agent-chat-panel')
      .getByTestId('chat-transcript-error');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('The executor rejected the read.');
    await expect(
      mockedPage.locator('[class*="Notification-root"]')
    ).toHaveCount(0);
  });
});

test.describe('Agent chat panel: untrusted content', () => {
  const hostile: SessionMessage[] = [
    {
      index: 0,
      role: 'user',
      timestamp: '2024-03-02T09:00:00Z',
      parts: [
        {
          kind: 'text',
          text: "<script>window.__pwned = 'text-part'</script>",
        },
      ],
    },
    {
      index: 1,
      role: 'agent',
      author: chatAgentName,
      timestamp: '2024-03-02T09:00:01Z',
      parts: [
        {
          kind: 'text',
          text: '<img src=x onerror="window.__pwned = \'img-part\'"> and **not bold**',
        },
        {
          kind: 'tool_call',
          tool_name: 'fetch_page',
          tool_call_id: 'call-x',
          arguments: {
            url: "javascript:window.__pwned='tool-args'",
          },
        },
        {
          kind: 'tool_result',
          tool_name: 'fetch_page',
          tool_call_id: 'call-x',
          result: {
            body: "<script>window.__pwned = 'tool-result'</script>",
            image: '<img src=x onerror="window.__pwned = \'tool-img\'">',
          },
        },
      ],
    },
  ];

  test('renders a script tag and an onerror image as text and executes neither', async ({
    mockedPage,
  }) => {
    const errors: string[] = [];
    mockedPage.on('pageerror', (error) => errors.push(error.message));

    await mockRoutes.agentSessions(mockedPage, {
      sessions: [mockData.chatSessions[0]],
      transcripts: { 'sess-refunds': hostile },
    });
    await mockedPage.goto(chatUrl);

    const transcript = mockedPage.getByTestId('chat-transcript');
    await expect(transcript).toBeVisible();

    // The bytes are on screen, exactly as they arrived.
    await expect(
      mockedPage.getByText("<script>window.__pwned = 'text-part'</script>", {
        exact: true,
      })
    ).toBeVisible();
    await expect(
      mockedPage.getByText(
        '<img src=x onerror="window.__pwned = \'img-part\'"> and **not bold**',
        { exact: true }
      )
    ).toBeVisible();

    // Open both tool payloads so the raw JSON is in the document too.
    await mockedPage.getByTestId('chat-part-1-1-toggle').click();
    await mockedPage.getByTestId('chat-part-1-2-toggle').click();
    await expect(mockedPage.getByTestId('chat-part-1-2-json')).toContainText(
      "<script>window.__pwned = 'tool-result'</script>"
    );

    // Nothing became markup: no script, no image, no emphasis.
    await expect(transcript.locator('script')).toHaveCount(0);
    await expect(transcript.locator('img')).toHaveCount(0);
    await expect(transcript.locator('strong, b, em, i')).toHaveCount(0);
    await expect(transcript.locator('a')).toHaveCount(0);

    // And nothing ran.
    expect(await mockedPage.evaluate(() => (window as Pwned).__pwned)).toBe(
      undefined
    );
    expect(errors).toEqual([]);
  });

  test('renders hostile text as text after a turn as well as on load', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      turnReply: [
        { kind: 'text', text: "<script>window.__pwned = 'reply'</script>" },
      ],
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-composer-input').fill('echo this');
    await mockedPage.getByTestId('chat-send').click();

    await expect(
      mockedPage.getByText("<script>window.__pwned = 'reply'</script>", {
        exact: true,
      })
    ).toBeVisible();
    await expect(
      mockedPage.getByTestId('chat-transcript').locator('script')
    ).toHaveCount(0);
    expect(await mockedPage.evaluate(() => (window as Pwned).__pwned)).toBe(
      undefined
    );
  });
});

/**
 * The panel paints its own surfaces, so a missing dark rule is a white slab in
 * a dark console rather than a broken layout that a smoke test would catch.
 */
test.describe('Agent chat panel: colour schemes', () => {
  async function surfaceLuminance(
    locator: Locator
  ): Promise<{ background: number; text: number }> {
    return locator.evaluate((element: HTMLElement) => {
      const relative = (colour: string) => {
        const parts = colour.match(/[\d.]+/g)?.map(Number) ?? [0, 0, 0];
        return (
          (0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]) / 255
        );
      };
      const style = getComputedStyle(element);
      return {
        background: relative(style.backgroundColor),
        text: relative(style.color),
      };
    });
  }

  test.describe('light', () => {
    test.use({ colorScheme: 'light' });

    test('paints a light panel with dark text', async ({ mockedPage }) => {
      await mockedPage.goto(chatUrl);
      await expect(mockedPage.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        'light'
      );

      await mockedPage.getByTestId('chat-part-1-0-toggle').click();

      const panel = await surfaceLuminance(
        mockedPage.getByTestId('agent-chat-panel')
      );
      expect(panel.background).toBeGreaterThan(0.8);

      const tool = await surfaceLuminance(
        mockedPage.getByTestId('chat-part-1-0')
      );
      expect(tool.background).toBeGreaterThan(0.8);

      const text = await surfaceLuminance(
        mockedPage.getByTestId('chat-part-1-2')
      );
      expect(text.text).toBeLessThan(0.5);
    });
  });

  test.describe('dark', () => {
    test.use({ colorScheme: 'dark' });

    test('paints a dark panel with light text', async ({ mockedPage }) => {
      await mockedPage.goto(chatUrl);
      await expect(mockedPage.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        'dark'
      );

      await mockedPage.getByTestId('chat-part-1-0-toggle').click();

      const panel = await surfaceLuminance(
        mockedPage.getByTestId('agent-chat-panel')
      );
      expect(panel.background).toBeLessThan(0.3);

      // The tool block has its own surface, and its own dark rule to get wrong.
      const tool = await surfaceLuminance(
        mockedPage.getByTestId('chat-part-1-0')
      );
      expect(tool.background).toBeLessThan(0.3);

      const text = await surfaceLuminance(
        mockedPage.getByTestId('chat-part-1-2')
      );
      expect(text.text).toBeGreaterThan(0.5);

      // Readable either way.
      await expect(
        mockedPage.getByText('Refunds are accepted within 30 days of delivery.')
      ).toBeVisible();
    });
  });
});
