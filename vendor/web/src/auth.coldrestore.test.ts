// GRACE-2 web — COLD-BOOT CLIENT-SIDE session restore (cases-blank-box-off fix,
// NATE 2026-06-20).
//
// THE BUG: the Cases rail (and cold case-view) were BLANK box-off (agent box
// asleep) for a SIGNED-IN user until the box woke and the user re-signed in.
// ROOT CAUSE: the ENTIRE Cognito token set lived in sessionStorage, which a
// fresh tab / browser restart / evicted-tab clears. So a cold boot looked SIGNED
// OUT (isSignedIn=false -> coldListIdentity="anon"), and the serverless
// /case-list fetch went out TOKENLESS -> the Lambda's authoritative-EMPTY answer
// -> blank rail.
//
// THE FIX (auth.ts): the long-lived REFRESH token is now mirrored to
// localStorage (durable). On a cold boot initAuth() reads it and mints a fresh
// ID token via the Cognito refresh_token grant (a direct POST to /oauth2/token
// — NO agent / WebSocket involvement), restoring the signed-in session
// CLIENT-SIDE. getIdToken() then returns a usable token, so the cold case-list /
// case-view fetches authenticate box-off exactly as box-on.
//
// These tests drive the REAL auth.ts module (fresh per test via resetModules so
// the `initialized` latch is clean), seed localStorage with a durable refresh
// token, mock fetch for the /oauth2/token refresh, and assert the session is
// restored with a usable token WITHOUT any agent/WS round-trip — and that
// sign-out clears the durable token so a cold boot then reads signed-out.

import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  vi,
} from "vitest";

const LS_REFRESH = "grace2_cognito_refresh";
const SS_TOKENS = "grace2_cognito_tokens";

