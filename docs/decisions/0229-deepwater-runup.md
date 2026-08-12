# ADR 0229 - Deep-water rung: the ETOPO full column survives the 3DEP land ocean-fill on a rupture-scale topobathy fetch

Status: Accepted
Date: 2026-08-12

Cross-links: ADR 0226 (the finite-fault Okada tsunami source whose run-up leg this
unblocks), ADR 0227 (the fetched bathymetry surfaced as a Case input layer), ADR
0224/0225 (the resolution doctrine + declared native cells this honours), ADR 0110
(the topobathy library_delegate fold this edits).

## Context

ADR 0226 landed the measured-inversion finite-fault Okada tsunami source (USGS
ComCat event -> `finite-fault` product -> N-subfault `CSVFault` dtopo). Its scoped
follow-up: the RUN-UP solve for the real 2021 M8.2 Chignik event
(`ak0219neiszm`) was refused by the worker's `GEOCLAW_BATHYMETRY_FLAT` guard --
the topobathy fetched over the ~6.5-deg rupture-enclosing Alaska domain came back
**land-only** (raw min -68 m, 0 % of cells below -5 m), so no genuine ocean column
reached the solver and the guard correctly refused a doomed dry solve.

### Root cause (measured, not guessed)

`fetch_topobathy` composites its sources LAST-wins in the precedence order
`ETOPO global base -> 3DEP land -> CUDEM 1/9" -> NCEI regional`
(`topobathy.py._select_and_merge` -> `_composite_sources_to_array`). For the
Chignik domain `(-160.8, 53.862, -154.293, 57.022)`:

- The ETOPO 2022 15" base leg alone returns a genuine deep column: **min -6438 m,
  87 % of cells below -5 m** (verified by reading the single covering tile
  `ETOPO_2022_v1_15s_N60W165_surface.tif`). ETOPO was never the problem.
- The **3DEP land leg** (`fetch_dem`, reused via `_fetch_3dep_land_to_file`)
  returns, over this Alaska ocean box, a **fully-filled** raster (finite fraction
  1.0) whose **83.5 % of cells are exactly 0.0 m** -- a flat sea-level FILL over
  open water (3DEP is a bare-earth LAND product; it has no bathymetry).
- Because the 3DEP land leg sits at **higher composite precedence than ETOPO**,
  that flat 0 m ocean fill **CLOBBERS** the ETOPO deep column across the entire
  ocean. The merged surface collapses to min -68 m, 0 % below -5 m -- land-only.

So the refusal was a real, correct guard firing on a genuine composite defect: a
LAND DEM's ocean fill overwriting the real bathymetry. It was NOT an ETOPO
clip/coverage gap, and NOT a finite-fault defect.

The existing `skip_land` param already fixes this for the SCREENING surge path
(SCHISM passes `skip_land=True`, dropping the land leg entirely), but the GeoClaw
tsunami path needs the ONSHORE land (for run-up) AND the deep ocean -- it cannot
just drop land.

## Decision

Add a **deep-water rung** to `fetch_topobathy`, keyed on the EXISTING
`force_bathy_base` intent (the offshore/tsunami flag the GeoClaw inundation
composer already passes for a tsunami). When the ETOPO bathy base is forced ON,
the 3DEP LAND leg is masked to contribute **only genuine emergent terrain**:

- `topobathy.py._mask_land_leg_ocean_fill(land_local_path)` reads the staged 3DEP
  tif and sets every cell **at or below the waterline** (`_LAND_LEG_WATERLINE_M =
  0.0` m) to NaN before the leg enters the composite. The flat 0 m ocean fill (and
  any slightly-negative fringe) is dropped; all positive land is preserved
  unchanged.
- `_select_and_merge` calls the mask on the fetched land tif when
  `force_bathy_base` is set (and cleans up both the masked tif and its raw 3DEP
  predecessor). The generic LAST-wins composite then lets the ETOPO full column
  show through offshore while the finer 3DEP land still paints onshore. CUDEM 1/9"
  (where present) still supersedes both nearshore.

