// GRACE-2 web — case-view COLD-LOAD client (sleep/wake STAGE 2, NATE 2026-06-18).
//
// "Pen = agent, paper = case." The agent (the pen) can be ASLEEP while the case
// (the paper) must still PAINT. The agent writes each case's full view-state to
// S3; a signer Lambda mints pre-signed GET URLs for it. This module is the WEB
// side of that COLD-LOAD contract:
//
//   1. `caseViewUrl()` derives the signer endpoint. Precedence (mirrors
//      lib/wake.ts wakeUrl):
//          VITE_GRACE2_CASE_VIEW_URL  >  VITE_GRACE2_PUBLIC_BASE(/case-view-url)
//          >  null
//      When NOTHING is configured (`null`) cold-load is DISABLED — dev / LAN
//      builds (where the box is never auto-stopped) behave exactly as before
//      (`fetchCaseView` returns null and the caller falls back to plain
//      Connecting / Wake).
//
//   2. `fetchCaseView(caseId)` does the TWO-HOP fetch:
//          GET  <signer>?case_id=<id>   -> 200 { url, expires_in, mode }
//          GET  <pre-signed S3 url>     -> the CaseOpenEnvelopePayload JSON
//      The S3 JSON is BYTE-IDENTICAL to what the agent emits on the WS
//      `case-open` (envelope_type:"case-open", session_state: CaseSessionState),
//      with inline vector GeoJSON already merged into loaded_layers, so feeding
//      it through useCases.onCaseOpen paints rasters AND vectors with ZERO new
//      render code.
//
//      A MISSING snapshot (the agent never materialised this case to S3 — e.g.
//      a brand-new / never-synced case opened while asleep) -> the signer
//      returns 404; `fetchCaseView` treats that as "no cold snapshot" and
//      returns null (the caller shows the case shell + Wake UI, NOT an error).
//
// The web holds NO AWS credentials — the signer is a least-privilege
// API-Gateway -> Lambda the infra root (infra/aws-autostop) provisions. This
// module performs no work beyond reading `import.meta.env` and issuing two
// `fetch`es; it is pure + unit-testable (the fetch is injectable). It NEVER
// throws — any failure collapses to `null` so a cold-load attempt can never
// wedge the open flow.

import { normalizePublicBase } from "./public_base";
import { CaseOpenEnvelopePayload } from "../contracts";

/** Read `VITE_GRACE2_PUBLIC_BASE` (build-time), normalised. null when unset. */
function publicBase(): string | null {
  const raw =
    (import.meta.env.VITE_GRACE2_PUBLIC_BASE as string | undefined) ?? null;
  return normalizePublicBase(raw);
}

/**
 * Canonical case-view signer endpoint URL, or null when cold-load is not
 * configured.
 *
 * Precedence:
 *   1. `VITE_GRACE2_CASE_VIEW_URL` — an explicit full URL to the API-Gateway
 *      "case-view-url" signer (e.g.
 *      "https://abc123.execute-api.us-west-2.amazonaws.com/case-view-url").
 *      Used verbatim (trailing slashes trimmed). This is the production path:
 *      the autostop API-Gateway is a SEPARATE origin from the CloudFront edge,
 *      so it must be supplied explicitly.
 *   2. `VITE_GRACE2_PUBLIC_BASE` + "/case-view-url" — a convenience for a future
 *      world where the route is folded behind the same edge as the agent.
 *   3. null — nothing configured; cold-load is disabled (dev/LAN, where the box
 *      is never auto-stopped so a case always opens over the live WS).
 */
export function caseViewUrl(): string | null {
  const explicit =
    (import.meta.env.VITE_GRACE2_CASE_VIEW_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.trim().replace(/\/+$/, "");
  }

  const base = publicBase();
  if (base) return `${base}/case-view-url`;

  return null;
}

/** True iff a case-view signer endpoint is configured — the cold-load path
 *  gates on this so dev/LAN never attempts the two-hop fetch (the box can't be
 *  stopped there, so a case always opens over the live WS). */
export function caseViewConfigured(): boolean {
  return caseViewUrl() !== null;
}

/** Minimal fetch signature so tests can inject without DOM `fetch`. Reads
 *  `ok`, `status`, and `json()` (the signer body, then the S3 JSON). */
export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    signal?: AbortSignal;
  },
) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

/** DOUBLE-REFRESH FIX (NATE 2026-06-26): cap a wedged cold-fetch at ~10s. Just
 *  longer than the live WS connect-attempt timeout (ws.ts) so a hung hop fails
 *  fast and the caller can release its guard + re-arm, instead of the request
 *  hanging past the effect-teardown that cancels it. */
export const COLD_FETCH_TIMEOUT_MS = 10_000;

/** Build an AbortController if the runtime has one (browsers + happy-dom do).
 *  Returns null when unavailable so the fetch still runs (unbounded) rather
 *  than throwing - the timeout is best-effort, never a hard dependency. */
export function makeAbortController(): AbortController | null {
  try {
    return typeof AbortController !== "undefined" ? new AbortController() : null;
  } catch {
    return null;
  }
}

/** Signer-response shape: a pre-signed S3 GET URL + metadata. */
interface SignerResponse {
  url?: unknown;
  expires_in?: unknown;
  mode?: unknown;
}

