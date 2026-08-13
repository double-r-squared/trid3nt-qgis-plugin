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

---

## AMENDMENT (2026-08-13) - the REAL-DATA river-barrier mode (the canonical demo)

NATE ruling (verbatim intent): the synthetic constant-fuel spotting deck is fine as
physics V&V but "just plain incorrect" as a real-world demo - and rendering a
synthetic deck over real basemap imagery IMPLIED a realism it lacked. The real
experiment: a fire meeting "a river mesh to act as a breaker ... the way it would be
used in real life", with ELMFIRE taking a real DEM (slope/aspect drive spread).

### Decision

`elmfire_spot_fire_barrier_crossing` gains a `mode` selector; the question class is
UNCHANGED (does ember spotting cross a barrier the contiguous front cannot?).

- `mode="real"` (NEW DEFAULT, the canonical entry): fetch REAL LANDFIRE FBFM40 +
  cbh/cbd/cc/ch + a USGS 3DEP DEM (with computed slope/aspect) over the caller's
  bbox - the SAME fetch path as `elmfire_fire_spread` (0231 parity). The barrier is a
  REAL RIVER: the LANDFIRE water class (FBFM40 code 98), which renders NON-BURNABLE
  (zero ROS), so the contiguous surface front stops at the near bank. The deck is
  built + solved TWICE on the SAME warp (spotting OFF then ON) - only the `&SPOTTING`
  namelist group differs, so the barrier + terrain are the identical landscape across
  the pair.
- `mode="verification"` (the former single mode): the constant flat grass deck with a
  synthetic NB1 strip; ASSERTS the clean OFF~0 / ON>0 discriminant. KEPT - a still-
  valid V&V path (delete-dont-disable does not apply). Both modes documented.

### The honest-verdict change (no assertion in real mode)

The verification mode hard-asserts the jump (`off <= 1e-3 < on`, honesty floor). The
REAL mode does NOT: per the ruling, "the river holds even with spotting" at a
realistic width is a VALID finding. The composer reports the physics - `break_jumped`
is 1 only when embers cleared a river the contiguous front could not, else the
verdict is `held` (or `inconclusive`); it never tunes inputs to force a crossing. An
`off_side_leaks` flag fires (honestly) if the OFF run put fire on the far side (the
contiguous fire flowed AROUND the river ends - the chosen reach did not fully
separate), so a confounded discriminant is surfaced rather than hidden.

### The river-split measurement (Invariant 1: measured off the grid)

`measure_river_split` (pure, unit-tested offline) splits burned cells relative to the
river read from the warped `fbfm40` grid:

1. Downwind is the +col (wind FROM the west) or -col (from the east) axis; a wind that
   is not E-W-dominant is rejected (a N-S river must be the CROSS-wind barrier).
2. Per raster row, the river crossing is the water run (>= 2 cells wide, skipping lone
   warped specks) nearest DOWNWIND of the ignition column; its near/far banks + width
   are recorded.
3. The river's contiguous cross-wind band around the ignition row is found (bridging
   <= 2-row warp-thinned bends); `river_width_m` = median run width x cellsize.
4. head fire = burned cells upwind of the near bank; far-side = burned cells downwind
   of the far bank, both summed WITHIN the band (so fire looping around the river ends
   outside the band is not miscounted as a jump). `river_band_coverage` reports how
   much of the domain height the band spans.

The river width is stated from the grid; the run guards that the head fire actually
reached the river (`head_area_km2 > 0`) before reporting a verdict.

### Consequence / surface

- `render_namelist(spotting_extra=)` already existed; `build_deck(..., spotting_extra=)`
  + `build_elmfire_deck(..., spotting_extra=)` now thread it onto the REAL-DATA deck
  (typed Python kwarg, NOT a dict-spec field - byte-identical when unset; the 26+1
  deck_builder golden tests stay green). NO worker-image rebuild (deck is server-side).
- `mode="real"` requires bbox + a user ignition UPWIND of the river (FIRE_IGNITION_
  REQUIRED otherwise, like `elmfire_fire_spread`); surfaces the fetched fuels (river =
  water class) + DEM as role=context inputs (0231).
- New offline test `server/tests/test_elmfire_river_barrier_split.py` (7 cases) +
  `test_build_deck_spotting_extra_threads_to_namelist` lock the math + the pass-through.
- Showcase canonical entry = the real river case; the toy stays as the verification.

### Live numbers (real river, through the baked image)

PENDING a LANDFIRE upstream outage (2026-08-13): during this build every LANDFIRE
host (`lfps.usgs.gov`, `landfire.gov`, `edcintl.cr.usgs.gov`) was unreachable (SSL
handshake timeout) while 3DEP (`elevation.nationalmap.gov`), ESRI imagery, and GitHub
all returned 200 - a LANDFIRE-specific outage, not a general network fault. The fuels
fetch is hard-required (no synthetic substitute - that is the toy this amendment
replaces), so the live real-river OFF/ON pair + its river-width/verdict numbers +
QGIS-true proofs are produced by `scripts/proof_elmfire_river_barrier.py` (which
auto-selects the widest grass-banked reach among Deschutes/John Day/Sacramento
candidates and auto-places the ignition upwind) once LANDFIRE recovers. The
verification (synthetic V&V) path was re-run LIVE through the unchanged image after
the refactor and is byte-for-byte the original discriminant: spotting OFF far-side =
0.0 km2, ON = 13.9041 km2 (15449 spot cells), `break_jumped=1` - proving the OFF/ON
spotting machinery + the refactor are intact.
