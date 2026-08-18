# ADR 0285 -- law 9: the `consequence` tag + refuse-in-auto for invented physics

Status: P1 LANDED (mechanism + 3-layer sweep guard + existing-entry tagging +
culvert mislabel fix + driver/test reconciliation; offline-provable). P2 LANDED
(the MODFLOW exemplar -- shared SoilGrids aquifer-resolution seam, demo constants
deleted, 12 archetypes wired, vadose/thermal/SFR rows, live Woburn A/B; see the
P2 section below). P3 LANDED (the soil-hydraulics substrate move: `_aquifer_resolve`
hoisted to `workflows/shared/aquifer_resolve.py`, `derive_soil_column` added for the
SWMM two-zone column; per-engine template conversions staged). P4 LANDED (roughness/
Manning: the shared `roughness_resolve` NLCD-derived-or-refuse seam, swmm
urban_flood row 23 + geoclaw storm_surge row 16 wired, the 0.03/0.025 demo constants
deleted, live urban_flood A/B; the row-24 CN misread + row-17 value-path +
geoclaw-sibling findings reported). P3-COMPLETION LANDED (the staged per-engine
template conversions on the P3 substrate: landlab susceptibility row 8 / groundwater
row 9 / green_ampt row 10 / channel_incision row 11 + swmm aquifer_baseflow row 27;
the demo constants deleted; the river_seepage refusal-test premise fixed; live
aquifer-column A/B; see the P3-completion section). P5 LANDED (the SCHISM/TELEMAC
coastal-forcing wave, AFK-conservative: the shared `discharge_resolve` NWM seam +
SCHISM baroclinic river_discharge row 19 wired + the row-20 salinity literature
offer + the row-22 synthetic-bathy verification (2 findings, no change); rows 21/28/
29/30/33 analyzed with per-row verdicts + a QUEUED-FOR-NATE list; live baroclinic
discharge A/B; see the P5 section). **P6/P7/P8 LANDED -- THE LADDER CLOSES** (the
tail wave): P6 = openquake Vs30 (row 18) verified refusing (psha/disaggregation/
event_based) + the scenario_gmf tagging GAP fixed + secondary_perils verified
compliant (Vs30 DEM-slope-derived, not invented) + the site-class literature offer
enriched. P7 = un-fetchable engineering + demo geometry (rows 12-15, 26): culvert
verified refusing; flood_2d peak mislabel corrected -> refuse; the Muncie demo
geometry (row 14) made an EXPLICIT `run_demo_geometry` opt-in (pahm_surge precedent);
levee breach_params (row 15) + dual_drainage inlet_capture (row 26) wired to refuse
with literature offers. P8 = the SILENT stragglers labeled (rows 32, 34 + network
literature constants + SWAN coefficients) scenario/numerical, no refusals. See the
P6/P7/P8 sections + the audit's closing STATUS. Nothing deleted this wave (relabels +
gate-wirings + one opt-in + provenance labels). Date: 2026-08-18. Source:
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

## P3 -- the soil-hydraulics substrate move (LANDED)

Audit rows 8-10 (landlab groundwater / green_ampt / susceptibility) + row 27
(swmm aquifer_baseflow) share the SAME SoilGrids pedotransfer substrate the P2
MODFLOW exemplar proved. P3 generalized that substrate so the per-engine
conversions ride ONE derivation, not three:

- The private `modflow/_aquifer_resolve.py` seam is HOISTED to
  `workflows/shared/aquifer_resolve.py` (`git mv`); all five MODFLOW importers +
  the test monkeypatch path + the proof script are reconciled to the shared path,
  and the module docstring generalizes to name landlab/swmm. Old path grepped to
  zero (bytecode cleared). Behavior-preserving: the modflow slice re-ran
  identically (the single pre-existing `test_river_seepage` failure reproduces at
  HEAD with P3 stashed - an offline-env SoilGrids-stub artifact, not a P3
  regression).
- `resolve_aquifer_properties` (K + porosity, the MODFLOW / landlab-groundwater
  path) is joined by `derive_soil_column` -> the two-zone SWMM moisture column
  (porosity / wilting point / field capacity / conductivity) surfaced from the
  SAME Saxton-Rawls texture fit (theta_s / theta_1500 / theta_33 / Ksat). One
  texture read, three consumers.

The per-engine TEMPLATE conversions (wiring landlab susceptibility/groundwater/
green_ampt + swmm aquifer_baseflow to derive-or-refuse through the shared seam)
are STAGED on this substrate, not half-wired - they land in their engine waves.

## P4 -- roughness / Manning (LANDED)

Audit rows 16 (geoclaw storm_surge Manning), 23 (swmm urban_flood
overland_manning), 24 (swmm network_import). The real source is the SAME
version-pinned NLCD land-cover -> Manning's n table SFINCS already builds its
per-cell roughness grid from (`shared/manning.py` + `data/manning_mapping.csv`),
reduced to a single representative scalar for a whole AOI.

### The shared resolution seam

`trid3nt_server/workflows/shared/roughness_resolve.py` (new) is the roughness
analogue of `aquifer_resolve.py`:

