import type { Locator, Page } from '@playwright/test';

import { getAgentRoute } from '@/core/constants/agent-routes';

import { chatAgentName, expect, mockData, mockRoutes, test } from './fixtures';

const configUrl = getAgentRoute('agent-1', { tab: 'config' });
const controlsUrl = getAgentRoute('agent-1', { tab: 'controls' });

/** Set by anything that manages to execute out of a rendered prompt body. */
type Pwned = Window & { __pwned?: string };

/** Wait for the tab to have finished its first read of the row. */
async function openConfigTab(page: Page) {
  await page.goto(configUrl);
  await expect(page.getByTestId('agent-config-tab')).toBeVisible();
}

/** Pick an allowlist entry by its visible label. */
async function chooseModel(page: Page, label: string | RegExp) {
  await page.getByTestId('model-select-input').click();
  await page.getByRole('option', { name: label }).click();
}

/** The page must never scroll sideways, whatever somebody pasted into it. */
async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => {
    const root = document.documentElement;
    return root.scrollWidth - root.clientWidth;
  });
  expect(overflow).toBeLessThanOrEqual(1);
}

/**
 * An element holds the width it was given rather than growing to fit its text.
 *
 * Stronger than checking the page for a horizontal scrollbar, and it has to
 * be: every pane on this tab sits inside an ancestor with `overflow: auto`, so
 * a pane that has quietly become 200000 pixels wide leaves the page itself
 * looking fine. Measured against a body with one 20000-character unbroken
 * token, this reads 552 against 552 with the wrapping rules in place and
 * 216738 against 552 without them.
 */
async function expectWrapsRatherThanWidens(locator: Locator) {
  const [scrollWidth, clientWidth] = await locator.evaluate((el) => [
    el.scrollWidth,
    el.clientWidth,
  ]);
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);
}

