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
