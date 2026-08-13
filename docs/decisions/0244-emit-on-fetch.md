# ADR 0244 - Emit-on-fetch: the render declaration IS the visualization intent

Status: SEAM + S2 COLLAPSE LANDED (2026-08-13). The IN-COMPOSER input-surfacing gap is closed
at the shared universal-fetcher router seam (`route()`): a fetch whose spec
returns a renderable `LayerURI` (a raster COG or a vector FGB, never a `record`
dict) is auto-surfaced as a `role="context"` "Input: ..." row whenever it is
fetched NESTED inside a composer, riding the existing `layer_uri_emit` machinery.
The direct-chat path is unchanged (the tool-wrapper already emits the returned
LayerURI). The S2 collapse (deleting the 18 per-family `_surface_*` helpers + the
~54 composer emission call sites + the uri-threading plumbing) and the live flood
canary are SCOPED to the NATE live-verification loop (per "flood canary after
LARGE changes" + "NATE tests live"); until then the seam and the legacy helpers
COEXIST, de-duped by uri (no visible double rows).
Date: 2026-08-13

## Context

Every engine consumes renderable inputs (DEM/topobathy, rivers, land cover,
fault traces, building footprints), but only the RESULT layer was published
automatically. Input surfacing was bolted on per-family through hand-written
`_surface_*` helpers + explicit `publish_input_layer` / `publish_raster_input_cog`
call sites threaded through composer bodies (task #207, ADR 0231). That is ~54
call sites across 34 workflow files plus the uri-threading plumbing that existed
only to feed them (3-tuple returns, `uri_sink` params, `WatershedMesh` input-uri
fields). It is repetitive, easy to forget on a new template, and it duplicates a
decision the spec already encodes.

## Decision (settled semantics, docs/IDEAS.md 2026-08-13)

No boolean flag. The spec's RENDER DECLARATION is the intent:

- **Presence** (the source returns a renderable `LayerURI`) = the data has a
  visual form and WILL be surfaced WHEREVER fetched, in BOTH calling modes. The
  direct-chat tool-wrapper already honours this; the in-composer bare-function
  path was the gap.
- **Absence** (a `record` source, `layer_type=record`) = the data genuinely has
  no visual form (records/series) and nothing tries.
- `visualize=False` is a per-CALL belt-and-suspenders reserved ONLY for PROBE
  fetches of visualizable data (AOI candidate scans); using it on consumed data
  is re-hiding a layer (sweep-test policeable in S2).
- `purpose=` lets a composer contribute ONE word to the surfaced layer's name
  (a label, never a pathway) - e.g. `purpose="mesh bed"`.

This is pipeline-library brick 2: `load()` = fetch + declared-emit.

## The seam as landed (S1)

`server/.../fetchers/_router/emit_on_fetch.py::maybe_emit_input_on_fetch`, called
from `route()` immediately after a successful `LayerURI` build. It fires IFF:

1. an emitter is bound (`current_emitter()`; ambient, the same one composers use);
2. this is NOT the direct dispatch of the fetcher itself. A new
   `dispatched_tool_name()` contextvar (bound by `emit_tool_call` alongside
   `current_emitter`) discriminates: name == the fetcher -> direct chat, the
   wrapper emits, seam stays silent; name == a composer -> nested, seam surfaces.
   This is robust under `substep(...)` (which does not touch the contextvar);
3. `visualize` is not `False`;
4. the spec declares a renderable output (`layer_type in {raster, vector}`);
5. the uri has not already been surfaced this session (dedup on
   `emitter._emitted_input_uris`).

Provenance name: `Input: <what> (<source>[, <resolution>])` (+ the `purpose`
word). Role forced to `context`, bbox dropped (no competing zoom-to). Rasters
ride `publish_raster_input_cog` (registers the preset + returns a
plugin-renderable uri), vectors ride `publish_input_layer` (inline server-side).
Emission is BEST-EFFORT: a failure NEVER fails the fetch (logged once).

### The sync/async bridge (load-bearing)

`route()` is SYNCHRONOUS and a fetcher is frequently off-loaded to a worker
thread (`_ALWAYS_OFFLOAD_SYNC_TOOLS`), while the emit machinery is async. The
seam drives its coroutine onto the emitter's captured loop (`_bound_loop`,
captured by `emit_tool_call`):

- **Worker thread** (no running loop there; the loop is free, awaiting the
  `to_thread`): `run_coroutine_threadsafe(...).result()` - WAITED, so ordering +
  WS framing are preserved (the composer task is parked on the thread, no
  concurrent sink write). This is the common composer path.
- **Loop thread** (a composer that did not off-load): fire-and-forget task (a
  blocking wait would deadlock); runs the instant the sync stack unwinds, still
  before the long solve. A strong ref is kept.
- **No loop** (verify/CI/direct): run inline.

## Consequences

- New templates get input surfacing for FREE - fetch through the router and the
  declared-renderable input appears. No per-composer call site to remember.
- `visualize` / `purpose` are router-level kwargs (absorbed by the promoted
  signature's `**_extra_ignored`, popped in `route()` before validation/cache),
  so EVERY spec inherits them with zero schema change.
- S2 (the collapse) removes the now-redundant `_surface_*` helpers + call sites
  + uri-threading plumbing and adds the name-pattern + call-pattern SWEEP TEST
  (0232 style). Until S2 lands atomically the two paths coexist (uri-deduped).
- LOOSE ENDS (audit S3): the seam covers AGENT-SIDE router fetches. IN-WORKER
  bathymetry sampled inside a solver container never touches `route()` and stays
  on the bespoke "recorded-COG" surface (river_dye's `bed_bathymetry.tif` ->
  `publish_raster_input_cog` is the template). artemis (agitation) + tomawac
  (telemac/wave_field) do NOT yet emit an in-worker bed COG - a worker-image
  change (write + record `bed_cog`), scoped to the worker wave, not this landing.

## S2 - the atomic collapse (COMPLETE, 2026-08-13)

The transitional double-path is gone: the seam is now the SINGLE path by which a
router-fetched renderable input surfaces. Realized scope (net **-555 LOC** of
production code, +65 / -620 across 19 workflow files - the ~-550 estimate held):

- **Deleted the 4 seam-covered `_surface_*` helpers** + every call site:
  `_surface_landlab_dem_input` (`landlab/_composer_common.py`, lit all 13 Landlab
  templates via `stage_solve_download` + 3 self-staging templates),
  `_surface_watershed_mesh_inputs` + `_surface_landcover_input`
  (`telemac/rain_on_grid`), `_surface_river_geometry_input`
  (`telemac/river_dye`). The audit's "18 helpers" was the pre-count of call
  SITES; the realized helper-def count was 4 (each fanned out to several
  composers). `_surface_bed_bathymetry_input` (river_dye) SURVIVES - it rides an
  IN-WORKER bed COG the router seam cannot cover (kept + sweep-allowlisted).
- **Deleted the direct role=context input-emission call sites** that surfaced a
  router-fetched renderable: sfincs flood (DEM/topobathy + landcover + river),
  swmm urban_flood (DEM + buildings) + dual_drainage (DEM), elmfire fire_spread +
  spotting (fuels + DEM), geoclaw inundation (topo/bathy), schism tidal_hydro +
  pahm_surge (bathymetry), hecras flood_2d (DEM), openquake psha (fault traces -
  `fetch_fault_sources` returns a renderable `FaultSourcesResult`) + secondary_perils
  (DEM). Where a site carried semantic naming it moved to `purpose="<word>"` on
  the fetch (terrain / mesh bed / bathymetry / land cover / river geometry /
  topo-bathymetry / fuel model / fault sources), which the seam threads into the
  `Input: <word> (...)` provenance name.
- **Reverted the uri-threading plumbing** that existed ONLY to feed the deleted
  helpers: `WatershedMesh.dem_input_s3_uri` / `river_input_s3_uri` fields + the
  `uri_sink` params (`rain_on_grid/mesh_acquisition._resolve_bare_earth_dem`,
  `swmm/_fetch_dem_for_urban`, `openquake/secondary_perils/_fetch_dem_local` +
  `_covariates_for_sites`) + the `_fetch_bathymetry_cog` 3-tuple return (schism
  tidal/pahm) - all back to their natural shapes.

**KEPT (the seam does NOT cover these, sweep-allowlisted by site with a reason):**
mesh previews (generated, not fetched); computed/derived result COGs (openquake
liquefaction + landslide + GMF-spread, schism bottom-salinity, geoclaw particles,
pahm storm best-track); IN-WORKER COGs (river_dye bed, telemac3d stratified_flow
bottom, swan wave_field bathy); the bare-OSM agitation breakwaters (router-bypass,
an S3 loose end); user-data overlays (MODFLOW capture_zone observed wells +
thermal_plume injection well). river_dye's river fetch rides `emit_tool_call`
(which suppresses the seam by binding `_DISPATCHED_TOOL` and emits the layer
itself), so its river stays visible with no hand-surfacing.

**Behavior deltas NATE should know (intended per the settled semantics):**
- role NORMALIZES `input` -> `context` for surfaced inputs (the seam forces
  `context`); coverage (which inputs appear) is preserved.
- Composers that fetch MULTIPLE renderable bands now surface each (e.g. elmfire's
  5 LANDFIRE bands fbfm40/cbh/cbd/cc/ch, not just fbfm40) and composers that fetch
  a SEPARATE coarse delineation DEM surface it too - "the render declaration IS
  the intent." If a band/probe is genuinely not worth showing, the fix is a
  spec-level non-renderable declaration or `visualize=False` on a true probe fetch
  - NOT re-adding a hand-emission.

**Tests:** `test_input_layer_surfacing.py` collapsed to the emission PRIMITIVES +
the `make_fault_sources_layer_uri` util + the river_dye in-worker-bed worker-COG
pins + a two-part SWEEP (no `_surface_*input*` helper except the bed-COG one; every
remaining `publish_input_layer`/`publish_raster_input_cog` call is an allow-listed
non-seam-covered emission with a reason). `test_emit_on_fetch_equivalence.py` pins
per-family coverage for the three representatives (landlab / telemac rain_on_grid /
sfincs): each declares `purpose=` on its input fetches, and `input_layer_name`
maps that word to the same `Input: <word>` row the helper emitted. The seam
firing is pinned by `test_emit_on_fetch_seam.py`. Offline-green: 154 passing from
repo root across the emit trio + edited-engine slices; full workflows import (269
modules) clean; net-new failures = 0 (the `services`-PYTHONPATH + river_dye-NWM
failures are pre-existing baseline, cwd/offline-env, not this change).

The live flood canary (direct SFINCS/TELEMAC flood run + WS turn smoke through the
restarted daemon + a live Case's input layers surfaced VIA THE SEAM) + NATE's QGIS
visual are the final gate, per the NATE live-verification loop.
