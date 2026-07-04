// GRACE-2 web — useAuth hook (GCP→AWS migration; was job-0253 Firebase).
//
// A thin React adapter over `../auth` (now the AWS Cognito Hosted UI OIDC
// client). It exists so components can render auth-aware UI without importing
// IdP types or calling `onAuthChanged` plumbing by hand.
//
// What it exposes:
//   - `user`     — the library-agnostic `AuthUser | null` (re-rendered on every
//                  auth-state change).
//   - `status`   — the `AuthInitStatus` ("disabled" | "initializing" | "ready"
//                  | "failed"). "disabled" means the VITE_COGNITO_* env vars are
//                  absent — the load-bearing dev/tailnet path.
//   - `resolved` — false until the first auth-state callback has fired, so the
//                  guard can avoid a sign-in flash before the persisted session
//                  is restored on a configured deployment.
//   - `signIn` / `signOut` — pass-throughs to `../auth`, stable references.
//                  `signIn` redirects to the Cognito Hosted UI (email/password).
//
// Invariant note (web Domain Discipline): this hook renders identity and emits
// sign-in/out intent only. It computes no user-facing numbers and holds no IdP
// objects beyond the `AuthUser` projection that `auth.ts` already produces.

import { useCallback, useEffect, useState } from "react";
import {
  type AuthInitStatus,
  type AuthUser,
  authStatus,
  onAuthChanged,
  signIn as authSignIn,
  signOut as authSignOut,
} from "../auth";

/** Reactive auth snapshot + intent emitters. No IdP types cross this seam. */
export interface UseAuthResult {
  /** Current signed-in identity, or null when signed out / Cognito disabled. */
  user: AuthUser | null;
  /** Auth subsystem status. "disabled" ⇒ env vars absent (dev/tailnet). */
  status: AuthInitStatus;
  /**
   * False until the first `onAuthChanged` callback fires. On a configured
   * deployment this prevents a sign-in flash before the persisted session is
   * restored; in "disabled" mode it flips true on the synchronous `cb(null)`
   * the auth subsystem delivers immediately.
   */
  resolved: boolean;
  /**
   * Begin the Cognito Hosted UI sign-in flow (email/password). Redirects the
   * browser to the Hosted UI authorize endpoint. Throws when Cognito is
   * disabled.
   */
  signIn: () => Promise<void>;
  /** Sign the current user out. Redirects to Hosted UI /logout when enabled. */
  signOut: () => Promise<void>;
}

/**
 * Subscribe to Firebase auth state and expose a render-friendly snapshot.
 *
 * Mirrors the App.tsx auth subscription that already existed (job-0123) but as
 * a reusable hook so `AuthGuard` (and any future auth-aware component) shares
 * one source of truth. Safe to mount many times — each instance owns its own
 * subscription and unsubscribes on unmount.
 */
export function useAuth(): UseAuthResult {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<AuthInitStatus>(() => authStatus());
  const [resolved, setResolved] = useState<boolean>(false);

  useEffect(() => {
    // `onAuthChanged` fires once with the current user (or null) after init,
    // then on every subsequent state change. In "disabled" mode it fires once
    // with null and never again — a stable signed-out snapshot.
    const unsub = onAuthChanged((u) => {
      setUser(u);
      // Read the status synchronously after each callback: `initAuth` has
      // resolved by the time the first callback lands, so the cached status
      // is now accurate ("ready" on a configured project, "disabled" / "failed"
      // otherwise).
      setStatus(authStatus());
      setResolved(true);
    });
    return unsub;
  }, []);

  const signIn = useCallback(() => authSignIn(), []);
  const signOut = useCallback(() => authSignOut(), []);

  return { user, status, resolved, signIn, signOut };
}
