# ADR 0231 - Input-layer parity: every fetched input that shapes a run surfaces as a Case layer

Status: Accepted
Date: 2026-08-12

## Context

NATE's ruling (2026-08-12, looking at the TELEMAC erodible-bed scour case, Snake
River nr Twin Falls ID): **"I want no hidden data layers - if there is a river bed
bathymetry I want it visualized."** Every fetched input that materially shapes a
run must surface as a `role="context"` (or `"input"`) Case layer the user can
spot-check in QGIS.

ADR 0227 built the reusable seam and adopted it for the bathymetry consumers
(schism surge/tidal, geoclaw inundation; sfincs_flood was the precedent):

- `publish_raster_input_cog(emitter, *, cog_uri, layer_id, name, style_preset,
  role="context", ...)` - rounds an EXISTING `s3://` COG through `publish_layer`
  and surfaces it (rides the object already in the runs/cache bucket; NO
  re-upload; provenance in the NAME; BEST-EFFORT, never raises / never fails the
  solve).
- `publish_input_layer(emitter, layer_uri, *, role)` - the vector twin (the
  `s3://` FlatGeobuf inlines server-side; forces role + strips the competing
  zoom-to).

This ADR is the parity SWEEP: audit EVERY registered template family for fetched
inputs that do NOT surface, and adopt the seam on every hidden row under the 0227
norms. The confirmed gap that motivated the ruling is the TELEMAC family (the
mesh derives from a 3DEP DEM + a fetched river geometry - neither surfaced).

### The load-bearing architectural distinction

A fetched input is surfaceable at the composer (server-side) ONLY when the fetch
runs agent-side (the emitter + the `s3://` object uri are both in scope). Some
engines fetch their terrain INSIDE the worker container (no emitter, no uri
returned) - those cannot surface without a worker-envelope change (the worker
uploads the COG + returns its uri) + an image rebuild. The inventory records this
per row, because it sets what "adopt now" vs "needs a worker seam" means.

## Inventory (template family x fetched input x verdict)

Legend: SURFACED = already a Case input layer; HIDDEN = fetched + discarded;
`role` is the surfaced role. "agent-side" = fetch runs in the composer (uri +
emitter in scope, surfaceable now); "in-worker" = fetched inside the solver
container (needs a worker-envelope change to surface). DERIVED artifacts (the mesh
itself, per-node CN/Manning grids) are covered by the mesh/result layers - the
SOURCE fetched input is the row that must surface.

