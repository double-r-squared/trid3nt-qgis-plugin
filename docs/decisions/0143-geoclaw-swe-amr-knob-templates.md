# 0143: GeoClaw SWE+AMR knob templates - 2 landed, 6 triaged

Date: 2026-08-05
Status: landed

## Context

Third per-engine grind batch: the eight GeoClaw CAND-S board rows. Triage
against the installed clawpack 5.14.0 setrun surface (introspected inside
the `trid3nt-local/geoclaw` image) sorted the eight into deck-knob
landings, already-covered mechanisms, and rows needing their own build or
deck machinery.

## Decision

Land the two rock-solid SWE+AMR deck knobs as capability-named templates
riding the existing inundation composer (`model_geoclaw_inundation`); the
setrun_builder gains additive, back-compat-preserving emission for both:

- `geoclaw_amr_refinement_regions` (row `region_based_amr_refinement_windows`)
  - explicit lat/lon/time AMR windows. A new `AmrRegionWindow` contract
  type -> `GeoClawRunArgs.amr_regions` -> build_spec -> setrun
  `regiondata.regions.append(...)` AFTER the engine default tiers (GeoClaw
  combines overlapping regions by MAX of covering min/max levels).
  `flag_richardson=False` + `flag2refine=True` are already emitted, so the
  window list is the "region-based over error flagging" answer.
- `geoclaw_regional_manning_friction` (row `manning_friction_by_region`) -
  spatially-varying (banded) Manning n. `GeoClawRunArgs.manning_coefficients`
  (list) + `manning_break` (ascending elevation breaks, len N-1) -> setrun
  `geo_data.manning_coefficient = [...]` + `geo_data.manning_break = [...]`.
  Verified against 5.14.0 (manning_coefficient is already a list,
  manning_break present). Absent -> the single scalar `manning_n` path is
  byte-identical.

Both were smoke-tested live against the local-docker solver on a thin
`geoclaw:knobs-test` image (FROM the base + COPY the updated worker; avoids
the long clawpack sdist rebuild) over a real US coastal AOI (Crescent City,
CA). Both produced genuine solves: initial water mass ~6.5e10 (a real ocean
column, not the dry ~1e5 failure mode), 6 fort.q frames, a live fgmax grid,
a coastal gauge waveform (amplitude ~0.6 m), and a peak-inundation depth COG
(0.73 / 0.76 m). Proofs (Esri-basemap map + plugin-dock gauge chart):
`docs/proof/templates/{amr_regions,regional_manning}{,_chart}.png`.

## Triage of the other six

- `okada_1d_dtopo_smoke_case` - STOP. Worker is 2D-only; the 1d_classic
  Fortran is in the image but a 1D run needs its own Makefile / num_dim=1
  setrun / 1D topo+dtopo / entrypoint branch. Disproportionate for a smoke.
- `fgmax_island_runup_maxima` - COVERED. The max-wave-height + first-arrival
  mechanism is already live (setrun emits fgmax point_style=2 num_fgmax_val=2;
  postprocess surfaces max_depth_m / max_inundation_m / arrival_time_s). Only
  user-settable time-window knobs would be new.
- `fgmax_dem_masked_grid` - DEFER. point_style=4 is supported in 5.14.0 but
  needs an entrypoint step to derive a topotype-3 DEM mask + fg.xy_fname.
- `lagrangian_particle_gauges_wake_tracking` + `lagrangian_gauge_output_and_plotting`
  - DEFER-LANDABLE (natural 6+7 fold). Machinery verified present
  (GaugeData.gtype accepts 'lagrangian'; gauges_module.f90 + particle_tools.py
  compiled in). Needs a new postprocess particle-track (xg,yg) reader + its
  own smoke; kept out of this batch to hold it to the two clean deck knobs.
- `wind_drag_law_selection` - STOP. The storm-surge/wind module is absent
  from the surfaced deck (the "surge" scenario is a v0.1 sea-level-offset
  stub with no surge_data block, no wind_forcing, no storm file). drag_law
  scales the wind-stress term and is inert without wind forcing; landing it
  is inseparable from landing the parametric-Holland storm module.

Full recipes for each deferred/STOP row are recorded inline in
`docs/validation/module-coverage-board.md`.

## Consequence

Registry 202 -> 204 (both new templates); door-dissolution EXPECTED_TEMPLATES
44 -> 46. setrun_builder, GeoClawRunArgs, and build_geoclaw_build_spec grew
three additive fields (`amr_regions`, `manning_coefficients`, `manning_break`)
that are no-ops when unset, so every prior GeoClaw deck renders byte-identical.
The storm-surge/wind module and a 1D solver path are named, recipe-backed gaps
for future waves.
