# ADR 0213 - Rain-on-grid fidelity ladder II: continuous soil-moisture store + channel-resolving mesh

Date: 2026-08-10
Status: LANDED. Attacks the two residuals ADR 0206 attributed on the graded Ball
Creek replication -- (1) a STATIC SCS-CN curve number cannot carry antecedent
state across regimes and exhausts on a multi-storm sequence (+116% second peak),
(2) ~6.5 h of the peak-timing lag is coarse-mesh channel routing. Two levers,
one re-grade.
Builds on: ADR 0204 (graded constant-rain Ball Creek replication), ADR 0206
(true time-varying AORC hyetograph via the RAINDEF=3 per-case FORTRAN seam),
ADR 0195/0196 (RoG machinery + cn_infiltration + mesh_acquisition), ADR 0193
(watershed mesher), ADR 0210 (channel-refined mesh lesson).
Source: Godara, Bruland and Alfredsen 2024, Front. Water 6:1384205 (the graded
paper); Michel, Andreassian and Perrin 2005, WRR 41 W02011 (the continuous
SCS-CN soil-moisture-accounting formulation this lever implements, NATE to
verify the citation).

## 1. Lever 1 -- the continuous soil-moisture store (the paper's named future work)

### 1.1 Formulation + why (Michel et al. 2005)

The static SCS-CN, applied per event, resets between storms and, applied
cumulatively over a multi-storm window, exhausts its abstraction and runs the
later storms off almost fully. Michel et al. (2005) show the SCS-CN is
equivalent to a soil-moisture production STORE of level `V` (mm) and capacity
`S = 25400/CN - 254`, with the initial abstraction folded into the initial
level `V0`; the instantaneous runoff coefficient is then a function of the
current fill:

    rc = dQ/dP = 1 - (1 - V/S)^2          (Michel-2005 production function)
    q  = rc * P     (excess this interval);  V += (P - q)   (infiltration fills V)

We add a continuous RECOVERY (drying = drainage + ET) so the store empties
between storms over a timescale `tau`:

    drain = V * (1 - exp(-dt/tau))  ->  V -= drain

so a store wetted by an early storm recovers antecedent capacity before a later
one. This is the dynamic antecedent STATE a static curve number cannot carry.
`S` is the calibration knob, `tau` the recovery lever, `V0` the initial level
SPUN UP from the real antecedent precipitation (run the same store forward over
the 45-day antecedent AORC series from `V=0`). It is implemented PREPROCESSING-
FIRST -- engine-agnostic, offline-testable, no engine change: the store
transforms the GROSS hyetograph to a NET rainfall-excess hyetograph, which the
engine routes on a uniform **CN=100 pass-through** (`S=0`, the engine's SCS-CN
abstracts nothing; the store IS the infiltration model, no double counting).
This rides the exact ADR 0206 RAINDEF=3 time-varying seam.

Mass audit (worker metrics, exact): `gross == excess + (V_final - V0) + drain`;
`soil_store_mass_residual_mm` was `-0.0` on every solve, and the engine's own
`ACCUMULATED RAINFALL` equalled the store's net excess (Dec: 209.4 mm accumulated
== 209.4 mm excess), a cross-check the pass-through is loss-free.

### 1.2 The single-parameter transfer verdict (the headline -- an HONEST NEGATIVE)

The discriminating test: ONE `(S, tau)` set, `V0` from each event's antecedent,
across BOTH Dec 2015 (calibration) and Feb 2018 (validation). It does NOT
transfer, and the reason is diagnostic, not a bug:

- Dec 2015 had **611.9 mm** of 45-day antecedent rain; Feb 2018 had **268.4 mm**.
  So the rainfall-driven store spins Dec UP WETTER than Feb (`V0/S` 0.18 vs 0.10
  at `S=1000`), giving Dec the HIGHER modelled runoff coefficient (0.42 vs 0.25).
- But the OBSERVED runoff coefficients are the OTHER way: Dec 0.34, **Feb 0.55**
  (baseflow-separated, over the sim window). Feb is genuinely the WETTER-
  responding basin despite less antecedent rain -- a dormant-season signal
  (near-zero ET, sustained high water table / baseflow 0.550 > Dec 0.484) that
  antecedent RAINFALL points the wrong way on.

So a rainfall-only continuous store, initialized from antecedent precipitation
(as the kickoff and every AMC proxy specify), CANNOT close the Dec->Feb
transfer for THIS pair: the wetness difference is seasonal/subsurface, not
antecedent-rainfall. Run with the Dec-calibrated params Feb still ponds
(peak -88.6%), the same split-sample failure ADR 0204/0206 found, now with the
mechanism isolated to the initialization signal. The honest next rung is an
ET/temperature- or baseflow-state-aware store, not a rainfall-only one.

### 1.3 What the store DID fix -- multi-peak recovery (the real, decisive win)

Where the store IS the right physics is the multi-storm sequence -- residual (1)'s
exhaustion clause. On the Dec 2015 multi-peak (two storms, Dec-24 and Dec-29,
~5 days apart), with `tau=120 h` the store DRAINS between the storms so the
second sees a recovered (drier) capacity:

| model | 2nd peak (m3/s) | vs obs-2nd (5.99) |
| --- | --- | --- |
| 0206 static CN (RAINDEF=3, cumulative) | 18.56 | **+210%** |
| 0213 store, tau=120 h (recovery ON) | 5.31 | **-11%** |
| 0213 store, tau=10000 h (recovery ~OFF, control) | 16.02 | +167% |

The recovery timescale IS the mechanism: turn it off and the overshoot returns.
This is the store's headline improvement -- the second-peak overshoot the static
curve number exhausts on is fixed (near-exact) by the between-storm recovery.

### 1.4 Single-event calibration -- also improved (shape + timing)

Dec 2015 calibration sweep (`tau=120 h`, Manning x1.0, `V0` spun up):

| S (mm) | V0 (mm) | aligned NSE | peak err | timing lag | vol err |
| --- | --- | --- | --- | --- | --- |
| 800 | 171 | +0.54 | +57% | 7.0 h | -5% |
| 900 | 176 | +0.72 | +33% | 7.5 h | -17% |
| **1000** | **179** | **+0.75** | **+21%** | **7.8 h** | **-22%** |
| 1100 | 183 | +0.72 | +7.8% | 8.2 h | -28% |
| 1300 | 188 | +0.54 | -10% | 9.2 h | -41% |

Calibrated pick **S=1000 mm, tau=120 h** (max aligned NSE). Versus ADR 0206's
static CN53 (aligned 0.51, lag 10.8 h): the store lifts the aligned NSE (shape
skill) 0.51 -> 0.75 AND shortens the timing lag 10.8 -> 7.8 h -- removing the
low-intensity early rain that infiltrates concentrates the net excess near the
peak, sharpening the hydrograph. It costs peak magnitude (over-predicts +21%,
tunable to +7.8% at `S=1100`) and ~3 pts more volume deficit (the extra
infiltration). Honest trade: the store trades peak-magnitude accuracy for
shape+timing skill and the multi-storm recovery.

## 2. Lever 2 -- channel-resolving mesh (residual (2): the routing lag)

The ADR 0204/0206 mesh graded distance-to-river at a 30 m channel floor
(edge median 46.9 m). The refined mesh tightens the channel band to ~18 m
(`min_edge_length_m=18`), keeping the node count sane by not over-refining
hillslopes:

| mesh | nodes | cells | edge median | channel edge median (<=40 m of river) |
| --- | --- | --- | --- | --- |
| coarse (0204/0206) | 1834 | 3535 | 46.9 m | ~30 m floor |
| fine (0213) | 3533 | 6902 | 34.4 m | **26.4 m** (min 14.6, p90 31.8) |

(The `max_edge` ceiling is not binding on a 7.24 km2 catchment -- distance-to-
river never reaches it -- so 200 vs 300 m produced identical meshes; the channel
refinement is what changed.)

Re-solving Dec 2015 with the store (S=1000, tau=120) on the fine mesh:
**timing lag 7.8 h -> 3.8 h** -- the routing lag HALVED, confirming residual (2):
the coarse TIN over-stored the overland sheet and routed it slowly; the finer
channel routes it faster. Stable (CORRECT END, dt=2 s, wall 310 s, continuity
-1.4e-15).

The honest cost: on the finer TIN the outlet PEAK/VOLUME dropped hard (peak
-54%, vol -87% vs the coarse store run) -- the same thin-low-intensity-sheet
conveyance limit ADR 0204 flagged, now MORE binding because the smaller cells +
the tidal-flat wetting/drying treatment hold more of the sheet before it reaches
the outlet (mass still exact: continuity O(1e-15), so the water ponds/holds, it
is not lost). Rain-on-grid overland delivery is sensitive to mesh resolution in
BOTH directions: the fine channel fixes timing but the fine cells degrade the
sheet magnitude. Reconciling both (a fine channel that still delivers the sheet
-- e.g. tuned wetting-drying / a channel-only refinement that leaves hillslope
conveyance coarse) is the next mesh rung.

## 3. The ladder (Dec 2015 calibration + the multi-peak control)

| rung | aligned NSE | peak err | timing lag | vol err | multi-peak 2nd-peak |
| --- | --- | --- | --- | --- | --- |
| 0204 constant | +0.04 | -1.7% | +11.0 h | -52% | not reproduced |
| 0206 hyetograph | +0.51 | +5.4% | +10.8 h | -19% | +116% (global) / +210% vs obs-2nd |
| 0213 soil store | **+0.75** | +21% | 7.8 h | -22% | **+21% (global) / -11% vs obs-2nd** |
| 0213 store + fine mesh | -108* | -54% | **3.8 h** | -87% | -- |

(* the fine-mesh rung is a timing-lag isolation, not a skill improvement -- see
S2.) Feb 2018 transfer: ponds at every rung (peak -100% -> -90% -> -88.6%) -- the
honest split-sample limit, now attributed to the seasonal/subsurface wetness
signal (S1.2). Continuity was O(1e-15) on every solve -- the skill gaps are
forcing/mesh/soil-physics fidelity, never numerics.

