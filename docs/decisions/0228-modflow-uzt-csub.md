# ADR 0228 - MODFLOW UZT (vadose transport) + CSUB (delay/effective-stress upgrade)

Status: Accepted (physics proven on local mf6 6.7.0; production template wiring is the follow-on, NOT shipped in this note)
Date: 2026-08-12

## Context

Two MODFLOW module-coverage-board rows drive this note (both published-first,
anchored on modflow6-examples):

- **UZT** (GWT section): `unsaturated_zone_solute_transport_uzt` [CAND-M].
  Question class: a tracer/contaminant applied at the LAND SURFACE transits the
  VADOSE zone before reaching the water table - how long until it arrives at the
  water table, and at what concentration? Anchor: `ex-gwt-uzt-2d` (UZF+UZT
  purely-advective unsaturated transport; MF6 has NO unsaturated dispersion).
- **CSUB** (GWF-CSUB section): `csub_effective_stress_vs_head_based_crosscheck`
  [CAND-M]. Question class: aquifer-system compaction / land subsidence from
  pumping (Central Valley / Houston). Anchor: `ex-gwf-csub-p04` class.

Where the tracks already are:

- The saturated contaminant plume already ships as `modflow_contaminant_plume`
  (single OR multi-species GWT on a shared saturated GWF flow field). It has NO
  unsaturated zone: transport starts at/below the water table.
- Land subsidence already ships as the `land_subsidence` archetype, surfaced via
  `modflow_sustainable_yield(couple_subsidence=True)`. The landed CSUB deck is ONE
  **no-delay HEAD_BASED** interbed per pumped footprint cell, preconsolidation =
  initial head (all drawdown drives permanent inelastic compaction). It does NOT
  yet exercise delay interbeds or the effective-stress formulation.

Local-mode fact that shapes scope (from ADR 0215): the server imports the worker
adapter directly, so worker deck changes are offline-testable against the local
mf6 binary; the image-staleness law bites only the DEPLOYED container path. New
CSUB knobs and a UZT deck are buildable + provable locally, but landing them as
LLM-drivable templates still needs contract fields, template dispatch, a live
solve through the postprocess->COG pipeline, QGIS proofs, and (for the container
path) a worker-image rebuild + smoke. Those are the follow-on, not this note.

## Decision (landed-as + justification)

### UZT -> a DISTINCT question-class deck, NOT a knob on contaminant_plume

Justification (distinctness argued, per the kickoff): the vadose-transport
question differs from the saturated plume on all three of question, deck, and
deliverable:

- **Deck**: it requires a UZF unsaturated-flow package (a vertical `ivertcon`
  chain of UZF cells with Brooks-Corey water-content parameters thtr/thts/eps)
  PLUS a UZT transport package keyed to the UZF flows (`flow_package_name`).
  `modflow_contaminant_plume`'s deck has neither; UZF is a different flow physics,
  not a transport knob.
- **Deliverable**: the answer is an ARRIVAL TIME at the water table + a
  breakthrough concentration curve, not a saturated plume footprint COG. The
  chart axis is time-to-water-table, not plan-view area.
- **Question**: "surface spill -> how long until it reaches groundwater" is a
  vadose-travel question; the plume template answers "how far does it spread once
  in the aquifer."

Recommended landing surface: a new `vadose_transport` archetype (mirrors the
land_subsidence pattern - a distinct archetype on the shared MODFLOWRunArgs), OR,
if a lighter surface is preferred, a `vadose=True` mode on
`modflow_contaminant_plume` that swaps in the UZF+UZT column ahead of the
saturated GWT. The archetype is cleaner because the deliverable type differs
(breakthrough time vs plume area) and would otherwise overload the plumes[]
envelope.

### CSUB -> KNOBS / upgrade on the EXISTING land_subsidence archetype

Justification: same question class (pumping -> subsidence bowl), same deliverable
(subsidence COG + compaction time-series), same deck skeleton. The board row is
explicitly a FORMULATION crosscheck + a delay-interbed upgrade, not a new
question. So it lands as two additive knobs on `land_subsidence`:

- `csub_delay_interbeds: bool` - switch the interbed `cdelay` from `"nodelay"`
  to `"delay"` (adds `ndelaycells`, a positive interbed vertical K -> finite
  consolidation diffusivity -> time-lagged compaction).
- `csub_effective_stress: bool` - drop `head_based=True` for the effective-stress
  formulation (`sgm`/`sgs` geostatic unit weights + specified initial
  preconsolidation stress). Default stays HEAD_BASED (byte-identical to today).

## Evidence (proven locally on mf6 6.7.0)

Two reusable smoke fixtures were authored and RUN; both assert monotone
behavior-proving physics (the kickoff's image-law smoke standard):

- `services/workers/modflow/fixtures/uzt_smoke/uzt_smoke.py` - a 1D vadose column
  (UZF vertical chain + UZT tracer at the surface). Arrival time at the water
  table (first crossing of 0.5 of the infiltration concentration), by vadose
  thickness: **2 m -> 20 d, 4 m -> 50 d, 8 m -> 110 d** (final concentration 1.0
  at the water table - purely advective, no unsat dispersion, matching
  ex-gwt-uzt-2d). ASSERTION: arrival time increases MONOTONICALLY with vadose
  thickness. PASS.
- `services/workers/modflow/fixtures/csub_delay_smoke/csub_delay_smoke.py` - a
  confined single-layer transient WEL deck + CSUB, mirroring the landed
  `csub_smoke`. Three proofs:
  - (A) compaction scales with pumping: -2000/-4000/-8000 m3/d ->
    **102.4 / 204.8 / 409.7 cm** final compaction. ASSERTION: monotone. PASS.
  - (B) delay-interbed lag: at end-of-pumping the delay interbed has compacted
    **40.6 cm** vs the no-delay bed's **204.8 cm** (time-lagged consolidation via
    low interbed kv). ASSERTION: delay < no-delay at end-of-pumping. PASS.
  - (C) effective-stress vs head-based crosscheck (same stress path): **94.0 cm**
    (effective-stress) vs **204.8 cm** (head-based), same order of magnitude
    (ratio 0.46) - the two formulations bracket the same subsidence scale, the
    board row's crosscheck.

## Status / follow-on (NOT landed in this note)

The physics decks are proven; the LLM-drivable landing remains. Follow-on work:
contract fields (`csub_delay_interbeds`, `csub_effective_stress`; a
`vadose_transport` archetype + its forcing fields), the adapter deck builders
(extend `_build_csub_interbeds`; add a UZF+UZT deck), template dispatch (knobs on
`modflow_sustainable_yield`; a new/mode UZT surface), a live solve through the
postprocess->COG pipeline at real US sites (subsidence: San Joaquin Valley /
Houston; vadose: an ag/spill setting), QGIS proofs over ESRI EPSG:3857 with input
layers surfaced, physics-asserted showcase cases, retrieval-corpus queries + a
model-free `retrieve_visible_tools` check, and - for the deployed container path -
a worker-image rebuild + behavior-proving smoke. The velocity ledger stays with
the orchestrator.

## Consequences

- The board rows are NOT marked LANDED (physics proven, wiring pending). The two
  smoke fixtures are the exact decks a landing productionizes, so the follow-on
  builder starts from proven geometry, not a blank page.
- CSUB landing as knobs keeps the minimal-parameter surface (Invariant 10): the
  default deck is byte-identical to today; the two booleans are additive.
- UZT as a distinct archetype avoids overloading the saturated plumes[] envelope
  with a differently-typed (breakthrough-time) deliverable.
