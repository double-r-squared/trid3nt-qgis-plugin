# ADR 0235 - MODFLOW GWE (Groundwater Energy / heat transport) archetype family

Status: Accepted (physics proven on local mf6 6.7.0 through the product adapter;
container-path deploy + LLM-drivable template registration is the follow-on, NOT
shipped in this note)
Date: 2026-08-12

## Context

The MODULE-COVERAGE-BOARD GWE section (7 CAND rows) is an entire MF6 model type
absent from TRID3NT: GWE (released MF6 6.5.0, 2024) simulates 3D thermal energy
transport in groundwater - advection, conduction, mechanical thermal dispersion,
water/solid heat equilibration, energy sources/sinks - via the ESL/CND/EST
sub-packages. It is STRUCTURALLY the heat twin of GWT solute transport: a GWE
model coupled to a GWF flow field through a `GWF6-GWE6` exchange, with
temperature as the dependent variable in place of concentration, CND thermal
conduction in place of DSP dispersion, EST heat storage in place of MST
mass-storage/sorption, and ESL/CTP energy loading in place of SRC mass loading.

Published-first source (NO external-citation gate on this front): the
NATE-provided modflow6-examples repository is the standing template source
(memory: reference_modflow6_examples_templates). GWE additionally ships analytic
verifications (Barends 1-D conduction-advection, Wexler POINT2 superposition,
Al-Khoury radial isotemperature contours) that are geography-free, so V&V rides
official USGS examples + analytic closed forms - consistent with the citations
law. The 7 board rows and the examples they anchor on:

| board row | mode it folds into | anchor example / analytic |
|---|---|---|
| `gwe_radial_conductive_advective_vs_analytical` [L] | injection_plume | ex-gwe-radial (Al-Khoury 2020 radial isotemperature) |
| `gwe_aquifer_thermal_energy_storage_cycling` [L] | ates | ex-gwe-ates (seasonal ATES charge/recover) |
| `gwe_borehole_heat_exchanger_thermal_loading` [M] | injection_plume | ex-gwe-bhe (Wexler 1992 POINT2 superposition) |
| `gwe_multisource_geothermal_interacting_bhes` [L] | injection_plume | ex-gwe-geotherm (Al-Khoury 2021 9-BHE FE reference) |
| `gwe_particle_path_thermal_profile` [L] | injection_plume (+PRT) | ex-gwe-prt (GWF+GWE+PRT coupled thermal pathline) |
| `gwe_vsc_temperature_dependent_viscosity_plume` [L] | injection_plume | ex-gwe-vsc (VSC viscosity-temperature feedback) |
| `gwe_infiltrating_heat_front_danckwerts` [M] | (follow-on: uze mode) | ex-gwe-danckwerts (Danckwerts 3rd-type BC) |

## Decision (landed-as + justification)

Following the 0215 archetype pattern and the kickoff's "heat-twin-of-plume folds
several rows into one gwe archetype family with modes" insight, GWE ships as ONE
adapter archetype `gwe_thermal` with a `gwe_mode` selector, NOT seven templates:

- **`gwe_thermal` / `injection_plume`** - a continuous warm-water injection WEL
  (carrying an AUXILIARY `TEMPERATURE`, mapped by the GWE SSM onto the
  energy-transport source) drives a downgradient thermal plume on the SAME
  UTM-georegistered 40x40x50 m grid + west->east REGIONAL_GRADIENT CHD as the
  spill deck. This is the heat twin of `modflow_contaminant_plume` and covers the
  radial conductive-advective, BHE thermal-loading, multi-source geotherm, VSC,
  and (with a PRT phase) particle-path-thermal-profile question classes: they are
  all "a thermal source in a flow field, how does the heat spread?" differing
  only in source geometry/schedule/coupling, not in the core GWF+GWE deck.
- **`gwe_thermal` / `ates`** - `n_cycles` of (inject warm season) then (extract
  season) at the SAME well, for aquifer thermal energy storage recovery
  efficiency (ex-gwe-ates).

The deck-author seam is `services/workers/modflow/gwt_adapter.py`
(`_build_gwe_thermal_deck`), dispatched from `build_modflow_deck` behind
`archetype == "gwe_thermal"`. Packages: GWF (DIS/IC/NPF/CHD/WEL+aux/OC) +
GWE (DIS/IC/ADV[TVD]/CND/EST/SSM/OC) + a `GWF6-GWE6` exchange. Temperature is
written to `gwe_model.ucn` (a HeadFile with text=TEMPERATURE) for the temperature
COG. Every other archetype/deck is byte-identical (additive dispatch + additive,
defaulted DeckManifest/contract fields).

