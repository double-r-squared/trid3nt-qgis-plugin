# Template input-provenance audit

Author: testing/audit specialist (NATE-directed, 2026-08-03)
Repo: `/home/nate/Documents/trid3nt-local`, branch `refactor/engine-doors`, HEAD 4fc8725
Scope: all engine-door templates under
`server/src/trid3nt_server/agent/workflows/<engine>/<template>/`
Status: AUDIT ONLY - no code was changed.

NATE's concern (verbatim): "some MODFLOW or other templates need some user input
like the input needs to either be authored or is a fetcher we don't have yet... I
don't want some stuff to be hand waved and then the LLM hallucinates input... we
should consider some user gates for the user to input necessary data."

## 0. What was audited and the count reconciliation

Kickoff said 32 templates. On disk there are **30 real templates** across 10
engines (`shared/model_satellite_fire_animation/` is an EMPTY dead dir - only a
`__pycache__`; `shared/data/` holds the Manning crosswalk CSV, not a template).
Each engine ships as a **door tool** + a **composer** pair that share input
handling; MODFLOW ships 12 archetype templates. The 30:

- MODFLOW (12): asr, sustainable_yield, capture_zone, wellhead_protection,
  contaminant_plume, model_groundwater_contamination_scenario, managed_recharge,
  mine_dewatering, regional_water_budget, river_seepage, saltwater_intrusion,
  wetland_hydroperiod
- SFINCS (2): flood, model_nws_flood_event_scenario
- SWMM (2): urban_flood, model_urban_flood_swmm
- TELEMAC (2): river_dye, model_river_dye_release_scenario
- SWAN (2): wave_field, model_wave_scenario
- ELMFIRE (2): fire_spread, model_fire_spread_scenario
- GeoClaw (2): inundation, model_dambreak_geoclaw_scenario
- Landlab (2): susceptibility, model_landslide_scenario
- OpenQuake (2): psha, model_seismic_hazard_scenario
- Pelicun (2): damage_assessment, compute_impact_envelope

Classification rubric used per input:
- **FETCHED** - sourced at runtime from a registered fetcher/real data.
- **USER-REQUIRED-GATED** - a typed error STOPS the run when absent
  (the Invariant-9 / `USER_INPUT_REQUIRED` pattern).
- **USER-REQUIRED-UNGATED** - the LLM/user may pass any value; a value is
  accepted with no provenance distinction (missing may or may not default).
- **DEMO-DEFAULTED-LABELED** - a baked synthetic value that IS surfaced in the
  result envelope/summary/narration as synthetic/default.
- **DEMO-DEFAULTED-UNLABELED** - a baked synthetic value NOT surfaced anywhere
  the LLM narrates from (the hand-wave).
- **MISSING-FETCHER** - genuinely fetchable from a real public source we lack or
  have-but-did-not-wire (candidate named).

---

## 1. The 32(30)-template classification table

Only meaningful/physical inputs are listed. Location/AOI resolution
(`location`/`bbox`/`aoi_latlon`) is USER-REQUIRED-GATED (XOR or presence) in
essentially every template via `_resolve_aoi_point`
(`sustainable_yield.py:191-197`) or a `*_PARAMS_INCOMPLETE` return; it is
collapsed to one row per engine group to keep the table readable. "The gate fires
on `is None`" is the load-bearing caveat: a **present** value - user-supplied OR
LLM-invented - always passes.

### MODFLOW (12)

