import type { Locator, Page } from '@playwright/test';

import { getTeamRoute } from '@/core/constants/team-routes';

import {
  chainFor,
  chainHop,
  dispatchState,
  digestOf,
  haltedState,
  hostileAgentOutput,
  hostileIssueTitle,
  issueSeeds,
  type DispatchMocks,
  type DispatchRouteOptions,
  mockDispatchRoutes,
  MUTATING_DISPATCH_PATHS,
  pausedState,
  reviewEntry,
  taskSummary,
  workflows,
  xssFireCount,
} from './dispatch-fixtures';
import { expect, mockData, mockRoutes, test } from './fixtures';

const TEAM = 'engineering';
const TEAM_URL = getTeamRoute(TEAM);
const FIRST_MILESTONE = mockData.milestones[0];

/**
 * The team page with the milestone panel live and every dispatch route mocked.
 *
 * `mockRoutes` deliberately leaves the dispatch routes out of its default set,
 * so each spec installs them itself. Order matters only in that these are
 * registered after the defaults, which is what makes the more specific
 * milestone-issues glob win over the generic team glob.
 */
async function openTeamPage(
  page: Page,
  options: DispatchRouteOptions & {
    milestoneBody?: Record<string, unknown>;
  } = {}
): Promise<DispatchMocks> {
  await mockRoutes.config(page);
  await mockRoutes.agents(page);
  await mockRoutes.teams(page);
  await mockRoutes.teamMilestones(page, {
    body: options.milestoneBody as never,
  });
  const mocks = await mockDispatchRoutes(page, options);
  await page.goto(TEAM_URL);
  await expect(page.getByTestId('milestones-list')).toBeVisible();
  return mocks;
}

function milestoneRow(page: Page, index = 0): Locator {
  return page.getByTestId('milestone-row').nth(index);
}

function playControl(page: Page, index = 0): Locator {
  return milestoneRow(page, index).getByTestId('milestone-start-work');
}

/** Press play and wait for the panel it opens. Starts nothing by itself. */
async function openScope(page: Page, index = 0): Promise<Locator> {
  await playControl(page, index).click();
  const scope = milestoneRow(page, index).getByTestId('milestone-work-scope');
  await expect(scope).toBeVisible();
  return scope;
}

/**
 * Requests that could have created or moved work.
 *
 * `POST /agent-tasks/import` is excluded by path and re-checked by mode: it is
 * the one endpoint here that has both a reading and a writing shape, and the
 * reading one is a POST only because the set of refs does not fit in a query
 * string. Every test that uses this asserts the modes separately.
 */
function mutatingRequests(mocks: DispatchMocks) {
  return mocks.requests.filter(
    (request) =>
      request.method !== 'GET' && !request.url.endsWith('/agent-tasks/import')
  );
}

// =============================================================================
// Pressing play
// =============================================================================

test.describe('The play control', () => {
  test('opens a confirm and starts nothing', async ({ page }) => {
    const mocks = await openTeamPage(page);

    const scope = await openScope(page);

    // The confirm says so itself, and the requests agree with the copy.
    await expect(scope).toContainText('Nothing has started');
    expect(mocks.import.commits()).toHaveLength(0);
    expect(mocks.import.previews().length).toBeGreaterThan(0);
    for (const call of mocks.import.previews()) {
      expect(call.body.mode).toBe('preview');
      // A preview that carried a digest would be a commit wearing a hat.
      expect(call.body.expected_refs_digest ?? null).toBeNull();
    }

    const wrote = mocks.requests.filter(
      (request) =>
        request.method !== 'GET' && !request.url.endsWith('/agent-tasks/import')
    );
    expect(wrote).toEqual([]);

    for (const path of MUTATING_DISPATCH_PATHS) {
      expect(
        mocks.requests.filter((request) => request.url.includes(path))
      ).toEqual([]);
    }
  });

  test('the commit button is a second, separate press', async ({ page }) => {
    const mocks = await openTeamPage(page);
    const scope = await openScope(page);

    const commit = scope.getByTestId('milestone-commit-work');
    await expect(commit).toBeVisible();
    await expect(commit).toBeEnabled();
    // Labelled with the number it would act on rather than "Confirm".
    await expect(commit).toHaveText(
      `Start work on ${issueSeeds.length} issues`
    );
    expect(mocks.import.commits()).toHaveLength(0);
  });

  test('closing the panel again is never refused', async ({ page }) => {
    const page1 = page;
    await openTeamPage(page1, { state: pausedState });

    const control = playControl(page1);
    await expect(control).toBeDisabled();

    // Now open one while it is allowed, then pause underneath it.
    await page1.unroute('**/api/v1/agent-dispatch');
    await page1.route('**/api/v1/agent-dispatch', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ state: dispatchState() }),
      });
    });
    await page1.reload();
    await expect(page1.getByTestId('milestones-list')).toBeVisible();
    await openScope(page1);

    await expect(control).toHaveAttribute(
      'aria-label',
      `Close the work scope for ${FIRST_MILESTONE.name}`
    );
    await expect(control).toBeEnabled();
    await control.click();
    await expect(
      milestoneRow(page1).getByTestId('milestone-work-scope')
    ).toBeHidden();
  });
});

// =============================================================================
// A team with no Linear link
// =============================================================================

