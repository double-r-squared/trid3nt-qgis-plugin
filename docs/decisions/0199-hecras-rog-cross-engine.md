# ADR 0199 - HEC-RAS 2D rain-on-grid front + the TELEMAC-vs-HEC-RAS cross-engine comparison

Date: 2026-08-08
Status: Accepted (offline authoring substrate + infiltration LANDED and live-accepted;
the HR2D RoG SOLVE is decoded to the last link and frozen as a bounded continuation --
see "Live decode + residual" below)
Source: Godara, Bruland and Alfredsen 2024, "Comparison of TELEMAC-2D and HEC-RAS 2D
for rain-on-grid flash-flood modelling in a steep catchment" (Front. Water 6:1384205,
doi 10.3389/frwa.2024.1384205). NATE-provided.
Builds on: ADR 0195/0196 (TELEMAC RoG, the landed twin), ADR 0140/0157/0188 (the
fresh-AOI hecras_flood_2d authoring chain + composable knobs), ADR 0133-0138 (the
plan-HDF geometry writer + Event-Conditions forcing decode), ADR 0137 ("authoring
blind segfaults -- needs a reference deck").

## Context

NATE approved the Godara-2024 HEC-RAS front: Rain-on-Grid in HEC-RAS 2D plus the
paper's TELEMAC-vs-HEC-RAS comparison. The TELEMAC side is LANDED and live
(telemac_rain_on_grid, ADR 0196). This wave builds the HEC-RAS 2D RoG twin on the
same fresh-AOI hecras_flood_2d authoring chain and the cross-engine comparison
harness, on OUR US catchment (Coweeta Creek NC, pour point -83.40402 35.05746 -- the
ADR 0193/0196 site), same 25 mm/hr x 6 h design storm, same AMC-II CN-equivalent.

This is a TEMPLATE/KNOB SMOKE + reference comparison, NOT the NATE-gated replication
calibration (no gauge calibration runs).

## Decision 1 - RoG as KNOBS on the composer, not a new registration (LANDED offline)

Rain-on-grid is authored as new knobs on the existing pure-2D deck composer
(`compose_pure2d_deck`), NOT a new registered tool: `design_storm_mm_per_hr` +
`storm_duration_hr` (the uniform design storm, replacing the inflow hydrograph -- no
`target_peak_cfs` required) + `curve_number` / `amc_condition` / `ia_ratio` /
`min_infiltration_in_hr` (the SCS-CN infiltration, analog to the TELEMAC side). Rain
replaces the inflow when set: the run is rain-fed with a single normal-depth Outlet
BC at the pour point (all other perimeter closed -- the watershed boundary), mirroring
the TELEMAC RoG design (KSORT free exit at the pour point, walls elsewhere). This
follows the analysis-is-playground + ADR 0188 precedent (a knob on an existing
template, not a new atomic tool).

Two new authoring modules in the freshtopo worker tree, both offline-tested:

- `hecras_infiltration.py` -- the geometry-HDF SCS Curve Number loss layer
  (`Geometry/2D Flow Areas/<area>/Infiltration`): per-cell `Curve Number` /
  `Abstraction Ratio` / `Minimum Infiltration Rate` + `Cell/Face Center
  Classifications` + `Properties` (`SCS Initial Loss Reset Time`) + the four
  `Infiltration *` attrs mirrored on the 2D-area group. Structure DECODED byte-for-
  byte from the shipped public-domain Bald Eagle Creek `BaldEagleDamBrk.g09.hdf`
  (an HEC example with a real SCS-CN layer). AMC I/II/III conversion is the canonical
  NRCS NEH-630 formula (byte-parity with the TELEMAC `runoff_scs_cn.f` branch,
  ADR 0195). Uniform CN (`curve_number` knob) or a distributed per-cell CN2 field
  (`per_cell_cn2`, the paper Table-1 NLCD pattern).
- `hecras_meteorology.py` -- the plan-HDF `Event Conditions/Meteorology/Precipitation`
  uniform design storm (see Decision 2 for the live decode).

`hecras_flood_2d`'s registered surface + the worker parser are FROZEN pending the
solve link (Decision 2): a registered knob that segfaults live would violate the
honesty floor (the ADR 0195 precedent -- land the offline substrate, freeze the live
proof).

## Decision 2 - the HR2D RoG plan-HDF forcing, LIVE-DECODED (the last link is the residual)

Authoring the meteorology/precipitation blind segfaults (ADR 0137); no reference RoG
deck exists locally, in the solver image, or as extractable engine strings (the 6.6
binary is stripped). So the structure was decoded by ITERATIVE LIVE SOLVES against the
production 6.6 `RasUnsteady` (the ADR 0136-0138 method), each error advancing the next
link. The chain the engine's meteorology readers open, in order:

1. `READ_UN_MET_PRECIP_DATA` reads a gridded cumulative series from
   `Precipitation/Values` DIRECTLY -- constant-mode attributes alone
   (`_update_constant_precipitation_hdf`, the GUI convenience) are NOT honoured by the
   solver ("Precipitation values not found"); and the location is the group child, NOT
   the GUI-import `Imported Raster Data/Values`. Both proven live.
2. `Precipitation/Timestamp` -- HEC `DDMonYYYY HH:MM:SS` FIXED strings. float64 ->
   HDF "no conversion path" segfault; ISO-8601 -> "severe (64) input conversion error"
   in the engine's internal formatted read; HEC format -> parses. Proven live.
3. `READ_UN_M2D_PRECIP_INTERP` (`MetInterp.f90:92`) opens a per-2D-area
   `Precipitation/2D Flow Areas/<area>` interpolation folder. Absent -> clean
   "2D Flow Areas folder not found"; present-but-guessed -> `severe (174) SIGSEGV` in
   MetInterp regardless of grid resolution (1x1 / NxN) or per-cell Values orientation.

Links 1-2 are authored by `write_uniform_precipitation` and PASS the engine's readers;
the infiltration geometry layer (Decision 1) is also live-ACCEPTED (the engine reads
past "Reading 2D Area(s)" with the Infiltration group present, no crash). Link 3 -- the
GUI-precomputed raster->cell interpolation folder that `MetInterp` reads -- is the
RESIDUAL: its exact schema is produced by the RAS Mapper "compute" preprocessing step,
not reproducible headless without a reference RoG deck (the ADR 0137 wall, hit across
~12 live iterations). The composer authors links 1-2 and deliberately does NOT stamp a
guessed per-area folder, so a live solve gives the clean, debuggable
"2D Flow Areas folder not found" rather than a segfault.

Frozen continuation (bounded, low-risk once a reference is available): obtain a
reference HEC-RAS 6.x rain-on-grid plan HDF (a shipped example or a NATE GUI export),
decode the `2D Flow Areas/<area>` interpolation schema, author it, live-solve the
Coweeta RoG deck, then register the `hecras_flood_2d` RoG knobs + bump the worker
parser + rebuild the image (RoG metric extraction: outlet discharge across the Outlet
BC edges + max velocity) + seed the showcase. Reason deferred: shipping unverified
worker/registration code violates offline-first + worker-image-staleness; the solve is
not completable/verifiable without link 3 (ADR 0195 rationale, verbatim-class).

## Decision 3 - the cross-engine comparison harness (LANDED)

`scripts/sandbox/hecras/rog_compare_engines.py` -- the paper's experiment on our
Coweeta catchment: (a) outlet/peak discharge, (b) wet-area extent, (c) wall-time, per
engine, and (d) the paper's qualitative findings CHECKED against ours (honest --
disagreement is a finding). TELEMAC-2D RoG is LIVE (ADR 0196: AMC II peak 45.5 m3/s,
runoff 162x10^3 m3, maxH 6.95 m, continuity 1.3e-15, ~45 s wall, triangular TIN 4854
nodes / 9521 cells -- CORRECT END, so the paper's "triangular mesh stable on steep
terrain" finding is CONFIRMED on our catchment). The HR2D row authors the Coweeta RoG
deck (fresh-AOI mesh + terrain subgrid tables + SCS-CN infiltration + links 1-2) and
records the exact MetInterp block; the HR2D peak-Q / wet-km2 / stability comparison is
pending link 3. Reported honestly, not fabricated.

## Applicability envelope (bake into the RoG knob docstring when registered)

Per the paper: RoG reproduces SINGLE-STORM flash-flood events (~10-20 h) in small
steep catchments. Multi-peak / sustained rain-on-snow is NOT reproduced -- infiltrated
water is permanently lost (no soil-routine / subsurface return flow), so inter-peak
baseflow is missed. The uniform CONSTANT design storm (the mass-balance-checkable
`depth = rate * duration`, `set_constant_precipitation`'s own "2D mesh-commissioning"
use) has no falling limb within the storm; a true time-varying hyetograph needs the
DSS / `Precipitation Hydrograph=` path -- the RoG caveat, the exact analog of the
TELEMAC constant-rain `RAINDEF=1` limit (ADR 0195/0196). US-only via our fetchers;
Coweeta NC is the US steep replication site for the Sleddalen (Norway) methodology.

## Consequences

- +0 registered tools / +0 templates this wave (RoG is composer knobs; the registered
  `hecras_flood_2d` surface + worker parser are frozen pending link 3). Registry +
  EXPECTED_TEMPLATES unchanged; no corpus/categories change.
- No worker image rebuilt (worker code unchanged -- RoG is authored host-side; no
  verifiable-through-the-image change until link 3 + the RoG metric extractor land).
- Offline (freshtopo worker tree, `env -u TRID3NT_CACHE_BUCKET`): new
  `test_hecras_rog.py` (10) + `test_hecras_deck2d.py` (10, unchanged) green; the RoG
  authoring composes the full deck + the infiltration + links-1-2 meteorology trees.
- Live-accepted through `trid3nt-local/hecras:latest` (id e2216711e2b0): the fresh-AOI
  2D geometry + SCS-CN infiltration layer + `Precipitation/Values` + HEC `Timestamp`
  (RasGeomPreprocess exit 0; RasUnsteady reads past all three, blocks only at the
  MetInterp per-area interpolation folder).
- Board rows `hecras_2d_rain_on_grid` and `rog_cross_engine_telemac_vs_hecras` ->
  substrate LANDED with the decode chain + the TELEMAC-live comparison; the HR2D solve
  is the documented residual.
- Files: `services/workers/hecras2025/subst/crux/freshtopo/{hecras_infiltration.py,
  hecras_meteorology.py,hecras_deck2d.py,flood2d_pipeline.py,test_hecras_rog.py}` +
  `scripts/sandbox/hecras/rog_compare_engines.py`.
