# ADR 0090 -- DEM + STORM_TRACKS wave: fetch_dem and fetch_storm_tracks both STOP (named gate-level residuals)

Status: accepted (2026-08-03)
Follows: ADR 0089 (topobathy STOP, the envelope-provenance gap). Same campaign
(universal-ingest fold), same disposition class: two never-assessed coded
data-fetchers characterized in full, both fail their OWN first acceptance gate,
both STOP with a precise residual. No code change.

## Context

The campaign coded-data-fetcher counter stands at 9 (target this wave: 9 -> 8 or
7). `fetch_dem` and `fetch_storm_tracks` are the two remaining never-assessed
coded data-fetchers. Both were read in full and audited against the CURRENT
router surface at HEAD 183c653 (`router.py::route`, `executors/http_json.py`,
`executors/vector_fgb.py`, `executors/raster_cog.py`,
`executors/chained_resolution.py`, `contracts/execution.py`). A `fetch_dem` SPEC
carries extra strategic weight: ADR 0089 names it as one unblock for the
topobathy nested-topo leg (a `route()`-recursion target). This wave asked, per
source, whether a fold can pass GATE 1 (offline edge-matrix parity vs the twin,
field-for-field) and whether the twin's bespoke steps have any router surface.

Answer for both: no. The residuals are stated at gate level below.

## Decision 1 -- fetch_dem STOP (multi-source composite; no fold)

`fetch_dem` is a 3DEP-primary + Copernicus-GLO-30-fallback COMPOSITE, not a
single-source raster read. Four blockers, each verified at 183c653:

1. MULTI-SOURCE FALLBACK LADDER WITH FETCH-TIME PROVENANCE RESTAMP -- the ADR
   0089 envelope-provenance gap in a second guise. On a 3DEP service failure /
   `DemPrimaryTimeoutError`, `fetch_dem` (in `source="auto"`) delegates to a
   DIFFERENT registered tool (`fetch_copernicus_dem`) and restamps
   `LayerURI.name` (" (Copernicus GLO-30 -- 3DEP unavailable)") + the native
   `LayerURI.fallback_note` field (which source served + a resolution note). The
   router's fallback surfaces are SAME-source mirror / parse chains
   (`vector_fgb` endpoint chain; `http_json` `endpoint_fallback` /
   `parse_fallback`) that produce ONE output with NO name / fallback_note
   restamp and cannot delegate to a DIFFERENT registered tool. And a cache HIT
   never runs `fetch_fn`, so which source served is unrecoverable on the hot
   path (the same cache-hit gap ADR 0089 named). A fold cannot reproduce the
   fallback path field-for-field -> GATE 1 fails before any live drive.

2. THE 3DEP-PRIMARY LEG IS BESPOKE WITH NO ROUTER SURFACE. It calls
   `py3dep.get_dem` (the seamless 1/3-arc-second path -- a DIFFERENT library
   than `fetch_3dep_extra`'s `pfdf.data.usgs.tnm.dem` multi-resolution path, so
   the existing pfdf `library_delegate` hook does NOT serve it) wrapped in
   (a) a hard wall-clock bounded-timeout DAEMON thread
   (`_fetch_3dep_dem_bytes_bounded` -> `DemPrimaryTimeoutError`; the in-flight
   attempt is abandoned + its result discarded so no cache write of a timed-out
   fetch) and (b) a partial-coverage gate (`_dem_wgs84_bounds` reprojects the
   returned raster's bounds to WGS84, `_bbox_covers` asserts 4-edge coverage
   within tolerance, raises `DemPartialCoverageError` -- a RETRYABLE
   `UpstreamAPIError` subclass the urban 1m->10m ladder + the agent's honest
   narration catch externally). A router hook is PURE (no I/O, ADR 0056); the
   bounds-reproject coverage check and the timeout thread are I/O-bound
   wrappers, not pure hooks, and there is no declarative bounded-wall-clock or
   coverage-gate directive.

