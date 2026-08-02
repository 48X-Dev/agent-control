import type { Halt } from '@/core/api/types';
import { HALT_STALL_WARNING_MS } from '@/core/hooks/query-hooks/use-halts';

import type { HaltState } from './message-composer';
import { useNow } from './use-now';

/**
 * What the panel should say about the stop on the live turn.
 *
 * The distinction this exists to keep is between what the executor claims and
 * what this server has seen. `applied` is the executor saying it blocked, and
 * the party being stopped is not a neutral witness; the state that means
 * *stopped* is the turn having ended, which the server observes for itself and
 * publishes as `turn_ended_at`. So an applied stop on a turn that has not
 * ended reads "acknowledged, waiting", and after a while it reads as a
 * warning rather than a spinner - because a stop button that can report
 * success without a stop is worse than no stop button.
 *
 * A stop that never even reaches a boundary needs the same treatment, and it
 * is the commoner case: a tool that is already running finishes first, and a
 * turn this server has stopped waiting for may have ended on the executor with
 * nothing left for a stop to land on. Either way the panel would otherwise sit
 * on "Stopping…" indefinitely with no way off the screen, so a pending stop
 * that outstays the same threshold reports itself as unlanded, which is what
 * brings the secondary "stop waiting" control back.
 */
function overdue(timestamp: string | null | undefined, now: number): boolean {
  if (!timestamp) return false;
  const at = new Date(timestamp).getTime();
  return !Number.isNaN(at) && now - at > HALT_STALL_WARNING_MS;
}

export function useHaltState(
  liveHalt: Halt | null,
  isRequesting: boolean
): HaltState {
  // The stall threshold is time-based, so it needs a clock that re-renders.
  // Running one only while a stop is outstanding keeps an idle chat idle.
  const now = useNow(liveHalt != null && liveHalt.turn_ended_at == null, 2000);

  if (!liveHalt) return isRequesting ? 'requested' : 'none';
  if (liveHalt.turn_ended_at != null || liveHalt.status === 'expired') {
    return 'none';
  }
  if (liveHalt.status !== 'applied') {
    return overdue(liveHalt.created_at, now) ? 'unlanded' : 'requested';
  }
  return overdue(liveHalt.applied_at, now) ? 'stalled' : 'acknowledged';
}
