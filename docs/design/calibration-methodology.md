# Calibration methodology: coastal surge against a real event

**STATUS: PROPOSED - FOR NATE SIGN-OFF, NO RUNS UNTIL SIGNED.**

Date: 2026-08-26. Written research-first, before any code and before any
run, per the standing experiments rule: NATE signs off methodology and
inputs BEFORE runs, and grading is deterministic code against fetched
observations, never a model's judgement.

Everything below that describes the world was probed read-only against the
live APIs while writing this document, and the numbers are quoted as
measured. Everything that describes what we would BUILD is a proposal with
an open question attached wherever the evidence did not settle it.

ADR 0319 named the gap this document closes: the rerun primitive is the
loop's engine, and the three pieces it still needs are OBSERVATIONS to
score against, an OBJECTIVE that turns answer-vs-observation into a
scalar, and a PROPOSER that picks the next override. This document
proposes all three, plus the case they run on.

The hard architectural rule for the wave that follows, restated so it
cannot be lost: **the loop driver CONSUMES `rerun_workflow` and never
grows its own re-run path.** Two implementations would disagree about what
a derived run is.

---

## 1. The case

### 1.1 The event

**Hurricane Michael (AL142018), landfall near 1730 UTC 10 October 2018**,
near Mexico Beach and Tyndall Air Force Base, Florida, at an assessed
intensity of 140 kt and 919 mb - category 5 on the Saffir-Simpson scale,
the strongest hurricane landfall of record in the Florida Panhandle (NHC
Tropical Cyclone Report, Beven, Berg and Hagen, 17 May 2019).

The case is proposed because our substrate already runs it. The committed
`coastal_tidal_surge` canaries - both the coarse and the refined leg - are
ALREADY Apalachicola, station 8728690, window 2018-10-09 to 2018-10-11.
The template, the fetch, the datum chain, the mesh and the solve are
proven on this exact event; what is missing is only the observations, the
objective and the proposer. That is the cheapest possible way to start a
calibration track, and it is not a coincidence to be talked around: it is
the reason to pick this event over any other.

### 1.2 The domain

Two configurations are on the table. They differ in one thing that decides
whether the calibration is meaningful, and NATE has to pick.

**Configuration A - the canary domain, unchanged.**
`bbox = [-85.02, 29.69, -84.90, 29.80]`, about 12 km by 12 km around the
Apalachicola waterfront. Forced by CO-OPS 8728690 at the seaward edge.

This is what runs today. Its weakness is structural and should be stated
before anything else: the forcing gauge sits INSIDE the domain, a couple
of kilometres from the boundary that carries its own series. Over that
distance and that depth, bottom friction has almost no leverage on the
water level at the gauge, so the objective would be nearly
boundary-determined and the friction parameter would be weakly
identifiable. Calibrating on it risks producing a number that looks
converged and means nothing.

**Configuration B - Apalachicola Bay, seaward boundary on the Gulf shelf.
RECOMMENDED.**
`bbox` approximately `[-85.15, 29.55, -84.70, 29.85]`, about 43 km by 33
km: the whole bay, St George Island and its passes, and open Gulf water
south of the barrier island. The seaward liquid boundary sits on the Gulf
side of St George Island; the bay interior, including the Apalachicola
waterfront, is INTERIOR to the domain.

Configuration B is recommended because the observations show the physics
we would be calibrating actually happening across it. The measured peak
times, from the probes in section 1.4:

| location | lon | peak (m NAVD88) | peak time UTC |
|---|---|---|---|
| USGS 9459, Rish Rec Park (St Joseph Peninsula, open Gulf) | -85.391 | 2.411 | 17:02 |
| USGS 9461, St George Island State Park (Gulf side of the barrier) | -84.762 | 2.478 | 17:38 |
| USGS 9468, Apalachicola Battery Park (bay head) | -84.983 | 2.506 | 18:22 |
| CO-OPS 8728690, Apalachicola (bay head) | -84.981 | **2.613** | **18:12** |

The bay head peaks 34 to 44 minutes AFTER the Gulf side of the barrier
island, and about 0.13 m higher. That lag and that amplification are the
signal bottom friction controls over a shallow bay, and they are what
makes configuration B a calibration rather than a curve-fit. Configuration
A contains none of it.

Configuration B has a cost and a dependency, both open questions in
section 8: the run is roughly 15x the nodes and 4x to 8x the timesteps
(section 4.4), and the seaward boundary needs a forcing series that is not
one of the gauges we are validating against (section 1.3).

### 1.3 Boundary forcing: which gauge forces, which validate

**This is the single most important methodological choice in the document,
and it is where circularity would enter if we were careless.**

In configuration A the forcing gauge and the only in-domain gauge are the
same instrument. Scoring a run at 8728690 that was forced by 8728690's own
record is not validation; it is a check that the boundary file was written
correctly. Configuration A can therefore only be validated against the
HIGH-WATER MARKS (section 1.4), which are spatially independent of the
boundary even though the driving signal is not.

In configuration B, three forcing options exist, and only two are honest:

1. **GTSM v3.0 tide-plus-surge reanalysis at the offshore boundary**
   (`fetch_gtsm_tide_surge`). Fully independent of every validation gauge.
   This is the methodologically clean option. It is KEYED: the spec
   declares `auth.mode: cds` with `TRID3NT_COPERNICUS_CDS_API_KEY`, and
   whether that key exists in the vault is an open question (section 8,
   Q3). GTSM is itself a model, not a gauge, so its own error enters our
   boundary; the spec's own caveat says as much.
2. **CO-OPS 8728690 at the offshore boundary, with the circularity
   declared.** Legal only if 8728690 is then EXCLUDED from the objective
   and used for nothing, with the other gauges and the HWMs carrying the
   whole score. Cheap, needs no new key, and is a defensible fallback.
   The cost: we lose the best-instrumented station in the bay from the
   validation set, and the boundary carries a bay-head signal onto an
   offshore edge, which is a known bias we would be asking friction to
   absorb.
3. **CO-OPS 8728690 forcing AND scoring.** Not legal. Named here only so
   it is on the record as rejected.

**Proposed: option 1, falling back to option 2 if the CDS key is absent,
with the fallback LOUD (a cross-dataset substitution, per the data-source
fallback norm) and recorded in the run journal, never silent.**

**The verification set (configuration B, option 1):**