| template | input | class | enforcement / labeled-in-output | file:line |
|---|---|---|---|---|
| asr | well_location_latlon | USER-REQUIRED-GATED | raise `ASRInputError` if None -> `USER_INPUT_REQUIRED` | asr.py:181-191 |
| asr | injection_rate_m3_day / recovery_rate_m3_day | USER-REQUIRED-GATED | same raise (both required) | asr.py:181-191 |
| asr | injection_months/recovery_months/n_cycles | DEMO-DEFAULTED-LABELED | adapter default; caveat names "cycle schedule are demo defaults" | asr.py:267-270 |
| asr | aquifer_k_ms/porosity/aquifer_sy | DEMO-DEFAULTED-LABELED (omitted) / USER-REQUIRED-UNGATED (passed) | `demo_aquifer_caveat` names K + Sy | asr.py:267-270; contracts:85,93 |
| sustainable_yield | well_location_latlon + pumping_rate_m3_day | USER-REQUIRED-GATED | raise `SustainableYieldInputError` if None | sustainable_yield.py:286-291 |
| sustainable_yield | river geometry (couple_river_sfr) | FETCHED(`fetch_river_geometry`) | missing flowline -> scenario error (not typed USER_INPUT) | sustainable_yield.py:528-541 |
| sustainable_yield | CSUB interbed Ssv/Sse/thickness | DEMO-DEFAULTED-LABELED | caveat: "CSUB interbed ... are demo defaults (no site clay-fraction fetcher)" | sustainable_yield.py:723-732 |
| sustainable_yield | aquifer_k_ms/porosity/sy/ss | DEMO-DEFAULTED-LABELED / UNGATED-when-passed | `demo_aquifer_caveat` | sustainable_yield.py:429-433 |
| capture_zone | well_location_latlon | USER-REQUIRED-GATED | raise `CaptureZoneInputError` if None | capture_zone.py:209-215 |
| capture_zone | travel_time_years | DEMO-DEFAULTED (tiers [1,5,10]) / UNGATED | default applied; tier values not flagged demo-vs-user in caveat | capture_zone.py:94,218-222 |
| capture_zone | n_particles | DEMO-DEFAULTED-UNLABELED | default 16; never in caveat | capture_zone.py:158 |
| capture_zone | aquifer_sy / aquifer_ss | DEMO-DEFAULTED-UNLABELED | `_aquifer_overrides(...,None,None)`; NOT exposed, NOT in caveat | capture_zone.py:261 |
| capture_zone | aquifer_k_ms/porosity | DEMO-DEFAULTED-LABELED | `demo_aquifer_caveat` (K/porosity/grid) | capture_zone.py:299-305 |
| wellhead_protection | (identical to capture_zone; reuses `model_capture_zone_scenario`) | as capture_zone | well gated; EPA 2/5/10-yr tiers cite a real framework but the VALUES are the module's own choice, no EPA data fetched | wellhead_protection.py:121-129; capture_zone.py:100 |
| contaminant_plume | spill point (location/spill_location_latlon) | USER-REQUIRED-GATED | XOR gate in `_resolve_spill_point` | contaminant_plume.py:267-303 |
| contaminant_plume | species: name + release_rate_kg_s | USER-REQUIRED-GATED | typed gate in `normalize_species_list` | contaminant_plume.py:155-237 |
| contaminant_plume | sorption_kd / decay_per_day / parent | USER-REQUIRED-UNGATED | any value validates via `SpeciesSpec`; no provenance check | contaminant_plume.py:155-237 |
| contaminant_plume | duration_days | DEMO-DEFAULTED-UNLABELED | `... else 20.0` fallback; NOT in caveat | contaminant_plume.py:424 |
| contaminant_plume | aquifer_k_ms/porosity | DEMO-DEFAULTED-LABELED / UNGATED | `demo_aquifer_caveat` (K/porosity only) | contaminant_plume.py:498-503 |
| model_groundwater_contamination_scenario | article_text / source_url | USER-REQUIRED-GATED | exactly-one required; url via `web_fetch` (FETCHED) | :800-806 |
| model_groundwater_contamination_scenario | contaminant / mass / duration / location | USER-REQUIRED-GATED (extracted) | `ParameterExtractionError` if extraction fails | :572-617 |
| model_groundwater_contamination_scenario | contaminant_density_kg_l | DEMO-DEFAULTED-LABELED (conditionally) | curated table; `extraction_notes` only when unknown | :207-238,581-585 |
| model_groundwater_contamination_scenario | duration/release-rate clamps | DEMO-DEFAULTED-LABELED | clamp bounds hardcoded; surfaced in `derived_params["clamps_applied"]` | :182-187,630-642 |
| model_groundwater_contamination_scenario | aquifer_k_ms/porosity | DEMO-DEFAULTED-LABELED / UNGATED | caveat in `_build_summary` + confirmation envelope | :698-703,745-748 |
| managed_recharge | basin_footprint_lonlat | USER-REQUIRED-GATED | raise `MARInputError` if None | managed_recharge.py:210-215 |
| managed_recharge | infiltration_rate_m_day / recharge_months | USER-REQUIRED-UNGATED (adapter-defaulted, no named constant) | passed None -> adapter default; caveat names "recharge rate / duration" generically | managed_recharge.py:174-175,276-280 |
| managed_recharge | aquifer_k_ms/sy | DEMO-DEFAULTED-LABELED (porosity not named) | `demo_aquifer_caveat` | managed_recharge.py:276-280 |
| mine_dewatering | pit_footprint_lonlat | USER-REQUIRED-GATED | raise `MineDewateringInputError` if None | mine_dewatering.py:193-198 |
| mine_dewatering | drain_elevation_m / drain_conductance_m2_day | USER-REQUIRED-UNGATED (adapter-defaulted) | caveat names "drain conductance / elevation" generically | mine_dewatering.py:158-159,251-254 |
| mine_dewatering | aquifer_k_ms (porosity NOT named) | DEMO-DEFAULTED-LABELED-by-name-for-K only | caveat | mine_dewatering.py:251-254 |
| regional_water_budget | AOI only | USER-REQUIRED-GATED (AOI); NO physical-input gate | re-raised `RegionalWaterBudgetInputError` | :153-164 |
| regional_water_budget | aquifer_k_ms / porosity | DEMO-DEFAULTED-**UNLABELED** | inline overrides; **no `demo_aquifer_caveat` key at all** | :121-122,167-171,213-217 |
| regional_water_budget | west->east gradient / CHD boundaries / grid | DEMO-DEFAULTED-UNLABELED | adapter-baked; not a contract field; never narrated | :269-270 |
| river_seepage | location / spill point | USER-REQUIRED-GATED (XOR, mechanical) | raise `RiverSeepageScenarioInputError` | river_seepage.py:205-209 |
| river_seepage | river geometry | FETCHED(`fetch_river_geometry`) | missing -> untyped `RiverSeepageScenarioError` (NOT USER_INPUT_REQUIRED) | river_seepage.py:259-263 |
| river_seepage | DEM streambed elevation | FETCHED(`fetch_dem`), OFF by default | `fetch_dem_for_streambed=False`; flat-demo fallback NOT narrated | river_seepage.py:165,266-279 |
| river_seepage | contaminant / release_rate / duration | USER-REQUIRED-UNGATED (defaulted TCE/0.01/30) | ordinary defaults | river_seepage.py:160-162 |
| river_seepage | streambed conductance; river stage | DEMO-DEFAULTED-LABELED (conductance) / UNLABELED (stage) | caveat names conductance only | river_seepage.py:339-343; contracts:204-214 |
| saltwater_intrusion | coastal_transect_latlon | USER-REQUIRED-GATED | raise `SaltwaterIntrusionInputError` if None (contract-mandated) | saltwater_intrusion.py:193-200 |
| saltwater_intrusion | seawater_salinity_ppt / n_vertical_layers | USER-REQUIRED-UNGATED (contract default 35 ppt / 20) | validated >0 / range | saltwater_intrusion.py:216-221 |
| saltwater_intrusion | freshwater_inflow_m3_day | USER-REQUIRED-UNGATED (auto-derived when None) | derived value NOT narrated | saltwater_intrusion.py:150,250 |
| saltwater_intrusion | aquifer_k_ms/porosity/grid | DEMO-DEFAULTED-LABELED | caveat (Henry-style, 100-col grid) | saltwater_intrusion.py:296-303 |
| wetland_hydroperiod | wetland_footprint_lonlat | USER-REQUIRED-GATED | raise `WetlandHydroperiodInputError` if None | wetland_hydroperiod.py:184-189 |
| wetland_hydroperiod | recharge_schedule / ET params | USER-REQUIRED-UNGATED (adapter "demo wet/dry alternation") | caveat names "recharge / ET schedule" generically | wetland_hydroperiod.py:146-149,264-268 |
| wetland_hydroperiod | aquifer_k_ms / specific_yield (porosity not named) | DEMO-DEFAULTED-LABELED | caveat | wetland_hydroperiod.py:264-268 |

