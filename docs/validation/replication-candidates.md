# Flood V&V Replication Candidates

Purpose: a verify-and-pick menu of published US flood validation studies whose event,
observations, and (where available) model files are public today, so TRID3NT can run its
own solver over the same event and check computed-vs-observed against the same USGS gauges /
high-water marks the original study used -- and, where a model archive exists, optionally
cross-check against the study's own computed water-surface profile.

Scope note: TRID3NT integrates SFINCS (coastal/compound), MODFLOW (groundwater), and PySWMM
(urban). It does NOT integrate HEC-RAS, HEC-HMS, SRH-2D, Delft3D FM, or TELEMAC. So for every
candidate below there are two replication modes, and the effort column distinguishes them:
- Mode A (benchmark): use the study's event + public USGS obs (and its reported calibration
  residuals) as ground truth for TRID3NT's OWN solver. No third-party solver integration needed.
- Mode B (exact rerun): rerun the study's archived model to reproduce its published profile.
  Requires standing up that solver (for HEC-RAS this is the "requires HEC-RAS integration" tag;
  see the Linux-feasibility subsection).

Source provenance and caveats: candidates were assembled by four parallel search passes.
Angle-4 candidates (SRH-2D / Delft3D / GeoClaw) were confirmed against the primary USGS PDF
text (curl + pdftotext) for solver name, DOI, gauge numbers, and calibration event. Angle-1
(HEC-RAS FIM) candidates were sourced from USGS report abstracts and data-release landing pages
via search snippets -- pubs.usgs.gov and sciencebase.gov returned HTTP 403 to the fetch tool,
so exact numeric calibration tables (WSE residuals in ft, RMSE) were NOT pulled from those PDFs
in this pass and must be confirmed from the PDF before citing numbers. Every URL below is
reproduced verbatim from the search output; none were invented. VERIFY the 403-blocked ones
(fetch the PDF in a browser) before committing engineering time.

---

## Shortlist (6)