// Cognito config the module reads from import.meta.env. All three gate
// isFirebaseConfigured() -> true so the restore path is exercised.
function stubCognitoEnv(): void {
  vi.stubEnv("VITE_COGNITO_USER_POOL_ID", "us-west-2_pool123");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", "client123");
  vi.stubEnv("VITE_COGNITO_DOMAIN", "grace2-auth.auth.us-west-2.amazoncognito.com");
  vi.stubEnv("VITE_COGNITO_REGION", "us-west-2");
  vi.stubEnv("VITE_COGNITO_REDIRECT_URI", "https://app.example/");
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
const FRESH_ID_TOKEN = makeJwt({
  sub: "cognito-sub-restored",
  email: "nate@example.com",
  name: "Nate",
  exp: FUTURE_EXP,
});

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

describe("auth.ts — cold-boot CLIENT-SIDE session restore (box-off cases fix)", () => {
  it("restores a signed-in session + usable token from the DURABLE localStorage refresh token", async () => {
    // Cold boot: ONLY the durable refresh token survives (sessionStorage empty,
    // as a fresh tab / restart leaves it). No agent / WS involved.
    localStorage.setItem(LS_REFRESH, "durable-refresh-xyz");

    // Cognito /oauth2/token refresh grant mints a fresh ID token.
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ id_token: FRESH_ID_TOKEN, access_token: "acc" }),
    }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");

    // Drive the async restore to completion first (initAuth mints the ID token
    // via the refresh grant), THEN read the resolved identity.
    await auth.initAuth();

    // The user resolves to the restored (non-anonymous) identity...
    const seen: (import("./auth").AuthUser | null)[] = [];
    const unsub = auth.onAuthChanged((u) => {
      seen.push(u);
    });
    // onAuthChanged fires cachedUser after initAuth's microtask settles.
    await Promise.resolve();
    await Promise.resolve();

    const resolved = seen[seen.length - 1];
    expect(resolved).not.toBeNull();
    expect(resolved?.uid).toBe("cognito-sub-restored");
    expect(resolved?.isAnonymous).toBe(false);
    expect(auth.authStatus()).toBe("ready");

    // ...and getIdToken returns the freshly minted (usable) ID token, so the
    // cold case-list / case-view fetches authenticate box-off.
    const token = await auth.getIdToken();
    expect(token).toBe(FRESH_ID_TOKEN);

    // The refresh grant hit Cognito's /oauth2/token directly (NO agent / WS).
    expect(fetchMock).toHaveBeenCalled();
    const url = String((fetchMock.mock.calls[0] as unknown[])[0]);
    expect(url).toContain("/oauth2/token");
    unsub();
  });

  it("a successful sign-in token exchange MIRRORS the refresh token to localStorage (so a later cold boot restores)", async () => {
    // Simulate the OAuth /callback exchange: a ?code= -> /oauth2/token POST that
    // returns id + refresh tokens. handleRedirectCallback persists them.
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({
        id_token: FRESH_ID_TOKEN,
        access_token: "acc",
        refresh_token: "fresh-refresh-from-exchange",
      }),
    }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // PKCE verifier must be present for the exchange to proceed.
    sessionStorage.setItem("grace2_cognito_pkce_verifier", "verifier123");
    // happy-dom: place a ?code= on the URL the callback reads.
    window.history.replaceState({}, "", "/?code=authcode123");

    const auth = await import("./auth");
    const user = await auth.handleRedirectCallback();
    expect(user?.uid).toBe("cognito-sub-restored");

    // The DURABLE refresh token is now in localStorage -> a future cold boot
    // (different module instance) can restore the session.
    expect(localStorage.getItem(LS_REFRESH)).toBe("fresh-refresh-from-exchange");
    // The live ID token set is also in sessionStorage (in-tab fast path).
    expect(sessionStorage.getItem(SS_TOKENS)).toBeTruthy();
  });

  it("a plain box-ON reload of a LIVE session (refresh token only in sessionStorage) SEEDS the durable localStorage mirror", async () => {
    // The pre-existing-session case NATE hit live: signed in BEFORE the durable
    // mirror landed, so the refresh token lives ONLY in the sessionStorage token
    // set; localStorage holds nothing. A normal box-on reload must mirror it to
    // localStorage so the NEXT box-off cold boot can restore — WITHOUT a fresh
    // sign-in. (Regression guard for the live-token branch skipping storeTokens.)
    sessionStorage.setItem(
      SS_TOKENS,
      JSON.stringify({
        idToken: FRESH_ID_TOKEN,
        accessToken: "acc",
        refreshToken: "session-only-refresh",
        expiresAt: FUTURE_EXP * 1000,
      }),
    );
    expect(localStorage.getItem(LS_REFRESH)).toBeNull(); // not seeded yet

    // No network: the live ID token is used as-is (no refresh grant fired).
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    await auth.initAuth();

    // The durable mirror is now seeded from the sessionStorage refresh token.
    expect(localStorage.getItem(LS_REFRESH)).toBe("session-only-refresh");
    // And no refresh grant was needed (the live ID token sufficed).
    expect(fetchMock).not.toHaveBeenCalled();
    // Identity resolved signed-in from the live token.
    const token = await auth.getIdToken();
    expect(token).toBe(FRESH_ID_TOKEN);
  });

  it("sign-out CLEARS the durable refresh token (a subsequent cold boot reads signed-out)", async () => {
    localStorage.setItem(LS_REFRESH, "durable-refresh-xyz");
    sessionStorage.setItem(
      SS_TOKENS,
      JSON.stringify({
        idToken: FRESH_ID_TOKEN,
        accessToken: "acc",
        refreshToken: "durable-refresh-xyz",
        expiresAt: FUTURE_EXP * 1000,
      }),
    );
    // signOut redirects via window.location.assign; stub it so the test does not
    // actually navigate.
    const assignSpy = vi
      .spyOn(window.location, "assign")
      .mockImplementation(() => {});

    const auth = await import("./auth");
    await auth.initAuth();
    await auth.signOut();

    // Durable refresh removed -> a cold boot would find nothing to restore.
    expect(localStorage.getItem(LS_REFRESH)).toBeNull();
    expect(sessionStorage.getItem(SS_TOKENS)).toBeNull();
    assignSpy.mockRestore();
  });

  it("a REVOKED refresh token (refresh 4xx) clears durable state -> signed-out cold boot, no agent dependency", async () => {
    localStorage.setItem(LS_REFRESH, "revoked-refresh");
    const fetchMock = vi.fn(async () => ({
      ok: false,
      status: 400,
      json: async () => ({ error: "invalid_grant" }),
    }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    await auth.initAuth();

    expect(await auth.getIdToken()).toBeNull();
    // Durable state cleared so we do not retry a dead refresh forever.
    expect(localStorage.getItem(LS_REFRESH)).toBeNull();
  });

  it("NO durable refresh token -> signed-out cold boot (anon), no fetch attempted", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");
    let resolved: import("./auth").AuthUser | null | undefined = undefined;
    const unsub = auth.onAuthChanged((u) => {
      resolved = u;
    });
    await auth.initAuth();
    await Promise.resolve();

    expect(resolved).toBeNull();
    expect(await auth.getIdToken()).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    unsub();
  });
});

