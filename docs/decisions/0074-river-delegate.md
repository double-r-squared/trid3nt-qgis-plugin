# 0074 - River Overpass fold (NHDPlus leg dropped) + the generic library-delegate mode

Context: two named-gap items land together. (1) fetch_river_geometry was STOP-RULED
in ADR 0070: its PRIMARY leg (an OSM Overpass waterway QL) is foldable via the
Overpass mode, but its FALLBACK -- an NHDPlus HR HUC4 FileGDB-zip region download +
GDB-layer clip -- is a DIFFERENT executor `select_executor` cannot chain after
http_json, and it is a do-not-regress flood leg (flood.py imported the twin
directly). (2) The biggest named-gap CLUSTER (fetch_3dep_extra, fetch_statsgo_soils,
fetch_hrrr_forecast/smoke) is sources whose maintained LIBRARY owns discovery + the
socket -- an ADR 0044 violation by necessity -- STOP-RULED across ADR 0059/0068/0069
pending a GENERIC library-delegate executor that generalizes the dataretrieval
precedent (ADR 0040).

Decision (2026-08-01):

## Item 1 -- fetch_river_geometry: Overpass fold, NHDPlus leg DELETED (NATE-decided)

NATE ruled (2026-08-01) to DROP the vestigial NHDPlus HR leg (evidence per ADR 0070:
an 8-envelope bbox->HUC4 heuristic + a ~144 MB region download, effectively never
reached since OSM is the reliable global primary). fetch_river_geometry folds as a
CLEAN Overpass source -- the roads/pois precedent (ADR 0070):
- `source.yaml` (shape vector-fgb) + `overpass_river.build_request` /
  `overpass_river.parse_response` hooks over the existing http_json
  `endpoint_fallback` 3-mirror chain. The hooks reuse the shared `_way_coords` /
  `_clip_linestring_parts` decode; the QL is a `waterway` regex with the twin's
  selectable class vocabulary (river/stream/canal default; ditch/drain opt-in; the
  alias table + comma/plus/space split; a closed vocab so no QL injection).
- `source` (kept for signature back-compat; nhdplus_hr/osm both resolve to the
  Overpass path, nhdplus/nhd alias in) + a `waterway_type` str param resolved in the
  hook. The docstring is carried but TRUTHFULLY edited: the NHDPlus fallback claims
  are gone (the leg was removed -- a NATE-decided BEHAVIOR change, recorded in the
  ledger).
- The twin's 5000 km2 guardrail is preserved by a new `gates.max_bbox_km2`
  (cos-lat-scaled `_bbox_area_km2`, byte-identical to the twin; strict no-op for
  every prior spec whose gates omit it).

Parity target: the Overpass-primary path is value-identical; the NHDPlus path is
INTENTIONALLY absent (the NATE flag-not-copy call). FLOOD LEG: flood.py imported the
twin directly -> re-pointed to the registry seam (`TOOL_REGISTRY["fetch_river_geometry"].fn`);
the telemac river_dye + modflow consumers already resolved by name. FLOOD CANARY
(mandatory, leg re-pointed): `scripts/run_sfincs_direct.py` PASSED -- status=ok, depth
COG published (`s3://trid3nt-runs/01KYZXWX5ZJWMHZYTSGGMCW8MC/overviews/...tif` + 7
depth frames + peak), with the river geometry fetched LIVE inside the pipeline via
the spec-driven tool (Overpass 200 -> 18-feature FGB). The 5 offline river_dye
baseline failures stay byte-identical in mode (the pre-existing `_fake_publish`
mock-arity failure, not river-induced -- verified the two river-touching cases fail
at the shared publish mock, never on river resolution).

Non-gating divergences (river): (a) synthesized `layer_id`/`name`
(`river_geometry-river_geometry` / "river_geometry river_geometry") vs the twin's
"rivers-.." / "Rivers & Streams" -- the ADR 0070 (e) LayerURI-cosmetics class (the
FGB DATA + `osm_waterways` preset + role=input + units are value-identical; consumers
read the uri, not the label). (b) `waterway_type` accepts a scalar string (aliases +
comma/plus/space split); a raw Python LIST is no longer the LLM surface (the router
`str` param), though the hook's own resolver still accepts a list for in-process
callers -- the model uses comma-joined strings / aliases. (c) distinct raw
`waterway_type` selectors that resolve to the same class set hit distinct cache
entries (the ADR 0070 (c) pre-cache-key-resolution class; value-identical output).

## Item 2 -- the generic library-delegate mode (generalizing ADR 0040)

A source whose maintained library owns discovery + the socket declares
`hooks.delegate` (a registered hook that CALLS the library and returns arrays/frames)
+ optional `hooks.delegate_validate` (a pre-cache input gate). The router keeps
params/gates/stamps/cache/publish/typed-errors; the delegate hook is the ONE
sanctioned impurity -- a hook that owns a socket -- so it is CONSTRAINED by
`executors/library_delegate.invoke`:
- a DECLARED timeout (`ingest.delegate.timeout_s`) is passed to the hook, which
  forwards it to the library call (never an unbounded hang);
- the call is TELEMETRY-marked library-owned (the impurity boundary is logged with
  library + source + timeout);