- `nlcd_class_histogram` reads a fetched NLCD raster's per-class cell counts (the
  shared boto3/GDAL reader, drops nodata sentinels).
- `area_weighted_manning` reduces the histogram to ONE bulk friction:
  `n_bar = sum(count_c * n_c) / sum(count_c)` over the classes carrying a mapping.
  The honest-simple reduction of heterogeneous cover to a single scalar - a
  SCREENING estimate, NOT a per-cell field (SFINCS builds that when the fidelity is
  needed), but DERIVED from the real land cover at the AOI, not invented. (The
  math is offline-unit-checked: 2x class-21 @0.035 + 1x class-42 @0.150 -> 0.0733.)
- `resolve_overland_manning(bbox, user_manning, *, param_name)` is the ladder:
  user (`basis="user"`) -> NLCD-derived (`basis="derived"`,
  `real_source="fetch_landcover (NLCD area-weighted Manning's n)"`, screening
  caveat in the note) -> UNRESOLVED (`basis="default_demo", consequence="physics"`,
  value None) so the input-review gate REFUSES in auto mode. The NLCD fetch + raster
  read are offloaded to a thread (never block the event loop). `param_name` lets
  each engine narrate under its own name (SWMM `overland_manning_n` / GeoClaw
  `manning_n`).

### The conversions