| # | Study | Solver | US site + event | Obs data -> our fetchers | Model files public? | What we would replicate | Effort |
|---|-------|--------|-----------------|--------------------------|---------------------|-------------------------|--------|
| 1 | Lower Nooksack River & Delta, WA (USGS SIR 2023-5047) | Delft3D Flexible Mesh (D-Flow FM), open/free | Lower Nooksack River + delta, Whatcom Co., WA; Nov 2021 atmospheric-river compound flood | Ferndale gauge 12213100 discharge/stage -> `fetch_usgs_nwis_gauges`; HWM/water-level sites (Slater Rd, Marine Dr, delta) -> `fetch_high_water_marks` | YES -- USGS data release DOI 10.5066/P9DJM7X2 (model input + projections) | Force with Nov-2021 Ferndale discharge; compare simulated max water levels vs the report's Ferndale / Slater Rd / Marine Dr / delta validation plots. Best SFINCS-relevant (compound coastal+riverine) target. | Medium (obs public; Mode A = SFINCS benchmark, no 3rd-party solver; Mode B needs Delft3D FM stand-up) |
| 2 | Joachim Creek, De Soto, MO (USGS SIR 2021-5058) | SRH-2D v3.2.3 (USBR), free | 6.7-mi reach, Joachim Creek at De Soto, MO; Apr 18 2013 flash flood + Sep 8 2018 peak measurement | Streamgage 07019500 stage/discharge -> `fetch_usgs_nwis_gauges`; Apr-2013 HWMs -> `fetch_high_water_marks` | YES -- USGS data release DOI 10.5066/P92MQYE7 (SRH-2D mesh + run files) | Rerun archived SRH-2D mesh; compare simulated WSE at 07019500 vs Apr-2013 HWM calibration targets (confirmed in SIR PDF text). | Medium (SRH-2D is free; no HEC-RAS integration; Mode B needs SMS/SRH-2D stand-up) |
| 3 | Wabash River near I-64, Grayville, IL (USGS SIR 2017-5140) | SRH-2D v2 (USBR), free | ~30-mi Wabash reach near I-64 bridge, Grayville, IL; steady-state calibration flows | Gauges 03377500 (Mount Carmel, IL) + 03378500 (New Harmony, IN) stage-discharge -> `fetch_usgs_nwis_gauges`; GPS-surveyed WSEs -> `fetch_high_water_marks` | YES -- USGS data release DOI 10.5066/F78P5ZCD | Rerun SRH-2D at reported steady discharges; match the paper's published residuals: mean WSE diff -0.04 ft at 03377500, +0.04 ft at 03378500 (table 4, confirmed in PDF) -- exact numeric target. | Medium (free solver; no HEC-RAS integration) |
| 4 | Hurricane Ike (2008) storm surge -- GeoClaw example (Mandli & Dawson 2014) | GeoClaw / Clawpack, fully open source | Upper TX coast / Galveston Bay; Hurricane Ike, Sep 2008 | Tide-gauge water-level time series (NOAA CO-OPS; fetcher TBD) + optional USGS STN Ike HWMs -> `fetch_high_water_marks` | YES -- runnable `storm-surge/ike` example in clawpack/geoclaw repo (setrun.py/setplot.py/Makefile) | Clone + run the built-in Ike example (the actual code path in the paper); compare simulated water level at gauge locations; optionally extend to USGS Ike HWM extent. Strong SFINCS coastal analog. | Low (fully open, built-in example, no integration friction). CAVEAT: paper validated vs tide gauges + ADCIRC, NOT USGS HWMs -- HWM check is ours to add. |
| 5 | Silver Creek Basin / Scott AFB, IL (USGS SIR 2024-5117) | HEC-HMS 4.9 -> HEC-RAS 6.5 (chained) | Silver Creek + tributaries, Scott AFB, IL; 6 high-flow events (2015, 2018, 2020x2, 2022, 2023) | Gauges 05594800 (Freeburg, IL) + 05594450 (near Troy, IL) streamflow time series -> `fetch_usgs_nwis_gauges` | YES -- USGS data release DOI 10.5066/P9GBYP2K (HEC-HMS + HEC-RAS files + calibration metrics) | Precip-to-inundation chain over 6 independent events at 2 gauges; compare discharge/stage time series vs both gauge records. Newest + most rigorous of the set; closest to TRID3NT's precip->gauge end-to-end pipeline. | High -- Mode B requires HEC-RAS + HEC-HMS integration (feasible on Linux, one Windows-GUI pass; see feasibility subsection). Mode A = use 6 gauge events as ground truth for TRID3NT's own solver, no integration. |
| 6 | White River at Noblesville, IN (USGS SIR 2017-5123) | HEC-RAS 1D step-backwater | 7.5-mi White River reach at Noblesville, IN; Sep 4 2003 + May 6 2017 floods | Gauge 03349000 stage-discharge (2016 rating) -> `fetch_usgs_nwis_gauges`; Sep-2003 + May-2017 HWMs -> `fetch_high_water_marks` | YES -- USGS data release DOI 10.5066/F7MG7N0J (parent item w/ Model Archive + depth-grid + shapefile children) | Rerun the 15 calibrated steady profiles (10-24 ft); compare stage/inundation vs the two independent historical HWM sets + 2016 rating -- two events = two independent computed-vs-observed checks at one site. | High -- Mode B requires HEC-RAS integration. Mode A = two-event gauge/HWM ground truth for TRID3NT's own solver. |

Table legend: "Mode A" = TRID3NT runs its own solver over the same event and checks against
public USGS obs. "Mode B" = literally rerun the study's archived model. The "requires HEC-RAS
integration" tag applies only to Mode B for candidates 5 and 6.

---

## Full verbatim citations (one block per candidate)

### Shortlist

