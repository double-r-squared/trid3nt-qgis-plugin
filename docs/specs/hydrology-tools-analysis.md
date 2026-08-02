# Python-Hydrology-Tools analysis (Collenteur curated list vs our live stack)

Read-only research analysis. Branch `refactor/engine-doors`. No code/infra changed;
this doc is the only artifact. Every maintenance claim is dated + cited (PyPI/GitHub,
checked 2026-07-29).

Source list: `raoulcollenteur/Python-Hydrology-Tools`
(https://github.com/raoulcollenteur/Python-Hydrology-Tools) - 97 packages across
Catchment Hydrology, Groundwater, Meteorological, Unsaturated Zone, Evaluation,
Miscellaneous, Data Collection, Geospatial, Optimization/Uncertainty, Statistics,
and a Legacy section.

## Ground-truth anchors (what we already run)

- **Data-router fold** (`docs/specs/data-router-fold.md`, `fetcher-fold-audit.md`):
  97 fetchers / 71,836 lines folding to YAML source specs + a shared router with
  pluggable ingestion executors. A router executor MAY delegate to a maintained
  client library instead of raw HTTP - this is exactly the FOLD lens below.
- **Calibration / V&V lane** (`docs/validation/tool-list.md`, `build-contract.md`):
  `compute_skill_metrics` wraps `spotpy.objectivefunctions` (NSE/KGE/PBIAS/RMSE
  native); `read_run_diagnostics`; setters use hydromt-sfincs `setup_*`,
  swmm-api/PySWMM, flopy; `run_pest_calibration` uses pyEMU/PstFrom (FROZEN loop).
- **Engine set** (already have doors): SFINCS, MODFLOW (flopy), SWMM (swmm-api),
  TELEMAC, GeoClaw, SWAN, Landlab, OpenQuake, ELMFIRE, Pelicun. All event/scenario
  scale; there is **no continuous rainfall-runoff / land-surface engine**.
- **Norms that gate cost class**: "analysis is playground, not tools" (composed
  analyses live in `code_exec`, atomic tools = DATA fetchers + irreducible
  primitives only); "template growth" (mine published examples -> engine doors);
  "data-source fallback"; "BMI = highest leverage" (engine-drivability ranking);
  ras-commander / HEC-RAS interop queued in `docs/IDEAS.md`.

## Cost classes used

- **router-source** - a YAML source spec (+ optionally one new router executor that
  delegates to the lib) in the data-router fold. Marginal cost per source is near
  zero once the executor exists. This is the FOLD landing form.
- **dep-only** - add the lib to `venvs/agent`; consume it inside the `code_exec`
  playground (per "analysis is playground") or inside an existing tool's internals.
  No new registered tool surface.
- **tool** - a new registered atomic primitive (data fetcher or irreducible
  compute primitive).
- **template** - a new engine door / workflow template (deck recipe + knob manifest
  + corpus), per the template-growth pattern.

## Verdict counts

| verdict | count | notes |
|---|---|---|
| FOLD | 1 | `dataretrieval` (official USGS) - the only lib on the list that cleanly REPLACES existing hand-written plumbing |
| INTEGRATION | 18 | new capability mapping to a named lane; priority varies (see table) |
| SKIP | 78 | redundant with what we run, unmaintained, or out of product scope |
| **total** | **97** | |

**Headline finding:** this list is **model-heavy and data-client-thin**. The
Data Collection section is only 8 packages and most are unmaintained (`ulmo` 2021,
`ecohydrolib`), stale (`hydrofunctions` 2022), or regional (`hydropandas`,
`openradar`, `hkvfewspy` = Dutch/Delft-FEWS). So the fold surface is **exactly one
slam-dunk** (`dataretrieval`). The real payoff is **INTEGRATIONS**: maintained
models + metric libraries that fill named gaps (continuous hydrology, groundwater
time-series, extended skill metrics, PET, watershed delineation, BMI coupling).

---

## FOLD

### dataretrieval - FOLD, cost class: router-source. **Top fold.**

- **What it does:** official USGS Python package; retrieves NWIS (daily/instantaneous
  values, site info, groundwater levels), Water Quality Portal (WQP), and STATs from
  U.S. federal hydrologic web services, returning pandas DataFrames. Handles the RDB
  tab-delimited and WaterML parsing internally.
  Repo: https://github.com/DOI-USGS/dataretrieval-python (USGS-python/dataretrieval).
- **Maintenance:** EXCELLENT. Latest **1.2.0, released 2026-06-24** (PyPI); authored
  by Timothy Hodson (USGS), maintained by USGS staff (thodson-usgs, jhariharan,
  E. Hinman); multiple releases through 2025-2026. Official agency backing.
- **What it replaces:** the USGS fetcher family's bespoke parsers -
  `fetch_usgs_nwis_gauges` (BESPOKE: `_parse_site_rdb` + `_parse_iv_json_window` +
  `_build_window_flatgeobuf`, ~366 foldable lines), `fetch_usgs_groundwater_levels`
  (SPEC: 2 OGC endpoints left-joined, ~741 lines), `fetch_usgs_water_quality`
  (HYBRID: WQP result-CSV parse + station join, ~541 lines). A single "dataretrieval
  executor" backs all three via source specs; the RDB/WaterML/CSV parsing we hand-roll
  moves upstream to the maintained lib.
- **Why it's more than cleanup (the maintenance argument):** USGS is transitioning the
  legacy NWIS web services (waterservices.usgs.gov RDB/WaterML) to the new Water Data
  OGC API (`api.waterdata.usgs.gov`); `dataretrieval` is the official library tracking
  that migration. Our hand-rolled RDB parser (`_parse_site_rdb`) is on a decommission
  clock - delegating to the official lib is decay-avoidance, not just line-count
  reduction. (The audit already shows `fetch_usgs_groundwater_levels` on the new OGC
  endpoints, so the codebase is partway there.)
- **Scope caveat (honesty):** covers NWIS + WQP confidently. It does NOT cover USGS STN
  high-water marks (`fetch_high_water_marks`) - that stays bespoke. NLDI coverage in
  newer `dataretrieval` should be verified before folding `fetch_nhdplus_nldi_navigate`.
- **Landing:** one router executor + 3 source specs during the USGS-family fan-out
  (data-router-fold Phase 2). Passes the replication gate by DataFrame->FGB parity vs
  the current envelopes.

---

## INTEGRATION

Ordered by priority. Lane / what-it-adds / cost class per row; evidence below.

| # | package | lane | adds | cost class | priority |
|---|---|---|---|---|---|
| 1 | pywatershed | NEW ENGINE (continuous hydrology) | rainfall-runoff hydrographs our event engines lack; USGS PRMS/GSFLOW, BMI | template | HIGH |
| 2 | pastas | calibration + groundwater/MODFLOW | data-driven GW head time-series models (stress decomposition, gap-fill) | dep-only (+ opt template) | HIGH |
| 3 | HydroErr | calibration / V&V | 70+ goodness-of-fit metrics (long tail past spotpy's ~4) | dep-only | MED |
| 4 | Hydrostats | calibration / V&V | metric-comparison + hydro viz around HydroErr | dep-only | MED |
| 5 | pyet | hydrology forcing / water balance | reference+potential ET (18 methods) from met inputs | dep-only | MED |
| 6 | pysheds | terrain / hydrology processing | watershed + flow-accumulation delineation (a real gap) | dep-only / tool | MED |
| 7 | pydsstools | HEC interop | HEC-DSS read/write (enables the ras-commander / HEC track) | dep-only | MED |
| 8 | PyMT | engine interop / doors | BMI model-coupling framework (BMI = highest-leverage note) | dep-only | MED |
| 9 | xskillscore | gridded/ensemble skill | xarray-native forecast verification (raster/ensemble) | dep-only | MED |
| 10 | climate-indices | climate / drought | compute SPI/SPEI/PDSI/PET from our climate fetchers | dep-only | MED |
| 11 | TimML / TTim | groundwater (lightweight) | analytic-element GW (well drawdown, capture zones) no meshing | template | MED |
| 12 | traval | obs QC / calibration | automatic timeseries error detection before pairing | dep-only | LOW |
| 13 | NeuralHydrology | continuous hydrology (ML) | LSTM streamflow prediction | template / dep-only | LOW |
| 14 | wetterdienst | weather stations (intl) | maintained multi-provider client (DWD/NOAA/ECCC/...) | router-source | LOW |
| 15 | PyGeoprocessing | terrain / hydrology processing | D8 routing + watershed (NatCap/InVEST); pysheds alternative | dep-only | LOW |
| 16 | SPEI | climate / drought | lighter SPI/SPEI/SGI (pandas); climate-indices overlaps | dep-only | LOW |
| 17 | MetPy | met calculations | Unidata met-calc primitives in the playground | dep-only | LOW |
| 18 | pysteps | precip nowcasting | radar-extrapolation ensemble QPF forcing | dep-only / template | LOW |

### 1. pywatershed - NEW continuous-hydrology engine. **Top integration.**
- **What:** USGS Python package modernizing PRMS (Precipitation-Runoff Modeling
  System) and GSFLOW - a continuous, spatially distributed rainfall-runoff / national
  hydrologic model; BMI-oriented.
  Repo: https://github.com/EC-USGS/pywatershed.
- **Maintenance:** EXCELLENT. Latest **3.0.0, released 2026-07-13** (PyPI); USGS
  Dev & Standards Team (McCreight, Bonelli); Production/Stable, CI + docs.
- **Adds / fills the named gap:** our engines are all event/scenario scale
  (SFINCS/SWMM flood, MODFLOW steady/transient GW). We currently **fetch** streamflow
  (`fetch_noaa_nwm_streamflow`, `fetch_usgs_nwis_gauges`) as forcing; pywatershed can
  **generate** continuous hydrographs from precip/temp - the continuous-water-balance
  door the fidelity ladder lacks, and it feeds SFINCS/SWMM boundary forcing natively.
  USGS-official de-risks it; BMI readiness couples to the "BMI = highest leverage"
  note and the engine-door refactor.
- **Cost class:** template (new engine door). Follows the exact template-growth
  pattern in IDEAS.md (mine PRMS/NHM examples -> `workflows/pywatershed/<template>/`).

### 2. pastas - groundwater time-series analysis. **Top integration (calibration+GW).**
- **What:** open-source framework for analysis of groundwater head time series using
  transfer-function-noise (TFN) models: decompose a head series into responses to
  stresses (recharge, pumping, river stage), gap-fill, detect trends/anomalies,
  optimize + validate.
  Repo: https://github.com/pastas/pastas.
- **Maintenance:** EXCELLENT. Latest **1.14.0, released 2026-03-26** (PyPI); this is
  the list author's flagship (R.A. Collenteur), MIT, regular 2025-2026 cadence.
- **Adds:** a data-driven complement to MODFLOW's physically-based heads - fits the
  calibration lane (feeds `compute_model_residuals` / the `variable="head"` skill
  preset) and the groundwater track. Turns a raw well hydrograph into a validated
  model with quantified stress contributions; strong for "which stress drives this
  well" and recharge estimation.
- **Cost class:** dep-only (playground) primarily - it IS composed analysis, so it
  belongs in `code_exec` per the norm; optionally a "GW time-series analysis"
  template later. Pairs with `traval` (QC) + `hydropandas`/dataretrieval (ingest).

### 3-4. HydroErr + Hydrostats - extended skill metrics. (spotpy: COMPLEMENT, not redundant.)
- **What:** HydroErr = 70+ goodness-of-fit metrics for hydrologic model comparison;
  Hydrostats = comparison workflows + hydro visualization built on HydroErr. BYU
  Hydroinformatics. Repos: https://github.com/BYU-Hydroinformatics/HydroErr,
  https://github.com/BYU-Hydroinformatics/Hydrostats.
- **Maintenance:** REVIVED (see Biggest Surprise). Dormant 2018-2019, then major
  releases **HydroErr 2.0.0 (2025-12-09)** and **Hydrostats 1.0.0 (2025-12-22)**.
  Freshly active, but verify the cadence holds beyond the one revival burst.
- **spotpy comparison (the task's explicit question):** COMPLEMENT. spotpy provides
  ~4 native objective functions (NSE/KGE/PBIAS/RMSE) - exactly the Moriasi core the
  build-contract committed `compute_skill_metrics` to. HydroErr adds ~70 metrics
  (volumetric efficiency, H-metrics, seasonal/threshold variants). NOT redundant, but
  the marginal value only lands when the calibration lane needs metrics beyond the
  committed core - which it currently does not. So keep spotpy for the wave; add
  HydroErr as an optional metric-library dep to `compute_skill_metrics` when the long
  tail is demanded (do NOT rip out spotpy).
- **Cost class:** dep-only (extends an existing tool's internals).

### 5. pyet - reference / potential evapotranspiration.
- **What:** 18 PET/ET0 methods (Penman-Monteith, Hargreaves, Priestley-Taylor, ...)
  from met inputs; pandas Series AND xarray DataArray (1-D and gridded).
  Repo: https://github.com/phydrus/pyet.
- **Maintenance:** EXCELLENT. Latest **1.5.0, released 2026-05-26** (PyPI); Vremec +
  Collenteur; Production/Stable, benchmarked against literature.
- **Adds:** we fetch met grids (gridMET, ERA5, HRRR) but compute no ET. pyet closes
  the water-balance loop for continuous hydrology (feeds pywatershed forcing) and any
  ET-dependent calc; xarray path fits our raster stack.
- **Cost class:** dep-only (a playground PET primitive).

### 6. pysheds - watershed delineation + flow accumulation.
- **What:** fast, pure-Python watershed/catchment delineation, flow direction (D8),
  flow accumulation, stream extraction from a DEM.
  Repo: https://github.com/mdbartos/pysheds.
- **Maintenance:** GOOD. Latest **0.5, released 2025-08-14** (PyPI); Matt Bartos
  (U. Michigan); Beta but current.
- **Adds / gap:** we have `fetch_dem` + `compute_contours` + GDAL, but no catchment
  delineation - which SFINCS domain definition and SWMM subcatchment derivation want.
  pysheds is the lightest option (vs PyGeoprocessing #15, Lidar). Also relevant to the
  `check_lidar_artifacts` gap (DEM hydro-conditioning / depression filling) noted in
  `tool-list.md` line 138.
- **Cost class:** dep-only (playground) OR a `compute_watershed` irreducible primitive
  (sibling of `compute_contours`) - borderline under "analysis is playground"; delineation
  is arguably irreducible enough to be a tool.

### 7. pydsstools - HEC-DSS read/write.
- **What:** Cython lib to read/write HEC-DSS database files (the HEC-RAS/HEC-HMS
  time-series + grid store). Repo: https://github.com/gyanz/pydsstools.
- **Maintenance:** GOOD. Latest **3.1.0, released 2026-06-06** (PyPI); consistent
  2025-2026 cadence.
- **Adds:** the enabling dependency for the HEC track queued in IDEAS.md
  (ras-commander MCP study, HEC-RAS surface). Any HEC interop needs DSS I/O; hand-rolling
  the binary DSS format is exactly the plumbing to avoid.
- **Cost class:** dep-only (consumed by a future HEC template/bridge).

### 8. PyMT - BMI model coupling.
- **What:** CSDMS framework for coupling models that expose the Basic Model Interface
  (BMI). Repo: https://github.com/csdms/pymt.
- **Maintenance:** MODERATE. Latest **1.3.1, released 2024-06-18** (GitHub); CSDMS
  institutional backing but slower cadence than the leaders here - flag before betting
  architecture on it.
- **Adds:** standardized BMI coupling - directly the "BMI = highest leverage" lever
  and relevant to the engine-door refactor (pywatershed and Landlab are BMI-capable).
  Even used only as the BMI reference (not a hard dep), it informs the door interface.
- **Cost class:** dep-only / architectural reference.

### 9. xskillscore - gridded / ensemble verification.
- **What:** xarray-native deterministic + probabilistic forecast-verification metrics.
  Repo: https://github.com/xarray-contrib/xskillscore.
- **Maintenance:** EXCELLENT. Latest **0.0.29, released 2026-02-18** (PyPI);
  xarray-contrib org, active.
- **Adds:** the gridded/ensemble counterpart to HydroErr's point/timeseries metrics -
  fits `compute_flood_extent_skill` (raster wet/dry confusion) and any ensemble/forecast
  scoring, on our native xarray/raster stack.
- **Cost class:** dep-only.

### 10. climate-indices - drought / climate indices.
- **What:** reference implementations of SPI, SPEI, PET, PNP, PCI, PDSI for drought
  monitoring. Repo: https://github.com/monocongo/climate_indices.
- **Maintenance:** GOOD. Latest **2.4.0, released 2026-04-06** (PyPI); active, Py 3.10-3.13.
- **Adds:** we FETCH the U.S. Drought Monitor (`fetch_us_drought_monitor`) but compute
  no indices; climate-indices turns our precip/temp fetchers into SPI/SPEI/PDSI fields.
  More comprehensive than SPEI (#16) - prefer this one; it overlaps/supersedes the
  bitbucket `PySDI`.
- **Cost class:** dep-only (playground).

### 11. TimML / TTim - analytic-element groundwater (lightweight).
- **What:** multi-layer analytic element groundwater models - steady (TimML) and
  transient (TTim). No grid/mesh; instant analytic solutions for wells, drawdown,
  capture zones. Repos: https://github.com/mbakker7/timml, .../ttim.
- **Maintenance:** GOOD. TimML **6.9.0, released 2026-01-28** (GitHub); Mark Bakker
  (TU Delft), active.
- **Adds:** a lightweight groundwater door complementing MODFLOW - answers well-drawdown
  / well-interference / capture-zone questions without a full MODFLOW deck build+solve.
  Good fit for the knob-cost-class idea (analytic = seconds vs MODFLOW rebuild).
- **Cost class:** template (a small analytic-GW engine door).

### 12. traval - automatic timeseries error detection.
- **What:** configurable error-detection + correction rules for (groundwater) time
  series QC. Repo: https://github.com/ArtesiaWater/traval.
- **Maintenance:** GOOD. Latest **0.5.4, released 2026-01-27** (GitHub); Artesia, active.
- **Adds:** QC of observation series before `extract_model_at_observations` pairing -
  screens spikes/flatlines/outliers so skill metrics are not poisoned by bad obs.
  Pairs with pastas.
- **Cost class:** dep-only.

### 13. NeuralHydrology - LSTM rainfall-runoff (ML streamflow).
- **What:** deep-learning library (LSTM etc.) for hydrologic time-series / streamflow
  prediction. Repo: https://github.com/neuralhydrology/neuralhydrology.
- **Maintenance:** GOOD. Latest **1.13.0, released 2026-01-14** (GitHub); Kratzert,
  active, community traction.
- **Adds:** an ML alternative for the same continuous-streamflow gap pywatershed fills;
  research-flavored + heavy (torch, training). Lower priority than pywatershed for the
  same lane, but the strongest ML option on the list.
- **Cost class:** template / dep-only.

### 14. wetterdienst - multi-provider weather-station client.
- **What:** maintained Python client for many weather services - DWD, NOAA, ECCC,
  AEMET, DMI, SMHI, KNMI, Meteo France, MeteoSwiss.
  Repo: https://github.com/earthobservations/wetterdienst.
- **Maintenance:** EXCELLENT. Latest **0.129.0, released 2026-07-27** (PyPI); active,
  but pre-1.0 with expected breaking changes (pin the version).
- **Adds:** international weather-station coverage (our weather fetchers are US-centric:
  IEM ASOS/RAWS, AirNow). Could back a new station-weather router source. It does NOT
  replace the US mesonet fetchers (IEM is the US source of record), so this is additive
  coverage, not a fold of existing code.
- **Cost class:** router-source (a new source spec, pin the version).

### 15. PyGeoprocessing - hydrological GIS ops.
- **What:** raster/vector/routing GIS incl. D8/MFD flow routing + watershed delineation,
  memory-efficient. NatCap (InVEST engine). Repo: https://github.com/natcap/pygeoprocessing.
- **Maintenance:** EXCELLENT. Latest **2.4.x, 2026** (PyPI, v2.4.10 2026-01-14);
  BSD-3, active. Adds routing/delineation like pysheds but heavier + battle-tested at
  scale. Pick pysheds for lightness, PyGeoprocessing if we need InVEST-grade routing.
- **Cost class:** dep-only.

### 16. SPEI - drought indices (lighter).
- **What:** SPI/SPEI/SGI drought indices on pandas Series (SciPy).
  Repo: https://github.com/martinvonk/SPEI.
- **Maintenance:** GOOD. Latest **0.8.2, released 2026-01-30** (PyPI); Martin Vonk
  (Pastas ecosystem). Overlaps climate-indices (#10); use only if the lighter pandas
  API is preferred. Cost class: dep-only.

### 17. MetPy - meteorological calculations.
- **What:** Unidata's broad met-calc + data-reading toolkit (thermodynamics, wind,
  soundings). Repo: https://github.com/Unidata/MetPy. Maintenance: EXCELLENT (Unidata,
  continuous). Adds playground met primitives (derived quantities from HRRR/ERA5).
  Cost class: dep-only. Low priority - narrow need today.

### 18. pysteps - precipitation nowcasting.
- **What:** community framework for radar-based precipitation nowcasting (extrapolation
  + ensemble QPF). Repo: https://github.com/pySTEPS/pysteps. Maintenance: MODERATE,
  latest **1.21.2, 2024-07-09** (GitHub) - slower cadence. Adds short-term precip
  nowcast as flood forcing (a real but specialized capability). Cost class: dep-only /
  template. Low priority.

---

## SKIP (grouped, one line each)

**Already in our stack (redundant):**
- FloPy (https://github.com/modflowpy/flopy) - we use it as the MODFLOW core.
- SPOTpy (https://github.com/thouska/spotpy) - `compute_skill_metrics` + `run_spotpy_calibration`.
- Pyemu (https://github.com/jtwhite79/pyemu) - planned `run_pest_calibration` / `set_modflow_parameters`.
- Landlab (https://github.com/landlab/landlab) - existing `run_landlab` engine door.

**Unmaintained / stale / superseded (a dead lib is worse than our code):**
- Ulmo (https://github.com/ulmo-dev/ulmo) - last release 0.8.8, **2021-09-02**; inactive. dataretrieval supersedes.
- Hydrofunctions (https://github.com/mroberge/hydrofunctions) - v0.2.4, **2022-06-14**; USGS NWIS only, stale; dataretrieval (official, broader) supersedes.
- PyHIS (https://pypi.org/project/pyhis/) - CUAHSI-HIS, deprecated (was superseded by ulmo, itself dead).
- Ecohydrolib (https://github.com/selimnairb/EcohydroLib) - old workflow libs, unmaintained.
- HydPy (https://github.com/hydpy-dev/hydpy) - active (6.4.0, 2024-06) but a full conceptual-model framework; pywatershed is the chosen continuous-hydrology pick.
- Legacy section (list-flagged): Catchmod, DRYP, EXP-HYDRO, HydroAnalysis, Hydropy, LHMP, LuKars, mhmpy, PyEto, PyGLUE, PyStream, PyTOPKAPI, RRMPG, wflow (moved to Julia/Wflow.jl), xsboringen - all legacy/unmaintained.

**Regional / national scope (not our US-first product):**
- HydroPandas (Dutch: KNMI/DINO/BRO/Lizard) - v0.18.1 2025-02; its US sources are covered by dataretrieval.
- NLmod (Netherlands MODFLOW), HKVFEWSPY (Delft-FEWS), Openradar (KNMI radar), Wetterdienst is the multi-region exception (kept as INTEGRATION #14).

**Out of product scope (water allocation / reservoir ops / vadose / niche analysis):**
- PYWR, iRONS (reservoir/allocation ops); Shyft (energy market); SPHY, Xanthos, VIC, SUMMA, wrfhydropy, CMF, SuperflexPy, SMARTPy, HydroGR (full RR/land-surface models - pywatershed covers the gap); river-route (NWM already gives us routed streamflow); PyHSPF (HSPF legacy); pyorc (video image-velocimetry - no video pipeline); Wetland/lidar (giswqs) - we have `fetch_nwi_wetlands` + imagery (lidar noted under pysheds for hydro-conditioning).
- Groundwater niche: Anaflow, WellTestPy (pumping-test analysis), PyHELP (recharge), gwrefpy, WellApplication, PyKasso, GeoArchPy, Gravi4GW, Timflow (TimML/TTim chosen for AEM), imod-python (Deltares, active 1.0.0.post1 2025-11 - but redundant with flopy at our scale; note as an option for very large regional structured MODFLOW grids).
- Unsaturated zone: pedon, Phydrus (HYDRUS-1D), pySWAP, Pytesmo, VS2DPY - vadose/soil-moisture niche, out of scope now.
- Meteorological niche: Evaporation, MELODIST, MetSim (forcing disaggregation - not a current need; pyet covers ET), Improver (Met Office operational, heavy), pyfao56 (FAO-56 crop ET, ag-specific).
- Statistics niche: EFlowCalc (environmental-flow metrics), HydroLM (trivial linear regression), PySDI (redundant with climate-indices).
- Geospatial heavy/old: HPGL (geostatistics, old), PcRaster (heavy env-modeling framework).
- Frameworks/utilities redundant with our xarray/rioxarray stack: ESMPY (regridding), IRIS (SciTools cubes), htimeseries, Hydrobox, Hydrointerp, Mesas (StorAge selection), pywr already noted.
- eWaterCycle (https://github.com/eWaterCycle/ewatercycle) - a BMI-based computational-hydrology PLATFORM; a peer/competitor architecture, not a component to fold in. Note as a reference for the BMI/engine-door design, not an integration.

---

## Summary for NATE

**Top-5 FOLDs** (replace hand-written plumbing). Honest caveat: this list yields
exactly ONE clean fold; the rest are "fold-adjacent" = reduce FUTURE plumbing rather
than replace existing code.
1. **dataretrieval** (official USGS, 1.2.0 2026-06) - folds the USGS family
   (`fetch_usgs_nwis_gauges`, `_groundwater_levels`, `_water_quality`) into one router
   executor; also de-risks the NWIS->OGC decommission. **The only true fold.**
2. *(fold-adjacent)* **wetterdienst** - backs a new international weather-station router
   source instead of hand-rolling each provider (additive, not a replacement).
3. *(fold-adjacent)* **pydsstools** - avoids hand-rolling the HEC-DSS binary format when
   the HEC track lands.
4. *(fold-adjacent)* **HydroErr** - backs `compute_skill_metrics`' long-tail metric math
   (we already delegate the core to spotpy, so this is a swap, not a rescue).
5. *(fold-adjacent)* **imod-python** - xarray MODFLOW builder for very large regional
   grids (only if flopy's approach strains; otherwise SKIP).

**Top-5 INTEGRATIONs** (new capability -> named lane):
1. **pywatershed** (USGS, 3.0.0 2026-07) - the continuous rainfall-runoff engine our
   event-scale set lacks; generates the hydrographs we currently only fetch; BMI-ready.
   Lane: NEW ENGINE (template).
2. **pastas** (Collenteur, 1.14.0 2026-03) - data-driven groundwater head time-series
   models; complements MODFLOW + feeds the head-calibration lane. dep-only (playground).
3. **HydroErr + Hydrostats** (BYU, revived 2025-12) - extended skill metrics; COMPLEMENT
   spotpy (not redundant), add when the calibration lane needs metrics past the Moriasi
   core. dep-only.
4. **pyet** (Collenteur, 1.5.0 2026-05) - reference/potential ET from our met grids;
   closes the water-balance loop for continuous hydrology. dep-only.
5. **pysheds** (0.5 2025-08) - watershed / flow-accumulation delineation, a genuine gap
   for SFINCS domains + SWMM subcatchments, and touches the `check_lidar_artifacts` gap.
   dep-only or a `compute_watershed` primitive.
   (Runners-up: PyMT/BMI coupling, pydsstools/HEC, xskillscore/gridded skill,
   climate-indices/drought, TimML/analytic-GW.)

**Counts:** FOLD 1, INTEGRATION 18, SKIP 78 (of 97).

**Biggest surprise:** the list inverts the premise. It is model-and-analysis heavy,
data-client thin - so the FOLD payoff is a single package (`dataretrieval`), while the
real value is INTEGRATIONS that fill named gaps (continuous hydrology, GW time-series,
extended metrics, PET, delineation). Two secondary surprises: (a) HydroErr/Hydrostats,
which look abandoned (dormant 2018-2019), were **revived to major releases in Dec 2025**
- a stale-looking eval library is actually fresh, flipping its verdict from SKIP to
INTEGRATION; and (b) the strongest single find, `pywatershed`, is **USGS-official and
BMI-ready**, meaning the continuous-hydrology gap can be filled with agency-backed,
door-refactor-aligned code rather than a research one-off.