**Units.** The deck runs in the adapter's DAYS/METERS base. CND thermal
conductivities are supplied in W/(m*degC) and converted to J/(m*day*degC) by
x86400 (`GWE_KT_WATER_DAY`, `GWE_KT_SOLID_DAY`). Heat capacities [J/(kg*degC)]
and densities [kg/m3] carry no time dimension. The standalone SI/SECONDS sandbox
(`scripts/sandbox/modflow/gwe_thermal_physics_sandbox.py`) proves the physics
unit-unambiguously; the adapter test re-proves it through the DAYS product deck.

**Thermal properties are LOUD demo defaults (0215 doctrine).** No thermal-property
fetcher exists, so ambient temperature (10 degC), water/solid heat capacity
(4184 / 800 J/kg/degC), density (1000 / 2650 kg/m3), and conductivity (0.56 / 2.5
W/m/degC) are stamped on the manifest (`thermal_defaults_are_demo=True`) and
narrated as demo assumptions; any is caller-overridable via the contract.

## Physics proven (local mf6 6.7.0, ALL GREEN)

Sandbox (`gwe_thermal_physics_sandbox.py`, SI/seconds, 10 m grid):
- A1 injection-plume warm-cell extent grows monotonically with injection dT:
  68 -> 87 -> 95 cells for dT = +5/+15/+30 degC.
- A2 advection shifts the thermal centroid downgradient (+1.07 cells) while a
  conduction-only (no-flow) field stays radially centered (-0.00 cells) -
  advection-dominated vs conduction-dominated regimes differ.
- B1 single-cycle ATES recovery efficiency is bounded in (0,1): 0.86.
- B2 recovery efficiency rises with cycle count: 0.860 -> 0.899 -> 0.916.

Adapter test (`test_gwt_adapter_gwe_thermal.py`, DAYS product deck, St. Paul MN):
6 passed - 4 pure-shape/rejection (GWE + exchange written; ATES period/cycle
counts; unknown-mode rejected; ates-without-n_cycles rejected) + 2 live-physics
(plume heat content monotone in injection dT; ATES recovery efficiency in (0,1)
and rising: 0.62 -> 0.72 -> 0.76 for 1/2/3 cycles at the coarser 50 m grid).

Proofs (`scripts/proof_modflow_gwe_thermal.py`, EPSG:3857, ESRI World Imagery,
mesh wireframe): `docs/proof/templates/modflow_gwe_thermal_injection_plume.png`
(warm plume + injection well over St. Paul, peak +30.3 degC) and
`modflow_gwe_thermal_ates_recovery_chart.png` (recovery efficiency 62/72/76/78%
for 1-4 cycles).

## Consequence / follow-on (NOT in this note)

Landed here (offline-provable): the GWE deck-builder + dispatch + manifest
fields, the contract archetype literal + `gwe_mode`/`injection_temperature_c`/
`ambient_temperature_c`/`thermal_conductivity_solid_wmc` fields, the run_modflow
build-spec threading (both the in-agent `build_and_stage_modflow_deck`
archetype_kwargs and the worker `_run_args_to_deck_kwargs`), tests, sandbox,
proofs.

Follow-on to make GWE an LLM-drivable, deployed template:
1. A temperature-COG postprocess runner (the `gwe_model.ucn` TEMPERATURE ->
   reprojected COG, analogous to `run_plume_postprocess`) + worker
   `_ARCHETYPE_POSTPROCESS_RUNNERS["gwe_thermal"]` dispatch.
2. A `gwe_thermal` workflow template + corpus.yaml (retrieval queries) +
   registration so `retrieve_visible_tools` surfaces it (new-tool retrieval-
   corpus-first law).
3. Worker image rebuild + through-image smoke (worker CODE changed -> the baked
   copy is stale until rebuilt; image-staleness law) - the injection_plume + ates
   decks solved end-to-end through the container.
4. A live daemon E2E at a US ATES/geothermal site (St. Paul MN) with input-layer
   surfacing (0231) and the COG rendered in QGIS (flood-canary-equivalent visual).
5. Optional `uze` mode (UZF+UZE infiltrating heat front, Danckwerts 3rd-type BC)
   to close the 7th board row; and a PRT phase on the injection_plume field for
   the temperature-along-pathline row.

Supersedes nothing; extends the MODFLOW archetype family (0215 pattern, 0228
vadose/dual-model laws) to the GWE model type.
