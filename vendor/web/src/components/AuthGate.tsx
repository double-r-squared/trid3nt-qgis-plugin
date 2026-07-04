// GRACE-2 web — AuthGate (job-0138, sprint-12-mega Wave 3.5).
//
// REPLACES the floating top-right AuthPanel (job-0123 / Wave 2) with a
// full-viewport gating page that renders BEFORE the main app shell.
// Per user direction 2026-06-08: "the auth shouldn't be a panel it should be
// a page that keeps us gated from using the app... let's make it its own page."
//
// Render rules:
//   - Mounted when App.tsx computes `appShouldRender === false`
//   - Full-viewport overlay (position: fixed; inset: 0)
//   - Centered card with GRACE-2 wordmark + 2 primary actions:
//       (1) "Sign in with Google" → signInWithGoogle() (Firebase popup)
//       (2) "Continue without saving (anonymous)" → sets
//           `localStorage.grace2_anonymous_accepted = "true"` and calls
//           `onAnonymousAccept` so the parent App can transition to the
//           main shell without round-tripping through Firebase.
//   - Footer "Why sign in?" link opens an explanatory modal listing what
//     signing in unlocks (Cases saved across devices, Tier-2 APIs).
//   - Dark-theme aware via the same overlay color tokens used by sibling
//     panels (LayerPanel, LayerLegend, AuthPanel, SecretsPanel) so a future
//     theme switch updates AuthGate uniformly.
//
// Test seams: AuthGate accepts overrideable handlers for the two CTAs so
// AuthGate.test.tsx never needs a real Firebase project. App.tsx wires the
// real handlers (signInWithGoogle from auth.ts; an `acceptAnonymous` helper
// it owns) so production behavior matches the kickoff.

import { useState } from "react";
import { signIn as authSignIn, isFirebaseConfigured } from "../auth";

/** localStorage flag set when the user explicitly chose "Continue without saving". */
export const ANONYMOUS_ACCEPTED_KEY = "grace2_anonymous_accepted";

export interface AuthGateProps {
  /** Called when the user clicks "Sign in". Defaults to the Cognito Hosted UI `signIn` from auth.ts. */
  onGoogleSignIn?: () => Promise<unknown>;
  /**
   * Called after the anonymous flag is written to localStorage. Lets App.tsx
   * re-evaluate `appShouldRender` synchronously without waiting for a storage
   * event. Provided by App.tsx; tests can stub.
   */
  onAnonymousAccept?: () => void;
}

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(12,14,20,0.96)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 10_000, // above every panel, hamburger, modal in App.tsx
  color: "#e8eaf0",
  fontFamily:
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif",
  padding: 24,
};

const cardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.96)",
  border: "1px solid #444",
  borderRadius: 12,
  padding: "40px 36px",
  maxWidth: 460,
  width: "100%",
  boxShadow: "0 24px 64px rgba(0,0,0,0.5)",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: 18,
};

const wordmarkStyle: React.CSSProperties = {
  fontSize: 36,
  fontWeight: 700,
  letterSpacing: "0.06em",
  color: "#e8eaf0",
  margin: 0,
};

const taglineStyle: React.CSSProperties = {
  fontSize: 14,
  color: "#aab0bc",
  textAlign: "center",
  margin: 0,
  lineHeight: 1.45,
};

const buttonBase: React.CSSProperties = {
  width: "100%",
  padding: "12px 16px",
  borderRadius: 8,
  fontSize: 14,
  fontFamily: "inherit",
  cursor: "pointer",
  border: "1px solid #555",
  textAlign: "center" as const,
  lineHeight: 1.2,
};

const primaryButtonStyle: React.CSSProperties = {
  ...buttonBase,
  background: "#3b82f6",
  borderColor: "#3b82f6",
  color: "#fff",
  fontWeight: 600,
};

const secondaryButtonStyle: React.CSSProperties = {
  ...buttonBase,
  background: "rgba(40,42,52,0.9)",
  borderColor: "#555",
  color: "#ddd",
};

const linkButtonStyle: React.CSSProperties = {
  background: "transparent",
  border: "none",
  color: "#7aa7ff",
  fontSize: 12,
  cursor: "pointer",
  padding: 4,
  fontFamily: "inherit",
  textDecoration: "underline",
};

const errorStyle: React.CSSProperties = {
  color: "#f88",
  fontSize: 12,
  textAlign: "center",
  marginTop: -4,
};

const modalBackdropStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.55)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 10_001,
};

const modalCardStyle: React.CSSProperties = {
  background: "rgba(20,22,30,0.98)",
  border: "1px solid #444",
  borderRadius: 10,
  padding: "24px 28px",
  maxWidth: 440,
  width: "92%",
  color: "#e8eaf0",
  boxShadow: "0 16px 48px rgba(0,0,0,0.5)",
};

/** Persist the anonymous-accepted flag. Wrapped so failures (private mode) don't crash the gate. */
export function persistAnonymousAccepted(): void {
  try {
    localStorage.setItem(ANONYMOUS_ACCEPTED_KEY, "true");
  } catch {
    // localStorage unavailable — proceed in-memory; reload returns to the gate.
  }
}

