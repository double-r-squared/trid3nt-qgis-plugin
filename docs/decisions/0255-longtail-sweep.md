# ADR 0255 - Long-tail triage sweep (12 rows, 6 families)

Date: 2026-08-13
Status: accepted
Continues: ADR 0253 (mixed-clusters sweep), ADR 0151 (SWMM mechanism-comparison
templates), ADR 0250/0249 (HEC-RAS 2025 2D-only structure/engine scope),
ADR 0157/0253 (HEC-RAS WQ/sediment engine-absent STOPs).

## Context

Long-tail triage over the open `[CAND-*]` rows on the module-coverage board,
excluding the holds (Ensembles/Monte-Carlo, WAQTEL builds, SnapWave, existing
STOPs, and the named substrate fronts: coastal-TELEMAC, GeoClaw PETSc,
MODFLOW gridgen). Twelve rows were selected to bias toward cheap knobs/modes on
LIVE machinery, clustered by family so any image rebuild would amortize. Every
verdict is verification-backed (component checked in-venv / in-image / against the
image README BEFORE adjudication), never title-inferred.

## Selection (12 rows) + one-line rationale

SWMM (live PySWMM/swmm-toolkit, host-side .inp, no image):
1. `swmm_lid_infiltration_trench_permeable_pavement` - two more of the 8-9 EPA LID
   process types on the already-live LID comparison template; a lid_type knob-add.

SFINCS (deltares/sfincs-cpu solve image, host-side hydromt deck authoring):
2. `weir_levee_from_dem_derived_crest` - setup_structures(stype=weir, dz) is a
   documented hydromt method; image consumes sfincs.weir natively.
3. `thin_dam_flow_barrier` - same setup_structures(stype=thd); folds with #2.
4. `landuse_lulc_roughness_reclass_subgrid` - NLCD->Manning reclass; is it already
   the shipped default?

ELMFIRE (deck_builder + real LANDFIRE, subprocess engine):
5. `real_world_fuel_terrain_ingestion_spread` - is real LANDFIRE ingest already
   the default of elmfire_fire_spread?
6. `live_fuel_moisture_raster_ingestion` - scalar sensitivity exists; raster leg?
7. `crown_fire_exact_solution_regression_gate` - rides elmfire_verification harness.

OpenQuake (in-process oq, no image):
8. `tabulated_gmpe_hazard_curve` - GMPETable gsim knob on classical PSHA.
9. `event_based_liquefaction_occurrence_set` - openquake.sep already wrapped?
10. `newmark_displacement_landslide_hazard` - openquake.sep Newmark already wrapped?

HEC-RAS Sediment Transport (known-open section - confirm engine from image):
11. `2d_fixed_bed_concentration_capacity_routing` (representing the whole section:
    + quasi_unsteady_mixed_regime + 2d_mobile_bed_multigrain).

MODFLOW (flopy + mf6 image):
12. `buy_density_driven_saltwater_intrusion` - BUY Henry; is modflow_saltwater_
    intrusion already this?

## Verdicts

### LANDED (1) - swmm_lid_infiltration_trench_permeable_pavement

New `lid_type=infiltration_vs_permeable_pavement` mode on the existing registered
`swmm_lid_performance_comparison` template. A 3-variant contrast on ONE 9000 ft2
footprint under one design storm:
- infiltration trench (IT): Surface + Storage, native-soil seepage 0.5 in/hr, NO
  underdrain -> removes captured volume.
- permeable pavement (PP): Surface + Pavement + Storage + Drain, seepage 0.1 in/hr
  over a near-lined subgrade + underdrain -> returns most water, attenuates peak.

This RESOLVES the board's flagged open parameterization question (the non-soil
bedding-aggregate physics that has three competing forum workarounds) by taking
Robert Dickinson's "merge bedding aggregate into Storage, drop the Soil layer"
choice for both LIDs - clean and documented.

Discriminating triple (parsed swmm-toolkit output, continuity gate):
baseline peak 2.73 cfs / vol 2360; IT peak 0.77 / vol 633 (volume removed);
PP peak 1.10 / vol 2427 (peak attenuated, volume returned). knob_demonstrated=True,
continuity |err| <= 0.11%.

In-venv swmm-toolkit; NO image rebuild; extends an existing template's enum so the
registry stays 254 and EXPECTED_TEMPLATES is unchanged. Proof:
`docs/proof/templates/swmm_lid_performance_comparison_infiltration_vs_permeable_pavement.png`.

### COVERED - already-landed duplicates (3, + 1 scenario-covered)

- `buy_density_driven_saltwater_intrusion` == `modflow_saltwater_intrusion`
  (registered GWF+GWT BUY variable-density Henry cross-section; ModflowGwfbuy in
  flopy 3.10.0). No gap; a pure Henry semi-analytic V&V variant would be NATE-gated.
- `real_world_fuel_terrain_ingestion_spread` == `elmfire_fire_spread` default
  (ingests real LANDFIRE 30 m fbfm40/cbh/cbd/cc/ch + 3DEP DEM). Residual is only a
  real-vs-idealized-ellipse diff template, thin if wanted.