test.describe('Agent configuration tab', () => {
  test('loads the stored prompt and model, with Save inert until something changes', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      mockData.storedPromptBody
    );
    await expect(mockedPage.getByTestId('model-select-input')).toHaveValue(
      'GPT-5.4 mini'
    );
    // The tab says which layer is actually in charge, rather than leaving it
    // to be inferred from the box having something in it.
    await expect(mockedPage.getByTestId('model-in-effect')).toHaveText(
      /Calling the model saved here/
    );

    await expect(mockedPage.getByTestId('config-save-button')).toBeDisabled();
    await expect(
      mockedPage.getByTestId('config-discard-button')
    ).toBeDisabled();

    await mockedPage.getByTestId('prompt-editor-input').fill('Changed.');
    await expect(mockedPage.getByTestId('config-save-button')).toBeEnabled();

    // Discard puts the server's row back rather than clearing the field.
    await mockedPage.getByTestId('config-discard-button').click();
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      mockData.storedPromptBody
    );
    expect(config.saves).toEqual([]);
  });

  test('saves an edited prompt, and the tab afterwards shows what the server now holds', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    const nextBody = 'You are the support agent for Acme.\nBe brief.';
    await mockedPage.getByTestId('prompt-editor-input').fill(nextBody);
    await mockedPage.getByTestId('config-note-input').fill('Shorter answers.');
    await mockedPage.getByTestId('config-save-button').click();

    // The version the write created, named in the toast.
    await expect(mockedPage.getByText('Saved as version 4')).toBeVisible();
    // Never "applied". A save reaches a running agent when that agent next
    // polls, and the tab is not in a position to know that it has.
    await expect(
      mockedPage
        .getByText(/Agents pick this up within about 60 seconds/)
        .first()
    ).toBeVisible();

    expect(config.saves).toEqual([
      {
        body: nextBody,
        expected_version: 3,
        prompt_enabled: true,
        note: 'Shorter answers.',
      },
    ]);
    // A prompt-only save leaves the model alone rather than restating it.
    expect(config.saves[0]).not.toHaveProperty('model_id');

    // The row is re-read after the write, so the next save's
    // `expected_version` is the one the server just created.
    await expect
      .poll(() => config.configReads, { timeout: 5000 })
      .toBeGreaterThan(1);
    await expect(mockedPage.getByTestId('config-version-row-4')).toBeVisible();

    await mockedPage.getByTestId('prompt-editor-input').fill('Again.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect.poll(() => config.saves.length, { timeout: 5000 }).toBe(2);
    expect(config.saves[1].expected_version).toBe(4);
  });

  test('a model change on its own is a save, and goes out without the body', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await chooseModel(mockedPage, /GPT-5\.6 Sol/);
    await expect(mockedPage.getByTestId('model-select-input')).toHaveValue(
      'GPT-5.6 Sol'
    );
    await mockedPage.getByTestId('config-save-button').click();

    await expect(mockedPage.getByText('Saved as version 4')).toBeVisible();
    expect(config.saves).toEqual([
      {
        model_id: 'gpt-5.6-sol',
        expected_version: 3,
        prompt_enabled: true,
        note: null,
      },
    ]);
    expect(config.saves[0]).not.toHaveProperty('body');
    expect(config.config.model_id).toBe('gpt-5.6-sol');
  });

  test('emptying the box is refused, and points at Clear prompt', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('   ');

    await expect(mockedPage.getByTestId('config-blocking-hint')).toContainText(
      'Clear prompt'
    );
    await expect(mockedPage.getByTestId('config-save-button')).toBeDisabled();
    expect(config.saves).toEqual([]);
  });

  test('a concurrent write comes back as a conflict that keeps the edits', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage, {
      saveError: {
        status: 409,
        errorCode: 'AGENT_CONFIG_VERSION_CONFLICT',
        title: 'Conflict',
        detail: 'This configuration is at version 5, not 3.',
      },
    });
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('My paragraph.');
    await mockedPage.getByTestId('config-save-button').click();

    const alert = mockedPage.getByTestId('config-save-error');
    await expect(alert).toContainText(
      'Somebody else saved while this was open'
    );
    await expect(alert).toContainText('version 5');
    await expect(
      mockedPage.getByTestId('config-conflict-reload')
    ).toBeVisible();
    // The edit is not thrown away by the refusal: it is still there to reapply.
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      'My paragraph.'
    );
  });

  test('after a conflict, reloading and saving again goes out at the version that won', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      saveError: {
        status: 409,
        errorCode: 'AGENT_CONFIG_VERSION_CONFLICT',
        title: 'Conflict',
        detail: 'This configuration is at version 5, not 3.',
      },
    });
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('My paragraph.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect(
      mockedPage.getByTestId('config-conflict-reload')
    ).toBeVisible();

    // Somebody else's write landed while this editor was open.
    config.config.current_version = 5;
    config.failSaves = false;

    await mockedPage.getByTestId('config-conflict-reload').click();
    // The edit survives the reload; only the version underneath it moved.
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      'My paragraph.'
    );

    await mockedPage.getByTestId('config-save-button').click();
    await expect(mockedPage.getByText('Saved as version 6')).toBeVisible();
    expect(config.saves).toHaveLength(2);
    expect(config.saves[1].expected_version).toBe(5);
  });

  test('advisory scan findings are shown after the save that produced them', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage, {
      saveFindings: [
        {
          scanner: 'secret_patterns',
          severity: 'warning',
          code: 'SECRET_LIKE_STRING',
          message: 'A string shaped like an API key was found in this body.',
          match_count: 1,
        },
      ],
    });
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('Use sk-abc123.');
    await mockedPage.getByTestId('config-save-button').click();

    const findings = mockedPage.getByTestId('config-scan-findings');
    await expect(findings).toContainText('A string shaped like an API key');
    // The write happened. The finding is a record, not a refusal.
    await expect(mockedPage.getByText('Saved as version 4')).toBeVisible();
    await expect(findings).toContainText('Advisory only');
  });

  test('a failed save does not leave the previous save’s findings on screen', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      saveFindings: [
        {
          scanner: 'secret_patterns',
          severity: 'warning',
          code: 'SECRET_LIKE_STRING',
          message: 'A string shaped like an API key was found in this body.',
          match_count: 1,
        },
      ],
    });
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('Use sk-abc123.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect(mockedPage.getByTestId('config-scan-findings')).toBeVisible();

    // The next save is refused. "Saved, with 1 thing worth a look" over a write
    // that did not happen would be a lie about a write.
    await mockedPage.route('**/api/v1/agents/*/config', async (route) => {
      if (route.request().method() !== 'PUT') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({
          type: 'about:blank',
          title: 'Error',
          status: 500,
          detail: 'The database went away.',
          error_code: 'INTERNAL_ERROR',
          reason: 'Server error',
        }),
      });
    });

    await mockedPage.getByTestId('prompt-editor-input').fill('Something else.');
    await mockedPage.getByTestId('config-save-button').click();

    await expect(mockedPage.getByTestId('config-save-error')).toBeVisible();
    await expect(mockedPage.getByTestId('config-scan-findings')).toHaveCount(0);
    expect(config.saves).toHaveLength(1);
  });

  // ===========================================================================
  // Unsaved changes
  // ===========================================================================

  test('switching tabs with unsaved edits asks first, and staying keeps them', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('Half a sentence');
    await mockedPage.getByRole('tab', { name: 'Controls' }).click();

    await expect(mockedPage.getByTestId('unsaved-stay')).toBeVisible();
    await mockedPage.getByTestId('unsaved-stay').click();

    // Still on Configuration, still holding the edit, and the URL never moved.
    await expect(mockedPage.getByTestId('agent-config-tab')).toBeVisible();
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      'Half a sentence'
    );
    expect(new URL(mockedPage.url()).searchParams.get('tab')).toBe('config');
  });

  test('discarding lets the tab switch through, and the edit is gone on return', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('Half a sentence');
    await mockedPage.getByRole('tab', { name: 'Controls' }).click();
    await mockedPage.getByTestId('unsaved-discard').click();

    await expect(mockedPage).toHaveURL(new RegExp('tab=controls'));
    await expect(mockedPage.getByTestId('agent-config-tab')).toHaveCount(0);

    await mockedPage.getByRole('tab', { name: 'Configuration' }).click();
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      mockData.storedPromptBody
    );
  });

  test('a model-only edit arms the guard too, not just the textarea', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await chooseModel(mockedPage, /Gemini 2\.5 Flash/);
    await mockedPage.getByRole('tab', { name: 'Monitor' }).click();

    await expect(mockedPage.getByTestId('unsaved-stay')).toBeVisible();
    await mockedPage.getByTestId('unsaved-stay').click();
    await expect(mockedPage.getByTestId('model-select-input')).toHaveValue(
      'Gemini 2.5 Flash'
    );
  });

  test('leaving the page entirely asks first, and the navigation resumes on discard', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('prompt-editor-input').fill('Unsaved words');
    await mockedPage.getByRole('link', { name: 'Teams' }).click();

    // The router transition was cancelled, not followed.
    await expect(mockedPage.getByTestId('unsaved-discard')).toBeVisible();
    expect(new URL(mockedPage.url()).pathname).toBe('/agents');

    await mockedPage.getByTestId('unsaved-discard').click();
    // Cancelling a pages-router transition does not queue it, so the guard has
    // to re-issue it. If it did not, this would sit on /agents forever.
    await expect(mockedPage).toHaveURL(new RegExp('/teams'));
  });

  test('leaving with nothing unsaved does not ask', async ({ mockedPage }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByRole('tab', { name: 'Controls' }).click();
    await expect(mockedPage).toHaveURL(new RegExp('tab=controls'));
    await expect(mockedPage.getByTestId('unsaved-stay')).toHaveCount(0);
  });

  // ===========================================================================
  // History, diff and rollback
  // ===========================================================================

  test('history lists every change, with the badge each one earned', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    const history = mockedPage.getByTestId('config-history');
    await expect(history).toBeVisible();

    const current = mockedPage.getByTestId('config-version-row-3');
    await expect(current).toContainText('v3');
    await expect(current).toContainText('updated');
    await expect(current).toContainText('current');
    await expect(current).toContainText('Tighter policy citation wording.');
    // A credential, never a person: under the default provider every dashboard
    // caller hashes to the same value.
    await expect(current).toContainText('credential c0ffee1234abcd');

    await expect(
      mockedPage.getByTestId('config-version-findings-2')
    ).toHaveText('1 finding');
    await expect(mockedPage.getByTestId('config-version-row-2')).toContainText(
      'model gpt-5.6-sol'
    );
    await expect(mockedPage.getByTestId('config-version-row-1')).toContainText(
      'created'
    );
  });

  test('viewing an old version diffs it against what is in effect now', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('config-version-view-2').click();

    const diff = mockedPage.getByTestId('config-diff');
    await expect(diff).toBeVisible();
    // The model half is a sentence, because a one-line value has no diff worth
    // drawing.
    await expect(mockedPage.getByTestId('config-diff-model')).toHaveText(
      'gpt-5.6-sol to gpt-5.4-mini'
    );
    const body = mockedPage.getByTestId('config-diff-body');
    await expect(body).toContainText('Always answer in one paragraph.');
    await expect(body).toContainText(
      'Cite the policy you used in every answer.'
    );
  });

  test('restoring an old version writes a new one rather than rewinding', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('config-version-restore-1').click();
    const confirm = mockedPage.getByTestId('restore-confirm');
    // The dialog names the version it will create, not the one it goes back to.
    await expect(confirm).toHaveText('Restore as version 4');
    await mockedPage.getByTestId('restore-note-input').fill('Back to basics.');
    await confirm.click();

    await expect
      .poll(() => config.restores, { timeout: 5000 })
      .toEqual([
        {
          versionNum: 1,
          body: { expected_version: 3, note: 'Back to basics.' },
        },
      ]);

    // The counter went forward and the version being undone is still there.
    const restored = mockedPage.getByTestId('config-version-row-4');
    await expect(restored).toContainText('restored');
    await expect(restored).toContainText('current');
    await expect(mockedPage.getByTestId('config-version-row-3')).toBeVisible();
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      'You are a support agent.'
    );
  });

  test('a restore refused over its model offers the prompt text on its own', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      restoreError: {
        status: 409,
        errorCode: 'MODEL_NOT_ALLOWED',
        title: 'Conflict',
        detail: 'gpt-5.6-sol is not on this server’s allowlist.',
      },
    });
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('config-version-restore-2').click();
    await mockedPage.getByTestId('restore-confirm').click();

    const error = mockedPage.getByTestId('restore-error');
    await expect(error).toContainText('could not be restored whole');
    await expect(error).toContainText('gpt-5.6-sol');

    await mockedPage.getByTestId('restore-prompt-only').click();

    // An ordinary save carrying the old body and no model at all.
    await expect.poll(() => config.saves.length, { timeout: 5000 }).toBe(1);
    expect(config.saves[0].body).toBe(
      mockData.agentConfigVersionBodies[2] as string
    );
    expect(config.saves[0]).not.toHaveProperty('model_id');
    expect(config.config.model_id).toBe('gpt-5.4-mini');
  });

  test('a restore refused because somebody else saved is not blamed on the model', async ({
    mockedPage,
  }) => {
    // Three different 409s can come back from a restore: the version this
    // editor loaded is stale, the stored body format is one the server no
    // longer understands, and the version names a model that has left the
    // allowlist. Only the last one is fixable by saving the prompt text on its
    // own, so only the last one may offer that.
    await mockRoutes.agentConfig(mockedPage, {
      restoreError: {
        status: 409,
        errorCode: 'AGENT_CONFIG_VERSION_CONFLICT',
        title: 'Conflict',
        detail: 'This configuration is at version 5, not 3.',
      },
    });
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('config-version-restore-2').click();
    await mockedPage.getByTestId('restore-confirm').click();

    const error = mockedPage.getByTestId('restore-error');
    await expect(error).toContainText('version 5');
    await expect(error).not.toContainText('could not be restored whole');
    await expect(mockedPage.getByTestId('restore-prompt-only')).toHaveCount(0);
  });

  test('clearing the prompt is confirmed first and hands the field back to the code', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('clear-prompt-button').click();
    await mockedPage.getByTestId('config-clear-confirm').click();

    await expect
      .poll(() => config.clears, { timeout: 5000 })
      .toEqual([{ field: 'prompt', body: { expected_version: 3 } }]);

    await expect(
      mockedPage.getByText('Prompt cleared in version 4')
    ).toBeVisible();
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue('');
    // Cleared is a state, not a deletion: the text is still in the history.
    await expect(mockedPage.getByTestId('config-version-row-3')).toBeVisible();
  });

  // ===========================================================================
  // The model half
  // ===========================================================================

  test('the picker offers the server allowlist with its tiers and its suggestion', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage.getByTestId('model-select-input').click();
    const options = mockedPage.getByRole('option');
    await expect(options).toHaveCount(3);

    const premium = mockedPage.getByRole('option', { name: /GPT-5\.6 Sol/ });
    await expect(premium).toContainText('premium');
    await expect(premium).toContainText('Recommended');
    await expect(
      mockedPage.getByRole('option', { name: /Gemini 2\.5 Flash/ })
    ).toContainText('standard');
    // Bands the operator wrote, and no currency anywhere: Agent Control does
    // not know prices.
    await expect(mockedPage.getByTestId('agent-config-tab')).not.toContainText(
      '$'
    );
  });

  test('a model that has left the allowlist is explained, not corrected', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      config: {
        model_id: 'gpt-5.9-retired',
        model_allowed: false,
        model_provider: null,
        model_cost_tier: null,
        model_source: 'code',
      },
    });
    await openConfigTab(mockedPage);

    // The stored id is still what the picker shows, marked for what it is,
    // rather than an empty box that reads as "no model configured".
    await expect(mockedPage.getByTestId('model-select-input')).toHaveValue(
      'gpt-5.9-retired (not available)'
    );

    const alert = mockedPage.getByTestId('model-not-available-alert');
    await expect(alert).toContainText('gpt-5.9-retired');
    await expect(alert).toContainText(
      'running the model its own code declares'
    );
    await expect(mockedPage.getByTestId('model-in-effect')).toHaveText(
      /Calling the model declared in the agent’s own code/
    );

    // Nothing was written and nothing was auto-picked.
    expect(config.saves).toEqual([]);
    expect(config.config.model_id).toBe('gpt-5.9-retired');
  });

  test('a server with no models configured says so, and the prompt half still works', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      models: [],
      config: {
        model_id: null,
        model_provider: null,
        model_cost_tier: null,
        model_source: 'code',
      },
    });
    await openConfigTab(mockedPage);

    await expect(
      mockedPage.getByTestId('model-select-empty-allowlist')
    ).toContainText('No models configured on this server');
    await expect(mockedPage.getByTestId('model-select-input')).toHaveCount(0);

    await mockedPage.getByTestId('prompt-editor-input').fill('Still editable.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect.poll(() => config.saves.length, { timeout: 5000 }).toBe(1);
    expect(config.saves[0]).not.toHaveProperty('model_id');
  });

  test('a failed allowlist request is reported as a failed request, not as an empty server', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage, {
      modelsError: {
        status: 500,
        errorCode: 'INTERNAL_ERROR',
        title: 'Error',
        detail: 'The allowlist could not be read.',
      },
    });
    await openConfigTab(mockedPage);

    const panel = mockedPage.getByTestId('model-select-unavailable');
    await expect(panel).toContainText('did not load');
    await expect(panel).toContainText('gpt-5.4-mini');

    // The two sentences this must not produce: a claim about how the server is
    // configured, and a claim that the agent has fallen back to its code.
    const tab = mockedPage.getByTestId('agent-config-tab');
    await expect(tab).not.toContainText('No models configured on this server');
    await expect(tab).not.toContainText('the server no longer offers');
    await expect(mockedPage.getByTestId('model-in-effect')).toHaveText(
      /Calling the model saved here/
    );
    // Not a refusal, so the tab stays writable and the save button lives.
    await expect(mockedPage.getByTestId('config-read-only')).toHaveCount(0);
    await expect(mockedPage.getByTestId('prompt-editor-input')).toBeEditable();
  });

  test('a key that cannot read the row at all gets a sentence, not a shrug', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage, {
      configError: {
        status: 403,
        errorCode: 'FORBIDDEN',
        title: 'Forbidden',
        detail: 'This operation requires an admin key.',
      },
    });
    await mockedPage.goto(configUrl);

    await expect(mockedPage.getByTestId('config-load-error')).toContainText(
      'That needs an admin key'
    );
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveCount(0);
  });

  test('a history that will not load does not take the editor down with it', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      versionsError: {
        status: 500,
        errorCode: 'INTERNAL_ERROR',
        title: 'Error',
        detail: 'The version history could not be read.',
      },
    });
    await openConfigTab(mockedPage);

    await expect(mockedPage.getByTestId('config-history')).toContainText(
      'History could not be loaded'
    );
    // The row itself loaded, so editing and saving still work.
    await mockedPage.getByTestId('prompt-editor-input').fill('Still saveable.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect.poll(() => config.saves.length, { timeout: 5000 }).toBe(1);
  });

  test('a key that cannot list models gets a read-only tab that still shows everything', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      modelsError: {
        status: 403,
        errorCode: 'FORBIDDEN',
        title: 'Forbidden',
        detail: 'This operation requires an admin key.',
      },
    });
    await openConfigTab(mockedPage);

    await expect(mockedPage.getByTestId('config-read-only')).toContainText(
      'need an admin key'
    );
    await expect(mockedPage.getByTestId('model-readonly-id')).toHaveText(
      'gpt-5.4-mini'
    );
    await expect(
      mockedPage.getByTestId('model-cost-tier-economy')
    ).toBeVisible();

    // Reading works, writing does not.
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      mockData.storedPromptBody
    );
    await expect(mockedPage.getByTestId('config-history')).toBeVisible();
    await expect(mockedPage.getByTestId('config-save-button')).toBeDisabled();
    await expect(
      mockedPage.getByTestId('config-version-restore-3')
    ).toBeDisabled();
    await expect(mockedPage.getByTestId('clear-prompt-button')).toHaveCount(0);
    await expect(mockedPage.getByTestId('clear-model-button')).toHaveCount(0);
    expect(config.saves).toEqual([]);
  });

  // ===========================================================================
  // The delivery gate
  // ===========================================================================

  test('a credential-less server explains itself instead of erroring', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage, {
      config: {
        delivery_state: 'blocked_insecure_auth',
        prompt_source: 'code',
        model_source: 'code',
      },
    });
    await openConfigTab(mockedPage);

    const banner = mockedPage.getByTestId('config-delivery-blocked');
    await expect(banner).toContainText('credential enforcement off');
    await expect(banner).toContainText('AGENT_CONTROL_API_KEY_ENABLED=true');
    // An explanation, not a failure: nothing on this page is broken.
    await expect(mockedPage.getByTestId('config-load-error')).toHaveCount(0);
    await expect(mockedPage.getByTestId('config-save-error')).toHaveCount(0);

    // Editing, saving and the audit trail all keep working while it is up.
    await mockedPage.getByTestId('prompt-editor-input').fill('Saved anyway.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect.poll(() => config.saves.length, { timeout: 5000 }).toBe(1);
    await expect(mockedPage.getByTestId('config-version-row-4')).toBeVisible();
  });

  test('the local-development tier cap says which half is not being delivered', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage, {
      config: {
        delivery_state: 'blocked_insecure_auth',
        prompt_source: 'managed',
        model_source: 'code',
        model_id: 'gpt-5.6-sol',
        model_cost_tier: 'premium',
      },
    });
    await openConfigTab(mockedPage);

    const banner = mockedPage.getByTestId('config-delivery-blocked');
    await expect(banner).toContainText('The prompt is being delivered');
    await expect(banner).toContainText('economy-tier models only');
    await expect(mockedPage.getByTestId('model-in-effect')).toHaveText(
      /Calling the model declared in the agent’s own code/
    );
  });

  test('an agent nobody has configured gets an empty state, not a blank box', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage, {
      config: {
        body: null,
        model_id: null,
        model_provider: null,
        model_cost_tier: null,
        prompt_source: 'none',
        model_source: 'code',
        current_version: 0,
        etag: null,
      },
      versions: [],
    });
    await openConfigTab(mockedPage);

    const empty = mockedPage.getByTestId('config-empty-state');
    await expect(empty).toContainText('runs the instruction and the model');
    await expect(empty).toContainText(
      'Clearing either field hands it straight'
    );
    await expect(mockedPage.getByTestId('config-history')).toContainText(
      'Nothing saved yet'
    );
    // Nothing stored means nothing to clear or to switch off.
    await expect(mockedPage.getByTestId('clear-prompt-button')).toHaveCount(0);
    await expect(mockedPage.getByTestId('prompt-enabled-switch')).toHaveCount(
      0
    );
  });

  test('prompt delivery can be switched off without losing the text', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    // The switch input is visually hidden behind Mantine's track, so this is
    // the click a person makes.
    await mockedPage.getByText('Deliver this prompt to the agent').click();

    await expect
      .poll(() => config.enablePatches, { timeout: 5000 })
      .toEqual([{ prompt_enabled: false, expected_version: 3 }]);
    await expect(
      mockedPage.getByTestId('prompt-enabled-switch')
    ).not.toBeChecked();
    // The text and the history survive it. That is the whole point of a
    // toggle beside a field that is expensive to retype.
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      mockData.storedPromptBody
    );
    await expect(mockedPage.getByTestId('config-version-row-4')).toContainText(
      'delivery off'
    );

    // Editing the text afterwards must not switch delivery back on behind the
    // operator's back.
    await mockedPage.getByTestId('prompt-editor-input').fill('New text.');
    await mockedPage.getByTestId('config-save-button').click();
    await expect.poll(() => config.saves.length, { timeout: 5000 }).toBe(1);
    expect(config.saves[0].prompt_enabled).toBe(false);
  });

  // ===========================================================================
  // Untrusted content and layout
  // ===========================================================================

  test('a prompt full of markup renders as characters and executes nothing', async ({
    mockedPage,
  }) => {
    const hostile = [
      '<script>window.__pwned = "prompt"</script>',
      '<img src=x onerror="window.__pwned = \'img\'">',
      '<b>not bold</b> & <a href="javascript:void 0">not a link</a>',
    ].join('\n');

    await mockRoutes.agentConfig(mockedPage, {
      config: { body: hostile },
      versions: [
        {
          version_num: 3,
          event_type: 'updated',
          origin: 'authored',
          model_id: 'gpt-5.4-mini',
          note: '<img src=x onerror="window.__pwned = \'note\'">',
          has_body: true,
          scan_findings: [],
          changed_by_hash: 'c0ffee1234abcd',
          created_at: '2026-07-30T11:15:00Z',
        },
        {
          version_num: 2,
          event_type: 'updated',
          origin: 'authored',
          model_id: 'gpt-5.6-sol',
          note: null,
          has_body: true,
          scan_findings: [],
          changed_by_hash: 'c0ffee1234abcd',
          created_at: '2026-07-12T08:00:00Z',
        },
      ],
      versionBodies: {
        3: hostile,
        2: '<script>window.__pwned = "old version"</script>',
      },
    });
    await openConfigTab(mockedPage);

    // In the editor: the bytes that were stored, unchanged.
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      hostile
    );

    // In the preview: the same text, fenced, inside a <pre>.
    await mockedPage.getByText('What the model receives').click();
    const preview = mockedPage.getByTestId('prompt-preview-block');
    await expect(preview).toContainText('<script>window.__pwned = "prompt"');
    await expect(preview).toContainText('<agent_control_system_prompt>');

    // In the diff, which is where an off-the-shelf renderer would have emitted
    // highlighted HTML.
    await mockedPage.getByTestId('config-version-view-2').click();
    const diffBody = mockedPage.getByTestId('config-diff-body');
    await expect(diffBody).toContainText('window.__pwned = "old version"');

    const tab = mockedPage.getByTestId('agent-config-tab');
    expect(await tab.locator('script').count()).toBe(0);
    expect(await tab.locator('img').count()).toBe(0);
    expect(await mockedPage.locator('body script[src=""]').count()).toBe(0);
    expect(
      await mockedPage.evaluate(() => (window as Pwned).__pwned)
    ).toBeUndefined();
  });

  test('a prompt at the length limit does not push the page sideways', async ({
    mockedPage,
  }) => {
    // The cap exactly, with the first 20000 characters an unbroken run. That
    // second shape is the one that breaks layouts: a body full of newlines
    // wraps by itself, whereas one 20000-character token only wraps because
    // somebody asked for it to.
    const long = ('x'.repeat(20_000) + '\nline\n'.repeat(2000)).slice(
      0,
      32_000
    );
    // A different long body on version 2, so the diff has something to draw.
    const olderLong = ('y'.repeat(20_000) + '\nrow\n'.repeat(2000)).slice(
      0,
      32_000
    );
    await mockRoutes.agentConfig(mockedPage, {
      config: { body: long },
      versionBodies: { 1: long, 2: olderLong, 3: long },
    });
    await openConfigTab(mockedPage);

    await expectNoHorizontalOverflow(mockedPage);

    // The editor stops growing and scrolls inside itself instead.
    const editorHeight = await mockedPage
      .getByTestId('prompt-editor-input')
      .evaluate((el) => el.getBoundingClientRect().height);
    expect(editorHeight).toBeLessThan(1200);

    await expect(mockedPage.getByTestId('prompt-editor-counter')).toContainText(
      '32,000 of 32,000 characters'
    );

    // The preview wraps that unbroken run rather than growing sideways behind
    // an ancestor's scrollbar, which is what "does not break the layout"
    // actually means here: the page not overflowing is also true of a pane
    // that has quietly become 200000 pixels wide.
    await mockedPage.getByText('What the model receives').click();
    await expectWrapsRatherThanWidens(
      mockedPage.getByTestId('prompt-preview-block')
    );

    // Same for the diff, and the panel is still where it was.
    await mockedPage.getByTestId('config-version-view-2').click();
    await expect(mockedPage.getByTestId('config-diff')).toBeVisible();
    await expectWrapsRatherThanWidens(
      mockedPage.getByTestId('config-diff-body')
    );
    await expectNoHorizontalOverflow(mockedPage);
  });

  test('a long note in the history wraps instead of stretching the panel', async ({
    mockedPage,
  }) => {
    const note = 'z'.repeat(600);
    await mockRoutes.agentConfig(mockedPage, {
      versions: [
        {
          version_num: 3,
          event_type: 'updated',
          origin: 'authored',
          model_id: 'gpt-5.4-mini',
          note,
          has_body: true,
          scan_findings: [],
          changed_by_hash: 'c0ffee1234abcd',
          created_at: '2026-07-30T11:15:00Z',
        },
      ],
    });
    await openConfigTab(mockedPage);

    await expectWrapsRatherThanWidens(
      mockedPage.getByTestId('config-version-row-3')
    );
    await expectNoHorizontalOverflow(mockedPage);
  });

  test('the counter changes its tune three quarters of the way to the cap', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    const counter = mockedPage.getByTestId('prompt-editor-counter');
    const colourOf = () => counter.evaluate((el) => getComputedStyle(el).color);

    const calm = await colourOf();
    // The token figure is labelled an estimate wherever it appears, because
    // no tokeniser runs in this browser and the number is here to stop
    // somebody pasting a novel, not to predict a bill.
    await expect(counter).toContainText(
      `${mockData.storedPromptBody.length} of 32,000 characters · roughly ` +
        `${Math.ceil(mockData.storedPromptBody.length / 4)} tokens, ` +
        'estimated at four characters per token'
    );

    await mockedPage
      .getByTestId('prompt-editor-input')
      .fill('a'.repeat(24_000));
    await expect(counter).toContainText('24,000 of 32,000');
    const warned = await colourOf();
    expect(warned).not.toBe(calm);

    await mockedPage
      .getByTestId('prompt-editor-input')
      .fill('a'.repeat(32_001));
    const refused = await colourOf();
    expect(refused).not.toBe(warned);
  });

  test('past the server’s cap the editor says so and refuses to send it', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    await mockedPage
      .getByTestId('prompt-editor-input')
      .fill('y'.repeat(32_001));

    await expect(mockedPage.getByTestId('prompt-editor-counter')).toContainText(
      '32,001 of 32,000'
    );
    await expect(mockedPage.getByTestId('config-save-button')).toBeDisabled();
    expect(config.saves).toEqual([]);
  });

  test('renders in both colour schemes without the layout coming apart', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentConfig(mockedPage);
    await openConfigTab(mockedPage);

    for (const scheme of ['light', 'dark'] as const) {
      await mockedPage.emulateMedia({ colorScheme: scheme });
      await expect(mockedPage.locator('html')).toHaveAttribute(
        'data-mantine-color-scheme',
        scheme
      );

      await expect(mockedPage.getByTestId('model-select-input')).toBeVisible();
      await expect(mockedPage.getByTestId('prompt-editor-input')).toBeVisible();
      await expect(mockedPage.getByTestId('config-history')).toBeVisible();
      await expectNoHorizontalOverflow(mockedPage);

      // Text and its panel must not resolve to the same colour in either
      // scheme, which is the way a themed panel usually breaks.
      const [ink, paper] = await mockedPage
        .getByTestId('config-version-row-3')
        .evaluate((el) => {
          const style = getComputedStyle(el);
          const panel = getComputedStyle(
            el.closest('[data-testid="config-history"]') as HTMLElement
          );
          return [style.color, panel.backgroundColor];
        });
      expect(ink).not.toBe(paper);
    }
  });

  test('the tab is reachable by URL and by the tab list, and reads its own agent', async ({
    mockedPage,
  }) => {
    const config = await mockRoutes.agentConfig(mockedPage);
    const read: string[] = [];
    mockedPage.on('request', (request) => {
      const match = request
        .url()
        .match(/\/api\/v1\/agents\/([^/]+)\/config(\?|$)/);
      if (match && request.method() === 'GET') read.push(match[1]);
    });

    await mockedPage.goto(controlsUrl);
    await expect(mockedPage.getByTestId('agent-config-tab')).toHaveCount(0);
    // Not mounted, so nothing was read for a tab nobody opened.
    expect(read).toEqual([]);

    await mockedPage.getByTestId('agent-config-tab-trigger').click();
    await expect(mockedPage).toHaveURL(new RegExp('tab=config'));
    await expect(mockedPage.getByTestId('prompt-editor-input')).toHaveValue(
      mockData.storedPromptBody
    );

    // The row belongs to the agent the page resolved, not to the id in the URL.
    expect(read).toEqual([chatAgentName]);
    expect(config.configReads).toBe(1);
  });
});
