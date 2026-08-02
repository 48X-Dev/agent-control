import { useEffect, useState } from 'react';

/**
 * A clock that re-renders, for the panel's "how long has this been going on"
 * copy.
 *
 * Reading `Date.now()` during render is impure and this repo's lint says so
 * with good reason: the value changes without a state change, so what is on
 * screen depends on when React happened to re-render rather than on anything
 * that happened. Ages and stall warnings are exactly the copy where that goes
 * wrong quietly - a warning that appears only when something else on the page
 * changes is worse than no warning.
 *
 * Ticks only while `enabled`. An idle chat runs no timer.
 */
export function useNow(enabled: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [enabled, intervalMs]);

  return now;
}
