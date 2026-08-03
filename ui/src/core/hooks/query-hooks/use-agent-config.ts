import { useQuery } from '@tanstack/react-query';

import { api } from '@/core/api/client';
import { isNotFoundError, parseApiError } from '@/core/api/errors';
import type { GetAgentConfigResponse } from '@/core/api/types';

/** The server's cap on a stored body, matching `BODY_MAX_LENGTH`. */
export const PROMPT_BODY_MAX_LENGTH = 32_000;

/** Where the editor starts warning that the body is getting long. */
export const PROMPT_BODY_WARN_AT = Math.floor(PROMPT_BODY_MAX_LENGTH * 0.75);

/** The server's cap on a version note, matching `NOTE_MAX_LENGTH`. */
export const CONFIG_NOTE_MAX_LENGTH = 500;

/**
 * How long a save takes to reach a running agent, in words the UI reuses.
 *
 * Not a promise that it has been applied. Agents poll on their own refresh
 * interval, default 60 seconds, and pick a change up at their next model call
 * after that. A call already dispatched is untouched.
 */
export const CONFIG_PICKUP_COPY = 'Agents pick this up within about 60 seconds';

export function agentConfigQueryKey(agentName: string) {
  return ['agent-config', agentName] as const;
}

/**
 * One agent's system prompt and model, resolved against server state.
 *
 * `prompt_source` and `model_source` are decided by the server, once, against
 * the current allowlist and the current delivery gate. Nothing here re-derives
 * them: a page that computed "managed" from a non-null body would claim
 * delivery on a server where delivery is switched off.
 *
 * An unknown agent is not retried. A 404 will not become a 200 on the way back.
 */
export function useAgentConfig(agentName: string) {
  return useQuery<GetAgentConfigResponse>({
    queryKey: agentConfigQueryKey(agentName),
    queryFn: async () => {
      const { data, error, response } = await api.agentConfigs.get(agentName);
      if (error) {
        throw parseApiError(
          error,
          'Failed to load this agent’s configuration',
          response?.status
        );
      }
      return data!;
    },
    enabled: Boolean(agentName),
    retry: (failureCount, error) => !isNotFoundError(error) && failureCount < 1,
  });
}
