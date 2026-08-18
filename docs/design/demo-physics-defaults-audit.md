# Demo physics-defaults audit -- the invented-value inventory

Scope distinct from `fallback-audit.md`: that audit swept RUNTIME fallbacks (a
primary dies and something else continues). THIS audit sweeps PARAMETER DEFAULTS
-- values a template uses when the user supplies nothing and no fetcher served
them. The charge (NATE, verbatim spirit): *"I don't want it to honestly degrade
and ruin the simulation -- I don't want it to do that in the first place."* A
label/caveat is NOT a license. The exemplar now ruled UNACCEPTABLE: MODFLOW's
`DEFAULT_AQUIFER_K_MS = 1e-4` / `DEFAULT_POROSITY = 0.3` demo aquifer -- it runs
by default, labeled `default_demo`, caveat in the summary, and NATE's ruling says
a physics-consequential parameter with no real data source must REFUSE by default
(a typed error naming what is needed), not run on an invented value.

Method: swept every registered template/composer in `trid3nt_server/workflows/`
for (a) `SyntheticInput(... basis="default_demo" ...)` provenance entries and
(b) module/RunArgs float defaults with no provenance surface at all. Read around
every physics-consequential hit. Classified each against the four buckets below.

## Counts by class

- **Registered composer templates swept:** ~90 (across 11 engines).
- **INVENTED-PHYSICS defaults (the target class):** 34 distinct
  (template, parameter) rows -- material properties, forcing magnitudes, source
  terms, boundary values, friction/decay coefficients. Enumerated in the table.
- **Of those, currently LABELED** (`default_demo` SyntheticInput, rides the
  input-review gate): 26 rows across ~30 templates.
- **Of those, riding SILENTLY** (no provenance surface -- worse than the
  exemplar): 8 rows (geoclaw storm_surge/regional_manning Manning, swmm
  aquifer_baseflow soil column, telemac do_sag/streeter BOD+reaeration, elmfire
  fire-weather wind/moisture, swan wave physics coefficients).
- **MISLABELED** (invented engineering stamped `basis="derived"` -- reads as
  site-derived): 1 template, 4 params (hecras culvert barrel geometry).
- **BORDERLINE (scenario-vs-invention)** flags: 9 rows -- default earthquake
  magnitude, default storm climatology, default fault throw, default BOD load,
  default fire weather. Detailed in the borderline section.
- **DATA-DERIVED defaults (fine):** the capture_zone SoilGrids pedotransfer K,
  the sfincs NLCD->Manning grid, fetched-bathy resolutions, station-nearest
  tide series -- real basis, out of scope.
- **SCENARIO parameters (fine):** sim_days / sim_duration_s / return period /
  species list / source location / what-if interventions / rain_scale. These are
  the user's QUESTION, not a claim about the world. Out of scope (borderline
  members flagged separately).
- **NUMERICAL / solver settings (fine):** target_resolution_m, site_grid_km,
  time_step_s, freq ranges, `sfincs/numerical_physics` tuning surface, RasUnsteady
  computation_interval. Not world-claims. Out of scope.
- **CANONICAL validation cases (fine):** thacker_validation, package_validation,
  transport_validation, closed_form_validation, hazus_* lifeline runs, elmfire
  crown_ros/verification. The "demo default" IS the published case definition;
  refusing would break the benchmark. Out of scope.

## The invented-physics table

Columns: PARAM = the invented value; DEFAULT + LOCATION; SURFACING = how it reaches
the user today (LABELED default_demo via the input-review gate / SILENT / MISLABEL);
CONVERSION = the refuse-by-default target (typed-error need + whether a real data
source can serve it instead).

### MODFLOW -- the exemplar family (shared `_input_review.py`)

