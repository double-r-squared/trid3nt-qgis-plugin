# ADR 0169 - TELEMAC WAQTEL O2 dissolved-oxygen sag front (`telemac_do_sag`)

Status: Accepted (2026-08-07)
Engine: TELEMAC-2D + WAQTEL (water-quality module)
Supersedes: none. Related: ADR 0154 (TELEMAC CAND-S wind fold + WAQTEL decay v1a,
in-image-dico law), 0148/0158 (image + strict-manifest laws), 0153/0163/0167
(closed-form V&V pattern).

## Context

The M/L sign-off shortlist ranks the **TELEMAC WAQTEL water-quality** front #6
(8 board rows) and names **dissolved-oxygen sag** (`dissolved_oxygen_bod_sag_curve`)
its strong US lead: the Clean Water Act TMDL / discharge-permit question - a
permitted outfall enters a reach, where does DO bottom out downstream and does it
violate the standard? WAQTEL couples to TELEMAC-2D via the steering file (the
`WATER QUALITY PROCESS` keyword). ADR 0154 already shipped the WAQTEL **decay**
class (process 17) as the precedent, and pinned the law that the WAQTEL dicos ship
**in-image** while the examples tree does **not** - so keywords must be verified
against the in-image dico, never guessed.

## Triage finding (the guess was wrong)

The kickoff guessed "the O2 module is process 11 per the v9 dicos". **Verified
against the in-image `telemac2d.dico`: `WATER QUALITY PROCESS` is a multiplicative
combination of primes - `2`=O2, `3`=BIOMASS, `5`=EUTRO, `7`=MICROPOL, `11`=THERMIC,
`17`=degradation law.** The O2 module is **process 2**, not 11 (11 is THERMIC).
The O2 module (`calcs2d_o2.f`, `nametrac_waqtel.F`) appends **three** tracers after
the user dye, in order: `DISSOLVED O2`, `ORGANIC LOAD` (= ultimate CBOD as an O2
equivalent), `NH4 LOAD`. Its source term is
`dO2/dt = k2(Cs-O2) - k1 L - k44 NH4 + (P-R) - BEN/h` with `dL/dt = -k1 L`.

## Decision

### 1. WAQTEL O2 machinery (worker, shared)

`ReachConfig` gains a fifth substance class `do_sag` + O2 fields
(`do_sag_bod_mgl`, `do_sag_upstream_do_mgl`, `do_sat_mgl`, `do_water_temp_c`,
`do_k1_per_day`, `do_k2_per_day`, `do_k2_formula`, `do_standard_mgl`).
`write_waqtel_o2()` authors the O2 steering file (every English keyword verified vs
the in-image `waqtel.dico`); `author_deck`'s `do_sag` branch couples
`WATER QUALITY PROCESS = 2`, widens `INITIAL VALUES OF TRACERS` to the 4 tracers,
widens `PRESCRIBED TRACERS VALUES` to 4-per-boundary (the fully-mixed CBOD + DO
ride in at the **inflow** boundary - boundary-major per `tr.f` `IRANK` order), adds
`T2,T3,T4` to the graphic printouts, and **omits the dye point-source block
entirely** (do_sag models the reach STARTING at the discharge; the source block
would collide a single-tracer array with the 4 O2 tracers). Every default leaves a
non-do_sag deck **byte-identical** (test-locked: the full 46-test worker suite
stays green).

### 2. Reach framing - inflow-mixed load, not a mid-reach point source

In-image experiments proved the WAQTEL O2 tracers do **not** inject via the
TELEMAC point-source (`SOURCES FILE`) mechanism the dye uses (only the user dye
tracer injects there); they inject cleanly at the **inflow liquid boundary** via
`PRESCRIBED TRACERS VALUES`. So the DO-sag template models the reach **downstream
of a fully-mixed discharge**: the effluent+river-blended CBOD and DO enter at the
top of the reach - the standard textbook Streeter-Phelps framing and a legitimate
TMDL framing (the modeled reach begins at the outfall).

### 3. `telemac_do_sag` template

