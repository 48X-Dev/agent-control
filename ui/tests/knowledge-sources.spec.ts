import { expect, test } from './fixtures';
import {
  mockKnowledgeRoutes,
  recordRequests,
  sourceStatus,
  statusBody,
} from './knowledge-fixtures';

/**
 * The sources panel: what an operator is told when the mirror stops moving.
 *
 * Every case here starts from a healthy source and changes one thing, because
 * the states this panel exists for all read as success in a row of counts. A
 * panel that warned about everything would pass each case below on its own.
 */

const KNOWLEDGE_URL = '/knowledge';

async function openSources(page: import('@playwright/test').Page) {
  await page.goto(KNOWLEDGE_URL);
  await page.getByTestId('knowledge-tab-sources').click();
  await expect(page.getByTestId('knowledge-sources')).toBeVisible();
}

test.describe('Company knowledge sources: the states that look like success', () => {
  /**
   * A missing environment passthrough and an empty folder produce the same
   * number. The panel cannot tell them apart either, and says so instead of
   * printing a zero somebody reads as "indexed, nothing to find".
   */
  test('a source holding nothing is called out, not printed as a zero', async ({
    mockedPage,
  }) => {
    // The server's own shape for this: `failing` set, and no failure code,
    // because the sync never recorded one. The state is the two counts, and it
    // must land as the sentence rather than as red.
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        document_count: 0,
        chunk_count: 0,
        sources_failing: 1,
        sources: [
          sourceStatus({
            document_count: 0,
            failing: true,
            last_failure_code: null,
          }),
        ],
      }),
    });

    await openSources(mockedPage);

    const row = mockedPage.getByTestId('knowledge-source-0');
    await expect(row.getByTestId('knowledge-source-empty')).toContainText(
      'never reached the sync'
    );
    await expect(row.getByTestId('knowledge-source-failing')).toHaveCount(0);
    await expect(row.getByTestId('knowledge-source-partial')).toHaveCount(0);
    await expect(row.getByTestId('knowledge-source-meta')).toContainText(
      'nothing indexed'
    );
    await expect(row.getByTestId('knowledge-source-meta')).not.toContainText(
      '0 document'
    );

    // And the header counter cannot call an empty folder a failed sync.
    const summary = mockedPage.getByTestId('knowledge-sources-summary');
    await expect(summary).toContainText('1 of 1 needs attention');
    await expect(summary).not.toContainText('failing');
  });

  /**
   * The code is the string an operator greps the sync log for, so it is on the
   * page beside the sentence rather than replaced by it. The second row is a
   * code this console has never heard of: a server one deploy ahead is the
   * ordinary case, and swallowing the code there would be the worst time to.
   */
  test('a failing source shows its failure code, known or not', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        sources_failing: 2,
        sources: [
          sourceStatus({
            failing: true,
            last_failure_code: 'root_unreachable',
          }),
          sourceStatus({
            source_id: 'engineering-docs',
            kind: 'github',
            failing: true,
            last_failure_code: 'corpus_on_fire',
          }),
        ],
      }),
    });

    await openSources(mockedPage);

    const known = mockedPage
      .getByTestId('knowledge-source-0')
      .getByTestId('knowledge-source-failing');
    await expect(known).toContainText('did not resolve');
    await expect(known).toContainText('root_unreachable');

    const unknown = mockedPage
      .getByTestId('knowledge-source-1')
      .getByTestId('knowledge-source-failing');
    await expect(unknown).toContainText('corpus_on_fire');

    await expect(
      mockedPage.getByTestId('knowledge-sources-summary')
    ).toContainText('2 of 2 need attention');
  });

  /**
   * A run that finished and recorded an error arrives as a code with `failing`
   * false. Calling that a stopped source would send somebody to restart a sync
   * that is running; losing the code would hide the only clue they have.
   */
  test('a run that finished with an error is reported without being called stopped', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        sources: [
          sourceStatus({
            failing: false,
            last_failure_code: 'unreadable_folder',
          }),
        ],
      }),
    });

    await openSources(mockedPage);

    const partial = mockedPage.getByTestId('knowledge-source-partial');
    await expect(partial).toContainText('finished but recorded an error');
    await expect(partial).toContainText('unreadable_folder');
    await expect(partial).not.toContainText('stopped syncing');
    await expect(
      mockedPage.getByTestId('knowledge-source-failing')
    ).toHaveCount(0);
  });

  /**
   * Corpus-wide, and it outranks every row: while the sync and the reader
   * disagree about the schema, every search answers `knowledge_unavailable`
   * however healthy the sources underneath look. So it sits above them.
   */
  test('a schema the reader and the sync disagree about is stated above the rows', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({ schema_supported: false, schema_version: 2 }),
    });

    await openSources(mockedPage);

    const banner = mockedPage.getByTestId('knowledge-schema-mismatch');
    await expect(banner).toContainText('every search');
    await expect(banner).toContainText('version 2');
    await expect(banner).toContainText('migrations');

    const first = mockedPage
      .locator(
        '[data-testid="knowledge-schema-mismatch"], [data-testid="knowledge-source-0"]'
      )
      .first();
    await expect(first).toHaveAttribute(
      'data-testid',
      'knowledge-schema-mismatch'
    );
  });

  /**
   * The same pair of fields carries a second, different failure: no version at
   * all means the store was never read, and "run the migrations" is the wrong
   * instruction for a store nobody could connect to. The empty source list that
   * comes with it must not read as "nobody has configured one" either.
   */
  test('a corpus nobody could read says so, not that its version is wrong', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        schema_supported: false,
        schema_version: null,
        sources: [],
      }),
    });

    await openSources(mockedPage);

    const banner = mockedPage.getByTestId('knowledge-schema-mismatch');
    await expect(banner).toContainText('could not read the knowledge store');
    await expect(banner).toContainText('DSN');
    await expect(banner).not.toContainText('migrations');
    await expect(mockedPage.getByTestId('knowledge-sources-none')).toHaveCount(
      0
    );
  });

  /**
   * The state before the sync has ever run. Nothing is wrong, so nothing here
   * may look like an error - an operator who reads a fresh install as a broken
   * one goes and debugs a component that has simply not started yet.
   */
  test('no sources configured reads as nothing configured, not as broken', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        sources: [],
        document_count: 0,
        chunk_count: 0,
        stale_seconds: null,
      }),
    });

    await openSources(mockedPage);

    await expect(
      mockedPage.getByTestId('knowledge-sources-none')
    ).toContainText('No source has been configured');
    await expect(mockedPage.getByTestId('knowledge-source-0')).toHaveCount(0);
    await expect(mockedPage.getByTestId('knowledge-error')).toHaveCount(0);
    await expect(
      mockedPage.getByTestId('knowledge-schema-mismatch')
    ).toHaveCount(0);
    await expect(
      mockedPage.getByTestId('knowledge-sources-summary')
    ).toContainText('never checked');
  });

  /**
   * The threshold is the deployment's, not this console's, and the comparison
   * is done here rather than left as two numbers beside each other. The second
   * source is inside the same threshold, which is what makes the first mean
   * something.
   */
  test('a source past the deployment threshold warns; one inside it does not', async ({
    mockedPage,
  }) => {
    const threeDays = 3 * 24 * 60 * 60;
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        stale_seconds: threeDays,
        staleness_warn_seconds: 24 * 60 * 60,
        sources: [
          sourceStatus({ stale_seconds: threeDays }),
          sourceStatus({ source_id: 'engineering-docs', stale_seconds: 480 }),
        ],
      }),
    });

    await openSources(mockedPage);

    const stale = mockedPage
      .getByTestId('knowledge-source-0')
      .getByTestId('knowledge-source-stale');
    await expect(stale).toContainText('3 days');
    await expect(stale).toContainText('1 day');

    await expect(
      mockedPage
        .getByTestId('knowledge-source-1')
        .getByTestId('knowledge-source-stale')
    ).toHaveCount(0);
  });

  /**
   * A standing total of what is out of the corpus today, which is what the
   * server counts. Labelling it as the last run's refusals would be plainly
   * wrong: these documents were excluded on some earlier run and still are.
   */
  test('excluded documents are counted and named, not called a last-run tally', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        sources: [
          sourceStatus({
            refusals_by_code: { oversize: 3, secret_file: 1, unheard_of: 2 },
          }),
        ],
      }),
    });

    await openSources(mockedPage);

    const excluded = mockedPage.getByTestId('knowledge-source-excluded');
    await expect(excluded).toContainText('Currently excluded');
    await expect(excluded).toContainText('3 over the size ceiling (oversize)');
    await expect(excluded).toContainText('1 named like a secret (secret_file)');
    await expect(excluded).toContainText('2 excluded as unheard_of');
    await expect(excluded).not.toContainText('last run');
  });

  /**
   * The absent half of all three warnings above. A panel that shows them on a
   * working source is furniture within a day, including on the day one of them
   * is true.
   */
  test('a working source is quiet', async ({ mockedPage }) => {
    await mockKnowledgeRoutes(mockedPage);

    await openSources(mockedPage);

    const row = mockedPage.getByTestId('knowledge-source-0');
    await expect(row.getByTestId('knowledge-source-meta')).toContainText(
      'synced 8 minutes ago'
    );
    await expect(row.getByTestId('knowledge-source-meta')).toContainText(
      '412 documents'
    );
    // The ref is the only human-readable label the contract carries, so it is
    // the row's name rather than something hidden behind a prettier one.
    await expect(row).toContainText('1AbC-opsHandbook-folderId');

    await expect(row.getByTestId('knowledge-source-empty')).toHaveCount(0);
    await expect(row.getByTestId('knowledge-source-failing')).toHaveCount(0);
    await expect(row.getByTestId('knowledge-source-partial')).toHaveCount(0);
    await expect(row.getByTestId('knowledge-source-stale')).toHaveCount(0);
    await expect(row.getByTestId('knowledge-source-excluded')).toHaveCount(0);
    await expect(
      mockedPage.getByTestId('knowledge-schema-mismatch')
    ).toHaveCount(0);
  });
});

