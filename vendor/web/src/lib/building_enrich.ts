// GRACE-2 web - building footprint CLICK-TO-ENRICH client (NATE 2026-06-27).
//
// Footprint layers store too much in the FRONTEND GeoJSON, which makes the map
// slow. The fix: the agent now emits ID-ONLY props per footprint (osm_id /
// osm_type / a composite fid) and DROPS the full tag bag (building / height /
// levels / name / addr:*) from the inline GeoJSON. The popup renders the slim
// feature IMMEDIATELY, then ASYNC-enriches by (osm_type, osm_id) via this
// module, merging the returned tags into the popup card on resolve.
//
// THE SEAM: `GET <httpBase()>/api/building-detail?osm_type=<>&osm_id=<>` ->
//   { fid, tags: { ... } }. Backed by the agent's hand-rolled HTTP listener
//   (tool_catalog_http.py): it reads the cached per-AOI tag sidecar, falling
//   back to a live Overpass-by-id query, so it works COLD (no running solve).
//
// This module performs ONE fetch and NEVER throws - any failure (network,
// timeout, non-200, malformed body) collapses to `null` so a failed enrich can
// never wedge the popup; the card simply keeps showing the id-only props. It is
// pure + unit-testable: the fetch is injectable and the timeout is best-effort.

import { httpBase } from "./public_base";
import { COLD_FETCH_TIMEOUT_MS, makeAbortController } from "./case_view";

/** The enrich result: a flat tag bag (string-keyed OSM tags) or null on miss. */
export type BuildingTags = Record<string, unknown>;

/**
 * Build the building-detail endpoint URL for a clicked footprint. Pure +
 * exported so the URL derivation is unit-testable without a fetch. The
 * identifiers are URL-encoded so a stray value can never break the query.
 */
export function buildingDetailUrl(osmType: string, osmId: string | number): string {
  const t = encodeURIComponent(String(osmType));
  const i = encodeURIComponent(String(osmId));
  return `${httpBase()}/api/building-detail?osm_type=${t}&osm_id=${i}`;
}

/**
 * Fetch the full tag bag for a clicked building footprint by (osm_type, osm_id).
 *
 * Returns the `tags` object on success, or `null` on ANY failure (missing ids,
 * network error, timeout, non-200, malformed body). NEVER throws - the caller
 * renders the id-only popup first and only MERGES tags if this resolves
 * non-null. The fetch is bounded by `COLD_FETCH_TIMEOUT_MS` (best-effort abort).
 *
 * `fetchImpl` is injectable for tests; it defaults to the global `fetch`.
 */
export async function fetchBuildingDetail(
  osmType: string | null | undefined,
  osmId: string | number | null | undefined,
  fetchImpl: typeof fetch | undefined = typeof fetch !== "undefined" ? fetch : undefined,
): Promise<BuildingTags | null> {
  // Guard: both identifiers are required to form a valid by-id query.
  if (osmType == null || osmId == null || String(osmType) === "" || String(osmId) === "") {
    return null;
  }
  if (!fetchImpl) return null;

  const url = buildingDetailUrl(String(osmType), osmId);
  const controller = makeAbortController();
  let timer: ReturnType<typeof setTimeout> | null = null;
  if (controller) {
    timer = setTimeout(() => {
      try {
        controller.abort();
      } catch {
        /* abort is best-effort */
      }
    }, COLD_FETCH_TIMEOUT_MS);
  }
  try {
    const resp = await fetchImpl(url, {
      method: "GET",
      signal: controller?.signal,
    });
    if (!resp || !resp.ok) return null;
    const body = (await resp.json()) as { tags?: unknown } | null;
    if (!body || typeof body !== "object") return null;
    const tags = (body as { tags?: unknown }).tags;
    if (!tags || typeof tags !== "object" || Array.isArray(tags)) return null;
    return tags as BuildingTags;
  } catch {
    // Network error / abort / non-JSON body - degrade to "no enrichment".
    return null;
  } finally {
    if (timer != null) clearTimeout(timer);
  }
}