| role | source | what it scores |
|---|---|---|
| forcing | GTSM v3.0 at the offshore edge | nothing |
| validation, primary | CO-OPS 8728690, 6-min water level, NAVD88 | full hydrograph: RMSE, NSE, KGE, peak, phase |
| validation, holdout | USGS STN 9461 St George Island SP | peak stage + peak time |
| validation, holdout | USGS STN 9468 Battery Park | peak stage + peak time |
| validation, spatial | USGS STN HWMs, Michael, in-domain | peak WSE at 100+ points |

That satisfies the requirement of at least one verification gauge not used
for forcing several times over: under option 1 NOTHING in the validation
set forces anything, and under option 2 three of the four still do not.

### 1.4 What the gauges actually recorded: probe findings

Probed read-only 2026-08-26 against `api.tidesandcurrents.noaa.gov` and
`stn.wim.usgs.gov`. Raw findings are reproduced in full because the
question "is the record usable" is exactly what a methodology sign-off
turns on.

**CO-OPS water level, `datum=NAVD`, `units=metric`, `time_zone=GMT`,
window 2018-10-08 to 2018-10-12 (5 days, 6-minute samples):**

| station | name | rows | expected | nulls | gaps > 6 min | peak m NAVD88 | peak time UTC |
|---|---|---|---|---|---|---|---|
| 8728690 | Apalachicola | 1200 | 1200 | 0 | 0 | **2.613** | **2018-10-10 18:12** |
| 8729108 | Panama City | 1200 | 1200 | 0 | 0 | 1.856 | 2018-10-10 18:06 |
| 8729210 | Panama City Beach | 1200 | 1200 | 0 | 0 | 1.439 | 2018-10-10 15:48 |
| 8729840 | Pensacola | 1200 | 1200 | 0 | 0 | 1.025 | 2018-10-10 09:18 |
| 8727520 | Cedar Key | 1200 | 1200 | 0 | 0 | 1.717 | 2018-10-10 19:54 |

**Verdict: the 8728690 record is fully usable and requires no gap
handling.** Every one of the 1200 expected 6-minute samples is present,
every sample carries `q="v"` (verified, not preliminary), and the record
is continuous straight through landfall. The QC flag field is
`"0,0,0,0"` on 1199 of 1200 samples with a single `"0,1,0,0"`. The gauge
did not fail during Michael.

For contrast, the two Panama City gauges are NOT clean through the event.
8729108 carries six `"1,0,0,0"` flags and its sigma jumps from ~0.02 to
0.144 across the peak, with the water level stepping 1.072 m at 17:00 to
1.834 m at 18:00; 8729210 carries five `"1,0,0,0"` and six `"0,0,1,0"`
flags and a sigma of 0.2 to 0.3 throughout. Neither is proposed for use.
They are also 70 km and 90 km west of Apalachicola, in a different
embayment, so no plausible domain contains both them and our bay.

**The CO-OPS station catalog** (`mdapi/prod/webapi/stations.json`,
`type=waterlevels`) returns 301 stations nationally, of which exactly
THREE fall in the window 28.8 to 30.6 N, -86.5 to -83.5 W: 8728690,
8729108 and 8729210. **There is no second CO-OPS gauge inside Apalachicola
Bay.** A CO-OPS-only holdout is therefore impossible at any domain size we
would want to solve, which is why the USGS STN network below carries the
holdout role.

**Datum chain, verified end to end.** The template fetches on MLLW
(`fetch_noaa_coops_tides` hardcodes `datum: MLLW`) and reconciles to the
NAVD88 bed with the station's OWN published datum table
(`workflows/shared/tide_series.py:datum_offset_m`, which raises rather
than returning zero on any miss). Cross-check: the committed canary
records `wl_max_m = 2.845` on MLLW and `datum_offset_m = -0.232`, giving
`sl_peak_m = 2.613`. My independent NAVD88 probe returns 2.613. **The
datum chain is correct and needs no change.**

The NHC report corroborates independently: "the NOS gauge in Apalachicola
measured a peak water level of 7.7 ft MHHW".

**USGS STN, event 287 "2018 Michael"** (`Events.json` resolves the name;
`fetch_high_water_marks` already does this resolution in its router hook):

