# ADR 0285 -- law 9: the `consequence` tag + refuse-in-auto for invented physics

Status: P1 LANDED (mechanism + 3-layer sweep guard + existing-entry tagging +
culvert mislabel fix + driver/test reconciliation; offline-provable). P2 LANDED
(the MODFLOW exemplar -- shared SoilGrids aquifer-resolution seam, demo constants
deleted, 12 archetypes wired, vadose/thermal/SFR rows, live Woburn A/B; see the
P2 section below). The 8 SILENT rows and the remaining real-source conversions
(P3-P8) are staged for their per-engine waves. Date: 2026-08-17. Source:
`docs/design/demo-physics-defaults-audit.md`.

## Context

Law 9 (charter): NEVER INVENT THE WORLD -- a physics-consequential value with no
real data source must REFUSE, never default to an invented demo value. The
demo-physics-defaults audit swept ~90 templates and found 34 invented-physics
`(template, param)` rows: 26 LABELED (`SyntheticInput(basis="default_demo")`,
riding the input-review gate but never blocking), 8 SILENT (raw floats, no
provenance surface), 1 MISLABELED (hecras culvert engineering stamped
`basis="derived"`). In auto mode the gate was a pass-through: a `default_demo`
value NEVER refused, so a place-named prompt silently solved on a demo aquifer /
uniform Manning / prescribed storm wind. `basis` alone cannot drive refusal --
`default_demo` covers invented physics (must refuse), scenario/aoi defaults (must
proceed), and numerical knobs (must proceed).

## Decision

1. **`SyntheticInput.consequence: Literal["physics","scenario","numerical","aoi"]`**
   (`trid3nt_contracts/common.py`), REQUIRED when `basis == "default_demo"`. A
   `model_validator(mode="after")` raises if the tag is absent -- the omission
   cannot construct. Tolerant read: a deserialization opting in via
   `context={"tolerant_history": True}` backfills `scenario` with a note instead
   of raising, so a pre-law-9 persisted record loads without crashing.

2. **Gate refuses in auto** (`gates/input_review.py`). `physics_refusal_reason()`
   builds a typed `PHYSICS_INPUT_REQUIRED` message naming every refusing param +
   its need. `gate_input_review` returns `proceed=False, cancelled=True` in auto
   mode -- AND in the headless no-emitter path -- when any entry is
   `consequence="physics"` + `basis="default_demo"`. scenario/numerical/aoi demo
   defaults still proceed. `user_gated` with a live session presents them for
   explicit approval (approval = consent; decline = refuse) unchanged.

3. **Three-layer sweep guard** (`tests/test_law9_consequence_guard.py`): (a)
   schema -- `default_demo` without `consequence` cannot construct; (b) static
   lint -- every `SyntheticInput(...)` site in `trid3nt_server/` naming
   `default_demo` carries `consequence=` (grep-shaped, fails new naked sites);
   (c) behavioral -- auto mode refuses physics demo defaults, proceeds on
   scenario/numerical/aoi. A new hidden default fails CI instead of shipping.

