# M/L SIGN-OFF SHORTLIST - the CAND-M / CAND-L triage

Prepared for NATE's sign-off review. Grounded in the module-coverage board
(docs/validation/module-coverage-board.md), the velocity ledger
(docs/validation/template-velocity.md measured rates), the ADR chain 0140-0157
(what landed this week), and the live code surface under
server/src/trid3nt_server/agent/workflows/. READ-ONLY: no board or code edits.

## Count reconciliation (measured, not the board header)

`grep -c` on the board today: **143 CAND-M + 93 CAND-L = 236 rows** (the board
prose says "141 CAND-M"; the 2-row drift is the two ELMFIRE crown rows carrying
both a `[CAND-M]` and an `[S->M, STOP ADR 0142]` tag - counted once here).
Of the 236, **4 are already SUBSUMED by landed easy-tier code** (below), so
**232 rows are genuinely live**. Also: 57 LANDED, 7 CAND-S still open, 0 SIGNED.

Triage buckets: **~34 HIGH-VALUE SHORTLIST**, **~168 MACHINERY-GATED** (18
named fronts), **~30 LOW-PRIORITY/CULL**.

---

## 0. HONESTY CROSS-CHECK - SUBSUMED rows (capability shipped, board row stale)

These CAND rows describe capability that **already landed** (mostly the SFINCS
forcing/infiltration surface built pre-0152 and never reconciled against the
board). They inflate the count and MUST be struck, not built.

| Board row | Tag | Evidence (file:line) | Verdict |
|---|---|---|---|
| SFINCS `spiderweb_tropical_cyclone_wind_forcing` | CAND-L | `sfincs_forcing_autowire.py:796` `_resolve_spiderweb_forcing` builds a Holland spiderweb from an IBTrACS track -> `SpiderwebForcing`; full emitter in `sfincs_spiderweb.py` (`build_spiderweb_for_storm`, `_emit_spiderweb_config`) | SUBSUMED - parametric-hurricane wind/pressure forcing ships end-to-end. Strike. |
| SFINCS `gridded_reanalysis_wind_forcing` | CAND-M | `sfincs_builder.py:359` `WindForcing` supports `grid_uri` netCDF (`wind10_u`/`wind10_v`, "e.g. ERA5 / HRRR") in addition to uniform | SUBSUMED - gridded time-varying wind is wired. Strike. |
| SFINCS `reanalysis_gridded_precip_forcing` | CAND-M | `sfincs_forcing_autowire.py:67` `forcing_raster_uri` accepts an accumulated-precip COG ("MRMS QPE, ERA5, gridMET") | SUBSUMED - gridded precip forcing is the existing pluvial path. Strike. |
| SFINCS `cn_infiltration_from_gridded_curve_number` | CAND-M | `sfincs_forcing_autowire.py:1121` calls `fetch_gcn250_curve_numbers`; `sfincs_builder.py:440` `cn_uri -> setup_cn_infiltration` writes `scsfile` | SUBSUMED - gridded GCN250 curve-number infiltration is wired. Strike. |

Note NOT subsumed: `green_ampt_native_infiltration`, `horton_native_infiltration`,
`cn_infiltration_with_recovery_ks` - grep confirms NO `green_ampt`/`horton`/
`setup_cn_infiltration_with_ks` anywhere in server code. These stay as genuine M
rows (SFINCS infiltration-method-extension front).

---

## 1. HIGH-VALUE SHORTLIST (~34 rows - build these first)

Selection rule: real user question + published anchor + machinery ready or one
small front away. Effort classes grounded in the velocity ledger:
**S-batch** ~8-19 min/template (recipe pre-scoped, shared spine, no image build:
landlab 8, pelicun 8.5, swmm 9, elmfire 19); **FEATURE-M** ~45-90 min (new
parser/postprocess/deck-branch, one row); **front + batch** = ~2-4 h front once,
then S-batch rate per row.

### Ready NOW (machinery in place - no front needed)