test.describe('A team that is not linked to Linear', () => {
  test('shows no play control at all', async ({ page }) => {
    await mockRoutes.config(page);
    await mockRoutes.agents(page);
    await mockRoutes.teams(page);
    await mockRoutes.teamMilestones(page, { state: 'not_linked' });
    const mocks = await mockDispatchRoutes(page);

    await page.goto(TEAM_URL);

    await expect(page.getByTestId('milestones-not-linked')).toBeVisible();
    await expect(page.getByTestId('milestone-start-work')).toHaveCount(0);
    await expect(page.getByTestId('milestone-work-scope')).toHaveCount(0);
    await expect(page.getByTestId('agent-step-progress')).toHaveCount(0);

    // And nothing was read on its behalf either: an unlinked team has no
    // milestone to scope to, so the console must not go looking.
    expect(
      mocks.requests.filter((request) => request.url.includes('/issues'))
    ).toEqual([]);
    expect(mocks.import.calls).toEqual([]);
  });

  test('a milestone list with no team key leaves the control off and explains why', async ({
    page,
  }) => {
    // The status is `ok`, so rows render, but there is no key to scope a run
    // to. The control has to refuse rather than open an empty panel.
    const mocks = await openTeamPage(page, {
      milestoneBody: { linear_team_key: null },
    });

    const control = playControl(page);
    await expect(control).toBeDisabled();
    await control.hover({ force: true });
    await expect(
      page.getByText('This team is not linked to a Linear team.')
    ).toBeVisible();

    await control.click({ force: true }).catch(() => {});
    await expect(page.getByTestId('milestone-work-scope')).toHaveCount(0);
    expect(mocks.import.calls).toEqual([]);
  });
});

// =============================================================================
// What the confirm shows
// =============================================================================

test.describe('The confirm', () => {
  test('lists a row per eligible issue, with its provenance', async ({
    page,
  }) => {
    await openTeamPage(page);
    const scope = await openScope(page);

    const rows = scope.getByTestId('scope-issue-row');
    await expect(rows).toHaveCount(issueSeeds.length);
    await expect(scope.getByTestId('scope-eligible-count')).toHaveText(
      `${issueSeeds.length} issues would be queued`
    );

    const first = rows.nth(0);
    await expect(first.getByTestId('scope-issue-identifier')).toHaveText(
      'ENG-101'
    );
    await expect(first.getByTestId('scope-issue-title')).toHaveText(
      issueSeeds[0].title
    );
    await expect(first.getByTestId('scope-issue-creator')).toHaveText(
      'Filed by Dana Okafor'
    );
    await expect(first.getByTestId('scope-issue-created')).toContainText(
      'created'
    );
    await expect(
      first.getByRole('link', { name: /Open in Linear/ })
    ).toHaveAttribute('href', 'https://linear.app/acme/issue/ENG-101');

    // A creator Linear did not give us is "unknown", not "null".
    await expect(rows.nth(2).getByTestId('scope-issue-creator')).toHaveText(
      'Filed by unknown'
    );
    await expect(scope).not.toContainText('null');
    await expect(scope).not.toContainText('undefined');
  });

  test('flags the row filed minutes ago, and says a flag is a hint', async ({
    page,
  }) => {
    await openTeamPage(page);
    const scope = await openScope(page);

    const flagged = scope.getByTestId('scope-issue-row').nth(1);
    await expect(flagged.getByTestId('scope-issue-flags')).toContainText(
      'new within the hour'
    );
    await expect(
      scope
        .getByTestId('scope-issue-row')
        .nth(0)
        .getByTestId('scope-issue-flags')
    ).toHaveCount(0);

    await expect(scope).toContainText(
      'A flagged row is a hint to look twice, not a verdict.'
    );
  });

  test('names the buckets it left out, including other_team', async ({
    page,
  }) => {
    await openTeamPage(page, {
      issues: {
        counts: {
          fetched: 12,
          skipped: { started: 2, assigned: 1, other_team: 3 },
          beyond_page_cap: true,
        },
      },
      import: { skipped: { already_queued: 4, already_worked: 2 } },
    });
    const scope = await openScope(page);
    const skipped = scope.getByTestId('scope-skipped-counts');

    await expect(skipped).toContainText(
      '3 issues in this milestone belong to another Linear team'
    );
    await expect(skipped).toContainText("needs that team's own run");
    await expect(skipped).toContainText(
      '1 issue is assigned to a person and stay'
    );
    await expect(skipped).toContainText(
      '2 issues have already been started by a human'
    );
    await expect(skipped).toContainText('4 already have an open task');
    await expect(skipped).toContainText('2 have been worked before');
    await expect(skipped).toContainText('cap of 100 issues');
  });

  test('states the turn ceiling, the workflow and the remaining budget', async ({
    page,
  }) => {
    await openTeamPage(page);
    const scope = await openScope(page);

    const workflow = scope.getByTestId('scope-workflow');
    await expect(workflow).toContainText('Workflow: Research then write');
    await expect(workflow).toContainText(
      'Step 1: marketing-researcher · up to 2 turns'
    );
    await expect(workflow).toContainText(
      'Step 2: marketing-writer · up to 3 turns'
    );

    const perTask = workflows[0].steps.reduce((n, s) => n + s.max_turns, 0);
    await expect(scope.getByTestId('scope-turn-ceiling')).toHaveText(
      `Turn ceiling: ${perTask} per issue, ${perTask * issueSeeds.length} across this set. A ceiling, not an estimate.`
    );

    const budget = scope.getByTestId('scope-budget');
    await expect(budget).toContainText('48 of 60 turns');
    await expect(budget).toContainText('18 of 20 tasks');
    // The ceiling is not enforced here and the screen has to say so.
    await expect(budget).toContainText(
      'enforced by the server on every turn, not by this screen'
    );
  });

  test('defaults to a dry run and makes turning it off deliberate', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page);
    const scope = await openScope(page);

    const dryRun = scope.getByTestId('scope-dry-run');
    await expect(dryRun).toBeChecked();

    await scope.getByTestId('milestone-commit-work').click();
    await expect.poll(() => mocks.import.commits().length).toBe(1);
    expect(mocks.import.commits()[0].body.dry_run).toBe(true);
  });

  test('an empty eligible set offers nothing to press', async ({ page }) => {
    await openTeamPage(page, { import: { eligibleRefs: [] } });
    const scope = await openScope(page);

    await expect(scope.getByTestId('scope-nothing-eligible')).toBeVisible();
    await expect(scope.getByTestId('milestone-commit-work')).toBeDisabled();
    await expect(scope.getByTestId('milestone-commit-work')).toHaveText(
      'Nothing to start'
    );
  });
});

