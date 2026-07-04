// GRACE-2 web - cases-list COLD-LOAD client (sleep/wake STAGE 2, NATE 2026-06-19).
//
// "Pen = agent, paper = case." The agent (the pen) can be ASLEEP while the
// Cases ROOT (the list of paper) must still RENDER. The agent writes the user's
// case-summary list to a serverless store; an API-Gateway -> Lambda serves it
// directly. This module is the WEB side of that COLD-LOAD contract - the SIBLING
// of lib/case_view.ts (which cold-loads ONE case's full view-state). Where
// case_view does a two-hop signer -> pre-signed S3 fetch, the cases LIST is
// small and returns INLINE, so this is a SINGLE GET.
//
//   1. `caseListUrl()` derives the list endpoint. Precedence (mirrors
//      lib/case_view.ts caseViewUrl + lib/wake.ts wakeUrl):
//          VITE_GRACE2_CASE_LIST_URL  >  VITE_GRACE2_PUBLIC_BASE(/case-list)
//          >  null
//      When NOTHING is configured (`null`) cold-load of the list is DISABLED -
//      dev / LAN builds (where the box is never auto-stopped) behave exactly as
//      before (`fetchCaseList` returns null and the caller falls back to the
//      plain Connecting / Wake path that reads the list over the live WS).
//
//   2. `fetchCaseList()` does a SINGLE GET:
//          GET  <case-list>   -> 200 { envelope_type:"case-list", cases:[...] }
//      The JSON is BYTE-IDENTICAL to what the agent emits on the WS `case-list`
//      (envelope_type:"case-list", cases: CaseSummary[]), so feeding it through
//      the existing case-list dispatch paints the Cases rail with ZERO new code.
//
// The web holds NO AWS credentials - the endpoint is a least-privilege
// API-Gateway -> Lambda the infra root (infra/aws-autostop) provisions. This
// module performs no work beyond reading `import.meta.env` and issuing one
// `fetch`; it is pure + unit-testable (the fetch is injectable). It NEVER
// throws - any failure collapses to `null` so a cold list-load attempt can
// never wedge the open flow.

import { normalizePublicBase } from "./public_base";
import { CaseListEnvelopePayload } from "../contracts";
// DOUBLE-REFRESH FIX (NATE 2026-06-26): reuse the same wedged-fetch bound the
// case-VIEW cold-load uses, so a hung /case-list also fails fast.
import { COLD_FETCH_TIMEOUT_MS, makeAbortController } from "./case_view";

/** Read `VITE_GRACE2_PUBLIC_BASE` (build-time), normalised. null when unset. */
function publicBase(): string | null {
  const raw =
    (import.meta.env.VITE_GRACE2_PUBLIC_BASE as string | undefined) ?? null;
  return normalizePublicBase(raw);
}

/**
 * Canonical case-list endpoint URL, or null when cold-load is not configured.
 *
 * Precedence:
 *   1. `VITE_GRACE2_CASE_LIST_URL` - an explicit full URL to the API-Gateway
 *      "case-list" endpoint (e.g.
 *      "https://abc123.execute-api.us-west-2.amazonaws.com/case-list").
 *      Used verbatim (trailing slashes trimmed). This is the production path:
 *      the autostop API-Gateway is a SEPARATE origin from the CloudFront edge,
 *      so it must be supplied explicitly.
 *   2. `VITE_GRACE2_PUBLIC_BASE` + "/case-list" - a convenience for a future
 *      world where the route is folded behind the same edge as the agent.
 *   3. null - nothing configured; cold-load is disabled (dev/LAN, where the box
 *      is never auto-stopped so the list always arrives over the live WS).
 */
export function caseListUrl(): string | null {
  const explicit =
    (import.meta.env.VITE_GRACE2_CASE_LIST_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.trim().replace(/\/+$/, "");
  }

  const base = publicBase();
  if (base) return `${base}/case-list`;

  return null;
}

/** True iff a case-list endpoint is configured - the cold-load path gates on
 *  this so dev/LAN never attempts the fetch (the box can't be stopped there, so
 *  the list always arrives over the live WS). */
export function caseListConfigured(): boolean {
  return caseListUrl() !== null;
}

/** Minimal fetch signature so tests can inject without DOM `fetch`. Reads
 *  `ok`, `status`, and `json()` (the case-list body). */
export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    signal?: AbortSignal;
  },
) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

/**
 * COLD-LOAD the user's cases list from the serverless endpoint.
 *
 * Single GET:
 *   GET <caseListUrl()> -> the CaseListEnvelopePayload JSON.
 *     - 2xx with a valid {envelope_type:"case-list"|absent, cases:[...]}
 *       -> resolve the payload.
 *     - anything else -> resolve null.
 *
 * Returns null (NEVER throws) when:
 *   - cold-load is unconfigured (caseListUrl() === null),
 *   - the GET is non-2xx / unparseable,
 *   - the parsed JSON is not a recognisable case-list envelope.
 *
 * @param fetchFn injectable fetch (defaults to the DOM `fetch`).
 * @param authToken optional Cognito bearer token forwarded as an Authorization
 *   header (the endpoint accepts an optional Authorization header; mirrors
 *   case_view's signer hop).
 */
export async function fetchCaseList(
  fetchFn?: FetchLike,
  authToken?: string | null,
): Promise<CaseListEnvelopePayload | null> {
  const endpoint = caseListUrl();
  if (endpoint === null) return null;

  const doFetch: FetchLike =
    fetchFn ?? ((input, init) => (globalThis.fetch as unknown as FetchLike)(input, init));

  // DOUBLE-REFRESH FIX (NATE 2026-06-26): bound a wedged /case-list GET with a
  // ~10s AbortController + timer so a stuck endpoint resolves to null fast
  // instead of relying on the caller's effect-teardown to cancel it.
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
    const headers: Record<string, string> = { accept: "application/json" };
    if (authToken != null && authToken.trim() !== "") {
      headers.authorization = `Bearer ${authToken.trim()}`;
    }
    const resp = await doFetch(endpoint, { method: "GET", headers, signal });
    if (!resp.ok) return null;

    const payload = (await resp.json()) as CaseListEnvelopePayload | null;
    return validateCaseListPayload(payload);
  } catch {
    // Network / parse / ABORT (timeout) failure -> no cold-load (fall back to
    // Connecting/Wake). NEVER throw; the open flow must not wedge.
    return null;
  } finally {
    if (timer !== null) clearTimeout(timer);
  }
}

/**
 * Validate that a parsed JSON value is a recognisable case-list envelope: an
 * object whose `cases` is an array. Anything else -> null. We DELIBERATELY do
 * not deep-validate each CaseSummary here - the rail-upsert path already
 * null-guards every field, and a partial-but-shaped list should still render
 * what it can.
 */
function validateCaseListPayload(
  payload: CaseListEnvelopePayload | null,
): CaseListEnvelopePayload | null {
  if (payload === null || typeof payload !== "object") return null;
  const cases = (payload as { cases?: unknown }).cases;
  if (!Array.isArray(cases)) return null;
  return payload;
}
