import type { Page } from '@playwright/test';

import { getAgentRoute } from '@/core/constants/agent-routes';
import { getTeamRoute } from '@/core/constants/team-routes';

import { mockDispatchRoutes } from './dispatch-fixtures';
import {
  expect,
  mockApiRoutesWithAuthRequired,
  mockData,
  mockRoutes,
  test,
} from './fixtures';

const SALES = 'sales-outreach';
const EMPTY_TEAM = 'marketing';
const CROWDED = mockData.crowdedTeam.slug;

const SALES_URL = getTeamRoute(SALES);

/** A held promise, for asserting a loading state before the response lands. */
function gate() {
  let release: () => void = () => {};
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { held, release: () => release() };
}

function agentRows(page: Page) {
  return page.getByTestId('team-agent-row');
}

test.describe('Team Detail', () => {
  // ==========================================================================
  // Header
  // ==========================================================================

  test('renders the team name, description, slug and member count', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(SALES_URL);

    const header = mockedPage.getByTestId('team-detail-header');
    await expect(header).toBeVisible();
    await expect(mockedPage.getByTestId('team-display-name')).toHaveText(
      'Sales & Outreach'
    );
    await expect(mockedPage.getByTestId('team-description')).toHaveText(
      'Owns pipeline, prospecting and follow-up.'
    );
    await expect(mockedPage.getByTestId('team-slug')).toHaveText(SALES);
    await expect(mockedPage.getByTestId('team-member-count')).toHaveText(
      '3 agents'
    );
  });

  test('a team with no description omits the line rather than printing null', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute(EMPTY_TEAM));

    await expect(mockedPage.getByTestId('team-display-name')).toHaveText(
      'Marketing'
    );
    await expect(mockedPage.getByTestId('team-description')).toHaveCount(0);
    await expect(mockedPage.getByText('null')).toHaveCount(0);
  });

  test('the back link returns to the overview', async ({ mockedPage }) => {
    await mockedPage.goto(SALES_URL);

    await expect(mockedPage.getByTestId('back-to-teams')).toHaveAttribute(
      'href',
      '/teams'
    );
    await mockedPage.getByTestId('back-to-teams').click();

    await expect(mockedPage).toHaveURL('/teams');
    await expect(mockedPage.getByTestId('team-card').first()).toBeVisible();
  });

  test('shows a loader while the team request is in flight', async ({
    page,
  }) => {
    const { held, release } = gate();
    await mockRoutes.config(page);
    await mockRoutes.teamMilestones(page);
    await page.route('**/api/v1/teams/*', async (route, request) => {
      if (new URL(request.url()).pathname.split('/').length > 5) {
        await route.fallback();
        return;
      }
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockData.teamDetails[SALES]),
      });
    });

    await page.goto(SALES_URL);

    await expect(page.getByText('Loading team...')).toBeVisible();
    await expect(page.getByTestId('team-detail-header')).toHaveCount(0);

    release();

    await expect(page.getByTestId('team-detail-header')).toBeVisible();
  });

  // ==========================================================================
  // Unknown slug
  // ==========================================================================

  test('an unknown slug renders not-found rather than crashing', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { detailStatus: 404 });
    await mockRoutes.teamMilestones(page, { status: 404 });

    const crashes: string[] = [];
    page.on('pageerror', (err) => crashes.push(err.message));

    await page.goto(getTeamRoute('no-such-team'));

    await expect(
      page.getByRole('heading', { name: 'Team not found' })
    ).toBeVisible();
    await expect(page.getByTestId('team-not-found')).toContainText(
      'no-such-team'
    );
    // The panels the page could not fill are simply absent
    await expect(page.getByTestId('team-detail-header')).toHaveCount(0);
    await expect(page.getByTestId('milestones-panel')).toHaveCount(0);
    expect(crashes).toEqual([]);
  });

  test('not-found offers a way back and keeps the chrome', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { detailStatus: 404 });
    await mockRoutes.teamMilestones(page, { status: 404 });

    await page.goto(getTeamRoute('no-such-team'));
    await expect(page.getByTestId('team-not-found')).toBeVisible();

    // The sidebar is still there, and the button leads home
    await expect(page.getByTitle('Teams')).toBeVisible();
    await expect(page.getByTestId('team-not-found-back')).toHaveAttribute(
      'href',
      '/teams'
    );

    await page.getByTestId('team-not-found-back').click();
    await expect(page).toHaveURL('/teams');
  });

  test('a 404 is not retried', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teamMilestones(page, { status: 404 });
    let attempts = 0;
    await page.route('**/api/v1/teams/*', async (route, request) => {
      if (new URL(request.url()).pathname.split('/').length > 5) {
        await route.fallback();
        return;
      }
      attempts += 1;
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({
          type: 'about:blank',
          title: 'Not Found',
          status: 404,
          detail: 'No such team',
          error_code: 'TEAM_NOT_FOUND',
          reason: 'Not found',
        }),
      });
    });

    await page.goto(getTeamRoute('no-such-team'));
    await expect(page.getByTestId('team-not-found')).toBeVisible();
    await page.waitForTimeout(1500);

    expect(attempts).toBe(1);
  });

  test('a 500 on the team read is an error alert, not not-found', async ({
    page,
  }) => {
    // The two states must stay distinguishable: one means the link is stale,
    // the other means the server is unwell.
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { detailStatus: 500 });
    await mockRoutes.teamMilestones(page);

    await page.goto(getTeamRoute('no-such-team'));

    await expect(page.getByTestId('team-detail-error')).toBeVisible();
    await expect(page.getByTestId('team-not-found')).toHaveCount(0);
  });

  // ==========================================================================
  // Agent list
  // ==========================================================================

  test('lists the agents that belong to the team', async ({ mockedPage }) => {
    await mockedPage.goto(SALES_URL);

    await expect(mockedPage.getByTestId('team-agents-list')).toBeVisible();
    await expect(agentRows(mockedPage)).toHaveCount(3);
    for (const name of mockData.teamMemberNames[SALES]) {
      await expect(
        mockedPage.getByTestId('team-agent-row').filter({ hasText: name })
      ).toBeVisible();
    }
  });

  test('only shows the requested team, not every agent', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute('engineering'));

    await expect(agentRows(mockedPage)).toHaveCount(2);
    // A member of a different team must not leak in
    await expect(
      mockedPage
        .getByTestId('team-agent-row')
        .filter({ hasText: 'lead-qualifier' })
    ).toHaveCount(0);
  });

  test('sends the slug as the team filter', async ({ mockedPage }) => {
    const requested: string[] = [];
    mockedPage.on('request', (req) => {
      const url = new URL(req.url());
      if (url.pathname === '/api/v1/agents') {
        requested.push(url.searchParams.get('team') ?? '');
      }
    });

    await mockedPage.goto(SALES_URL);
    await expect(agentRows(mockedPage)).toHaveCount(3);

    expect(requested).toContain(SALES);
  });

  test('each agent row links to that agent', async ({ mockedPage }) => {
    await mockedPage.goto(SALES_URL);
    await expect(agentRows(mockedPage)).toHaveCount(3);

    const row = mockedPage.locator(
      '[data-testid="team-agent-row"][data-agent-name="customer-support-bot"]'
    );
    await expect(row).toHaveAttribute(
      'href',
      getAgentRoute('customer-support-bot')
    );

    await row.click();
    await expect(mockedPage).toHaveURL(/\/agents\?id=customer-support-bot/);
  });

  test('a team with no agents explains itself instead of showing a blank panel', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute(EMPTY_TEAM));

    await expect(mockedPage.getByTestId('team-agents-empty')).toBeVisible();
    await expect(mockedPage.getByText('No agents in this team')).toBeVisible();
    await expect(agentRows(mockedPage)).toHaveCount(0);
    // The count chip only makes sense with rows behind it
    await expect(mockedPage.getByTestId('team-agents-count')).toHaveCount(0);
    // And the header still reports zero rather than going blank
    await expect(mockedPage.getByTestId('team-member-count')).toHaveText(
      'No agents'
    );
  });

  test('the empty agent panel names the exact call that fills it', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute(EMPTY_TEAM));

    await expect(mockedPage.getByTestId('team-agents-empty')).toContainText(
      `/api/v1/teams/${EMPTY_TEAM}/members/`
    );
  });

  test('shows skeletons while the agent list loads', async ({ page }) => {
    const { held, release } = gate();
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    await page.route('**/api/v1/agents?**', async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agents: mockData.teamAgents[SALES],
          pagination: {
            total: 3,
            limit: 20,
            has_more: false,
            next_cursor: null,
          },
        }),
      });
    });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('team-agents-loading')).toBeVisible();
    await expect(agentRows(page)).toHaveCount(0);

    release();

    await expect(agentRows(page)).toHaveCount(3);
    await expect(page.getByTestId('team-agents-loading')).toHaveCount(0);
  });

  test('a failed agent read shows a retry that recovers', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);

    let fail = true;
    await page.route('**/api/v1/agents?**', async (route) => {
      if (fail) {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ status: 500, detail: 'boom' }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          agents: mockData.teamAgents[SALES],
          pagination: {
            total: 3,
            limit: 20,
            has_more: false,
            next_cursor: null,
          },
        }),
      });
    });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('team-agents-error')).toBeVisible({
      timeout: 15000,
    });
    // The rest of the page survives a failed agent read
    await expect(page.getByTestId('team-detail-header')).toBeVisible();
    await expect(page.getByTestId('milestones-panel')).toBeVisible();

    fail = false;
    await page.getByTestId('team-agents-retry').click();

    await expect(agentRows(page)).toHaveCount(3);
    await expect(page.getByTestId('team-agents-error')).toHaveCount(0);
  });

  test('a large membership pages in without breaking the layout', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute(CROWDED));

    // 14 members against a page size of 20: one page, every row present
    await expect(agentRows(mockedPage)).toHaveCount(
      mockData.crowdedMemberNames.length
    );

    const panel = await mockedPage
      .getByTestId('team-agents-panel')
      .evaluate((el) => ({
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      }));
    expect(panel.scrollWidth).toBeLessThanOrEqual(panel.clientWidth + 1);

    const doc = await mockedPage.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth + 1);
  });

  // ==========================================================================
  // Milestones: the five states
  // ==========================================================================

  test('the ok state lists one row per milestone', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'ok' });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-list')).toBeVisible();
    await expect(page.getByTestId('milestone-row')).toHaveCount(
      mockData.milestones.length
    );
    await expect(page.getByText('Beta launch')).toBeVisible();
    await expect(page.getByTestId('linear-team-key-badge')).toHaveText('ENG');
    await expect(page.getByTestId('milestone-progress').first()).toHaveText(
      '25%'
    );
    // The other four states must not also be on screen
    for (const other of [
      'milestones-empty',
      'milestones-error',
      'milestones-not-linked',
      'milestones-not-configured',
    ]) {
      await expect(page.getByTestId(other)).toHaveCount(0);
    }
  });

  test('a milestone without a date or progress still renders', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'ok' });

    await page.goto(SALES_URL);

    const bare = page
      .getByTestId('milestone-row')
      .filter({ hasText: 'Untargeted follow-up work' });
    await expect(bare).toBeVisible();
    await expect(bare.getByText('No target date')).toBeVisible();
    await expect(bare.getByTestId('milestone-progress')).toHaveCount(0);
    await expect(bare.getByTestId('milestone-status')).toHaveCount(0);
  });

  test('a target date is not slid backwards by the local timezone', async ({
    browser,
  }) => {
    // A plain calendar date formatted in the viewer's zone reads as the
    // previous day anywhere west of UTC, so this is pinned to Honolulu rather
    // than left to whatever zone the runner happens to be in.
    const context = await browser.newContext({
      timezoneId: 'Pacific/Honolulu',
    });
    const page = await context.newPage();
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'ok' });

    await page.goto(SALES_URL);

    // 2026-09-01, not 2026-08-31
    const date = page.getByTestId('milestone-target-date').first();
    await expect(date).toContainText('Sep');
    await expect(date).toContainText('1');
    await expect(date).toContainText('2026');
    await expect(date).not.toContainText('Aug');

    await context.close();
  });

  test('the empty state says nothing is scheduled', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'empty' });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-empty')).toBeVisible();
    await expect(page.getByText('No milestones yet')).toBeVisible();
    await expect(page.getByTestId('milestone-row')).toHaveCount(0);
    await expect(page.getByTestId('milestones-error')).toHaveCount(0);
    // Still linked, so the badge stays
    await expect(page.getByTestId('linear-team-key-badge')).toHaveText('ENG');
  });

  test('the not-linked state offers the link form', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'not_linked' });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-not-linked')).toBeVisible();
    await expect(page.getByText('Not linked to Linear yet')).toBeVisible();
    await expect(page.getByTestId('link-linear-team-form')).toBeVisible();
    await expect(page.getByTestId('linear-team-key-badge')).toHaveCount(0);
    await expect(page.getByTestId('milestones-not-configured')).toHaveCount(0);
  });

  test('the not-configured state names the server setting, and offers no form', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'not_configured' });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-not-configured')).toBeVisible();
    await expect(page.getByText('Milestones are turned off')).toBeVisible();
    await expect(page.getByText('AGENT_CONTROL_LINEAR_API_KEY')).toBeVisible();
    // Linking is pointless without a server credential, so no form here
    await expect(page.getByTestId('link-linear-team-form')).toHaveCount(0);
    await expect(page.getByTestId('milestones-not-linked')).toHaveCount(0);
  });

  test('the error state reports Linear and offers a retry', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'error' });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-error')).toBeVisible();
    await expect(page.getByText('Could not reach Linear')).toBeVisible();
    await expect(page.getByTestId('milestones-error')).toContainText(
      'Linear did not answer in time.'
    );
    await expect(page.getByTestId('milestones-error')).toContainText('30s');
    await expect(page.getByTestId('milestones-retry')).toBeVisible();
    await expect(page.getByTestId('milestone-row')).toHaveCount(0);
  });

  test('the five states are mutually exclusive', async ({ page }) => {
    const states = [
      ['ok', 'milestones-list'],
      ['empty', 'milestones-empty'],
      ['not_linked', 'milestones-not-linked'],
      ['not_configured', 'milestones-not-configured'],
      ['error', 'milestones-error'],
    ] as const;
    const allTestIds = states.map(([, testId]) => testId);

    for (const [state, testId] of states) {
      await mockRoutes.config(page);
      await mockRoutes.teams(page);
      await mockRoutes.agents(page);
      await mockRoutes.teamMilestones(page, { state });

      await page.goto(SALES_URL);
      await expect(page.getByTestId(testId)).toBeVisible();

      for (const other of allTestIds.filter((id) => id !== testId)) {
        await expect(page.getByTestId(other)).toHaveCount(0);
      }
    }
  });

  test('retrying the error state recovers to a list', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);

    let failing = true;
    await page.route('**/api/v1/teams/*/milestones', async (route) => {
      const state = failing ? 'error' : 'ok';
      failing = false;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockData.milestoneStates[state](SALES)),
      });
    });

    await page.goto(SALES_URL);
    await expect(page.getByTestId('milestones-error')).toBeVisible();

    await page.getByTestId('milestones-retry').click();

    await expect(page.getByTestId('milestones-list')).toBeVisible();
    await expect(page.getByTestId('milestones-error')).toHaveCount(0);
  });

  // ==========================================================================
  // Linear cannot take the page down
  // ==========================================================================

  test('a Linear outage leaves the agent list rendering', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'error' });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-error')).toBeVisible();
    // The other two panels are untouched
    await expect(page.getByTestId('team-detail-header')).toBeVisible();
    await expect(agentRows(page)).toHaveCount(3);
  });

  test('a milestones request that fails outright still leaves the agents up', async ({
    page,
  }) => {
    // Not a Linear outage: Agent Control itself 500ing on that route.
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { status: 500 });

    const crashes: string[] = [];
    page.on('pageerror', (err) => crashes.push(err.message));

    await page.goto(SALES_URL);

    await expect(agentRows(page)).toHaveCount(3);
    await expect(page.getByTestId('team-detail-header')).toBeVisible();
    // And the panel says so without blaming Linear
    await expect(page.getByTestId('milestones-error')).toBeVisible({
      timeout: 15000,
    });
    await expect(page.getByText('Could not load milestones')).toBeVisible();
    await expect(page.getByText('Could not reach Linear')).toHaveCount(0);
    expect(crashes).toEqual([]);
  });

  test('a milestones response the page cannot parse does not blank the page', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await page.route('**/api/v1/teams/*/milestones', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: 'not json at all',
      });
    });

    await page.goto(SALES_URL);

    // Whatever the panel decides to show, the agent list is still there
    await expect(agentRows(page)).toHaveCount(3);
    await expect(page.getByTestId('team-detail-header')).toBeVisible();
  });

  // ==========================================================================
  // Linking a Linear team
  // ==========================================================================

  test('submitting the link form sends the key upper-cased', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'not_linked' });
    const submitted = await mockRoutes.teamPatch(page);

    await page.goto(SALES_URL);
    await expect(page.getByTestId('link-linear-team-form')).toBeVisible();

    await page.getByTestId('linear-team-key-input').fill('eng');
    await page.getByTestId('link-linear-team-submit').click();

    await expect.poll(() => submitted.length).toBe(1);
    expect(submitted[0]).toEqual({ linear_team_key: 'ENG' });
  });

  test('a malformed key is caught before a request is made', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'not_linked' });
    const submitted = await mockRoutes.teamPatch(page);

    await page.goto(SALES_URL);
    await page.getByTestId('linear-team-key-input').fill('not a key');
    await page.getByTestId('link-linear-team-submit').click();

    await expect(
      page.getByText('Use letters and digits only, with no spaces.')
    ).toBeVisible();
    expect(submitted).toEqual([]);
  });

  test('a 403 on the link tells the user an admin key is needed', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'not_linked' });
    await mockRoutes.teamPatch(page, { status: 403 });

    await page.goto(SALES_URL);
    await page.getByTestId('linear-team-key-input').fill('ENG');
    await page.getByTestId('link-linear-team-submit').click();

    await expect(page.getByTestId('link-linear-team-error')).toContainText(
      'admin API key'
    );
    // The form stays open so the user can retry
    await expect(page.getByTestId('link-linear-team-form')).toBeVisible();
  });

  test('a 422 on the link explains the key format', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'not_linked' });
    await mockRoutes.teamPatch(page, {
      status: 422,
      errorCode: 'VALIDATION_ERROR',
    });

    await page.goto(SALES_URL);
    await page.getByTestId('linear-team-key-input').fill('ENG');
    await page.getByTestId('link-linear-team-submit').click();

    await expect(page.getByTestId('link-linear-team-error')).toContainText(
      'Linear rejected that key'
    );
  });

  test('a linked team offers Change, prefilled with the current key', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'ok' });

    await page.goto(SALES_URL);
    await expect(page.getByTestId('milestones-list')).toBeVisible();

    await page.getByTestId('milestones-change-link').click();

    await expect(page.getByTestId('linear-team-key-input')).toHaveValue('ENG');
    await page.getByTestId('link-linear-team-cancel').click();
    await expect(page.getByTestId('milestones-list')).toBeVisible();
  });

  test('the not-configured state does not offer Change', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, {
      state: 'not_configured',
      body: { linear_team_key: 'ENG' },
    });

    await page.goto(SALES_URL);

    await expect(page.getByTestId('milestones-not-configured')).toBeVisible();
    await expect(page.getByTestId('milestones-change-link')).toHaveCount(0);
  });

  test('half-finished form state does not follow you to another team', async ({
    mockedPage,
  }) => {
    await mockRoutes.teamMilestones(mockedPage, { state: 'not_linked' });

    await mockedPage.goto(SALES_URL);
    await mockedPage.getByTestId('linear-team-key-input').fill('TYPO');

    await mockedPage.getByTestId('back-to-teams').click();
    await mockedPage.getByTestId('team-card').first().waitFor();
    await mockedPage.goto(getTeamRoute('engineering'));

    await expect(mockedPage.getByTestId('linear-team-key-input')).toHaveValue(
      ''
    );
  });

  // ==========================================================================
  // Nothing secret reaches the browser
  // ==========================================================================

  test('no API key appears in the DOM or the client bundle', async ({
    page,
  }) => {
    // The keys the local server is started with, plus the shapes a Linear
    // credential takes. None of these has any business in the browser.
    const forbidden = [
      'local-admin-key',
      'local-agent-key',
      'lin_api_',
      'AGENT_CONTROL_API_KEYS',
      'AGENT_CONTROL_ADMIN_API_KEYS',
    ];

    const scripts: string[] = [];
    page.on('response', async (response) => {
      const type = response.headers()['content-type'] ?? '';
      if (!type.includes('javascript')) return;
      scripts.push(await response.text().catch(() => ''));
    });

    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'ok' });

    await page.goto(SALES_URL);
    await expect(page.getByTestId('milestones-list')).toBeVisible();

    const html = await page.content();
    for (const needle of forbidden) {
      expect(html, `"${needle}" must not be in the DOM`).not.toContain(needle);
    }

    // The panel does mention the env var by name as setup guidance, so that
    // one is checked only against the served JavaScript's own values.
    expect(scripts.length).toBeGreaterThan(0);
    for (const source of scripts) {
      expect(source).not.toContain('local-admin-key');
      expect(source).not.toContain('local-agent-key');
      expect(source).not.toContain('lin_api_');
    }
  });

  test('an X-API-Key header is never sent from the page', async ({ page }) => {
    // The browser authenticates with the session cookie the login flow sets.
    // A key in a header would mean one had been handed to the client.
    const withKeyHeader: string[] = [];
    page.on('request', (req) => {
      const headers = req.headers();
      if (headers['x-api-key'] || headers['authorization']) {
        withKeyHeader.push(req.url());
      }
    });

    await mockRoutes.config(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page, { state: 'ok' });

    await page.goto(SALES_URL);
    await expect(page.getByTestId('milestones-list')).toBeVisible();
    await expect(agentRows(page)).toHaveCount(3);

    expect(withKeyHeader).toEqual([]);
  });

  // ==========================================================================
  // Colour schemes
  // ==========================================================================

  for (const colorScheme of ['light', 'dark'] as const) {
    test(`renders in the ${colorScheme} colour scheme`, async ({ page }) => {
      await page.emulateMedia({ colorScheme });
      await mockRoutes.config(page);
      await mockRoutes.teams(page);
      await mockRoutes.agents(page);
      await mockRoutes.teamMilestones(page, { state: 'ok' });

      await page.goto(SALES_URL);

      await expect(page.getByTestId('team-detail-header')).toBeVisible();
      await expect(page.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        colorScheme
      );
      await expect(agentRows(page)).toHaveCount(3);
      await expect(page.getByTestId('milestones-list')).toBeVisible();

      const styles = await page
        .getByTestId('team-agents-panel')
        .evaluate((el) => {
          const computed = getComputedStyle(el);
          return {
            background: computed.backgroundColor,
            accent: computed.getPropertyValue('--team-accent-solid').trim(),
          };
        });
      expect(styles.background).not.toBe('rgba(0, 0, 0, 0)');
      expect(styles.accent).toMatch(/^(#|rgb|var\(--mantine-color-)/);

      // The team name must actually be readable, not painted onto its own
      // background. This is the check the accent-token choice turns on.
      const contrast = await page
        .getByTestId('team-display-name')
        .evaluate((el) => {
          const luminance = (color: string) => {
            const [r, g, b] = (color.match(/[\d.]+/g) ?? ['0', '0', '0']).map(
              Number
            );
            const channel = (value: number) => {
              const c = value / 255;
              return c <= 0.03928
                ? c / 12.92
                : Math.pow((c + 0.055) / 1.055, 2.4);
            };
            return (
              0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
            );
          };

          let background = '';
          let node: HTMLElement | null = el as HTMLElement;
          while (node && !background) {
            const value = getComputedStyle(node).backgroundColor;
            if (
              value &&
              value !== 'rgba(0, 0, 0, 0)' &&
              value !== 'transparent'
            )
              background = value;
            node = node.parentElement;
          }

          const a = luminance(getComputedStyle(el).color);
          const b = luminance(background || 'rgb(255,255,255)');
          return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
        });
      expect(contrast).toBeGreaterThan(3);
    });
  }

  test('the light and dark panels differ', async ({ page }) => {
    const backgroundFor = async (scheme: 'light' | 'dark') => {
      await page.emulateMedia({ colorScheme: scheme });
      await mockRoutes.config(page);
      await mockRoutes.teams(page);
      await mockRoutes.agents(page);
      await mockRoutes.teamMilestones(page, { state: 'ok' });
      await page.goto(SALES_URL);
      await expect(page.getByTestId('team-agents-panel')).toBeVisible();
      return page
        .getByTestId('team-agents-panel')
        .evaluate((el) => getComputedStyle(el).backgroundColor);
    };

    const light = await backgroundFor('light');
    const dark = await backgroundFor('dark');

    expect(dark).not.toBe(light);
  });

  test('the not-found state renders in the dark colour scheme', async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { detailStatus: 404 });
    await mockRoutes.teamMilestones(page, { status: 404 });

    await page.goto(getTeamRoute('no-such-team'));

    await expect(page.getByTestId('team-not-found')).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute(
      'data-mantine-color-scheme',
      'dark'
    );
  });

  // ==========================================================================
  // Layout and keyboard
  // ==========================================================================

  test('the panels stack on a narrow viewport without horizontal scroll', async ({
    mockedPage,
  }) => {
    await mockedPage.setViewportSize({ width: 390, height: 844 });
    await mockedPage.goto(SALES_URL);

    await expect(mockedPage.getByTestId('team-agents-panel')).toBeVisible();
    await expect(mockedPage.getByTestId('milestones-panel')).toBeVisible();

    const agentsBox = await mockedPage
      .getByTestId('team-agents-panel')
      .boundingBox();
    const milestonesBox = await mockedPage
      .getByTestId('milestones-panel')
      .boundingBox();
    expect(milestonesBox!.y).toBeGreaterThan(
      agentsBox!.y + agentsBox!.height - 1
    );

    const doc = await mockedPage.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth + 1);
  });

  test('the panels stay stacked full width on a wide viewport', async ({
    mockedPage,
  }) => {
    await mockedPage.setViewportSize({ width: 1440, height: 900 });
    await mockedPage.goto(SALES_URL);

    await expect(mockedPage.getByTestId('team-agents-panel')).toBeVisible();
    await expect(mockedPage.getByTestId('milestones-panel')).toBeVisible();

    const agentsBox = await mockedPage
      .getByTestId('team-agents-panel')
      .boundingBox();
    const milestonesBox = await mockedPage
      .getByTestId('milestones-panel')
      .boundingBox();
    expect(milestonesBox!.y).toBeGreaterThan(
      agentsBox!.y + agentsBox!.height - 1
    );
    expect(Math.abs(milestonesBox!.x - agentsBox!.x)).toBeLessThanOrEqual(1);
  });

  test('an agent row is keyboard reachable and shows a focus ring', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(SALES_URL);
    await expect(agentRows(mockedPage)).toHaveCount(3);

    const row = mockedPage.locator(
      '[data-testid="team-agent-row"][data-agent-name="customer-support-bot"]'
    );
    const readOutline = () =>
      row.evaluate((el) => {
        const style = getComputedStyle(el);
        return { width: style.outlineWidth, style: style.outlineStyle };
      });

    const resting = await readOutline();
    expect(parseFloat(resting.width) === 0 || resting.style === 'none').toBe(
      true
    );

    let focused = false;
    for (let i = 0; i < 30 && !focused; i++) {
      await mockedPage.keyboard.press('Tab');
      focused = await mockedPage.evaluate(
        () =>
          (document.activeElement as HTMLElement | null)?.getAttribute(
            'data-agent-name'
          ) === 'customer-support-bot'
      );
    }
    expect(focused).toBe(true);

    const outline = await readOutline();
    expect(outline.style).toBe('solid');
    expect(parseFloat(outline.width)).toBeGreaterThan(0);

    await mockedPage.keyboard.press('Enter');
    await expect(mockedPage).toHaveURL(/\/agents\?id=customer-support-bot/);
  });

  test('the Teams nav item stays active on the detail page', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(SALES_URL);
    await expect(mockedPage.getByTestId('team-detail-header')).toBeVisible();

    await expect(mockedPage.getByTitle('Teams')).toHaveAttribute(
      'data-active',
      'true'
    );
    await expect(mockedPage.getByTitle('My agents')).not.toHaveAttribute(
      'data-active',
      'true'
    );
  });

  test('navigating between two teams swaps every panel', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(SALES_URL);
    await expect(agentRows(mockedPage)).toHaveCount(3);

    await mockedPage.goto(getTeamRoute('engineering'));

    await expect(mockedPage.getByTestId('team-display-name')).toHaveText(
      'Engineering'
    );
    await expect(agentRows(mockedPage)).toHaveCount(2);
    // No row from the previous team is left behind
    await expect(
      mockedPage
        .getByTestId('team-agent-row')
        .filter({ hasText: 'outreach-scheduler' })
    ).toHaveCount(0);
  });

  test('the slug from the URL is the one that is requested', async ({
    page,
  }) => {
    const requested: string[] = [];
    await mockRoutes.config(page);
    await mockRoutes.teamMilestones(page);
    await page.route('**/api/v1/teams/*', async (route, request) => {
      const pathname = new URL(request.url()).pathname;
      if (pathname.split('/').length > 5) {
        await route.fallback();
        return;
      }
      const slug = decodeURIComponent(pathname.split('/').pop() ?? '');
      requested.push(slug);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...mockData.teamDetails[SALES],
          slug,
          display_name: 'Odd Slug',
        }),
      });
    });

    await page.goto(getTeamRoute('team-with-a-very-long-slug-2024'));

    await expect(page.getByTestId('team-display-name')).toHaveText('Odd Slug');
    // Never the literal "[slug]" placeholder, and never a truncated value
    expect(requested).toContain('team-with-a-very-long-slug-2024');
  });

  // ==========================================================================
  // Default agent
  // ==========================================================================

  test('shows the agent a team dispatches under', async ({ mockedPage }) => {
    await mockedPage.goto(SALES_URL);

    await expect(mockedPage.getByTestId('team-default-agent-value')).toHaveText(
      'lead-qualifier'
    );
    await expect(mockedPage.getByTestId('team-default-agent')).toContainText(
      'names no agent'
    );
  });

  test('a team without one says so rather than showing nothing', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute('engineering'));

    await expect(mockedPage.getByTestId('team-default-agent-unset')).toHaveText(
      'Not set'
    );
    await expect(
      mockedPage.getByTestId('team-default-agent-value')
    ).toHaveCount(0);
  });

  test('the picker offers this team’s members and nothing else', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    await mockRoutes.teamPatch(page);

    await page.goto(getTeamRoute('engineering'));
    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();

    const options = page.getByRole('option');
    await expect(options).toHaveText([
      'No default agent',
      'code-review-assistant',
      'data-analysis-agent',
    ]);
    // A member of another team is not offered: the server refuses a non-member
    await expect(options.filter({ hasText: 'lead-qualifier' })).toHaveCount(0);
  });

  test('choosing an agent sends it as the default', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    const submitted = await mockRoutes.teamPatch(page);

    await page.goto(getTeamRoute('engineering'));
    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();
    await page.getByRole('option', { name: 'data-analysis-agent' }).click();
    await page.getByTestId('team-default-agent-submit').click();

    await expect.poll(() => submitted.length).toBe(1);
    expect(submitted[0]).toEqual({ default_agent_name: 'data-analysis-agent' });
    await expect(page.getByTestId('team-default-agent-form')).toHaveCount(0);
  });

  test('a team can be returned to having no default agent', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    const submitted = await mockRoutes.teamPatch(page);

    await page.goto(SALES_URL);
    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();
    await page.getByRole('option', { name: 'No default agent' }).click();
    await page.getByTestId('team-default-agent-submit').click();

    await expect.poll(() => submitted.length).toBe(1);
    // Explicitly null, not omitted: the server leaves omitted fields alone
    expect(submitted[0]).toEqual({ default_agent_name: null });
  });

  test('a 403 says an admin key is needed and keeps the form open', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    await mockRoutes.teamPatch(page, { status: 403 });

    await page.goto(getTeamRoute('engineering'));
    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();
    await page.getByRole('option', { name: 'code-review-assistant' }).click();
    await page.getByTestId('team-default-agent-submit').click();

    await expect(page.getByTestId('team-default-agent-error')).toContainText(
      'admin API key'
    );
    await expect(page.getByTestId('team-default-agent-form')).toBeVisible();
  });

  test('a 409 explains that the agent is not in the team', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page);
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    await mockRoutes.teamPatch(page, {
      status: 409,
      errorCode: 'AGENT_NOT_IN_TEAM',
    });

    await page.goto(getTeamRoute('engineering'));
    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();
    await page.getByRole('option', { name: 'code-review-assistant' }).click();
    await page.getByTestId('team-default-agent-submit').click();

    await expect(page.getByTestId('team-default-agent-error')).toContainText(
      'not in this team'
    );
  });

  test('a team with no agents cannot set one, and says why', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute(EMPTY_TEAM));

    const blocked = mockedPage.getByTestId('team-default-agent-blocked');
    await expect(blocked).toHaveAttribute(
      'data-reason',
      'Add an agent to this team first'
    );
    await expect(
      mockedPage.getByTestId('team-default-agent-change')
    ).toBeDisabled();
  });

  test('a non-admin session gets a reason, not a button that 403s', async ({
    mockedPage,
  }) => {
    // A session resumed from its cookie, which is the ordinary case: nothing
    // in this tab ever logged in, so the verdict can only come from the probe.
    await mockRoutes.adminProbe(mockedPage, { admin: false });

    await mockedPage.goto(SALES_URL);

    const blocked = mockedPage.getByTestId('team-default-agent-blocked');
    await expect(blocked).toHaveAttribute(
      'data-reason',
      'Requires an admin key'
    );
    await expect(
      mockedPage.getByTestId('team-default-agent-change')
    ).toBeDisabled();
    // The current value is still readable; only the write is withheld
    await expect(mockedPage.getByTestId('team-default-agent-value')).toHaveText(
      'lead-qualifier'
    );
  });

  test('a read-only key that logged in this tab is refused too', async ({
    page,
  }) => {
    await mockApiRoutesWithAuthRequired(page);
    await mockRoutes.adminProbe(page, { admin: false });
    // The dispatch reads this page issues are admin-gated on the real server.
    // Left unmocked they answer 401, and a 401 sends the session back to the
    // login modal, which is a different story than the one under test.
    await mockDispatchRoutes(page);
    await mockRoutes.login(page, { authenticated: true, is_admin: false });

    await page.goto(SALES_URL);
    await expect(
      page.getByText('Enter your API key to continue.')
    ).toBeVisible();
    await page.getByPlaceholder('Enter your API key').fill('read-only-key');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByTestId('team-detail-header')).toBeVisible();

    await expect(
      page.getByTestId('team-default-agent-blocked')
    ).toHaveAttribute('data-reason', 'Requires an admin key');
  });

  test('a probe that fails for any other reason leaves the control usable', async ({
    mockedPage,
  }) => {
    // A 500 is a server fault, not a statement about this credential. Guessing
    // "not an admin" from it would hide the control from an admin.
    await mockedPage.route('**/api/v1/agent-models', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({
          type: 'about:blank',
          title: 'Error',
          status: 500,
          detail: 'boom',
          error_code: 'INTERNAL_ERROR',
          reason: 'Server error',
        }),
      });
    });

    await mockedPage.goto(SALES_URL);

    await expect(
      mockedPage.getByTestId('team-default-agent-change')
    ).toBeEnabled();
    await expect(
      mockedPage.getByTestId('team-default-agent-blocked')
    ).toHaveCount(0);
  });

  test('the shown default updates after a save, without a reload', async ({
    page,
  }) => {
    // A private copy: the PATCH mock writes into it, so the refetch that
    // follows the save returns the new value rather than the seeded one.
    const details = {
      engineering: structuredClone(mockData.teamDetails.engineering),
    };

    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page, { details });
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    await mockRoutes.teamPatch(page, { details });

    await page.goto(getTeamRoute('engineering'));
    await expect(page.getByTestId('team-default-agent-unset')).toHaveText(
      'Not set'
    );

    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();
    await page.getByRole('option', { name: 'data-analysis-agent' }).click();
    await page.getByTestId('team-default-agent-submit').click();

    await expect(page.getByTestId('team-default-agent-value')).toHaveText(
      'data-analysis-agent'
    );
    await expect(page.getByTestId('team-default-agent-unset')).toHaveCount(0);
  });

  test('a default naming a non-member is shown as such, and can be cleared', async ({
    page,
  }) => {
    // Reachable for rows written before the server enforced membership, or
    // edited outside the API. The picker must represent the value it holds
    // instead of rendering blank, and clearing must stay available.
    const details = {
      engineering: {
        ...structuredClone(mockData.teamDetails.engineering),
        default_agent_name: 'retired-agent',
      },
    };

    await mockRoutes.config(page);
    await mockRoutes.adminProbe(page);
    await mockRoutes.teams(page, { details });
    await mockRoutes.agents(page);
    await mockRoutes.teamMilestones(page);
    const submitted = await mockRoutes.teamPatch(page, { details });

    await page.goto(getTeamRoute('engineering'));
    await expect(page.getByTestId('team-default-agent-value')).toHaveText(
      'retired-agent'
    );

    await page.getByTestId('team-default-agent-change').click();
    await page.getByTestId('team-default-agent-select').click();
    await expect(page.getByRole('option')).toHaveText([
      'No default agent',
      'retired-agent (not in this team)',
      'code-review-assistant',
      'data-analysis-agent',
    ]);

    await page.getByRole('option', { name: 'No default agent' }).click();
    await page.getByTestId('team-default-agent-submit').click();

    await expect.poll(() => submitted.length).toBe(1);
    expect(submitted[0]).toEqual({ default_agent_name: null });
    await expect(page.getByTestId('team-default-agent-unset')).toHaveText(
      'Not set'
    );
  });
});
