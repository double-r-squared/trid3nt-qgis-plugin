# ADR 0239 - ELMFIRE ember spotting: the barrier-jump question class + the spotting-knob-bounds trap

Status: COMPLETE (2026-08-13). Physics PROVEN local-first (in-image, through the
baked `trid3nt/elmfire:dev` binary, the spotting OFF-vs-ON barrier-jump
discriminant) AND productionized: registered tool
`elmfire_spot_fire_barrier_crossing` (engine=elmfire, tier=template), a
server-side `&SPOTTING` namelist surface + a `fuel_break` deck feature, LIVE
end-to-end through the docker image (COG published). No worker-image rebuild (deck
build is server-side for the local-docker backend). See the COMPLETE section.
Date: 2026-08-13

## Context

The MODULE-COVERAGE-BOARD carries a Spotting section (4 open CAND rows). ELMFIRE
spotting models ember lofting/transport/landing ahead of the front as a stochastic
Lagrangian process, producing new ignition points beyond the contiguous perimeter -
disabled by default (`ENABLE_SPOTTING=.FALSE.`) and, per the board, "not confirmed
as surfaced in TRID3NT today." ELMFIRE was already LANDED (fire_spread + crown +
sensitivity templates, worker, LANDFIRE inputs); spotting is a NEW capability on that
surface.

## GATE VERDICT: PASS (config accepted + behaviour changes)

The baked binary accepts a `&SPOTTING` namelist group (`READ_SPOTTING`, a SEPARATE
group from `&SIMULATOR`) and its behaviour changes exactly as the physics demands.
The discriminant is the cleanest in the program: on a constant dry-grass deck with a
NON-BURNABLE fuel-break strip spanning the full cross-wind extent, the contiguous
head fire STOPS at the break with spotting OFF (0 cells beyond it) and JUMPS the break
with spotting ON (lofted embers ignite spot fires on the far side). The question
class is literally "does the fire jump the break?"

## Load-bearing findings (verified against the baked source, `elmfire_spotting*.f90`)

1. **The spotting-knob-bounds trap (THE gate blocker).** Whenever `ENABLE_SPOTTING`,
   the binary runs `SET_SPOTTING_PARAMETERS`, which OVERWRITES the scalar knobs
   (`MEAN_SPOTTING_DIST`, `PIGN`, `SPOT_FLIN_EXP`, `SPOT_WS_EXP`, `NEMBERS_MAX`,
   `SURFACE_FIRE_SPOTTING_PERCENT`) from their `_MIN/_MAX/_LO/_HI` bounds. Setting the
   bare scalars is a silent no-op. Worse, surface-fire spotting stays OFF unless
   `GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT` (default 0) is raised - it drives
   `SURFACE_FIRE_SPOTTING_PERCENT(:) = GLOBAL * MULT`, so the default 0 kills every
   surface ember. A deterministic run therefore sets the BOUNDS with MIN==MAX (e.g.
   `MEAN_SPOTTING_DIST_MIN==MAX`, `GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT_MIN==MAX=100`),
   never the scalars. This single fact is why the first six gate attempts cast zero
   embers.
2. **Surface vs crown launch.** The launch trigger checks `FLIN_SURFACE >= CRITICAL_FLIN`
   (the CROWN critical intensity) first, then, if `ENABLE_SURFACE_FIRE_SPOTTING`,
   `FLIN_SURFACE >= CRITICAL_SPOTTING_FIRELINE_INTENSITY(fbfm)`. On a no-canopy grass
   deck only the SURFACE path fires, so `ENABLE_SURFACE_FIRE_SPOTTING=.TRUE.` is
   required.
3. **`NEMBERS` is not a namelist member** (only `NEMBERS_MIN` + `NEMBERS_MAX_*` are).
   The superseded (default) path draws `NEMBERS = NEMBERS_MIN + NINT(R0*(NEMBERS_MAX-NEMBERS_MIN))`
   - so board row 3's `NEMBERS_MIN/MAX` knobs DO drive the default model (the earlier
   "NEMBERS_MIN/MAX are UMD-only" read was wrong).