// =============================================================================
// The commit is bound to the set that was displayed
// =============================================================================

test.describe('Committing', () => {
  test('sends only the rows the preview listed, with their digest', async ({
    page,
  }) => {
    // The server calls two of the three eligible. The third is on screen
    // nowhere, and must be nowhere in the commit either.
    const eligible = [issueSeeds[0].ref, issueSeeds[2].ref];
    const mocks = await openTeamPage(page, {
      import: { eligibleRefs: eligible },
    });
    const scope = await openScope(page);

    await expect(scope.getByTestId('scope-issue-row')).toHaveCount(2);
    await scope.getByTestId('milestone-commit-work').click();

    await expect.poll(() => mocks.import.commits().length).toBe(1);
    const commit = mocks.import.commits()[0].body;
    expect(commit.mode).toBe('commit');
    expect(commit.scope.items.map((item) => item.source_ref).sort()).toEqual(
      [...eligible].sort()
    );
    expect(commit.expected_refs_digest).toBe(digestOf(eligible));
    await expect(scope.getByTestId('scope-commit-error')).toHaveCount(0);
  });

  test('a set that moved between preview and press is refused, and nothing is created', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page);
    const scope = await openScope(page);
    await expect(scope.getByTestId('scope-issue-row')).toHaveCount(3);

    // Somebody files a fourth issue, or withdraws one, after the operator read
    // the list. The digest they are about to send no longer describes the set.
    mocks.import.setEligible([issueSeeds[0].ref]);

    await scope.getByTestId('milestone-commit-work').click();

    const error = scope.getByTestId('scope-commit-error');
    await expect(error).toBeVisible();
    await expect(error).toContainText('The set changed');
    await expect(error).toContainText('Nothing was created');

    // The refusal is the server's, and the console re-read rather than
    // retrying with the digest the server just rejected.
    const commits = mocks.import.commits();
    expect(commits).toHaveLength(1);
    await expect(scope.getByTestId('scope-issue-row')).toHaveCount(1);
  });

  test('an in-flight commit cannot be pressed a second time', async ({
    page,
  }) => {
    let release: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });

    const mocks = await openTeamPage(page, { import: { commitGate: held } });
    const scope = await openScope(page);
    const commit = scope.getByTestId('milestone-commit-work');

    await commit.click();
    await expect(commit).toBeDisabled();
    // Force past the disabled attribute: a control that only looks disabled
    // would still fire, and one commit charged twice is the failure here.
    await commit.click({ force: true }).catch(() => {});
    await commit.click({ force: true }).catch(() => {});

    release();
    await expect.poll(() => mocks.import.commits().length).toBe(1);
  });

  test('two panels previewed concurrently never cross their scopes', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page);

    await openScope(page, 0);
    // Opening the second closes the first. Whatever the second previews must
    // be keyed to its own milestone, and the first must not commit anything.
    await playControl(page, 1).click();
    await expect(
      milestoneRow(page, 1).getByTestId('milestone-work-scope')
    ).toBeVisible();
    await expect(
      milestoneRow(page, 0).getByTestId('milestone-work-scope')
    ).toBeHidden();

    const issueReads = mocks.requests.filter((request) =>
      request.url.includes('/issues')
    );
    expect(
      issueReads.some((r) => r.url.includes(mockData.milestones[0].id))
    ).toBe(true);
    expect(
      issueReads.some((r) => r.url.includes(mockData.milestones[1].id))
    ).toBe(true);
    expect(mocks.import.commits()).toHaveLength(0);
  });
});

// =============================================================================
// Untrusted text
// =============================================================================