- **522 high-water marks in Florida.** This matches USGS OFR 2019-1059
  exactly ("a total of 522 high-water marks were recovered and surveyed
  from 331 sites"), so the API is serving the published set.
- In a bay-wide bbox `[-85.4, 29.5, -84.6, 30.1]`: **249 HWMs, all 249 on
  NAVD88**, quality Excellent (+/- 0.05 ft) 68, Good (+/- 0.10 ft) 101,
  Fair 39, Poor 32, VP 5, Unknown 4. 159 are flagged `stillwater=1`. 126
  are both stillwater and Excellent-or-Good. Types: 180 seed line, 42
  debris, 26 mud.
- In the tight configuration-A bbox: 12 HWMs, all NAVD88, elevations 8.12
  to 9.25 ft (2.47 to 2.82 m NAVD88) - bracketing the gauge peak of 2.613
  m, which is a good sign that gauge and marks describe the same water.
- **55 deployed instruments** for the event (52 pressure transducers, 3
  rapid-deployment gages), of which 8 fall in the bay-wide bbox. Four
  matter: 9451 Indian Pass, 9459 Rish Rec Park, 9461 St George Island SP,
  9468 Apalachicola Battery Park. The NHC report names the same three
  Franklin County sensors, so this is the published deployment.
- Peak summaries, all on `vdatum_id=2` = NAVD88, none estimated:
  9461 peak 8.13 ft at 2018-10-10T17:38 UTC ("filtered water level data");
  9468 peak 8.22 ft at 18:22 UTC; 9459 peak 7.91 ft at 17:02 UTC.
  Instrument 9451 has no data file and no peak: **it is unusable, and the
  methodology must not assume it.**
- Sensor TIME SERIES: available but not directly. `DataFiles/{id}/Files`
  serves two artifacts per instrument. The `.csv` is the RAW HOBO logger
  export in **absolute pressure, psi**, at 30 s (12,212 rows for 9461).
  The `_chopped.nc` is the processed CF-1.6 NetCDF carrying `sea_pressure`
  (barometrically corrected), with
  `geospatial_vertical_reference = NAVD88`,
  `sensor_orifice_elevation_at_deployment_time = 2.3134` m and
  `salinity = "Salt Water (> 30 ppt)"`. Turning that into a NAVD88 water
  level is a real, documented, deterministic conversion (head from sea
  pressure, plus orifice elevation), but it IS a conversion we would be
  writing. **Proposed: phase 1 uses the PEAK SUMMARY scalars only, which
  need no conversion; the full sensor hydrograph is a phase-2 option.**

### 1.5 The window

**Proposed: 2018-10-09T12:00Z to 2018-10-11T00:00Z, 36 hours**, with a
6-hour spin-up before the surge arrives and the full rise and fall
contained. The committed canary uses `start_date=2018-10-09`,
`end_date=2018-10-11` for the FETCH and then simulates only
`duration_hours=6.0` of it; the fetch window can stay, the simulated
window has to grow to contain the peak and its recession or the phase
metric has nothing to measure.

Open question Q5: 36 h at the configuration-B node count is the dominant
cost term. A 24-hour window (2018-10-10T00:00Z to 2018-10-11T00:00Z) still
brackets every observed peak in the table above, with less spin-up.

---

## 2. What is calibrated

Each entry below is a **declared `Param` that `rerun_workflow` can already
override today**, which is the point: nothing in this section needs new
plumbing, only a proposer that names values. Bounds quoted are the ones
`coastal_tidal_surge/declarations.py` declares now.

### 2.1 `friction_coefficient` - the classic first, and the only one that has to be there

- Door: SCENARIO. Default 40.0. Declared bounds `(0.001, 200.0)`.
- Under the default `friction_law = 3` (Strickler) this is a Strickler
  Ks in m^(1/3)/s, where HIGHER is SMOOTHER, and `n = 1/Ks`.
- **Proposed calibration bounds: Ks in [25, 70]**, that is Manning n in
  [0.014, 0.040]. Physical justification: an Apalachicola Bay bed is
  sand and mud with marsh fringes and oyster reef. Chow's open-channel
  table puts clean sand at n ~ 0.020 (Ks 50), natural sandy-bed channels
  at n 0.025 to 0.035 (Ks 29 to 40), and vegetated marsh well above that.
  The declared default of 40 (n = 0.025) sits mid-band, which is a good
  sign the default was chosen and not guessed. The band is deliberately
  NARROWER than the declared `Param` bound because the declared bound has
  to span three friction laws (ADR 0319) while a calibration search should
  not wander into values that are the other quantity.
- **The coupled-validity rule already guards the failure mode**:
  `friction_coefficient_matches_law` refuses `COUPLED_VALIDITY_REFUSED`
  before anything runs if a proposer moves the coefficient across the
  crossover without moving the law. A sweep can therefore be written
  naively and will still be caught. That is worth exercising deliberately
  once as part of the proof.

### 2.2 `wind_speed_mps` and `wind_direction_from_deg` - earn their place only conditionally

- Doors: SCENARIO. Defaults 0.0 and 0.0. Bounds `(0.0, 80.0)` and
  `(0.0, 360.0)`.
- Michael is emphatically wind-driven, and published surge hindcasting
  treats wind drag as a first-class calibration axis alongside bottom
  friction (Bhaskaran et al. and the Bay of Bengal / Hurricane Rita
  literature both run bottom-friction x wind-drag sensitivity grids).
- **But our template's wind is a single CONSTANT speed and direction over
  the whole domain for the whole run.** A category 5 eyewall passing 40 km
  west of the domain in 3 hours is not a constant wind field. Fitting a
  constant wind to a moving vortex would be pure compensation: the number
  that came out would absorb every other error in the model, and it would
  have no physical meaning at all.
- **Proposed: wind is NOT calibrated in phase 1. It is held at 0.0 and
  the honesty statement says so** - the run reproduces the surge that
  arrives THROUGH the boundary, not the local wind set-up. Phase 1 asks a
  narrower question than "can we hindcast Michael", and says so.
- Recorded as open question Q2: a spatially and temporally varying wind
  field (`fetch_storm_tracks` exists; TELEMAC-2D reads a meteo file) is a
  real capability gap this case surfaces, and it belongs to NATE to
  schedule, not to this loop to smuggle in.

### 2.3 Held fixed, deliberately

Named here because a value nobody named is a value nobody chose:

- `friction_law = 3` (Strickler). Moving it mid-loop changes what the
  coefficient means.
- `time_step_s = 20.0`. Numerics, not physics. If dt moved with the
  proposal, the objective would be scoring the time integration and the
  bed roughness with one number.
- `target_resolution_m`. Same argument: mesh resolution is a fidelity
  choice, and mixing it into the objective produces a calibrated friction
  that is really a discretisation correction. It is laddered ACROSS
  phases (section 4.4), never varied WITHIN a loop.
- `datum_offset_m`. Derived from the station's own table. If it were free,
  the loop would happily calibrate the vertical datum, which is the exact
  defect `tide_series.py` was written to prevent.
- `bathy_source = noaa_demall`, `series_type = observed`, `ocean_edge`.

### 2.4 A candidate that does NOT earn its place yet

Turbulence / eddy viscosity is a standard TELEMAC-2D calibration knob and
the Patos Lagoon calibration study varies it explicitly. It is **not a
declared `Param` on `coastal_tidal_surge` today**, so proposing it would
mean widening the template's contract before the loop exists. Deferred,
with the note that it is the obvious second axis if friction alone cannot
reach the acceptance thresholds.

---

## 3. The objective

### 3.1 Everything is computed by code, against fetched bytes

The metrics are computed by `compute_skill_metrics`, which is already
registered and already delegates every metric to
`spotpy.objectivefunctions` rather than reimplementing the math: NSE, KGE,
PBIAS, RSR, RMSE, R2, plus peak error percent and peak-timing error. The
pairing is done by `extract_model_at_observations`, which already handles
BOTH shapes we need - a static max-WSE raster against surveyed HWM points,
and a time-series point layer against a time-series point layer, and which
already names `fetch_noaa_coops_tides` as one of the shapes it consumes.
It reconciles vertical datum and physical quantity, and it raises typed
rather than pairing a depth against an elevation.

**No model judges anything at any point.** The proposer sees a scalar that
code produced from bytes on disk.

### 3.2 The primary metric

**Proposed primary: `CF(0.15 m)` at the primary validation gauge - the
central frequency, the percentage of 6-minute paired errors whose absolute
value is at or below 0.15 m - with the acceptance threshold `CF >= 90%`.**

Why this one, and why that number. NOAA's own standard for accepting an
operational water-level model is NOS CS 17 (Hess et al., 2003), section
9.4 and Table 4: "For h, hnn, ahw, and alw, the acceptable error X is 15
cm (0.5 ft)", with the criteria `CF(X) >= 90%`, `POF(2X) <= 1%`,
`NOF(2X) <= 1%`, `MDPO(2X) <= L`, `MDNO(2X) <= L` and `WOF(2X) <= 0.5%`,
where L = 24 hours. It is the closest thing to a published, numeric,
agency-owned pass mark for exactly the quantity we are producing, and it
is stated in metres of water rather than in a dimensionless index, which
makes a failure legible.

That said, CF is a threshold count, which makes it FLAT: many friction
values give the same CF, and a proposer cannot descend a flat surface.
So:

**Proposed optimisation objective: minimise RMSE at the primary validation
gauge.** RMSE is continuous, differentiable in practice, in metres, and is
the natural companion to a 15 cm acceptable error. CF is the ACCEPTANCE
gate; RMSE is what the loop descends. The two are reported together at
every iteration and the distinction is stated in the journal, because a
loop that optimises one number and is graded on another has to say so out
loud.

### 3.3 The full reported set, every iteration

| metric | source | role |
|---|---|---|
| RMSE (m) at primary gauge | `compute_skill_metrics` | **objective, minimised** |
| CF(0.15 m) (%) | new, computed alongside | **acceptance gate, >= 90%** |
| NSE | `compute_skill_metrics` (spotpy) | reported; Moriasi band |
| KGE | `compute_skill_metrics` (spotpy) | reported |
| PBIAS (%) | `compute_skill_metrics` (spotpy) | reported; bias direction |
| RSR | `compute_skill_metrics` (spotpy) | reported; Moriasi band |
| peak error (%) and (m) | `compute_skill_metrics` | reported; surge magnitude |
| peak-timing error (min) | `compute_skill_metrics` | **phase; see 3.4** |
| RMSE (m) vs the HWM cloud | pairing lane C + skill metrics | spatial, holdout |
| peak-stage error at 9461, 9468 | scalar diff | holdout |

`CF(0.15 m)`, `POF`, `NOF` and the outlier durations are the only pieces
`compute_skill_metrics` does not already return. They are arithmetic over
the same paired array, and **proposed: they are added to
`compute_skill_metrics` as a `variable="water_level"` branch**, beside the
existing `variable="head"` SRMS branch, rather than computed in the
calibration lane. A metric that lives in the loop is a metric no other
caller can have.

Secondary bands, for reporting rather than gating: Moriasi et al. (2007)
grade NSE > 0.75 as "very good", 0.65 to 0.75 "good", 0.50 to 0.65
"satisfactory". `compute_skill_metrics` already carries this table and
already stamps `verdict_is_heuristic = true` on it. That stamp stays.

### 3.4 Phase error, and why it may need its own decision

Peak-timing error is the metric that would actually discriminate friction
in configuration B, because the bay-head lag is what friction sets. The
observations give it to us to the minute: 17:38 at the barrier, 18:12 and
18:22 at the bay head.

But the model's temporal resolution caps it. The committed refined canary
wrote 41 output frames over 6 h, which is one frame every ~9 minutes; over
36 h at the same `output_interval_min` it would be far coarser. **Proposed:
`output_interval_min` is pinned to 6.0 for calibration runs, matching the
CO-OPS sample interval exactly**, so the pairing is exact rather than
nearest-within-tolerance and the phase metric is not quantised by our own
write cadence. It is a declared USER-door param and costs only disk.

### 3.5 A measured warning about which model number to score

The committed canaries disagree with themselves about `peak_wl_m`: the
250 m leg reports 3.4863 m and the 50 m leg reports 1.1931 m, for the same
event, the same bbox, the same boundary series with the same
`sl_peak_m = 2.613`. That is a domain-wide max over a wet mask, and it is
evidently dominated by a wetting-and-drying artifact somewhere in the
domain rather than by the surge.

**Consequence for this methodology: the objective is computed at GAUGE
POINTS from the SELAFIN, never from the answer's scalar `peak_wl_m`.** A
resolution-unstable domain-max is not a quantity a calibration can
descend. This is a finding, not a proposal, and it is the reason section
5.3 exists.

---

## 4. The loop

Staged deliberately, cheapest first, and each stage is useful even if the
next never gets built.

### 4.1 Phase 1 - manual what-if, NATE-driven

`rerun_workflow(run_id, overrides={"friction_coefficient": <value>})`,
called by hand, three to five times across the band: Ks = 25, 40, 55, 70.

This phase is not a formality. It answers, before a line of loop code
exists, the question the whole track depends on: **does friction move the
answer at all, and in the direction physics says?** Higher Ks is smoother,
so the surge should arrive EARLIER and slightly HIGHER at the bay head.
If the four runs are indistinguishable, configuration A's identifiability
problem has reappeared in configuration B, and the wave stops and reports
rather than automating a flat surface.

It also builds the intuition that lets a human read the sweep in phase 2,
and it produces the first four rows of the run journal in exactly the
shape everything downstream consumes. Every child carries
`parent_run_id`, `overrides: ["friction_coefficient"]` and a
`door=user basis=user note="override of run <parent>"` row already, at no
cost, because ADR 0319 built it.

### 4.2 Phase 2 - systematic sweep

A one-dimensional grid over the friction band: **11 points, Ks = 25 to 70
in steps of 4.5**, every one a `rerun_workflow` off the SAME parent.

One dimension does not need a Latin hypercube; a grid is exhaustive,
trivially parallel, and produces a response curve a human can look at.
Latin hypercube sampling earns its place only if a second axis is admitted
(section 2.4 or Q2), at which point 2-D grid cost grows as the square and
LHS's space-filling property starts paying for itself.

A grid also gives something an optimiser cannot: the SHAPE of the
objective. A flat bottom means the parameter is unidentifiable and the
honest report says "friction between 40 and 55 is indistinguishable at
this fidelity", which is a better answer than a spuriously precise
optimum.

### 4.3 Phase 3 - a derivative-free driver

**References, cited as benchmarks and not as shape** (per the standing
rule):

- **PEST++** (White et al., USGS) is the reference implementation of
  model-independent parameter estimation: it drives an unmodified model
  through its own input and output files, and offers Gauss-Marquardt-
  Levenberg, iterative ensemble smoothers and global sensitivity. What
  it does that matters to us: parameter transforms and bounds, singular
  value decomposition for ill-posed problems, and Tikhonov regularisation
  toward a prior - the machinery for the case where the objective is flat.
- **OSTRICH** (Matott, University at Buffalo) is a model-independent
  optimisation and uncertainty toolkit wrapping DDS, particle swarm,
  simulated annealing and others behind a text-file interface.
- **NLopt** (Johnson, MIT) is a library rather than a harness: BOBYQA,
  COBYLA, Nelder-Mead, Subplex for derivative-free bounded problems.
- **spotpy** (Houska et al., 2015) is already a dependency, already used
  by `compute_skill_metrics` for the metrics, and also carries samplers
  (SCE-UA, DREAM, ROPE, DDS).

**Proposed: a lean driver of our own that consumes `rerun_workflow`.** All
four references are file-driven or in-process harnesses that own the model
invocation, and ours cannot be: our re-run path is a run derived from a
run, with sheet inheritance, ledger seeding and provenance that a
file-swapping harness knows nothing about. Wrapping any of them would mean
either giving up the derivation (a second re-run path, which ADR 0319
forbids in as many words) or writing a shim that pretends a run is a file.

**The wrap-or-not decision is explicitly NOT made here.** It is NATE's
later call once phase 2 has shown the objective's shape. What IS proposed
now is the interface, so that either answer stays cheap:

```
propose(history: list[tuple[dict, float]], bounds: dict) -> dict | None
```

A proposer sees past (overrides, objective) pairs and the bounds, and
returns the next overrides or `None` to stop. Grid, golden-section,
Nelder-Mead and an ensemble smoother are all that signature. If NATE later
rules "wrap spotpy's SCE-UA", the wrapper is a `propose` implementation
and the driver does not change.

For one bounded parameter, **golden-section search is proposed** as the
first real proposer: it needs no derivative, converges linearly with a
known contraction ratio, and reaches a 1-unit-of-Ks bracket from a
45-unit band in about 9 evaluations.

### 4.4 Budget, from the canary numbers

Measured, from `data/persistence/run_journal.jsonl`, configuration A
(0.12 x 0.11 deg, 6 h simulated, dt 20 s):

| resolution | mesh | wall seconds (n runs) |
|---|---|---|
| 250 m | ~2.3k nodes | 42.4, 42.4, 43.6, 43.7, 44.1, 45.7, 53.5, 53.5, 55.0 |
| 50 m | 57,021 nodes / 113,088 elements | 93.2, 94.5, 95.2, 97.1, 106.2 |

Extrapolating to configuration B (area ~10x, at 200 m spacing ~35k nodes,
which is ~15x the 250 m node count) with a 24 h window (4x the timesteps),
and taking cost as roughly linear in nodes x timesteps from a 44 s base:
**~20 to 25 minutes per iteration.** At 36 h it is ~35 minutes.

**This extrapolation is not evidence and must not be treated as one.** The
first act of the execution wave is one pilot run at the proposed
configuration that MEASURES the number, and the sweep size is set from
that measurement, not from this paragraph. If the pilot comes back at an
hour, the calibration ladder coarsens (300 m, 24 h) and the fine
resolution moves to the confirmation run only.

**Proposed ladder:** calibrate at 200 m over 24 h (11 sweep points ~ 4 h
wall, parallelisable); confirm the single winning value at 50 m over 36 h,
once. The confirmation run is the one that gets a delivery packet.

### 4.5 Stopping

The loop stops on the FIRST of:

1. **Acceptance reached** - `CF(0.15 m) >= 90%` at the primary gauge, and
   the HWM RMSE at or below the threshold in Q6.
2. **Convergence** - the proposer's bracket is narrower than the
   parameter's meaningful resolution (proposed: 1 unit of Ks, since a Ks
   step of 1 near 40 is a Manning n step of 0.0006, below what anyone can
   defend from a bed description).