### SFINCS (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| flood | DEM (land/coastal) | FETCHED(`fetch_dem` 3DEP / `fetch_topobathy`) | ADR-0091 gated fallback; hard fail raises | flood.py:704-731 |
| flood | landcover / Manning classes | FETCHED(`fetch_landcover` NLCD) | class set gated vs `manning_mapping.csv` (`LULC_MAPPING_MISMATCH`) | flood.py:741-758; sfincs_builder.py:698-706 |
| flood | Manning's-n VALUES | DEMO-DEFAULTED-UNLABELED | static authored crosswalk CSV; not fetched; gate checks class coverage only, not value correctness | sfincs_builder.py:20,2402-2416 |
| flood | rainfall (design storm) | FETCHED(`lookup_precip_return_period` NOAA Atlas 14) | | flood.py:840-856 |
| flood | rainfall (observed) | FETCHED(caller `forcing_raster_uri`, e.g. MRMS) | | flood.py:793-839 |
| flood | coastal tide/surge; river discharge | FETCHED (CO-OPS->GTSM->parametric; NWM->NWIS->skip) | parametric last resort is synthesized | flood.py:1168-1222 |
| flood | levee-breach peak discharge | USER-REQUIRED-GATED | typed `USER_INPUT_REQUIRED`: "the breach hydrograph is not fabricated" | flood.py:549-572 |
| flood | tsunami wave height | USER-REQUIRED-GATED | typed `USER_INPUT_REQUIRED`: "the wave form is not fabricated" | flood.py:573-595 |
| flood | wind | USER-REQUIRED-UNGATED (dict) | "never fabricated" per docstring; no value provenance check | flood.py:399-403,1224-1226 |
| flood | forcing_type label | DEMO-DEFAULTED-LABELED (but overloaded) | `ForcingSummary.forcing_type="pluvial_synthetic"` also stamped on OBSERVED precip (admitted mislabel) | flood.py:813-824 |
| model_nws_flood_event_scenario | warning polygon / MRMS QPE | FETCHED(`fetch_nws_alerts_conus`, `fetch_mrms_qpe`) | no-warning/no-data -> structured status, never raises | :540-582,646-648 |
| model_nws_flood_event_scenario | (DEM/landcover/Manning/surge) | delegated to `model_flood_scenario` | inherits row set above; `pluvial_synthetic` mislabel inherited | :670-678 |

### SWMM (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| urban_flood / model_urban_flood_swmm | bbox | USER-REQUIRED-GATED (AOI only) | `SWMM_PARAMS_INCOMPLETE` | urban_flood.py:228-236 |
| urban_flood | DEM | FETCHED(`fetch_3dep_extra` 1m -> `fetch_dem` 10m) | both-fail -> `SWMM_DEM_FETCH_FAILED` | model_urban_flood_swmm.py:201-235 |
| urban_flood | buildings (obstruction) | FETCHED(`fetch_buildings` OSM), best-effort | | model_urban_flood_swmm.py:238-259 |
| urban_flood | rainfall (design storm) | FETCHED(Atlas 14) with SILENT fallback | on lookup failure -> baked `total_rain_depth_mm=120.0`, log-only | swmm_mesh_builder.py:963 |
| urban_flood | **drainage / pipe network** | SYNTHESIZED-UNLABELED | one STORAGE node per DEM cell, 4-connectivity overland conduits, ONE outfall - NOT a real storm-sewer network | swmm_mesh_builder.py:958-1226 |
| urban_flood | Manning n (overland) | USER-REQUIRED-UNGATED (flat 0.03) | `nlcd_manning` builder hook exists but NEVER wired; landcover roughness dead here | urban_flood.py:118; swmm_mesh_builder.py:976 |
| urban_flood | infiltration magnitudes (CN, Green-Ampt) | DEMO-DEFAULTED-UNLABELED | not even exposed to the LLM signature | swmm_mesh_builder.py:971-974 |
| urban_flood | subcatchment slope / n_imperv / n_perv | DEMO-DEFAULTED-UNLABELED | hardcoded 0.5 / 0.012 / 0.1 | swmm_mesh_builder.py:1155-1159,1245 |
| urban_flood | pollutant buildup/washoff coeffs | DEMO-DEFAULTED-LABELED-in-docstring-ONLY | "narrated as such by the composer" claim is NOT implemented in the output path | swmm_contracts.py:141-218 |
| urban_flood | mass-balance continuity | (output honesty gate, not an input) | `SWMM_MASS_BALANCE_EXCEEDED` refuses to publish silently-wrong depth | swmm_mesh_builder.py:1729-1742 |