**[1] Lower Nooksack River & Delta, WA -- Delft3D FM**
- Grossman, E.E., vanArendonk, N.R., and Nederhoff, K. (2023), "Compound Flood Model for the Lower Nooksack River and Delta, Western Washington -- Assessment of Vulnerability and Nature-Based Adaptation Opportunities to Mitigate Higher Sea Level and Stream Flooding," U.S. Geological Survey Scientific Investigations Report 2023-5047, 49 p.
- Report DOI: https://doi.org/10.3133/sir20235047
- Model/data release: Grossman, E.E., vanArendonk, N.R., Nederhoff, K., and Parker, K.A. (2023), "Model input and projections of extent, frequency, and depths for the lower Nooksack River and delta, western Washington State," USGS data release, https://doi.org/10.5066/P9DJM7X2
- Solver: Delft3D Flexible Mesh (D-Flow FM). Event: Nov 2021 Pacific Northwest atmospheric-river flood. Obs: USGS streamgage 12213100 (Ferndale); validation sites Slater Road, Marine Drive, Nooksack delta. Confirmed from SIR PDF ("Model Validation"/"Model Calibration" sections, fig. 3 Ferndale discharge comparison).

**[2] Joachim Creek, De Soto, MO -- SRH-2D**
- Hix, K.D., Rydlund, P.H., and Heimann, D.C. (2021), "Two-Dimensional Hydraulic Analyses of Joachim Creek, De Soto, Missouri," U.S. Geological Survey Scientific Investigations Report 2021-5058, 28 p.
- Report DOI: https://doi.org/10.3133/sir20215058
- Model/data release: Hix, K.D., and Heimann, D.C. (2021), "Geospatial data and model archive associated with the two-dimensional hydraulic analysis of Joachim Creek, De Soto, Missouri," USGS data release, https://doi.org/10.5066/P92MQYE7
- Solver: SRH-2D version 3.2.3 (USBR 2008) via SMS. Event: Apr 18 2013 flash flood + Sep 8 2018 peak measurement. Obs: USGS streamgage 07019500 (Joachim Creek at De Soto, MO); Apr-2013 HWMs; lidar DEM. Confirmed from SIR PDF text ("SRH-2D, version 3.2.3 ... calibrated to the highest stage-streamflow measurements ... USGS streamgage ... station 07019500").

**[3] Wabash River near I-64 Bridge, Grayville, IL -- SRH-2D**
- Boldt, J.A. (2018), "Development of a Hydraulic Model and Flood-Inundation Maps for the Wabash River near the Interstate 64 Bridge near Grayville, Illinois," U.S. Geological Survey Scientific Investigations Report 2017-5140, 13 p.
- Report DOI: https://doi.org/10.3133/sir20175140
- Model/data release: Boldt, J.A. (2018), USGS data release for the Wabash River I-64 flood-inundation study, https://doi.org/10.5066/F78P5ZCD
- Solver: SRH-2D version 2 (USBR 2008). Event: steady-state calibration flows (no single named flood). Obs: USGS streamgages 03377500 (Wabash River at Mount Carmel, IL) + 03378500 (Wabash River at New Harmony, IN); GPS-surveyed WSEs. Reported residuals (table 4, confirmed in PDF): mean WSE diff -0.04 ft at Mount Carmel, +0.04 ft at New Harmony.

**[4] Hurricane Ike (2008) storm surge -- GeoClaw**
- Mandli, K.T., and Dawson, C.N. (2014), "Adaptive Mesh Refinement for Storm Surge," Ocean Modelling, vol. 75, p. 36-50.
- Paper DOI: https://doi.org/10.1016/j.ocemod.2014.01.002
- Preprint: https://arxiv.org/abs/1401.5744
- Runnable model example: https://github.com/clawpack/geoclaw/tree/master/examples/storm-surge/ike
- Quick-start guide: https://www.clawpack.org/quick_surge.html
- Solver: GeoClaw (Clawpack), fully open source. Event: Hurricane Ike, Sep 2008, Galveston Bay / upper TX coast. Obs (per paper): tide-gauge water-level time series, cross-checked vs ADCIRC. NOTE: the paper's own validation is tide gauges + ADCIRC, NOT the USGS STN/OTWSC Ike high-water-mark survey -- pairing the run with that separately-archived USGS Ike HWM dataset is our step, not the paper's. (Validation-dataset claim sourced from search summarization of the abstract/arXiv listing, not a full-text fetch -- Elsevier paywall.)

