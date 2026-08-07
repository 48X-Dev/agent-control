import type {
  KnowledgeSearchResponse,
  KnowledgeSnippet,
} from '@/core/api/types';

import {
  expect,
  mockApiRoutesWithAuthRequired,
  mockRoutes,
  test,
} from './fixtures';
import {
  answer,
  corpusCalls,
  hostileResult,
  httpUrls,
  mockKnowledgeRoutes,
  PLANTED_PROBE_PATHS,
  recordRequests,
  workspaceResult,
  XSS_COUNTER,
  xssFireCount,
} from './knowledge-fixtures';

/**
 * The knowledge panel's boundaries, as opposed to its reading.
 *
 * `knowledge.spec.ts` covers what the panel says. This file covers what it is
 * allowed to do: which credential opens it, what leaves the browser, what the
 * request may ask for, and what a document's author can make the DOM build.
 * Those are the plan's properties rather than the panel's copy, so they are
 * asserted the way the wire tests assert theirs - by absence, with a live half
 * beside each absent half so a passing silence still means something.
 */

const KNOWLEDGE_URL = '/knowledge';
const API_KEY_PROMPT = 'Enter your API key to continue.';

/**
 * Budget for the first result after a sign-in, which is the slowest path here:
 * a login round trip, then a route the local dev server may still be
 * compiling, with every other worker asking for it at the same moment. The
 * default 5s is not a considered number for that, and writing 5000 by hand
 * only made it look like one.
 */
const SIGNED_IN_FIRST_PAINT_MS = 15_000;

function result(overrides: Partial<KnowledgeSnippet>): KnowledgeSnippet {
  return { ...workspaceResult, ...overrides };
}

test.describe('Company knowledge panel: which credential opens it', () => {
  /**
   * The header provider's half of the matrix.
   *
   * `company_knowledge.status` is AUTHENTICATED there, and the browser's only
   * credential is the console session. So the interesting assertion is the one
   * before signing in: not that the server would refuse, but that the panel
   * never asks. A page that fetched behind the login modal would be spending a
   * metered window on somebody who has not proved who they are, and relying on
   * the server alone to notice.
   *
   * The half after signing in is what makes the first half mean something. The
   * mocks are live throughout; the only thing that changed is the session.
   */
  test('nothing is read from the corpus until the console has a session', async ({
    page,
  }) => {
    await mockApiRoutesWithAuthRequired(page);
    await mockRoutes.login(page, { authenticated: true, is_admin: false });
    const calls = await mockKnowledgeRoutes(page);

    await page.goto(KNOWLEDGE_URL);
    await expect(page.getByText(API_KEY_PROMPT)).toBeVisible();

    await expect(
      page.getByRole('heading', { name: 'Company knowledge' })
    ).toHaveCount(0);
    await expect(page.getByTestId('knowledge-result-0')).toHaveCount(0);
    expect(calls.recents).toHaveLength(0);
    expect(calls.searches).toHaveLength(0);

    await page.getByPlaceholder('Enter your API key').fill('a-real-key');
    await page.getByRole('button', { name: 'Sign in' }).click();

    await expect(page.getByTestId('knowledge-result-0')).toBeVisible({
      timeout: SIGNED_IN_FIRST_PAINT_MS,
    });
    expect(calls.recents).toHaveLength(1);
  });

  /**
   * The other provider, where every operation succeeds and there is no caller
   * identity at all. The panel has to be right in both configurations, and the
   * failure mode here is the panel inventing an authorization story: a key
   * prompt, or a permissions error, on a deployment that has no keys.
   */
  test('with the key requirement off, the panel loads and nobody is asked for a key', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);

    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    await expect(mockedPage.getByText(API_KEY_PROMPT)).toHaveCount(0);
    await expect(mockedPage.getByTestId('knowledge-error')).toHaveCount(0);
    expect(calls.recents).toHaveLength(1);
  });

  /**
   * 401 and 403 are not the same sentence and not the same fix.
   *
   * A 403 means this key does not carry the status operation, and the panel
   * says so in a red box. A 401 means the console session lapsed - the fix is
   * to sign in again, and telling somebody their key lacks an operation while
   * the real problem is an expired cookie sends them to change a grant that is
   * already correct.
   */
  test('a session that lapsed mid-search asks for a key again, not for a better one', async ({
    page,
  }) => {
    await mockApiRoutesWithAuthRequired(page);
    await mockRoutes.login(page, { authenticated: true, is_admin: false });
    await mockKnowledgeRoutes(page, { searchStatus: 401 });

    await page.goto(KNOWLEDGE_URL);
    await page.getByPlaceholder('Enter your API key').fill('a-real-key');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByTestId('knowledge-result-0')).toBeVisible({
      timeout: SIGNED_IN_FIRST_PAINT_MS,
    });

    await page.getByTestId('knowledge-tab-search').click();
    await page.getByTestId('knowledge-search-input').fill('laptops');
    await page.getByTestId('knowledge-search-submit').click();

    await expect(page.getByText(API_KEY_PROMPT)).toBeVisible();
    await expect(page.getByTestId('knowledge-error')).toHaveCount(0);
  });
});

