/**
 * Unit tests for the diff the Configuration tab computes for itself.
 *
 * Server-side diffing is deliberately absent from this feature, so the
 * algorithm is ours and its edge cases are ours: an empty body is no lines
 * rather than one blank line, and a long unchanged stretch collapses into a
 * marker that has to count the right number of lines.
 *
 * These live under tests/ct because that is where the runner with no Next.js
 * server is. They mount nothing and need no browser.
 */

import { expect, test } from '@playwright/experimental-ct-react';

import {
  collapseContext,
  diffLines,
} from '../../src/core/page-components/agent-detail/config/config-diff';

test.describe('diffLines', () => {
  test('reports identical bodies as all context', () => {
    expect(diffLines('same\nlines', 'same\nlines')).toEqual([
      { kind: 'context', text: 'same' },
      { kind: 'context', text: 'lines' },
    ]);
  });

  test('reports a whole-body swap as one removal and one addition', () => {
    expect(diffLines('a', 'b')).toEqual([
      { kind: 'removed', text: 'a' },
      { kind: 'added', text: 'b' },
    ]);
  });

  test('treats an empty body as no lines rather than one blank line', () => {
    expect(diffLines('', 'first')).toEqual([{ kind: 'added', text: 'first' }]);
    expect(diffLines('first', '')).toEqual([
      { kind: 'removed', text: 'first' },
    ]);
    expect(diffLines('', '')).toEqual([]);
  });

  test('keeps the unchanged head and tail around an edit in the middle', () => {
    expect(diffLines('one\ntwo\nthree', 'one\ntwo point five\nthree')).toEqual([
      { kind: 'context', text: 'one' },
      { kind: 'removed', text: 'two' },
      { kind: 'added', text: 'two point five' },
      { kind: 'context', text: 'three' },
    ]);
  });

  test('does not lose a line that only moved', () => {
    const out = diffLines('a\nb\nc', 'b\nc\na');
    expect(out.filter((line) => line.kind === 'removed')).toEqual([
      { kind: 'removed', text: 'a' },
    ]);
    expect(out.filter((line) => line.kind === 'added')).toEqual([
      { kind: 'added', text: 'a' },
    ]);
  });

  test('falls back to both sides whole when the bodies are too big to align', () => {
    // Past the cell budget the panel stops computing an exact alignment. It
    // must still show every line rather than dropping the ones it skipped.
    const before = Array.from({ length: 700 }, (_, i) => `a${i}`).join('\n');
    const after = Array.from({ length: 700 }, (_, i) => `b${i}`).join('\n');

    const out = diffLines(before, after);
    expect(out.filter((line) => line.kind === 'removed')).toHaveLength(700);
    expect(out.filter((line) => line.kind === 'added')).toHaveLength(700);
  });
});

test.describe('collapseContext', () => {
  test('counts the lines it hid', () => {
    const lines = [
      { kind: 'added' as const, text: 'new' },
      ...Array.from({ length: 21 }, (_, i) => ({
        kind: 'context' as const,
        text: `l${i}`,
      })),
    ];

    const collapsed = collapseContext(lines);

    // Three lines of context are kept either side of the change; the rest of
    // the run becomes one marker naming how many are not being drawn.
    expect(collapsed.map((line) => line.text)).toEqual([
      'new',
      'l0',
      'l1',
      'l2',
      '18 unchanged lines',
    ]);
  });

  test('says "line" when it hid exactly one', () => {
    const lines = [
      { kind: 'added' as const, text: 'new' },
      ...Array.from({ length: 4 }, (_, i) => ({
        kind: 'context' as const,
        text: `l${i}`,
      })),
    ];

    expect(collapseContext(lines).at(-1)).toEqual({
      kind: 'gap',
      text: '1 unchanged line',
    });
  });

  test('leaves a short diff alone', () => {
    const lines = [
      { kind: 'context' as const, text: 'a' },
      { kind: 'removed' as const, text: 'b' },
      { kind: 'added' as const, text: 'c' },
    ];
    expect(collapseContext(lines)).toEqual(lines);
  });
});