**[5] Silver Creek Basin / Scott AFB, IL -- HEC-HMS + HEC-RAS**
- Cigrand, C.V., Heimann, D.C., and Rydlund, P.H. (2024), "Hydrologic and Hydraulic Analyses of Silver Creek and Selected Tributaries Associated with Scott Air Force Base, Illinois, 2022-24," U.S. Geological Survey Scientific Investigations Report 2024-5117.
- Report (full): https://pubs.usgs.gov/publication/sir20245117/full
- Model archive landing: https://www.usgs.gov/data/archive-hydrologic-and-hydraulic-models-used-analyses-silver-creek-basin-and-selected
- Data release DOI: https://doi.org/10.5066/P9GBYP2K
- Solver: HEC-HMS 4.9 chained to HEC-RAS 6.5. Events: six high-flow events -- Jun 21 2015; Sep 8-10 2018; Jan 12-13 2020; Aug 12-13 2020; Jul 26-27 2022; Mar 23-26 2023. Obs: USGS streamgages 05594800 (Silver Creek at Freeburg, IL) + 05594450 (near Troy, IL). Archive states it includes the agency's own calibration metrics + model outputs.

**[6] White River at Noblesville, IN -- HEC-RAS 1D**
- Martin, Z.W. (2017), "Flood-Inundation Maps for the White River at Noblesville, Indiana," U.S. Geological Survey Scientific Investigations Report 2017-5123.
- Report: https://pubs.usgs.gov/publication/sir20175123
- Model archive (ScienceBase parent item, 3 children -- model archive, depth grids, shapefile): https://www.sciencebase.gov/catalog/item/5909fd0ce4b0fc4e44916004
- Data release DOI: https://doi.org/10.5066/F7MG7N0J
- Landing page: https://www.usgs.gov/data/white-river-noblesville-indiana-flood-inundation-hec-ras-model-and-gis-data
- Solver: HEC-RAS 1D step-backwater. Events: Sep 4 2003 + May 6 2017 floods. Obs: USGS streamgage 03349000 (White River at Noblesville, IN) 2016 stage-discharge rating; Sep-2003 + May-2017 HWMs; LiDAR DEM (0.98-ft vertical accuracy). 15 calibrated steady profiles (10-24 ft). (Sourced from report abstract + data-release landing page; pubs.usgs.gov / sciencebase.gov were 403-blocked to the fetch tool -- confirm numbers from the PDF.)

### Additional USGS archives available but not shortlisted (same publication pattern)

These are real, downloadable, gauge/HWM-referenced USGS archives that lost only on redundancy
or on requiring HEC-RAS integration for a Mode-B rerun. Keep as backups.

**[A] St. Joseph River at Elkhart, IN -- HEC-RAS 1D (SIR 2016-5179)**
- Martin, Z.W. (2017), "Flood-Inundation Maps for the St. Joseph River at Elkhart, Indiana," USGS Scientific Investigations Report 2016-5179.
- Report DOI: https://doi.org/10.3133/sir20165179 (also https://pubs.usgs.gov/publication/sir20165179)
- Model archive (ScienceBase): https://www.sciencebase.gov/catalog/item/584197dfe4b04fc80e518b6b
- Landing page: https://www.usgs.gov/data/st-joseph-river-elkhart-indiana-flood-inundation-hec-ras-model
- Obs: USGS streamgage 04101000; surveyed HWMs from the March 1982 flood; LiDAR DEM (0.49-ft RMSE). Six calibrated 1-ft-interval steady profiles (23-28 ft). Simplest 1D FIM archive; superseded in the shortlist by Noblesville (two events vs one). Requires HEC-RAS integration for Mode B.

