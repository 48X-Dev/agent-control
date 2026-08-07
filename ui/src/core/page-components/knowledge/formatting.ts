/**
 * Sentences and small formatting for the Knowledge panel.
 *
 * Every string that passes through here is written by hand, the same
 * discipline the server's refusal enum follows: a code is a machine fact and a
 * person reads a sentence. Nothing here produces markup, and nothing here
 * formats corpus text - a snippet reaches the page as it left the server and
 * is rendered as a text node.
 *
 * The sentences are the console's own, deliberately not the tool's. The tool
 * tells a model what to do next ("say plainly that you could not check the
 * company documents"); an operator needs to know what to go and fix.
 */

import type {
  KnowledgeRefusalCode,
  KnowledgeSearchResponse,
} from '@/core/api/types';

const REFUSALS: Record<KnowledgeRefusalCode, string> = {
  query_too_short:
    'That is too short to search for. Try three characters or more.',
  query_too_long: 'That is too long to search for. Try a shorter question.',
  rate_limited:
    'You have searched a few times in the last minute, so this one was not run.',
  knowledge_unavailable:
    'The knowledge base could not be reached. Check the server log for the DSN it was given.',
  knowledge_disabled:
    'Company knowledge is switched off on this deployment. Set AGENT_CONTROL_KNOWLEDGE_ENABLED to turn it on.',
  corpus_empty:
    'The knowledge base is reachable and has nothing in it yet. Nothing has been synced.',
};

const FALLBACK_REFUSAL =
  'The search did not run, and the server did not say why.';

const DAY_SECONDS = 24 * 60 * 60;
const HOUR_SECONDS = 60 * 60;
const MINUTE_SECONDS = 60;

/**
 * Used only when the server did not say, which means a server predating the
 * field. Not a second source of truth: the deployment's own
 * `staleness_warn_seconds` rides on the corpus block, so an operator who
 * shortens it changes what this page colours yellow.
 */
const FALLBACK_STALENESS_WARN_SECONDS = DAY_SECONDS;

export function refusalSentence(
  code: KnowledgeRefusalCode | null | undefined,
  retryAfterSeconds?: number | null
): string | null {
  if (!code) return null;
  const sentence = REFUSALS[code] ?? FALLBACK_REFUSAL;
  if (code === 'rate_limited' && retryAfterSeconds) {
    return `${sentence} Try again in about ${retryAfterSeconds} seconds.`;
  }
  return sentence;
}

/** "4 hours", or null when the server could not compute an age. */
export function formatStaleness(
  seconds: number | null | undefined
): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < MINUTE_SECONDS) return 'under a minute';
  if (seconds < HOUR_SECONDS) {
    const minutes = Math.floor(seconds / MINUTE_SECONDS);
    return `${minutes} minute${minutes === 1 ? '' : 's'}`;
  }
  if (seconds < DAY_SECONDS) {
    const hours = Math.floor(seconds / HOUR_SECONDS);
    return `${hours} hour${hours === 1 ? '' : 's'}`;
  }
  const days = Math.floor(seconds / DAY_SECONDS);
  return `${days} day${days === 1 ? '' : 's'}`;
}

const DATE_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
});

export function formatDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : DATE_FORMATTER.format(parsed);
}

export type FreshnessStrip =
  | { read: false }
  | {
      read: true;
      documents: string;
      age: string | null;
      stale: boolean;
      failing: number;
    };

/**
 * The footer, from the corpus block any response carries.
 *
 * A mirror that is quietly three weeks behind is the confident-wrongness risk
 * this whole feature is careful about, so the age is on the page whether or
 * not anything is wrong with it, rather than appearing only once it is bad.
 *
 * The `read` flag is what stops that honesty inverting. Those counters are
 * required on every response including refusals - a deny control selects the
 * whole object and treats a missing key as a match, so the server must never
 * omit them - which means a refusal raised before the store was opened carries
 * zeros that were never measured. Printing "0 documents from 0 sources" under
 * a rate-limit refusal would send somebody to debug a sync that is working.
 * The server says which it is; this used to be inferred from the refusal code,
 * which put a list of the server's internals in the browser and made the two
 * drift apart the first time a refusal changed its mind about reading stats.
 *
 * `elapsedSeconds` is how long this response has been sitting on the page.
 * `stale_seconds` was computed server-side when the request was answered, so
 * without it a tab left open all afternoon reports an age measured at lunch,
 * in the one place on the page built to answer "is this current".
 */
export function freshnessStrip(
  response: KnowledgeSearchResponse | undefined,
  elapsedSeconds = 0
): FreshnessStrip | null {
  if (!response) return null;

  // Two absences, one answer. The block may be missing outright - the type
  // describes a body that arrived over a network and was cast, not one that
  // was validated, and a footer must not take the page down - or it may be
  // present carrying counters nobody measured. Neither is worth reporting.
  const corpus = response.corpus;
  if (!corpus?.measured) return { read: false };

  const measuredAge = corpus.stale_seconds;
  const age =
    measuredAge === null || measuredAge === undefined
      ? null
      : measuredAge + Math.max(0, elapsedSeconds);
  const warnAfter =
    corpus.staleness_warn_seconds ?? FALLBACK_STALENESS_WARN_SECONDS;

  return {
    read: true,
    documents: `${corpus.documents} document${corpus.documents === 1 ? '' : 's'} from ${corpus.sources} source${corpus.sources === 1 ? '' : 's'}`,
    age: formatStaleness(age),
    stale: age !== null && age > warnAfter,
    failing: corpus.sources_failing,
  };
}

/**
 * The line under the count, when some results were not written in the
 * workspace.
 *
 * 'unknown' authorship counts as external on the server, and the sentence is
 * written so a reader is not told those are safe: "we could not tell" and
 * "somebody outside wrote it" land in the same number on purpose.
 */
export function externalAuthorNote(
  response: KnowledgeSearchResponse | undefined
): string | null {
  // Typed as a number, cast rather than validated, and `undefined < 1` is
  // false - which would put the literal word "undefined" at the front of a
  // sentence about who wrote these documents.
  const count = response?.external_author_count;
  if (typeof count !== 'number' || count < 1) return null;
  return `${count} of these ${count === 1 ? 'was' : 'were'} not written inside the workspace, or the sync could not tell who wrote ${count === 1 ? 'it' : 'them'}.`;
}