| # | Row (engine) | Question it answers | Effort | Anchor |
|---|---|---|---|---|
| 1 | HECRAS `2d_diffusion_wave_vs_full_swe_regression` | For a breach flood, how much do peak Q / arrival / max WSE differ between Diffusion-Wave and full SWE at matched mesh? | FEATURE-M ~45-60 min (equation_set knob LANDED 0157 + `hecras_flood_2d` authored mesh) | Bald Eagle/Sayers Dam, ~516k cfs peak |
| 2 | HECRAS `2d_model_stability_diagnostic_sweep` | Does tightening timestep/slope/culvert-invert/alignment drive 2D volume error below threshold? (mesh-QA agent) | FEATURE-M ~60-90 min (on `hecras_flood_2d`) | Bald Eagle/Lock Haven, vol err <1e-6% |
| 3 | OPENQUAKE `seismic_hazard_disaggregation_by_scenario` | Which M-distance-epsilon-TRT dominates hazard at 10%/50yr at this site? | FEATURE-M ~60 min (disagg calculator = job.ini variant on `openquake_psha`) | oq-demo Disaggregation README |
| 4 | OPENQUAKE `site_model_vs30_amplification_build` | Does a fetched-Vs30 site model change hazard-curve amplitude vs uniform reference_vs30? | FEATURE-M ~60 min (Vs30 fetcher -> site CSV) | GEM site-model demo |
| 5 | OPENQUAKE `classical_psha_source_typology_sweep` | Which source typology should the deck use and how do curves differ? | FEATURE-M ~60 min (source-geometry authoring on PSHA surface) | oq-demos/hazard typology set |
| 6 | OPENQUAKE `stochastic_event_set_ground_motion_fields` | Do event-based GMFs back-derive a hazard curve matching classical PSHA? | FEATURE-M ~60-90 min (event_based calculator) | oq-demo EventBasedPSHA |
| 7 | SCHISM `parametric_spectra_wave_forcing` | Given a prescribed offshore JONSWAP spectrum, what nearshore wave transformation/setup? | FEATURE-M ~45-60 min (WWM binary `pschism_WWM_GOTM` BUILT 0131; `wwminput.nml.spectra` sample) | schism-docs-wwm |
| 8 | SCHISM `baroclinic_3d_circulation` | Density-driven 3D current / thermocline / stratification in a shelf-estuary? | ENGINE-adjacent ~1.5-2 h author (TRIADED landable 0126, existing binary, no build); live acceptance NATE-remote (28-day 3D) | Test_CORIE Columbia R. estuary |
| 9 | GEOCLAW `thacker_analytic_swe_validation` | Mass/momentum conservation of the SWE+AMR wet-dry solver vs Thacker's exact bowl solution? | FEATURE-M ~45 min (existing 2D SWE solver; idealized V&V) | Thacker analytic (non-US, V&V) |
| 10 | GEOCLAW `fgout_animation_frames` | Uniform-grid animated depth/surface sequence at fixed times (smooth flood/tsunami animation)? | FEATURE-M ~60 min (fgout postprocess; 0155 already folded fgmax mask) - high UI value (time-series animation demo) | clawpack fgout examples |
| 11 | MODFLOW `prt_backward_capture_zone_quadrefined` | Delineate a well's capture zone via backward particle tracking on a quad-refined grid? | FEATURE-M ~60-90 min (native mf6 PRT in the 6.7.0 binary - NO mp7 install needed) | mf6-examples PRT capture-zone |
| 12 | MODFLOW `buy_density_driven_saltwater_intrusion` | Does BUY on a GWF-GWT pair reproduce the Henry saltwater-interface shape? | FEATURE-M ~60-90 min (GWT already wired - `gwt_adapter`; BUY is the small add) | Henry problem (coastal aquifer, US-relevant) |
| 13 | LANDLAB `detachment_limited_incision_steady_state` | Does the steady channel slope-area match the analytical stream-power prediction? | S-batch ~10-15 min (landlab composer 0141; exec-mode) | analytical slope-area V&V |
| 14 | LANDLAB `channel_steepness_chi_map` | Which reaches are anomalously steep for their drainage area (knickpoint/tectonic proxy)? | S-batch ~10-15 min | West Bijou Creek escarpment, CO (US) |
| 15 | LANDLAB `stochastic_storm_sequence_generator` | Drive analysis with a realistic multi-year storm/interstorm sequence (reusable forcing utility) | S-batch ~15-20 min (feeds gw/overland/ensemble chains) | landlab PrecipitationDistribution |
| 16 | TELEMAC `rainfall_evaporation_forcing` | How does distributed on-mesh rainfall change inundation depth/timing independent of the inflow hydrograph? | FEATURE-M ~45-60 min (reuse the wired gridMET precip fetcher) | telemac rain/evap case |
| 17 | ELMFIRE `initial_attack_containment_probability` | Hirsch-model probability of containment given fire size + head-fire intensity; sensitivity to attack delay? | FEATURE-M ~45 min (closed-form, no full engine run) | Hirsch POC closed-form coefficients |

