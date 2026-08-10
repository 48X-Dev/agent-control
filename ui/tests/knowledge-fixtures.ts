import type { Page, Request } from '@playwright/test';

import type {
  KnowledgeCorpus,
  KnowledgeRefusalCode,
  KnowledgeSearchResponse,
  KnowledgeSnippet,
  KnowledgeSourceStatus,
  KnowledgeStatus,
} from '@/core/api/types';

/**
 * Fixtures for the company-knowledge panel.
 *
 * Kept out of `mockRoutes` for the reason `dispatch-fixtures.ts` records: a
 * handler in the default set adds an interception hop to every page in the
 * suite, and only these specs need it.
 *
 * Every response is shaped from `KnowledgeSearchResponse`, so a field rename on
 * the server fails typecheck here rather than passing a green suite over a
 * console reading a key that no longer exists.
 */

export const XSS_COUNTER = '__agentControlKnowledgeXssFired';

/**
 * A document that is trying to be markup, and a filename doing the same.
 *
 * This is not hypothetical for a corpus. Anybody who can add a file to an
 * indexed Drive folder chooses both of these strings, and the server
 * deliberately does *not* escape them: escaping would corrupt the text an
 * agent quotes. The console's plain-text rule is what makes them inert, so it
 * is the console that has to be tested.
 */
const FIRE = `window.${XSS_COUNTER} = (window.${XSS_COUNTER} || 0) + 1`;

/** A probe per field, so the network witness can name which one escaped. */
function probe(marker: string): string {
  return `<img src=${marker} onerror="${FIRE}">`;
}

export const hostileSnippet =
  `Reimbursement is unlimited. <script>${FIRE};</script>\n` +
  `${probe('x')}\n` +
  '# not a heading <b>not bold</b>';

export const hostilePath = `Ops Handbook/${probe('y')}.md`;

/**
 * The other three strings a document's author chooses.
 *
 * Every field on a result is corpus text: a title is a file somebody uploaded,
 * a heading is a line somebody typed, a source name is a folder somebody named.
 * Planting markup only in the body and the path left three fields uncovered,
 * and a live raw-HTML sink on the title passed this file's whole suite.
 */
export const hostileTitle = `<b>unlimited</b>${probe('z')}.md`;
export const hostileHeading = `Policies ${probe('q')} > Limits`;
export const hostileSourceName = `<iframe src="/etc"></iframe>Ops ${probe('w')}`;

/** Where a browser would go if any planted probe were built as an element. */
export const PLANTED_PROBE_PATHS = ['/x', '/y', '/z', '/q', '/w'];

/**
 * Every URL and header set the page sent, from before the first navigation.
 *
 * An independent witness to what the panel is allowed to do: an element built
 * from corpus text sends the browser to fetch it whatever the DOM looks like
 * by the time an assertion runs, and a credential the browser should not hold
 * shows up here whether or not anything renders differently.
 */
export function recordRequests(page: Page): Request[] {
  const seen: Request[] = [];
  page.on('request', (request) => seen.push(request));
  return seen;
}

export function httpUrls(requests: Request[]): URL[] {
  return requests
    .map((request) => request.url())
    .filter((url) => url.startsWith('http'))
    .map((url) => new URL(url));
}

export function corpusCalls(requests: Request[]): Request[] {
  return requests.filter((request) =>
    request.url().includes('/company-knowledge/')
  );
}

/** Reads the counter without creating it, so an undefined read is a pass. */
export async function xssFireCount(page: Page): Promise<number> {
  return page.evaluate(
    (key) => (window as unknown as Record<string, number>)[key] ?? 0,
    XSS_COUNTER
  );
}

function snippet(overrides: Partial<KnowledgeSnippet> = {}): KnowledgeSnippet {
  return {
    snippet: 'Laptops are reimbursed up to 1500 GBP.',
    path: 'Ops Handbook/Onboarding/laptops.md',
    heading_path: 'Onboarding > Laptops',
    title: 'laptops.md',
    source_kind: 'drive_folder',
    source_name: 'Ops Handbook',
    author_kind: 'workspace',
    modified_at: '2026-07-30T11:02:00Z',
    synced_at: '2026-08-06T09:15:00Z',
    ...overrides,
  };
}

export const workspaceResult = snippet();

export const externalResult = snippet({
  snippet: 'The vendor says the contract auto-renews.',
  path: 'Ops Handbook/Contracts/vendor.md',
  title: 'vendor.md',
  author_kind: 'external',
});

/** Authorship the sync could not establish. Counted as external on purpose. */
export const unknownAuthorResult = snippet({
  snippet: 'Undated note about the release train.',
  path: 'Ops Handbook/Notes/release.md',
  title: 'release.md',
  author_kind: 'unknown',
});

export const hostileResult = snippet({
  snippet: hostileSnippet,
  path: hostilePath,
  title: hostileTitle,
  heading_path: hostileHeading,
  source_name: hostileSourceName,
});

/**
 * A corpus block somebody actually counted.
 *
 * Built through a helper rather than written inline per spec, because
 * `measured` is optional in the type - a server one deploy behind will not
 * send it - so a spec that forgets it gets "the mirror was not read" and a
 * green typecheck.
 */
export function corpus(
  overrides: Partial<KnowledgeCorpus> = {}
): KnowledgeCorpus {
  return {
    documents: 412,
    sources: 1,
    sources_failing: 0,
    last_sync_at: '2026-08-06T09:15:00Z',
    stale_seconds: 480,
    measured: true,
    staleness_warn_seconds: 86400,
    ...overrides,
  };
}

