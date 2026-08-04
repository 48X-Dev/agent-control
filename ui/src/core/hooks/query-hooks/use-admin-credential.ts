import { isForbiddenError } from '@/core/api/errors';

import { useAgentModels } from './use-agent-models';

/**
 * Whether this credential holds the admin tier.
 *
 * The login response carries `is_admin`, but only for a login made in this
 * tab: a session resumed from its cookie reports null, because `/api/config`
 * says a session exists and never says what tier its key holds. That is the
 * common case for an open console, so gating admin-only controls on the login
 * response alone leaves them enabled for everyone who reloaded the page.
 *
 * `GET /agent-models` answers instead. It is gated on `AGENT_CONFIGS_WRITE`,
 * which sits at `AccessLevel.ADMIN` alongside `TEAMS_WRITE` and every other
 * admin operation, so its 403 is the same 403 an admin-gated write would give
 * back - and it costs nothing to ask, because a refused probe is not retried
 * and the answer is cached for the whole session.
 *
 * Undefined while the probe is in flight, and true on any failure that is not
 * a refusal: a network or server fault is not a statement about this
 * credential, and guessing "no" from one would hide a control from an admin.
 */
export function useHasAdminCredential(): boolean | undefined {
  const query = useAgentModels();

  if (query.isPending) return undefined;
  return !isForbiddenError(query.error);
}