### TELEMAC (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| river_dye / model_river_dye_release_scenario | location / bbox | USER-REQUIRED-GATED (AOI only) | XOR raise; NO physical-forcing gate anywhere | river_dye.py:298-306; scenario:759-763 |
| river_dye | river reach flowline (seed) | FETCHED(`fetch_river_geometry`) | seed pick only; worker re-derives NLDI/NHDPlus centerline | scenario:807-815 |
| river_dye | channel bathymetry/bed | DEMO-DEFAULTED-LABELED | worker samples real Copernicus/3DEP DEM but collapses to ONE slope -> synthetic planar bed; "planar idealized channel bed" caveat | worker telemac_river_dye_build.py:1193-1234 |
| river_dye | **carrier discharge `inflow_q_m3s`** | DEMO-DEFAULTED-UNLABELED, NOT AN EXPOSED PARAM | hardcoded worker constant 250 m3/s, heuristically rescaled from width only; comment: "Data-driven NWM streamflow is the follow-up (#223)" | worker :64; entrypoint.py:252-261 |
| river_dye | spill source_q_m3s / fraction / duration / conc | USER-REQUIRED-UNGATED, DEMO-DEFAULTED | clamped to defaults; never gated | river_dye.py:441-450,380-404 |
| river_dye | friction (Strickler/Manning) | USER-REQUIRED-UNGATED, DEMO-DEFAULTED-LABELED (broad) | default 33; docstring "no site-roughness fetcher exists" | river_dye.py:232-234; physics_registry.py:422-430 |
| river_dye | decay half-life / grain size | USER-REQUIRED-UNGATED, DEMO-DEFAULTED-LABELED | "honest demo defaults" per docstring | river_dye.py:217-228 |
| river_dye | overall label | DEMO-DEFAULTED-LABELED (broad) | `fallback_note` "Idealized demo ... Not a calibrated site study" - does NOT itemize discharge/friction as synthetic | scenario:1267-1275 |

### SWAN (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| wave_field / model_wave_scenario | bathymetry (topo/bathy DEM) | FETCHED(`fetch_topobathy`), **USER-REQUIRED-GATED** | raises `SWAN_DEM_FETCH_FAILED` / `SWAN_NO_BATHYMETRY` on absence/land-only | model_wave_scenario.py:132-171 |
| wave_field | offshore boundary Hs/Tp/Dir/spread | DEMO-DEFAULTED-**UNLABELED** | `synthesize_demo_wave_boundary` (3m/9s/180deg); docstring claims narration but `fallback_note` is NEVER populated anywhere in the SWAN tree | run_swan.py:142-178,253; swan_contracts.py:117-120 |
| wave_field | wind field (`wind_uri`) | USER-REQUIRED-UNGATED + MISSING-FETCHER(ERA5) | passthrough only; `fetch_era5_reanalysis` exists but never called | run_swan.py:281,338-340 |
| wave_field | water level / tide | ABSENT (no param) | MISSING-FETCHER(`fetch_noaa_coops_tides` / `fetch_gtsm_tide_surge` - both registered, neither wired) | (no forcing hits in workflows/swan) |

### ELMFIRE (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| fire_spread / model_fire_spread_scenario | ignition_lonlat | USER-REQUIRED-GATED | `FIRE_IGNITION_REQUIRED` if None | fire_spread.py:154-166 |
| fire_spread | fuel model / canopy | FETCHED(`fetch_landfire_fuels`) | CONUS-only; typed error on no coverage | run_elmfire.py:220-271 |
| fire_spread | DEM / slope / aspect | FETCHED(`fetch_dem` + derived) | | run_elmfire.py:273-297 |
| fire_spread | bbox (domain) | USER-REQUIRED-UNGATED | silently derives ~5km box around ignition; derivation not labeled | elmfire_contracts.py:259-268 |
| fire_spread | wind_speed_mph / wind_dir_deg | DEMO-DEFAULTED-UNLABELED (15 mph / 0 deg) | NO field on `FireSpreadLayerURI`; surfaced ONLY on the pre-run confirm card prose | elmfire_contracts.py:166-169 |
| fire_spread | fuel_moisture (m1/m10/m100 preset) | DEMO-DEFAULTED-UNLABELED ("dry") | contract admits gridMET/HRRR is the "documented v2 path ... never silently substituted here" | elmfire_contracts.py:80-93,170 |

MISSING-FETCHER for ELMFIRE wind + moisture: `fetch_gridmet` (`vs`, `fm100`,
`fm1000`), `fetch_raws_weather`, `fetch_hrrr_forecast` - ALL registered, none
wired.

### GeoClaw (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| inundation / model_dambreak_geoclaw_scenario | bbox | USER-REQUIRED-GATED (AOI only) | `GEOCLAW_PARAMS_INCOMPLETE` | inundation.py:181-189 |
| inundation | bathymetry/topo | FETCHED(`fetch_topobathy` -> `fetch_dem` 10m) | data-source fallback | scenario:123-176 |
| inundation | dam location (`source_lonlat`) | USER-REQUIRED-UNGATED, defaults to AOI CENTROID | MISSING-FETCHER(`fetch_usace_dams` NID lat/lon - registered, unwired) | geoclaw_contracts.py:157-160 |
| inundation | dam height (`dam_break_depth_m`) | DEMO-DEFAULTED-UNLABELED (10.0 m) | MISSING-FETCHER(`fetch_usace_dams` `DAM_HEIGHT`/`NID_STORAGE`); "demo values, not site-calibrated" comment never reaches the layer | geoclaw_contracts.py:109,211 |
| inundation | earthquake magnitude (`source_magnitude`) | USER-REQUIRED-UNGATED (Mw 8.0) | MISSING-FETCHER(USGS ComCat catalog) | geoclaw_contracts.py:165-166 |
| inundation | Okada fault geometry (strike/dip/rake/depth) | DEMO-DEFAULTED, banner-in-stdout-ONLY | worker prints "NON-SITE-SPECIFIC synthetic source ... illustrative, NOT a site-specific seismic source" to `geoclaw.stdout`; NEVER propagated to `GeoClawDepthLayerURI`/narration | worker setrun_builder.py:522-541 |
| inundation | manning_n / sea_level / amr_levels | DEMO-DEFAULTED-UNLABELED | run-config knobs | geoclaw_contracts.py:220-224 |

