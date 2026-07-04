// GRACE-2 web — AuthGuard (job-0253, sprint-13.5 Stage 1).
//
// Wraps the APP entry (NOT the public Landing or Privacy pages — those route
// around the app in EntryRouter.tsx). It enforces the sprint-13.5 Decision 6
// posture — "production requires sign-in; anonymous is dev-only" — without
// disturbing the dev/tailnet experience.
//
// ┌──────────────────────── THREE-MODE MATRIX (load-bearing) ────────────────┐
// │                                                                          │
// │  Cognito DISABLED  (VITE_COGNITO_* absent)                               │
// │      → render `children` UNCHANGED. Pixel-identical to today. This is    │
// │        the live tailnet demo path and EVERY dev session; it must not     │
// │        change by a single pixel. The guard is a transparent pass-through.│
// │                                                                          │
// │  Cognito ENABLED + no signed-in user  (or auth expired — 4401)           │
// │      → render the minimal sign-in surface: GRACE-2 wordmark, "Sign in /  │
// │        Sign up" (Cognito Hosted UI, email/password), a /privacy link. NO │
// │        "continue as anonymous" here — Decision 6 (the auth.ts anonymous  │
// │        helper stays for dev/tests; it is simply never surfaced here).    │
// │                                                                          │
// │  Cognito ENABLED + signed-in user                                        │
// │      → render `children`. The Cognito ID token flows to the agent over   │
// │        the EXISTING ws.ts `auth-token` envelope path (unchanged here).   │
// │                                                                          │
// └──────────────────────────────────────────────────────────────────────────┘
//
// The visual language deliberately matches the job-0285 Landing: dark chrome,
// the system sans-serif stack, a hairline-bordered card. It is intentionally
// minimal — onboarding polish is job-0258, not this job.
//
// Sign-out affordance: the kickoff routes sign-out to the existing Settings
// popup ONLY IF `SettingsPopup.tsx` is clean in git at job start. It is NOT
// (it carries unrelated uncommitted edits), so per the kickoff the sign-out
// control lives on THIS surface instead — a small text button beneath the
// signed-in children is impossible (children own the viewport), so we expose
// sign-out on the sign-in surface's "signed in as …" path is moot; instead the
// guard renders a tiny fixed "Sign out" affordance only when enabled+signed-in.
// See `signOutAffordance` below. Flagged in the report.

import { useCallback, useState, type ReactNode } from "react";
import { useAuth } from "../hooks/useAuth";
import { isFirebaseConfigured, signInWithAccessCode } from "../auth";

export interface AuthGuardProps {
  /** The app tree to render once auth allows it (or always, when disabled). */
  children: ReactNode;
  /**
   * Auth-expired signal from ws.ts (close code 4401 / AUTH_FAILED). When true,
   * an otherwise-signed-in user is dropped to the sign-in surface because their
   * token was rejected by the agent gate. Defaults false. Wired by App.tsx.
   */
  authExpired?: boolean;
  /**
   * Test/dev seam: force the firebase-configured verdict. Production leaves
   * this undefined and the guard reads `isFirebaseConfigured()` (which is
   * driven by the `__setAuthForTesting` seam in auth.ts under test).
   */
  forceConfigured?: boolean;
}

// ── Styles — mirror AuthGate / Landing dark chrome (sans-serif, hairline). ──

const SANS =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif";

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(11,16,24,0.98)", // matches Landing/route-fallback #0b1018
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 10_000,
  color: "#e8eaf0",
  fontFamily: SANS,
  padding: 24,
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.96)",
  border: "1px solid #2a3240",
  borderRadius: 12,
  padding: "40px 36px",
  maxWidth: 420,
  width: "100%",
  boxShadow: "0 24px 64px rgba(0,0,0,0.5)",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: 18,
};

const wordmarkStyle: React.CSSProperties = {
  fontSize: 34,
  fontWeight: 700,
  letterSpacing: "0.06em",
  color: "#e8eaf0",
  margin: 0,
  fontFamily: SANS,
};