- `newmark_displacement_landslide_hazard` == `openquake_secondary_perils`
  (Jibson 2007 Newmark displacement + Jibson 2000 probability, openquake.sep).
- `event_based_liquefaction_occurrence_set` - scenario liquefaction (Zhu 2015)
  covered by openquake_secondary_perils; the 50-realization ENSEMBLE aggregate is
  on the Monte-Carlo HOLD list.

### STOP-RECIPE (verified blockers)

- HEC-RAS Sediment Transport (whole section: quasi_unsteady_mixed_regime,
  2d_fixed_bed_concentration_capacity, 2d_mobile_bed_multigrain): NO Linux
  sediment engine binary. The 6.x bundle ships only RasSteady/RasUnsteady/
  RasGeomPreprocess (image README verbatim: "Sediment / water-quality legs are
  not in HEC's Linux test set -- out of scope"); the 2025 managed engine is
  2D-hydraulic-only (DWE/ExpSWE/ImpSWE, no sediment module - ADR 0250). Recipe:
  obtain a Linux sediment engine (gating unknown - HEC ships none in the Linux
  test set), add to image (rebuild + ADR 0148 staleness), author a sediment deck +
  parse the sediment HDF. NEW engine + data, same class as the WQ STOP.
- `live_fuel_moisture_raster_ingestion`: uniform-scalar half already served by
  elmfire_live_fuel_moisture_sensitivity. Raster half STOPs on (1) deck_builder
  has NO M_LH/M_LW *_FILENAME raster leg (WEATHER_RASTERS = ws,wd,m1,m10,m100,
  dead-fuel only) -> new raster-input leg + &INPUTS wiring + image rebuild;
  (2) live fuel moisture is UN-FETCHABLE (LANDFIRE ships no LFMC raster; needs a
  remote-sensing LFMC source or an input-review-gated labeled default).

### KNOB-ELIGIBLE - verified machinery, deferred to its own job (3+)

- SFINCS `weir_levee_from_dem_derived_crest` + `thin_dam_flow_barrier`: ONE
  StructureSpec knob. hydromt_sfincs 1.2.2 `setup_structures(structures, stype=
  'weir'|'thd', dep, dz)` verified in-venv; deltares/sfincs-cpu consumes
  sfincs.weir/.thd natively (host-side, NO rebuild). Not yet wired in sfincs_builder
  (it has subgrid/building-obstacle/manning, no setup_structures leg). Deferred to
  a SFINCS build+solve smoke wave (docker solve, not a host-only unit).
- SFINCS `landuse_lulc_roughness_reclass_subgrid`: the LULC->Manning RECLASS is
  ALREADY the shipped default on the plain deck (setup_manning_roughness +
  version-pinned manning_mapping.csv + a hard validation gate). Residual is feeding
  that reclass into setup_subgrid(datasets_rgh=...) - a thin knob riding the same
  build-smoke wave; the uniform-vs-reclass contrast is answerable on the plain path
  today.
- OpenQuake `tabulated_gmpe_hazard_curve`: GMPETable importable in-venv; a
  gsim-choice knob on the psha composer. Blocker is DATA (gmpe.hdf5 un-fetchable);
  recipe = ship a demo table or input-review-gate a user table, add a gsim branch,
  SA(10.0) curve.
- ELMFIRE `crown_fire_exact_solution_regression_gate` (twin of
  active_crown_fire_spread_rate_verification): rides elmfire_verification +
  crown machinery; residual is a crown-enabled verification deck vs the Cruz (2005)
  active-crown exact ellipse (Verification Case 02). Cruz analytic is closed-form;
  moderate V&V template, own job.

## Decision

Land the SWMM LID IT/PP knob (cheapest, host-side, verified discriminating,
resolves a documented open parameterization). Record the three already-COVERED
duplicates so the board stops carrying them as open. STOP-RECIPE the HEC-RAS
sediment section (no engine) and the LFM raster row (no raster leg + no data).
Tag the SFINCS structures cluster + GMPETable + crown V&V KNOB-ELIGIBLE with
precise recipes for their own future jobs (each needs either a build-smoke docker
wave or a moderate template, not a same-wave one-knob).

## Consequence

- One registered-behavior addition (a lid_type mode), registry unchanged at 254,
  EXPECTED_TEMPLATES unchanged, no image rebuild.
- 12 board rows adjudicated: 1 LANDED, 4 COVERED, 4 STOP-RECIPE, 3+ KNOB-ELIGIBLE.
- The SFINCS `setup_structures` seam is now the strongest documented next-wave LAND
  (verified machinery, only the builder wiring + a docker smoke remain).
- Verification: swmm_mechanism_compare 27 passed, catalog_surfacing + door_dissolution
  17 passed, SWMM slice (deck_runner/hyetograph/mechanism/wq) 66 passed, retrieval
  top-8 HIT x3; offline baseline unchanged (fetch_resolution_gate 4 + river_dye 2).