### Landlab (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| susceptibility / model_landslide_scenario | bbox | USER-REQUIRED-GATED (AOI only) | `LANDLAB_PARAMS_INCOMPLETE` | susceptibility.py:166-174 |
| susceptibility | DEM / slope | FETCHED(`fetch_3dep_extra` 1m -> `fetch_dem` 10m) | data-source fallback | scenario:164-193 |
| susceptibility | soil cohesion / friction / density / thickness / transmissivity | DEMO-DEFAULTED-UNLABELED | NO field on `LandlabSusceptibilityLayerURI`; contract's "narrated by the composer" claim is aspirational, never attached | landlab_contracts.py:92-98,170-186 |
| susceptibility | recharge / rainfall_intensity / storm_duration | DEMO-DEFAULTED-UNLABELED (30 mm/day; 50 mm/hr; 2 hr) | MISSING-FETCHER(`fetch_gridmet` `pr` - registered, unused; NOAA Atlas 14) | landlab_contracts.py:97,100-101 |
| susceptibility | root cohesion | ABSENT (not modeled) | | (no field) |

MISSING-FETCHER for Landlab soil block: SSURGO/gNATSGO (USDA NRCS) or POLARIS -
none registered anywhere.

### OpenQuake (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| psha / model_seismic_hazard_scenario | bbox | USER-REQUIRED-GATED (AOI only) | `OQ_PARAMS_INCOMPLETE` | psha.py:168-176 |
| psha | seismic source (real-fault path) | FETCHED(`fetch_fault_sources` GEM Global Active Faults) | slip-rate-driven MFD | scenario:224-303 |
| psha | seismic source (fallback, no mapped fault) | DEMO-DEFAULTED-LABELED | typed `source_model_kind`/`source_model_note` on the layer (genuine honesty-floor field) | contracts:225-236; scenario:896-906 |
| psha | G-R a/b, min/max magnitude | DEMO-DEFAULTED-LABELED-in-docstring-ONLY | no typed output field surfaces the a/b/mag actually used | openquake_contracts.py:140-143 |
| psha | GMPE | USER-REQUIRED-UNGATED + DEMO-DEFAULTED-UNLABELED | single default regardless of tectonic region; only non-empty-string check; no caveat | openquake_contracts.py:161 |
| psha | **Vs30 site conditions** | DEMO-DEFAULTED-UNLABELED + MISSING-FETCHER | hardcoded 760 m/s "measured" GLOBALLY in `job_ini`; NOT a param, NOT fetched, NOT in any output field | worker job_ini.py:559-563 |
| psha | seismic source zones (USGS NSHM) | MISSING-FETCHER(USGS National Seismic Hazard Model) | never referenced | (absent) |

### Pelicun (2)

| template | input | class | enforcement / labeled | file:line |
|---|---|---|---|---|
| damage_assessment | hazard_raster_uri | USER-REQUIRED-GATED (consumes upstream layer) | `USER_INPUT_REQUIRED` if absent; ground-motion pairing NOT wired | damage_assessment.py:1489-1509 |
| damage_assessment | asset inventory (assets_uri OR bbox) | USER-REQUIRED-GATED | either-or gate | damage_assessment.py:1489-1509 |
| damage_assessment | assets via bbox auto-fetch | FETCHED(`compute_building_density` MS Global Buildings) | every asset hardcoded `component_type="RES1"` (UNLABELED per-feature) | damage_assessment.py:358-403,339 |
| damage_assessment | assets via NSI (sibling composer) | FETCHED(`fetch_usace_nsi` USACE NSI) | real occupancy class + replacement value | compute_impact_envelope.py:388-401 |
| damage_assessment | fragility_set | USER-REQUIRED-GATED enum | `hazus_flood_v6` = bundled-real HAZUS v6.1 CSV; `fema_hazus_eq_2020` = registered but hard-raises (unimplemented) | damage_assessment.py:599-612,1354-1360 |
| damage_assessment | replacement_value per asset | FETCHED (NSI) or DEMO-DEFAULTED-LABELED | per-feature `replacement_value_defaulted` bool on output FGB (HAZUS-MH table) | damage_assessment.py:1216-1233,1309 |
| compute_impact_envelope | flood_layer_uri | USER-REQUIRED-GATED | raise `ComputeImpactEnvelopeInputError`; docstring "NEVER invent" | :333-343 |
| compute_impact_envelope | structure inventory | FETCHED(`fetch_usace_nsi` default / MS buildings) | | :388-450 |
| compute_impact_envelope | default-value provenance | DEMO-DEFAULTED-LABELED-but-DROPPED | `n_assets_default_replacement_value` computed in `raw_envelope` but EXCLUDED from `envelope_summary` (the cited surface) | :486-502; postprocess_pelicun.py:544-577 |

---

## 2. Summary counts

