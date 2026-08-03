import { useRouter } from 'next/router';
import { useCallback, useEffect, useRef, useState } from 'react';

/** Thrown to abort a pages-router navigation. Next.js expects a throw here. */
const ABORT_SENTINEL = 'Route change aborted: unsaved agent configuration';

export const UNSAVED_CHANGES_MESSAGE =
  'This tab has edits that have not been saved. Leaving now discards them.';

type Guard = {
  /** True while the confirm dialog should be on screen. */
  askOpen: boolean;
  /**
   * Ask to leave. Runs `proceed` straight away when nothing is dirty, and
   * otherwise opens the dialog and runs it only if the person confirms.
   */
  requestExit: (proceed: () => void) => void;
  /** Throw the edits away and continue to wherever we were going. */
  confirmDiscard: () => void;
  /** Stay put. */
  cancelDiscard: () => void;
};

/**
 * One confirm for three ways out of an editor with unsaved edits.
 *
 * The three exits are genuinely different mechanisms and all three matter,
 * because this tab writes production behaviour and there is no autosave to
 * fall back on. An in-page tab switch is intercepted by the caller before it
 * pushes a route. A client-side navigation is caught on `routeChangeStart` and
 * cancelled the way the pages router expects, by emitting `routeChangeError`
 * and throwing. A browser close or reload gets the browser's own dialog, which
 * cannot be styled and does not need to be.
 *
 * Discarding clears the dirty flag through a ref rather than waiting for a
 * re-render, because the navigation it unblocks happens in the same tick.
 */
export function useUnsavedChangesGuard(
  isDirty: boolean,
  onDiscard: () => void
): Guard {
  const router = useRouter();
  const [askOpen, setAskOpen] = useState(false);
  const dirtyRef = useRef(isDirty);
  const pendingRef = useRef<(() => void) | null>(null);
  const pendingUrlRef = useRef<string | null>(null);
  const onDiscardRef = useRef(onDiscard);

  useEffect(() => {
    dirtyRef.current = isDirty;
  }, [isDirty]);

  useEffect(() => {
    onDiscardRef.current = onDiscard;
  }, [onDiscard]);

  useEffect(() => {
    const handleRouteChange = (url: string) => {
      if (!dirtyRef.current) return;
      pendingUrlRef.current = url;
      pendingRef.current = null;
      setAskOpen(true);
      router.events.emit('routeChangeError');
      throw ABORT_SENTINEL;
    };

    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      event.preventDefault();
      // Legacy assignment: some browsers still require a returnValue to show
      // their own dialog, and none of them show our text.
      event.returnValue = UNSAVED_CHANGES_MESSAGE;
      return UNSAVED_CHANGES_MESSAGE;
    };

    router.events.on('routeChangeStart', handleRouteChange);
    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      router.events.off('routeChangeStart', handleRouteChange);
      window.removeEventListener('beforeunload', handleBeforeUnload);
    };
  }, [router]);

  const requestExit = useCallback((proceed: () => void) => {
    if (!dirtyRef.current) {
      proceed();
      return;
    }
    pendingRef.current = proceed;
    pendingUrlRef.current = null;
    setAskOpen(true);
  }, []);

  const confirmDiscard = useCallback(() => {
    dirtyRef.current = false;
    setAskOpen(false);
    onDiscardRef.current();

    const proceed = pendingRef.current;
    const url = pendingUrlRef.current;
    pendingRef.current = null;
    pendingUrlRef.current = null;

    if (proceed) {
      proceed();
      return;
    }
    // The navigation we cancelled has to be re-issued: cancelling a pages
    // router transition does not queue it.
    if (url) void router.push(url);
  }, [router]);

  const cancelDiscard = useCallback(() => {
    setAskOpen(false);
    pendingRef.current = null;
    pendingUrlRef.current = null;
  }, []);

  return { askOpen, requestExit, confirmDiscard, cancelDiscard };
}
