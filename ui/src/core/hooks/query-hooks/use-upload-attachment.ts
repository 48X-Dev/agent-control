import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useCallback, useRef } from 'react';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  Attachment,
  CreateAttachmentResponse,
  DeleteAttachmentResponse,
} from '@/core/api/types';

import { sessionAttachmentsQueryKey } from './use-session-attachments';

export type UploadVariables = {
  file: File;
  /**
   * The chip this upload belongs to.
   *
   * Cancellation is per upload and not per hook: two large files can be in
   * flight at once, and a single shared controller would have Cancel on the
   * first chip abort whichever request started last - the operator cancels one
   * file and watches a different one disappear.
   */
  uploadId: string;
  /** Called with 0..1 as the body goes out. */
  onProgress?: (fraction: number) => void;
};

/**
 * Thrown when the operator cancelled an upload in progress.
 *
 * Not a failure worth showing: they pressed the X. The chip disappears and
 * nothing is said, which is the same treatment abandoning a turn gets.
 */
export class UploadCancelledError extends Error {
  constructor() {
    super('Upload cancelled.');
    this.name = 'UploadCancelledError';
  }
}

export function isUploadCancelled(error: unknown): boolean {
  return error instanceof UploadCancelledError;
}

/**
 * Send one file to a session.
 *
 * Exposes `cancel(uploadId)` because an upload is the one request in this panel
 * that can take a visible amount of time on a slow connection, and a
 * twenty-megabyte PDF with no way out is a stuck composer. Cancelling aborts
 * that upload's request; the server writes the metadata row and the bytes in
 * one transaction, so a half-sent file leaves nothing behind.
 *
 * The controllers are held in a map rather than a single ref because the picker
 * allows three files and nothing stops the second being chosen while the first
 * is still going. With one shared controller, Cancel on the first chip aborts
 * the newest request instead, and the cancellation is reported to the wrong
 * chip - so the file that vanishes is not the one the operator cancelled. On a
 * slow connection with two large PDFs that is the ordinary case rather than a
 * race.
 */
export function useUploadAttachment(sessionKey: string | null) {
  const queryClient = useQueryClient();
  const abortRef = useRef(new Map<string, AbortController>());

  const mutation = useMutation<Attachment, Error, UploadVariables>({
    mutationFn: async ({ file, uploadId, onProgress }: UploadVariables) => {
      const controller = new AbortController();
      abortRef.current.set(uploadId, controller);
      try {
        const { data, error, response } =
          await api.agentSessions.uploadAttachment(sessionKey!, file, {
            onProgress,
            signal: controller.signal,
          });
        if (error) {
          throw parseApiError(
            error,
            'The file could not be attached',
            response?.status
          );
        }
        return (data as CreateAttachmentResponse).attachment;
      } catch (caught) {
        if (controller.signal.aborted) throw new UploadCancelledError();
        throw caught;
      } finally {
        abortRef.current.delete(uploadId);
      }
    },
    onSuccess: () => {
      if (!sessionKey) return;
      void queryClient.invalidateQueries({
        queryKey: sessionAttachmentsQueryKey(sessionKey),
      });
    },
  });

  const cancel = useCallback((uploadId: string) => {
    abortRef.current.get(uploadId)?.abort();
  }, []);

  return { ...mutation, cancel };
}

/**
 * Remove one file from a session.
 *
 * The bytes go and the record stays, which is what the confirmation copy says:
 * a model that already read this file has already read it, and the executor
 * keeps its own copy of the conversation until the session itself is deleted.
 */
export function useDeleteAttachment(sessionKey: string | null) {
  const queryClient = useQueryClient();

  return useMutation<DeleteAttachmentResponse, Error, string>({
    mutationFn: async (attachmentKey: string) => {
      const { data, error, response } =
        await api.agentSessions.deleteAttachment(sessionKey!, attachmentKey);
      if (error) {
        throw parseApiError(
          error,
          'The file could not be removed',
          response?.status
        );
      }
      return data!;
    },
    onSettled: () => {
      if (!sessionKey) return;
      void queryClient.invalidateQueries({
        queryKey: sessionAttachmentsQueryKey(sessionKey),
      });
    },
  });
}
