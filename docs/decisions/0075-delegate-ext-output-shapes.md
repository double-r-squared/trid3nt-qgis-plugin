# 0075 - Delegate fast-follows + output shapes: fetch_3dep_extra folded, the two named ADR 0074 extensions built

Context: ADR 0074 built the generic library-delegate mode and proved it end-to-end
by folding fetch_statsgo_soils, then STOP-RULED fetch_3dep_extra and the HRRR pair
with NAMED residuals. This wave (ADR 0075) is the fast-follow: build the two named
extensions fetch_3dep_extra required, fold it under the mode, and re-scope the
remaining named residuals (HRRR + the Cluster-B JSON-record fetchers) with sharper,
smaller seams discovered by reading each twin end-to-end.

Decision (2026-08-01):

## Folded: fetch_3dep_extra (proof-by-migration for the two ADR 0074 3DEP residuals)

fetch_3dep_extra folds as a library-delegate raster source (the statsgo precedent):
pfdf `tnm.dem.read(BoundingBox(...crs=4326), resolution=, max_tiles=, timeout=)` ->
a pfdf `Raster` the hook reads directly into `(array, affine, crs)` (nodata masked
to NaN), serialized by the shared COG writer. The two residuals ADR 0074 named are
now DECLARATIVE spec fields, each strictly no-op for the 60 prior specs:

1. `output.auto_publish` (bool, default True): propagated into
   `AtomicToolMetadata.auto_publish` at `register_spec`. The twin's metadata carried
   `auto_publish=False` (a role=input intermediate DEM the server dispatch wrapper
   must NOT auto-render); the spec sets `output.auto_publish: false` and the wrapper
   suppresses the auto-render byte-identically. Every prior spec omits the field ->
   default True == the terminal-product behaviour they already have.

2. `payload_estimate.mb_per_sq_deg_by_param` (per-param coefficient table): the twin
   scales the SAME bbox area by a PER-RESOLUTION MB/deg^2 coefficient
   (5 / 500 / 5000 / 1 / 200 for 1-arc-sec / 1/9-arc-sec / 1 m / 2-arc-sec / 5 m; a
   `default` for any unmapped value) the scalar `mb_per_sq_deg` cannot hold. Wired
   into `synthesize_payload_estimator`'s bbox_area branch: the resolved param value
   keys the table, absent -> `default` -> scalar -> 0.01. Unset -> the scalar
   coefficient (strict no-op for every prior bbox_area spec).

Hooks: `pfdf_3dep.validate` is the twin's exact US envelope (-180, 13, -65, 72) run
pre-cache (3DEP is US-only incl. AK/HI/territories; the live TNM query is the
authoritative coverage check); `pfdf_3dep.read` reproduces the twin's lowercased-
message pfdf exception dispatch verbatim -- zero-coverage (NoTNMProducts) -> typed
EMPTY, tile-count overrun -> typed INPUT (raise max_tiles / shrink bbox), else the
backstop UPSTREAM. `resolution` is a router enum; `max_tiles` a router int-range
[1, 500]; `timeout_s` is the router-owned `ingest.delegate.timeout_s=120.0` (dropped
from the LLM surface + the cache key, the statsgo precedent).

Value parity: same pfdf array; the COG is DEFLATE (router default) vs the twin's LZW
(same array/CRS/nodata -- the ADR 0074 statsgo divergence class). The pfdf `Raster`
is read directly into `(array, affine, crs)` rather than the twin's
save -> rioxarray-reopen -> re-encode (same array; the statsgo array path). No
all-NaN empty gate (the twin had none for 3DEP -- pfdf's NoTNMProducts is the only
empty signal). Consumers `model_urban_flood_swmm` + `model_landslide_scenario`
re-pointed to the registry seam (`TOOL_REGISTRY["fetch_3dep_extra"].fn`). Twin +
its unit test (the 3DEP block of test_pfdf_unlock_statsgo_nldi_3dep.py) DELETED, the
value-bearing coverage migrated to test_router_3dep_extra.py (registration + the
auto_publish opt-out + per-resolution payload + US validate + delegate array->COG +
empty/tile-limit/upstream mapping + LayerURI stamps). Retrieval unshifted: the
docstring is carried verbatim, and fetch_3dep_extra HITs top-8 for its corpus
queries (model-free retrieve_visible_tools), exactly as statsgo does.

Non-gating divergences (3dep): (a) synthesized `layer_id`/`name`
(`3dep_extra-3dep_extra` / "3dep_extra 3dep_extra") vs the twin's
"3dep-extra-<res>-<coords>" / "USGS 3DEP DEM (<res>, <hint>)" -- the ADR 0070 (e)
LayerURI-cosmetics class (the COG DATA + `continuous_dem` preset + role=input +
units="meters" value-identical; consumers read the uri). (b) DEFLATE vs LZW
compression (statsgo class). (c) `timeout_s` dropped from the param surface + cache
key (statsgo class).

## STOP-RULED with SHARPER named residuals

### fetch_hrrr_forecast + fetch_hrrr_smoke (Cluster A item 2)

