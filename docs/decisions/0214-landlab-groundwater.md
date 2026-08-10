# ADR 0214 - Landlab groundwater front (GroundwaterDupuitPercolator)

Status: Accepted
Date: 2026-08-10

## Context

The module-coverage board's Landlab GROUNDWATER section was unsurfaced:
`GroundwaterDupuitPercolator` (Dupuit-Forchheimer shallow unconfined aquifer) was
not wired into any TRID3NT analysis. Two board rows named it:
`aquifer_storm_seepage_hydrograph` (CAND-L) and `constant_recharge_mass_balance_gate`
(CAND-M, the mandatory V&V harness). ADR 0184 recommended it as the NEXT Landlab
front - the FIRST groundwater solver chain, needing a new adaptive-dt loop, a
seepage-flux aggregation, and the mass-conservation V&V as a prerequisite.

Published-first source: the official Landlab `groundwater_flow` tutorial
(GroundwaterDupuitPercolator), which documents the component fields, the adaptive
time-step solver, and the cumulative mass-conservation check.

Thematic tie: the groundwater return-flow (seepage) this component models is
exactly the process a surface-only rain-on-grid run (TELEMAC RoG) omits - stated
in both docstrings as a modeled process, NOT a coupling claim.

## Decision

### Landed (2 templates, registry 239 -> 241, EXPECTED_TEMPLATES +2)

Two genuinely-distinct question-class templates over the SAME
GroundwaterDupuitPercolator, sharing the aquifer-grid builder + the seepage /
mass-balance seam (the dem_pit_fill/lake_mapping shared-plumbing precedent). Real
US site: Panola Mountain Research Watershed, GA (a classic USGS Piedmont
groundwater/baseflow research catchment).

1. **`landlab_groundwater_water_table`** (worker analysis `groundwater_steady`).
   The aquifer relaxed to steady state under constant areal recharge. PRIMARY =
   the depth-to-water-table raster (topo - water table; 0 at a seepage face);
   SECONDARY = water-table-elevation + seepage (surface-water specific discharge)
   rasters; chart = the steady baseflow partition (groundwater underflow vs
   surface seepage). V&V = the STEADY-STATE mass balance on instantaneous rates
   at convergence: recharge in == groundwater underflow + saturation-excess
   seepage generated; |rel error| < 1%.
   Live V&V (Panola Mtn GA, 30 m, 250 mm/yr recharge, 15 m aquifer, demo K=1e-4):
   **mean depth-to-water 11.4 m, baseflow 0.162 m3/s (= recharge in 0.160,
   conserved), mass-balance rel err -1.4e-3**, seep_frac 0.006. Run
   01KZPN1ZS5ZKEV7CPYJADYHMGB. The depth-to-water map is physically correct:
   shallow along valleys/drainage, deep under ridges.

2. **`landlab_groundwater_storm_recession`** (worker analysis `groundwater_storm`).
   The SAME aquifer forced by a Poisson storm sequence
   (`PrecipitationDistribution`) and integrated transiently. PRIMARY = the
   per-cell peak-seepage raster (where return-flow emerges during storms); chart
   = the baseflow-discharge-vs-time hydrograph; the recession timescale is fit
   from the first clean recession limb after the peak. V&V = the transient
   cumulative mass balance (recharge in == cumulative outflux + storage change);
   |rel error| < 1%.
   Live V&V (Panola Mtn GA, 6 m aquifer, mean storm 22 mm, 120 d):
   **peak baseflow 12.95 m3/s, final 1.53 m3/s, first-limb recession tau 0.72 d,
   43 storms, mass-balance rel err -6.5e-3**, seep_frac 0.197. Run
   01KZPN3V7VKRFVW4DH4JG5DSHD.

### Key engineering decisions (the V&V front)

- **Drained-edge boundary.** A fixed-value boundary at the initial mid-aquifer
  water table makes high-relief real-DEM edges act as constant SOURCES (net
  inflow, storage drains, mass balance blows up to >200%). The physically-correct
  BC is a free-drainage ("drained edge") boundary: the open-boundary water table
  is pinned at the aquifer BASE so groundwater always discharges OUTWARD across
  the domain edge.
- **Seepage measured at the SOURCE.** `calc_sw_flux_out` routes seepage to the
  boundary via a FlowAccumulator, which fails on a pitted real DEM (seepage
  collects in interior pits, never reaching the boundary -> sw_out ~ 0, balance
  broken). The saturation-excess seepage GENERATED (sum of
  surface_water__specific_discharge x cell_area over the core) is the correct
  conservation term - measured where the aquifer sheds it, router-independent.
  This lets the FlowAccumulator be dropped entirely.
- **Steady V&V = instantaneous balance at convergence** (dStorage/dt -> 0), not
  the cumulative integral (whose endpoint-flux-sampling error over a large outer
  dt does not close on real relief). The storm (transient) V&V keeps the
  cumulative balance with `subdivide_interstorms=True` so the dry intervals are
  delta_t-chunked (a 72 h interstorm sampled as one endpoint overstates the flux
  integral).
- **Aquifer + recharge = labeled demo defaults**, input-review gated (the
  green_ampt soil-block precedent): no aquifer-property fetcher yet; the DEM is
  REAL (3DEP). fetch_statsgo_soils (KFFACT erodibility) / fetch_soilgrids
  (texture) do not provide saturated K directly - a pedotransfer seam is a future
  wave, noted honestly.

## Consequences

- New worker analyses `groundwater_steady` + `groundwater_storm`
  (component_chain.py dispatch + `_JSON_SAFE_EXTRA_ANALYSES`), a shared
  `_build_aquifer_grid` (drained edge) + `_seepage_generated_m3s` helper.
- New `LandlabRunArgs` knobs (gw_hydraulic_conductivity_m_s / gw_porosity /
  gw_aquifer_thickness_m / gw_recharge_mm_yr / gw_regularization_f + the storm
  gw_storm_* family) + analysis synonyms.
- Two new `LayerURI` carriers: `LandlabGroundwaterLayerURI` +
  `LandlabGroundwaterStormLayerURI`.
- Style presets REUSED (NAMED RESIDUALS): depth-to-water + seepage ->
  `continuous_flood_depth`; water-table elevation -> `continuous_dem`.
- Two chart builders: `build_baseflow_partition_chart_spec` (steady) +
  `build_baseflow_hydrograph_chart_spec` (storm).
- Pins bumped: registry 239 -> 241 (test_catalog_surfacing), EXPECTED_TEMPLATES
  +2 (test_door_dissolution); categories.py PRIMARY (hydrology) + secondary
  entries.
- Retrieval-corpus-first honoured: corpus.yaml queries for each; model-free
  `retrieve_visible_tools(q, None, 8)` surfaces both (6/6 phrasings top-8).
- Offline: +3 worker chain tests (component_chain) + 11 server tests
  (test_landlab_groundwater.py: contract / build_spec / chart builders / COG
  reproject / bbox gate / registration).
- Proofs (over ESRI World Imagery, EPSG:3857 tiles AND data): docs/proof/templates/
  landlab_groundwater_water_table{,_seepage,_chart}.png +
  landlab_groundwater_storm_recession{,_chart}.png.
- Showcase +2 (Panola Mtn GA, --only groundwater).
- Board rows LANDED: `aquifer_storm_seepage_hydrograph` ->
  landlab_groundwater_storm_recession; `constant_recharge_mass_balance_gate`
  folded as the V&V gate baked into landlab_groundwater_water_table.