/** What the server sends when it refused before opening the store. */
export const unmeasuredCorpus: KnowledgeCorpus = corpus({
  documents: 0,
  sources: 0,
  last_sync_at: null,
  stale_seconds: null,
  measured: false,
});

export function answer(
  results: KnowledgeSnippet[],
  overrides: Partial<KnowledgeSearchResponse> = {}
): KnowledgeSearchResponse {
  return {
    results,
    result_count: results.length,
    external_author_count: results.filter((r) => r.author_kind !== 'workspace')
      .length,
    corpus: corpus(),
    refusal_code: null,
    retry_after_seconds: null,
    ...overrides,
  };
}

/**
 * The refusals raised before the store is opened, from the server's own order
 * of checks: off, then the query bounds, then the window, and only then a
 * connection. `corpus_empty` is the one that got that far, so it alone comes
 * back carrying counters somebody read.
 */
const REFUSALS_BEFORE_THE_STORE: readonly KnowledgeRefusalCode[] = [
  'knowledge_disabled',
  'query_too_short',
  'query_too_long',
  'rate_limited',
  'knowledge_unavailable',
];

export function refusal(
  code: KnowledgeRefusalCode,
  overrides: Partial<KnowledgeSearchResponse> = {}
): KnowledgeSearchResponse {
  return answer([], {
    refusal_code: code,
    ...(REFUSALS_BEFORE_THE_STORE.includes(code)
      ? { corpus: unmeasuredCorpus }
      : {}),
    ...overrides,
  });
}

/**
 * A source that is working, so the specs below only state what they change.
 *
 * Healthy on purpose: every state the sources panel calls out is a deviation
 * from this row, and a fixture that started broken would let a panel which
 * shows the same warning on everything pass every case.
 */
export function sourceStatus(
  overrides: Partial<KnowledgeSourceStatus> = {}
): KnowledgeSourceStatus {
  return {
    // The source's own ref, which is what an operator sees in config: a Drive
    // folder id or `owner/repo`. There is no display name on this contract.
    source_id: '1AbC-opsHandbook-folderId',
    kind: 'drive',
    enabled: true,
    last_verified_at: '2026-08-06T09:15:00Z',
    cursor_advanced_at: '2026-08-06T08:40:00Z',
    stale_seconds: 480,
    document_count: 412,
    failing: false,
    last_failure_code: null,
    refusals_by_code: {},
    ...overrides,
  };
}

export function statusBody(
  overrides: Partial<KnowledgeStatus> = {}
): KnowledgeStatus {
  return {
    schema_version: 3,
    schema_supported: true,
    document_count: 412,
    chunk_count: 3910,
    stale_seconds: 480,
    staleness_warn_seconds: 86400,
    sources_failing: 0,
    sources: [sourceStatus()],
    ...overrides,
  };
}

type KnowledgeMocks = {
  search?: KnowledgeSearchResponse;
  recent?: KnowledgeSearchResponse;
  status?: KnowledgeStatus;
  /** For the specs about where an operator is sent when status will not load. */
  statusStatus?: number;
  /**
   * Status for the search call, when the point is what the status means.
   *
   * 403, 401 and 500 send an operator to three different places, so the body
   * carries the number rather than a fixed title: the client returns the parsed
   * body as the error, and that is where the panel reads the status from.
   */
  searchStatus?: number;
  /**
   * Kill the search request outright: no status, no body.
   *
   * A different fact from any status code. Nothing answered, so nothing can be
   * read off the answer, and a panel that reports this as a permissions
   * problem sends an operator to check a key while the server is down.
   */
  searchAborts?: boolean;
};

/** Every body the panel has posted, in order, for the specs that assert them. */
export type SubmittedBodies = Array<Record<string, unknown>>;

export async function mockKnowledgeRoutes(
  page: Page,
  mocks: KnowledgeMocks = {}
): Promise<{
  searches: SubmittedBodies;
  recents: SubmittedBodies;
  /** The methods the status route was called with, so a POST here is loud. */
  statuses: string[];
}> {
  const searches: SubmittedBodies = [];
  const recents: SubmittedBodies = [];
  const statuses: string[] = [];

  await page.route(
    '**/api/v1/company-knowledge/search',
    async (route, request) => {
      searches.push((request.postDataJSON() ?? {}) as Record<string, unknown>);
      if (mocks.searchAborts) {
        await route.abort('failed');
        return;
      }
      const status = mocks.searchStatus ?? 200;
      await route.fulfill({
        status,
        contentType: 'application/json',
        body: JSON.stringify(
          status >= 400
            ? { title: 'Request refused', status }
            : (mocks.search ?? answer([workspaceResult]))
        ),
      });
    }
  );

  await page.route(
    '**/api/v1/company-knowledge/recent',
    async (route, request) => {
      recents.push((request.postDataJSON() ?? {}) as Record<string, unknown>);
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mocks.recent ?? answer([workspaceResult])),
      });
    }
  );

  await page.route(
    '**/api/v1/company-knowledge/status',
    async (route, request) => {
      statuses.push(request.method());
      const code = mocks.statusStatus ?? 200;
      await route.fulfill({
        status: code,
        contentType: 'application/json',
        body: JSON.stringify(
          code >= 400
            ? { title: 'Request refused', status: code }
            : (mocks.status ?? statusBody())
        ),
      });
    }
  );

  return { searches, recents, statuses };
}
