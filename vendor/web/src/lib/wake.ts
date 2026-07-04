// GRACE-2 web — agent-box wake-up client (auto-stop/wake infra, NATE 2026-06-17).
//
// The always-on AGENT box (EC2 t3.large running grace2-agent WS :8765 +
// catalog HTTP :8766 + titiler :8080, fronted by CloudFront) is now eligible
// to be STOPPED by an idle-check Lambda after N consecutive zero-connection
// polls. A stopped box answers neither the WebSocket nor any HTTP endpoint,
// so the browser cannot reach the agent until the instance is started again.
//
// This module is the WEB side of the wake contract:
//   - `wakeUrl()` derives the API-Gateway HTTP endpoint that fronts the
//     StartInstances "wake" Lambda. Precedence (most specific wins), mirroring
//     lib/public_base.ts:
//         VITE_GRACE2_WAKE_URL  >  VITE_GRACE2_PUBLIC_BASE(/wake)  >  null
//     When NOTHING is configured, `wakeUrl()` returns null and `wakeAgent()`
//     is a no-op — dev / localhost / LAN builds (where the box is never
//     auto-stopped) behave exactly as before.
//   - `wakeAgent()` fires a single POST to that endpoint to ask the Lambda to
//     StartInstances. It is FIRE-AND-FORGET (the WS reconnect loop owns the
//     retry that actually re-establishes the connection) and DEBOUNCED so a
//     burst of reconnect ticks coalesces into one StartInstances request.
//
// The web NEVER calls EC2 directly and holds NO AWS credentials — the wake
// endpoint is a least-privilege API-Gateway → Lambda that the infra root
// (infra/aws-autostop) provisions. This module performs no work beyond reading
// `import.meta.env` / `import.meta.env.VITE_GRACE2_PUBLIC_BASE` and issuing a
// `fetch`; it is pure + unit-testable (the fetch + clock are injectable).

import { normalizePublicBase } from "./public_base";

/**
 * Read `VITE_GRACE2_PUBLIC_BASE` (build-time), normalised to an origin with no
 * trailing slash. null when unset/blank. Local copy of public_base.ts's private
 * helper so the two seams stay decoupled (public_base owns WS/HTTP; this owns
 * the wake endpoint).
 */
function publicBase(): string | null {
  const raw =
    (import.meta.env.VITE_GRACE2_PUBLIC_BASE as string | undefined) ?? null;
  return normalizePublicBase(raw);
}

/**
 * Canonical wake-endpoint URL, or null when wake is not configured.
 *
 * Precedence:
 *   1. `VITE_GRACE2_WAKE_URL` — an explicit full URL to the API-Gateway wake
 *      endpoint (e.g. "https://abc123.execute-api.us-west-2.amazonaws.com/wake").
 *      Used verbatim (trailing slashes trimmed). This is the production path:
 *      the autostop API-Gateway is a SEPARATE origin from the CloudFront edge,
 *      so it must be supplied explicitly.
 *   2. `VITE_GRACE2_PUBLIC_BASE` + "/wake" — a convenience for a future world
 *      where the wake route is folded behind the same edge as the agent.
 *   3. null — nothing configured; wake is disabled (dev/LAN; the box is never
 *      auto-stopped there).
 */
export function wakeUrl(): string | null {
  const explicit =
    (import.meta.env.VITE_GRACE2_WAKE_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.trim().replace(/\/+$/, "");
  }

  const base = publicBase();
  if (base) return `${base}/wake`;

  return null;
}

/** True iff a wake endpoint is configured — UI gates the "Wake up agent"
 *  overlay on this so dev/LAN never shows it (the box can't be stopped there). */
export function wakeConfigured(): boolean {
  return wakeUrl() !== null;
}

/** Outcome of a `wakeAgent()` call. */
export type WakeResult =
  | { status: "sent" } // POST issued and accepted (2xx) — Lambda asked to start the box
  | { status: "debounced" } // a recent wake is still within the debounce window; skipped
  | { status: "disabled" } // no wake endpoint configured (dev/LAN)
  | { status: "error"; error: unknown }; // POST failed (network / non-2xx)

/**
 * Reported lifecycle state of the agent EC2 box (sleep/wake STAGE 2 asleep
 * detection). Mirrors the EC2 instance-state vocabulary the wake Lambda returns
 * on a GET (report-only) probe, collapsed to the values the composer machine
 * branches on:
 *   - "stopped" / "stopping" — the box is (or is becoming) ASLEEP → show Wake UI.
 *   - "running" / "pending"  — the box is up (or coming up) → keep retrying WS
 *     (Connecting). "pending" can follow a POST wake (StartInstances issued).
 *   - "unknown"              — wake not configured (dev/LAN), the probe failed,
 *     or the body was unparseable. The caller treats this as "don't show Wake
 *     UI from a probe" and falls back to the plain reconnect/Connecting path.
 *
 * GET /wake REPORTS this WITHOUT waking; only POST /wake wakes (the handler
 * enforces the split server-side). `wakeState()` is therefore safe to call on
 * every WS connect-fail with no risk of starting the box.
 */