3. PIN SEMANTICS + AUTO-COARSEN. A `source` enum (auto / 3dep / copernicus)
   routes to different behaviors (incl. the internal `fetch_copernicus_dem`
   seam), plus a pixel-budget auto-coarsen that stamps the DELIVERED resolution
   into `LayerURI.name` + a 5,000,000 km^2 continent ceiling. The enum + gates
   are partially expressible, but not stacked on blockers (1) and (2).

4. WIDE DIRECT-IMPORT BLAST RADIUS. EIGHT consumers import the `fetch_dem`
   FUNCTION directly (not via `TOOL_REGISTRY`): `flood.py`, `fetch_topobathy`
   (the nested 3DEP-land call), `compute_contours`,
   `extract_model_at_observations`, `run_elmfire`,
   `model_dambreak_geoclaw_scenario`, `model_urban_flood_swmm`,
   `model_landslide_scenario` -- plus the `__init__` / `main` registration
   imports. A registration-fold + module-delete breaks all 8 unless the twin
   core stays importable (a hollow relabel-split, LOC delta ~0, the exact shape
   the clean-as-you-go doctrine warns against) or all 8 re-point to
   `TOOL_REGISTRY` (a wide, high-risk repoint for zero functional gain).
   `flood.py` + `fetch_topobathy` are DO-NOT-REGRESS flood legs, so a real fold
   would additionally mandate a flood-consumer re-point + FLOOD CANARY.

### Why even a PARTIAL dem spec does not unblock the topo leg (for ADR 0089)

The "dem source spec" the topobathy nested-topo leg wants PARTIALLY exists
already: `fetch_copernicus_dem` (global GLO-30 leg, tier=internal) +
`fetch_3dep_extra` (multi-resolution TNM leg, pfdf `library_delegate`). What is
missing is a `py3dep`-seamless-10m spec, which needs a NEW `py3dep`
`library_delegate` hook + the bounded-timeout + the coverage-gate machinery of
blocker (2). And decisively: `fetch_topobathy` calls `fetch_dem` for its 3DEP
LAND leg and depends on `fetch_dem`'s FULL contract (the auto Copernicus
fallback + the partial-coverage gate); a partial dem spec covering only the
seamless leg would NOT reproduce that, so ADR 0089's "route() on a dem spec"
unblock remains genuinely blocked. The counter stays honest.

## Decision 2 -- fetch_storm_tracks STOP (scoped-job: binary-secondary-enrichment mode)

IMPORTANT FRAMING: the ADR 0089 remaining-worklist filed storm_tracks under
"track/line assembly (fetch_storm_tracks, fetch_movebank_tracks)". That premise
is now STALE -- `fetch_movebank_tracks` is ALREADY FOLDED (a `vector-fgb` spec +
`movebank_tracks.build_request` / `parse_response` / `classify_status` hooks +
`properties_by_param` for the linestring-per-individual vs point-per-fix schema
switch). Per-storm LineString grouping is therefore PROVEN foldable; this wave
evaluated storm_tracks against that proven pattern, not the stale bucket.

WHAT WOULD FOLD (the historical mode alone). HISTORICAL IBTrACS mode maps
cleanly onto `http_json` + hooks exactly like movebank: `build_request` returns
the 1-2 selected per-basin CSV plans (the basin-envelope file selection is pure
compute over params); `parse_response` parses the multi-file CSV (units-row
skip, `spur` TRACK_TYPE drop, season + name filter, USA_WIND->WMO_WIND fallback,
the wind-structure spiderweb columns), does the storm-wise bbox selection
keeping the FULL track, and builds either one LineString per storm (aggregate
props) or one Point per fix; `classify_status` maps HTTP status;
`properties_by_param` carries the lines / points schema switch; the honest-empty
`StormTracksNoStormsError` becomes `router_empty_error`.

