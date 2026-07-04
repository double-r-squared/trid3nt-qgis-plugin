// GRACE-2 web — code-gate (access-code) sign-in tests.
//
// signInWithAccessCode(code) is the JUDGE-visible login path: it POSTs the
// entered access code to the demo-token endpoint and, on a 200 carrying a
// Cognito token set, establishes the session CLIENT-SIDE exactly as the OAuth
// /callback exchange (handleRedirectCallback) does — it writes SS_TOKENS + the
// durable LS_REFRESH mirror and fires onAuthChanged (which flips AuthGuard to
// MODE 3 -> children mount). On ANY failure it throws a GENERIC "Invalid code"
// (no oracle distinguishing a wrong code from a transport fault).
//
// These tests drive the REAL auth.ts module (fresh per test via resetModules so
// the `initialized` latch is clean), stub the demo-token endpoint env + fetch,
// and assert: a 200 runs setSession (SS_TOKENS + LS_REFRESH written +
// onAuthChanged fired); a 401 throws "Invalid code" and writes nothing.

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

const LS_REFRESH = "grace2_cognito_refresh";
const SS_TOKENS = "grace2_cognito_tokens";
const DEMO_TOKEN_URL =
  "https://abc123.execute-api.us-west-2.amazonaws.com/demo-token";

// Cognito config the module reads from import.meta.env (isFirebaseConfigured()
// is not required for the code path, but stub the demo-token endpoint URL).
function stubEnv(): void {
  vi.stubEnv("VITE_GRACE2_DEMO_TOKEN_URL", DEMO_TOKEN_URL);
}

/** Build an UNSIGNED JWT with the given claims (auth.ts decodes claims only;
 *  the agent verifies the real signature against JWKS — irrelevant here). */
function makeJwt(claims: Record<string, unknown>): string {
  const enc = (o: unknown) =>
    Buffer.from(JSON.stringify(o))
      .toString("base64")
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  return `${enc({ alg: "none", typ: "JWT" })}.${enc(claims)}.`;
}

const FUTURE_EXP = Math.floor(Date.now() / 1000) + 3600; // 1h ahead
const DEMO_ID_TOKEN = makeJwt({
  sub: "cognito-demo-judge",
  email: "demo@grace2-dev.test",
  name: "Demo Judge",
  exp: FUTURE_EXP,
});

beforeEach(() => {
  vi.resetModules();
  stubEnv();
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

describe("auth.ts — signInWithAccessCode (code-gate)", () => {
  it("a 200 establishes the session: POSTs {code} as JSON, runs setSession (SS_TOKENS + LS_REFRESH written, onAuthChanged fired)", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        id_token: DEMO_ID_TOKEN,
        access_token: "acc-demo",
        refresh_token: "refresh-demo",
      }),
    }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");

    const seen: (import("./auth").AuthUser | null)[] = [];
    const unsub = auth.onAuthChanged((u) => seen.push(u));

    await auth.signInWithAccessCode("THE-CODE");

    // The endpoint + payload contract.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe(DEMO_TOKEN_URL);
    expect(init.method).toBe("POST");
    expect(
      (init.headers as Record<string, string>)["Content-Type"],
    ).toBe("application/json");
    expect(JSON.parse(init.body as string)).toEqual({ code: "THE-CODE" });

    // setSession wrote the token set to sessionStorage + the durable refresh
    // mirror to localStorage.
    expect(sessionStorage.getItem(SS_TOKENS)).toBeTruthy();
    expect(localStorage.getItem(LS_REFRESH)).toBe("refresh-demo");

    // onAuthChanged fired the restored, non-anonymous identity.
    const resolved = seen[seen.length - 1];
    expect(resolved).not.toBeNull();
    expect(resolved?.uid).toBe("cognito-demo-judge");
    expect(resolved?.isAnonymous).toBe(false);
    expect(auth.authStatus()).toBe("ready");

    // getIdToken now returns the minted ID token (the WS handshake picks it up).
    expect(await auth.getIdToken()).toBe(DEMO_ID_TOKEN);

    unsub();
  });

  it("a 401 throws a GENERIC 'Invalid code' and writes NOTHING (no oracle)", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 401,
      json: async () => ({ error: "unauthorized" }),
    }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    await expect(auth.signInWithAccessCode("WRONG")).rejects.toThrow(
      /^Invalid code$/,
    );

    // Nothing persisted; no session established.
    expect(sessionStorage.getItem(SS_TOKENS)).toBeNull();
    expect(localStorage.getItem(LS_REFRESH)).toBeNull();
    expect(await auth.getIdToken()).toBeNull();
  });

  it("a network error throws the SAME generic 'Invalid code' (no transport-vs-code oracle)", async () => {
    const fetchMock = vi.fn(async () => {
      throw new Error("network down");
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    await expect(auth.signInWithAccessCode("ANY")).rejects.toThrow(
      /^Invalid code$/,
    );
    expect(sessionStorage.getItem(SS_TOKENS)).toBeNull();
  });

  it("derives the endpoint from VITE_GRACE2_PUBLIC_BASE + /demo-token when no explicit URL is set", async () => {
    vi.unstubAllEnvs();
    vi.stubEnv("VITE_GRACE2_PUBLIC_BASE", "d125yfbyjrpbre.cloudfront.net");
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        id_token: DEMO_ID_TOKEN,
        access_token: "acc",
        refresh_token: "r",
      }),
    }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    await auth.signInWithAccessCode("CODE");

    const url = String((fetchMock.mock.calls[0] as unknown[])[0]);
    expect(url).toBe("https://d125yfbyjrpbre.cloudfront.net/demo-token");
  });

  it("throws 'Invalid code' (fail-closed) when NO demo-token endpoint is configured", async () => {
    vi.unstubAllEnvs(); // neither DEMO_TOKEN_URL nor PUBLIC_BASE
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    await expect(auth.signInWithAccessCode("CODE")).rejects.toThrow(
      /^Invalid code$/,
    );
    // Fail-closed: no fetch even attempted.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
