# ADR 0180 - Layer-emission audit: emitted layers are georeferenced map citizens

Date: 2026-08-07
Status: accepted

## Context

NATE norm (docs/IDEAS.md "Layer-emission audit", 2026-08-06 + the template
proof-process rule): an emitted layer is a map-app citizen. A template whose
deliverable is a chart or a scalar must NOT drop an orphan non-georeferenced
raster onto the QGIS map -- a COG built in a fixture's local units (or placed at
null-island) cannot honestly sit on the basemap. The IDEAS entry named three
suspect classes to sweep:

  1. `modflow_package_validation` cases (synthetic local-unit MF6 benchmarks) --
     do they emit rasters?
  2. `schism_transport_validation` (idealized QuarterAnnulus, local units).
  3. any validation-fixture template publishing local-coordinate COGs.

The prescribed fix per orphan: (a) drop the layer emission in favor of the
chart/scalars, or (b) give it a real-AOI georeferenced mode where that is cheap
and honest.

This ADR is the audit of all 68 registered engine templates
(`EXPECTED_TEMPLATES`, `test_door_dissolution.py`) plus the shared emitters they
delegate to.

## Finding

**No strict orphan emission exists. The norm is already systematically upheld
across all 68 templates.** Every template resolves to exactly one of two honest
shapes, and the two named suspects were already authored chart/scalar-only.

