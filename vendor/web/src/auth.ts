// GRACE-2 web — AWS Cognito Auth client (GCP→AWS migration).
//
// Replaces the Firebase client with an AWS Cognito Hosted UI OIDC
// authorization-code-flow (with PKCE) client behind the SAME exported surface
// the rest of the app already consumes:
//   - isFirebaseConfigured()  — kept as the name so AuthGuard / AuthGate /
//                                useAuth need no rename. Now reports whether the
//                                Cognito Hosted UI is configured.
//   - authStatus()            — synchronous init-status read.
//   - initAuth()              — restores any persisted session (no network).
//   - onAuthChanged(cb)       — fires the current AuthUser | null.
//   - getIdToken(forceRefresh)— returns the stored Cognito ID token (refreshing
//                                via the refresh_token grant when near expiry).
//   - signIn()                — redirects to the Hosted UI authorize endpoint
//                                (email/password sign-up + confirm + sign-in).
//   - signInWithGoogle()      — alias of signIn() so existing imports keep
//                                building; the Hosted UI is email/password.
//   - signOut()               — redirects to the Hosted UI /logout.
//   - handleRedirectCallback()— exchanges the ?code= for tokens on /callback.
//   - __setAuthForTesting()   — unit-test seam (kept; takes an AuthUser | null).
//   - AuthUser shape          — unchanged, so AuthGuard/useAuth/ws.ts stay aligned.
//
// Decision F (wire isolation) is preserved: only the ID token (a JWT) crosses
// to the agent on the ws auth-token frame; the refresh token never leaves the
// browser. The agent verifies the ID token against the Cognito JWKS.
//
// Disabled mode: when VITE_COGNITO_USER_POOL_ID / VITE_COGNITO_CLIENT_ID /
// VITE_COGNITO_DOMAIN are not all set, isFirebaseConfigured() returns false and
// the app is in anonymous-only / pass-through mode (the load-bearing dev /
// tailnet path) — byte-identical to the old Firebase-disabled posture. This is
// the default so the live demo is unaffected until the orchestrator injects the
// VITE_COGNITO_* env and rebuilds.

/** Minimal user-facing identity shape exposed to React. Decoupled from the IdP so the rest of the app stays library-agnostic. */
export interface AuthUser {
  /** IdP subject (Cognito `sub`); stable across token refreshes. Stored as the agent's lookup key. */
  uid: string;
  /** Display name (Cognito `name` claim; null when absent). */
  displayName: string | null;
  /** Email (Cognito `email` claim; null when absent). */
  email: string | null;
  /** Photo URL — Cognito email/password has none; always null. Kept for shape parity. */
  photoURL: string | null;
  /** True for anonymous sessions. Cognito Hosted UI sign-in is always non-anonymous. */
  isAnonymous: boolean;
}

/** Connection status of the auth subsystem. */
export type AuthInitStatus =
  | "disabled" // VITE_COGNITO_* absent — local dev / anonymous-only mode
  | "initializing"
  | "ready"
  | "failed";

// --------------------------------------------------------------------------- //
// Configuration (Vite env). All three must be set to enable Cognito.
// --------------------------------------------------------------------------- //

interface CognitoConfig {
  poolId: string;
  clientId: string;
  /** Hosted UI domain, e.g. `grace2-auth.auth.us-west-2.amazoncognito.com`. */
  domain: string;
  region: string;
  /** Redirect URI registered as a Hosted UI callback URL. */
  redirectUri: string;
}

function readConfig(): CognitoConfig {
  const env = import.meta.env;
  const region = ((env.VITE_COGNITO_REGION as string | undefined) ?? "us-west-2") || "us-west-2";
  let redirectUri = (env.VITE_COGNITO_REDIRECT_URI as string | undefined) ?? "";
  if (!redirectUri && typeof window !== "undefined" && window.location?.origin) {
    // Default the redirect to the app origin root so a sensible value exists
    // when the env var is omitted (must still match a registered callback URL).
    redirectUri = `${window.location.origin}/`;
  }
  return {
    poolId: ((env.VITE_COGNITO_USER_POOL_ID as string | undefined) ?? "") as string,
    clientId: ((env.VITE_COGNITO_CLIENT_ID as string | undefined) ?? "") as string,
    domain: ((env.VITE_COGNITO_DOMAIN as string | undefined) ?? "") as string,
    region,
    redirectUri,
  };
}

