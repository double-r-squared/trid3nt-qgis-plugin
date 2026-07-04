// GRACE-2 web - case data-EXPORT client (data export, NATE 2026-06-19).
//
// A user can EXPORT a whole case's data bundle (its rendered layers, packaged
// server-side) as a single downloadable archive. The agent (or a serverless
// Lambda when the box is asleep) packages the bundle to S3 and an
// API-Gateway -> Lambda mints a pre-signed GET URL for it. This module is the
// WEB side of that EXPORT contract - the SIBLING of lib/case_list.ts (the
// SINGLE-GET cold-load) and lib/case_view.ts (the two-hop signer):
//
//   1. `caseExportUrl()` derives the export endpoint. Precedence (mirrors
//      lib/case_list.ts caseListUrl + lib/case_view.ts caseViewUrl):
//          VITE_GRACE2_CASE_EXPORT_URL  >  VITE_GRACE2_PUBLIC_BASE(/case-export-url)
//          >  null
//      When NOTHING is configured (`null`) export is DISABLED - dev / LAN
//      builds behave exactly as before (`requestCaseExport` returns null and the
//      Export button is hidden by `caseExportConfigured()`).
//
//   2. `requestCaseExport(caseId)` does a SINGLE GET:
//          GET  <case-export>?case_id=<id>  -> 200 { url, size_bytes, layer_count }
//      where `url` is a pre-signed S3 GET to the packaged archive. The web then
//      triggers a browser download of that url.
//
// The web holds NO AWS credentials - the endpoint is a least-privilege
// API-Gateway -> Lambda the infra root provisions. This module performs no work
// beyond reading `import.meta.env` and issuing one `fetch`; it is pure +
// unit-testable (the fetch is injectable). It NEVER throws - any failure
// collapses to `null` so an export attempt can never wedge the UI.

import { normalizePublicBase } from "./public_base";

/** Read `VITE_GRACE2_PUBLIC_BASE` (build-time), normalised. null when unset. */
function publicBase(): string | null {
  const raw =
    (import.meta.env.VITE_GRACE2_PUBLIC_BASE as string | undefined) ?? null;
  return normalizePublicBase(raw);
}

/**
 * Canonical case-export endpoint URL, or null when export is not configured.
 *
 * Precedence:
 *   1. `VITE_GRACE2_CASE_EXPORT_URL` - an explicit full URL to the API-Gateway
 *      "case-export-url" endpoint (e.g.
 *      "https://abc123.execute-api.us-west-2.amazonaws.com/case-export-url").
 *      Used verbatim (trailing slashes trimmed). This is the production path:
 *      the export API-Gateway is a SEPARATE origin from the CloudFront edge, so
 *      it must be supplied explicitly.
 *   2. `VITE_GRACE2_PUBLIC_BASE` + "/case-export-url" - a convenience for a
 *      future world where the route is folded behind the same edge as the agent.
 *   3. null - nothing configured; export is disabled (dev/LAN).
 */
export function caseExportUrl(): string | null {
  const explicit =
    (import.meta.env.VITE_GRACE2_CASE_EXPORT_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.trim().replace(/\/+$/, "");
  }

  const base = publicBase();
  if (base) return `${base}/case-export-url`;

  return null;
}

/** True iff a case-export endpoint is configured - the Export button gates on
 *  this so dev/LAN never shows a control that cannot resolve. */
export function caseExportConfigured(): boolean {
  return caseExportUrl() !== null;
}

/** Minimal fetch signature so tests can inject without DOM `fetch`. Reads
 *  `ok`, `status`, and `json()` (the export-response body). */
export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    signal?: AbortSignal;
  },
) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

/** Successful export response: a pre-signed S3 GET URL + bundle metadata. */
export interface CaseExportResult {
  /** Pre-signed S3 GET URL to the packaged archive. */
  url: string;
  /** Total size of the archive in bytes. */
  size_bytes: number;
  /** Number of layers packaged into the archive. */
  layer_count: number;
}

/**
 * REQUEST an export of a case's data bundle from the serverless endpoint.
 *
 * Single GET:
 *   GET <caseExportUrl()>?case_id=<id> -> the CaseExportResult JSON.
 *     - 2xx with a valid { url, size_bytes, layer_count } -> resolve the result.
 *     - anything else -> resolve null.
 *
 * Returns null (NEVER throws) when:
 *   - export is unconfigured (caseExportUrl() === null),
 *   - caseId is empty/blank (no fetch is issued),
 *   - the GET is non-2xx (403 / 5xx / etc.),
 *   - the GET is unparseable,
 *   - the parsed JSON is not a recognisable export result.
 *
 * @param caseId  the ULID of the case to export.
 * @param fetchFn injectable fetch (defaults to the DOM `fetch`).
 * @param authToken optional Cognito bearer token forwarded as an Authorization
 *   header (the endpoint requires a signed-in user; mirrors case_list's bearer).
 */
export async function requestCaseExport(
  caseId: string,
  fetchFn?: FetchLike,
  authToken?: string | null,
): Promise<CaseExportResult | null> {
  const endpoint = caseExportUrl();
  if (endpoint === null) return null;
  if (typeof caseId !== "string" || caseId.trim() === "") return null;

  const doFetch: FetchLike =
    fetchFn ?? ((input, init) => (globalThis.fetch as unknown as FetchLike)(input, init));

  try {
    const url = `${endpoint}?case_id=${encodeURIComponent(caseId.trim())}`;
    const headers: Record<string, string> = { accept: "application/json" };
    if (authToken != null && authToken.trim() !== "") {
      headers.authorization = `Bearer ${authToken.trim()}`;
    }
    const resp = await doFetch(url, { method: "GET", headers });
    if (!resp.ok) return null;

    const payload = (await resp.json()) as unknown;
    return validateExportResult(payload);
  } catch {
    // Network / parse failure -> no export (the button shows an inline error).
    // NEVER throw; the UI must not wedge.
    return null;
  }
}

/**
 * Validate that a parsed JSON value is a recognisable export result: an object
 * carrying a non-empty `url` string, plus numeric `size_bytes` and
 * `layer_count`. Anything else -> null.
 */
function validateExportResult(payload: unknown): CaseExportResult | null {
  if (payload === null || typeof payload !== "object") return null;
  const obj = payload as {
    url?: unknown;
    size_bytes?: unknown;
    layer_count?: unknown;
  };
  if (typeof obj.url !== "string" || obj.url.trim() === "") return null;
  if (typeof obj.size_bytes !== "number" || !Number.isFinite(obj.size_bytes)) {
    return null;
  }
  if (typeof obj.layer_count !== "number" || !Number.isFinite(obj.layer_count)) {
    return null;
  }
  return {
    url: obj.url,
    size_bytes: obj.size_bytes,
    layer_count: obj.layer_count,
  };
}
