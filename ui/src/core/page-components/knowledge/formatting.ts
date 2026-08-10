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
  KnowledgeSourceStatus,
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
export const FALLBACK_STALENESS_WARN_SECONDS = DAY_SECONDS;

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

// =============================================================================
// The sources view
// =============================================================================

/** Codes a run records when a whole source stops working, not one item in it. */
const SOURCE_FAILURES: Record<string, string> = {
  root_unreachable:
    'The configured root did not resolve. A shared-drive folder read without shared-drive support answers 404, which looks exactly like a folder nobody shared.',
  unreadable_folder:
    'A folder under the root could not be listed. The reader can see it and not read it.',
  schema_unsupported:
    'The sync and the store disagree about the corpus schema. Migrations have run on one of them and not the other.',
  lease_lost:
    'Another sync took the lease mid-run. That run was abandoned; the next one should finish.',
  run_failed:
    'The run did not finish, and the sync did not name a reason. Its log has one.',
  drive_error:
    'Drive refused the read. The sync log carries the status it answered with.',
};

/**
 * Why documents on this source are out of the corpus.
 *
 * A standing total, not a tally from the last run: these are the tombstone
 * reasons currently on the source's rows, so "3 oversize" means three
 * documents are excluded today, not that three were refused this morning.
 */
const EXCLUSIONS: Record<string, string> = {
  unshared: 'no longer shared with the reader',
  excluded: 'ruled out by the sync config',
  oversize: 'over the size ceiling',
  secret_file: 'named like a secret',
};

/**
 * What a failure code means, for an operator deciding where to go next. The
 * code itself is rendered beside this rather than replaced by it: an unknown
 * code with no sentence is still the string to search the sync's log for.
 */
export function failureSentence(
  code: string | null | undefined
): string | null {
  if (!code) return null;
  return (
    SOURCE_FAILURES[code] ??
    'This console has no sentence for that code. The sync log is where it came from.'
  );
}

/** "3 over the size ceiling (oversize), 1 named like a secret (secret_file)". */
export function exclusionSummary(
  excluded: Record<string, number> | null | undefined
): string | null {
  const counted = Object.entries(excluded ?? {}).filter(
    ([, count]) => typeof count === 'number' && count > 0
  );
  if (counted.length === 0) return null;

  return counted
    .sort(([codeA, a], [codeB, b]) => b - a || codeA.localeCompare(codeB))
    .map(([code, count]) => {
      const reason = EXCLUSIONS[code];
      return reason
        ? `${count} ${reason} (${code})`
        : `${count} excluded as ${code}`;
    })
    .join(', ');
}

/**
 * How long since this source last verified, in seconds.
 *
 * Server-measured where possible, for the reason the freshness strip prefers
 * the same field: a browser with a wrong clock would otherwise report an age
 * nobody measured. `elapsedSeconds` keeps it counting while the tab sits open.
 */
export function sourceAgeSeconds(
  source: KnowledgeSourceStatus,
  elapsedSeconds = 0
): number | null {
  if (typeof source.stale_seconds === 'number') {
    return source.stale_seconds + Math.max(0, elapsedSeconds);
  }
  if (!source.last_verified_at) return null;
  const verified = new Date(source.last_verified_at).getTime();
  if (Number.isNaN(verified)) return null;
  return Math.max(0, (Date.now() - verified) / 1000);
}

/**
 * The one state a row is in, in the order an operator should read them.
 *
 * A source somebody switched off outranks everything: it is not failing, it is
 * not indexing, and colouring it red would train a reader to ignore red.
 */
export type SourceHealth =
  | 'disabled'
  | 'failing'
  | 'no_documents'
  | 'partial'
  | 'stale'
  | 'ok';

export function sourceHealth(
  source: KnowledgeSourceStatus,
  ageSeconds: number | null,
  warnAfterSeconds: number
): SourceHealth {
  if (!source.enabled) return 'disabled';

  const code = source.last_failure_code;

  // A reason the sync recorded explains an empty source better than its
  // emptiness does.
  if (source.failing && code) return 'failing';
  // `failing` is also set for an enabled source holding nothing, which the
  // contract leaves without a code, so this precedes the bare flag or the
  // emptiness loses its own sentence.
  if (source.document_count === 0) return 'no_documents';
  if (source.failing) return 'failing';
  // A run that finished and recorded an error is not a source that stopped.
  if (code) return 'partial';
  if (ageSeconds !== null && ageSeconds > warnAfterSeconds) return 'stale';
  return 'ok';
}