// --------------------------------------------------------------------------- //
// COLD-RELOAD RESTORE RACE (session durability Job A).
//
// THE BUG: on a FULL browser close+reload only the durable localStorage refresh
// token survives; restoring the session requires the async refresh_token grant
// inside initAuth(). The app mounts TWO concurrent onAuthChanged subscribers
// (App.tsx + AuthGuard via useAuth). The OLD code flipped a module-level
// `initialized = true` latch SYNCHRONOUSLY at the top of initAuth() before
// awaiting the refresh grant. So the 1st subscriber ran the real async restore,
// while the 2nd saw initialized===true, short-circuited, and fired its callback
// with cachedUser STILL NULL -> the gate painted Sign-in before the refresh
// resolved (a spurious sign-out flash + a fresh full reload).
//
// THE FIX: a memoized in-flight `initPromise` shared by every concurrent caller.
// Both subscribers await the SAME settle, so NEITHER can ever observe a null
// user while a durable refresh token is present.
//
// These tests reproduce the race precisely: subscribe BOTH listeners WITHOUT
// awaiting initAuth() first (the live mount order), pump microtasks, and assert
// NEITHER subscriber's callback HISTORY ever contained a null once a durable
// refresh token exists. A slow (deferred) refresh grant widens the race window.
// --------------------------------------------------------------------------- //

/** A controllable fetch mock whose /oauth2/token resolution we can defer to a
 *  manual trigger, so the async refresh window stays open across the concurrent
 *  subscribe (reproducing the real cold-reload timing deterministically). */
function makeDeferredRefreshFetch(idToken: string): {
  fetchMock: ReturnType<typeof vi.fn>;
  release: () => void;
} {
  let release!: () => void;
  const gate = new Promise<void>((res) => {
    release = res;
  });
  const fetchMock = vi.fn(async () => {
    await gate; // hold the refresh grant open until the test releases it
    return {
      ok: true,
      status: 200,
      json: async () => ({ id_token: idToken, access_token: "acc" }),
    };
  });
  return { fetchMock, release };
}

/** Drain the microtask queue until it is quiet - robust to the multi-hop
 *  init -> refresh -> .finally -> onAuthChanged .then(cb) chain (counting fixed
 *  `await Promise.resolve()` hops is fragile across that depth). */
async function flushMicrotasks(rounds = 30): Promise<void> {
  for (let i = 0; i < rounds; i++) {
    await Promise.resolve();
  }
}

