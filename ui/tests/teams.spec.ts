import type { Page } from '@playwright/test';

import type { GetTeamResponse, ListTeamsResponse } from '@/core/api/types';
import { getTeamRoute } from '@/core/constants/team-routes';

import {
  crowdedTeamName,
  expect,
  mockData,
  mockRoutes,
  test,
} from './fixtures';

const TEAMS_URL = '/teams';

/** The card for a given slug. */
function card(page: Page, slug: string) {
  return page.locator(`[data-testid="team-card"][data-team-slug="${slug}"]`);
}

function allCards(page: Page) {
  return page.getByTestId('team-card');
}

/** Element box measurements used by the overflow assertions. */
async function boxOf(page: Page, selector: string) {
  return page
    .locator(selector)
    .first()
    .evaluate((el) => ({
      scrollWidth: el.scrollWidth,
      clientWidth: el.clientWidth,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
    }));
}

test.describe('Teams Overview', () => {
  test('displays the page header', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);

    await expect(
      mockedPage.getByRole('heading', { name: 'Teams', exact: true })
    ).toBeVisible();
    await expect(
      mockedPage.getByText(
        'Every department in your namespace. Pick one to see the agents that belong to it.'
      )
    ).toBeVisible();
  });

  test('renders one card per team with name, slug and member count', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(TEAMS_URL);

    await expect(allCards(mockedPage)).toHaveCount(mockData.teams.teams.length);

    for (const team of mockData.teams.teams) {
      const teamCard = card(mockedPage, team.slug);
      await expect(teamCard).toBeVisible();
      await expect(
        teamCard.getByText(team.display_name, { exact: true })
      ).toBeVisible();
      await expect(
        teamCard.getByText(team.slug, { exact: true })
      ).toBeVisible();
    }

    await expect(
      card(mockedPage, 'sales-outreach').getByTestId('team-member-count')
    ).toHaveText('3 agents');
    await expect(
      card(mockedPage, 'engineering').getByTestId('team-member-count')
    ).toHaveText('2 agents');
  });

  test('fills in agent names from the per-team request', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(TEAMS_URL);

    const salesCard = card(mockedPage, 'sales-outreach');
    for (const name of mockData.teamMemberNames['sales-outreach']) {
      await expect(salesCard.getByText(name, { exact: true })).toBeVisible();
    }
  });

  test('a single-member team reads "1 agent", not "1 agents"', async ({
    page,
  }) => {
    const soloTeam = { ...mockData.teams.teams[1], member_count: 1 };
    const list: ListTeamsResponse = {
      ...mockData.teams,
      teams: [soloTeam],
      pagination: { ...mockData.teams.pagination, total: 1 },
    };
    const details: Record<string, GetTeamResponse> = {
      [soloTeam.slug]: {
        ...soloTeam,
        members: [
          {
            agent_name: 'code-review-assistant',
            joined_at: '2024-01-05T00:00:00Z',
          },
        ],
      },
    };

    await mockRoutes.config(page);
    await mockRoutes.teams(page, { list: { data: list }, details });
    await page.goto(TEAMS_URL);

    await expect(
      card(page, soloTeam.slug).getByTestId('team-member-count')
    ).toHaveText('1 agent');
  });

  // ==========================================================================
  // Empty, loading and error states
  // ==========================================================================

  test('shows the empty state when there are no teams', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { list: { data: mockData.emptyTeams } });

    await page.goto(TEAMS_URL);

    // The page must explain itself rather than render an empty grid
    await expect(
      page.getByRole('heading', { name: 'No teams yet' })
    ).toBeVisible();
    await expect(
      page.getByText(
        'Teams group your agents into departments. Create one with an admin API key, then add agents to it. They show up here as a map of your organization.'
      )
    ).toBeVisible();
    await expect(page.getByText('View docs')).toBeVisible();
    await expect(allCards(page)).toHaveCount(0);
    await expect(page.getByTestId('teams-grid')).toHaveCount(0);
  });

  test('empty state keeps the page header and shows the create snippet', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { list: { data: mockData.emptyTeams } });

    await page.goto(TEAMS_URL);

    await expect(
      page.getByRole('heading', { name: 'Teams', exact: true })
    ).toBeVisible();
    await expect(page.getByText('/api/v1/teams').first()).toBeVisible();
  });

  test('shows skeleton cards while the list loads', async ({ page }) => {
    await mockRoutes.config(page);
    // Hold the response open until the skeletons have been asserted, so the
    // test never races a fast reply
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route('**/api/v1/teams?**', async (route) => {
      await held;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockData.teams),
      });
    });

    await page.goto(TEAMS_URL);

    await expect(page.getByTestId('teams-loading')).toBeVisible();
    await expect(allCards(page)).toHaveCount(0);

    release();

    await expect(page.getByTestId('teams-grid')).toBeVisible();
    await expect(page.getByTestId('teams-loading')).toHaveCount(0);
  });

  test('shows an error alert when the list request fails', async ({ page }) => {
    await mockRoutes.config(page);
    await page.route('**/api/v1/teams?**', async (route) => {
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'Internal server error' }),
      });
    });

    await page.goto(TEAMS_URL);

    await expect(page.getByText('Error loading teams')).toBeVisible();
    await expect(
      page.getByText('Failed to fetch teams. Please try again later.')
    ).toBeVisible();
  });

  test('a failed member read drops the pills instead of pinning a skeleton', async ({
    page,
  }) => {
    // Given: the list succeeds but every per-team read 500s
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { details: {} });

    await page.goto(TEAMS_URL);

    const salesCard = card(page, 'sales-outreach');
    // The count from the list still tells the true story
    await expect(salesCard.getByTestId('team-member-count')).toHaveText(
      '3 agents'
    );
    // And the card settles: no skeleton is left spinning forever
    await expect(salesCard.locator('.mantine-Skeleton-root')).toHaveCount(0, {
      timeout: 15000,
    });
  });

  // ==========================================================================
  // Layout edge cases
  // ==========================================================================

  test('a team with zero members renders without breaking the card', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(TEAMS_URL);

    const emptyCard = card(mockedPage, 'marketing');
    await expect(emptyCard).toBeVisible();
    await expect(emptyCard.getByTestId('team-member-count')).toHaveText(
      'No agents'
    );
    await expect(emptyCard.getByText('No agents assigned yet')).toBeVisible();

    // It sits in the grid like any other card: same width, non-trivial height
    const emptyBox = await emptyCard.boundingBox();
    const populatedBox = await card(mockedPage, 'engineering').boundingBox();
    expect(emptyBox).not.toBeNull();
    expect(populatedBox).not.toBeNull();
    expect(emptyBox!.width).toBeCloseTo(populatedBox!.width, 0);
    expect(emptyBox!.height).toBeGreaterThan(80);

    // The card is still a link, not a dead tile
    await expect(emptyCard).toHaveAttribute('href', getTeamRoute('marketing'));
  });

  test('a zero-member team makes no per-team request', async ({ page }) => {
    await mockRoutes.config(page);
    const requested: string[] = [];
    await page.route('**/api/v1/teams/*', async (route, request) => {
      const slug = new URL(request.url()).pathname.split('/').pop()!;
      requested.push(slug);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockData.teamDetails[slug]),
      });
    });
    await page.route('**/api/v1/teams?**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockData.teams),
      });
    });

    await page.goto(TEAMS_URL);
    await expect(
      card(page, 'sales-outreach').getByText('lead-qualifier')
    ).toBeVisible();

    expect(requested.sort()).toEqual(['engineering', 'sales-outreach']);
  });

  test('a long name and a large membership stay inside the card', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { list: { data: mockData.crowdedTeams } });

    await page.goto(TEAMS_URL);

    const slug = mockData.crowdedTeam.slug;
    const crowded = card(page, slug);
    await expect(crowded).toBeVisible();
    await expect(crowded.getByTestId('team-member-count')).toHaveText(
      `${mockData.crowdedMemberNames.length} agents`
    );

    // Only the preview plus an overflow pill, never all 14 names
    await expect(crowded.getByText('+10 more')).toBeVisible();

    // Nothing spills out of the card box
    const cardBox = await boxOf(
      page,
      `[data-testid="team-card"][data-team-slug="${slug}"]`
    );
    expect(cardBox.scrollWidth).toBeLessThanOrEqual(cardBox.clientWidth + 1);
    expect(cardBox.scrollHeight).toBeLessThanOrEqual(cardBox.clientHeight + 1);

    // And the page itself does not gain a horizontal scrollbar
    const doc = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth + 1);

    // The long title is clamped to the same single line a short name occupies
    const longTitleHeight = await crowded
      .getByText(crowdedTeamName)
      .evaluate((el) => el.clientHeight);
    const shortTitleHeight = await card(page, 'marketing')
      .getByText('Marketing', { exact: true })
      .evaluate((el) => el.clientHeight);
    expect(longTitleHeight).toBe(shortTitleHeight);

    // Cards in the same row keep equal widths despite the long content
    const crowdedBox = await crowded.boundingBox();
    const neighbourBox = await card(page, 'sales-outreach').boundingBox();
    expect(crowdedBox!.width).toBeCloseTo(neighbourBox!.width, 0);
  });

  test('the grid collapses to one column on a narrow viewport', async ({
    page,
  }) => {
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { list: { data: mockData.crowdedTeams } });
    await page.setViewportSize({ width: 390, height: 844 });

    await page.goto(TEAMS_URL);

    const crowded = card(page, mockData.crowdedTeam.slug);
    await expect(crowded).toBeVisible();

    const first = await crowded.boundingBox();
    const second = await card(page, 'sales-outreach').boundingBox();
    // Stacked, not side by side
    expect(second!.y).toBeGreaterThan(first!.y + first!.height - 1);

    const doc = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth + 1);
  });

  // ==========================================================================
  // Navigation
  // ==========================================================================

  test('the Teams nav item is active on /teams', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);

    const teamsNav = mockedPage.getByRole('link', { name: 'Teams' });
    await expect(teamsNav).toHaveAttribute('data-active', 'true');
    await expect(
      mockedPage.getByRole('link', { name: 'My agents' })
    ).not.toHaveAttribute('data-active', 'true');
  });

  test('a team route serves the detail page', async ({ mockedPage }) => {
    const response = await mockedPage.goto(getTeamRoute('sales-outreach'));

    expect(response?.status()).toBe(200);
    await expect(mockedPage.getByTestId('team-detail-header')).toBeVisible();
  });

  test('the Teams nav item stays active on a team route', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(getTeamRoute('sales-outreach'));

    // The detail page carries its own "Teams" back link, so the sidebar item
    // is addressed by the title only NavItem sets.
    await expect(mockedPage.getByTitle('Teams')).toHaveAttribute(
      'data-active',
      'true'
    );
  });

  test('the Teams nav item is not active on the agents overview', async ({
    mockedPage,
  }) => {
    await mockedPage.goto('/');

    const teamsNav = mockedPage.getByRole('link', { name: 'Teams' });
    await expect(teamsNav).toBeVisible();
    await expect(teamsNav).not.toHaveAttribute('data-active', 'true');
    await expect(
      mockedPage.getByRole('link', { name: 'My agents' })
    ).toHaveAttribute('data-active', 'true');
  });

  test('the sidebar navigates from agents to teams', async ({ mockedPage }) => {
    await mockedPage.goto('/');

    await mockedPage.getByRole('link', { name: 'Teams' }).click();

    await expect(mockedPage).toHaveURL(TEAMS_URL);
    await expect(allCards(mockedPage)).toHaveCount(mockData.teams.teams.length);
  });

  test('each card links to its team route', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);

    for (const team of mockData.teams.teams) {
      await expect(card(mockedPage, team.slug)).toHaveAttribute(
        'href',
        getTeamRoute(team.slug)
      );
    }
  });

  test('clicking a card navigates to that team', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);

    await card(mockedPage, 'engineering').click();

    await expect(mockedPage).toHaveURL(getTeamRoute('engineering'));
  });

  // ==========================================================================
  // Keyboard and accessibility
  // ==========================================================================

  test('cards are reachable by keyboard and expose a descriptive label', async ({
    mockedPage,
  }) => {
    await mockedPage.goto(TEAMS_URL);
    await expect(allCards(mockedPage).first()).toBeVisible();

    const firstCard = card(mockedPage, 'sales-outreach');
    await expect(firstCard).toHaveAttribute(
      'aria-label',
      'Sales & Outreach team, 3 agents'
    );
    await expect(card(mockedPage, 'marketing')).toHaveAttribute(
      'aria-label',
      'Marketing team, No agents'
    );

    // Tab from the top of the document until focus lands on a card, so this
    // fails if the cards ever stop being focusable
    let focusedSlug: string | null = null;
    for (let i = 0; i < 25 && focusedSlug === null; i++) {
      await mockedPage.keyboard.press('Tab');
      focusedSlug = await mockedPage.evaluate(
        () =>
          (document.activeElement as HTMLElement | null)?.getAttribute(
            'data-team-slug'
          ) ?? null
      );
    }
    expect(focusedSlug).toBe('sales-outreach');
    await expect(firstCard).toBeFocused();
  });

  test('a focused card activates with Enter', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);
    await expect(allCards(mockedPage).first()).toBeVisible();

    await card(mockedPage, 'engineering').focus();
    await expect(card(mockedPage, 'engineering')).toBeFocused();

    await mockedPage.keyboard.press('Enter');

    await expect(mockedPage).toHaveURL(getTeamRoute('engineering'));
  });

  test('keyboard focus reaches every card in order', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);
    await expect(allCards(mockedPage)).toHaveCount(3);

    await card(mockedPage, 'sales-outreach').focus();
    const seen: string[] = ['sales-outreach'];
    for (let i = 0; i < 2; i++) {
      await mockedPage.keyboard.press('Tab');
      const slug = await mockedPage.evaluate(
        () =>
          (document.activeElement as HTMLElement | null)?.getAttribute(
            'data-team-slug'
          ) ?? null
      );
      if (slug) seen.push(slug);
    }

    expect(seen).toEqual(['sales-outreach', 'engineering', 'marketing']);
  });

  test('a focused card shows a visible focus ring', async ({ mockedPage }) => {
    await mockedPage.goto(TEAMS_URL);
    await expect(allCards(mockedPage).first()).toBeVisible();

    const readOutline = () =>
      card(mockedPage, 'sales-outreach').evaluate((el) => {
        const style = getComputedStyle(el);
        return { width: style.outlineWidth, style: style.outlineStyle };
      });

    // Baseline: no ring before the card is focused, so the check below is not
    // satisfied by some always-on outline
    const resting = await readOutline();
    expect(parseFloat(resting.width) === 0 || resting.style === 'none').toBe(
      true
    );

    // Reach the card by keyboard so :focus-visible applies
    let focused = false;
    for (let i = 0; i < 25 && !focused; i++) {
      await mockedPage.keyboard.press('Tab');
      focused = await mockedPage.evaluate(
        () =>
          (document.activeElement as HTMLElement | null)?.getAttribute(
            'data-team-slug'
          ) === 'sales-outreach'
      );
    }
    expect(focused).toBe(true);

    const outline = await readOutline();
    expect(outline.style).toBe('solid');
    expect(parseFloat(outline.width)).toBeGreaterThan(0);
  });

  // ==========================================================================
  // Colour schemes
  // ==========================================================================

  for (const colorScheme of ['light', 'dark'] as const) {
    test(`renders in the ${colorScheme} colour scheme`, async ({
      mockedPage,
    }) => {
      await mockedPage.emulateMedia({ colorScheme });
      await mockedPage.goto(TEAMS_URL);

      await expect(allCards(mockedPage)).toHaveCount(3);
      await expect(mockedPage.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        colorScheme
      );

      const salesCard = card(mockedPage, 'sales-outreach');
      await expect(salesCard.getByText('Sales & Outreach')).toBeVisible();

      const styles = await salesCard.evaluate((el) => {
        const computed = getComputedStyle(el);
        const title = el.querySelector('p, span, div');
        return {
          background: computed.backgroundColor,
          accent: computed.getPropertyValue('--team-accent-solid').trim(),
          titleColor: title ? getComputedStyle(title).color : '',
        };
      });

      // The card paints a real background rather than falling through
      expect(styles.background).not.toBe('rgba(0, 0, 0, 0)');
      expect(styles.background).not.toBe('transparent');
      // And the accent variable resolves to a colour in both schemes
      expect(styles.accent).toMatch(/^(#|rgb|var\(--mantine-color-)/);

      const cardBox = await boxOf(
        mockedPage,
        '[data-testid="team-card"][data-team-slug="sales-outreach"]'
      );
      expect(cardBox.scrollWidth).toBeLessThanOrEqual(cardBox.clientWidth + 1);
    });
  }

  test('the light and dark cards differ in background', async ({
    mockedPage,
  }) => {
    const backgroundFor = async (scheme: 'light' | 'dark') => {
      await mockedPage.emulateMedia({ colorScheme: scheme });
      await mockedPage.goto(TEAMS_URL);
      await expect(card(mockedPage, 'sales-outreach')).toBeVisible();
      return card(mockedPage, 'sales-outreach').evaluate(
        (el) => getComputedStyle(el).backgroundColor
      );
    };

    const light = await backgroundFor('light');
    const dark = await backgroundFor('dark');

    // Proves the dark-scheme rules actually apply, not just that the page renders
    expect(dark).not.toBe(light);
  });

  test('the empty state renders in the dark colour scheme', async ({
    page,
  }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await mockRoutes.config(page);
    await mockRoutes.teams(page, { list: { data: mockData.emptyTeams } });

    await page.goto(TEAMS_URL);

    await expect(
      page.getByRole('heading', { name: 'No teams yet' })
    ).toBeVisible();
    await expect(page.locator('html')).toHaveAttribute(
      'data-mantine-color-scheme',
      'dark'
    );
  });
});