Reading both twins collapsed the ADR 0074 "three residuals" to ONE new mechanism:
the post-array LCC->4326 reproject (b) and the multi-component `10m_wind_speed`
hypot(u,v) (c) both fold INSIDE the delegate read hook (the hook OWNS the library
socket, so it can open the zarr, reproject, hypot, and clip, returning
`(array, transform, crs)` already in 4326 -- no new router mechanism). The ONLY
genuinely-missing mechanism is (a): a SOCKETED delegate RESOLVE phase. The twin
walks S3 `fs.exists` backward up to 6 h to resolve the published cycle, then merges
`(cycle_date, cycle_hour)` into params BEFORE `read_through` so the resolved cycle
enters the cache key (a `cycle=None` request would otherwise compute a
non-deterministic key). The chained-resolution `resolve_build`/`resolve_parse`
(ADR 0063) and the mrms grib pre-cache resolve (ADR 0069) are the pre-cache-resolve
precedent, but both use the http_json transport; HRRR resolves over a LIBRARY socket
(s3fs). Unblock (small, precise): add `hooks.delegate_resolve` -- a socketed
pre-cache-key resolve constrained by `library_delegate` (declared timeout +
telemetry + upstream backstop, like `delegate`) whose dict return merges into params
in `route()` before `read_through`; then two HRRR delegate hooks (one shared resolve,
one read that does open-zarr + reproject + optional hypot + clip). Two source.yamls
(distinct source_class hrrr / hrrr_smoke; smoke = 2 single-array variables with a
friendly-name 4-tuple, no derived/multi-component). QUEUED (HRRR-Zarr delegate finish
wave). HRRR feeds NO flood seam (grep: only an era5 docstring mention + registration/
categories/normalizer) -> no flood canary implicated.

### fetch_wfigs_incident / fetch_fault_sources / fetch_population / fetch_lehd_jobs (Cluster B)

All four return a bare JSON dict / scalar record (or, for population, a runtime
shape-switch that includes a record leg), NOT a LayerURI. The router's `OutputSpec`
is `layer_type: Literal["raster","vector"]` and `route()` returns a LayerURI; there
is NO record-return path. This is the SAME named gap the ledger already carries for
the fetch_landcover fold ("a DICT-return output contract ... the router emits ONLY a
LayerURI, no dict path"). Unblock (the shared Cluster-B mechanism): a record-return
output shape -- `output.layer_type: "record"` (+ ext json), a router path that
validates params, runs a `hooks.record` (or a delegate) returning a dict, caches the
JSON bytes via `read_through`, and returns the dict envelope with the honesty floor
preserved (typed errors propagate; no silent empty). `route()` becomes
`-> LayerURI | dict`; the promoted closure + server dispatch already tolerate a
dict return (the twins returned dicts). Per-source residuals ON TOP of the shared
record path:
- fetch_wfigs_incident: the cleanest record fetcher, but its resolution is heavily
  bespoke (a token-OR `UPPER(IncidentName) LIKE` builder + a 2-endpoint
  Current->YearToDate best-feature-by-size selection + bbox-from-point +
  epoch_ms->ISO); the fold saves little declaratively -- it is a ~150-line
  `hooks.record`. Consumer: the satellite / frame-animation playbook references it
  (grep + re-point on fold). FOLD once the record shape exists.
- fetch_fault_sources: json-record + empty-is-success dict + a two-tier
  constant/AOI cache + a `resolve_fault_sources` import coupling (test_seismic_real_
  fault_wiring + the OpenQuake seismic workflow import it). Needs the record path
  PLUS a cache-tier knob (constant vs AOI) -- STOP with those two named seams.
- fetch_population: a runtime shape-switch BY dataset param (a raster leg AND a
  record/summary leg) + a direct submodule consumer (`compute_exposure_summary`
  imports it). `endpoint_by_param`/`fan_out` express the endpoint switch but not the
  OUTPUT-shape switch (raster vs record per param). Needs the record path + a
  per-param output-shape switch -- STOP.
- fetch_lehd_jobs: a bulk gzip-CSV LODES download joined on a values-leg (the
  `gzip_object` + join-in-enrich precedents compose toward it) that ALSO emits a
  record summary. Needs the record path + the CSV-join-to-record seam -- STOP.

Consequence: the generic library-delegate mode now covers 3DEP (its two ADR 0074
residuals are declarative spec fields, live-provable and strict-no-op for priors),
and the remaining named residuals are re-scoped to two small, precise mechanisms --
`hooks.delegate_resolve` (a socketed pre-cache resolve) for the HRRR pair, and the
`output.layer_type: "record"` dict-return shape (already ledger-named for
fetch_landcover) for the four Cluster-B JSON-record fetchers. Extends the tier-3 hook
contract (ADR 0056/0061/0063/0071/0073/0074) with `output.auto_publish` +
`payload_estimate.mb_per_sq_deg_by_param`. Supersedes the ADR 0074 fetch_3dep_extra
STOP-RULE.
