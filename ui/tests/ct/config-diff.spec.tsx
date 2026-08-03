/// <reference path="./playwright-jsx-runtime.d.ts" />
/** @jsxImportSource playwright */

/**
 * Component tests for the diff renderer.
 *
 * This is the component the design named as the exception waiting to happen:
 * most off-the-shelf diff renderers emit highlighted HTML strings, and a
 * stored prompt renders in an admin console whose session cookie is a valid
 * credential on this API. Mounting it in a real browser with no server in
 * between is the only place "given these exact bytes, this exact DOM" can be
 * asserted as a rule rather than as a consequence of how the tab happens to
 * wire it up.
 */

import { expect, test } from '@playwright/experimental-ct-react';

import { ConfigDiff } from '../../src/core/page-components/agent-detail/config/config-diff';

type Pwned = Window & { __pwned?: string };

test.describe('ConfigDiff (component)', () => {
  test('renders markup in a body as the characters somebody typed', async ({
    mount,
    page,
  }) => {
    const hostile =
      '<script>window.__pwned = "diff"</script>\n' +
      '<img src=x onerror="window.__pwned = \'diff-img\'">';

    const component = await mount(
      <ConfigDiff
        before={{ label: 'Version 2', body: hostile, modelId: 'gpt-5.6-sol' }}
        after={{ label: 'now', body: 'harmless', modelId: 'gpt-5.4-mini' }}
      />
    );

    const body = component.getByTestId('config-diff-body');
    await expect(body).toContainText(
      '<script>window.__pwned = "diff"</script>'
    );
    await expect(body).toContainText('<img src=x onerror=');

    // No element was created from that text, and nothing ran.
    expect(await component.locator('script').count()).toBe(0);
    expect(await component.locator('img').count()).toBe(0);
    expect(
      await page.evaluate(() => (window as Pwned).__pwned)
    ).toBeUndefined();
  });

  test('marks added and removed lines and keeps the ones that did not move', async ({
    mount,
  }) => {
    const component = await mount(
      <ConfigDiff
        before={{ label: 'Version 1', body: 'one\ntwo\nthree', modelId: null }}
        after={{
          label: 'now',
          body: 'one\ntwo point five\nthree',
          modelId: null,
        }}
      />
    );

    const body = component.getByTestId('config-diff-body');
    await expect(body).toContainText('-two');
    await expect(body).toContainText('+two point five');
    await expect(body).toContainText('one');
    await expect(body).toContainText('three');
  });

  test('describes a model change as a sentence, and says so when it did not change', async ({
    mount,
  }) => {
    const changed = await mount(
      <ConfigDiff
        before={{ label: 'Version 2', body: 'same', modelId: null }}
        after={{ label: 'now', body: 'same', modelId: 'gpt-5.4-mini' }}
      />
    );
    await expect(changed.getByTestId('config-diff-model')).toHaveText(
      'whatever the code declares to gpt-5.4-mini'
    );
    // Nothing to draw for an unchanged body, and it says which case it is.
    await expect(changed.getByTestId('config-diff-body-unchanged')).toHaveText(
      'Unchanged.'
    );
    await expect(changed.getByTestId('config-diff-body')).toHaveCount(0);

    await changed.unmount();

    const unchanged = await mount(
      <ConfigDiff
        before={{ label: 'Version 2', body: null, modelId: 'gpt-5.4-mini' }}
        after={{ label: 'now', body: null, modelId: 'gpt-5.4-mini' }}
      />
    );
    await expect(unchanged.getByTestId('config-diff-model')).toHaveText(
      'Unchanged: gpt-5.4-mini'
    );
    await expect(
      unchanged.getByTestId('config-diff-body-unchanged')
    ).toHaveText('No prompt on either side.');
  });

  test('collapses a long unchanged run into one counted marker', async ({
    mount,
  }) => {
    const before = ['head', ...Array.from({ length: 40 }, (_, i) => `l${i}`)];
    const after = ['head changed', ...before.slice(1)];

    const component = await mount(
      <ConfigDiff
        before={{ label: 'Version 2', body: before.join('\n'), modelId: null }}
        after={{ label: 'now', body: after.join('\n'), modelId: null }}
      />
    );

    const body = component.getByTestId('config-diff-body');
    await expect(body).toContainText('unchanged lines');
    // The far end of a 40-line run is not drawn line by line.
    await expect(body).not.toContainText('l39');
  });
});