| # | TEMPLATE(S) | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 1 | capture_zone, river_seepage, wetland_hydroperiod, asr, managed_recharge, mine_dewatering, regional_water_budget, sustainable_yield, saltwater_intrusion, contaminant_plume, thermal_plume, vadose_transport | `aquifer_k_ms` | `1e-4` m/s `contracts/trid3nt_contracts/modflow_contracts.py:96` | LABELED (`aquifer_k_review_entry`) | REFUSE: "aquifer hydraulic conductivity (m/s) is required and could not be resolved." **Real source EXISTS**: capture_zone already derives K from SoilGrids texture via Saxton-Rawls pedotransfer (`aquifer_k_basis` -> `soil_pedotransfer`); wire that path into the whole archetype family and refuse when SoilGrids is unavailable. |
| 2 | (same family) | `porosity` | `0.3` `modflow_contracts.py:97` | LABELED | REFUSE or pedotransfer-derive alongside K (same SoilGrids read). |
| 3 | vadose_transport | Brooks-Corey `thtr=0.05 / thts=0.35 / eps=4.0 / thti=0.08 / vks=0.1` + `infiltration_rate=0.01 m/day` + `infiltration_conc=1.0` | `workers/modflow/gwt_adapter.py:265-273` | LABELED (`vadose_soil_review_entries`) | REFUSE: "unsaturated-zone soil hydraulics (Brooks-Corey theta_r/theta_s/eps + Ksat) required." SoilGrids texture + pedotransfer can serve theta_r/theta_s/Ksat; the infiltration flux is a scenario forcing (keep as scenario). |
| 4 | thermal_plume | `ambient_temperature_c=10`, `injection_temperature_c=ambient+30`, `thermal_conductivity_solid` + heat capacities `4184/800` + densities `1000/2650` | `gwt_adapter.py` + `_input_review.thermal_demo_review_entries` | LABELED | REFUSE: "undisturbed aquifer temperature + grain thermal conductivity required." Ambient temp has a real source (groundwater-temperature climatology / mean annual air temp proxy); grain conductivity is a literature-range user-gated offer. |
| 5 | river_seepage, contaminant_plume (SFR reaches) | `DEFAULT_SFR_STREAMBED_K_M_DAY=0.5`, `DEFAULT_SFR_STREAMBED_GRADIENT=0.001` | `gwt_adapter.py:169,181` | LABELED (SyntheticInput on archetype deck) | Streambed gradient IS DEM-derivable (rbot from the 3DEP long-profile) -- make DEM the default, REFUSE when unreadable rather than the flat 0.001. Streambed K: literature-range user-gated offer. |
| 6 | saltwater_intrusion | `seawater_salinity_ppt` demo default | `modflow_contracts.py:974` + gate_and_stamp | LABELED | Seawater salinity is well-constrained (~35 ppt ocean, estuary gradient) -- a literature constant, low harm; keep as user-gated literature offer, low priority. |
| 7 | capture_zone | `regional_gradient` demo when no DEM | `capture_zone.py:1391` (`default_demo` when `layer_grad_source` not user/dem) | LABELED | Already DEM-derives when it can (`basis="derived"` on the dem path); REFUSE when DEM unreadable instead of the demo gradient. |

