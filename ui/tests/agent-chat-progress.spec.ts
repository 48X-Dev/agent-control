import type { Locator, Page } from '@playwright/test';

import type { Plan, PlanStep, PlanStepStatus } from '@/core/api/types';
import { getAgentRoute } from '@/core/constants/agent-routes';

import { chatAgentName, expect, mockRoutes, test } from './fixtures';

const chatUrl = getAgentRoute('agent-1', { tab: 'chat' });
const SESSION = 'sess-refunds';

/**
 * The rail renders one agent's account of its own work.
 *
 * So the assertions in this file are mostly about what it refuses to say. The
 * failure being guarded against is not a broken layout; it is a panel that
 * turns "the agent marked two of five steps" into "40% complete", which is a
 * measurement nobody took, attached to a label that claims an author.
 */

function step(overrides: Partial<PlanStep> = {}): PlanStep {
  return {
    index: 0,
    title: 'Read the ticket',
    status: 'pending',
    note: null,
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

function plan(overrides: Partial<Plan> = {}): Plan {
  const now = new Date().toISOString();
  return {
    session_key: SESSION,
    revision: 1,
    revision_count: 1,
    steps: [
      step({ index: 0, title: 'Read the ticket' }),
      step({ index: 1, title: 'Check the refund policy' }),
      step({ index: 2, title: 'Draft the reply' }),
    ],
    declared_at: now,
    last_updated_at: now,
    ...overrides,
  };
}

function marked(statuses: PlanStepStatus[], at = new Date().toISOString()): Plan {
  return plan({
    steps: statuses.map((status, index) =>
      step({ index, title: `Step ${index}`, status, updated_at: at })
    ),
    last_updated_at: at,
  });
}

/** Everything the rail is currently rendering, as one string. */
async function railText(page: Page): Promise<string> {
  return (await page.getByTestId('chat-progress-rail').innerText()).trim();
}

/** A promise a test resolves when it wants the pending turn to answer. */
function gate(): { promise: Promise<void>; release: () => void } {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

test.describe('Agent chat: the progress rail', () => {
  test('an agent that never declared a plan gets the fallback, not an empty rail', async ({
    mockedPage,
  }) => {
    // The ordinary case. A rail with a heading and nothing under it reads as a
    // feature that broke; the honest answer is the handful of facts this
    // console can see for itself.
    await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    const fallback = mockedPage.getByTestId('chat-progress-fallback');
    await expect(fallback).toBeVisible();
    await expect(
      fallback.getByText(
        'This agent has not reported a plan, so there is nothing to show but what this console can see for itself.'
      )
    ).toBeVisible();
    // One user message in this transcript, and the window reaches the start of
    // the conversation, so the count is exact rather than a floor.
    await expect(fallback).toContainText('1 turn');
    await expect(fallback).not.toContainText('at least');
    await expect(mockedPage.getByTestId('chat-progress-step')).toHaveCount(0);
  });

  test('the fallback offers the trace as the evidence, when there is one', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      detailOverrides: {
        [SESSION]: { last_trace_id: 'trace-abc', in_flight_since: null },
      },
    });
    await mockedPage.goto(chatUrl);

    const link = mockedPage.getByTestId('chat-progress-trace-link');
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute('href', /trace-abc$/);
  });

  test('a declared plan is rendered as the agent worded it, under its own label', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, { plans: { [SESSION]: plan() } });
    await mockedPage.goto(chatUrl);

    // The label is exact. Softening it to "Progress" would drop the author.
    await expect(
      mockedPage.getByText('Plan reported by the agent')
    ).toBeVisible();
    const steps = mockedPage.getByTestId('chat-progress-step');
    await expect(steps).toHaveCount(3);
    await expect(steps.nth(0)).toContainText('Read the ticket');
    await expect(steps.nth(1)).toContainText('Check the refund policy');
    await expect(steps.nth(2)).toContainText('Draft the reply');
    await expect(mockedPage.getByTestId('chat-progress-fallback')).toHaveCount(
      0
    );
  });

  test('each status keeps its own name, and none is folded into done', async ({
    mockedPage,
  }) => {
    // A plan that read as finished when a third of it failed would be a
    // percentage arriving by a slower route.
    await mockRoutes.agentSessions(mockedPage, {
      plans: {
        [SESSION]: marked(['done', 'failed', 'skipped', 'active', 'pending']),
      },
    });
    await mockedPage.goto(chatUrl);

    const steps = mockedPage.getByTestId('chat-progress-step');
    await expect(steps).toHaveCount(5);
    for (const [index, status] of [
      'done',
      'failed',
      'skipped',
      'active',
      'pending',
    ].entries()) {
      await expect(steps.nth(index)).toHaveAttribute('data-step-status', status);
    }
    const tally = mockedPage.getByTestId('chat-progress-tally');
    await expect(tally).toContainText('1 done');
    await expect(tally).toContainText('1 failed');
    await expect(tally).toContainText('1 skipped');
    await expect(tally).toContainText('1 not marked');
  });

  test('the agent’s note on a step is shown beside it', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      plans: {
        [SESSION]: plan({
          steps: [
            step({ status: 'failed', note: 'the policy API refused twice' }),
          ],
        }),
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-progress-step')).toContainText(
      'the policy API refused twice'
    );
  });

  test('a replan says so, and shows the revision it is showing', async ({
    mockedPage,
  }) => {
    // The panel must never quietly swap the steps a person read a minute ago
    // for different ones. Saying "revised" is the whole point of keeping
    // revisions rather than editing in place.
    await mockRoutes.agentSessions(mockedPage, {
      plans: {
        [SESSION]: plan({
          revision: 3,
          revision_count: 3,
          steps: [step({ title: 'The new approach' })],
        }),
      },
    });
    await mockedPage.goto(chatUrl);

    const revisions = mockedPage.getByTestId('chat-progress-revisions');
    await expect(revisions).toHaveText(
      'Plan revised 2 times · showing revision 3'
    );
    await expect(mockedPage.getByTestId('chat-progress-step')).toContainText(
      'The new approach'
    );
  });

  test('an agent that replans mid-turn moves the rail onto the new plan', async ({
    mockedPage,
  }) => {
    // The rail is polled while a turn is live, because that is when the agent
    // is writing to it. The panel must follow the replan rather than keep
    // showing steps that were superseded a minute ago.
    const turn = gate();
    const chat = await mockRoutes.agentSessions(mockedPage, {
      turnGate: turn.promise,
      plans: { [SESSION]: plan({ steps: [step({ title: 'The first idea' })] }) },
    });
    await mockedPage.goto(chatUrl);
    await expect(mockedPage.getByTestId('chat-progress-step')).toContainText(
      'The first idea'
    );

    await mockedPage.getByTestId('chat-composer-input').fill('Try again');
    await mockedPage.getByTestId('chat-send').click();
    await expect(mockedPage.getByTestId('chat-turn-elapsed')).toBeVisible();

    // The agent replanned. Nothing the browser did caused this: plans are
    // written by the agent under its own session-bound credential.
    chat.plans[SESSION] = plan({
      revision: 2,
      revision_count: 2,
      steps: [step({ title: 'The second idea', status: 'active' })],
    });

    await expect(mockedPage.getByTestId('chat-progress-revisions')).toHaveText(
      'Plan revised 1 time · showing revision 2',
      { timeout: 15000 }
    );
    await expect(mockedPage.getByTestId('chat-progress-step')).toContainText(
      'The second idea'
    );
    turn.release();
  });

  test('the plan is re-read once the turn ends, so a final mark is not missed', async ({
    mockedPage,
  }) => {
    // Polling stops when the turn does. Without an explicit re-read at that
    // moment the rail would keep showing the state from the last poll, which is
    // almost always one mark short of the truth.
    const turn = gate();
    const chat = await mockRoutes.agentSessions(mockedPage, {
      turnGate: turn.promise,
      plans: { [SESSION]: marked(['active']) },
    });
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-composer-input').fill('Finish it');
    await mockedPage.getByTestId('chat-send').click();
    await expect(mockedPage.getByTestId('chat-turn-elapsed')).toBeVisible();

    chat.plans[SESSION] = marked(['done']);
    turn.release();

    await expect(mockedPage.getByTestId('chat-progress-step')).toHaveAttribute(
      'data-step-status',
      'done',
      { timeout: 15000 }
    );
  });

  test('a first plan says nothing about revisions', async ({ mockedPage }) => {
    await mockRoutes.agentSessions(mockedPage, { plans: { [SESSION]: plan() } });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-progress-step')).toHaveCount(3);
    await expect(mockedPage.getByTestId('chat-progress-revisions')).toHaveCount(
      0
    );
  });
});

