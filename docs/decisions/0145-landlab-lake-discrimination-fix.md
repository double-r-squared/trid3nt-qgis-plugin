# 0145: Landlab lake discrimination fix + overland conditioning opt-in

Date: 2026-08-05
Status: landed

## Context

NATE spot-checked the ADR 0141 proofs and caught that `landlab_lake_mapping`
emitted exactly the `landlab_dem_conditioning` fill field restyled - every
DEM noise pit counted as a "lake" (45 "lakes" strung along a canyon
streamline in an AOI with no real lakes). A second flag on overland-flow
water placement was withdrawn after a gradient-relief render showed the
placement correct - the imagery basemap had made elevation unreadable.

## Decision

`_run_lake_mapping` discriminates per-lake with two floors -
`min_lake_depth_m` (default 1.0) and `min_lake_area_m2` (default 10000) -
dropping lakes that fail either from the field, extent, vector, and counts,
and reporting `n_lakes_raw` vs `n_lakes_kept` loudly. Semantic honesty made
explicit at the source: the method detects topographic closed basins
(potential impoundments); existing water surfaces are flat in the DEM and
are NOT detected - `fetch_nhd_waterbodies` is the source for those.
`landlab_overland_flow_timeseries` keeps its validated raw-DEM default;
`condition_dem` (default False) is an opt-in fill lever with honest
conditioning facts in the emission.

## Consequence

Boulder foothills smoke: 45 mapped -> 1 kept (a real ~20.6 m closed basin).
Horsetooth Reservoir smoke: 215 mapped -> 7 kept small basins; the
reservoir pool itself is invisible to fill-depth by construction (flat
surface), which the docstring now states. NHD geometric corroboration is a
recorded follow-on. No registry change (behavior fix on existing tools).