/**
 * Are the required Vite env vars set so Cognito Hosted UI can be used?
 *
 * Name retained from the Firebase era so AuthGuard / AuthGate / useAuth call
 * sites need no rename. Returns true only when pool id + client id + domain are
 * all present; absence ⇒ disabled / anonymous-only mode (pass-through).
 */
export function isFirebaseConfigured(): boolean {
  const c = readConfig();
  return c.poolId.length > 0 && c.clientId.length > 0 && c.domain.length > 0;
}

// --------------------------------------------------------------------------- //
// Token store: sessionStorage for the live ID/access token set (tab-scoped, fast
// reload path) + a DURABLE refresh token in localStorage so the Cognito session
// can be restored CLIENT-SIDE on a cold boot (new tab / browser restart / a
// phone that evicted the tab), independent of the agent / WebSocket.
//
// COLD-CASES-BOX-OFF FIX (NATE 2026-06-20): the cases rail (and cold case-view)
// fetch the serverless /case-list with the Cognito ID token; with the agent box
// ASLEEP that is the ONLY way they can authenticate. Previously the ENTIRE token
// set lived in sessionStorage, which a fresh tab / restart clears — so box-off
// the user looked SIGNED OUT (isSignedIn=false -> coldListIdentity="anon" -> a
// tokenless, authoritative-EMPTY list) until the box woke and the user re-signed
// in. Persisting the long-lived REFRESH token in localStorage lets initAuth()
// mint a fresh ID token via the Cognito refresh_token grant on load (a direct
// POST to the Hosted UI /oauth2/token endpoint — NO agent involvement), so the
// session is restored and the cold fetches go out authenticated. The minted ID
// token is byte-identical to a Hosted-UI-minted one (same Cognito issuer,
// claims, JWKS), so the agent's verify_id_token + the Lambdas' verification are
// completely unchanged. The refresh token NEVER leaves the browser (Decision F
// wire isolation preserved — only the ID token crosses to the agent).
// --------------------------------------------------------------------------- //

interface TokenSet {
  idToken: string;
  accessToken: string | null;
  refreshToken: string | null;
  /** Epoch ms when the ID token expires (from the `exp` claim). */
  expiresAt: number;
}

const SS_TOKENS = "grace2_cognito_tokens";
const SS_PKCE_VERIFIER = "grace2_cognito_pkce_verifier";
/** Durable (localStorage) refresh token — survives tab close / browser restart
 *  so the session can be restored on a cold boot box-off. */
const LS_REFRESH = "grace2_cognito_refresh";

let injectedUser: AuthUser | null = null;
let injectedActive = false;

let cachedTokens: TokenSet | null = null;
let cachedUser: AuthUser | null = null;
let cachedInitStatus: AuthInitStatus = "disabled";
// Post-resolve FAST-PATH flag only. Set true ONLY after the async restore in
// initAuth() has fully settled (the durable refresh grant resolved). A caller
// that sees this true can skip re-running init. It is NOT the concurrency latch
// anymore - see initPromise below.
let initialized = false;
// In-flight memoized init promise (COLD-RELOAD RESTORE RACE FIX, session
// durability Job A). The FIRST initAuth() caller creates+awaits this promise;
// EVERY concurrent caller awaits the SAME promise, so no racing subscriber can
// observe a pre-refresh (null) user. Two onAuthChanged subscribers mount on a
// cold reload (App.tsx + AuthGuard via useAuth); before this, the 2nd saw the
// synchronous `initialized = true` latch, short-circuited, and fired its
// callback with cachedUser still null -> the gate painted Sign-in before the
// durable refresh_token grant resolved. Now both await the same settle.
let initPromise: Promise<void> | null = null;

const subscribers = new Set<(u: AuthUser | null) => void>();

