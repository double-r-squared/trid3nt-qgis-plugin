# Router pilot contract - the generic data-router fold, phase-1 pilot

Authority: `docs/specs/data-router-fold.md` (architecture + retention +
indistinguishability principle + gates) and `docs/specs/fetcher-fold-audit.md`
(per-fetcher shapes + the canonical anatomy). This document PINS the pilot's
build surface BEFORE any code lands: the source-spec schema, the executor set
and the exact shared seams each executor binds to, the fold-arm surfacing
mechanism + experiment toggle, the replication-parity harness, and file
placement. Branch `refactor/engine-doors`. No code or tree state changes here.

NATE-approved scope: the 5 pilots (`fetch_gridmet`,
`fetch_hifld_critical_infrastructure`, `fetch_noaa_coops_tides`,
`fetch_esri_landcover_10m`, `fetch_census_acs`) + the JOIN named transform as a
first-class primitive. Hand-written twins STAY registered and untouched; the
fold arm is an experiment-time env toggle, never a tree change.

## 0. The design axiom this contract enforces

INDISTINGUISHABILITY (data-router-fold.md, retention principle): a spec-driven
source must flow through the IDENTICAL pipeline and surface IDENTICALLY to a
catalog-native or hand-written one. A consumer (LLM retrieval, nested workflow
import, envelope reader) cannot tell the origin. Concretely, every executor
below terminates in the SAME four shared seams the audit named as the fetcher
boilerplate (typed errors, `read_through` cache, `estimate_payload_mb` gate,
`LayerURI`), so the only thing that differs between twin and spec is the ~15-25%
source-specific body - which is exactly what the YAML captures.

The pilots span the audit's five shapes plus the awkward JOIN case:

| pilot | audit shape | verdict | executor | proves |
|---|---|---|---|---|
| fetch_gridmet | raster-COG (OPeNDAP-xarray) | SPEC | raster-cog | netCDF subset -> time-mean -> COG |
| fetch_hifld_critical_infrastructure | vector-API-FGB (ArcGIS) | SPEC | vector-fgb | the ~25-fetcher ArcGIS resultOffset pattern |
| fetch_noaa_coops_tides | station-timeseries-FGB | SPEC | station-timeseries-fgb | catalog-discover + per-station loop + inline time_series_csv |
| fetch_esri_landcover_10m | tiled-imagery (STAC + mosaic) | HYBRID | raster-cog + tiled-mosaic transform | STAC search + auto-tile grid + rasterio.merge |
| fetch_census_acs | vector 2-endpoint geometry+values | HYBRID | vector-fgb + JOIN-on-key transform | the two-source join 6+ fetchers share |

---

## 1. SOURCE SPEC SCHEMA (data, not code)

One YAML per source, co-located beside the source's existing tool folder as
`source.yaml` (sibling to `fetch_X.py` + `corpus.yaml`), so the fold is
clean-as-you-go: when the twin dies in phase 2 its `fetch_X.py` is deleted and
`source.yaml` remains. The spec is validated by a pydantic model
`SourceSpec` (proposed home: `trid3nt_contracts.source_spec`, mirroring
`CatalogEntry` - one shape shared by the router loader AND the parity harness so
the two paths cannot drift). The loader `_compose_specs_from_tree()` walks
`fetchers/**/source.yaml` with `rglob`, exactly mirroring
`search_tools._compose_corpus_from_tree()`'s `rglob("corpus.yaml")`.

### 1.1 Schema (all top-level keys)