test.describe('Company knowledge sources: what the panel is not', () => {
  /**
   * The refused button, asserted rather than written down. Linking is a
   * one-time act at the command line; a control here would move a live source
   * credential into Postgres, where every backup and `pg_dump` carries it.
   */
  test('nothing on this panel writes, and there is no connect control', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, {
      status: statusBody({
        sources_failing: 1,
        sources: [
          sourceStatus({
            failing: true,
            last_failure_code: 'root_unreachable',
          }),
        ],
      }),
    });

    await openSources(mockedPage);

    // A failing source is where a "reconnect" button would be proposed first,
    // so this is the fixture the refusal is asserted against.
    const panel = mockedPage.getByRole('tabpanel');
    await expect(panel.getByRole('button')).toHaveCount(0);
    await expect(panel.locator('input, textarea, select, form')).toHaveCount(0);
    await expect(
      mockedPage.getByTestId('knowledge-sources-readonly')
    ).toContainText('no button for it here');
  });

  /**
   * Read on opening the tab and not before, and not again for a full poll
   * interval. This route is the one company-knowledge call the server does not
   * rate limit, so nothing but this would stop a tab left open all afternoon.
   */
  test('the status is read on opening the tab, as a GET carrying no key', async ({
    mockedPage,
  }) => {
    const requests = recordRequests(mockedPage);
    const calls = await mockKnowledgeRoutes(mockedPage);

    await mockedPage.goto(KNOWLEDGE_URL);
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    expect(calls.statuses).toHaveLength(0);

    await mockedPage.getByTestId('knowledge-tab-sources').click();
    await expect(mockedPage.getByTestId('knowledge-sources')).toBeVisible();
    expect(calls.statuses).toEqual(['GET']);

    await mockedPage.getByTestId('knowledge-tab-recent').click();
    await expect(mockedPage.getByTestId('knowledge-result-0')).toBeVisible();
    await mockedPage.getByTestId('knowledge-tab-sources').click();
    await expect(mockedPage.getByTestId('knowledge-sources')).toBeVisible();

    // Longer than any cadence somebody would reach for by hand, and far short
    // of the interval this actually polls on.
    await mockedPage.waitForTimeout(1500);
    expect(calls.statuses).toEqual(['GET']);

    const sent = requests.filter((request) =>
      request.url().includes('/company-knowledge/status')
    );
    expect(sent.length).toBeGreaterThan(0);
    for (const call of sent) {
      expect(new URL(call.url()).search).toBe('');
      const headers = call.headers();
      expect(Object.keys(headers)).not.toContain('authorization');
      expect(Object.keys(headers)).not.toContain('x-api-key');
    }
  });

  /**
   * A server that answered with an error answered, so the fix is in its log.
   * The panel says which of the three it was rather than rendering an empty
   * table that reads as "no sources configured".
   */
  test('a status the server refused is an error, not an empty source list', async ({
    mockedPage,
  }) => {
    await mockKnowledgeRoutes(mockedPage, { statusStatus: 500 });

    await mockedPage.goto(KNOWLEDGE_URL);
    await mockedPage.getByTestId('knowledge-tab-sources').click();

    await expect(mockedPage.getByTestId('knowledge-error')).toContainText(
      'status 500'
    );
    await expect(mockedPage.getByTestId('knowledge-sources-none')).toHaveCount(
      0
    );
  });
});