The resulting seam is exactly what the follow-up asked for: **ETOPO 2022 full
column (deep + shelf + coarse onshore) <- 3DEP fine ONSHORE (positive only) <-
CUDEM 1/9" nearshore <- NCEI regional**, working at arbitrary box sizes via the
existing windowed/decimated reads (a 6.5-deg box at ETOPO ~450 m is a ~1052 x 1120
base grid -- modest). ETOPO 2022 is itself a complete topo-bathy, so genuine
below-datum US land (inland, never in an offshore tsunami domain) is still
represented by ETOPO; the mask never drops run-up-relevant terrain.

**No new resolution knob.** The rung is automatic under the existing
`force_bathy_base` flag; per the NATE naming ruling, had a resolution param been
needed it would have been `target_resolution_m`, but none was.

## Consequence

- `fetch_topobathy(force_bathy_base=True)` over the Chignik domain now returns
  **min -6438 m, 80.8 % below -5 m, 55.6 % below -100 m**, max +2500 m (onshore
  land preserved). The non-forced path is byte-for-byte unchanged (still min -68 m
  on this box) -- the guard is scoped strictly to the offshore/tsunami intent.
- The `GEOCLAW_BATHYMETRY_FLAT` guard now **PASSES** on real bathymetry: the staged
  DEM the solver ran on reaches min -6437 m. The Chignik run-up SOLVES end to end.

### Live run-up (local-docker, real finite-fault source)

Direct-call `geoclaw_inundation(earthquake_source="Alaska Peninsula", ...,
coastal_gauge_lonlat=(-159.30, 55.30), sim_duration_s=1800, amr_levels=3,
fgout_frames=15)` -> status=ok:

- max inundation depth **0.094 m**, flooded area **0.061 km2** (a modest ~1 m-class
  event on a 450 m-resolved coast -- small overland run-up is physically honest, so
  the coastal GAUGE + the fgout surface field, not the onshore depth, are the run-up
  evidence).
- Coastal gauge (-159.30, 55.30): tsunami mareogram with a leading depression to
  -0.065 m then run-up through zero -- **peak-to-trough amplitude 0.083 m**.
- Okada deformation product: max uplift **+1.01 m**, subsidence **-0.60 m**, 294
  subfaults, `basis="measured_inversion"` (product `ak0219neiszm_2`).
- Physics asserts (from the fgout monitor, W->E shelf transect at 61/90/118 km from
  the epicentre) ALL hold: amplitude **nonzero**; peak surface perturbation
  **decays with distance** (0.138 -> 0.062 -> 0.032 m); first-arrival time
  **increases with distance** (643 s -> 1671 s; the 118 km point never crosses the
  0.05 m arrival threshold in 1800 s, consistent with continued decay).

Proof renders (EPSG:3857 over Esri World Imagery), `docs/proof/templates/`:
`geoclaw_chignik_runup_bathy_input.png` (the deep column reaching -6400 m -- the
headline), `..._deformation.png` (the signed 294-subfault dipole, ADR 0227 input
layer), `..._max_amplitude.png` (fgout max surface amplitude + AMR mesh + transect
points), `..._gauge_chart.png` (the mareogram), `..._transect_chart.png` (decay +
arrival vs distance).

### The payload gate

The proof run honours the resolution/native-fidelity doctrine (ADR 0224): sim
duration was bounded to 1800 s for tractability (stated), the grid is the native
adaptive plan. [Gate-card text recorded in the final report when the showcase
re-seed fired it.]

## Files

- `server/.../fetchers/_router/hooks/topobathy.py` -- `_mask_land_leg_ocean_fill`
  + `_LAND_LEG_WATERLINE_M`; `_select_and_merge` masks the land leg under
  `force_bathy_base` + cleans up the raw predecessor.
- `server/tests/test_router_topobathy.py` -- `test_mask_land_leg_drops_ocean_fill_
  keeps_land`, `test_deep_rung_restores_deep_column_under_land_fill`,
  `test_deep_rung_off_leaves_land_fill_clobber`.
- `scripts/drive_geoclaw_chignik_runup_proof.py`, `scripts/proof_geoclaw_chignik_
  runup.py` -- the live re-drive + proof renders / physics asserts.
- `scripts/seed_showcase_cases.py` -- the Chignik showcase entry gains the run-up
  (gauge + fgout; the bathymetry-gated note removed).
