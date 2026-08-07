import type { KnowledgeSearchResponse } from '@/core/api/types';

import { expect, test } from './fixtures';
import {
  answer,
  corpus,
  externalResult,
  hostileHeading,
  hostilePath,
  hostileResult,
  hostileSnippet,
  hostileSourceName,
  hostileTitle,
  mockKnowledgeRoutes,
  refusal,
  unknownAuthorResult,
  unmeasuredCorpus,
  workspaceResult,
  xssFireCount,
} from './knowledge-fixtures';

/**
 * The console's company-knowledge panel.
 *
 * The case that matters most is the inert one. A snippet is text somebody
 * wrote in a Drive folder, and the server deliberately does not escape it -
 * escaping would corrupt the string an agent quotes back to a person. So the
 * rendering is the only thing standing between a document's author and the
 * console's DOM, and it is asserted two ways: no node was built, and nothing
 * executed by any other route either.
 */

const KNOWLEDGE_URL = '/knowledge';

test.describe('Company knowledge panel', () => {
  test('opens on what changed, so freshness is answered before it is asked', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);

    await expect(
      mockedPage.getByRole('heading', { name: 'Company knowledge' })
    ).toBeVisible();
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();

    // The recency verb ran on mount and the search verb did not: a person who
    // has not typed anything has still been told how current the mirror is.
    expect(calls.recents).toHaveLength(1);
    expect(calls.searches).toHaveLength(0);

    const strip = mockedPage.getByTestId('knowledge-freshness');
    await expect(strip).toContainText('412 documents');
    await expect(strip).toContainText('last checked 8 minutes ago');
  });

  test('a snippet containing markup renders inert', async ({ mockedPage }) => {
    await mockKnowledgeRoutes(mockedPage, {
      recent: answer([hostileResult]),
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    const snippet = mockedPage.getByTestId('knowledge-result-snippet');
    await expect(snippet).toBeVisible();

    // The characters are on the page, which is the point: the text is shown
    // verbatim rather than stripped, and it is shown as text.
    await expect(snippet).toContainText('<script>');
    await expect(snippet).toContainText('not bold');
    await expect(snippet.locator('script, img, b')).toHaveCount(0);

    const path = mockedPage.getByTestId('knowledge-result-path');
    await expect(path).toContainText('<img src=y');
    await expect(path.locator('img')).toHaveCount(0);

    // Every field on the card, not just the two this test started with. The
    // title, the heading trail and the source name are chosen by the same
    // person who chose the body, and a sink on any one of them is the same
    // failure - a raw-HTML title passed every case in this file while a real
    // <img onerror> node sat in the DOM, because nothing here looked outside
    // the snippet and the path.
    const card = mockedPage.getByTestId('knowledge-result-0');
    await expect(card).toContainText('<b>unlimited</b>');
    await expect(card).toContainText('<iframe');
    await expect(card.locator('b, i, script, img, iframe, svg')).toHaveCount(0);

    // Absence of a node proves the markup was not built. The counter proves
    // nothing ran by another route either.
    expect(await xssFireCount(mockedPage)).toBe(0);
    expect(hostileSnippet).toContain('<script>');
    expect(hostilePath).toContain('onerror');
    expect(hostileTitle).toContain('onerror');
    expect(hostileHeading).toContain('onerror');
    expect(hostileSourceName).toContain('onerror');
  });

  test('a search runs on submit, not on every keystroke', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage
      .getByTestId('knowledge-search-input')
      .fill('laptop reimbursement');

    // Nine characters typed and still nothing spent from the window. A
    // search-as-you-type box would have burnt the allowance on prefixes.
    expect(calls.searches).toHaveLength(0);

    await mockedPage.getByTestId('knowledge-search-submit').click();

    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    expect(calls.searches).toHaveLength(1);
    expect(calls.searches[0]).toMatchObject({ query: 'laptop reimbursement' });
    await expect(
      mockedPage.getByTestId('knowledge-result-snippet')
    ).toContainText(workspaceResult.snippet);
  });

  test('a refusal is a sentence, and it is not an error box', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      recent: refusal('knowledge_disabled'),
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    const stated = mockedPage.getByTestId('knowledge-refusal');
    await expect(stated).toContainText('switched off on this deployment');
    await expect(stated).toContainText('AGENT_CONTROL_KNOWLEDGE_ENABLED');
    await expect(mockedPage.getByTestId('knowledge-error')).toHaveCount(0);
  });

  test('a store nobody can reach is a stated empty state, not a crash', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      recent: refusal('knowledge_unavailable'),
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    // Switched off and unreachable are the two ways this page has nothing to
    // show through no fault of the person looking at it. Both arrive as a 200
    // carrying a code, so both have to render as a sentence; the page still
    // stands up, the tabs still work, and the footer is still there.
    await expect(mockedPage.getByTestId('knowledge-refusal')).toContainText(
      'could not be reached'
    );
    await expect(mockedPage.getByTestId('knowledge-error')).toHaveCount(0);
    await expect(mockedPage.getByTestId('knowledge-freshness')).toBeVisible();
    await expect(
      mockedPage.getByRole('heading', { name: 'Company knowledge' })
    ).toBeVisible();
  });

  test('a rate-limited search says when it is worth trying again', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      search: refusal('rate_limited', { retry_after_seconds: 42 }),
    });

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('laptops');
    await mockedPage.getByTestId('knowledge-search-submit').click();

    await expect(mockedPage.getByTestId('knowledge-refusal')).toContainText(
      'about 42 seconds'
    );
  });

  test('a rejected credential reads differently from a refused search', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, { searchStatus: 403 });

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('laptops');
    await mockedPage.getByTestId('knowledge-search-submit').click();

    // "your key was rejected" and "the corpus is switched off" are different
    // facts and lead to different fixes. One red box for both would hide that.
    const error = mockedPage.getByTestId('knowledge-error');
    await expect(error).toBeVisible();
    await expect(mockedPage.getByTestId('knowledge-refusal')).toHaveCount(0);

    // Naming the operation, not just failing. Asserting only that a red box
    // appeared leaves the panel free to print the server-is-down sentence at
    // somebody whose key is the actual problem, which is the same
    // misdiagnosis as the reverse and passes an assertion about visibility.
    await expect(error).toContainText('key was rejected');
    await expect(error).toContainText('status operation');
  });

  test('authorship nobody could establish is flagged, not smoothed over', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      recent: answer([workspaceResult, externalResult, unknownAuthorResult]),
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    await expect(
      mockedPage.getByTestId('knowledge-external-note')
    ).toContainText('2 of these');
    await expect(
      mockedPage.getByTestId('knowledge-result-1').getByText('outside author')
    ).toBeVisible();
    await expect(
      mockedPage.getByTestId('knowledge-result-2').getByText('author unknown')
    ).toBeVisible();
  });

  test('a mirror days behind says so where a person will read it', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      recent: answer([workspaceResult], {
        corpus: corpus({
          documents: 12,
          sources: 2,
          sources_failing: 1,
          last_sync_at: '2026-08-01T09:15:00Z',
          stale_seconds: 5 * 24 * 60 * 60,
        }),
      }),
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    const strip = mockedPage.getByTestId('knowledge-freshness');
    await expect(strip).toContainText('last checked 5 days ago');
    await expect(strip).toContainText('1 source failing');
  });

  test('the freshness footer survives switching to a search nobody has run', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-freshness')).toContainText(
      '412 documents'
    );

    await mockedPage.getByTestId('knowledge-tab-search').click();

    // A footer that disappears on the tab a person spends most of their time
    // on is not a footer. The strip is the answer to "is this mirror current",
    // and that question does not stop being worth answering because the search
    // box is still empty - which is also why the tab change spends nothing.
    await expect(mockedPage.getByTestId('knowledge-freshness')).toContainText(
      '412 documents'
    );
    expect(calls.searches).toHaveLength(0);
  });

  test('a server that never answered does not read as a permissions problem', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, { searchAborts: true });

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('laptops');
    await mockedPage.getByTestId('knowledge-search-submit').click();

    // Three failures, three fixes: rotate a key, start the server, check the
    // knowledge DSN. Telling an operator to go and find a better key while the
    // server is down costs them the whole first hour of the investigation.
    const error = mockedPage.getByTestId('knowledge-error');
    await expect(error).toBeVisible();
    await expect(error).not.toContainText('status operation');
    await expect(error).toContainText('did not answer');
  });

  test('a refusal that never reached the corpus does not report a corpus', async ({
    mockedPage,
  }) => {
    // Exactly what the server sends: the counters are required on every
    // response so a deny control fails closed, so a refusal raised before the
    // store was ever opened carries zeros. They are placeholders, not readings.
    await mockKnowledgeRoutes(mockedPage, {
      recent: refusal('rate_limited', { corpus: unmeasuredCorpus }),
    });

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-refusal')).toBeVisible();

    // "0 documents from 0 sources" is a sentence about a healthy mirror that
    // happens to be empty. Printing it here would send somebody to debug a
    // sync that is fine.
    const strip = mockedPage.getByTestId('knowledge-freshness');
    await expect(strip).not.toContainText('0 documents');
    await expect(strip).toContainText('not read');
  });

  test('an empty corpus is a reading, and still reports itself', async ({
    mockedPage,
  }) => {
    // The one refusal that did open the store. Zero is the measurement here,
    // so the strip states it, and the sync timestamp beside it is what tells
    // an operator the difference from the case above.
    await mockKnowledgeRoutes(mockedPage, {
      recent: refusal('corpus_empty', {
        corpus: corpus({ documents: 0, sources: 2 }),
      }),
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    const strip = mockedPage.getByTestId('knowledge-freshness');
    await expect(strip).toContainText('0 documents from 2 sources');
    await expect(strip).toContainText('last checked 8 minutes ago');
  });

  test('a server that answered with an error is not reported as unreachable', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, { searchStatus: 500 });

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('laptops');
    await mockedPage.getByTestId('knowledge-search-submit').click();

    // It answered. "Check that it is running" aimed at a process that is
    // running and returning 500s costs the same hour as sending somebody to
    // rotate a key while the server is down - the mistake one case over.
    const error = mockedPage.getByTestId('knowledge-error');
    await expect(error).toContainText('answered with an error');
    await expect(error).toContainText('500');
    await expect(error).not.toContainText('did not answer');
    await expect(error).not.toContainText('status operation');
  });

  test('the footer can be asked again, and asking spends exactly one call', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-freshness')).toContainText(
      '412 documents'
    );
    expect(calls.recents).toHaveLength(1);

    // The age the strip prints was computed by the server when it answered, so
    // it only ever climbs. Somebody watching for a sync to land needs a way to
    // ask, and a full browser reload is not a way this page suggests. One call
    // per press is the whole contract: a poll would spend a window sized for
    // an agent's turn on a tab nobody is looking at.
    await mockedPage.getByTestId('knowledge-recheck').click();
    await expect.poll(() => calls.recents.length, { timeout: 5000 }).toBe(2);

    await mockedPage.waitForTimeout(600);
    expect(calls.recents).toHaveLength(2);
    expect(calls.searches).toHaveLength(0);
  });

  test('a response missing its results does not blank the page', async ({
    mockedPage,
  }) => {
    // The twin of knowledge-console.spec.ts's missing-corpus case, and the
    // same argument: the type describes a body that was cast, not validated,
    // so a malformed 200 from a proxy should cost a region rather than the
    // page an operator is using to work out why the proxy is malformed.
    const withoutResults = {
      ...answer([workspaceResult]),
    } as Partial<KnowledgeSearchResponse>;
    delete withoutResults.results;
    await mockKnowledgeRoutes(mockedPage, {
      recent: withoutResults as KnowledgeSearchResponse,
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    await expect(
      mockedPage.getByRole('heading', { name: 'Company knowledge' })
    ).toBeVisible();
    await expect(mockedPage.getByTestId('knowledge-empty')).toBeVisible();
    await expect(mockedPage.getByTestId('knowledge-freshness')).toContainText(
      '412 documents'
    );
  });

  test('a count the server did not send is not rendered as the word undefined', async ({
    mockedPage,
  }) => {
    const withoutCount = {
      ...answer([externalResult]),
    } as Partial<KnowledgeSearchResponse>;
    delete withoutCount.external_author_count;
    await mockKnowledgeRoutes(mockedPage, {
      recent: withoutCount as KnowledgeSearchResponse,
    });

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();

    // `undefined < 1` is false, so the absent count used to reach the sentence
    // and print itself. A line about who wrote these documents that opens with
    // "undefined" is worse than no line: the badge still flags the result.
    await expect(mockedPage.getByTestId('knowledge-external-note')).toHaveCount(
      0
    );
    await expect(mockedPage.getByText('undefined of these')).toHaveCount(0);
    await expect(mockedPage.getByText('outside author')).toBeVisible();
  });

  test('there is no way to ask for the next page', async ({ mockedPage }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();

    // Scoped to the panel rather than the page: the dev server mounts its own
    // "Open Next.js Dev Tools" button, and a page-wide regex would be
    // asserting something about the harness.
    const panel = mockedPage.getByRole('tabpanel');
    await expect(
      panel.getByRole('button', { name: /more|next|page/i })
    ).toHaveCount(0);

    // The stronger half. No control could page this even if one were added,
    // because the request the panel sends carries nothing that would advance
    // it - which is the same absence the request model enforces server-side.
    for (const body of [...calls.recents, ...calls.searches]) {
      expect(Object.keys(body).sort()).not.toContain('cursor');
      expect(Object.keys(body).sort()).not.toContain('offset');
      expect(Object.keys(body).sort()).not.toContain('page');
    }
    expect(calls.recents).toHaveLength(1);
  });
});