### One small front away (front cost amortized over the whole family)

| # | Row (engine) | Question | Effort | Front + anchor |
|---|---|---|---|---|
| 18 | GEOCLAW `multi_subfault_dtopo_from_finite_fault_model` | dtopo from a published multi-subfault finite-fault model | front ~2-3 h + S-batch | Okada-dtopo authoring; **1964 Alaska/Prince William Sound, NOAA SIFT (real US)** |
| 19 | GEOCLAW `okada_single_subfault_dtopo` | dtopo from a single rectangular fault (Okada) | S-batch after #18 front | same Okada-dtopo front |
| 20 | GEOCLAW `parametric_holland_wind_surge_ike` | Parametric-Holland wind storm-surge vs committed regression output | front ~3-4 h + S-batch | GeoClaw storm-surge front; **Hurricane Ike 2008, committed regression_data** |
| 21 | ELMFIRE `active_crown_fire_spread_rate_verification` | Does ELMFIRE reproduce Cruz-2005 active crown-fire spread rate + crown/surface phi_w linkage? | front ~2-4 h + S-batch (`most build-ready crown template`) | ELMFIRE crown-fire front; Cruz 2005 CROSA |
| 22 | ELMFIRE `random_ignition_burn_probability_ensemble` | Per-pixel burn-probability raster from N randomized ignitions | front ~2-4 h + L | ELMFIRE ensemble front; **2018 County Fire CONUS (NUM_ENSEMBLE=100)** |
| 23 | OPENQUAKE `scenario_liquefaction_probability_map` | Map liquefaction occurrence-class/probability from one rupture + site covariates | front ~2-3 h + S-batch | openquake.sep front; feeds off the SIGNED scenario GMF (multi-hazard) |
| 24 | MODFLOW `advanced_package_mover_routing_uzf_sfr_lak_wel` | Does MVR transfer rejected UZF + LAK/WEL discharge into SFR in one timestep? | front ~2-4 h + M (SFR smoke fixture already exists) | MODFLOW advanced-package front |
| 25 | TELEMAC `weir_controlled_discharge_staging` | How does a low-head dam/weir regulate downstream Q and upstream backwater? | front ~2-3 h + S-batch | TELEMAC singularity front; t2d_weirs.cas verified; **NID low-head dams pervasive US** |
| 26 | TELEMAC `dissolved_oxygen_bod_sag_curve` | Streeter-Phelps downstream oxygen-sag; where is min-DO? | front ~2-4 h + S-batch | TELEMAC WAQTEL front; **Clean Water Act DO-impairment/TMDL (US)** |
| 27 | SWAN `ww3_boundary_nested_regional_downscale` | Downscale a WW3 regional hindcast into a nearshore SWAN grid | front ~2-3 h + M | SWAN nesting front; **USGS Hawaii projection DOI 10.5066/F7G73CP1; NOAA WW3 operational** |
| 28 | SWAN `nonstationary_time_marching_storm_evolution` | Minute-by-minute nearshore wave field through a 24-48h storm | FEATURE-M ~60-90 min (NONSTAT deck recipe, native solver) | SWAN transient forcing |
| 29 | LANDLAB `single_event_landslide_runout_validated` | Where does debris runout go vs an observed DEM-of-Difference? | front ~2-3 h + L | landlab BedrockLandslider; **2021 Cascade Mtns US, field DoD (paper-first/US doctrine)** |
| 30 | LANDLAB `aquifer_storm_seepage_hydrograph` | Groundwater seepage/return-flow through a storm sequence; where does it emerge? | front ~2-3 h + L (first landlab gw chain) | V&V gate <1% cumulative-flux error |
| 31 | PELICUN `custom_model_dl_calculation_cli_wrapper` | Minimal DL_calculation.py invocation to bring a new hazard's fragility/consequence tables in | **front itself ~2-4 h** (chdir+tempdir to_thread wrapper per 0146 STOP) - unblocks the whole Pelicun family | pelicun DL_calculation.py |
| 32 | PELICUN `wind_only_hazus_hurricane_run` | Turn a peak-gust demand into the CI-checked HAZUS wind damage/loss | S-batch ~8-10 min after #31 | pelicun CI fixture |
| 33 | PELICUN `coupled_wind_surge_governing_damage_state` | Governing damage state from combined wind + surge demand | M after #31 (multi-hazard) | combine_wind_flood.csv CI fixture |
| 34 | SWMM `swmm_rdii_rtk_unit_hydrograph` | How much rainfall-derived I&I enters the pipe network at a manhole vs direct runoff? | FEATURE-M ~60-90 min (new Hydrograph/RDII object) | **EPA worked example, Table 7-1 published intermediate flows** |