export type WakeState =
  | "stopped"
  | "stopping"
  | "running"
  | "pending"
  | "unknown";

/**
 * Normalise an EC2-ish instance-state string into the closed {@link WakeState}.
 * Unknown / absent values collapse to "unknown" (never spuriously "stopped").
 */
function normalizeWakeState(raw: unknown): WakeState {
  if (typeof raw !== "string") return "unknown";
  const s = raw.trim().toLowerCase();
  // "shutting-down" / "terminated" are degenerate (the box is going away) — we
  // treat them like "stopping": asleep → show Wake UI (the tap re-StartInstances
  // path is idempotent server-side).
  if (s === "stopped") return "stopped";
  if (s === "stopping" || s === "shutting-down" || s === "terminated") {
    return "stopping";
  }
  if (s === "running") return "running";
  if (s === "pending") return "pending";
  return "unknown";
}

/**
 * Minimal fetch signature so tests can inject without DOM `fetch`.
 *
 * `json()` is OPTIONAL: the wake POST (`AgentWaker.wake`) reads only `ok` +
 * `status`, so existing injected mocks that return `{ ok, status }` keep
 * working unchanged. The asleep-detection GET (`wakeState`) additionally reads
 * the JSON body (`{ state }`), so its fetch mock supplies `json()`. The DOM
 * `fetch` Response satisfies both shapes structurally.
 */
export type FetchLike = (
  input: string,
  init?: { method?: string; headers?: Record<string, string>; body?: string; signal?: AbortSignal },
) => Promise<{ ok: boolean; status: number; json?: () => Promise<unknown> }>;

/** Injectable clock for deterministic debounce tests. */
export type NowFn = () => number;

/**
 * Default debounce window. A stopped EC2 box takes ~1-2 min to boot the agent;
 * the WS reconnect loop ticks far more often than that (capped 5s backoff), so
 * without a debounce every tick would POST StartInstances. One request per
 * window is plenty — StartInstances is idempotent server-side, but we avoid the
 * churn (and the API-Gateway cost) regardless.
 */
export const WAKE_DEBOUNCE_MS = 20_000;

/**
 * A small stateful waker. Holds the last-attempt timestamp + an in-flight guard
 * so concurrent/rapid calls coalesce. Construct one per app session (App.tsx
 * holds a singleton via a ref); the module-level `wakeAgent` uses a shared
 * default instance for callers that don't need their own (ws.ts).
 */
export class AgentWaker {
  // -Infinity (not 0) so the FIRST-EVER wake always passes the debounce window
  // check (`now - lastAttempt < debounceMs`) regardless of the wall-clock /
  // injected-clock origin. `resetDebounce()` restores this sentinel.
  private lastAttemptMs = Number.NEGATIVE_INFINITY;
  private inFlight = false;
  private readonly fetchFn: FetchLike;
  private readonly now: NowFn;
  private readonly debounceMs: number;

  constructor(opts?: { fetchFn?: FetchLike; now?: NowFn; debounceMs?: number }) {
    this.fetchFn =
      opts?.fetchFn ??
      ((input, init) =>
        // Cast through the DOM fetch; the structural return type matches what
        // we read (`ok`, `status`).
        (globalThis.fetch as unknown as FetchLike)(input, init));
    this.now = opts?.now ?? (() => Date.now());
    this.debounceMs = opts?.debounceMs ?? WAKE_DEBOUNCE_MS;
  }