test.describe('Company knowledge panel: what leaves the browser', () => {
  /**
   * Both halves of one rule, asserted on the wire rather than in the client.
   *
   * The question goes in the body because a search somebody typed does not
   * belong in a URL that lands in a proxy log or a browser history - "what is
   * our severance policy" is a sentence about a person as often as about a
   * policy. And no key rides in a header: the console authenticates with the
   * cookie the login route set, so a key here would mean the browser is
   * holding a credential it has no business holding.
   */
  test('the question travels in the body, and no key travels in a header', async ({
    mockedPage,
  }) => {
    const requests = recordRequests(mockedPage);
    await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage
      .getByTestId('knowledge-search-input')
      .fill('severance terms for the redundancy round');

    // Waiting on the request rather than on a rendered result, deliberately.
    // A URL this test disapproves of is a URL the fixture's route glob stops
    // matching, and then the panel renders an error and every assertion below
    // passes on a page that never searched.
    const sent = mockedPage.waitForRequest((request) =>
      request.url().includes('/company-knowledge/search')
    );
    await mockedPage.getByTestId('knowledge-search-submit').click();
    await sent;

    const calls = corpusCalls(requests);
    expect(calls.length).toBeGreaterThan(0);
    for (const call of calls) {
      expect(call.method()).toBe('POST');
      expect(new URL(call.url()).search).toBe('');
      expect(call.url()).not.toContain('redundancy');

      const headers = call.headers();
      expect(Object.keys(headers)).not.toContain('authorization');
      expect(Object.keys(headers)).not.toContain('x-api-key');
    }
  });

  /**
   * Proof by absence, with the network as the witness.
   *
   * Section 2.2's matrix keeps the Drive and GitHub credentials in the sync
   * process; the browser holds the console session and nothing else, and it
   * reaches exactly one origin. The day somebody renders a Drive thumbnail
   * into a result, the credential separation stops being a diagram - and the
   * corpus path travels to Google in the query string on the way.
   *
   * The same recording witnesses the rendering rule independently: the hostile
   * fixture carries one probe per corpus-authored field, and an element built
   * from any of them sends the browser to fetch it, whatever the DOM looks
   * like by the time an assertion runs.
   */
  test('no request leaves for a source host, and a planted image is never fetched', async ({
    mockedPage,
  }) => {
    const requests = recordRequests(mockedPage);
    await mockKnowledgeRoutes(mockedPage, { recent: answer([hostileResult]) });

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(
      mockedPage.getByTestId('knowledge-result-snippet')
    ).toBeVisible();

    const urls = httpUrls(requests);
    expect(urls.length).toBeGreaterThan(0);

    const offsite = urls
      .filter((url) =>
        /googleapis|googleusercontent|drive\.google|github/i.test(url.host)
      )
      .map((url) => url.href);
    expect(offsite).toEqual([]);

    const planted = urls
      .filter((url) => PLANTED_PROBE_PATHS.includes(url.pathname))
      .map((url) => url.href);
    expect(planted).toEqual([]);
  });
});

