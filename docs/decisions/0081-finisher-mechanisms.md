# 0081 - Finisher mechanisms: fetch_fault_sources folded (constant-cache two-tier + emptiness output-switch)

Context: ADR 0077 STOP-RULEd three per-source residuals as wave-sized new-router-
mechanism builds, each precisely named by a twin read: fetch_fault_sources (an
emptiness-driven output switch + a two-tier cache + a result-model migration),
fetch_landcover (a WCS GetCoverage access mode + a dict sidecar + palette COG +
auto-coarsen, flood-canary-gated), and fetch_flood_extent_observation (a
categorical tiled-mosaic raster mode + a LANCE dir-walk resolve). This wave builds
the two mechanisms fetch_fault_sources named, FOLDS it (keyless + live-200 GEM GAF,
so live-provable), and re-confirms the other two STOP-RULES.

Decision (2026-08-01):

## BUILT: two minimal, opt-in-no-op router mechanisms

### 1. `ingest.constant_cache` -- the two-tier cache (http_json executor)

A source whose bespoke body is "download ONE whole-world file once, then filter it
per-AOI in the parse hook" (the GEM GAF harmonized GeoJSON, ~10.6 MB, 13696 faults)
declares `ingest.constant_cache: {file_id, ext}`. The `http_json` executor then
fetches each `build_request` plan through an INNER `read_through` keyed on a
CONSTANT (`{"file": file_id}`, independent of the AOI), so distinct AOIs share one
cached download; the OUTER AOI-keyed `read_through` in `route()` still caches the
small AOI-filtered FGB per AOI. The naive-fold per-AOI re-download regression ADR
0077 named is IMPOSSIBLE: 3 distinct AOIs -> 1 GEM download (proven). STRICT no-op
for every prior http_json spec (none declare `constant_cache`).

### 2. `output.variant_by_emptiness` -- the emptiness output-switch (router)

A source whose non-empty path is a renderable `LayerURI` (subclass) but whose
empty-AOI degrade is a bare record dict + typed note (fetch_fault_sources' honesty
gate: a zero-fault AOI is NEVER given a layer) declares
`output.variant_by_emptiness: <hook>`. After the vector FGB is produced, `route()`
reads its feature count; when zero it returns the hook's `(spec, params) -> dict`
record INSTEAD of the LayerURI (an unreadable FGB counts as non-empty, so a read
hiccup never swallows a real fetch into the degrade). The honesty floor is intact:
the switch only chooses between two already-honest shapes -- it never flips an error
to success. Validated as a resolvable hook at load. STRICT no-op for every prior
spec (none declare it -> the LayerURI is always returned).

`FaultSourcesResult` (the fault-trace `LayerURI` subclass carrying the kinematic
records + categorical legend) moved into `contracts/execution.py`
`LAYER_RESULT_MODELS` (the `HighWaterMarksLayerURI` precedent, ADR 0073), so the
spec-driven surface has no coded twin.

## FOLDED: fetch_fault_sources (spec + fault_sources hooks on the two mechanisms)

The GEM active-faults twin folded to a `source.yaml` (vector-fgb, error_prefix
FAULT_SOURCES) + `fault_sources` hooks: `build_request` (ONE GET of the constant
whole-world file), `parse_response` (verbatim-from-the-twin '(best,min,max)' triple
parse + the >=2-distinct-vertex + slip>0 gate + GEM depth/dip/rake defaults + the
bbox filter -> one LineString feature per fault; a zero-fault AOI -> `[]`), the
post-emit `envelope` (kinematic-record reconstruction from the produced FGB + name +
categorical legend -> FaultSourcesResult), and `empty_record` (the
variant_by_emptiness zero-fault degrade dict). `catalog` folds to an enum param
(lowercase, values [gem]); the payload estimate reproduces the twin's constant
0.2 MB via `bbox_area` floor=ceil=0.2.

CONSUMER re-point: `resolve_fault_sources` (openquake/model_seismic_hazard_scenario)
imported the twin symbol + caught `FaultSourcesError`; re-pointed to the registry
seam (`TOOL_REGISTRY["fetch_fault_sources"].fn`) catching the router's shared
`FetchError` base (byte-identical FAULT_SOURCES_* A.6 codes). It already tolerated
dict-or-object off `.faults`/`.note`, so no read-side change. test_seismic_real_
fault_wiring.py (the consumer test file) migrated to the registry-seam swap
(dataclasses.replace, the ADR 0080 canopy precedent) -- 10/10 green.

## Evidence

- **TWIN-vs-ROUTER value parity PASS** (SPEC-IDENTITY gate, twin restored from git
  HEAD, same fixture + in-memory cache): non-empty SF path VALUE-IDENTICAL across
  catalog / fault_count / style_preset (fault_line) / role (context) / units (None) /
  bbox / name ("Active fault traces (2)") / legend (categorical #FF6A00) / the
  kinematic fault RECORDS (name/geometry/slip/dip/rake/depths/slip_type/catalog_name)
  / source; the empty OCEAN path VALUE-IDENTICAL dict (catalog/fault_count/faults/
  note/bbox/source).
- **Two-tier cache PROVEN**: 3 distinct AOIs -> 1 GEM file download + per-AOI FGB
  entries (the naive-fold regression is impossible).
- Value coverage -> `test_router_fault_sources.py` (9 tests). Twin py +
  `test_fetch_fault_sources.py` DELETED.
- Offline suite FAILED set unchanged from the 9-failure baseline (both mechanisms
  strict no-op for the priors). Retrieval UNSHIFTED (6/6 corpus phrasings top-8,
  model-free). Docstring carried VERBATIM (3,001 chars); corpus untouched.
- LIVE (keyless GEM GAF, HTTP 200): the fold is live-reachable; the offline parity +
  the constant-cache proof carry the value gate.

## Non-gating divergences

- **layer_id**: the router synthesizes `gem_active_faults-gem` vs the twin's
  `fault-sources-<lon>-<lat>` (the standing layer-id-cosmetics fold divergence;
  style_preset / role / bbox / legend / name are reproduced exactly).
- **catalog_name**: reproduced always-present (None when absent) on the kinematic
  record, vs the twin's drawable-FeatureCollection which omitted a falsy
  catalog_name -- the RECORD shape (which the worker + consumer read) is unchanged;
  more faithful, not less.

## Metrics

Coded fetchers -1 (fault_sources); coded tools -1. Spec-served sources 70 -> 71 (+1).
Registry name-count UNCHANGED (one coded twin died, one spec surface took its name).
test_catalog_surfacing: n_specs 70 -> 71, declarable delta -69 -> -70, index 69 -> 70
(expected promote metrics). Offline baseline UNCHANGED (FAILED set == the 9
pre-existing).

Consequence: the http_json family gains a `constant_cache` two-tier cache and the
router gains a `variant_by_emptiness` emptiness output-switch, both opt-in-no-op;
fetch_fault_sources folds with no coded twin. Supersedes the ADR 0076/0077
fetch_fault_sources STOP-RULE. The other two ADR 0077 finishers remain STOP-RULED:
fetch_landcover (WCS GetCoverage + palette COG + the SFINCS sidecar re-point +
the mandatory flood canary -- its own wave; the SFINCS seam is UNTOUCHED here, so NO
canary is owed) and fetch_flood_extent_observation (categorical first-valid tiled-
mosaic mode + LANCE dir-walk resolve -- its own wave; V&V consumer couples by
docstring, not import, so non-fold breaks nothing).
