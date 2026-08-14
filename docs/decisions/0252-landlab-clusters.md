# ADR 0252 - Landlab coverage clusters (Tectonics/Flexure, Vegetation/Ecohydrology) + SFINCS Infiltration adjudication. One landing: `landlab_normal_fault_scarp_evolution` (NormalFault tectonic-forcing landscape evolution). Six other rows adjudicated knob-or-STOP with recipes.

Date: 2026-08-13
Status: accepted
Continues: ADR 0184 (Landlab shortlist: channel_incision / chi_map / storm_sequence), ADR 0214 (Landlab groundwater). Reuses the channel_incision LEM loop.

## Context

Coverage-wave adjudication of three module-coverage-board clusters. The board's
src links decide the engine family per section (NOT the section title):

- "Tectonics & Flexure" -> landlab tutorials -> LANDLAB.
- "Vegetation & Ecohydrology" -> landlab tutorials -> LANDLAB.
- "Infiltration methods" -> hydromt_sfincs / sfincs.readthedocs.io -> SFINCS
  (adjudicated against the SFINCS deck-builder + soil-fetcher machinery, not
  landlab; a landlab specialist adjudicates but does not build the SFINCS leg).

In-venv component verification (landlab 2.11.0, agent venv - landlab runs as a
subprocess, exec_kind, no docker image): Flexure, NormalFault,
ListricKinematicExtender, Vegetation, VegCA, Radiation,
PotentialEvapotranspiration, SoilMoisture, SoilInfiltrationGreenAmpt,
SpeciesEvolver, LithoLayers all import cleanly. Component existence gates NONE
of these rows - the gates are heavy-machinery, un-fetchable data, and scope.

## Decision

### LANDED - `normal_fault_scarp_and_footwall_evolution` -> `landlab_normal_fault_scarp_evolution`

The key insight: `_run_channel_incision` ALREADY ships a full landscape-evolution
loop (FlowAccumulator + FastscapeEroder + optional LinearDiffuser, with per-step
uplift `z[core] += U*dt`). The normal-fault row is that loop with the spatially
UNIFORM uplift swapped for a Landlab `NormalFault` footwall throw. So the [L]
tag reflected coupling complexity, not new heavy machinery - the LEM step budget
already ships and runs in bounded time. This is a knob-class landing, not a new
subsystem.

- Worker: new `_run_normal_fault` mode in `component_chain.py` (+ dispatch +
  `_JSON_SAFE_EXTRA_ANALYSES` in entrypoint). `NormalFault.faulted_nodes` drives
  a footwall-only dip-projected throw; the fault is an E-W trace at
  `fault_position_frac` of the N-S extent (a labeled demo geometry - a mapped
  fault trace / measured slip rate is a scenario input, not a fetchable datum -
  through the input-review gate). Primary field = evolved topography (the
  scarp); secondary = cumulative-throw footwall raster.
- Contract: `analysis="normal_fault"`, fields `fault_throw_rate_m_yr` /
  `fault_dip_deg` / `fault_position_frac` (reuses incision_run_duration_yr /
  incision_n_timesteps / k_bedrock / hillslope_diffusivity_m2_yr); new
  `LandlabNormalFaultLayerURI` (total_throw_m, footwall_relief_m,
  n_footwall_channel_nodes, ...).
- Composer: `workflows/landlab/normal_fault/normal_fault.py` +
  postprocess_landlab_normal_fault + FAULT_THROW_STYLE_PRESET (reuses
  continuous_dem for the evolved elevation).
- Registry: tools/__init__.py, categories.py (x2), corpus.yaml,
  test_catalog_surfacing (253->254), test_door_dissolution EXPECTED_TEMPLATES.

Live E2E (Wasatch Range front, Provo UT; 3DEP 90 m; 5x10^5 yr; run
01KZZ4DR5EJ96MSPY6Q2NV2GP0): total_throw 577 m, footwall_relief 337 m, 75
footwall channel nodes, status ok + evolved-elevation COG published. Discriminating
ON/OFF proof at docs/proof/templates/landlab_normal_fault_scarp_evolution.png
(ON footwall relief 337 m vs OFF control 22 m; the real Wasatch front already
carries ~22 m, the fault adds ~315 m of scarp relief).

### STOP - the other six rows (recipe per row, see board)

- `lithospheric_flexure_under_surface_load` (landlab) - KNOB-READY, queued.
  Flexure is baked + trivial (one component, one step) but the load is an
  un-fetchable scenario input and the deflection is DEM-independent - a synthetic
  scenario capability that lands through the gate with a labeled default EET as
  its own small template, not bundled into this tectonics landing.
- `aspect_driven_vegetation_pattern_ca` (landlab) - STOP (heavy subsystem). All
  5 components baked, but it is a coupled ~50 yr CA + a stochastic storm driver
  whose storm/interstorm statistics must be derived from climatology (only MAP
  is fetchable) + PFT/radiation plumbing. Its own dedicated job, replicating the
  published NM MAP=254 mm case.
- `species_zone_biogeography_under_landscape_change` (landlab) - STOP (SCOPE).
  SpeciesEvolver is baked + deterministic, but biogeography is a scope stretch
  beyond the current engine roster; held for a NATE scope decision.
- `cn_infiltration_with_recovery_ks`, `green_ampt_native_infiltration`,
  `horton_native_infiltration` (SFINCS) - STOP (un-fetchable soil-hydraulics +
  missing builder path). Our soil fetchers serve texture (SoilGrids:
  clay/sand/silt/soc/bdod/ph) + Kffactor (STATSGO) only - NO saturated
  hydraulic conductivity, wetting-front suction, or moisture-deficit grids, so
  Green-Ampt (ksfile/sigmafile/psifile) and CN-with-recovery (ksfile) need a
  new pedotransfer-function derivation layer; Horton (f0/fc/kd) has no fetchable
  source at all (empirical calibration params). The SFINCS deck builder's
  `InfiltrationForcing` also emits ONLY cn_uri / lulc+reclass / bare-constant
  qinf - no native GA/Horton/CN-with-ks emission path. Two-part recipe (PTF
  derivation + builder emission) documented on each board row.

## Consequence

+1 registered template (254). One new worker LEM mode reusing shipped
machinery; zero new heavy compute. The tectonics cluster is half-closed
(normal_fault landed, flexure queued knob-ready); the vegetation cluster and
the SFINCS advanced-infiltration cluster are STOP with precise recipes. The
recurring blocker across the STOPs is data, not code: soil-hydraulic PTFs
(SFINCS infiltration), stochastic-climatology derivation (vegetation CA), and
scenario-load framing (flexure) are the reusable next builds.