4. **Tagged all existing entries** -- 93 literal `default_demo` `SyntheticInput`
   sites + 5 variable-basis builders that can resolve to `default_demo`
   (aquifer_k_review_entry, baroclinic estuary_aoi, network_import storm depth
   x2, agitation breakwater). The culvert MISLABEL (#12) is corrected:
   barrel_diameter / opening_type / entrance_exit_loss / barrel_manning move from
   `basis="derived"` to `default_demo` (when unmodified) + `consequence="physics"`.

## Per-site consequence verdicts (coverage law)

Physics (REFUSE in auto), by audit row: modflow aquifer K/porosity/gradient/SFR
streambed/vadose Brooks-Corey + thickness/ambient temp/thermal conductivity
(#1-#7), landlab soil strength + aquifer K + green-ampt Ksat + channel_incision
K_sp (#8-#11), hecras culvert engineering + Muncie baked geometry (#12, #14),
openquake vs30 (#18), schism river_discharge/ocean_salinity/synthetic
bathymetry/tidal amplitude (#19-#22), swmm overland_manning/drainage_network/
junction+inverts/inlet_capture (#23-#26), telemac wind/wave forcing/thermocline/
bathy_source/datum_offset/bank geometry (#28-#33).

Borderline flags -> `scenario` (the user's QUESTION, proceeds labeled), per the
audit's per-row reasoning:
- Default earthquake magnitude + rupture geometry (geoclaw inundation, openquake
  scenario_gmf/secondary_perils): a *default* Mw/geometry is a scenario starting
  point, not a world claim.
- Storm climatology (landlab storm_sequence/storm_ensemble/groundwater_storm_
  recession, gw_recharge): forcing that is the scenario question.
- Tectonic forcing (landlab normal_fault throw, channel_incision uplift): the
  question; the co-located erodibility K_sp is the true invention (#11, physics).
- Injected-water temperature + vadose infiltration flux (modflow thermal/vadose):
  the scenario source term, distinct from the ambient/soil-hydraulics physics.
- Muncie `flow_scale` (hecras): a what-if multiplier (the baked *geometry* is the
  physics refusal, #14).

SWMM urban_flood -> `scenario` (borderline resolution, flagged for NATE): the
audit table lists overland_manning (#23) and synthesized drainage_network (#25)
as physics. But urban_flood has NO supply path for either (no manning param wired
to the entry, no real-network import -- that is the P4 conversion), so a physics
tag would BRICK the flagship PySWMM urban demo in auto with no alternative. The
audit's own borderline guidance resolves both to scenario: a uniform Manning is a
"standard published value -> user-gated literature offer, not a hard refuse", and
the quasi-2D overland grid is the "explicit synthesized-demo opt-in" (#25's stated
alternative) -- a labeled screening MODEL CHOICE ("NOT a surveyed network"), not a
world-value claimed as real. Both stay LOUDLY LABELED. P4 upgrades overland_manning
to NLCD-derived (scenario -> derived). NATE may overturn to physics once the manning
param + network opt-in land. swmm network_import junction/inverts (#24) and
dual_drainage inlet_capture (#26) remain physics (un-fetchable engineering, and
their templates are not bricked -- no auto-solve tests depend on them).

Numerical -> proceeds: sfincs solver_settings/scope, schism baroclinic_config/
turbulence_closure, target/bathy resolution knobs. Aoi -> proceeds: default
estuary/domain extents, mesh_source selection, domain-extent clamps. Canonical
validation cases (modflow/schism/geoclaw *_validation) -> `scenario` (the demo
default IS the published case; refusing would break the benchmark).

## Driver / test reconciliation (this wave)

Physics-tagged templates now REFUSE under-specified auto runs -- THE POINT.
Reconciled to the new truth:
- `test_capture_zone_soil_k.py`: the demo-K fallback test now asserts the
  `PHYSICS_INPUT_REQUIRED` refusal (the pre-law-9 silent fallback is gone); the
  K-seam tests supply a DEM-derived gradient fixture so the gradient physics
  default does not mask the K path.
- `test_engine_chart_emission.py` (regional_water_budget, sustainable_yield):
  supply `aquifer_k_ms` explicitly so the chart-emission seam runs (the honest
  "provide the value" path, never re-tagging physics as scenario to dodge).
- `test_input_review_gate.py`: demo entries carry `consequence` tags.
The flood canary (SFINCS) is unaffected -- sfincs numerical_physics defaults are
`numerical` (proceed), and the canary drives explicit params.

## Scope decision -- the 8 SILENT rows

The audit's 8 SILENT rows (geoclaw storm_surge/regional_manning Manning, swmm
aquifer_baseflow soil column, telemac do_sag/streeter_phelps BOD+reaeration,
elmfire fire-weather, swan physics coefficients) ride as raw floats in templates
with NO provenance surface and NO gate call. Adding a labeled entry to each
requires new gate plumbing (build entries -> `gate_input_review` -> cancelled
short-circuit) that only lands coherently alongside the real-source wiring their
conversion waves add (P3 soil hydraulics, P4 NLCD Manning, P5 coastal forcing,
P8 label-only). Landing bare refuse-or-nothing gates here would flip 6 more
templates to refuse ALL auto runs without the fetch alternative those waves
provide. The SILENT-row labeling is therefore folded into P3/P4/P5/P8. This is a
scope call surfaced for NATE (law 6): the mechanism enforces law 9 for every
CURRENTLY-labeled default today; the SILENT rows convert with their engine wave.

## Consequences

- ~45-50 templates change auto-mode behavior: an under-specified physics prompt
  returns a typed refusal until the value is supplied, fetched, or approved in
  user_gated mode. The consequence tag keeps canonical/scenario/numerical/aoi
  defaults out of the refusal.
- The sweep guard makes a new invented default a CI failure, not a silent ship.
- P2-P8 wire the real sources (SoilGrids pedotransfer, NLCD Manning, NOAA/NWIS/
  WOA forcing, USGS Vs30) so a refusal becomes a fetch where a source exists.

## P2 -- the MODFLOW exemplar: refusal becomes real ingestion (LANDED 2026-08-17)

The exemplar NATE named (audit rows 1-7). The mechanism from P1 is now backed by
a real data source for the whole MODFLOW archetype family, so an under-specified
run DERIVES the physics from the AOI rather than only refusing.

### The shared resolution seam

`trid3nt_server/workflows/modflow/_aquifer_resolve.py` (new) generalizes the
SoilGrids pedotransfer path that previously lived private to `capture_zone` into
the seam every archetype shares. `resolve_aquifer_properties(lat, lon, k, por)`:

- **user** -- a caller-supplied value is used verbatim (`basis="user"`).
- **derived** -- a missing value is DERIVED from SoilGrids sand+clay texture at
  the AOI (5-15 cm horizon, a tight ~2 km window) via the shared Saxton-Rawls
  (2006) pedotransfer seam (`basis="derived"`,
  `real_source_if_any="fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)"`). The
  entry note states the SCREENING caveat truthfully: a near-surface soil proxy,
  NOT a measured aquifer conductivity (true aquifer K can differ by orders of
  magnitude). Derived-from-real-data is acceptable under law 9; invented is not.
- **unresolved -> REFUSE** -- when SoilGrids cannot serve (fetch fails, AOI off
  the soil surface / ocean, or all-nodata over the window) the value stays None
  and its `SyntheticInput` carries `basis="default_demo", consequence="physics"`,
  so the input-review gate refuses in auto mode. There is no demo constant to
  fall back to -- the former `DEFAULT_AQUIFER_K_MS=1e-4` / `DEFAULT_POROSITY=0.3`
  are DELETED (`modflow_contracts.py`; `aquifer_k_ms`/`porosity` are now REQUIRED
  fields on `MODFLOWRunArgs`). See the DELETION_LEDGER rows.

Texture is read as the **mean of the valid cells over the AOI window**, not a
single centroid pixel: an urban / open-water / river-corridor centroid can land
on a nodata cell while the surrounding AOI carries real texture, which would
wrongly force a refusal when a genuine screening value exists (this is exactly
what the Woburn AOI exhibits -- see the A/B). None is returned only when EVERY
cell is nodata.

### The 12 archetypes + the other rows

All 12 members of the aquifer-K family (capture_zone, river_seepage,
wetland_hydroperiod, asr, managed_recharge, mine_dewatering, regional_water_budget,
sustainable_yield, saltwater_intrusion, contaminant_plume, thermal_plume,
vadose_transport; wellhead_protection rides capture_zone) resolve K+porosity
through the seam and gate BEFORE the solver. The other audit rows:

- **row 3 (vadose Brooks-Corey)** -- the saturated-zone K/porosity resolve via the
  seam; the Brooks-Corey soil hydraulics stay `consequence="physics"` default_demo
  (refuse in auto until supplied); the infiltration flux is `consequence="scenario"`
  (the user's forcing, proceeds).
- **row 4 (thermal)** -- ambient/undisturbed aquifer temperature + grain thermal
  conductivity tagged `consequence="physics"` (refuse); injection temperature is
  the scenario forcing.
- **row 5 (SFR streambed)** -- when a streambed DEM is requested and the 3DEP
  fetch fails, the demo streambed is a `consequence="physics"` default that
  REFUSES (a wrong streambed elevation shifts the whole gaining/losing budget)
  rather than silently solving on a flat demo value.
- **row 7 (capture_zone regional gradient)** -- already DEM-derives when it can;
  now REFUSES (physics) when the DEM is unreadable instead of the demo
  west->east gradient.

### The proving A/B -- the Woburn TCE plume (`scripts/proof_law9_aquifer_ab.py`)

Live, same prompt (`Woburn, Massachusetts` / trichloroethylene), local mf6:

- **(A) under-specified auto run, SoilGrids unavailable** -> the typed
  `PHYSICS_INPUT_REQUIRED` refusal, naming both `aquifer_k_ms` and `porosity`
  with the need -- the plume is NOT solved (law 9 live).
- **(B) the same prompt with SoilGrids resolution on** -> a real mf6 GWT solve
  (run `01M09R3RJWTZEFNMC0VAKV8A29`, 1 plume, depth COG in `trid3nt-runs`). The
  gate entries + `summary.aquifer_provenance` read `basis="derived"`,
  `real_source="fetch_soilgrids (Saxton-Rawls 2006 pedotransfer)"`, from AOI
  texture sand=55.8% / clay=10.5% (5-15 cm).

The resolved values vs the dead constant: **K = 9.1e-06 m/s** (derived) vs the
deleted **1e-04 m/s** demo constant -- ~11x lower. The demo constant would have
transported TCE roughly an order of magnitude too fast: precisely the "silently
ruin the simulation" outcome law 9 forbids. Porosity resolved to 0.278 vs the
deleted 0.3 demo value.

### Guard + tests

The P1 sweep guard (`tests/test_law9_consequence_guard.py`) still holds. The
MODFLOW composer + archetype + contract tests were reconciled to the new required
fields and the `demo_aquifer_caveat` -> `aquifer_provenance` summary key. Gates
green: modflow root suite (583 passed / 1 skip), `workers/modflow` (218 / 15
skip), contracts (721), daemon restart + `ws_smoke` all_passed, and the live A/B
above (A=PASS refuse, B=PASS derived solve).