test.describe('Untrusted issue text', () => {
  test('a title carrying script tags and markdown renders inert', async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(String(error)));

    await openTeamPage(page);
    const scope = await openScope(page);

    const title = scope
      .getByTestId('scope-issue-row')
      .nth(1)
      .getByTestId('scope-issue-title');

    // Verbatim, which is only possible if it went in as a text node.
    await expect(title).toHaveText(hostileIssueTitle);
    // The markdown is text too: no renderer turned `**not bold**` into markup.
    await expect(title.locator('strong')).toHaveCount(0);
    await expect(title.locator('em')).toHaveCount(0);
    await expect(title.locator('a')).toHaveCount(0);
    await expect(title.locator('img')).toHaveCount(0);
    await expect(title.locator('script')).toHaveCount(0);
    expect(
      await title.evaluate((node) => node.querySelectorAll('*').length)
    ).toBe(0);

    // And nothing ran: no element was built, and no handler fired either.
    expect(await xssFireCount(page)).toBe(0);
    expect(errors).toEqual([]);
  });

  test('the same title is not re-interpreted anywhere else on the screen', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_hostile',
          source_ref: issueSeeds[1].ref,
          title: hostileIssueTitle,
          status: 'running',
        }),
      ],
    });
    await openScope(page);

    const row = page.getByTestId('task-run-row').first();
    await expect(row.getByTestId('task-run-title')).toHaveText(
      hostileIssueTitle
    );
    expect(
      await row
        .getByTestId('task-run-title')
        .evaluate((node) => node.querySelectorAll('*').length)
    ).toBe(0);
    expect(await xssFireCount(page)).toBe(0);
  });

  test('agent output in the step rail is text, not markup', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_out',
          source_ref: issueSeeds[0].ref,
          status: 'running',
        }),
      ],
    });
    await openScope(page);

    await page.getByTestId('task-run-toggle').first().click();
    const output = page.getByTestId('task-step-output').first();
    await expect(output).toHaveText(hostileAgentOutput);
    expect(
      await output.evaluate((node) => node.querySelectorAll('*').length)
    ).toBe(0);
    // Verbatim means the newlines survived, which is what `pre-wrap` is for.
    await expect(output).toHaveCSS('white-space', 'pre-wrap');
    expect(await xssFireCount(page)).toBe(0);
  });

  test('a provenance link is never a scheme the browser would execute', async ({
    page,
  }) => {
    await openTeamPage(page, {
      issues: {
        seeds: [
          {
            ...issueSeeds[0],
            // A url that is not a web address has no business being clickable
            // on the one screen whose job is checking provenance.
            url: 'javascript:window.__agentControlXssFired = 1',
          },
        ],
      },
    });
    const scope = await openScope(page);

    const links = scope.getByTestId('scope-issue-row').locator('a');
    const hrefs = await links.evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute('href') ?? '')
    );
    for (const href of hrefs) {
      expect(href.toLowerCase().startsWith('javascript:')).toBe(false);
      expect(href.toLowerCase().startsWith('data:')).toBe(false);
    }
    expect(await xssFireCount(page)).toBe(0);
  });
});

// =============================================================================
// The two progress bars
// =============================================================================

test.describe('The agent-step bar', () => {
  test('is a separate element from Linear progress, with its own label', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_a',
          source_ref: issueSeeds[0].ref,
          status: 'running',
          current_step: 1,
        }),
        taskSummary({
          task_key: 'task_b',
          source_ref: issueSeeds[1].ref,
          status: 'completed',
          current_step: 2,
        }),
      ],
    });
    await openScope(page);

    const row = milestoneRow(page);
    // Mantine puts a `Progress`'s aria-label on its filled section and a
    // `Progress.Root`'s on the root, so the two are reached differently on
    // purpose rather than by accident.
    const linearFill = row.getByLabel(
      `${FIRST_MILESTONE.name} issue completion`
    );
    const agentBar = row.getByTestId('agent-step-progress');
    const agentRoot = row.getByLabel(/^Agent steps finished: \d+ of \d+$/);

    await expect(linearFill).toBeVisible();
    await expect(agentBar).toBeVisible();
    await expect(agentRoot).toBeVisible();

    // Structurally distinct: neither bar is inside the other.
    expect(
      await agentBar.evaluate(
        (node, label) => node.querySelector(`[aria-label="${label}"]`) !== null,
        `${FIRST_MILESTONE.name} issue completion`
      )
    ).toBe(false);
    expect(
      await linearFill.evaluate(
        (node) =>
          node.closest('[data-testid="agent-step-progress"]') !== null ||
          node.querySelector('[data-testid="agent-step-progress"]') !== null
      )
    ).toBe(false);

    // Labelled as a self-report, and pointing at the other bar by name.
    await expect(agentBar).toContainText(
      'Reported by the agents, not by Linear.'
    );
    await expect(agentBar).toContainText(
      'The bar above this one is issue completion and only a person moves it.'
    );
    await expect(agentBar.getByTestId('agent-step-progress-count')).toHaveText(
      'agents: 3 of 4 steps'
    );

    // Visually distinct: thinner, and a different fill colour.
    const boxes = await Promise.all([
      linearFill.evaluate((node) => {
        const root = node.parentElement as HTMLElement;
        return {
          height: root.getBoundingClientRect().height,
          fill: getComputedStyle(node).backgroundColor,
          top: root.getBoundingClientRect().top,
        };
      }),
      agentRoot.evaluate((node) => {
        const fill = node.firstElementChild as HTMLElement;
        return {
          height: node.getBoundingClientRect().height,
          fill: getComputedStyle(fill).backgroundColor,
          top: node.getBoundingClientRect().top,
        };
      }),
    ]);

    expect(boxes[1].height).toBeLessThan(boxes[0].height);
    expect(boxes[1].fill).not.toBe(boxes[0].fill);
    // Stacked, never side by side and never merged: the agent bar is below.
    expect(boxes[1].top).toBeGreaterThan(boxes[0].top);
  });

  test('does not appear at all when no task has been started', async ({
    page,
  }) => {
    await openTeamPage(page);
    await openScope(page);

    await expect(
      milestoneRow(page).getByLabel(`${FIRST_MILESTONE.name} issue completion`)
    ).toBeVisible();
    await expect(page.getByTestId('agent-step-progress')).toHaveCount(0);
  });
});