const taglineStyle: React.CSSProperties = {
  fontSize: 14,
  color: "#aab0bc",
  textAlign: "center",
  margin: 0,
  lineHeight: 1.45,
  fontFamily: SANS,
};

const googleButtonStyle: React.CSSProperties = {
  width: "100%",
  padding: "12px 16px",
  borderRadius: 8,
  fontSize: 14,
  fontFamily: SANS,
  fontWeight: 600,
  cursor: "pointer",
  border: "1px solid #3b82f6",
  background: "#3b82f6",
  color: "#fff",
  textAlign: "center",
  lineHeight: 1.2,
};

const codeFormStyle: React.CSSProperties = {
  width: "100%",
  display: "flex",
  flexDirection: "column",
  gap: 12,
};

const codeInputStyle: React.CSSProperties = {
  width: "100%",
  boxSizing: "border-box",
  padding: "12px 14px",
  borderRadius: 8,
  fontSize: 15,
  fontFamily: SANS,
  letterSpacing: "0.18em",
  textAlign: "center",
  border: "1px solid #2a3240",
  background: "rgba(11,16,24,0.9)",
  color: "#e8eaf0",
  outline: "none",
};

const expiredNoteStyle: React.CSSProperties = {
  color: "#f0b24a",
  fontSize: 12.5,
  textAlign: "center",
  margin: 0,
  fontFamily: SANS,
  lineHeight: 1.4,
};

const errorStyle: React.CSSProperties = {
  color: "#f88",
  fontSize: 12,
  textAlign: "center",
  margin: 0,
  fontFamily: SANS,
};

const privacyLinkStyle: React.CSSProperties = {
  color: "#7aa7ff",
  fontSize: 12,
  textDecoration: "underline",
  fontFamily: SANS,
};

/**
 * Gate the app behind Firebase Auth per the three-mode matrix above.
 *
 * `disabled` mode is a transparent pass-through — this is the only behavior
 * any current dev/tailnet session ever sees, and it MUST stay pixel-identical.
 */
