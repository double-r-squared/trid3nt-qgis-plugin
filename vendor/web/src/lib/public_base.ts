// GRACE-2 web — public-origin URL derivation (sprint-14-aws CloudFront/HTTPS).
//
// One canonical seam for deriving the agent WebSocket URL and the HTTP base
// (tool-catalog, telemetry, and — once the agent's GRACE2_TILE_SERVER_BASE is
// pointed at the same edge — tiles) the browser should dial.
//
// THE SEAM: `VITE_GRACE2_PUBLIC_BASE`.
//   When set to a single public origin (e.g. a CloudFront distribution domain
//   "https://d123.cloudfront.net" or "d123.cloudfront.net"), the whole app
//   collapses onto ONE https/wss origin with no per-service ports:
//       agent WS   -> wss://<domain>/ws
//       http base  -> https://<domain>      (catalog appends /api/tool-catalog)
//   This eliminates mixed-content blocking once the page itself is served over
//   https through the same edge.
//
//   When UNSET (today's default), every derivation is BYTE-IDENTICAL to the
//   pre-existing inline logic:
//       agent WS   -> ws://<window.hostname>:8765   (or ws://localhost:8765 SSR)
//       http base  -> <window.protocol>//<window.hostname>:8766  (or
//                     http://localhost:8766 SSR)
//   Nothing changes at runtime until the env var is supplied at build time and
//   the page is served over https.
//
// Precedence (most specific wins) so existing per-surface overrides still work:
//   WS  : VITE_GRACE2_WS_URL  >  VITE_GRACE2_PUBLIC_BASE(/ws)  >  hostname:8765
//   HTTP: VITE_GRACE2_HTTP_URL > VITE_GRACE2_PUBLIC_BASE       >  proto//host:8766
//
// This module performs NO network I/O and reads no globals beyond
// `import.meta.env` and `window.location`; it is pure + unit-testable.

import { isLocalDeployment } from "./deployment";

/** Normalise a public-base value into an origin string with NO trailing slash.
 *  Accepts bare domains ("d.cloudfront.net") and full origins
 *  ("https://d.cloudfront.net/"). A bare domain is assumed https (the seam's
 *  whole purpose is the https/wss cutover). Returns null for empty/whitespace. */
export function normalizePublicBase(raw: string | null | undefined): string | null {
  if (raw == null) return null;
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(trimmed)
    ? trimmed
    : `https://${trimmed}`;
  return withScheme.replace(/\/+$/, "");
}

/** Read VITE_GRACE2_PUBLIC_BASE (build-time), normalised. null when unset. */
function publicBase(): string | null {
  const raw =
    (import.meta.env.VITE_GRACE2_PUBLIC_BASE as string | undefined) ?? null;
  return normalizePublicBase(raw);
}

/** Map an http(s) origin to its ws(s) equivalent ("https://x" -> "wss://x"). */
function toWsOrigin(httpOrigin: string): string {
  if (httpOrigin.startsWith("https://")) return "wss://" + httpOrigin.slice("https://".length);
  if (httpOrigin.startsWith("http://")) return "ws://" + httpOrigin.slice("http://".length);
  // Already a ws(s) origin or scheme-less — return as-is.
  return httpOrigin;
}

/**
 * Canonical agent-WebSocket URL.
 *
 * Precedence: explicit VITE_GRACE2_WS_URL override → public-base (wss://<base>/ws)
 * → today's hostname-derived ws://<host>:8765 default. SSR-safe fallback is
 * ws://localhost:8765, matching App.tsx's pre-existing logic exactly.
 */