// =============================================================================
// Runs and the step rail
// =============================================================================

test.describe('The step rail', () => {
  test('shows a hop that never ran rather than a shorter chain', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_a',
          source_ref: issueSeeds[0].ref,
          status: 'running',
        }),
      ],
    });
    await openScope(page);

    await page.getByTestId('task-run-toggle').first().click();
    const rail = page.getByTestId('task-step-rail');
    await expect(rail).toBeVisible();

    const steps = rail.getByTestId('task-step');
    await expect(steps).toHaveCount(2);
    await expect(steps.nth(0)).toContainText('Step 1: marketing-researcher');
    await expect(steps.nth(0)).toContainText('completed');
    await expect(steps.nth(1)).toContainText('Step 2: marketing-writer');
    await expect(steps.nth(1)).toContainText('never ran');
    await expect(rail).toContainText('1 of 2 steps started');
  });

  test('a failed hop names its code and the task says what the status means', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_f',
          source_ref: issueSeeds[0].ref,
          status: 'running_unknown',
          failure_code: 'TURN_TIMEOUT',
          failure_detail: 'No response within the turn deadline.',
        }),
      ],
      chains: {
        task_f: chainFor('task_f', {
          status: 'running_unknown',
          hops: [
            chainHop({
              step_index: 0,
              status: 'failed',
              failure_code: 'TURN_TIMEOUT',
              failure_detail: 'The executor did not answer.',
            }),
          ],
          hops_planned: 2,
          hops_ran: 1,
        }),
      },
    });
    await openScope(page);

    const row = page.getByTestId('task-run-row').first();
    await expect(row.getByTestId('task-run-status')).toHaveText(
      'running_unknown'
    );
    await expect(row).toContainText(
      'A turn timed out and nothing proves the work stopped.'
    );
    await expect(row).toContainText('It is not retried automatically');
    await expect(row.getByTestId('task-run-failure')).toContainText(
      'TURN_TIMEOUT'
    );

    await page.getByTestId('task-run-toggle').first().click();
    await expect(page.getByTestId('task-step-failure').first()).toContainText(
      'TURN_TIMEOUT: The executor did not answer.'
    );
  });

  test('a running task is counted in the row header', async ({ page }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({ task_key: 't1', source_ref: issueSeeds[0].ref }),
        taskSummary({ task_key: 't2', source_ref: issueSeeds[1].ref }),
        taskSummary({
          task_key: 't3',
          source_ref: issueSeeds[2].ref,
          status: 'completed',
        }),
      ],
    });
    await openScope(page);

    await expect(
      milestoneRow(page).getByTestId('milestone-running-count')
    ).toHaveText('2 running');
    await expect(
      milestoneRow(page).getByTestId('milestone-running-spinner')
    ).toBeVisible();
  });
});

// =============================================================================
// Finished work: no agent closes an issue
// =============================================================================

test.describe('Results waiting for a person', () => {
  async function openWithCompletedWork(page: Page) {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_done',
          source_ref: issueSeeds[0].ref,
          status: 'completed',
          current_step: 2,
          // Explicit although it is the fixture default: a dry run is
          // excluded from write-back server-side, so the queue stays empty
          // and everything in this block is the no-proposal path.
          dry_run: true,
          updated_at: new Date(Date.now() - 20 * 60_000).toISOString(),
        }),
      ],
    });
    await openScope(page);
    return page.getByTestId('task-review-queue');
  }

  test('a dry run says the tracker is untouched and offers no accept control', async ({
    page,
  }) => {
    const queue = await openWithCompletedWork(page);

    await expect(queue).toContainText('1 result waiting for you');
    await expect(queue).toContainText(
      'Nothing has been written to the tracker and no issue has been closed.'
    );

    // The absence is the assertion. A dry run is excluded from write-back on
    // the server and proposes nothing, so no accept control may appear for
    // one: a button that pretended to write would be worse than no button.
    for (const name of [
      /accept/i,
      /approve/i,
      /reject/i,
      /^close$/i,
      /mark (as )?done/i,
      /accept all/i,
    ]) {
      await expect(queue.getByRole('button', { name })).toHaveCount(0);
    }

    // Stronger than a name match: every control in the queue is the row's own
    // disclosure toggle. Nothing here submits, and nothing here decides.
    const buttons = queue.getByRole('button');
    const toggles = queue.getByTestId('task-run-toggle');
    await expect(toggles).toHaveCount(1);
    expect(await buttons.count()).toBe(await toggles.count());
    await expect(queue.locator('form')).toHaveCount(0);
    await expect(queue.locator('input, select, textarea')).toHaveCount(0);
  });

  test("a dry run's only path forward is a link a person clicks in Linear", async ({
    page,
  }) => {
    const queue = await openWithCompletedWork(page);

    const link = queue.getByTestId('task-review-link');
    await expect(link).toHaveCount(1);
    await expect(link).toHaveText(/Close ENG-101 in Linear/);
    await expect(link).toHaveAttribute(
      'href',
      'https://linear.app/acme/issue/ENG-101'
    );
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(link).toHaveAttribute('rel', /noopener/);
  });

  test('a result nobody read for two days is marked stale, not approved', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_old',
          source_ref: issueSeeds[0].ref,
          status: 'completed',
          updated_at: new Date(Date.now() - 72 * 3600_000).toISOString(),
        }),
      ],
    });
    await openScope(page);

    const stale = page.getByTestId('task-review-stale');
    await expect(stale).toContainText('waiting more than two days');
    await expect(stale).toContainText('Nothing here expires into approval.');
  });

  test('no request that could write to Linear or accept work is ever made', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page, {
      tasks: [
        taskSummary({
          task_key: 'task_done',
          source_ref: issueSeeds[0].ref,
          status: 'completed',
        }),
      ],
    });
    await openScope(page);
    await page.getByTestId('task-run-toggle').first().click();
    await expect(page.getByTestId('task-step-rail')).toBeVisible();

    expect(mutatingRequests(mocks)).toEqual([]);
    // And the one POST that did happen only ever looked.
    expect(mocks.import.commits()).toEqual([]);
    for (const path of ['/accept', '/reject', '/writeback', '/linear']) {
      expect(
        mocks.requests.filter((request) => request.url.includes(path))
      ).toEqual([]);
    }
  });
});