- Real templates audited: **30** (10 engines; MODFLOW 12).
- **Typed presence-gate on at least one dominant PHYSICAL input** (stops the run
  on `is None`): **~15 templates** - all MODFLOW well/footprint/spill archetypes
  (10: asr, sustainable_yield, capture_zone, wellhead_protection,
  contaminant_plume, groundwater_contamination, managed_recharge, mine_dewatering,
  saltwater_intrusion, wetland_hydroperiod), SFINCS flood (breach/tsunami
  conditional), Pelicun x2 (hazard + assets), ELMFIRE (ignition), SWAN
  (bathymetry, fetched-gated).
- **NO stop-gate on a dominant physical forcing** (forcing defaulted/synthesized,
  run proceeds): **~15 templates** - SWMM x2, TELEMAC x2, GeoClaw x2, Landlab x2,
  OpenQuake x2, SFINCS nws (delegates), MODFLOW river_seepage + regional_water_budget,
  and SWAN's boundary/wind/tide (bathymetry gated, forcing not).
- **DEMO-DEFAULTED-UNLABELED physical inputs** (the hand-wave, per template with
  at least one): **~11 templates** - ELMFIRE (wind, moisture), GeoClaw (dam
  height, quake Mw, fault), Landlab (entire soil block, rainfall), OpenQuake
  (Vs30, GMPE), SWMM x2 (network, Manning, infiltration, subcatchment),
  TELEMAC x2 (carrier discharge), SWAN (wave boundary), MODFLOW
  regional_water_budget (K/porosity/gradient), plus minor unlabeled residue in
  capture_zone (sy/ss, n_particles) and contaminant_plume (duration_days).
- **MISSING-FETCHER, fetcher ALREADY registered but not wired**: gridMET,
  RAWS, HRRR (ELMFIRE/Landlab), USACE dams (GeoClaw), NOAA NWM (TELEMAC),
  CO-OPS tides + GTSM + ERA5 (SWAN). Cheap wins.
- **MISSING-FETCHER, needs a NEW source**: USGS Vs30 (OpenQuake), SSURGO/POLARIS
  soils (Landlab + SWMM infiltration), USGS ComCat (GeoClaw quake), NDBC/WW3/
  GFS-Wave (SWAN boundary), USGS NSHM zones (OpenQuake), municipal storm-sewer
  GIS (SWMM - likely permanently user-supplied).

The core structural fact behind NATE's concern: **every gate fires on `is None`.**
It stops a MISSING input; it does NOT distinguish a user-provided value from an
LLM-invented one. Once the LLM passes any plausible number, no template rejects it
and no field records that it was model-originated.

---

## 3. Ranked risk list (worst first)

Where an LLM-invented or silently-defaulted input most plausibly corrupts a
real-looking result:

**1. OpenQuake PSHA - Vs30 hardcoded 760 m/s globally + single default GMPE.**
Site amplification is the single dominant control on ground motion; a soft-soil
basin (Vs30 ~180-300) can double or more the hazard vs the baked NEHRP B/C rock
value. `reference_vs30_value = 760.0` is written into every `job.ini`
(`job_ini.py:559-563`) with no parameter, no fetch, and no output field. GMPE is a
single class regardless of tectonic region. **Failure story:** user asks for
seismic hazard over a soft-soil city, gets a confident 475-yr PGA map that
silently modeled rock site conditions with a possibly region-inappropriate GMPE,
and NOTHING in the narration flags it. The one genuine honesty field
(`source_model_kind`) covers only the fault-vs-synthetic source, not Vs30/GMPE.

**2. SWMM urban_flood - the "storm-drain" model has no storm drains.** The
drainage network is fully synthesized from DEM cells (one storage node per cell,
4-connectivity overland links, one outfall - `swmm_mesh_builder.py:958-1226`); it
is a quasi-2D overland grid, not a pipe network. Manning n is a flat ungated 0.03
(landcover roughness dead despite NLCD being fetched for SFINCS), infiltration
constants are baked and not even exposed, and rainfall SILENTLY falls back to
120 mm if the Atlas-14 lookup fails. **Failure story:** user asks "can the storm
drains in [city] handle a 100-yr storm" - the system prompt explicitly routes
storm-drain/pipe-network questions here - and gets a pipe-capacity-looking answer
from a model with zero pipes and possibly a silent 120 mm default. No caveat
reaches chat.

**3. GeoClaw dam-break / tsunami - source is invented, honesty banner never
narrated.** Dam height defaults to 10 m, dam location to the AOI centroid,
earthquake to Mw 8.0, fault geometry to synthetic Okada. The worker DOES print a
loud "NON-SITE-SPECIFIC synthetic source ... illustrative" banner - but only to
`geoclaw.stdout`, never into `GeoClawDepthLayerURI` or the narration
(`setrun_builder.py:522-541`). `fetch_usace_dams` (NID) exposes real
`DAM_HEIGHT`/lat-lon and is unwired. **Failure story:** user asks to model a break
at a named dam, gets an authoritative inundation map from a generic 10 m dam at the
map centroid (not the real dam), no caveat in chat.

**4. Landlab landslide susceptibility - the deliverable IS the unfetched soil
physics.** Cohesion, friction angle, density, thickness, transmissivity, and
triggering rainfall are all baked demo constants with NO output caveat
(`landlab_contracts.py:170-186`); the factor-of-safety / probability-of-failure
map is entirely a function of them. **Failure story:** user asks for landslide
susceptibility over a real area, gets a site-specific-looking probability map
computed from generic soil, indistinguishable from a calibrated result. SSURGO/
POLARIS/gridMET are the real sources and none exist.