- **row 23 (swmm urban_flood `overland_manning_n`)** -- the composer resolves the
  overland n through the seam after the DEM fetch, threads the resolved value into
  `effective_args.manning_overland`, and includes the resolver's entry in the
  input-review gate (refuses in auto when unresolved). The static
  `overland_manning_n=0.03 consequence="scenario"` "landcover-derived n not wired"
  entry is DELETED (both gate + envelope sites); `DEFAULT_MANNING_OVERLAND` is
  DELETED and `manning_overland` is `float | None` (None -> derive-or-refuse) on
  both `SWMMRunArgs` and the tool. ADR 0285's earlier `scenario` verdict for this
  row (a stopgap so the demo did not brick with NO supply path) is now UPGRADED to
  `physics` -> derived, exactly as that verdict flagged ("P4 upgrades
  overland_manning to NLCD-derived (scenario -> derived)"). The frozen engine
  primitive `build_swmm_mesh`'s mechanical `DEFAULT_OVERLAND_N=0.03` fallback is
  RETAINED (direct-call/unit-test only; the composer always resolves/refuses first)
  - a numerical-class default, never a user-facing invented physics surface.
- **row 16 (geoclaw storm_surge `manning_n`, a SILENT row)** -- storm_surge had NO
  provenance surface and NO gate. It now resolves `manning_n` through the SAME seam,
  passes the entry through a new `gate_input_review` call (refuses in auto:
  `GEOCLAW_PHYSICS_INPUT_REQUIRED`), and surfaces the entry on the returned layer.
  Tool signature + docstring `0.025` -> `None`.

### Scope findings surfaced (law 6 - verified, not silently widened)

- **row 24 CN is an audit MISREAD.** The audit cited `network_import.py:544` as
  `curve_number = 90.0`; that line is `_resolve_storm_depth() -> return 90.0,
  "default_demo"` - a demo *rainfall depth* (90 mm, a `scenario` forcing), NOT a
  curve number. `network_import` has NO curve number anywhere and uses HORTON
  infiltration (`InfiltrationHorton`), not SCS-CN, so there is no CN to derive from
  NLCD+SSURGO/GCN250. The real invented overland constitutive params in the
  network subcatchments are `n_imperv=0.012 / n_perv=0.1 / imperviousness=70.0` +
  the Horton coefficients (`swmm_network.py:837-843`) - literature-canonical
  per-surface-type SWMM constants (like the SWAN coefficients), NOT a single bulk
  roughness that NLCD area-weighting replaces. `junction_subarea` + `node_inverts`
  are ALREADY physics-tagged (P1). No P4 wiring is honest here; QUEUED for a
  label-only pass (P8-class) at NATE's call.
- **fallback-audit row 17 (raster_cell_mesh roughness) is a DIFFERENT value-path.**
  `raster_cell_mesh` `_n_imperv=0.012 / _n_perv=0.1 / imperviousness_pct`
  (~lines 1205-1213) are the SubArea (subcatchment overland-flow) impervious/
  pervious surface roughness, distinct from the overland CONDUIT roughness
  (`overland_manning_n`, row 23) that P4 converts. They ride behind
  `advanced_physics` (a documented lever) and are literature per-surface constants,
  not an NLCD area-weight target. NOT converted (would widen scope wrongly);
  reported for a label-only pass.
- **geoclaw siblings beyond the audit's named 2.** The SAME `manning_n=0.025`
  default lives in geoclaw `inundation` / `amr_regions` / `gauge_timeseries` (audit
  row 16 named only storm_surge + regional_manning; regional_manning REQUIRES
  `manning_coefficients` so has NO invented default). These siblings are QUEUED for
  NATE's call, not converted this wave.

### Guard + tests + live proof

The P1 sweep guard holds (15 passed). Contracts reconciled (the
`test_swmm_run_args_minimal_applies_demo_defaults` assertion is now
`manning_overland is None`; `DEFAULT_MANNING_OVERLAND` import dropped) - 58 passed.
The three composer suites (`test_run_swmm_local_chain`,
`test_urban_flood_publish_offloop`, `test_swmm_two_card_sim_observability`) gained
an autouse fixture that stubs `resolve_overland_manning` offline (the honest
"provide the value / stub the fetch offline" pattern; the live derive/refuse is
proven by the A/B, never by re-tagging physics as scenario) - 27 passed. Offline
path checks: resolver refuse (default_demo/physics), user (basis=user), and
storm_surge auto-refuse (`GEOCLAW_PHYSICS_INPUT_REQUIRED`) all pass.

The live A/B on the cheapest affected template (swmm urban_flood, a real CONUS
urban AOI): [A] under-specified auto run with NLCD available -> the area-weighted
NLCD Manning's n is DERIVED and the pyswmm solve completes with the derived
provenance visible (`basis="derived"`); [B] the SAME AOI with `fetch_landcover`
force-failed -> the typed `SWMM_PHYSICS_INPUT_REQUIRED` refusal, no solve. (A/B
values captured in the P4 return report.)

## P3-completion -- the staged per-engine soil-hydraulics conversions (LANDED)

P3 (LANDED earlier) built the SUBSTRATE: it hoisted `_aquifer_resolve` to
`workflows/shared/aquifer_resolve.py` and added `derive_soil_column` (the two-zone
SWMM column). P3-completion wires the per-engine TEMPLATE conversions P3 staged
FOR that substrate (audit rows 8-11 + 27), so an under-specified auto run DERIVES
the material property from the AOI or REFUSES - never an invented default.

### Shared-seam additions (`aquifer_resolve.py`)

- `derive_soil_scalars(lat, lon) -> SoilDerivation` - ONE SoilGrids texture read
  serves every Landlab consumer's scalar: Ksat, drainable porosity, dry BULK
  DENSITY (`rho_b = (1 - theta_s) * 2650`, Saxton-Rawls Eq. 6), and the USDA
  texture class. `usda_texture_class(sand, clay)` classifies the texture triangle
  into the label Landlab's Green-Ampt table keys the capillary suction on.
- `soil_derived_entry(...)` - the user -> SoilGrids-derived -> REFUSE ladder for
  ONE texture-derived scalar (returns the effective value + a `SyntheticInput`).
- `literature_offer_entry(...)` - the user -> REFUSE-with-literature-offer ladder
  for an UN-derivable physics scalar (geotechnical strength, calibration
  coefficients): `default_demo/physics` with the literature range in the note a
  `user_gated` session can approve. Both keep the gate's auto-refuse mechanism.

### The per-row conversions

- **row 8 (landlab susceptibility, + landslide_storm_ensemble share the block).**
  Of the five strength params, only the DRY BULK DENSITY is honestly served by
  texture (Saxton-Rawls) - DERIVED. Cohesion + internal friction (no fetchable
  value), soil mantle thickness (depth-to-bedrock, NOT a texture output), and
  transmissivity (needs that thickness) REFUSE in auto with literature-range
  user-gated offers. **Law-6 correction to the audit:** the audit's conversion
  column said "SoilGrids + a strength pedotransfer can serve density/THICKNESS" -
  thickness is NOT texture-derivable (our texture read gives sand/clay, not
  depth-to-restrictive-layer), so thickness REFUSES, it is not derived. The
  overland-flow chain (rainfall-driven) does NOT gate on soil strength.
- **row 9 (landlab groundwater_water_table + groundwater_storm_recession).** K +
  drainable porosity DERIVED from the shared texture read or REFUSE. **Recharge
  decision (the P3 gridMET-derivability check):** recharge stays
  `consequence="scenario"` and PROCEEDS labeled - it is NOT auto-derived. A
  precip-fraction screening estimate (recharge = f * P) requires INVENTING the
  fraction f, which varies an order of magnitude with climate/soil/land use
  (~1-50%); deriving "recharge = 0.1 * precip" would invent the 0.1 - itself a
  law-9 violation. Areal recharge is the user's scenario FORCING question (the
  audit's own borderline guidance + the P1 tag), so it proceeds labeled, not
  refused. The aquifer thickness (max saturated thickness above the Dupuit base)
  is a screening STRUCTURAL assumption (no fetcher) -> `consequence="scenario"`
  (a NATE judgment-call surfaced: physics-consequential but not fetchable and not
  the FoS-style silent-hazard driver; tagging it physics would brick the template
  with no supply path, mirroring the ADR's urban_flood borderline resolution).
- **row 10 (landlab green_ampt).** Ksat (Saxton-Rawls) AND the USDA texture class
  (which SELECTS the Green-Ampt capillary suction) DERIVED from ONE SoilGrids read
  or REFUSE. A straight SoilColumn/texture fit, exactly as the audit named.
- **row 11 (landlab channel_incision).** K_sp REFUSES with a literature-range
  offer (a calibration coefficient, no fetchable value). The combined
  `uplift_erodibility_forcing` physics entry is SPLIT: K_sp -> physics (refuse);
  uplift_rate -> scenario (the tectonic what-if); m_sp/n_sp -> numerical (canonical
  published exponents, kept). channel_incision now REFUSES in auto until k_bedrock
  is supplied - the honest law-9 position for an un-fetchable coefficient.
- **row 27 (swmm aquifer_baseflow, a SILENT row).** The two-zone [AQUIFERS]
  moisture column (porosity=theta_s, wilting=theta_1500, field_capacity=theta_33,
  conductivity=Ksat) DERIVED from `derive_soil_column` at a `location`/`lat`/`lon`
  AOI, through a NEW `gate_input_review` call, or REFUSE. This row rode COMPLETELY
  UNLABELED before (no provenance, no gate); it now surfaces `aquifer_provenance`
  + the derived column and refuses (`SWMM_PHYSICS_INPUT_REQUIRED`) when neither a
  site nor an explicit column is given / SoilGrids cannot serve.

### Contract + build-spec + worker

The 10 wired `LandlabRunArgs` fields (5 strength + Ksat + texture class + gwK +
gwPorosity + k_bedrock) became `float|None` / `str|None` (no invented default); the
10 physics `DEFAULT_*` constants are DELETED. `build_landlab_build_spec` merges each
ONLY when the tool resolved it (a None never reaches the worker on the analysis that
reads it - the tool refused first). The worker `component_chain.py` keeps its OWN
inline `spec.get(k, literal)` mechanical fallbacks (worker-local, self-contained -
NOT imported from the contract, so the contract deletion does not touch them):
RETAINED as the direct-call/unit-test last resort, the SAME P4 precedent as
`build_swmm_mesh`'s `DEFAULT_OVERLAND_N`.

### The refusal-test premise fix

`test_river_seepage.py::test_demo_aquifer_refuses_in_auto` asserted a law-9 refusal
but the offline SoilGrids REST fetcher SERVES real texture (reachable without our
infra), so the shared resolver DERIVED a K and the run PROCEEDED - the test never
exercised the refusal it named. Fixed by monkeypatching `aquifer_resolve.derive_soil_k`
to None (the proof-script pattern) so SoilGrids is forced unavailable and the refusal
path runs.

### The proving A/B -- swmm aquifer_baseflow (`scripts/proof_law9_soil_column_ab.py`)

The cheapest wired template (in-process pyswmm, no DEM/worker image), at Ames, Iowa
(deep agricultural soil - clear SoilGrids coverage):

- **(A) SoilGrids ON** -> the two-zone column is DERIVED from AOI texture
  (sand=16.9% / clay=32.1%, a silty clay loam): porosity=0.464, wilting=0.196,
  field_capacity=0.357, **conductivity=0.132 in/hr** (`basis="derived"`,
  `fetch_soilgrids (Saxton-Rawls 2006 two-zone column)`); the pyswmm baseflow solve
  completes (routing error 0.0%, recession tau ~374 h, baseflow contribution
  0.94 cfs).
- **(B) SoilGrids force-failed** -> the typed `SWMM_PHYSICS_INPUT_REQUIRED` refusal
  naming `aquifer_soil_column`, no solve.

The derived conductivity **0.132 in/hr vs the deleted 0.8 in/hr demo (~6x LOWER)**:
the Iowa silty clay loam drains far slower than the demo sand assumed, reshaping the
between-storms recession - precisely the "silently ruin the simulation" outcome law 9
forbids.

## P5 -- SCHISM/TELEMAC coastal forcing (LANDED, AFK-conservative)

Audit rows 19-22 (SCHISM) + 28-30, 33 (TELEMAC). The conservative rule for this
wave (binding): wire rows to fetchers that ALREADY EXIST first; build a new fetcher
ONLY where small + unambiguous; anything needing a heavy new surface or a
contestable source choice REFUSES in auto (typed) + gets a user-gated literature
offer + is QUEUED for NATE. Never stretch. One row was wired to an existing fetcher
as the landed exemplar; the rest are analyzed with per-row verdicts + a queue.

### The shared seam

`trid3nt_server/workflows/shared/discharge_resolve.py` (new) is the discharge
analogue of `aquifer_resolve`/`roughness_resolve`. `resolve_dominant_discharge(bbox,
user_value, *, param_name, note_role)` is the ladder: user (`basis="user"`) ->
NWM-DOMINANT-REACH derived (`basis="derived"`, `real_source="fetch_noaa_nwm_
streamflow (NWM analysis, dominant reach)"`) -> UNRESOLVED (`basis="default_demo",
consequence="physics"`, value None -> the input-review gate REFUSES in auto). It
reuses the SAME proven NWM reach-read machinery river_dye's `_resolve_reach_discharge`
uses (fetch `fetch_noaa_nwm_streamflow` via TOOL_REGISTRY, read the reach FlatGeobuf,
iterate `streamflow_cms`), but selects the DOMINANT (max-streamflow) reach over the
AOI = the main-stem freshwater carrier feeding the estuary (river_dye selects the
reach NEAREST a seed point; the estuary case wants the bulk inflow). The fetch +
geopandas read are offloaded to a thread by the composer (never block the loop).

### Per-row verdict

| Row | Engine / param | Verdict | What landed / why |
|---|---|---|---|
| 19 | schism baroclinic `river_discharge` (=500) | **WIRED** | Existing fetcher (`fetch_noaa_nwm_streamflow`) + proven machinery. Composer resolves through the new seam -> derive-or-refuse; tool default `500.0` -> `None`; demo constant deleted (ledger). Live A/B below. |
| 20 | schism baroclinic `ocean_salinity` (=33) | **REFUSE + literature offer; QUEUE WOA** | NO ocean-salinity fetcher in the registry (`fetch_noaa_sst` is temperature, not salinity). Stays `consequence="physics"` default_demo (refuses in auto); note enriched with the well-constrained open-shelf literature range 33-35 psu a `user_gated` session can approve. World Ocean Atlas climatology fetcher QUEUED for NATE. |
| 21 | schism tidal_hydro `tidal_amplitude` + baked M2 boundary | **STAGE (existing fetcher, heavy composer surgery)** | `fetch_noaa_coops_tides` EXISTS (harmonic constituents ARE fetchable), so this is genuinely wire-able - BUT wiring real constituents into `tidal_hydro`'s baked M2 ANALYTICAL open-boundary is deep deck-authoring surgery (the boundary forcing is generated, not a scalar param). Not a "never stretch" one-liner. QUEUED as a dedicated schism-tidal wave (P6). |
| 22 | schism synthetic bathymetry (coupled_waves, pahm_surge) | **VERIFIED COMPLIANT, no change (2 findings)** | (a) **pahm_surge**: the synthetic-shelf path ALREADY hard-refuses (`SCHISM_BATHYMETRY_UNAVAILABLE`) unless `allow_synthetic_domain=True` (default False). That opt-in bool IS the consent plumbing the row asks for, and it refuses in BOTH auto AND user_gated - STRONGER than a consequence tag. Re-tagging the consented entry to `physics` would BRICK the declared mechanism-demo mode. No change. (b) **coupled_waves = AUDIT MISREAD**: it has NO synthetic-bathy-when-no-COG path; it runs the canonical DUCK94 FRF validation mesh (bundled) + REAL observed 8m-array wave spectra -> per the ADR canonical-validation carve-out these are scenario/aoi and correctly proceed. No invented terrain exists. |
| 28 | telemac wave_field `wind_speed_mps` (=20) | **DESIGN FORK -> NATE (do not silently wire)** | gridMET (`fetch_gridmet`, `vs`) serves real wind, BUT wave_field has NO time-window param and its wind is a "sustained STORM wind". gridMET gives a DAILY-MEAN AMBIENT wind (~3-6 m/s), which is NOT a 20 m/s storm - substituting it would MISREPRESENT the demo, and deriving "a storm wind" needs a storm date = a scenario choice, not a physics derivation. Two defensible readings (fork): (a) re-tag wind -> `scenario` (a storm IS the question, matching the ADR's own storm-climatology precedent, proceeds labeled); (b) keep `physics` and require a user storm-wind + a real hindcast source (HRRR/ERA5 at a storm time). Recommend (a). Surfaced for NATE per law 6. |
| 29 | telemac agitation `wave_period/height/reflection` | **REFUSE + literature offer; QUEUE NDBC** | Incident boundary wave obs are NDBC-buoy-derivable, but NDBC is a NEW fetcher build (source.yaml + corpus + catalog pins move + retrieval check) = a heavy new surface mid-wave -> "never stretch". Stays `physics` default_demo (refuses in auto). NDBC buoy fetcher QUEUED for NATE (one build serves rows 28-offshore + 29 + a row-33 alternative). `reflection_coef` is a literature-range user-gated offer (already). |
| 30 | telemac stratified_flow thermocline temps | **REFUSE + literature offer; QUEUE (do NOT build)** | Lake temperature profiles have NO obvious existing US fetcher. Stays `physics` default_demo (refuses in auto); user-gated literature offer. A lake-profile source (e.g. a GLTC/GLM class dataset) QUEUED for NATE - explicitly NOT built this wave per the kickoff. |
| 33 | telemac coastal_tidal_surge `datum_offset_m` | **STAGE (small new fetcher); QUEUE NOAA datums** | Station-derivable via the NOAA CO-OPS **datums** API (the CO-OPS family we already speak in `fetch_noaa_coops_tides`), BUT the datums PRODUCT is a different endpoint than the water-levels fetcher, so it is a NEW small fetcher (catalog pins move). Small + unambiguous per the rule, but a build + registry checklist mid-wave is a stretch alongside the discharge landing. QUEUED as a build-small pass (P6). |

### QUEUED FOR NATE (P5 outflow)

1. **World Ocean Atlas ocean-salinity fetcher** (row 20) - climatological open-ocean
   salinity boundary. Enables baroclinic to DERIVE `ocean_salinity` instead of the
   literature offer. Single well-known source (NOAA NCEI WOA).
2. **`schism_tidal_hydro` real-constituent boundary** (row 21) - wire
   `fetch_noaa_coops_tides` harmonic constituents into the SCHISM open-boundary
   forcing (replaces the baked M2 analytical boundary). Deep deck-authoring work =
   a dedicated wave, not a "never stretch" one-liner.
3. **wave_field wind consequence FORK** (row 28) - DECISION: re-tag wind ->
   `scenario` (recommended, matches the storm-climatology precedent) vs keep
   `physics` + require a real storm-wind hindcast source. NATE's call.
4. **NDBC buoy-observation fetcher** (rows 28-offshore, 29, 33-alt) - a single
   well-documented REST source; one build serves incident wave forcing
   (period/height) + offshore wind + a datum-offset alternative. A registry-checklist
   build = its own small wave.
5. **NOAA CO-OPS datums fetcher** (row 33) - the datums-product endpoint of the
   CO-OPS family; derives `datum_offset_m` (a wrong datum shifts the whole surge
   vertically). Small build-small pass.
6. **Lake temperature-profile source** (row 30) - thermocline warm/cold temps.
   No obvious existing US fetcher; NATE to name a source before any build.
7. **river_dye / discharge_resolve convergence** (hygiene) - river_dye keeps its
   private `_resolve_reach_discharge` (NEAREST-reach, its own typed gate). The new
   shared `discharge_resolve` (DOMINANT-reach) was NOT retro-fitted into river_dye
   this wave (its 2 expected-failing offline tests + a different selection policy =
   risk). Converge them (one seam, two selection modes) in a later cleanup.
8. **NLDI upstream-navigation inflow refinement** (row 19 fidelity) - the current
   "dominant reach within the AOI bbox" under-samples a wide estuary's true inflow
   (the main stem enters upstream of the bay). `fetch_nhdplus_nldi_navigate` exists;
   navigate upstream from the estuary head to the true main-stem inflow reach for a
   representative (not screening-lower-bound) discharge.

### Guard + tests + live A/B

The P1 sweep guard holds (`test_law9_consequence_guard.py`). The new seam gets an
offline ladder guard (`tests/test_discharge_resolve.py`: user / NWM-derived /
UNRESOLVED-refuse). The schism suite is unchanged-green (the deck-authoring unit
tests pass explicit discharge values; nothing depends on the deleted 500 tool
default). Four-slice suite baseline preserved.

The live A/B on the wired row (`scripts/proof_law9_discharge_ab.py`, SCHISM
baroclinic river_discharge, Delaware Bay, local schism docker):
- **(A) under-specified auto run, NWM force-unavailable** -> the input-review gate
  REFUSES (`SCHISM_INPUT_REVIEW_CANCELLED` / `PHYSICS_INPUT_REQUIRED` naming
  `river_discharge_m3s` AND `ocean_salinity` - both physics), no solve - law 9 live.
- **(B) NWM available, salinity user-supplied** -> the dominant-reach discharge is
  DERIVED (`basis="derived"`, `fetch_noaa_nwm_streamflow`, **Q=1.0 m3/s** vs the
  deleted **500 m3/s** demo constant - a 500x delta) and the 3D SCHISM baroclinic
  solve completes (run `01M0AF7195S9J9V3TBCJ53FJ3W`, surface salinity [26.06, 33.02]
  psu, max stratification 1.708 psu, surface-salinity COG in `trid3nt-runs`) on the
  DERIVED inflow. Salinity is supplied as a user value in (B) because row 20 has no
  fetcher and refuses in auto (the WOA offer is the user_gated path) - so (B)
  isolates the discharge wiring.

**Honest fidelity finding (law 6, surfaced not buried):** the derived **Q=1.0 m3/s**
over the WIDE Delaware Bay bbox is a real NWM reach but NOT the Delaware River main
stem (which enters upstream of the bay footprint, outside the AOI) - "dominant reach
within the estuary bbox" under-samples the true inflow for a wide tidal bay. This is
law-9-COMPLIANT (a real, loudly-labeled screening value; the note states the caveat
and points to a tighter river-mouth AOI / explicit discharge), but the derivation's
selection policy is a screening lower bound, not a calibrated inflow. QUEUED: an
NLDI upstream-navigation refinement (`fetch_nhdplus_nldi_navigate` exists) to find
the true main-stem inflow reach from the estuary head.

## P6 -- seismic site response: openquake Vs30 (LANDED)

Audit row 18 (`reference_vs30` across the openquake family). Vs30 controls the
site amplification of every ground-motion result; a single uniform reference Vs30
stamped `reference_vs30_type = measured` is invented site response. There is no
Vs30 fetcher yet (USGS Vs30 web service / topographic-slope Vs30 is QUEUED for
NATE, a new surface -- not built this wave), so the law-9 position is REFUSE in
auto with a user_gated literature offer.

### Verification finding (the point of P6)

The P1 tagging already made `reference_vs30` `physics`+`default_demo` in **psha,
disaggregation, event_based** -- verified LIVE: an under-specified psha auto-run
returns `USER_INPUT_CANCELLED` / `PHYSICS_INPUT_REQUIRED` naming `vs30`. But the
verify found the row-18 family INCOMPLETE:

- **scenario_gmf -- TAGGING GAP (fixed, in scope).** `openquake_scenario_gmf`
  gated only `magnitude` + `rupture_geometry` (scenario); its uniform 760 rock
  Vs30 (stamped `reference_vs30_type = measured` in the site model) had NO physics
  entry, so it did NOT refuse in auto. A `vs30` `physics`+`default_demo` entry was
  ADDED to its gate; it now refuses (verified offline + live).
- **secondary_perils -- VERIFIED COMPLIANT (law-6 correction to the audit, no
  change).** The audit lumped secondary_perils into row 18, but it DERIVES per-cell
  Vs30 from the DEM topographic slope via the Wald-Allen (2007) active-tectonic
  table (`wald_allen_vs30_active`); the `vs30` arg is only a UNIFORM OVERRIDE that
  defaults to None -> slope-derived. That is derived-from-real-data, NOT an invented
  default. Tagging it `physics` would wrongly refuse a template that already does
  the honest thing. No change (a spurious `physics` entry added during the verify
  was reverted).

### The literature offer (site-class ranges)

The refuse-in-auto note (`_local_oq.VS30_DEMO_NOTE`, shared by all four refusing
templates) doubles as the user_gated literature offer: it names the ASCE 7-22
site-class Vs30 bands (A >1500, B 760-1500, C 360-760, D 180-360, E <180 m/s) so a
reviewer can approve a class-representative value. Verified live in the psha
refusal message.

### Verdict per template (row 18)

| Template | Verdict |
|---|---|
| openquake_psha | REFUSES (P1, verified live) + enriched literature offer |
| openquake_disaggregation | REFUSES (P1) + enriched note |
| openquake_event_based | REFUSES (P1) + enriched note |
| openquake_scenario_gmf | GAP FIXED -> now REFUSES (uniform 760, no derivation) |
| openquake_secondary_perils | COMPLIANT, no change (Vs30 = DEM-slope-derived) |

QUEUED FOR NATE: a USGS Vs30 web-service / topographic-slope Vs30 fetcher (turns
the psha/scenario_gmf refusals into a derivation for the uniform-reference case).

## P7 -- un-fetchable engineering + demo geometry (LANDED)

Audit rows 12-15, 26. Genuinely un-derivable engineering values become user-gated
offers (refuse in auto, literature note); the baked demo geometry becomes an
EXPLICIT opt-in. No real source exists for any of these -- the honest law-9 path
is refuse-or-approve, not invent.

### Per-row verdict

| Row | Template / param | Verdict | What landed |
|---|---|---|---|
| 12 | hecras culvert barrel geometry | **VERIFIED (already wired P1)** | The P1 mislabel fix (barrel_diameter/opening_type/entrance_exit_loss/barrel_manning `derived`->`default_demo`+`physics`) already routes through `gate_input_review`; an under-specified auto run REFUSES (`HECRAS_INPUT_REVIEW_CANCELLED` naming `barrel_diameter`). Verified live. |
| 13 | hecras flood_2d `peak_inflow_cfs` (=5000) | **MISLABEL CORRECTED -> refuse** | The inflow-branch entry stamped the `_DEFAULT_PEAK_CFS` fallback `basis="user"` (a mislabel). Now threaded `peak_is_default` from the tool: an un-supplied peak is `default_demo`+`physics` and REFUSES in auto naming `peak_inflow_cfs` (a USGS regional-regression / gauge peak-flow fetcher is the queued real source). Also fixed a co-located LATENT BUG: the `equation_set`/`computation_interval` entries used `basis="default"`, which is NOT a valid `InputBasis` Literal and would raise on every default-equation-set inflow run -- corrected to `default_demo`+`numerical` (proceed, labeled). |
| 14 | hecras Muncie baked geometry (riverine_flood, levee_breach) | **EXPLICIT OPT-IN** | The pahm_surge `allow_synthetic_domain` precedent: a `run_demo_geometry: bool = False` param. Left False the run REFUSES BEFORE the gate with a typed `HECRAS_DEMO_GEOMETRY_REQUIRED` (naming that the template models ONLY HEC's Muncie reach, and pointing to sfincs_flood for a real place). Set True the geometry entry is a CONSENTED `default_demo`+`scenario` (banner-noted DEMONSTRATION GEOMETRY) and proceeds. Never a silent answer to a place-named request. **Law-6 note:** the audit listed flood_2d + culvert under row 14, but flood_2d AUTHORS its own mesh from the fetched DEM and culvert builds fresh topo -- neither bakes Muncie, so only riverine_flood + levee_breach carry the opt-in. |
| 15 | hecras levee_breach `breach_params` | **scenario -> physics + literature offer** | The shipped Muncie breach geometry/timing was tagged `scenario` (proceeded). Re-tagged `physics`: an active-breach auto run REFUSES naming `breach_params`, with a literature note (overtopping breaches ~2-4x levee height wide, 0.5-3 h formation; USACE/Froehlich). A levee-HOLDS run (breach_enabled=False) keeps `basis="user"` (breaches inactive -> not consequential -> no refuse). Two-layer consent: opt into the demo geometry AND approve/supply the breach engineering. |
| 26 | swmm dual_drainage `inlet_capture` | **gate wired -> refuse + literature offer** | The entry was stamped on the result envelope but NEVER gated (no `gate_input_review` call) -- it labeled but did not refuse. Added a gate after the precip step: an un-supplied `inlet_opening_m` (the demo 0.6 m) is `default_demo`+`physics` and REFUSES in auto (`USER_INPUT_CANCELLED`), literature note (HEC-22 grate/curb inlet capture, 0.3-1.0 m). A user value proceeds. |

Live: the culvert auto-refusal (row 12) is proven live (`HECRAS_INPUT_REVIEW_CANCELLED`
naming barrel_diameter); riverine/levee opt-in refusals + the levee breach_params
refusal + the dual_drainage inlet refusal are offline-proven (gate logic,
deterministic).

## P8 -- label the SILENT stragglers (LANDED, label-only, NO refusals)

Audit rows 32, 34 + the borderline network/SWAN constants. These rode SILENT (no
provenance surface). Per the audit plan they get a provenance entry tagged
`scenario` or `numerical` (documented-default) so they SURFACE without refusing --
refusing would brick canonical/what-if templates with no fetch alternative. No
`physics` refusals added.

- **row 34 (elmfire fire_spread wind/dir/moisture).** `wind_speed_mph`/`wind_dir_deg`/
  `fuel_moisture` DRIVE the entire spread and rode SILENT. Surfaced as `scenario`
  what-if levers (the audit's borderline resolution -- a fire-weather regime is the
  user's question) stamped on the primary layer's `synthetic_inputs`. A RAWS/gridMET/
  HRRR fire-weather fetch is the QUEUED real-source upgrade.
- **row 32 (telemac do_sag WQ terms).** `discharge_bod_mgl` (the pollutant
  source-term question) -> `scenario`; `water_temp_c` -> `scenario` (20 C is the
  standard closed-form Streeter-Phelps condition; the canonical-validation carve-out
  applies -- a site temperature via `fetch_usgs_water_quality` is QUEUED, not a
  refusal that would break the analytical benchmark); `k1_per_day`/`k2_per_day` ->
  `numerical` (documented rate coefficients; O'Connor-Dobbins reaeration derivation
  QUEUED). Note: `streeter_phelps.py` is an analytics helper (no registered tool) --
  do_sag IS the Streeter-Phelps template. The audit's `physics` reading of temp/
  reaeration is deferred to the queued fetcher rather than a benchmark-breaking
  refuse (law-6 judgment, surfaced).
- **network_import literature constants.** The SubArea `n_imperv=0.012 / n_perv=0.10 /
  imperviousness` + Horton infiltration constants (the P4 finding's QUEUED label-only
  items) surfaced as `scenario` documented-defaults (literature-canonical per-surface
  SWMM values, NOT a single bulk NLCD roughness). `junction_subarea`/`node_inverts`
  keep their P1 `physics` refuse.
- **SWAN physics coefficients.** `breaking_alpha/breaking_gamma/friction_cfjon/triads`
  (literature-canonical SWAN calibration constants) surfaced as ONE `wave_physics_
  coefficients` `numerical` documented-default entry on the peak layer. The storm
  climatology (`storm_peak_hs_m`/`storm_peak_hour`) has NO hidden default -- it is
  opt-in (None unless the user sets it), so there is no silent value to label.

## Consolidated NATE queue (the ladder's outflow)

The refusals become derivations WHEN these sources land (all deferred per the
conservative rule -- name the source, then build):

1. **USGS Vs30 fetcher** (row 18) -- turns psha/scenario_gmf uniform-reference
   refusals into a slope/service-derived Vs30 (secondary_perils already slope-derives).
2. **USGS regional peak-flow regression / gauge fetcher** (row 13) -- flood_2d
   `peak_inflow_cfs` derivation.
3. **RAWS / gridMET / HRRR fire-weather fetcher** (row 34) -- elmfire wind + fuel
   moisture (turns the scenario label into a derived option).
4. **`fetch_usgs_water_quality` water-temperature wiring** (row 32) -- do_sag
   `water_temp_c`; O'Connor-Dobbins reaeration k2 from velocity/depth.
5. Carried from P5: World Ocean Atlas salinity, CO-OPS tidal-constituent boundary,
   the wave_field wind consequence fork, NDBC buoy obs, CO-OPS datums, lake
   temperature profiles, river_dye/discharge_resolve convergence, NLDI
   upstream-navigation inflow refinement.

Culvert barrel geometry (row 12), levee breach engineering (row 15), dual_drainage
inlet capture (row 26), network junction/invert engineering (row 24) have NO
fetchable source -- they stay user-gated engineering offers permanently (the honest
law-9 endpoint for un-fetchable engineering).