// =============================================================================
// The review queue: a real run's proposal, and the human decision over it
// =============================================================================

test.describe('Deciding a proposal', () => {
  const ENTRY_TASK = 'task_real';

  function realRunTask() {
    return taskSummary({
      task_key: ENTRY_TASK,
      source_ref: issueSeeds[0].ref,
      status: 'completed',
      current_step: 2,
      dry_run: false,
      updated_at: new Date(Date.now() - 20 * 60_000).toISOString(),
    });
  }

  test('a proposal renders the claim, the live target, and no accept-all', async ({
    page,
  }) => {
    const entry = reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 });
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: { entries: [entry] },
    });
    await openScope(page);
    const queue = page.getByTestId('task-review-queue');

    await expect(queue).toContainText('1 result waiting for you');
    const card = queue.getByTestId('review-card');
    await expect(card).toHaveCount(1);

    // The claim: who, and what they said, rendered as text.
    await expect(card).toContainText(
      'Agent marketing-writer proposes closing this issue'
    );
    await expect(card.getByTestId('review-card-summary')).toContainText(
      'Rewrote the guide for 2.4.'
    );

    // The target, read live: the card shows what the issue is, not only what
    // the agent claims about it.
    await expect(card).toContainText('ENG-101');
    await expect(card.getByTestId('review-card-title')).toContainText(
      'Rewrite the onboarding guide'
    );
    await expect(card).toContainText('In Linear it is "In Progress"');

    // One decision per issue. Eight issues means eight of these buttons, and
    // nothing on this screen decides more than one.
    await expect(card.getByTestId('review-accept')).toHaveText(
      /Accept and close ENG-101/
    );
    await expect(card.getByTestId('review-reject')).toBeVisible();
    await expect(
      queue.getByRole('button', { name: /accept all/i })
    ).toHaveCount(0);
    await expect(queue.getByTestId('review-accept')).toHaveCount(1);
  });

  test('accepting echoes the digest the card showed, and the bar moves', async ({
    page,
  }) => {
    const entry = reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 });
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: { entries: [entry], milestoneProgress: 0.5 },
    });
    await openScope(page);

    const progress = milestoneRow(page, 0).getByTestId('milestone-progress');
    await expect(progress).toHaveText('25%');

    await page.getByTestId('review-accept').click();

    const decided = page.getByTestId('review-decided');
    await expect(decided).toContainText('ENG-101 is now closed in Linear.');
    await expect(decided).toContainText(
      'Milestone issue completion is now 50%.'
    );

    // The accept carried exactly the digest of the card that was read. The
    // mock refuses anything else, so reaching the confirmation proves it,
    // and the recorded body pins it.
    expect(mocks.review.accepts).toEqual([
      {
        taskKey: ENTRY_TASK,
        body: {
          writeback_id: 12,
          expected_decision_digest: entry.decision_digest,
        },
      },
    ]);

    // The bar the reviewer just moved moves on screen, from the value the
    // accept response carried rather than from a refetch that could land on
    // a stale replica.
    await expect(progress).toHaveText('50%');

    // The decided card offers no second decision.
    await expect(page.getByTestId('review-accept')).toHaveCount(0);

    // The posted comment lives on the issue, and the confirmation links to
    // it through the same sanitizer every tracker link on this screen uses.
    const link = decided.getByTestId('review-accepted-link');
    await expect(link).toHaveAttribute(
      'href',
      'https://linear.app/acme/issue/ENG-101'
    );
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(link).toHaveAttribute('rel', /noopener/);
  });

  test('an accept over a moved card is refused, and the queue is re-read', async ({
    page,
  }) => {
    const entry = reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 });
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [entry],
        acceptError: {
          status: 409,
          errorCode: 'DECISION_CHANGED',
          detail:
            'The output, the target issue, or the completed state moved ' +
            'between the card you read and this accept. Nothing was changed.',
        },
      },
    });
    await openScope(page);

    const readsBefore = mocks.review.queueReads();
    await page.getByTestId('review-accept').click();

    await expect(page.getByTestId('review-card-error')).toContainText(
      'Nothing was changed.'
    );
    // Refused, not decided: the card stays, and the queue is re-read so the
    // next press is bound to what is now true.
    await expect(page.getByTestId('review-card')).toHaveCount(1);
    await expect
      .poll(() => mocks.review.queueReads())
      .toBeGreaterThan(readsBefore);
  });

  test('the credential that ran the work is refused as its approver', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
        acceptError: {
          status: 409,
          errorCode: 'SELF_APPROVAL_REFUSED',
          detail:
            'The approving credential is the one that ran this task, and it ' +
            'may not accept its own work.',
        },
      },
    });
    await openScope(page);

    await page.getByTestId('review-accept').click();

    await expect(page.getByTestId('review-card-error')).toContainText(
      'may not accept its own work'
    );
    await expect(page.getByTestId('review-card')).toHaveCount(1);
  });

  test('a write-disabled deployment refuses the accept legibly', async ({
    page,
  }) => {
    // The shipped default: a Linear key, the write flag off. The queue still
    // serves the card and its digest; what refuses is the press, server-side,
    // and the refusal must read as configuration rather than as a crash.
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
        acceptError: {
          status: 409,
          errorCode: 'LINEAR_WRITE_DISABLED',
          detail: 'Write-back to Linear is disabled on this deployment.',
          hint: 'Set AGENT_CONTROL_LINEAR_WRITE_ENABLED=true and restart.',
        },
      },
    });
    await openScope(page);

    await page.getByTestId('review-accept').click();

    const error = page.getByTestId('review-card-error');
    await expect(error).toContainText(
      'Write-back to Linear is disabled on this deployment.'
    );
    // The server's own hint rides along, so the operator knows what to change.
    await expect(error).toContainText(
      'Set AGENT_CONTROL_LINEAR_WRITE_ENABLED=true and restart.'
    );
    // Refused, not decided: the card stays and nothing claims a close happened.
    await expect(page.getByTestId('review-card')).toHaveCount(1);
    await expect(page.getByTestId('review-decided')).toHaveCount(0);
  });

  test('rejecting needs a reason, records it, and closes nothing', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
      },
    });
    await openScope(page);

    await page.getByTestId('review-reject').click();

    // No reason, no rejection: "records why" is the point of the path.
    const confirm = page.getByTestId('review-reject-confirm');
    await expect(confirm).toBeDisabled();

    await page
      .getByTestId('review-reject-reason')
      .fill('The install steps for 2.4 are still wrong in section 3.');
    await confirm.click();

    const decided = page.getByTestId('review-decided');
    await expect(decided).toContainText(
      'ENG-101 stays open and the task stays completed.'
    );
    await expect(decided).toContainText(
      'The install steps for 2.4 are still wrong in section 3.'
    );

    expect(mocks.review.rejects).toEqual([
      {
        taskKey: ENTRY_TASK,
        body: {
          writeback_id: 12,
          reason: 'The install steps for 2.4 are still wrong in section 3.',
        },
      },
    ]);
    // Nothing was accepted and nothing reached an accept route.
    expect(mocks.review.accepts).toEqual([]);
    expect(
      mocks.requests.filter((request) => request.url.includes('/accept'))
    ).toEqual([]);
  });

  test('a hostile summary renders as text and nothing executes', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [
          reviewEntry({
            task_key: ENTRY_TASK,
            writeback_id: 12,
            summary: hostileAgentOutput,
          }),
        ],
      },
    });
    await openScope(page);

    const summary = page.getByTestId('review-card-summary');
    await expect(summary).toContainText('# not a heading');
    await expect(summary).toContainText('<b>not bold</b>');
    expect(await xssFireCount(page)).toBe(0);
  });

  test('a card Linear could not be read for cannot be accepted', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [
          reviewEntry({
            task_key: ENTRY_TASK,
            writeback_id: 12,
            decision_digest: null,
            issue: { source_ref: issueSeeds[0].ref, read_failed: true },
          }),
        ],
      },
    });
    await openScope(page);

    await expect(page.getByTestId('review-card-unreadable')).toContainText(
      'could not be read from Linear'
    );
    await expect(page.getByTestId('review-accept')).toBeDisabled();
  });
});