```yaml
name: fetch_noaa_coops_tides          # registry key; MUST equal the twin's name
source_class: noaa_coops_tides        # cache <source-class> prefix (cache.py)
shape: station-timeseries-fgb         # selects the executor (enum, sec 2)
supports_global_query: false          # AtomicToolMetadata flag; bbox=None policy

# --- endpoints + auth ---------------------------------------------------
endpoints:
  catalog:                            # named endpoints; templated per-request
    url: "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
    query: {type: waterlevels, units: metric, format: json}
  data:
    url_template: "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
auth:
  mode: none                          # none | api_key_env | cds | vault | token
  # api_key_env: {var: CENSUS_API_KEY, required: false}  # keyless-fallback ok
  user_agent: trid3nt_default         # -> shared _fetch_common User-Agent

# --- request-param schema (validate BEFORE any network call) ------------
params:
  bbox:      {type: bbox, required: true, quantize: round_6dp}
  start_date:{type: iso_date, required: true}
  end_date:  {type: iso_date, required: true, max_range_days: 366}
  product:   {type: enum, values: [water_level, predictions], default: water_level}
gates:
  conus_only: false                   # bbox-intersects-CONUS gate (gridmet: true)
  max_bbox_deg2: null                 # hard ceiling (esri_landcover: 8.0)
  max_stations: 50                    # station-timeseries only
  max_features: 30000                 # vector only (paging cap)

# --- ingestion (shape-specific block; keyed by `shape`) -----------------
ingest:
  station_catalog: {lat_key: lat, lon_key: lng, id_key: id, name_key: name}
  per_station:
    request: {datum: MLLW, time_zone: gmt, interval: h, units: metric,
              begin_date: "{start:%Y%m%d}", end_date: "{end:%Y%m%d}",
              station: "{id}", product: "{product}", format: json}
    rows_key: [data, predictions]     # first present wins
    time_key: t
    value_key: v
    time_series_csv: true             # inline CSV attribute (SFINCS forcing)
    scalars: [wl_min_m, wl_max_m, wl_mean_m, n_timesteps, time_start, time_end]

# --- normalization stamps (indistinguishability) ------------------------
normalize:
  crs: EPSG:4326
  units: "m (MLLW)"
  datum: MLLW                         # null when N/A
  quantity: water_level
  orientation: north_up               # raster only; gridmet lesson (no sortby)

# --- output surface -----------------------------------------------------
output:
  layer_type: vector                  # raster | vector
  ext: fgb                            # tif | fgb | json
  role: primary
  style_preset: "coops_{product}"     # may template on a param

# --- cache + payload gate -----------------------------------------------
cache:
  ttl_class: dynamic-1h               # one of the four TTLClass literals
payload_estimate:                     # -> synthesized estimate_payload_mb()
  model: per_station                  # bbox_area | per_station | per_feature | tiled
  kb_per_station_per_day: 2.0
  overhead_kb: 0.5
  stations_per_sq_deg: 2.0
  floor_mb: 0.01

# --- honesty: caveats + fallback chain ----------------------------------
caveats:
  - "~300 US stations globally; empty-bbox -> COOPS_TIDES_EMPTY (retryable=false)"
fallback: []                          # ordered sibling sources (census: keyless)

# --- retrieval phrasings (verbatim from the twin's corpus.yaml) ---------
corpus:
  - "what are the tide levels at Fort Myers for this date range"
  - "observed coastal water level for SFINCS boundary forcing at a US gauge"
  # ... carried verbatim from the co-located corpus.yaml
```

### 1.2 Per-shape `ingest` block (grounded in the pilots)

- raster-cog / OPeNDAP (`fetch_gridmet`): `dap_url_template`
  (`agg_met_{variable}_1979_CurrentYear_CONUS.nc`), `variables` table
  `{code: {long_name, units}}`, `time_dim` candidates, `time_reduce: mean`,
  `time_base` (days-since-1900 fallback). Lifted from the `_VARIABLES` dict +
  `_open_thredds_subset`.
- vector-fgb / ArcGIS (`fetch_hifld_critical_infrastructure`): `routing` dict
  `facility_type -> {service, label}`, `aliases` table, `query_template`
  (where / geometry envelope / inSR / outFields / f=geojson / orderByFields),
  `pagination: {mode: result_offset, page_size: 2000}`. Lifted from
  `FACILITY_TYPES` + `_build_query_url` + `_fetch_features_paginated`.
- station-timeseries-fgb (`fetch_noaa_coops_tides`): as 1.1.
- tiled-mosaic + STAC (`fetch_esri_landcover_10m`): `stac: {root, collection,
  data_asset, sign: sas}`, `year: {min, max, default}`, `native_cell_m: 10`,
  `tile_deg2: 0.5`, `mosaic: {method: first_non_nodata, resampling: nearest,
  nodata: 0}`, `palette: passthrough`, `class_labels` table. Lifted from
  `_select_items` + `_plan_tile_grid` + `_fetch_single_tile_mosaic`.
- vector-fgb + JOIN (`fetch_census_acs`): TWO endpoints (`geometry` =
  TIGERweb tract query, `values` = data.census.gov table) + a `join` block (sec
  2.5).