3. **Budget exhausted** - a declared maximum iteration count (proposed:
   20 for phase 3, 11 for the phase-2 grid).
4. **Objective flat** - the objective's spread across the whole band is
   smaller than the run-to-run noise floor. This one is a REFUSAL, not a
   success: the loop reports that the parameter is unidentifiable at this
   fidelity and stops, rather than returning the arithmetic minimum of
   noise as though it were a calibration.

The noise floor for (4) is not assumed. Canary replay already proves this
template is deterministic (`coastal_tidal_surge` replayed 18/18 identical
in the ADR 0319 gate), so a repeated identical run has zero spread and the
floor is set by the observation uncertainty instead: the CO-OPS sigma at
the peak is ~0.037 m and the best HWM class is +/- 0.05 ft.

### 4.6 The journal is the record

`data/persistence/run_journal.jsonl` already carries every field a
calibration experiment needs: `run_id`, `parent_run_id`, `overrides`,
`replayed`, `wall_seconds`, `template`, `engine`, `mesh`, the full
resolved `sheet` and the `answer`. **Proposed: the loop adds exactly one
field, `objective`,** carrying the metric name, its value, the paired
count and the observation artifact's content hash. Nothing else. The
experiment record is then the journal filtered by `parent_run_id`, which
means it is queryable by anything and maintained by nobody.

