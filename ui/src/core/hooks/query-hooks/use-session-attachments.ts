import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isNotFoundError, parseApiError } from '@/core/api/errors';
import type { Attachment, ListAttachmentsResponse } from '@/core/api/types';

/** Files one turn may carry, matching `ATTACHMENT_MAX_PER_TURN` on the server. */
export const ATTACHMENT_MAX_PER_TURN = 3;

/** Per-file ceiling, matching this deployment's `attachment_max_bytes` default. */
export const ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;

/**
 * What the upload route accepts, and the sentence to show when it will not.
 *
 * The list is short because it is exactly what the server's sniffer can name
 * and the model can use. Checking it here saves a round trip; the server checks
 * it again on the bytes rather than on the declared type, which is the check
 * that actually decides.
 */
export const ATTACHMENT_ACCEPT =
  'application/pdf,image/png,image/jpeg,image/webp';

export const ATTACHMENT_TYPE_HELP =
  'PDF, PNG, JPEG and WebP. Word and PowerPoint are not accepted: use File, ' +
  'Export, PDF and attach that, and the same goes for Google Slides. For ' +
  'plain text, markdown or CSV, paste the contents into the message instead.';

export function sessionAttachmentsQueryKey(sessionKey: string) {
  return ['agent-sessions', 'attachments', sessionKey] as const;
}

/** True when this file can actually be carried by a turn. */
export function isAttachmentSendable(attachment: Attachment): boolean {
  return attachment.status === 'ready';
}

/**
 * The files already stored against one session.
 *
 * Not polled. Attachments change when somebody in this panel uploads or removes
 * one, and both of those invalidate this key; a session nobody is touching does
 * not need a request every two seconds for the rest of the afternoon.
 *
 * The composer reads it to keep its chips honest. A chip is built from the row
 * the upload answered with, and that row is a snapshot: the same file can be
 * removed from another tab or tombstoned by the blob sweep, and both leave a
 * status here that the chip would otherwise never see. Without this the send
 * button would go on offering a file the server refuses, which is the 409 the
 * warning above the button exists to pre-empt.
 */
export function useSessionAttachments(sessionKey: string | null) {
  return useQuery<ListAttachmentsResponse>({
    queryKey: sessionAttachmentsQueryKey(sessionKey ?? ''),
    queryFn: async () => {
      const { data, error, response } = await api.agentSessions.listAttachments(
        sessionKey!
      );
      if (error) {
        throw parseApiError(
          error,
          'The attachments could not be read',
          response?.status
        );
      }
      return data!;
    },
    enabled: Boolean(sessionKey),
    retry: (attempt, error) => !isNotFoundError(error) && attempt < 1,
  });
}