4. **`STOCHASTIC_SPOTTING` is inert** in this binary (source comment: "This is not
   currently used"). The min/max bounds ARE the stochastic mechanism, resolved once
   per run via the internal draw; a true Monte-Carlo spread needs `NUM_ENSEMBLE_MEMBERS>1`
   (a distinct capability). Board row 4 is therefore DEFERRED (see residuals).
5. **Board default corrections** (baked-binary truth, not the docs page): `SPOT_FLIN_EXP=0.5`
   (not 0.3), `SPOT_WS_EXP=0.9` (not 0.7), `PIGN` default `1.0`=1% (the docs "100%" is
   wrong for this binary). The template uses explicit values, not the binary defaults.

## The 4 board rows -> one knob-class template

| board row | disposition |
|---|---|
| `lognormal_spot_distance_model_calibration` [M] | FOLDED as the `mean_spotting_distance_m` knob (the MEAN_SPOTTING_DIST distance-model scale; larger = farther downwind spot fires). |
| `critical_spotting_intensity_threshold_gate` [S->M] | FOLDED as the `critical_spotting_intensity_kwm` knob (broadcast across the per-fuel array via the namelist `N*value` repeat form; 0 = generate always). |
| `ember_count_and_landing_ignition_probability` [S->M] | FOLDED as the `nembers` + `pign_pct` knobs (NEMBERS_MIN/MAX + PIGN_MIN/MAX bounds). |
| `stochastic_spotting_parameter_ensemble` [L] | DEFERRED - `STOCHASTIC_SPOTTING` is inert in this binary; a real ensemble needs multi-member Monte Carlo, a separate capability. |

The barrier-jump is a DISTINCT question class (contiguous spread cannot answer "does
the fire cross the break?"), and the three parameter rows fold cleanly as its knobs -
so this is a KNOB-CLASS wave (the wind_drag precedent): one template closes rows 1-3
and delivers the barrier-jump discriminant; row 4 is honestly deferred.

## Live numbers (through the baked image, the registered composer path)

Constant dry-grass GR2 deck, 12 km x 1.5 km, wind 25 mph from the west, 4 h, 30 m
cells, a ~240 m NB1 fuel break ~1/3 across; spotting knobs MEAN_SPOTTING_DIST=25,
NEMBERS=20, PIGN=100, LOGNORMAL:

- **spotting OFF**: far-side burned area = **0.0 km2** (head fire STOPS at the break).
- **spotting ON**: far-side burned area = **13.90 km2** (15449 spot cells beyond the
  break), head fire 2.32 km2 west of the break; the ON time-of-arrival COG published
  to S3. Physics assertion `off <= 1e-3 km2 < on` held (`break_jumped=1`).
- **critical-intensity gate** (row 2, second discriminant): the `critical_spotting_intensity_kwm`
  knob emits `CRITICAL_SPOTTING_FIRELINE_INTENSITY = 304*<value>` (the per-fuel-array
  namelist repeat form, verified accepted by the binary). Raising it to 5000 kW/m -
  above the grass head-fire FLIN (median ~783, max ~1829) - SUPPRESSES all spotting:
  nembers=0, far-side burned area = 0 (identical to spotting OFF), while the 0-default
  jumps the break. The generation gate works.

## Architecture decision: server-side deck, NO image rebuild

For the local-docker backend the deck (including `elmfire.data`) is built SERVER-SIDE
(`build_constant_flat_deck` -> `render_namelist`); the image only runs
`elmfire_2025.0526 ./inputs/elmfire.data` on the mounted deck. So the `&SPOTTING`
namelist surface + the `fuel_break` deck feature take effect WITHOUT rebuilding
`trid3nt/elmfire:dev` - the parser/image law does not bind here (proven by the live
E2E through the unchanged image).

## Consequence

- `render_namelist(..., spotting_extra=)` emits a whole `&SPOTTING` group only when
  given (byte-identical base deck otherwise - 26 deck_builder golden tests green).
- `write_fbfm_with_break` stamps a non-burnable strip; `build_constant_flat_deck` /
  `solve_constant_case` thread `spotting_extra` + `fuel_break`; the far-side burned
  area is measured off the ToA raster (Invariant 1) in the shared helper.
- New template `elmfire_spot_fire_barrier_crossing` runs the OFF-vs-ON pair, asserts
  the jump, publishes the ON ToA COG, emits an OFF-vs-ON comparison chart.

## Residuals

- Board row 4 (stochastic ensemble): DEFERRED - needs multi-member Monte Carlo
  (`STOCHASTIC_SPOTTING` inert), a separate capability, not this binary's single-run
  bounds mechanism.
- The crown-fire-triggered ember spotting row (Crown section, `crown_fire_triggered_ember_spotting`)
  is now UNBLOCKED (both crown + spotting exist) but not built here - a clean follow-up
  (drop the surface-fire path, set `CROWN_FIRE_SPOTTING_PERCENT` on a canopied deck).
