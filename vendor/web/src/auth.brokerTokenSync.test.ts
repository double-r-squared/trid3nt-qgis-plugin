// GRACE-2 web -- getIdTokenSync() pre-upgrade `?st` carrier race fix
// (NATE 2026-06-29).
//
// THE BUG: a SIGNED-IN real-account user's WebSocket dial (ws.ts openSocket)
// reads the broker `?st=` carrier SYNCHRONOUSLY via getIdTokenSync(). On a fresh
// page load / reload, `cachedTokens` is populated only when initAuth()'s ASYNC
// restore settles -- which happens AFTER App mount fires the first dial. So the
// first dial carried NO `?st`, the per-session broker rejected the connect
// (decide_route: `verify(None) -> None` -> reject 4401), and no agent was ever
// provisioned. The DEMO-CODE path never hit this because signInWithAccessCode()
// sets `cachedTokens` synchronously in the same gesture.
//
// THE FIX: getIdTokenSync() falls back to a SYNCHRONOUS read of the persisted
// sessionStorage token set (the SAME store initAuth() will later hydrate), so a
// same-tab reload carries a valid `?st` on the VERY FIRST dial -- before
// initAuth() resolves. Only a non-expired id token is returned.
//
// These tests drive the REAL auth.ts module (fresh per test via resetModules so
// the in-memory `cachedTokens` is null, exactly as on a fresh page load) and
// assert the sync fallback reads / gates the persisted token correctly.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

const SS_TOKENS = "grace2_cognito_tokens";

function stubCognitoEnv(): void {
  vi.stubEnv("VITE_COGNITO_USER_POOL_ID", "us-west-2_pool123");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", "client123");
  vi.stubEnv("VITE_COGNITO_DOMAIN", "grace2-auth.auth.us-west-2.amazoncognito.com");
  vi.stubEnv("VITE_COGNITO_REGION", "us-west-2");
  vi.stubEnv("VITE_COGNITO_REDIRECT_URI", "https://app.example/");
}

/** Build an UNSIGNED JWT (auth.ts decodes claims only; JWKS verify is the broker's). */
function makeJwt(claims: Record<string, unknown>): string {
  const enc = (o: unknown) =>
    Buffer.from(JSON.stringify(o))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${enc({ alg: "none", typ: "JWT" })}.${enc(claims)}.`;
}

const LIVE_ID_TOKEN = makeJwt({ sub: "cognito-sub-nate", exp: 0 });

function seedSessionTokens(idToken: string, expiresAt: number): void {
  sessionStorage.setItem(
    SS_TOKENS,
    JSON.stringify({
      idToken,
      accessToken: "acc",
      refreshToken: "ref",
      expiresAt,
    }),
  );
}

beforeEach(() => {
  vi.resetModules();
  stubCognitoEnv();
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});

describe("getIdTokenSync() -- pre-upgrade ?st carrier (broker-auth race fix)", () => {
  it("returns a LIVE persisted id token even though cachedTokens is null (the reload race)", async () => {
    // Simulate a same-tab reload of a signed-in user: sessionStorage holds the
    // live token, but initAuth() has NOT run yet so the in-memory cache is empty.
    seedSessionTokens(LIVE_ID_TOKEN, Date.now() + 3_600_000);
    const auth = await import("./auth");
    // NO initAuth() call -> cachedTokens is null, exactly as at App-mount dial time.
    expect(auth.getIdTokenSync()).toBe(LIVE_ID_TOKEN);
  });

  it("returns null when the persisted token is EXPIRED (a hard-reject would follow)", async () => {
    seedSessionTokens(LIVE_ID_TOKEN, Date.now() - 1_000); // expired
    const auth = await import("./auth");
    expect(auth.getIdTokenSync()).toBeNull();
  });

  it("returns null when there is no persisted token (signed-out / disabled -> no ?st)", async () => {
    const auth = await import("./auth");
    expect(auth.getIdTokenSync()).toBeNull();
  });

  it("returns null when the persisted set carries an empty id token", async () => {
    seedSessionTokens("", Date.now() + 3_600_000);
    const auth = await import("./auth");
    expect(auth.getIdTokenSync()).toBeNull();
  });

  it("does not throw on a malformed sessionStorage blob (degrades to null)", async () => {
    sessionStorage.setItem(SS_TOKENS, "{not-json");
    const auth = await import("./auth");
    expect(() => auth.getIdTokenSync()).not.toThrow();
    expect(auth.getIdTokenSync()).toBeNull();
  });
});
