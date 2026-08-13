# 0161: ELMFIRE transient-weather + crown-fire machinery fronts

Date: 2026-08-06
Status: landed

## Context

The #2 and #3 ranked fronts on `docs/validation/ml-signoff-shortlist.md`:
the ELMFIRE transient/multi-band weather deck (front A, ~5 board rows) and the
ELMFIRE crown-fire family (front B, ~5 rows). Both were blocked on machinery the
constant single-band weather deck path did not have, and both carried 0142 STOP
rows (`dead_fuel_moisture_interpolation_frequency_control`,
`crown_fire_initiation_threshold_sweep`, `spread_rate_ceiling_calibration`) that
were genuine no-ops until that machinery existed.

Every namelist keyword was triaged against the IN-IMAGE ELMFIRE first. The
on-disk `third_party/elmfire` checkout is at commit `23a4cbd` == the image's
pinned release tag `2025.0526`, so its Fortran source IS the baked binary's
source. Verified there (and then live through the image):

- `NUM_METEOROLOGY_TIMES` lives in the `&MONTE_CARLO` group (NOT &SIMULATOR /
  &INPUTS), read unconditionally on every run (`elmfire.f90` READ_MONTE_CARLO);
  the weather rasters (ws/wd/m1/m10/m100) are multi-band BSQ, band = met time,
  linearly interpolated every `DT_METEOROLOGY` (`elmfire_level_set.f90`
  ITLO/ITHI_METEOROLOGY / F_METEOROLOGY).
- `DT_INTERPOLATE_M1/M10/M100` (dead-fuel moisture interpolation cadence) live in
  `&TIME_CONTROL`; `TARGET_CFL`/`SIMULATION_DT` too. `BANDTHICKNESS`,
  `CRITICAL_CANOPY_COVER`, `CROWN_FIRE_MODEL`, `CROWN_FIRE_SPREAD_RATE_LIMIT` live
  in `&SIMULATOR`. Verification-02 crown-fire values: SIMULATION_DT=1.0,
  TARGET_CFL=0.10, BANDTHICKNESS=3, CROWN_FIRE_MODEL=1.
- Crown output: `DUMP_CROWN_FIRE` writes a per-cell crown-type raster
  (`crown_fire_<case>_<t>.bil`: 0 none / 1 passive-torching / 2 active crown -
  `elmfire_spread_rate.f90` sets 2 when the Cruz active-crown criterion holds AND
  `CC >= CRITICAL_CANOPY_COVER`). Canopy stored units (the &INPUTS unit-flag
  defaults, all TRUE): CC_IN_PERCENT / CH_TIMES_10 / CBH_TIMES_10 / CBD_TIMES_100.

## Decision

**Machinery (deck_builder + composer, all on the typed constant-flat-deck kwarg
path - the dict-spec fetch path is unchanged, so its ADR 0158 allowlist is
untouched by construction).**

- `deck_builder.render_namelist` gained `dt_meteorology_s` / `num_meteorology_times`
  / `target_cfl` / `time_control_extra`. `num_meteorology_times > 1` emits a
  `&MONTE_CARLO` group carrying `NUM_METEOROLOGY_TIMES`; `target_cfl` and
  `time_control_extra` extend &TIME_CONTROL. At the defaults the deck is
  byte-identical to the constant tutorial-01 deck (pinned by the existing
  golden-deck test).
- `deck_builder.write_weather_bands` writes a multi-band (one band per met time)
  spatially-uniform Float32 weather raster.
- `run_elmfire.build_constant_flat_deck` gained `weather_schedule` (a list of
  per-band ws/wd/m1/m10/m100 dicts, absent keys inheriting the base weather),
  `dt_meteorology_s`, `target_cfl`, `time_control_extra`. A schedule writes the
  five weather rasters multi-band and sets `NUM_METEOROLOGY_TIMES`.
  `_normalize_weather_schedule` raises loudly on an unknown schedule key (no
  silent drop).
- `_sensitivity_common.solve_constant_case` threads those kwargs and gained
  `measure_crown` (reads the per-cell `crown_fire` raster -> active-crown /
  any-crown area). `postprocess_elmfire.discover_elmfire_rasters` gained the
  `crown_fire` family.

**Front A - two templates (transient/):**

- `elmfire_transient_wind_schedule_spread` - a mid-run wind-direction shift
  (multi-band schedule) vs a constant wind on the same deck; the shift redirects
  the fire. The synthetic wind schedule rides the ADR 0107 input-review gate
  (`SyntheticInput` basis `default_demo`).
- `elmfire_dead_fuel_moisture_interpolation_frequency_control` - the 0142 STOP,
  now real on a transient deck: sweep `DT_INTERPOLATE_M1` (M10/M100 scaled 10x/
  100x) on a synthetic dead-fuel moisture-RECOVERY schedule; a coarser cadence is
  cheaper but lags the recovering moisture -> burned area drifts from the fine
  reference (accuracy-vs-cost). Also gated.