### LANDLAB

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 8 | susceptibility, landslide_storm_ensemble | soil `cohesion_pa / internal_friction_deg / density / thickness / transmissivity` | RunArgs `run_landlab.py:162-165` | LABELED (`soil_properties` entry) | REFUSE: "slope-stability soil strength (cohesion, friction angle, thickness) required." SoilGrids + a strength pedotransfer can serve density/thickness; cohesion/friction are literature-range user-gated offers (they set the factor-of-safety directly). |
| 9 | groundwater_water_table, groundwater_storm_recession | `gw_hydraulic_conductivity_m_s`, `gw_porosity=0.3`, `gw_recharge_mm_yr=200` | RunArgs `run_landlab.py:224-231` | LABELED (`aquifer_properties`) | REFUSE: K + recharge required. K -> SoilGrids pedotransfer (same seam as MODFLOW #1); recharge has a real source (gridded recharge / P-ET), or scenario-gate it. |
| 10 | green_ampt | `soil_hydraulic_conductivity_m_s` + `green_ampt_soil_type` | RunArgs; entry `green_ampt.py:277` | LABELED (`soil_hydraulic_properties`) | REFUSE or SoilGrids-derive Ksat + Green-Ampt suction from texture (published texture->parameter table). |
| 11 | channel_incision | stream-power `K_sp` erodibility + `m_sp=0.5 / n_sp=1.0` + uplift rate | RunArgs `run_landlab.py:207-208`; entry `channel_incision.py:177` | LABELED (`uplift_erodibility_forcing`) | Erodibility K_sp has NO fetchable real-world value (calibration coefficient) -> REFUSE by default with a literature-range user-gated offer. m_sp/n_sp are canonical stream-power exponents (0.5/1.0) -- keep as documented defaults (borderline, see flags). |

### HEC-RAS

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 12 | culvert_embankment_flow | `barrel_diameter_m=1.0`, `k_in=0.5`, `k_out=1.0`, `barrel_manning=0.013` | `culvert_embankment_flow.py:76-81` | **MISLABEL** -- stamped `basis="derived"` (barrel_diameter: `derived` when == default!), reads as site-derived (`culvert_embankment_flow.py:319-326`) | FIRST relabel `derived`->`default_demo` (these are un-fetchable engineering, the docstring even says "UN-FETCHABLE"), THEN REFUSE by default: "culvert barrel rise/span + entrance-loss coefficients required (not derivable from terrain)." No real source; user-gated engineering entry only. |
| 13 | flood_2d | `peak_inflow` from `_DEFAULT_PEAK_CFS=5000` | `flood_2d.py:77,325` | MISLABEL-adjacent -- built from the demo constant then stamped `basis="user"` at `:657` | Peak inflow IS regionally derivable (USGS regression / gauge); make that the default and REFUSE when it cannot be resolved, never the 5000 cfs literal stamped as user. |
| 14 | riverine_flood, levee_breach, flood_2d, culvert | `geometry` = baked Muncie White River demonstration model | `riverine_flood.py:394`, `levee_breach.py:413` | LABELED (`default_demo`) | This is `fallback-audit.md` row 27 (NEEDS-GATE): a place-named request silently solves on foreign geometry. Physics-consequential (whole terrain). REFUSE/GATE: a real-AOI request must not answer with Muncie; the demo geometry is only valid for an explicit "run the Muncie demo" ask. |
| 15 | levee_breach | `breach_params` = 2 baked lateral-structure breaches | `levee_breach.py:407` | LABELED | Breach width/invert/formation-time are un-fetchable scenario engineering -> user-gated offer; couples to #14 (baked geometry). |

### GEOCLAW

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 16 | storm_surge, regional_manning | `manning_n = 0.025` | `storm_surge.py:139`, `regional_manning.py` | **SILENT** (no provenance surface) | REFUSE or NLCD-derive: the sfincs NLCD->Manning table (`manning_mapping.csv`) already exists -- reuse it. A single 0.025 over a whole coast is invented friction; at minimum surface it, target NLCD-derived. |
| 17 | inundation | `fault_geometry` = "generic synthetic Okada" | `inundation.py:704` | LABELED | Scenario source when the user names no fault -> REFUSE unless a real/scenario fault is named (couples to #18 magnitude). |

### OPENQUAKE

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 18 | psha, scenario_gmf, secondary_perils, disaggregation, event_based | `reference_vs30` (site amplification) | RunArgs; entries `psha.py:307`, `scenario_gmf.py`, etc. | LABELED (`default_demo` when not user) | Vs30 controls the site amplification of every ground-motion result -> REFUSE by default. **Real source**: USGS Vs30 web service / global topographic-slope Vs30 (no fetcher today -- build one, or refuse). A single reference Vs30 over a region is invented site response. |

### SCHISM

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 19 | baroclinic_circulation | `river_discharge = 500 m3/s` | `baroclinic_circulation.py:553` | LABELED | REFUSE or fetch: NWIS streamflow (hydrology fetchers) serves real discharge at the estuary's river input. |
| 20 | baroclinic_circulation | `ocean_salinity = 33 psu` | `:556` | LABELED | Ocean boundary salinity -> World Ocean Atlas climatology (real source) or user-gated literature (well-constrained ~33-35). |
| 21 | tidal_hydro | `tidal_amplitude_m = 0.5` + baked M2 analytical boundary | `tidal_hydro.py:434,617` | LABELED | Tidal amplitude/constituents are fetchable (NOAA CO-OPS harmonic constituents) -> derive, REFUSE when the AOI has no station. |
| 22 | coupled_waves, pahm_surge | bundled wave-boundary spectrum / parametric forcing; synthetic bathymetry when no COG | `coupled_waves.py:389`, `pahm_surge.py:822,827` | LABELED | Synthetic bathymetry = invented terrain -> REFUSE when the real bathy COG is unavailable (do not solve surge on a made-up seabed). Wave boundary is a scenario forcing (keep). |

### SWMM

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 23 | urban_flood | `overland_manning_n = 0.03` | `urban_flood.py:695,991` | LABELED | NLCD-derive (reuse `manning_mapping.csv`) or REFUSE; a uniform 0.03 over an urban catchment is invented roughness. |
| 24 | network_import | subcatchment `curve_number = 90.0`, `junction_subarea` uniform, `node_inverts` filled | `network_import.py:544,425,582` | LABELED | CN is NLCD+SSURGO-derivable (real source) -> derive or REFUSE. Junction sub-area / inverts are un-fetchable network engineering (user-gated). |
| 25 | urban_flood | `drainage_network = "synthesized"` (synthetic pipe topology) | `urban_flood.py:690,986` | LABELED | Synthetic topology changes routing -> REFUSE unless a real network is imported, or gate loudly as an explicit "no real network -> synthesized demo" opt-in. |
| 26 | dual_drainage | `inlet_capture` opening geometry | `dual_drainage.py:339` | LABELED | Un-fetchable inlet engineering -> user-gated literature offer. |
| 27 | aquifer_baseflow | `porosity=0.46, wilting=0.13, field_capacity=0.28, conductivity=0.8 in/hr, initial_water_table_ft=4` | `aquifer_baseflow.py:121-126` | **SILENT** (no provenance surface) | REFUSE or SoilGrids-derive the soil-column hydraulics; these five drive the baseflow recession directly and ride completely unlabeled today. |

### TELEMAC

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 28 | wave_field | `wind_speed_mps = 20` (storm wind forcing) | `wave_field.py:89,526` | LABELED | REFUSE or fetch: weather/wind fetchers serve real wind; a prescribed 20 m/s storm is invented forcing. Same for wind_direction. |
| 29 | agitation | `wave_period_s=8`, `wave_height_m=1.0`, `reflection_coef=1.0` | `agitation.py:93-96,627-631` | LABELED | Incident wave forcing + wall reflection -> REFUSE or fetch boundary wave (NDBC/WW3); reflection_coef is a literature-range user-gated offer. |
| 30 | stratified_flow | `wind_speed_mps` + thermocline `warm_temp_c/cold_temp_c` | `stratified_flow.py:553,567` | LABELED | Thermocline temperatures are the initial/boundary stratification -> REFUSE or fetch (lake temperature profiles); wind as #28. |
| 31 | river_dye | `channel_width_m=60`, `reach_length_km=6` (ribbon fallback) | `river_dye.py:925-926,2696` | LABELED (`bank_geometry` default_demo when not NHD) | GOLD sibling: `bank_source` is already user-gated (`fallback-audit.md` row 23). Extend: the ribbon channel width is invented geometry -> keep it behind the same gate, refuse silent substitution (already partly done). |
| 32 | do_sag, streeter_phelps | `discharge_bod_mgl=20`, `water_temp_c=20`, constant reaeration k2 | `do_sag.py:112-114`; `streeter_phelps.py` | **SILENT** (no provenance surface) | Water temp + reaeration coefficient are physics -> REFUSE or fetch (water-quality/temperature: `fetch_usgs_water_quality` exists). BOD load is the scenario question (keep, but surface it -- rides unlabeled today). |
| 33 | coastal_tidal_surge | `datum_offset_m` demo default | `coastal_tidal_surge.py:658` | LABELED | Vertical datum offset is station-derivable (NOAA datums) -> derive or REFUSE; a wrong datum shifts the whole surge vertically. |

### ELMFIRE

| # | TEMPLATE | PARAM | DEFAULT + LOCATION | SURFACING | CONVERSION |
|---|---|---|---|---|---|
| 34 | fire_spread (+ spotting, crown, initial_attack, sensitivity variants) | `wind_speed_mph=15`, `wind_dir_deg=0`, `fuel_moisture="dry"` | `fire_spread.py:123-125` | **SILENT** (no provenance surface) | Fire weather drives the entire spread -> at minimum LABEL it; target REFUSE-or-fetch (RAWS / gridMET / HRRR fire weather). BORDERLINE: these are also the what-if levers -- see flags. `wind_schedule` + `dead_fuel_interp` already LABEL their transient schedules `default_demo`. |

## Borderline flags (scenario-vs-invention)

These sit on the line: the parameter is partly the user's QUESTION and partly a
claim about the world. NATE's call needed per row.

- **Default earthquake magnitude** (`DEFAULT_SCENARIO_MAGNITUDE`, geoclaw
  inundation Mw=8, openquake scenario_gmf/secondary_perils): a scenario run's
  magnitude IS the question -- BUT a *default* Mw invents "what event" when the
  user only names a place. Flag: a scenario template with no named
  event/magnitude should arguably prompt, not assume Mw 8.
- **Default fire weather** (elmfire wind/moisture, row 34): the what-if lever,
  but "dry / critical / 15 mph" is a specific invented weather when the user just
  names a fire. Real source exists -> lean toward fetch-or-refuse.
- **Default storm climatology** (landlab storm_sequence / storm_ensemble /
  groundwater_storm_recession: Poisson mean depth + intensity): forcing that is
  also the scenario. Flag as scenario-default (keep) but surface loudly.
- **Default fault throw / uplift rate** (landlab normal_fault, channel_incision
  uplift): tectonic forcing = the question; keep as scenario, but the
  erodibility `K_sp` in the same entry is a true invented coefficient (#11).
- **Default BOD load** (telemac do_sag `discharge_bod_mgl=20`): the pollutant
  source term IS the scenario question -- keep, but it rides SILENT today and
  must at least be labeled.
- **Stream-power exponents** `m_sp=0.5 / n_sp=1.0` (landlab): canonical published
  values, not site claims -> keep as documented defaults (like a numerical
  constant).
- **SWAN physics coefficients** `breaking_gamma=0.73 / breaking_alpha=1.0 /
  friction_cfjon=0.067 / triad_*` (`swan/wave_field/wave_field.py:259-264`):
  literature-canonical SWAN calibration constants, single universally-accepted
  published values -> keep, but they ride SILENT; add a documented-default label.
- **Seawater salinity ~35 ppt** (#6, #20): well-constrained physical constant ->
  low-harm literature default, low priority.
- **Green-Ampt / Manning literature constants** generally: where a single
  published value is the professional standard (concrete-pipe n=0.013), a
  user-gated literature offer is the right conversion, not a hard refuse.

## Gate mechanics finding + auto-mode blast radius

**How the gate behaves today.** `trid3nt_server/gates/input_review.py`
`gate_input_review()` is NATE's two-mode design:

- `auto` mode (session default): returns `ReviewOutcome(proceed=True, ...)`
  immediately (`input_review.py:238-240`) -- a pass-through. Every entry,
  including `basis="default_demo"`, rides through labeled but UN-BLOCKED.
- `user_gated` mode: pauses on the #154 pending-confirmation spine and presents
  the resolved entries; with no live emitter (direct-call/offline) it FAILS OPEN
  (`:250-256`, `proceed=True`).

So **today, in auto mode, an invented-physics default NEVER refuses.** The label
is the only consequence. This is exactly what the ruling overturns.

**What the ruling changes.** An invented-physics entry (physics-consequential +
`basis="default_demo"` + no real source served it) must REFUSE in auto mode -- a
typed error naming the need -- mirroring `fallback-ladders.md` rule 4: *"synthetic
rungs ALWAYS gate, and their labeled default is refuse."* The gate cannot make
this call from `basis` alone, because `default_demo` today covers three very
different things: invented physics (refuse), scenario/AOI defaults (fine to
proceed), and even numerical defaults. **The enabling change is a consequence tag
on `SyntheticInput`** (e.g. `consequence: Literal["physics","scenario",
"numerical","aoi"]`, or a narrower `refuse_in_auto: bool`) so the gate can, in
auto mode, cancel when any entry is `consequence="physics"` AND
`basis="default_demo"`, while letting scenario/numerical/aoi demo defaults pass.
Without that discriminator, refusing on all `default_demo` would wrongly break
every scenario-default and AOI-default run.

**Blast radius.** The gate flip touches nearly the entire template surface. In
auto mode today these all PROCEED on an invented physics default and would flip to
REFUSE-unless-supplied-or-fetched:

- MODFLOW archetype family: **12 templates** (every one, via the shared aquifer
  K/porosity default) -- the single largest cluster.
- LANDLAB: **6-8** (susceptibility, landslide_storm_ensemble,
  groundwater_water_table, groundwater_storm_recession, green_ampt,
  channel_incision, +normal_fault/storm_sequence borderline).
- OPENQUAKE: **5** (vs30 across psha, scenario_gmf, secondary_perils,
  disaggregation, event_based).
- TELEMAC: **6** (wave_field, agitation, stratified_flow, river_dye,
  coastal_tidal_surge, do_sag).
- SWMM: **5** (urban_flood, network_import, dual_drainage, deck_runner,
  aquifer_baseflow).
- SCHISM: **4** (baroclinic_circulation, coupled_waves, pahm_surge, tidal_hydro).
- HEC-RAS: **4** (riverine_flood, levee_breach, flood_2d, culvert).
- GEOCLAW: **3** (inundation, storm_surge, regional_manning).
- ELMFIRE: **1 + variants** (fire_spread family, borderline).

**~45-50 templates change auto-mode behavior.** A place-named prompt that today
silently solves on a demo aquifer / uniform Manning / prescribed storm wind would
instead return a typed refusal until the value is supplied, fetched, or the user
opts into the demo. This is the number NATE must sign off before the wave: it is
close to "every physics engine changes its default answer for an under-specified
prompt." The canonical-validation, scenario, numerical, and data-derived defaults
(the "fine" buckets) do NOT change -- the consequence tag is what keeps them out.

## Conversion-wave plan

Grouped by family; sized S/M/L. The mechanism wave (P1) is the gate; it must land
first because every downstream wave keys on the consequence tag and the
refuse-in-auto behavior.

> **STATUS (2026-08-18): P1, P2, P3, P3-completion, P4, P5 LANDED** (ADR 0285). P5
> (AFK-conservative) wired schism baroclinic river_discharge (#19) to the new shared
> `discharge_resolve` NWM seam (derive-or-refuse; 500 demo constant deleted; live
> A/B), made ocean_salinity (#20) a literature offer (WOA queued), and VERIFIED the
> synthetic-bathy rows (#22) compliant (pahm_surge opt-in-gated; coupled_waves an
> audit misread). Rows 21/28/29/30/33 carry per-row verdicts + a QUEUED-FOR-NATE list
> (WOA / tidal-constituent boundary / wind consequence fork / NDBC / CO-OPS datums /
> lake profiles). See ADR 0285 P5. P3-completion
> wired the staged per-engine template conversions on the P3 substrate: landlab
> **row 8** (susceptibility soil-strength: bulk density DERIVED from texture,
> cohesion/friction/thickness/transmissivity REFUSE with literature offers -- law-6
> correction: thickness is NOT texture-derivable, contra the audit's conversion
> column), **row 9** (groundwater K+porosity DERIVED; recharge stays scenario -- a
> precip-fraction estimate would invent the fraction; aquifer thickness = scenario
> structural assumption), **row 10** (green_ampt Ksat + USDA texture class DERIVED),
> **row 11** (channel_incision K_sp REFUSES with a literature offer; uplift ->
> scenario, m/n -> numerical), and swmm **row 27** (aquifer_baseflow two-zone column
> DERIVED from SoilGrids via a new gate, was SILENT). The 10 physics demo constants
> DELETED; the river_seepage refusal-test premise fixed; live aquifer-column A/B at
> Ames IA (derived conductivity 0.13 in/hr vs the dead 0.8 demo). P1 = the `consequence`
> tag + refuse-in-auto + the 3-layer sweep guard. P2 = the MODFLOW exemplar (rows
> 1-7): the shared `_aquifer_resolve` SoilGrids seam, the demo constants deleted,
> 12 archetypes wired to derive-or-refuse, and the live Woburn TCE A/B. P3 = the
> soil-hydraulics substrate move (`_aquifer_resolve` hoisted to
> `workflows/shared/aquifer_resolve.py` + `derive_soil_column` for the SWMM
> two-zone column; per-engine template conversions for rows 8-10, 27 staged).
> P4 = roughness/Manning: the shared `roughness_resolve` NLCD-derived-or-refuse
> seam, **row 23** (swmm urban_flood `overland_manning_n`) + **row 16** (geoclaw
> storm_surge `manning_n`) CONVERTED (0.03/0.025 demo constants deleted, live
> urban_flood A/B). **row 24** = an audit MISREAD (the cited `curve_number=90.0` is
> a demo *rainfall depth*, not a CN; network_import uses Horton, not SCS-CN -- no CN
> to derive; the real invented params there are the SubArea `n_imperv/n_perv/
> imperviousness` literature constants, QUEUED for a label-only pass). fallback-audit
> **row 17** (raster_cell_mesh `n_imperv/n_perv`) is a DIFFERENT value-path (SubArea
> surface roughness, not the overland conduit n) -- NOT converted (reported, not
> silently widened). Geoclaw siblings `inundation`/`amr_regions`/`gauge_timeseries`
> share the 0.025 default (beyond the audit's named 2) -- QUEUED for NATE. P5-P8
> remain staged for their per-engine waves.

- **P1 -- Mechanism + sweep guard (S). [LANDED]** Add `consequence` to `SyntheticInput`
  (contracts). Teach `gate_input_review` to REFUSE in auto mode when an entry is
  `consequence="physics"` + `basis="default_demo"` (typed
  `*_PHYSICS_INPUT_REQUIRED` error carrying the param name + the need), while
  passing scenario/numerical/aoi. Add the standard typed-error helper the
  templates raise. **Sweep guard** (design below) lands here so the next waves
  are enforced, not hoped.
- **P2 -- MODFLOW exemplar (M). [LANDED, ADR 0285]** aquifer K/porosity, vadose
  hydraulics, thermal, SFR gradient (#1-#7). Highest leverage: the SoilGrids
  pedotransfer seam already exists in capture_zone -- generalized to the archetype
  family (the shared `_aquifer_resolve` seam) as the DEFAULT, refuse when it cannot
  resolve. The demo constants (`DEFAULT_AQUIFER_K_MS`/`DEFAULT_POROSITY`) are
  deleted; `aquifer_k_ms`/`porosity` are REQUIRED on `MODFLOWRunArgs`. Proven by
  the live Woburn TCE A/B (undeclared K + SoilGrids unavailable -> typed refuse;
  SoilGrids available -> derived K=9.1e-6 m/s vs the dead 1e-4). Texture is read as
  the AOI-window valid-cell mean (robust to a nodata centroid). Rows 3/4/5/7
  refuse the un-derivable physics (Brooks-Corey, ambient temp + grain conductivity,
  DEM streambed, regional gradient) while scenario forcings proceed.
- **P3 -- Groundwater + soil material props (M). [LANDED, ADR 0285 P3 + P3-completion]**
  landlab susceptibility/groundwater/green_ampt/channel_incision (#8-#11), swmm
  aquifer_baseflow (#27). The P3 wave built the substrate (`aquifer_resolve` hoist +
  `derive_soil_column`); P3-completion wired every template to derive-or-refuse
  through it (`derive_soil_scalars` / `soil_derived_entry` / `literature_offer_entry`),
  deleted the 10 physics demo constants, and proved it with the live Ames-IA
  aquifer-column A/B. Row 11 K_sp refuses (calibration coefficient); recharge stays
  scenario (a precip-fraction would invent the fraction).
- **P4 -- Roughness / Manning (S-M). [LANDED, ADR 0285 P4]** geoclaw manning (#16),
  swmm overland_manning (#23) via the shared `roughness_resolve` seam (area-weighted
  NLCD Manning's n from `manning_mapping.csv` -> derive or refuse). #24's
  "curve_number=90" was an audit MISREAD (a demo rainfall depth; network_import uses
  Horton, not SCS-CN) -- no CN wiring is honest, QUEUED for a label-only pass.
- **P5 -- Coastal/hydro forcing + boundaries (M-L). [LANDED, ADR 0285 P5,
  AFK-conservative]** Per the conservative rule (wire what EXISTS first; build a
  new fetcher only where small+unambiguous; else refuse+queue). **row 19** WIRED:
  schism baroclinic river_discharge -> the new shared `discharge_resolve` seam
  derives the dominant NWM reach over the AOI (`fetch_noaa_nwm_streamflow`,
  `basis="derived"`) or REFUSES; the 500 demo constant DELETED; live A/B.
  **row 20** ocean_salinity -> literature offer (33-35 psu, refuse-in-auto), WOA
  fetcher QUEUED (no ocean-salinity fetcher exists). **row 22** synthetic bathy
  VERIFIED COMPLIANT (2 findings, no change): pahm_surge already hard-refuses
  unless `allow_synthetic_domain=True`; coupled_waves = an AUDIT MISREAD (canonical
  DUCK94 FRF validation mesh + real observed spectra, no synthetic terrain).
  **row 21** tidal (STAGE: existing `fetch_noaa_coops_tides` but deep M2-boundary
  deck surgery -> P6). **row 28** wind (DESIGN FORK -> NATE: gridMET gives ambient,
  not the storm the param means; no time window; recommend re-tag scenario).
  **row 29** agitation waves (QUEUE NDBC fetcher -- new-surface build). **row 30**
  thermocline (QUEUE: no US lake-profile fetcher; do NOT build). **row 33** datum
  (STAGE: NOAA CO-OPS datums = a new small fetcher). See the ADR P5 QUEUED-FOR-NATE
  list. The bulk new-fetcher surface (WOA, NDBC, CO-OPS datums, Vs30, lake profiles)
  is deferred to dedicated small builds per the conservative rule.
- **P6 -- Seismic site response (M).** openquake vs30 (#18). Needs a Vs30 fetcher
  (USGS Vs30 service / topographic-slope Vs30) OR refuse; no existing seam.
- **P7 -- Un-fetchable engineering + demo geometry (S).** hecras culvert MISLABEL
  fix (#12: relabel derived->default_demo, then refuse), flood_2d peak mislabel
  (#13), the Muncie demo-geometry gate (#14, = fallback-audit row 27), levee
  breach params (#15), swmm inlet/junction engineering (#24 partial, #26). These
  have no real source -> user-gated literature/engineering offers + the
  demo-geometry explicit opt-in.
- **P8 -- Label the SILENT + scenario-surface (S).** do_sag BOD (#32), elmfire fire
  weather (#34), storm climatology, swan physics coefficients: at minimum add the
  provenance entry (they ride unlabeled today) tagged `consequence="scenario"` or
  a documented-default so they surface without refusing. Resolves the borderline
  flags per NATE's per-row call.

### Sweep-guard test design (so a new invented default cannot be born)

Two enforcing tests, mirroring the honesty-floor lint pattern:

1. **Static lint (grep-shaped, in `tests/`).** Every `SyntheticInput(... basis=
   "default_demo" ...)` site in `trid3nt_server/workflows/` must carry an explicit
   `consequence=` kwarg. A physics-consequence entry with no real-source
   conversion must be reachable only through a gate that can refuse in auto (the
   template imports/raises the `*_PHYSICS_INPUT_REQUIRED` helper). A new
   `default_demo` entry with no `consequence` tag FAILS the test -- the next
   hidden default fails CI instead of shipping.
2. **Behavioral test (per-template, offline).** For each registered template,
   construct its resolved review entries under `input_mode="auto"` with NO
   user-supplied physics inputs and assert that any `consequence="physics"` +
   `basis="default_demo"` entry causes `gate_input_review` to return
   `proceed=False` (refuse), NOT proceed. Scenario/numerical/aoi demo defaults
   must still proceed. This pins the blast-radius behavior template-by-template
   and catches a regression that flips a physics default back to silent-proceed.

Additional guard: a contracts test that `SyntheticInput` requires `consequence`
for `basis="default_demo"` (schema-level), so the omission cannot even construct.

## Cross-references

- `fallback-ladders.md` -- the refuse-by-default doctrine this operationalizes for
  defaults (rule 4: synthetic rungs' labeled default is refuse). The consequence
  tag here is the parameter-default analogue of a ladder's synthetic rung.
- `fallback-audit.md` -- runtime-fallback scope; overlaps at row 16 (swmm
  synthetic network), row 17 (raster_cell_mesh roughness/imperviousness demo
  defaults, SILENT -- a mesh-layer sibling of #23), row 19 (SFR streambed gradient
  = #5), row 27 (Muncie demo geometry = #14). Those rows are runtime fallbacks of
  the SAME invented values; the conversions should land together.
</content>

## CLOSING STATUS -- the ladder is complete (2026-08-18, ADR 0285 P1-P8)

All 34 invented-physics rows are dispositioned. The mechanism (P1: the
`consequence` tag + refuse-in-auto + the 3-layer sweep guard) plus the eight
conversion waves close law 9 for the whole swept surface: no physics-consequential
value with no real source runs on an invention in auto -- it derives, refuses, or
(for un-fetchable engineering) waits for explicit approval; scenario/numerical/aoi
and documented-canonical values proceed labeled.

### Disposition of the 34 rows

**WIRED -- derive-from-real-data-or-REFUSE (15 rows).** A real fetcher serves the
value; an under-specified run derives it from the AOI or refuses when the source
cannot serve.
- MODFLOW aquifer K/porosity/vadose/thermal/SFR/gradient #1-#7 (SoilGrids
  pedotransfer, P2).
- Landlab soil strength/groundwater/green_ampt/channel_incision #8-#11 (SoilGrids
  texture + literature offers, P3-completion).
- GeoClaw storm_surge manning #16 + SWMM urban_flood overland_manning #23
  (NLCD area-weighted Manning, P4).
- SCHISM baroclinic river_discharge #19 (NWM dominant reach, P5).
- SWMM aquifer_baseflow soil column #27 (SoilGrids two-zone column, P3-completion).

**REFUSE + user-gated engineering/literature offer (10 rows).** No real source (or
a QUEUED one); refuses in auto naming the need, approvable in user_gated.
- Permanent (un-fetchable engineering): hecras culvert #12, levee breach_params
  #15, swmm dual_drainage inlet_capture #26, swmm network junction/inverts #24.
- Until a QUEUED source lands: hecras flood_2d peak #13 (USGS peak-flow regression),
  openquake vs30 #18 (USGS Vs30), schism ocean_salinity #20 (WOA), telemac agitation
  waves #29 (NDBC), telemac thermocline #30 (lake profiles), telemac datum #33
  (CO-OPS datums).

**GATED / opt-in / verified-compliant (5 rows).** Consent is explicit, or the value
is already derived from real data.
- hecras Muncie geometry #14 -> `run_demo_geometry` explicit opt-in (P7).
- schism synthetic bathymetry #22 -> `allow_synthetic_domain` opt-in, VERIFIED
  compliant (P5; coupled_waves = a misread, canonical DUCK94).
- swmm synthesized drainage_network #25 -> labeled screening model-choice.
- telemac river_dye bank geometry #31 -> already user-gated (fallback-audit row 23).
- openquake secondary_perils Vs30 (part of #18) -> DEM-slope-derived (Wald-Allen),
  compliant, no change (P6 law-6 correction).

**LABELED -- surface without refusing (P8, scenario/numerical documented-default).**
- elmfire fire weather #34, telemac do_sag WQ #32, swmm network n_imperv/n_perv/
  Horton constants (part of #24), SWAN wave-physics coefficients, geoclaw default
  fault/magnitude #17 + the storm-climatology borderline flags -> all scenario/
  numerical documented labels, no refuse (a fire-weather regime / BOD load / storm
  is the user's QUESTION; SWAN/Horton constants are literature-canonical).

**DESIGN FORK -> NATE (1 row).** telemac wave_field wind #28 -- gridMET gives ambient
not the storm the param means; recommend re-tag scenario (see ADR P5).

### The consolidated NATE queue (refusals -> derivations when these land)

USGS Vs30 (#18) - USGS peak-flow regression (#13) - RAWS/gridMET/HRRR fire weather
(#34) - fetch_usgs_water_quality temp + O'Connor-Dobbins k2 (#32) - World Ocean
Atlas salinity (#20) - NOAA CO-OPS tidal-constituent boundary (#21) + datums (#33) -
NDBC buoy obs (#28/#29) - lake temperature profiles (#30) - wave_field wind fork
(#28) - river_dye/discharge_resolve convergence - NLDI upstream-navigation inflow
refinement (#19 fidelity). Un-fetchable engineering (#12/#15/#24/#26) stays
user-gated permanently -- the honest law-9 endpoint.

### Counts

34 rows: **15 WIRED** (derive-or-refuse via a real fetcher) - **10 REFUSE**
(4 permanent engineering + 6 until a queued source) - **5 GATED/compliant** -
**~6 LABELED** (P8, no refuse; some rows span categories) - **1 FORK -> NATE**.
Nothing was deleted in P6-P8 (relabels + gate-wirings + one opt-in + provenance
labels; the demo constants that DIED were deleted in P2-P4, see the ledger).