export function AuthGuard({
  children,
  authExpired = false,
  forceConfigured,
}: AuthGuardProps): JSX.Element {
  const { user, resolved, signIn } = useAuth();
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  // Code-gate (JUDGE-visible) entry state. These hooks live INSIDE AuthGuard,
  // whose own early returns (MODE 1 / pending / MODE 2) are consistent across
  // renders, so adding hooks here is SAFE (unlike a hook below App.tsx's auth
  // early-return, which would trip React #310 and blank the authed app).
  const [code, setCode] = useState<string>("");
  const [showCode, setShowCode] = useState<boolean>(false);

  const configured =
    forceConfigured !== undefined ? forceConfigured : isFirebaseConfigured();

  // ADMIN ESCAPE HATCH: ?admin in the query string surfaces the ORIGINAL
  // Cognito Hosted-UI sign-in button instead of the code form, so
  // https://trid3nt.vercel.app/app?admin=1 reaches NATE's admin login.
  const adminMode = (() => {
    try {
      return new URLSearchParams(window.location.search).has("admin");
    } catch {
      return false;
    }
  })();

  const handleSignIn = useCallback(async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      // Redirects to the Cognito Hosted UI (email/password). The browser
      // navigates away; on return the /callback handler in App.tsx exchanges
      // the code and the useAuth subscription flips `user` non-null.
      await signIn();
    } catch (e) {
      setError((e as Error).message || "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }, [signIn]);

  const handleCodeSubmit = useCallback(async (): Promise<void> => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      // POSTs the access code to the demo-token endpoint; on success setSession
      // fires onAuthChanged -> useAuth flips `user` non-null -> MODE 3 mounts
      // children. No browser redirect (unlike the Hosted-UI path).
      await signInWithAccessCode(code);
    } catch (e) {
      setError((e as Error).message || "Invalid code");
    } finally {
      setBusy(false);
    }
  }, [busy, code]);

  // ── MODE 1: Firebase disabled. Transparent pass-through (load-bearing). ──
  // No wrapper element, no extra DOM — the children render exactly as if the
  // guard were not present. This is the dev/tailnet path.
  if (!configured) {
    return <>{children}</>;
  }

  // Auth not yet resolved on a configured project: hold a blank dark frame to
  // avoid a sign-in flash before Firebase restores the persisted session.
  if (!resolved) {
    return (
      <div
        data-testid="grace2-auth-guard-pending"
        style={{ minHeight: "100vh", background: "#0b1018" }}
      />
    );
  }

  // ── MODE 2: enabled + (no user OR auth expired). Sign-in surface. ──
  const signedIn = !!user && !user.isAnonymous;
  if (!signedIn || authExpired) {
    return (
      <div
        data-testid="grace2-auth-guard-signin"
        role="dialog"
        aria-modal="true"
        aria-label="TRID3NT sign-in"
        style={overlayStyle}
      >
        <div data-testid="grace2-auth-guard-card" style={cardStyle}>
          <h1 data-testid="grace2-auth-guard-wordmark" style={wordmarkStyle}>
            TRID3NT
          </h1>
          <p style={taglineStyle}>
            Multi-hazard modeling workbench. Sign in to continue.
          </p>

          {authExpired && (
            <p data-testid="grace2-auth-guard-expired" role="status" style={expiredNoteStyle}>
              Your session expired. Please sign in again.
            </p>
          )}

          {adminMode ? (
            // ── ADMIN: original Cognito Hosted-UI sign-in (NATE's login). ──
            <button
              data-testid="grace2-auth-guard-signin-btn"
              disabled={busy}
              onClick={() => void handleSignIn()}
              style={{
                ...googleButtonStyle,
                opacity: busy ? 0.55 : 1,
                cursor: busy ? "not-allowed" : "pointer",
              }}
              aria-label="Sign in or sign up"
            >
              Sign in / Sign up
            </button>
          ) : (
            // ── JUDGE-visible: access-code entry form (the default surface). ──
            <form
              data-testid="grace2-code-form"
              style={codeFormStyle}
              onSubmit={(e) => {
                e.preventDefault();
                void handleCodeSubmit();
              }}
            >
              <input
                data-testid="grace2-code-input"
                type={showCode ? "text" : "password"}
                // eslint-disable-next-line jsx-a11y/no-autofocus
                autoFocus
                autoComplete="off"
                value={code}
                disabled={busy}
                onChange={(e) => setCode(e.target.value)}
                placeholder="Access code"
                aria-label="Access code"
                style={codeInputStyle}
              />
              <button
                data-testid="grace2-code-toggle"
                type="button"
                onClick={() => setShowCode((v) => !v)}
                style={{
                  alignSelf: "flex-end",
                  background: "none",
                  border: "none",
                  color: "#9ca3af",
                  fontSize: 12,
                  fontFamily: SANS,
                  cursor: "pointer",
                  padding: 0,
                  textDecoration: "underline",
                }}
                aria-label={showCode ? "Hide access code" : "Show access code"}
                aria-pressed={showCode}
              >
                {showCode ? "Hide code" : "Show code"}
              </button>
              <button
                data-testid="grace2-code-submit"
                type="submit"
                disabled={busy}
                style={{
                  ...googleButtonStyle,
                  opacity: busy ? 0.55 : 1,
                  cursor: busy ? "not-allowed" : "pointer",
                }}
                aria-label="Enter"
              >
                Enter
              </button>
            </form>
          )}

          {error && (
            <p data-testid="grace2-auth-guard-error" role="alert" style={errorStyle}>
              {error}
            </p>
          )}

          <a
            data-testid="grace2-auth-guard-privacy"
            href="/privacy"
            style={privacyLinkStyle}
          >
            Privacy Policy
          </a>
        </div>
      </div>
    );
  }

  // ── MODE 3: enabled + signed-in. Render the app. ──
  // ux-batch-1 (F12): the sign-out control now lives ONLY in the Settings page
  // (SettingsPopup.tsx, wired to App.tsx handleSignOut). The previous fixed
  // top-right affordance has been removed so there is a single, discoverable
  // place to sign out. The guard is now a transparent pass-through once
  // signed-in — no extra DOM.
  return <>{children}</>;
}
