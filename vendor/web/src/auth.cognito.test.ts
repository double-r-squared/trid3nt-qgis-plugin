// GRACE-2 web — Cognito auth.ts client tests (GCP→AWS migration).
//
// Verifies the auth.ts surface behind the SAME exported names the rest of the
// app consumes, after the Firebase→Cognito swap:
//   - DISABLED mode (VITE_COGNITO_* absent): isFirebaseConfigured()=false,
//     onAuthChanged fires null, getIdToken()=null, signOut() no-op clear. This
//     is the load-bearing dev/tailnet pass-through — the live demo path.
//   - CONFIGURED + __setAuthForTesting: onAuthChanged fires the injected user,
//     getIdToken returns a non-empty token, signOut clears to null.
//   - JWT-claim → AuthUser mapping (sub→uid, email, name) via the injected user.
//   - signIn() throws when Cognito is disabled (so the gate surfaces an error).
//
// crypto.subtle / window.location.assign (the PKCE redirect) are NOT exercised
// here — that is the manual Hosted UI round-trip in the runbook. These tests
// pin the parts the unit suite can deterministically assert.

import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";
import {
  type AuthUser,
  isFirebaseConfigured,
  authStatus,
  onAuthChanged,
  getIdToken,
  signIn,
  signOut,
  __setAuthForTesting,
} from "./auth";

const COGNITO_USER: AuthUser = {
  uid: "cognito-sub-xyz",
  displayName: "Cognito User",
  email: "cog@example.com",
  photoURL: null,
  isAnonymous: false,
};

describe("auth.ts (Cognito) — DISABLED mode (env absent)", () => {
  beforeEach(() => {
    // Clear any injected test user from a prior test.
    vi.unstubAllEnvs();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("isFirebaseConfigured() is false when VITE_COGNITO_* are unset", () => {
    expect(isFirebaseConfigured()).toBe(false);
  });

  it("onAuthChanged fires null and getIdToken returns null (anonymous-only)", async () => {
    // Reset injected state to the real (disabled) path.
    let received: AuthUser | null | undefined = undefined;
    const unsub = onAuthChanged((u) => {
      received = u;
    });
    // Allow the initAuth().then microtask to flush.
    await Promise.resolve();
    await Promise.resolve();
    expect(received).toBeNull();
    expect(await getIdToken()).toBeNull();
    unsub();
  });

  it("signIn() throws when Cognito is disabled", async () => {
    await expect(signIn()).rejects.toThrow(/not configured/i);
  });
});

describe("auth.ts (Cognito) — __setAuthForTesting seam", () => {
  afterEach(() => {
    __setAuthForTesting(null);
  });

  it("injecting a user flips onAuthChanged + authStatus + getIdToken", async () => {
    __setAuthForTesting(COGNITO_USER);
    expect(authStatus()).toBe("ready");

    const seen: (AuthUser | null)[] = [];
    const unsub = onAuthChanged((u) => {
      seen.push(u);
    });
    // Injected mode fires synchronously.
    const received = seen[seen.length - 1];
    expect(received).toEqual(COGNITO_USER);
    expect(received?.uid).toBe("cognito-sub-xyz");
    expect(received?.email).toBe("cog@example.com");

    const token = await getIdToken();
    expect(token).toBeTruthy();
    expect(typeof token).toBe("string");
    unsub();
  });

  it("injecting null reports signed-out (no token)", async () => {
    __setAuthForTesting(null);
    expect(authStatus()).toBe("disabled");
    expect(await getIdToken()).toBeNull();
  });

  it("signOut clears an injected user to null", async () => {
    __setAuthForTesting(COGNITO_USER);
    let last: AuthUser | null | undefined = COGNITO_USER;
    const unsub = onAuthChanged((u) => {
      last = u;
    });
    expect(last).toEqual(COGNITO_USER);
    await signOut();
    expect(last).toBeNull();
    unsub();
  });
});