| Family / template | Fetched input | Where fetched | Verdict (pre-0231) | 0231 action |
|---|---|---|---|---|
| **telemac** river_dye / do_sag (scour, erodible-bed) | river geometry (fetch_river_geometry) | agent-side | HIDDEN | **ADOPTED** role=context vector |
| telemac river_dye / do_sag | DEM bed (fetch_dem_bed, Copernicus/3DEP) | **in-worker** | HIDDEN | QUEUED - needs worker-envelope seam (worker uploads bed COG + returns uri) |
| **telemac** rain_on_grid | DEM bed (fetch_dem 3DEP 10 m) | agent-side (acquire_watershed_mesh) | HIDDEN | **ADOPTED** role=context raster |
| **telemac** rain_on_grid | river geometry (fetch_river_geometry nhdplus_hr) | agent-side | HIDDEN | **ADOPTED** role=context vector |
| telemac rain_on_grid | mesh, per-node CN2/Manning | derived | (mesh layer surfaced) | n/a - derivation, source DEM/landcover is the row |
| telemac rain_on_grid | landcover (fetch_landcover NLCD) | agent-side (node-CN sampling) | HIDDEN | QUEUED - agent-side raster, next seeding |
| **sfincs** flood (all modes) | DEM/topobathy, landcover, rivers | agent-side | SURFACED (role=input) | precedent (0227) - unchanged |
| sfincs numerical_physics | (rides flood builder) | agent-side | SURFACED | unchanged |
| **schism** pahm_surge, tidal_hydro | bathymetry (topobathy) | agent-side | SURFACED role=context (0227) | unchanged |
| **schism** baroclinic_circulation | bottom/bathy + mesh | agent-side | SURFACED role=context | unchanged |
| schism coupled_waves | bathy/mesh | agent-side | SURFACED role=context | unchanged |
| schism transport_validation | (canonical case, bundled) | n/a | n/a | n/a |
| **geoclaw** inundation | seamless topo/bathy DEM | agent-side | SURFACED role=context (0227) | unchanged |
| geoclaw storm_surge, gauge_timeseries | topo/bathy (fetch_topobathy->fetch_dem) | agent-side | HIDDEN | QUEUED - reuse the 0227 geoclaw bathy seam |
| geoclaw amr_regions, regional_manning, nid_dams | DEM / USACE dams | agent-side | HIDDEN | QUEUED (dams = vector; DEM = raster) |
| geoclaw thacker_validation | (analytic bowl) | n/a | n/a | n/a |
| **hecras** flood_2d | DEM (fetch_dem 3DEP 10 m) | agent-side | HIDDEN (mesh surfaced) | QUEUED - agent-side raster (dem_uri in scope) |
| hecras flood_2d (rain-on-grid) | river geometry | agent-side (hecras_build) | HIDDEN | QUEUED - reuse WatershedMesh uris |
| hecras riverine_flood, levee_breach | (bundled Muncie deck) | n/a | (mesh surfaced) | n/a - no fetched inputs |
| **modflow** capture_zone | pumping wells | agent-side | SURFACED role=context | unchanged |
| modflow capture_zone / river_seepage | DEM, river geometry | agent-side | HIDDEN | QUEUED - DEM raster + river vector |
| modflow (archetypes: asr, mine_dewatering, saltwater_intrusion, sustainable_yield, managed_recharge, regional_water_budget, wellhead_protection, wetland_hydroperiod, contaminant_plume) | DEM, soilgrids, USGS gw levels, wells | agent-side | HIDDEN (wells surfaced where present) | QUEUED - wells vector + DEM raster + gauge vector |
| modflow vadose_transport | soil (fetch_soilgrids) | agent-side | HIDDEN | QUEUED - raster |
| **swmm** urban_flood | building footprints | agent-side | SURFACED role=input | unchanged |
| swmm urban_flood, dual_drainage | DEM (fetch_dem 10 m) | agent-side | HIDDEN (mesh surfaced) | QUEUED - agent-side raster |
| swmm network_import | published deck (fetch_published_deck) | agent-side | HIDDEN | QUEUED - deck geometry vector |
| swmm (deck_* + comparison templates) | (bundled/synthetic decks) | n/a | n/a | n/a - no fetched terrain |
| **openquake** psha | fault sources (fetch_fault_sources) | agent-side | SURFACED role=input | unchanged |
| openquake psha, scenario_gmf, secondary_perils | DEM (fetch_copernicus_dem for vs30/slope) | agent-side | HIDDEN | QUEUED - site-model DEM raster |
| openquake scenario_gmf, secondary_perils, event_based, disaggregation | fault sources | agent-side | HIDDEN (psha only surfaces) | QUEUED - fault vector via make_fault_sources_layer_uri |
| **landlab** (all 13: susceptibility, channel_incision, chi_map, dem_conditioning, flow_accumulation, green_ampt, groundwater x2, hacks_law, hand_wetness, lake_mapping, landslide_storm_ensemble, overland_flow_timeseries, storm_sequence) | DEM (fetch_3dep_extra->fetch_dem) | agent-side (_composer_common) | HIDDEN | QUEUED - ONE adoption in _composer_common lights all 13 |
| **swan** wave_field, physics_sensitivity_sweep, stationary_snapshot_batch | topobathy (fetch_topobathy / fetch_swan_dem_once) | agent-side | HIDDEN | QUEUED - reuse 0227 bathy seam |
| **pahm** / storm forcing (schism, geoclaw) | storm tracks (fetch_storm_tracks) | agent-side | SURFACED role=input (best-track overlay) | unchanged |
| **pelicun** (all templates) | (consumes hazard from other engines; no terrain fetch) | n/a | n/a | n/a |
| **elmfire** fire_spread, crown, initial_attack, sensitivity, transient | LANDFIRE fuels, DEM (fetch_landfire_fuels, fetch_dem) | agent-side (run_elmfire) | HIDDEN | QUEUED - fuels raster + DEM raster |
| elmfire verification | (canonical case) | n/a | n/a | n/a |

## Decision

Adopt the 0227 seam on the hidden rows under the SAME norms:

- **role="context"**: an input renders non-intrusively beneath the primary result
  with no competing zoom-to (the schism/mesh convention on these same templates).
- **provenance in the NAME**: source + native resolution is the spot-check fact
  (`Input: DEM bed (<place>, USGS 3DEP bare-earth 10 m)`, `Input: river geometry
  (<place>, NHDPlus HR flowlines)`).
- **ride the existing object**: no re-upload (the DEM/flowline COG already lives in
  the runs / cache bucket).
- **BEST-EFFORT**: `publish_raster_input_cog` / `publish_input_layer` never raise;
  a failure to surface an input can never fail the solve.
- **cannot-silently-drop**: a parameterized offline test asserts a valid input
  LayerURI actually reaches the emitter (role=context, correct preset, provenance
  name) per adopted family (the 0217 track-overlay lesson).

### This wave (TELEMAC FIRST, landed + tested)

