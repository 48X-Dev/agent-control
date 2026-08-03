/**
 * Formatting for the dispatch panels.
 *
 * Every string that passes through here originated with somebody who can file
 * an issue in a tracker, or with an agent that read one. It is formatted and
 * rendered as text; nothing in this file produces markup.
 */

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
});

const TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  hour: '2-digit',
  minute: '2-digit',
});

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

function parse(value: string | null | undefined): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

export function formatDateTime(
  value: string | null | undefined
): string | null {
  const parsed = parse(value);
  return parsed ? DATE_TIME_FORMATTER.format(parsed) : null;
}

export function formatTime(value: string | null | undefined): string | null {
  const parsed = parse(value);
  return parsed ? TIME_FORMATTER.format(parsed) : null;
}

/** "4 minutes ago", or null when there is no usable timestamp. */
export function formatAge(
  value: string | null | undefined,
  now: number
): string | null {
  const parsed = parse(value);
  if (!parsed) return null;

  const elapsed = now - parsed.getTime();
  if (elapsed < MINUTE_MS) return 'just now';
  if (elapsed < HOUR_MS) {
    const minutes = Math.floor(elapsed / MINUTE_MS);
    return `${minutes} minute${minutes === 1 ? '' : 's'} ago`;
  }
  if (elapsed < DAY_MS) {
    const hours = Math.floor(elapsed / HOUR_MS);
    return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  }
  const days = Math.floor(elapsed / DAY_MS);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

export function ageInMs(
  value: string | null | undefined,
  now: number
): number | null {
  const parsed = parse(value);
  return parsed ? now - parsed.getTime() : null;
}

/** An issue filed in the last hour, which is a provenance heuristic and no more. */
export function isNewWithinHour(
  createdAt: string | null | undefined,
  now: number
): boolean {
  const age = ageInMs(createdAt, now);
  return age !== null && age >= 0 && age < HOUR_MS;
}

/**
 * A url this console is willing to turn into something clickable, or null.
 *
 * `ImportTaskItem.source_url` is checked server-side for exactly this, and its
 * validator says why: the provenance link is the one part of an untrusted item
 * the console makes clickable, and it sits on the screen whose whole job is
 * letting somebody check provenance. `MilestoneIssue.url` reaches the same
 * anchor by a different route and carries no such check, so it gets one here.
 *
 * Relative and protocol-relative values are refused too. Neither can appear in
 * a Linear issue url, and both would let a link that looks like a tracker link
 * point somewhere else entirely.
 */
export function safeHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return null;
    }
    return trimmed;
  } catch {
    return null;
  }
}

export type TruncatedText = { text: string; truncated: boolean };

/**
 * Cut a string to the ledger's limit and say so inside the string itself.
 *
 * Silence is the failure worth avoiding: a task description that lost its last
 * two paragraphs on the way into the ledger is an agent confidently doing half
 * a job, and neither the operator nor the reviewer would ever see where it
 * stopped. The marker is plain text and travels with the body.
 */
export function truncateWithMarker(
  value: string,
  maxLength: number
): TruncatedText {
  if (value.length <= maxLength) return { text: value, truncated: false };

  const omitted = value.length - maxLength;
  const marker = `\n\n[... truncated by the console, ${omitted} characters omitted ...]`;
  const head = value.slice(0, Math.max(0, maxLength - marker.length));
  return { text: `${head}${marker}`, truncated: true };
}

export function pluralize(count: number, singular: string, plural?: string) {
  return count === 1 ? singular : (plural ?? `${singular}s`);
}