  /**
   * Ask the wake Lambda to start the agent box. Fire-and-forget + debounced.
   *
   * Returns:
   *   - `disabled`  when no wake endpoint is configured (dev/LAN).
   *   - `debounced` when a wake was attempted within the debounce window OR a
   *     wake is currently in flight (coalesces a burst of reconnect ticks).
   *   - `sent`      when the POST returned 2xx.
   *   - `error`     when the POST threw or returned non-2xx.
   *
   * Never throws — the WS reconnect loop must not be wedged by a wake failure.
   */
  async wake(): Promise<WakeResult> {
    const url = wakeUrl();
    if (url === null) return { status: "disabled" };

    const t = this.now();
    if (this.inFlight || t - this.lastAttemptMs < this.debounceMs) {
      return { status: "debounced" };
    }
    this.lastAttemptMs = t;
    this.inFlight = true;
    try {
      const resp = await this.fetchFn(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      if (resp.ok) return { status: "sent" };
      return { status: "error", error: new Error(`wake POST ${resp.status}`) };
    } catch (error) {
      return { status: "error", error };
    } finally {
      this.inFlight = false;
    }
  }

  /** Reset debounce state so the next `wake()` fires immediately. The wake-up
   *  overlay calls this on an explicit user TAP so a manual "Wake up agent"
   *  press is never silently swallowed by a recent automatic attempt. */
  resetDebounce(): void {
    this.lastAttemptMs = Number.NEGATIVE_INFINITY;
  }

  /**
   * REPORT the agent box's lifecycle state WITHOUT waking it (sleep/wake STAGE 2
   * asleep detection). Issues a GET to the wake endpoint — the wake Lambda's
   * GET branch describes the instance and returns `{ state, started:false, ... }`
   * and NEVER calls StartInstances (the POST branch wakes). So this is safe to
   * call on every WS connect-fail.
   *
   * Returns:
   *   - the normalised {@link WakeState} from the body's `state` field on a 2xx.
   *   - "unknown" when wake is unconfigured (dev/LAN), the GET is non-2xx, the
   *     body is missing/unparseable, or the fetch throws. NEVER throws — the
   *     caller falls back to the plain Connecting/reconnect path on "unknown".
   *
   * NOT debounced (a state probe is read-only + cheap) and NOT subject to the
   * in-flight POST guard — it shares no mutable state with `wake()`.
   */
  async reportState(): Promise<WakeState> {
    const url = wakeUrl();
    if (url === null) return "unknown";
    try {
      const resp = await this.fetchFn(url, {
        method: "GET",
        headers: { accept: "application/json" },
      });
      if (!resp.ok) return "unknown";
      if (typeof resp.json !== "function") return "unknown";
      const body = (await resp.json()) as { state?: unknown } | null;
      return normalizeWakeState(body?.state);
    } catch {
      return "unknown";
    }
  }
}

// Shared default waker for callers (ws.ts reconnect loop) that don't manage
// their own instance. App.tsx constructs its own so the overlay's explicit-tap
// `resetDebounce()` and the reconnect loop share state when wired together.
const defaultWaker = new AgentWaker();

/** Convenience: wake via the shared default `AgentWaker`. */
export function wakeAgent(): Promise<WakeResult> {
  return defaultWaker.wake();
}

/**
 * Convenience: REPORT the box state (GET, report-only — never wakes) via the
 * shared default `AgentWaker`. "unknown" when wake is unconfigured / the probe
 * fails. See {@link AgentWaker.reportState}.
 */
export function wakeState(): Promise<WakeState> {
  return defaultWaker.reportState();
}

/**
 * Outcome of a {@link requestSleep} call (the INVERSE of `wake()` - an explicit
 * user-initiated "Put agent to sleep" from Settings).
 *
 *   - "ok"           - the POST returned 200: the wake Lambda accepted the
 *     StopInstances request; the box is going to sleep. The existing wake state
 *     machine surfaces the wake overlay on its own once the box is asleep.
 *   - "busy"         - 409: the agent is mid-work (an active WS connection / a
 *     running solve) so the box will not stop yet.
 *   - "unauthorized" - 401: no / invalid bearer token; the caller prompts to
 *     sign in.
 *   - "disabled"     - no wake endpoint configured (dev/LAN; the box is never
 *     auto-stopped there) so there is nothing to put to sleep.
 *   - "error"        - the POST threw (network) or returned any other non-2xx.
 */
export type SleepResult =
  | { status: "ok" }
  | { status: "busy" }
  | { status: "unauthorized" }
  | { status: "disabled" }
  | { status: "error"; error: unknown };

/**
 * Ask the wake Lambda to STOP the agent box (explicit user "Put agent to
 * sleep"). POSTs `{"action":"stop"}` to the same wake endpoint base as
 * `wakeAgent()`, authenticated with the caller's Cognito bearer token.
 *
 * Mirrors the `AgentWaker.wake` POST shape (JSON body + content-type) and the
 * `reportState` "never throws, collapse failures to a typed result" contract.
 * The endpoint distinguishes outcomes by HTTP status:
 *   200 -> "ok" (stopping) ; 409 -> "busy" ; 401 -> "unauthorized" ;
 *   anything else / a thrown fetch -> "error". When wake is unconfigured the
 *   call is a no-op returning "disabled".
 *
 * The token is sent ONLY in the Authorization header and is never logged or
 * echoed. `fetchFn` is injectable (DOM `fetch` by default) for unit tests.
 */
export async function requestSleep(
  token: string | null,
  fetchFn?: FetchLike,
): Promise<SleepResult> {
  const url = wakeUrl();
  if (url === null) return { status: "disabled" };

  const doFetch: FetchLike =
    fetchFn ?? ((input, init) => (globalThis.fetch as unknown as FetchLike)(input, init));

  const headers: Record<string, string> = { "content-type": "application/json" };
  if (token != null && token.trim() !== "") {
    headers.authorization = `Bearer ${token.trim()}`;
  }

  try {
    const resp = await doFetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ action: "stop" }),
    });
    if (resp.ok) return { status: "ok" };
    if (resp.status === 409) return { status: "busy" };
    if (resp.status === 401) return { status: "unauthorized" };
    return { status: "error", error: new Error(`sleep POST ${resp.status}`) };
  } catch (error) {
    return { status: "error", error };
  }
}
