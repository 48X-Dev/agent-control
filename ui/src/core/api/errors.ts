/**
 * API error utilities for handling RFC 7807 ProblemDetail responses
 */

import type { ProblemDetail } from './types';

/**
 * Custom error class that includes the ProblemDetail response
 */
export class ApiError extends Error {
  constructor(
    message: string,
    public problemDetail: ProblemDetail
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * Create a fallback ProblemDetail for unexpected errors
 */
export function createFallbackProblemDetail(
  message: string,
  status = 500
): ProblemDetail {
  return {
    type: 'about:blank',
    title: 'Error',
    status,
    detail: message,
    error_code: 'UNKNOWN_ERROR',
    reason: 'Unknown',
  };
}

/**
 * Parse an error response into an ApiError
 * Handles both ProblemDetail responses and generic errors
 */
export function parseApiError(
  error: unknown,
  fallbackMessage: string,
  status?: number
): ApiError {
  // Check if error is already a ProblemDetail
  const problemDetail = error as Partial<ProblemDetail>;
  if (problemDetail?.detail && problemDetail?.error_code) {
    return new ApiError(problemDetail.detail, problemDetail as ProblemDetail);
  }

  // Fallback for unexpected error format
  return new ApiError(
    fallbackMessage,
    createFallbackProblemDetail(fallbackMessage, status)
  );
}

/**
 * Check if an error is an ApiError
 */
export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

/**
 * HTTP status carried by a thrown error, when it has one.
 *
 * Accepts both an {@link ApiError} and a raw ProblemDetail body, since query
 * hooks throw the response body directly.
 */
export function getErrorStatus(error: unknown): number | undefined {
  if (isApiError(error)) return error.problemDetail.status;

  const status = (error as Partial<ProblemDetail> | null | undefined)?.status;
  return typeof status === 'number' ? status : undefined;
}

/**
 * The `error_code` the server sent, when it sent one.
 *
 * Worth having beside {@link getErrorStatus} because a status is sometimes
 * ambiguous where the code is not: a restore can answer 409 because the
 * editor's version is stale, because the stored body format is one the server
 * no longer understands, or because the version names a model that has left
 * the allowlist. Only the last of those is fixable without reloading, so the
 * three cannot be told apart by their status.
 */
export function getErrorCode(error: unknown): string | undefined {
  if (isApiError(error)) return error.problemDetail.error_code;

  const code = (error as Partial<ProblemDetail> | null | undefined)?.error_code;
  return typeof code === 'string' ? code : undefined;
}

/** True when the server answered 404. */
export function isNotFoundError(error: unknown): boolean {
  return getErrorStatus(error) === 404;
}

/**
 * True when the server answered 403.
 *
 * Worth its own helper because a 403 is a routine, expected answer on the
 * admin-gated surfaces: a read-only key can see an agent's configuration and
 * its history but cannot list the model allowlist or save. Surfacing it as a
 * sentence beats retrying a refusal that will not change.
 */
export function isForbiddenError(error: unknown): boolean {
  return getErrorStatus(error) === 403;
}

/**
 * True when the server answered 409.
 *
 * On the agent-config surface this means somebody else wrote the row since
 * this editor loaded it. The response carries the real `current_version`, so
 * the caller can offer reload-and-reapply rather than a dead end.
 */
export function isConflictError(error: unknown): boolean {
  return getErrorStatus(error) === 409;
}
