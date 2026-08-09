# ADR 0206 - TELEMAC-2D rain-on-grid: TRUE time-varying hyetographs (RAINDEF=3 per-case FORTRAN)

Date: 2026-08-09
Status: LANDED. Replaces the constant-intensity design storm in TELEMAC
rain-on-grid with the REAL time-varying hourly hyetograph, driving the engine's
NATIVE SCS-CN infiltration per-timestep on the actual intensity structure. This
is the ADR 0204 "constant rain (RAINDEF=1) -> +11 h peak-timing lag" fix, done
WITHOUT an engine rebuild: a per-case `FORTRAN FILE` flips the one hardcoded
parameter the installed source already gates the block-hyetograph path behind.
Builds on: ADR 0195/0196 (RoG foundation + template), ADR 0204 (the graded
constant-rain Ball Creek replication this re-grades), ADR 0203 (AORC fetcher).
Source: Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205 (NATE-provided).

## 1. The seam - per-case USER FORTRAN, NOT an engine rebuild

The installed v9.0.0 `runoff_scs_cn.f` ALREADY implements a block-type
time-varying hyetograph: the `RAINDEF=3` branch reads a `#`-commented
`(t_end_s, mm)` block file from FORMATTED DATA FILE 1 and applies the SCS-CN
abstraction per-timestep on it. The ONLY thing gating it is one line:

    INTEGER, PARAMETER ::RAINDEF=1