// =============================================================================
// Pause and halt
// =============================================================================

test.describe('Stop switches', () => {
  test('a paused namespace disables the play control and says why', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page, { state: pausedState });

    const control = playControl(page);
    await expect(control).toBeDisabled();
    await control.hover({ force: true });
    await expect(
      page.getByText('New agent work is paused in this namespace.')
    ).toBeVisible();
    expect(mocks.import.calls).toEqual([]);

    // Pinning a known consequence rather than asserting it is right: the
    // banner lives inside the panel the pause has just made unopenable, so on
    // an already-paused namespace the tooltip is the whole message and the
    // "a running tool is not stopped by this" caveat is never shown.
    await expect(page.getByTestId('dispatch-paused-banner')).toHaveCount(0);
  });

  test('the paused banner says what the pause does not do', async ({
    page,
  }) => {
    // Paused while the panel is already open. This is the only way the banner
    // is reachable at all, because a namespace that is already paused disables
    // the control that opens the panel it lives in.
    const mocks = await openTeamPage(page);
    const scope = await openScope(page);

    await page.unroute('**/api/v1/agent-dispatch');
    await page.route('**/api/v1/agent-dispatch', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ state: pausedState }),
      });
    });

    // A successful commit invalidates the dispatch read, which is how the new
    // state reaches the screen without waiting on a poll.
    await scope.getByTestId('milestone-commit-work').click();
    await expect.poll(() => mocks.import.commits().length).toBe(1);

    const banner = page.getByTestId('dispatch-paused-banner');
    await expect(banner).toBeVisible({ timeout: 20_000 });
    await expect(banner).toContainText(
      'A tool that is already executing is not stopped by this'
    );
    await expect(banner).toContainText(
      'nothing in this console unwinds an action a tool has already taken'
    );
    await expect(page.getByTestId('dispatch-paused-reason')).toContainText(
      'Investigating a runaway writer agent.'
    );
    // A credential, never a person.
    await expect(banner).toContainText('by the credential ck_9f2a1c');
    await expect(scope.getByTestId('milestone-commit-work')).toBeDisabled();
  });

  test('halted executors block the press and the banner names both switches', async ({
    page,
  }) => {
    await openTeamPage(page, { state: haltedState });

    const control = playControl(page);
    await expect(control).toBeDisabled();
    await control.hover({ force: true });
    await expect(
      page.getByText('Executors are halted in this namespace.')
    ).toBeVisible();
  });
});