1. `telemac_river_dye` (and `telemac_do_sag`, which routes through the shared
   `model_telemac_river_dye`): surface the fetched river flowline as a role=context
   vector (`_surface_river_geometry_input`, agent-side). This is the erodible-bed
   scour case NATE is looking at.
2. `telemac_rain_on_grid`: `acquire_watershed_mesh` now threads the fetched DEM
   bed `s3://` COG uri + river flowline uri up through `WatershedMesh`
   (`dem_input_s3_uri` / `river_input_s3_uri`, populated via a non-breaking
   `uri_sink` on `_resolve_bare_earth_dem`); the composer surfaces both as
   role=context inputs (`_surface_watershed_mesh_inputs`).

### Queued (next seeding picks it up - the 0227 precedent)

Every row marked QUEUED is agent-side surfaceable with the SAME two calls; each
family's next seeding adopts it with an offline emitter test. The single exception
is the `river_dye` / `do_sag` **in-worker DEM bed**: it needs the worker to upload
its sampled bed COG and return the uri in the result envelope (a worker-envelope
change + telemac image rebuild), tracked separately so the erodible-bed scour case
gets its bed bathymetry surfaced end-to-end.

## Consequences

- The TELEMAC family stops hiding its channel geometry: re-seeding river_dye /
  do_sag / rain_on_grid surfaces the river flowline (and, for rain_on_grid, the
  DEM bed) as provenance-named role=context Case layers in QGIS.
- No new upload cost (rides the fetched object); no registry change (no new
  tools); no LayerURI schema change (existing shapes).
- The inventory above is the audit map: it names every remaining hidden row and
  whether it is agent-side (adopt now) or in-worker (needs a worker seam).

## Rejected alternatives

- Surface as `role="input"` (the flood default): chose `role="context"` to match
  the sibling mesh/bathy overlays on these templates - a terrain/geometry backdrop,
  not a competing answer.
- Double-fetch the DEM agent-side to surface river_dye's in-worker bed: rejected -
  it would diverge from the bed the worker actually sampled; the honest path is the
  worker returning its own object uri.
- A bespoke river/DEM colormap: rejected - `osm_waterways` / `nhdplus_flowlines` /
  `continuous_dem` already exist and are the presets the fetchers stamp.

## COMPLETE (2026-08-12 - the parity sweep landed)

The audit wave landed TELEMAC agent-side surfacing (river_dye/do_sag river
flowline + rain_on_grid DEM/river). This completion wave landed EVERYTHING ELSE:
the in-worker bed seam NATE explicitly named, plus every remaining agent-side
QUEUED row, each with a parameterized cannot-silently-drop test.

### The in-worker bed seam (the row NATE named - river bed bathymetry)

The `river_dye`/`do_sag` bed is sampled + fitted INSIDE the worker
(`telemac_river_dye_build.fetch_dem_bed`), so it cannot surface agent-side. The
worker-envelope seam:

- `write_bed_cog()` rasterizes the solved per-node bed onto a small EPSG:4326 COG
  (`bed_bathymetry.tif`, long side capped at 512 px, griddata-linear clipped to
  the channel), written into the run prefix the supervisor already uploads from
  (added to `DEFAULT_OUTPUTS`).
- the run records `bed_cog` / `bed_cog_min_m` / `bed_cog_max_m` / `bed_cog_source`
  in `telemac_metrics.json` (the result envelope).
- the strict spec-parser stamp bumped `telemac-reach-6` -> `telemac-reach-7`
  (the version doubles as the worker-image/behavior provenance marker per the
  image-staleness law) + the rejection test now names v7.
- the composer's `_surface_bed_bathymetry_input` reads the key, builds the
  runs-bucket uri, and rounds it through `publish_raster_input_cog`
  (`Input: river bed bathymetry (<reach>, <source>-sampled, in-worker)`,
  role=context, continuous_dem).
- telemac worker image REBUILT (absolute -f/context; in-image import + strict-
  field gate green) + a behavior-proving smoke THROUGH the image: the bed COG
  lands EPSG:4326, 1385 finite px, range ~4.99 m (non-constant), min/max real.

This generalizes: any future in-worker fetch surfaces the same way (worker writes
COG + records key -> composer emits via the existing raster-input seam).

### Final inventory disposition (every row SURFACED, inherited, or verdict)