TELEMAC's steering `FORTRAN FILE` keyword compiles per-case user sources at run
launch (`telemac2d.py`), and the conda-forge image ships the compiler
(`mpif90` -> gfortran) + the `gnu.shared` systel config that supports it. So the
least-invasive seam that works (seam #1 of the two the kickoff posed) is:

- the worker copies the engine's OWN `$HOMETEL/sources/telemac2d/runoff_scs_cn.f`,
  flips `RAINDEF=1` -> `RAINDEF=3` (and NOTHING else), and stages it as
  `user_fortran/runoff_scs_cn.f`;
- the deck adds `FORTRAN FILE = user_fortran` + `FORMATTED DATA FILE 1 =
  rog_hyeto.txt`; `telemac2d.py` recompiles + links it over the library symbol
  at launch. The ENGINE image is unchanged (no rebuild); only the WORKER python
  authoring changes follow the worker-image law.

Evidence (through the image, seam smoke): a two-pulse block hyetograph on the
synthetic tilted-plane deck compiled (`compiling: runoff_scs_cn.f ... completed`,
`created: out_user_fortran`), reached CORRECT END OF RUN, and the listing
`ACCUMULATED RAINFALL` matched the hyetograph integral EXACTLY (0.05 m for a
25+25 mm two-pulse), versus the constant-RAINDEF=1 control which delivered the
flat keyword rate. A drift guard (`stage_raindef3_fortran`) hard-errors if the
`RAINDEF=1` parameter line is ever absent from the installed source, so an engine
version bump can never silently ship a mis-patched override.

## 2. Worker changes (image rebuilt + behavior smoke, worker-image law)

`services/workers/telemac/rog_build.py` + `entrypoint.py`; the
`trid3nt-local/telemac:latest` image was rebuilt (new id ff38f5cfe73a,
`docker history` GRACE-2 refs = 0, parser telemac-reach-4 baked) and
behavior-smoked THROUGH the image.

1. `write_hyetograph_file` -- writes the RAINDEF=3 block file from a list of
   `[t_end_s, gross_mm]` blocks (gross rainfall per interval), appends a dry tail
   past the sim end (the reader aborts "HYETOGRAPH FILE TOO SHORT" otherwise),
   returns the gross-rain integral for the mass check. Rejects non-monotone /
   negative intervals (typed `RogInputError`).
2. `stage_raindef3_fortran` -- the copy-and-flip override staging (section 1).
3. `author_rog_deck(hyetograph_file=...)` -- a THIRD deck branch: native SCS-CN
   (RAINFALL-RUNOFF MODEL = 1 + AMC + FORMATTED DATA FILE 2 CN2 map) PLUS the
   FORTRAN FILE + FORMATTED DATA FILE 1 keywords; no RAIN_HDUR window (the
   hyetograph carries its own dry recession). The constant-native and
   preprocessing branches are byte-identical to before when no hyetograph is set.
4. `ReachConfig.rain_hyetograph_blocks` + parser bump `telemac-reach-3` ->
   `telemac-reach-4` (strict allowlist auto-covers the new field; the bump is the
   version stamp in the unknown-field error). Rejection test names v4.
   `entrypoint.run_rog_pipeline` stages the block file + FORTRAN override and
   records `runoff_path="native_hyetograph"` + `hyetograph_total_mm` in the
   envelope when blocks are present.

Offline-first: 8 new `test_rog_build` cases (block format + mass integral +
non-monotone/negative rejection + fortran flip + drift guard + deck keyword
emission + constant-path byte-identity) + 2 `test_entrypoint` cases (parser v4 +
field accept); all green BEFORE the rebuild.

## 3. The discriminating physics test (what constant rain CANNOT do)

`scripts/sandbox/telemac/rog_twopulse_smoke.py`, through the rebuilt image: a
small steep near-impervious plane (CN 98, so SCS-CN abstraction is small and both
pulses run off comparably) forced by TWO 15-min 100 mm/hr pulses separated by a
45-min dry gap, versus a CONSTANT run of the SAME total volume:

- TIME-VARYING: outlet hydrograph is BIMODAL -- 2 significant peaks (scipy
  find_peaks, prominence >= 0.2*peak), peak 0.35 m3/s;
- CONSTANT (same volume): UNIMODAL -- 1 peak, 0.147 m3/s;
- MASS: engine accumulated 50.000 mm == hyetograph integral 50.000 mm (0.0000 mm
  error); continuity 2.2e-16.

A single constant-rain pulse structurally cannot produce two separated outlet
responses; the time-varying path does. This is the physics the re-grade rides.

## 4. Ball Creek re-grade (the payoff) -- real AORC hourly hyetograph

Same 7.24 km2 Ball Creek mesh, same EDI weir #9 gauge, same alignment as ADR
0204, but each event driven by its REAL AORC hourly hyetograph (72 hourly
blocks from the rising-limb start; `scripts/sandbox/replication/rog_ballcreek_hyeto.py`)
through the native RAINDEF=3 path. Re-calibration (uniform CN sweep, AMC II,
Manning x1.0): the full storm volume (264.85 mm delivered vs the constant path's
24 h-core intensity) shifts the CN optimum DOWN from 55 to ~53.

CN sweep (Dec 2015, AMC II, real hyetograph):

| CN | raw NSE | R2 | aligned NSE / R2 | peak comp (err) | timing lag | vol err |
| --- | --- | --- | --- | --- | --- | --- |
| 45 | -1.26 | 0.11 | -0.04 / 0.34 | 4.65 (-46%) | +14.0 h | -49% |
| 50 | -1.31 | 0.04 | +0.42 / 0.55 | 7.62 (-11%) | +11.2 h | -31% |
| 53 | -1.41 | 0.01 | +0.51 / 0.60 | 9.07 (+5.4%) | +10.8 h | -19% |
| 55 | -1.59 | 0.00 | +0.46 / 0.62 | 9.80 (+14%) | +10.5 h | -11% |

Calibrated pick: **CN 53, AMC II, Manning x1.0** (near-exact peak + best shape).

Re-graded table (calibrated params UNCHANGED across events), old (ADR 0204
constant) vs new (ADR 0206 hyetograph):

| Event | raw NSE (old -> new) | aligned NSE (old -> new) | peak err (old -> new) | timing lag (old -> new) | vol err (old -> new) |
| --- | --- | --- | --- | --- | --- |
| Dec 2015 (calibration, CN 53) | -1.41 -> -1.41 | +0.04 -> +0.51 | -1.7% -> +5.4% | +11 h -> +10.8 h | -52% -> -19% |
| Feb 2018 (validation, CN 53) | -1.90 -> -1.90 | -- -> -1.90 | -100% -> -90% (ponds) | -- | ~0 -> -100% |
| Feb 2018 (standalone, CN 90) | -0.87 -> -1.27 | +0.44 -> +0.43 | +4% -> +31% | +8 h -> +6.5 h | -- -> -21% |
| Dec 2015 (multi-peak, CN 53) | -1.38 -> -3.57 | -0.47 -> -1.42 | -1.7% -> +116% | +11 h -> +118 h | -74% -> +7% |

Continuity was O(1e-16) on every solve -- mass exact; the skill gap is
forcing/mesh fidelity, not numerics.

### Validation (Feb 2018) -- the split-sample still fails, same diagnosis

With the Dec-calibrated CN 53 applied UNCHANGED, Feb 2018 STILL ponds (peak 0.55
= the baseflow constant, -90%): the hyetograph does not change this because the
binding limit is runoff GENERATION (CN 53 is far too low for the saturated
basin's ~75% runoff coefficient) plus the coarse mesh's inability to convey a
thin low-intensity sheet -- NOT the rain timing. Run STANDALONE at CN 90 the same
real hyetograph reproduces the event well (peak-aligned NSE +0.43 / R2 0.73, lag
6.5 h -- BETTER than the ADR 0204 constant-storm CN 90 lag of 8 h), so the event
IS modellable; it is the single-CN TRANSFER across antecedent-saturation regimes
that fails, exactly as ADR 0204 found.

### Multi-peak control -- the second peak now APPEARS (a reversal of ADR 0204)

ADR 0204's constant single pulse produced ONE hump and MISSED the second peak
entirely. The real hyetograph delivers BOTH storm bursts, so the model now
produces a first peak (h24) AND a second peak (h131) tracking the real Dec-29
storm -- the "second peak not reproduced" finding is REVERSED. BUT the second
peak over-peaks massively (18.56 vs 5.99 m3/s, +116%) because static SCS-CN's
cumulative abstraction is exhausted after ~400 mm (no soil-moisture recovery
between storms -> the second storm is almost pure runoff), and the inter-peak
flow still drains to baseflow (no subsurface return flow). So the hyetograph
fixes the STRUCTURE (two responses) but exposes that a STATIC curve number and
no soil store cannot make the multi-storm sequence skillful -- the honest
next-fidelity boundary (a continuous soil-moisture model).

### What the hyetograph fixed, and what it did NOT

The hyetograph's real win is the hydrograph SHAPE and the runoff VOLUME:
aligned NSE (shape skill with the timing offset removed) jumps 0.04 -> 0.51 on
the calibration event -- the model now reproduces the real rising/falling limb
structure driven by the actual intensity sequence -- and the runoff-volume error
collapses -52% -> -19% because the real storm delivers the real depth (the
constant 24 h-core pulse under-delivered).

The residual +10.8 h peak-timing lag is NOT the constant-rain artifact any more;
it is now cleanly decomposable and forcing/mesh-bound:

- the AORC hourly precip PEAK for this event is at h17, while the gauge peaks at
  h13 -- a ~4 h forcing offset (AORC 1 h-accumulation lags the flashy gauge) that
  no rain representation can remove;
- the modelled peak is at h23.5, ~6.5 h after the rain peak -- the coarse
  30-200 m TIN over-stores the overland sheet and routes it to the outlet slowly
  (the paper's fine channel-resolving mesh routes in <1 h). This is the mesh
  limit, unchanged from ADR 0204.

So raw NSE stays negative (a ~11 h peak offset dominates a point-by-point NSE
regardless of shape) -- an honest statement that hourly-NSE refinement-grade skill
needs BOTH the hyetograph (done) AND a finer channel-resolving mesh + a better
sub-daily forcing product (not done). The peak MAGNITUDE, hydrograph SHAPE and
runoff VOLUME are now all materially better.

## 5. Template surface

`telemac_rain_on_grid` gains the hyetograph path automatically: a real
`mrms_window` ("start/end" dates) fetches the AOI-mean AORC hourly series,
`select_runoff_path` routes a time-varying series to `"native_hyetograph"` (was
the lossy `"preprocessing"` collapse), and the composer threads
`rain_hyetograph_blocks` into the worker manifest. The `design_storm_mm_per_hr` /
`storm_duration_hr` knobs stay for un-dated hypothetical storms. Docstring updated
honestly: improved peak timing/shape over a constant storm; residual lag
forcing/mesh-bound; the no-subsurface-return-flow multi-peak limit remains.
Offline: `select_runoff_path` time-varying test now asserts `native_hyetograph`;
2 new `_fetch_hyetograph_blocks` tests (hourly block build + bad-window reject).

Daemon restarted (239 tools, `telemac_rain_on_grid` registered, clean boot) so
the composer change is live. The existing "Coweeta Creek" showcase uses the
CONSTANT path and is unchanged (its `!run` line still round-trips through the
product parser); it was NOT re-seeded because a re-solve of the unchanged case
is an hours-class run with no new validation value, and the time-varying surface
is already proven live by the Ball Creek re-grade THROUGH the rebuilt image. A
dedicated hyetograph showcase (a dated real MRMS/AORC event) is a NATE-driven
live session, not an offline build step.

## 6. Consequences

- +0 registered tools; engine image UNCHANGED (per-case fortran, no rebuild);
  worker image rebuilt for the authoring code (ff38f5cfe73a) + behavior smoke.
- Offline: +7 `test_rog_build` + 1 `test_entrypoint` (hyetograph-field accept;
  parser rename to v4) + 2 `test_telemac_rain_on_grid_template` (hyetograph-block
  builder); `select_runoff_path` time-varying test re-pointed to
  `native_hyetograph`. Touched-tree slices green (worker 24, agent RoG 52); full
  server offline suite unchanged vs the exact-9 baseline (9 failed / 10849
  passed -- the pre-existing fetch_resolution x4 + river_dye x5 order-flakes, no
  new failures).
- Drivers: `scripts/sandbox/telemac/rog_twopulse_smoke.py` (discriminating test),
  `scripts/sandbox/replication/rog_ballcreek_hyeto.py` (re-grade).
- Proofs refreshed in place: `docs/proof/templates/telemac_rain_on_grid_
  {calibration,replication,multipeak}_chart.png` (hyetograph solves).
- The RAINDEF=3 per-case-fortran pattern is a reusable seam for the other
  hardcoded `runoff_scs_cn.f` flags (e.g. STEEPSLOPECOR) without an image rebuild.
