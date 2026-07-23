# Hydrologic/Hydraulic Engine Calibration, Validation, and Automated-Review Research

Synthesis of four research briefs (SFINCS, SWMM, MODFLOW 6, cross-engine standards) into a single reference for TRID3NT's calibration and automated post-run review design. Every claim carries its source URL. ASCII hyphens only throughout.

---

## 1. Per-engine calibration practice

### 1.1 SFINCS (compound coastal/pluvial/fluvial flood)

**Parameters calibrated + typical ranges**
- Manning roughness (`manning`/`manning_land`/`manning_sea`/`manningfile`): defaults land = 0.04, sea = 0.02 s/m^(1/3); land/sea split by elevation via `rgh_lev_land`; may be a spatially varying raster (`manningfile`). One published compound-flood global framework used constant 0.03 (river) / 0.1 (land) s/m^(1/3) for consistency with CaMa-Flood. [parameters manual](https://sfincs.readthedocs.io/en/latest/parameters.html), [NHESS 2023](https://nhess.copernicus.org/articles/23/823/2023/)
- Infiltration: `qinf` (uniform constant mm/hr, default 0); Curve Number method A (`scsfile`, no recovery) and method B (`smaxfile`/`sefffile`/`ksfile`, with recovery), `sfacinf` = CN initial-abstraction factor (default 0.2); Green and Ampt (`sigmafile`/`psifile`/`ksfile`); Horton (`f0file`/`fcfile`/`kdfile`). SCS Curve Number was applied for Houston/Harvey (Sebastian et al. 2021). [parameters manual](https://sfincs.readthedocs.io/en/latest/parameters.html)
- Wind drag: piecewise-linear `cdwnd`/`cdval` breakpoints, default 3 breakpoints at 0/28/50 m/s with Cd = 0.001/0.0025/0.0015.
- Numerical/subgrid stability (tuned, not physically calibrated): `alpha` (CFL time-step reduction, default 0.5, range 0.1-0.75), `huthresh` (min flow depth, default 0.05 m), `hmin_cfl`, subgrid lookup tables (`sbgfile`) built via hydromt-sfincs `setup_subgrid`.

**Sensitivity note:** a published sensitivity study found skill relatively insensitive to river depth and land Manning roughness, but highly sensitive to pluvial/fluvial forcing magnitude, i.e. calibration effort is often better spent on forcing/boundary data than roughness tuning. [NHESS 2023](https://nhess.copernicus.org/articles/23/823/2023/)

**Observations used:** tide/storm-surge gauges (NOAA, 5 stations for Hurricane Michael, Mobile Bay); USGS gauges + high-water marks (Harvey/Houston: 21 USGS points + 115 HWMs, average error 0.73 m; Florence: 89 water-level gauges + 763 HWM locations); Sentinel-1 SAR flood extent (10 m; TC Idai, TC Eloise; 2025 global riverine study of 499 events). Extent classification threshold commonly set at simulated depth > 15 cm = "flooded." [NHESS 2023](https://nhess.copernicus.org/articles/23/823/2023/)

**Tooling/workflow:** hydromt-sfincs (Deltares Python plugin) automates model building (topo/bathy, roughness, infiltration, forcing) via CLI+YAML or Python API, explicitly designed so parameter maps/forcing "can easily be modified for sensitivity analysis or calibration" in an iterative build-run-inspect loop. This is a manual/iterative workflow, not automated optimization. `setup_subgrid` precomputes subgrid volume/roughness tables from high-res DEM so coarse computational grids keep fine-scale accuracy, the primary resolution-vs-cost lever alongside Manning/infiltration. [HydroMT-SFINCS docs](https://deltares.github.io/hydromt_sfincs/latest/)

**Cost/gap:** no primary-source PEST-style automated-optimization SFINCS calibration wrapper was found; automated SFINCS calibration is an open gap, not established practice.

### 1.2 SWMM (urban drainage / stormwater)

**Parameters calibrated + sensitivity ranking:** outputs most sensitive to imperviousness and impervious depression storage, least sensitive to overland Manning's n. [Automatic Calibration of SWMM, ASCE](https://ascelibrary.org/doi/10.1061/(ASCE)0733-9429(2008)134:4(466))
- Depression storage: typical starting points ~0.08 in impervious, ~0.2 in pervious. [swmm5.org manual](https://swmm5.org/2019/03/14/mm-storm-water-management-model-users-manual-version-5/)
- Manning's n overland: separate pervious/impervious required; impervious ~0.011-0.024 (smooth pavement), pervious ~0.05-0.8 (grass/turf/litter). [SWMM Ref Manual Vol 1 Hydrology](https://downloads.tuflow.com/SWMM/SWMM5_Reference_Manual_Volume1_Hydrology_P100NYRA.pdf)
- Manning's n conduit: pipe-material tables (concrete ~0.013, corrugated metal ~0.024) per Ref Manual Vol II Hydraulics.
- Horton infiltration: max rate f0, final rate fc, decay kd, drying time Td. Typical fc by NRCS hydrologic soil group (in/hr): A 0.30-1.5, B 0.15-2, C 0.05-0.25, D ~0.10 or less. [Innovyze Horton](https://help2.innovyze.com/infoworksicm/Content/HTML/ICM_ILCM/Horton_Infiltration_Model.htm), [openswmm Horton](https://www.openswmm.org/Topic/28737/how-can-i-find-the-max-and-min-infiltration-rate-for-horton-or-modified-horton-method)
- Green-Ampt: suction head, saturated hydraulic conductivity, initial moisture deficit, typically from SSURGO texture-class lookup then fine-tuned.
- RDII (RTK triangular unit hydrograph): three params per triangle -- R (fraction of rainfall to RDII), T (time to peak, hr), K (recession multiplier of T); commonly 3 stacked triangles (fast/delayed/groundwater): R1 T~0.5-2.0 K~1.0-2.0; R2 T~3-5 K~2.0-3.0; R3 T~10-15 K~3.0-7.0; calibrated by trial-and-error against wet-weather flow monitor hydrographs. [openswmm RTK](https://www.openswmm.org/Topic/4125/rtk-parameters-tri-triangular-method-rdii-modeling), [chijournal RTK](https://www.chijournal.org/R241-11)

**Observations used:** flow monitors at manholes/outfalls (primary target for dry- and wet-weather response); depth/level sensors at surcharge-prone nodes (hydraulic grade line, surcharge timing); post-event high-water marks; flooding-complaint / incident logs (qualitative validation of known problem locations).

**Tooling:** EPA SWMM Applications Manual (EPA/600/R-09/000, 2009), nine worked calibration examples. [chiwater PDF](https://www.chiwater.com/Files/Swmm_Apps_Manual.pdf) | PySWMM (JOSS 5(52):2292, 2020), step-wise engine control enabling automated calibration/RTC. [JOSS PDF](https://www.theoj.org/joss-papers/joss.02292/10.21105.joss.02292.pdf), [GitHub](https://github.com/pyswmm/pyswmm) | PySWMM+Pymoo multi-objective calibration. [MDPI](https://www.mdpi.com/2306-5338/12/6/129) | OSTRICH-SWMM (parallelized heuristic single/multi-objective) | SWMMCALPY (genetic-algorithm auto-calibration) | swmm_calibration harness on PySWMM. [GitHub](https://github.com/mmmatthew/swmm_calibration/blob/new-master/swmm_calibration/classes/optimizer.py) A named SPOTPY+SWMM published integration was not confirmed (unverified).

**Cost:** automated calibration is well-supported and Python-native (PySWMM in-process), the lowest-friction of the three engines for an automated loop.

### 1.3 MODFLOW 6 (groundwater flow + GWT transport)

**PEST/PEST++ ecosystem (USGS, open source), the industry standard.** PEST++ v5 ships four solvers plus MODFLOW 6:
- PESTPP-GLM: Gauss-Levenberg-Marquardt nonlinear regression + FOSM uncertainty; run cost scales with parameter count.
- PESTPP-IES: iterative ensemble smoother, history matching + uncertainty in one pass; run cost scales with number of realizations, not parameters, making it tractable for pilot-point-heavy models.
- PESTPP-SEN: global sensitivity (Morris/Sobol) to screen parameters up front.
- PESTPP-OPT / PESTPP-MOU: management optimization (less relevant to calibration).
[White et al. 2020, USGS TM 7-C26](https://pubs.usgs.gov/publication/tm7C26)

**pyEMU** is the Python glue: builds `.tpl`/`.ins` files, FOSM linear uncertainty and data-worth, ensemble generation, PEST++ post-processing. [docs](https://pyemu.readthedocs.io/en/latest/), [repo](https://github.com/pypest/pyemu) **Flopy integration:** `pyemu.utils.PstFrom` walks a flopy/MODFLOW 6 model and auto-generates the PEST interface; `gw_utils`/`pp_utils` provide head/flux/volume observation setup tied to `(kper, k)` pairs and pilot-point templates. [pp_utils](https://pyemu.readthedocs.io/en/develop/autoapi/pyemu/utils/pp_utils/index.html), [gw_utils](https://pyemu.readthedocs.io/en/develop/autoapi/pyemu/utils/gw_utils/index.html)

**Parameterization pattern (GMDSI Freyberg tutorials):** K fields via pilot points with geostatistical interpolation; recharge as per-zone/per-stress-period multipliers; storage (Ss/Sy) as regularized zone/pilot multipliers (weakly identifiable); boundary conductances (GHB/river/drain) as conductance multipliers. [GMDSI notebooks](https://github.com/gmdsi/GMDSI_notebooks), [tutorials](https://gmdsi.org/education/tutorials/)

**Observations used:** heads (static + transient hydrographs, dominant type); baseflow/spring discharge from stream-gain or drain/river fluxes; concentration/breakthrough curves for GWT transport. Historical UCODE/MODFLOWP practice formalized this as a weighted least-squares objective across all types. [Hill 1998, USGS WRIR 98-4005](https://water.usgs.gov/nrp/gwsoftware/modflow2000/WRIR98-4005.pdf)

**Run cost (PESTPP-IES):** 100-250 realizations x 2 iterations generally sufficient (swept against 10-2000 realizations to find diminishing returns); a local machine with 8-32 cores faces a few hundred to ~1000 MODFLOW 6 runs total, versus tens of thousands for a dense-Jacobian GLM with hundreds of pilot-point parameters. Corroborated by TM 7-C26 (IES cost is realization-driven). [GMDSI notebooks](https://github.com/gmdsi/GMDSI_notebooks), [White et al. 2020](https://pubs.usgs.gov/publication/tm7C26)

### 1.4 Cross-engine calibration-framework landscape

- PEST/PEST++: model-independent, gradient + ensemble; deepest MODFLOW integration; also used for 2D hydraulics (Iber-PEST). [pestpp](https://github.com/usgs/pestpp), [TM7-C26](https://pubs.usgs.gov/tm/07/c26/tm7c26.pdf), [Iber-PEST](https://www.sciencedirect.com/science/article/pii/S1364815224001087)
- OSTRICH: multi-algorithm (DDS, PSO, GA, SCE); direct SWMM precedent (OSTRICH-SWMM). [manual](http://www.civil.uwaterloo.ca/jrcraig/CIVE781/Ostrich_Manual_17_12_19.pdf)
- SPOTPY: pure-Python, in-process, 8 algorithms, 11 objective functions (NSE/KGE/PBIAS/RMSE native), MPI-parallel; best architectural fit for a Python-native TRID3NT calibration layer. [docs](https://spotpy.readthedocs.io/), [Houska et al. 2015 PLOS ONE](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0145180)

---

## 2. Validation metrics + numeric acceptance criteria

### 2.1 Metric definitions

- NSE = 1 - [Sum(Obs-Sim)^2 / Sum(Obs-Mean(Obs))^2]; range -inf to 1; 1 = perfect, 0 = as good as mean-flow benchmark. [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf)
- PBIAS = 100 x Sum(Sim-Obs) / Sum(Obs); positive = overestimation. [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf)
- RSR = RMSE / StDev(Obs). [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf)
- RMSE = sqrt[Sum(Sim-Obs)^2 / n]; no universal threshold, used relative to RSR/observed range.
- KGE = 1 - sqrt[(r-1)^2 + (beta-1)^2 + (gamma-1)^2]; r = Pearson correlation, beta = mean(Sim)/mean(Obs), gamma = CV(Sim)/CV(Obs). KGE > -0.41 = beats the observed-mean naive predictor. No agency threshold table (post-Moriasi). [Knoben et al. 2019 HESS](https://hess.copernicus.org/preprints/hess-2019-327/hess-2019-327.pdf)
- Flood-extent categorical (2x2 confusion of Model wet/dry x Benchmark wet/dry; hit M1B1, false alarm M1B0, miss M0B1): Hit Rate H = M1B1/(M1B1+M0B1); False Alarm Ratio F = M1B0/(M1B0+M1B1); Critical Success Index C = M1B1/(M1B1+M0B1+M1B0). [SEAMLESS-WAVE metrics](https://www.seamlesswave.com/metrics.html)
- Scaled RMSE (SRMS, groundwater) = head-residual RMSE / observed head range (max minus min).

### 2.2 Numeric acceptance criteria table

| Metric | Satisfactory | Good | Very good / best | Domain / notes | Source |
|---|---|---|---|---|---|
| NSE (streamflow, monthly) | > 0.50 (with RSR <= 0.70) | 0.65-0.75 | > 0.75 | Moriasi 2007 | [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf) |
| NSE (daily/monthly/annual, watershed) | > 0.50 | -- | -- | with R2 > 0.60, PBIAS <= +/-15% | [Moriasi 2015](https://web.ics.purdue.edu/~mgitau/pdf/Moriasi%20et%20al%202015.pdf) |
| PBIAS (streamflow) | <= +/-25% | <= +/-15% | <= +/-10% | Moriasi 2007; 2015 tightened satisfactory to <= +/-15% | [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf), [2015](https://web.ics.purdue.edu/~mgitau/pdf/Moriasi%20et%20al%202015.pdf) |
| PBIAS (sediment) | <= +/-55% | -- | -- | Moriasi 2007 | [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf) |
| PBIAS (N/P) | <= +/-70% | -- | -- | Moriasi 2007 | [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf) |
| RSR | <= 0.70 | <= 0.60 | <= 0.50 | Moriasi 2007, use with NSE+PBIAS | [Moriasi 2007](https://swat.tamu.edu/media/90109/moriasimodeleval.pdf) |
| KGE | > -0.41 = adds value | -- | -- | diagnostic, no graded pass/fail | [Knoben et al. 2019](https://hess.copernicus.org/preprints/hess-2019-327/hess-2019-327.pdf) |
| Peak flow/stage error (PEPF) | ~ +/-10-15% (contextual) | -- | -- | engineering rule of thumb (FEMA, UK EA), not codified | (standards brief; no single agency number found) |
| Flood-extent CSI (C) | ~ 0.5-0.7 = good agreement | -- | -- | research convention, not agency-codified | [SEAMLESS-WAVE](https://www.seamlesswave.com/metrics.html), [SFINCS preprint](https://egusphere.copernicus.org/preprints/2025/egusphere-2025-4387/) |
| SFINCS CSI (published, satellite extent) | global mean 0.42 (basin > 1000 km2); 0.29 (< 50 km2); 0.67 with observed vs GloFAS discharge (10 US events) | -- | -- | empirical, not a target | [EGUsphere 2025](https://egusphere.copernicus.org/preprints/2025/egusphere-2025-4387/) |
| SFINCS H / F / C (TC Idai) | C=0.75, H=0.94, F=0.22 (vs CaMa-Flood C=0.73/H=0.83/F=0.14) | -- | -- | compound flood vs Sentinel-1 | [NHESS 2023](https://nhess.copernicus.org/articles/23/823/2023/) |
| SFINCS H / F / C (TC Eloise) | C=0.46, H=0.82, F=0.48 | -- | -- | FAR climbs for small/urban events | [NHESS 2023](https://nhess.copernicus.org/articles/23/823/2023/) |
| SFINCS water-level average error | 0.73 m (Harvey/Houston, USGS + HWMs) | -- | -- | Sebastian et al. 2021 | [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0378383920304828) |
| SWMM peak/avg flow (community rule) | avg within 20%, peak within 30% for 80% of events | -- | -- | community convention, NOT an EPA mandate | [openswmm](https://www.openswmm.org/Topic/19690/calibration-peak-flow) |
| SWMM continuity error (runoff + routing) | single-digit %; practitioner bar ~3% | -- | -- | SWMM docs cite 10% illustratively; ~3% is the real bar | [openswmm](https://www.openswmm.org/Topic/19212/acceptable-continuity-error) |
| MODFLOW scaled RMSE (SRMS, heads) | < 10% | -- | -- | Anderson and Woessner convention; heuristic not hard rule | (MODFLOW brief; SRMS 4.4%/8.1% cited vs 10% limit) |
| MODFLOW volumetric budget percent discrepancy | < 1% | -- | -- | necessary not sufficient (0.012% case still judged inadequate) | [MODFLOW group](https://groups.google.com/g/modflow/c/ITGjdSNiyuE) |
| Flood Modeller 1D mass error | < 1% acceptable; 1-10% contextual; >= 10% investigate | -- | -- | %MassError = VolumeDiscrepancy/MaxSystemVolume x 100 | [Flood Modeller](https://help.floodmodeller.com/docs/1d-mass-balance) |

**Standing standards references:** CIWEM UDG "Code of Practice for the Hydraulic Modelling of Urban Drainage Systems" (successor to WaPUG) is the UK sewer-model verification reference and is known to define good/fair/poor bands for peak-flow %, volume %, time-to-peak, and node depth in mm/m, but the exact numeric bands could not be extracted from the PDF in this pass -- treat as unverified until the PDF text is pulled. [CIWEM CoP](https://www.ciwem.org/assets/pdf/Special%20Interest%20Groups/Urban%20Drainage%20Group/Code%20of%20Practice%20for%20the%20Hydraulic%20Modelling%20of%20Ur.pdf) ASTM D5490 (groundwater model comparison-to-observations) was withdrawn 2023 with no identified successor. [ASTM](https://www.astm.org/Standards/D5490.htm)

---

## 3. Review-checklist items with responsibility-cut classification

Enumerated from the Natural Resources Wales Flood Consequence Assessment flood-model checklist (24 numbered sections, ~120 items; the most complete machine-checkable proforma found), cross-referenced with FEMA, SEPA, ARR Book 7, and the engine-specific review cultures. Classification column is the responsibility cut: **Machine** = fully automatable from run artifacts; **Assisted** = machine flags/pre-fills, human confirms; **Human-only** = requires domain/local knowledge or visual judgment no artifact encodes. [NRW checklist](https://cyfoethnaturiolcymru.gov.uk/guidance-and-advice/business-sectors/planning-and-development/advice-for-developers/flood-consequence-assessment-flood-model-checklist/)

| # | Checklist item | Class | Basis |
|---|---|---|---|
| 1 | Mass balance / volume checking (continuity error, volume reconciles with flow lines) | Machine | continuity % is a parseable field (SWMM .rpt, MODFLOW LST, Flood Modeller, HEC-RAS log) |
| 2 | Stability: errors/warnings minimized, no oscillating cells/links | Machine | instability index, non-convergence counts, warning logs all parseable |
| 3 | Timestep adequacy (CFL, iterations/step, % non-converging steps) | Machine | SFINCS `timestep_analysis`, SWMM convergence stats, MODFLOW outer-iteration counts |
| 4 | Flows reconcile with independent hydrology estimates | Assisted | machine compares totals; human confirms hydrology source is the right one |
| 5 | Run duration long enough to capture peak + recession | Assisted | machine detects if peak is at series end; human confirms event framing |
| 6 | Reach length / cross-section spacing / grid size appropriate | Assisted | machine checks against thresholds; human judges local adequacy |
| 7 | Manning's n values within physical ranges for cover/material | Assisted | machine range-checks against lookup tables; human confirms cover mapping |
| 8 | Buildings represented appropriately (2D) | Human-only | requires visual/local judgment of representation method |
| 9 | DTM/LiDAR currency, resolution, datum, recent topo change | Assisted | machine reads metadata (resolution, datum, acquisition date); human judges "recent change" |
| 10 | Boundary condition location, extent, type correctness | Human-only | no published BC linter exists; unit/timing consistency partially machine-checkable |
| 11 | Inflow/rainfall/infiltration inputs correct | Assisted | machine checks presence/units/ranges; human confirms design event |
| 12 | 1D-2D coupling correctness | Human-only | topological/conceptual judgment |
| 13 | Verification against recorded flood extent | Assisted | machine computes CSI/H/F vs observed raster; human confirms the observed extent is valid |
| 14 | Verification against anecdotal / local knowledge | Human-only | local knowledge, no artifact encodes it |
| 15 | Calibration event selection + hydrograph peak/shape match | Assisted | machine computes NSE/PBIAS/peak error; human selects/justifies events |
| 16 | Sensitivity analysis performed | Assisted | machine runs parameter sweeps; human interprets |
| 17 | Uncertainty limits + freeboard stated | Human-only | judgment on acceptable limits |
| 18 | Blockage / breach scenario setup (FCA) | Human-only | scenario design judgment |
| 19 | Off-site impact <= 5 mm threshold check | Machine | differencing two rasters against a fixed threshold |
| 20 | Mitigation adequacy | Human-only | design judgment |
| 21 | Tidal boundary / wave-overtopping calcs | Assisted | machine range/units-checks; human confirms method |
| 22 | Structure (weir/culvert/bridge) data correctness | Human-only | as-built/field judgment |
| 23 | Housekeeping: log files present, complete, no fatal errors | Machine | log presence + fatal-error grep |
| 24 | Dry-weather flow / RDII (RTK) plausibility (urban) | Assisted | machine checks continuity/volume; human confirms monitor basis |

**Precedent for automated review:** FEMA CHECK-2/CHECK-RAS are agency-sanctioned automated hydraulic-model checkers (the closest FEMA analog to automated QA). [FEMA general hydraulics guidance](https://www.fema.gov/sites/default/files/documents/fema_general-hydraulics-guidance_112022.pdf), [FEMA model acceptance checklist](https://www.fema.gov/sites/default/files/2020-03/Model_Acceptance_Checklist_Feb_2018.pdf) HEC-RAS 2D Computation Log is the most mature machine-checkable volume-accounting + stability-flag precedent (with a documented false-positive class: storage-area-only models misreport final volume). [HEC-RAS volume accounting](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/running-a-model-with-2d-flow-areas/computation-progress-numerical-stability-and-volume-accounting), [HEC-RAS troubleshooting](https://www.hec.usace.army.mil/confluence/rasdocs/rasum/latest/troubleshooting-with-hec-ras) OpenQuake Hamlet (per-logic-tree-branch PSHA evaluation) is an architectural precedent for per-component auto-checks. [Hamlet](https://cossatot.gitlab.io/hamlet/) No published boundary-condition linter exists, confirming that BC correctness and local-knowledge items stay Human-only across every source reviewed.

---

## 4. Engine self-diagnostics parseable for automated post-run checks

### 4.1 SFINCS
- `sfincs_map.nc`: `cuminf` (cumulative infiltration depth, whole sim), `cumprcp` (cumulative precipitation depth), `zsmax`/`vmax`/`qmax` (maxima when `dtmaxout>0`), plus storage/subgrid volume fields, the closest mass-balance inputs.
- Screen/log at completion: total runtime, average timestep, time consumption per section (boundaries/momentum/continuity/output), and maximum occurred water depth (instability indicator).
- `timestep_analysis = 1` writes `average_required_timestep` and `percentage_limiting_timestep` per cell to `sfincs_map.nc`, plus a log summary naming the single most CFL-limiting U/V point and its bottleneck frequency, directly parseable for a numerical-health check.
- Gap: no named "mass balance error" / "continuity error" percentage field. A mass-balance check must be derived post-hoc: sum(`cumprcp`) vs sum(`cuminf`) + net boundary flux + (final - initial) storage_volume, computed from `sfincs_map.nc`/`sfincs_his.nc`. Flag this gap vs SWMM-style explicit reporting.
[output manual](https://sfincs.readthedocs.io/en/latest/output.html), [input manual](https://sfincs.readthedocs.io/en/latest/input.html)

### 4.2 SWMM (.rpt)
- Continuity Error lines: Runoff Quantity Continuity and Flow Routing Continuity, reported as %. Practitioner bar ~3% / single-digit.
- Flow Routing Continuity table: inflow, outflow, initial/final storage, % error (grep the block directly).
- Node Flooding Summary: total flood volume + hours flooded per node (unexpected flooding = red flag).
- Node Surcharge Summary + per-link Flow Instability Index (e.g. 24 = flow oscillated across several timesteps), a direct machine-checkable instability signal.
- Time-Step-Critical Elements / non-converging steps: average iterations per step + % steps not converging under dynamic wave, both numeric pass/fail fields.
[openswmm continuity](https://www.openswmm.org/Topic/19212/acceptable-continuity-error), [instability index](https://www.openswmm.org/Topic/4761/model-instability-measure-model-report-card), [status report tolerances](https://www.openswmm.org/Topic/11534/status-report-tolerances-rules-of-thumb)

### 4.3 MODFLOW 6 (LST listing file)
- Volumetric budget percent discrepancy: printed per stress period/time step; < 1% commonly cited "adequate" (necessary not sufficient).
- Convergence failures: summary/count of time steps that failed to converge; grep for "failed to converge" / oscillating-cell messages.
- Dry-cell events: reported in cell-by-cell output; count of dry cells per stress period is a standard early-warning grep target.
- Caveat: the MODFLOW 6 IO reference (mf6io.pdf, TM 6-A55/6-A57) returned HTTP 403 this pass; exact LST field names/message text should be confirmed against the primary source before hardcoding a parser. Numbers above are corroborated by community/practitioner sources.
[MODFLOW budget thread](https://groups.google.com/g/modflow/c/ITGjdSNiyuE), [convergence thread](https://groups.google.com/g/modflow/c/PjPH8O_rnLI), [Aquaveo troubleshooting](https://aquaveo.com/blog/post/troubleshooting-modflow)

### 4.4 Cross-engine precedents for the parser
- Flood Modeller 1D: explicit `%MassError = VolumeDiscrepancy / MaxSystemVolume x 100`, V_NET = V_IN + V_LINKIN + V_MUSKOUT - (V_OUT + V_LINKOUT + V_MUSKIN); bands < 1% / 1-10% / >= 10%. The cleanest published numeric mass-balance template. [Flood Modeller](https://help.floodmodeller.com/docs/1d-mass-balance)
- HEC-RAS: built-in per-2D-flow-area + whole-model volume accounting as parseable text, plus an Errors/Warnings/Notes system. [HEC-RAS](https://www.hec.usace.army.mil/confluence/rasdocs/r2dum/latest/running-a-model-with-2d-flow-areas/computation-progress-numerical-stability-and-volume-accounting)

---

## 5. Recommended capability roadmap for TRID3NT (design only)

Design-only. Honors: the simplicity principle (99% coverage with far fewer/simpler instructions beats 100% that is 10x more complex), typed honest errors (every path degrades primary -> fallback -> honest typed error, never silent success), and the decision-card machinery for human checkpoints.

### Capability A -- Post-run diagnostics reader (Machine tier, ship first)
A single per-engine "read the run's own self-report" primitive: parse the artifacts in section 4 into one normalized envelope `{engine, continuity_error_pct, max_instability_index, nonconverging_steps_pct, dry_cells, mass_balance_source: "reported"|"derived", warnings[]}`. Simplicity: one small parser per engine, one shared schema, no modeling logic. Honest errors: if a field is absent (SFINCS has no continuity %), the envelope returns `mass_balance_source: "derived"` and computes it from `cumprcp`/`cuminf`/storage, or returns a typed `DiagnosticUnavailable` rather than a fabricated pass. This is a DATA fetcher, not a composed analysis, so it belongs as an atomic tool; richer scoring lives in the playground.

### Capability B -- Numeric acceptance gate as a decision card (Assisted tier)
Compute the section-2 metrics (NSE/PBIAS/RSR/KGE for gauge series; H/F/CSI for extent rasters; SRMS for heads) against user-supplied observations, then surface results through a decision card rather than an auto-pass/fail. The card shows each metric, its Moriasi/published band, and a machine-suggested verdict, and asks the human to confirm event selection and observation validity (checklist items 4, 13, 15). This keeps the honest position that thresholds are heuristics, not hard rules (explicit in both the MODFLOW SRMS and Moriasi sources), and puts the judgment call where the review culture keeps it: with a human. Metric formulas are a thin, reusable library (SPOTPY already implements NSE/KGE/PBIAS/RMSE natively) rather than bespoke code.

### Capability C -- Review-checklist runner mapped to the responsibility cut (Assisted + Human-only tiers)
Encode the section-3 table as the checklist. Machine items (1, 2, 3, 19, 23) run automatically off Capability A output and the run logs. Assisted items pre-fill from metadata/metrics and raise a decision card for confirmation. Human-only items (8, 10, 12, 14, 17, 18, 20, 22) are never auto-passed; they appear on the card as explicit unchecked human sign-offs with a typed `RequiresHumanReview` status. Simplicity: one declarative checklist table drives all three tiers; the classification column is the routing key. This directly mirrors the NRW proforma and the FEMA CHECK-RAS precedent without reimplementing either.

### Capability D -- Calibration as a playground workflow, not a monolithic tool (design guardrail)
Calibration composes layers (parameter perturbation -> run -> metric -> optimizer step), so per the "analysis is playground, not tools" rule it belongs in the code_exec playground driving atomic primitives, not a single opaque calibrate() tool. Provide atomic primitives only: (a) a parameter-write primitive per engine (Manning/infiltration/K-field template injection, the PstFrom/hydromt-sfincs/PySWMM pattern), (b) the run primitive, (c) the Capability B metric primitive. SPOTPY is the recommended in-process Python backend (native NSE/KGE/PBIAS, importable) for the surface engines; PEST++/pyEMU stays the MODFLOW path (industry standard, ensemble run cost tractable at a few hundred runs). Honest cost surfacing: expose estimated run count up front (IES ~100-250 realizations x 2 iterations; GLM scales with parameter count) so a calibration request that would exceed the local budget returns a typed `BudgetExceeded` with the estimate, feeding the granularity/decision-card gate rather than silently launching thousands of runs.

### Sequencing rationale
A before B before C: diagnostics are pure machine work with the highest certainty and lowest surface, and both the metric gate and the checklist runner consume A's normalized envelope. D is a guardrail on how calibration is built (playground + atomic primitives), applied whenever a calibration capability is actually scheduled, and gated behind the same decision-card + budget-estimate machinery so heavy compute never launches without a human checkpoint.

### Flagged gaps carried into design
- No PEST-style automated SFINCS calibration exists in the literature; treat SFINCS auto-calibration as R&D, not a shippable primitive yet.
- SFINCS has no explicit mass-balance field; Capability A must derive it and label it `derived`.
- CIWEM/WaPUG exact numeric verification bands are unverified pending a successful PDF extract; do not hardcode them.
- MODFLOW 6 IO doc (TM 6-A55/6-A57) was 403-blocked; confirm exact LST field names before shipping the MODFLOW parser.
- No published boundary-condition linter exists; BC correctness stays Human-only by design.
