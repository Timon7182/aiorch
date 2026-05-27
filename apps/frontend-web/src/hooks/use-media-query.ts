import { useState, useEffect } from 'react';

/**
 * Subscribe to a CSS media query and re-render when it changes.
 *
 * SSR/edge-safe: returns `false` when `window.matchMedia` is unavailable.
 */
export function useMediaQuery(query: string): boolean {
  const getMatch = () =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : false;

  const [matches, setMatches] = useState<boolean>(getMatch);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return;
    }
    const mql = window.matchMedia(query);
    const handler = (event: MediaQueryListEvent) => setMatches(event.matches);

    // Sync immediately in case the query changed between render and effect.
    setMatches(mql.matches);
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, [query]);

  return matches;
}

/** Tailwind's `md` breakpoint is 768px — below it we treat the viewport as mobile. */
export const MOBILE_BREAKPOINT = 768;

/** True when the viewport is narrower than the `md` breakpoint (phones / small tablets). */
export function useIsMobile(): boolean {
  return useMediaQuery(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`);
}
