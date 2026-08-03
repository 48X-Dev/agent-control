import { Button } from '@rungalileo/jupiter-ds';
import { useRef } from 'react';

import {
  ATTACHMENT_ACCEPT,
  ATTACHMENT_MAX_BYTES,
} from '@/core/hooks/query-hooks/use-session-attachments';

/**
 * The attach control: a button, a hidden input, and two client-side refusals.
 *
 * Both refusals are conveniences and neither is the check that decides. The
 * server sniffs the magic bytes and counts the streamed body, because a
 * declared type is advisory and a browser is not a trust boundary. Doing it
 * here as well saves somebody a twenty-megabyte round trip to be told no.
 *
 * `accept` on the input is a filter in the file dialog, not a guarantee: a
 * person can always choose "all files", which is why it is paired with the
 * type refusal below rather than relied on.
 */
export function AttachmentPicker({
  disabled,
  onPick,
  onRefuse,
}: {
  disabled: boolean;
  onPick: (file: File) => void;
  /** Told the sentence to show. Never a code. */
  onRefuse: (name: string, message: string) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    // Cleared before anything else, so choosing the same file twice in a row
    // still fires a change event.
    event.currentTarget.value = '';
    if (!file) return;

    if (file.size === 0) {
      onRefuse(file.name, 'This file is empty, so there is nothing to send.');
      return;
    }
    if (file.size > ATTACHMENT_MAX_BYTES) {
      onRefuse(
        file.name,
        `This file is larger than the ${Math.round(
          ATTACHMENT_MAX_BYTES / (1024 * 1024)
        )} MB limit on this server.`
      );
      return;
    }
    onPick(file);
  };

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={ATTACHMENT_ACCEPT}
        onChange={handleChange}
        style={{ display: 'none' }}
        data-testid="chat-attachment-input"
      />
      <Button
        variant="ghost"
        size="sm"
        disabled={disabled}
        onClick={() => inputRef.current?.click()}
        data-testid="chat-attach"
      >
        Attach
      </Button>
    </>
  );
}
