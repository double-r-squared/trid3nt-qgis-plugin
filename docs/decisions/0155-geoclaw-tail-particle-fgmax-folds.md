# 0155: GeoClaw CAND-S tail - Lagrangian particle + onshore-fgmax knob folds

Date: 2026-08-05
Status: landed

## Context

The five remaining GeoClaw CAND-S rows on the module-coverage board. ADR 0143
already ground-truthed all five against the installed clawpack 5.14.0; ADR 0155
EXECUTES those verdicts (no re-litigation):

- Three are bookkeeping-only (the build verdict was already decided):
  - `fgmax_island_runup_maxima` -> COVERED. The max-wave-height + first-arrival
    mechanism is already live (setrun emits fgmax point_style=2 num_fgmax_val=2;
    postprocess.read_fgmax_output surfaces max_depth_m / max_inundation_m /
    arrival_time_s on every tsunami/surge run). Flipped to LANDED.
  - `okada_1d_dtopo_smoke_case` -> STOP (2D-only worker; a 1D run needs its own
    Makefile/num_dim=1 setrun/entrypoint branch). Retagged CAND-S -> CAND-M with
    the recipe inline.
  - `wind_drag_law_selection` -> STOP (the surge/wind module is absent; drag_law
    is inert without wind_forcing + a storm file, inseparable from a parametric
    Holland storm module). Retagged CAND-S -> CAND-M with the recipe inline.

- Two are real builds, landed here as KNOB FOLDS on the existing GeoClaw surface
  (no new tool -- the "prefer the fold" directive; folds do not bump pins):
  - `lagrangian_particle_gauges_wake_tracking` (folds its plotting sibling too).
  - `fgmax_dem_masked_grid`.

## Decision

### 1. Lagrangian particle gauges (`lagrangian_particles` knob)

`GeoClawRunArgs.lagrangian_particles: list[(lon,lat)]` -> build_spec ->
setrun_builder appends one gauge per seed point (ids 100+) with a PER-GAUGE
`rundata.gaugedata.gtype = {id: 'lagrangian'}` dict (the stationary coastal gauge
id 1 stays 'stationary'). GeoClaw then advects each particle by the depth-averaged
velocity and writes its position x(t),y(t) into the q[2,3] columns (verified
against gauges_module.f90: the "# Lagrangian particle" header + xg,yg replacing
hu,hv). Agent-side postprocess gains `parse_geoclaw_particle_tracks` (a pure,
clawpack-free reader of the `# Lagrangian particle` gauge files) +
`build_geoclaw_particle_track_geojson` (one LineString per drifter -> a
`particle_track` vector PRODUCT layer, particles.geojson) +
`build_particle_track_chart_spec` (the plotting sibling: cumulative drift distance
vs time, one series per particle). Track scalars (count / max length / duration)
ride the peak `GeoClawDepthLayerURI`. Surfaced as the `lagrangian_particles`
param on `geoclaw_inundation`, which sets `emit_particle_tracks=True`.

### 2. Onshore fgmax mask (`fgmax_mask` knob)

`GeoClawRunArgs.fgmax_mask: 'full'|'onshore'` (default 'full', byte-identical to
the prior point_style=2 grid, test-locked). On 'onshore' the setrun emits
`fg.point_style=4` + `fg.xy_fname='fgmax_mask.tt3'`, and a NEW entrypoint step
(`_generate_fgmax_mask`) writes that topotype-3 mask over the AOI at the SAME
geometry as the full fgmax grid (shared `setrun_builder.fgmax_grid_geom`, dx
locked to the emitted point_style=2 dx_fine by a unit test): Z=1 where the DEM
topography is above `sea_level_m` (onshore), Z=0 elsewhere. Pure-numpy
nearest-neighbour sampling of the (regular) topotype-3 topo (no scipy dep added).
The masked points are a strict subset of the full grid, so common-cell maxima
match; the fgmax OUTPUT (fgmax0001.txt, 9-col) is unchanged in format (fewer
rows), so read_fgmax_output consumes it unchanged. An all-offshore mask (zero
cells) raises the typed `GEOCLAW_FGMAX_MASK_FAILED` gate rather than a silent
empty fgmax.

Both folds are additive no-ops when unset: every prior GeoClaw deck renders
byte-identical (locked by test_no_lagrangian_particles_is_byte_identical_default
+ test_fgmax_mask_full_is_byte_identical_point_style_2).

## Live smoke (local-docker, rebuilt image, Crescent City CA)

Two tsunami Mw9 solves over the Crescent City harbour AOI (amr_levels=3, 900 s),
both real (max_depth 1.326 m, genuine coastal inundation):

- Lagrangian particles (3 drifters seeded in the harbour): 3 tracks recorded,
  1105 samples each over 899 s, cumulative drift 241 / 258 / 125 m. The drift
  accelerates when the wave reaches the harbour (~500 s) -- physically sensible.
  particles.geojson (3 LineStrings) + the cumulative-drift chart emitted; peak
  layer carries particle_track_count=3, max_track_length_m=258.2, duration_s=899.
  Proofs: docs/proof/templates/geoclaw_lagrangian_particles{,_chart}.png.
- Onshore fgmax mask: fgmax output points full=21460 vs onshore=13080 (39.0%
  fewer output points -- the size win) and the max ON-LAND depth agrees exactly
  (onshore max_land_h = full max_land_h = 1.41 m; onshore is a strict land subset
  of the full grid). The mask is built from the FINEST staged topo (finest-wins
  layering) so its coastline matches the topo GeoClaw actually solves on -- an
  earlier coarse-primary-topo mask misclassified the nearshore and disagreed.

## Consequence

Registry 216 -> 216, door-dissolution EXPECTED_TEMPLATES 58 -> 58 (both folds,
NO new tools). setrun_builder + entrypoint (container-executed) touched -> the
`trid3nt-local/geoclaw:latest` image was REBUILT (ADR 0148 worker-image law;
clawpack layers cached, only the worker COPY changed) and both live smokes ran
THROUGH it. The board's five CAND-S GeoClaw rows are now resolved: 2 LANDED, 1
LANDED (already-covered mechanism named), 2 retagged CAND-M with recipes. The 1D
solver path and the storm-surge/wind module remain named, recipe-backed CAND-M
gaps for future waves.