**Front B - one folded template (crown/):**

- `elmfire_crown_fire_initiation_threshold_sweep` - the 0142 crown pair folded
  into ONE template with a `sweep_variable` check: `critical_canopy_cover` (the
  INITIATION boundary - active crown collapses once the threshold rises past the
  deck's 0.60 canopy cover) or `spread_rate_limit` (the Cruz active-crown RATE
  ceiling - capped vs uncapped extent). Canopy stack in ELMFIRE stored units
  (cc=60% ch=37.5 m cbh=1.0 m cbd=0.18 kg/m3), validated by
  `_validate_canopy_stored_units`. TIME_CONTROL emits TARGET_CFL; &SIMULATOR emits
  BANDTHICKNESS + CROWN_FIRE_MODEL (Verification-02 values).

**Sub-STOP (honest, recipe recorded):** `DUMP_CROWN_FIRE_AREA` SIGSEGVs this
ELMFIRE build's `fire_size_stats_to_rasters` postprocess on a single-case run
(isolated: `DUMP_CROWN_FIRE` alone is clean rc=0; adding `DUMP_CROWN_FIRE_AREA`
-> rc=139). The crown template uses `DUMP_CROWN_FIRE` only and derives the
active-crown AREA from the per-cell raster - nothing essential is lost. Recipe to
revisit: the crash is in the fire-size-stats raster writer at run finalize; a
newer ELMFIRE release or a `DUMP_FIRE_SIZE_STATS=.FALSE.` + area-only deck may
clear it.

## Worker-image law (ADRs 0148/0158)

The local-docker spec runs the agent-built deck through `trid3nt/elmfire:dev` via
`bash -c '... elmfire_2025.0526 ./inputs/elmfire.data'`. The deck builder runs
AGENT-SIDE, so the namelist change is live agent-side without a rebuild - but the
image carries a deck_builder copy and (found this session) had been rebuilt under
ADR 0158 WITH the Batch/FIRE-4 python entrypoint, which swallows the spec's
`bash -c` argv and breaks the local path. Corrected with a thin dev overlay
`services/workers/elmfire/Dockerfile.dev` (FROM the compiled base: re-bakes the
worker dir, resets `ENTRYPOINT []`), rebuilt with ABSOLUTE `-f` + context
(context-drift law). Provenance verified: `docker history` carries NO GRACE-2
reference; entrypoint reset to null; the baked deck_builder carries the new
`write_weather_bands` / `num_meteorology_times`. All FOUR template runs then
passed live THROUGH the rebuilt image (real run_solver local-docker + postprocess
+ COG publish to MinIO):

- transient wind: constant 0.216 vs transient 0.299 km2, heading shift ~68 deg.
- crown initiation: active-crown 0.619 km2 at threshold <= 0.525, collapses to 0
  above the deck's 0.60 cover (max crowning threshold 0.525).
- crown ceiling: capped 0.194 vs uncapped 2.106 km2 (10.8x extent).
- dead-fuel cadence: reference (60 s) 0.410 vs coarsest (1800 s) 0.515 km2
  (25.4% deviation).

## Consequence

- Registry 219 -> 222; templates 61 -> 64 (`test_catalog_surfacing` +
  `test_door_dissolution` pins bumped honestly; +3 CODED templates this landing).
  categories.py + three co-located corpus.yaml added; model-free
  `retrieve_visible_tools(prompt, None, 8)` surfaces all three (door-dissolution
  retrieval matrix green).
- Board rows unblocked. Front A: `transient_wind_schedule_spread` +
  `dead_fuel_moisture_interpolation_frequency_control` landed; the shared
  multi-band + time-interpolation machinery now also unblocks
  `historical_met_band_ensemble`, `live_fuel_moisture_raster`, and
  `raster_perturbation_ensemble` (they reuse `weather_schedule` + a real-reanalysis
  band source, a later front). Front B: the folded crown template covers
  `crown_fire_initiation_threshold_sweep` + `spread_rate_ceiling_calibration`; the
  crown machinery (canopy deck + DUMP_CROWN_FIRE + crown-area metric) unblocks
  `active_crown_fire_spread_rate_verification` (#21) and bridges toward
  `crown_triggered_spotting` once the &SPOTTING front lands.
- Tests: 4 new deck_builder unit tests (transient namelist + multi-band write,
  byte-identical default preserved); offline slice green.
- Proofs in `docs/proof/templates/`: `elmfire_transient_wind_schedule_spread{,_chart}.png`,
  `elmfire_crown_fire_initiation_threshold_sweep{,_chart}.png`,
  `elmfire_dead_fuel_moisture_interpolation_frequency_control{,_chart}.png`.