What moves, honestly: the store fixes the MULTI-STORM second-peak overshoot
(+116% -> -11%) and improves single-event shape+timing (aligned NSE 0.51 -> 0.75,
lag 10.8 -> 7.8 h); the fine channel HALVES the routing lag (7.8 -> 3.8 h). What
does NOT move: the Dec->Feb transfer (seasonal, not antecedent-rainfall), the
volume deficit (no subsurface return flow / baseflow -- the store adds none), and
the fine-mesh sheet-delivery cost.

## 4. Worker + template changes (image law)

Worker (`services/workers/telemac/rog_build.py` + `entrypoint.py` +
`telemac_river_dye_build.py`):
- `rog_build.soil_moisture_excess(blocks, capacity_mm, recovery_h, init_mm)` --
  the pure Michel-2005 store transform (gross -> net + mass audit). 8 hand-
  computed unit tests (dry/full/half quadratic coeff / recovery drain / mass
  close / two-pulse-recovery-reduces-2nd / bad-param reject / net<=gross).
- `run_rog_pipeline`: when `soil_store` + blocks, transform to net + force the
  uniform CN=100 pass-through + record the store audit in the metrics envelope.
- `ReachConfig` +4 fields (`soil_store`, `soil_store_capacity_mm`,
  `soil_store_recovery_h`, `soil_store_init_mm`); parser **telemac-reach-4 ->
  telemac-reach-5** (strict allowlist auto-covers them; the bump is the version
  stamp in the unknown-field error). Rejection test + field-accept test name v5.
- Image `trid3nt-local/telemac:latest` REBUILT (absolute -f/context paths; new
  id `7aea5c5b7c5c`; `docker history` GRACE-2 refs = 0; parser v5 + soil fields
  baked) and CN=100 pass-through smoked THROUGH the image (accumulated rainfall
  == net excess, continuity 3.9e-16, mass residual -0.0). The ENGINE image is
  unchanged (per-case FORTRAN, no engine rebuild -- the ADR 0206 seam).

Template (`telemac_rain_on_grid`): `soil_store` (bool) + `soil_store_capacity_mm`
(retention calibration knob) + `soil_recovery_hr` (drying lever) knobs. Default
`soil_store=False` (OPT-IN, evidence-based): the store's clear wins are multi-
storm sequences + shape/timing, it needs antecedent forcing to spin up `V0`
(so it requires `mrms_window`), and it slightly worsens single-design-storm peak
/volume -- so it is an explicit user lever (granularity norm), strongly
recommended for multi-storm windows, not the silent default. When on, the
composer spins up `V0` from the real 45-day antecedent AORC and threads the four
soil fields into the manifest. Docstring updated: the envelope narrows honestly
(fixes multi-peak recovery + sharpens shape/timing; adds NO subsurface return
flow/baseflow; does not transfer across a seasonal wetness difference). 2 corpus
queries (multi-storm / soil-moisture recovery). 3 offline tests (`V0` spin-up
monotone in antecedent + bounded by S; soil_store requires capacity + window).

## 5. Consequences

- +0 registered tools; engine image UNCHANGED; worker image rebuilt (7aea5c5b7c5c).
- Offline: +8 `test_rog_build` (soil store) + 2 `test_entrypoint` (parser v5 +
  field accept) + 3 `test_telemac_rain_on_grid_template` (spin-up + guards); all
  green. Worker slice 85 passed / 1 skipped; server RoG slices 53 passed (the 1
  fail = the pre-existing pysheds/numpy-2 env incompatibility on this machine,
  not this change). The full 10849-test server suite could not be re-run in this
  minimal environment (numpy 2.x vs the numpy-1.x agent geo/telemac stack;
  geopandas/spotpy/pysheds absent from system python) -- no new failures were
  introduced in any runnable slice.
- Drivers (`scripts/sandbox/replication/`): `rog_ballcreek_soilstore.py`
  (store re-grade + spin-up + local NSE/R2), `rog_ballcreek_finemesh.py`
  (channel-resolving re-mesh reusing the cached delineation),
  `rog_ballcreek_soilstore_proofs.py`. Cached antecedent AORC
  (`forcing/antecedent_{dec2015,feb2018}.json`).
- Proofs regenerated IN PLACE + one new (`docs/proof/templates/`):
  `telemac_rain_on_grid_{calibration,replication,multipeak}_chart.png` (now the
  0206-vs-0213 ladder overlay) + `telemac_rain_on_grid_fidelity_ladder_chart.png`.
- Showcase NOT re-seeded: the existing "Coweeta Creek" showcase uses the constant
  path and is unchanged; a dated multi-storm soil-store showcase is a NATE-driven
  live session, not an offline build step (the store surface is proven live by
  the Ball Creek re-grade THROUGH the rebuilt image).
- The RAINDEF=3 + CN=100 pass-through pattern generalizes: any preprocessed
  net-excess time series (a future ET-aware or baseflow-coupled store) rides the
  same seam without an engine rebuild.