/**
 * COLD-LOAD a case's persisted view-state snapshot from S3 via the signer.
 *
 * Two-hop:
 *   1. GET <caseViewUrl()>?case_id=<caseId>
 *        - 200 { url } -> proceed to hop 2.
 *        - 404         -> NO snapshot (never materialised); resolve null.
 *        - other non-2xx / no url -> resolve null.
 *   2. GET <pre-signed url> -> the CaseOpenEnvelopePayload JSON.
 *        - 2xx with a valid {envelope_type:"case-open"|absent, session_state}
 *          -> resolve the payload.
 *        - anything else -> resolve null.
 *
 * Returns null (NEVER throws) when:
 *   - cold-load is unconfigured (caseViewUrl() === null),
 *   - the signer 404s (no snapshot),
 *   - either hop is non-2xx / unparseable,
 *   - the parsed JSON is not a recognisable case-open envelope.
 *
 * @param caseId  the ULID of the case to cold-load.
 * @param fetchFn injectable fetch (defaults to the DOM `fetch`); both hops use it.
 * @param authToken optional Cognito bearer token forwarded to the SIGNER hop
 *   (the signer accepts an optional Authorization header; the pre-signed S3 GET
 *   carries its own auth in the query string, so hop 2 is unauthenticated).
 */
export async function fetchCaseView(
  caseId: string,
  fetchFn?: FetchLike,
  authToken?: string | null,
): Promise<CaseOpenEnvelopePayload | null> {
  const signer = caseViewUrl();
  if (signer === null) return null;
  if (typeof caseId !== "string" || caseId.trim() === "") return null;

  const doFetch: FetchLike =
    fetchFn ?? ((input, init) => (globalThis.fetch as unknown as FetchLike)(input, init));

  // DOUBLE-REFRESH FIX (NATE 2026-06-26): a WEDGED hop (signer or S3 hanging
  // with the box asleep) must FAIL FAST rather than relying on the caller's
  // effect-teardown to cancel it. Bound BOTH hops with a single ~10s
  // AbortController + timer so a stuck request resolves to null (no cold-load)
  // well before the caller's connect-attempt oscillation tears the effect down.
  // The signal threads into every hop; the timer is always cleared.
  const controller = makeAbortController();
  const timer =
    controller !== null
      ? setTimeout(() => {
          try {
            controller.abort();
          } catch {
            /* ignore */
          }
        }, COLD_FETCH_TIMEOUT_MS)
      : null;
  const signal = controller?.signal;

  try {
    // --- Hop 1: signer -> pre-signed S3 url -------------------------------- //
    const signerUrl = `${signer}?case_id=${encodeURIComponent(caseId.trim())}`;
    const headers: Record<string, string> = { accept: "application/json" };
    if (authToken != null && authToken.trim() !== "") {
      headers.authorization = `Bearer ${authToken.trim()}`;
    }
    const signerResp = await doFetch(signerUrl, { method: "GET", headers, signal });
    // 404 = no snapshot for this case (never materialised) -> caller shows the
    // case shell + Wake; any other non-2xx is also a clean "no cold-load".
    if (!signerResp.ok) return null;

    const signerBody = (await signerResp.json()) as SignerResponse | null;
    const presigned =
      signerBody && typeof signerBody.url === "string" ? signerBody.url : null;
    if (presigned === null || presigned.trim() === "") return null;

    // --- Hop 2: pre-signed S3 GET -> the case-open envelope JSON ----------- //
    // No auth header: the pre-signed url carries its own signature in the query
    // string (and adding headers can invalidate an S3 SigV4 pre-sign).
    const s3Resp = await doFetch(presigned, { method: "GET", signal });
    if (!s3Resp.ok) return null;

    const payload = (await s3Resp.json()) as CaseOpenEnvelopePayload | null;
    return validateCaseOpenPayload(payload);
  } catch {
    // Network / parse / ABORT (timeout) failure on either hop -> no cold-load
    // (fall back to Connecting/Wake). NEVER throw; the open flow must not wedge.
    return null;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/**
 * Validate that a parsed JSON value is a recognisable case-open envelope:
 * an object whose `session_state` is either null or an object carrying a
 * `case.case_id` string (the minimum useCases.onCaseOpen reads). Anything else
 * -> null. We DELIBERATELY do not deep-validate loaded_layers / chat_history
 * here — the rehydration path (App.tsx) already null-guards every field, and a
 * partial-but-shaped snapshot should still paint what it can.
 */
function validateCaseOpenPayload(
  payload: CaseOpenEnvelopePayload | null,
): CaseOpenEnvelopePayload | null {
  if (payload === null || typeof payload !== "object") return null;
  // session_state may legitimately be null (an empty/never-opened case
  // snapshot); the onCaseOpen handler tolerates a null session_state.
  if (!("session_state" in payload)) return null;
  const ss = payload.session_state;
  if (ss === null) return payload;
  if (typeof ss !== "object") return null;
  // A non-null session_state MUST carry a case.case_id for the rail upsert +
  // activeCaseId derivation in useCases.onCaseOpen.
  const caseObj = (ss as { case?: { case_id?: unknown } }).case;
  if (!caseObj || typeof caseObj.case_id !== "string") return null;
  return payload;
}