/** Read the anonymous-accepted flag. Tests + App.tsx use this to gate render. */
export function readAnonymousAccepted(): boolean {
  try {
    return localStorage.getItem(ANONYMOUS_ACCEPTED_KEY) === "true";
  } catch {
    return false;
  }
}

/** Clear the anonymous-accepted flag. Called on sign-out / sign-in upgrade. */
export function clearAnonymousAccepted(): void {
  try {
    localStorage.removeItem(ANONYMOUS_ACCEPTED_KEY);
  } catch {
    // non-fatal
  }
}

export function AuthGate({
  onGoogleSignIn,
  onAnonymousAccept,
}: AuthGateProps): JSX.Element {
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [whyOpen, setWhyOpen] = useState<boolean>(false);
  const firebaseConfigured = isFirebaseConfigured();

  async function handleGoogle(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const fn = onGoogleSignIn ?? authSignIn;
      await fn();
      // The Cognito Hosted UI redirect navigates away; on return the App.tsx
      // /callback handler exchanges the code and the auth-state subscription
      // flips `appShouldRender` to true. Nothing else to do here.
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function handleAnonymous(): void {
    setError(null);
    persistAnonymousAccepted();
    onAnonymousAccept?.();
  }

  return (
    <div
      data-testid="grace2-auth-gate"
      role="dialog"
      aria-modal="true"
      aria-label="TRID3NT sign-in"
      style={overlayStyle}
    >
      <div data-testid="grace2-auth-gate-card" style={cardStyle}>
        <h1 data-testid="grace2-auth-gate-wordmark" style={wordmarkStyle}>
          TRID3NT
        </h1>
        <p style={taglineStyle}>
          Multi-hazard modeling workbench. Sign in to save your Cases across
          devices, or continue without saving.
        </p>

        <button
          data-testid="grace2-auth-gate-google"
          disabled={busy || !firebaseConfigured}
          onClick={handleGoogle}
          style={{
            ...primaryButtonStyle,
            opacity: busy || !firebaseConfigured ? 0.55 : 1,
            cursor: busy || !firebaseConfigured ? "not-allowed" : "pointer",
          }}
          aria-label="Sign in or sign up"
          title={
            firebaseConfigured
              ? "Sign in / Sign up"
              : "Sign-in requires VITE_COGNITO_* env vars"
          }
        >
          Sign in / Sign up
        </button>

        <button
          data-testid="grace2-auth-gate-anonymous"
          disabled={busy}
          onClick={handleAnonymous}
          style={secondaryButtonStyle}
          aria-label="Continue without saving"
        >
          Continue without saving (anonymous)
        </button>

        {error && (
          <span data-testid="grace2-auth-gate-error" role="alert" style={errorStyle}>
            {error}
          </span>
        )}

        {!firebaseConfigured && (
          <span
            data-testid="grace2-auth-gate-config-note"
            style={{ ...errorStyle, color: "#aab0bc" }}
          >
            Sign-in disabled — set VITE_COGNITO_* env vars to enable.
          </span>
        )}

        <button
          data-testid="grace2-auth-gate-why"
          onClick={() => setWhyOpen(true)}
          style={linkButtonStyle}
          aria-label="Why sign in?"
        >
          Why sign in?
        </button>
      </div>

      {whyOpen && (
        <div
          data-testid="grace2-auth-gate-why-modal"
          role="dialog"
          aria-modal="true"
          aria-label="Why sign in"
          style={modalBackdropStyle}
          onClick={() => setWhyOpen(false)}
        >
          <div
            data-testid="grace2-auth-gate-why-card"
            style={modalCardStyle}
            onClick={(e) => e.stopPropagation()}
          >
            <h2 style={{ fontSize: 18, margin: "0 0 12px", color: "#e8eaf0" }}>
              Why sign in?
            </h2>
            <ul
              style={{
                margin: 0,
                paddingLeft: 18,
                color: "#c8ccd6",
                fontSize: 13,
                lineHeight: 1.55,
              }}
            >
              <li>
                <strong>Save your Cases.</strong> Hazard scenarios, layers,
                and chat history persist to MongoDB Atlas.
              </li>
              <li>
                <strong>Sync across devices.</strong> Sign in on another
                browser and pick up where you left off.
              </li>
              <li>
                <strong>Unlock Tier-2 APIs.</strong> Per-Case API keys for
                providers that require attestation (Google, Anthropic, OpenAI).
              </li>
              <li>
                Anonymous sessions still work — they just stay on this device
                and can&apos;t be shared.
              </li>
            </ul>
            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                marginTop: 16,
              }}
            >
              <button
                data-testid="grace2-auth-gate-why-close"
                onClick={() => setWhyOpen(false)}
                style={{
                  ...buttonBase,
                  width: "auto",
                  background: "rgba(40,42,52,0.9)",
                }}
                aria-label="Close why-sign-in"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