THE DECISIVE BLOCKER (why the WHOLE tool cannot fold). The tool is ONE
registered name with an `active_only` param and MUST reproduce BOTH modes
field-for-field. The ACTIVE mode (`active_only=True`) is a resolve-then-fetch
chain whose SECOND round is a BINARY zip-shapefile: fetch NHC
`CurrentStorms.json`, then for EACH active storm fetch its
`forecastTrack.zipFile` (a URL discovered from the primary JSON), extract
`*_pts.shp` to a tempdir, read via geopandas + reproject to EPSG:4326, and
append forecast points (`tau_h` / `is_forecast`). `chained_resolution`'s enrich
phase fetches detail bodies via the shared transport (bytes are fine) but the
`enrich_merge` hook that DECODES them is PURE (no I/O, ADR 0056); decoding a
zip-shapefile requires tempdir `extractall` + `geopandas.read_file` (file I/O) +
CRS reprojection -- I/O inside a pure hook, which no router surface carries.
Dropping the enrichment to dodge the I/O changes the output (the forecast points
+ `tau_h` / `is_forecast` vanish) -> GATE 1 fails. So a whole-tool fold is
blocked on a binary-secondary-enrichment mode that no wave has built.

SECONDARY STRUCTURAL NOTE: two entirely different fetch SHAPES live under one
name (historical multi-file-CSV vs active JSON + per-storm-binary-zip) with
mode-dependent param relevance -- beyond `endpoint_select`, though the
`build_request` / `parse_response` hooks COULD branch on `active_only` if the
active-mode I/O were expressible.

CONSUMER: only `sfincs_forcing_autowire` imports `fetch_storm_tracks` directly
(`autowire.py:838` -- the SFINCS spiderweb wind-forcing leg), a NARROW blast
radius; but the narrow radius does not rescue the fold (the active-mode I/O
residual is decisive). Not folded, seam UNTOUCHED, so NO flood / SFINCS canary
was mandated or run this wave.

## Consequences

- No code change. Both twins untouched; router / spec / contracts untouched.
  Campaign coded-data-fetcher counter UNCHANGED at 9 (target 9 -> 8 or 7 NOT met
  -- honestly deferred, the same disposition as ADR 0089). coded tools 98, coded
  fetchers 11, spec-served 88, registry 186 all unchanged.
- Offline baseline unchanged by construction (docs-only wave): EXACTLY 9
  failures (test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5) from
  the repo root, no .env.local. The `test_fetch_resolution_gate`
  `[fetch_dem-dem]` parametrized member is a PRE-EXISTING baseline member whose
  failure mode is untouched (no fetch_dem or router code changed) -- it fails
  identically pre / post. Suite re-run this wave: 9 failed, matching baseline.
- DELETION_LEDGER gains a `fetch_dem fold` row + a `fetch_storm_tracks fold`
  row (both STOP-RULED / QUEUED with the named residual + unblock).
- metrics.md gains a rolling docs-only row (2026-08-03c).

### Unblock conditions

- fetch_dem: a cross-registered-tool FALLBACK-WITH-RESTAMP seam (primary tool
  fails -> call a DIFFERENT tool -> restamp `name` + `fallback_note`,
  cache-replayable so a hit reproduces provenance) + a `py3dep`-seamless
  `library_delegate` hook + a bounded-wall-clock-timeout directive + a
  declarative coverage-gate (reproject-bounds partial-coverage) surface + the
  `source`-enum pin + the pixel-budget auto-coarsen + the 8-consumer re-point +
  FLOOD CANARY. A scoped multi-source-composite job (the same class as
  fetch_noaa_nwm_streamflow and fetch_topobathy), not a fold wave.
- fetch_storm_tracks: a BINARY-SECONDARY-ENRICHMENT mode (a per-item secondary
  fetch whose body is a zip-shapefile decoded + reprojected by an I/O-permitted
  delegate step, cache-consistent), OR accepting a `library_delegate`-style
  whole-tool delegate carrying both modes (a forced relabel of ~500 bespoke LOC,
  LOC delta ~0 -- the topobathy-style rejection). Historical-only folds cleanly
  but is not a legal fold of the single-named tool.

An honest STOP with named, gate-level residuals beats a forced fold that cannot
reproduce the twin field-for-field.
