// GRACE-2 web — entry-level path switch (job-0285).
//
// A deliberately tiny, dependency-free router mounted by main.tsx. It exists
// so the public landing page ("/") and privacy policy ("/privacy") can ship
// without touching App.tsx (owned by a concurrent job) and without pulling in
// a router library.
//
// ┌────────────────────────────────────────────────────────────────────────┐
// │ THE PASSTHROUGH RULE (load-bearing — read before changing)             │
// │                                                                        │
// │   "/"        → LANDING only for visitors with NO existing GRACE-2     │
// │                session key in localStorage. If ANY session key is      │
// │                present, "/" renders the APP exactly as it always has.  │
// │   "/app"     → APP, always (also "/app/*").                            │
// │   "/privacy" → privacy policy, always.                                 │
// │   "/landing" → landing, always (explicit preview; shows the            │
// │                "Resume session" CTA variant for returning users).      │
// │   anything else → APP (legacy deep-link behavior preserved).           │
// │                                                                        │
// │ WHY: every live-verify Playwright tool under web/tools/ navigates to   │
// │ "http://host:5173/" expecting the APP, and they all seed               │
// │ `grace2_anonymous_accepted` via addInitScript before navigation (the   │
// │ AuthGate requires it). The user's own browser carries                  │
// │ `grace2.session_id` from any prior visit. Both therefore pass straight │
// │ through to the app — only a genuinely fresh visitor sees the landing.  │
// └────────────────────────────────────────────────────────────────────────┘
//
// Navigation is full-page (<a href>): the landing CTA points at "/app", the
// privacy footer points back at "/". No history listening, no client-side
// route state — the switch runs once per page load.
//
// Code-splitting: App (MapLibre, Vega, Firebase, …) and Privacy are loaded
// via React.lazy so the landing chunk stays lean; Landing is eager because
// it is small and must paint instantly for first-time visitors.

import { lazy, Suspense } from "react";
import { Landing } from "./pages/Landing";
import { ErrorBoundary } from "./components/ErrorBoundary";

const App = lazy(() => import("./App").then((m) => ({ default: m.App })));
const Privacy = lazy(() =>
  import("./pages/Privacy").then((m) => ({ default: m.Privacy })),
);

export type EntryRoute = "app" | "landing" | "privacy";

/**
 * localStorage keys that mark a browser as having an existing GRACE-2
 * session. Presence of ANY of them routes "/" to the app.
 *
 *  - grace2.session_id          — ws.ts SESSION_KEY (written on first connect)
 *  - grace2.anonymous_user_id   — ws.ts anonymous-identity key
 *  - grace2_anonymous_accepted  — AuthGate "continue without saving" flag;
 *                                 ALSO the key every Playwright live-verify
 *                                 tool seeds via addInitScript, which is what
 *                                 keeps "/" rendering the app for tooling.
 */
export const GRACE2_SESSION_KEYS = [
  "grace2.session_id",
  "grace2.anonymous_user_id",
  "grace2_anonymous_accepted",
] as const;

/** True when any GRACE-2 session key exists in the given storage. */
export function hasExistingSession(
  storage?: Pick<Storage, "getItem">,
): boolean {
  try {
    const s = storage ?? window.localStorage;
    return GRACE2_SESSION_KEYS.some((key) => s.getItem(key) != null);
  } catch {
    // localStorage unavailable (privacy mode) — treat as a fresh visitor.
    return false;
  }
}

/** Pure path → route mapping; see the passthrough rule above. */
export function resolveRoute(pathname: string, hasSession: boolean): EntryRoute {
  // Normalize trailing slashes ("/privacy/" === "/privacy").
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/privacy") return "privacy";
  if (path === "/landing") return "landing";
  if (path === "/app" || path.startsWith("/app/")) return "app";
  if (path === "/") return hasSession ? "app" : "landing";
  // Unknown paths keep today's behavior: the app owned everything before
  // this switch existed, so deep links continue to render the app.
  return "app";
}

/** Minimal dark fallback while a lazy chunk loads (sub-second on LAN). */
function RouteFallback(): JSX.Element {
  return (
    <div
      data-testid="grace2-route-fallback"
      style={{ minHeight: "100vh", background: "#0b1018" }}
    />
  );
}

export function EntryRouter(): JSX.Element {
  const session = hasExistingSession();
  const route = resolveRoute(window.location.pathname, session);

  if (route === "landing") {
    return <Landing hasSession={session} />;
  }
  if (route === "privacy") {
    return (
      <Suspense fallback={<RouteFallback />}>
        <Privacy />
      </Suspense>
    );
  }
  return (
    // Inner boundary: an App render throw degrades to the dark fallback even
    // when this router is mounted without the main.tsx outer boundary (e.g. a
    // future host). main.tsx still wraps the whole tree as the outer guard.
    <ErrorBoundary>
      <Suspense fallback={<RouteFallback />}>
        <App />
      </Suspense>
    </ErrorBoundary>
  );
}
