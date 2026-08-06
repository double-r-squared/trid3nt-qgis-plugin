# MODULE COVERAGE BOARD - the grind queue (v1, mined 2026-08-04)

NATE doctrine: multiple templates per module until every aspect of every
engine is utilized; overlap embraced (nuance documented in the comparison
matrix at the bottom). Status: LANDED | SIGNED (build waves queued) |
CAND-S (EASY - pre-authorized to land per NATE 2026-08-04, cheap smokes)
| CAND-M / CAND-L (await sign-off). Rolling metric updated at EVERY
template landing (standing close-out rule). Mined by 13 research agents,

**TOTALS: 126 modules mined, 334 candidate template rows, of which 92 are unsigned CAND-S (pre-authorized easy tier).** (ADR 0156 closed the 4 SCHISM easy-tier rows: transport_scheme_accuracy_comparison LANDED + generic_passive_tracer_mass_conservation FOLDED into the one `schism_transport_validation` template; tidal_constituent_extraction_inrun STOP-RECIPE (iharind needs a USE_HA build); harmonic_analysis_ccrm_wiki_method_notes DOC.) (ADR 0157 closed the 6 HECRAS easy-tier rows: simple_2d_diffusion_wave_mesh LANDED as an explicit `equation_set` knob on `hecras_flood_2d` (diffusion-wave was silent skeleton inheritance; now first-class + reviewed + host-side, no image rebuild); mixed_regime_multi_profile_solve + storage_area_network_flow_reversal + pump_station_trigger_and_ramp_control + wq_module_smoke_test_suite + simple_breach_geometry_setup all STOP-RECIPE - the roster's "1D steady signed" claim was WRONG (RasSteady is in the image but never invoked), and these need genuinely-new 1D-network/structure/WQ-engine authoring, not S-tier knobs.) (ADR 0162 closed the LAST 2 SFINCS easy-tier rows the ADR 0152 wave missed due to an extraction bug: uniform_wind_timeseries_forcing LANDED as a `WindForcing.timeseries` schedule -> `setup_wind_forcing(timeseries=<csv>)` -> native sfincs.wnd; wind_drag_coefficient_curve_tuning LANDED as a new `wind_drag_curve` physics_registry knob (list of >=2 (wind_mps, cd) pairs) -> cdnrb/cdwnd/cdval, mutually exclusive with the existing flat `wind_drag`. Zero SFINCS [CAND-S] rows remain.)

WAVE-2 TRIAGE RE-SCOPE (ADR 0121, 2026-08-04): the hazard-cluster wave-2 rows
(openquake scenario + logic-tree, pelicun FEMA-P58 / HAZUS-EQ / loss-aggregation,
swmm wq_buildup_washoff, geoclaw gauge-timeseries, landlab flow-accumulator /
priorityflood / green-ampt, elmfire elliptical-verification, swan windsea-swell /
regression) were ground-truthed against the actual worker/deck/library surfaces
and NONE survived as a pure-knob drop-in. 7 are M/L needing new worker/library
deck-authoring or an image rebuild; swmm wq is already-covered by
swmm_urban_flood(pollutants); the remaining 4 (landlab flow-accum folding
priorityflood, landlab green-ampt, elmfire ellipse, geoclaw gauge) are contained
server-side / exec-mode FEATURE builds with no image rebuild -- the recommended
next-build subset. See ADR 0121 for the per-row verdicts, blockers, and the
cheapest-first build order. ALL 4 NOW LANDED: landlab_flow_accumulation (ADR 0122),
then landlab_green_ampt_overland_flow + elmfire_verification_elliptical_replication
+ geoclaw_tsunami_gauge_timeseries (ADR 0123).

citations live-verified; roster gaps recorded honestly per engine.


## SCHISM (10 modules)

### Hydro core (barotropic/baroclinic + transport schemes)
Purpose: Semi-implicit finite-element/finite-volume solver for the 3D shallow-water (baroclinic) or depth-integrated (barotropic) equations on an unstructured hybrid (tri/quad) horizontal grid with LSC2/SZ vertical layers; the base engine every other module tracers into.
Today: TRID3NT surfaces hydro-core barotropic tides only (per roster note) - no baroclinic T/S, no TVD^2/WENO scheme knob exposed, no vertical-grid choice, no hydraulic structures.
Aspects: barotropic-only tidal mode (theta implicitness, no T/S); full baroclinic 3D mode (T/S transport, density-driven circulation, vertical velocity); transport scheme selection (upwind vs TVD^2 vs 3rd-order WENO); vertical grid type (SZ hybrid vs pure LSC2 vs single sigma ivcor=1); Eulerian-Lagrangian backtracking (btrack) for advection; turbulence closure (GOTM k-eps/k-omega/MY); hydraulic structures (weirs/culverts/pumps/gates) coupling into the momentum solve
- [LANDED] `barotropic_tidal_run` [S] [US] - Given a coastal AOI and tide-only forcing (FES2014/bctides.in), what is the M2/K1-dominated water-level and depth-avg current field?
  src: https://schism-dev.github.io/schism/master/schism/barotropic-solver.html (schism-docs-barotropic-solver)
  knobs: theta (implicitness), ibc/ibtp flags (barotropic vs baroclinic), dt, hgrid/vgrid
  notes: Existing TRID3NT capability - baseline, already surfaced.
- [CAND-L] `baroclinic_3d_circulation` [L] [US] - Given a shelf/estuary AOI with T/S boundary forcing (e.g. G-RTOFS), what is the density-driven 3D current, thermocline, and stratification structure?
  src: https://schism-dev.github.io/schism/master/schism/barotropic-solver.html (schism-docs-barotropic-solver)
  knobs: ibc=0 (baroclinic on), nws, T/S IC (ts.ic), vgrid.in.SZ layer count, TS boundary .th.nc
  notes: Requires enabling the tracer transport of T/S and 3D vertical grid - new solver capability beyond current barotropic-only surface. [TRIAGED 2026-08-04, ADR 0126] as schism_estuary_circulation via Test_CORIE (Columbia R. estuary): deck + forcing SHIP (NARR sflux/hotstart/T-S nudging/river flux bundled), module=None -> runs on the EXISTING hydro-core binary, no new build. LANDABLE bake-and-parameterize; live acceptance DEFERRED (28-day 3D baroclinic nvrt=54 + ~600MB deck -> NATE-remote-drive class, coastal_tin acceptance-b posture). Recipe in ADR 0126 sec 2c.
- [LANDED] `transport_scheme_accuracy_comparison` [S] [US] (2026-08-05, ADR 0156: the row-1 deliverable of the NEW `schism_transport_validation` template. A temperature FRONT is advected across SCHISM's own QuarterAnnulus M2 tidal channel TWICE through the identical flow on the hydro-core binary `pschism_TVD-VL` - once with the TVD^2 limiter (`tvd.prop=1`), once first-order upwind everywhere (`tvd.prop=0`), the SAME `itr_met=3` code path - so the scheme is the only difference. Live product-path smoke (run_solver+MinIO, sim_days=2.0): TVD retains 95.7% of the front spatial variance vs upwind's 90.7% -> upwind numerically mixes 2.16x more. Proof docs/proof/templates/schism_transport_validation_mixing.png. TRIAGE facts: Test_HeatConsv = "Module needed: None" (verified in test_suite.md), so no image change; itr_met=1 is REJECTED ("Unknown tracer method 1") - the scheme toggle is tvd.prop, not itr_met; barotropic ibc=1 FREEZES T so the tracer needs ibc=0 + a 3D vgrid. WENO (itr_met=4) is not exercised - "Controls for WENO are not yet in place" per param.nml.) - For a heat/tracer front, how much does upwind vs TVD^2 vs WENO change numerical mixing (heat conservation) at a given Courant number?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/docs/getting-started/test_suite.md (schism-verification-Test_HeatConsv_TVD-Upwind)
  knobs: itr_met (transport scheme), TVD_lim, courant target
  notes: Test_HeatConsv_TVD / Test_HeatConsv_Upwind are published paired verification cases (svn schism_verification_tests) demonstrating the same setup at two scheme settings - exact knob-on-existing-solver candidate.
- [CAND-M] `hydraulic_structure_regulation` [M] [US] - With a weir/culvert/pump network in-domain, how does structure operation reshape upstream/downstream water levels vs the unregulated case?
  src: https://schism-dev.github.io/schism/master/modules/hydraulics.html (schism-docs-hydraulics)
  knobs: hydraulics.in (structure type/geometry/operation rule), USE_HYDRAULICS build flag
  notes: Test_HydraulicStruct is a named published verification case (schism_verification_tests, svn); manual only as external PDF (http://ccrm.vims.edu/yinglong/wiki_files/structs_main.pdf) - not independently fetched, so cite with caution.
- [CAND-M] `vertical_grid_type_sensitivity` [M] [US] - For a deep shelf-to-estuary transect, how do LSC2 vs hybrid-SZ vertical grids change bottom boundary-layer resolution and stratification capture?
  src: https://schism-dev.github.io/schism/master/schism/vertical-velocity.html (schism-docs-vertical-velocity)
  knobs: ivcor (1=LSC2, 2=SZ), vgrid.in.S / vgrid.in.SZ / vgrid.in.ivcor=1 sample files
  notes: Three vgrid sample files exist in sample_inputs/, confirming this is a first-class configuration axis.

### WWM-III (Wind Wave Model)
Purpose: Third-generation spectral wave model (Roland's WWM-III) two-way coupled into SCHISM for wind-wave generation, propagation, and wave-current interaction; source terms and dissipation follow WAM-family physics.
Today: Not surfaced in TRID3NT today (hydro-core is barotropic-tides-only; WWM is a separate compile-time module not enabled per the roster note).
Aspects: wave generation from wind forcing; wave-current interaction (radiation stress feedback into SCHISM momentum); nearshore breaking/dissipation source terms; boundary spectra nesting (WW3 or parametric spectra input); coupled wave-enhanced bottom stress for SED3D
- [CAND-L] `wave_current_interaction_nested_boundary` [L] [US] - For a coastal AOI nested inside a WW3 basin-scale forecast, what nearshore significant wave height (Hs) and wave-driven currents result from the WWM-III/SCHISM two-way coupling?
  src: https://schism-dev.github.io/schism/master/modules/wwm.html (schism-docs-wwm)
  knobs: wwminput.nml.WW3 (boundary nesting mode, source term set), USE_WWM build flag, coupling interval
  notes: wwminput.nml.WW3 sample confirmed present in sample_inputs/; module manual only in-repo LaTeX (src/WWMIII/Manual/manual.tex), not independently WebFetched.
- [CAND-M] `parametric_spectra_wave_forcing` [M] [US] - Given a prescribed offshore directional spectrum (no external wave model), what nearshore wave transformation and setup result?
  src: https://schism-dev.github.io/schism/master/modules/wwm.html (schism-docs-wwm)
  knobs: wwminput.nml.spectra, boundary spectrum shape (JONSWAP params)
  notes: wwminput.nml.spectra sample confirmed present in sample_inputs/.
- [LANDED] `published_wwm_verification_replication` [L] [US] - Do we reproduce the published Duck NC / L31-2A / VF-adiabatic WWM verification benchmarks (measured vs modeled Hs/Tp)?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/docs/getting-started/test_suite.md (schism-verification-Test_WWM_Duck)
  knobs: WWM test case configs (Test_WWM_Analytical, Test_WWM_Duck, Test_WWM_L31_2A, Test_WWM_VF_adiabatic_case, Test_WWM_limon_NODIF)
  notes: 5 named published WWM cases confirmed in the official test_suite.md table (svn schism_verification_tests); Duck NC is a documented US field site - strong published-first, US-applicable candidate for V&V-doctrine replication. [TRIAGED 2026-08-04, ADR 0126] as schism_coupled_waves via Test_WWM_Duck. STOP with build recipe: deck + spectral boundary SHIP (17MB, cheap 4-hr run); a targeted WWM-only binary builds cheaply; the two-way WWM+SCHISM coupling is PROVEN end-to-end (Hs max 2.24m / mean 1.04m, Tp 10.8s, correct cross-shore transform at Duck 12 Oct 1994 -- itur=0 GOTM-free spike). The ONE blocker: the case's itur=3 GOTM k-epsilon turbulence -- no module-free k-epsilon in v5.11.0 (itur=5 needs USE_SED, itur=3 needs USE_GOTM) and GOTM is not cmake-buildable in-tree (GOTM3.2.5 is Makefile-only). Cheapest next SCHISM template once the GOTM leg is resolved. Recipe in ADR 0126 sec 1e. [LANDED 2026-08-04, ADR 0131] as `schism_coupled_waves`: the GOTM blocker RESOLVED via a cmake shim compiling in-tree GOTM 3.2.5's turbulence+util as cmake targets -> `pschism_WWM_GOTM_TVD-VL` builds green; the FAITHFUL itur=3 GOTM Duck run reproduces the cross-shore Hs transect at correlation 0.94 (16 gauges; RMSE 0.32m, offshore obs/mod 1.84/2.19m -- the shape fidelity is the load-bearing V&V). Registered template + entrypoint `wwm` variant + SHA-pinned fixture. Duck ONLY this wave (L31-2A / VF-adiabatic / Analytical / limon remain unlanded on the same binary).

### SED3D / SED2D (sediment transport & morphology)
Purpose: Multi-class non-cohesive (and cohesive, via bed layering) sediment transport adapted from the Community Sediment Transport Model, with an Exner-equation bed-evolution (morphology) option; SED2D is a simplified depth-averaged variant.
Today: Not surfaced in TRID3NT today.
Aspects: suspended-load transport (multi-class concentration tracers); bed morphology / Exner-equation bed evolution (sed_morph); multi-layer bed stratigraphy (Nbed); wave-enhanced bottom stress (requires WWM coupling); point-source sediment input at river boundaries (dry-boundary crash mitigation); 2D depth-averaged variant (SED2D) vs full 3D (SED3D)
- [CAND-M] `multiclass_suspended_sediment_transport` [M] [US] - Given river + tidal forcing and multiple grain-size classes, what is the resulting suspended sediment concentration field (no morphology)?
  src: https://schism-dev.github.io/schism/master/modules/sed3d.html (schism-docs-sed3d)
  knobs: sediment.nml (grain classes, settling velocity, critical shear), Nbed=1, sed_morph=0
  notes: sediment.nml sample confirmed in sample_inputs/.
- [CAND-M] `morphodynamic_bed_evolution` [M] [US] - Over a tidal/river forcing period, how does the bed elevation evolve (accretion/erosion pattern) under active morphology?
  src: https://schism-dev.github.io/schism/master/modules/sed3d.html (schism-docs-sed3d)
  knobs: sed_morph=1, sed_morph_time, morph_fac (time-acceleration factor), Nbed multi-layer
  notes: Doc explicitly warns of a known river-boundary dry-out failure mode - worth encoding as a guardrail in the template.
- [CAND-L] `trench_migration_benchmark_replication` [L] [US] - Do we reproduce the published Trench Migration benchmark (measured vs modeled bed profile evolution) for SED3D vs SED2D?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/docs/getting-started/test_suite.md (schism-verification-Test_SED_Trench_Migration)
  knobs: Test_SED_Trench_Migration, Test_Sed2d_Trench_Migration, Test_SED_meander_2 (published verification configs)
  notes: 3 named published sediment cases confirmed in official test_suite.md; classic morphodynamic benchmark with documented expected bed-profile output - strong V&V candidate.
- [CAND-L] `wave_enhanced_bottom_stress_sediment` [L] [US] - With WWM wave coupling active, how much does wave-enhanced bottom shear stress change sediment resuspension vs the current-only case?
  src: https://schism-dev.github.io/schism/master/modules/sed3d.html (schism-docs-sed3d)
  knobs: USE_WWM + USE_SED3D joint compile, wave-current bottom stress formulation switch
  notes: Requires the WWM module already enabled - cross-module coupling capability, new build-flag combination.

### ICM (Integrated Compartment Model water quality, + CoSiNE, + FIB)
Purpose: USACE-lineage eutrophication/water-quality model tracking ~17+ state variables (C/N/P/phytoplankton/DO) with optional sub-modules for silica, zooplankton, pH/alkalinity, SAV/marsh vegetation, benthic sediment flux, and CoSiNE (alternate N-Si-C ecosystem model) or FIB (fecal indicator bacteria) as parallel tracer sets.
Today: Not surfaced in TRID3NT today.
Aspects: core eutrophication state variables (C/N/P cycling, 3 phytoplankton groups, DO); silica sub-module; zooplankton sub-module; pH/alkalinity sub-module; SAV / marsh vegetation coupling (imarsh_icm) with N/P dynamics option (iNmarsh); benthic sediment flux model (iSFM); CoSiNE alternate biogeochemistry (nutrient-plankton-oxygen-silica cycling); FABM-CoSiNE coupling variant; FIB fecal indicator bacteria (separate tracer module, not confirmed in fetched ICM page - flagged as a gap)
- [CAND-M] `eutrophication_core_wq_run` [M] [US] - Given nutrient loading (point + nonpoint) into an estuary, what is the resulting chlorophyll-a / dissolved-oxygen response?
  src: https://schism-dev.github.io/schism/master/modules/icm.html (schism-docs-icm)
  knobs: icm.nml core switches (iKe, iLight, iPR, iZB), 3-phytoplankton-group loading
  notes: icm.nml sample confirmed in sample_inputs/ with ~20 named integer switches verified via raw fetch.
- [CAND-L] `chesapeake_bay_icm_benchmark_replication` [L] [US] - Do we reproduce the published Chesapeake Bay ICM benchmark (Test_ICM_ChesBay) DO/chlorophyll seasonal cycle?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/docs/getting-started/test_suite.md (schism-verification-Test_ICM_ChesBay)
  knobs: Test_ICM_ChesBay published config (also Test_ICM_UB)
  notes: Chesapeake Bay is a US estuary with a long published USACE/SCHISM ICM calibration history - strong published-first, US-applicable V&V candidate; 2 named cases confirmed in official test_suite.md.
- [CAND-M] `marsh_nutrient_coupling_icm` [M] [US] - With marsh vegetation N/P dynamics turned on, how does marsh uptake change the estuary-wide nutrient budget vs the no-marsh case?
  src: https://schism-dev.github.io/schism/master/modules/icm.html (schism-docs-icm)
  knobs: imarsh_icm (0/1/2: off/mechanistic/simple), iNmarsh (N/P dynamics on/off)
  notes: Cross-links to the standalone marsh-migration module; icm.nml flags verified via raw content fetch.
- [CAND-L] `cosine_sfbay_benchmark_replication` [L] [US] - Do we reproduce the published San Francisco Bay CoSiNE benchmark nutrient/plankton cycle (with the FABM-coupled variant as an alternate)?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/docs/getting-started/test_suite.md (schism-verification-Test_COSINE_SFBay)
  knobs: Test_COSINE_SFBay, Test_FABM_COSINE_SFBay (published configs); cosine.nml switches idelay, ibgraze, idapt, iz2graze
  notes: San Francisco Bay is a US estuary; 2 named published cases confirmed; cosine.nml content verified via raw fetch (idelay/ibgraze/idapt/iz2graze confirmed real switches).
- [CAND-M] `fib_bacteria_indicator_transport` [M] [US] - Given a wastewater/CSO point source, what is the downstream fecal-indicator-bacteria (FIB) decay/dilution plume and does it exceed a recreational-water threshold?
  src: https://schism-dev.github.io/schism/master/modules/overview.html (schism-docs-overview)
  knobs: FIB tracer module switches (not independently verified in detail)
  notes: ROSTER GAP: overview.md confirms 'Fecal bacteria' as one of the 12 tracer modules, but the dedicated FIB doc page was not enumerated/fetched in this pass (no modules/fib.html found in the docs/modules directory listing) - verify a dedicated page exists before committing effort; module list only names it in overview.md.

### GEN / AGE tracers
Purpose: GEN is a generic passive-tracer template (constant settling velocity, user-customizable) used both as a standalone capability and as a scaffold for bespoke tracer physics; AGE tracks water age/residence time via paired concentration+age tracers per Shen & Haas (2004).
Today: Not surfaced in TRID3NT today.
Aspects: GEN: multi-class passive tracer with constant/negative (swimming) settling velocity; GEN: mass-conservation verification; AGE: paired concentration/age tracer injection at a specified level and region; AGE: residence-time / water-age field output
- [LANDED-FOLD] `generic_passive_tracer_mass_conservation` [S] [US] (2026-08-05, ADR 0156: FOLDED into `schism_transport_validation` as the row-2 deliverable - the domain-integrated conservative-tracer MASS drift over the run, the numerical-scheme sanity gate. Live smoke (sim_days=2.0): TVD mass drift -0.69%, upwind -1.04%, both within the +/-3% bound (the drift is open-boundary tidal exchange oscillating at the M2 period). Proof docs/proof/templates/schism_transport_validation_mass_conservation.png. TRIAGE: Test_GEN_MassConsv = "Module needed: GEN", and USE_GEN is a full-monty-only build whose binary unconditionally initializes EVERY module (every namelist required) - heavy/fragile. The mass-conservation MECHANISM is identical for any conservative scalar, so the template demonstrates it with a TEMPERATURE tracer on the clean hydro-core binary (zero namelist burden, zero image change); the GEN-module-specific path is documented in the result's gen_module_note as a full-monty recipe, not built.) - Injecting a conservative generic tracer at a boundary, does the domain-integrated mass stay conserved over the run (numerical-scheme sanity check)?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/docs/getting-started/test_suite.md (schism-verification-Test_GEN_MassConsv)
  knobs: ntracer_gen, gen_wsett (settling velocity), flag_ic(3), Test_GEN_MassConsv / Test_GEN_MassConsv2 published configs
  notes: 2 named published GEN test cases confirmed in official test_suite.md - direct knob-on-existing-transport-solver candidate, minimal new capability.
- [CAND-M] `water_age_residence_time` [M] [US] - For a specified sub-region (e.g. an estuary arm or reservoir), what is the mean water age / residence time under current forcing?
  src: https://schism-dev.github.io/schism/master/modules/age.html (schism-docs-age)
  knobs: ntracer_age (paired tracers), level_age (injection level per pair), iof_age output flags, AGE_hvar_*.ic region files
  notes: Shen & Haas (2004) is the cited theoretical foundation; no dedicated verification test case was found in test_suite.md for AGE specifically - flag as a minor gap (relies on the general transport solver's own verification).

### Marsh migration
Purpose: Long-term (multi-decadal) marsh migration model under sea-level rise, tracking marsh extent evolution and optionally coupling to vegetation drag/turbulence and sediment/wave modules per Nunez et al. (2020).
Today: Not surfaced in TRID3NT today.
Aspects: sea-level-rise-driven marsh extent migration (slr_rate); migration barrier constraint (marsh_barrier.prop); vegetation drag/turbulence coupling (isav); optional coupling to SED and WWM for physically-consistent marsh accretion
- [CAND-M] `slr_marsh_extent_migration` [M] [US] - Under a specified sea-level-rise rate, how does marsh extent (element-level presence/absence) shift over a multi-decadal run?
  src: https://schism-dev.github.io/schism/master/modules/marsh-migration.html (schism-docs-marsh-migration)
  knobs: USE_MARSH build flag, slr_rate (mm/yr), marsh_init.prop, marsh_barrier.prop, iof_marsh output
  notes: Nunez et al. (2020) cited as the published methodological foundation - no direct schism_verification_tests entry found for marsh (roster_gap); knobs verified via WebFetch of the module page.
- [CAND-M] `vegetated_marsh_drag_coupling` [M] [US] - With marsh vegetation form-drag/turbulence enabled, how does flow attenuation through the marsh differ from an unvegetated bare-marsh case?
  src: https://schism-dev.github.io/schism/master/modules/marsh-migration.html (schism-docs-marsh-migration)
  knobs: isav (vegetation option) combined with USE_MARSH
  notes: Cross-links to the general vegetation/drag capability in the momentum solver.

### PaHM (Parametric Hurricane Model)
Purpose: NOAA/CSDL parametric tropical cyclone wind/pressure field generator (GAHM and symmetric-vortex options) that forces SCHISM directly from best-track data, optionally blended with a regional NWP (e.g. HRRR/GFS) wind field; operational in STOFS-3D-Atlantic.
Today: Not surfaced in TRID3NT today.
Aspects: GAHM (Generalized Asymmetric Holland Model) parametric vortex; symmetric-vortex simpler formulation; blending with a regional weather model wind field; best-track (a/b-deck) ingestion
- [CAND-L] `besttrack_parametric_hurricane_wind_forcing` [L] [US] - Given a historical Atlantic hurricane's best-track (b-deck), what is the resulting storm-surge water-level response using PaHM-generated GAHM winds alone?
  src: https://schism-dev.github.io/schism/master/modules/pahm.html (schism-docs-pahm)
  knobs: PaHM_inputs (b-deck file), GAHM vs symmetric-vortex switch, pahm_control.in
  notes: New coupled forcing capability (build+run PaHM ahead of/alongside SCHISM); manual hosted externally at noaa-ocs-modeling.github.io/PaHM (not independently WebFetched - cite with caution).
- [CAND-M] `hurricane_niran_gahm_vs_regional_wx_blend_replication` [M] [non-US] - Do we reproduce the published Cyclone Niran (2021) comparison of GAHM-only vs regional-weather-model-blended wind/surge output?
  src: https://github.com/schism-dev/schism/tree/master/sample_inputs/PaHM_inputs (schism-repo-sample-inputs-PaHM-niran2021)
  knobs: niran2021-bdeck.dat + cyclone_Niran_RegionalWeatherModel_vs_GAHM.jpg / _vs_HM.jpg comparison figures
  notes: Cyclone Niran struck New Caledonia/Australia region - NOT a US case; confirmed present via gh api listing (niran2021-bdeck.dat, two comparison JPGs) but fails the US-only doctrine. Keep as a mechanical-contract PaHM smoke test only, not a V&V replication target; pair with a genuine US Atlantic storm best-track for the real V&V candidate.
- [CAND-L] `stofs3d_operational_pahm_forcing_pattern` [L] [US] - Following the STOFS-3D-Atlantic operational pattern, what does an end-to-end PaHM-forced SCHISM storm-surge nowcast/forecast pipeline look like for a US Atlantic-basin storm?
  src: https://registry.opendata.aws/noaa-nos-stofs3d/ (noaa-stofs3d-atlantic-aws-registry)
  knobs: PaHM + NWM river coupling + G-RTOFS open-boundary; operational STOFS-operational repo config under noaa-ocs-modeling/STOFS-operational/stofs_3d_atl
  notes: Confirmed live: NOAA STOFS-3D-Atlantic is the first operational SCHISM system at NOAA (Jan 2023), US Atlantic basin, published config repo noaa-ocs-modeling/STOFS-operational verified to exist via gh api (contains stofs_3d_atl/ subdir). Best published-first, US-applicable PaHM template candidate.

### Harmonic analysis (in-code HA)
Purpose: Online harmonic analysis of the elevation (and optionally velocity) time series during the run itself, decomposing the signal into named tidal constituents (S2/M2/N2/K1/O1/Q1 etc.) without a separate post-processing pass.
Today: Not surfaced in TRID3NT today - not mentioned in the roster note at all (gap in current coverage awareness).
Aspects: in-run harmonic decomposition of water-surface elevation; (inactive/???) velocity harmonic decomposition; constituent list + nodal factor/argument specification; de-tending percentage / analysis window (start/end day)
- [STOP-RECIPE] `tidal_constituent_extraction_inrun` [S] [US] (2026-08-05, ADR 0156 triage: the S-label "knob on the existing barotropic-tidal surface" is FALSE. iharind requires the USE_HA compile flag, which the shipped tidal binary `pschism_TVD-VL` (hydro-core build, no module toggles) does NOT have - param.nml itself says "If used, need to turn on USE_HA in Makefile". EMPIRICALLY CONFIRMED: the hydro binary runs the QA deck to completion with iharind=1 (param.out.nml echoes IHARIND=1) but writes NO harmonic-analysis output - the flag is a SILENT no-op because USE_HA is uncompiled. RECIPE to land: either (a) add a targeted USE_HA=ON hydro variant to the worker Dockerfile (a 4th binary, mirroring the WWM+GOTM leg) + author harm.in (the 6-constituent S2/M2/N2/K1/O1/Q1 sample with real angular freqs) + iharind=1 + combine the per-rank harme output; or (b) drive the full-monty binary, which HAS USE_HA compiled (`_HA_` in its name) but demands every module namelist. Either is a WORKER-IMAGE rebuild, out of easy-tier scope. Zero registry growth.) - Over a multi-week tidal run, what are the extracted M2/K1/O1/... amplitude and phase at every node, without a separate post-processing harmonic-analysis pass?
  src: https://raw.githubusercontent.com/schism-dev/schism/master/sample_inputs/param.nml (schism-repo-sample-inputs-param-nml-iharind)
  knobs: iharind (0/1 flag), USE_HA build flag, harm.in (constituent list, angular freq/nodal factor/argument, analysis start/end day, de-tending %), combine_outHA post-combiner
  notes: Verified live via raw GitHub fetch of both param.nml (iharind flag + comment block) and harm.in (6-constituent S2/M2/N2/K1/O1/Q1 sample with real angular frequencies) - a knob-on-existing-solver candidate since TRID3NT already runs barotropic tides; this only adds the built-in analysis output.
- [DOC] `harmonic_analysis_ccrm_wiki_method_notes` [S] [US] (2026-08-05, ADR 0156: DOC-class method notes, no template. The harme.53 -> combine_outHA -> ad2tct.f chain (Dr. Andre Fortunato's ADCIRC-derived offline post-processor per the CCRM wiki) reads the per-rank harme.53 least-squares harmonic fit SCHISM writes when USE_HA+iharind are on, combine_outHA stitches the subdomain outputs into global per-node amplitude+phase per constituent, and ad2tct.f converts to a tidal-constituent table. It is functionally equivalent to an offline t_tide/UTide fit of the elevation time series, differing only in that the least-squares fit is assembled DURING the run (no full time-series output needed) rather than after. VERIFIED: none of these utilities ship in the worker image (`ls /opt/schism/bin` = the 3 pschism binaries only; combine_outHA/harme/ad2tct absent - the SCHISM cmake used here does not build them). The offline t_tide/UTide route over a station/out2d elevation series is the already-available alternative; method notes recorded in ADR 0156. Zero registry growth.) - What does the harme.53 -> combine_outHA -> ad2tct.f post-processing chain produce, and how does it compare to an offline t_tide/UTide harmonic fit?
  src: https://ccrm.vims.edu/w/index.php/Harmonic_analysis (ccrm-wiki-harmonic-analysis)
  knobs: combine_outHA, ad2tct.f auxiliary programs; attribution to Dr. Andre Fortunato using ADCIRC-derived routines
  notes: Found via WebSearch only (not independently WebFetched in this pass) - treat citation as provisional; corroborates the param.nml/harm.in findings which ARE independently verified.

### Particle tracking (ptrack)
Purpose: Standalone post-processing utility (reads schout*.nc) for 3D Lagrangian particle tracing driven by a completed SCHISM run, supporting passive drifters or an oil-spill mode with dispersion/beaching.
Today: Not surfaced in TRID3NT today.
Aspects: passive particle transport (drogue-style); oil-spill mode (dispersion + beach retention); forward vs backward tracking; free-surface-following ('stiff') vertical positioning; sub-stepping for accuracy (ndeltp)
- [CAND-M] `passive_drifter_backtracking` [M] [US] - Released at a point/time, where did the water parcel(s) at a given location originate from (backward tracking) or where do they end up (forward tracking)?
  src: https://schism-dev.github.io/schism/master/modules/particle-tracking.html (schism-docs-particle-tracking)
  knobs: mod_part=0 (passive), ibf (1=forward/-1=backward), particle.bp (particle list/timing/coords), ndeltp
  notes: Post-processing utility separate from the main solver build - requires completed schout*.nc, hgrid.gr3, vgrid.in as inputs; no dedicated verification test case found in test_suite.md (roster_gap) though Test_Btrack_Cone/Test_Btrack_Gausshill exist for the internal Eulerian-Lagrangian backtracking algorithm (a different, solver-internal capability, not the standalone ptrack utility) - do not conflate the two.
- [CAND-M] `oil_spill_dispersion_beaching` [M] [US] - For a point-source oil release, what is the surface slick trajectory and shoreline beaching pattern including dispersion loss?
  src: https://schism-dev.github.io/schism/master/modules/particle-tracking.html (schism-docs-particle-tracking)
  knobs: mod_part=1 (oil spill), dispersion parameters, beach-retention logic in particle.bp
  notes: Same utility, alternate mode; no published test case/expected-output was found for the oil-spill mode specifically.

### Ice (single-class + multi-class/Icepack)
Purpose: Sea-ice dynamics/thermodynamics coupled to SCHISM: a lighter single-class module (FESOM2-derived, VP/EVP/mEVP rheology) and a heavier multi-class module coupling the Los Alamos Icepack column-physics package for full ice-thickness-distribution physics.
Today: Not surfaced in TRID3NT today; per the fold-doctrine roster (SFINCS is screening-only, hydro-core tides-only) ice physics is entirely unaddressed - likely low near-term priority given a US-hazard focus, but included per the exhaustive-mining mandate.
Aspects: single-class: VP rheology (implicit, stiff); single-class: EVP rheology (pseudo-elastic, larger timestep); single-class: mEVP with mesh-adaptive coefficients; multi-class: ice-thickness-distribution (ITD) for sub-grid heterogeneity; multi-class: thermodynamic formulation choice (zero-layer / Bitz-Lipscomb constant-salinity / mushy-layer evolving-salinity); multi-class: melt-pond parameterization; multi-class: mechanical redistribution (ridging/rafting); multi-class: shortwave scheme (CCSM3 vs delta-Eddington); multi-class: hotstart mode (cold-start / external-model init e.g. HYCOM / restart)
- [CAND-L] `single_class_sea_ice_mevp` [L] [US] - For a seasonally ice-covered US water body (e.g. Great Lakes), what ice area/thickness/drift results from the mEVP rheology at operational mesh resolution?
  src: https://schism-dev.github.io/schism/master/modules/single-class-ice.html (schism-docs-single-class-ice)
  knobs: USE_ICE build flag, ice.nml (mevp_coef, thermodynamic constants), nstep_ice, ice_fct.gr3
  notes: ice.nml sample confirmed present in sample_inputs/; Great Lakes is a plausible US-applicable use case though no published SCHISM ice test case/site was independently verified (roster_gap on a concrete US example).
- [CAND-L] `multiclass_icepack_thickness_distribution` [L] [US] - With full Icepack ITD physics + mushy-layer thermodynamics, how does the multi-class ice module's simulated melt-pond fraction and ridging compare to the simpler single-class module for the same forcing?
  src: https://schism-dev.github.io/schism/master/modules/multi-class-ice.html (schism-docs-multi-class-ice)
  knobs: USE_MICE + USE_EVAP build flags, mice.nml, namelist.icepack (Icepack v1.3.4 params), ice_advection=6 (recommended hybrid TVD-upwind), ihot_mice (0/1/2)
  notes: mice.nml and namelist.icepack samples confirmed present in sample_inputs/; no dedicated verification test case found in test_suite.md (roster_gap) - this is the heaviest-build-flag, most speculative candidate in the whole roster given near-zero near-term US-hazard relevance.


## HECRAS (7 modules)

### 1D Steady Flow
Purpose: Solve the standard-step energy equation along a river network for a set of fixed discharge profiles to produce water-surface profiles and floodplain extents.
Today: roster note said steady flow is already signed - CORRECTED (ADR 0157): this is WRONG. NO steady solve is wired; every HEC-RAS workflow runs RasUnsteady, and `RasSteady` (in the image) is never invoked. A 1D steady deck author + RasSteady leg is unbuilt (see mixed_regime_multi_profile_solve STOP-RECIPE below).
Aspects: multi-profile standard-step solve; mixed flow-regime (sub/super-critical) passes; Manning's n calibration to observed high-water marks; floodway/encroachment determination
- [CAND-M] `steady_profile_calibration_to_high_water_marks` [M] [US] - Given N discharge profiles and a set of observed high-water marks, what Manning's n zoning (base vs NLCD-refined) minimizes profile error at those marks?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/1d-steady-flow/steady-flow-modeling-with-hec-ras (hecras_hgt_steady_flow_merced_yosemite)
  knobs: 6 discharge profiles 100-10,000 cfs; base Manning's n=0.04 vs NLCD-2019-derived spatial roughness; normal-depth downstream BC; calibration target = observed HWM 3966 ft at 10,000 cfs
  notes: Merced River at Yosemite Valley, CA - published tutorial with a real HWM calibration target and a documented before/after roughness-refinement improvement.
- [CAND-M] `mixed_regime_multi_profile_solve` [S] [US] - Across a reach with alternating subcritical/supercritical reaches, does the steady solver correctly locate hydraulic jumps via the mixed-flow-regime algorithm? [STOP-RECIPE 2026-08-05 (ADR 0157): the roster's "1D steady signed" claim is WRONG - NO steady solve is wired anywhere; every HEC-RAS workflow runs RasGeomPreprocess + RasUnsteady, and while `RasSteady` IS baked in the solver image it is NEVER invoked. Recipe: author a 1D steady geometry (mixed sub/supercritical reach) + a `.fNN` multi-profile flow file + invoke RasSteady + parse the steady WSE profile HDF. This is a NEW solver leg (steady deck author + RasSteady wiring), not an S-tier knob.]
  src: https://www.hec.usace.army.mil/confluence/rasdocs/ras1dtechref/6.5/theoretical-basis-for-one-dimensional-and-two-dimensional-hydrodynamic-calculations/1d-steady-flow-water-surface-profiles (hecras_ras1dtechref_steady_profiles_theory)
  knobs: flow regime selector (subcritical/supercritical/mixed); critical-depth method (parabolic vs secant)
  notes: Theoretical-basis chapter of the Hydraulic Reference Manual; existing steady-flow capability, this is a knobs-on-existing (S) case exercising mixed regime rather than a new recipe.
- [CAND-M] `steady_floodway_encroachment_delineation` [M] [US] - Given a base 1% annual-chance profile, what channel/floodplain encroachment stations produce a target water-surface rise (regulatory floodway) at every cross section?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasappguide/latest/floodway-determination-example-6 (hecras_appguide_example6_floodway_beaver_creek)
  knobs: encroachment Method 1/4/5 selector; target WS rise (0.7-1.1 ft trialed); target energy change (1.2 ft); per-station encroachment overrides
  notes: Beaver Creek Kentwood reach, 14,000 cfs (1% flood); published trial-by-trial encroachment stations and resulting WS rise (1.00-1.01 ft) - this is the FEMA-style regulatory-floodway deliverable, not currently in the roster.

### 1D Unsteady Flow
Purpose: Solve the full Saint-Venant equations through a river network with junctions, storage areas, and internal structures under time-varying flow.
Today: 1D unsteady on shipped geometry is signed per the roster note
Aspects: full unsteady St. Venant network routing; hydrologic (Modified Puls) routing as a stability fallback; flow-reversal / storage-area interaction; hydrograph optimization / calibration against gauges
- [CAND-M] `modified_puls_vs_full_unsteady_reconciliation` [M] [US] - On a steep reach where full unsteady routing goes unstable, does switching affected river stations to Modified Puls hydrologic routing keep water-surface results within a couple inches of the full solve?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasappguide/latest/hydrologic-unsteady-routing-modified-puls-example-19 (hecras_appguide_example19_modpuls_bald_eagle)
  knobs: steady-flow storage/discharge relationship (30 profiles, 1,000-60,000 cfs); computation interval 30s; Modified-Puls region toggle per river station; tailwater-check on/off
  notes: Bald Eagle River, PA. Published finding: WSE differences 'within a couple of inches or less' vs full unsteady - a concrete pass/fail regression target.
- [CAND-M] `storage_area_network_flow_reversal` [S] [non-US] - On a flat multi-reach network with junctions and storage areas, does the unsteady solver correctly reproduce flow reversal and lateral-weir activation as tailwater exceeds headwater? [STOP-RECIPE 2026-08-05 (ADR 0157): no 1D-network authoring exists - the only geometry paths are shipped-Muncie reparameterization and fresh pure-2D mesh authoring (which strips all Structures). Recipe: author a multi-reach 1D network deck (junctions + 4 storage areas + lateral weirs/bridges/culverts) as the synthetic Diamond River fixture + unsteady BCs, then solve + check flow-reversal sign at the junction. NEW 1D-network deck author, not an S-tier knob.]
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasappguide/latest/unsteady-flow-application-example-17 (hecras_appguide_example17_diamond_river)
  knobs: upstream hydrograph (100-5000 cfs baseflow/peak); normal-depth downstream slope 0.0000947; 15-min computation interval; 4 storage areas; lateral-weir/bridge/culvert network
  notes: Synthetic 'Diamond River' network (not a real US stream, so us_applicable=false as a case, though the mechanic is broadly applicable) - good pure-mechanism regression fixture for reversal/junction behavior.
- [CAND-M] `unsteady_hydrograph_optimization_calibration` [M] [US] - Given an observed downstream stage/flow gauge record, what upstream boundary hydrograph and/or n-value adjustment minimizes simulated-vs-observed error via HEC-RAS's built-in optimization?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/flow-hydrograph-optimization (hecras_hgt_flow_hydrograph_optimization)
  knobs: optimization target (gauge time series); parameters allowed to vary (BC hydrograph ordinates, Manning's n); convergence tolerance
  notes: Titled under the 2D-unsteady tutorial tree but the optimization mechanism is BC/routing-level and applies to 1D unsteady runs too; not yet WebFetched for full detail - flag for a closer read before building.

### 2D Unsteady Flow
Purpose: Solve either the Diffusion-Wave or full Shallow-Water Equations on an unstructured/structured 2D mesh, standalone or coupled to 1D reaches.
Today: 2D unsteady on shipped geometry is signed; levee handling is LANDED -- `hecras_levee_breach` template (ADR 0125, 2026-08-04): toggles the shipped Muncie deck's lateral-structure breach (levee fails -> ~4881 wet cells / 20.24 ft on the protected 2D floodplain; levee holds -> valid dry success, 0 wet cells), acceptance GREEN through the registered template. Fresh-AOI 2D flood is LANDED -- `hecras_flood_2d` template (ADR 0140, 2026-08-05): authors a brand-new 2D mesh on fetched 3DEP terrain for ANY US AOI (Linux authoring worker image + geometry writer, no shipped deck), solved by the production 6.6 engine; acceptances = Wabash/New Harmony IN (2824 wet, 6.9e-5% vol err) + Blanco/Wimberley TX (canyon-confined, 5.1e-6% vol err), both flux-balanced and correctly geolocated. Geometry authoring is UNBLOCKED (the 0127-0140 beta arc discharged the Windows-Phase-1 dependency); the CAND-S/M rows below are now buildable
Aspects: mesh generation + cell-size/breakline tuning; equation-set selection (Diffusion Wave vs full SWE) + accuracy/stability tradeoff; combined 1D-2D coupling via SA/2D connections; Courant-driven timestep + convergence troubleshooting; levee/refinement-region mesh control
- [LANDED] `simple_2d_diffusion_wave_mesh` [S] [US] - For a river reach draining to a defined floodplain, does a uniform-cell diffusion-wave 2D mesh with tributary inflows and normal-depth downstream BC converge to a stable, low-volume-error solution? [LANDED 2026-08-05 (ADR 0157): `hecras_flood_2d` already authors a uniform-cell 2D mesh + normal-depth DS BC and solved diffusion-wave by SILENT skeleton inheritance (the copied Muncie plan carries `2D Equation Set = Diffusion Wave`). This landing exposes it as an explicit `equation_set` knob ("diffusion_wave" default, VALIDATED / "full_swe" advanced) on the template + `compose_pure2d_deck`, input-reviewed, offline-tested, host-side (no image rebuild). Live carve->compose->solve smoke (Muncie NW-quadrant, 2068 cells, 2000 cfs): 1906 wet / max depth 12.22 ft / WSE 946.94 / 0.011% vol err, reproducing ADR 0138 exactly; SWE-ELM stamps + solves green, coinciding to 6 digits on this low-gradient reach (a DW-vs-SWE DIFFERENCE needs an inertial regime = the M-row's job, now unblocked). Proof: docs/proof/templates/hecras_flood_2d_equation_set_convergence.png. RESIDUAL: multi-inflow TRIBUTARY BC lines (compose authors 1 Inflow + 1 DS) - noted, not built.]
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/creating-a-simple-2d-model (hecras_hgt_simple_2d_bald_eagle)
  knobs: 500-ft uniform cell spacing; Manning's n=0.04; 3 tributary hydrographs; normal-depth downstream slope 0.001; 1-min timestep; ~20 hr warmup
  notes: Bald Eagle Creek near Lock Haven, PA; published finding: <0.1 ft WSE difference between 1-min and 10-min timestep near Lock Haven, useful as a timestep-sensitivity regression gate.
- [CAND-M] `2d_model_stability_diagnostic_sweep` [M] [US] - Given a levee-protected town downstream of a 2D mesh, does systematically tightening timestep (Courant~1.0), fixing downstream slope, raising culvert inverts to cell minimums, and re-aligning cells reduce volume error below a documented threshold?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/troubleshooting-a-2d-model (hecras_hgt_2d_troubleshooting_bald_eagle)
  knobs: computational timestep 30s->10s; downstream normal-depth slope 0.2->0.0006; culvert invert vs cell-min elevation; mesh alignment perpendicular to flow; Advanced Convergence Criteria toggle
  notes: Same Bald Eagle Creek/Lock Haven site, protecting a real levee-protected town; published 5-trial convergence path ending at volume error <0.000001% and max WSE error ~0.05 ft - a strong automated-diagnostic template (mesh QA agent).
- [CAND-M] `combined_1d2d_pump_station_coupling` [M] [US] - Where a 2D floodplain is separated from a river/storage area by a levee with an interior drainage pump, does the SA/2D pump-station connection correctly trigger on stage and respect startup-transition ramping?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/6.6/development-of-a-2d-or-combined-1d-2d-model/modeling-pump-stations-inside-2d-flow-areas (hecras_r2dum_pump_stations_2d)
  knobs: pump connection endpoints (2D area/storage area/1D XS); up to 10 pump groups x 10 pumps; on/off trigger elevations; startup transition time; efficiency curve minus system losses
  notes: Manual figure examples (TestPump, SAPump) rather than a numbered worked example with published output - no quantitative benchmark found; good capability coverage regardless.
- [CAND-M] `2d_diffusion_wave_vs_full_swe_regression` [M] [US] - For the same breach-driven 2D floodplain inundation, how much do peak discharge, arrival time, and max WSE differ between the Diffusion Wave and full Shallow Water Equations solvers at matched mesh/timestep?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/dam-breach-analysis-with-2d-areas (hecras_hgt_dam_breach_2d_areas_bald_eagle)
  knobs: equation set toggle (Diffusion Wave vs full SWE); 2D cell size 500ft->250ft sensitivity; timestep 20s; Manning's n=0.04
  notes: Same Bald Eagle Creek/Sayers Dam site; published numbers: ~516,000 cfs peak breach outflow (~435,000 cfs breach component), ~305,000 cfs at downstream boundary - concrete regression targets for an equation-set A/B template.

### Structures (Bridges / Culverts / Gates / Pumps)
Purpose: Model hydraulic losses and controlled/uncontrolled conveyance through in-line and lateral structures - bridges, culverts, gated spillways, rating-curve/time-series outlets, and pump stations - inside 1D reaches or 2D flow areas.
Today: unknown from roster note - not explicitly listed as signed; likely present only implicitly within shipped 1D/2D geometry
Aspects: multi-opening bridge+culvert+relief flow split; combined weir/gate/culvert/outlet inline structure; pump-station triggering and ramping; user-defined operation rules for gates and pumps; 1D-vs-2D bridge hydraulics (rating-curve family vs 2D pressure/overtopping)
- [CAND-M] `multi_opening_flow_split_bridge_culvert_relief` [M] [US] - At a single river station with a culvert group, a main bridge, and a relief bridge all conveying simultaneously, does HEC-RAS's flow-distribution algorithm split total discharge across openings within energy-balance tolerance?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasappguide/latest/multiple-openings-example-5 (hecras_appguide_example5_multiple_openings_beaver_creek)
  knobs: stagnation-point X-coordinates (fixed vs free); expansion/contraction reach lengths (290/190 ft) and coefficients (0.5/0.3 near structure, 0.3/0.1 elsewhere); ineffective-flow block method; 3 steady profiles (5,000/10,000/14,000 cfs)
  notes: Beaver Creek, published flow split at 14,000 cfs: 567 cfs culverts / 9,950 cfs main bridge / 3,483 cfs relief bridge, energy balanced within 0.05 ft - a strong quantitative regression target.
- [CAND-M] `advanced_inline_structure_multi_component` [M] [US] - For an in-line dam-like structure combining an overflow weir, a gated spillway, two culverts, an outlet rating curve, and a time-scheduled hydropower outlet, does the combined discharge rating reproduce each component's expected regime (free vs submerged)?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasappguide/latest/advanced-inline-structure-modeling-example-18 (hecras_appguide_example18_beaver_creek_kentwood)
  knobs: sluice gate (5ft height x 10ft width, invert 210ft, Cd=0.6); 2 culverts (circular + box); outlet rating curve; time-series outlet (0/150/220 cfs by time-of-day)
  notes: Beaver Creek Kentwood reach; no single numeric benchmark but explicit component-by-component discharge behavior to check via detailed output tables.
- [CAND-M] `pump_station_trigger_and_ramp_control` [S] [US] - For an interior-drainage pump station lifting flow from a 2D floodplain over a levee into a storage area, does the pump correctly stage-trigger on/off and ramp through its startup-transition time rather than stepping instantaneously? [STOP-RECIPE 2026-08-05 (ADR 0157): no pump-station machinery - `compose_pure2d_deck` strips all Structures and the Muncie deck has no pump. Recipe: author a pump-station structure (SA/2D connection endpoints, pump groups x pumps, on/off trigger elevations, startup ramp, efficiency curve) into the plan HDF + geometry, then solve + verify the pumped-outflow ramp vs step. NEW structure-authoring capability, not an S-tier knob.]
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasum/6.4/advanced-features-for-unsteady-flow-routing/modeling-pump-stations (hecras_rasum_modeling_pump_stations)
  knobs: up to 10 pump groups x 10 pumps; per-pump on/off trigger elevation; startup transition time; pump efficiency curve
  notes: Documentation page (not yet independently WebFetched in this pass - already covered functionally via the r2dum 2D pump-station page above); listed separately because this is the 1D/storage-area variant, S-effort as knobs-on-existing pump capability.
- [CAND-L] `gate_pump_user_defined_operation_rules` [L] [US] - Can a gate or pump be driven by a rule referencing real-time upstream/downstream stage, time-of-day, or cumulative outflow rather than a fixed trigger elevation, and does the rule engine correctly override or supplement the base geometry operation?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rasum/6.5/advanced-features-for-unsteady-flow-routing/user-defined-rules-for-hydraulic-structures-and-pumps (hecras_rasum_user_defined_rules)
  knobs: rule control variables (local/remote stage, local/remote flow, time-of-day/season, cumulative or averaged history); applies to inline/lateral structures, SA connections, and pumps
  notes: No worked numeric example found on this page (roster_gaps candidate for a concrete rules test case); this is the operations-logic layer needed for reservoir-release or real-time-control scenarios - a new solver-capability (rule scripting), hence L.

### Sediment Transport
Purpose: Route sediment mass balance (Exner equation) alongside hydraulics, either 1D quasi-unsteady (cross-section-centered) or 2D (cell-centered, multi-grain-class, mobile-bed).
Today: not in the roster note - appears unbuilt for TRID3NT today
Aspects: 1D quasi-unsteady mixed-flow-regime sediment continuity; 2D fixed-bed concentration/capacity-only transport (no bed change); 2D mobile-bed multi-grain-class transport with bed change and armoring; transport-function selection appropriateness (regime validity)
- [CAND-M] `quasi_unsteady_mixed_regime_sediment_continuity` [M] [US] - For a headcut/mining-pit edge where flow locally goes super-critical, does enabling the mixed-flow-regime option in quasi-unsteady sediment simulation increase computed erosion relative to a sub-critical-only run, and how far outside each transport function's validated regime does that push results?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/rassed1d/1d-sediment-transport-user-s-manual/simulating-sediment-transport/mixed-flow-quasi-unsteady-sediment-simulation (hecras_rassed1d_mixed_flow_quasi_unsteady)
  knobs: flow regime (sub/super/mixed) for the sediment computational pass; critical-depth method (parabolic vs secant); transport-function selection (regime-of-validity warning)
  notes: Manual documents a real headcut case study qualitatively (erosion increases with mixed-regime), no numeric benchmark published on this page - candidate for a follow-up search of the underlying case study if a numeric target is needed.
- [CAND-M] `2d_fixed_bed_concentration_capacity_routing` [M] [US] - In a sand-bed river reach with flow-control structures, does 'Concentration Only' vs 'Capacity Only' 2D fixed-bed mode correctly quantify how the structures change local transport capacity without moving the bed?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-sediment-transport/fixed-bed-2d-sediment-modeling (hecras_hgt_fixed_bed_2d_sediment)
  knobs: simulation mode (Concentration Only vs Capacity Only); Active Layer sorting method; 4 grain classes (VF/F/C/VC sand); equilibrium-load upstream sediment BC; rising hydrograph 30,000-120,000 cfs
  notes: Real dredged Mississippi River reach; published qualitative comparison (with/without structures capacity change) but no single numeric target quoted in the fetched summary.
- [CAND-L] `2d_mobile_bed_multigrain_morphology` [L] [US] - For a multi-grain-class 2D mobile-bed run, does the solver correctly separate bedload vs suspended-load transport, apply mixing/armoring across the active layer, and produce a converged bed-change (morphology) field over the simulation window?
  src: https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS%202D%20Sediment%20Technical%20Reference%20Manual-v6.4.1.pdf (hecras_2d_sediment_technical_reference_v641)
  knobs: grain-class gradation table; bedload vs suspended-load formula pair; subgrid sediment/morphology option; mixing-layer thickness; boundary sediment supply (equilibrium load vs rating)
  notes: PDF confirmed reachable (valid PDF-1.5, 5MB) but content is scanned/compressed and not machine-text-extractable in this pass - full-bed-change mobile 2D sediment is a genuinely new solver capability (multi-grain morphology), hence L; recommend a follow-up plain-text/HTML confluence read (rassed2d tree) before scoping.

### Water Quality / Temperature
Purpose: Advect/disperse heat and dissolved constituents (nutrients, DO, CBOD, algae, general conservative/reactive tracers) along the already-solved 1D/2D hydraulic flow field.
Today: not in the roster note - appears unbuilt for TRID3NT today
Aspects: water temperature heat-budget simulation; general constituent simulation module (conservative/reactive tracer, GCSM); nutrient simulation module I (algae/N/P/DO/CBOD cycle, NSM I); sediment-diagenesis coupling for dynamic sediment oxygen demand
- [CAND-M] `water_temperature_heat_budget_advection_dispersion` [M] [US] - Given a full heat-energy budget (solar, latent, sensible, evaporative fluxes) and a QUICKEST-ULTIMATE advection-dispersion solve, does simulated water temperature track observed gauge temperature within a documented MAE/RMSE band?
  src: https://www.usgs.gov/publications/development-and-calibration-hec-ras-hydraulic-temperature-and-nutrient-models-mohawk (usgs_mohawk_river_hecras_temp_nutrient)
  knobs: meteorological forcing (solar/latent/sensible flux inputs); QUICKEST-ULTIMATE numerical scheme; calibration period (May-Sep 2016)
  notes: USGS-published 127-mile Mohawk River, NY model; published stats: temperature MAE 0.87-0.90 degC, RMSE 1.00-1.07 degC - a genuine published-first US replication target with numeric acceptance thresholds.
- [CAND-L] `nutrient_simulation_module_I_algae_do_cbod` [L] [US] - Replicating the same Mohawk River NSM I setup, does simulated organic phosphorus / orthophosphate track observations within the published tolerance, and do scenario phosphorus-reduction runs (WWTP effluent changes) reproduce the reported monthly-mean deltas?
  src: https://www.usgs.gov/publications/development-and-calibration-hec-ras-hydraulic-temperature-and-nutrient-models-mohawk (usgs_mohawk_river_hecras_temp_nutrient)
  knobs: NSM I state variables (NO3/NO2/NH4/Org-N, PO4/Org-P, algae, DO, CBOD); optional sediment-diagenesis module toggle; 9 WWTP-effluent reduction scenarios
  notes: Published tolerance: organic P within 0.01 mg/L, orthophosphate within 0.09 mg/L (0.25 at Rome, NY); scenario deltas -0.018 to -0.076 mg/L organic P. This is the flagship US published-first WQ replication case but a new solver capability (NSM I coupling) -> L.
- [CAND-M] `wq_module_smoke_test_suite` [S] [US] - Do HEC's own bundled water-quality test data sets (temperature/GCSM/nutrient toy cases) run to completion and match the manual's stated expected output, as a pre-replication smoke gate before attempting Mohawk-scale calibration? [STOP-RECIPE 2026-08-05 (ADR 0157): the solver image carries ONLY the hydraulic engines (RasGeomPreprocess/RasUnsteady/RasSteady) - there is NO water-quality engine binary, and the bundled WQ test datasets are NOT in the repo. Recipe: obtain the Linux WQ engine + the bundled WQ test datasets, add them to the image (image rebuild + ADR 0148 staleness), wire a WQ solve leg, then run the toy cases to completion vs the manual's expected output. NEW engine + data, not an S-tier knob.]
  src: https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS%20Water%20Quality%20Test%20Data%20Sets-v6.4.1.pdf (hecras_wq_test_data_sets_v641)
  knobs: per-test-case module selection (temperature/GCSM/NSM I); test case boundary/initial conditions as shipped
  notes: PDF confirmed reachable (208KB, valid PDF) but not machine-text-extractable in this pass; recommend opening in an actual PDF reader (small enough) before building the smoke-test harness - S effort since it is knobs-on-shipped-test-cases, not new capability.

### Breach (Dam / Levee Failure)
Purpose: Simulate progressive structural failure of a dam or levee (via SA/2D connection breach parameters) and route the resulting hydrograph through downstream 1D/2D geometry.
Today: levee handling is signed per the roster note; dedicated breach failure simulation (dam or levee) not indicated as signed
Aspects: breach-parameter estimation (regression vs physically-based); breach hydrograph coupling into a downstream 2D floodplain; levee vs dam breach geometry (linear breach growth vs piping/overtopping); equation-set sensitivity (diffusion wave vs full SWE) for breach-driven flood waves
- [CAND-M] `breach_parameter_regression_ensemble` [M] [US] - For a given dam height/reservoir geometry under a PMF overtopping scenario, how much do breach bottom-width, side-slope, and failure-time estimates vary across the five standard regression methods (Froehlich 1995/2008, MacDonald-Langridge, Von Thun-Gillette, Xu-Zhang) versus the physically-based NWS-BREACH model?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/ras1dtechref/latest/performing-a-dam-break-study-with-hec-ras/estimating-dam-breach-parameters/example-application (hecras_ras1dtechref_breach_param_example)
  knobs: regression method selector (5 methods); dam height/volume inputs; PMF vs sunny-day failure mode
  notes: Published table for a 42.9m fictitious earthen dam: bottom width 136.7-249.0 m, side slopes 0.5-1.4H:1V, failure time 1.14-13.92 hr (NWS-BREACH: 238m/0.9/4.2hr) - a concrete ensemble-spread regression target.
- [CAND-M] `dam_breach_reservoir_to_2d_floodplain_coupling` [M] [US] - For a reservoir behind a dam that fails and drains into a downstream 2D floodplain protecting a levee-protected town, does the breach hydrograph (peak, volume, timing) propagate correctly through the SA/2D connection into the mesh, and does peak outflow scale sensibly between the 500ft and 250ft mesh refinement runs?
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/dam-breach-analysis-with-2d-areas (hecras_hgt_dam_breach_2d_areas_bald_eagle)
  knobs: 2D cell size (500ft base / 250ft sensitivity); timestep 20s; equation set (Diffusion Wave vs full SWE); breach progressive-failure timing/dimensions
  notes: Sayers Dam / Bald Eagle Creek, Lock Haven PA; published peak breach outflow ~516,000 cfs total (~435,000 cfs breach component), ~305,000 cfs reaching downstream boundary - reuse of the same dataset as the equation-set candidate above, different aspect (mesh-refinement sensitivity).
- [CAND-M] `simple_breach_geometry_setup` [S] [US] - Can a minimal SA/2D-connection breach (crest elevation, auxiliary spillway, low-level outlet, progressive breach growth) be configured and run to completion as a bring-up smoke test before attempting the full Sayers Dam PMF case? [STOP-RECIPE 2026-08-05 (ADR 0157): the breach-RUNS-TO-COMPLETION bring-up INTENT is ALREADY served by the registered `hecras_levee_breach` template (breach_enabled=True = a GREEN lateral-structure SA/2D breach smoke gate, ADR 0125). But a FRESHLY-AUTHORED breach (dam crest/aux-spillway/low-level-outlet/progressive growth on the cited Sayers geometry) does NOT exist - `set_breach_enabled` only toggles the SHIPPED Muncie breaches, and the composed fresh deck strips all Structures. Recipe: author an SA/2D-connection structure + a Breach Data block (crest 683ft, aux spillway, low-level outlet, progressive growth) onto a fresh deck. Shared residual with the two M-effort breach rows + the QUEUED Bald Eagle case (ADR 0125). NEW structure-authoring, not an S-tier knob.]
  src: https://www.hec.usace.army.mil/confluence/rasdocs/hgt/latest/tutorials/2d-unsteady-flow/dam-breach-simple-2d-geometry (hecras_hgt_dam_breach_simple_2d)
  knobs: dam crest elevation (683ft); auxiliary spillway (500ft wide, 657ft elev); low-level outlet (2ft circular culvert, 590ft invert); breach growth timing
  notes: Explicitly a non-benchmarked, qualitative 'if it runs, it worked' bring-up tutorial (Bald Eagle Creek / Sayer's Dam, PA) - good as a cheap S-effort smoke-test template ahead of the two M-effort quantitative breach candidates above.

Roster gaps (hecras): Two manual/PDF sources (HEC-RAS 2D Sediment Technical Reference v6.4.1 and HEC-RAS Water Quality Test Data Sets v6.4.1) were confirmed reachable as valid PDFs (correct headers/sizes returned over HTTPS from hec.usace.army.mil) but their content is scanned/compressed and not machine-text-extractable via WebFetch's small model in this pass - cited with knobs inferred from adjacent WebSearch snippets and companion confluence pages rather than direct extraction; recommend a direct PDF-reader open before using them to scope build effort. HEC-RAS_2D_Users_Manual_v6.6.pdf (the full 2D manual) exceeded WebFetch's 10MB content-size limit and could not be verified in this pass at all - not cited above; the equivalent content was instead sourced from live confluence (r2dum) HTML pages that did verify. The HEC water-quality-modules landing page (hec.usace.army.mil/software/waterquality/modules.aspx) verified live but is a thin index with no example/knob detail, so the flagship WQ citations instead use the USGS Mohawk River publication, which is a real calibrated US case with numbers. No quantitative published benchmark could be found (in this pass) for: the sediment-transport-function headcut case study beyond a qualitative description, the fixed-bed 2D sediment structures comparison beyond a qualitative description, or the SA/2D pump-station and user-defined-rules pages (manual figures only, no worked numeric example) - flagged individually in each candidate's notes rather than silently assumed.


## OPENQUAKE (10 modules)

### Classical PSHA calculator (hazardlib.calc.hazard_curve + openquake.calculators.classical)
Purpose: Cornell-McGuire probabilistic seismic hazard: integrate over source, magnitude, distance, GMPE uncertainty to produce hazard curves/maps/UHS for a site or grid.
Today: TRID3NT surfaces classical PSHA (per engine ranking doctrine); source-typology and logic-tree variety beyond a single default path is unclear/unknown.
Aspects: source typology (area, point, multipoint, simple fault, complex fault, characteristic fault, nonparametric); single-branch vs non-trivial source-model logic trees; GMPE logic-tree branching per tectonic region type; Latin-hypercube / sampled logic-tree realizations vs full enumeration; hazard-map and Uniform Hazard Spectrum (UHS) extraction at target exceedance probabilities; mean/quantile aggregation across realizations
- [CAND-M] `classical_psha_source_typology_sweep` [M] [US] - Given a fault/area/point source geometry the user draws or fetches, which typology (area/point/multipoint/simple-fault/complex-fault/characteristic-fault/nonparametric) should the deck use and how do hazard curves differ?
  src: https://github.com/gem/oq-engine/tree/master/demos/hazard (oq-demos-hazard-typology-set)
  knobs: source_model.xml geometry+typology, area_source_discretization, width_of_mfd_bin
- [FOLDED] `classical_psha_nontrivial_logic_tree` [S] [US] (2026-08-05: folded into `openquake_psha` knob `logic_tree="source_models"` -- two competing weighted source-model interpretations + 2 GMPEs on active shallow crust (GEM LogicTreeCase1 mechanism) -> mean hazard curve + 5/50/95 quantile-spread line chart; epistemic modes bypass the real-fault fetch and use a synthetic AOI demo source. Live: SF Bay AOI, 4 realizations, 11s on oq 3.25.1 through run_oq.py; at PGA~0.20g q05/mean/q95 PoE = 0.179/0.284/0.382. Proof docs/proof/templates/openquake_psha_logic_tree_source_models_chart.png) - Given competing source models and multiple GMPEs per tectonic region, what is the mean hazard curve and 5/50/95 quantile spread across all logic-tree realizations?
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/LogicTreeCase1ClassicalPSHA/README.txt (oq-demo-logictreecase1)
  knobs: source_model_logic_tree.xml branch weights, gsim_logic_tree.xml, number_of_logic_tree_samples
- [FOLDED] `classical_psha_gr_uncertainty_logic_tree` [S] [US-applicable] (2026-08-05: folded into `openquake_psha` knob `logic_tree="gr_uncertainty"` -- abGRAbsolute + maxMagGRAbsolute 3-way branches on a two-source model (active shallow + stable continental) x 2 GMPEs per TRT = 324 realizations (the published LogicTreeCase2 count), over the caller AOI -> mean + 5/50/95 quantile-spread chart. Live: SF Bay AOI, 324 realizations, 11s on oq 3.25.1 through run_oq.py; at PGA~0.20g q05/mean/q95 PoE = 0.203/0.525/0.792 (wider band than the competing-source case, as expected for a/b+Mmax uncertainty). Proof docs/proof/templates/openquake_psha_logic_tree_gr_uncertainty_chart.png) - How sensitive is the hazard curve to Gutenberg-Richter a/b-value and Mmax epistemic uncertainty (LogicTreeCase2, 324 realizations)?
  src: https://github.com/gem/oq-engine/tree/master/demos/hazard/LogicTreeCase2ClassicalPSHA (oq-demo-logictreecase2)
  knobs: abGRAbsolute/bGRRelative/maxMagGRAbsolute uncertaintyModel branches
- [FOLDED] `classical_psha_uhs_and_hazard_map` [S] [US] (2026-08-05: folded into `openquake_psha` knobs `uniform_hazard_spectra=True` + `secondary_poe=0.02` -- the classical deck already exported the 475y map + hazard curve; the fold adds the UHS export (SA-period ladder + hazard_uhs-mean) + the second-PoE map so BOTH 10%/475y and 2%/2475y export together, plus a UHS SA-vs-period chart. Live: SF Bay AOI, oq 3.25.1 through run_oq.py; 475y map max 0.301g, 2475y map max 0.475g. Proof docs/proof/templates/openquake_psha_uhs_multipoe_chart.png) - For a US site, produce a Uniform Hazard Spectrum and hazard map at 10%/2% probability of exceedance in 50 years alongside the curve.
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/AreaSourceClassicalPSHA/README.txt (oq-demo-areasource-uhs)
  knobs: poes, intensity_measure_types_and_levels (PGV/PGA/SA set), site count (2112 in demo)
- [CAND-L] `classical_psha_us_nshm_source_model` [L] [US] - Run classical PSHA against the actual GEM-translated USGS 2018/2021 US National Seismic Hazard Model source model (conterminous US, incl. UCERF3 California) instead of a toy demo source.
  src: https://hazard.openquake.org/gem/models/USA/ (gem-mosaic-usa-model)
  knobs: license-gated model download (v2021.1.0), full CEUS/WUS GMPE logic tree bundled with model

### Hazard disaggregation calculator (openquake.calculators.disaggregation)
Purpose: Decompose the hazard at a given exceedance probability into contributions by magnitude, distance, epsilon, longitude/latitude, and tectonic region type - answers 'which scenarios drive this hazard level'.
Today: unknown / not confirmed surfaced (roster note: classical PSHA + scenario/logic-tree signed only).
Aspects: multi-dimensional disaggregation matrix construction (mag/dist/eps/lon-lat/TRT); sub-matrix extraction/2D marginal views; single-site vs small-set-of-sites scope constraint; cross tectonic-region-type disaggregation when multiple TRTs contribute
- [CAND-M] `seismic_hazard_disaggregation_by_scenario` [M] [US] - At a given site, what magnitude-distance-epsilon-TRT combination dominates the hazard at 10% probability of exceedance in 50 years?
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/Disaggregation/README.txt (oq-demo-disaggregation)
  knobs: poes_disagg, mag_bin_width, distance_bin_width, coordinate_bin_width, num_epsilon_bins, disagg_outputs selection

### Event-based PSHA / stochastic event set calculator (openquake.calculators.event_based)
Purpose: Generate stochastic event sets (synthetic earthquake catalogs) and full ground-motion fields per rupture; hazard curves can be back-derived and are the substrate for event-based risk.
Today: roster note lists 'scenario + logic-tree signed' but event-based specifically is unconfirmed; likely a gap.
Aspects: stochastic event set generation with reproducible random_seed; ground motion field (GMF) computation per rupture across many sites; hazard-curve equivalence check against classical PSHA (cross-validation); hazard-map extraction from event-based results; nodal-plane / hypocentral-depth uncertainty propagation into rupture sampling
- [CAND-M] `stochastic_event_set_ground_motion_fields` [M] [US] - Generate N stochastic event sets and per-rupture ground motion fields for a site, and confirm the event-based hazard curve matches the classical PSHA curve within tolerance.
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/EventBasedPSHA/README.txt (oq-demo-eventbasedpsha)
  knobs: ses_per_logic_tree_path, random_seed, investigation_time, area_source_discretization, nodal-plane distribution

### Scenario hazard calculator (openquake.calculators.scenario)
Purpose: Deterministic ground-motion-field simulation for a single specified rupture - the base for scenario damage/loss and for feeding secondary-peril models with a known event.
Today: roster note: scenario is signed/working today.
Aspects: single specified rupture geometry -> GMF ensemble (Monte Carlo realizations of spatial correlation); GMF-to-secondary-peril handoff (feeds liquefaction/landslide directly); spectral/period selection for the scenario
- [LANDED] `scenario_rupture_gmf_realization_set` (ADR 0164: openquake_scenario_gmf - Hayward-trace live 0.59 g, JB2009-correlated realizations) [S] [US] (2026-08-05 triage: NOT folded - this is `calculation_mode = scenario`, a distinct calculator from the classical deck openquake_psha renders, so it warrants its own tool, not a knob. The bundled ScenarioCase1 demo runs verbatim on oq 3.25.1 (produces gmf-data_*.csv (event_id, gmv_PGA per site) + avg_gmf_*.csv (custom_site_id, lon, lat, gmv_PGA, gsd_PGA = the across-realization spread) + sitemesh/events CSVs). RECIPE for the follow-on tool: (1) new job_ini branch calculation_mode=scenario with region+region_grid_spacing (site grid), rupture_model_file, intensity_measure_types (no levels), gsim (single, no logic tree), ground_motion_correlation_model=JB2009, number_of_ground_motion_fields>=10, truncation_level; (2) render a simpleFaultRupture rupture_model.xml (magnitude/rake/hypocenter/simpleFaultGeometry) - reuse the fetch_fault_sources trace or a synthetic single plane; (3) postprocess avg_gmf_*.csv -> rasterize gmv_PGA (mean field) to a COG + a GMF realization-spread chart from gmv/gsd; (4) new scenario contract + tool + corpus + categories. Est: 1 focused pass, ~8s live per run.) - For a specified single-plane rupture, generate 10+ Monte Carlo ground-motion-field realizations across a site grid for downstream loss/secondary-peril use.
  src: https://github.com/gem/oq-engine/tree/master/demos/hazard/ScenarioCase1 (oq-demo-scenariocase1)
  knobs: number_of_ground_motion_fields, truncation_level, correlation_model (JB2009), rupture_model.xml geometry

### GMPE table / custom ground-motion model calculator (hazardlib.gsim.gmpe_table + GMPETablePSHA demo)
Purpose: Use a tabulated (HDF5) ground-motion model instead of an analytical GMPE equation - needed for regions/IMTs where GEM/USGS distribute numerical attenuation tables (e.g. subduction, CEUS).
Today: unknown; not confirmed surfaced.
Aspects: HDF5 GMPE-table ingestion as a GSIM logic-tree branch; long-period SA extrapolation via table interpolation; swap-in for tectonic regions lacking closed-form GMPEs
- [CAND-M] `tabulated_gmpe_hazard_curve` [M] [US] - Compute a classical hazard curve at long spectral period (e.g. SA(10.0)) using a tabulated GMPE (gmpe.hdf5) instead of an analytical model.
  src: https://github.com/gem/oq-engine/tree/master/demos/hazard/GMPETablePSHA (oq-demo-gmpetable)
  knobs: gsim_logic_tree.xml GMPETable uncertaintyModel path, intensity_measure_types_and_levels logscale range, pointsource_distance

### Median spectrum / multi-period UHS calculator (MedianSpectrum demo, part of Advanced Calculations)
Purpose: Compute the median response spectrum (not just mean/quantile hazard) across a dense IMT set for engineering design-spectrum use cases.
Today: unknown; not confirmed surfaced.
Aspects: dense period sampling (SA(0.05)...SA(10.0)); region-specific GMM logic tree (e.g. NGA-East / CanadaSHM6 style) applied across the full period range
- [CAND-M] `median_response_spectrum_multi_period` [M] [non-US] - For a city-scale grid, compute the median response spectrum across SA(0.05)-SA(10.0) alongside hazard curves and UHS.
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/MedianSpectrum/README.txt (oq-demo-medianspectrum)
  knobs: IMT list breadth, GMM logic tree file, poes for UHS extraction

### Secondary perils - liquefaction (openquake.sep, oq-mbtk sep module)
Purpose: Given a ground-motion field (scenario or event-based), estimate probability/occurrence class of liquefaction from geotechnical/geospatial site covariates.
Today: not surfaced (risk/secondary-peril territory outside current classical/scenario/logic-tree scope).
Aspects: HAZUS liquefaction-susceptibility-class probability model; Zhu et al. 2015 logistic model (PGA, Vs30, CTI, groundwater depth); Zhu et al. 2017 coastal/general PGV-based models with spatial-extent estimate; Bozzoni 2021 / Rashidian-Baise 2020 / Allstadt 2022 regional recalibrations; Todorovic et al. 2022 non-parametric model; event-based (many ruptures) vs scenario (single rupture) liquefaction workflow
- [CAND-L] `scenario_liquefaction_probability_map` [L] [US] - Given one rupture and a site model with Vs30/precipitation/water-table-depth/distance-to-water, map liquefaction occurrence class and probability across the domain.
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/ScenarioLiquefaction/README.txt (oq-demo-scenarioliquefaction)
  knobs: liquefaction_sites.csv site params, model selection (HAZUS/Zhu2015/Zhu2017/Bozzoni/Todorovic), probability threshold
- [CAND-L] `event_based_liquefaction_occurrence_set` [L] [US] - Across a stochastic event set (50 realizations), what is the aggregate liquefaction occurrence/probability field over 4000+ sites?
  src: https://raw.githubusercontent.com/gem/oq-engine/master/demos/hazard/EventBasedLiquefaction/README.txt (oq-demo-eventbasedliquefaction)
  knobs: ses_per_logic_tree_path, gmmLT.xml weighted GMPE mix, IMT=PGV

### Secondary perils - landslide (openquake.sep, oq-mbtk sep module)
Purpose: Newmark-type permanent-displacement and probability-of-failure estimation for slope instability triggered by earthquake shaking.
Today: not surfaced.
Aspects: Jibson (2007) / Jibson et al. (2000) coseismic-displacement regressions (PGA, Arias intensity, critical acceleration); Saygili & Rathje (2008) / Rathje & Saygili (2009) PGA+PGV combined displacement models; Cho & Rathje (2022) slope-period-aware model; Nowicki Jessee et al. (2018) / Allstadt et al. (2022) geospatial probability-of-landslide models (lithology, landcover, CTI, slope); Fotopoulou & Pitilakis (2015) 4-variant model family
- [CAND-L] `newmark_displacement_landslide_hazard` [L] [US] - Given a scenario rupture's PGA/PGV field plus DEM-derived slope and critical acceleration, estimate coseismic Newmark displacement and classify landslide hazard.
  src: https://docs.openquake.org/oq-engine/master/manual/underlying-science/secondary-perils.html (oq-manual-secondary-perils)
  knobs: model choice (Jibson/Saygili-Rathje/Cho-Rathje), critical_accel input raster, slope/lithology/landcover covariates
- [CAND-L] `probabilistic_landslide_susceptibility_map` [L] [US] - Produce a geospatial probability-of-landslide map (Nowicki Jessee / Allstadt) from a ground-motion field plus DEM slope, CTI, lithology and landcover layers.
  src: https://gemsciencetools.github.io/oq-mbtk/contents/sep_docs/sep_models.html (oq-mbtk-sep-models)
  knobs: lithology/landcover coefficient tables, PGV/CTI caps per model variant, minimum-slope exclusion threshold

### Site response / site amplification model (site-model-inputs, Vs30-based GMPE amplification, amplification function tables)
Purpose: Modify bedrock ground motion for local soil/site conditions via Vs30-dependent GMPE terms or explicit amplification-function tables, before it reaches the hazard or secondary-peril calculators.
Today: unknown / partial - reference_depth_to_2pt5km_per_sec and reference_vs30_value knobs exist in demo job.ini files but no confirmed TRID3NT-surfaced site-model builder.
Aspects: per-site Vs30 (measured vs inferred) driving built-in GMPE site terms; basin-depth terms (z1pt0/z2pt5) for GMPEs that support them; explicit AmplificationFunction table (ampcode/ec8/amplfactor) as a discrete site-class multiplier, decoupled from Vs30-continuous GMPEs; site model CSV construction from a raster/DEM (Vs30 proxy) fetch
- [CAND-M] `site_model_vs30_amplification_build` [M] [US] - Build a per-site Vs30 (and basin-depth where applicable) site model CSV from a fetched raster for an AOI, and confirm it changes hazard-curve amplitude vs the uniform reference_vs30 default.
  src: https://docs.openquake.org/oq-engine/3.23/manual/user-guide/inputs/site-model-inputs.html (oq-manual-site-model-inputs)
  knobs: site_model.csv columns (vs30, vs30measured, z1pt0, z2pt5), reference_vs30_value fallback
- [CAND-M] `discrete_amplification_function_apply` [M] [US] - Apply a discrete site-class amplification-function table (ampcode/EC8 class -> amplfactor) instead of a continuous Vs30 GMPE term, for a region with categorical soil-class mapping.
  src: https://docs.openquake.org/oq-engine/3.23/manual/user-guide/inputs/site-model-inputs.html (oq-manual-amplification-function)
  knobs: amplification.csv ampcode/amplfactor table, soil_intensities discretization

### US National Seismic Hazard Model source model (GEM-translated USGS NSHM mosaic entry)
Purpose: Published-first, US-specific hazard input surface: the actual USGS-authored conterminous-US (2018) + Alaska (2007) + Hawaii (1998) source models and logic trees, translated into OQ format by GEM, as an alternative to synthetic demo sources.
Today: not surfaced; license-gated GEM download, unconfirmed whether TRID3NT has obtained it.
Aspects: conterminous US model (incl. UCERF3 time-independent California branch); Alaska model (2007 USGS, incl. Aleutians); Hawaii model (1998 USGS); CEUS vs WUS GMPE logic-tree split baked into the model
- [CAND-L] `conterminous_us_nshm_classical_run` [L] [US] - Run classical PSHA end-to-end against the GEM-translated conterminous-US 2018 NSHM source model + its native GMPE logic tree for an arbitrary US site.
  src: https://hazard.openquake.org/gem/pdf/usa-report.pdf (gem-usa-report-pdf)
  knobs: model version (v2021.1.0), license-request access, full source_model_logic_tree.xml as shipped


## GEOCLAW (8 modules)

### SWE + AMR core
Purpose: Depth-averaged shallow-water equations solved on adaptively refined Cartesian AMR grids over bathymetry/topography, with wetting/drying Riemann solvers - the GeoClaw core solver used by every other module.
Today: TRID3NT surfaces SWE+AMR core per the roster note. LANDED (ADR 0143): explicit region-based AMR control (`geoclaw_amr_refinement_regions`) and spatially-varying Manning friction (`geoclaw_regional_manning_friction`) are now exposed knobs on the inundation deck. Adjoint-guided AMR remains a separate-solver gap. LANDED (ADR 0150): the AMR mesh is now a first-class per-run PRODUCT - every GeoClaw inundation template (amr_regions / regional_manning / gauge_timeseries) emits `mesh.geojson`, the RAW cell-edge grid lines of the peak-relevant fort.q frame's AMR patches (all levels, one vector layer, style_preset mesh_grid, crs EPSG:4326), so refinement is visible as grid DENSITY beside the depth COG. Honest decimation on megabyte-scale finest patches (per-feature `decimated` flag + policy in the FC metadata).
Aspects: AMR refinement control: explicit region-based flagging vs default flag2refine vs Richardson error estimation; Adjoint-guided AMR flagging targeted at a specific quantity of interest (e.g. a gauge); Manning bottom-friction, spatially-varying by region/topography; Wetting/drying shoreline Riemann solver and mass/momentum conservation; Analytic-solution validation (Thacker exact solutions: radial parabolic-bowl / sloshing)
- [LANDED] `region_based_amr_refinement_windows` [S] [US] - How do I control AMR refinement level and duration with explicit lat/lon/time regions instead of relying on default error flagging? [RESOLVED 2026-08-05 (ADR 0144): root cause = two pre-existing bugs. (1) rasterize_frame_to_grid preserved a coarse AMR cell's WET value under a finer patch that read DRY -> a single coarse cell smeared into a flat rectangle that survived the land mask. Fix: finest-available AMR level wins per area UNCONDITIONALLY (finer DRY erases coarser wet) + overland mask requires topo > sea_level (<= datum = water). (2) plan_geoclaw_grid was called with SWAPPED (aoi, domain) args, sizing the cell budget off the huge domain and capping amr_levels at 2 -> the AOI never refined past L2, so the field read near-uniform and the AMR window was inert. Fix: correct arg order -> L1-L4 nest forms, published field went from 2 to 80+ unique depths. ADR 0123 depth layer contaminated by (1) - same postprocess; fixed by the same change. Note: the explicit AMR window is SUBSUMED by the AOI-default region (both reach L4 at this AOI size) - a template-design limitation, not a solve error. Re-smoked Crescent City live; proofs refreshed.] [RESOLVED 2026-08-05 (ADR 0148): window subsumption ROOT CAUSE = (a) the deployed geoclaw image was STALE (its baked setrun_builder predated ADR 0143 - parse_build_spec never read amr_regions, so explicit windows were SILENTLY DROPPED from the deck), and (b) even once threaded, plan_geoclaw_grid OVERRODE amr_levels to the cost-bounded finest (L4 here), so a user window at that same level was subsumed by the AOI default region. Fix: (1) rebuild the image with the landed worker; (2) when explicit windows are present the windows GOVERN refinement - the deck's finest follows the finest window (honoring the user's level, bounded to +1 over the plan's whole-AOI ceiling) and the AOI ambient region is pinned ONE LEVEL BELOW it, so the window is demonstrably finer than its surroundings; (3) fgmax monitors at the AOI ambient level so the whole AOI depth field is still captured. Live re-smoke Crescent City (amr_levels=4, L4 window): inside the window ALL cells reach L4 (3600 cells), outside the window (+nesting buffer) capped at L3 (0 L4 leakage) - a crisp, budget-safe contrast. The windows now also ride the ADR 0107 input-review gate (basis=prompt_interpreted for a model-derived box, user for a drawn geometry) so the LLM cannot silently invent window placement; the plugin draw-a-rectangle supply path is the follow-on. Proofs refreshed (raster/mesh split - the new standing norm, separate toggleable-layer files never a composite): amr_regions.png (mid-run eta raster ONLY), amr_regions_mesh.png (AMR patch structure ALONE - black L4 + light-grey L3 + window), amr_regions_depth.png (peak-depth product), amr_regions_chart.png (inside-vs-outside level contrast).] [MESH PRODUCT 2026-08-05 (ADR 0150): the AMR mesh is now EMITTED as a per-run layer (`mesh.geojson`) by the shared agent postprocess, not just a proof artifact - the RAW cell-edge grid lines of the peak-relevant fort.q frame (all levels, one black vector layer). amr_regions_mesh.png regenerated as the RAW UNIFIED MESH (Clawpack-gallery style: actual grid lines, ONE colour, density IS the refinement - no per-level colour coding); amr_regions.png kept as the approved eta (sea-surface anomaly) snapshot re-rendered from the SAME run with PINNED presentation (fixed frame t=900 s + fixed symmetric +-0.5 m scale, both stated in the caption - the determinism rule for any frame-snapshot proof: never auto-select, so a re-smoke stays comparable); amr_regions_depth.png re-rendered as the peak-depth product from the same run. Live Crescent City re-smoke (amr_levels=4, L4 window): 75 AMR patches (L1:1 L2:4 L3:64 L4:6), 4786 grid lines, 9572 vertices, 0.27 MB, CRS EPSG:4326, ALL finest (L4) grid vertices over the user window (frac 1.0). No worker rebuild (agent-side postprocess from the downloaded fort.q; container code unchanged).]
  src: https://github.com/clawpack/geoclaw/blob/master/examples/tsunami/chile2010/setrun.py (geoclaw_chile2010_setrun)
  knobs: amrdata.regions=[[level,level,t0,t1,x0,x1,y0,y1],...], refinement_ratios_x/y, flag2refine=True, flag_richardson=False
  LANDED as `geoclaw_amr_refinement_regions` (ADR 0143): user-supplied AmrRegionWindow list appended to setrun regiondata AFTER the engine default tiers (GeoClaw combines by MAX of covering levels); flag_richardson stays False + flag2refine True (region-based over error flagging). Live smoke Crescent City CA: level-3 window over the harbour, real solve (initial water mass ~6.5e10 = genuine ocean, not dry ~1e5), max_depth 0.73 m. proof docs/proof/templates/amr_regions.png + _chart.png.
- [CAND-L] `adjoint_guided_amr_flagging` [L] [US] - How do I flag AMR refinement using an adjoint (backward-in-time) sensitivity solve targeted at a specific gauge/QoI, instead of uniform/error-based refinement?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/tsunami/chile2010_adjoint (geoclaw_chile2010_adjoint)
  knobs: adjointdata.use_adjoint, adjoint output time window, refinement tolerance (Davis & LeVeque method)
  notes: New solver capability - requires a separate backward adjoint run coupled to the forward run, not just a setrun toggle.
- [LANDED] `manning_friction_by_region` [S] [US] - How do I set a spatially-varying Manning's n bottom-friction coefficient (e.g. different value onshore vs offshore) instead of a single global n? [RESOLVED 2026-08-05 (ADR 0144): depth-COG uniform-rectangle root cause fixed at the source (finest-AMR-wins-unconditionally rasterize + topo>sea_level overland mask; plus the plan_geoclaw_grid swapped-arg fix that had capped amr_levels at 2). Published field now coast-following with 80+ unique depths (no rectangle). OPEN follow-up (separate deck defect, NOT this postprocess): the spatially-varying (list) Manning form is inert - banded [0.015,0.06] vs scalar 0.025 gives BYTE-IDENTICAL output while scalar friction is verified sensitive (control n=0.001 vs 0.5 shifts peak depth 0.59->0.22 m); the manning_coefficient list / manning_break is not activated in the GeoClaw deck. Re-smoked live; proof chart states this numerically.] [RESOLVED 2026-08-05 (ADR 0148): "knob inert" ROOT CAUSE was NOT a deck-writer bug - the setrun_builder already emitted geo_data.manning_coefficient=[..] + manning_break=[..] correctly, and clawpack 5.14.0 consumes it (GeoClawData.write authors num_manning + the two lists; geoclaw_module pads manning_break to +inf at the top band; src2.f90 selects the per-cell coefficient by topography B against the breaks). The defect was a STALE DEPLOYED IMAGE: the baked trid3nt-local/geoclaw:latest predated ADR 0143, so its parse_build_spec had NO manning_coefficients field and SILENTLY DROPPED the banded list, authoring only the scalar manning_n -> banded and scalar decks were identical -> byte-identical output. Fix: rebuild the image with the landed worker. Live re-smoke on the SAME Crescent City deck (amr_levels=2): banded [0.015 offshore, 0.06 onshore] vs scalar 0.025 now DIFFERS measurably - gauge max|eta diff| 0.0587 m at t=764 s, peak-field max|diff| 0.0521 m over 133 common wet cells, and banded floods LESS (0.0739 vs 0.0766 km2 - the higher onshore n=0.06 damps overland reach, the physically-correct direction); the scalar path stays byte-identical to before. Proof chart now shows the two waveforms visibly diverging with the numeric max|diff| stated.]
  src: https://www.clawpack.org/manning.html (geoclaw_manning_doc)
  knobs: rundata.friction_data.manning_coefficient (list), manning_break (topography breakpoints); default n=0.025
  LANDED as `geoclaw_regional_manning_friction` (ADR 0143): geo_data.manning_coefficient (list) + geo_data.manning_break (ascending elevation breaks, len = N-1). Verified against clawpack 5.14.0 (manning_coefficient already a list, manning_break present). Live smoke Crescent City CA: [0.015 offshore B<0, 0.06 onshore B>=0] break 0 m, real solve (initial mass ~6.5e10), max_depth 0.76 m. proof docs/proof/templates/regional_manning.png + _chart.png.
- [CAND-M] `thacker_analytic_swe_validation` [M] [non-US] - How do I validate the SWE+AMR wetting-drying solver's mass/momentum conservation against Thacker's exact analytic solution (radially symmetric parabolic-bowl sloshing), checking gauges on the x-axis vs diagonal for symmetry?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/tsunami/bowl-radial (geoclaw_bowl_radial)
  knobs: grid resolution, AMR restricted to one quadrant, gauge placement (x-axis vs diagonal) as a symmetry check against the published analytic solution
  notes: Synthetic idealized bowl, not a US site - this is a solver V&V/calibration case per the model-fidelity doctrine, not a hazard demo.

### dtopo sources (Okada / dtopo tools)
Purpose: Generate time-dependent seafloor-deformation (dtopo) files from earthquake fault-slip parameters via the Okada elastic half-space model (or kinematic rupture), to seed tsunami generation in GeoClaw.
Today: Unknown/likely absent - not listed in the roster note (TRID3NT today = SWE+AMR+fgmax+gauges); this is a capability gap for earthquake-sourced tsunami scenarios.
Aspects: Static Okada single/multi-subfault deformation from fault geometry + slip; Kinematic rupture with per-subfault rupture_time/rise_time (time-dependent, not instantaneous); Triangular subfault geometry (v5.5.0+) as an alternative to rectangular; Importing published finite-fault slip models (CSVFault/SiftFault/UCSBFault/SegmentedPlaneFault) rather than hand-specifying geometry; dtopo file format (type 1 vs recommended type 3) and time-sampling
- [CAND-M] `okada_single_subfault_dtopo` [M] [US] - How do I generate a dtopo seafloor-deformation file from a single rectangular fault (strike/dip/rake/slip/depth) using the Okada model?
  src: https://www.clawpack.org/gallery/_static/apps/notebooks/geoclaw/Okada.html (geoclaw_okada_notebook)
  knobs: fault.subfaults[i].{strike,dip,rake,slip,length,width,depth,coordinate_specification}, dtopo dx, times
- [CAND-M] `multi_subfault_dtopo_from_finite_fault_model` [M] [US] - How do I build a dtopo file from a published multi-subfault finite-fault slip model (NOAA SIFT-format subfault CSV) instead of a single uniform-slip fault?
  src: https://www.clawpack.org/gallery/_static/apps/notebooks/geoclaw/dtopotools_examples.html (geoclaw_dtopotools_examples)
  knobs: dtopotools.CSVFault/SiftFault, per-subfault slip/rupture_time/rise_time, grid resolution
  notes: Notebook's worked example is the 1964 Alaska (Prince William Sound) earthquake, 12 subfaults from the NOAA SIFT database - a real, published US case satisfying the US-only + paper-first doctrine directly.
- [CAND-M] `kinematic_rupture_dtopo` [M] [US] - How do I produce a time-dependent (kinematic, not instantaneous) dtopo deformation by assigning rupture_time and rise_time to each subfault?
  src: https://www.clawpack.org/dtopo.html (geoclaw_dtopo_doc)
  knobs: subfault.rupture_time, subfault.rise_time, dtopo file type 3 (mt time steps, t0/dt)
- [CAND-M] `okada_1d_dtopo_smoke_case` [M] [non-US] - How do I run a minimal 1D Okada-generated-dtopo tsunami case with a fast, checkable output as a build/regression smoke test? [RETAG CAND-S -> CAND-M 2026-08-05 (ADR 0155, executing the ADR 0143 STOP verdict): the TRID3NT GeoClaw worker is 2D-ONLY (Makefile includes geoclaw/src/2d/shallow/Makefile.geoclaw; setrun authors num_dim=2; entrypoint stages a 2D topo/dtopo). A 1D run needs its OWN deck path, so this is NOT the S-tier one-knob fold its old tag implied. Recipe to land: add a `dim=1` build_spec branch in setrun_builder (render_setrun_1d authoring a num_dim=1 grid/gauge/qinit + render_makefile_1d building EXE from the (present-in-image) $CLAW/geoclaw/src/1d_classic modules + rp1 solver), stage a 1D topo + 1D dtopo, and add a 2D-vs-1D entrypoint switch; then a synthetic idealized 1D Okada smoke. Disproportionate machinery for a smoke test; deferred until a genuine 1D question class appears.]
  src: https://github.com/clawpack/geoclaw/tree/master/examples/1d_classic/okada_dtopo (geoclaw_1d_okada_dtopo)
  knobs: 1D fault params, mx grid resolution
  notes: Generic 1D regression case, not tied to a real site - useful only as a fast solver-build smoke test.
  STOP (ADR 0143, triage): the TRID3NT GeoClaw worker is 2D-only - the Makefile includes geoclaw/src/2d/shallow/Makefile.geoclaw, the setrun authors num_dim=2, the entrypoint stages a 2D topo/dtopo. The 1d_classic Fortran IS present in the image source tree (/opt/clawpack-src/geoclaw/src/1d_classic) but a 1D run needs its OWN deck path: a distinct Makefile (EXE from the 1d_classic modules + rp1 solver), a num_dim=1 setrun (1D grid/gauge/qinit), a 1D topo + 1D dtopo, and an entrypoint branch. Recipe to land: add a `dim=1` build_spec branch in setrun_builder (render_setrun_1d + render_makefile_1d pointing at 1d_classic) + a 2D-vs-1D entrypoint switch; then a synthetic idealized 1D okada smoke. Disproportionate machinery for a smoke test; deferred until a genuine 1D question class appears.

### fgmax (fixed grid maximum monitoring)
Purpose: Interpolates from the moving AMR grids onto a user-specified fixed grid/point-set to record maximum depth/speed/momentum-flux and first-arrival time over the full run.
Today: TRID3NT surfaces fgmax per the roster note; exact aspect coverage (point-style options, DEM-masking) unconfirmed.
Aspects: Point-style selection (arbitrary points / 1D transect / 2D Cartesian grid / logically-rectangular quad / DEM-masked); Quantity selection (depth, speed, momentum, momentum-flux, min-depth, arrival-time); Time-window + check-frequency + min-AMR-level gating; Combined use with fgout for max-envelope + animation in one case
- [LANDED] `fgmax_island_runup_maxima` [S] [non-US] - How do I set up an fgmax grid over an island/shelf domain to record max wave height and first-arrival time for runup analysis? [FLIP CAND-S -> LANDED 2026-08-05 (ADR 0155, executing the ADR 0143 COVERED verdict): the max-wave-height + first-arrival-time MECHANISM is ALREADY live on the shared inundation/gauge/AMR/Manning deck. setrun_builder emits an fgmax grid (point_style=2, num_fgmax_val=2, arrival_tol) for every tsunami/surge run, and postprocess.read_fgmax_output surfaces max_depth_m / max_inundation_m / arrival_time_s onto GeoClawDepthLayerURI (the landed AMR/Manning/gauge smokes all wrote _output/fgmax0001.txt with real data). The board's [non-US] radial-ocean-island fixture is a mechanism reference; the SAME point-style/arrival machinery runs on our real US coastal AOIs (Crescent City). The onshore-restricted variant of this grid is the sibling fgmax_dem_masked_grid, LANDED alongside this flip (ADR 0155) as the fgmax_mask='onshore' knob.]
  src: https://github.com/clawpack/geoclaw/tree/master/examples/tsunami/radial-ocean-island-fgmax (geoclaw_radial_ocean_island_fgmax)
  knobs: fgmax_tools.FGmaxGrid point_style, tstart_max/tend_max, dt_check, arrival_tol
  notes: Synthetic radial ocean+island domain; the point-style/arrival-time pattern transfers directly to any real US coastal AOI.
  COVERED (ADR 0143, triage): the max-wave-height + first-arrival-time MECHANISM is ALREADY live - the inundation/gauge/AMR/Manning deck emits an fgmax grid (point_style=2, num_fgmax_val=2, arrival_tol) and postprocess.read_fgmax_output surfaces max_depth_m / max_inundation_m / arrival_time_s on every tsunami+surge run. The landed AMR/Manning smokes both wrote _output/fgmax0001.txt with real data. What is NOT yet a first-class knob: user-settable tstart_max/tend_max/dt_check as template args (currently deck-derived). Small follow-up: expose those on a dedicated fgmax template if a distinct question class is requested.
- [LANDED] `fgmax_dem_masked_grid` [S] [US] - How do I restrict an fgmax grid to only the wet/onshore cells of a real DEM (point_style=4) rather than a full rectangular grid, to cut output size for a real coastal AOI? [LANDED 2026-08-05 (ADR 0155): the `fgmax_mask='onshore'|'full'` knob on the GeoClaw surface (geoclaw_inundation), defaulting to `'full'` (byte-identical to the prior point_style=2 grid; test-locked). On `'onshore'` the setrun emits `fg.point_style=4` + `fg.xy_fname='fgmax_mask.tt3'`, and a NEW entrypoint step generates that mask: a topotype-3 file over the AOI at the SAME geometry as the full fgmax grid (shared `setrun_builder.fgmax_grid_geom`), Z=1 on cells whose DEM topography is above `sea_level_m` (onshore) and Z=0 elsewhere -- a strict subset of the full grid, so common-cell maxima match. An empty (all-offshore) mask raises the typed `GEOCLAW_FGMAX_MASK_FAILED` gate rather than a silent empty fgmax. Live docker smoke Crescent City (rebuilt image), tsunami Mw9 amr_levels=3: fgmax points full=21460 vs onshore=13080 (39.0% fewer output points -- the output-size win), and the max ON-LAND depth agrees exactly (onshore max_land_h=1.41 m == full max_land_h=1.41 m; the onshore points are a strict land subset of the full grid). Real solve (max_depth 1.326 m). No new tool (knob fold): registry + EXPECTED_TEMPLATES unchanged.]
  src: https://www.clawpack.org/fgmax.html (geoclaw_fgmax_doc)
  knobs: point_style=4 (DEM-based masking), min_level_check, interp_method=0
  DEFER (ADR 0143, triage): point_style=4 IS supported in clawpack 5.14.0 (FGmaxGrid.read reads xy_fname = a mask stored as a topo_type-3 file: "points are a subset of a uniform grid, as specified by a mask array"). Landable but needs NEW entrypoint machinery: derive a wet/onshore mask from the staged DEM, write it as a topo_type-3 file, and reference it via fg.xy_fname + fg.write_xy_fname. Recipe: add a `fgmax_point_style` build_spec knob (2|4) + an entrypoint mask-generation step (rasterio: onshore-or-near-shore cells from the topo band -> topotype-3 mask), then a US-AOI smoke comparing output-file cell count 4-vs-2. Deferred behind the already-live point_style=2 coverage (row above).
- [CAND-M] `fgmax_plus_fgout_combined_run` [M] [non-US] - How do I run fgmax (max-value envelope + arrival time) and fgout (uniform-grid animation frames every N minutes) together in one case and post-process both?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/tsunami/chile2010_fgmax-fgout (geoclaw_chile2010_fgmax_fgout)
  knobs: combined fgmax_tools + fgout_tools setup on a shared AOI, fgout output every 15 min
  notes: Chile 2010 case; the combined fgmax+fgout recipe is directly reusable on a US AOI.

### fgout (fixed grid output)
Purpose: Interpolates full solution snapshots onto a uniform grid at arbitrary, AMR-patch-independent output times, for animation and post-hoc spatial/particle analysis.
Today: Unknown/likely absent - the roster note lists SWE+AMR+fgmax+gauges only, no fgout; this is a capability gap for smooth time-stepped water animations (a stated TRID3NT priority).
Aspects: Output format/precision selection (ascii/binary32/binary64); Uniform-resolution animation frame generation decoupled from AMR patch structure; Post-hoc particle tracking / arbitrary-point spatial interpolation from stored fgout snapshots; netCDF aggregation of multiple fgout frames plus transect extraction for downstream analysis
- [CAND-M] `fgout_animation_frames` [M] [US] - How do I generate a uniform-grid animated depth/surface-elevation sequence (MP4/HTML) at fixed output times, decoupled from the AMR patch structure, for a smooth flood/tsunami animation?
  src: https://www.clawpack.org/fgout.html (geoclaw_fgout_doc)
  knobs: fgout_grid output_style, tstart/tend/nout, format=binary32, plotdata.file_prefix='fgoutNNNN'
- [CAND-M] `fgout_netcdf_transect_export` [M] [non-US] - How do I combine multiple fgout frames into a single netCDF file and extract a transect (surface elevation vs topography) for downstream analysis outside GeoClaw's own plotting?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/tsunami/chile2010_fgmax-fgout (geoclaw_chile2010_fgmax_fgout)
  knobs: fgout_tools netCDF combine, transect sampling line definition
  notes: Chile case; netCDF/transect export pattern transfers to any US AOI.

### gauges (Eulerian + Lagrangian)
Purpose: Time-series recording at fixed points (standard Eulerian gauges, h/hu/hv/eta) or moving with the flow (Lagrangian particle gauges advected by depth-averaged velocity).
Today: LANDED as the `geoclaw_tsunami_gauge_timeseries` template (ADR 0123): a capability-named tsunami run recording a coastal Eulerian gauge -> the surface-elevation time series (waveform + co-seismic subsidence, the initial post-quake offset) + typed gauge scalars on the peak-inundation layer, riding the existing inundation deck (the composer's fort.*-only download filter was widened to include gaugeNNNNN.txt). Live docker smoke (Crescent City AOI): real tsunami solve -> gauge parsed -> scalars (max_surface_elevation 3.64 m, amplitude 0.43 m, coseismic_offset 3.64 m). Lagrangian particle gauges (v5.7.0+) remain a distinct unsurfaced capability.
Aspects: Standard fixed-point Eulerian gauge time series; Lagrangian (particle-tracking) gauges advected by Forward-Euler integration of depth-averaged velocity; Gauge update-frequency control (min_time_increment); Coupled visualization via clawpack.visclaw.particle_tools
- [LANDED] `lagrangian_particle_gauges_wake_tracking` [S] [US] - How do I switch a gauge from fixed-point to Lagrangian mode so it is advected by the flow (e.g. to trace a vortex/wake behind an island or structure)? [LANDED 2026-08-05 (ADR 0155, folds this row AND its plotting sibling): the `lagrangian_particles=[(lon,lat),...]` knob on the GeoClaw surface (geoclaw_inundation). Each seed point is added as a gauge with per-gauge `gtype='lagrangian'` (the stationary coastal gauge stays 'stationary'), so GeoClaw advects it by the depth-averaged velocity and writes its position x(t),y(t) in the q[2,3] columns ("# Lagrangian particle" header, verified in gauges_module.f90). NEW agent-side postprocess: `parse_geoclaw_particle_tracks` reads those columns into drift tracks and emits a LineString PRODUCT layer (particles.geojson, style_preset particle_track, one line per drifter) PLUS a cumulative-drift-vs-time chart (the plotting sibling) to the dock; track scalars (count / max length / duration) ride the peak layer. The board's [non-US] conical-island fixture is a mechanism reference; our Crescent City harbour deck IS US and is what we smoke. Live docker smoke (rebuilt image), Crescent City harbour tsunami Mw9 with 3 drifters seeded in the harbour: 3 Lagrangian tracks recorded (1105 samples each over 899 s), cumulative drift 241 / 258 / 125 m -- the drift accelerates when the wave reaches the harbour (~500 s), physically sensible. particles.geojson (3 LineStrings) + the cumulative-drift chart emitted. Proofs docs/proof/templates/geoclaw_lagrangian_particles{,_chart}.png (map: seed dots + end triangles + depth raster over Esri imagery; chart: cumulative drift vs time, 3 series). No new tool (knob fold): registry + EXPECTED_TEMPLATES unchanged.]
  src: https://github.com/clawpack/geoclaw/tree/master/examples/tsunami/island-particles (geoclaw_island_particles)
  knobs: rundata.gaugedata.gtype='lagrangian' (global or per-gauge dict), min_time_increment
  notes: Synthetic conical island case; the gtype toggle is a direct knob on the existing gauge system.
  DEFER-LANDABLE (ADR 0143, triage): machinery VERIFIED PRESENT in clawpack 5.14.0 - GaugeData.gtype (default 'stationary') accepts 'lagrangian'; gauges_module.f90 + visclaw/particle_tools.py are compiled into the image. Not landed this batch (kept the batch to the two rock-solid deck knobs). Recipe to land (fold 6+7 into one template `geoclaw_lagrangian_particle_gauges`): (1) setrun_builder emit rundata.gaugedata.gtype='lagrangian' + seed gauges at lon/lat + min_time_increment; (2) postprocess NEW reader branch - a Lagrangian gaugeNNNNN.txt records the advected particle position (xg,yg) not h/hu/hv (fortran writes "# Lagrangian particle," header, cols xg,yg,ug,vg), so add parse_geoclaw_particle_track + a track chart spec (xg vs yg path); (3) new template + corpus + categories + pins; (4) live smoke on a synthetic conical-island (or US harbour) tsunami tracing the wake, own docker rebuild + proof.

### storm surge / wind module
Purpose: Parametric or gridded tropical-cyclone wind+pressure forcing applied as a source term to the SWE solver for storm-surge simulation.
Today: Unknown/likely absent - not listed in the roster note; this is a clear capability gap for the coastal-flood/hurricane use case.
Aspects: Best-track data ingestion (ATCF/HURDAT/IBTrACS/JMA/tcvitals) converted to the GeoClaw storm format; Parametric wind/pressure model (Holland 1980) driven directly from track data; Gridded meteorological forcing (OWI ASCII pressure+wind pairs, or CF-compliant NetCDF, e.g. ERA5) as an alternative to parametric; Wind drag law selection (none / Garratt / Powell 2006) controlling wind-stress magnitude; Storm-surge validation against a real, published historical US hurricane (Ike, Isaac)
- [CAND-M] `best_track_to_geoclaw_storm_file` [M] [US] - How do I convert a real NHC/ATCF best-track advisory (or HURDAT2 archive record) for a named US hurricane into the GeoClaw storm-format file the solver reads?
  src: https://www.clawpack.org/storm_module.html (geoclaw_storm_module_doc)
  knobs: Storm(path, file_format='ATCF'|'HURDAT'|'IBTrACS'|'JMA'|'tcvitals').write(path, file_format='geoclaw')
- [CAND-M] `parametric_holland_wind_surge_ike` [M] [US] - How do I run a parametric Holland-1980 wind/pressure storm-surge simulation for a real, published US hurricane (Ike, 2008) and check it against the example's committed regression output?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/storm-surge/ike (geoclaw_storm_surge_ike)
  knobs: storm_specification_type='holland80', drag_law (0/1/2), storm_data path, AMR level count
  notes: Real published US case (Hurricane Ike) with committed regression_data - strong V&V template.
- [CAND-L] `gridded_wind_pressure_forcing_isaac` [L] [US] - How do I force storm surge with gridded meteorological fields (OWI ASCII pressure/wind pairs, or CF-NetCDF/ERA5) instead of a parametric wind model, from the same best-track source?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/storm-surge/isaac (geoclaw_storm_surge_isaac)
  knobs: storm_specification_type='owi'|'netcdf', gridded forcing file paths, ERA5-derived NetCDF variant
  notes: Real published US case (Hurricane Isaac, 2012); README explicitly demonstrates 'two meteorological-forcing families driven from the same ATCF best-track file' - a genuinely new forcing pathway, not a knob.
- [CAND-M] `wind_drag_law_selection` [M] [US] - How do I choose between no wind drag / Garratt / Powell(2006) drag laws for the wind-stress term, and how much does the choice change peak surge? [RETAG CAND-S -> CAND-M 2026-08-05 (ADR 0155, executing the ADR 0143 STOP verdict): the whole storm-surge/wind MODULE is absent from the surfaced deck, so drag_law is INERT and this is NOT an S-tier one-knob fold. rundata.surge_data EXISTS in the clawpack API (drag_law, wind_forcing, storm_specification_type, storm_file) and the surge Fortran is compiled in (geoclaw/src/2d/shallow/surge), BUT the TRID3NT "surge" scenario is a v0.1 sea-level-offset stub: NO surge_data setrun block, NO wind_forcing, NO storm file. drag_law scales the WIND-STRESS term, and with wind_forcing=False + no storm it changes NOTHING (you cannot demonstrate a peak-surge delta without wind). Landing drag_law is INSEPARABLE from landing the parametric-Holland wind module (this is best_track_to_geoclaw_storm_file + parametric_holland_wind_surge_ike territory). Recipe: (1) ATCF/HURDAT best-track fetcher -> geoclaw storm file (clawpack.geoclaw.surge.storm.Storm.write); (2) setrun_builder emit surge_data (storm_specification_type='holland80', storm_file, wind_forcing=True, pressure_forcing=True, drag_law knob); (3) entrypoint stage the storm file; (4) validate against the committed Ike regression_data. Then drag_law becomes a real 0/1/2 sweep. Deferred as its own storm-surge wave.]
  src: https://www.clawpack.org/quick_surge.html (geoclaw_quick_surge_doc)
  knobs: rundata.surge_data.drag_law = 0 (none) / 1 (Garratt) / 2 (Powell 2006)
  STOP (ADR 0143, triage): the whole storm-surge/wind MODULE is absent from the surfaced deck path, so drag_law is inert. rundata.surge_data EXISTS in the clawpack API (drag_law, wind_forcing, storm_specification_type, storm_file) and the surge Fortran is compiled in (geoclaw/src/2d/shallow/surge), BUT the TRID3NT "surge" scenario is a v0.1 sea-level-offset stub: NO surge_data setrun block, NO wind_forcing, NO storm file. drag_law scales the WIND-STRESS term - with wind_forcing=False and no storm it changes nothing (you cannot demonstrate a peak-surge delta without wind). Landing drag_law is inseparable from landing the wind module (this is really best_track_to_geoclaw_storm_file + parametric_holland_wind_surge_ike territory). Recipe: (1) ATCF/HURDAT best-track fetcher -> geoclaw storm file (clawpack.geoclaw.surge.storm.Storm.write); (2) setrun_builder emit surge_data (storm_specification_type='holland80', storm_file, wind_forcing=True, pressure_forcing=True, drag_law knob); (3) entrypoint stage the storm file; (4) validate against the committed Ike regression_data. Then drag_law becomes a real 0/1/2 sweep. Deferred as its own storm-surge wave.

### multilayer shallow water
Purpose: Two-(or more)-layer shallow-water equations for density-stratified flows (e.g. internal/interfacial waves), a distinct solver build (Makefile.multilayer) extending the single-layer SWE core.
Today: Unknown/likely absent - not listed in the roster note; this is a separate solver build target, not a knob on the existing single-layer SWE core.
Aspects: Two-layer plane-wave/internal-wave propagation over a bathymetry jump; Dry-state handling for multilayer (a layer vanishing/wetting near shore); AMR support for multilayer (documented as still maturing / may have bugs); Per-layer density and interface-depth parameterization
- [CAND-L] `two_layer_plane_wave_internal_wave` [L] [non-US] - How do I set up a two-layer shallow-water plane-wave test with a bathymetry jump and specified per-layer densities/depths to model an internal (stratified) wave?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/multi-layer/plane_wave (geoclaw_multilayer_plane_wave)
  knobs: per-layer rho (density), eta (interface depths), wave angle/location, Makefile.multilayer build target
  notes: Idealized test case (Mandli 2013 theoretical background); a real US stratified use case (e.g. estuary salt-wedge) would need further adaptation. Separate Riemann solver/build from single-layer SWE.
- [CAND-L] `multilayer_dry_state_bowl_validation` [L] [non-US] - How do I validate multilayer wetting/drying (a layer vanishing near shore) using the multilayer radial-bowl test case, and what are the current known AMR limitations?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/multi-layer/bowl-radial (geoclaw_multilayer_bowl_radial)
  knobs: layer dry-state tolerance, AMR levels (experimental for multilayer per docs)

### boussinesq (2D SGN dispersive solver)
Purpose: Adds dispersive (non-hydrostatic) wave physics beyond the hydrostatic SWE via Serre-Green-Naghdi (recommended) or Madsen-Sorensen equations, needed when wavelength is not long relative to depth - near-field tsunami sources (submarine landslides, asteroid/impact waves) and some earthquake cases.
Today: Unknown/likely absent - not listed in the roster note; this is the newest, most build-heavy GeoClaw module (requires PETSc 3.20+ and num_eqn=5) and a clear capability gap for near-field/dispersive tsunami sources.
Aspects: Equation-set selection (SGN recommended vs Madsen-Sorensen vs SWE fallback); Depth-based automatic switching between Boussinesq and SWE (bouss_min_depth) so shallow/runup regions stay on the robust SWE solver; AMR-level gating for where Boussinesq correction terms apply (bouss_min_level/max_level); 1D Boussinesq wave-tank replication cases vs full 2D; PETSc-based implicit sparse solve across AMR levels (build/runtime requirement, num_eqn=5)
- [CAND-L] `sgn_boussinesq_depth_switched_solve` [L] [US] - How do I enable SGN Boussinesq dispersive correction terms (num_eqn=5, bouss_equations=2) with automatic fallback to SWE below bouss_min_depth so shoreline/runup regions stay on the robust solver?
  src: https://www.clawpack.org/bouss2d.html (geoclaw_bouss2d_doc)
  knobs: bouss_equations (0=SWE/1=Madsen-Sorensen/2=SGN), bouss_min_depth (default 10m), bouss_min_level/max_level, num_eqn=5
  notes: New solver capability: requires PETSc 3.20+ and MPI for the implicit sparse solve - not present in TRID3NT's current SWE build.
- [CAND-L] `radial_flat_bouss_dispersion_smoke_case` [L] [non-US] - How do I run the reference 2D radial Boussinesq test case (flat bathymetry, dispersive wave spreading from a localized source) as a build-verification smoke case before trusting SGN output on a real AOI?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/bouss/radial_flat (geoclaw_bouss_radial_flat)
  knobs: grid resolution, bouss_equations=2, PETSc solver options
  notes: Synthetic flat-bottom test case (v5.10.0); build/regression smoke test, not a hazard demo.
- [CAND-M] `1d_bouss_wavetank_replication` [M] [non-US] - How do I replicate a published 1D Boussinesq wave-tank flume experiment (Matsuyama or USACE) as a calibration/V&V case with documented expected wave-gauge output, before trusting 2D SGN on a real coastal AOI?
  src: https://github.com/clawpack/geoclaw/tree/master/examples/1d_classic/bouss_wavetank_matsuyama (geoclaw_1d_bouss_wavetank)
  knobs: 1D bouss_equations selection, wave-maker boundary condition, gauge comparison to the published flume dataset
  notes: Matsuyama/USACE flumes are lab experiments (not US-hazard field cases) but serve as documented V&V per the model-fidelity doctrine - published expected wave-gauge traces exist to compare against.

Roster gaps (geoclaw): All cited source_urls returned live 200 content via WebFetch. Gaps/caveats: (1) no dedicated clawpack.org doc page for multilayer shallow water was found (searched; only a GitHub examples/multi-layer README + release notes exist) - multilayer candidates cite GitHub example dirs only, and the docs note AMR support for multilayer is "still under development and may have some bugs." (2) quick_surge.html (storm-surge quick-start) is explicitly self-described as "a work in progress and only partially filled out" - thin content; storm_module.html and the ike/isaac example READMEs were used to fill the gap. (3) The standard (non-Lagrangian) fixed-point gauge doc page itself was not independently fetched (relied on setrun_geoclaw.html cross-refs + TRID3NT's existing signed gauge feature per the roster note) since Lagrangian gauges were the actual capability gap of interest. (4) IMD (Indian Meteorological Dept) best-track format is listed in storm_module.html as "planned," not implemented - excluded from candidates. (5) Could not independently verify wind-drag-law formula details (Garratt/Powell 2006) beyond parameter names - quick_surge.html did not expose the underlying physics text.


## ELMFIRE (7 modules)

### Core Fire Spread (Eulerian Level Set + Rothermel Ellipse)
Purpose: Propagate the fire front as independent elliptical Huygens wavelets whose surface spread rate comes from the Rothermel (1972) model, tracked numerically via an Eulerian level-set (phi) field.
Today: Point-ignition spread implemented; elliptical verification (Verification Case 01) signed per roster note. Transient-wind, real-fuel-ingestion, and landscape-potential (MODE=2) capabilities not confirmed as surfaced.
Aspects: constant/uniform wind point-ignition spread (Rothermel + Richards ellipse math); transient/time-varying wind field ingestion; real-world heterogeneous fuel/terrain ingestion (LANDFIRE); landscape-scale head-fire potential mode (no ignition/time, FlamMap-style per-pixel scan); length-to-width ratio ceiling under extreme wind
- [LANDED] `elmfire_verification_elliptical_replication` [S] [US] - Does ELMFIRE's numerical fire perimeter under constant wind/flat terrain match the closed-form elliptical solution (Richards ellipse geometry) within tolerance, as an automated verification? (ADR 0123)
  src: https://elmfire.io/verification/verification_01.html (elmfire-verification-01)
  knobs: fuel_model GR2/102 (constant), wind_speed_mph, wind_dir_deg, duration_hours, domain_km, cellsize_m
  notes: LANDED as the elliptical-verification template: an ALL-CONSTANT flat-grid deck authored agent-side (GR2 fuel, flat terrain, uniform wind -- no LANDFIRE/DEM fetch), ToA perimeter compared to the Richards ellipse from the observed head/back/flank rates -> verification triple (rmse/err/corr-class) + ellipse-overlay chart. Live docker smoke (10 km domain, 15 mph, 1.5 h): err_fraction 0.0375 (< 0.08 coarse tolerance), correlation 0.986 (good), LW 3.41, passed. NAMED RESIDUAL: the fine-grid <0.5% published gate (Rothermel-rate cross-check).
- [LANDED] `transient_wind_schedule_spread` (ADR 0161: elmfire transient deck - 68 deg heading shift) [M] [US] - How does the fire perimeter shape respond to a scripted multi-hour wind direction/speed shift delivered via a multi-band wind raster with linear time interpolation?
  src: https://elmfire.io/tutorials/tutorial_02.html (elmfire-tutorial-02)
  knobs: NUM_METEOROLOGY_TIMES=8, DT_METEOROLOGY=3600, multi-band wind speed/direction rasters (e.g. N at 15kt hrs 0-2, shifting to NE by hr 4), NUM_IGNITIONS/X_IGN/Y_IGN/T_IGN
  notes: Idealized flat-terrain domain, not tied to a real place; published expected behavior is a qualitative shape-shift (south then southwest), no numeric tolerance given.
- [CAND-M] `real_world_fuel_terrain_ingestion_spread` [M] [US] - How does point-ignition spread deviate from the idealized ellipse once real LANDFIRE fuel/terrain heterogeneity replaces spatially-uniform inputs, and is the deviation attributable to fuel vs topography?
  src: https://elmfire.io/tutorials/tutorial_03.html (elmfire-tutorial-03)
  knobs: LANDFIRE version 2.2.0 via Cloudfire fuel_wx_ign.py, default 60km x 60km extent, point ignition lat/lon, inherited transient wind/moisture from tutorial 02
  notes: Published finding: deviations from ellipse are 'primarily fuels with a minor influence of topography' - a qualitative, citable expected outcome.
- [CAND-L] `landscape_scale_headfire_potential_scan` [L] [US] - What is the per-pixel head-fire flame-length/spread-rate potential across an entire landscape under N discrete wind-speed scenarios, independent of ignition point or elapsed time (FlamMap-style)?
  src: https://elmfire.io/tutorials/tutorial_04.html (elmfire-tutorial-04)
  knobs: MODE=2, METEOROLOGY_BAND_START/STOP/SKIP_INTERVAL (6 bands, 0-25mph in 5mph steps), LANDFIRE 2.2.0 fuel/terrain
  notes: New solver operating mode (per-pixel deterministic potential, not transient point-ignition spread) - not a knob on the existing spread path. Outputs head_fire_flame_length_NNN.tif / spread_rate per band.
- [LANDED] `length_to_width_ratio_ceiling_sensitivity` [S] [US] (ADR 0142: `elmfire_length_to_width_ceiling_sensitivity` - redesigned in triage: sweeps MAX_LOW at fixed wind, the faithful cap-binding demo; wind-sweep framing did not isolate the cap) - How sensitive is fire-shape elongation to the MAX_LOW length-to-width cap under extreme wind, and at what wind speed does the cap start binding?
  src: https://elmfire.io/user_guide/physics.html (elmfire-physics)
  knobs: MAX_LOW (default 8.0) in &SIMULATOR
  notes: Directly implicated in Verification Case 02 (crown fire triggers MAX_LOW at high spread rates), giving a documented cross-reference case.

### Crown Fire
Purpose: Model transition from surface to crown fire and the resulting active/passive crown spread rate, linked back into the surface Rothermel wind-factor term.
Today: Not confirmed as surfaced; roster lists only point-ignition surface spread with elliptical verification signed. Crown fire is default-on in ELMFIRE (CROWN_FIRE_MODEL=1) but not evidenced as TRID3NT-exposed.
Aspects: initiation threshold (critical canopy cover + fireline intensity); active crown spread rate (Cruz et al. 2005); crown-to-surface linkage via phi_w coefficient; crown spread-rate ceiling; crown-fire-triggered ember spotting
- [CAND-M] `active_crown_fire_spread_rate_verification` [M] [US] - Does ELMFIRE reproduce the Cruz et al. (2005) active crown fire spread rate and the correct crown/surface phi_w linkage against the exact-ellipse benchmark?
  src: https://elmfire.io/verification/verification_02.html (elmfire-verification-02)
  knobs: 20-ft wind=12mph (10m=22.2km/h), 1-hr FM=4%, canopy bulk density=0.18kg/m3; expect CROSA=65.63 m/min (3938 m/hr), compare hourly_isochrones.shp vs exact_ellipses/ellipse_1.shp
  notes: Published worked numeric expected output (CROSA value); this is TRID3NT's most build-ready crown-fire template. Pass/fail tolerance not numerically quantified on the page.
- [LANDED] `crown_fire_initiation_threshold_sweep` (ADR 0161: crown template - initiation collapse at cc 0.525)  [US] - At what combination of canopy cover and fireline intensity does crown fire initiate, and how does adjusting CRITICAL_CANOPY_COVER shift that boundary?
  src: https://elmfire.io/user_guide/physics.html (elmfire-physics)
  knobs: CRITICAL_CANOPY_COVER (default 0.39), CROWN_FIRE_MODEL (0/1)
- [LANDED] `crown_fire_spread_rate_ceiling_calibration` (ADR 0161: FOLDED into the crown template - capped vs uncapped 10.8x)  [US] - How does capping CROWN_FIRE_SPREAD_RATE_LIMIT change simulated crown-run extent relative to the uncapped Cruz rate (which can reach thousands of m/hr per Verification 02)?
  src: https://elmfire.io/user_guide/physics.html (elmfire-physics)
  knobs: CROWN_FIRE_SPREAD_RATE_LIMIT (default 250 ft/min)
- [CAND-M] `crown_fire_triggered_ember_spotting` [M] [US] - How does ember generation restricted to crown-fire pixels (vs all surface-fire pixels) change downwind spot-fire density and timing?
  src: https://elmfire.io/user_guide/spotting.html (elmfire-spotting)
  knobs: CROWN_FIRE_SPOTTING_PERCENT (default 1.0), ENABLE_SPOTTING=.TRUE.
  notes: Bridges Crown Fire and Spotting modules; worth building once both modules exist independently.

### Spotting
Purpose: Model ember lofting/transport/landing ahead of the main fire front as a stochastic Lagrangian process, producing new ignition points beyond the contiguous perimeter.
Today: Disabled by default in ELMFIRE, off unless ENABLE_SPOTTING=.TRUE.; not confirmed as surfaced in TRID3NT today.
Aspects: lognormal spot-distance distribution as fn(wind speed, fireline intensity); critical fireline-intensity gate for spot generation; ember count per torching event + landing ignition probability; stochastic/randomized spotting parameters for calibration and Monte Carlo
- [CAND-M] `lognormal_spot_distance_model_calibration` [M] [US] - How do the semi-empirical lognormal spotting-distance parameters (mean/variance as power-law functions of wind speed and fireline intensity) shift the downwind spot-fire footprint?
  src: https://elmfire.io/user_guide/spotting.html (elmfire-spotting)
  knobs: MEAN_SPOTTING_DIST (default 5.0m), SPOT_FLIN_EXP (0.3), SPOT_WS_EXP (0.7), NORMALIZED_SPOTTING_DIST_VARIANCE (250.0), SPOTTING_DISTRIBUTION_TYPE=LOGNORMAL
  notes: Author's cited precursor paper (doi:10.1016/j.firesaf.2013.08.014) is explicitly noted as describing a different model than what's implemented - do not cite that DOI as the source, cite this page.
- [CAND-M] `critical_spotting_intensity_threshold_gate` [S->M, STOP ADR 0142] [US] - Below what fireline intensity does ember generation stop, and how does raising CRITICAL_SPOTTING_FIRELINE_INTENSITY suppress nuisance spotting from low-intensity backing fire?
  src: https://elmfire.io/user_guide/spotting.html (elmfire-spotting)
  knobs: CRITICAL_SPOTTING_FIRELINE_INTENSITY (default 0.0 kW/m), SURFACE_FIRE_SPOTTING_PERCENT(:) per fuel model, ENABLE_SURFACE_FIRE_SPOTTING
- [CAND-M] `ember_count_and_landing_ignition_probability` [S->M, STOP ADR 0142, fold w/ intensity-gate row] [US] - How does the number of embers cast per torching event and their probability of igniting on landing change spot-fire proliferation rate?
  src: https://elmfire.io/user_guide/spotting.html (elmfire-spotting)
  knobs: NEMBERS_MIN/NEMBERS_MAX (default 1,1), PIGN (default 100%)
- [CAND-L] `stochastic_spotting_parameter_ensemble` [L] [US] - What is the spread of spot-fire outcomes when all spotting parameters are randomized within user-defined ranges for Monte Carlo calibration, versus a single deterministic parameter set?
  src: https://elmfire.io/user_guide/spotting.html (elmfire-spotting)
  knobs: STOCHASTIC_SPOTTING=.TRUE. (randomizes 8 named parameters within min/max ranges)
  notes: Couples directly to the Ensembles/Monte Carlo module; page states this enables 'automated calibration' - a distinct capability from a single fixed-parameter spotting run.

### Suppression
Purpose: Model firefighting response as two linked stages: initial-attack containment probability at first response, and extended-attack containment-rate change over multi-day incidents.
Today: Both models disabled by default in ELMFIRE ('experimental' per site overview); not confirmed as surfaced in TRID3NT today.
Aspects: initial-attack containment probability (Hirsch POC formula, response-time chain); extended-attack containment-change / Suppression Difficulty Index model; combined initial->extended attack pipeline
- [CAND-M] `initial_attack_containment_probability` [M] [US] - Given fire size and head-fire fireline intensity at the moment of first response, what is the Hirsch-model probability of containment, and how sensitive is it to INITIAL_ATTACK_TIME (detection+report+travel delay)?
  src: https://elmfire.io/user_guide/suppression.html (elmfire-suppression)
  knobs: ENABLE_INITIAL_ATTACK=.TRUE., INITIAL_ATTACK_TIME (seconds from ignition); POC = E/(1+E), ln(E) = 4.6835 - 0.7043*A - 0.00041*I - 0.000052*A*I (A in ha, I in kW/m)
  notes: Published closed-form formula with exact coefficients - strong basis for a deterministic verification template even without a full engine run.
- [CAND-M] `extended_attack_suppression_difficulty_index` [M] [US] - How does the Suppression Difficulty Index (SDI) formulation change the daily containment-percentage growth curve versus the simpler default areal-growth-rate model?
  src: https://elmfire.io/user_guide/suppression.html (elmfire-suppression)
  knobs: ENABLE_EXTENDED_ATTACK=.TRUE., USE_SDI (default .FALSE.), USE_SDI_LOG_FUNCTION (default .FALSE.), SDI_FACTOR (1.0), B_SDI (1.0), DT_EXTENDED_ATTACK (3600s), MAX_CONTAINMENT_PER_DAY (100%), AREA_NO_CONTAINMENT_CHANGE (10000.0)
- [CAND-L] `combined_initial_extended_attack_pipeline` [L] [US] - Chained end-to-end: does a fire ever escape initial attack per the Hirsch POC model, and if so how does the extended-attack SDI-based containment curb final burned area versus an unsuppressed run?
  src: https://elmfire.io/user_guide/suppression.html (elmfire-suppression)
  knobs: ENABLE_INITIAL_ATTACK + ENABLE_EXTENDED_ATTACK together, full parameter set from both sub-models
  notes: No single published worked example chains both stages together on this page - inferred composite, not directly evidenced.

### Fuel Moisture Conditioning
Purpose: Supply and temporally resolve dead and live fuel moisture content, which directly drives the Rothermel spread-rate calculation and crown-fire Cruz correlation.
Today: Weather/moisture rasters are consumed implicitly by existing point-ignition spread runs (per Tutorial 01/03 inputs) but conditioning controls are not confirmed as their own exposed, independently-tunable capability.
Aspects: dead fuel moisture (1/10/100-hr) raster ingestion + linear-interpolation frequency control; live fuel moisture (herbaceous/woody/foliar) spatially-uniform override; live fuel moisture raster ingestion (spatially-varying); real-world weather/moisture retrieval pipeline (Cloudfire fuel_wx_ign.py)
- [LANDED] `dead_fuel_moisture_interpolation_frequency_control` (ADR 0161: DT_METEOROLOGY accuracy-vs-cost, 25.4% at 1800s)  [US] - How much does coarsening the 1/10/100-hr dead-fuel-moisture linear-interpolation interval degrade simulated spread-rate accuracy versus the runtime cost saved?
  src: https://elmfire.io/user_guide/io.html (elmfire-io)
  knobs: DT_INTERPOLATE_M1 (default 300s), DT_INTERPOLATE_M10, DT_INTERPOLATE_M100
- [LANDED] `live_fuel_moisture_uniform_override` [S] [US] (ADR 0142: `elmfire_live_fuel_moisture_sensitivity`) - How does shifting spatially-uniform live herbaceous/woody/foliar moisture (a single scalar per run) change spread rate and crown-fire onset relative to raster defaults?
  src: https://elmfire.io/user_guide/io.html (elmfire-io)
  knobs: LH_MOISTURE_CONTENT, LW_MOISTURE_CONTENT, FOLIAR_MOISTURE_CONTENT (all in &INPUTS, percent)
- [CAND-M] `live_fuel_moisture_raster_ingestion` [M] [US] - What is gained in spread-pattern realism by switching live fuel moisture from a spatially-uniform scalar to a spatially-varying raster input?
  src: https://elmfire.io/user_guide/io.html (elmfire-io)
  knobs: raster-mode LH/LW/FOLIAR moisture inputs vs scalar &INPUTS defaults
  notes: Page documents the uniform default and the raster alternative exists per ELMFIRE's general raster-input pattern for weather rasters, but a dedicated worked example specific to live-moisture rasters was not found on this page - verify before building.
- [CAND-L] `realworld_weather_moisture_conditioning_pipeline` [L] [US] - Can fuel-moisture and weather conditioning for a real US location be retrieved and staged automatically (rather than hand-authored idealized rasters), matching Tutorial 03's real-fuel/real-weather setup?
  src: https://elmfire.io/tutorials/tutorial_03.html (elmfire-tutorial-03)
  knobs: fuel_wx_ign.py (Cloudfire gRPC microservice), LANDFIRE 2.2.0, point ignition coordinates
  notes: Requires standing up/consuming the Cloudfire microservice client, not just a namelist knob - new integration surface.

### Ensembles / Monte Carlo
Purpose: Run many stochastically-perturbed realizations (ignition location, weather stream, input-raster perturbation, wind fluctuation) and aggregate into probabilistic outputs like burn probability.
Today: Not confirmed as surfaced; roster lists only single point-ignition spread. NUM_ENSEMBLE_MEMBERS=100 appears in the published Validation Case 01 (County Fire, CA) as the field-tested ensemble scale.
Aspects: randomized ignition locations (uniform vs density-weighted, ignition-mask constrained); weather-stream / historical-meteorology-band sampling; raster perturbation (uniform PDF on fuel moisture, wind, canopy, etc.); wind fluctuation intensity randomization; burn-probability aggregation output (conventional / passive crown / active crown bands)
- [CAND-L] `random_ignition_burn_probability_ensemble` [L] [US] - Running N randomized ignition realizations across a landscape, what is the resulting per-pixel burn-probability raster (conventional, passive crown, active crown bands)?
  src: https://elmfire.io/user_guide/monte_carlo.html (elmfire-monte-carlo)
  knobs: RANDOM_IGNITIONS, NUM_ENSEMBLE_MEMBERS or PERCENT_OF_PIXELS_TO_IGNITE, RANDOM_IGNITIONS_TYPE (uniform/density), EDGEBUFFER, ALLOW_MULTIPLE_IGNITIONS_AT_A_PIXEL, CALCULATE_BURN_PROBABILITY -> burn_probability.tif
  notes: Directly cross-referenced by Validation Case 01 which used NUM_ENSEMBLE_MEMBERS=100 on a real CONUS fire - strong published anchor for expected ensemble scale.
- [CAND-M] `ignition_mask_constrained_ensemble` [M] [US] - How does constraining random ignition points to a raster mask (e.g. transmission-line buffer, WUI polygon) change the resulting burn-probability distribution versus an unconstrained domain-wide ensemble?
  src: https://elmfire.io/user_guide/monte_carlo.html (elmfire-monte-carlo)
  knobs: USE_IGNITION_MASK, IGNITION_MASK_FILENAME
- [CAND-M] `historical_meteorology_band_ensemble_sampling` [M] [US] - Sampling across a bank of historical/representative weather scenarios (bands in a multi-band raster) rather than a single wind condition, how does the resulting fire-potential ensemble differ from a single-scenario run?
  src: https://elmfire.io/user_guide/monte_carlo.html (elmfire-monte-carlo)
  knobs: NUM_METEOROLOGY_TIMES, METEOROLOGY_BAND_START/STOP, METEOROLOGY_BAND_SKIP_INTERVAL
  notes: Same band-selection mechanism as Tutorial 04's deterministic 6-scenario scan, but applied stochastically across an ensemble rather than as 6 discrete deterministic runs.
- [CAND-L] `raster_perturbation_uniform_pdf_ensemble` [L] [US] - Perturbing fuel-moisture/wind input rasters by a bounded uniform distribution (spatially and/or temporally varying) across ensemble members, how much does output burn-probability spread widen versus unperturbed inputs?
  src: https://elmfire.io/user_guide/monte_carlo.html (elmfire-monte-carlo)
  knobs: NUM_RASTERS_TO_PERTURB, RASTER_TO_PERTURB(:), PDF_TYPE (uniform only), PDF_LOWER_LIMIT/PDF_UPPER_LIMIT, SPATIAL_PERTURBATION (global/pixel), TEMPORAL_PERTURBATION (static/dynamic)
- [LANDED] `wind_fluctuation_intensity_randomization` [S] [US] (ADR 0142: `elmfire_wind_fluctuation_randomization` - single-run WIND_FLUCTUATIONS members; the 100-member ctl-driven burn-probability ensemble is a follow-on) - Randomizing wind-speed/direction fluctuation intensity within a range per ensemble member (rather than a single fixed fluctuation setting), how does the resulting fire-shape variability compare to the deterministic-fluctuation case?
  src: https://elmfire.io/user_guide/monte_carlo.html (elmfire-monte-carlo)
  knobs: WIND_SPEED_FLUCTUATION_INTENSITY_MIN/MAX, WIND_DIRECTION_FLUCTUATION_INTENSITY_MIN/MAX (randomizes the &SIMULATOR WIND_FLUCTUATIONS base values)
  notes: Builds on the deterministic Wind Fluctuations sub-feature documented in Physics and Numerics (WIND_FLUCTUATIONS, DT_WIND_FLUCTUATIONS) - S effort if that base capability already exists as a knob.

### Verification & Validation Suite
Purpose: ELMFIRE's own published regression/accuracy gates: exact-solution verification cases (idealized physics) and real-fire hindcast validation cases (field accuracy).
Today: Elliptical verification (Verification Case 01) signed per roster note. Crown-fire verification (02) and both validation cases not confirmed as signed.
Aspects: elliptical exact-solution regression (idealized surface fire); crown-fire exact-solution regression (Cruz correlation + ellipse); single historical-fire hindcast validation (real US fire, ensemble fitness metric); multiple-fires validation (unpublished/in-progress)
- [LANDED] `elmfire_verification_elliptical_replication` [S] [US] - Reproduce the elliptical solution as a verification (FOLDED with the Core Spread row -- one template). (ADR 0123)
  src: https://elmfire.io/verification/verification_01.html (elmfire-verification-01)
  knobs: same as Core Spread candidate #1 (fuel GR2/102, uniform wind, flat terrain)
  notes: Already signed per roster; this candidate formalizes it as a standing regression gate rather than a one-time proof, e.g. re-run on every solver-build change.
- [CAND-M] `crown_fire_exact_solution_regression_gate` [M] [US] - As a companion CI-style gate to the elliptical check: does every crown-fire-enabled build reproduce the Cruz (2005) active-crown exact ellipse from Verification Case 02?
  src: https://elmfire.io/verification/verification_02.html (elmfire-verification-02)
  knobs: same as Verification 02 (20-ft wind 12mph, 1-hr FM 4%, CBD 0.18 kg/m3); CROSA expected = 65.63 m/min
  notes: Depends on Crown Fire module existing first; page does not state a numeric pass/fail tolerance the way V01 does (0.5%), so tolerance would need to be chosen/documented when building this gate.
- [CAND-L] `historical_fire_hindcast_ensemble_replication` [L] [US] - Replicating a published single-fire hindcast validation (real US fire, LANDFIRE fuels, 100-member ensemble), does the modeled perimeter ensemble's fitness metric against observed perimeter rasters match the published finding of good agreement at 36 hours?
  src: https://elmfire.io/validation/validation_01.html (elmfire-validation-01)
  knobs: NUM_ENSEMBLE_MEMBERS=100, RUN_HOURS=48, FUEL_SOURCE=landfire FUEL_VERSION=1.4.0, RUN_TEMPLATE=hindcast, CALC_FITNESS=yes, 30km domain buffer around fire polygon
  notes: Published template case is the 2018 County Fire (Yolo/Napa Counties, CA) - candidate name kept capability-generic per naming doctrine; the specific fire is documented context, not the template identity. Requires get_polygons.py + fuel_wx_ign.py tooling plus the full Ensembles module.

Roster gaps (elmfire): Validation Case 02 (Multiple Fires, https://elmfire.io/validation/validation_02.html) is explicitly marked "In progress" on elmfire.io - no methodology or results to cite; excluded from candidates pending publication. The spotting page's own citation (Sardoy et al., doi:10.1016/j.firesaf.2013.08.014) is noted by ELMFIRE's authors as describing a DIFFERENT model than the one currently implemented, so that DOI was not used as a source_url - only the elmfire.io spotting page itself (which documents the current lognormal formulation) was cited. Technical Reference page (tech_ref.html) covers only spread-rate/elliptical math; spotting, suppression, fuel moisture, and Monte Carlo are documented solely in user_guide/*, not tech_ref - no equations-with-citations exist yet for suppression's Hirsch POC formula or the SDI extended-attack model beyond what user_guide/suppression.html states (no dedicated tech_ref section or worked numeric example for suppression, unlike verification_01/02's exact-solution comparisons). No published tutorial/verification case exercises MODE=2 landscape-potential together with spotting, suppression, or Monte Carlo simultaneously - each module's worked examples are documented in isolation, so a combined-capability template is inferred (L effort) rather than citing a single published source.


## SWAN (10 modules)

### MODE (STATIONARY/NONSTATIONARY)
Purpose: Selects whether SWAN solves the action-balance equation as a single time-independent snapshot or marches it forward through time; also toggles 1D vs 2D.
Today: Stationary regular-grid field is signed (per roster note); nonstationary time-marching not yet surfaced.
Aspects: stationary single-timestep solve; nonstationary time-marching solve; 1D vs 2D dimensionality
- [CAND-M] `nonstationary_time_marching_storm_evolution` [M] [US] - How does the nearshore wave field evolve minute-by-minute through a 24-48hr storm rather than a single snapshot?
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node23.html (swan_user_manual_node23_mode)
  knobs: MODE NONSTATIONARY; COMPUTE NONSTAT [tbegc] [deltc] [tendc]; requires time-varying wind/boundary input via nonstationary INPGRID/BOUNDSPEC
  notes: New deck recipe: swaps the single COMPUTE STAT call for a COMPUTE NONSTAT loop plus time-varying forcing plumbing; solver capability itself is native to SWAN, so not L.
- [LANDED] `sequential_stationary_snapshot_batch` [S] [US] (ADR 0147: `swan_stationary_snapshot_batch`; N stationary solves over one AOI+DEM, per-snapshot boundary Hs/Tp/dir; trajectory chart + per-snapshot layers) - Give me wave conditions at several discrete times during an event without paying for a full nonstationary run.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node23.html (swan_user_manual_node23_mode)
  knobs: repeated MODE STATIONARY / COMPUTE STAT blocks with updated wind/boundary each pass
  notes: Pure orchestration of the existing stationary solve TRID3NT already runs; no new physics. Fetches the topo/bathy DEM ONCE and reuses it across snapshots.

### Wave generation source terms (GEN1/GEN2/GEN3)
Purpose: Controls the wind-input growth formulation that pumps energy into the spectrum; GEN3 (third-generation, default WESTH) is the operational standard.
Today: Unknown which GEN3 sub-formulation TRID3NT's signed regression run uses (roster note doesn't specify); presumed default WESTH.
Aspects: GEN1 first-generation simple growth; GEN2 second-generation growth; GEN3 sub-formulations: Komen, Janssen, Westhuysen (default), ST6/Rogers-2012
- [FOLDED] `gen3_westhuysen_default_wind_growth` [S] [US] (ADR 0147: deck-builder `gen_formulation` knob, default "westhuysen" -> unchanged "GEN3 WESTHUYSEN"; explicit on SwanRunArgs + swan_wave_field; also a `swan_physics_sensitivity_sweep` axis) - What is the standard, operationally-tuned wind-generation physics for a coastal storm run?
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: GEN3 WESTH (default, no args needed) - combines saturation-based whitecapping with Yan(1987) wind input
  notes: The implicit default is now an explicit, auditable knob (gen_formulation). GEN growth is wind-input-only, so it is meaningful with a wind grid; the sweep flags it wind-dependent.
- [CAND-M] `gen3_st6_rogers2012_modern_wind_input` [M] [US] - Apply the newer ST6 wind-input/dissipation physics (Rogers et al. 2012) with a specific drag-law choice for a high-wind event.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: GEN3 ST6 [a1sds] [a2sds] ... UP|DOWN, HWANG|FAN|ECMWF drag choice, U10PROXY [windscaling] (default 32), DEBIAS [cdfac]
  notes: New parameter surface (drag-law + normalization enums) beyond a simple default swap.
- [LANDED] `gen_formulation_sensitivity_sweep` [S] [US] (ADR 0147: `swan_physics_sensitivity_sweep` axis="gen_formulation" over [westhuysen, komen, janssen] + gen1/gen2; N solves, overlay chart) - How sensitive is significant wave height to the choice among GEN1/GEN2/Komen/Janssen/Westhuysen for this domain?
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: swap GEN1/GEN2/GEN3 KOMEN/JANSSEN/WESTH keyword across otherwise-identical runs
  notes: The sweep template varies ONE physics axis across N stationary solves and overlays the Hs response. GEN is wind-input-only -> flagged wind_dependent (boundary-forced-only runs barely differ across GEN); the demonstrable default axis is breaking_gamma.

### Whitecapping (WCAPPING)
Purpose: Deep-water dissipation of wave energy from surface whitecaps once steepness exceeds a threshold.
Today: Unknown/implicit (likely GEN3 WESTH default pairing).
Aspects: Alves & Banner (2003) default scheme; Komen et al. (1984) alternative scheme; current-induced whitecapping enhancement
- [FOLDED] `whitecap_dissipation_scheme_toggle` [S] [US] (ADR 0147: deck-builder `whitecapping` knob -> "WCAPPING AB" | "WCAPPING KOMEN" (GEN3 only); on SwanRunArgs + swan_wave_field + a sweep axis) - Compare the default Alves-Banner whitecapping against the classical Komen formulation for the same storm.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: WCAPPING AB [cds2=5.0e-5] [br=1.75e-3]  vs  WCAPPING KOMEN [cds2=2.36e-5] [stpm] [powst] [delta] [powk]
  notes: Straight knob swap; only emitted for GEN3 formulations. Wind-dependent (deep-water whitecapping tracks wind-sea), flagged as such in the sweep.
- [CAND-M] `current_enhanced_whitecapping_opposing_flow` [M] [US] - Does an opposing tidal/river current measurably increase whitecap dissipation and steepen the nearshore spectrum?
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: WCAPPING AB ... CURRENT [cds3=0.8]; requires a current field (INPGRID CURRENT)
  notes: Needs a coupled/forced current field input, not just a keyword flip.

### Quadruplet wave-wave interactions (QUADRUPL)
Purpose: Nonlinear energy transfer among four spectral components that reshapes the spectrum in deep-to-intermediate water; primary mechanism spreading energy from the spectral peak.
Today: Unknown (implicit default iquad=2).
Aspects: DIA integration method selection (per-sweep vs per-iteration); shallow-water scaling coefficients
- [FOLDED] `quad_integration_method_for_ambient_currents` [S] [US] (ADR 0147: deck-builder `quad_iquad` knob -> "QUADRUPL <iquad>" (GEN3 + wind only); on SwanRunArgs + swan_wave_field + a sweep axis) - Get accurate quadruplet transfer in a domain with strong ambient tidal/inlet currents.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: QUADRUPL [iquad]=3 (fully explicit DIA per iteration, recommended with currents) vs default 2
  notes: Single-integer knob landed. CAVEAT: iquad's effect requires an ambient CURRENT field (INPGRID CURRENT) which the standalone wave-field path does not yet stage, and quads are OFF for zero-wind runs -- so the visible effect is nil until a current-forced deck lands. Knob is wired + validated; the current-field forcing is the follow-on.
- [CAND-M] `quad_shallow_water_scaling_calibration` [M] [US] - Tune the shallow-water quadruplet scaling for a specific estuary/inlet bathymetry.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: QUADRUPL [lambda=0.25] [Cnl4=3e7] [Csh1=5.5] [Csh2=0.833] [Csh3=-1.25]
  notes: Multi-coefficient calibration recipe, not a single flag.

### Depth-induced breaking (BREAKING)
Purpose: Dissipates wave energy as waves break in shallowing water - the dominant surf-zone/reef-flat energy sink and a first-order control on nearshore Hs used for coastal flood forcing.
Today: Unknown (implicit default constant-gamma).
Aspects: constant breaker index (default gamma=0.73); bottom-slope dependent breaker index (BKD)
- [FOLDED] `constant_breaker_index_surfzone_default` [S] [US] (ADR 0147: deck-builder `breaking_alpha`/`breaking_gamma` knobs, defaults 1.0/0.73 -> "BREAKING CONSTANT 1.000 0.730" (solve-identical to the prior 1.0 0.73); on SwanRunArgs + swan_wave_field; the DEFAULT `swan_physics_sensitivity_sweep` axis) - Standard surf-zone wave-height dissipation for a beach or barrier-island run.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: BREAKING CONSTANT [alpha=1.0] [gamma=0.73]
  notes: Implicit default is now an explicit, documented knob. gamma is the first-order surf-zone Hs control and the robustly-demonstrable sweep axis (acts on boundary-forced waves without wind).
- [CAND-M] `slope_dependent_breaker_index_bkd_reef_beach` [M] [US] - Get a more accurate breaker index across a steep fringing-reef edge or engineered mild-slope beach than a single constant gamma allows.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: BREAKING BKD [alpha=1.0] [gamma0=0.54] [a1=7.59] [a2=-8.06] [a3=8.09]
  notes: Newer (post-40.91A) formulation; relevant to US reef settings (Hawaii/Guam/PR) and engineered beach nourishment projects.

### Bottom friction (FRICTION)
Purpose: Dissipates wave energy via bed shear stress along the wave propagation path over the shelf/bay bottom.
Today: Unknown (implicit default off unless configured, per manual note that omission = no friction).
Aspects: JONSWAP semi-empirical (default); Collins (1972) drag-law; Madsen et al. (1988) roughness-length; ripple/sediment-coupled friction (Smith et al. 2011)
- [LANDED] `jonswap_friction_regional_coefficient_calibration` [S] [US] (ADR 0147: deck-builder `friction_cfjon` knob -> "FRICTION JONSWAP CONSTANT <cfjon>"; on SwanRunArgs + swan_wave_field; `swan_physics_sensitivity_sweep` axis="friction_cfjon" defaults [0.019, 0.038, 0.067]) - Calibrate bottom friction for a specific US shelf - sandy Atlantic/Gulf shelf vs the smoother Gulf of Mexico bottom the manual explicitly calls out.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: FRICTION JONSWAP [cfjon]=0.038 (default, sandy) or 0.019 (manual-recommended for Gulf of Mexico-type smoother beds)
  notes: The manual's named Gulf-of-Mexico (0.019) vs sandy (0.038) vs swell (0.067) coefficients are the sweep defaults. Dissipation acts on boundary-forced waves -> demonstrable without wind. LIVE-DEMONSTRATED on the Apalachee Bay FL shelf (cfjon [0.01,0.10,0.30]): whole-field mean Hs 1.884 -> 1.645 -> 1.285 m (31.8% monotonic spread) while the boundary-pinned peak holds ~3.0 m. friction_cfjon is the sweep template's DEFAULT axis (whole-path dissipation, demonstrable on any shelf); breaking_gamma is surf-zone-confined (byte-identical fields on a deep open-coast box -- a real physics finding). New sensitivity metric mean_hs_m added to WaveFieldLayerURI (agent-side postprocess, no image rebuild).
- [CAND-M] `madsen_spatially_variable_roughness_field` [M] [US] - Vary bottom roughness spatially across a domain with mixed sand/rock/seagrass substrate.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: FRICTION MADSEN [kn]=0.05m default, spatially variable via INPGRID FRICTION + READINP FRICTION
  notes: Requires a gridded roughness input field, not just a scalar.
- [CAND-L] `ripple_sediment_coupled_friction` [L] [US] - Let bottom friction respond to sediment grain size and ripple geometry rather than a fixed coefficient.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: FRICTION RIPPLES [S]=2.65 [D]=0.0001m, plus a sediment-grainsize input field
  notes: New capability: needs a sediment-property fetcher/field feeding SWAN, not just a manual coefficient - pairs naturally with MODFLOW-GWT/fields-of-the-world sediment work already in the roadmap.

### Triad wave-wave interactions (TRIAD)
Purpose: Nonlinear shallow-water energy transfer that generates higher-harmonic/bound waves - governs spectral shape over bars, reefs, and in the surf zone.
Today: Unknown (implicit default DCTA).
Aspects: DCTA (default) collinear/noncollinear interactions; LTA extended linear transfer; FTIM full triad model; SPB (Becq-Girard) full triad; biphase parametrization (Eldeberky default vs DeWit); transfer-coefficient model (QUADWAVE default vs FG/MS/BREDMOSE)
- [FOLDED] `dcta_default_triad_surfzone_harmonics` [S] [US] (ADR 0147: bare "TRIAD" (DCTA default) preserved as the `triads` toggle default; explicit DCTA+biphase rendered when `triad_biphase` is set) - Standard shallow-water triad energy transfer for a surf-zone or tidal-flat run.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: TRIAD DCTA [trfac=4.4] [p=4/3] COLL (default) or NONC for noncollinear
  notes: The DCTA default is the documented bare-TRIAD path; the explicit "TRIAD DCTA 4.4 0.66667 COLL BIPHASE ..." form is emitted only when a biphase parametrization is chosen (row below). COLL/NONC left at the DCTA default.
- [CAND-M] `spb_full_triad_reef_bar_system` [M] [US] - Get a more physically complete triad transfer over a complex reef/sandbar system where DCTA's approximations may break down.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: TRIAD SPB [trfac=0.9] [a=0.95]
  notes: Heavier physics recipe; relevant to fringing-reef US territories (Hawaii/Guam/American Samoa).
- [FOLDED] `biphase_parametrization_eldeberky_vs_dewit_calibration` [S] [US] (ADR 0147: deck-builder `triad_biphase` knob -> "TRIAD DCTA 4.4 0.66667 COLL BIPHASE ELDEBERKY <urcrit>" | "... DEWIT <lpar>"; triad_urcrit/triad_lpar calibration; on SwanRunArgs + swan_wave_field + a `swan_physics_sensitivity_sweep` axis) - Tune the phase-coupling assumption underlying triad energy transfer for a specific site's Ursell-number regime.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node28.html (swan_user_manual_node28_physics)
  knobs: TRIAD ... ELDEBERKY [urcrit]=0.63 (default, field-data value) or DEWIT [lpar]=0
  notes: Affects DCTA/LTA/FTIM alike; single-keyword calibration knob.

### Computational grid & nesting (CGRID/NGRID/GROUP/BOUNDNEST)
Purpose: Defines the domain discretization (regular/curvilinear/unstructured) and the hierarchical coupling of coarse offshore grids into fine nearshore grids - the core scale-bridging mechanism SWAN uses to go from ocean-scale wind-wave fields to nearshore/reef-scale surf physics.
Today: Stationary regular-grid field only (per roster note) - no nesting, no curvilinear, no unstructured mesh surfaced yet.
Aspects: regular rectangular grid; curvilinear (coastline-following) grid; unstructured triangular mesh (vertex-centered, locally refined); SWAN-to-SWAN nested-grid coupling (NGRID/NESTOUT/BOUNDNEST1); WAVEWATCH III boundary nesting (BOUNDNEST3); WAM boundary nesting (BOUNDNEST2, untested per manual)
- [CAND-L] `two_level_nested_grid_coarse_to_fine_coupling` [L] [US] - Run a coarse regional grid and automatically feed its boundary spectra into a fine nearshore grid for a bay/inlet.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node31.html (swan_user_manual_node31_ngrid_group)
  knobs: coarse run: NGRID 'sname' + NESTOUT; fine run: BOUNDNEST1 reads coarse NESTOUT file, interpolates freq/dir automatically
  notes: New solver capability: orchestrating two coupled SWAN runs (coarse then fine) plus the file handoff, not a single-run knob.
- [CAND-L] `unstructured_triangular_mesh_local_refinement` [L] [US] - Resolve a complex, highly irregular shoreline/island/reef system with local mesh refinement instead of nested rectangular grids.
  src: https://swanmodel.sourceforge.io/online_doc/swantech/node84.html (swan_tech_manual_node84_unstructured)
  knobs: CGRID UNSTRUCTURED + READGRID UNSTRUC (vertex/triangle connectivity from an external mesh generator e.g. Triangle/Easymesh)
  notes: New build/solver capability: vertex-centered fully-implicit Gauss-Seidel solver path is a materially different code path from the regular-grid sweep SWAN uses today; also needs a mesh-generation step upstream.
- [CAND-M] `ww3_boundary_nested_regional_downscale` [M] [US] - Downscale a WAVEWATCH III regional/global hindcast or forecast into a nearshore SWAN grid - the exact pattern USGS used for Pacific-island coastal flood forecasting.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node27.html (swan_user_manual_node27_boundary)
  knobs: BOUNDNEST3 CLOSED|OPEN, FREE|UNFORMATTED WW3 output files (post-processed via ww3_outp), Cartesian or spherical
  notes: Mirrors the WAVEWATCH3-to-SWAN nesting pattern documented for the USGS Hawaiian Islands wave-projection release (DOI 10.5066/F7G73CP1); most US-relevant nesting entry point since NOAA runs WW3 operationally.
- [CAND-M] `curvilinear_grid_coastline_following_domain` [M] [US] - Align the computational grid with a curving coastline or estuary channel instead of a rectangular regular grid.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node25.html (swan_user_manual_node25_cgrid)
  knobs: CGRID CURVILINEAR + READGRID COOR (externally supplied grid-point coordinates)
  notes: New grid-type deck recipe; needs an upstream coordinate-generation step (e.g. from a channel-following mesh tool).

### Boundary condition specification (BOUNDSPEC)
Purpose: Sets the incident wave energy entering the domain's open boundaries, either as parametric Hs/Tp/direction/spreading or a full spectral file.
Today: Unknown.
Aspects: SIDE (whole-edge) vs SEGMENT (partial/complex-boundary) specification; PAR parametric input vs FILE spectral input
- [FOLDED] `parametric_boundary_spectrum_from_hindcast_or_buoy` [S] [US] (ADR 0147: ALREADY SHIPPED on swan_wave_field -- boundary_hs_m/boundary_tp_s/boundary_dir_deg/boundary_spread_deg/boundary_side render "BOUND SHAPE JONSWAP" + "BOUNDSPEC SIDE <side> CONSTANT PAR"; SwanWaveBoundary contract; the snapshot-batch template drives it per snapshot) - Force the domain boundary with a simple Hs/Tp/direction/spread set from a buoy observation or hindcast point.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node27.html (swan_user_manual_node27_boundary)
  knobs: BOUNDSPEC SIDE [side] PAR [hs] [per] [dir] [dd]
  notes: The parametric BOUNDSPEC SIDE ... PAR path was already fully wired (SwanWaveBoundary + the boundary_* knobs + _coerce_boundary_inward). NO NDBC/buoy fetcher spec exists on the universal fetcher surface (grep: zero ndbc/buoy specs), so live buoy-forced boundaries are a data-source follow-on; today the forcing is user-supplied or a labeled synthetic demo boundary.
- [CAND-M] `segment_variable_boundary_complex_coastline` [M] [US] - Vary boundary wave conditions along a non-straight, multi-segment open edge (e.g. an inlet plus adjoining barrier-island faces).
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node27.html (swan_user_manual_node27_boundary)
  knobs: BOUNDSPEC SEGMENT XY|IJ [coordinates] PAR|FILE (per-segment values)
  notes: Multi-segment deck recipe rather than a single SIDE knob.

### Output & spectral partitioning (BLOCK/TABLE/SPECOUT/NESTOUT + watershed partitioning)
Purpose: Extracts gridded fields, point time series, and full spectra from the run, and decomposes a mixed sea state into wind-sea and swell partitions.
Today: Partition + regression signed already (per roster note) - base partitioning capability exists.
Aspects: BLOCK spatial field export (ASCII/MATLAB/netCDF/VTK); TABLE point time series; SPECOUT full spectra (reusable as a downstream boundary); NESTOUT nested-boundary spectra; watershed wave-system partitioning (wind sea + up to 9 swell trains)
- [STOP] `full_partition_parameter_set_windsea_swell_export` [S] [US] (ADR 0147: partition output is NOT in the pipeline -- the board's "partition signed" roster note is unverified against code) - Report the complete per-partition parameter set (not just Hs) - period, wavelength, direction, spread, energy fraction, steepness - for each wind-sea/swell train.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node32.html (swan_user_manual_node32_output)
  knobs: TABLE/BLOCK output vars: PTHSIGN, PTRTP, PTWLEN, PTDIR, PTDSPR, PTWFRAC, PTSTEEP (up to 10 partitions, wind sea first by watershed algorithm)
  notes: STOP recipe -- the deck's _VALID_OUTPUT_QUANTITIES = {HSIGN,RTP,TPS,PER,TM01,TM02,DIR,PDIR,DSPR,SETUP} carries NO PT* partition vars, and postprocess_swan matches only Hsig/HSIGN prefixes (no partition reader). To land: (1) add the PT* quantities to _VALID_OUTPUT_QUANTITIES + emit a second "BLOCK ... PTHSIGN PTRTP PTDIR ..." with a preceding output-request for the watershed partitioner; (2) teach postprocess_swan to read the per-partition arrays from swan_out.mat and rasterize/tabulate them; (3) extend WaveFieldLayerURI (or a sibling) to carry the per-partition scalars. 3-layer change (deck + postprocess + contract) across the worker image -- not a one-command knob.
- [STOP] `netcdf_block_output_for_qgis_cog_pipeline` [S] [US] (ADR 0147: SWAN 41.51 binary in trid3nt-local/swan:latest is NOT built with netCDF -- ldd swan.exe shows no libnetcdf/libhdf5; the .mat->COG postprocess ALREADY feeds the QGIS COG path, so the OUTCOME is covered) - Get SWAN's gridded output directly in a format that slots into TRID3NT's QGIS-native raster/COG publishing path.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node32.html (swan_user_manual_node32_output)
  knobs: BLOCK 'sname' NOHEADER '<file>.nc' LAYOUT ... (netCDF output option)
  notes: STOP recipe -- "BLOCK ... '<file>.nc'" requires SWAN compiled with netCDF-fortran (the current image links neither). The QGIS-COG OUTCOME this row wants is ALREADY delivered: postprocess_swan reads swan_out.mat (scipy) and writes a display-ready EPSG:4326 Hs COG straight into publish_layer. To add native netCDF: rebuild the image with netcdf-fortran + "--enable-netcdf", then add an output_format knob selecting the .nc BLOCK and a netCDF reader branch in postprocess. Internal plumbing, not a user-facing template; low value while the .mat->COG path works.
- [CAND-M] `spectral_output_chained_boundary_for_nested_run` [M] [US] - Feed this run's boundary spectra forward as the BOUNDSPEC FILE input to a subsequent finer-resolution SWAN run.
  src: https://swanmodel.sourceforge.io/online_doc/swanuse/node32.html (swan_user_manual_node32_output)
  knobs: SPECOUT 'sname' SPEC2D ABS '<file>.nc' consumed downstream via BOUNDSPEC ... FILE
  notes: Couples output module to the nesting module above; new orchestration recipe chaining two runs.

Roster gaps (swan): USGS dr1184 (Storlazzi et al. 2024, "Forecasting storm-induced coastal flooding...Hawaiian, Mariana, and American Samoan Islands") could NOT be verified page-by-page: https://pubs.usgs.gov/publication/dr1184 and the direct PDF https://pubs.usgs.gov/dr/1184/dr1184.pdf both returned HTTP 403 to WebFetch on repeated attempts (pubs.usgs.gov appears to block automated fetch across all tested paths), and web.archive.org is not fetchable in this environment (tool refuses the domain). WebSearch snippets corroborate the report exists and contains "Appendix 2: SWAN Model Grid Information" documenting a nested-grid hierarchy for HI/Mariana/American Samoa, but exact grid counts/resolutions/physics-command values in that appendix are UNVERIFIED and are NOT used as candidate sources below. Substituted where possible with the reachable, DOI-verified predecessor USGS data release (2019, main Hawaiian Islands, DOI 10.5066/F7G73CP1) whose landing page IS fetchable and independently confirms the physics roster (bottom friction, depth-induced breaking, quadruplet + triad interactions) and WAVEWATCH3-to-SWAN downscaling pattern. Also unverified/not used: swanmodel.sourceforge.io PDF bundle URLs (swanuse.pdf, swantech.pdf, swanimp.pdf) were returned by search but not individually WebFetched - the equivalent online_doc/ HTML node pages were fetched instead and are cited in their place. Ris, Booij & Holthuijsen (1999) J. Geophys. Res. verification paper (the classical SWAN test-bank/verification citation) was found via search but its field cases (Haringvliet, Norderneyer Seegat, Friesche Zeegat) are Netherlands/Germany, not US - per standing doctrine it is NOT used to source a template candidate here, only noted as the theoretical-verification lineage behind the GEN3/BREAKING/FRICTION/TRIAD defaults.


## PELICUN (11 modules)

### FEMA P-58 Component-Based Seismic Loss Assessment
Purpose: PEER-methodology component-level probabilistic seismic damage-to-repair-cost/time/casualty loss assessment; the high-resolution end of pelicun's fidelity spectrum.
Today: SIGNED per roster note (P-58+HAZUS-EQ+aggregation signed) - version = FEMA P-58 2nd Edition dataset
Aspects: per-component fragility curves indexed by damage state (structural + nonstructural + contents); consequence functions (repair cost, repair time) per component per DS; collapse fragility + collapse consequences (injuries/fatalities); irreparable-damage (residual drift, RID) override consequences; damage-process chaining (e.g. sprinkler-leak-triggers-water-damage rules); decision variables beyond cost/time (injuries, fatalities, red-tag)
- [LANDED] `closed_form_damage_state_probability_check` [S] [US] (ADR 0146: `pelicun_closed_form_validation`, check=ds_probability; MC-vs-analytic max delta 0.00079 @ n=200k) - Does pelicun's Monte-Carlo damage-state sampling match the analytic lognormal closed-form probability for a 2-DS sequential component?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/validation/v1 (pelicun/tests/validation/v1 (readme + closed-form eqns, CI-verified))
  knobs: median/dispersion of capacity RVs (delta_C1,C2 / beta_C1,C2), demand median/dispersion, sample size
- [LANDED] `mixed_fragility_and_loss_function_aggregation` [S] [US] (ADR 0146: `pelicun_mixed_fragility_loss_assessment`) - How do fragility-driven damage consequences and direct loss-functions combine in one P-58 assessment, and how do AcrossFloors/AcrossDamageStates aggregation settings change portfolio Cost and Time?
  src: https://nheri-simcenter.github.io/pelicun/examples/notebooks/example_3.html (pelicun docs Example 3 (project PRJ-3411v5, 8 performance groups))
  knobs: correlation structure (perfect vs independent), sample size, random seed, aggregation level, loss-map assignment
- [CAND-M] `collapse_and_irreparable_consequence_override` [M] [US] - Given a P-58 damage-process JSON, how do building-collapse and RID-triggered irreparable-damage rules override component-level repair consequences with replacement cost/time?
  src: https://github.com/NHERI-SimCenter/pelicun/blob/master/pelicun/examples/0_tmp/DMG_process_P58.json (pelicun/examples/0_tmp/DMG_process_P58.json)
  knobs: collapse fragility params, RID threshold, replacement cost/time constants
- [CAND-M] `default_p58_fragility_consequence_db_sweep` [M] [US] - For a given component inventory (CMP_QNT), which of the full FEMA P-58 2nd Ed default fragility/consequence catalog entries apply, and what is the coverage/miss rate against a real component list?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/seismic/building/component/FEMA%20P-58%202nd%20Edition (DamageAndLossModelLibrary seismic/building/component/FEMA P-58 2nd Edition (fragility.csv+json, consequence_repair.csv+json))
  knobs: component ID list, DL_Method alias 'FEMA P-58'

### HAZUS Earthquake - Building Portfolio Loss Assessment
Purpose: Whole-building-level seismic fragility and repair-cost consequence by HAZUS model-building-type and seismic design level; the regional/portfolio-scale earthquake methodology (not per-component).
Today: SIGNED per roster note - default DL_Method alias 'Hazus Earthquake - Buildings' resolves to v6.1
Aspects: building-type/design-level fragility curves (structural, nonstructural drift-sens, nonstructural accel-sens); auto-population from asset attributes (structure type, year built, occupancy, code era) to HAZUS building type code; repair-cost consequence by occupancy class; dataset versioning (Hazus v5.1 vs v6.1, alias defaults to v6.1)
- [LANDED] `auto_populated_building_type_seismic_run` (ADR 0160: pelicun_hazus_seismic_dl_run - e1 manifest delta=0, seeded)  [US] - Given a single building's AIM (asset inventory model) attributes and a response.csv of EDPs, does DL_calculation.py with --auto_script correctly infer the HAZUS building type/design level and reproduce the checked-in damage/loss response?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/dl_calculation/e1 (pelicun/tests/dl_calculation/e1 (8000-AIM.json, DL_Method='Hazus Earthquake - Buildings', CI regression fixture w/ expected response.csv))
  knobs: auto_script on/off (compare against e1_no_autopop), Realizations count, coupled_EDP
- [LANDED] `hazus_eq_v5_vs_v6_dataset_comparison` (ADR 0160: pelicun_hazus_eq_version_comparison - shared components byte-identical, shift exactly 0; +58 new v6.1 types)  [US] - How much do portfolio-level damage-state probabilities and repair-cost estimates shift between Hazus v5.1 and v6.1 building fragility/consequence datasets for the same building type?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/seismic/building/portfolio/Hazus%20v6.1 (DamageAndLossModelLibrary seismic/building/portfolio/Hazus v5.1 vs v6.1)
  knobs: DLML dataset version pin (full dataset ID vs bare alias)

### HAZUS Earthquake - Building Story-Level (Subassembly) Damage
Purpose: Per-story drift/acceleration-based seismic damage - finer resolution than whole-building portfolio, coarser than P-58 components; used for multi-story buildings where story-by-story demand varies.
Today: not confirmed surfaced - roster note only claims generic 'HAZUS-EQ', likely the Buildings-portfolio variant; story-level is a distinct dataset
Aspects: per-story PID (drift) and PFA (acceleration) demand handling; structural (drift-sensitive) vs nonstructural (accel-sensitive) subassembly fragility; story-level repair-cost/time consequence roll-up to whole-building DVs
- [CAND-M] `story_level_multistory_damage_run` [M] [US] - For a multi-story building with story-varying EDPs, does the 'Hazus Earthquake - Stories' DL_Method (subassembly dataset) produce per-story damage states that roll up to a consistent whole-building loss, matching the checked-in CI fixture?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/dl_calculation/e2 (pelicun/tests/dl_calculation/e2 (also e3, e4; DL_Method='Hazus Earthquake - Stories', Hazus v5.1 subassembly dataset))
  knobs: number of stories, per-story EDP correlation, Realizations

### HAZUS Earthquake - Lifeline Network Damage Models
Purpose: Seismic damage/repair-cost assessment for infrastructure networks (not buildings) - transportation, potable water, and electric power systems - portfolio-scale HAZUS methodology.
Today: unknown/likely unsurfaced - TRID3NT's HAZUS-EQ signing note only references buildings; these are separate asset classes never mentioned
Aspects: transportation network: bridge/road/tunnel component fragility (Hazus v5.1); potable water network: pipe/facility fragility (Hazus v6.1); electric power network: substation/generation fragility (Hazus v5.1); network-asset schema distinct from assetType='Buildings' (new asset class for TRID3NT)
- [CAND-L] `transportation_network_seismic_damage` [L] [US] - For a bridge/road inventory subjected to a ground-motion field, what fraction of network links reach each HAZUS damage state and what is the associated repair cost?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/seismic/transportation_network/portfolio/Hazus%20v5.1 (DamageAndLossModelLibrary seismic/transportation_network/portfolio/Hazus v5.1 (fragility+consequence_repair csv/json, pelicun_config.py))
  knobs: DL_Method='Hazus Earthquake - Transportation', bridge/road inventory attributes
- [CAND-L] `potable_water_network_seismic_damage` [L] [US] - How does pipe-network seismic fragility (Hazus v6.1) translate PGV/PGD demand into break/leak damage states and repair cost across a water distribution system?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/seismic/water_network/portfolio/Hazus%20v6.1 (DamageAndLossModelLibrary seismic/water_network/portfolio/Hazus v6.1 (fragility.csv, pelicun_config.py))
  knobs: DL_Method='Hazus Earthquake - Potable Water', pipe material/diameter attributes
- [CAND-L] `electric_power_network_seismic_damage` [L] [US] - For a substation/generation-facility inventory, what are the HAZUS-derived seismic damage-state probabilities and repair-cost/time estimates, and how do they aggregate to system-level outage duration?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/seismic/power_network/portfolio/Hazus%20v5.1 (DamageAndLossModelLibrary seismic/power_network/portfolio/Hazus v5.1 (fragility.csv/json, pelicun_config.py))
  knobs: DL_Method='Hazus Earthquake - Electric Power', facility inventory attributes

### HAZUS Hurricane Wind Building Loss (wind-only)
Purpose: Wind-only HAZUS building loss assessment - envelope/roof damage fragility driven purely by wind speed, no storm-surge interaction.
Today: unknown/likely unsurfaced - roster note lists only HAZUS flood + P-58 + HAZUS-EQ as signed
Aspects: wind-speed EDP to building-envelope/roof damage-state fragility; repair-cost consequence by building type/terrain exposure; 'original' (uncoupled) dataset variant
- [CAND-M] `wind_only_hazus_hurricane_run` [M] [US] - Does DL_calculation.py with DL_Method='Hazus Hurricane Wind - Buildings - Original' correctly turn a peak-gust-wind-speed demand into the CI-checked damage/loss response for a single building?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/dl_calculation/e8 (pelicun/tests/dl_calculation/e8 (DL_Method='Hazus Hurricane Wind - Buildings', response.csv expected output))
  knobs: terrain exposure, building type, roof shape/cover, Realizations

### HAZUS Hurricane Coupled Wind + Storm-Surge Loss
Purpose: Combines wind damage and storm-surge/flood damage into one governing damage state per building via an explicit combination-rules table - the joint-hazard variant of the hurricane methodology.
Today: unknown - this is a distinct capability from the standalone HAZUS-flood module the roster claims signed; the coupling logic itself is unsurfaced
Aspects: wind fragility branch (shared with wind-only module); storm-surge flood fragility branch (shares the HAZUS flood dataset); combine_wind_flood.csv governing-damage-state combination logic; joint repair-cost/time consequence under the combined DS
- [CAND-L] `coupled_wind_surge_governing_damage_state` [L] [US] - Given both a wind-speed demand and a storm-surge depth demand for the same building, does the coupled DL_Method produce the combine_wind_flood.csv-governed damage state and repair cost, matching the CI-checked expected output?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/dl_calculation/e7 (pelicun/tests/dl_calculation/e7 (DL_Method='Hazus Hurricane Wind - Buildings, Hazus Hurricane Storm Surge - Buildings', response.csv))
  knobs: wind speed + surge depth demand pair, combination-table governing rule, Realizations

### SimCenter Wind Component Fragility Library
Purpose: P-58-style component-level (rather than whole-building) wind fragilities for envelope elements (roof cover, windows, doors) - enables high-resolution wind damage modeling analogous to the seismic FEMA P-58 approach.
Today: unknown/likely unsurfaced
Aspects: per-envelope-component fragility curves vs wind speed; component-level consequence functions; combinable with whole-building HAZUS hurricane for hybrid resolution
- [CAND-M] `component_level_wind_envelope_damage` [M] [US] - For a building's envelope component inventory, what per-component wind-damage-state probabilities result from the SimCenter Wind Component Library fragility curves, and how does that compare to the whole-building HAZUS wind result for the same wind field?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/hurricane/building/component/SimCenter%20Wind%20Component%20Library (DamageAndLossModelLibrary hurricane/building/component/SimCenter Wind Component Library (fragility.csv/json))
  knobs: DL_Method alias 'SimCenter Wind Component Library', component inventory list, wind speed

### HAZUS Flood Building Portfolio Loss Assessment
Purpose: Depth-damage-function-based flood (storm-surge) loss assessment by occupancy and foundation type at the whole-building portfolio scale.
Today: SIGNED per roster note (TRID3NT today: HAZUS flood)
Aspects: depth-damage functions by occupancy class and foundation type; FloodRulesets.py auto-population (foundation type, first-floor elevation -> flood building class); loss_repair.csv consequence (no separate fragility.csv - flood uses direct loss functions, not discrete damage states)
- [LANDED] `foundation_type_depth_damage_sweep` [S] [US] (ADR 0146: `pelicun_flood_foundation_depth_damage_sweep`; 4 distinct RES1 foundation curves) - How does the FloodRulesets.py auto-population logic assign flood building class from foundation type and first-floor elevation, and how sensitive is the resulting repair-cost loss function to that assignment?
  src: https://github.com/NHERI-SimCenter/DamageAndLossModelLibrary/tree/main/src/dlml/data/flood/building/portfolio/Hazus%20v6.1 (DamageAndLossModelLibrary flood/building/portfolio/Hazus v6.1 (FloodRulesets.py, loss_repair.csv, pelicun_config.py))
  knobs: foundation type, first-floor elevation, occupancy class, flood depth demand
- [TEST-ONLY] `standalone_vs_coupled_surge_consistency` [S] [US] (ADR 0146: pelicun ships ONE flood-building alias so both paths resolve to the identical Hazus v6.1 dataset by construction - asserted as a resource-path identity test) - Does the standalone HAZUS-flood loss for a given depth match the storm-surge branch used inside the coupled wind+surge module for the same building/depth (consistency check across the two integration points)?
  src: https://github.com/NHERI-SimCenter/pelicun/blob/master/pelicun/resources/dlml_resource_paths.json (pelicun/resources/dlml_resource_paths.json (both 'Hazus Hurricane Storm Surge - Buildings' and standalone flood alias resolve to flood/building/portfolio/Hazus v6.1))
  knobs: DL_Method alias choice

### Custom Fragility/Loss-Function Definition Framework
Purpose: The generic escape-hatch machinery for defining fragility/consequence/loss-function models OUTSIDE the bundled HAZUS/P-58 datasets - lets TRID3NT define damage/loss for hazards pelicun doesn't ship natively (e.g. tsunami, wildfire structure loss).
Today: unknown/foundational - implicitly exercised by the HAZUS flood module (flood has no discrete fragility, loss-function-only) but not surfaced as a general capability
Aspects: custom fragility.csv/json schema (discrete damage states); custom consequence_repair schema (repair cost/time functions); loss-functions (direct EDP-to-loss regression, bypassing discrete damage states entirely); damage-process JSON chaining/collapse/irreparable rules for custom models; pelicun_config.py hook for dataset-specific auto-population logic; regional/batch execution mode (--regional flag, many buildings in one run)
- [CAND-M] `custom_tsunami_damage_loss_model` [M] [US] - Using a fully custom (non-DLML) fragility+consequence dataset for tsunami inundation depth, does DL_calculation.py in --regional batch mode reproduce the CI-checked damage/loss response across a multi-building inventory?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/dl_calculation/e9 (pelicun/tests/dl_calculation/e9/CustomDLModels (damage_Tsunami.csv/json, loss_repair_Tsunami.csv/json, pelicun_command.txt shows --regional true --custom_model_dir; CI-checked response.csv))
  knobs: custom fragility/consequence CSV content, --regional batch size, --ground_failure flag, --coupled_EDP
- [LANDED] `loss_function_only_1to1_validation` [S] [US] (ADR 0146: FOLDED into `pelicun_closed_form_validation`, check=loss_function_identity; exact to 4 decimals) - For a component defined purely via a loss-function (no fragility/damage-state layer), does the resulting loss distribution exactly reproduce the input EDP distribution as pelicun's own closed-form CI test expects?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/validation/v0 (pelicun/tests/validation/v0 (readme + closed-form 1:1 mapping test, CI-verified))
  knobs: loss-function slope/intercept, EDP distribution params
- [CAND-M] `custom_model_dl_calculation_cli_wrapper` [M] [US] - What is the minimal DL_calculation.py CLI invocation (config JSON + custom_model_dir) needed to bring an arbitrary new hazard's fragility/consequence tables into a pelicun run without touching pelicun source?
  src: https://github.com/NHERI-SimCenter/pelicun/blob/master/pelicun/tools/DL_calculation.py (pelicun/tools/DL_calculation.py (CLI entry point) + pelicun/settings/input_schema.json)
  knobs: --filenameDL, --demandFile, --custom_model_dir, --auto_script, --dirnameOutput

### Demand Model & Uncertainty Quantification
Purpose: Converts raw hazard-response samples (engineering demand parameters, EDPs) into a calibrated multivariate probabilistic demand model that feeds every damage module above.
Today: unknown - underlying machinery for all modules above, not separately surfaced
Aspects: univariate distribution fitting to samples or percentiles (empirical, lognormal, normal, multilinear CDF); multivariate sampling via Gaussian-copula correlation structure across EDPs; RID (residual interstory drift) inference from PID (peak interstory drift); demand cloning (mirroring EDPs across missing stories/directions); sample expansion for undersampled demand sets; custom demand-type registration (verbose name, acronym, unit type)
- [LANDED] `correlation_structure_sensitivity` [S] [US] (ADR 0146: FOLDED into `pelicun_mixed_fragility_loss_assessment`; perfect-vs-independent spread ratio 1.026, perfect wider as theory demands) - How much does portfolio loss uncertainty (Cost/Time distribution spread) change between a perfectly-correlated and an independent EDP correlation structure for the same component inventory?
  src: https://nheri-simcenter.github.io/pelicun/examples/notebooks/example_3.html (pelicun docs Example 3 (demonstrates perfect-correlation demand sampling))
  knobs: correlation matrix / copula structure, sample size
- [LANDED] `rid_from_pid_inference` [S] [US] (ADR 0146: FOLDED into `pelicun_replacement_threshold_override_sweep` via rid_source=inferred / estimate_RID) - Given only peak interstory drift (PID) samples, does pelicun's RID-inference feature produce residual-drift estimates usable to trigger P-58 irreparable-damage consequences without a separate RID demand file?
  src: https://nheri-simcenter.github.io/pelicun/user_guide/feature_overview.html (pelicun docs Feature Overview - Demand Simulation (RID|PID inference listed))
  knobs: PID demand set, RID-inference model params

### Damage-to-Loss Aggregation & Decision-Variable Reporting
Purpose: Rolls per-component/per-story damage samples up to portfolio-level decision variables (repair cost, repair time, injuries, fatalities, red-tag) at configurable aggregation granularity.
Today: SIGNED per roster note (...+aggregation signed)
Aspects: aggregation levels (AcrossFloors, AcrossDamageStates, per performance-group); replacement-cost/time threshold overrides (irreparable-damage cutover); simultaneous multi-DV aggregation (Cost + Time + Injury + Fatality together); sample save/load round-trip for staged/distributed loss-model runs
- [LANDED] `replacement_threshold_override_sweep` [S] [US] (ADR 0146: `pelicun_replacement_threshold_override_sweep`; frac_replaced monotone 0.486->0.352 across RID threshold sweep) - At what aggregate damage/RID threshold does the irreparable-damage override switch a building's reported loss from summed repair consequences to full replacement cost/time, and how does moving that threshold change the portfolio loss curve?
  src: https://nheri-simcenter.github.io/pelicun/examples/notebooks/example_3.html (pelicun docs Example 3 (irreparable/collapse consequence overrides feeding aggregation))
  knobs: replacement cost/time constants, RID/collapse threshold
- [TEST-ONLY] `loss_model_sample_save_load_consistency` [S] [US] (ADR 0146: landed as the save/load byte-identity regression test, not a user-facing template) - Does a LossModel's aggregated decision-variable output remain numerically identical after saving samples to disk and reloading them into a fresh session (needed for staged/batch regional runs)?
  src: https://github.com/NHERI-SimCenter/pelicun/tree/master/pelicun/tests/validation/v2 (pelicun/tests/validation/v2 (readme: 'Testing the save/load sample methods of LossModel', CI-verified))
  knobs: sample count, save/load file format

Roster gaps (pelicun): All cited source_urls were live-verified (HTTP 200) via curl/WebFetch on 2026-08-04. Two items could not be independently confirmed and are flagged rather than guessed: (1) TRID3NT's exact signed HAZUS-EQ dataset version - the roster note says only 'HAZUS-EQ' generically; the code was not inspected in this pass, so whether it pins 'Hazus Earthquake - Buildings' (v6.1, default alias) vs the v5.1 dataset vs the Stories/subassembly variant is unconfirmed - verify against the actual DL_Method string in the TRID3NT pelicun adapter/config before treating the Building-Story module as net-new. (2) pelicun's official example gallery (examples/index.html) currently ships only 3 generic notebooks (loss-function validation, damage-state validation, combined fragility+loss-function) with no HAZUS-named example notebooks - all HAZUS EQ/hurricane/flood template candidates above are instead sourced from the pelicun repo's own CI regression fixtures (pelicun/tests/dl_calculation/e1-e9, pelicun/tests/validation/v0-v2) and the DamageAndLossModelLibrary dataset tree, which are maintainer-curated and CI-checked-output but not badged 'published example' the way the P-58 notebook examples are - treat as first-tier-verified/second-tier-published. The DLML also ships a Streamlit web explorer (src/dlml/web/*) for browsing/searching all model variants interactively; not evaluated as a template source here but worth a follow-up pass if a GUI-driven model-picker is wanted.


## MODFLOW (10 modules)

### GWF-NPF / STO (Node Property Flow / Storage)
Purpose: Core saturated groundwater flow physics: hydraulic conductivity (K, anisotropy, XT3D), confined/unconfined behavior, transient storage (Ss/Sy), Newton-Raphson dry-cell handling.
Today: Core to all 12 existing GWF templates (implicit); no standalone edge-case template for XT3D anisotropy or Newton dry/rewet stress-testing per roster note.
Aspects: anisotropic/XT3D conductance; unconfined rewetting via Newton formulation; transient storage under stress-period-varying stresses; confined-vs-unconfined comparison
- [LANDED] `unconfined_newton_dry_rewet_channel` [S] [US] (2026-08-05, ADR 0153: registered as case `newton_dry_rewet` of the NEW `modflow_package_validation` template, GWF-NPF Newton V&V. Replicates the Zaidel 200x1x1 staircase channel (top 25, botm 20/15/10/5/0 at cols 40/80/120/160, CHD 23->10, k=1e-4, NEWTON) on local mf6 6.7.0. The notebook publishes NO analytical array, so the case is an honest Newton-vs-standard ROBUSTNESS contrast: Newton keeps all 200 cells wet in a monotone staircase (0 dry, heads 10..23 m); the standard formulation collapses 62 cells to dry (nonphysical). Proof docs/proof/templates/modflow_package_validation_newton_dry_rewet.png) - Can the solver handle a staircase-shaped impervious base drying and rewetting an unconfined channel without oscillation/failure?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-gwf-zaidel.html (modflow6-examples:ex-gwf-zaidel)
  knobs: IHC/NEWTON option, base elevation staircase, 200x1x1 grid

### GWF-WEL / RIV / DRN / GHB (basic stress packages)
Purpose: Simplest boundary-condition packages: pumping/injection wells, river leakage, drains (with optional return flow), and general-head boundaries.
Today: Likely covered by existing GWF templates for basic stresses; DRN-vs-UZF discharge comparison and DRN return-flow routing not confirmed as separate templates.
Aspects: basic WEL/RIV/GHB stress-period cycling; DRN discharge-to-land-surface vs UZF seepage comparison; DRN return-flow (ddrn) routing via MVR
- [CAND-M] `drn_vs_uzf_seepage_discharge_comparison` [M] [US] - Does land-surface groundwater discharge simulated via DRN-with-mover match discharge simulated via UZF seepage, for the same watershed?
  src: https://modflow6-examples.readthedocs.io/en/master/_notebooks/ex-gwf-drn-p01.html (modflow6-examples:ex-gwf-drn-p01)
  knobs: DRN elevation/conductance vs UZF SURFDEP, MVR routing to SFR

### GWF-MAW (Multi-Aquifer Well)
Purpose: Simulates a single wellbore open across multiple aquifers/layers, including wellbore storage, skin effects, and flowing (artesian) wells, superseding simple WEL for cross-layer connection.
Today: Roster note lists WEL/RIV/DRN/GHB/etc under GWF but MAW deep-dive status unconfirmed; treat as gap pending audit.
Aspects: non-pumping cross-aquifer flow via well casing (Sokol analytical check); pumped multi-layer well with wellbore storage; flowing (artesian) well discharge at land surface
- [LANDED] `maw_crossaquifer_nonpumping_analytical` [S] [US] (2026-08-05, ADR 0153: case `maw_crossaquifer` of `modflow_package_validation`, GWF-MAW V&V. A non-pumping MAW casing connects two confined aquifers (T_upper=92.9, T_lower=371.6 m2/d; near-zero kv so the well is the ONLY cross-aquifer path). FREE V&V point: computed MAW head 7.92800 m vs the Sokol (1963) transmissivity-weighted analytical level 7.92800 m, delta 2.0e-11 m (rel 2.5e-12). Live mf6 6.7.0. Proof docs/proof/templates/modflow_package_validation_maw_crossaquifer.png) - Does a non-pumping multi-aquifer well equilibrate to the Sokol (1963) analytical water level between two confined aquifers?
  src: https://modflow6-examples.readthedocs.io/en/master/_examples/ex-gwf-maw-p01.html (modflow6-examples:ex-gwf-maw-p01)
  knobs: well screen intervals, CONDEQN, WELL_STORAGE
- [CAND-M] `maw_flowing_well_artesian_discharge` [M] [US] - Can MAW simulate a flowing (artesian) well that discharges at land surface without an external pump, and track discharge decline over time?
  src: https://modflow6-examples.readthedocs.io/en/master/_examples/ex-gwf-maw-p02.html (modflow6-examples:ex-gwf-maw-p02)
  knobs: FLOWING_WELL block, FLOWING_ELEV, FLOWING_COND

### GWF-LAK / SFR / UZF / MVR (advanced surface-water & unsaturated-zone coupling)
Purpose: Route water among lakes, streams, the unsaturated zone, and wells/aquifer through explicit surface-water-network and water-mover accounting rather than simple recharge/leakage.
Today: SFR/LAK/UZF present per roster note (GWF deep, 12 templates) but MVR-mediated multi-package routing and full watershed integration not confirmed as a distinct template.
Aspects: lake-stream-aquifer coupled routing; UZF unsaturated-zone infiltration/ET partitioning with layer-varying properties; MVR provider->receiver water transfer between advanced packages and WEL; watershed-scale UZF+SFR+DRN+MVR integration (Sagehen)
- [CAND-M] `advanced_package_mover_routing_uzf_sfr_lak_wel` [M] [US] - Can MVR correctly transfer rejected UZF infiltration and LAK/WEL discharge into SFR reaches within one coupled timestep?
  src: https://modflow6-examples.readthedocs.io/en/master/_examples/ex-gwf-sfr-p01b.html (modflow6-examples:ex-gwf-sfr-p01b)
  knobs: MVR PACKAGES/PERIOD blocks, provider/receiver pairing, MAXMVR
- [CAND-L] `watershed_uzf_sfr_drn_mvr_integration` [L] [US] - Does a real topographic watershed (elevation-driven infiltration, 200+ stream reaches, drain seepage routed to nearest stream) close its water balance under MVR?
  src: https://modflow6-examples.readthedocs.io/en/develop/_examples/ex-gwf-sagehen.html (modflow6-examples:ex-gwf-sagehen)
  knobs: UZF per-layer properties, SFR network topology, DRN+MVR nearest-reach routing

### GWF-HFB (Horizontal Flow Barrier)
Purpose: Discretization-independent thin barriers (faults, slurry walls, grout curtains) placed between adjacent cell pairs via a hydraulic characteristic, without needing a zero-K cell row.
Today: Not mentioned in roster note at all -- appears to be a gap; only the official MF6 input-guide page was found live, no dedicated worked example in modflow6-examples with published expected output.
Aspects: single cell-pair barrier definition; barrier-wall containment scenario (e.g. slurry wall cutoff)
- [LANDED] `hfb_barrier_wall_containment_knob` [S] [US] (2026-08-05, ADR 0153: case `hfb_barrier` of `modflow_package_validation`, GWF-HFB V&V. A HYDCHR=1e-6 1/d barrier splits a 1000 m single-layer domain (CHD 10->1) solved at 10/20/40/80 columns. Cross-barrier flux 8.9991e-4 m3/d matches the HYDCHR analytical (HYDCHR*area*dh = 9.0e-4; delta 8.9e-8, rel 1e-4) AND varies < 8.7e-6 relative across the 4 grids = grid-refinement INDEPENDENT (the HFB point). No published worked example exists (board note confirmed) - reference is the MF6 gwf-hfb docs conductance formula, honestly docs-cited not notebook-cited. PAIRS WITH the drawn-structures direction: this case proves the HFB knob; the plugin draw-a-cutoff-wall supply path that would feed CELLID1/CELLID2 pairs is the follow-on affordance (NOT built here). Proof docs/proof/templates/modflow_package_validation_hfb_barrier.png) - Can a defined-thickness barrier between two specific cells reduce flux across a wall to a target hydraulic characteristic, independent of grid refinement?
  src: https://modflow6.readthedocs.io/en/latest/_mf6io/gwf-hfb.html (modflow6-docs:gwf-hfb)
  knobs: CELLID1/CELLID2 pairs, HYDCHR

### GWF-CSUB (Skeletal Storage, Compaction, Subsidence)
Purpose: Simulates aquitard/interbed compaction and land subsidence via either simple head-based storage or an effective-stress (delay/no-delay interbed) formulation.
Today: Roster note: CSUB partial in TRID3NT today.
Aspects: effective-stress vs head-based formulation cross-check; delay-interbed time-lagged compaction; preconsolidation stress / inelastic vs elastic compressibility
- [CAND-M] `csub_effective_stress_vs_head_based_crosscheck` [M] [US] - Does the effective-stress CSUB formulation reproduce historical head-based compaction estimates at a real subsidence-monitored site within reported error bounds?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-gwf-csub-p03.html (modflow6-examples:ex-gwf-csub-p03)
  knobs: EFFECTIVE_STRESS_LAG option, interbed thickness/theta, CG_ske_cr/CR

### GWT (Groundwater Transport model)
Purpose: Solute transport (advection/dispersion/sorption/decay/mass storage) coupled to a GWF flow model via a GWF-GWT exchange; includes ADV, DSP, MST, SSM, IST, CNC, SRC sub-packages.
Today: Roster note: GWT deep (part of 12 existing templates).
Aspects: variable-density coupling via BUY buoyancy (saltwater intrusion); unsaturated-zone transport through UZF/UZT; viscosity-temperature-dependent transport feedback
- [CAND-M] `buy_density_driven_saltwater_intrusion` [M] [US] - Does activating the BUY buoyancy package on a GWF-GWT pair reproduce the classic freshwater-outflow-over-recirculating-saltwater interface shape (Henry problem)?
  src: https://modflow6-examples.readthedocs.io/en/latest/_notebooks/ex-gwt-henry.html (modflow6-examples:ex-gwt-henry)
  knobs: BUY DENSEREF/DRHODC, inflow rate scenarios, GWF-GWT exchange
- [CAND-M] `unsaturated_zone_solute_transport_uzt` [M] [US] - Does UZF/UZT purely-advective unsaturated-zone transport match an independently-validated MT3D-USGS/VS2DT benchmark for a wetting front carrying solute to the water table?
  src: https://modflow6-examples.readthedocs.io/en/develop/_examples/ex-gwt-uzt-2d.html (modflow6-examples:ex-gwt-uzt-2d)
  knobs: UZT concentration BC, dispersion on/off (MF6 lacks unsat dispersion)

### GWE (Groundwater Energy / heat transport model)
Purpose: Simulates 3D thermal energy transport in groundwater (advection, conduction, mechanical dispersion, groundwater-solid thermal equilibration, sources/sinks) via ESL/CND/EST sub-packages; released MF6 6.5.0 (2024).
Today: Roster note: GWE zero in TRID3NT today -- entire model type is a gap.
Aspects: radial conductive-advective heat transport vs analytical solution; aquifer thermal energy storage (ATES) seasonal charge/discharge cycling; borehole heat exchanger (BHE) thermal loading, single and multi-well interacting; GWF+GWE+PRT joint thermal-particle-path coupling; viscosity-temperature feedback on flow (VSC); infiltrating heat front through the unsaturated zone (Danckwerts BC)
- [CAND-L] `gwe_radial_conductive_advective_vs_analytical` [L] [US] - Does a GWE radial heat-transport model around a borehole match the Al-Khoury et al. (2020) published analytical isotemperature-contour solution at 48 hours?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-gwe-radial.html (modflow6-examples:ex-gwe-radial)
  knobs: ESL/CND thermal conductivity, radial DISV grid, borehole heat load
- [CAND-L] `gwe_aquifer_thermal_energy_storage_cycling` [L] [US] - Over a multi-year seasonal ATES injection/extraction cycle on a refined DISV mesh, how much thermal energy 'bleeds' into adjacent layers vs is recovered?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-gwe-ates.html (modflow6-examples:ex-gwe-ates)
  knobs: 127 stress periods, TVD advection scheme, two hydrogeologic zones
- [CAND-M] `gwe_borehole_heat_exchanger_thermal_loading` [M] [US] - Does transient multi-BHE thermal loading in a uniform flow field match the Wexler (1992) POINT2 analytical superposition solution?
  src: https://modflow6-examples.readthedocs.io/en/latest/_notebooks/ex-gwe-bhe.html (modflow6-examples:ex-gwe-bhe)
  knobs: multiple BHE locations, time-varying thermal load schedule (3yr)
- [CAND-L] `gwe_multisource_geothermal_interacting_bhes` [L] [US] - Do nine interacting borehole heat exchangers in a 3x3 grid match the Al-Khoury et al. (2021) finite-element multi-source reference solution over 50 days?
  src: https://modflow6-examples.readthedocs.io/en/develop/_notebooks/ex-gwe-geotherm.html (modflow6-examples:ex-gwe-geotherm)
  knobs: 9-BHE 3x3 layout, per-BHE independent load schedules
- [CAND-L] `gwe_particle_path_thermal_profile` [L] [US] - Given a coupled GWF+GWE steady-state flow/thermal field, can a PRT particle's temperature-along-pathline be extracted and does it show monotonic warming with travel time?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-gwe-prt.html (modflow6-examples:ex-gwe-prt)
  knobs: DISV Voronoi grid, WEL injection/extraction, PRT release points
- [CAND-L] `gwe_vsc_temperature_dependent_viscosity_plume` [L] [US] - How much does a solute plume's shape and predicted concentration change when VSC couples GWE-simulated temperature gradients (30C vs 90C) into GWT conductance scaling?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-gwe-vsc.html (modflow6-examples:ex-gwe-vsc)
  knobs: VSC package, two GWF/GWT models + one GWE model, boundary dT
- [CAND-M] `gwe_infiltrating_heat_front_danckwerts` [M] [US] - Does an infiltrating heat front through the unsaturated zone (UZF+GWE) match the third-type (Danckwerts) analytical boundary-condition solution?
  src: https://modflow6-examples.readthedocs.io/en/develop/_examples/ex-gwe-danckwerts.html (modflow6-examples:ex-gwe-danckwerts)
  knobs: UZF constant infiltration rate, ESL thermal boundary condition

### PRT (Particle Tracking model)
Purpose: Native MF6 forward/backward particle tracking (successor workflow to MODPATH 7) over structured, quad-refined, and transient flow fields, coupled via a GWF-PRT exchange.
Today: Roster note: PRT partial in TRID3NT today.
Aspects: forward tracking, structured grid, steady flow (baseline vs MODPATH7); backward tracking, quad-refined DISV grid, steady flow (capture-zone delineation); forward tracking under transient flow; backward tracking with lateral open boundaries (irregular domain)
- [STOP-RECIPE] `prt_forward_structured_steady_vs_modpath7` [S] [US] (2026-08-05, ADR 0153 triage: the row's load-bearing ask is an EXACT PRT-vs-MODPATH7 cross-tool match, which is IMPOSSIBLE here - no mp7 (MODPATH 7) binary exists in the image or local env (only mp7 example INPUT files ship in third_party; `which mp7` = none), AND the cited ex-prt-mp7-p01 notebook publishes NO numeric reference travel-times/endpoints (WebFetch-confirmed), so the mission's fallback "PRT-only vs published reference values" is also unavailable as a tight V&V. Native PRT forward tracking itself WORKS: the bundled 6.5.0 prt deck fails only on an OC-format drift under mf6 6.7.0; a flopy-3.10-authored PRT deck runs clean. RECIPE to land: (1) install the USGS MODPATH 7.2.001 linux binary into the modflow env/image (flopy.utils.get_modflow can fetch it) + SHA-pin it; (2) author the ex-prt-mp7-p01 GWF (3x21x20, wel+riv+rcha) once, run BOTH mf6-PRT (flopy ModflowPrt, forward, 21 release pts along col 3) and MODPATH7 (flopy Modpath7) off the SAME GWF head/budget; (3) compare per-particle termination node + travel time (exact-match assert); (4) register as a 4th `modflow_package_validation` case `prt_forward_vs_modpath7`. Est 1 focused pass once mp7 is present; needs a WORKER-IMAGE rebuild for the container path, or just a local-env install for the live local-exec path.) - Do native PRT pathlines/travel-times on a structured grid with a well+river match an equivalent MODPATH 7 run exactly (same input, cross-tool validation)? [REEVAL 2026-08-06: NATE flag - nothing wrong, CONTINUE DEVELOPING the wellhead track (candidate directions: transient/multi-well pumping, heterogeneous K from real data, kriged potentiometric surface over the plane fit, NHD river boundary condition, permit-grade features).]
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-prt-mp7-p01.html (modflow6-examples:ex-prt-mp7-p01)
  knobs: release point density (sparse/dense), forward direction, structured 3x21x20
- [CAND-M] `prt_backward_capture_zone_quadrefined` [M] [US] - Can backward-tracked particles on a quad-refined DISV grid delineate a well's capture zone under steady flow?
  src: https://modflow6-examples.readthedocs.io/en/develop/_examples/ex-prt-mp7-p02.html (modflow6-examples:ex-prt-mp7-p02)
  knobs: DISV quad-refinement around well, backward TRACKING_STRT
- [CAND-M] `prt_forward_transient_flow_pathlines` [M] [US] - How do particle pathlines/arrival times shift when the same two-aquifer system is driven by transient rather than steady-state flow?
  src: https://modflow6-examples.readthedocs.io/en/develop/_examples/ex-prt-mp7-p03.html (modflow6-examples:ex-prt-mp7-p03)
  knobs: transient stress periods, forward tracking, structured grid
- [CAND-M] `prt_backward_lateral_boundary_injection_wells` [M] [US] - On an irregular quad-refined domain with lateral boundary inflow represented by injection wells, do backward-tracked particles correctly trace to those boundary sources?
  src: https://modflow6-examples.readthedocs.io/en/latest/_examples/ex-prt-mp7-p04.html (modflow6-examples:ex-prt-mp7-p04)
  knobs: large irregular inactive region, boundary injection wells as proxies

### Exchanges (GWF-GWF / GWF-GWT / GWF-GWE / GWF-PRT) + DISV/DISU grids
Purpose: Couples independent models (local grid refinement between two GWF models, or flow-to-transport/energy/particle linkage) and supports non-rectangular discretization (DISV vertex-based, DISU fully unstructured).
Today: Grid-type and exchange support presumed present given 12 GWF/GWT templates exist, but LGR-specific and DISU-specific templates not confirmed by roster note.
Aspects: local grid refinement (LGR) between parent/child GWF models with MVR+SFR cascading; fully unstructured (DISU) grid for irregular/pinched geometries; GWF-GWT/GWF-GWE simultaneous multi-model coupling (covered under GWT/GWE modules above)
- [CAND-L] `gwfgwf_lgr_mvr_sfr_cascading_streamflow` [L] [US] - Does a locally-refined child grid correctly exchange both groundwater flow AND MVR-routed streamflow (SFR reach-to-reach cascading) with its parent grid?
  src: https://modflow6-examples.readthedocs.io/en/master/_examples/ex-gwf-lgr.html (modflow6-examples:ex-gwf-lgr)
  knobs: GWF-GWF exchange EXGTYPE, MVR PACKAGES across parent/child, SFR reach connectivity
- [CAND-L] `disu_fully_unstructured_radial_grid` [L] [US] - Can a fully-unstructured (DISU) connectivity-list grid represent a radial or pinched-out geometry that DIS/DISV cannot, and solve correctly?
  src: https://modflow6-examples.readthedocs.io/en/latest/ (modflow6-examples:ex-gwf-rad-disu)
  knobs: IAC/JA connectivity arrays, CL1/CL2/HWVA

Roster gaps (MODFLOW 6 (USGS MF6, all model types: GWF, GWT, GWE, PRT)): All source_url values above were WebFetch-verified live and content-checked during this run, EXCEPT the DISU radial-grid candidate: I could only re-confirm 'ex-gwf-rad-disu' exists via a prior WebFetch summary of the introduction/DISU index page (not fetched at its own dedicated example-page URL, which I could not locate/verify directly this session) -- treat that one source_url as an index-page placeholder needing a direct per-example URL confirmation before use. Two additional gaps: (1) HFB has no dedicated modflow6-examples worked example with a published comparison -- only the bare MF6 input-guide reference page was verifiable live, so the HFB candidate is knob-level (S effort) rather than a full recipe; if NATE wants an HFB template with published expected output, none currently exists in the official example roster and one would need to be authored from a textbook case (e.g., Hatari Labs tutorial, unverified/non-canonical) or left as a documented gap. (2) ex-gwf-maw-p01a (the specific URL guessed from the roster note's naming pattern) 404'd; the correct live URLs are ex-gwf-maw-p01/p02/p03 (no 'a' suffix) -- corrected above. I did not attempt to verify GWF-RCH/RCHA, EVT/EVTA, or basic WEL/RIV/GHB standalone example URLs individually (treated as already covered by the roster note's '12 existing templates'); if TRID3NT's actual template inventory doesn't include RCHA (time-array recharge) or EVTA (time-array ET) as distinct knobs, that would be a real gap worth a follow-up audit rather than a guess here.


## SFINCS (9 modules)

### SFINCS core solver (SSWE numerics)
Purpose: The base 2D super-fast shallow water equations solver: continuity + momentum with simplified advection, controlling stability/speed/accuracy tradeoffs shared by every SFINCS run regardless of grid or subgrid mode.
Today: TRID3NT runs core SFINCS as the base of the core+subgrid+quadtree stack per roster note; explicit exposure of advection-scheme/theta/viscosity as user-facing knobs is unconfirmed (unknown -- likely solver defaults only).
Aspects: advection scheme selection (upw1 vs legacy 'original'); momentum smoothing (theta implicitness); horizontal viscosity (nuvisc); friction/roughness zonation (uniform / land-sea / per-cell Manning n); numerical stability limiters (huthresh, alpha Courant, advlim)
- [FOLDED-ALREADY ADR 0152 -> sfincs_advanced_numerical_physics_knobs(advection)] `advection_scheme_toggle` [S] [US] - Should momentum advection use the current default upw1 scheme or the legacy 'original' scheme (Leijnse et al. 2021) for backward-compatible runs?
  disposition: SFINCS v2.3.3 exposes advection only as 0 (SFINCS-LIE local-inertial) or 1 (SFINCS-SSWE, the upw1 default); there is NO separate 'original'-scheme keyword in the binary/hydromt-sfincs 1.2.2 surface (SfincsInput has no `advection_scheme` attr). The advection 0/1 toggle IS the exposed scheme selector, already a first-class knob.
  src: https://sfincs.readthedocs.io/en/latest/parameters.html (sfincs-parameters-advection)
  knobs: advection (0/1), scheme=upw1|original
- [LANDED ADR 0152 -> sfincs_advanced_numerical_physics_knobs(manning_land, manning_sea)] `manning_roughness_zonation_mode` [S] [US] - Should Manning's n be spatially uniform, land/sea-differentiated, or fully spatially varying per cell?
  disposition: the three zonation modes are all reachable on the signed surface -- PER-CELL is the default (NLCD reclass -> setup_manning_roughness datasets_rgh), LAND/SEA is manning_land != manning_sea, UNIFORM is manning_land == manning_sea. The manning_land/manning_sea constant fallbacks were in physics_registry + emitted by both deck builders already; this fold SURFACES them as opt-in knobs on the numerical-physics template (added to _KNOB_KEYS + params). Live smoke: manning_land=0.06 / manning_sea=0.025 written verbatim to sfincs.inp, solve status=ok.
  src: https://sfincs.readthedocs.io/en/latest/input.html (sfincs-input-friction)
  knobs: manning, manning_land, manning_sea, manningfile
- [FOLDED-ALREADY ADR 0152 -> sfincs_advanced_numerical_physics_knobs(theta)] `momentum_smoothing_theta_tuning` [S] [US] - Does this domain need reduced implicitness (theta between 0.8-1.0) to damp instabilities on steep/complex bathymetry?
  disposition: theta was already a first-class knob on the numerical-physics template (registry key theta, range 0.8-1.0, emitted verbatim to sfincs.inp:theta). No change needed.
  src: https://sfincs.readthedocs.io/en/latest/parameters.html (sfincs-parameters-theta)
  knobs: theta (0.8-1.0, default 1.0)
- [LANDED ADR 0152 -> sfincs_advanced_numerical_physics_knobs(viscosity, nuvisc)] `horizontal_viscosity_smoothing` [S] [US] - Should horizontal eddy viscosity smoothing be enabled (viscosity=1, nuvisc) to stabilize flow around sharp topographic gradients?
  disposition: added viscosity (0/1) + nuvisc (m2/s) to physics_registry['sfincs'] and to both _emit_physics_config copies (server sfincs_builder.py + vendored _sfincs_build/deck.py) via the setup_config passthrough; surfaced on the numerical-physics template _KNOB_KEYS. viscosity IS a hydromt-sfincs 1.2.2 SfincsInput attr; nuvisc is NOT but rides SfincsInput.from_dict verbatim into sfincs.inp. Live smoke: viscosity=1 + nuvisc=0.02 written verbatim to sfincs.inp, SFINCS v2.3.3 solve status=ok + depth COG.
  src: https://sfincs.readthedocs.io/en/latest/parameters.html (sfincs-parameters-viscosity)
  knobs: viscosity (0/1), nuvisc (default 0.01)

### Subgrid
Purpose: Pre-computes high-resolution elevation/roughness/river data into per-cell lookup tables so wet/dry hypsometry and conveyance are captured at fine resolution while fluxes are solved on a coarser computational grid -- the main SFINCS speed/accuracy lever.
Today: TRID3NT already surfaces subgrid as part of the core+subgrid+quadtree 4 S-tier knob templates signed.
Aspects: subgrid table generation from DEM + land-use roughness; river centerline burn-in (bathymetry/width/manning); flux-table weighting method (q_table_option legacy vs improved); subgrid-specific stability limiters (uvmax, hmin_cfl, uvlim, slopelim, wiggle_suppression); netcdf vs legacy binary subgrid file format
- [CAND-M] `river_centerline_burn_in_subgrid` [M] [US] - Should river centerlines (with depth/width/manning attributes) be burned into the subgrid elevation/roughness tables before running, for domains where the DEM under-resolves channel conveyance?
  src: https://raw.githubusercontent.com/Deltares/hydromt_sfincs/main/examples/1_build_from_script.ipynb (hydromt-sfincs-build-from-script-subgrid-river)
  knobs: sf.subgrid.create(datasets_riv=[...])
- [CAND-M] `landuse_lulc_roughness_reclass_subgrid` [M] [US] - Should subgrid roughness come from a land-use/land-cover raster reclassified to Manning's n (e.g. VITO 2015) rather than a uniform land/sea manning value?
  src: https://raw.githubusercontent.com/Deltares/hydromt_sfincs/main/examples/1_build_from_script.ipynb (hydromt-sfincs-build-from-script-subgrid-lulc)
  knobs: sf.subgrid.create(roughness_list=[{'lulc':..., 'reclass_table':...}])
- [STOP ADR 0152] `subgrid_stability_limiter_tuning` [S] [US] - For numerically stiff subgrid domains (thin channels, steep slopes), should uvmax/hmin_cfl/uvlim/slopelim/wiggle_suppression be tightened from defaults to avoid CFL-driven blowups?
  disposition: BLOCKED on our pinned stack. The cited knob names (uvmax/hmin_cfl/uvlim/slopelim/wiggle_suppression) are from the hydromt-sfincs STABLE-docs newer API; our pinned hydromt_sfincs 1.2.2 setup_subgrid signature exposes only {buffer_cells, nlevels, nbins, nr_subgrid_pixels, nrmax, max_gradient, z_minimum, huthresh, q_table_option, weight_option} -- none of uvmax/uvlim/slopelim/wiggle, and SfincsInput has no uvmax/uvlim/advlim runtime attr. RECIPE: (a) verify the exact runtime keyword the SFINCS v2.3.3 binary honors (read the sfincs-cpu source or a v2.3.3 sfincs.inp reference), then fold as a setup_config passthrough via _emit_physics_config; OR (b) upgrade hydromt_sfincs to the version whose setup_subgrid exposes these, then thread as BuildOptions -> setup_subgrid kwargs. max_gradient/z_minimum ARE available today but are a DIFFERENT concept (subgrid-table build slope limiter, not the runtime CFL limiter the row asks for) -- folding them under this row's name would mislabel the capability, so held.
  src: https://deltares.github.io/hydromt_sfincs/stable/_generated/hydromt_sfincs.SfincsModel.setup_subgrid.html (hydromt-sfincs-setup-subgrid-stability)
  knobs: uvmax, hmin_cfl, uvlim, slopelim, max_gradient, z_minimum, huthresh
- [STOP ADR 0152] `subgrid_qtable_weighting_method` [S] [US] - Should the subgrid flux lookup table use the legacy (q_table_option=1) or improved SFINCS>=2.1.1 (q_table_option=2, default) weighting method?
  disposition: NOT blocked (q_table_option + weight_option ARE confirmed kwargs in the pinned hydromt_sfincs 1.2.2 setup_subgrid signature, default 2/'min'), but DEFERRED under the lean bar: it is a low-value backward-compat method-comparison knob (the default 2 is already the improved method), only bites enable_subgrid=True runs, and a full fold would require mirroring BuildOptions field + build_options_from_dict + setup_subgrid emit into the ON-HOLD vendored cloud deck (_sfincs_build/deck.py, un-through-image-verifiable while s2z is on hold) plus a dedicated subgrid live-smoke. RECIPE (trivial, ~15 lines): add subgrid_q_table_option:int|None + subgrid_weight_option:str|None to BuildOptions (both copies) + build_options_from_dict (deck.py); in the setup_subgrid emit (both copies, after nr_subgrid_pixels) append `q_table_option: {v}` / `weight_option: {v}` when set; add composer params on model_flood_scenario threading to BuildOptions; verify with an enable_subgrid=True smoke.
  src: https://deltares.github.io/hydromt_sfincs/stable/_generated/hydromt_sfincs.SfincsModel.setup_subgrid.html (hydromt-sfincs-setup-subgrid-qtable)
  knobs: q_table_option (1|2), weight_option (min|mean)

### Quadtree grid
Purpose: Flexible non-uniform mesh: finer cells where resolution matters (urban core, floodplain edges), coarser offshore/upland, cutting total cell count relative to an equivalent regular grid at the finest resolution.
Today: TRID3NT already surfaces quadtree as part of the core+subgrid+quadtree 4 S-tier knob templates signed.
Aspects: grid type selection at build time (regular vs quadtree); quadtree + subgrid combination; quadtree netcdf map output for QGIS-native visualization (v2.4.0); refinement-level/polygon specification (mechanism not fully documented in fetched pages -- see roster_gaps)
- [FOLDED-ALREADY ADR 0152 -> sfincs_flood(quadtree=True)] `grid_type_regular_vs_quadtree` [S] [US] - Should the model grid be built as a uniform regular grid or a quadtree grid (grid_type='quadtree') for domains with mixed resolution needs?
  disposition: the quadtree-vs-regular grid selection is already a first-class composer flag -- sfincs_flood(quadtree=False|True). quadtree=True routes the deck build through build_sfincs_quadtree_deck (cht_sfincs, per the earlier mesh waves) with coast/refine-region refinement; quadtree=False is the uniform regular grid. No change needed. (Runtime note: the quadtree deck build path depends on the cht_sfincs/grace2-sfincs env; the canary in this wave was a regular-grid pluvial run.)
  src: https://deltares.github.io/hydromt_sfincs/stable/_generated/hydromt_sfincs.SfincsModel.setup_grid_from_region.html (hydromt-sfincs-setup-grid-from-region-quadtree)
  knobs: grid_type='regular'|'quadtree', res, rotated
- [STOP ADR 0152 -- HIGH PRODUCT VALUE] `quadtree_mesh_netcdf_qgis_output` [S] [US] - Should sfincs_map.nc be written/read as native quadtree mesh netcdf (outputformat=1) for direct QGIS mesh-layer loading, instead of resampled regular-gridded netcdf (0, default)?
  disposition: emitting outputformat=1 is a one-line setup_config passthrough (outputformat IS a SfincsInput attr), but the ROW'S product value -- a QGIS-NATIVE MESH LAYER -- is NOT delivered by that alone. The quadtree solve already writes a UGRID face-indexed sfincs_map.nc (postprocess_sfincs._is_quadtree_output / _read_face_coords already probe + read it), but the postprocess immediately RASTERIZES the faces to a regular-grid depth COG (_rasterize_face_field) -- the resampled deliverable the row wants to replace. Landing the QGIS-native-mesh value requires a NEW deliverable on the publish side: carry the mesh netcdf through to a QGIS mesh layer (MDAL/UGRID) instead of (or alongside) the rasterized COG, plus the plugin dock loading it as a mesh layer. This is deliverable/publish-plumbing, not a solver knob -- strongly aligned with the QGIS-only product doctrine (native variable-resolution mesh visualization). RECIPE: (1) fold outputformat as a quadtree-path setup_config key; (2) add a mesh-layer deliverable in postprocess/publish that ships the UGRID sfincs_map.nc as an MDAL mesh layer; (3) dock support to load + style it. Queue as a product-facing mesh-deliverable job (not an S-tier knob).
  src: https://sfincs.readthedocs.io/en/latest/developments.html (sfincs-developments-quadtree-qgis-output)
  knobs: outputformat (0|1)
- [CAND-M] `quadtree_subgrid_combination` [M] [US] - Should subgrid tables be generated directly on a quadtree grid (rather than a regular grid) to combine flexible-mesh cell-count savings with subgrid sub-cell hypsometry accuracy?
  src: https://sfincs.readthedocs.io/en/latest/input.html (sfincs-input-quadtree-subgrid-combo)
  knobs: grid_type=quadtree + sf.subgrid.create(...)

### SnapWave
Purpose: Integrated stationary/implicit nearshore wave transformation solver coupled into SFINCS, propagating offshore wave conditions to the nearshore to estimate incident-wave-induced setup and (in newer versions) infragravity wave energy for compound flood/wave-driven flooding.
Today: Unknown/not yet surfaced -- roster note lists TRID3NT today as core+subgrid+quadtree only; no SnapWave-specific tool or hydromt setup method was found (see roster_gaps).
Aspects: wave refraction and shoaling (core); depth-induced wave breaking (Baldock et al. 1998, tunable gamma); bottom friction (Collins 1972, space-varying); directional spreading resolution; incident wave setup vs infragravity (IG) wave energy balance; wave-force toggle (snapwave_waveforces_factor)
- [CAND-M] `wave_breaking_gamma_tuning` [M] [US] - Should the Baldock et al. (1998) depth-induced breaking parameter (gamma) be tuned per site (e.g. steeper reef vs sandy beach) rather than left at default?
  src: https://gmd.copernicus.org/articles/18/9469/2025/ (gmd-snapwave-2025-breaking)
  knobs: snapwave breaker gamma
- [CAND-M] `reef_bottom_friction_high_roughness_case` [M] [non-US] - For coral-reef-lined coasts, should SnapWave bottom friction use a space-varying high-roughness field (as in the Ningaloo Reef validation case) instead of a uniform default?
  src: https://gmd.copernicus.org/articles/18/9469/2025/ (gmd-snapwave-2025-ningaloo-friction)
  knobs: snapwave bottom friction (Collins 1972) roughness field
- [STOP ADR 0152] `incident_wave_setup_toggle` [S] [US] - Should incident-wave-induced setup/forcing be included or disabled (snapwave_waveforces_factor=0) for a run where only surge/riverine drivers matter?
  disposition: BLOCKED -- SnapWave wave FORCING does not yet ship. The signed SFINCS surface (sfincs_builder.py / _sfincs_build/deck.py) emits NO snapwave.bnd / snapwave.bhd / wave-spectra / snapwave_waveforces_factor; flood.py explicitly documents "the SnapWave wave coupling is a documented follow-up (the quadtree grid already carries the snapwave_mask)". A toggle over a wave-force that is never applied would be an inert no-op knob (Invariant 7). RECIPE: land the SnapWave-forced deck first (the Hurricane Michael/Mexico Beach lineage -- offshore wave boundary spectra -> snapwave.bnd + the wave-force coupling term), THEN snapwave_waveforces_factor becomes a real 0/1 toggle folded via _emit_physics_config. See the SnapWave CAND-M rows (wave_breaking_gamma_tuning, us_coastal_snapwave_boundary_case) which gate the same machinery.
  src: https://sfincs.readthedocs.io/en/latest/developments.html (sfincs-developments-snapwave-waveforces-toggle)
  knobs: snapwave_waveforces_factor (0 disables)
- [CAND-L] `us_coastal_snapwave_boundary_case` [L] [US] - Replicating the paper's St Croix (US Virgin Islands) open-boundary case: can SnapWave be driven with CDIP buoy / open boundary wave spectra to reproduce the published nearshore skill scores as a US-applicable canonical validation case?
  src: https://gmd.copernicus.org/articles/18/9469/2025/ (gmd-snapwave-2025-stcroix)
  knobs: open wave boundary spectra, snapwave.bnd

### Infiltration methods
Purpose: Rainfall-runoff loss modeling: converts gross precipitation into net rainfall-runoff by removing water absorbed into the soil, only active when precipitation forcing is applied.
Today: Unknown -- not listed among the 4 signed S-tier knob templates (core+subgrid+quadtree); no infiltration-specific TRID3NT tool confirmed.
Aspects: spatially uniform constant-in-time (qinf); spatially varying constant-in-time (qinffile); SCS Curve Number without recovery (setup_cn_infiltration); SCS Curve Number with soil-saturation recovery (setup_cn_infiltration_with_ks); Green-Ampt (native file-based only); Horton (native file-based only); antecedent moisture condition (dry/avg/wet)
- [LANDED] `cn_infiltration_from_gridded_curve_number` (SUBSUMED - capability shipped in code before the row was written; struck per the M/L audit + NATE approval 2026-08-06) [M] [US] - Should net rainfall be derived from a gridded global Curve Number dataset (e.g. gcn250) with a chosen antecedent moisture condition, rather than a single uniform qinf value?
  src: https://deltares.github.io/hydromt_sfincs/stable/_generated/hydromt_sfincs.SfincsModel.setup_cn_infiltration.html (hydromt-sfincs-setup-cn-infiltration)
  knobs: cn source, antecedent_moisture=dry|avg|wet, reproj_method
- [CAND-M] `cn_infiltration_with_recovery_ks` [M] [US] - For long-duration or multi-event storms, should Curve Number infiltration include soil-saturation recovery (setup_cn_infiltration_with_ks, using saturated hydraulic conductivity) instead of the no-recovery CN method?
  src: https://deltares.github.io/hydromt_sfincs/latest/_generated/hydromt_sfincs.SfincsModel.setup_cn_infiltration_with_ks.html (hydromt-sfincs-setup-cn-infiltration-with-ks)
  knobs: ks (saturated hydraulic conductivity), scsfile+smaxfile+sefffile+ksfile
- [CAND-M] `green_ampt_native_infiltration` [M] [US] - Should Green-Ampt physically-based infiltration (ksfile/sigmafile/psifile) be used instead of empirical CN, for sites with known soil hydraulic properties?
  src: https://sfincs.readthedocs.io/en/latest/input.html (sfincs-input-green-ampt)
  knobs: ksfile, sigmafile, psifile
- [CAND-M] `horton_native_infiltration` [M] [US] - Should time-decaying Horton infiltration (f0/fc/kd) be used to capture the initial-high-rate-decaying-to-steady-state infiltration curve, e.g. for short intense convective storms?
  src: https://sfincs.readthedocs.io/en/latest/input.html (sfincs-input-horton)
  knobs: f0file, fcfile, kdfile
- [LANDED ADR 0152 -> sfincs_flood(infiltration_constant_mm_per_hr)] `spatially_uniform_constant_infiltration` [S] [US] - For a quick screening run, should a single spatially-uniform constant infiltration rate (qinf) be used instead of a gridded method?
  disposition: both deck builders ALREADY emitted a bare InfiltrationForcing.constant_mm_per_hr as sfincs.inp:qinf, but the composer surfaced ONLY the gridded GCN250 path (infiltration: bool|str). Added a model_flood_scenario param infiltration_constant_mm_per_hr:float|None that builds InfiltrationForcing(constant_mm_per_hr=v), mutually exclusive with the GCN250 path (typed INFILTRATION_METHOD_CONFLICT error). Threaded through both sfincs_flood + assess_flood_impact. Live smoke: infiltration_constant_mm_per_hr=5.0 -> qinf = 5.0 written verbatim to sfincs.inp, SFINCS v2.3.3 solve status=ok + depth COG.
  src: https://deltares.github.io/hydromt_sfincs/stable/api.html (hydromt-sfincs-setup-constant-infiltration)
  knobs: qinf (mm/hr)

### Structures (thin dams and weirs)
Purpose: Line-element features (dikes, dunes, floodwalls, levees, impermeable barriers) explicitly represented as sub-grid-scale flow barriers/overflow structures rather than relying on DEM resolution alone.
Today: Unknown -- not among the 4 signed S-tier knob templates; no structures-specific TRID3NT tool confirmed.
Aspects: thin dam (infinite wall, fully blocks flow); weir/levee (finite crest elevation, submerged-flow overtopping with discharge coefficient); elevation source for weir crest (from geometry z, DEM+offset dz, or explicit value); drainage structures (culverts/pumps, distinct component)
- [CAND-M] `weir_levee_from_dem_derived_crest` [M] [US] - Should a levee/floodwall line be added as a SFINCS weir with crest elevation auto-derived from the DEM plus an offset (dz), when survey crest heights aren't available?
  src: https://raw.githubusercontent.com/Deltares/hydromt_sfincs/main/examples/3_update_geometries.ipynb (hydromt-sfincs-update-geometries-weir-dz)
  knobs: sf.weirs.create(locations=..., dz=...), discharge coefficient par1 (default 0.6)
- [CAND-M] `thin_dam_flow_barrier` [M] [US] - Should an impermeable linear barrier (seawall, floodwall with no overtopping) be modeled as a thin dam (fully blocks flow) rather than a weir?
  src: https://sfincs.readthedocs.io/en/latest/input_structures.html (sfincs-input-structures-thin-dam)
  knobs: sf.thin_dams.create(locations=...)
- [STOP ADR 0152] `weir_discharge_coefficient_tuning` [S] [US] - Should the weir discharge coefficient (par1/cd, default 0.6) be adjusted per structure type (sharp-crested vs broad-crested levee)?
  disposition: BLOCKED -- SFINCS structures (weirs / thin dams) do not ship. Neither deck builder emits sfincs.weir / sfincs.thd or a setup_structures step; there is no weir-geometry ingestion on the signed SFINCS surface (grep weir/thin_dam/setup_structures = 0 across workflows/sfincs + mesh). par1/cd is a per-weir-segment attribute, so it cannot exist before the weir line-element itself. RECIPE: land the SFINCS structures family first (the CAND-M rows weir_levee_from_dem_derived_crest + thin_dam_flow_barrier -- weir/thin-dam line ingestion via sf.weirs.create / sf.thin_dams.create -> sfincs.weir/sfincs.thd), THEN par1/cd folds as a per-segment attribute knob on that ingestion.
  src: https://sfincs.readthedocs.io/en/latest/input_structures.html (sfincs-input-structures-cd)
  knobs: par1/cd per weir segment (default 0.6)

### Precipitation forcing
Purpose: Direct rainfall-runoff driver for pluvial/compound flooding, feeding infiltration and triggering runoff generation across the domain.
Today: Unknown -- not among the 4 signed S-tier knob templates.
Aspects: spatially uniform time series; spatially varying gridded (Delft3D ASCII amprfile / netCDF); reanalysis-driven gridded precip (e.g. ERA5 hourly); interpolation mode for gridded rates (ampr_block step-hold vs linear)
- [LANDED] `reanalysis_gridded_precip_forcing` (SUBSUMED - capability shipped in code before the row was written; struck per the M/L audit + NATE approval 2026-08-06) [M] [US] - Should the model be forced with gridded hourly reanalysis precipitation (e.g. ERA5) over the domain rather than a single uniform rain gauge time series?
  src: https://raw.githubusercontent.com/Deltares/hydromt_sfincs/main/examples/2_update_forcing.ipynb (hydromt-sfincs-update-forcing-precip-era5)
  knobs: sf.precipitation.create(precip='era5_hourly', aggregate=False)
- [FOLDED-ALREADY ADR 0152 -> sfincs_flood setup_precip_forcing] `uniform_precip_timeseries_forcing` [S] [US] - For a quick single-gauge or design-storm run, should spatially uniform precipitation (sfincs.prcp, mm/hr time series) be used?
  disposition: spatially-uniform precip IS the DEFAULT pluvial forcing already -- both deck builders emit setup_precip_forcing with a uniform magnitude (mm/hr) derived from either the Atlas-14 design storm (return_period_yr/duration_hr) or an observed-raster area-mean netamt, projected onto the model time grid. The row's mechanism (uniform precip via setup_precip_forcing) is the base capability. (Residual: the current uniform forcing is a constant-magnitude hyetograph, not a multi-point time-varying series; a true time-varying uniform hyetograph, sfincs.prcp with N (time, mm/hr) rows, would be a follow-up on the same setup_precip_forcing seam -- noted, not blocking this row's uniform-precip disposition.)
  src: https://sfincs.readthedocs.io/en/latest/input_forcing.html (sfincs-input-forcing-precip-uniform)
  knobs: precipfile (time, mm/hr)
- [STOP ADR 0152] `gridded_precip_interpolation_mode` [S] [US] - Should gridded precipitation rates be treated as block-constant per timestep (ampr_block=1, default) or linearly interpolated between timesteps (ampr_block=0)?
  disposition: BLOCKED -- GRIDDED precip (amprfile / setup_precip_forcing_from_grid) does not yet ship. The signed surface collapses an observed precip raster to a SINGLE area-mean netamt magnitude (uniform), with the spatially-varying gridded path documented as the future spw/from_grid upgrade (sfincs_builder OQ-6 note). ampr_block ONLY governs the temporal interpolation of a gridded amprfile, so it is inert without gridded forcing (Invariant 7). RECIPE: land setup_precip_forcing_from_grid (the reanalysis_gridded_precip_forcing CAND-M row -- ERA5/MRMS 2D precip -> SFINCS amprfile/precip_2d.nc) first, THEN ampr_block (0/1) folds as a setup_config passthrough via _emit_physics_config (ampr_block is not a hydromt SfincsInput attr but rides from_dict verbatim, same pattern as nuvisc).
  src: https://sfincs.readthedocs.io/en/latest/input_forcing.html (sfincs-input-forcing-ampr-block)
  knobs: ampr_block (0|1)

### Wind forcing
Purpose: Surface wind stress driver for storm surge setup and wind-driven wave/current effects, spanning uniform steady winds to full tropical-cyclone vortex fields.
Today: Unknown -- not among the 4 signed S-tier knob templates.
Aspects: spatially uniform time series; spatially varying gridded (amu/amv Delft3D ASCII or netCDF); spiderweb tropical cyclone vortex fields (ASCII .spw or netCDF); wind-speed-dependent drag coefficient curve
- [LANDED] `spiderweb_tropical_cyclone_wind_forcing` (SUBSUMED - capability shipped in code before the row was written; struck per the M/L audit + NATE approval 2026-08-06) [L] [US] - For a hurricane/tropical storm scenario, should wind (and implied pressure) be forced from a spiderweb vortex file (track + radii of max wind) rather than a uniform or gridded field?
  src: https://sfincs.readthedocs.io/en/latest/input_forcing.html (sfincs-input-forcing-spiderweb)
  knobs: spwfile / netspwfile
- [LANDED] `gridded_reanalysis_wind_forcing` (SUBSUMED - capability shipped in code before the row was written; struck per the M/L audit + NATE approval 2026-08-06) [M] [US] - Should the model be forced with a gridded time-varying wind field (amu/amv or netCDF) instead of a single uniform wind vector?
  src: https://sfincs.readthedocs.io/en/latest/input_forcing.html (sfincs-input-forcing-wind-gridded)
  knobs: amufile, amvfile, netamuamvfile
- [LANDED] `wind_drag_coefficient_curve_tuning` [S] [US] - Should the wind drag coefficient curve (cd_nr breakpoints of cd_wnd vs cd_val) be adjusted from SFINCS defaults for extreme-wind (hurricane-force) conditions where drag saturates/decreases? [LANDED 2026-08-06 (ADR 0162): a new `wind_drag_curve` knob in `physics_registry['sfincs']` (type `"float_pairs"`, a new registry-validated shape: an ordered list of >=2 `(wind_speed_mps, drag_coefficient)` pairs with strictly-increasing wind breakpoints, range-checked per column) alongside the existing flat `wind_drag`. `_emit_physics_config` (sfincs_builder.py) writes it as `cdnrb: len(curve)` / `cdwnd: [...]` / `cdval: [...]` into the setup_config passthrough -- the SAME sfincs.inp keys ADR 0152's flat `wind_drag` already proved (`SfincsInput.__init__` defaults `cdnrb=3, cdwnd=[0,28,50], cdval=[0.001,0.0025,0.0015]`, confirmed live against hydromt_sfincs 1.2.2). Mutually exclusive with `wind_drag` (both target the same keys); setting both raises typed `SFINCSSetupError("WIND_DRAG_CURVE_CONFLICT")` rather than a silent last-key-wins overwrite. Surfaced on the `sfincs_advanced_numerical_physics_knobs` template's `_KNOB_KEYS` + params (same fold pattern as ADR 0152's `viscosity`/`nuvisc`). Unset = byte-identical. Live docker smoke (downtown Chattanooga TN, stock `deltares/sfincs-cpu:sfincs-v2.3.3`, no image rebuild needed): sfincs.inp carries `cdnrb = 3`, `cdwnd = 0.0 28.0 50.0`, `cdval = 0.001 0.0025 0.0018` verbatim (run_id 01KZC2V0MGNV2CZJT0QWW1ZQXV).]
  src: https://sfincs.readthedocs.io/en/latest/input.html (sfincs-input-wind-drag-curve)
  knobs: cd_nr, cd_wnd[], cd_val[]
- [LANDED] `uniform_wind_timeseries_forcing` [S] [US] - For a simple screening run, should a single uniform wind speed+direction time series (sfincs.wnd) be used? [LANDED 2026-08-06 (ADR 0162): `WindForcing` gains an optional `timeseries` field (an ordered list of `(t_s, magnitude_mps, direction_deg)` tuples, seconds since sim-start) alongside the existing constant `magnitude`/`direction` pair. `_emit_surge_forcing_blocks` (sfincs_builder.py) materialises the schedule to a CSV (`_write_wind_timeseries_csv`, absolute timestamps off the module's `SFINCS_TREF`) staged in the SAME per-build temp dir as the rest of the deck, then calls `setup_wind_forcing(timeseries=<path>)` -- the live hydromt_sfincs 1.2.2 signature (`timeseries=None, magnitude=None, direction=None`) already accepts a tabulated CSV via `data_catalog.get_dataframe(parse_dates=True, index_col=0)`; hydromt's own `SfincsModel.write_forcing` then writes the native `sfincs.wnd` ASCII file (seconds-since-tref, mag, dir columns) -- the SAME `wndfile` artifact the constant-wind path already produced. Threaded end-to-end: `sfincs_flood`/`model_flood_scenario`'s `wind` dict param + `_build_surge_forcing_members` (sfincs_forcing_autowire.py) accept a `{"timeseries": [...]}` sub-key. Precedence: grid > schedule > constant; a `None` timeseries leaves the constant path byte-identical (test-locked). Live docker smoke (downtown Chattanooga TN, stock `deltares/sfincs-cpu:sfincs-v2.3.3` image, no worker code changes so no image rebuild needed): a 10 m/s@270deg -> 25 m/s@180deg ramping/veering 2-hour schedule round-tripped byte-exact through the staged `sfincs.wnd` (`0.0 10.00 270.00 / 3600.0 17.50 225.00 / 7200.0 25.00 180.00`), sfincs.inp carries `wndfile = sfincs.wnd`, status=ok, depth COG published (run_id 01KZC2V0MGNV2CZJT0QWW1ZQXV). Chart proof docs/proof/templates/sfincs_advanced_numerical_physics_knobs_wind_schedule.png (magnitude + direction vs time, two panels).]
  src: https://deltares.github.io/hydromt_sfincs/stable/api.html (hydromt-sfincs-setup-wind-forcing-uniform)
  knobs: sf.wind... timeseries (time, magnitude m/s, direction deg)

### Wavemaker (infragravity boundary forcing)
Purpose: Absorbing-generating wave boundary condition (van Dongeren and Svendsen, 1997) that injects the rapidly-varying infragravity/short-wave water-level component at the offshore boundary, coupling nearshore SnapWave/wave-driven setup into the SFINCS shallow-water solver.
Today: Unknown/not yet surfaced -- no wavemaker-specific TRID3NT tool confirmed; SnapWave IG coupling itself is not yet exposed per roster note.
Aspects: incident infragravity wave signal specification (bzifile); absorbing vs pure-generating boundary behavior; coupling to SnapWave-derived IG wave energy vs externally supplied spectra; legacy wavemaker variable naming (renamed with backward-compat aliases)
- [CAND-M] `infragravity_wavemaker_boundary_signal` [M] [US] - Should an incoming infragravity/short-wave water-level signal (zero-mean bzi time series) be injected at the offshore boundary via the absorbing-generating wavemaker, rather than a flat water-level-only boundary?
  src: https://sfincs.readthedocs.io/en/latest/input_forcing.html (sfincs-input-forcing-bzi-wavemaker)
  knobs: bzifile (time, ig water-level component, must average ~0)
- [CAND-L] `snapwave_ig_energy_coupled_wavemaker` [L] [US] - Should the wavemaker boundary be driven by SnapWave's own infragravity energy balance (Leijnse et al. 2024 upgrade) rather than an externally supplied bzi time series, for a fully-coupled offshore-to-nearshore-to-inland wave-driven flood run?
  src: https://sfincs.readthedocs.io/en/latest/developments.html (sfincs-developments-snapwave-ig-wavemaker-coupling)
  knobs: snapwave IG energy balance -> wavemaker_wvmfile

Roster gaps (SFINCS): hydromt_sfincs docs are mid-migration between two API generations: /stable/api.html and its _generated/*.html pages (WebFetch-verified, HTTP 200) still describe the old setup_* method signatures (setup_subgrid, setup_cn_infiltration, setup_structures, setup_wind_forcing, setup_grid_from_region), while the live example notebooks on main (raw.githubusercontent.com/Deltares/hydromt_sfincs/main/examples/*.ipynb, fetched and grepped directly) use the newer component API (sf.subgrid.create, sf.infiltration.create_cn, sf.weirs.create, sf.thin_dams.create, sf.precipitation.create). Both are cited below per-candidate since both are live and load-bearing; treat setup_* names as the documented contract and sf.<component>.create() as the version actually exercised in current examples. Could not confirm: (1) the exact quadtree refinement-polygon/level API (grid_type='quadtree' is confirmed live in the setup_grid_from_region docstring per WebFetch, but the mechanism for specifying variable refinement levels was not found in the fetched docs pages -- input.html and the generated setup_grid_from_region page both omit it; would need to read hydromt_sfincs/quadtree.py source or the Deltares/SFINCS docs/input.rst directly); (2) a dedicated hydromt_sfincs setup method for SnapWave -- no setup_waves/setup_snapwave method surfaced in api.html or the example notebooks, implying SnapWave forcing (snapwave.bnd, snapwave.bhd, wave spectra) is still hand-authored against sfincs.readthedocs.io/en/latest/input_forcing.html rather than HydroMT-generated, confirmed by absence rather than a broken link; (3) Green-Ampt and Horton infiltration have no hydromt_sfincs setup_*/component equivalent found (only setup_cn_infiltration / setup_cn_infiltration_with_ks / setup_constant_infiltration appear in api.html) -- they exist only as native SFINCS sfincs.inp/ascii-file options per input.html, so automating them is a new deck recipe against raw file formats, not a Python setup call.


## SWMM (11 modules)

REAL-NETWORK FAMILY LANDED (ADR 0124, 2026-08-04): the practice-verification's
#1-ranked gap (real municipal storm-sewer import as the START of a project, not
the DEM-synthesized mesh) is CLOSED. Two new engine="swmm" tier="template" tools:
- [LANDED] `swmm_network_import` [row #1] - build a runnable SWMM model from a REAL
  municipal storm-drain GIS network (nodes + conduits, any schema), design-storm
  loaded; where do the pipes surcharge/flood and how much reaches the outfall.
  Multi-source input (upload / s3 / https GeoJSON / keyless ArcGIS FeatureServer).
  LIVE smoke on a public Houston-area TX Storm_Sewer_System FeatureServer (185
  manholes + 518 mains -> 484 junctions / 473 conduits / 47.3 km pipe -> peak
  outfall 2.906 CMS, 464 flooded nodes, continuity +0.482%).
- [LANDED] `swmm_dual_drainage_coupling` [row #2] - the DEFINING dual-drainage
  feature: the overland MAJOR-system mesh EXCHANGES flow with the imported piped
  MINOR system at inlets (surface -> pipe capture; surcharging pipe -> street).
  LIVE smoke: overland depth raster + 27 inlets coupled to the real network, 34
  pipe conduits surcharged.
ROWS #3-#7 STOPPED (published-deck runner is a separate capability): the cited
LID / green-grey / CSO-regulator / WWTP-detention / PID-pump decks are PRE-BUILT
published .inp files carrying LID controls, storage curves, regulators, pumps, and
RTC rules that NEITHER the DEM-mesh builder NOR the GIS-network parser produces -
they do not consume the row-#1 machinery; queue a dedicated published-deck-runner
wave. See ADR 0124 + the wave report for per-row blockers.

### Hydrology - Subcatchment Rainfall-Runoff & Infiltration
Purpose: Convert rainfall on a subcatchment into a runoff hydrograph via the nonlinear-reservoir routing model plus a selectable infiltration method for the pervious fraction.
Today: Base subcatchment hydrology already ships as the foundation of the signed 7-template quasi-2D network family (per-DEM-cell subcatchments, Horton infiltration default per the roughness/NLCD roster note); Green-Ampt and Curve-Number infiltration methods, and a named pre/post-development comparison deck, are not yet distinguished as separate templates.
Aspects: infiltration method choice (Horton vs Green-Ampt vs Curve Number); pervious/impervious width-function routing calibration; pre-development vs post-development imperviousness comparison; depression storage and evaporation
- [LANDED ADR 0151 -> swmm_subcatchment_runoff_comparison(compare=infiltration_method)] `swmm_infiltration_method_comparison` [S] [US] - For the same subcatchment and storm, how much does the choice of infiltration method (Horton vs Green-Ampt vs Curve Number) change peak flow and total runoff volume?
  src: https://downloads.tuflow.com/SWMM/SWMM5_Reference_Manual_Volume1_Hydrology_P100NYRA.pdf (EPA SWMM5 Reference Manual Volume I - Hydrology, infiltration chapter (Horton/Green-Ampt/Curve-Number))
  knobs: infiltration method selector; Horton (MaxRate/MinRate/Decay/DryTime); Green-Ampt (Suction/Ksat/InitialMoistureDeficit); Curve Number (CN/DryTime)
  notes: Manual PDF is live-reachable but text layer is not machine-extractable via WebFetch (same tuflow-mirror limitation flagged in the 2026-08-04 quasi-2D verification pass); content taken from secondary summaries, not quoted verbatim -- see roster_gaps. Pure three-way knob swap on the existing per-cell subcatchment template, same solver wiring.
- [LANDED ADR 0151 -> swmm_subcatchment_runoff_comparison(compare=development_intensity)] `swmm_predev_postdev_runoff` [S] [US] - For the same parcel, how much more and how much faster does post-development (increased imperviousness) runoff arrive compared to pre-development (pasture/undeveloped) conditions?
  src: https://www.chiwater.com/Files/Swmm_Apps_Manual.pdf (EPA SWMM Applications Manual EPA/600/R-09/000, Example 1 (Post-Development Runoff))
  knobs: imperviousness %, subcatchment width (max overland-flow-length assumption ~500 ft for undeveloped areas), soil CN/Horton params (silt-loam pasture baseline), subcatchment count/geometry (single pre-dev vs multiple post-dev)
  notes: EPA's own canonical worked example (pasture/silt-loam pre-dev vs roadway-bounded post-dev parcel); the PDF itself did not decode via WebFetch (binary stream), content summarized from search snippets plus swmm5.org's InfoSWMM companion walkthrough of the same example, not independently verified verbatim -- see roster_gaps. Reuses the existing subcatchment template twice with different parameter sets; built-in expected-outcome check (post-dev peak/volume must exceed pre-dev).

### Hydrology - Snowmelt
Purpose: Track snowpack accumulation, redistribution, and degree-day-based melt on a subcatchment so that runoff timing reflects cold-climate seasonality rather than assuming all precipitation is rain.
Today: Not surfaced - TRID3NT's hydrology template has no Snow Pack object; all current AOIs treat precipitation as rain-only regardless of climate.
Aspects: degree-day melt coefficient; base-temperature rain/snow precipitation split; areal depletion curve (fraction of area still snow-covered); snow removal/redistribution (plowing)
- [CAND-M] `swmm_snowmelt_degree_day` [M] [US] - For a subcatchment that receives both rain and snow, how does snowpack accumulation and degree-day melt change the shape and timing of the runoff hydrograph compared to treating all precipitation as immediate rain?
  src: https://swmm5.org/2013/08/05/example-swmm-5-snowmelt-model/ ("Example SWMM 5 Snowmelt Model" - swmm5.org (CHI), attached simple sample snowmelt .inp)
  knobs: base temperature (rain/snow split), degree-day melt coefficient, areal depletion curve (pervious/impervious fraction still snow-covered), initial/final snow cover
  notes: Page states a sample .inp is attached (direct link not independently resolved from the Blogger-migrated post - flag); two output images shown but no extracted numeric plot data, so mechanism reference not a calibration target. New Snow Pack object plus a temperature time series input TRID3NT does not currently ingest -> M, not a pure knob swap.
- [CAND-M] `swmm_snow_removal_plowing` [M] [US] - When snow is actively removed/relocated (plowed) from part of a subcatchment during a storm, how does that change where and when meltwater runoff appears downstream?
  src: https://swmm5.org/2019/01/20/refactoring-the-swmm-5-help-file-snowmelt-in-swmm5/ ("Refactoring the SWMM 5 Help File - Snowmelt in SWMM5" (swmm5.org markdown re-publication of the EPA SWMM5 User's Manual snowmelt chapter))
  knobs: snow removal fraction/schedule, redistribution target area, plow-trigger snow depth
  notes: Documentation-only source; no standalone worked example or published numeric output was found (see roster_gaps) - mechanism reference, pair with swmm_snowmelt_degree_day for a runnable base deck. Adds a distinct snow-removal control block on top of the new Snow Pack object.

### Hydrology - Groundwater (Aquifer Coupling)
Purpose: Model the shallow saturated-zone aquifer beneath one or more subcatchments and its exchange of baseflow with a receiving conveyance-network node.
Today: Not surfaced - no Aquifer/Groundwater object exists in the current quasi-2D template; the per-cell storage/conduit mesh has no subsurface baseflow pathway into the drainage network, only direct surface runoff.
Aspects: aquifer-subcatchment linkage; groundwater flow-to-node equation coefficients (A1/A2/B1/B2/A3); unsaturated/saturated zone moisture balance; seasonal baseflow contribution vs surface runoff
- [CAND-M] `swmm_aquifer_baseflow_to_node` [M] [US] - How much steady baseflow does a shallow aquifer contribute to a stream/outfall node between storms, and how does adding that groundwater contribution change the node's total hydrograph versus surface runoff alone?
  src: https://swmm5.org/2013/08/10/aquifer-and-groundwater-objects-in-swmm-5/ ("Aquifer and Groundwater Objects in SWMM 5" - swmm5.org (CHI), companion post "Groundwater Equation Editor for InfoSWMM and H2OMap SWMM")
  knobs: Aquifer porosity/field capacity/wilting point/conductivity/initial water-table elevation; Groundwater flow coefficients A1 A2 B1 B2 A3; receiving-node assignment; surface-water-elevation source (node invert vs fixed)
  notes: Page content truncated mid-explanation of the A1-A3 coefficients on live fetch - flagged in roster_gaps; only the two-object-type structure (Aquifer applies across subcatchments, Groundwater is per-subcatchment) was independently confirmed. New Aquifer+Groundwater object pair and a new node-exchange pathway not present in the current per-cell mesh -> M.

### Hydrology - RDII (Rainfall-Dependent Inflow/Infiltration)
Purpose: Represent wet-weather inflow/infiltration entering a sanitary or combined sewer through defects, independent of direct subcatchment surface runoff, via the RTK triangular unit-hydrograph method.
Today: Not surfaced - no RDII/Hydrograph object exists; the quasi-2D mesh has no mechanism for wet-weather sewer inflow independent of direct subcatchment runoff.
Aspects: RTK triangular unit-hydrograph synthesis (short/medium/long-term response); sewershed I/I split from a rainfall record; antecedent-moisture/initial-abstraction parameters (Dmax/Drec/D0); calibration to an observed flow-meter record
- [CAND-M] `swmm_rdii_rtk_unit_hydrograph` [M] [US] - For a sewershed with known inflow/infiltration behavior, how much rainfall-derived flow enters the pipe network at a manhole, and how does it compare in magnitude and timing to direct subcatchment runoff at the same point?
  src: https://swmm5.org/2016/09/04/rainfall-dependent-inflow-and-infiltration-from-the-epa-swmm-5-hydrology-manual/ ("Rainfall Dependent Inflow and Infiltration" - swmm5.org (CHI markdown of EPA SWMM5 Hydrology Manual Ch.7); worked example: 10-acre catchment, node N1, 3-triangle RTK set (R sums to 0.36))
  knobs: R/T/K per short/medium/long-term unit hydrograph (up to 3 triangles), Dmax/Drec/D0 (antecedent abstraction), sewershed contributing area, rainfall time series
  notes: EPA's own worked numeric example: 10-acre area, node N1, two-storm 1-hr rainfall series (Table 7-1, 0.0-0.8 in/hr), resulting RDII interface-file flows ~0.2-1.0 cfs at 15-min steps (Figures 7-8/7-9/7-10) - a genuine replication target with published intermediate values, live-verified via WebFetch. New Hydrograph/RDII object class not in the current template -> M.
- [CAND-L] `swmm_rdii_flowmeter_calibration` [L] [US] - Given an observed flow-meter record at a manhole during wet weather, what RTK parameter set best reproduces the measured RDII response across its short/medium/long-term components?
  src: https://www.chijournal.org/R241-12 ("Comparison of RDII Unit Hydrograph Approaches for Continuous Simulation using SWMM 5" - CHI Journal R241-12)
  knobs: same RTK/Dmax/Drec/D0 parameter set as swmm_rdii_rtk_unit_hydrograph, but fit/optimized against an observed hydrograph rather than hand-specified
  notes: Peer-reviewed comparison of RTK-fitting approaches for continuous simulation - a calibration workflow, not a single knob-swap deck. Requires an optimization/fitting harness around the RDII object plus a real flow-meter time series input we do not currently fetch -> L (new capability: automated RTK parameter fitting, not just new deck parameters).

### Hydraulics - Dynamic Wave Flow Routing
Purpose: Solve the full Saint-Venant equations across the conveyance network so that backwater, surcharging, and flow reversal are physically represented rather than approximated.
Today: Signed and in active use - the 7-template quasi-2D network family already routes via SWMM's Dynamic Wave engine (confirmed in reports/design/swmm-practice-verification-2026-08-04.md as the same node-link + dynamic-wave pattern PCSWMM's proprietary '2D' mode uses under the hood); node ponding/flooding are native SWMM mechanisms already available on any STORAGE/JUNCTION node in the mesh, but not yet exercised as a distinct dedicated template.
Aspects: Saint-Venant solver stability / variable time-stepping; node surcharging and flooding; surface ponding at nodes; backwater and bidirectional flow reversal through the network
- [LANDED ADR 0151 -> swmm_node_hydraulics_comparison(scenario=surcharge_ponding)] `swmm_node_surcharge_ponding` [S] [US] - When the pipe/mesh network is undersized for a storm and backs up, where does water surcharge and pond at the surface, and how much depth/volume accumulates at each flooded node?
  src: https://downloads.tuflow.com/SWMM/SWMM5_Reference_Manual_Volume2_Hydaulics_P100S9AS.pdf (EPA SWMM5 Reference Manual Volume II - Hydraulics, node flooding / Allow Ponding chapter)
  knobs: node Allow-Ponding toggle + ponded surface area, node rim elevation/max depth, storm size (chosen to deliberately undersize the network)
  notes: Manual text layer not machine-extractable via WebFetch (same tuflow-mirror limitation as swmm_infiltration_method_comparison, reconfirmed here); ponding/flooding mechanics corroborated via secondary sources (openswmm.org forum threads) rather than quoted verbatim - see roster_gaps. Pure knob toggle (Allow Ponding on/off, oversized storm) on the existing dynamic-wave network template -> S.

### Hydraulics - Conveyance Structures (Orifices/Weirs/Outlets/Diversions)
Purpose: Model discrete regulator and outlet structures - orifices, weirs, rating-curve outlets, and flow-diversion junctions - that control how flow splits and discharges through the network.
Today: Partial - the quasi-2D mesh already uses RECT_OPEN conduits plus flap-gate orifices for one-way walls/obstructions; named weir types (V-notch, transverse, trapezoidal) and outlet rating-curve structures are not yet distinct templates.
Aspects: orifice types (side vs bottom, circular vs rectangular); weir types (transverse/side-flow/V-notch/trapezoidal); outlet rating-curve structures; flow-diversion (side-overflow) structures
- [LANDED ADR 0151 -> swmm_node_hydraulics_comparison(scenario=outlet_family)] `swmm_weir_orifice_outlet_family` [S] [US] - For an outfall or regulator structure, how does discharge behavior differ across a transverse weir, a V-notch weir, a circular orifice, and a rating-curve outlet at the same pond/node?
  src: https://swmm5.org/2018/07/03/weirs-in-swmm5-and-infoswmm/ ("Weirs in SWMM5 and InfoSWMM" - swmm5.org (CHI); companion "Orifices in InfoSWMM and SWMM5" (swmm5.org, 2018/07/04))
  knobs: structure type selector (TRANSVERSE/SIDEFLOW/V-NOTCH/TRAPEZOIDAL weir; SIDE/BOTTOM orifice; TABULAR/FUNCTIONAL outlet rating curve), crest/invert offset, discharge coefficient
  notes: Both structure families are already-native SWMM link types the current dynamic-wave solver requires no new build for; the deck work is knob-selection across structure type on an existing outfall/regulator node -> S. No numeric published results on either page - documentation/mechanism reference, not a calibration target.
- [LANDED ADR 0151 -> swmm_node_hydraulics_comparison(scenario=flow_diversion)] `swmm_flow_diversion_structure` [S] [US] - At a junction where flow needs to split between a main channel/pipe and a relief/diversion pipe (e.g. to protect a downstream WWTP or route excess to a bypass), how much flow goes each way as the incoming flow rises?
  src: https://help2.innovyze.com/infoworksicm/Content/HTML/ICM_ILCM/Control_Rules_Format%20(SWMM).htm (SWMM Control Rules Format reference (Innovyze/Autodesk mirror of the EPA SWMM5 User's Manual diversion-structure section))
  knobs: diversion weir/orifice crest elevation, downstream link capacity, diversion trigger depth
  notes: Reuses the weir/orifice family above at a specific split-junction topology rather than a single outfall - same S effort, offered as the distinct 'diversion' aspect versus the single-outfall-regulator aspect above.

### Hydraulics - Pumps & Storage Units
Purpose: Model lift stations and detention/wet-well storage: pump curves that turn on/off (or run continuously) as a function of wet-well depth, plus the storage-unit stage-area behavior they draw down.
Today: Partial - swmm_wwtp_detention_ponds (signed per reports/design/template-candidates-2026-08-03.md) already exercises STORAGE nodes with interconnecting orifices; named PUMP objects with pump curves and force-main routing are not yet a distinct template.
Aspects: pump curve types (Type1-4, on/off staging); wet-well storage-unit stage-area curves; force-main routing; multi-pump duty/standby alternation logic
- [LANDED ADR 0151 -> swmm_wetwell_pump_control_comparison(fixed-setpoint variant)] `swmm_pump_curve_wetwell` [S] [US] - For a lift station with a defined pump curve, how does wet-well depth and pump discharge respond over a storm, and at what depths do the pumps turn on and off?
  src: https://swmm5.org/2016/09/26/setting-controls-for-the-pump-and-orifice-in-infoswmm-and-swmm5/ ("Setting Controls For The Pump and Orifice in InfoSWMM and SWMM5" - swmm5.org (CHI))
  knobs: pump curve type (1-4)/table, on-depth/off-depth setpoints, wet-well storage stage-area curve, force-main length/diameter/roughness
  notes: Documentation/mechanism reference, no published numeric results. Knob-only build on the existing STORAGE-node template plus a new PUMP link type -> S, since pumps are already a native SWMM link class implied by the signed detention-pond template's routing.
- [LANDED ADR 0151 -> swmm_wetwell_pump_control_comparison(duty/standby variant)] `swmm_pump_alternation_duty_standby` [S] [US] - With three pumps sharing one wet well, how does a depth-staged duty/standby alternation rule (only one pump running at low depth, two at higher depth) change pump run-time and cycling compared to running all pumps off one fixed setpoint?
  src: https://www.openswmm.org/Topic/10083/example-vsp-control-rules-for-3-pumps ("Example VSP control rules for 3 pumps" - openswmm.org Topic 10083, downloadable "VSP rules for 3 pumps.inp" (SWMM 5.1.012, 6-hr dynamic-wave run))
  knobs: per-pump depth thresholds (<=3ft / 3-5ft / >5ft), orifice downstream-flow threshold rule (>25 cfs), simulation duration
  notes: Complete downloadable .inp verified live via WebFetch; draft page with no published results (author marks it still being edited) - mechanism template, not a calibration target. Reuses the multi-pump wet-well setup from swmm_pump_curve_wetwell plus a CONTROLS rule block -> S.

### Hydraulics - Real-Time Control (RTC)
Purpose: Dynamically adjust pumps, orifices, and weirs during a simulation based on rules or feedback control, rather than fixed static settings, to actively manage the network's response.
Today: swmm_pump_pid_rtc is signed [M] per reports/design/template-candidates-2026-08-03.md pending confirmation that PID-RTC control-rule emission is plumbed through the current pyswmm/.inp writer; simple/rule-based (non-PID) multi-condition controls and published multi-scenario RTC benchmarking are not yet templates.
Aspects: simple time/level-triggered controls; multi-condition rule-based logic (AND/OR chaining); PID feedback control; published control-objective benchmarking (baseline vs controlled performance)
- [LANDED ADR 0151 -> swmm_wetwell_pump_control_comparison(multi-condition variant)] `swmm_rtc_multicondition_rules` [S] [US] - With a rule that only opens a regulator when BOTH upstream depth is high AND downstream link flow is below a threshold, how does that multi-condition logic change regulator behavior versus a single-condition rule?
  src: https://www.openswmm.org/Topic/10083/example-vsp-control-rules-for-3-pumps (openswmm.org Topic 10083 (same source as swmm_pump_alternation_duty_standby) - the orifice rule (downstream link flow >25 cfs, chained with the depth-based pump rules) is the multi-condition-logic aspect of this same verified .inp)
  knobs: AND/OR condition chaining, condition variable (node depth, link flow, simulation time, link setting)
  notes: Same verified .inp as swmm_pump_alternation_duty_standby; listed separately because it targets the RTC-rules-authoring aspect (control logic) rather than the pump-hardware aspect -> S, pure CONTROLS-block knob work.
- [CAND-L] `swmm_rtc_cso_minimization_benchmark` [L] [US] - Using a real-world-inspired combined-sewer network with multiple regulator weirs, how much can a rule-based or optimized control policy reduce total combined-sewer-overflow volume compared to an uncontrolled (static-weir) baseline?
  src: https://github.com/kLabUM/pystorms (pystorms Scenario Alpha (kLabUM/pystorms) - 0.12 km2 residential combined-sewer network, 5 weirs at interceptor connections, control objective = minimize total CSO volume; theta/beta/gamma/delta/epsilon/zeta are companion scenarios at increasing realism (difficulty levels 1-3: ideal / sensor-noise+actuator-fault / adverse))
  knobs: control policy (rule-based vs optimized vs uncontrolled baseline), difficulty level (1-3), scenario selection (alpha/beta/gamma/delta/epsilon/zeta/theta), per-weir setting schedule
  notes: Peer-reviewed (arxiv 2110.12289, confirmed as a real published paper via ScienceDirect/ResearchGate listings; PDF itself failed to decode via WebFetch, see roster_gaps), open-source SWMM .inp networks bundled in the repo per the README, with a documented env.performance() metric and CSO-reduction control objective - the strongest RTC replication/benchmarking candidate in this roster. Requires wiring pystorms' gym-style step/performance-evaluation loop around our SWMM worker, a new control-benchmarking capability, not a knob swap -> L.

### Water Quality - Buildup/Washoff
Purpose: Accumulate pollutant mass on subcatchment surfaces during dry weather (buildup) and mobilize it into runoff during storms (washoff), producing a pollutograph/loadograph at each point in the network.
Today: Signed and largely shipped - swmm_wq_buildup_washoff_single is signed [S] (single-subcatchment exponential washoff, per reports/design/template-candidates-2026-08-03.md) and TRID3NT just landed the SWMM-WQ-1 per-pollutant concentration rescale (commit 3ba58243); multi-land-use buildup normalized by curb length, and an EMC-vs-exponential-washoff side-by-side comparison, are not yet distinct templates.
Aspects: buildup function form (power/exponential/saturation) by land use; washoff function form (exponential/rating-curve/EMC); curb-length vs area normalization of buildup; multi-event pollutograph/loadograph comparison across storm sizes
- [LANDED ADR 0151 -> swmm_wq_buildup_washoff_comparison(compare=normalization|washoff)] `swmm_wq_multiland_use_curblength_buildup` [S] [US] - Across a watershed with residential and commercial land uses, how much does normalizing pollutant buildup by curb length (instead of area) change predicted TSS loads, and how does the EMC washoff method compare to the exponential washoff method for the same storms?
  src: https://swmm5.org/2017/10/12/example-5-runoff-water-quality-for-swmm5-and-infoswmm-from-the-epa-applications-manual/ (EPA SWMM Applications Manual EPA/600/R-09/000, Example 5 (Runoff Water Quality) - swmm5.org walkthrough)
  knobs: buildup normalization basis (area vs curb-length), buildup C1 max-buildup (~62-116 lb/curb-mile/day residential vs commercial) / C2 rate constant, washoff method toggle (EMC constant 160-200 mg/L vs exponential C2 exponent 1.8-2.2), storm depth (0.1/0.23/1.0 in)
  notes: EPA's own official worked example with published multi-scenario results (EX5-EMC vs EX5-EXP compared across 3 storms, event-mean concentrations 67.7-199.2 mg/L range, pollutograph + loadograph figures) - the strongest WQ calibration/replication target found this pass; content live-verified via WebFetch, not just a search snippet. Same solver wiring as the already-signed single-subcatchment WQ template, extended to multi-land-use + curb-length basis -> S.

### Water Quality - Treatment
Purpose: Apply removal/decay of pollutant mass at storage units, conveyance nodes, or LID outlets - representing a detention pond, wetland, or treatment BMP - so downstream concentration reflects capture, not just conveyance.
Today: Not surfaced - no Treatment object exists in any signed template; pollutant routing currently stops at buildup/washoff plus conveyance, with no removal or decay applied at storage or LID outlets.
Aspects: node/storage-unit removal-efficiency equations (function of flow, depth, HRT, or concentration); first-order in-conduit decay; treatment-train sequencing (LID + storage treatment in series)
- [CAND-M] `swmm_storage_treatment_removal_function` [M] [US] - At a detention pond or storage unit, how much does using a flow/HRT-dependent removal-efficiency function (rather than a flat percent-removal assumption) change downstream pollutant concentration across storms of different sizes?
  src: https://www.openswmm.org/Topic/9833/suds-treatment-efficiency-equations ("SuDS treatment efficiency equations" - openswmm.org Topic 9833, referencing the EPA SWMM5 Reference Manual Volume III - Water Quality treatment-function chapter)
  knobs: treatment function form (removal as f(HRT), f(FLOW), f(DEPTH), f(AREA), or f(other-pollutant-removal)), per-pollutant treatment equation, node/storage-unit assignment
  notes: Forum explicitly warns flat removal rates give 'unrealistically good' results across storm sizes and recommends flow/depth-dependent functions instead - a documented pitfall worth building the template to avoid by default. No numeric worked example on the thread itself; the EPA Reference Manual Volume III markdown version (swmm5.org, 2018/11/14) is the backstop for actual equation forms but was not independently content-verified this pass - see roster_gaps. New Treatment object attached to storage/conveyance nodes, no existing wiring -> M.

### LID Controls (8-9 EPA process types)
Purpose: Represent green-infrastructure/BMP practices as a layered subsurface stack (surface/pavement/soil/storage/drain/drainmat) that intercepts a fraction of a subcatchment's runoff before it reaches the conveyance network.
Today: Partial - swmm_lid_raingarden_wq (rain garden, paired with WQ) and swmm_green_grey_infra_storms (bioretention vs dry pond) are signed [M each] per reports/design/template-candidates-2026-08-03.md; green roof, infiltration trench, permeable pavement/block pavers, rain barrel, rooftop disconnection, and vegetative swale are NOT yet distinct templates despite being named, distinct EPA LID types each with its own layer stack (per swmm5.org's LID layer-requirement table: bio-retention=Surface+Soil+opt.Storage+opt.Drain; rain garden=Surface+Soil; green roof=Surface+Soil+DrainMat; infiltration trench=Surface+Storage+opt.Drain; permeable pavement/block pavers=Surface+Pavement+Storage+opt.Drain; rain barrel=Surface+Storage; rooftop disconnection=Surface only; vegetative swale=Surface only).
Aspects: bio-retention cell; rain garden; green roof; infiltration trench; permeable pavement / block pavers; rain barrel / cistern; rooftop disconnection; vegetative swale; percent-of-area treated + underdrain on/off + treatment-train sequencing across types
- [LANDED ADR 0151 -> swmm_lid_performance_comparison(lid_type=green_roof)] `swmm_lid_green_roof_detention` [S] [US] - For a subcatchment with a fraction of its impervious roof area converted to a green roof, how much less and how much slower does runoff leave the subcatchment compared to the same area with conventional roofing?
  src: https://www.openswmm.org/Topic/15497/a-very-simple-two-subcatchment-model-with-and-without-green-roofs ("A very simple two subcatchment model with and without green roofs" - openswmm.org Topic 15497, downloadable "Simple green roof example.inp", author Robert Dickinson)
  knobs: green roof coverage % of impervious area (25% in source), surface layer (depth/Manning's n/storage depth/vegetation volume/infiltration rate), soil layer (porosity/field capacity/wilting point/conductivity/slope/suction), drainmat layer (thickness/flow coefficient/storage depth)
  notes: Complete .inp verified live and downloadable via WebFetch. No quantitative comparison published (page only asserts 'less runoff' qualitatively) - mechanism template, pair with a design-storm sweep for a quantified result. Same LidControls layer-stack pattern as the signed bioretention/rain-garden templates, different layer combination (adds DrainMat, drops Storage) -> S.
- [CAND-M] `swmm_lid_infiltration_trench_permeable_pavement` [M] [US] - Comparing an infiltration trench (gravel-filled excavation under a swale) to permeable pavement (paver surface over the same gravel sub-base) on the same footprint, how do captured-runoff volume and time-to-drain differ?
  src: https://www.openswmm.org/Topic/37545/swmm-for-permeable-paving-lid (openswmm.org Topic 37545 ("SWMM for Permeable Paving LID") + Topic 6477 ("Permeable pavement") + Topic 2554 ("Routing over permeable pavers") - Robert Dickinson layer-mapping guidance (Pavement Layer=paving blocks, Soil Layer=bedding-aggregate workaround, Storage Layer=sub-base))
  knobs: pavement layer thickness/void-ratio/permeability/clogging factor, storage layer porosity (~0.45)/conductivity (~200 in/hr sub-base), underdrain offset/coefficient, trench-vs-pavement toggle (drop Pavement layer for trench)
  notes: Documented open modeling problem, not a clean solved example: the Soil layer's field-capacity/wilting-point physics don't fit non-soil bedding aggregate, and the thread offers three competing workarounds (merge into Storage / fudge Soil params to mimic gravel / patch SWMM5 source) rather than one canonical answer. No numeric worked example; needs a documented default choice before building -> M (new LID type combination plus an open parameterization question to resolve, not a copy-paste knob).
- [LANDED ADR 0151 -> swmm_lid_performance_comparison(lid_type=rainbarrel_vs_disconnect)] `swmm_lid_rainbarrel_rooftop_disconnect` [S] [US] - For a residential lot, how much roof runoff does simple rooftop disconnection (draining onto a pervious yard) capture compared to a rain barrel (fixed-volume cistern with a controlled release), for storms of increasing size?
  src: https://www.openswmm.org/Topic/4482/lid-underdrain-parameters (openswmm.org LID underdrain parameter thread (Topic 4482) + EPA SWMM5 LID type/layer table (swmm5.org "Refactoring the SWMM 5 Help File - LID's In SWMM5", 2019/01/20): Rain Barrel = Surface+Storage only; Rooftop Disconnection = Surface layer only)
  knobs: rain barrel storage layer height/void fraction, barrel drain coefficient/exponent/offset, rooftop disconnection surface storage depth/Manning's n/pervious receiving area, storm size sweep
  notes: Simplest two LID types by layer count per swmm5.org's own layer-requirement table (live-verified) - the lowest-lift LID templates in the whole family, a good first pair to build once the layer-stack generator exists from the rain-garden/bioretention/green-roof work -> S each, bundled as one candidate pair.
- [LANDED ADR 0151 -> swmm_lid_performance_comparison(lid_type=vegetative_swale)] `swmm_lid_vegetative_swale_conveyance` [S] [US] - Routing runoff through a vegetated swale (a surface-layer-only LID acting as a conveyance element, not just storage) instead of a conventional lined channel, how much does travel time and peak attenuation change along the same flow path?
  src: https://www.openswmm.org/Topic/29954/how-vegetative-swales-work-in-swmm ("How Vegetative Swales Work in SWMM?" - openswmm.org Topic 29954)
  knobs: swale surface roughness (Manning's n, vegetated vs bare), swale slope/side-slope/bottom-width geometry, swale length, swale surface storage depth
  notes: Surface-layer-only LID type (per the layer table) applied along a linear flow path rather than a subcatchment area - a conceptually distinct aspect (LID-as-conveyance vs LID-as-storage) worth its own template. No numeric published example, mechanism reference only -> S, reuses the surface-layer machinery already needed by every other LID candidate.

Roster gaps (SWMM): pystorms.readthedocs.io scenario reference page returned HTTP 404 live; scenario descriptions beyond theta (docstring-verified) and alpha (search-corroborated: 0.12 km2 residential combined sewer, 5 weirs, minimize-CSO objective) were reconstructed from the GitHub README + search snippets, not independently fetched verbatim for beta/gamma/delta/epsilon/zeta. -- EPA SWMM5 Reference Manual Volume I (Hydrology) and Volume II (Hydraulics) PDFs (tuflow.com mirrors) are live-reachable but their text layer is not machine-extractable via WebFetch (same limitation already flagged in reports/design/swmm-practice-verification-2026-08-04.md) -- infiltration-method and node-ponding/flooding content summarized from secondary sources (swmm5.org, openswmm.org forum threads), not quoted verbatim from the primary manual. -- EPA SWMM Applications Manual PDF (chiwater.com mirror, EPA/600/R-09/000) also failed to decode via WebFetch (binary/compressed PDF stream, same failure mode as the CHI green-infrastructure paper at chijournal.org/Content/Files/R246-15.pdf, which was dropped as a source entirely) -- Example 1 (pre/post-development) content is search-snippet-derived only; Example 5 (WQ) content is corroborated because swmm5.org's own companion blog walkthrough DID fetch cleanly and is cited as the primary source_url instead of the raw EPA PDF. -- The Aquifer/Groundwater A1/A2/B1/B2/A3 flow-equation-coefficient page (swmm5.org 2013/08/10) truncated mid-explanation on live fetch; full coefficient definitions not independently verified this pass, only the two-object-type structure (Aquifer vs Groundwater) was confirmed. -- No downloadable .inp or published numeric worked example was found for three candidates (snow removal/plowing, storage-treatment removal functions, infiltration-trench/permeable-pavement LID) -- each is flagged mechanism/documentation-only in its own notes field rather than a replication target.


## TELEMAC (10 modules)

### TELEMAC-2D
Purpose: Depth-averaged (Saint-Venant) 2D free-surface hydrodynamics - the workhorse solver TRID3NT already runs for every river reach.
Today: Live and load-bearing: base 2D SWE mesh solve, dye tracer, oil-spill Lagrangian drift (M3), WAQTEL decay (process 17), GAIA v1 supply-limited sediment all shipped and routed via classify_substance(). WAQTEL thermic (process 11) and GAIA v2 erodible-bed scour are fully scoped but unbuilt. No weir/culvert/rainfall/wind/breach singularity authoring exists in the worker.
Aspects: base shallow-water hydrodynamics; passive/active tracer transport; hydraulic singularities (weirs/culverts/siphons); rainfall-evaporation forcing; wind stress forcing; dam/levee breach; tidal boundary forcing; WAQTEL water-quality coupling; GAIA sediment coupling; Lagrangian oil-spill drift
- [CAND-M] `weir_controlled_discharge_staging` [M] [US] - How does a low-head dam or weir structure regulate downstream discharge and upstream backwater staging?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac2d/weirs (github_ogoe_mirror_t2d_weirs)
  knobs: NUMBER OF WEIRS + a weir-data file (crest elevation, width, discharge coefficient per structure) - new keyword family, not currently emitted by telemac_river_dye_build.py
  notes: Live-fetched t2d_weirs.cas confirms NUMBER OF WEIRS=3, 600 m3/s inflow, 1.35 m stage, a working reference deck. Low-head dams are pervasive on US rivers (many already in the NID).
- [CAND-L] `culvert_siphon_flow_singularity` [L] [US] - How does flow route through a culvert or siphon crossing under a road/levee embankment, including the choked/submerged-orifice regime?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac3d/culvert (github_ogoe_mirror_t3d_culvert)
  knobs: CULVERT DATA FILE (points, invert elevations, diameter/shape, loss coefficients) - new file format the worker does not author today; examples/telemac2d/siphon is the 2D-side analogue
  notes: Confirmed dirs on both the 2D (siphon) and 3D (culvert) sides of the mirror; genuinely new singularity-file authoring, not a knob on the existing deck.
- [CAND-M] `rainfall_evaporation_forcing` [M] [US] - How does distributed rainfall (or evaporation) over the mesh change inundation depth and timing during a storm, independent of the upstream inflow hydrograph?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac2d/pluie (github_ogoe_mirror_t2d_pluie)
  knobs: RAIN OR EVAPORATION = YES + a rate (constant or time-varying file, mm/hr)
  notes: Complements SWMM's urban rainfall-runoff angle with a direct-on-mesh riverine/floodplain rainfall term; could reuse the gridmet/GridMET precip fetcher already wired for other engines.
- [LANDED] `wind_stress_forcing` [S] [US] - How does sustained wind set up a water-surface slope or drive circulation on a wide reach, embayment, or lake? [LANDED 2026-08-05 (ADR 0154): folded as a wind-stress KNOB on telemac_river_dye (wind_speed_mps + wind_direction_deg, 0 new tools). author_deck emits WIND=YES + OPTION FOR WIND=1 + WIND VELOCITY ALONG X/Y (UTM-frame components from the meteorological FROM-bearing) + THRESHOLD DEPTH FOR WIND=1, keywords pinned vs telemac2d.dico v9.0; unset (0) leaves every deck BYTE-IDENTICAL (pinned by test). Worker image rebuilt. Live smoke Eel River near Scotia CA on the DEFAULT bank_source=nhd_area (REAL NHDArea polygon banks, domain_mode=water-polygon, mean width 131 m, 1702 nodes / 3048 elements, ~20 m): baseline vs 18 m/s wind FROM 270 deg, BOTH CORRECT END OF RUN (~35 s solver wall each), wind setup 8.09 cm range (~8 cm upwind setdown converging downwind - textbook direction). Proofs from the nhd_area run: telemac_wind_stress_forcing_chart.png (dock overlay) + _.png (setup field over Esri) + _mesh.png (raw triangulation over Esri - mesh follows the variable-width real banks); telemac_wind_stress_forcing_mesh_ribbon.png kept as the constant_ribbon contrast artifact.]
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac2d/wind (github_ogoe_mirror_t2d_wind)
  knobs: WIND VELOCITY (constant) or WIND VELOCITY FILE (time-varying, per examples/telemac2d/wind_txy)
  notes: Two confirmed example dirs (wind, wind_txy). fetch_gridmet already serves the vs (wind speed) variable used for the scoped-but-unbuilt WAQTEL thermic candidate - directly reusable here.
- [STOP] `instantaneous_dam_breach_flood_wave` [S] [US] - How does a sudden (or progressive) dam/levee breach propagate a flood wave downstream, and how far/fast does the wave attenuate? [STOP 2026-08-05 (ADR 0154): the canonical form is an initial free-surface DISCONTINUITY (two water levels) requiring a user-fortran CONDITIONS INITIALES='PARTICULAR' CONDIN the worker neither authors nor compiles (it emits only CONSTANT DEPTH); the canonical published geometry (Malpasset) is non-US and outside our US-fetcher surface. A breach-hydrograph proxy (sudden upstream discharge surge via LIQUID BOUNDARIES FILE) risks the SUPERCRITICAL ENTRY WITH FREE DEPTH instability the GAIA work already hit and is a weaker posing. Recipe: add a CONDIN fortran authoring+compile path (or vendor a US dam-break benchmark geometry) + a time-varying breach-hydrograph boundary writer, then smoke front-arrival/attenuation.]
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac2d/malpasset (github_ogoe_mirror_t2d_malpasset)
  knobs: breach geometry + timing (instantaneous vs progressive widening) on the existing mesh/inflow deck path
  notes: Malpasset is the canonical published-outputs validation case (real 1959 failure, documented gauge/survey comparison) and is already "parked from the Malpasset arc" per project memory - this candidate is mostly wiring it into run_telemac as a named scenario rather than net-new physics.

### TELEMAC-3D
Purpose: Fully 3D (Navier-Stokes, hydrostatic or non-hydrostatic) free-surface flow - the fidelity-ladder rung above 2D for stratified/vertically-structured problems.
Today: Not surfaced at all - the worker/build pipeline (telemac_river_dye_build.py) is entirely 2D-oriented; no vertical-layer meshing or 3D deck authoring exists.
Aspects: vertical (sigma/z) layering; thermal/density stratification; salinity intrusion; vertical structures (culverts, intakes); 3D particle tracking; tidal-flat wetting/drying in 3D
- [CAND-L] `thermal_stratification_lake_reservoir` [L] [US] - How does a lake or reservoir stratify into a warm epilimnion over a cold hypolimnion across a season, and does wind mixing erode the thermocline?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac3d (github_ogoe_mirror_t3d_stratification_heat_exchange)
  knobs: vertical layer count/spacing, surface heat-exchange law (same atmosphere-water model family as the scoped WAQTEL thermic work), initial vertical temperature profile
  notes: Confirmed dirs: stratification, heat_exchange. Directly relevant to hydropower cold-water-release studies (a real US reservoir-management question).
- [CAND-L] `saline_density_intrusion_estuary` [L] [US] - How far upstream does denser saltwater intrude along the channel bed beneath fresher river outflow (a salt wedge) in a tidal river reach?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac3d (github_ogoe_mirror_t3d_lock_exchange)
  knobs: salinity as an active (density-coupling) tracer, tidal boundary forcing, vertical layering resolution near the bed
  notes: Confirmed dir: lock-exchange (canonical density-current test case). Relevant to Delaware Bay / Sacramento-San Joaquin Delta drought-planning salinity questions.
- [CAND-L] `vertical_culvert_recirculation_structure` [L] [US] - How does flow through a large box culvert or intake develop vertical recirculation that a depth-averaged 2D model cannot represent?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/telemac3d/culvert (github_ogoe_mirror_t3d_culvert)
  knobs: 3D CULVERT DATA FILE + vertical layering local refinement around the structure
  notes: The 3D-specific complement to the 2D culvert_siphon candidate - only worth building once basic 3D meshing exists.

### GAIA
Purpose: Unified sediment-transport and bed-morphodynamics module (SISYPHE's stated successor) - erosion, deposition, and bed evolution coupled to TELEMAC-2D/3D.
Today: v1 supply-limited suspended sediment (single grain class, LAYERS INITIAL THICKNESS=0 so nothing erodes) is live (commit f9eea1bb). v2 erodible-bed scour/bedload is fully scoped (design doc, 2026-07-20) but unbuilt. Multi-grain, cohesive-mud, and sediment-supply-boundary aspects are untouched.
Aspects: supply-limited suspended-load deposition; erodible-bed bedload + scour; multiple grain-size classes (mixed sand); cohesive mud (consolidation/settling); upstream sediment-supply boundary condition; NESTOR dredging coupling
- [CAND-M] `erodible_bed_scour_morphodynamics` [M] [US] - Where does the bed scour (not just deposit) below a dam, weir, or bridge contraction, and where does the eroded material re-deposit downstream?
  src: https://www.researchgate.net/publication/336126793_Introducing_GAIA_the_brand_new_sediment_transport_module_of_the_TELEMAC-MASCARET_system (researchgate_gaia_intro_paper_snippet)
  knobs: LAYERS INITIAL THICKNESS>0, BED LOAD FOR ALL SANDS=YES, BED-LOAD TRANSPORT FORMULA integer (dico-version-pinned)
  notes: Full build already scoped (unbuilt) in reports/design/telemac-river-addons-scoping-2026-07-20.md - listed here for roster completeness, not a new discovery. ResearchGate abstract confirmed via search snippet only (403'd direct WebFetch).
- [CAND-M] `mixed_grain_size_sediment_budget` [M] [US] - How does a sediment mixture of several grain sizes (fine to coarse sand) sort and segregate as it moves downstream, versus a single representative grain size?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/sisyphe (github_ogoe_mirror_sisyphe_yen_multigrain)
  knobs: multiple sediment classes with distinct d50/density, per-class active-layer stratification
  notes: Confirmed dir: yen_multigrain (SISYPHE-lineage proxy for GAIA's stated multi-class generalized framework).
- [CAND-M] `cohesive_mud_settling_consolidation` [M] [US] - How does fine cohesive mud (versus non-cohesive sand) settle, consolidate, and resuspend differently in a low-energy backwater or estuary reach?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/sisyphe (github_ogoe_mirror_sisyphe_bosse_vase_conservation_vase)
  knobs: cohesive sediment class (critical shear stresses for erosion/deposition differ from Shields-based sand physics), consolidation layering
  notes: Confirmed dirs: bosse_vase, conservation_vase ("vase"=mud/silt in the SISYPHE case naming).
- [COVERED] `upstream_sediment_supply_boundary` [S] [US] - How does a prescribed upstream sediment SUPPLY rate (rather than an initial bed stock) change downstream deposition patterns - the reservoir-inflow-sedimentation framing? [COVERED 2026-08-05 (ADR 0154): the existing GAIA v1 path (telemac_river_dye substance=sediment/sand/silt/mud) IS this question - SUPPLY-LIMITED (LAYERS INITIAL THICKNESS=0, deposition from a prescribed upstream source concentration, not a bed stock). No worker change; corpus queries + the substance docstring make it retrievable under the "reservoir-inflow / upstream sediment supply" framing (model-free retrieval HITs confirmed). 0 new tools.]
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/sisyphe (github_ogoe_mirror_sisyphe_canal_solid_discharge_inflow)
  knobs: SOLID DISCHARGE boundary condition at the upstream liquid boundary vs. the current pulse-source approach
  notes: Confirmed dir: canal_solid_discharge_inflow. Relevant to USACE reservoir sediment-budget questions where inflow supply, not a single spill, drives the deposit.

### TOMAWAC
Purpose: Third-generation spectral wave model - wind-wave generation and propagation in open/coastal seas, coupled to TELEMAC hydrodynamics.
Today: Not surfaced - no wave-spectrum solver exists in the worker; SFINCS/SnapWave carries the coastal-screening role today per the fidelity-ladder doctrine.
Aspects: wind-driven wave growth; nearshore refraction/shoaling; wave-current interaction; bottom-friction/whitecapping dissipation; wave breaking/blocking
- [CAND-L] `wind_generated_wave_growth` [L] [US] - How do local wind-driven waves grow across a limited fetch (a bay, sound, or wide reach) - a fetch-limited spectrum?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/tomawac (github_ogoe_mirror_tomawac_fetch_limited)
  knobs: wind field forcing, fetch geometry, spectral discretization (directions x frequencies)
  notes: Confirmed dir: fetch_limited.
- [CAND-L] `nearshore_wave_refraction_shoaling` [L] [US] - How do offshore swell waves refract and shoal (steepen) as they cross a shallowing nearshore bathymetry?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/tomawac (github_ogoe_mirror_tomawac_shoal)
  knobs: bathymetry mesh resolution near shore, boundary spectrum specification
  notes: Confirmed dir: shoal. TOMAWAC is refinement-grade vs SFINCS/SnapWave's screening role - a fidelity-ladder complement, not a replacement.
- [CAND-L] `wave_current_interaction` [L] [US] - How does an opposing (or following) tidal/river current steepen or flatten incoming waves at an inlet or estuary mouth?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/tomawac (github_ogoe_mirror_tomawac_opposing_current)
  knobs: coupled current field (from a TELEMAC-2D hydrodynamic run) feeding the wave solver
  notes: Confirmed dir: opposing_current.
- [CAND-L] `bottom_friction_wave_dissipation` [L] [US] - How much wave energy dissipates to bottom friction as waves cross a shallow, wide continental-shelf or bay bathymetry?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/tomawac (github_ogoe_mirror_tomawac_bottom_friction)
  knobs: bottom friction coefficient/formulation choice
  notes: Confirmed dir: bottom_friction.

### ARTEMIS
Purpose: Elliptic mild-slope wave-agitation solver for harbors/ports - steady-state diffraction, reflection, and refraction in coastal/port basins.
Today: Not surfaced - no elliptic wave solver exists in the worker.
Aspects: harbor resonance/agitation; breakwater diffraction/sheltering; structure reflection (quay walls, jetties)
- [CAND-L] `harbor_wave_agitation_resonance` [L] [US] - How much does incoming swell amplify inside a harbor/port basin due to resonance and reflection off quay walls?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/artemis (github_ogoe_mirror_artemis_port)
  knobs: incident wave period/direction spectrum, quay-wall reflection coefficients
  notes: Confirmed dir: port. Relevant to US port/marina agitation studies.
- [CAND-L] `breakwater_wave_diffraction_sheltering` [L] [US] - How effectively does a breakwater shelter a berthing area from incident wave energy, and where does diffracted energy still reach?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/artemis (github_ogoe_mirror_artemis_breaking)
  knobs: breakwater geometry/gap width, incident wave conditions
  notes: Confirmed dir: breaking.
- [CAND-L] `reef_shoal_wave_sheltering` [L] [US] - How much does a nearshore reef or shoal reduce wave energy reaching a coastline behind it (natural-infrastructure framing)?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/artemis (github_ogoe_mirror_artemis_recif)
  knobs: reef/shoal bathymetry, incident spectrum
  notes: Confirmed dir: recif (reef). Pairs with natural-infrastructure/living-shoreline coastal-resilience demos.

### WAQTEL
Purpose: Water-quality process library coupled to TELEMAC-2D/3D - biochemical, thermal, and micropollutant processes riding the hydrodynamic tracer transport.
Today: Decay (process 17) is live (commit 62ca06d0). Thermic (process 11) is fully scoped (design doc) but unbuilt. O2/EUTRO/MICROPOL/BIOMASS/AED2 are untouched.
Aspects: first-order bacterial decay; thermal discharge/heat exchange; dissolved-oxygen/BOD balance; eutrophication (algae + N/P cycles); micropollutant adsorption-desorption; single-species biomass (algae-only); AED2 library coupling
- [CAND-M] `dissolved_oxygen_bod_sag_curve` [M] [US] - How does an organic (BOD) discharge create the classic downstream oxygen-sag (Streeter-Phelps) curve, and where is the minimum DO point?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/waqtel (github_ogoe_mirror_waqtel_waq2d_o2)
  knobs: WATER QUALITY PROCESS = O2, BOD5 + DO as coupled tracers, reaeration-rate formulation
  notes: Confirmed dir: waq2d_o2. Directly maps to Clean Water Act DO-impairment/TMDL demo framing - strong US relevance.
- [CAND-L] `eutrophication_algal_nutrient_cycle` [L] [US] - How does a nutrient (nitrogen/phosphorus) load drive a downstream algal bloom (chlorophyll-a buildup) and secondary DO depletion?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/waqtel (github_ogoe_mirror_waqtel_waq2d_eutro)
  knobs: WATER QUALITY PROCESS = EUTRO, NH4/PO4/phytoplankton as a coupled multi-tracer system
  notes: Confirmed dir: waq2d_eutro. Heavier build than O2 (multiple coupled tracers) - relevant to harmful-algal-bloom/nutrient-TMDL demos.
- [CAND-M] `micropollutant_adsorption_desorption` [M] [US] - How does a dissolved/particulate contaminant (heavy metal or similar) partition between the water column and suspended sediment as it moves downstream?
  src: https://github.com/ogoe/OpenTelemac/blob/master/examples/waqtel/waq2d_micropol/micropol_steer.cas (github_ogoe_mirror_waqtel_waq2d_micropol_steer_cas)
  knobs: WATER QUALITY PROCESS = MICROPOL, partition coefficient (Kd), coupling to suspended-sediment concentration (natural pairing with GAIA)
  notes: Confirmed both the dir and the actual steering-file case (micropol_steer.cas) via a direct file fetch.
- [CAND-M] `biomass_algae_only_growth` [M] [US] - How does phytoplankton biomass grow/decay on its own (light + temperature limited), without the full N/P eutrophication cycle - a lighter-weight algal-growth demo?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/waqtel (github_ogoe_mirror_waqtel_waq2d_biomas)
  knobs: WATER QUALITY PROCESS = BIOMASS, growth/mortality rate constants
  notes: Confirmed dir: waq2d_biomas. A cheaper single-tracer stepping stone toward full eutrophication.

### KHIONE
Purpose: River-ice module - frazil ice formation/transport, ice-cover development, and ice-hydrodynamics/structure interaction (trash-rack clogging), coupled to TELEMAC-2D.
Today: Not surfaced, not scoped. No example deck exists in the mirror used this session (post-dates the snapshot).
Aspects: frazil ice formation/transport; ice-cover growth; ice-jam river staging; structure icing/clogging
- [CAND-L] `frazil_ice_formation_transport` [L] [US] - How does supercooled turbulent river water form and transport frazil ice crystals downstream before a solid ice cover forms - a winter river-ice hazard?
  src: https://eprints.hrwallingford.com/1274/ (hrwallingford_eprints_khione_intro_paper)
  knobs: heat-balance/supercooling parameters, frazil crystal growth model
  notes: Page confirmed reachable; abstract confirms EDF/HR Wallingford/Clarkson collaboration and validation-case existence, but full physics detail sits behind the PDF (not fetched). US relevance: Great Lakes basin, Upper Mississippi/Missouri ice-affected reaches; a US canonical case is not yet identified (candidate for the USACE CRREL ice-jam database as a future data source).
- [CAND-L] `ice_jam_flood_staging` [L] [US] - How much does an ice cover or ice jam raise upstream water levels versus the open-water hydraulic condition, and does it threaten a flood stage?
  src: https://eprints.hrwallingford.com/1274/ (hrwallingford_eprints_khione_intro_paper)
  knobs: ice-cover roughness/thickness feedback on hydrodynamics
  notes: Same source as above (only KHIONE citation independently verified reachable this session). Lower confidence than most candidates in this roster - flagged in roster_gaps.

### MASCARET
Purpose: 1D free-surface (Saint-Venant) solver for branched river networks - faster than 2D for regional routing and network-scale dam-breach studies.
Today: Not surfaced, not scoped - the worker builds only 2D meshes; no 1D network solver path exists.
Aspects: branched-network 1D routing; progressive/instantaneous dam breach on a network; dry-bed wetting front
- [CAND-L] `branched_network_1d_routing` [L] [US] - How does a flood wave route through a branched river network (multiple confluences) at regional scale, faster than a full 2D mesh solve?
  src: https://www.opentelemac.org/index.php/presentation?id=138 (opentelemac_mascaret_presentation_snippet)
  knobs: network topology (branches + confluences), cross-section geometry per reach segment
  notes: opentelemac.org TLS broken - content is search-snippet only, not independently fetched. The mirror's examples/mascaret dir holds only test/toolbox (no case-named decks) - lowest-confidence source in this roster; flag for direct verification against EDF's dedicated mascaret repository before committing effort.
- [CAND-L] `progressive_dam_breach_1d_network` [L] [US] - How does a slowly-widening (progressive, not instantaneous) dam breach evolve the downstream 1D flood hydrograph across a network, versus an instantaneous failure?
  src: https://www.opentelemac.org/index.php/presentation?id=138 (opentelemac_mascaret_presentation_snippet)
  knobs: breach widening rate/geometry over time
  notes: Search snippet references a real validated case (Bort-les-Orgues dam, France - not US); same low-confidence sourcing caveat as above.

### COURLIS
Purpose: 1D fine-sediment (suspension + bedload) module coupled to MASCARET - reservoir/river deposition, erosion, and flushing dynamics in a 1D network.
Today: Not surfaced, not scoped; hard-dependent on MASCARET (also not built).
Aspects: reservoir siltation over multi-year operation; flushing-drawdown remobilization; cohesive mud advection-diffusion in 1D
- [CAND-L] `reservoir_siltation_flushing_1d` [L] [US] - How does sediment accumulate behind a dam over multi-year reservoir operation, and how effectively does a flushing drawdown remobilize and pass it downstream?
  src: https://www.e3s-conferences.org/articles/e3sconf/pdf/2018/15/e3sconf_riverflow2018_05038.pdf (e3s_conferences_courlis_paper_snippet)
  knobs: reservoir bathymetry, inflow/outflow schedule, flushing drawdown timing
  notes: PDF returned 403 to WebFetch (bot-blocked, not confirmed dead) - sourced from search snippet only. Real US relevance: USACE mainstem-reservoir sediment management (e.g. Missouri River system), but genuinely low near-term priority given the unbuilt MASCARET dependency - see roster_gaps.

### NESTOR
Purpose: Dredging-operations module - simulates channel excavation and disposal/dumping, coupled to GAIA/SISYPHE bed evolution.
Today: Not surfaced, not scoped. Compiled-library presence in the current worker image is unverified this session.
Aspects: channel maintenance dredging; disposal/dumping placement; automated critical-elevation dig/dump triggers
- [CAND-L] `channel_maintenance_dredging` [L] [US] - How does periodic channel dredging maintain a navigable depth against ongoing natural sediment deposition?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/sisyphe (github_ogoe_mirror_sisyphe_nestorExample1)
  knobs: dredge schedule/volume, target channel depth
  notes: Confirmed dir: nestorExample1. Federal-channel maintenance dredging (e.g. USACE navigation channels) is a real, common US application.
- [CAND-L] `dredge_spoil_disposal_placement` [L] [US] - Where does dredged material redistribute once placed at a designated open-water or upland disposal site?
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/sisyphe (github_ogoe_mirror_sisyphe_nestorExample2_dump)
  knobs: disposal-site location/geometry, placement rate
  notes: Confirmed dir: nestorExample2_Dump.
- [CAND-L] `critical_elevation_triggered_dig_dump` [L] [US] - Modeling an automated maintenance rule - dredge when the bed rises above a critical elevation, dump when a disposal cell fills - over a multi-year simulation of a maintained federal channel.
  src: https://github.com/ogoe/OpenTelemac/tree/master/examples/sisyphe (github_ogoe_mirror_sisyphe_nestorExample3_critdigdump)
  knobs: critical dig/dump elevation thresholds, disposal-cell capacity
  notes: Confirmed dir: nestorExample3_critDigDump - the most sophisticated of the three confirmed NESTOR examples (closed-loop rule, not a one-off dredge event).

Roster gaps (TELEMAC): opentelemac.org (the modules-list index and the v7p0 PDF manual) served a broken TLS chain to WebFetch on every attempt this session (both https and the http-upgraded form) - module purpose/description text for that domain is WebSearch-synthesis of its indexed content, not independently fetched-and-read; flagged per-candidate where it is the only source. wiki.opentelemac.org refused the connection outright (dead this session). The official gitlab.pam-retd.fr/otm/telemac-mascaret examples tree 404'd at the paths tried (repo layout likely differs from assumption; not further explored). Used the unofficial github.com/ogoe/OpenTelemac mirror as the primary example-tree source instead - live-fetched and enumerated examples/{telemac2d,telemac3d,tomawac,artemis,waqtel,sisyphe,mascaret}, all verified reachable. That mirror predates GAIA's fork from SISYPHE and predates KHIONE/COURLIS mainline merge: no examples/gaia (404 confirmed), no examples/khione, no examples/courlis directory exists in it. GAIA candidates below use SISYPHE examples as a physics-lineage proxy (GAIA is SISYPHE's documented successor - ResearchGate/ScienceDirect abstracts confirm this via search snippet, but both venues 403'd WebFetch directly, likely bot-blocking not dead links); exact GAIA keyword/formula-integer syntax must still be pinned against the in-image v9 GAIA dico before any build, same discipline the shipped v1/v2 GAIA work already follows (see telemac-river-addons-scoping-2026-07-20.md). examples/mascaret in the mirror holds only test/ and toolbox/ (no case-named decks), so MASCARET and COURLIS candidates lean on academic search snippets (HAL INRAE Anubis-blocked, e3s-conferences PDF 403'd) rather than a fetched example deck - lowest-confidence entries in this roster, flagged individually. NESTOR's compiled-library status in the current trid3nt-local/telemac:latest image was not checked this session (only WAQTEL's and GAIA's .so files are confirmed precompiled, per the 2026-07-20 design doc) - verify before trusting the NESTOR effort estimates. Separately: TRID3NT's actual shipped TELEMAC-2D baseline is broader than "2D + tracer only" - git log + classify_substance() in model_river_dye_release_scenario.py confirm dye tracer + oil spill (Lagrangian drift, commit 72bf6e03) + WAQTEL decay (WQ process 17, commit 62ca06d0) + GAIA v1 supply-limited sediment (commit f9eea1bb) are all live; WAQTEL thermic (process 11) and GAIA v2 erodible-bed scour are fully scoped (design doc, 2026-07-20) but confirmed NOT yet built - both listed below for roster completeness with effort reflecting "design exists, build doesn't."


## LANDLAB (16 modules)

### Flow Direction & Accumulation
Purpose: Route water/sediment downslope across a grid and accumulate drainage area/discharge - the substrate every other surface-process chain reads (slope, contributing area).
Today: LANDED as the `landlab_flow_accumulation` template (ADR 0122, hazard-easy-four #1): a standalone flow-accumulation analysis (`analysis="flow_accumulation"`) emitting the drainage-area raster + an extracted channel-network vector + a D8/Dinf/MFD routing-comparison chart, over a real AOI DEM (fetch_3dep_extra -> fetch_dem). flow_director (D8/Dinf/MFD) rides advanced_physics; depression_handler (fill DepressionFinderAndRouter | priority_flood PriorityFloodFlowRouter) + channel_threshold_cells are first-class knobs. Exec-mode (no image rebuild). LossyFlowAccumulator remains unsurfaced.
Aspects: single-path steepest-descent routing (D4/D8); multi-flow-direction distribution (MFD/D-infinity); RichDEM-backed unified fill+route+accumulate for large/real DEMs (PriorityFlood); lossy/attenuated downstream accumulation
- [LANDED] `landlab_flow_accumulation` [S] [US] - Drainage-area / flow-accumulation layer + channel network for this watershed on its own, without a landslide analysis. (ADR 0122)
  src: https://landlab.readthedocs.io/en/latest/tutorials/flow_direction_and_accumulation/the_FlowAccumulator.html (landlab_flowaccumulator_tutorial)
  knobs: flow_director(D8 default|Dinf|MFD), depression_handler(fill|priority_flood), channel_threshold_cells, target_resolution_m
  notes: Surfaces the FlowAccumulator drainage_area as the PRIMARY log-styled raster + a drainage-area-threshold channel-network vector; the routing-comparison chart answers how much the director moves the concentrated flow paths. Live exec smoke (synthetic UTM DEM): max drainage_area 4.176 km2, channelized fraction 0.011, 3-director comparison (D8 0.011 / Dinf 0.129 / MFD 0.123). Determinism verified.
- [LANDED] `multi_flow_direction_routing` [M->S] [US] - Route flow with multiple downslope neighbors (MFD/Dinf) vs single steepest-path - how does the drainage pattern change? FOLDED into landlab_flow_accumulation.
  src: https://landlab.readthedocs.io/en/latest/tutorials/flow_direction_and_accumulation/the_FlowAccumulator.html (landlab_flowaccumulator_tutorial)
  knobs: flow_director=D8|Dinf|MFD
  notes: Same landed template - swap flow_director; the built-in routing-comparison chart (all 3 directors) is exactly this question.
- [LANDED] `priority_flood_large_aoi_routing` [M->S] [US] - Robust fill+route+accumulate for messy/real DEMs via PriorityFloodFlowRouter. FOLDED into landlab_flow_accumulation as depression_handler="priority_flood" (the folded ADR 0121 row 9).
  src: https://landlab.readthedocs.io/en/latest/tutorials/flow_direction_and_accumulation/the_Flow_Director_Accumulator_PriorityFlood.html (landlab_priorityflood_tutorial)
  knobs: depression_handler=priority_flood, flow_director->flow_metric(D8|Dinf|Quinn)
  notes: richdem present in venvs/agent (PriorityFloodFlowRouter imports); no image rebuild (exec-mode). Live smoke exercised priority_flood across all 3 directors.

### Erosion & Stream Power (channel incision / landscape evolution)
Purpose: Model long-term channel incision and hillslope-to-channel sediment routing driven by drainage area and slope - bedrock rivers, transport-limited gravel rivers, threshold-limited erosion.
Today: None surfaced - TRID3NT has no landscape-evolution / channel-incision analysis today; this is a wholly new module.
Aspects: detachment-limited bedrock incision (stream power law); smooth-threshold incision (avoids numerical artifacts at low erosive power); transport-limited gravel-bed river evolution with downstream abrasion; combined erosion-deposition / lateral bank erosion
- [CAND-M] `detachment_limited_incision_steady_state` [M] [US] - Run this catchment to a steady-state channel profile under a given uplift rate and erodibility - does the resulting channel slope-area relationship match the analytical stream-power prediction?
  src: https://landlab.readthedocs.io/en/latest/tutorials/landscape_evolution/erosion_deposition/shared_stream_power.html (landlab_shared_stream_power_tutorial)
  knobs: k_bedrock, k_transport, m_sp, n_sp, uplift_rate, run duration
  notes: Published expected output: power-law slope-area relationship + steepness index rising over ~10,000yr after a 10x uplift-rate step - a strong V&V check.
- [CAND-M] `threshold_incision_sensitivity` [M] [US] - Below what erosive power does this channel stop incising, and does that threshold create unrealistic numerical jumps?
  src: https://landlab.readthedocs.io/en/latest/tutorials/landscape_evolution/smooth_threshold_eroder/stream_power_smooth_threshold_eroder.html (landlab_spste_tutorial)
  knobs: threshold_sp (erosion threshold), K_sp, m_sp, n_sp
  notes: Smooth formulation E = w - wc(1-e^(-w/wc)) avoids the hard-threshold numerical daemons the tutorial documents; steady-state slope-area still matches analytics.
- [CAND-M] `gravel_bed_transport_limited_evolution` [M] [US] - For a gravel-bed river, what equilibrium channel slope/width/grain-size does it reach given a sediment supply and downstream abrasion?
  src: https://landlab.readthedocs.io/en/latest/tutorials/landscape_evolution/gravel_river_transporter/gravel_river_transporter.html (landlab_gravel_river_transporter_tutorial)
  knobs: transport coefficient, abrasion coefficient, sediment porosity, bankfull runoff rate, intermittency factor
  notes: Tutorial validates numerical output against closed-form S=(supply/capacity)^(6/7) and width scaling - replicable V&V gate.

### Diffusion & Hillslope Processes
Purpose: Model soil creep / hillslope diffusion - how hillslopes relax topography via slope-dependent (linear or nonlinear) sediment flux.
Today: None surfaced.
Aspects: linear diffusion; nonlinear/critical-slope Taylor-series diffusion; depth-dependent diffusion (soil-mantle thickness feedback); transport-length creep-to-mass-wasting continuum
- [CAND-M] `nonlinear_hillslope_diffusion_to_equilibrium` [M] [US] - Run this hillslope's soil creep to equilibrium - does the resulting profile match the analytical parabolic solution, and how does it change near the critical slope?
  src: https://landlab.readthedocs.io/en/latest/tutorials/hillslope_geomorphology/taylor_diffuser/taylor_diffuser.html (landlab_taylor_diffuser_tutorial)
  knobs: linear_diffusivity D, slope_crit Sc, nterms(1=linear|2=cubic)
  notes: Published expected output: linear regime predicts ridge height 0.25 m via eta=(U/2D)(L^2-x^2); nonlinear regime flattens near Sc - falsifiable V&V check.
- [CAND-M] `transport_length_creep_to_massmovement_continuum` [M] [US] - On this hillslope, where does sediment deposit locally (creep) vs travel far downslope (near-failure mass wasting)?
  src: https://landlab.readthedocs.io/en/latest/tutorials/hillslope_geomorphology/transport-length_hillslope_diffuser/TLHDiff_tutorial.html (landlab_tlhdiff_tutorial)
  knobs: erodibility kappa, critical slope Sc, transport length L
  notes: Requires FlowDirectorSteepest for topographic__steepest_slope; distinct transport regime from TaylorNonLinearDiffuser (finite runout, not local flux only).

### Landslides & Mass Wasting
Purpose: Hazard/susceptibility and event-scale mass-movement modeling - the sprint-17 North Star hazard class.
Today: LandslideProbability is signed as the landslide_probability analysis (default chain, FlowAccumulator->LandslideProbability). BedrockLandslider and MassWastingRunout are unsurfaced - TRID3NT has no event-scale runout or landscape-evolution-coupled landslide-frequency capability. Storm-ensemble recharge sensitivity is LANDED as `landlab_landslide_storm_ensemble` (ADR 0141): PrecipitationDistribution Monte-Carlo recharge draws -> susceptibility-vs-recharge sensitivity slope.
Aspects: infinite-slope Monte-Carlo probability-of-failure susceptibility mapping (signed); episodic bedrock-landslide magnitude/frequency within a landscape-evolution run; event-scale runout of a specific mapped failure with a validated deposit extent
- [CAND-L] `episodic_bedrock_landslide_magnitude_frequency` [L] [US] - Over decades of uplift and erosion, where and how large are the stochastic bedrock landslides this landscape produces, and what does their size-frequency distribution look like?
  src: https://landlab.readthedocs.io/en/latest/tutorials/landscape_evolution/hylands/HyLandsTutorial.html (landlab_hylands_tutorial)
  knobs: uplift_rate, landslide return period / density-dependent failure probability params, run duration
  notes: New solver capability: couples FlowAccumulator+FastscapeEroder+BedrockLandslider in a landscape-evolution loop (not a single-shot analysis); published output shows power-law landslide-area frequency.
- [CAND-L] `single_event_landslide_runout_validated` [L] [US] - Given a mapped failure scar, where does the debris runout go, and how does the modeled deposit compare against an observed post-event DEM-of-Difference?
  src: https://landlab.readthedocs.io/en/latest/tutorials/mass_wasting_runout/landslide_runout_animation.html (landlab_masswastingrunout_tutorial)
  knobs: failure_depth, scar extent polygon, cellular-automata runout/friction coefficients
  notes: Tutorial itself replicates a REAL, documented 2021 Cascade Mountains (US) landslide-scar enlargement validated against a field-observed DEM-of-Difference - directly matches the paper-first, US-only replication doctrine.
- [LANDED] `landslide_probability_storm_ensemble_sensitivity` [S] [US] (ADR 0141: `landlab_landslide_storm_ensemble`) - Instead of one fixed daily recharge, run the failure-probability map across a realistic sequence of storm/recharge draws to see how susceptibility grows with rainfall variability.
  src: https://landlab.readthedocs.io/en/latest/generated/api/landlab.components.uniform_precip.generate_uniform_precip.html (landlab_precipitationdistribution_api)
  knobs: mean_storm_duration, mean_interstorm_duration, mean_storm_depth -> recharge_mm_day per draw, n_monte_carlo
  notes: Knob/loop addition on the ALREADY-SIGNED LandslideProbability chain: sweep recharge scenarios via PrecipitationDistribution instead of the current single fixed DEFAULT_RECHARGE_MM_DAY.

### Overland Flow (routing-physics variants)
Purpose: Route storm rainfall as shallow surface flow across a DEM to a hydrograph/inundation-depth field, at different physics/numerics tradeoffs.
Today: OverlandFlow (de Almeida) is signed as the overland_flow analysis, reporting peak surface_water__depth over a fixed storm. Bates/Kinwave-explicit/KinwaveImplicit/LinearDiffusion/Rengers variants and the Green-Ampt infiltration coupling are unsurfaced; the signed chain also only ever emits the peak depth, discarding the time series. The time series is now surfaced: `landlab_overland_flow_timeseries` (ADR 0141) emits depth at output_interval_s frames (animation-ready) + the outlet hydrograph chart.
Aspects: full de Almeida shallow-water routing (signed, explicit local inertial); implicit kinematic-wave routing (large stable timesteps for long storms); coupled Green-Ampt infiltration + kinematic-wave runoff generation; time-resolved inundation output vs single peak-depth output
- [CAND-M] `implicit_kinematic_wave_runoff_hydrograph` [M] [US] - Give me the outlet hydrograph for a long storm-plus-recession without the tiny stable timesteps the signed de Almeida chain needs.
  src: https://landlab.readthedocs.io/en/latest/tutorials/overland_flow/kinwave_implicit/kinwave_implicit_overland_flow.html (landlab_kinwave_implicit_tutorial)
  knobs: rainfall_mm_hr time series (incl. recession), roughness (Manning's n), outlet node selection
  notes: Implicit timestepping tolerates much larger dt than the signed explicit de Almeida chain - cheaper for long/multi-day storms.
- [LANDED] `landlab_green_ampt_overland_flow` [M->S] [US] - How much of this storm infiltrates vs runs off, and where does runoff actually initiate (partial-area vs saturation-excess)? (ADR 0123)
  src: https://landlab.readthedocs.io/en/latest/tutorials/overland_flow/soil_infiltration_green_ampt/infilt_green_ampt_with_overland_flow.html (landlab_green_ampt_tutorial)
  knobs: soil_hydraulic_conductivity_m_s K, initial_soil_moisture_content, green_ampt_soil_type, rainfall_return_period_yr/storm_duration_hr (Atlas-14 design storm), target_resolution_m
  notes: LANDED as the exec-mode SoilInfiltrationGreenAmpt + de Almeida OverlandFlow partition chain: PRIMARY infiltration-depth raster + a runoff-depth (rainfall-excess) raster + the infiltration-vs-runoff partition chart, over a real AOI DEM; triggering rainfall from the real NOAA Atlas-14 design storm (0102 seam), soil block demo-labeled. Live exec smoke (synthetic UTM DEM, 45 mm storm, K=1e-5): infiltrated_fraction 0.79, runoff_fraction 0.21; K-monotonicity + determinism verified.
- [LANDED] `overland_flow_depth_timeseries_output` [S] [US] (ADR 0141: `landlab_overland_flow_timeseries`; ADR 0145: raw-DEM default NATE-validated via relief render, condition_dem opt-in knob added) - Instead of only the peak surface-water depth, show me how inundation grows and recedes frame by frame during the storm.
  src: https://landlab.readthedocs.io/en/latest/tutorials/overland_flow/overland_flow_driver.html (landlab_overland_flow_driver_tutorial)
  knobs: output_interval_s, storm/simulation duration
  notes: Output-shape change on the ALREADY-SIGNED OverlandFlow chain - write depth at N intervals (tutorial samples 10/50/100s) instead of discarding all but the max, enabling the animation UI norm.

### Groundwater
Purpose: Shallow unconfined aquifer flow (Dupuit-Forchheimer approximation), coupling groundwater storage/discharge to surface-water seepage.
Today: Unsurfaced - GroundwaterDupuitPercolator is not wired into any TRID3NT analysis; the signed landslide chain's soil parameters (transmissivity, thickness) are static constants with no coupled water-table dynamics.
Aspects: constant-recharge mass-conservation baseline; storm-driven time-varying recharge -> seepage/outflow hydrograph; fixed-gradient / regional-flow boundary conditions
- [CAND-L] `aquifer_storm_seepage_hydrograph` [L] [US] - For this catchment, what does groundwater seepage/return-flow to the surface look like through a sequence of storms, and where does it emerge?
  src: https://landlab.readthedocs.io/en/latest/tutorials/groundwater/groundwater_flow.html (landlab_groundwater_flow_tutorial)
  knobs: hydraulic conductivity, porosity, aquifer thickness, storm generator (mean_storm/interstorm duration+depth), regularization factor
  notes: First groundwater engine chain in TRID3NT. Published V&V gate: cumulative flux vs cumulative recharge conserved to <1% relative error; peak surface-water flux ~0.04 m3/s in the tutorial's 500x500m case.
- [CAND-M] `constant_recharge_mass_balance_gate` [M] [US] - Prove the groundwater chain conserves mass across a range of recharge rates before trusting the seepage numbers on a real AOI.
  src: https://landlab.readthedocs.io/en/latest/tutorials/groundwater/groundwater_flow.html (landlab_groundwater_flow_tutorial)
  knobs: recharge_rate sweep (1e-5 to 1e-8 m/s), adaptive timestep size
  notes: A V&V/test-harness recipe layered on the aquifer solver above, not new physics - the mandatory acceptance gate before the seepage chain ships.

### Vegetation & Ecohydrology
Purpose: Couple radiation/PET/soil-moisture/vegetation-competition components into a spatially explicit plant-functional-type model driven by topography and climate.
Today: Unsurfaced - no vegetation/ecohydrology capability exists in TRID3NT today.
Aspects: radiation-driven aspect control on plant-functional-type organization (cellular-automaton tree-grass-shrub competition, CATGRaSS); flat-domain (no-topography) ecohydrology baseline; species-level biogeography/evolution tied to shifting landscape zones
- [CAND-L] `aspect_driven_vegetation_pattern_ca` [L] [US] - Given this DEM's aspect and a semi-arid rainfall regime, what steady-state vegetation pattern (tree/shrub/grass/bare) does topography produce?
  src: https://landlab.readthedocs.io/en/latest/tutorials/ecohydrology/cellular_automaton_vegetation_DEM/cellular_automaton_vegetation_DEM.html (landlab_catgrass_dem_tutorial)
  knobs: mean_annual_precipitation, initial PFT fractions, simulation years (~50yr in published case)
  notes: New 5-component chain (Radiation->PET->SoilMoisture->VegetationDynamics->PlantCompetitionCA). Published US case (central New Mexico, MAP=254mm) has a documented expected pattern: more trees on north-facing slopes, shrub/grass on south-facing - a strong, falsifiable V&V target.
- [CAND-L] `species_zone_biogeography_under_landscape_change` [L] [US] - As this landscape's habitable zones fragment or merge over time, how do species ranges split, merge, or go extinct?
  src: https://landlab.readthedocs.io/en/latest/tutorials/species_evolution/Introduction_to_SpeciesEvolver.html (landlab_speciesevolver_tutorial)
  knobs: zone connectivity function, environmental driver (e.g. temperature) time series
  notes: Published worked example has a fully deterministic expected outcome (2 founder species -> 22 extant taxa after two fragmentation events) - an excellent V&V case, but ecological/biogeography framing is a scope stretch for a hazard workbench; flag for a scope decision rather than building speculatively.

### Lithology & Stratigraphy
Purpose: Give a landscape-evolution run spatially/vertically variable rock properties (erodibility, diffusivity) instead of a uniform substrate.
Today: Unsurfaced.
Aspects: anchor-point rock-layer stacks with arbitrary structural geometry (anticline, dip); coupling rock-type-dependent K_sp/diffusivity into erosion components as topography exhumes different layers
- [CAND-L] `structural_lithology_controlled_erosion` [L] [US] - This landscape has resistant vs weak rock layers (e.g. a ridge-forming unit over shale) - how does that structure control which parts erode fastest as the terrain evolves?
  src: https://landlab.readthedocs.io/en/latest/tutorials/lithology/lithology_and_litholayers.html (landlab_lithology_tutorial)
  knobs: layer elevations/dips at an anchor point, per-rock-type K_sp and diffusivity D
  notes: Couples LithoLayers with FastscapeEroder+LinearDiffuser. Published expected output includes a falsifiable check: infilling old valleys with resistant material INVERTS the topography (old valleys become new ridges).

### Tectonics & Flexure
Purpose: Apply tectonic deformation (fault offset, lithospheric flexure) as boundary forcing to a landscape-evolution run.
Today: Unsurfaced.
Aspects: discrete normal-fault throw (constant or time-varying) as an uplift driver; elastic lithospheric flexure under distributed surface loads; listric (curved) fault kinematics for extensional terrains
- [CAND-L] `normal_fault_scarp_and_footwall_evolution` [L] [US] - Impose a normal-fault throw history on this landscape and show how the resulting fault scarp degrades and the footwall drainage network develops.
  src: https://landlab.readthedocs.io/en/latest/tutorials/normal_fault/normal_fault_component_tutorial.html (landlab_normal_fault_tutorial)
  knobs: fault trace (x1,y1)-(x2,y2), throw rate (constant or {time,rate} time series)
  notes: New tectonic-forcing capability coupled to erosion/diffusion for a landscape-evolution run; supports both raster and hex grids.
- [CAND-M] `lithospheric_flexure_under_surface_load` [M] [US] - A large sediment/ice/water load was placed on this crust - how much does the lithosphere flex or subside, and over what wavelength?
  src: https://landlab.readthedocs.io/en/latest/tutorials/flexure/lots_of_loads.html (landlab_flexure_tutorial)
  knobs: load magnitude/geometry (point vs distributed), effective elastic thickness (EET)
  notes: Single-component load-in/deflection-out - simpler than the fault-coupled landscape-evolution model above; tutorial shows doubling EET measurably changes deflection pattern.

### Depression & Lake Processing (DEM conditioning)
Purpose: Precondition a DEM (fill/breach pits, map lakes) so downstream flow-routing components behave correctly - a prerequisite step, not itself a hazard product.
Today: DepressionFinderAndRouter already runs INSIDE the signed landslide_probability chain's FlowAccumulator call (depression_finder="DepressionFinderAndRouter") but is not independently selectable/tunable, and the computed fill-depth is discarded rather than surfaced as its own layer. LakeMapperBarnes (the faster Barnes et al. algorithm) is unsurfaced. BOTH now LANDED (ADR 0141): `landlab_dem_conditioning` (fill-depth COG via LakeMapperBarnes) + `landlab_lake_mapping` (lake extent/depth + vector), shared plumbing.
Aspects: fill-based pit removal (mass-preserving, raises depression floor); breach-based pit removal (cuts an outlet channel, realistic for incised/engineered terrain); lake identification/tracking as its own output
- [LANDED] `dem_pit_fill_conditioning_layer` [S] [US] (ADR 0141: `landlab_dem_conditioning`) - Show me where and how much this DEM needed to be filled before routing flow across it - is my DEM actually routable?
  src: https://landlab.readthedocs.io/en/latest/tutorials/overland_flow/how_to_d4_pitfill_a_dem.html (landlab_d4_pitfill_tutorial)
  knobs: fill vs breach, fill_flat, redirect_flow_steepest_descent
  notes: Surfaces the ALREADY-COMPUTED fill-depth (currently discarded) as its own COG. Tutorial explicitly uses FlowAccumulator(depression_finder="LakeMapperBarnes") - the faster alternative to today's DepressionFinderAndRouter.
- [LANDED] `lake_extent_and_depth_mapping` [S] [US] (ADR 0141: `landlab_lake_mapping`; REVISED ADR 0145 after NATE caught zero discrimination: min_lake_depth_m/min_lake_area_m2 floors, n_lakes_raw vs n_lakes_kept loud, closed-basin-not-existing-water semantic stated; Boulder 45->1, Horsetooth 215->7) - Are there real lakes or ponds on this landscape (not just DEM noise-pits) - where are they and how deep?
  src: https://landlab.readthedocs.io/en/latest/tutorials/overland_flow/how_to_d4_pitfill_a_dem.html (landlab_d4_pitfill_tutorial)
  knobs: method(Steepest), track_lakes on, redirect_flow_steepest_descent
  notes: Same LakeMapperBarnes call as the pit-fill candidate above, with lake tracking enabled instead of discarded - reuses the same worker plumbing.

### Terrain / Drainage-Network Analysis (diagnostic metrics)
Purpose: Extract quantitative drainage-network descriptors from an already-routed DEM - channel steepness, drainage density, basin scaling - diagnostic metrics rather than process models.
Today: Unsurfaced. Hack's Law is LANDED as `landlab_hacks_law_scaling` (ADR 0141): exponent fit + basin vector (Boulder foothills smoke: 0.566, in the classic range). ChiFinder/drainage-density remain CAND-M.
Aspects: normalized channel steepness / chi-based concavity mapping; drainage density (channel-network extent per unit area); Hack's-law basin-shape scaling
- [CAND-M] `channel_steepness_chi_map` [M] [US] - Which channel reaches in this watershed are anomalously steep for their drainage area (a common tectonic-activity / knickpoint proxy)?
  src: https://landlab.readthedocs.io/en/latest/tutorials/terrain_analysis/chi_finder/chi_finder.html (landlab_chi_finder_tutorial)
  knobs: reference_concavity theta (~0.5 default), minimum drainage area for channel definition
  notes: Published US case: NASADEM snippet over West Bijou Creek escarpment, Colorado high plains.
- [CAND-M] `drainage_density_index` [M] [US] - How densely channelized is this watershed, and how does that compare across sub-basins?
  src: https://landlab.readthedocs.io/en/latest/tutorials/terrain_analysis/drainage_density/drainage_density.html (landlab_drainage_density_tutorial)
  knobs: channel-definition method (area-slope threshold vs supplied channel mask), threshold value
  notes: Outputs a scalar drainage-density value + a surface_to_channel__minimum_distance field per node.
- [LANDED] `hacks_law_basin_scaling_diagnostic` [S] [US] (ADR 0141: `landlab_hacks_law_scaling`) - Does this basin's longest-flow-path-vs-drainage-area scaling match the classic Hack's Law exponent (~0.5-0.6), or is it anomalous?
  src: https://landlab.readthedocs.io/en/latest/tutorials/terrain_analysis/hack_calculator/hack_calculator.html (landlab_hack_calculator_tutorial)
  knobs: channel threshold, single-basin vs multi-basin batch
  notes: Pure diagnostic on top of the already-computed FlowAccumulator fields; same NASADEM/West Bijou Creek published case as ChiFinder.

### Network Sediment Transport (link-based river-network grid)
Purpose: Track discrete sediment parcels moving through a LINK-based river network (NetworkModelGrid, not a raster) - grain-size sorting, storage/burial, network-scale sediment budgets.
Today: Unsurfaced. TRID3NT has no NetworkModelGrid-based capability at all today - everything is RasterModelGrid - so this is a new grid paradigm, not a knob addition.
Aspects: parcel-based transport with grain-size-dependent mobility; initializing parcels from real river-network data (NHDPlus HR, shapefiles); episodic sediment pulses (e.g. a landslide or dam-removal input) into an existing network
- [CAND-L] `network_sediment_parcel_routing_and_sorting` [L] [US] - Track how sediment grain size sorts and moves downstream through this river network over time.
  src: https://landlab.readthedocs.io/en/latest/tutorials/network_sediment_transporter/network_sediment_transporter.html (landlab_nst_tutorial)
  knobs: parcel D50/volume distribution, network topology source (synthetic|shapefile|NHDPlus HR), simulation duration
  notes: Biggest architectural lift in this roster (new NetworkModelGrid + DataRecord parcel tracking). Published qualitative check: smaller parcels transport farther than coarse material.
- [CAND-L] `sediment_pulse_from_hazard_event` [L] [US] - A landslide just dumped sediment into this channel network - how does that pulse move and disperse downstream over the following years?
  src: https://landlab.readthedocs.io/en/latest/tutorials/network_sediment_transporter/network_sediment_transporter.html (landlab_nst_tutorial)
  knobs: pulse volume/grain-size distribution, injection link and time
  notes: Same NetworkSedimentTransporter core as above using its SedimentPulserAtLinks/EachParcel companions; natural composition with the Landslides module's BedrockLandslider output (hazard-chain -> sediment-fate chain).

### Climate & Precipitation Generators (stochastic forcing)
Purpose: Generate the stochastic storm/tide forcing that drives other process components, rather than requiring a hand-supplied fixed hyetograph.
Today: Unsurfaced as a reusable forcing tool - the signed overland_flow chain's storm_duration_hr/rainfall_intensity_mm_hr are fixed scalars, not a drawn stochastic sequence.
Aspects: point (spatially-uniform) stochastic storm generator (Poisson storm/interstorm/depth); tidal-cycle-averaged flow-velocity forcing for estuarine/marsh AOIs
- [CAND-M] `stochastic_storm_sequence_generator` [M] [US] - Instead of one fixed design storm, drive this analysis with a realistic multi-year sequence of storm/interstorm events.
  src: https://landlab.readthedocs.io/en/latest/generated/api/landlab.components.uniform_precip.generate_uniform_precip.html (landlab_precipitationdistribution_api)
  knobs: mean_storm_duration, mean_interstorm_duration, mean_storm_depth, total_t, random_seed
  notes: Reusable forcing utility other chains (groundwater, overland flow, landslide-probability ensembles) can all consume - a genuine capability upgrade over today's fixed-scalar storms.
- [CAND-L] `tidal_cycle_flow_forcing` [L] [US] - For this coastal marsh/estuary AOI, what does tidal-cycle-averaged flow velocity look like (ebb vs flood), and how does channel vs marsh roughness change it?
  src: https://landlab.readthedocs.io/en/latest/tutorials/tidal_flow/tidal_flow_calculator.html (landlab_tidal_flow_tutorial)
  knobs: tidal_range, tidal_period, mean_sea_level, spatially-varying Manning's n (channel vs marsh)
  notes: New coastal/tidal physics, distinct from the storm-rainfall chains. TRID3NT's coastal hazard today is SFINCS-only (screening-grade per the model-fidelity doctrine) - this would be a tidal-circulation companion, not a flood-depth replacement; worth a scope discussion before building.

### River Dynamics (2D shallow-water on a defined reach)
Purpose: Depth-averaged 2D shallow-water flow within a specific channel/reach (distinct from watershed-scale overland flow) - for in-channel hydraulics with defined inlet boundary conditions.
Today: Unsurfaced.
Aspects: idealized rectangular-channel flow with fixed inlet depth/velocity boundary conditions; real natural-topography channel/side-channel flow
- [CAND-L] `instream_2d_shallow_water_reach_flow` [L] [US] - Model 2D depth-averaged flow through this specific reach or side-channel given a known inflow, rather than watershed-wide overland routing.
  src: https://landlab.readthedocs.io/en/latest/tutorials/river_flow_dynamics/river_flow_dynamics_tutorial.html (landlab_river_flow_dynamics_tutorial)
  knobs: Manning's n, inlet depth/velocity boundary condition, channel slope
  notes: Different discretization/physics register from OverlandFlow (semi-implicit semi-Lagrangian shallow water on a reach). Published real-DEM case: a natural side-channel of the Kootenai River, Idaho (US-applicable).

### Weathering
Purpose: Convert bedrock to mobile regolith/soil at a rate that declines exponentially as soil thickens - the missing 'soil production' half of the diffusion/landslide chains, which today assume a fixed constant soil thickness.
Today: Unsurfaced - the signed landslide_probability chain uses a FIXED DEFAULT_SOIL_THICKNESS_M=1.0 constant rather than a weathering-produced, spatially-varying soil-thickness field.
Aspects: exponential bedrock-to-soil production law (Ahnert 1976 formulation)
- [CAND-M] `bedrock_to_soil_production_coupling` [M] [US] - Instead of assuming a uniform 1m soil mantle everywhere, let soil thickness emerge from a weathering-vs-erosion balance so the landslide susceptibility map reflects where soil has actually had time to accumulate.
  src: https://landlab.readthedocs.io/en/latest/generated/api/landlab.components.weathering.exponential_weathering.html (landlab_exponentialweatherer_api)
  knobs: soil_production_maximum_rate (w0), soil_production_decay_depth (w_star)
  notes: No dedicated worked tutorial found this pass (API doctest only, verified: default params give soil_production__rate=1.0 m/yr at zero soil depth) - couples ExponentialWeatherer's output soil__depth field into the existing landslide chain's DEFAULT_SOIL_THICKNESS_M in place of the constant.

### Terrain-relative wetness / HAND (minor, single-component)
Purpose: Fast, hydrology-model-free elevation-above-nearest-drainage layer as a wetness/flood-susceptibility proxy.
Today: Unsurfaced. LANDED as `landlab_hand_wetness` (ADR 0141): HAND COG + channel vector via HeightAboveDrainageCalculator (API-doctest-verified).
Aspects: height-above-nearest-drainage (HAND) calculation from an existing channel mask
- [LANDED] `hand_wetness_proxy_layer` [S] [US] (ADR 0141: `landlab_hand_wetness`) - Give me a HAND (height-above-nearest-drainage) layer as a fast flood-susceptibility/wetness proxy for this AOI, without running a full hydraulic model.
  src: https://landlab.readthedocs.io/en/latest/generated/api/landlab.components.hand_calculator.hand_calculator.html (landlab_handcalculator_api)
  knobs: channel_mask source (from FlowAccumulator threshold or supplied)
  notes: No dedicated worked tutorial found this pass (API doctest only, verified: a 4x5 grid case reproduces the exact published HAND array via the Nobre et al. 2011 method) - cheap single-component add reusing the channel mask other terrain-analysis candidates already compute.

Roster gaps (LANDLAB): Components confirmed to exist (via the verified landlab.readthedocs.io component_list.html index) but NOT given a dedicated verified template candidate this pass, because a standalone worked tutorial with expected output could not be confirmed via WebFetch in the time/search budget available (WebSearch budget was exhausted mid-session): OverlandFlowBates, LinearDiffusionOverlandFlowRouter, KinematicWaveRengers (overland-flow variants - API-only, roll into the Overland Flow module later); DepthDependentDiffuser, DepthDependentTaylorDiffuser, PerronNLDiffuse, DischargeDiffuser (diffusion variants); DetachmentLtdErosion, DepthSlopeProductErosion, ErosionDeposition, GravelBedrockEroder, LateralEroder, ThresholdEroder, AreaSlopeTransporter (erosion variants beyond the 3 mined); LossyFlowAccumulator, PotentialityFlowRouter, FlowDirectorD8/DINF/MFD standalone tutorials (flow-routing variants - the FlowAccumulator tutorial mentions but doesn't deep-dive them); ExponentialWeathererIntegrated; DimensionlessDischarge; VegetationDynamics/SoilMoistureDynamics/PotentialEvapotranspiration/Radiation as standalone (only verified coupled inside the CATGRaSS chain); SpatialPrecipitationDistribution, FireGenerator; SimpleSubmarineDiffuser, CarbonateProducer (marine/submarine family - not mined this pass); Flexure1D, ListricKinematicExtender, gFlex (tectonics/flexure variants beyond the 2 mined); BedParcelInitializer* and SedimentPulserAtLinks/EachParcel as standalone tutorials (verified only as referenced within the NetworkSedimentTransporter tutorial); ConcentrationTrackerForDiffusion/ForSpace; AdvectionSolverTVD, FractureGridGenerator (utility/grid-generation components, likely trivial-effort if ever needed). None of these were guessed at or given a fabricated source_url - they are named here per doctrine rather than included as unverified candidates above. A dedicated LakeMapperBarnes-only tutorial (distinct from the D4 pit-fill tutorial cited above) and a standalone HeightAboveDrainageCalculator/ExponentialWeatherer worked notebook (beyond the API-doc doctests cited) were also not found live this pass.


## TELEMAC - SUPPLEMENTARY MINING (7 modules; NOTE: the agent assigned the cross-engine nuance matrix returned this second TELEMAC pass instead - the matrix is QUEUED as a re-run)

### TELEMAC-2D
Purpose: Depth-averaged (shallow-water/Saint-Venant) free-surface hydrodynamics for rivers, estuaries, coasts and harbours, including tracer/dye transport.
Today: SURFACED - telemac_river_dye (2D + tracer) is live; this supplementary pass adds V&V-class candidates.
Aspects: shallow-water hydrodynamics (finite element, unstructured mesh); dam-break / extreme flood wave propagation; tsunami propagation and runup; tidal and storm-surge boundary forcing; passive tracer/dye transport; wetting-drying on complex/urban terrain
- [CAND-L] `unstructured_mesh_dam_break_validation` [L] [non-US] - Can TRID3NT reproduce the Malpasset dam-break flood wave (1959, France) on an unstructured mesh and match the LNH 1/400-scale physical-model gauge time series and inundation extent?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac2d/malpasset (opentelemac-examples/telemac2d/malpasset (t2d_malpasset-large.cas, t2d_malpasset-small*.cas + geo_malpasset*.slf, verified file listing))
  knobs: mesh (large vs small refined), friction law (t2d_malpasset-small_charac vs _prim variants), numerical scheme (kinetic _cin vs primitive _pos/_prim), advection char./ERIA variants
  notes: Canonical dam-break V&V case for unstructured 2D solvers (also used to cross-check GeoClaw/HEC-RAS per the Journal of Hydraulic Engineering discussion thread found in search); published gauge comparison against LNH physical model.
- [CAND-M] `tsunami_runup_benchmark_monai_valley` [M] [non-US] - Can TRID3NT replicate the 1993 Okushiri/Monai Valley laboratory tsunami runup benchmark (published NOAA/Cornell time-series gauges) on TELEMAC-2D's unstructured mesh?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac2d/monai_valley (opentelemac-examples/telemac2d/monai_valley (t2d_monai.cas, geo_monai.slf/.cli, initial_wave.prn, verified file listing))
  knobs: initial wave time-series (initial_wave.prn) as boundary forcing, mesh resolution near the conical island, wetting-drying threshold
  notes: Same canonical benchmark already used for GeoClaw per project memory (reference_geoclaw_tsunami_tutorial_templates.md) - gives a same-case cross-engine V&V pair inside TRID3NT's existing tsunami doctrine.
- [CAND-L] `okada_fault_source_tsunami_propagation` [L] [US] - Can TRID3NT drive TELEMAC-2D tsunami propagation directly from an Okada (1985) finite-fault rupture source rather than a prescribed wave time-series?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac2d (opentelemac-examples/telemac2d/okada (directory confirmed present in verified 60-case listing))
  knobs: fault geometry (strike/dip/rake/slip), source-to-nearshore propagation distance, mesh coarsening offshore vs refinement nearshore
  notes: Matches the existing ResearchGate-published 'Testing TELEMAC-2D suitability for tsunami propagation from source to near shore' study found in search; relevant to US Pacific NW / Alaska tsunami source scenarios.
- [CAND-M] `tidal_storm_surge_boundary_forcing` [M] [US] - Can TRID3NT set up TELEMAC-2D with tidal harmonic or storm-surge time-series open-boundary forcing for an estuary/coastal domain?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac2d (opentelemac-examples/telemac2d/tide (directory confirmed present in verified 60-case listing))
  knobs: tidal constituents vs prescribed stage hydrograph, liquid boundary type, Coriolis on/off

### TELEMAC-3D
Purpose: Three-dimensional (hydrostatic or non-hydrostatic) Navier-Stokes hydrodynamics with active/passive tracer transport, for stratified, thermal or short-wave problems 2D depth-averaging cannot resolve.
Today: Not surfaced; no 3D baroclinic/stratified solver currently in TRID3NT's engine roster (SFINCS/HEC-RAS/MODFLOW are 2D or Darcy-scale).
Aspects: 3D baroclinic/stratified circulation; thermal stratification and heat exchange; salinity intrusion / density-driven flow; vertical sigma/z-layer discretization tradeoffs; particle tracking
- [CAND-M] `thermal_stratification_reservoir` [M] [US] - Can TRID3NT model vertical thermal stratification and heat exchange with the atmosphere in a lake/reservoir using TELEMAC-3D?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac3d (opentelemac-examples/telemac3d/heat_exchange, /stratification, /stratif_wind (directories confirmed present in verified 37-case listing))
  knobs: vertical layering (sigma count), heat-exchange formula, wind-driven mixing on/off
- [CAND-L] `salinity_intrusion_estuary` [L] [US] - Can TRID3NT reproduce salt-wedge / density-driven salinity intrusion in a partially-mixed estuary with TELEMAC-3D active tracer transport?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac3d (opentelemac-examples/telemac3d/lock-exchange, /tidal_flats (directories confirmed present in verified 37-case listing))
  knobs: baroclinic pressure gradient on/off, tidal forcing amplitude, vertical diffusivity closure
- [CAND-L] `dam_break_3d_cross_check` [L] [non-US] - Does the 3D Malpasset case (non-hydrostatic) change the flood-wave arrival times versus the 2D depth-averaged Malpasset result already validated in TELEMAC-2D?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/telemac3d (opentelemac-examples/telemac3d/malpasset (directory confirmed present in verified 37-case listing))
  knobs: hydrostatic vs non-hydrostatic mode, number of vertical planes
  notes: Same benchmark as TELEMAC-2D candidate - a 2D-vs-3D fidelity-ladder rung on one documented case, directly serving the model-fidelity-ladder doctrine.

### GAIA
Purpose: Unified sediment-transport and bed-evolution (morphodynamics) module - successor to SISYPHE - covering bedload, suspended load and total load across rivers, coastal seas and transitional waters, coupled to TELEMAC-2D/3D.
Today: Not surfaced; no morphodynamic/bed-evolution engine in TRID3NT today (contrast: HEC-RAS 1D/2D sediment exists as a candidate elsewhere on the roster).
Aspects: bedload transport formulas (multi-fraction/multi-class sediment); suspended-load and total-load coupling to hydrodynamics; bed evolution / morphodynamic feedback on the mesh; coastal dune/sandbar migration; river/estuary channel morphology (dredging, scour)
- [CAND-L] `multiclass_sediment_bed_evolution` [L] [US] - Can TRID3NT run a coupled TELEMAC-2D/GAIA multi-grain-class bedload+suspended-load simulation and reproduce a documented trench-infill or bar-formation laboratory experiment?
  src: https://gitlab.pam-retd.fr/otm/telemac-mascaret/-/tree/v8p5/examples/gaia (gitlab.pam-retd.fr otm/telemac-mascaret examples/gaia (v8p5 tag - page verified reachable, JS-rendered listing not extractable by fetch tool; see roster_gaps))
  knobs: grain-size classes, transport formula (Meyer-Peter-Muller / Van Rijn / Engelund-Hansen selectable in GAIA), morphodynamic time-acceleration factor
  notes: Backed by the peer-reviewed 'GAIA - a unified framework for sediment transport and bed evolution ... in the TELEMAC-MASCARET system' (ResearchGate, found in search) and 'Simulation of embayment lab experiments with TELEMAC-2D/GAIA' (Academia.edu).
- [CAND-L] `coastal_dune_migration_3d_coupling` [L] [US] - Can TRID3NT couple TELEMAC-3D hydrodynamics to GAIA to reproduce residual-flow-driven marine dune/sandwave migration?
  src: https://www.vliz.be/imisdocs/publications/369805.pdf (VLIZ IMIS publication 369805, 'Sediment transport modelling (TELEMAC-3D + GAIA) case study' (found via search, PDF host))
  knobs: 3D vs 2D hydrodynamic driver, wind/wave forcing on residual currents, bed roughness formulation
- [DOC] `sisyphe_gaia_migration_path` [S] [US] - For legacy TELEMAC setups authored against SISYPHE (the module GAIA replaced), can TRID3NT auto-translate steering-file sediment blocks to GAIA's newer keyword set? [DOC/STOP 2026-08-05 (ADR 0154): TRID3NT AUTHORS GAIA decks natively (worker write_gaia_deck; GAIA v1 live) and STANDARDIZES on GAIA - it has NO legacy-SISYPHE-deck ingestion path, so a SISYPHE->GAIA translator serves no internal workflow. Decision: standardize on GAIA (status quo). Recipe if external-deck ingestion is ever added: parse SISYPHE .cas sediment keywords -> GAIA keyword map against gaia.dico v9.0.]
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples (opentelemac-examples/sisyphe (top-level directory confirmed present alongside gaia's absence in this older mirror - evidence GAIA postdates this snapshot))
  knobs: n/a - tooling/compat aspect, not a physics knob
  notes: Not a modeling template but a migration-utility candidate: the two module families coexist in the wild; TRID3NT should decide which one it standardizes template authorship on.

### TOMAWAC
Purpose: Third-generation spectral (phase-averaged) wave model solving the wave-action balance equation for regional/offshore-to-nearshore sea states - significant wave height, period, direction, wave-induced currents.
Today: Not surfaced; TRID3NT's only wave-adjacent capability today is SFINCS's SnapWave (screening-grade, per SFINCS North Star demo doctrine) and the queued SWAN roster entry.
Aspects: deep-water/offshore spectral wave generation (wind fetch, JONSWAP); depth-induced shoaling and breaking (BJ78 etc.); wave-current interaction / opposing currents; bottom-friction dissipation; coupling to TELEMAC-2D/3D for wave-driven currents
- [CAND-M] `fetch_limited_wind_wave_growth` [M] [US] - Can TRID3NT reproduce fetch-limited JONSWAP wind-wave growth (a documented spectral benchmark) with TOMAWAC?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/tomawac (opentelemac-examples/tomawac/fetch_limited, /dean, /turning_wind (directories confirmed present in verified 15-case listing))
  knobs: wind speed/duration, fetch distance, source-term parameterization
  notes: JONSWAP validation numbers found in search (Hs, Tp, cutoff frequency examples) - published expected spectral shape available.
- [CAND-M] `nearshore_shoaling_breaking_benchmark` [M] [US] - Can TRID3NT reproduce the depth-induced shoaling/breaking transformation of significant wave height over a sloping bathymetry profile (BJ78 breaker model) with TOMAWAC?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/tomawac (opentelemac-examples/tomawac/shoal, /deferl_bj78 (directories confirmed present in verified 15-case listing))
  knobs: breaker model (BJ78 gamma coefficient, Miche criterion), bottom-friction formula
  notes: Search confirmed BJ78 breaker-model validation reproduces Hs variation along a bathymetry profile with published scatter index (~18.2% in one cross-model study).
- [CAND-M] `wave_current_opposing_interaction` [M] [US] - Can TRID3NT model wave-height amplification/steepening where waves oppose a strong tidal or river current using TOMAWAC?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/tomawac (opentelemac-examples/tomawac/opposing_current, /whirl_current (directories confirmed present in verified 15-case listing))
  knobs: current field source (prescribed vs TELEMAC-2D coupled), current magnitude

### ARTEMIS
Purpose: Phase-resolving elliptic mild-slope-equation solver for wave propagation toward shore and agitation inside harbours (refraction, diffraction, partial reflection, breaking).
Today: Not surfaced; no harbour-agitation/mild-slope capability exists in TRID3NT today.
Aspects: harbour tranquility / wave agitation behind breakwaters; refraction-diffraction around structures and islands; partial reflection off quay walls/revetments; phase-resolving vs phase-averaged fidelity tradeoff (vs TOMAWAC)
- [CAND-L] `harbour_tranquility_breakwater_agitation` [L] [US] - Can TRID3NT compute wave agitation behind a harbour breakwater and compare it against field-observed wave heights, matching ARTEMIS's published better-than-TOMAWAC onshore accuracy?
  src: https://ouci.dntb.gov.ua/en/works/leRGPGa9/ (Jang, H. et al., 'Wave Transformation behind a Breakwater in Jukbyeon Port, Korea - A Comparison of TOMAWAC and ARTEMIS of the TELEMAC System', J. Marine Sci. Eng. 10(12):2032, DOI 10.3390/jmse10122032 (fetched via ouci.dntb.gov.ua mirror after MDPI 403; content verified))
  knobs: reflection coefficient per structure face, incident spectrum (from TOMAWAC boundary or prescribed), mesh density in the harbour basin
  notes: Published finding: ARTEMIS (phase-resolving) beat TOMAWAC (phase-averaged) onshore/in-lee, but TOMAWAC was slightly better offshore where ARTEMIS suffered spurious reflections off complex coastlines - directly informs the phase-resolving-vs-averaged fidelity line.
- [CAND-M] `island_diffraction_refraction` [M] [US] - Can TRID3NT reproduce classical diffraction/refraction around an island or breakwater tip with ARTEMIS's mild-slope solver?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/artemis (opentelemac-examples/artemis/ile_para, /recif, /kochin (directories confirmed present in verified 22-case listing))
  knobs: incident wave period/direction, island/reef geometry, bottom friction
- [CAND-L] `artemis_tomawac_fidelity_pairing` [L] [US] - Given one incident offshore sea state, can TRID3NT run both TOMAWAC (regional, phase-averaged) and ARTEMIS (harbour, phase-resolving) on nested domains and hand off the boundary spectrum automatically?
  src: https://ouci.dntb.gov.ua/en/works/leRGPGa9/ (same Jukbyeon Port JMSE paper (verified) - describes exactly this two-model nesting workflow)
  knobs: nesting boundary location, spectral-to-monochromatic conversion for ARTEMIS's incident condition
  notes: This is the template that operationalizes the WWM/SWAN-style overlap doctrine internally to TELEMAC: two wave physics tiers on the same estuary, chosen by scale.

### WAQTEL
Purpose: Water-quality module (biochemical, thermal and micropollutant processes) coupled to TELEMAC-2D/3D hydrodynamic transport - dissolved oxygen, eutrophication, biomass, thermal budget, micropollutant fate.
Today: Partially adjacent: TRID3NT already ships SWMM water quality (buildup/washoff, per commit 7f402a22 / 3ba58243, SWMM-WQ-1) for the urban/pipe-network scale; WAQTEL would be the open-water/receiving-water counterpart, not currently surfaced.
Aspects: dissolved oxygen / BOD (O2 sub-model); eutrophication (EUTRO: phytoplankton, N, P cycling); micropollutant fate and transport (MICROPOL); thermal budget / heat exchange (THERMIC); biomass / algal dynamics (BIOMAS); coupling to AED2 (Aquatic Ecosystem Dynamics) for advanced 3D lake ecology
- [CAND-M] `dissolved_oxygen_bod_channel_validation` [M] [US] - Can TRID3NT reproduce the analytical dissolved-oxygen solution for constant/pulse BOD injection in a simple 1D/2D channel using WAQTEL's O2 sub-model?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/waqtel (opentelemac-examples/waqtel/waq2d_o2, /waq3d_o2 (directories confirmed present in verified 12-case listing))
  knobs: reaeration formula (K2 selectable: O'Connor-Dobbins, etc.), BOD decay rate K1, saturation O2
  notes: Search confirmed a published analytical-vs-TELEMAC-WAQTEL-O2 verification for constant and pulse DO injections in 1D and 2D channels.
- [CAND-L] `lagoon_eutrophication_risk` [L] [non-US] - Can TRID3NT run WAQTEL's EUTRO sub-model (8 reactive state variables: DO, N, P, phytoplankton biomass) to assess eutrophication risk in a coastal lagoon?
  src: https://pubmed.ncbi.nlm.nih.gov/36462031/ ('Evaluating the eutrophication risk of artificial lagoons - case study El Gouna, Egypt' (PMC9719455 / PubMed 36462031, TELEMAC-2D + WAQTEL-EUTRO, verified accessible abstract))
  knobs: nutrient loading boundary conditions, residence-time driven by hydrodynamics, seasonal temperature forcing
  notes: Non-US case, but the ONLY located published-with-metrics EUTRO application; flag for a US-lagoon (e.g. Gulf Coast/Chesapeake) re-run before adoption per the US-cases-paper-first-replication doctrine.
- [STOP] `micropollutant_fate_validation_case` [S] [US] - Can TRID3NT stand up WAQTEL's MICROPOL sub-model (organic-load degradation, nitrification, algal uptake) using the shipped validation steering file as a golden regression case? [STOP 2026-08-05 (ADR 0154): the row's premise (golden regression FROM the shipped validation steering file) FAILS - examples/waqtel/waq2d_micropol/micropol_steer.cas does NOT ship in our telemac:latest image (no examples tree, verified twice: only builds/configs/scripts/sources) and the worker ingests no external decks (it authors from US NHDPlus reaches). Recipe: vendor the micropol deck + its geometry (.slf/.cli) into the image + add a shipped-deck-runner path (distinct from the authored-from-reach pipeline), then pin the WAQ MICROPOL validation numbers (K120=0.35, K520=0.35, ...) as a regression.]
  src: https://github.com/ogoe/OpenTelemac/blob/master/examples/waqtel/waq2d_micropol/micropol_steer.cas (opentelemac-examples/waqtel/waq2d_micropol/micropol_steer.cas (fetched and parsed: title 'WAQ MICROPOL: VALIDATION CASE', K120=0.35, K520=0.35, max photosynthesis rate 2.0/d @20C, K2=0.3, mortality/respiration 0.05, verified content))
  knobs: K120 (organic degradation), K520 (nitrification), photosynthesis max rate, K2 reaeration, mortality/respiration rate
  notes: Already-shipped official validation case with explicit numeric parameters extracted - lowest-effort WAQTEL template (S: knobs on an existing published case).
- [CAND-M] `thermal_budget_heat_exchange` [M] [US] - Can TRID3NT model a receiving-water thermal budget (e.g. thermal discharge plume cooling) with WAQTEL's THERMIC sub-model coupled to TELEMAC-3D stratification?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/waqtel (opentelemac-examples/waqtel/waq2d_thermic (directory confirmed present in verified 12-case listing))
  knobs: heat-exchange formula, ambient meteorology forcing, discharge temperature/flow
- [CAND-L] `aed2_lake_ecology_coupling` [L] [US] - Can TRID3NT couple WAQTEL/TELEMAC-3D to the AED2 (Aquatic Ecosystem Dynamics) library for advanced 3D lake biogeochemistry beyond WAQTEL's native sub-models?
  src: https://api.github.com/repos/ogoe/OpenTelemac/contents/examples/waqtel (opentelemac-examples/waqtel/waq3d_aed2, /waq3d_aed2_flume (directories confirmed present in verified 12-case listing))
  knobs: AED2 process library selection, vertical layering for biogeochemical gradients
  notes: AED2 is the same ecosystem-model family used by GLM/other US lake-management tools - a plausible cross-engine credibility anchor.

### CROSS-CUTTING OVERLAP COMPARISON MATRIX
Purpose: For the four NATE-specified overlap pairs, capture the physics/scale/coupling nuance that justifies keeping both engines on the roster rather than collapsing to one - this is the fidelity-line feedstock, not a template list.
Today: n/a - analysis artifact, not a surfaced capability.
Aspects: WWM vs SWAN (spectral wave models); GAIA vs SED3D vs HEC-RAS (sediment transport); WAQTEL vs ICM vs SWMM-WQ (water quality); rain-on-grid: HEC-RAS vs SWMM vs SFINCS pluvial
- [DOC] `overlap_wwm_vs_swan` [S] [US] - WWM (Wind Wave Model) vs SWAN - when does each win? [DOC 2026-08-05 (ADR 0154): DOC-class fidelity guidance landed in the adapter system-prompt "Cross-engine OVERLAP routing" block (in-context every turn), grounded in what ships: schism_coupled_waves is SCHISM+WWM (tight two-way coupling); swan_wave_field is standalone SWAN; TRID3NT surfaces WWM ONLY inside SCHISM (no standalone WWM tool - claim verified vs the real template surface). Not a simulation.]
  src: https://oceanpredict.org/docs/Documents/Task%20Teams/COSS-TT/Meetings/June-2025/Orals/2.1-Wed-2-Herzfeld.pdf (OceanPredict COSS-TT June 2025 orals, 'Wave-flow coupling of SWAN with an unstructured model' (found via search) + SCHISM-WWM-III coupling literature (search-confirmed, not individually WebFetched))
  notes: NUANCE: SWAN is purpose-built nearshore/coastal and ships as a standalone third-gen spectral solver widely coupled to structured/unstructured hydrodynamic models (incl. TELEMAC via TOMAWAC-class coupling); it is documented to underestimate nearshore significant wave height because it omits low-frequency infragravity-wave energy (search-confirmed). WWM is the wave engine natively co-developed and tightly coupled inside SCHISM (SCHISM-WWM-III / prior SELFE-WWM-II), giving implicit two-way current-wave feedback at every SCHISM timestep rather than a looser offline/online coupling interval. Practical line: pick WWM+SCHISM when the study needs tight wave-current feedback on an unstructured circulation mesh already in SCHISM; pick SWAN when the wave field is the primary deliverable and can be coupled more loosely to any host circulation model (including TELEMAC/TOMAWAC's own spectral solver, which is architecturally the TELEMAC-family analogue of this same tradeoff).
- [DOC] `overlap_gaia_vs_sed3d_vs_hecras_sediment` [S] [US] - GAIA vs SED3D vs HEC-RAS sediment - which for which scale/question? [DOC 2026-08-05 (ADR 0154): DOC-class guidance in the adapter overlap block: the GAIA sediment mode of telemac_river_dye is the surfaced morphodynamic path; SED3D (EPA) is archived/defunct (verified) - never offered; HEC-RAS 2D sediment is NOT surfaced in TRID3NT (only HEC-RAS hydraulics). Claims verified vs the real surfaces. Not a simulation.]
  src: https://www.epa.gov/hydrowq/sed3d (EPA Hydrologic & Water Quality System page for SED3D (verified: 'This model has been archived and is no longer available on this website') + HEC-RAS 2D Sediment User Manual (hec.usace.army.mil/confluence/rasdocs/h2sd/ras2dsed/6.4/sediment-data, verified reachable) + GAIA framework paper (ResearchGate, found via search))
  notes: HONEST FINDING: SED3D (EPA/CEAM's 3D hydrodynamic-sediment-WASP linkage model for lakes/estuaries) is CONFIRMED ARCHIVED/DEFUNCT by EPA's own hydrowq page - it is not a live comparison candidate; note the acronym is also informally reused inside SCHISM for its 3D sediment sub-module (distinct codebase, per SCHISM docs found in search), which should not be conflated with the dead EPA tool. Of the two LIVE options: HEC-RAS 2D sediment (h2sd) is a US-standard, USACE-maintained, tightly regulatory-integrated 1D/2D coupled hydraulics+sediment tool oriented at channel/reservoir sediment yield and stable-channel design (confirmed via its own transport-function docs). GAIA is TELEMAC's unstructured-mesh, multi-fraction bed/suspended/total-load morphodynamic framework built for rivers THROUGH coastal/transitional waters in one unified formulation (per the cited GAIA framework paper), better suited to complex coastal morphology (dune migration, embayments) than HEC-RAS's channel-centric formulation. Roster implication: replace the SED3D leg of this pair with 'GAIA vs HEC-RAS-2D-sediment' going forward; SED3D is dead software.
- [DOC] `overlap_waqtel_vs_icm_vs_swmmwq` [S] [US] - WAQTEL vs ICM vs SWMM-WQ water quality - receiving water vs pipe network vs urban catchment? [DOC 2026-08-05 (ADR 0154): DOC-class guidance in the adapter overlap block: SWMM-WQ = urban catchment->pipe->outfall load (shipped); WAQTEL = receiving-water fate, reachable as the telemac_river_dye decay mode (substance=sewage/effluent); chain SWMM-WQ -> telemac decay for source-to-receiving-water. ICM (commercial InfoWorks) is NOT in TRID3NT - use the SWMM+telemac combination. Not a simulation.]
  src: https://pubmed.ncbi.nlm.nih.gov/36462031/ (El Gouna WAQTEL-EUTRO/O2 case (verified) + EPA SWMM water-quality description (epa.gov/water-research/storm-water-management-model-swmm, verified reachable) - ICM (Innovyze/Autodesk InfoWorks ICM) comparison NOT independently WebFetched this session (search budget exhausted); characterization below is flagged lower-confidence and listed in roster_gaps)
  notes: NUANCE (partially unverified - see roster_gaps): SWMM-WQ (already shipped in TRID3NT per commit 3ba58243/7f402a22) is a catchment-to-pipe buildup/washoff + in-pipe routing model - its native scale is the urban drainage network from land surface to outfall, with per-pollutant EMC/build-up kinetics. WAQTEL is a RECEIVING-water biogeochemical model (DO/BOD, eutrophication N-P-phytoplankton, micropollutant fate, thermal budget) coupled to open-water 2D/3D hydrodynamics (TELEMAC) - it picks up where SWMM-WQ's outfall load is deposited, modeling what that load DOES in the lake/estuary/river (reaeration, algal uptake, decay), not how it built up on the street. ICM (InfoWorks ICM) is understood from general domain knowledge (not verified this session) to be a commercial integrated 1D pipe-network + 2D overland + water-quality package spanning both ends (buildup/washoff AND limited receiving-water quality) in one proprietary tool, competing directly with a SWMM-WQ+WAQTEL combination - this claim needs a citation pass before it is trusted.
- [DOC] `overlap_rain_on_grid_hecras_swmm_sfincs` [S] [US] - Rain-on-grid: HEC-RAS 2D vs SWMM vs SFINCS pluvial - which physics/scale for which pluvial question? [DOC 2026-08-05 (ADR 0154): DOC-class guidance in the adapter overlap block as a THREE-tier fidelity ladder: SFINCS (sfincs_flood) = fast reduced-physics SCREENING; HEC-RAS 2D (hecras_flood_2d) = full shallow-water overland REFINEMENT; SWMM (swmm_urban_flood) = when the drainage NETWORK topology is the object. Matches the existing model-fidelity-ladder doctrine (SFINCS screening-only). Not a simulation.]
  src: https://sfincs.readthedocs.io/en/latest/ (SFINCS ReadTheDocs overview (verified: 'enables rapid simulation of storm surge, riverine (fluvial), rainfall-runoff (pluvial), and wave-driven flooding from national, regional to local scales') + EPA SWMM description (verified) + HEC-RAS 2D User's Manual TOC (hec.usace.army.mil/confluence/rasdocs/r2dum/latest, verified reachable, no dedicated rain-on-grid subpage located this session))
  notes: NUANCE: SWMM treats rainfall through explicit sub-catchment hydrology (interception/infiltration/depression storage per sub-area) THEN routes runoff into a 1D pipe/channel network - it is a lumped-subcatchment-to-network model, cheapest and most mature for engineered urban drainage. HEC-RAS 2D rain-on-grid applies rainfall directly onto every 2D mesh cell with a cell-level infiltration loss method (Green-Ampt/SCS/etc., per the 2D Sediment/User manual family) and lets the shallow-water solver do ALL overland routing implicitly - no predefined sub-catchment delineation required, at higher compute cost; TRID3NT's existing model-fidelity-ladder doctrine already flags SFINCS as screening-only, never refinement-grade, which is exactly the rain-on-grid tradeoff: SFINCS's reduced-physics local-inertial solver (confirmed via its own docs to natively support pluvial forcing at 'national, regional to local scales') is the fast screening tier, HEC-RAS 2D rain-on-grid is the refinement tier for a given AOI, and SWMM is the correct tool specifically when the drainage network topology (pipes, inlets, weirs) is the object of the question rather than the free-surface overland flow field. This is a THREE-WAY, not two-way, fidelity ladder rung matching the doctrine's model-fidelity-ladder framing exactly.

Roster gaps (TELEMAC): Naming mismatch flagged up front: the task specified the target as the "QUALITY_OF_LIFE engine," but every source-roster item given (WWM vs SWAN, GAIA vs SED3D vs HEC-RAS sediment, WAQTEL vs ICM vs SWMM-WQ, rain-on-grid) is TELEMAC-family content, matching the existing project memory note "TELEMAC modules list = telemac template source." No "QUALITY_OF_LIFE" engine exists anywhere in the TRID3NT roster (checked against CLAUDE.md/memory engine list: SFINCS, MODFLOW, PySWMM/SWMM, HEC-RAS, GeoClaw, ELMFIRE, OpenQuake, Pelicun, Landlab, SWAN, TELEMAC). Treated `engine` as "TELEMAC" and proceeded on that assumption; if "QUALITY_OF_LIFE" was intentional (e.g. a real but undocumented engine codename), this entire deliverable is mis-scoped and should be redone against the correct roster note.

Access gaps: opentelemac.org and wiki.opentelemac.org (the CANONICAL official docs/module-presentation pages, e.g. the ARTEMIS/module-list pages) were unreachable from this session's WebFetch tool on both https and http ("unable to verify the first certificate" / connection refused) - likely a TLS chain issue on their server, not a dead link (search snippets confirm the pages exist with correct titles). Substituted gitlab.pam-retd.fr (official GitLab, reachable but JS-rendered so directory contents couldn't be text-extracted - only top-level example-folder URLs were confirmed live) and the community GitHub mirror github.com/ogoe/OpenTelemac (fully readable via its raw/API endpoints, used for verified file-listings and one parsed .cas file). The GitHub mirror predates GAIA's introduction (has `sisyphe/` but no `gaia/` folder), so GAIA candidates rely solely on the JS-opaque official GitLab path plus academic PDFs (ResearchGate/VLIZ) rather than a directly-parsed example file.

WebSearch budget was exhausted mid-task (shared session-wide cap, "200 of 200" reached) before two planned searches completed: a direct WWM-vs-SWAN engine-to-engine technical comparison beyond what was already surfaced, and an InfoWorks ICM water-quality documentation/citation. The WAQTEL-vs-ICM-vs-SWMM-WQ matrix entry's ICM characterization is consequently UNVERIFIED domain knowledge, not a WebFetch-confirmed claim, and is explicitly flagged as such in that candidate's notes - it should not be treated as citation-grade until a follow-up pass fetches Innovyze/Autodesk ICM water-quality docs directly.

SED3D (the EPA/CEAM 3D lake-estuary hydrodynamic-sediment model originally named in the roster) is CONFIRMED ARCHIVED/DEFUNCT via a direct WebFetch of epa.gov/hydrowq/sed3d ("This model has been archived and is no longer available on this website"). The sediment overlap pair as specified is stale; recommend NATE re-scope that comparison to "GAIA vs HEC-RAS-2D-sediment" (both live) going forward, noting the SED3D acronym is also informally reused for a distinct, unrelated 3D sediment sub-module inside SCHISM (per SCHISM's own docs, found in search but not independently WebFetched).

No HEC-RAS rain-on-grid-specific documentation subpage could be located this session (only the general HEC-RAS 2D User's Manual root and 2D Sediment manual pages were confirmed reachable); a dedicated rain-on-grid citation (e.g. a HEC/USACE white paper or an ASCE/MDPI comparison study) should be sourced in a follow-up pass now that the search budget has reset.

Niche/utility TELEMAC modules not covered here for lack of time budget: KHIONE (ice), NESTOR (dredging/sediment management control), ESTEL (surface-groundwater exchange), MASCARET (1D companion, already partially distinct from the 2D/3D scope requested). These exist per the module-members doxygen index found in search and should get their own pass if TELEMAC lands on the roster.
