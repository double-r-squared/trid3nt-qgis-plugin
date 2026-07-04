// GRACE-2 web — useIsMobile viewport hook (job-0278, mobile-friendly UI).
//
// The app's styling is predominantly inline style objects (React.CSSProperties),
// so responsive behavior comes from THIS hook driving conditional style
// branches in components — NOT from media-query stylesheets bolted onto
// inline styles (which they cannot override without !important).
//
// Contract:
//   - Mobile ⇔ viewport width < 768px (i.e. `(max-width: 767px)` matches).
//   - SSR-safe: returns false when `window` / `window.matchMedia` is absent.
//   - Live: subscribes to MediaQueryList changes so rotation / resize across
//     the breakpoint re-renders consumers. Falls back to the legacy
//     addListener/removeListener API for older WebKit.
//   - Desktop invariant (kickoff): every mobile style branch in the app must
//     be guarded by this hook so >=768px renders pixel-identical to before.

import { useEffect, useState } from "react";

/** Below this width the app renders the mobile layout. */
export const MOBILE_BREAKPOINT_PX = 768;

/** The media query the hook listens on (matches ⇔ mobile). */
export const MOBILE_MEDIA_QUERY = `(max-width: ${MOBILE_BREAKPOINT_PX - 1}px)`;

/**
 * One-shot read of the current mobile state. SSR-safe (false without a
 * window / matchMedia). Exported for tests + non-hook callers.
 */
export function readIsMobile(): boolean {
  if (
    typeof window === "undefined" ||
    typeof window.matchMedia !== "function"
  ) {
    return false;
  }
  try {
    return window.matchMedia(MOBILE_MEDIA_QUERY).matches;
  } catch {
    return false;
  }
}

/**
 * True iff the viewport is mobile-sized (< 768px). Re-renders the consumer
 * when the viewport crosses the breakpoint.
 */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState<boolean>(readIsMobile);

  useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    ) {
      return undefined;
    }
    let mql: MediaQueryList;
    try {
      mql = window.matchMedia(MOBILE_MEDIA_QUERY);
    } catch {
      return undefined;
    }
    const onChange = (e: MediaQueryListEvent): void => {
      setIsMobile(e.matches);
    };
    // Re-sync on mount in case the viewport changed between the initial
    // useState read and effect registration.
    setIsMobile(mql.matches);
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    }
    if (typeof mql.addListener === "function") {
      mql.addListener(onChange);
      return () => mql.removeListener?.(onChange);
    }
    return undefined;
  }, []);

  return isMobile;
}