**[B] Muddy Creek at Harrisonville, MO -- HEC-HMS 4.4.1 + HEC-RAS 5.0.7 (SIR 2022-5084)**
- Heimann, D.C., and Rydlund, P.H. (2022), "Precipitation-Driven Flood-Inundation Mapping of Muddy Creek at Harrisonville, Missouri," USGS Scientific Investigations Report 2022-5084.
- Report: https://pubs.usgs.gov/publication/sir20225084 (PDF: https://pubs.usgs.gov/sir/2022/5084/sir20225084.pdf)
- Data/model archive landing: https://www.usgs.gov/data/geospatial-data-and-model-archives-associated-precipitation-driven-flood-inundation-mapping
- Data release DOI: https://doi.org/10.5066/P969ZOLB
- Obs: observed streamflow/stage for Sep 28 2019, May 27 2021, Jun 25 2021 runoff events near Harrisonville (exact USGS site numbers NOT independently confirmed in the search pass -- verify in the SIR before citing). Full rainfall-to-inundation chain; superseded in the shortlist by Silver Creek (newer HEC-RAS 6.5, 6 events, 2 gauges). Requires HEC-RAS + HEC-HMS integration for Mode B.

USGS also runs dozens more Flood Inundation Mapping (FIM) sites nationwide (browsable at
water.usgs.gov/osw/flood_inundation/ -- not individually verified here); the archives above set
the template for what a public, gauge-referenced, HWM-calibrated USGS archive looks like.

---

## "Requires HEC-RAS integration" -- Linux-feasibility findings (why the tag, and how hard)

Relevant to shortlist candidates 5 and 6 (and backups A, B) if we choose Mode B (exact rerun).
Bottom line: headless HEC-RAS on Linux/Docker IS feasible today for the COMPUTE step, but NOT
the full authoring workflow.

- USACE ships native RHEL8 x64 Linux compute binaries (RasGeomPreprocess, RasUnsteady,
  RasSteady) since v6.1, through v6.5, v6.6, and current v7.0.1 ("Windows + Linux" installer,
  313 MB, released Jun 2 2026). These are native ELF binaries, not WSL-only.
  - v6.5 Linux release notes: https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_650_Linux_Build_Release_Notes.pdf
  - v6.6: https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_66_Linux_Build_Release_Notes.pdf
  - v6.1: https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_610_Linux_Build_Release_Notes.pdf
  - Linux Computation Engines manual: https://www.hec.usace.army.mil/confluence/rasdocs/rasum/latest/working-with-hec-ras/linux-computation-engines
  - Download bundle: https://www.hec.usace.army.mil/software/hec-ras/download.aspx