/** Test seam: inject a fake signed-in user (or null) without a real Cognito pool. */
export function __setAuthForTesting(user: AuthUser | null): void {
  injectedUser = user;
  injectedActive = true;
  cachedUser = user;
  cachedInitStatus = user ? "ready" : "disabled";
  initialized = true;
  // A resolved promise so any pending `await initAuth()` settles immediately
  // against the injected identity (the injectedActive guard short-circuits init,
  // but keep the memo consistent so a fast-path caller never re-runs restore).
  initPromise = Promise.resolve();
  for (const cb of subscribers) cb(user);
}

/** Current init status (synchronous read). */
export function authStatus(): AuthInitStatus {
  return cachedInitStatus;
}

// --------------------------------------------------------------------------- //
// JWT helpers (no signature verification — the agent does that against JWKS).
// --------------------------------------------------------------------------- //

function base64UrlDecode(input: string): string {
  const pad = input.length % 4 === 0 ? "" : "=".repeat(4 - (input.length % 4));
  const b64 = (input + pad).replace(/-/g, "+").replace(/_/g, "/");
  if (typeof atob === "function") return atob(b64);
  // Node / SSR fallback.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (globalThis as any).Buffer.from(b64, "base64").toString("binary");
}

function decodeJwtClaims(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split(".");
    const payload = parts[1];
    if (!payload) return null;
    const json = decodeURIComponent(
      Array.prototype.map
        .call(base64UrlDecode(payload), (c: string) => {
          return "%" + ("00" + c.charCodeAt(0).toString(16)).slice(-2);
        })
        .join(""),
    );
    return JSON.parse(json) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function userFromIdToken(idToken: string): AuthUser | null {
  const claims = decodeJwtClaims(idToken);
  if (!claims || typeof claims.sub !== "string") return null;
  return {
    uid: claims.sub,
    displayName: (claims.name as string | undefined) ?? null,
    email: (claims.email as string | undefined) ?? null,
    photoURL: null,
    isAnonymous: false,
  };
}

function expiresAtFromIdToken(idToken: string): number {
  const claims = decodeJwtClaims(idToken);
  const exp = claims && typeof claims.exp === "number" ? claims.exp : 0;
  return exp * 1000;
}

// --------------------------------------------------------------------------- //
// Token persistence.
// --------------------------------------------------------------------------- //

/** Read the durable refresh token from localStorage (null when absent / blocked). */
function loadDurableRefresh(): string | null {
  try {
    const raw = localStorage.getItem(LS_REFRESH);
    return raw && raw.trim() !== "" ? raw : null;
  } catch {
    // localStorage unavailable (private mode) — durable restore not possible.
    return null;
  }
}

/**
 * Load the persisted token set.
 *
 * Precedence:
 *   1. The full sessionStorage ID/access/refresh token set (same-tab fast path,
 *      survives a reload). Returned verbatim when it carries an ID token.
 *   2. When sessionStorage holds no ID token but localStorage holds a DURABLE
 *      refresh token (a cold boot box-off: fresh tab / restart), synthesise a
 *      refresh-ONLY TokenSet (idToken="" / expiresAt=0). initAuth()'s
 *      "expired-but-has-refresh" branch then mints a fresh ID token client-side
 *      via the Cognito refresh_token grant — restoring the session with no agent
 *      / WebSocket involvement.
 *   3. null — neither present (genuinely signed out).
 */
function loadTokens(): TokenSet | null {
  try {
    const raw = sessionStorage.getItem(SS_TOKENS);
    if (raw) {
      const t = JSON.parse(raw) as TokenSet;
      // Backfill the durable refresh token if the sessionStorage copy lost it
      // (Cognito does not re-issue one on refresh, so a refreshed sessionStorage
      // set carries the original — but be defensive across storage edits).
      if (t.idToken) {
        if (!t.refreshToken) t.refreshToken = loadDurableRefresh();
        return t;
      }
    }
  } catch {
    // fall through to the durable-refresh restore
  }
  // No usable sessionStorage ID token — try the durable refresh token so a cold
  // boot box-off can still restore the signed-in session client-side.
  const durable = loadDurableRefresh();
  if (durable) {
    return { idToken: "", accessToken: null, refreshToken: durable, expiresAt: 0 };
  }
  return null;
}

function storeTokens(t: TokenSet | null): void {
  cachedTokens = t;
  try {
    if (t) sessionStorage.setItem(SS_TOKENS, JSON.stringify(t));
    else sessionStorage.removeItem(SS_TOKENS);
  } catch {
    // sessionStorage unavailable (private mode) — proceed in-memory only.
  }
  // Mirror the long-lived refresh token to localStorage so the session survives
  // a tab close / browser restart (the box-off cold-boot restore path). Clearing
  // the token set (sign-out / terminal refresh failure) removes it too.
  try {
    if (t && t.refreshToken) localStorage.setItem(LS_REFRESH, t.refreshToken);
    else localStorage.removeItem(LS_REFRESH);
  } catch {
    // localStorage unavailable (private mode) — durable restore won't be
    // available next cold boot, but the in-tab session still works.
  }
}

function setSession(t: TokenSet | null): void {
  storeTokens(t);
  cachedUser = t ? userFromIdToken(t.idToken) : null;
  for (const cb of subscribers) cb(cachedUser);
}

// --------------------------------------------------------------------------- //
// PKCE helpers.
// --------------------------------------------------------------------------- //

function randomString(bytes = 48): string {
  const arr = new Uint8Array(bytes);
  crypto.getRandomValues(arr);
  let s = "";
  for (const b of arr) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256Base64Url(input: string): Promise<string> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", data);
  let s = "";
  const view = new Uint8Array(digest);
  for (const b of view) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// --------------------------------------------------------------------------- //
// Public surface.
// --------------------------------------------------------------------------- //

/**
 * Initialise the auth subsystem. Restores the signed-in session CLIENT-SIDE:
 *   - a same-tab reload reuses the sessionStorage ID token (no network), OR
 *   - a cold boot (fresh tab / browser restart / box-off) mints a fresh ID
 *     token from the DURABLE localStorage refresh token via the Cognito
 *     refresh_token grant (one direct POST to /oauth2/token — NO agent / WS),
 * or reports "disabled" when Cognito env vars are absent.
 *
 * The restored ID token is what getIdToken() returns + what ws.ts sends on the
 * auth-token handshake, so the cold case-list / case-view fetches authenticate
 * box-off exactly as box-on. Idempotent.
 */
export async function initAuth(): Promise<void> {
  // Injected test seam: never run the real restore (the seam owns cachedUser).
  if (injectedActive) return;
  // Fast path: the async restore has already fully settled.
  if (initialized) return;
  // CONCURRENCY: the FIRST caller creates the in-flight promise; EVERY
  // concurrent caller awaits the SAME promise, so none reports (fires its
  // onAuthChanged callback / returns from getIdToken) before the durable
  // refresh_token grant settles. `initialized` is flipped true at the END of
  // runInit() (a post-resolve fast-path flag) - NOT synchronously at the top,
  // which was the cold-reload Sign-in-flash race. runInit() has no throwing
  // paths (the refresh grant is .catch-guarded), so it always reaches that flag.
  // Awaiting runInit() directly (no extra .finally wrapper) keeps the resolution
  // microtask-depth identical to the old `await runInit()` for the synchronous
  // disabled / already-restored paths existing consumers + tests depend on.
  if (!initPromise) {
    initPromise = runInit();
  }
  await initPromise;
}

/** The actual restore work, run exactly once and shared via `initPromise`. */
async function runInit(): Promise<void> {
  if (!isFirebaseConfigured()) {
    cachedInitStatus = "disabled";
    cachedTokens = null;
    cachedUser = null;
    initialized = true;
    return;
  }

  cachedInitStatus = "initializing";
  const t = loadTokens();
  if (t && t.expiresAt > Date.now()) {
    // Live ID token (same-tab reload fast path). Persist via storeTokens — NOT a
    // bare `cachedTokens = t` — so the DURABLE localStorage refresh mirror is
    // (re)seeded on EVERY box-on load that has a valid session.
    //
    // COLD-CASES-BOX-OFF SEED FIX (NATE 2026-06-20): a session signed in BEFORE
    // the durable-mirror landed (or any plain reload) carries its refresh token
    // only in sessionStorage. The old live-token branch skipped storeTokens, so
    // localStorage's LS_REFRESH was NEVER written on a normal reload — only a
    // fresh OAuth sign-in or a token-expiry refresh seeded it. That left the
    // user one cold boot away from resolving signed-OUT box-off (blank Cases
    // rail) despite holding a perfectly good refresh token. Routing through
    // storeTokens mirrors that refresh token to localStorage every load, so the
    // very next box-off cold boot restores the session with no manual re-sign-in.
    storeTokens(t);
    cachedUser = userFromIdToken(t.idToken);
  } else if (t && t.refreshToken) {
    // No live ID token but we hold a refresh token (expired sessionStorage set
    // OR a durable-refresh-only cold boot) — mint a fresh ID token client-side.
    const refreshed = await refreshTokens(t.refreshToken).catch(() => null);
    if (refreshed) {
      // Persist so the in-tab fast path + the durable refresh mirror are both
      // up to date (storeTokens writes sessionStorage + localStorage).
      storeTokens(refreshed);
      cachedUser = userFromIdToken(refreshed.idToken);
    } else {
      // Refresh failed (revoked / expired refresh token) — clear durable state.
      storeTokens(null);
      cachedUser = null;
    }
  } else {
    cachedTokens = null;
    cachedUser = null;
  }
  cachedInitStatus = "ready";
  // Post-resolve fast-path flag: the async restore (incl. the refresh grant) has
  // fully settled here, so a later initAuth() caller can short-circuit. Set ONLY
  // now, never synchronously - that synchronous latch was the cold-reload race.
  initialized = true;
}

/**
 * Subscribe to auth-state changes. Fires once with the current user (or null)
 * after init, then on every sign-in / sign-out / refresh. Returns an
 * unsubscribe function.
 *
 * In disabled mode (env vars absent) it fires once with null and stays a
 * stable signed-out snapshot — anonymous-only mode, exactly as before.
 */
export function onAuthChanged(cb: (u: AuthUser | null) => void): () => void {
  subscribers.add(cb);
  let cancelled = false;
  if (injectedActive) {
    cb(injectedUser);
  } else {
    void initAuth().then(() => {
      if (!cancelled) cb(cachedUser);
    });
  }
  return () => {
    cancelled = true;
    subscribers.delete(cb);
  };
}

/**
 * Retrieve the current user's Cognito **ID token** (JWT) for the WebSocket
 * handshake (H.5). Returns null when signed out OR Cognito is disabled — ws.ts
 * handles both as "anonymous fallback: skip auth-token".
 *
 * If `forceRefresh` is true OR the token is near expiry and we hold a refresh
 * token, mints a fresh ID token via the refresh_token grant. This satisfies
 * the ws.ts handleAuthFailure one-shot forceRefresh retry.
 */
export async function getIdToken(forceRefresh = false): Promise<string | null> {
  if (injectedActive) {
    // Tests: synthesize a stable fake JWT-ish token from the injected user, or
    // null when signed out. ws.ts only needs a non-empty string here.
    return injectedUser ? `test-id-token:${injectedUser.uid}` : null;
  }
  await initAuth();
  const t = cachedTokens;
  if (!t) return null;
  const nearExpiry = t.expiresAt - Date.now() < 60_000; // refresh within 1 min
  if ((forceRefresh || nearExpiry) && t.refreshToken) {
    const refreshed = await refreshTokens(t.refreshToken).catch(() => null);
    if (refreshed) {
      setSession(refreshed);
      return refreshed.idToken;
    }
    // Refresh failed — if the current token is still live, keep using it;
    // otherwise treat as signed out.
    if (t.expiresAt <= Date.now()) {
      setSession(null);
      return null;
    }
  }
  return t.idToken;
}

/**
 * Synchronous, best-effort read of the current Cognito ID token from the
 * in-memory cache -- NO network, NO refresh, NO promise.
 *
 * Returns the SAME id token `getIdToken()` would return when the token is not
 * near expiry: it reads the very same `cachedTokens` cache that `initAuth()` /
 * `getIdToken()` populate (so it is NOT a new auth fetch -- it is the existing
 * token source, read synchronously). Returns null when signed out / disabled.
 *
 * ws.ts uses this to carry the id token on the WebSocket subprotocol at DIAL
 * time, where the socket construction must stay synchronous (the connect path
 * and its reconnect/keepalive timers depend on it). The authoritative refresh
 * still happens in the async `getIdToken()` path that feeds the in-band
 * `auth-token` handshake; this is only the pre-upgrade routing carrier, so a
 * not-yet-refreshed-but-valid cached token is acceptable here (the broker
 * re-verifies and the agent's in-band check is authoritative). Mirrors
 * `getIdToken()`'s injected-test behaviour so unit tests behave consistently.
 */
export function getIdTokenSync(): string | null {
  if (injectedActive) {
    return injectedUser ? `test-id-token:${injectedUser.uid}` : null;
  }
  const t = cachedTokens;
  if (t && t.idToken) return t.idToken;
  // BROKER-AUTH RACE FIX (real-account reload, NATE 2026-06-29). `cachedTokens`
  // is populated ONLY when `initAuth()`'s ASYNC restore settles -- a same-tab
  // reload reads sessionStorage, a cold boot awaits the refresh_token grant. But
  // the WebSocket dial is SYNCHRONOUS and fires at App mount BEFORE that settle
  // resolves, so on a fresh page load a SIGNED-IN user's first dial read
  // `cachedTokens === null` -> carried NO `?st` token -> the per-session broker
  // rejected the connect (close 4401) and never provisioned an agent (routes
  // Count=0). The DEMO-CODE path never hit this because `signInWithAccessCode()`
  // sets `cachedTokens` SYNCHRONOUSLY in the same gesture, so its first dial
  // always carried `?st`. THE FIX: fall back to a SYNCHRONOUS read of the
  // persisted sessionStorage token set so a same-tab reload carries a valid
  // `?st` on the VERY FIRST dial, before `initAuth()` resolves. Warm
  // `cachedTokens` so later sync reads + the in-band `auth-token` handshake see
  // the same token (idempotent with the value `initAuth()` will store). Only a
  // NON-EXPIRED id token is returned: handing the broker a definitively-expired
  // token would hard-reject, whereas null lets the async `getIdToken()` refresh
  // (+ the existing reconnect) carry a fresh one. Pure sync, no network, no
  // reconnect -- non-breaking for the demo-code path AND the single box (the box
  // ignores `?st` and reads the in-band auth-token; anonymous/signed-out/disabled
  // have no token set here and still return null, so no `?st` rides).
  try {
    const raw = sessionStorage.getItem(SS_TOKENS);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as TokenSet;
    if (parsed && parsed.idToken && parsed.expiresAt > Date.now()) {
      cachedTokens = parsed;
      return parsed.idToken;
    }
  } catch {
    // sessionStorage unavailable (private mode) / malformed -> no sync token.
  }
  return null;
}

/**
 * Begin sign-in: redirect to the Cognito Hosted UI authorize endpoint
 * (email/password sign-up + confirm + sign-in). Uses the OIDC authorization
 * code flow with PKCE (the SPA app client is public / no secret).
 *
 * Throws when Cognito is not configured.
 */
export async function signIn(): Promise<void> {
  const c = readConfig();
  if (!isFirebaseConfigured()) {
    throw new Error(
      "Cognito not configured (set VITE_COGNITO_USER_POOL_ID / VITE_COGNITO_CLIENT_ID / VITE_COGNITO_DOMAIN)",
    );
  }
  const verifier = randomString(48);
  try {
    sessionStorage.setItem(SS_PKCE_VERIFIER, verifier);
  } catch {
    // sessionStorage unavailable — PKCE cannot survive the redirect; sign-in
    // will fail on callback, which surfaces as an auth error (acceptable).
  }
  const challenge = await sha256Base64Url(verifier);
  const params = new URLSearchParams({
    client_id: c.clientId,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: c.redirectUri,
    code_challenge_method: "S256",
    code_challenge: challenge,
  });
  window.location.assign(`https://${c.domain}/oauth2/authorize?${params.toString()}`);
}

/** Alias for `signIn` so existing `signInWithGoogle` imports keep building. */
export async function signInWithGoogle(): Promise<AuthUser | null> {
  await signIn();
  return null; // the redirect navigates away; the user resolves on /callback.
}

/**
 * Exchange the Hosted UI `?code=` for tokens on the OAuth /callback. Call from
 * App.tsx boot when `?code=` is present in the URL. Stores the token set,
 * flips `onAuthChanged` to the signed-in user, and returns the AuthUser.
 *
 * No-op (returns null) when there is no code, Cognito is disabled, or the PKCE
 * verifier is missing.
 */
export async function handleRedirectCallback(): Promise<AuthUser | null> {
  if (!isFirebaseConfigured()) return null;
  const url = new URL(window.location.href);
  const code = url.searchParams.get("code");
  if (!code) return null;

  const c = readConfig();
  let verifier = "";
  try {
    verifier = sessionStorage.getItem(SS_PKCE_VERIFIER) ?? "";
  } catch {
    verifier = "";
  }
  if (!verifier) {
    // No PKCE verifier (e.g. opened the callback in a fresh tab) — cannot
    // complete the exchange. Strip the code and bail to the sign-in surface.
    return null;
  }

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: c.clientId,
    code,
    redirect_uri: c.redirectUri,
    code_verifier: verifier,
  });
  try {
    const resp = await fetch(`https://${c.domain}/oauth2/token`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    if (!resp.ok) {
      // eslint-disable-next-line no-console
      console.warn("[auth] token exchange failed:", resp.status);
      return null;
    }
    const data = (await resp.json()) as {
      id_token?: string;
      access_token?: string;
      refresh_token?: string;
    };
    if (!data.id_token) return null;
    const tokens: TokenSet = {
      idToken: data.id_token,
      accessToken: data.access_token ?? null,
      refreshToken: data.refresh_token ?? null,
      expiresAt: expiresAtFromIdToken(data.id_token),
    };
    try {
      sessionStorage.removeItem(SS_PKCE_VERIFIER);
    } catch {
      // ignore
    }
    // The exchange has fully established the session synchronously here, so mark
    // init settled AND seed a resolved memo. Any concurrent `await initAuth()`
    // (a subscriber that mounted before the callback finished) then settles
    // against this signed-in identity instead of re-running the restore - it can
    // never observe a pre-session null user.
    initialized = true;
    initPromise = Promise.resolve();
    cachedInitStatus = "ready";
    setSession(tokens);
    return cachedUser;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[auth] token exchange error:", err);
    return null;
  }
}

/**
 * Resolve the demo-token (code-gate) endpoint URL, or null when unconfigured.
 *
 * Precedence (mirrors the wake-api base precedence in lib/case_list.ts):
 *   1. `VITE_GRACE2_DEMO_TOKEN_URL` -- an explicit full URL to the demo-token
 *      Lambda / API-Gateway route (e.g.
 *      "https://abc123.execute-api.us-west-2.amazonaws.com/demo-token").
 *      Used verbatim (trailing slashes trimmed). This is the production path:
 *      the autostop API-Gateway is a SEPARATE origin from the CloudFront edge,
 *      so it must be supplied explicitly.
 *   2. `VITE_GRACE2_PUBLIC_BASE` + "/demo-token" -- a convenience for a future
 *      world where the route is folded behind the same edge as the agent.
 *   3. null -- nothing configured (the code-entry submit then fails closed with
 *      the generic "Invalid code" error, no oracle).
 */
function demoTokenUrl(): string | null {
  const explicit =
    (import.meta.env.VITE_GRACE2_DEMO_TOKEN_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.trim().replace(/\/+$/, "");
  }
  // The demo-token route lives on the autostop API-Gateway -- a SEPARATE origin
  // from the CloudFront edge that VITE_GRACE2_PUBLIC_BASE points at -- so we do
  // NOT derive it from PUBLIC_BASE (that resolved to <cloudfront>/demo-token,
  // which has no such route and CORS-fails in the browser). Use the wake-api
  // route directly. VITE_GRACE2_DEMO_TOKEN_URL still overrides. Host is public.
  return "https://9ib093sis6.execute-api.us-west-2.amazonaws.com/demo-token";
}

/**
 * Code-gate sign-in (JUDGE-visible access-code path). POSTs the entered access
 * code to the demo-token endpoint, which returns a Cognito token set minted for
 * the shared demo identity. On success the session is established CLIENT-SIDE
 * exactly as the OAuth /callback exchange does (handleRedirectCallback) -- the
 * init-settle fields are stamped BEFORE setSession so any concurrent
 * `await initAuth()` subscriber settles against this signed-in identity instead
 * of re-running the restore (it can never observe a pre-session null user), and
 * setSession writes SS_TOKENS + the durable LS_REFRESH mirror + fires
 * onAuthChanged (which flips the AuthGuard to MODE 3 -> children mount).
 *
 * On ANY failure (unconfigured endpoint, non-200, network error, missing
 * id_token) this throws a GENERIC `Error("Invalid code")` -- deliberately no
 * oracle that distinguishes a wrong code from a server/transport fault.
 */
export async function signInWithAccessCode(code: string): Promise<void> {
  const endpoint = demoTokenUrl();
  if (!endpoint) throw new Error("Invalid code");

  let data: {
    id_token?: string;
    access_token?: string;
    refresh_token?: string;
  };
  try {
    const resp = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: code.trim() }),
    });
    if (!resp.ok) throw new Error("Invalid code");
    data = (await resp.json()) as typeof data;
  } catch {
    // Generic — never leak whether the code was wrong vs the transport failed.
    throw new Error("Invalid code");
  }

  if (!data.id_token) throw new Error("Invalid code");

  const tokens: TokenSet = {
    idToken: data.id_token,
    accessToken: data.access_token ?? null,
    refreshToken: data.refresh_token ?? null,
    expiresAt: expiresAtFromIdToken(data.id_token),
  };
  // Stamp the init-settle fields EXACTLY as handleRedirectCallback does, BEFORE
  // setSession, so the session is fully established synchronously here.
  initialized = true;
  initPromise = Promise.resolve();
  cachedInitStatus = "ready";
  setSession(tokens);
}