- **Chart/scalar-only templates emit NO map layer.** The validation / comparison
  / sensitivity / published-schematic-deck templates emit charts + typed scalars
  and call no layer-publishing seam (`publish_layer`, `register_manifest_layers`,
  `emit_layer_uri`, `emit_frame_layers`, `publish_quantities`). Ten of them carry
  an explicit chart-only docstring ("there is no map layer" / "NO georeferenced
  map layer" / "never a map"); prior authors had already baked the norm in.

- **Map-emitting templates are real-AOI georeferenced.** Every raster/vector
  emitter derives its CRS + placement from a real AOI: a user `bbox` /
  `location` (geocoded) / `aoi_latlon`, a real coastal `coastal_transect_latlon`,
  a fetched DEM's projection, or the shipped HEC-RAS Muncie deck's real Indiana
  State-Plane projection. All COGs are warped to EPSG:4326 via
  `cog_io.write_cog_4326_from_grid` from that real source CRS; all vectors carry
  a real 4326 bbox.

### Disposition table (by category)

| category | templates | emits | georeferenced? | disposition |
| --- | --- | --- | --- | --- |
| Fixture / analytic validation | modflow_package_validation, schism_transport_validation, pelicun_closed_form_validation, pelicun_mixed_fragility_loss_assessment, pelicun_replacement_threshold_override_sweep, pelicun_flood_foundation_depth_damage_sweep, pelicun_hazus_eq_version_comparison, pelicun_hazus_seismic_dl_run | chart + scalars | n/a (no map layer) | NO CHANGE - already compliant (the two IDEAS-named suspects included) |
| Comparison / sensitivity / sweep | swmm_subcatchment_runoff_comparison, swmm_node_hydraulics_comparison, swmm_wetwell_pump_control_comparison, swmm_lid_performance_comparison, swmm_wq_buildup_washoff_comparison, swan_physics_sensitivity_sweep | chart + scalars | n/a (no map layer) | NO CHANGE - already compliant |
| Published schematic deck (SWMM) | swmm_lid_raingarden_wq, swmm_wwtp_detention_ponds, swmm_pump_pid_rtc | chart + scalars | n/a (schematic .inp coords, no map layer by design) | NO CHANGE - already compliant |
| Chart-deliverable at a real reach/gauge | telemac_do_sag, geoclaw_gauge_timeseries, geoclaw_amr_regions (region-control diag) | chart + scalars | real AOI (no orphan raster) | NO CHANGE |
| Real-AOI raster/field emitters | sfincs_flood, sfincs_advanced_numerical_physics_knobs, swmm_urban_flood, swmm_dual_drainage, telemac_river_dye, hecras_riverine_flood, hecras_levee_breach, hecras_flood_2d, schism_tidal_hydro, schism_coupled_waves, swan_wave_field, swan_stationary_snapshot_batch, geoclaw_inundation, geoclaw_regional_manning, geoclaw_storm_surge, elmfire_fire_spread (+ elmfire sensitivity/transient/crown variants), landlab_* (susceptibility, flow_accumulation, green_ampt, dem_conditioning, lake_mapping, hand_wetness, hacks_law, landslide_storm_ensemble, overland_flow_timeseries), openquake_psha, openquake_scenario_gmf, openquake_secondary_perils, modflow_* (asr, capture_zone, contaminant_plume, managed_recharge, mine_dewatering, regional_water_budget, river_seepage, saltwater_intrusion, sustainable_yield, wellhead_protection, wetland_hydroperiod) | raster COG / vector / mesh (+ frames) | YES - real bbox / latlon / geocode / deck projection / transect | NO CHANGE - georeferenced (option-b already satisfied) |
| Real-AOI vector at a real transect | modflow_saltwater_intrusion | FlatGeobuf transect + toe point + cross-section chart | YES - real `coastal_transect_latlon` (honesty gate requires it) | NO CHANGE - the Henry cross-section is a chart; the MAP layer is the real transect line |
| Real buildings/bbox damage | pelicun_damage_assessment | building-damage vector | YES - real footprints / building-density grid over a real bbox | NO CHANGE |

(`swmm_network_import` imports a real georeferenced GIS storm-drain network;
`swmm_mechanism_compare` is the shared chart-only comparison base.)

### Two adjacent findings (NOT strict orphans; documented, not fixed here)

1. **`elmfire_verification_elliptical_replication`** publishes an idealized
   constant-fuel time-of-arrival COG at a FIXED demo center
   (`_VERIFICATION_CENTER_LON/LAT = -98.5/38.5`, rural Kansas). It is
   georeferenced (real US center, EPSG:5070), so it is NOT a strict orphan --
   option (b) is technically satisfied. But its true deliverable is the
   RMSE/correlation/`passed` scalars + the numerical-vs-Richards ellipse-overlay
   chart; the georeferenced-but-synthetic fire at an arbitrary place is a
   borderline "honest map citizen?" case. It is NOT one of the IDEAS-named
   local-unit suspects (it lands at real coords, not local units), and dropping
   the layer is a return-type/contract change (`ElmfireEllipseVerificationLayerURI`).
   FLAGGED for NATE to rule on (option-a drop vs keep); left unchanged pending
   that call.

2. **`postprocess_modflow._write_reprojected_cog` identity-transform fallback.**
   When the deck cannot be loaded (`_grid_georegistration_from_deck` returns
   `None` on a flopy load failure), the COG path SILENTLY falls back to
   `rasterio.Affine.identity()` -- a null-island placement -- rather than raising.
   This is the one genuinely norm-adjacent code path (a raster that "cannot
   honestly sit on the map"), and it is INCONSISTENT with the mesh path in the
   same module: `modflow_mesh._emit_*` already SKIPS (returns `None`) on
   `geo is None` rather than emitting a mis-placed mesh. Recommend hardening the
   COG path to a loud typed error (or the same skip convention) in a dedicated
   modflow-hardening job -- deferred here because it is a real-AOI-template error
   path (out of the fixture-orphan mission scope) and a shared-postprocess
   behavior change across 11 modflow templates warrants the MODFLOW engine canary
   (Docker solver), not a cheap offline smoke.

## Decision

No template code changes. The audit confirms the norm is already upheld: the two
IDEAS-named suspects (`modflow_package_validation`, `schism_transport_validation`)
and every fixture/validation/comparison template are chart/scalar-only with no
map layer; no local-unit / null-island orphan COG is emitted anywhere; every
map-emitting template is real-AOI georeferenced. The two adjacent findings above
are registered as scoped follow-ups (IDEAS.md), NOT chased in this job.

## Consequence

- The norm now has an authoritative, template-by-template audit of record; a
  future template landing has a clear rule: a chart/scalar deliverable emits NO
  raster, and any emitted layer derives its CRS + placement from a real AOI.
- No showcase Case (ADR 0175) behaves differently: the seeded
  `modflow_package_validation` Cases already carried only charts (never an orphan
  layer), so no reseed is warranted (forward-only per NATE).
- Two follow-ups are on the books for NATE's call: the elmfire-verification
  idealized-at-real-place layer, and the modflow COG identity-transform fallback.
