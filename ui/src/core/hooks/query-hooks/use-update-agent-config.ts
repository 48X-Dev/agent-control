import { useMutation, useQueryClient } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { parseApiError } from '@/core/api/errors';
import type {
  ClearAgentConfigFieldResponse,
  SetAgentConfigRequest,
  SetAgentConfigResponse,
  SetPromptEnabledRequest,
} from '@/core/api/types';

import { agentConfigQueryKey } from './use-agent-config';
import { agentConfigVersionsQueryKey } from './use-agent-config-versions';

/**
 * Re-read the row and its history after a write.
 *
 * Both, always. A save that only refreshed the row would leave a history panel
 * claiming the version it just created does not exist, and the version number
 * is what the next write's `expected_version` is built from.
 */
function useInvalidateAgentConfig(agentName: string) {
  const queryClient = useQueryClient();
  return () => {
    void queryClient.invalidateQueries({
      queryKey: agentConfigQueryKey(agentName),
    });
    void queryClient.invalidateQueries({
      queryKey: agentConfigVersionsQueryKey(agentName),
    });
  };
}

/**
 * Save the prompt, the model, or both, as one version.
 *
 * `expected_version` is required and compared under a row lock on the server.
 * One row carries both fields, so a prompt edit and a model edit conflict with
 * each other; that is correct, they are one version. A 409 comes back naming
 * the real current version and the caller offers reload-and-reapply rather
 * than quietly overwriting a colleague's paragraph.
 *
 * A field left out of the request is left alone rather than nulled. Removing
 * one is an explicit clear.
 */
export function useUpdateAgentConfig(agentName: string) {
  const invalidate = useInvalidateAgentConfig(agentName);

  return useMutation<SetAgentConfigResponse, Error, SetAgentConfigRequest>({
    mutationFn: async (request) => {
      const { data, error, response } = await api.agentConfigs.set(
        agentName,
        request
      );
      if (error) {
        throw parseApiError(
          error,
          'The configuration was not saved',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: invalidate,
  });
}

/**
 * Stop using the managed prompt. The agent falls back to what its code says.
 *
 * Idempotent: clearing a prompt that is already absent answers
 * `cleared: false` and writes no version row.
 */
export function useClearAgentPrompt(agentName: string) {
  const invalidate = useInvalidateAgentConfig(agentName);

  return useMutation<
    ClearAgentConfigFieldResponse,
    Error,
    { expected_version: number; note?: string | null }
  >({
    mutationFn: async (request) => {
      const { data, error, response } = await api.agentConfigs.clearPrompt(
        agentName,
        request
      );
      if (error) {
        throw parseApiError(
          error,
          'The prompt was not cleared',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: invalidate,
  });
}

/** Stop using the managed model. The agent falls back to its code's choice. */
export function useClearAgentModel(agentName: string) {
  const invalidate = useInvalidateAgentConfig(agentName);

  return useMutation<
    ClearAgentConfigFieldResponse,
    Error,
    { expected_version: number; note?: string | null }
  >({
    mutationFn: async (request) => {
      const { data, error, response } = await api.agentConfigs.clearModel(
        agentName,
        request
      );
      if (error) {
        throw parseApiError(
          error,
          'The model was not cleared',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: invalidate,
  });
}

/**
 * Switch managed-prompt delivery on or off without touching the body.
 *
 * Writes a version row even though no text changed, so the history explains a
 * behaviour change that involved no edit.
 */
export function useSetPromptEnabled(agentName: string) {
  const invalidate = useInvalidateAgentConfig(agentName);

  return useMutation<SetAgentConfigResponse, Error, SetPromptEnabledRequest>({
    mutationFn: async (request) => {
      const { data, error, response } = await api.agentConfigs.setPromptEnabled(
        agentName,
        request
      );
      if (error) {
        throw parseApiError(
          error,
          'Prompt delivery was not changed',
          response?.status
        );
      }
      return data!;
    },
    onSuccess: invalidate,
  });
}