---

## 5. Observations through the substrate

### 5.1 What is already there

| observation | fetcher | status |
|---|---|---|
| CO-OPS water level, Michael window | `fetch_noaa_coops_tides` | registered, proven on this exact case |
| CO-OPS astronomical prediction (surge residual) | same, `product="predictions"` | registered |
| USGS STN HWMs, event "2018 Michael" | `fetch_high_water_marks` | registered; hook resolves the event NAME to id |
| NOAA topobathy bed | `fetch_ncei_dem_mosaic` | registered, in the template |
| offshore forcing (option 1) | `fetch_gtsm_tide_surge` | registered, KEYED (Q3) |

### 5.2 What is missing, named as specs

**Gap 1: `fetch_noaa_coops_tides` cannot ask for NAVD88 or for a chosen
interval.** The spec hardcodes `datum: MLLW` and `interval: h` in
`ingest.per_station.request`, and exposes neither on `params`. In practice
CO-OPS returns 6-minute data for `product=water_level` regardless of
`interval=h` (the committed canary's inline series is at 00:00, 00:06,
00:12 and carries `n_timesteps=720` for 3 days, which is 6-minute), so the
docstring's promise of hourly is already not what the tool does.

Proposed, and it is a two-line spec change plus a docstring correction,
NOT a new tool: **add `datum` (enum MLLW | NAVD | MSL | STND, default
MLLW) and `interval` (enum 6 | h, default 6) to `params`, and correct the
docstring's "returns hourly" claim.** The MLLW default and the existing
`datum_offset_m` reconciliation stay exactly as they are so the template
is unaffected; the calibration lane fetches the OBSERVATIONS directly on
NAVD to skip a conversion it does not need.