---

## 2. EXECUTOR SET

Five executors live in a new shared package `fetchers/_router/` (leading
underscore = helper, not a tool - matching `_fetch_common.py`, `_pc_stac.py`).
`router.py` is the single engine: `resolve spec -> validate params -> build
request -> fetch w/ retry+fallback -> dispatch to executor by `shape` -> stamp
(CRS/units/datum) -> read_through cache -> emit LayerURI`. Each executor is a
pure `(spec, validated_params) -> bytes` function passed as the `fetch_fn` to
`read_through`; the router owns everything around it.

### The four shared seams every executor binds to (exact functions)

| seam | exact binding | module |
|---|---|---|
| cache read-through | `read_through(metadata, params, ext, fetch_fn)` -> `ReadThroughResult(uri, data, hit)` | `agent/tools/cache.py` |
| granularity / payload gate | metadata `payload_mb_estimator_name` -> the router SYNTHESIZES `estimate_payload_mb(**args)` from `spec.payload_estimate`; server.py reads it for the `tool-payload-warning` envelope (>25MB warns, >250MB blocks, #154 granularity block) | `server.py` dispatch |
| typed upstream errors | per-source subclasses of the shared base `FetchError`/`UpstreamAPIError`/`BboxInvalidError` (carry `error_code` + `retryable`); the router raises `RouterUpstreamError`/`RouterInputError`/`RouterEmptyError` whose `error_code` is `spec.source_class.upper()+"_UPSTREAM"` etc. so the A.6 frame is byte-identical to the twin's | `fetchers/_fetch_common.py` |
| LayerURI emission | `LayerURI(layer_id, name, layer_type, uri=result.uri, style_preset, role, units, bbox)` templated from `spec.output` | `trid3nt_contracts.execution` |

Plus bbox handling: `_validate_bbox` + `round_bbox_to_resolution` (raster) /
6dp round (vector) from `_fetch_common.py`; registration via `register_tool` +
`AtomicToolMetadata` synthesized from the spec's `name/source_class/ttl_class/
supports_global_query/payload_mb_estimator_name` fields (`agent/tools/__init__.py`).

### 2.1 raster-cog executor  (OPeNDAP + direct-COG/http + STAC)

Reads a gridded source to a CRS-tagged single-band COG. Three sub-modes keyed
by `ingest.access`: `opendap` (xarray subset + `time_reduce` collapse, gridmet),
`direct_window` (rasterio `/vsicurl/` windowed read of a known COG/VRT/
ImageServer), `stac_search` (pystac-client search + `_pc_stac.sas_sign_href` +
`_pc_stac.bbox_pixel_dims` windowed reproject). Emits `nodata=nan`, north-up
(no lat sortby - the gridmet orientation lesson is a spec `normalize.orientation`
directive), `rio.write_crs` re-asserted post-astype. Reuses `_pc_stac`
primitives verbatim for the STAC sub-mode.

### 2.2 vector-fgb executor  (incl ArcGIS paging mode)

Query -> GeoJSON/esri-json -> FlatGeobuf via `geopandas ... driver="FlatGeobuf",
engine="pyogrio"`. `pagination.mode` selects `result_offset` (hifld/census) or
`exceeded_transfer_limit`; the loop mirrors `_fetch_features_paginated` with the
`max_features` cap. `esri_json_rings -> GeoJSON` normalization is an opt-in
`ingest.esri_json: true` (NWI/EJSCREEN reuse later). ALWAYS emits a valid FGB -
an empty result is a header-only FGB (honest-empty, never a fabricated error),
matching hifld/census twins.

### 2.3 station-timeseries-fgb executor

Catalog-discover (`ingest.station_catalog` bbox filter, `max_stations` cap) ->
per-station data loop (`_STATION_REQUEST_DELAY` politeness) -> point-FGB with
one Point per station + the scalar rollups + the inline `time_series_csv`
attribute (comma-separated `iso,value` rows for SFINCS boundary consumption).
Individual station failures are swallowed to `None` (one bad station never
aborts the bbox); all-empty -> typed `*_EMPTY`. This is the exact
`_fetch_coops_tides_bytes` + `_build_flatgeobuf` contract, generalized.

### 2.4 tiled-mosaic transform  (HYBRID glue, esri_landcover)

A named transform WRAPPING the raster-cog STAC sub-mode: `_plan_tile_grid`
splits a bbox >`tile_deg2` into sub-tiles, each fetched via the raster-cog
executor, written to temp GTiffs, merged via `rasterio.merge` (`method="first"`,
`resampling=nearest`, categorical-safe), palette passthrough preserved. Single-
tile bbox is the fast path (one executor call). Hard ceiling `max_bbox_deg2`
raises the typed bbox error redirecting to the sibling (NLCD). This is a
transform (composes the executor N times), not a new executor - keeping executors
atomic per the "analysis is composition" norm.

### 2.5 JOIN-on-key transform  (first-class, NATE-approved)

The pattern that decides the fold's ceiling (audit surprise #1: census_acs,
lehd_jobs, usgs_gw, usgs_wq, volcano_alerts, openfema). Declarative block:

```yaml
join:
  geometry: {endpoint: geometry, key_field: GEOID, keep: [NAME, STATE, COUNTY]}
  values:
    endpoint: values
    key_field: geoid11                # derived: str(GEO_ID).split("US")[-1]
    scope_by: [STATE, COUNTY]         # values fetched per-county from geometry set
    null_sentinel_below: -666666000.0
    variables:                        # the ACS_VARIABLES registry, declarative
      median_income: {table: B19013, code: B19013_001E, kind: value, units: usd}
      poverty_rate:  {table: B17001, num: [B17001_002E], denom: B17001_001E,
                      kind: pct, units: percent}
  derive: {value: kind, pct: "100*sum(num)/denom"}   # _compute_value, declarative
```

The transform fetches geometry (vector-fgb executor), extracts the scope set
(counties), fetches values per-scope, left-joins on the key, derives the
per-feature value, serializes FGB. Missing value -> `null` (NEVER fabricated -
honesty rule). Optional `api_key_env` with keyless fallback is expressed in
`auth` + `fallback`. If this join carries census_acs indistinguishably, the whole
socioeconomic + USGS-join subfamily folds.

---

## 3. SURFACING for the fold arm

### 3.1 The mechanism the audit's tier machinery gives us

`AtomicToolMetadata.tier` (`general` / `door` / `template`) already DECOUPLES
registration from retrieval visibility, proven live: a `tier="template"` tool is
in `TOOL_REGISTRY` but EXCLUDED from every default-pool producer -
`search_tools._build_index` (skips `tier=="template"`, search_tools.py L610),
`tool_retrieval._full_registry_floor` (non-template filter), and
`server._default_declarable_registry` (non-template filter). All three exclude
templates by the SAME registry-lookup filter. That is precisely the lever the
fold arm needs: swap what the DEFAULT POOL sees without touching TOOL_REGISTRY.

### 3.2 Leading candidate: per-source VIRTUAL surfaces (not the consumption pair)

Two candidates were on the table (data-router-fold.md RETRIEVAL section):

- (A) per-source virtual tools - each spec registered under the SAME name +
  SAME corpus as its twin, competing in the retrieval pool identically.
- (B) the consumption pair `search_data_catalog` + `fetch_from_catalog`.

LEADING = (A), per-source virtual surfaces, for three decisive reasons:

1. INDISTINGUISHABILITY / fair A/B. The routing-parity experiment must isolate
   ONE variable: implementation body (twin Python vs spec+router). Registering
   the spec-driven source under the identical name, corpus phrasings, docstring
   surface, and `tier="general"` means the retrieval index, RRF channels, and
   pool producers see a byte-identical entry EXCEPT the callable - so any ranking
   delta is attributable to nothing but the fold. The consumption pair changes
   the surface shape (one generic tool + an entry-id arg), confounding the test.
2. NESTED-CONSUMER seam (the real do-not-break set). ~40 workflow/gate modules
   import fetchers by name (`from ...fetch_noaa_coops_tides import ...`;
   `sfincs_forcing_autowire` alone imports 6). A per-source virtual tool exposes
   the SAME callable seam per source (registry-resolved), so nested consumers
   migrate mechanically (data-router-fold.md consumer-compatibility). The
   consumption pair would force rewriting every nested import into an entry-id
   dispatch - a large, risky, out-of-pilot-scope change.
3. CORPUS reuse is free. The spec carries the twin's `corpus.yaml` phrasings
   verbatim; `_compose_corpus_from_tree` already keys corpus by tool name, so the
   phrasings route to the virtual tool with zero index change.

(B) is NOT discarded - it is the bench's ALTERNATE arm (the experiment also
measures the consumption pair as a second fold-arm variant), and it remains the
right surface for genuinely long-tail catalog entries with no dedicated name. But
for the 5 registered pilots the leading + primary-measured candidate is (A).

### 3.3 The experiment toggle (env-flagged, tree state UNCHANGED)

`TRID3NT_FETCHER_FOLD_ARM` env var. UNSET = baseline arm = the tree exactly as
today. SET = fold arm. The switch operates ONLY at the pool-producer seams, never
on `TOOL_REGISTRY` membership:

- BOTH surfaces are registered in `TOOL_REGISTRY` at import: the hand-written
  twin (`fetch_X`, tier=general, untouched) AND the spec-driven virtual tool
  (registered by the router loader under an internal alias, tier=general). Tree
  state = twins present + untouched (satisfies the HARD RULE).
- A single env-gated substitution map `{twin_name: virtual_entry}` is consulted
  by the three pool producers (`_build_index`, `_full_registry_floor` /
  `retrieve_visible_tools`, `_default_declarable_registry`) - the SAME three that
  already filter on `tier`. Baseline: twin in pool, virtual pool-excluded (as if
  `tier=template`). Fold: virtual in pool under the twin's name, twin pool-
  excluded. The exclusion reuses the existing non-template filter idiom; no new
  filtering machinery.
- Because the switch is a runtime pool substitution keyed on an env flag, it
  "deregisters twins from the DEFAULT POOL ONLY at experiment runtime" - never a
  code deletion, never a registry mutation. Flip the env, re-run the arm, flip
  back: identical tree.

This keeps the fold arm a pure experiment-time toggle and lets phase-2 migration
(clean-as-you-go) later replace the substitution with an actual twin deletion
per source only after BOTH gates pass.

---

## 4. PARITY HARNESS design

Two gates, two harnesses. The ROUTING gate already has a signed-pending DESIGN
(`experiments/fetcher_fold_routing/DESIGN.md`) - this contract adds the
REPLICATION gate (data-router-fold.md's per-source cull doctrine), item 4.

### 4.1 Routing parity (reference; already designed)

Model-free, deterministic. Baseline vs fold arm through the production retrieval
path (`retrieve_ranked_tools` / `retrieve_visible_tools`) over NATE-signed
phrasings; grading = per-phrasing ACCEPTABLE SET validated against the live
registry (routing_sweep rules); metrics hit@5/hit@8/top1/nDCG@5/MRR@5. Pass:
fold hit@8 >= baseline, nDCG@5 within 0.05, controls byte-identical. See DESIGN.md.

### 4.2 Replication parity (this contract's addition)

For each pilot, run the hand-written twin (`TOOL_REGISTRY[name].fn(**args)`,
direct call per the direct-invocation norm) AND the spec-driven router executor
with the SAME fixed args, then compare envelopes field-by-field. Deterministic,
NATE-signs-the-fixed-requests-first (experiments methodology rule). Cached-
friendly: `read_through` against MinIO with `.env.local` env (never ambient AWS);
re-runs hit cache so the gate is cheap + reproducible. In-process, offline-first
validated on the stub before any live drive.

Per-source FIXED requests (small, cheap, deterministic - from the twins' own
docstring examples, no downtown-single-building, natural extents):

| pilot | fixed request | exercises |
|---|---|---|
| gridmet | ~1 deg Riverside Co CA bbox, `fm100`, 3-day window | OPeNDAP subset + time-mean |
| hifld | small metro bbox, `facility_type=hospitals` | ArcGIS resultOffset single page |
| coops_tides | Fort Myers+Naples bbox (2 stations), 1-day `water_level` | station loop + time_series_csv |
| esri_landcover | one <0.5 deg2 bbox (single-tile) AND one ~1 deg2 bbox (2-tile) | STAC read + the mosaic transform |
| census_acs | small Harris Co TX bbox, `median_income` (value) AND `poverty_rate` (pct) | the JOIN + both derive kinds |

ENVELOPE comparison fields (a PASS requires ALL equal, floats within tolerance):

- VALUES / artifact semantics. Raster: band count, dtype, CRS, nodata,
  transform bounds, and `nanmin/nanmax/nanmean` of finite pixels (tol 1e-3
  relative). Vector: feature count, geometry type, CRS, property schema (column
  set), and a value spot-check (e.g. the `median_income` value on a known GEOID;
  the `wl_mean_m` on CO-OPS station 8725520).
- LAYER OUTPUT. `LayerURI` fields: `layer_type`, `style_preset`, `role`,
  `units`, `bbox` present/absent. (`layer_id`/`name` may differ in cosmetic
  formatting - compared for structure, not string equality.)
- CAVEATS / fallback. The honest caveat + `fallback_note` text the twin surfaces
  must be reproduced from `spec.caveats` (semantic-equal, not necessarily byte).
- ERROR PATHS incl one FORCED upstream failure per source. Monkeypatch the
  endpoint(s) to a 500 / timeout; assert BOTH twin and router raise the SAME
  typed class semantics: identical `error_code`, identical `retryable`, and the
  upstream body/reason surfaced VERBATIM (upstream-provider-errors-never-
  internalized rule). Also assert the empty-result path (empty bbox) yields the
  SAME outcome (honest-empty FGB for hifld/census; typed `*_EMPTY` for
  coops/gridmet/esri).

GRADING: field-by-field equality with the float tolerances above; per-source
verdict PASS iff values + layer-output + caveats + BOTH error paths all match. A
source's fold BLOCKS on any replication mismatch; it dies (twin deleted) only
after BOTH routing AND replication pass (cull doctrine).

---

## 5. File placement (repo conventions)

- Router engine (new shared package, underscore = not-a-tool):
  `server/src/trid3nt_server/agent/tools/fetchers/_router/router.py` (engine),
  `_router/spec.py` (loader `_compose_specs_from_tree` + `SourceSpec` binding),
  `_router/executors/{raster_cog,vector_fgb,station_timeseries}.py` (executors),
  `_router/transforms/{tiled_mosaic,join}.py` (named transforms),
  `_router/errors.py` (Router* typed errors over `_fetch_common` bases),
  `_router/registration.py` (synthesizes `AtomicToolMetadata` +
  `estimate_payload_mb` + `register_tool` from a spec; owns the env-gated pool
  substitution map).
- Source specs (co-located, clean-as-you-go): `fetchers/<family>/<source>/
  source.yaml`, sibling to the existing `fetch_X.py` + `corpus.yaml` (e.g.
  `fetchers/ocean/fetch_noaa_coops_tides/source.yaml`). Corpus phrasings carried
  verbatim from the adjacent `corpus.yaml`.
- Shared contract shape: `contracts/src/trid3nt_contracts/source_spec.py`
  (`SourceSpec` pydantic model), mirroring `catalog.py`'s `CatalogEntry`.
- Experiments: `experiments/fetcher_fold_routing/` (routing gate, exists) +
  `experiments/fetcher_fold_replication/` (NEW - `inputs/requests.json`
  NATE-signed, `run.py` twin-vs-router driver, `results/VERDICT.md`).
- Tests: `server/tests/test_router_engine.py`, `test_router_spec_loader.py`,
  `test_router_executors.py` (mirror the existing `test_fetch_*` conventions:
  registry-presence + typed-error + mocked-fetch roundtrip + cache-hit), plus
  one `test_router_parity_<source>.py` per pilot.
- Decision note (ADR-lite norm): `trid3nt-local/docs/decisions/
  00NN-fetcher-fold-router.md` (context / decision / consequence), supersede-not-
  rewrite.

---

## 6. Open decisions surfaced for NATE (not blocking the build)

1. `SourceSpec` home - `trid3nt_contracts` (shared with the harness, mirrors
   `CatalogEntry`) vs router-package-local. This contract pins CONTRACTS for
   shared validation; flag if you prefer engine-local.
2. Virtual-tool registration alias - the fold arm registers BOTH twin and
   virtual surface; the virtual entry needs an internal alias name (e.g.
   `fetch_X__spec`) that the pool-substitution map re-labels to `fetch_X`. Pins
   an alias convention; alternative is a registry side-table.
3. The routing experiment's SECOND arm (consumption pair, sec 3.2 B) is measured
   but not the leading surface; if you want only arm (A) measured for the pilot,
   say so and the consumption-pair arm drops.