test.describe('Company knowledge panel: the ceilings a person cannot raise', () => {
  /**
   * The console is a second door onto a metered corpus, so it asks inside the
   * same bounds the agents' surface has: `max_results` no higher than the hard
   * cap of 8, `days` no wider than the 14-day window. The server clamps either
   * way - this is about the console not asking, because a panel that requests
   * 500 is a panel somebody writes a paging control for next.
   */
  test('the console asks for one page inside the ceilings and cannot ask past them', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('laptops');
    await mockedPage.getByTestId('knowledge-search-submit').click();
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();

    expect(calls.recents).toHaveLength(1);
    expect(calls.searches).toHaveLength(1);

    for (const body of [...calls.recents, ...calls.searches]) {
      expect(body.max_results as number).toBeLessThanOrEqual(8);
      expect(body.max_results as number).toBeGreaterThan(0);
    }
    expect(calls.recents[0].days as number).toBeLessThanOrEqual(14);
  });

  /**
   * A corpus does not move between two presses of the same button, and every
   * press spends from a sliding window six calls a minute wide. So the repeat
   * is cached and the allowance stays with the question somebody meant to ask
   * next - the third press here, which is also what proves the second was
   * cached rather than merely slow.
   */
  test('asking the same question twice spends one call; a different question spends another', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('laptops');
    await mockedPage.getByTestId('knowledge-search-submit').click();
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    expect(calls.searches).toHaveLength(1);

    await mockedPage.getByTestId('knowledge-search-submit').click();
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    expect(calls.searches).toHaveLength(1);

    await mockedPage.getByTestId('knowledge-search-input').fill('monitors');
    await mockedPage.getByTestId('knowledge-search-submit').click();
    await expect.poll(() => calls.searches.length, { timeout: 5000 }).toBe(2);
  });

  /**
   * The mirror is read once, and neither clicking about nor waiting reads it
   * again. Two ways to lose that: the inactive tab unmounts rather than
   * hiding, so every tab click is a fresh mount, and a freshness footer is
   * exactly the component somebody decides to keep live with a poll. A console
   * tab left open all afternoon would then spend a window sized for an agent's
   * turn, on nobody.
   */
  test('the mirror is read once, and neither tab churn nor time spends another call', async ({
    mockedPage,
  }) => {
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();

    for (let round = 0; round < 2; round += 1) {
      await mockedPage.getByTestId('knowledge-tab-search').click();
      await expect(
        mockedPage.getByTestId('knowledge-search-input')
      ).toBeVisible();
      await mockedPage.getByTestId('knowledge-tab-recent').click();
      await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    }

    // Proving nothing happens over an interval costs the interval. Short, but
    // longer than any polling cadence anybody would plausibly reach for.
    await mockedPage.waitForTimeout(600);

    expect(calls.recents).toHaveLength(1);
    expect(calls.searches).toHaveLength(0);
  });
});

test.describe('Company knowledge panel: corpus text in a browser', () => {
  /**
   * The wire test for the model-visible path plants its markers in the body,
   * the filename and the heading, because all three are strings a document's
   * author picked. The console's version of that is every field on a result,
   * for the same reason: a source name is a folder somebody named, a title is
   * a file somebody uploaded, and none of them arrive escaped - the server
   * deliberately does not escape, because escaping would corrupt the text an
   * agent quotes back to a person.
   */
  test('every field the corpus controls arrives as text, not as markup', async ({
    mockedPage,
  }) => {
    const everywhere = result({
      title: `<b>invoice</b><script>window.${XSS_COUNTER}=1</script>.docx`,
      source_name: '<iframe src="/etc"></iframe>Finance',
      heading_path: '<img src=x onerror="alert(1)"> > Rates',
      path: 'Finance/<svg onload="alert(1)">/rates.md',
      snippet: 'Rates are <b>fixed</b> until <i>April</i>.',
    });
    await mockKnowledgeRoutes(mockedPage, { recent: answer([everywhere]) });

    await mockedPage.goto(KNOWLEDGE_URL);
    const card = mockedPage.getByTestId('knowledge-result-0');
    await expect(card).toBeVisible();

    // Present as characters, which is the point: nothing was stripped, and a
    // person reading this sees what the document actually says.
    await expect(card).toContainText('<b>invoice</b>');
    await expect(card).toContainText('<iframe');
    await expect(card).toContainText('<img src=x');
    await expect(card).toContainText('<svg onload');
    await expect(card).toContainText('<b>fixed</b>');

    // And absent as nodes, in every field at once.
    await expect(card.locator('b, i, script, img, iframe, svg')).toHaveCount(0);
    expect(await xssFireCount(mockedPage)).toBe(0);
  });

  /**
   * There is no link per result today: the corpus schema carries no URL, and a
   * Drive link guessed from a path is a link that mostly 404s. The text is
   * still full of things a helpful component would linkify, one of them a
   * `javascript:` scheme. When a real URL column exists, this is what forces
   * the link through `safeHttpUrl` rather than straight into an `href`.
   */
  test('a snippet that looks like a link does not become one', async ({
    mockedPage,
  }) => {
    const linky = result({
      snippet:
        'Full terms at https://not-our-domain.example/terms and javascript:alert(1)',
      path: 'Ops Handbook/<a href="https://not-our-domain.example">terms</a>.md',
      heading_path: null,
    });
    await mockKnowledgeRoutes(mockedPage, { recent: answer([linky]) });

    await mockedPage.goto(KNOWLEDGE_URL);
    const panel = mockedPage.getByRole('tabpanel');
    await expect(
      mockedPage.getByTestId('knowledge-result-snippet')
    ).toContainText('not-our-domain.example');

    await expect(panel.locator('a')).toHaveCount(0);
    await expect(panel.locator('[href]')).toHaveCount(0);
  });

  /**
   * The absent half of the outside-author note. The warning is only worth
   * anything if it is quiet when there is nothing to warn about; a note that
   * appeared on every page would be read as furniture within a day, including
   * on the page where two of the five results came from outside the workspace.
   */
  test('nothing is said about outside authors when every result came from inside', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      recent: answer([workspaceResult, result({ title: 'expenses.md' })]),
    });

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-result-1')).toBeVisible();

    await expect(mockedPage.getByTestId('knowledge-external-note')).toHaveCount(
      0
    );
    await expect(mockedPage.getByText('outside author')).toHaveCount(0);
    await expect(mockedPage.getByText('author unknown')).toHaveCount(0);
  });
});