**Gap 2: no fetcher serves USGS STN deployed-sensor peaks or series.**
Proposed new spec: **`fetch_storm_tide_sensors`**, a sibling of
`fetch_high_water_marks` on the same STN host, same event-name resolution
hook, same `vector-fgb` shape, one Point per instrument carrying
`instrument_id`, `sensor_type`, `deployment_type`, `site_no`,
`peak_stage_ft`, `peak_date`, `vertical_datum`, `is_peak_estimated` and
the `good_start` / `good_end` validity window. Phase 1 needs only the peak
scalars, which the `PeakSummaries` endpoint serves directly. The
`time_series_csv` attribute (from the `_chopped.nc` sea-pressure series
plus the orifice elevation) is a phase-2 extension of the same spec, and
should be recorded as such rather than built speculatively.

Both gaps go through the corpus rule: a new or changed tool gets
`tool_query_corpus.yaml` queries and a model-free
`retrieve_visible_tools(prompt, None, 8)` check before acceptance.

**Gap 3: the model has no water level at a gauge point.** This is the
largest piece of work in the wave and it is not an observation gap, it is
an OUTPUT gap. `coastal_tidal_surge` returns scalars
(`peak_depth_m`, `flooded_land_km2`, `peak_wl_m`, `sl_peak_m`); nothing
produces the modelled hydrograph that `extract_model_at_observations`
needs on the model side of the pairing. Two paths:

- **(a) TELEMAC-native.** Add `LIST OF POINTS` / `NAMES OF POINTS` to the
  coastal `.cas` and let TELEMAC write the time series itself. Cheapest
  at runtime, exact at the node, and it is the "free timeseries-at-points
  from the engine" already flagged in `docs/IDEAS.md`. Costs a deck change
  and therefore a worker image rebuild, with the image-staleness rule in
  force.
- **(b) Post-process the result SELAFIN.** `postprocess_telemac.py`
  already opens `res_coastal.slf` and already reads FREE SURFACE and WATER
  DEPTH for `peak_wl_max_m`. Sampling the nearest node to each of N gauge
  positions at every frame is a small addition in the same read, and it
  emits directly into the point-layer-with-`time_series_csv` shape the
  pairing tool consumes. **Proposed: (b)**, because it needs no deck
  change, no image rebuild and no new TELEMAC keyword, and because it
  generalises to every SELAFIN-producing template at once rather than to
  the coastal deck only.

Either way the masking rule from the packet ruling applies: FREE SURFACE
is only meaningful where `WATER DEPTH > 0.02` (TELEMAC sets FREE SURFACE =
BOTTOM on dry nodes), so a gauge node that goes dry emits an honest
`None`, never a bed elevation dressed as a water level.

### 5.3 Staging and pinning

**The observations are fetched ONCE, at the top of the loop, and pinned.**

Concretely: the loop's first act stages the CO-OPS series, the HWM
collection and the sensor peaks to run-scoped artifacts, records the
sha256 of each staged file in the journal, and every iteration scores
against those bytes. Not against a re-fetch, not against a cache hit that
happens to be warm.

The reason is not tidiness. `fetch_noaa_coops_tides` carries
`ttl_class: dynamic-1h`, and a comparison whose reference moves between
iteration 3 and iteration 9 is a comparison of nothing. This is the same
argument ADR 0319 makes for deriving from a parent's sheet rather than
re-resolving the wire, applied to the observation side, and the same
determinism artifact the reach family adopted after the do_sag flake: a
recorded content hash makes "the same comparison twice" verifiable rather
than an impression.

The rerun primitive gives most of this for free. Because the child
inherits the parent's ledger records, a `friction_coefficient` override
does not even ASK for the tide series again - it replays the parent's
record and reuses the parent's object at the parent's URI. The pinning
proposed here makes that guarantee explicit and hashed rather than
implicit and structural.

---

## 6. Honesty

### 6.1 Every calibrated value is labelled

A value the loop chose is not a value the world provided, and the
provenance has to say which. The existing rerun lane already stamps an
override as `door=user basis=user note="override of run <parent>"`, which
is correct for a hand-typed what-if and WRONG for a loop's output, because
it attributes to a user a number a search algorithm found.

**Proposed: a `basis=calibrated` label, carrying the loop id, the
objective name and the objective value.** The final calibrated run's
provenance row for `friction_coefficient` should read as something a
reader can audit without leaving the page: the value, that it was
calibrated, which loop produced it, against which objective, to what
score, and against which pinned observation hash.

### 6.2 The holdout guard

Overfitting is the failure mode a calibration invites, and one number is
not enough to detect it. **Proposed guard, decided BEFORE the first run:**

- **Fit on**: the primary validation gauge hydrograph (CO-OPS 8728690
  under forcing option 1; a designated alternative under option 2).
- **Held out entirely from the objective**: USGS STN 9461 and 9468 peak
  stage and peak time, and a randomly-chosen but SEED-PINNED half of the
  in-domain HWM cloud.
- **The holdout is scored exactly once**, on the winning parameter set,
  after the loop has stopped.
- **The interpretation rule, stated in advance**: if the holdout RMSE is
  materially worse than the fit RMSE, the report says the calibration
  overfit, and the calibrated value is reported with that caveat attached
  rather than quietly kept.

Scoring the holdout more than once turns it into training data. This is
the one rule in the document that a well-meaning execution wave is most
likely to break by accident, so it is written as a gate in section 7.

### 6.3 What we will not claim

- **A calibrated hindcast is not a validated forecast.** We will have
  fitted one parameter to one event at one place. Nothing about that
  licenses a claim about the next storm, a different coast, or a
  forecast.
- **Under forcing option 2, the boundary is not independent** and the
  report says so in the same breath as the score.
- **With wind held at zero, this is not a Michael hindcast.** It is a
  hindcast of the surge that arrives through the boundary, and it is
  labelled that way.
- **A calibrated friction is not a measured friction.** It is the value
  that made this model best match these observations, and it carries every
  compensating error in the mesh, the bathymetry, the datum and the
  omitted wind.
- **The existing template caption stays true until the whole chain is
  green.** `coastal_tidal_surge` currently says "Planning-grade screening,
  not a calibrated hindcast", and that sentence is only allowed to change
  for the specific calibrated configuration, not for the template.
- **SFINCS remains screening-only.** The fidelity ladder is unaffected by
  anything here.

---

## 7. Deliverables and gates

### 7.1 The packet question, answered

**No packet per iteration. The journal is the experiment record; packets
are for the final run only.**

An 11-point sweep would produce 11 delivery packets, each with six
rendered panels, two animations and a chart, of which ten would be
identical in structure and never looked at. That is the assembler's cost
paid ten times for nothing, and it would bury the one packet that
matters. The journal already carries the run id, the override, the wall
time, the full sheet and the answer for every iteration; adding
`objective` (section 4.6) makes it complete.

Proposed deliverables:

1. **One sweep table** (markdown, in the wave's report): one row per
   iteration - override value, RMSE, CF, NSE, KGE, PBIAS, peak error,
   phase error, wall seconds, run id. Generated from the journal by
   script, never hand-typed.
2. **One response curve** (objective vs parameter), which is the artifact
   that shows whether the parameter is identifiable at all.
3. **One delivery packet, for the final calibrated run only**, at the
   confirmation fidelity, through `scripts/assemble_proof_packet.py`, with
   the standard QGIS-true renders plus one new panel: the modelled
   hydrograph over the observed one at the primary gauge, with the
   residual.
4. **One holdout report**: the single scoring of 9461, 9468 and the
   held-out HWM half, with the overfit verdict.
5. **An ADR** recording the decision, the calibrated value, the objective
   it minimised, and everything in section 6.3 that we are declining to
   claim.
6. **A `docs/proof/templates/coastal_tidal_surge/calibration/` directory**
   holding the pinned observation artifacts and their hashes, so the
   experiment is re-scoreable without a re-fetch.

### 7.2 Gates for the execution wave

Ordered, each one blocking the next:

1. **Sign-off gate.** This document is signed and the open questions in
   section 8 are answered. No run before that.
2. **Observation gate.** Every fetcher in section 5.1 returns the pinned
   artifacts and their hashes are recorded. New and changed specs pass
   the corpus rule (`tool_query_corpus.yaml` plus a model-free
   `retrieve_visible_tools(prompt, None, 8)` check).
3. **Pairing gate.** `extract_model_at_observations` produces a paired
   table over ONE run with a non-zero `n_paired` and an empty or fully
   explained `dropped[]`, and `compute_skill_metrics` returns finite
   metrics over it. The scoring chain is proven before it is looped.
4. **Identifiability gate.** Phase 1's hand-driven runs show the objective
   MOVING with the parameter, monotonically and in the physically
   expected direction. If it does not, the wave stops and reports.
5. **Pilot-budget gate.** One run at the proposed configuration, wall time
   measured, sweep size set from the measurement.
6. **Loop gate.** The driver dispatches only through `rerun_workflow`. A
   grep proving the calibration lane contains no second re-run path is
   part of the evidence, not an assurance in prose.
7. **Holdout gate.** The holdout is scored exactly once. The evidence
   shows a single scoring event.
8. **Regression gate.** The standing gates: the offline slices at their
   known baseline, contracts, canary replay with `coastal_tidal_surge`
   still 18/18 identical (a calibration wave that changes the template's
   default answer has broken something), `ws_smoke.py` all passed, and
   the flood-sim canary per the standing large-change rule.
9. **Deletion gate.** `docs/DELETION_LEDGER.md` is checked: the malpasset
   constellation is QUEUED for chop, superseded by this case as the V&V
   exemplar, and the wave that lands this case is the one that satisfies
   the supersession condition.

---

## 8. Open questions for NATE at sign-off

Settled by the probes, needing no decision: the event, the gauge record's
usability, the datum chain, the HWM availability and datum, the sensor
peak availability, the metric library, the pairing library.

Not settled, needing NATE:

**Q1. Configuration A or B?** B is recommended and is the only one where
friction is identifiable, but it is roughly 30x the compute of the
committed canary and needs a forcing decision (Q3). A is nearly free and
may produce a meaningless number. **This is the load-bearing question; the
rest are consequences of it.**

**Q2. Wind: held at zero for phase 1, or does this case get a real
meteorological forcing first?** Held at zero, the phase-1 claim is
narrower than "hindcast Michael" and section 6.3 says so. A
time-and-space-varying wind field is a genuine template capability gap
this case surfaced. Per the standing rule I am surfacing it rather than
scope-expanding into it.

**Q3. Is there a Copernicus CDS key in the vault
(`TRID3NT_COPERNICUS_CDS_API_KEY`)?** Forcing option 1 depends on it. If
not, is option 2 (CO-OPS at the boundary, that gauge excluded from the
objective, circularity declared) acceptable, or is obtaining the key part
of the wave?

**Q4. Does `coastal_tidal_surge` need an open-boundary declaration for
configuration B?** The template hardcodes a single seaward liquid boundary
on one bbox edge (`ocean_edge`), resolved to "E" on the committed canary.
A bay domain has a Gulf boundary plus two tidal passes, and misrepresented
coastal connectivity is quiet physics corruption - reflections that should
radiate. The mesh-wave charter already anticipated that calibration might
surface open-boundary needs early. **It has.** Does the wave absorb a
minimal open-boundary declaration, or does calibration wait behind the
mesh wave?

**Q5. Window: 36 h (12:00 on the 9th to 00:00 on the 11th) or 24 h (the
10th)?** Both bracket every observed peak. 36 h buys spin-up and
recession; 24 h buys a third off the per-iteration cost.

**Q6. What is the acceptance threshold against the HWM cloud?** NOS CS 17
gives a defensible number for a gauge hydrograph (CF(0.15 m) >= 90%) but
says nothing about surveyed marks. Published surge-hindcast practice
typically reports HWM RMSE in the 0.2 to 0.4 m band with a best-fit slope
near 1, but I did not find a single agency-owned numeric pass mark of the
kind NOS CS 17 provides, and I would rather say so than invent one.
Options: propose 0.30 m RMSE from practice and label it as ours; or make
the HWM comparison REPORTED-only and gate solely on the gauge.

**Q7. Does `basis=calibrated` need a new door, or is it a note on the
existing user door?** Section 6.1 argues the label matters; whether it is
a first-class basis value or a structured note inside the existing
override provenance is a contract question that belongs to whoever owns
`workflows/runtime`.

**Q8. Gap-3 path (a) or (b)?** (b), post-processing the SELAFIN, is
recommended: no deck change, no image rebuild, generalises to every
SELAFIN template. (a), TELEMAC-native gauge output, is the more
"correct" engine usage and is already on the ideas list. This one is
cheap either way and can be decided at implementation.

---

## 9. Citations

Primary sources, each verified by fetching it while writing this document.

**Acceptance thresholds and skill-assessment practice**

- Hess, K.W., T.F. Gross, R.A. Schmalz, J.G.W. Kelley, F. Aikman III, E.
  Wei and M.S. Vincent (2003). *NOS Standards for Evaluating Operational
  Nowcast and Forecast Hydrodynamic Model Systems.* NOAA Technical Report
  NOS CS 17, October 2003.
  https://tidesandcurrents.noaa.gov/ofs/publications/CS_Techrpt_017_SkillAss_Standards_2003.pdf
  Source of the primary acceptance gate. Section 9.4: "For h, hnn, ahw,
  and alw, the acceptable error X is 15 cm (0.5 ft)". Table 4 (Standard
  Suite and Standard Criteria): `CF(X) >= 90%`, `POF(2X) <= 1%`,
  `NOF(2X) <= 1%`, `MDPO(2X) <= L`, `MDNO(2X) <= L`, `WOF(2X) <= 0.5%`,
  with L = 24 hours.