/** Mint a fresh token set via the refresh_token grant. Returns null on failure. */
async function refreshTokens(refreshToken: string): Promise<TokenSet | null> {
  const c = readConfig();
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: c.clientId,
    refresh_token: refreshToken,
  });
  const resp = await fetch(`https://${c.domain}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
  if (!resp.ok) return null;
  const data = (await resp.json()) as { id_token?: string; access_token?: string };
  if (!data.id_token) return null;
  // Cognito does not return a new refresh_token on refresh — reuse the old one.
  return {
    idToken: data.id_token,
    accessToken: data.access_token ?? null,
    refreshToken,
    expiresAt: expiresAtFromIdToken(data.id_token),
  };
}

/**
 * Sign out. Clears the local token set and (when configured) redirects to the
 * Cognito Hosted UI /logout endpoint so the IdP session is also cleared.
 * No-op clear when Cognito is disabled.
 */
export async function signOut(): Promise<void> {
  if (injectedActive) {
    injectedUser = null;
    cachedUser = null;
    for (const cb of subscribers) cb(null);
    return;
  }
  setSession(null);
  if (!isFirebaseConfigured()) return;
  const c = readConfig();
  const params = new URLSearchParams({
    client_id: c.clientId,
    logout_uri: c.redirectUri,
  });
  window.location.assign(`https://${c.domain}/logout?${params.toString()}`);
}

/**
 * Anonymous sign-in. Cognito has no client-side anonymous identity (the agent
 * provides the H.3 anonymous fallback when no auth-token is sent), so this is a
 * no-op returning null — ws.ts skips the auth-token frame and the server's
 * anonymous fallback handles the session. Kept for import compatibility.
 */
export async function signInAnonymous(): Promise<AuthUser | null> {
  return null;
}
