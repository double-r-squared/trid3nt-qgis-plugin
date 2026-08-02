# ADR 0084 -- Trigger wave: lehd_jobs (join VALUES-hook) + buildings (sidecar-write) folds; nwis STOP

Status: accepted (2026-08-02)
Supersedes: the ADR 0083 fetch_lehd_jobs "CHARACTERIZED FOLD-READY / DEFERRED" row; the
ADR 0083 fetch_buildings "sidecar-write" STOP. Sharpens the ADR 0083 fetch_usgs_nwis_gauges
STOP (still QUEUED, untouched -> no canary owed).

## Context

The merge-trigger wave: the two tractable folds outside the animation family that each
needed exactly ONE new (strictly no-op) router mechanism, plus the flood-canary-gated nwis
twin left untouched. Both mechanisms are declared entirely in the source spec + pure hooks;
the router core is extended by two guarded branches that are a strict no-op for every prior
spec.

## Decisions

### 1. fetch_lehd_jobs -- FOLDED (join VALUES-hook seam)

The TIGERweb tract geometry LEFT-JOIN a per-tract value on 11-digit GEOID is the
`transforms/join` shape (the census_acs precedent), but the values leg is a per-STATE LODES
WAC bulk gzip-CSV whole-object download aggregated block -> tract, NOT the census Data-API
the built-in `join.fetch_values` speaks. The seam: `join.values.values_hook = {plan, parse}`
naming two PURE hooks; the join transform owns the I/O (GETs the plans over the shared
transport) and the cache -- the storm_events gzip-CSV precedent, kept pure.

- `lehd_jobs.values_plan` (pure): the in-scope state FIPS set -> the ordered
  `(fips, RequestPlan)` LODES WAC GETs (FIPS -> 2-letter-abbr table + WAC URL template).
- `lehd_jobs.values_parse` (pure): the per-state gzip bodies -> `{tract11: {"value": sum}}`,
  block rows summed over the segment's WAC columns (`var_spec.cols`).

Three tiny join-transform config keys carry the twin's exact surface, all defaulting to
census's behaviour (strict no-op for the 6 join priors): `variable_param` (the param name;
lehd "segment"), `value_field` (the label property; lehd "segment"), `extra_props`
(param-echo columns; lehd `year`), and `allow_raw_code=false` (closed segment vocabulary,
no ACS-code passthrough).

LIVE PROOF (twin `_fetch_lehd_bytes` vs router `join.execute`, small Harris County TX AOI,
direct-compute both paths): 58 tracts, GEOID sets identical, ZERO per-tract job-value
mismatches (total + low_wage segments; total jobs 355328 identical), geometry area
value-identical; ocean-AOI -> both header-only 8-column empty FGB (schema-identical);
unknown segment -> LEHD_JOBS_INPUT_INVALID. Only consumer was the tools/__init__ import
(spec-driven now). Value coverage: test_router_lehd_jobs.py (25). Retrieval unshifted (8/8
top-8). Docstring verbatim (2809 chars). Divergences (non-gating): FGB column ORDER (same
SET); synthesized layer_id/name.

### 2. fetch_buildings -- FOLDED (constrained sidecar-WRITE executor extension)

Overpass-primary polygon source (folds onto the overpass mode) whose blocker was the ONE
sanctioned side write: a `.tags.json` object keyed off the SAME cache key as the `.fgb`
(the full OSM tag bag per footprint), read back cross-module by `/api/building-detail` so
the inline FGB stays slim. The `overpass_sidecar` executor is the minimal constrained write
extension -- constrained like the library_delegate:

- `ingest.sidecar_write = {ext: tags.json, parse: buildings.parse}` -> the executor fetches
  (endpoint_fallback), runs the `(features, tags)` parse, serializes the slim FGB, and writes
  ONE declared sidecar SIBLING of the `.fgb` (recomputes the exact `read_through` key:
  same metadata + params + ttl vintage -- the twin's `buildings_cache_uri` contract).
- BEST-EFFORT + telemetry-marked: a sidecar fault NEVER fails the fetch; the honesty floor
  is untouched (`read_through` still owns the `.fgb`). Empty AOI -> BUILDINGS_EMPTY.
- The dead msft/abfs GeoParquet leg stays flag-not-copy (Overpass is the reliable source);
  `source` is echoed only into the cache key. Two pure hooks: `buildings.build_request`
  (the QL) + `buildings.parse` (ways->Polygon, relations->(Multi)Polygon, intersects-not-
  clip, slim props + tag capture).

Consumers re-pointed to `TOOL_REGISTRY["fetch_buildings"].fn` (compute_exposure_summary,
sfincs_forcing_autowire building-obstacles, model_urban_flood_swmm) + the `/api/building-detail`
HTTP consumer re-pointed to derive the sidecar identity from the promoted spec
(`registration.get_spec`, literal fallback).

LIVE PROOF (twin `_fetch_osm_buildings_bytes` vs router build+fetch+parse, small Houston
AOI, one Overpass request each): slim FGB schema identical (osm_id/osm_type/fid); per-fid
tag bags IDENTICAL for all 299 common footprints; geometry areas match; sidecar URI is the
exact `.tags.json` sibling of the `.fgb` key. (A raw-vs-quantized-bbox harness artifact
accounts for the edge-footprint count delta 299 vs 309; on the same quantized bbox the fid
sets match.) Value coverage: test_router_buildings.py (12). Consumer tests green:
test_building_detail_http_route, test_inland_building_obstacles,
test_model_flood_scenario_surge_plumbing, test_data_fetch, test_compute_exposure_summary,
test_run_swmm_local_chain (192 + 23). Retrieval unshifted (10/10 top-8). Docstring verbatim
(2469 chars). Divergences (non-gating): synthesized layer_id/name; empty-AOI stamps
BUILDINGS_EMPTY (non-retryable) vs the twin's UPSTREAM_API_ERROR (retryable); a payload
estimate is synthesized (the twin had none -- the SourceSpec requires one), a bbox_area
30 MB/deg^2 heuristic that never false-warns on a county AOI.

### 3. fetch_usgs_nwis_gauges -- STOP (untouched; flood-canary-gated)

Left ENTIRELY untouched (sfincs_forcing_autowire still resolves the twin directly at line
1051/1054 in HYDROGRAPH mode) -> NO flood-consumer seam re-pointed -> NO flood canary owed
this wave. The two blockers hold: (a) the FGB PROPERTY SCHEMA switches at runtime by
window-presence (5-field instantaneous vs 12-field hydrograph -- an output-shape-switch-by-
param the router does not express); (b) the two-tier cross-parser fallback (IV WaterML-JSON
empty -> Site RDB). UNBLOCK: a derived-output-shape selector + a parse-fallback chain + the
flood-leg re-point + the MANDATORY flood canary.

## Consequences

- Coded fetchers (NATE tally): 22 -> 20 (fetch_lehd_jobs, fetch_buildings deleted). At <= 20
  the merge trigger is crossed. n_specs 76 -> 78; registry 190 unchanged (folds are
  name-preserving). (Mechanical `@register_tool fetch_*.py` twin count is a different, lower
  denominator -- flagged for the orchestrator to reconcile against NATE's basis.)
- Two new router branches, both strict no-op for every prior spec: the `join.values_hook`
  dispatch (+ 3 join config keys defaulting to census) and the `ingest.sidecar_write`
  executor selection. Two new hook modules (lehd_jobs, buildings) + one new executor
  (overpass_sidecar).
- The sidecar-write extension is the SECOND sanctioned router impurity after the
  library_delegate: ONE declared ext, telemetry-marked, best-effort, honesty floor untouched.
- Offline baseline UNCHANGED: exactly 9 failures (test_fetch_resolution_gate x4 +
  test_run_river_dye_scenario x5) from the repo root. test_catalog_surfacing spec-served
  counts updated (n_specs 78; declarable-pool delta 77; index tool_names 77).