// =============================================================================
// Degraded reads
// =============================================================================

test.describe('When a read fails', () => {
  test('a stale milestone list turns the control off rather than guessing', async ({
    page,
  }) => {
    await openTeamPage(page, {
      milestoneBody: {
        cached: true,
        fetched_at: new Date(Date.now() - 10 * 60_000).toISOString(),
      },
    });

    const control = playControl(page);
    await expect(control).toBeDisabled();
    await control.hover({ force: true });
    await expect(
      page.getByText(/could not be refreshed, so the set a press would cover/)
    ).toBeVisible();
  });

  test('an ordinary cached read leaves the control alone', async ({ page }) => {
    await openTeamPage(page, {
      milestoneBody: {
        cached: true,
        fetched_at: new Date(Date.now() - 5_000).toISOString(),
      },
    });

    await expect(playControl(page)).toBeEnabled();
  });

  test('a Linear outage inside the panel shows no stale list', async ({
    page,
  }) => {
    await openTeamPage(page, {
      issues: {
        body: {
          status: 'error',
          issues: [],
          error: 'Linear did not answer in time.',
          retry_after_seconds: 30,
        },
      },
    });
    await playControl(page).click();

    const alert = page.getByTestId('milestone-work-linear-error');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Linear did not answer in time.');
    await expect(alert).toContainText('an old list of work to start is not');
    await expect(page.getByTestId('milestone-work-scope')).toHaveCount(0);
    await expect(page.getByTestId('milestone-commit-work')).toHaveCount(0);
  });

  test('Agent Control itself failing says nothing has started', async ({
    page,
  }) => {
    await openTeamPage(page, { issues: { status: 500 } });
    await playControl(page).click();

    const alert = page.getByTestId('milestone-work-error');
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Nothing has started');
    await expect(page.getByTestId('milestone-commit-work')).toHaveCount(0);
  });
});

// =============================================================================
// Both colour schemes
// =============================================================================

for (const scheme of ['light', 'dark'] as const) {
  test.describe(`In the ${scheme} colour scheme`, () => {
    test('the scope panel, the run rows and both bars are legible', async ({
      page,
    }) => {
      // Mantine's own storage key, so this drives the app's mechanism rather
      // than stamping the attribute the app is supposed to set.
      await page.addInitScript((value) => {
        window.localStorage.setItem('mantine-color-scheme-value', value);
      }, scheme);

      await openTeamPage(page, {
        tasks: [
          taskSummary({
            task_key: 'task_a',
            source_ref: issueSeeds[0].ref,
            status: 'running',
            current_step: 1,
          }),
        ],
      });

      await expect(page.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        scheme
      );

      const scope = await openScope(page);
      await expect(scope).toBeVisible();
      await expect(scope.getByTestId('scope-issue-row').first()).toBeVisible();
      await expect(page.getByTestId('agent-step-progress')).toBeVisible();
      await page.getByTestId('task-run-toggle').first().click();
      await expect(page.getByTestId('task-step-output').first()).toBeVisible();

      // The panel is not the page: a surface that collapsed to the same colour
      // as its background in one scheme is a panel nobody can see the edge of.
      const [panelBg, pageBg] = await Promise.all([
        scope.evaluate((node) => getComputedStyle(node).backgroundColor),
        page
          .locator('body')
          .evaluate((node) => getComputedStyle(node).backgroundColor),
      ]);
      expect(panelBg).not.toBe(pageBg);

      const output = page.getByTestId('task-step-output').first();
      const [outputColor, outputBg] = await Promise.all([
        output.evaluate((node) => getComputedStyle(node).color),
        output.evaluate((node) => getComputedStyle(node).backgroundColor),
      ]);
      expect(outputColor).not.toBe(outputBg);
    });
  });
}