test.describe('Agent chat: the number the rail must never render', () => {
  const cases: Array<[string, Plan]> = [
    ['nothing marked', marked(['pending', 'pending', 'pending', 'pending'])],
    ['some marked', marked(['done', 'done', 'pending', 'pending'])],
    ['half marked', marked(['done', 'done', 'failed', 'pending'])],
    ['all marked', marked(['done', 'done', 'done', 'done'])],
  ];

  for (const [name, declared] of cases) {
    test(`shows no percentage and no ratio with ${name}`, async ({
      mockedPage,
    }) => {
      // Checked over everything the rail renders rather than over one element,
      // because the failure mode is somebody adding a helpful summary line in
      // six months, not somebody editing the one this test named.
      await mockRoutes.agentSessions(mockedPage, {
        plans: { [SESSION]: declared },
      });
      await mockedPage.goto(chatUrl);
      await expect(mockedPage.getByTestId('chat-progress-rail')).toBeVisible();

      const text = await railText(mockedPage);

      expect(text).not.toMatch(/%/);
      expect(text).not.toMatch(/\bpercent/i);
      expect(text).not.toMatch(/\bcomplete\b/i);
      // "2 of 4" and "2/4" are percentages with the division left as an
      // exercise for the reader.
      expect(text).not.toMatch(/\b\d+\s*(of|\/)\s*\d+\b/i);
      expect(text).not.toMatch(/\bprogress\b/i);
    });
  }

  test('draws no progress bar, meter or gauge', async ({ mockedPage }) => {
    await mockRoutes.agentSessions(mockedPage, {
      plans: { [SESSION]: marked(['done', 'done', 'pending']) },
    });
    await mockedPage.goto(chatUrl);

    const rail = mockedPage.getByTestId('chat-progress-rail');
    await expect(rail).toBeVisible();
    await expect(rail.locator('progress')).toHaveCount(0);
    await expect(rail.locator('meter')).toHaveCount(0);
    await expect(rail.locator('[role="progressbar"]')).toHaveCount(0);
    await expect(rail.locator('[role="meter"]')).toHaveCount(0);
  });
});

