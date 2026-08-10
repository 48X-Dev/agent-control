import { Alert, Badge, Button, Group, Stack, Text } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import { useEffect, useState } from 'react';

import type {
  KnowledgeSearchResponse,
  KnowledgeSnippet,
} from '@/core/api/types';

import {
  externalAuthorNote,
  formatDate,
  freshnessStrip,
  refusalSentence,
} from './formatting';
import classes from './knowledge.module.css';

/**
 * One page of results, a refusal, or an empty answer.
 *
 * **Everything from the corpus renders as a text node.** Snippet, path,
 * heading trail, source name: each one is a string somebody wrote in a
 * document, and a document is exactly as attacker-authored as a task
 * description. React interpolation escapes it; nothing here reaches for the
 * raw-HTML prop, which CI greps this directory for, and a Playwright case
 * asserts a planted `<script>` arrives as characters on the page rather than
 * as a node in the DOM.
 *
 * There is no link to the original. The corpus schema carries no URL yet - the
 * sync that would populate one is a later phase - and inventing a Drive link
 * from a path would be a link that mostly 404s. The full path is shown instead,
 * which is what a person actually searches their Drive for.
 */

function AuthorBadge({ kind }: { kind: string }) {
  if (kind === 'workspace') return null;
  // 'unknown' is not 'safe'. The server counts it as external for exactly this
  // reason and the badge says which of the two it is rather than smoothing it.
  return (
    <Badge color="yellow" variant="light" size="xs">
      {kind === 'external' ? 'outside author' : 'author unknown'}
    </Badge>
  );
}

function Result({
  result,
  index,
}: {
  result: KnowledgeSnippet;
  index: number;
}) {
  const modified = formatDate(result.modified_at);
  const synced = formatDate(result.synced_at);
  const external = result.author_kind !== 'workspace';

  return (
    <Stack
      gap={6}
      className={`${classes.result} ${external ? classes.resultExternal : ''}`}
      data-testid={`knowledge-result-${index}`}
    >
      <Group gap="xs" justify="space-between" wrap="nowrap" align="flex-start">
        <Text size="sm" fw={600}>
          {result.title}
        </Text>
        <AuthorBadge kind={result.author_kind} />
      </Group>

      <Text
        className={classes.path}
        c="dimmed"
        data-testid="knowledge-result-path"
      >
        {result.path}
        {result.heading_path ? ` — ${result.heading_path}` : ''}
      </Text>

      <Text className={classes.snippet} data-testid="knowledge-result-snippet">
        {result.snippet}
      </Text>

      <Text size="xs" c="dimmed">
        {result.source_name}
        {modified ? ` · changed ${modified}` : ''}
        {synced ? ` · mirrored ${synced}` : ''}
      </Text>
    </Stack>
  );
}

/**
 * How long this response has been on the page, in seconds.
 *
 * `stale_seconds` was computed when the server answered, so the strip is a
 * snapshot the moment it renders. Nothing here goes to the network - the tick
 * only stops the page asserting an age it stopped measuring.
 */
const AGE_TICK_MS = 30_000;

export function useSecondsOnScreen(since: number | undefined): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const tick = window.setInterval(() => setNow(Date.now()), AGE_TICK_MS);
    return () => window.clearInterval(tick);
  }, []);

  if (since === undefined) return 0;
  // Clamped rather than reset when a refetch lands: `since` jumps ahead of the
  // last tick, and a fresh response reading zero seconds old is the truth.
  return Math.max(0, (now - since) / 1000);
}

export function FreshnessStripView({
  response,
  dataUpdatedAt,
  onRecheck,
  rechecking,
}: {
  response: KnowledgeSearchResponse | undefined;
  dataUpdatedAt?: number;
  onRecheck?: () => void;
  rechecking?: boolean;
}) {
  const elapsed = useSecondsOnScreen(response ? dataUpdatedAt : undefined);
  const strip = freshnessStrip(response, elapsed);
  if (!strip) return null;

  return (
    <Group
      gap="xs"
      className={classes.strip}
      data-testid="knowledge-freshness"
      wrap="wrap"
    >
      {!strip.read ? (
        <Text size="xs" c="dimmed">
          The mirror was not read on this request, so there is nothing to report
          about how current it is.
        </Text>
      ) : (
        <>
          <Text size="xs" c="dimmed">
            {strip.documents}
          </Text>
          {strip.age ? (
            <Text size="xs" c={strip.stale ? 'yellow.7' : 'dimmed'}>
              · last checked {strip.age} ago
            </Text>
          ) : (
            <Text size="xs" c="yellow.7">
              · never verified
            </Text>
          )}
          {strip.failing > 0 ? (
            <Text size="xs" c="yellow.7">
              · {strip.failing} source{strip.failing === 1 ? '' : 's'} failing
            </Text>
          ) : null}
        </>
      )}
      {/* The age above only climbs. Somebody watching for a sync to land needs
          a way to ask, and a full browser reload is not a way the page
          suggests. One call per press, from the same window a search spends,
          which is why it is a press and not a poll. */}
      {onRecheck ? (
        <Button
          variant="subtle"
          size="compact-xs"
          onClick={onRecheck}
          loading={rechecking}
          data-testid="knowledge-recheck"
        >
          Check again
        </Button>
      ) : null}
    </Group>
  );
}

export function KnowledgeResults({
  response,
  emptyMessage,
}: {
  response: KnowledgeSearchResponse | undefined;
  emptyMessage: string;
}) {
  if (!response) return null;

  const refusal = refusalSentence(
    response.refusal_code,
    response.retry_after_seconds
  );

  if (refusal) {
    // A refusal is not an error. The corpus said why it did not run, and the
    // page says the same thing rather than a red box that reads as a bug.
    return (
      <Alert
        color="gray"
        icon={<IconAlertTriangle size={16} />}
        data-testid="knowledge-refusal"
      >
        <Text size="sm">{refusal}</Text>
      </Alert>
    );
  }

  // Guarded for the reason the freshness strip guards its own block: a body
  // that arrived over a network and was cast is not a body that was validated,
  // and a malformed 200 from a proxy should cost the results, not the page.
  const results = response.results ?? [];
  if (results.length === 0) {
    return (
      <Text size="sm" c="dimmed" data-testid="knowledge-empty">
        {emptyMessage}
      </Text>
    );
  }

  const note = externalAuthorNote(response);

  return (
    <Stack gap="sm">
      {note ? (
        <Text size="xs" c="yellow.7" data-testid="knowledge-external-note">
          {note}
        </Text>
      ) : null}
      {results.map((result, index) => (
        <Result key={`${result.path}-${index}`} result={result} index={index} />
      ))}
    </Stack>
  );
}