- THE CATCH (USACE's own words, v6.5 notes): "The procedure for performing a Linux run for a
  HEC-RAS river system is to generate the Unsteady Flow model input files by first performing a
  Windows HEC-RAS GUI run." Geometry/mesh authoring and any plan edits still need ONE pass
  through the Windows GUI (a VM is fine), after which the plan HDF's "Results" group is stripped
  via HEC's provided Python script before the native Linux binaries consume it. To rerun an
  already-archived study (Mode B) this is a one-time step per model.
- Tooling to script the batch run:
  - RAS-Commander (MIT): https://github.com/gpt-cmdr/ras-commander -- exposes
    RasCmdr.compute_plan_linux() to drive RasUnsteady/RasSteady headlessly + read results via HDF.
  - FEMA/USACE FFRD production spec (independent proof a federal program runs Dockerized HEC-RAS
    6.1 on UBI8 Linux today): https://fema-ffrd.github.io/specs/draft/ras_sim/ras_sim/ ;
    org: https://github.com/fema-ffrd (see fema-ffrd/rashdf, fema-ffrd/hecstac).
- Licensing: HEC-RAS is US-Government-owned, free, redistributable, but NOT public-domain code;
  the Terms prohibit modifying/decompiling/reverse-engineering:
  https://www.hec.usace.army.mil/software/terms_and_conditions.aspx
- Forward-looking (do NOT build on yet): HEC-RAS 2025 is a container/cloud-native rebuild but is
  Beta (since Apr 2026) and USACE says not to use it for studies/production.
  https://www.hec.usace.army.mil/software/hec-ras/2025/ ;
  https://www.hec.usace.army.mil/confluence/hecnews/fall-2024/future-of-hec-ras
- Unofficial full-Linux 2D path (LOW CONFIDENCE, unverified, reimplements USACE's proprietary
  geometry preprocessor -- cross-check against a GUI-generated HDF before trusting):
  https://github.com/neeraip/hecras-v66-linux

Practical implication for the effort column: for a Mode-B HEC-RAS/HEC-HMS rerun, budget for
(1) obtaining the archive's geometry/plan/flow files, (2) one Windows-GUI pass, (3) strip
Results, (4) run native Linux binaries (match the study's RAS version) via RAS-Commander,
(5) diff computed stage/flow vs the study's USGS gauge comparison. Mode A (TRID3NT's own solver
vs the same public gauges/HWMs) avoids all of this.

---

## Near-misses (looked good, fail replicability -- with the reason)

**Lower Pembina River, ND/MB -- TELEMAC-2D (NRC Canadian Hydraulics Centre for the IJC, 2009+).**
The ONLY genuinely strong US-anchored, gauge-validated TELEMAC-2D study found. Point obs are
fully public and USGS-fetchable: Pembina River at Walhalla, ND (USGS 05099600), at Neche, ND
(USGS 05100000), and Red River at Pembina, ND (USGS 05102490); calibration event Apr 2006,
verification event Jun 2005; areal-extent V&V against NDSWC aerial photos (report figs 10-13).
FAILS on model-file replicability: it is a client-commissioned technical report with NO open
model archive (no mesh/.slf, .cli, .cas). Exact geometry (25 culvert groups, road/dyke
elevations, Agriculture-and-Agri-Food-Canada 2006 LiDAR) is not re-hostable, so only an
equivalent-fidelity rebuild is possible, not an exact rerun. Kept out of the shortlist for that
reason, but flagged as the highest-value TELEMAC lead if TELEMAC integration is ever pursued.
- Report PDF: https://legacyfiles.ijc.org/publications/Preparation%20of%202d%20Pembina%20Model.pdf
- Project page (all phases): https://www.ijc.org/en/rrb/2-d-telemac-modelling-lower-pembina-river-phase-5
- Phase 5 (2015) report: https://www.ijc.org/sites/default/files/migrate_default_content_files/Simulation_of_Hypothetical_Flood_Mitigation_Scenarios_on_the_Lower_Pembina_River_Floodplains_with_the_TELEMAC2D_Hydrodynamic_Model_Phase_5.pdf
- June 2010 follow-on: https://www.ijc.org/sites/default/files/report_Pembina_CHC_June_2010_5.pdf

**Hurricane Ike, Texas coast -- coupled TELEMAC-2D + TOMAWAC + SISYPHE (McCarron et al., 2021).**
Right event (Ike, TX) and plausibly right class of obs (USGS SWaTH storm-tide sensors, NOAA tide
gauges), but a private-consultancy conference paper with NO data-availability statement and NO
model archive; full text was 403/DNS-blocked so the exact gauge citations could not be confirmed.
Cannot verify the observation dataset or claim a numeric benchmark. Excluded.
- Abstract/record: https://www.researchgate.net/publication/372960802_Modelling_the_impacts_of_Hurricane_Ike_on_the_Texas_coast_using_a_fully_coupled_TELEMAC-TOMAWAC-SISYPHE_model
- HR Wallingford EPrints: https://eprints.hrwallingford.com/1493/
- Proceedings PDF (reachability unconfirmed this session): https://tuc2020.org/Proceedings_TUC_2020_year_2021_v1.0.pdf

**Willamette River, OR -- 2D HEC-RAS 5.0.7 (USGS SIR 2022-5025, White & Wallick 2022).**
A genuine large modern 2D HEC-RAS archive with real topo-bathymetric DEM and a downloadable
data release, BUT framed around juvenile-salmonid habitat across a flow range, not
computed-vs-observed flood-peak/HWM validation against a named historical flood. Would need the
full SIR text to confirm it reports discrete WSE-difference calibration statistics before
treating it as a flood V&V benchmark. Excluded pending that confirmation.
- Report: https://pubs.usgs.gov/publication/sir20225025 (PDF: https://pubs.usgs.gov/sir/2022/5025/sir20225025.pdf)
- Data (ScienceBase): https://www.sciencebase.gov/catalog/item/620e94dad34e6c7e83baa7ce
- DOI: https://doi.org/10.5066/P9NB0KUT

**Continental-scale historical flood validation -- LISFLOOD-FP / First Street NFM (Wing et al., 2021).**
The single best-documented multi-event USGS-HWM validation table in the literature (LISFLOOD-FP
engine, 35 CONUS events, 9 validated against USGS high-water marks with residuals reported). NOT
replicable: the FSF-NFM model inputs/code are closed; the data-availability statement gates model
output to "reasonable request from the corresponding author." At most a published benchmark table
to compare a from-scratch open LISFLOOD-FP rebuild against -- not a run-it-yourself study.
- Wing, O.E.J., Smith, A.M., Marston, M.L., Porter, J.R., Amodeo, M.F., Sampson, C.C., and Bates, P.D. (2021), "Simulating historical flood events at the continental scale: observational validation of a large-scale hydrodynamic model," Natural Hazards and Earth System Sciences, 21, 559-575. https://doi.org/10.5194/nhess-21-559-2021

**HEC-RAS-on-Linux infrastructure (angle 3).** RAS-Commander, the FFRD spec, HEC-RAS 2025 Beta,
and neeraip/hecras-v66-linux are tooling/feasibility sources, NOT V&V studies with obs to
replicate. Folded into the feasibility subsection above rather than listed as candidates.

**Omitted rather than guessed (no citable primary source found):** a SWMM+TELEMAC-2D urban-flood
coupling paper (ScienceDirect 2024, likely China-based, 403-blocked -- unconfirmed); a Deer
Creek / Brentwood MO HEC-RAS-vs-TELEMAC comparison seen only in snippets (no title/authors/venue
located); a Delft3D-FM Galveston Bay HWM-validation figure (possible lead: Munoz et al., JAWRA
2022, https://doi.org/10.1111/1752-1688.12952 -- unverified for open model-file availability).
These were left out to avoid fabricating citations; treat as follow-up search leads only.

---

## Orchestrator lean (OPINION, not fact)

If we want the fastest defensible first replication, my lean is candidate [4] GeoClaw/Ike (zero
integration friction, fully open, built-in example -- validates the coastal/SFINCS analog path
end-to-end) followed by [1] Nooksack (a true compound-flood, gauge+HWM, open-archive SFINCS
target). The two SRH-2D archives ([2] Joachim, [3] Wabash) are the best "exact numeric residual
to match" targets if we want a hard pass/fail number. Defer the HEC-RAS Mode-B reruns ([5], [6])
until we've decided whether the value is the rerun itself or just the event-as-ground-truth --
in Mode A they need no HEC-RAS integration at all. This is a lean, not a decision: pick per what
you actually want the first benchmark to prove.