**5. TELEMAC river dye - the carrier discharge that governs dilution is a hidden
constant.** `inflow_q_m3s = 250 m3/s` is a hardcoded worker constant, not even an
exposed parameter, heuristically rescaled only from channel width
(`worker :64`, `entrypoint.py:252` flags NWM as the "#223 follow-up"). Dilution
and transport concentrations scale directly with this; on a small creek it can be
100x too high. The `fallback_note` caveat says "idealized bed" but never says
"discharge is a guess." **Failure story:** user asks where a spill in a river
goes; the concentration field is off by orders of magnitude because of an invisible
250 m3/s, presented under a caveat that points at the wrong assumption.

Runners-up: **SWAN** (offshore Hs/Tp/Dir synthesized as a 3 m storm when unset,
and `fallback_note` never populated despite a docstring claim - fully synthetic
wave forcing reaches the map with zero label; wind/tide fetchers exist unwired);
**ELMFIRE** (wind 15 mph / "dry" fuels drive spread and are unlabeled in the
result - but mitigated because the pre-run confirm card DOES show them in prose
and they are overridable); **MODFLOW regional_water_budget** (only MODFLOW
template with no `demo_aquifer_caveat` at all - lower stakes as a coarse budget).

---

## 4. MISSING-FETCHER queue (seed a fetcher backlog)

Ordered by leverage. "Wire" = fetcher already registered, just plumb it in;
"New source.yaml" = candidate for the universal-ingest spec model.

| # | Missing input | Real public source | Consumers | Ingest fit |
|---|---|---|---|---|
| 1 | Vs30 site conditions | USGS Vs30 map (global slope-based Vs30 / Thompson et al.) | OpenQuake (highest risk) | New source.yaml (WCS/raster sample), add `vs30` param |
| 2 | Soil geotech + hydraulic | SSURGO / gNATSGO (USDA NRCS), POLARIS (cohesion-proxy, texture, Ksat, depth) | Landlab soil block; SWMM curve-number/Green-Ampt | New source.yaml (raster + zonal); highest breadth |
| 3 | Wind + dead-fuel moisture | `fetch_gridmet` (`vs`, `fm100`, `fm1000`), `fetch_raws_weather`, `fetch_hrrr_forecast` | ELMFIRE (all registered, unwired) | WIRE existing |
| 4 | Dam geometry + location | `fetch_usace_dams` (NID: `DAM_HEIGHT`, `NID_STORAGE`, lat/lon) | GeoClaw dam-break (registered, unwired) | WIRE existing |
| 5 | River carrier discharge | `fetch_noaa_nwm_streamflow` (registered) / USGS NWIS | TELEMAC (#223), firm up SFINCS fluvial | WIRE existing / small source.yaml |
| 6 | Wave boundary Hs/Tp/Dir | NDBC buoys / WAVEWATCH III / GFS-Wave | SWAN offshore boundary | New source.yaml |
| 7 | Tide / still-water level | `fetch_noaa_coops_tides`, `fetch_gtsm_tide_surge` (both registered) | SWAN (unwired), GeoClaw surge | WIRE existing |
| 8 | Offshore/coastal wind | `fetch_era5_reanalysis` (registered, `10m_wind_speed`) | SWAN wind field (unwired) | WIRE existing |
| 9 | Triggering / design rainfall | NOAA Atlas 14 (`lookup_precip_return_period`, already used in SFINCS/SWMM), `fetch_gridmet` `pr` | Landlab recharge/intensity (unwired) | WIRE existing |
| 10 | Earthquake source magnitude | USGS ComCat earthquake catalog | GeoClaw tsunami plausible Mw | New source.yaml |
| 11 | Seismic source zones | USGS National Seismic Hazard Model (NSHM) | OpenQuake areal source model | New source.yaml (larger) |
| 12 | Storm-sewer / pipe network | Municipal/county stormwater-utility GIS (open data, per-city) | SWMM real network | Likely stays user-supplied; per-city, no universal source |

Note how much of the gap is "have-but-not-wired" (#3, #4, #5, #7, #8, #9): the
codebase already fetches these elsewhere. That is the cheapest tranche and it
converts several UNLABELED defaults straight to FETCHED.

---

## 5. Gate design proposal (proposal only)

Two complementary mechanisms, both consistent with the existing gate patterns:
the #154 granularity gate (`PayloadWarningEnvelopePayload` +
proceed/cancel/narrow_scope, `gates/cards/solver_confirm.py`), ADR 0091's gated
DEM fallback (a typed retryable error carrying `.suggestions`, surfaced by
`summarize_tool_result` and riding the tool-retry loop), and ADR 0009 (sims own
their inputs; user overrides validated; typed errors on failure).

### 5a. A typed `INPUT_REQUIRED` envelope for sensitivity-dominant inputs

Promote the existing string `error_code="USER_INPUT_REQUIRED"` into a STRUCTURED
recovery envelope. Reuse ADR 0091's chosen mechanism verbatim: a typed error with
a `.suggestions`/`.missing_inputs` payload that `summarize_tool_result` already
surfaces as a structured list (`adapter.py:2167-2171`) - so NO new transport
machinery is needed. The composer raises it when a HIGH-SENSITIVITY physical input
is absent AND no fetcher supplied it:

```
InputRequiredError(
  error_code="INPUT_REQUIRED",
  missing_inputs=[
    {param: "vs30", units: "m/s", plausible_range: [150, 1500],
     why: "site amplification dominates PGA; no Vs30 fetcher yet",
     fetch_candidate: "USGS Vs30 map",
     default_if_proceed: 760.0}
  ],
  options: ["provide", "proceed_with_defaults", "cancel"])
```

The agent narrates: "I need Vs30 (m/s, typically 150-1500) - or I can proceed with
a generic 760 m/s rock default, which will not be site-specific. Which do you
want?" This turns a silent dominant default into an explicit user choice, exactly
the "user gate" NATE asked for, without blocking the legitimate toy-demo path
(`proceed_with_defaults`). It rides the existing tool-retry loop: the user's answer
comes back, the LLM retries with the value.

Apply the hard `INPUT_REQUIRED` gate to the sensitivity-dominant, currently-ungated
inputs: **OpenQuake Vs30 (+ GMPE region check), GeoClaw dam height/location + quake
Mw/fault, TELEMAC carrier discharge, SWAN wave boundary, Landlab soil block +
triggering rainfall, SWMM (acknowledge the synthetic network + expose Manning).**
~6 engines. (The MODFLOW well/spill/footprint gates already do exactly this shape;
they are the template.)

### 5b. Structured, loud demo-default labeling for the toy-demo path

Where proceeding with a default is legitimate, make the default machine-visible
instead of buried prose:

1. **Envelope field.** Add a typed `synthetic_inputs` / `assumptions` list to every
   result summary:
   `[{param, value, units, basis: "demo-default", real_source_if_any}]`. This
   replaces the free-text `demo_aquifer_caveat` / `fallback_note` prose with a
   structured field the LLM can enumerate. Three narration-seam fixes make it
   actually reach the user:
   - Add `synthetic_inputs` to the kept keys in
     `_summarize_published_scenario_layer` (`adapter.py:2049-2090`) - today it
     drops everything but layer id/name/bbox, so any caveat on the bare-published
     LayerURI path (`sfincs_flood`, `swmm_urban_flood`) is lost entirely.
   - Promote Pelicun's `n_assets_default_replacement_value` (and a new
     `n_assets_default_occupancy`) from `raw_envelope` into `envelope_summary`
     (`compute_impact_envelope.py:486-502`) - it is computed today and then
     dropped from the cited surface.
   - Populate `fallback_note` in the SWAN chain (currently the docstring claims it,
     `run_swan.py:150-152`, but `grep` finds zero writes) and itemize discharge/
     friction in TELEMAC's `fallback_note`.

