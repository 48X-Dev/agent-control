/// <reference path="./playwright-jsx-runtime.d.ts" />
/** @jsxImportSource playwright */

/**
 * Component tests for the transcript renderer.
 *
 * These mount the real component in a real browser with no server and no
 * mocking layer in between, which is the only place the rendering rule can be
 * tested as a rule: given these exact bytes, this exact DOM. The integration
 * spec proves the panel wires it up; this proves what it does with what it is
 * handed.
 */

import { expect, test } from '@playwright/experimental-ct-react';

import type { SessionMessage } from '../../src/core/api/types';
import { MessageList } from '../../src/core/page-components/agent-detail/agent-chat/message-list';

type Pwned = Window & { __pwned?: string };

const noop = () => {};

function textMessage(index: number, text: string): SessionMessage {
  return {
    index,
    role: index % 2 === 0 ? 'user' : 'agent',
    author: 'support-bot',
    timestamp: '2024-03-02T09:00:00Z',
    parts: [{ kind: 'text', text }],
  };
}

test.describe('MessageList (component)', () => {
  test('renders a text part verbatim, newlines and all', async ({
    mount,
    page,
  }) => {
    await mount(
      <MessageList
        messages={[textMessage(0, 'first line\nsecond line')]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );

    const part = page.getByTestId('chat-part-0-0');
    await expect(part).toHaveText('first line\nsecond line');
    // Preserved by CSS, not by inserting markup for the break.
    await expect(part).toHaveCSS('white-space', 'pre-wrap');
    await expect(part.locator('br')).toHaveCount(0);
  });

  test('renders markdown syntax as the characters that were sent', async ({
    mount,
    page,
  }) => {
    await mount(
      <MessageList
        messages={[
          textMessage(
            0,
            '**bold** _italic_ [link](https://example.invalid) `code`'
          ),
        ]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );

    const transcript = page.getByTestId('chat-transcript');
    await expect(page.getByTestId('chat-part-0-0')).toHaveText(
      '**bold** _italic_ [link](https://example.invalid) `code`'
    );
    await expect(transcript.locator('strong, b, em, i, code, a')).toHaveCount(
      0
    );
  });

  test('renders a script tag and an onerror image without executing either', async ({
    mount,
    page,
  }) => {
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));

    await mount(
      <MessageList
        messages={[
          textMessage(0, "<script>window.__pwned = 'script'</script>"),
          {
            index: 1,
            role: 'agent',
            author: 'support-bot',
            timestamp: '2024-03-02T09:00:01Z',
            parts: [
              {
                kind: 'text',
                text: '<img src=x onerror="window.__pwned = \'img\'">',
              },
              {
                kind: 'tool_result',
                tool_name: 'fetch_page',
                tool_call_id: 'call-1',
                result: {
                  body: "<script>window.__pwned = 'tool'</script>",
                  image: '<img src=y onerror="window.__pwned = \'toolimg\'">',
                },
              },
            ],
          },
        ]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );

    // Expand the tool payload so the hostile bytes are in the document too.
    await page.getByTestId('chat-part-1-1-toggle').click();
    await expect(page.getByTestId('chat-part-1-1-json')).toContainText(
      "<script>window.__pwned = 'tool'</script>"
    );

    const transcript = page.getByTestId('chat-transcript');
    await expect(page.getByTestId('chat-part-0-0')).toHaveText(
      "<script>window.__pwned = 'script'</script>"
    );
    await expect(page.getByTestId('chat-part-1-0')).toHaveText(
      '<img src=x onerror="window.__pwned = \'img\'">'
    );
    await expect(transcript.locator('script')).toHaveCount(0);
    await expect(transcript.locator('img')).toHaveCount(0);

    expect(await page.evaluate(() => (window as Pwned).__pwned)).toBe(
      undefined
    );
    expect(errors).toEqual([]);
  });

  test('renders a tool call collapsed and expands it to the raw arguments', async ({
    mount,
    page,
  }) => {
    await mount(
      <MessageList
        messages={[
          {
            index: 0,
            role: 'agent',
            author: 'support-bot',
            timestamp: '2024-03-02T09:00:00Z',
            parts: [
              {
                kind: 'tool_call',
                tool_name: 'send_email',
                tool_call_id: 'call-1',
                arguments: { to: 'ops@example.invalid', subject: 'Report' },
              },
            ],
          },
        ]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );

    const toggle = page.getByTestId('chat-part-0-0-toggle');
    await expect(page.getByTestId('chat-part-0-0')).toContainText('Called');
    await expect(page.getByTestId('chat-part-0-0')).toContainText('send_email');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByTestId('chat-part-0-0-json')).toHaveCount(0);

    await toggle.click();

    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const raw =
      (await page.getByTestId('chat-part-0-0-json').textContent()) ?? '';
    expect(JSON.parse(raw)).toEqual({
      to: 'ops@example.invalid',
      subject: 'Report',
    });

    await toggle.click();
    await expect(page.getByTestId('chat-part-0-0-json')).toHaveCount(0);
  });

  test('labels a tool result and renders a missing payload rather than nothing', async ({
    mount,
    page,
  }) => {
    await mount(
      <MessageList
        messages={[
          {
            index: 0,
            role: 'agent',
            timestamp: '2024-03-02T09:00:00Z',
            parts: [
              {
                kind: 'tool_result',
                tool_name: 'fetch_page',
                tool_call_id: 'call-1',
                result: null,
              },
              { kind: 'unsupported' },
            ],
          },
        ]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );

    await expect(page.getByTestId('chat-part-0-0')).toContainText('Returned');
    await page.getByTestId('chat-part-0-0-toggle').click();
    await expect(page.getByTestId('chat-part-0-0-json')).toHaveText(
      'No payload.'
    );

    // A part kind this build does not know about says so instead of vanishing.
    await expect(page.getByTestId('chat-part-0-1')).toContainText(
      'This message contained something this panel cannot display.'
    );
  });

  test('caps a large tool payload instead of pasting all of it into the DOM', async ({
    mount,
    page,
  }) => {
    await mount(
      <MessageList
        messages={[
          {
            index: 0,
            role: 'agent',
            timestamp: '2024-03-02T09:00:00Z',
            parts: [
              {
                kind: 'tool_result',
                tool_name: 'dump',
                tool_call_id: 'call-1',
                result: { blob: 'x'.repeat(60000) },
              },
            ],
          },
        ]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );

    await page.getByTestId('chat-part-0-0-toggle').click();
    const rendered =
      (await page.getByTestId('chat-part-0-0-json').textContent()) ?? '';
    expect(rendered.length).toBeLessThan(21000);
    expect(rendered).toContain('truncated');
  });

  test('offers "load earlier" only when something precedes the window', async ({
    mount,
    page,
  }) => {
    const component = await mount(
      <MessageList
        messages={[textMessage(0, 'only message')]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice={null}
      />
    );
    await expect(page.getByTestId('chat-load-earlier')).toHaveCount(0);
    await expect(page.getByTestId('chat-jump-to-latest')).toHaveCount(0);

    await component.update(
      <MessageList
        messages={[textMessage(0, 'only message')]}
        hasEarlier
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing={false}
        isLoading={false}
        notice={null}
      />
    );
    await expect(page.getByTestId('chat-load-earlier')).toBeVisible();
    // Not tailing means there is an explicit way back to the end.
    await expect(page.getByTestId('chat-jump-to-latest')).toBeVisible();
  });

  test('renders a transcript notice above an empty transcript', async ({
    mount,
    page,
  }) => {
    await mount(
      <MessageList
        messages={[]}
        hasEarlier={false}
        onLoadEarlier={noop}
        onJumpToLatest={noop}
        isTailing
        isLoading={false}
        notice="The executor no longer holds this chat."
      />
    );

    await expect(page.getByTestId('chat-transcript-notice')).toContainText(
      'The executor no longer holds this chat.'
    );
    await expect(page.getByTestId('chat-transcript-empty')).toBeVisible();
  });
});
