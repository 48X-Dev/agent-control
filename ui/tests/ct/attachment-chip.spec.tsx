/// <reference path="./playwright-jsx-runtime.d.ts" />
/** @jsxImportSource playwright */

/**
 * Component tests for the composer's file chip.
 *
 * The integration spec proves the panel wires the chip up. This proves what
 * the chip does with what it is handed, which is the half that matters:
 * `display_name` is chosen by whoever uploaded the file - on the tracker path,
 * by whoever filed the issue - and this console's session cookie is a
 * credential on every admin endpoint in the product.
 *
 * Two rules, one test each, and neither is visible from a screenshot:
 *
 * * a name is a text node. No markdown, no `dangerouslySetInnerHTML`, no link
 *   built from a filename, and the assertion is that nothing executed rather
 *   than that it looked right.
 * * a long name is cut by CSS. Slicing a string at a fixed length can cut a
 *   surrogate pair in half, and the replacement character that produces looks
 *   like corruption in a file somebody uploaded ten seconds ago - so the whole
 *   name has to still be in the DOM, and in the tooltip, with only the paint
 *   truncated.
 */

import { expect, test } from '@playwright/experimental-ct-react';

import type { Attachment } from '../../src/core/api/types';
import { AttachmentChip } from '../../src/core/page-components/agent-detail/agent-chat/attachment-chip';

type Pwned = Window & { __pwned?: string };

const noop = () => {};

function attachment(overrides: Partial<Attachment> = {}): Attachment {
  return {
    attachment_key: 'att-1',
    session_key: 'sess-1',
    display_name: 'spec.pdf',
    display_name_normalized: false,
    declared_mime: 'application/pdf',
    sniffed_mime: 'application/pdf',
    mime_mismatch: false,
    size_bytes: 2_500_000,
    source_sha256: 'a'.repeat(64),
    status: 'ready',
    origin: 'operator_upload',
    created_at: '2024-03-02T09:00:00Z',
    updated_at: '2024-03-02T09:00:00Z',
    ...overrides,
  };
}

