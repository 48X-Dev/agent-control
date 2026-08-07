/**
 * The freshness strip's arithmetic, at the level where it is arithmetic.
 *
 * Two of these facts cannot be asserted honestly from a browser test. The age
 * only ticks every thirty seconds, so proving it climbs would cost the suite
 * thirty seconds of waiting; and the staleness threshold decides a text colour,
 * which is a brittle thing to assert and a weak thing to prove. Both are pure
 * functions of a response, so they are tested as pure functions of a response.
 *
 * These live under tests/ct because that is where the runner with no Next.js
 * server is. They mount nothing and need no browser.
 */

import { expect, test } from '@playwright/experimental-ct-react';

import type {
  KnowledgeCorpus,
  KnowledgeSearchResponse,
} from '../../src/core/api/types';
import {
  externalAuthorNote,
  freshnessStrip,
} from '../../src/core/page-components/knowledge/formatting';

const HOUR = 60 * 60;

function response(
  corpus: Partial<KnowledgeCorpus> | null,
  overrides: Partial<KnowledgeSearchResponse> = {}
): KnowledgeSearchResponse {
  const body: Partial<KnowledgeSearchResponse> = {
    results: [],
    result_count: 0,
    external_author_count: 0,
    refusal_code: null,
    ...overrides,
  };
  if (corpus) {
    body.corpus = {
      documents: 412,
      sources: 1,
      sources_failing: 0,
      last_sync_at: '2026-08-06T09:15:00Z',
      stale_seconds: 480,
      measured: true,
      staleness_warn_seconds: 24 * HOUR,
      ...corpus,
    };
  }
  return body as KnowledgeSearchResponse;
}

/** Narrows to the branch that reports, so a regression fails on the assert. */
function reading(strip: ReturnType<typeof freshnessStrip>) {
  if (!strip || !strip.read) {
    throw new Error('expected a strip that read the corpus');
  }
  return strip;
}

test.describe('freshnessStrip: whether anybody counted', () => {
  test('a corpus block nobody measured reports nothing', () => {
    // Every counter here is a number, and every one of them is a default the
    // model supplied so the block would be present for a deny control. A strip
    // that printed them would say "0 documents from 0 sources" under a
    // rate-limit refusal, which is a sentence about a broken sync.
    const strip = freshnessStrip(
      response(
        { documents: 0, sources: 0, stale_seconds: null, measured: false },
        { refusal_code: 'rate_limited' }
      )
    );

    expect(strip).toEqual({ read: false });
  });

  test('an empty corpus somebody did measure reports itself', () => {
    const strip = reading(
      freshnessStrip(
        response(
          { documents: 0, sources: 2, measured: true },
          { refusal_code: 'corpus_empty' }
        )
      )
    );

    expect(strip.documents).toBe('0 documents from 2 sources');
  });

  test('a refusal that measured is not judged by its code', () => {
    // The console used to keep a list of which refusal codes had read the
    // store. That list was a copy of the server's internals held in a browser,
    // and it went wrong silently in both directions. The flag is on the wire
    // now, so a code this console has never seen is still read correctly.
    const strip = reading(
      freshnessStrip(
        response({ documents: 7, measured: true }, {
          refusal_code: 'corpus_quarantined',
        } as unknown as Partial<KnowledgeSearchResponse>)
      )
    );

    expect(strip.documents).toBe('7 documents from 1 source');
  });

  test('a body with no corpus block at all reports nothing', () => {
    expect(freshnessStrip(response(null))).toEqual({ read: false });
  });

  test('a server too old to send the flag is not taken at face value', () => {
    // Saying less than it could beats saying more than it knows.
    const strip = freshnessStrip(
      response({ measured: undefined as unknown as boolean })
    );

    expect(strip).toEqual({ read: false });
  });
});

test.describe('freshnessStrip: the age keeps counting', () => {
  test('time on the page is added to the age the server computed', () => {
    // `stale_seconds` was measured when the request was answered. A tab open
    // for an hour reports an hour-old measurement as current without this.
    const fresh = reading(freshnessStrip(response({ stale_seconds: 59 }), 0));
    const later = reading(freshnessStrip(response({ stale_seconds: 59 }), 120));

    expect(fresh.age).toBe('under a minute');
    expect(later.age).toBe('2 minutes');
  });

  test('an age the server could not compute stays uncomputable', () => {
    const strip = reading(
      freshnessStrip(response({ stale_seconds: null }), 3600)
    );

    expect(strip.age).toBeNull();
    // Never verified is not "verified a while ago", so no arithmetic applies.
    expect(strip.stale).toBe(false);
  });
});

test.describe('freshnessStrip: whose threshold decides', () => {
  test('the deployment sets when an age becomes a warning', () => {
    const strict = reading(
      freshnessStrip(
        response({ stale_seconds: 2 * HOUR, staleness_warn_seconds: HOUR })
      )
    );
    const relaxed = reading(
      freshnessStrip(
        response({ stale_seconds: 2 * HOUR, staleness_warn_seconds: 24 * HOUR })
      )
    );

    // Same age, two deployments. An operator who sets the warning to six hours
    // because a six-hour-old mirror matters to them changes what this colours.
    expect(strict.stale).toBe(true);
    expect(relaxed.stale).toBe(false);
  });

  test('sitting on the page can carry an age past the threshold', () => {
    const arrived = reading(
      freshnessStrip(
        response({ stale_seconds: HOUR - 100, staleness_warn_seconds: HOUR })
      )
    );
    const waited = reading(
      freshnessStrip(
        response({ stale_seconds: HOUR - 100, staleness_warn_seconds: HOUR }),
        200
      )
    );

    expect(arrived.stale).toBe(false);
    expect(waited.stale).toBe(true);
  });

  test('a server too old to send a threshold falls back to a day', () => {
    const underADay = reading(
      freshnessStrip(
        response({
          stale_seconds: 23 * HOUR,
          staleness_warn_seconds: undefined as unknown as number,
        })
      )
    );
    const overADay = reading(
      freshnessStrip(
        response({
          stale_seconds: 25 * HOUR,
          staleness_warn_seconds: undefined as unknown as number,
        })
      )
    );

    expect(underADay.stale).toBe(false);
    expect(overADay.stale).toBe(true);
  });
});

test.describe('externalAuthorNote', () => {
  test('a count the server did not send is not a sentence', () => {
    // `undefined < 1` is false, so an absent count used to reach the template
    // and render the literal word. A line about provenance that opens with
    // "undefined" is worse than no line.
    const note = externalAuthorNote(
      response({}, {
        external_author_count: undefined,
      } as Partial<KnowledgeSearchResponse>)
    );

    expect(note).toBeNull();
  });

  test('one outside author reads as one, not as a plural', () => {
    const note = externalAuthorNote(response({}, { external_author_count: 1 }));

    expect(note).toContain('1 of these was');
    expect(note).toContain('who wrote it.');
  });
});