test.describe('Agent chat: an abandoned plan', () => {
  test('reads as old by its last update, and never as stalled partway', async ({
    mockedPage,
  }) => {
    // Nothing decays and nothing completes itself. The one honest thing to say
    // about a plan nobody has touched for an hour is that nobody has touched
    // it for an hour.
    const anHourAgo = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    await mockRoutes.agentSessions(mockedPage, {
      plans: {
        [SESSION]: marked(['done', 'pending', 'pending', 'pending'], anHourAgo),
      },
    });
    await mockedPage.goto(chatUrl);

    const stale = mockedPage.getByTestId('chat-progress-stale');
    await expect(stale).toBeVisible();
    await expect(stale).toContainText('No step has been marked for');
    await expect(stale).toContainText(
      'nothing here says whether the work is still happening'
    );
    // Still one done and three unmarked, with no inference laid over them.
    await expect(mockedPage.getByTestId('chat-progress-tally')).toHaveText(
      '1 done · 3 not marked'
    );
    expect(await railText(mockedPage)).not.toMatch(/stalled|stuck|frozen/i);
  });

  test('a plan touched a moment ago is not called stale', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      plans: { [SESSION]: marked(['done', 'pending']) },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-progress-tally')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-progress-stale')).toHaveCount(0);
  });
});

test.describe('Agent chat: reading the plan, and failing to', () => {
  test('a read that failed is not reported as an agent that said nothing', async ({
    mockedPage,
  }) => {
    // These are different facts. Showing the fallback here would tell a person
    // the agent reported nothing when in truth nobody managed to ask.
    await mockRoutes.agentSessions(mockedPage);
    // Registered after the session mock so it wins: Playwright matches the most
    // recently added route first.
    await mockedPage.route('**/api/v1/agent-sessions/*/plan', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({
          error_code: 'INTERNAL_ERROR',
          detail: 'no',
          title: 'Internal Server Error',
        }),
      })
    );
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-progress-error')).toBeVisible({
      timeout: 20000,
    });
    await expect(mockedPage.getByTestId('chat-progress-error')).toContainText(
      'That says nothing about whether the agent is working.'
    );
    await expect(mockedPage.getByTestId('chat-progress-fallback')).toHaveCount(
      0
    );
  });

  test('the rail never invents a plan for a session that has none', async ({
    mockedPage,
  }) => {
    // Two chats, one with a plan and one without. Switching between them must
    // not carry the first one's steps into the second.
    await mockRoutes.agentSessions(mockedPage, {
      plans: { [SESSION]: plan() },
    });
    await mockedPage.goto(chatUrl);
    await expect(mockedPage.getByTestId('chat-progress-step')).toHaveCount(3);

    await mockedPage.getByTestId('chat-session-switcher').click();
    await mockedPage.getByRole('option', { name: /Onboarding checklist/ }).click();

    await expect(mockedPage.getByTestId('chat-progress-fallback')).toBeVisible();
    await expect(mockedPage.getByTestId('chat-progress-step')).toHaveCount(0);
  });
});

