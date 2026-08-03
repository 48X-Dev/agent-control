import { Box, Group, Progress, Text } from '@mantine/core';
import { Button } from '@rungalileo/jupiter-ds';

import type { Attachment } from '@/core/api/types';

/**
 * One file on the composer, in one of three states.
 *
 * **This component renders plain text and nothing else.** `display_name` is
 * chosen by whoever uploaded the file - and on the tracker path by whoever
 * filed the issue - and this console's session cookie is a credential on every
 * admin endpoint in the product. There is no sanitizer here and there is not
 * meant to be one: React escapes a text node, so the file called
 * `<img src=x onerror=alert(1)>` renders as those characters and does nothing.
 * No markdown, no raw-HTML escape hatch, and no link built from a filename.
 * CI greps this directory for the name of that escape hatch, which is why this
 * comment describes it instead of spelling it.
 *
 * The name is truncated by CSS rather than by slicing the string. Slicing at a
 * fixed length can cut a surrogate pair in half, and the replacement character
 * that produces looks like corruption in a file somebody just uploaded.
 */

export type ChipState =
  | { kind: 'uploading'; id: string; name: string; progress: number }
  | { kind: 'ready'; id: string; attachment: Attachment }
  | { kind: 'failed'; id: string; name: string; message: string };

function humanBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** What the server says about a file that is stored but not sendable. */
function statusNote(attachment: Attachment): string | null {
  switch (attachment.status) {
    case 'ready':
      return null;
    case 'pending':
    case 'converting':
      return 'checking file';
    case 'tombstoned':
      return 'its contents were reclaimed; attach it again';
    default:
      return attachment.failure_code
        ? `not sendable (${attachment.failure_code})`
        : 'not sendable';
  }
}

export function AttachmentChip({
  state,
  onCancel,
  onRemove,
  onDismiss,
}: {
  state: ChipState;
  /** Cancel this chip's upload, named so a second one in flight is untouched. */
  onCancel: (id: string) => void;
  onRemove: (attachmentKey: string) => void;
  onDismiss: (id: string) => void;
}) {
  if (state.kind === 'uploading') {
    return (
      <Box data-testid="chat-attachment-chip" data-state="uploading" maw={280}>
        <Group gap="xs" wrap="nowrap">
          <Text
            size="xs"
            lineClamp={1}
            title={state.name}
            style={{ minWidth: 0 }}
          >
            {state.name}
          </Text>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onCancel(state.id)}
            data-testid="chat-attachment-cancel"
          >
            Cancel
          </Button>
        </Group>
        <Progress
          value={Math.round(state.progress * 100)}
          size="xs"
          aria-label={`Uploading ${state.name}`}
        />
      </Box>
    );
  }

  if (state.kind === 'failed') {
    return (
      <Box data-testid="chat-attachment-chip" data-state="failed" maw={320}>
        <Group gap="xs" wrap="nowrap">
          <Text
            size="xs"
            lineClamp={1}
            title={state.name}
            style={{ minWidth: 0 }}
          >
            {state.name}
          </Text>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDismiss(state.id)}
            data-testid="chat-attachment-dismiss"
          >
            Dismiss
          </Button>
        </Group>
        {/* The server's own sentence. It writes every refusal by hand and
            never echoes an upstream body, so passing its words through is both
            safer and more accurate than paraphrasing here. */}
        <Text size="xs" c="red" data-testid="chat-attachment-error">
          {state.message}
        </Text>
      </Box>
    );
  }

  const { attachment } = state;
  const note = statusNote(attachment);
  return (
    <Box
      data-testid="chat-attachment-chip"
      data-state={attachment.status}
      maw={320}
    >
      <Group gap="xs" wrap="nowrap">
        <Text
          size="xs"
          lineClamp={1}
          title={attachment.display_name}
          style={{ minWidth: 0, whiteSpace: 'pre-wrap' }}
          data-testid="chat-attachment-name"
        >
          {attachment.display_name}
        </Text>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onRemove(attachment.attachment_key)}
          data-testid="chat-attachment-remove"
        >
          Remove
        </Button>
      </Group>
      <Text size="xs" c="dimmed">
        {attachment.sniffed_mime} · {humanBytes(attachment.size_bytes)}
        {note ? ` · ${note}` : ''}
        {attachment.display_name_normalized ? ' · renamed for display' : ''}
      </Text>
    </Box>
  );
}
