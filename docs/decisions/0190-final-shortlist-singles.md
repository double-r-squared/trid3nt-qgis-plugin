# ADR 0190 - final shortlist singles: TELEMAC rainfall, ELMFIRE Hirsch POC, SWAN nonstationary storm, SWMM RTK RDII

Date: 2026-08-08
Status: accepted

## Context

The FINAL four "ready NOW" singles from the M/L sign-off shortlist
(`docs/validation/ml-signoff-shortlist.md` rows 16/17/28/34), one per engine,
each fully proven before the next. Two are composable KNOBS on existing
templates (TELEMAC, SWAN); two are NEW registered closed-form/validation-class
tools (ELMFIRE, SWMM). Registry 233 -> 235 (+1 ELMFIRE, +1 SWMM; TELEMAC + SWAN
add zero tools). EXPECTED_TEMPLATES grows by the two new tools.

## Row 1 - TELEMAC `rainfall_evaporation_forcing` = KNOBS on `telemac_river_dye`

Distributed ON-MESH rainfall/evaporation as a native TELEMAC-2D source term,
independent of the inflow-boundary hydrograph. Three composable knobs
(`rainfall_mm_per_day`, `evaporation_mm_per_day`, `rainfall_gridmet_window`); the
gridMET window auto-sources a REAL storm total from the wired `fetch_gridmet`
(`pr`). `author_deck` emits `RAIN OR EVAPORATION = YES` + `RAIN OR EVAPORATION IN
MM PER DAY = <signed>` (+rain / -evap) + `VALUES OF TRACERS IN THE RAIN`
(DAMOCLES REQUIRES the rainwater tracer concentration when tracers exist - the
first live run aborted without it). Unset -> the deck is BYTE-IDENTICAL. Worker
image `trid3nt-local/telemac:latest` REBUILT (`41c72ebeaa85`, absolute
-f/context, `docker history` carries NO GRACE-2 ref, parser `telemac-reach-2`).

Evidence: with-rain vs without on the Snake River reach through the run_solver
seam + rebuilt image (`rainfall_forcing_compare.py`): 1500 mm/day over 90 min
raises the domain-mean wet depth +8.6 mm (max +21 mm), a monotone accumulation
distinct from the identical inflow BC. gridMET real-storm path live: Hurricane
Harvey over Buffalo Bayou Houston = 158.5 mm/day domain-mean (2017-08-26..30).
NOTE: the gridMET window samples the geocode-derived AOI, so its accuracy on a
place-name prompt depends on geocode precision (exact for a drawn bbox AOI); the
showcase uses an explicit rate on the proven Eel reach.

## Row 2 - ELMFIRE `initial_attack_containment_probability` = NEW closed-form tool

`elmfire_initial_attack_containment_probability` (registered, tier=template,
engine=elmfire; NO engine run). The EXACT published elmfire.io suppression Hirsch
POC formula `POC = E/(1+E), ln(E) = 4.6835 - 0.7043*A - 0.00041*I -
0.000052*A*I` (A = fire size ha, I = head-fire intensity kW/m) - taken from the
module-coverage-board's own recorded formula (elmfire.io/user_guide/
suppression.html, after Hirsch/Corey/Martell 1998), NOT a reconstruction.
Attack-delay sensitivity is coupled via Byram (1959) `ROS = I/(H*w)` -> elliptical
point-source growth during the get-away time -> `POC(delay)`. Deliverable = a
POC-vs-delay chart (one curve per intensity) + a POC(size,intensity) surface +
typed scalars, NO raster (closed-form validation class). corpus.yaml + the
model-free `retrieve_visible_tools(prompt, None, 8)` top-8 check (5/5); primary
category `model_validation`.

Evidence: critical delays (POC=0.5) 1000 kW/m never within 120 min / 2500 kW/m
53 min / 4000 kW/m 29 min / 6000 kW/m 15 min - the get-away-time doctrine. Live
showcase `01KZGYTV8WRG1J7BF1YG7RYR6Y` (1 chart, no map layer - correct for the
validation class).

## Row 3 - SWAN `nonstationary_time_marching_storm_evolution` = KNOBS + 3 bug fixes

`swan_wave_field(mode="nonstationary")` existed but had NEVER run live - three
latent bugs made it a no-op, all fixed in `deck_builder.py`:

1. `COMPUTE NONSTATIONARY` emitted the duration as bare seconds where SWAN needs
   an ISO `YYYYMMDD.HHMMSS` time string (overflowed past 24 h) -> a fixed-epoch
   ISO helper (`_swan_iso_time`);
2. the `BLOCK` had no `OUTPUT <t> <dt> <unit>` clause -> SWAN wrote NO per-frame
   dumps -> the animation had one frame;