---

## 2. MACHINERY-GATED - fronts ranked by rows-unblocked

Each front is a named capability build (`~2-4 h` per the velocity ledger's
"machinery ~2-4 h each family, then template rates apply"). Rows listed are the
board rows that front unblocks. Ranked by rows-unblocked-per-front-effort (the
build order that opens the most board value fastest).

| Rank | Front | Rows unblocked | Front effort | Notes / included shortlist rows |
|---|---|---|---|---|
| 1 | **Pelicun DL_calculation.py CLI harness** (chdir + tempdir + to_thread) | **~10** | ~2-4 h once | Unblocks collapse-override, p58-db-sweep, auto-populate (0146 STOP), v5-vs-v6 (0146 STOP), story-level, wind-only(#32), custom-tsunami, component-wind-envelope, wind-surge(#33), + the wrapper(#31) itself. Highest leverage: one harness, ~10 rows at ~8.5 min each after. |
| 2 | **MODFLOW GWE (heat-transport) package** | **7** | ~2-4 h | gwe_radial/ATES/BHE-loading/multisource/particle-thermal/vsc-viscosity/danckwerts. Every row has a published analytical anchor (Al-Khoury, Wexler POINT2, Danckwerts). BUT geothermal/ATES is a hazard-workbench scope stretch - recommend land 1-2 (BHE Wexler + radial) to prove the package, DEFER the other 5. |
| 3 | **SCHISM ICM/CoSiNE water-quality build** (USE_ICM) | 5 | ENGINE-class ~2 h + image | eutrophication-core, ChesBay(US), marsh-nutrient, CoSiNE-SFBay(US), FIB. Strong US estuary anchors; overlaps TELEMAC-WAQTEL + HEC-RAS-WQ (WQ is a cross-engine theme - pick ONE lead engine). |
| 4 | **ELMFIRE transient/multi-band weather deck** | 5 | ~2-4 h | transient-wind-schedule, dead-fuel-moisture-interp (0142 STOP), historical-met-band-ensemble, live-fuel-moisture-raster, raster-perturbation-ensemble. Shared multi-band + time-interp machinery; several 0142 STOPs collapse here. |
| 5 | **TELEMAC TOMAWAC spectral-wave** | 7 | ENGINE-class ~2 h | wind-wave-growth, refraction-shoaling, wave-current, bottom-friction, fetch-limited, shoaling-breaking-benchmark, wave-current-opposing. Overlaps SWAN + SnapWave heavily (fidelity ladder) - LOW marginal value, defer behind SWAN. |
| 6 | **TELEMAC WAQTEL water-quality** | 8 | ~2-4 h | DO-sag(#26,US-TMDL), eutro-algal, micropollutant, biomass, DO-channel-analytical, lagoon-eutro(non-US), thermal-budget, aed2-lake. DO-sag is the strong US lead; rest are deeper WQ. |
| 7 | **GeoClaw storm-surge deck** (parametric Holland + best-track fetch) | 4 | ~3-4 h | best-track-to-storm-file, parametric-Holland-Ike(#20), gridded-wind-Isaac, wind-drag-law (0155 retag). The whole surge scenario is a v0.1 sea-level stub today; drag_law is INERT without this. Overlaps SFINCS spiderweb surge (fidelity ladder - GeoClaw = refinement-grade). |
| 8 | **ELMFIRE crown-fire** (CROWN_FIRE module) | 5 | ~2-4 h | active-crown-verification(#21), crown-initiation (0142 STOP), spread-rate-ceiling (0142 STOP), crown-exact-regression-gate, crown-triggered-spotting (bridges spotting). |
| 9 | **MODFLOW advanced-package** (MVR/UZF/SFR/LAK/MAW) | 4 | ~2-4 h (SFR smoke fixture exists) | drn-vs-uzf-seepage, maw-flowing-well, mvr-routing(#24), watershed-uzf-sfr-drn-mvr. Real US watershed water-balance questions. |
| 10 | **ELMFIRE spotting** (ember module) | 4 | ~2-4 h | lognormal-spot-distance, critical-spotting-intensity (0142 STOP), ember-count-landing (0142 STOP), stochastic-spotting-ensemble. |
| 11 | **MODFLOW PRT particle-tracking** | 3 (+1 GWE) | ~2-4 h (native PRT, NO mp7) | capture-zone(#11), forward-transient-pathlines, backward-lateral-injection. Wellhead-protection = strong user question; the memory's "MODPATH7 install" blocker is MOOT - mf6 native PRT is in the 6.7.0 binary. |
| 12 | **SCHISM SED3D sediment** (USE_SED) | 4 | ENGINE-class ~2 h + image | multiclass-suspended, morphodynamic-bed, trench-migration-benchmark, wave-enhanced-stress (needs WWM+SED joint). Overlaps TELEMAC-GAIA + HEC-RAS-sediment. |
| 13 | **TELEMAC GAIA sediment** | 5 | ~2-4 h (GAIA standardized 0154) | erodible-bed-scour (already scoped in telemac-river-addons doc), mixed-grain, cohesive-mud, multiclass-bed-evolution, dune-migration-3d. |
| 14 | **OpenQuake secondary-perils** (openquake.sep liquefaction+landslide) | 6 | ~2-3 h | scenario-liquefaction(#23), event-based-liquefaction, newmark-landslide, probabilistic-landslide-susceptibility, + (site-model amp rows adjacent). Multi-hazard EQ-cascade value; feeds off signed scenario GMF. |
| 15 | **TELEMAC ARTEMIS harbor-wave** (phase-resolving) | 6 | ENGINE-class ~2 h | harbor-agitation, breakwater-diffraction, reef-shoal, harbour-tranquility, island-diffraction, artemis-tomawac-pairing. Niche (port agitation); low near-term hazard value. |
| 16 | **HEC-RAS structure authoring** (bridges/culverts/gates/pumps/inline) | 5 | ~2-4 h (genuinely new authoring per 0157) | multi-opening-flow-split, advanced-inline-multi-component, gate-pump-rules(L), 1d2d-pump-station, breach-param-ensemble. Beaver Creek published flow-splits. |
| 17 | **HEC-RAS 1D-network / RasSteady deck author** | 4 | ~2-4 h (0157: RasSteady in image but NEVER invoked - "1D steady signed" was WRONG) | steady-HWM-calibration, steady-floodway-encroachment (FEMA floodway), modified-puls, unsteady-hydrograph-optimization. Merced/Beaver Creek/Bald Eagle anchors. |
| 18 | **HEC-RAS WQ engine** (RAS temperature/NSM) | 2 | ENGINE-class ~2 h | water-temp-heat-budget(#-, Mohawk USGS MAE 0.87degC), nutrient-NSM-I (Mohawk P). Overlaps TELEMAC-WAQTEL + SCHISM-ICM. |

Smaller/single-row fronts (build opportunistically alongside a neighbor):
SFINCS infiltration-method-extension (green_ampt/horton/cn-with-ks, 3 rows) |
SFINCS structures (weir/thin-dam, 2 rows, gated per 0152) | SFINCS SnapWave decks
(gamma/reef-friction/US-boundary/IG-wavemaker, ~5 rows, gated per 0152) |
SCHISM baroclinic-3D-vgrid (vertical-grid-sensitivity + water-age, share #8's
enablement) | SCHISM PaHM (best-track surge, 2 US rows) | SCHISM marsh (USE_MARSH,
2) | SCHISM hydraulics (USE_HYDRAULICS, 1) | SCHISM ptrack postproc (drifter/oil,
2) | SWMM snowmelt (Snow Pack object, 2) | SWMM RDII (2, EPA anchor) | SWMM
groundwater-baseflow (1) | MODFLOW GWT-transport (UZT/CSUB, 2) | MODFLOW grid-type
(LGR/DISU, 2) | GeoClaw Okada-dtopo (#18/#19, 3) | GeoClaw fgout-postproc (#10 +
netcdf-transect + fgmax-fgout-combined, 3) | SWAN nesting/BC-chaining (#27 +
two-level + spectral-chained + segment-variable, 4) | SWAN grid-type (unstructured
+ curvilinear, 2) | SWAN physics-knobs (gen3-st6/quad-scaling/bkd/spb/madsen/
ripple, ~6, mostly individually foldable).

---

## 3. LOW-PRIORITY / QUESTIONABLE - defer or cull (~30 rows)

| Row(s) | Reason | Recommendation |
|---|---|---|
| SCHISM `multiclass_icepack_thickness_distribution`, `single_class_sea_ice_mevp` | "heaviest-build-flag, most speculative in the roster"; near-zero US-hazard relevance | CULL icepack; DEFER single-class ice until a Great Lakes ice question appears |
| GeoClaw `two_layer_plane_wave_internal_wave`, `multilayer_dry_state_bowl_validation` | idealized, non-US, separate Riemann-solver build | DEFER (multilayer build) |
| GeoClaw `adjoint_guided_amr_flagging` | separate backward adjoint run; niche optimization | DEFER |
| GeoClaw `okada_1d_dtopo_smoke_case`, `1d_bouss_wavetank`, `sgn_boussinesq`, `radial_flat_bouss_smoke` | 1D path + Boussinesq need a PETSc/MPI or 1D build "disproportionate machinery for a smoke test" (0155) | DEFER Boussinesq+1D front until a real dispersive/1D question class |
| SCHISM `hurricane_niran...` (non-US), OpenQuake `median_response_spectrum_multi_period` (non-US), GeoClaw `two_layer...`/`thacker` (non-US idealized), TELEMAC `lagoon_eutrophication_risk`/`progressive_dam_breach_1d`/`dam_break_3d`/`malpasset` (non-US) | fail US-only doctrine as V&V targets | Keep US-idealized (Thacker/Monai/Malpasset) as V&V-doctrine cross-checks ONLY; CULL Niran as a V&V target (keep as a mechanical PaHM smoke at most) |
| TELEMAC MASCARET front: `branched_network_1d_routing`, `progressive_dam_breach_1d_network`, `reservoir_siltation_flushing_1d` | lowest-confidence sourcing (TLS broken, search-snippet only); unbuilt MASCARET dependency | DEFER whole MASCARET front; re-verify sources first |
| TELEMAC KHIONE ice: `frazil_ice_formation_transport`, `ice_jam_flood_staging` | low-confidence sourcing; no US canonical case identified | DEFER; find a USACE CRREL ice-jam case first |
| TELEMAC NESTOR dredging (3 rows), ARTEMIS (6 rows) | niche (federal-channel dredging, port agitation); heavy fronts, low near-term hazard value | DEFER |
| TELEMAC-3D rows duplicated across the two TELEMAC board sections (`thermal_stratification_lake/reservoir`, `saline/salinity_intrusion_estuary`) | the supplementary-mining section RE-MINED overlapping rows (board note: "matrix QUEUED as a re-run") | DE-DUPLICATE - count once; the supplementary TELEMAC section is a redundant pass |
| LANDLAB `species_zone_biogeography_under_landscape_change`, `aspect_driven_vegetation_pattern_ca`, `tidal_cycle_flow_forcing` | ecology/biogeography + tidal-circulation are scope stretches for a hazard workbench (board flags "scope decision") | NATE scope call before building; lean DEFER |
| ELMFIRE `combined_initial_extended_attack_pipeline` | "inferred composite, not directly evidenced" (no single worked example) | DEFER until initial+extended land separately |
| MODFLOW GWE rows 3-7 (ATES cycling, multisource-geothermal, particle-thermal, vsc-viscosity) | geothermal/ATES scope stretch; 7-row front on a hazard workbench | Land 1-2 to prove the package (see front #2); DEFER the rest |

---

## 4. NATE-FACING SUMMARY (one page)

### Shortlist by engine (34 rows)

| Engine | Ready-now | Front-away | Shortlist total |
|---|---|---|---|
| OpenQuake | 4 (disagg, vs30-site, typology, event-based) | 1 (liquefaction) | 5 |
| MODFLOW | 2 (PRT capture-zone, BUY-Henry) | 1 (MVR) | 3 |
| Landlab | 3 (incision, chi-map, storm-gen) | 2 (landslide-runout, gw-seepage) | 5 |
| HEC-RAS | 2 (DW-vs-SWE, stability-sweep) | - | 2 |
| GeoClaw | 2 (Thacker, fgout-anim) | 3 (Alaska dtopo x2, Ike surge) | 5 |
| SCHISM | 2 (spectra-wave, baroclinic-3D) | - | 2 |
| TELEMAC | 1 (rain/evap) | 2 (weir, DO-sag) | 3 |
| ELMFIRE | 1 (Hirsch containment) | 2 (crown-verif, burn-prob-ensemble) | 3 |
| SWAN | 1 (nonstationary) | 1 (WW3-nesting) | 2 |
| Pelicun | - | 3 (CLI-harness, wind, wind-surge) | 3 |
| SWMM | 1 (RDII-RTK) | - | 1 |

### Fronts ranked (build order = most board value fastest)

1. **Pelicun DL_calculation harness** - ~10 rows / one ~2-4 h build (best leverage)
2. **ELMFIRE transient-weather deck** - 5 rows, collapses several 0142 STOPs
3. **ELMFIRE crown-fire** - 5 rows, active-crown is "most build-ready"
4. **MODFLOW advanced-package (MVR/SFR)** - 4 rows, SFR smoke fixture exists
5. **MODFLOW PRT** - 3 rows, native (mp7 blocker is moot), wellhead-protection
6. **OpenQuake secondary-perils** - 6 rows, feeds signed scenario GMF (multi-hazard)
7. **GeoClaw storm-surge deck** - 4 rows, Ike/Isaac anchors
8. **TELEMAC WAQTEL WQ** - lead with DO-sag (US TMDL), defer the deep WQ tail
9. **HEC-RAS 1D-network + structure authoring** - 9 rows across 2 fronts (0157 corrected "1D steady signed" to WRONG)
- DEFER: MODFLOW GWE tail, SCHISM ICM/SED/ice, TELEMAC TOMAWAC/ARTEMIS/MASCARET/
  NESTOR/KHIONE (WQ+wave overlap fidelity-ladder; land the SWAN/SFINCS leads first)

### Cull / strike now

- **STRIKE 4 SUBSUMED rows** (SFINCS spiderweb, gridded-wind, gridded-precip,
  gridded-CN) - capability shipped, board never reconciled. Live count 236 -> 232.
- **DE-DUPLICATE** the TELEMAC supplementary-mining section against the main
  TELEMAC section (3D + wave rows re-mined; board already flags the matrix as a
  queued re-run).
- **CULL**: SCHISM icepack, GeoClaw multilayer/adjoint, non-US V&V-as-hazard
  targets (Niran), TELEMAC MASCARET/KHIONE (low-confidence sourcing).
- **NATE scope call**: Landlab ecology/biogeography/tidal, MODFLOW GWE
  geothermal/ATES tail, cross-engine WQ (which of SCHISM-ICM / TELEMAC-WAQTEL /
  HEC-RAS-WQ leads).

### Cross-engine overlap flags (avoid building the same physics 3x)

- **Water quality** appears in SCHISM (ICM/CoSiNE, 5), TELEMAC (WAQTEL, 8),
  HEC-RAS (temp/NSM, 2) = 15 rows of overlapping WQ. Pick ONE lead engine.
- **Spectral waves** in SWAN (~14), TELEMAC-TOMAWAC (7), SCHISM-WWM (2),
  SFINCS-SnapWave (~5). Fidelity ladder: SWAN/SnapWave = screening lead;
  TOMAWAC/WWM = refinement; ARTEMIS = phase-resolving niche.
- **Storm surge** in SFINCS (spiderweb, SHIPPED), GeoClaw (surge deck, stub),
  SCHISM (PaHM). SFINCS already covers screening; build GeoClaw/SCHISM only for
  refinement-grade.
- **Sediment/morphology** in SCHISM-SED (4), TELEMAC-GAIA (5), HEC-RAS-sediment
  (3). Standardize authoring on one before fanning out.

## NATE RULINGS (2026-08-06, applied)
- CULLED: GeoClaw multilayer + adjoint only.
- KEPT as open candidates: SCHISM icepack (Alaska coverage), TELEMAC
  MASCARET/KHIONE (await real anchors), non-US V&V rows (as fixtures).
- Ecology/geothermal: in-scope (geospatial-intelligence identity),
  queued AFTER the fronts + the 34-row shortlist.
- Overlap: IRRELEVANT to builds (coverage=capability); fidelity ladder
  owns quality/routing. The pick-one-WQ-lead recommendation is void.