test.describe('Agent chat: plan text is text', () => {
  test('markup an agent put in a step title is shown, not rendered', async ({
    mockedPage,
  }) => {
    // Step titles are model-authored and a model will happily repeat whatever a
    // tool result handed it. This panel is plain text everywhere, and the rail
    // does not get an exception for looking tidier.
    const hostile =
      '<img src=x onerror="window.__pwned=1"> **not bold** <script>window.__pwned=1</script>';
    await mockRoutes.agentSessions(mockedPage, {
      plans: {
        [SESSION]: plan({
          steps: [step({ title: hostile, note: hostile, status: 'active' })],
        }),
      },
    });
    await mockedPage.goto(chatUrl);

    const rail = mockedPage.getByTestId('chat-progress-rail');
    await expect(rail).toContainText('**not bold**');
    await expect(rail).toContainText('<img src=x');
    await expect(rail.locator('img')).toHaveCount(0);
    await expect(rail.locator('script')).toHaveCount(0);
    await expect(rail.locator('strong')).toHaveCount(0);
    expect(
      await mockedPage.evaluate(
        () => (window as unknown as { __pwned?: number }).__pwned
      )
    ).toBe(undefined);
  });

  test('a step title mentioning the agent is still just a title', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage, {
      plans: {
        [SESSION]: plan({
          steps: [step({ title: `Ask ${chatAgentName} to confirm` })],
        }),
      },
    });
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-progress-step')).toContainText(
      `Ask ${chatAgentName} to confirm`
    );
  });
});

/**
 * The rail paints its own surface and its own border, so a missing dark rule is
 * a bright slab in a dark console rather than a broken layout a smoke test
 * would catch.
 */
test.describe('Agent chat: the rail in both colour schemes', () => {
  async function surfaceOf(
    locator: Locator
  ): Promise<{ background: number; text: number; border: string }> {
    return locator.evaluate((element: HTMLElement) => {
      const relative = (colour: string) => {
        const parts = colour.match(/[\d.]+/g)?.map(Number) ?? [0, 0, 0];
        return (0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]) / 255;
      };
      const style = getComputedStyle(element);
      return {
        background: relative(style.backgroundColor),
        text: relative(style.color),
        border: style.borderTopColor,
      };
    });
  }

  test.describe('light', () => {
    test.use({ colorScheme: 'light' });

    test('reads as dark text on a light rail', async ({ mockedPage }) => {
      await mockRoutes.agentSessions(mockedPage, {
        plans: { [SESSION]: marked(['done', 'pending']) },
      });
      await mockedPage.goto(chatUrl);
      await expect(mockedPage.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        'light'
      );
      await expect(mockedPage.getByTestId('chat-progress-rail')).toBeVisible();

      const surface = await surfaceOf(
        mockedPage.getByTestId('chat-progress-rail')
      );
      expect(surface.text).toBeLessThan(0.5);
      expect(surface.border).not.toBe('rgba(0, 0, 0, 0)');
      await expect(
        mockedPage.getByText('Plan reported by the agent')
      ).toBeVisible();
    });
  });

  test.describe('dark', () => {
    test.use({ colorScheme: 'dark' });

    test('reads as light text on a dark rail', async ({ mockedPage }) => {
      await mockRoutes.agentSessions(mockedPage, {
        plans: { [SESSION]: marked(['done', 'pending']) },
      });
      await mockedPage.goto(chatUrl);
      await expect(mockedPage.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        'dark'
      );
      await expect(mockedPage.getByTestId('chat-progress-rail')).toBeVisible();

      const surface = await surfaceOf(
        mockedPage.getByTestId('chat-progress-rail')
      );
      expect(surface.text).toBeGreaterThan(0.5);
      expect(surface.border).not.toBe('rgba(0, 0, 0, 0)');
      // The label has to survive the dark rule too; it is the whole point.
      await expect(
        mockedPage.getByText('Plan reported by the agent')
      ).toBeVisible();
    });
  });
});
