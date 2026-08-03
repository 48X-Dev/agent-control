import { getAgentRoute } from '@/core/constants/agent-routes';

import { expect, mockRoutes, test } from './fixtures';

const chatUrl = getAgentRoute('agent-1', { tab: 'chat' });
const SESSION = 'sess-refunds';

const PDF_BYTES = Buffer.from(
  '%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\n%%EOF\n'
);

async function attach(
  page: Parameters<typeof mockRoutes.agentSessions>[0],
  name: string,
  bytes: Buffer = PDF_BYTES
) {
  await page
    .getByTestId('chat-attachment-input')
    .setInputFiles({ name, mimeType: 'application/pdf', buffer: bytes });
}

test.describe('Agent chat: attaching a file', () => {
  test('an attached file rides the turn as a key, and the message is unchanged', async ({
    mockedPage,
  }) => {
    // Keys, never bytes. The server resolves them against rows this caller had
    // to be authorized to create, so nothing a browser says decides what the
    // model reads.
    const chat = await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'q3-forecast.pdf');
    await expect(mockedPage.getByTestId('chat-attachment-name')).toHaveText(
      'q3-forecast.pdf'
    );

    await mockedPage.getByTestId('chat-composer-input').fill('Summarise this');
    await mockedPage.getByTestId('chat-send').click();

    expect(chat.turns).toEqual([
      { sessionKey: SESSION, message: 'Summarise this' },
    ]);
    expect(chat.turnAttachmentKeys).toEqual([
      { sessionKey: SESSION, keys: [expect.stringContaining('att-')] },
    ]);
    // The chips clear with the draft, so the next message does not re-send a
    // file the operator has already sent once.
    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(0);
  });

  test('a turn with no files sends no attachment field at all', async ({
    mockedPage,
  }) => {
    // The regression guard for every existing caller of this endpoint: an
    // empty list and an absent field are different bodies, and only one of
    // them is what this route has always been sent.
    const chat = await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await mockedPage.getByTestId('chat-composer-input').fill('No files here');
    await mockedPage.getByTestId('chat-send').click();

    expect(chat.turnAttachmentKeys).toEqual([
      { sessionKey: SESSION, keys: undefined },
    ]);
  });

  test('a filename that looks like markup renders as text', async ({
    mockedPage,
  }) => {
    // The console's session cookie is a credential on every admin endpoint in
    // this product, and a filename is chosen by whoever uploaded the file. If
    // this ever renders as markup, a stored XSS here escalates straight to
    // ADMIN. React escapes a text node; this asserts nobody has reached past
    // it for a preview library.
    await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    const hostile = '<img src=x onerror=alert(1)>.pdf';
    await attach(mockedPage, hostile);

    const chip = mockedPage.getByTestId('chat-attachment-chip');
    await expect(chip.getByTestId('chat-attachment-name')).toHaveText(hostile);
    // Rendered as characters, not as an element, so no image was created.
    await expect(chip.locator('img')).toHaveCount(0);
  });

  test('a file the server refuses says why, and the turn is not blocked by it', async ({
    mockedPage,
  }) => {
    // The server's own sentence. It writes every refusal by hand and never
    // echoes an upstream body, so passing its words through is both safer and
    // more accurate than paraphrasing in the panel.
    await mockRoutes.agentSessions(mockedPage, {
      attachmentUploadError: {
        status: 415,
        errorCode: 'VALIDATION_ERROR',
        title: 'Unsupported Media Type',
        detail:
          'This file is a application/zip and this deployment accepts PDF, ' +
          'PNG, JPEG and WebP. If it is a Word or PowerPoint document, export ' +
          'it to PDF and attach that.',
      },
    });
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'deck.pptx');
    await expect(mockedPage.getByTestId('chat-attachment-error')).toContainText(
      'export it to PDF and attach that'
    );
  });

  test('the send button says a file will not be sent before the click', async ({
    mockedPage,
  }) => {
    // The chat panel's deliberate asymmetry with the dispatch path: nobody is
    // watching a chain, so an undelivered file there becomes a line the agent
    // reads. Here the operator is right in front of it, so they are told and
    // the turn does not start - finding out from a 409 after pressing send is
    // a worse way to learn the same thing.
    const chat = await mockRoutes.agentSessions(mockedPage, {
      attachmentUploadError: {
        status: 413,
        errorCode: 'ATTACHMENT_TOO_LARGE',
        title: 'Attachment Too Large',
        detail: 'That file is larger than this server accepts.',
      },
    });
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'huge.pdf');
    await expect(
      mockedPage.getByTestId('chat-attachment-warning')
    ).toContainText('1 file will not be sent');

    await mockedPage.getByTestId('chat-composer-input').fill('Read this');
    await expect(mockedPage.getByTestId('chat-send')).toBeDisabled();
    expect(chat.turns).toEqual([]);
  });

  test('dismissing a refused file lets the turn go again', async ({
    mockedPage,
  }) => {
    const chat = await mockRoutes.agentSessions(mockedPage, {
      attachmentUploadError: {
        status: 413,
        errorCode: 'ATTACHMENT_TOO_LARGE',
        title: 'Attachment Too Large',
        detail: 'That file is larger than this server accepts.',
      },
    });
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'huge.pdf');
    await mockedPage.getByTestId('chat-attachment-dismiss').click();
    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(0);

    await mockedPage.getByTestId('chat-composer-input').fill('Never mind');
    await mockedPage.getByTestId('chat-send').click();
    expect(chat.turns).toEqual([
      { sessionKey: SESSION, message: 'Never mind' },
    ]);
  });

  test('removing an attached file takes it off the server too', async ({
    mockedPage,
  }) => {
    const chat = await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'notes.pdf');
    await mockedPage.getByTestId('chat-attachment-remove').click();

    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(0);
    expect(chat.attachmentsDeleted).toHaveLength(1);
    expect(chat.attachmentsDeleted[0].sessionKey).toBe(SESSION);
  });

  test('the picker refuses an empty file without a round trip', async ({
    mockedPage,
  }) => {
    // A convenience, not the check that decides: the server refuses zero bytes
    // with a `CHECK` constraint and a 400 either way. Doing it here saves the
    // request and says the same sentence.
    const chat = await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'nothing.pdf', Buffer.alloc(0));
    await expect(mockedPage.getByTestId('chat-attachment-error')).toContainText(
      'This file is empty'
    );
    expect(chat.attachmentsUploaded).toEqual([]);
  });

  test('the panel says what is accepted and what to do instead', async ({
    mockedPage,
  }) => {
    // Word, PowerPoint and Google Slides are the three things somebody will
    // try first, and the answer for all three is the same one sentence.
    await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await expect(mockedPage.getByTestId('chat-attachment-help')).toContainText(
      'Word and PowerPoint are not accepted'
    );
    await expect(mockedPage.getByTestId('chat-attachment-help')).toContainText(
      'paste the contents into the message instead'
    );
  });

  test('a file the server has moved on from stops being offered', async ({
    mockedPage,
  }) => {
    // A chip is built from the row the upload answered with, and that row is a
    // snapshot. The same file can be removed from another tab or tombstoned by
    // the blob sweep; both leave a status the panel would never see if it only
    // ever trusted its own upload response. The warning above the send button
    // is the whole point of reading the list back: the alternative is a 409 at
    // send, which is what that warning exists to pre-empt.
    const chat = await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'reclaimed.pdf');
    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(1);
    await expect(mockedPage.getByTestId('chat-attachment-warning')).toHaveCount(
      0
    );

    chat.attachments[SESSION][0].status = 'tombstoned';
    // The second upload invalidates the list, which is when the panel re-reads
    // it. Nothing here polls: a session nobody is touching does not need a
    // request every two seconds for the rest of the afternoon.
    await attach(mockedPage, 'fresh.pdf');

    await expect(
      mockedPage.getByTestId('chat-attachment-warning')
    ).toContainText('1 file will not be sent');
    await mockedPage.getByTestId('chat-composer-input').fill('Read these');
    await expect(mockedPage.getByTestId('chat-send')).toBeDisabled();
    expect(chat.turns).toEqual([]);
  });

  test('switching chats leaves the files behind with the conversation', async ({
    mockedPage,
  }) => {
    // An attachment key resolves only against the session it was uploaded to,
    // so a chip carried across a switch would post a key the next session 404s
    // - refusing the turn after the draft had already been cleared, which is
    // the one way this panel is not supposed to be able to lose a message.
    const chat = await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'refunds.pdf');
    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(1);

    await mockedPage.getByTestId('chat-session-switcher').click();
    await mockedPage
      .getByRole('option', { name: 'Onboarding checklist' })
      .click();

    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(0);

    await mockedPage.getByTestId('chat-composer-input').fill('Anything else?');
    await mockedPage.getByTestId('chat-send').click();

    expect(chat.turnAttachmentKeys).toEqual([
      { sessionKey: 'sess-onboarding', keys: undefined },
    ]);
  });

  test('the attach button stops offering once a turn can carry no more', async ({
    mockedPage,
  }) => {
    await mockRoutes.agentSessions(mockedPage);
    await mockedPage.goto(chatUrl);

    await attach(mockedPage, 'one.pdf');
    await attach(mockedPage, 'two.pdf');
    await attach(mockedPage, 'three.pdf');
    await expect(mockedPage.getByTestId('chat-attachment-chip')).toHaveCount(3);
    await expect(mockedPage.getByTestId('chat-attach')).toBeDisabled();
  });
});