export function defaultWsUrl(): string {
  const explicit = (import.meta.env.VITE_GRACE2_WS_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") return explicit;

  const base = publicBase();
  if (base) {
    // Single-origin edge: WS upgrade lands on the path-agnostic agent handler
    // routed by the /ws* CloudFront behavior. Scheme follows the base
    // (https -> wss), so an https page never trips mixed-content.
    return `${toWsOrigin(base)}/ws`;
  }

  if (typeof window !== "undefined" && window.location?.hostname) {
    return `ws://${window.location.hostname}:8765`;
  }
  return "ws://localhost:8765";
}

/**
 * Canonical HTTP base origin (NO trailing slash, NO path) for the agent's HTTP
 * listener — callers append their own path (e.g. "/api/tool-catalog").
 *
 * Precedence: explicit VITE_GRACE2_HTTP_URL override → public-base (https://<base>)
 * → today's <proto>//<host>:8766 default. SSR fallback http://localhost:8766
 * matches the pre-existing inline logic in ToolsCatalogPopup / RoutingQualityDashboard.
 */
export function httpBase(): string {
  const explicit = (import.meta.env.VITE_GRACE2_HTTP_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.replace(/\/+$/, "");
  }

  const base = publicBase();
  if (base) return base;

  if (typeof window !== "undefined" && window.location?.hostname) {
    const { protocol, hostname } = window.location;
    return `${protocol}//${hostname}:8766`;
  }
  return "http://localhost:8766";
}

/** Convenience: full tool-catalog endpoint URL off the canonical HTTP base. */
export function catalogUrl(): string {
  return httpBase() + "/api/tool-catalog";
}

/**
 * COLD (agent-less) tool-catalog URL -- a durable STATIC snapshot of the catalog
 * published to the public web S3 bucket at deploy time (see
 * scripts/deploy_agent_bundle.sh). NATE 2026-06-27: "I shouldn't have to start
 * an agent to see tools." This object is served 24/7 with no running agent and,
 * crucially, fetching it does NOT wake the auto-stopped agent box (unlike the
 * live /api/tool-catalog origin on :8766). It is the PRIMARY catalog source for
 * the read-only popup; `catalogUrl()` (live agent) is only the fallback.
 *
 * The object is public-read (web bucket already carries a PublicReadGetObject
 * policy) and CORS-enabled for the Vercel origins, so the browser GETs it
 * directly from the S3 REST endpoint cross-origin. Override at build time with
 * VITE_GRACE2_COLD_CATALOG_URL; default is the live us-west-2 web bucket key.
 *
 * LOCAL build (VITE_DEPLOYMENT=local; fingerprint audit L1): the local agent is
 * always-on (no sleep/wake), so there is no "cold" source distinct from the
 * live one - and the cloud S3 default would silently GET a live AWS object
 * from the local product. In local mode the cold URL therefore collapses onto
 * the live agent catalog endpoint (catalogUrl()). An explicit
 * VITE_GRACE2_COLD_CATALOG_URL still wins in BOTH modes (e.g. a
 * MinIO-published snapshot).
 */
export function coldCatalogUrl(): string {
  const explicit =
    (import.meta.env.VITE_GRACE2_COLD_CATALOG_URL as string | undefined) ?? null;
  if (explicit != null && explicit.trim() !== "") {
    return explicit.trim();
  }
  if (isLocalDeployment()) {
    return catalogUrl();
  }
  return "https://grace2-hazard-web-226996537797.s3.us-west-2.amazonaws.com/catalog/tool-catalog.json";
}

/**
 * Tile base the agent SHOULD bake into TiTiler templates once the edge is live.
 *
 * The web does NOT build tile URLs itself (publish_layer.py emits the full
 * /cog/tiles template using GRACE2_TILE_SERVER_BASE). This helper exists so the
 * web and the agent agree on the same value: when VITE_GRACE2_PUBLIC_BASE is
 * set the tile base is https://<domain>; when unset it is null (today's
 * agent-side default — the legacy http://<ip>:8080 — stands untouched).
 *
 * Returned for diagnostics / a future render-time normalisation of persisted
 * http tile templates; not wired into Map.tsx here (Map.tsx is out of track).
 */
export function publicTileBase(): string | null {
  return publicBase();
}
