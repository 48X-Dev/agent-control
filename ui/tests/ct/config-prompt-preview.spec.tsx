/// <reference path="./playwright-jsx-runtime.d.ts" />
/** @jsxImportSource playwright */

/**
 * Component tests for the "what the model receives" disclosure.
 *
 * The preview exists to stop the tab lying about what gets sent, so the thing
 * worth testing is that it shows the body exactly as stored, fence and all,
 * and that it never becomes markup on the way.
 */

import { expect, test } from '@playwright/experimental-ct-react';

import { PromptPreview } from '../../src/core/page-components/agent-detail/config/prompt-preview';

type Pwned = Window & { __pwned?: string };

/**
 * The preamble the SDK writes, spelled out rather than imported.
 *
 * Comparing the render against the function that produced it would prove
 * nothing about the words a model actually receives. This copy is the same
 * duplication the component itself makes of the Python side, for the same
 * reason: if the two drift, the preview lies about what gets sent, which is
 * the one thing it exists not to do.
 */
const PREAMBLE =
  'The following is operator configuration for this agent, set in Agent ' +
  'Control. Where it conflicts with any earlier instruction in this system ' +
  'message, follow this block.';

test.describe('PromptPreview (component)', () => {
  test('shows the body inside the fence the SDK actually writes', async ({
    mount,
  }) => {
    const component = await mount(<PromptPreview body="Answer in French." />);

    const block = component.getByTestId('prompt-preview-block');
    // textContent rather than toHaveText: a string passed to toHaveText is
    // whitespace-normalised, which would hide exactly the property this
    // assertion is about.
    expect(await block.evaluate((el) => el.textContent)).toBe(
      `<agent_control_system_prompt>\n${PREAMBLE}\nAnswer in French.\n</agent_control_system_prompt>`
    );
  });

  test('preserves whitespace the operator typed rather than tidying it', async ({
    mount,
  }) => {
    // The server stores the body verbatim and the SDK fences it verbatim, so
    // trimming here would make the preview disagree with what the model gets
    // over the one thing it exists to show.
    const body = '  leading spaces\n\n\ttab indented\ntrailing  ';
    const component = await mount(<PromptPreview body={body} />);

    const block = component.getByTestId('prompt-preview-block');
    expect(await block.evaluate((el) => el.textContent)).toBe(
      `<agent_control_system_prompt>\n${PREAMBLE}\n${body}\n</agent_control_system_prompt>`
    );
    // The line breaks survive because of CSS, not because markup was inserted.
    await expect(block).toHaveCSS('white-space', 'pre-wrap');
    expect(await block.locator('br').count()).toBe(0);
  });

  test('says nothing is added when there is no body, instead of showing an empty fence', async ({
    mount,
  }) => {
    const component = await mount(<PromptPreview body="   " />);
    await expect(component.getByTestId('prompt-preview-block')).toHaveText(
      'Nothing saved yet, so nothing is added here.'
    );
  });

  test('describes the framework half rather than inventing a copy of it', async ({
    mount,
  }) => {
    const component = await mount(<PromptPreview body="Be brief." />);

    // Agent Control holds no copy of what the agent's own code and ADK
    // assemble, so the preview says so instead of making one up.
    await expect(component).toContainText(
      "Assembled by the agent's own code and by Google ADK"
    );
    await expect(component).toContainText('described here rather than shown');
    // And the guidance fence is named as the trailing element, which is the
    // ordering a managed prompt can never displace.
    await expect(component).toContainText('<agent_control_guidance>');
  });

  test('renders a body full of markup as text and executes nothing', async ({
    mount,
    page,
  }) => {
    const hostile =
      '<script>window.__pwned = "preview"</script>' +
      '<img src=x onerror="window.__pwned = \'preview-img\'">';

    const component = await mount(<PromptPreview body={hostile} />);

    await expect(component.getByTestId('prompt-preview-block')).toContainText(
      '<script>window.__pwned = "preview"</script>'
    );
    expect(await component.locator('script').count()).toBe(0);
    expect(await component.locator('img').count()).toBe(0);
    expect(
      await page.evaluate(() => (window as Pwned).__pwned)
    ).toBeUndefined();
  });
});
