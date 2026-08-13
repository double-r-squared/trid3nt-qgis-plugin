# 0147: SWAN physics-scheme knobs + 2 CAND-S templates (folding 8 rows, 2 STOPs)

Date: 2026-08-05
Status: landed

## Context

Per-engine grind batch: the twelve SWAN CAND-S rows. Triage confirmed
the board's fold prior. The existing swan_wave_field deck
(services/workers/swan/deck_builder.py, SWAN 41.51) HARDCODED its
physics lines ("GEN3 WESTHUYSEN", "FRICTION JONSWAP CONSTANT 0.067",
"BREAKING CONSTANT 1.0 0.73", bare "TRIAD", implicit WCAPPING/QUADRUPL).
Rows 2-9 are single-command physics knobs on that SAME stationary deck;
row 1 is pure stationary-solve orchestration; row 10's parametric
BOUNDSPEC was already fully wired; rows 11-12 are I/O-shaped.

The manual (node28/node32) + the installed binary (VERNUM 41.51,
SdsBabanin ST6 present) confirm every command exists. Two ground-truth
checks reshaped the plan: `ldd swan.exe` shows NO libnetcdf/libhdf5, and
postprocess_swan matches only Hsig/HSIGN prefixes (no PT* partition
reader) -- so the board's "partition signed" roster note is unverified
against code.

## Decision

Expose the physics schemes as EXPLICIT, auditable knobs on ONE deck +
contract + tool surface, and land TWO multi-run templates; STOP the two
I/O rows with recipes.

Deck-builder knob extension (SwanBuildSpec + parse_build_spec +
render_swn_command_file, threaded through SwanRunArgs ->
build_swan_build_spec -> swan_wave_field): gen_formulation
(westhuysen/komen/janssen/gen1/gen2), whitecapping (ab/komen, GEN3 only),
quad_iquad (GEN3+wind only), breaking_alpha/breaking_gamma,
friction_cfjon, triad_biphase (eldeberky/dewit)+triad_urcrit/triad_lpar.
Defaults reproduce the prior deck's PHYSICS exactly (0.0670 parses ==
0.067; 1.000 0.730 == 1.0 0.73; GEN3 WESTHUYSEN + bare TRIAD unchanged),
so the signed stationary regression is solve-identical. GEN1/GEN2 emit
neither WCAPPING nor the no-wind OFF QUAD (not GEN3).

Two templates (workflows/swan/*, shared _sweep_common.py: DEM fetched
ONCE and reused across N solves; Seam-1 via model_swan_wave_field):
- swan_physics_sensitivity_sweep (folds rows 2-9): varies ONE physics
  axis across N cheap stationary solves over one AOI+boundary; overlays
  peak-Hs + wave-footprint (normalized to the baseline scheme) as a
  color-grouped line. Default axis breaking_gamma (dissipation acts on
  boundary-forced waves -> demonstrable without wind); GEN/WCAPPING/
  QUADRUPL flagged wind_dependent.
- swan_stationary_snapshot_batch (row 1): N stationary solves with
  per-snapshot boundary Hs/Tp/dir sampling a storm build-up/decay; a
  peak-Hs/footprint trajectory chart + per-snapshot layers.

Row 10 FOLDS into swan_wave_field (boundary_* knobs already shipped; no
NDBC/buoy fetcher spec exists -> live buoy forcing is a data follow-on).

Rows 11-12 STOP with recipes: partition output needs a 3-layer change
(deck PT* vars + postprocess partition reader + contract) across the
worker image; native netCDF BLOCK needs SWAN recompiled with
netcdf-fortran, and the .mat->COG postprocess ALREADY feeds the QGIS COG
path so the row's OUTCOME is delivered.

## Live evidence (local-docker, SWAN 41.51, Huntington Beach CA)

The knobs are INERT until the worker image is rebuilt (the image bakes
in deck_builder.py). After rebuild:
- Demonstrability is DOMAIN-DEPENDENT (a real physics finding, not a
  bug): on the DEEP Huntington Beach box, breaking_gamma [0.4,0.9] gave
  byte-identical Hs COGs (max abs diff 0.0 -- no cells shallow enough to
  break), and friction cfjon [0.01,0.20] moved the field only 0.6%
  (20928 cells differ, mean 2.605 -> 2.589). Dissipation knobs act only
  where the water is shallow.
- On a BROAD SHALLOW SHELF (Apalachee Bay / St. George Island FL,
  -85.55,29.70,-85.40,29.85), swan_physics_sensitivity_sweep
  axis=friction_cfjon [0.01,0.10,0.30] is emphatic: whole-field MEAN Hs
  1.884 -> 1.645 -> 1.285 m (a 31.8% monotonic spread) while the
  boundary-pinned MAX Hs holds 3.033 -> 2.985. The chart overlays these
  two series -- the dissipation-sensitive mean sloping down, the peak
  flat -- so the knob is directly visible. This is why the template's
  default axis is friction_cfjon (whole-path dissipation, demonstrable on
  any shelf) and its sensitivity metric is the new mean_hs_m, not the
  boundary-pinned max_hs_m or the threshold-gated wave_area_km2.
- swan_stationary_snapshot_batch hs_sequence [2.0,4.0] (Huntington
  Beach): peak Hs 1.990 -> 3.980 m across the two snapshots (boundary-
  driven), both solves status=ok, per-snapshot Hs COGs emitted.
- postprocess_swan already computed mean_hs_m; it is now carried onto
  WaveFieldLayerURI (optional, agent-side -- no image rebuild).
- Proofs: docs/proof/templates/swan_physics_sensitivity_sweep{,_chart}.png
  + swan_stationary_snapshot_batch{,_chart}.png.

## Consequence

+2 registered templates (registry 208 -> 210; tier=template set 50 ->
52). swan_wave_field gains 9 optional physics params (additive; defaults
preserve behavior). The physics knobs require the SWAN image to be
rebuilt to take effect live (documented). netCDF + partitioning remain
open with precise recipes. The sensitivity sweep is the SWAN analogue of
the SFINCS numerical-knobs + pelicun sweep templates.