A registered engine template (`engine="telemac", tier="template"`,
`workflows/telemac/do_sag/`) that reuses the whole `telemac_river_dye`
reach-seeding + mesh + two-pass solve via
`model_telemac_river_dye(do_sag_config=...)` and postprocesses through the new
`postprocess_telemac_do` (steady-state DISSOLVED-O2 field COG in EPSG:4326 + the
along-reach DO-sag curve binned by downstream distance + `TelemacDoLayerURI`
scalars: `do_min_mgl`, `do_min_distance_m`, `do_violates_standard`, `sag_curve_*`).
A `continuous_dissolved_oxygen` style preset renders the field. DO saturation Cs is
temperature-derived (Elmore-Hayes) or user-overridden.

### 4. V&V - Streeter-Phelps 1925 closed form

The WAQTEL O2 kinetics reduce **exactly** to Streeter-Phelps when `P=R=BEN=k44=0`,
`FORMK2=0` (constant k2), `FORMCS=0` (constant Cs), T=20 C: `dD/dt = k1 L - k2 D`,
`dL/dt = -k1 L`. `streeter_phelps.py` is the deterministic analytical reference.

## Consequences / evidence

- **In-image V&V (shipped `trid3nt-local/telemac:latest`, provenance-checked):** a
  12 km straight-channel WAQTEL O2 solve through the **landed** `author_deck`
  reproduces the S-P closed form to **0.011 mg/L at the sag minimum (0.28 %)**, sag
  location within **21 m (0.3 %)**, profile **RMS 0.010 mg/L**. Committed as the
  fixture `server/tests/fixtures/telemac_o2_sp_idealized_profile.json` and
  re-checked deterministically by `test_telemac_do_sag.py` (no re-solve needed).
  Proof: `docs/proof/templates/telemac_do_sag_sp_overlay.png` (+ `_curve`, `_field`,
  `_mesh`).
- **Real-reach smoke (default nhd_area path):** the full path runs end-to-end on a
  real NHDPlus reach (Sacramento R. near Colusa CA): NLDI centerline -> **real
  NHDArea banks (100 % coverage)** -> mesh (3073 nodes) -> WAQTEL O2 solve
  (`status=ok`) -> `postprocess_telemac_do` -> DO field COG. The sag is minimal
  there (do_min 7.89 mg/L) - **correct physics** for a big fast river with a
  realistic low k1 over a short reach (little travel time = little CBOD decay).
- **Known limitations / recipes (honest STOPs):**
  1. `postprocess_telemac_do` derives downstream distance by a principal-flow-axis
     (PCA) proxy when no centerline is threaded - **exact for a straight channel
     (the V&V), unreliable on a meandering reach** (it can orient backwards).
     Recipe: have the worker write `centerline_utm` into `telemac_metrics.json` and
     thread it to the postprocess for true arc-length. v2.
  2. The Snake R. reach failed its **second-pass** solve: the entrypoint probe
     guesses `["outflow","inflow"]`; when `map_liquid_boundaries` returns a
     different order the re-authored deck can diverge on a steep-DEM reach where the
     NLDI flow direction and the boundary geometry disagree. This is a
     **pre-existing shared `river_dye` two-pass hydraulic sensitivity** (tracer
     values do not affect hydraulics), NOT a WAQTEL O2 defect. Reaches whose mapped
     order equals the guess (Sacramento) run clean.
  3. The deeper WAQTEL WQ tail (eutrophication/BIOMASS process 3, MICROPOL process
     7, THERMIC process 11, the AED2 lake path) stays a STOP - the O2 machinery
     makes them cheaper (same coupling seam + steering-writer pattern) but each adds
     its own tracer set + postprocess. The micropollutant golden-deck STOP (0154)
     stays a STOP.

## Registry / pins

Coded tools +1 (`telemac_do_sag`, a hand-authored template). Registry 225 -> **226**
(`test_catalog_surfacing.py`); `EXPECTED_TEMPLATES` +1 (`test_door_dissolution.py`),
surfaced in the model-free top-8 by `do_sag/corpus.yaml`; `categories.py` +1
(hazard_modeling). No deletions (pure addition; no DELETION_LEDGER entry).
