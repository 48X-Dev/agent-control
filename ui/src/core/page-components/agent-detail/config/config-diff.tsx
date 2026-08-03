import { Box, Stack, Text } from '@mantine/core';
import { useMemo } from 'react';

import classes from './config-tab.module.css';

export type DiffLineKind = 'context' | 'added' | 'removed' | 'gap';

export type DiffLine = {
  kind: DiffLineKind;
  text: string;
};

/**
 * Above this many line-pairs the exact diff is not computed.
 *
 * The table is quadratic in lines, and a 32000-character body can be a few
 * thousand of them. Past the budget the panel says it is showing the two
 * bodies whole rather than quietly producing a diff that took a second to
 * compute.
 */
const LCS_CELL_BUDGET = 400_000;

/** Unchanged lines kept either side of a change, for context. */
const CONTEXT_LINES = 3;

function splitLines(text: string): string[] {
  return text.length === 0 ? [] : text.split('\n');
}

/**
 * A line-level diff, computed here rather than fetched.
 *
 * Server-side diffing is deliberately absent: the bodies are small, the client
 * already holds both sides, and an API that returns diffs has to pick an
 * algorithm and keep it stable forever. It returns data, not markup. Every
 * off-the-shelf diff renderer this could have used emits highlighted HTML
 * strings, and a stored prompt is attacker-influenceable content rendering in
 * a console whose session cookie is a valid admin credential on this API.
 */
export function diffLines(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);

  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) {
    start += 1;
  }

  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA -= 1;
    endB -= 1;
  }

  const midA = a.slice(start, endA);
  const midB = b.slice(start, endB);

  const head: DiffLine[] = a
    .slice(0, start)
    .map((text) => ({ kind: 'context' as const, text }));
  const tail: DiffLine[] = a
    .slice(endA)
    .map((text) => ({ kind: 'context' as const, text }));

  if (midA.length === 0 && midB.length === 0) {
    return [...head, ...tail];
  }

  if (midA.length * midB.length > LCS_CELL_BUDGET) {
    return [
      ...head,
      ...midA.map((text) => ({ kind: 'removed' as const, text })),
      ...midB.map((text) => ({ kind: 'added' as const, text })),
      ...tail,
    ];
  }

  // Standard LCS table over the differing middle only.
  const rows = midA.length + 1;
  const cols = midB.length + 1;
  const table = new Uint32Array(rows * cols);
  for (let i = midA.length - 1; i >= 0; i -= 1) {
    for (let j = midB.length - 1; j >= 0; j -= 1) {
      table[i * cols + j] =
        midA[i] === midB[j]
          ? table[(i + 1) * cols + (j + 1)] + 1
          : Math.max(table[(i + 1) * cols + j], table[i * cols + (j + 1)]);
    }
  }

  const middle: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < midA.length && j < midB.length) {
    if (midA[i] === midB[j]) {
      middle.push({ kind: 'context', text: midA[i] });
      i += 1;
      j += 1;
    } else if (table[(i + 1) * cols + j] >= table[i * cols + (j + 1)]) {
      middle.push({ kind: 'removed', text: midA[i] });
      i += 1;
    } else {
      middle.push({ kind: 'added', text: midB[j] });
      j += 1;
    }
  }
  while (i < midA.length) {
    middle.push({ kind: 'removed', text: midA[i] });
    i += 1;
  }
  while (j < midB.length) {
    middle.push({ kind: 'added', text: midB[j] });
    j += 1;
  }

  return [...head, ...middle, ...tail];
}

/** Replace long runs of unchanged lines with one counted marker. */
export function collapseContext(lines: DiffLine[]): DiffLine[] {
  const changedAt = lines.map((line) => line.kind !== 'context');
  const keep = lines.map((_, index) => {
    for (let offset = -CONTEXT_LINES; offset <= CONTEXT_LINES; offset += 1) {
      if (changedAt[index + offset]) return true;
    }
    return false;
  });

  const out: DiffLine[] = [];
  let hidden = 0;
  lines.forEach((line, index) => {
    if (keep[index]) {
      if (hidden > 0) {
        out.push({
          kind: 'gap',
          text: `${hidden} unchanged ${hidden === 1 ? 'line' : 'lines'}`,
        });
        hidden = 0;
      }
      out.push(line);
      return;
    }
    hidden += 1;
  });
  if (hidden > 0) {
    out.push({
      kind: 'gap',
      text: `${hidden} unchanged ${hidden === 1 ? 'line' : 'lines'}`,
    });
  }
  return out;
}

const MARKER: Record<DiffLineKind, string> = {
  context: ' ',
  added: '+',
  removed: '-',
  gap: '⋯',
};

const LINE_CLASS: Record<DiffLineKind, string | undefined> = {
  context: undefined,
  added: classes.diffAdded,
  removed: classes.diffRemoved,
  gap: classes.diffContext,
};

export type ConfigDiffSide = {
  label: string;
  body: string | null | undefined;
  modelId: string | null | undefined;
};

/**
 * Two versions of one configuration, side by side in one column.
 *
 * The model change renders as a plain sentence built from the two ids, because
 * a one-line value has no diff worth drawing. Both halves are React text
 * nodes: this component assembles no HTML string, and a CI grep keeps it that
 * way for the whole directory.
 */
export function ConfigDiff({
  before,
  after,
}: {
  before: ConfigDiffSide;
  after: ConfigDiffSide;
}) {
  const lines = useMemo(
    () => collapseContext(diffLines(before.body ?? '', after.body ?? '')),
    [before.body, after.body]
  );

  const bodyChanged = (before.body ?? '') !== (after.body ?? '');
  const modelChanged = (before.modelId ?? null) !== (after.modelId ?? null);

  return (
    <Stack gap="sm" data-testid="config-diff">
      <Text size="xs" c="dimmed">
        {before.label} compared with {after.label}.
      </Text>

      <Stack gap={2}>
        <Text size="sm" fw={500}>
          Model
        </Text>
        {modelChanged ? (
          <Text size="sm" data-testid="config-diff-model">
            {before.modelId ?? 'whatever the code declares'} to{' '}
            {after.modelId ?? 'whatever the code declares'}
          </Text>
        ) : (
          <Text size="sm" c="dimmed" data-testid="config-diff-model">
            Unchanged
            {after.modelId ? `: ${after.modelId}` : ' (set by the code)'}
          </Text>
        )}
      </Stack>

      <Stack gap={2}>
        <Text size="sm" fw={500}>
          System prompt
        </Text>
        {!bodyChanged ? (
          <Text size="sm" c="dimmed" data-testid="config-diff-body-unchanged">
            {after.body ? 'Unchanged.' : 'No prompt on either side.'}
          </Text>
        ) : (
          <Box className={classes.diffPane} data-testid="config-diff-body">
            {lines.map((line, index) => (
              <Box
                key={`${index}-${line.kind}`}
                className={`${classes.diffLine} ${LINE_CLASS[line.kind] ?? ''}`}
              >
                <span className={classes.diffMarker}>{MARKER[line.kind]}</span>
                <span>{line.text}</span>
              </Box>
            ))}
          </Box>
        )}
      </Stack>
    </Stack>
  );
}