2. **Narration rule (system prompt).** Add ONE standing directive - this is the
   doctrine gap. Today `adapter.py:399-402` says "Never fabricate numbers" but that
   covers only NARRATING result numbers (depth/area/count) and geocoding a
   location; there is NO instruction about choosing model INPUT parameters. Add:

   > Physical model inputs (rates, magnitudes, material properties, forcing
   > values) are never invented. If a required physical parameter has no value and
   > no fetcher, do NOT fill it - the tool returns `INPUT_REQUIRED`; relay the
   > missing parameters (with units and typical ranges) and ASK the user. When a
   > tool result carries a `synthetic_inputs`/`assumptions` list, you MUST state in
   > your narration which quantities are demo defaults vs site-derived.

### 5c. Scope estimate

- `INPUT_REQUIRED` hard gate (5a): OpenQuake, GeoClaw, TELEMAC, SWAN, Landlab, SWMM
  (~6 engines). MODFLOW/SFINCS/Pelicun/ELMFIRE already gate their irreducible
  inputs.
- Wire-existing-fetcher (cheapest; removes the default outright): ELMFIRE
  (gridMET/RAWS/HRRR), GeoClaw (USACE dams), SWAN (ERA5/CO-OPS/GTSM), TELEMAC
  (NWM), Landlab + SWMM rainfall (Atlas14/gridMET). Do these BEFORE gating - a
  wired fetcher is strictly better than a user prompt.
- Structured `synthetic_inputs` label + the three narration-seam fixes + the
  system-prompt rule (5b): benefits all 30, priority on the currently-UNLABELED
  set (ELMFIRE, GeoClaw, Landlab, OpenQuake Vs30, SWMM, SWAN,
  regional_water_budget). The already-labeled MODFLOW templates just migrate their
  prose caveat into the structured field.

---

## 6. Is NATE overstating it? Honest verdict

No - but the picture is uneven, not uniform, and precision matters. The
irreducible, obviously-user inputs are genuinely handled: well locations, pumping/
injection/recovery rates, spill mass and species, basin/pit/wetland footprints,
the saltwater coastal transect, levee-breach discharge, tsunami wave height, the
fire ignition point, and the Pelicun hazard raster + asset inventory ALL raise a
typed `USER_INPUT_REQUIRED`/`*_INPUT_INVALID` error that stops the run rather than
inventing a value. That is roughly the MODFLOW + SFINCS + Pelicun + ELMFIRE-ignition
half of the surface, and there the "totally missing input silently invented" fear
is real-but-mitigated.

Where NATE is exactly right, and it is not a small corner: (1) every gate fires on
ABSENCE only - a present-but-invented value sails through with zero provenance
distinction, so an LLM that fills a plausible number is indistinguishable from a
user who supplied one, and no field records which it was; (2) a whole class of
physically DOMINANT parameters is silently demo-defaulted with no caveat reaching
the user - Vs30 (OpenQuake), the entire soil block (Landlab), dam height/location +
quake source (GeoClaw), carrier discharge (TELEMAC), the wave boundary (SWAN), and
the whole "drainage network" + Manning + infiltration (SWMM) - and several of these
have real fetchers already sitting unused in the codebase; (3) even the honest
labels that DO exist are free-text prose in a summary dict rather than structured
provenance, on the published-layer narration path they are dropped entirely, and
the system prompt has no standing rule telling the model to ask rather than invent
a physical input. The result is authoritative-looking output built on invented
physics for about six of the ten engines - precisely the "hand waved ... LLM
hallucinates input" failure mode he named. His instinct to add user-input gates is
correct; the right build is the two-part gate above, done fetcher-first so a gate
is the fallback, not the first resort.