- ERROR MAPPING: the hook maps the library's typed failures to the A.6 classes
  (input/empty/upstream) via the shared factories -- exactly as the twin did; any
  library exception the hook did NOT map is caught by the wrapper as a retryable
  upstream error (verbatim reason), never leaking a raw traceback. There is no HTTP
  status for a library socket, so `classify_status` does not apply -- the hook owns
  the taxonomy, the wrapper is the backstop.

Routing: a VECTOR delegate returns GeoJSON features -> the generic
`library_delegate.execute` -> the shared `vector_fgb` writer. A RASTER delegate
declares `ingest.access: library_delegate`; the spec routes through
`raster_cog.execute`, whose `fetch_source_array` calls `library_delegate.invoke` for
`(array, transform, crs)` and the shared COG writer serializes it. The legacy
dataretrieval delegate (ADR 0040) keeps its own module (routed by
`ingest.delegate.library == 'dataretrieval'`, no `hooks.delegate`) -- untouched, its
parity intact. Registration validates the `delegate` / `delegate_validate` hook names
at load. STRICT no-op for every prior spec (none declare `hooks.delegate`; the 59
prior spec count + the dataretrieval routing are unchanged; daemon import clean).

### Folded: fetch_statsgo_soils (proof-by-migration for the library-delegate mode)

pfdf `statsgo.read(field, BoundingBox(...crs=4326), timeout=...)` -> a pfdf `Raster`
the hook reads directly (`.values`/`.affine`/`.crs`, nodata masked to NaN) into
`(array, transform, crs)`. `pfdf_statsgo.validate` is the twin's exact CONUS envelope
(-125,24,-66.5,49.5) run pre-cache; the field enum is router-declarative;
units/style-by-field via `units_by_param` / `style_preset_by_param`; payload
`bbox_area` (1.5 MB/deg2). Live proof: real ScienceBase KFFACT over a Kansas AOI ->
376x288, values 0.31-0.38, 108k finite px -> a valid COG. Value-identical to the twin
(same pfdf array; the COG is in the pfdf-NATIVE Albers CRS EPSG:5069, exactly as the
twin's save->reopen->re-encode preserved -- no reproject either side). Twin deleted;
consumers model_debris_flow + compute_sediment_yield re-pointed to the registry seam.

Non-gating divergences (statsgo): (a) `timeout_s` is dropped from the LLM param
surface and declared as the router-owned `ingest.delegate.timeout_s=60.0` (the
mission's declared-timeout constraint; consumers call with bbox/field only, **kwargs
absorbs a stray timeout_s); this also removes it from the cache key. (b) COG
compression is the router default DEFLATE vs the twin's LZW (same array/CRS/nodata;
the tile pipeline reads either). (c) synthesized `layer_id`/`name` (ADR 0070 (e)
class; units/style/role/layer_type value-identical).

### STOP-RULED with named residuals

- fetch_3dep_extra: the library-delegate mode covers the pfdf.tnm.dem.read call and
  the (array,transform,crs) return cleanly, BUT two residuals remain: (a) it opts OUT
  of auto-publish (role=input intermediate) -- the router has no `output.auto_publish`
  spec field to propagate `auto_publish=False`; (b) its payload estimate is a
  PER-RESOLUTION coefficient table (5/500/5000/1/200 MB/deg2 by the resolution enum)
  the PayloadEstimateSpec models cannot hold. Unblock: add `output.auto_publish`
  (propagated in register_spec) + a per-enum-value payload model (or a flagged flat
  divergence). A fast-follow of THIS mode, not a new mechanism.
- fetch_hrrr_forecast + fetch_hrrr_smoke (same twin body): the delegate mode covers
  the fsspec/xarray Zarr open, BUT three residuals: (a) cycle resolution walks S3
  (`fs.exists`) BEFORE read_through to embed the resolved cycle in the cache key -- a
  delegate PRE-RESOLVE phase doing socket I/O (the pure-hook + declared-delegate
  contract does not yet host a socketed resolve; a cycle=None request would otherwise
  compute a non-deterministic key); (b) the heavy native LCC->EPSG:4326
  rioxarray reproject + clip_box is a heavy post-array step; (c) forecast's derived
  10m_wind_speed opens BOTH U and V components + hypot (multi-array synthesis).
  Unblock: a delegate resolve-phase (socketed pre-cache-key merge) + a post-array
  reproject hook + a multi-component delegate return. QUEUED (HRRR-Zarr delegate
  finish wave).

Consequence: the Overpass family's river member is folded (NHDPlus leg deleted per
NATE), and the generic library-delegate mode -- the mechanism the fetch_3dep_extra /
statsgo / HRRR STOP-RULES all named -- now EXISTS and is proven end-to-end by the
statsgo fold (live). Extends the tier-3 hook contract (ADR 0056/0061/0063/0071/0073)
with `hooks.delegate` + `hooks.delegate_validate` and the `ingest.access:
library_delegate` raster seam; generalizes the dataretrieval delegate (ADR 0040).
Supersedes the ADR 0070 river STOP-RULE and the ADR 0059/0068/0069/0071 statsgo
STOP-RULE.