describe("auth.ts - cold-RELOAD restore race (two concurrent onAuthChanged subscribers)", () => {
  it("NEITHER of two concurrent subscribers ever observes a null user when a durable refresh token is present", async () => {
    // Cold reload: ONLY the durable refresh token survives (sessionStorage is
    // empty - a full close clears it). No live in-memory ID token.
    localStorage.setItem(LS_REFRESH, "durable-refresh-race");

    // Deferred refresh grant: the window stays open across BOTH subscribes, so a
    // racing subscriber that short-circuited the OLD latch would fire null here.
    const { fetchMock, release } = makeDeferredRefreshFetch(FRESH_ID_TOKEN);
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");

    // Mount TWO subscribers concurrently, mirroring App.tsx + AuthGuard, WITHOUT
    // pre-awaiting initAuth(). Record the FULL callback history of each.
    const seenA: (import("./auth").AuthUser | null)[] = [];
    const seenB: (import("./auth").AuthUser | null)[] = [];
    const unsubA = auth.onAuthChanged((u) => seenA.push(u));
    const unsubB = auth.onAuthChanged((u) => seenB.push(u));

    // Pump microtasks while the refresh is still GATED. Under the old latch the
    // 2nd subscriber would have already fired cb(null) by now.
    await flushMicrotasks();
    // While the grant is still open NEITHER subscriber may have fired at all
    // (correct: they await the in-flight init), and crucially neither fired null.
    expect(seenA.some((u) => u === null)).toBe(false);
    expect(seenB.some((u) => u === null)).toBe(false);

    // Now let the refresh grant resolve and drain the full chain.
    release();
    // Awaiting getIdToken() guarantees the shared init promise has settled (it
    // awaits the SAME initPromise), after which the subscriber .then(cb) callbacks
    // flush deterministically.
    await auth.getIdToken();
    await flushMicrotasks();

    // THE ASSERTION: across each subscriber's ENTIRE history, no null was ever
    // observed. The race would surface as an early null in seenB (or seenA).
    expect(seenA.length).toBeGreaterThan(0);
    expect(seenB.length).toBeGreaterThan(0);
    expect(seenA.some((u) => u === null)).toBe(false);
    expect(seenB.some((u) => u === null)).toBe(false);

    // And both ultimately observe the restored, non-anonymous identity.
    expect(seenA[seenA.length - 1]?.uid).toBe("cognito-sub-restored");
    expect(seenA[seenA.length - 1]?.isAnonymous).toBe(false);
    expect(seenB[seenB.length - 1]?.uid).toBe("cognito-sub-restored");
    expect(seenB[seenB.length - 1]?.isAnonymous).toBe(false);

    // The refresh grant ran exactly ONCE despite two concurrent subscribers
    // (the memoized in-flight promise is shared, not re-run per subscriber).
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unsubA();
    unsubB();
  });

  it("getIdToken() concurrent with a subscriber also never reports before the durable refresh settles", async () => {
    // The other concurrent caller of initAuth() is getIdToken() (ws.ts handshake
    // / cold case-list fetch). It must not win the latch and return null/empty
    // before the refresh grant restores the session.
    localStorage.setItem(LS_REFRESH, "durable-refresh-token-path");

    const { fetchMock, release } = makeDeferredRefreshFetch(FRESH_ID_TOKEN);
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");

    const seen: (import("./auth").AuthUser | null)[] = [];
    const unsub = auth.onAuthChanged((u) => seen.push(u));
    // Fire getIdToken() concurrently (do NOT await it yet) - it awaits the SAME
    // in-flight init promise.
    const tokenPromise = auth.getIdToken();

    await flushMicrotasks();
    // Still gated: the subscriber has not yet fired a (null) user.
    expect(seen.some((u) => u === null)).toBe(false);

    release();
    const token = await tokenPromise;
    await flushMicrotasks();

    // getIdToken returns the freshly minted ID token (NOT null), and the
    // subscriber never saw a null.
    expect(token).toBe(FRESH_ID_TOKEN);
    expect(seen.some((u) => u === null)).toBe(false);
    expect(seen[seen.length - 1]?.uid).toBe("cognito-sub-restored");
    // One shared refresh grant for both the subscriber and getIdToken.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    unsub();
  });

  it("DISABLED mode (Cognito env absent) still fires a SINGLE null for each subscriber (anonymous-only preserved)", async () => {
    // No Cognito env -> isFirebaseConfigured() false -> disabled / anonymous-only
    // mode. The contract: each subscriber fires exactly once with null and never
    // again (a stable signed-out snapshot). The promise-memoization must NOT
    // change this byte-for-byte.
    vi.unstubAllEnvs(); // drop the Cognito env stubbed in beforeEach
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const auth = await import("./auth");

    const seenA: (import("./auth").AuthUser | null)[] = [];
    const seenB: (import("./auth").AuthUser | null)[] = [];
    const unsubA = auth.onAuthChanged((u) => seenA.push(u));
    const unsubB = auth.onAuthChanged((u) => seenB.push(u));

    await flushMicrotasks();

    // Exactly one null fire each - no double-fire, no spurious non-null.
    expect(seenA).toEqual([null]);
    expect(seenB).toEqual([null]);
    expect(auth.authStatus()).toBe("disabled");
    // No network attempted in disabled mode.
    expect(fetchMock).not.toHaveBeenCalled();
    unsubA();
    unsubB();
  });
});