| Family / row | Disposition |
|---|---|
| telemac river_dye/do_sag river geometry | SURFACED (audit wave) |
| **telemac river_dye/do_sag in-worker DEM bed** | **SURFACED (this wave - worker-envelope seam + image rebuild + smoke)** |
| telemac rain_on_grid DEM + river | SURFACED (audit wave) |
| **telemac rain_on_grid landcover (NLCD)** | **SURFACED (this wave - `_surface_landcover_input`, categorical)** |
| **landlab (all 13) DEM** | **SURFACED (this wave - `_surface_landlab_dem_input`: ONE point in `stage_solve_download` lights 9 via the shared stage; 3 self-staging templates (flow_accumulation, green_ampt, susceptibility) wired at their own stage; storm_sequence rides the shared point)** |
| **swan wave_field / physics_sensitivity_sweep / stationary_snapshot_batch topobathy** | **SURFACED (this wave - one adoption in `model_swan_wave_field` lights all 3)** |
| **hecras flood_2d DEM** | **SURFACED (this wave - `_fetch_dem_local` returns the s3 uri; composer surfaces continuous_dem)** |
| **swmm urban_flood + dual_drainage DEM** | **SURFACED (this wave - `_fetch_dem_for_urban` uri_sink; both flood composers surface the terrain, urban_flood beside the buildings input)** |
| **elmfire fire_spread LANDFIRE fuels + DEM** | **SURFACED (this wave - the deck-input s3 uris (`fbfm40`, `dem`) surfaced directly)** |
| **openquake secondary_perils DEM (vs30/slope/CTI)** | **SURFACED (this wave - `_fetch_dem_local` uri_sink threaded through `_covariates_for_sites`)** |
| sfincs (all) DEM/landcover/rivers | SURFACED (0227 precedent) |
| schism surge/tidal/baroclinic/waves bathy | SURFACED (0227) |
| geoclaw inundation topo/bathy | SURFACED (0227) |
| geoclaw storm_surge / gauge_timeseries / amr_regions / regional_manning DEM+bathy | SURFACED (INHERITED - all delegate to `model_geoclaw_inundation`, which carries the 0227 bathy seam unconditionally) |
| openquake psha fault sources | SURFACED (precedent) |
| modflow capture_zone/archetypes pumping wells | SURFACED (precedent - `_build_used_wells_layer`) |
| swmm urban_flood building footprints | SURFACED (precedent) |
| pahm/geoclaw storm tracks | SURFACED (precedent - best-track overlay) |
| **geoclaw nid_dams (USACE dams vector)** | **DEFERRED (verdict): the only emit point is inside `inundation/inundation.py` (the sole caller of `resolve_nid_dam`), frozen by the concurrent Cascadia harvest wave; a one-line `publish_input_layer` there lands it once that file frees. The dam-break DEM already surfaces via the inherited 0227 bathy seam.** |
| **openquake scenario_gmf / event_based / disaggregation fault sources** | **DEFERRED (verdict): the rupture is placed on a single chosen fault TRACE by a sync selection helper (`resolve_scenario_rupture`) that returns a trace, not a layer uri; surfacing the full GEM fault set needs `make_fault_sources_layer_uri` wiring per template - a focused openquake seeding, not this terrain sweep.** |
| **modflow archetype DEM / river / soilgrids / USGS gw-levels** | **DEFERRED (verdict): consumed as SCALARS (regional gradient / recharge) deep inside sync helpers across 11 archetype dirs (`capture_zone` fetches DEM/soilgrids/river/gw and derives a gradient); each needs a uri-sink replumb. Wells already surfaced. A focused modflow seeding lands these without destabilizing the shared archetype composer.** |
| swmm network_import network deck | n/a: the user-supplied network geometry (nodes/conduits) IS the primary rendered result layer, not a hidden input; no `fetch_published_deck` terrain is fetched on this path. |
| hecras flood_2d rain-on-grid river geometry | DEFERRED (verdict): the secondary rog channel path (`acquire_channel_inputs`); the base flood_2d DEM (the dominant input) is surfaced this wave. |
| geoclaw thacker / schism transport / hecras Muncie / elmfire verification / pelicun / swmm deck_* | n/a - bundled/analytic/synthetic, no fetched terrain |

Norms held on every landed row: role="context", provenance in the name, ride the
existing object (no re-upload), best-effort (never fails the solve), a
parameterized cannot-silently-drop test per adopted family.

## Amendment 2026-08-13 -- ARTEMIS surveyed breakwater surfaced (ADR 0237 real-marina)

| **artemis_harbor_agitation real-bathy diffraction (OSM breakwater)** | **SURFACED: the surveyed breakwater geometry is fetched AGENT-SIDE (composer OSM man_made=breakwater auto-fetch), staged as a `role="context"` FlatGeobuf, and surfaced via `publish_input_layer` (best-effort, never fatal). The NOAA lake bathymetry is fetched INSIDE the worker (no agent-side uri) -> a 0231 WORKER-SEAM RESIDUAL (surfacing it needs the worker to upload the sampled bed COG + return its uri + an image rebuild), recorded per the load-bearing distinction above.** |