- Moriasi, D.N., J.G. Arnold, M.W. Van Liew, R.L. Bingner, R.D. Harmel
  and T.L. Veith (2007). *Model evaluation guidelines for systematic
  quantification of accuracy in watershed simulations.* Transactions of
  the ASABE 50(3): 885-900. DOI 10.13031/2013.23153.
  The NSE / PBIAS / RSR grading bands `compute_skill_metrics` already
  carries and already stamps as heuristic.

- Gupta, H.V., H. Kling, K.K. Yilmaz and G.F. Martinez (2009).
  *Decomposition of the mean squared error and NSE performance criteria:
  implications for improving hydrological modelling.* Journal of Hydrology
  377(1-2): 80-91. DOI 10.1016/j.jhydrol.2009.08.003. Origin of KGE.

- Nash, J.E. and J.V. Sutcliffe (1970). *River flow forecasting through
  conceptual models part I - a discussion of principles.* Journal of
  Hydrology 10(3): 282-290. Origin of NSE.

**The event and its observations**

- Beven, J.L. II, R. Berg and A. Hagen (2019). *Hurricane Michael
  (AL142018), 7-11 October 2018.* National Hurricane Center Tropical
  Cyclone Report, 17 May 2019.
  https://www.nhc.noaa.gov/data/tcr/AL142018_Michael.pdf
  Landfall near 1730 UTC 10 October 2018 near Mexico Beach and Tyndall
  AFB; 140 kt, 919 mb, category 5. "the NOS gauge in Apalachicola measured
  a peak water level of 7.7 ft MHHW"; names the three Franklin County
  USGS sensors (Alligator Point, Apalachicola, St George Island State
  Park) that this methodology uses as holdouts.

- Byrne, M.J. (2019). *Monitoring storm tide from Hurricane Michael along
  the northwest coast of Florida, October 2018.* U.S. Geological Survey
  Open-File Report 2019-1059. DOI 10.3133/ofr20191059.
  https://pubs.usgs.gov/publication/ofr20191059
  34 sensor sites; 522 high-water marks from 331 sites, Seaside to Cedar
  Key. The 522 figure matches the STN API's Florida count for event 287
  exactly, which is how we know the API serves the published set.

- NOAA CO-OPS Data API, `products/water_level` and `mdapi` station
  metadata and datums. https://api.tidesandcurrents.noaa.gov/api/prod/
  and https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/

- USGS Short-Term Network (STN) flood-event data portal.
  https://stn.wim.usgs.gov/STNServices/ - `Events`, `HWMs/FilteredHWMs`,
  `Instruments/FilteredInstruments`, `Instruments/{id}/DataFiles`,
  `PeakSummaries/{id}`.

**Calibration and optimisation, as benchmarks only**

Cited to judge our ergonomics and ground our physics. The design's shape
is ours.

- White, J.T., M.N. Fienen and J.E. Doherty (2016). *A python framework
  for environmental model uncertainty analysis.* Environmental Modelling
  and Software 85: 217-228. DOI 10.1016/j.envsoft.2016.08.017. PEST++ /
  pyEMU; model-independent parameter estimation, regularisation and
  ensemble methods.

- Matott, L.S. *OSTRICH: an Optimization Software Toolkit for Research
  Involving Computational Heuristics.* University at Buffalo.
  http://www.civil.uwaterloo.ca/envmodelling/Ostrich.html
  Model-independent optimisation via a text-file interface.

- Johnson, S.G. *The NLopt nonlinear-optimization package.*
  https://github.com/stevengj/nlopt - BOBYQA, COBYLA, Nelder-Mead,
  Subplex for bounded derivative-free problems.

- Houska, T., P. Kraft, A. Chamorro-Chavez and L. Breuer (2015). *SPOTting
  model parameters using a ready-made Python package.* PLoS ONE 10(12):
  e0145180. DOI 10.1371/journal.pone.0145180. Already a dependency;
  already supplies our metrics; also carries samplers.

**Coastal-model calibration practice**

- Grey, S. et al. (2025). *Hurricane surge and inundation in the Bahamas,
  part 1: Storm surge model.* Journal of Flood Risk Management 18:
  e13018. DOI 10.1111/jfr3.13018.
  TELEMAC-2D storm surge validated against tide-gauge water levels for
  Irene, Sandy, Matthew and Dorian; operational at the Bahamas Department
  of Meteorology. The closest published analogue to what this methodology
  proposes, on the same engine.

- Fernandes, E.H., K.R. Dyer and L.F.H. Niencheski (2001). *TELEMAC-2D
  calibration and validation to the hydrodynamics of the Patos Lagoon
  (Brazil).* Journal of Coastal Research SI 34: 470-488.
  Sensitivity to bottom friction, space discretisation and eddy
  viscosity; friction varying with bed sediment grain size as a further
  improvement. The reference for section 2.4's deferred second axis.

- Bhaskaran, P.K. et al. *Effect of bottom friction, wind drag coefficient
  and meteorological forcing in hindcast of Hurricane Rita storm surge
  using SWAN + ADCIRC.* The joint bottom-friction / wind-drag sensitivity
  practice section 2.2 declines to follow in phase 1, and why.

- TELEMAC-2D User Manual, opentelemac.org. LAW OF BOTTOM FRICTION (2 =
  Chezy, 3 = Strickler, 4 = Manning) and FRICTION COEFFICIENT semantics,
  which are the source of the coupled-validity rule ADR 0319 ported.

- Chow, V.T. (1959). *Open-Channel Hydraulics.* McGraw-Hill. The Manning
  n tables behind the proposed friction band in section 2.1.

**In-repo, load-bearing**

- ADR 0319, `docs/decisions/0319-rerun-with-overrides-and-coupled-validity.md`.
  The primitive this loop consumes, the three-piece gap it names, and the
  hard rule against a second re-run path.
- `docs/IDEAS.md`: the calibration rulings this document implements -
  coastal surge against a real event with CO-OPS gauges; NATE-first
  methodology sign-off before runs; references are benchmarks never shape;
  the US-only rule refined to "wherever gauges and sensors our substrate
  can fetch live"; the malpasset chop with no code inheritance.