3. the default higher-order propagation scheme ABORTS on CFL for a time-march
   (error level 2) -> `PROP BSBT` (SWAN's stable nonstationary scheme).

Plus a TIME-VARYING storm boundary for genuine evolution: `storm_peak_hs_m` +
`storm_peak_hour` build a build-peak-decay offshore-Hs series (`build_storm_
hydrograph`) written as a SWAN TPAR file (`BOUNDSPEC SIDE ... CONSTANT FILE`),
threaded via a new `boundary_timeseries` build-spec field (parser `swan-spec-3`,
strict allowlist). Zero new tools. Worker image `trid3nt-local/swan:latest`
REBUILT (`bffb3ac6cc02`, absolute -f/context, no GRACE-2 ref).

Evidence: a live 36 h storm on the Apalachee Bay FL shelf through the native SWAN
solver + rebuilt image (`run_swan_storm_direct.py`): 19 time-stamped Hs frames
build 1.0 -> 4.3 -> 6.0 m (peak hour 18) -> 4.3 -> 2.7 -> 1.0 m; `max_hs_m` 6.02 m
matches the forcing. Showcase `01KZGX66T41AGKKQPRF53D7VVR` (20 layers = peak +
19 frames persisted, feeding the scrubber). Proofs: peak-Hs map + the frame
filmstrip (pinned times + shared color scale).

## Row 4 - SWMM `swmm_rdii_rtk_unit_hydrograph` = NEW tool, EPA Table 7-1 anchor

`swmm_rdii_rtk_unit_hydrograph` (registered; host-side pyswmm, NO worker image -
SWMM solves in the agent venv via `swmm5_run`). The RTK triangular unit-
hydrograph RDII method: each UH (short/medium/long) is a triangle from (R,T,K)
with area = `R * rainfall * area` (the RTK volume identity), convolved with the
storm and split against direct runoff at the node. TWO acceptance checks: the
volume identity (ratio 1.00006) + a NATIVE SWMM 5 cross-check (a real
`[HYDROGRAPHS]`/`[RDII]` deck solved through `swmm5_run`; the closed-form peak
matches the engine to <1%). corpus.yaml + retrieval top-8 (5/5); primary
category `simulation_modeling`.

EPA Table 7-1 replication: the EPA SWMM 5 Hydrology Manual Ch.7 worked example
(swmm5.org markdown) - 10-acre sewershed, node N1, R summing to 0.36, the
published hourly rainfall (Table 7-1). With a REPRESENTATIVE R/T/K split (sum R =
0.36), the closed form reproduces the native SWMM engine to 0.6% and lands within
~4% of the published Figure 7-10 peak (1.06 vs 1.021 cfs). The exact per-UH
R/T/K appear ONLY in the manual's Figure 7-8 (not the text / not machine-
accessible), so bit-exact reproduction of the tabulated flows is flagged for NATE
to supply those values; the METHOD, the volume identity, and the native-SWMM
cross-check are exact, and the published Fig 7-10 flows are overlaid on the proof.

Evidence: showcase `01KZGYWHF98YZCV5KSG0YRQVPM` (the EPA example, 1 chart). Proof
`swmm_rdii_rtk_unit_hydrograph_rdii_vs_runoff.png` (closed form + native SWMM +
published Fig 7-10 X-marks + direct runoff) + `_unit_hydrographs.png`.

## Offline coverage (green, `env -u TRID3NT_CACHE_BUCKET`)

- `services/workers/telemac/tests/test_rain_forcing.py` (5): RAIN block +
  TRAIN keyword, byte-identical when unset.
- `server/tests/test_telemac_rain_forcing.py` (8): the signed net-rate resolver +
  gridMET-window parser.
- `server/tests/test_elmfire_initial_attack.py` (13): the published-formula POC,
  Byram ROS, elliptical growth, POC-vs-delay, registration.
- `services/workers/swan/test_deck_builder.py` (+8 -> 41): ISO times, BLOCK
  OUTPUT, PROP BSBT, TPAR boundary + the strict-field rejection.
- `server/tests/test_swan_storm_evolution.py` (6): the storm hydrograph +
  build_spec threading.
- `server/tests/test_swmm_rdii_rtk.py` (10): the RTK UH + volume identity + the
  native-SWMM cross-check + the EPA Table 7-1 replication.
- Pins: `test_door_dissolution` (+2 templates), `test_catalog_surfacing`
  (registry 233 -> 235, +2), `test_categories` (both new tools get a primary
  category). Model-free retrieval top-8 for both new tools (5/5 each).

## Consequences

- Coded-tools metric: **+2 registered tools** (`elmfire_initial_attack_
  containment_probability`, `swmm_rdii_rtk_unit_hydrograph`); registry 233 -> 235,
  EXPECTED_TEMPLATES +2. TELEMAC + SWAN added ZERO tools (composable knobs only).
- Two worker images rebuilt (TELEMAC manifest/RAIN, SWAN nonstationary+TPAR);
  SWMM adds no image (host-side pyswmm). SWAN's nonstationary path now runs
  end-to-end for the FIRST time (three latent bugs the stationary path never hit).
- No flood seam touched -> no flood canary mandated.

## Open issues / flagged for NATE

1. SWMM Table 7-1 bit-exact flows need the per-UH R/T/K from EPA Figure 7-8 (not
   in the manual text); the current tool replicates the SETUP + validates against
   the native engine and lands within ~4% of the published peak with a
   representative split.
2. The TELEMAC gridMET rain window samples the geocode AOI; a place-name prompt's
   accuracy depends on geocode precision (exact for a drawn bbox). A per-reach
   rain-gauge sampling refinement is the follow-up.
3. SWAN storm uses a uniform-in-space time-varying boundary (a passing swell
   train); a spatially-varying moving-storm wind field (multi-grid ERA5) is the
   richer, deferred refinement.