test.describe('AttachmentChip (component)', () => {
  test('renders a hostile filename as characters and executes none of it', async ({
    mount,
    page,
  }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const hostile =
      "<img src=x onerror=\"window.__pwned = 'chip'\">report<script>window.__pwned = 'script'</script>.pdf";

    const component = await mount(
      <AttachmentChip
        state={{
          kind: 'ready',
          id: 'one',
          attachment: attachment({ display_name: hostile }),
        }}
        onCancel={noop}
        onRemove={noop}
        onDismiss={noop}
      />
    );

    await expect(page.getByTestId('chat-attachment-name')).toHaveText(hostile);
    await expect(component.locator('script')).toHaveCount(0);
    await expect(component.locator('img')).toHaveCount(0);
    await expect(component.locator('a')).toHaveCount(0);
    expect(await page.evaluate(() => (window as Pwned).__pwned)).toBe(
      undefined
    );
    expect(errors).toEqual([]);
  });

  test('truncates a long name by painting, not by cutting the string', async ({
    mount,
    page,
  }) => {
    // A name whose 128th character lands inside an astral pair. Slicing here
    // is what produces the replacement character that reads as a corrupt file.
    const name = `${'n'.repeat(127)}🧿${'n'.repeat(40)}.pdf`;

    await mount(
      <AttachmentChip
        state={{
          kind: 'ready',
          id: 'one',
          attachment: attachment({ display_name: name }),
        }}
        onCancel={noop}
        onRemove={noop}
        onDismiss={noop}
      />
    );

    const label = page.getByTestId('chat-attachment-name');
    await expect(label).toHaveText(name);
    await expect(label).toHaveAttribute('title', name);
    expect(
      await label.evaluate((node) => getComputedStyle(node).overflow)
    ).toBe('hidden');
    expect(
      await label.evaluate((node) => getComputedStyle(node).webkitLineClamp)
    ).toBe('1');
    // The paint is clipped; the string is whole.
    expect(
      await label.evaluate((node) => (node.textContent ?? '').length)
    ).toBe(name.length);
  });

  test('an uploading chip offers cancel and reports its progress', async ({
    mount,
    page,
  }) => {
    // Cancel names the chip it belongs to. The picker allows three files and
    // nothing stops a second being chosen while the first is still going, so a
    // cancellation that did not say which upload it meant would abort whichever
    // request started last - the operator cancels one file and a different one
    // vanishes.
    const cancelled: string[] = [];

    await mount(
      <AttachmentChip
        state={{
          kind: 'uploading',
          id: 'the-second-upload',
          name: 'big-deck.pdf',
          progress: 0.42,
        }}
        onCancel={(id) => cancelled.push(id)}
        onRemove={noop}
        onDismiss={noop}
      />
    );

    const chip = page.getByTestId('chat-attachment-chip');
    await expect(chip).toHaveAttribute('data-state', 'uploading');
    await expect(
      chip.getByRole('progressbar', { name: 'Uploading big-deck.pdf' })
    ).toHaveAttribute('aria-valuenow', '42');

    await page.getByTestId('chat-attachment-cancel').click();
    expect(cancelled).toEqual(['the-second-upload']);
  });

  test("a failed chip shows the server's own sentence and can be dismissed", async ({
    mount,
    page,
  }) => {
    /**
     * The server writes every refusal by hand and never echoes an upstream
     * body, so passing its words through is both safer and more accurate than
     * paraphrasing. What this pins is that the sentence arrives intact and
     * that dismissing it is the operator's, not a timeout's.
     */
    const dismissed: string[] = [];
    const sentence =
      'This deployment does not accept files of that type. Export it to PDF and attach that.';

    await mount(
      <AttachmentChip
        state={{
          kind: 'failed',
          id: 'one',
          name: 'deck.pptx',
          message: sentence,
        }}
        onCancel={noop}
        onRemove={noop}
        onDismiss={(id) => dismissed.push(id)}
      />
    );

    await expect(page.getByTestId('chat-attachment-chip')).toHaveAttribute(
      'data-state',
      'failed'
    );
    await expect(page.getByTestId('chat-attachment-error')).toHaveText(
      sentence
    );

    await page.getByTestId('chat-attachment-dismiss').click();
    expect(dismissed).toEqual(['one']);
  });

  test('a stored file that cannot be sent says so on the chip itself', async ({
    mount,
    page,
  }) => {
    /**
     * A tombstoned attachment still lists, and the turn would be refused with
     * a 409 if it were named. Telling the operator here - before the click -
     * is the difference between a sentence and a failed send.
     */
    await mount(
      <AttachmentChip
        state={{
          kind: 'ready',
          id: 'one',
          attachment: attachment({ status: 'tombstoned' }),
        }}
        onCancel={noop}
        onRemove={noop}
        onDismiss={noop}
      />
    );

    const chip = page.getByTestId('chat-attachment-chip');
    await expect(chip).toHaveAttribute('data-state', 'tombstoned');
    await expect(chip).toContainText('attach it again');
  });

  test('a renamed file says it was renamed', async ({ mount, page }) => {
    /**
     * Normalization is silent otherwise, and a file whose name changed between
     * the picker and the chip looks like the wrong file was uploaded.
     */
    await mount(
      <AttachmentChip
        state={{
          kind: 'ready',
          id: 'one',
          attachment: attachment({
            display_name: 'invoice.pdf',
            display_name_normalized: true,
          }),
        }}
        onCancel={noop}
        onRemove={noop}
        onDismiss={noop}
      />
    );

    await expect(page.getByTestId('chat-attachment-chip')).toContainText(
      'renamed for display'
    );
  });

  test('removing a ready file names the key the server knows it by', async ({
    mount,
    page,
  }) => {
    const removed: string[] = [];

    await mount(
      <AttachmentChip
        state={{
          kind: 'ready',
          id: 'local-id-not-the-key',
          attachment: attachment({ attachment_key: 'att-real-key' }),
        }}
        onCancel={noop}
        onRemove={(key) => removed.push(key)}
        onDismiss={noop}
      />
    );

    await page.getByTestId('chat-attachment-remove').click();
    expect(removed).toEqual(['att-real-key']);
  });
});