test.describe('Company knowledge panel: answers that are not results', () => {
  /**
   * A gap is a finding, the same thing the tool tells a model when a search
   * comes back empty. The person needs the second half too: how big the thing
   * that found nothing is. "Nothing matched" over a 412-document mirror and
   * "nothing matched" over a mirror holding one folder are different answers
   * to the same query, and only the strip tells them apart.
   */
  test('an empty answer is a finding, and the strip still says how big the mirror is', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, { search: answer([]) });

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-search').click();
    await mockedPage.getByTestId('knowledge-search-input').fill('sabbatical');
    await mockedPage.getByTestId('knowledge-search-submit').click();

    await expect(mockedPage.getByTestId('knowledge-empty')).toContainText(
      'the query ran and found nothing'
    );
    await expect(mockedPage.getByTestId('knowledge-error')).toHaveCount(0);
    await expect(mockedPage.getByTestId('knowledge-refusal')).toHaveCount(0);
    await expect(mockedPage.getByTestId('knowledge-freshness')).toContainText(
      '412 documents'
    );
  });

  /**
   * The refusal enum is closed on the server and closed in the console's type,
   * and those two closures are versioned separately. A server one deploy ahead
   * is the ordinary case, not the exotic one, and the panel's job then is to
   * say something a person can act on rather than print a machine word or
   * render nothing at all where a sentence should be.
   */
  test('a refusal code this console has never seen still gets a sentence', async ({
    mockedPage,
  }) => {
    const unknown = {
      ...answer([]),
      refusal_code: 'corpus_quarantined',
    } as unknown as KnowledgeSearchResponse;
    await mockKnowledgeRoutes(mockedPage, { recent: unknown });

    await mockedPage.goto(KNOWLEDGE_URL);

    const stated = mockedPage.getByTestId('knowledge-refusal');
    await expect(stated).toContainText('did not say why');
    await expect(stated).not.toContainText('corpus_quarantined');
  });

  /**
   * The footer is the smallest thing on the page and it reads a block the type
   * says is always there - a type describing a body that was cast, not
   * validated. A malformed response should cost the strip, not the page: an
   * operator whose server is answering strangely is exactly the person who
   * still needs the results and the tabs.
   */
  test('a response missing its corpus block does not take the page down', async ({
    mockedPage,
  }) => {
    const withoutCorpus = {
      ...answer([workspaceResult]),
    } as Partial<KnowledgeSearchResponse>;
    delete withoutCorpus.corpus;
    await mockKnowledgeRoutes(mockedPage, {
      recent: withoutCorpus as KnowledgeSearchResponse,
    });

    await mockedPage.goto(KNOWLEDGE_URL);

    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    await expect(mockedPage.getByTestId('knowledge-freshness')).toContainText(
      'not read'
    );
    await expect(
      mockedPage.getByRole('heading', { name: 'Company knowledge' })
    ).toBeVisible();
  });
});
