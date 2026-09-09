# Hygiene manifest - `docs/decisions/`

Scope: every file under `docs/decisions/`, enumerated with `find docs/decisions -type f | sort`
BEFORE anything was read. 328 files, 46,270 lines (the 326 numbered records 0001-0327, with
gaps at 0221 and 0234 and a collision at 0307, plus `README.md` and `afk-ledger-2026-08-24.md`).
Every row below was written by the agent that READ that file end to end; no row is a grep
result.

Shape and verdict taxonomy per DOCS CENSUS RULED: BINDING survives, SUPERSEDED and DEAD are
deleted outright and git is the archive. The census's own 3.6 table is the shape; each verdict
here was re-derived from the record's own text plus the tree, not copied.

Tree evidence the verdicts rest on, taken this pass:

- `trid3nt_server/workflows/` holds `mesh runtime shared solver telemac` and nothing else;
  `workers/` holds `mesh` and `telemac` and nothing else.
- `grep -rli <engine> trid3nt_server workers`: hecras 0, schism 0, elmfire 0, swan 0. The
  residual counts for sfincs (20), swmm (11), modflow (9), openquake (5), geoclaw (4),
  pelicun (3) and landlab (2) are spec/comment mentions - none of them is a workflow package.
- `tests/test_door_dissolution.py::EXPECTED_TEMPLATES` is 8 TELEMAC names
  (`telemac_river_dye`, `river_oil_spill`, `river_scour`, `river_sediment_plume`, `do_sag`,
  `rain_on_grid`, `telemac3d_stratified_flow`, `artemis_harbor_agitation`);
  `PARKED_TEMPLATES` is empty. No coastal, TOMAWAC, HEC-RAS, SFINCS, SWMM or MODFLOW template
  registers.
- `contracts/trid3nt_contracts/styles.yaml` and `trid3nt_server/mcp_server.py` do not exist;
  `trid3nt_server/emission/presets.py`, `trid3nt_server/fallbacks/{ladder,walker,persist}.py`
  and `trid3nt_server/workflows/runtime/resolution.py` do.

Fate vocabulary: KEEP (BINDING, survives the chop); KEEP (AMEND: ...) for a BINDING record
that contradicts a later ruling and must say what is true; DELETE; DELETE after lifting the
named surviving clause or revisit trigger first; MOVE; REWRITE.

| number | title | BINDING / SUPERSEDED-by / DEAD-because | fate |
|---|---|---|---|
| 0001 | TRID3NT is a standalone QGIS product | BINDING | KEEP |
| 0002 | one daemon, one user, one process | BINDING | KEEP |
| 0003 | 99% coverage with 10x less beats 100% with 10x more | BINDING | KEEP |
| 0004 | TRID3NT everywhere, zero legacy names | BINDING | KEEP (AMEND: Layer B unmet - `GraceModel` is still the contract base class and `persistence.py` still migrates `~/.grace2`; the rename moves to the work queue) |
| 0005 | QGIS reads COGs natively; no tile server | SUPERSEDED by 0327 | DELETE after lifting the surviving clause (COGs are read natively, no tile server) into 0327 |
| 0006 | local-only server; cloud code lives elsewhere | BINDING | KEEP |
| 0007 | secrets in a local file vault | SUPERSEDED by 0062 | DELETE |
| 0008 | discovery is the front door; fetchers are adapters | BINDING | KEEP |
| 0009 | simulations own their inputs | BINDING | KEEP |
| 0010 | analysis is composed, not enumerated | BINDING | KEEP |
| 0011 | code execution is user-gated and honestly timed out | BINDING | KEEP |
| 0012 | fix typos in retrieval, never in the prompt | BINDING | KEEP |
| 0013 | rules graduate from prompt to code | BINDING | KEEP |
| 0014 | the LLM passes handles, never URIs (accepted, to implement) | BINDING | KEEP |
| 0015 | server/wheels holds PyPI-absent deps | BINDING | KEEP (AMEND: the path is `wheels/` at the repo root after 0272/0274, not `server/wheels/`) |
| 0016 | server uses the src/ layout | SUPERSEDED by 0272 then 0274 | DELETE |
| 0017 | the harness absorbs the prompt (PROPOSAL) | SUPERSEDED by 0014 / 0019 / 0030 | DELETE |
| 0018 | auto and ask modes (accepted, picker to implement) | SUPERSEDED by 0107 | DELETE |
| 0019 | search wins at scale; enumerate only what's small and hot | BINDING | KEEP |
| 0020 | remote access is the same single user | BINDING | KEEP |
| 0021 | V&V wave lock: folds and the 9-tool scope | SUPERSEDED by 0319 | DELETE |
| 0022 | Fidelity ladder + canonical-case V&V (Malpasset / TELEMAC-2D) | SUPERSEDED by the 2026-08-26 fetchable-observations refinement | DELETE after lifting the surviving clause (the fidelity ladder itself) into the standing norm |
| 0023 | US-only validation cases + paper-first replication standard | BINDING | KEEP (AMEND: the US-only clause was refined 2026-08-26 to cases wherever our substrate can FETCH gauges; paper-first is unchanged) |
| 0024 | Full engine control: entirety over scenario keyholes | SUPERSEDED by 0025 | DELETE |
| 0025 | Template library + knob manifests (runtime doctrine) | BINDING | KEEP |
| 0026 | Cut the contamination-affected-fields composer (playground recipe) | DEAD - subject gone | DELETE |
| 0027 | session durability across WebSocket churn | BINDING | KEEP |
| 0028 | dual-socket session-scoped registries | BINDING | KEEP |
| 0029 | rendering and camera do not depend on the LLM remembering | SUPERSEDED by 0313 | DELETE |
| 0030 | the AOI is pinned to the solve domain | BINDING | KEEP |
| 0031 | LayerURI emission integrity | BINDING | KEEP |
| 0032 | measured prompt-fit context compaction | SUPERSEDED by 0311 | DELETE |
| 0033 | sandbox containment posture on a single host | SUPERSEDED by 0324 | DELETE |
| 0034 | engine-door template registration | SUPERSEDED by 0094 | DELETE |
| 0035 | local solver execution I/O contract | BINDING | KEEP |
| 0036 | generic data-router (fetcher fold) router core | BINDING | KEEP |
| 0037 | fetcher-fold replication-parity closure (5/5 pilots) | SUPERSEDED by 0056 | DELETE |
| 0038 | fetcher-fold pilot promotion (phase-2 wave 1, the first real cut) | DEAD - the pilots' twins were cut and 3 of 5 left the tree | DELETE |
| 0039 | fetcher-fold phase-2 wave-2 (ArcGIS FeatureServer/MapServer vector family) | DEAD - re-adjudicated by the 2026-09-09 G1 ESRI-on-GDAL ruling | DELETE |
| 0040 | fetcher-fold phase-2 wave-3 (USGS water-data family, dataretrieval-delegated) | SUPERSEDED by 0036 + the 2026-09-09 library-fold closure | DELETE |
| 0041 | shared-workflows cull phase A (news + conservation + goes; P1 slider) | DEAD - subjects cut; the composer class died at 0105 | DELETE |
| 0042 | glm-lightning-animation cull; P2 dropped (revised gate) | DEAD - subject cut | DELETE |
| 0043 | processing/ redundancy cull wave (charts + clip + zonal + aggregate) | BINDING | KEEP |
| 0044 | ingest transport: httpx coalescing opener for remote-FILE reads | BINDING | KEEP |
| 0045 | fetcher-fold phase-2 wave-4 (station family; CO-OPS currents snapshot) | SUPERSEDED by 0065 | DELETE |
| 0046 | satellite fire-animation composer preemptive cull | DEAD - subject cut | DELETE |
| 0047 | fetcher fold wave-5 raster: STAC-tile transport migration + raster-family defer | SUPERSEDED by the shared `stac_raster` executor | DELETE |
| 0048 | processing de-cloud: trim the GDAL subprocess wiring | BINDING | KEEP |
| 0049 | catalog-surfacing experiment: registry-shrink decision (INCONCLUSIVE) | DEAD - INCONCLUSIVE; the re-run it asked for never happened | DELETE after lifting its revisit trigger into `REANALYZE_LEDGER.md` |
| 0050 | Design 3: stratified data pool (auto-trigger composed declaration) - NO_ADVANCE | DEAD - NO_ADVANCE; no pool machinery exists and 0094 dissolved the doors | DELETE after lifting its revisit trigger into `REANALYZE_LEDGER.md` |
| 0051 | observability/retention batch: rotation, telemetry retention, error actionability, shape classifier | BINDING | KEEP |
| 0052 | fetcher fold wave-6 VECTOR/ZIP family: 3 vector folds + ZIP-family defer | SUPERSEDED by 0067 | DELETE |
| 0053 | fetcher fold wave-7 RASTER ENABLERS: imageserver_export + continuous-float STAC | SUPERSEDED by the shared `stac_raster` executor | DELETE |
| 0054 | fetcher fold wave-8: copernicus re-point + gcn250 (redirect/skip-HEAD + int16 direct_window) | DEAD - the redirect/skip-HEAD machinery has no spec consumer | DELETE |
| 0055 | fetcher fold wave-9: multi_url VRT fan-out + gzip_object whole-object mode | SUPERSEDED - `fan_out.py` survives, `gzip_object` has no consumer | DELETE after lifting the surviving clause (the fan-out transform) into the router map |
| 0056 | fetcher fold wave-10: the tier-3 HOOK CONTRACT + proof by migration | BINDING | KEEP |
| 0057 | mass docstring refresh + the verbatim-carry supersession | SUPERSEDED by THE DOCSTRING LIMIT RULED (2026-09-09) | DELETE |
| 0058 | hygiene batch: case-hydration redesign + cases/ package + transport/canopy fixes | BINDING | KEEP |
| 0059 | fetcher fold wave-11: copernicus absorption + the tier-2-able sweep (a fold-zero result) | DEAD - a zero-landing sweep; its DEFER verdicts were re-decided | DELETE |
| 0060 | opening a case restores its layers with its chat, in one gesture | BINDING | KEEP |
| 0061 | tier-3 hook wave: extend the fold across auth-free JSON/REST point-obs | DEAD - a per-source wave under the 0056 contract, which is the survivor | DELETE |
| 0062 | credentials collapse onto QgsAuthManager + resolver session cache | BINDING | KEEP |
| 0063 | chained-resolution mode: promote the 4x hook-ratchet into ONE declarative mode | BINDING | KEEP |
| 0064 | remaining ratchets: spend the two hook-ratchet flags + the free cleanups | DEAD - ratchet bookkeeping for a closed campaign | DELETE |
| 0065 | station-siblings: the wave-4 deferred station family folds onto the EXISTING phases | DEAD - a per-source wave; `station_timeseries` belongs to 0045's mechanism | DELETE |
| 0066 | arcgis-odd: the wave-11 ArcGIS deferrals fold onto the EXISTING hooks | SUPERSEDED by the 2026-09-09 G1 ESRI-on-GDAL ruling | DELETE |
| 0067 | zip/multi-file: the wave-6 ZIP-family deferral folds via WHOLE-OBJECT extract | SUPERSEDED by the 2026-09-09 zip fold | DELETE |
| 0068 | raster stragglers: the SLR MapServer-export pair folds; the rest STOP with named gaps | DEAD - its STOPs were re-decided | DELETE |
| 0069 | weather/GRIB: MRMS folds via a whole-object grib_object mode; the rest STOP with named gaps | DEAD - no `grib_object` shape is declared by any spec | DELETE |
| 0070 | Overpass-family: OSM QL fold via the http_json endpoint_fallback mirror chain | SUPERSEDED by the 2026-09-09 G3 OSMnx ruling | DELETE |
| 0071 | Keyed + misc leftovers: 5 folds via two no-op enablers; 9 honest STOPs | DEAD - per-source verdicts, all re-decided | DELETE |
| 0072 | Model bench: the arm3 NO_CALL empties are MODEL behavior, not provider artifacts (nemotron hypothesis REFUTED); local 8-9B ollama is not good enough | DEAD - its finding is carried by 0271 / 0301 / 0311 | DELETE |
| 0073 | LayerURI-envelope wave: the post-emit envelope hook (ADR 0056 reopened) | BINDING | KEEP |
| 0074 | River Overpass fold (NHDPlus leg dropped) + the generic library-delegate mode | BINDING | KEEP |
| 0075 | Delegate fast-follows + output shapes: fetch_3dep_extra folded, the two named ADR 0074 extensions built | SUPERSEDED by 0313 | DELETE after lifting the surviving clause (the payload-estimate field) into the router map |
| 0076 | Record-return output shape + socketed delegate_resolve: wfigs folded, HRRR/cluster-B re-scoped | BINDING | KEEP |
| 0077 | Per-source finishers: movebank folded (keyed CSV + composite Basic-Auth); fault/landcover/flood_extent re-STOP with sharpened residuals | DEAD - `fetch_movebank_tracks` and its family left the tree | DELETE |
| 0078 | Satellite family: slider_timestamps folded (record shape's first live-no-cache source); the animation cluster STOPs on the frames-list output ... | SUPERSEDED by 0087 / 0088 | DELETE |
| 0079 | Quick folds: firms_active_fire (keyed CSV http_json), noaa_sst (griddap raster mode), sentinel1_sar (stac_float + log10_db + coverage-select) | DEAD - per-source campaign log | DELETE |
| 0080 | STAC multi-asset RGB composite: the imagery trio folded (landsat / sentinel2 / naip), one composite mode | SUPERSEDED by the shared `stac_raster` executor | DELETE |
| 0081 | Finisher mechanisms: fetch_fault_sources folded (constant-cache two-tier + emptiness output-switch) | DEAD - the two mechanisms have no spec consumer | DELETE |
| 0082 | Landcover + flood-extent: the last two ADR-0077 finishers folded | DEAD - per-source finishers, since re-decided | DELETE |
| 0083 | Endgame sweep: HRRR-Zarr + FTW GeoParquet folds; population/nwis/buildings STOPs | DEAD - the STOPs were re-decided; only the HRRR hook remains | DELETE |
| 0084 | Trigger wave: lehd_jobs (join VALUES-hook) + buildings (sidecar-write) folds; nwis STOP | DEAD - `fetch_lehd_jobs` and the `join` transform left the tree | DELETE |
| 0085 | Post-merge wave: CDS library_delegate pair (ERA5 + GTSM) + the last flood-seam twin (NWIS) folded; JRC re-attempt STOP | DEAD - only the CDS hook remains | DELETE |
| 0086 | Raster-modes wave: JRC + SoilGrids folded (two new access modes + a colormap hook); topobathy STOP re-affirmed; the dead ERA5/GTSM credential ... | SUPERSEDED by the shared `stac_raster` executor | DELETE |
| 0087 | Animation wave 1: the frames-list output shape + SLIDER-stitch per-frame mode; goes/viirs SLIDER animations folded | BINDING | KEEP |
| 0088 | Animation wave 2: the netcdf_cf_object per-frame mode; goes_archive_animation + goes_active_fire folded; goes_satellite + glm STOPPED with refined ... | DEAD - its per-source STOPs were re-decided by 0111 | DELETE |
| 0089 | Topobathy fold wave: fetch_topobathy STOP re-affirmed and SHARPENED (the envelope-provenance gap is decisive) | SUPERSEDED by 0110 | DELETE |
| 0090 | DEM + STORM_TRACKS wave: fetch_dem and fetch_storm_tracks both STOP (named gate-level residuals) | SUPERSEDED by 0097 / 0111 | DELETE |
| 0091 | Gated cross-dataset DEM fallback (fetch_dem 3DEP -> Copernicus) | SUPERSEDED by 0289-0300 (the fallback ladders) | DELETE after lifting its revisit trigger into `REANALYZE_LEDGER.md` |
| 0092 | Approved folds: fetch_population (WorldPop) + fetch_glm_lightning (frames) | DEAD - the folds' products stand, not this record's constraint | DELETE |
| 0093 | LLM fed-surface dedupe: door envelope-shape boilerplate | DEAD - its subject, the engine-door envelope, was dissolved by 0094 | DELETE |
| 0094 | Engine-door dissolution: templates rejoin the tool-search surface | BINDING | KEEP |
| 0095 | Combined hygiene wave: north-star purge, composer characterization, CaMa deletion, NEXRAD reclassify | BINDING | KEEP |
| 0096 | fetch_dem fold: STOP (blocker 2 CLEARS; the source="copernicus" cross-tool leg is the decisive standing residual) | SUPERSEDED by 0097 | DELETE |
| 0097 | Cross-sibling dispatch seam + the fetch_dem fold | SUPERSEDED by the fallback-ladder migrate-all ruling (0290 / 0299) | DELETE |
| 0098 | Mesh layer, wave M1 (EXTRACT) | SUPERSEDED by the `workflows/mesh/` rebuild (om2d + reg_grid) | DELETE |
| 0099 | Mesh layer, wave M2 (GENERALIZE) | SUPERSEDED by the `workflows/mesh/` rebuild | DELETE |
| 0100 | Mesh layer, wave M3 (HECRAS WRITER + worker) | DEAD - the HEC-RAS mesher went with the purge; hecras grep = 0 | DELETE |
| 0101 | Oceanmesh wave (leg 1 bank fallback, leg 2 coastal_tin, leg 3 RiverMapper) | SUPERSEDED by the one mesh router; only om2d survives | DELETE |
| 0102 | Template input-provenance: wire the have-but-not-wired fetchers | SUPERSEDED by 0106 then 0231 | DELETE |
| 0103 | Daemon-hosted plugin repository, remote-mode cull, chat de-noise | BINDING | KEEP |
| 0104 | Live-drive bug-fix wave (six defects from remote driving) | DEAD - six defects, no rule; the code was rewritten by 0261-0279 | DELETE |
| 0105 | Composer dissolution wave | BINDING | KEEP |
| 0106 | Structured input provenance (provenance-chain WAVE 2) | BINDING | KEEP |
| 0107 | Two-mode INPUT_REQUIRED gate (review-before-run) | BINDING | KEEP |
| 0108 | pysheds terrain-hydrology primitives + reach-selection fix | BINDING | KEEP |
| 0109 | HEC-RAS engine landing (engine #11, template-first) | DEAD - HEC-RAS atticked; hecras grep = 0 in `trid3nt_server` + `workers` | DELETE |
| 0110 | The fetch-time provenance channel + the fetch_topobathy fold | BINDING | KEEP |
| 0111 | fetch_storm_tracks + fetch_goes_satellite folds (FETCHER FINALE WAVE 2) | DEAD - the storm-tracks leg was re-folded 2026-09-09 | DELETE |
| 0112 | fetch_noaa_nwm_streamflow fold (FETCHER FINALE ENDGAME -- the last coded data-fetcher) | SUPERSEDED by the 2026-09-09 library-fold closure | DELETE |
| 0113 | M4 QUADTREE TRUTH: the real SFINCS quadtree leg (cht_sfincs) | DEAD - SFINCS atticked; no `workflows/sfincs/` | DELETE |
| 0114 | !run CHAT INVOCATION: direct tool dispatch from the composer | BINDING | KEEP |
| 0115 | SCHISM feasibility spike (build + verification + mesh bridge + landing map) | DEAD - SCHISM atticked; schism grep = 0 | DELETE |
| 0116 | Remote streaming: layers register in place, no download | BINDING | KEEP |
| 0117 | ESRI Living Atlas as a discoverable, two-pool data population | BINDING | KEEP |
| 0118 | SCHISM engine landing (engine #12, barotropic tidal archetype) | DEAD - SCHISM atticked; schism grep = 0 | DELETE |
| 0119 | Charts move to a TUFLOW-Viewer-style bottom window | BINDING | KEEP |
| 0120 | S-tier template wave 1 (flood/hydraulics cluster) + template hygiene gate | DEAD - every subject atticked | DELETE |
| 0121 | S-tier template wave 2 (hazard cluster) -- triage outcome | DEAD - subjects atticked; its "hazard cluster" framing is banned by 0177 | DELETE |
| 0122 | Hazard easy-four build wave (ADR 0121 greenlit subset) | DEAD - subjects atticked | DELETE |
| 0123 | Hazard easy-four continuation (rows 2-4 of ADR 0122) | DEAD - subjects atticked | DELETE |
| 0124 | SWMM real-network family (network import + dual-drainage coupling) | DEAD - SWMM atticked; no `workflows/swmm/` | DELETE |
| 0125 | HEC-RAS archetypes: the levee-breach landing + the archetype triage | DEAD - HEC-RAS atticked; hecras grep = 0 | DELETE |
| 0126 | SCHISM candidates wave (schism_estuary_circulation + schism_coupled_waves): TRIAGE | DEAD - SCHISM atticked | DELETE |
| 0127 | HEC-RAS 2025 Beta headless spike: characterization + NO-GO-YET | DEAD - HEC-RAS atticked; its one durable finding is a memory fact, not a repo constraint | DELETE |
| 0128 | Published-deck runner: the cited-SWMM-example template family | DEAD - the SWMM deck runner atticked | DELETE |
| 0129 | HEC-RAS 2025 Beta: Linux native-SUBSTITUTION experiment (meshing verbs) | DEAD - HEC-RAS atticked | DELETE |
| 0130 | HEC-RAS 2025 Beta: THE PREPARE CRUX -- subgrid property tables run on Linux | DEAD - HEC-RAS atticked | DELETE |
| 0131 | schism_coupled_waves LANDS: the GOTM build leg resolved, SCHISM+WWM two-way coupling validated on Duck FRF | DEAD - SCHISM atticked | DELETE |
| 0132 | HEC-RAS: THE MUNCIE TRANSPLANT -- 2025-authored mesh + tables into the 6.x solver | DEAD - HEC-RAS atticked | DELETE |
| 0133 | HEC-RAS: the 2D geometry WRITER lands (OI-2) + the deck-skeleton triage | DEAD - the HEC-RAS geometry writer atticked | DELETE |
| 0134 | HEC-RAS: the pure-2D forcing reference obtained (OI-A discharged) + the precise remaining chain | DEAD - HEC-RAS atticked | DELETE |
| 0135 | HEC-RAS: the Boundary Condition Lines writer lands (link c3) + the pure-2D .bNN forcing is empirically discharged; the fresh-topology solve is the ... | DEAD - HEC-RAS atticked | DELETE |
| 0136 | HEC-RAS: the FRESH-TOPOLOGY solve probe -- a repo-authored 2D tessellation is ACCEPTED and SOLVED by the production 6.6 engines (ADR 0135 fused ... | DEAD - HEC-RAS atticked | DELETE |
| 0137 | HEC-RAS: the version-correct pure-2D reference is FOUND (Chippewa, dam-free) and the fresh carved topology SOLVES through a genuine pure-2D ... | DEAD - HEC-RAS atticked | DELETE |
| 0138 | HEC-RAS: the 2D-BC-line `/Event Conditions` schema is FOUND, DECODED, and re-authored by our own code -- the fresh carved topology now WETS ... | DEAD - HEC-RAS atticked | DELETE |
| 0139 | HEC-RAS flood_2d: the C# AuthorMesh worker + the pure-2D DECK COMPOSER land, and BOTH acceptances solve end-to-end -- a genuinely-new US AOI is ... | DEAD - HEC-RAS atticked | DELETE |
| 0140 | HEC-RAS flood_2d PROMOTED: the authoring worker image is built + the `hecras_flood_2d` template registers, backed by a fresh-AOI ... | DEAD - the template does not register; `EXPECTED_TEMPLATES` is 8 TELEMAC names | DELETE |
| 0141 | Landlab six-row diagnostic/knob template wave | DEAD - Landlab atticked; no `workflows/landlab/` | DELETE |
| 0142 | ELMFIRE sensitivity wave - 3 landed, 5 honest STOPs | DEAD - ELMFIRE atticked; elmfire grep = 0 | DELETE |
| 0143 | GeoClaw SWE+AMR knob templates - 2 landed, 6 triaged | DEAD - GeoClaw atticked; no `workflows/geoclaw/` | DELETE |
| 0144 | GeoClaw depth-COG revision - finest-AMR-wins + overland mask + grid-plan arg fix | DEAD - GeoClaw atticked | DELETE |
| 0145 | Landlab lake discrimination fix + overland conditioning opt-in | DEAD - Landlab atticked | DELETE |
| 0146 | Pelicun validation wave - 4 templates folding 7 rows + 2 test-only + 2 STOPs | DEAD - Pelicun atticked; no `workflows/pelicun/` | DELETE |
| 0147 | SWAN physics-scheme knobs + 2 CAND-S templates (folding 8 rows, 2 STOPs) | DEAD - SWAN atticked; swan grep = 0 | DELETE |
| 0148 | GeoClaw knob activation - stale-image rebuild + AMR window governs refinement | DEAD - GeoClaw atticked; the stale-image rule is a standing norm, not this record | DELETE |
| 0149 | OpenQuake epistemic logic-tree + UHS/multi-PoE folded into openquake_psha knobs | DEAD - OpenQuake atticked; no `workflows/openquake/` | DELETE |
| 0150 | GeoClaw AMR mesh as a first-class per-run product (raw grid emission) | DEAD - GeoClaw atticked | DELETE |
| 0151 | SWMM mechanism-comparison templates for the 12 CAND-S board rows | DEAD - SWMM atticked | DELETE |
| 0152 | SFINCS CAND-S rows: knob folds, not new templates | DEAD - SFINCS atticked | DELETE |
| 0153 | MODFLOW CAND-S rows: one package-validation template + a PRT STOP | DEAD - MODFLOW atticked; no `workflows/modflow/` | DELETE |
| 0154 | TELEMAC CAND-S rows: wind-stress knob fold, GAIA supply already-covered, cross-engine overlap doctrine, two honest STOPs | SUPERSEDED - the overlap doctrine is carried by the declaration substrate (0312 / 0314 / 0322) | DELETE |
| 0155 | GeoClaw CAND-S tail - Lagrangian particle + onshore-fgmax knob folds | DEAD - GeoClaw atticked | DELETE |
| 0156 | SCHISM CAND-S tail: one transport-validation template + HA STOP/DOC | DEAD - SCHISM atticked | DELETE |
| 0157 | HEC-RAS CAND-S tail: diffusion-wave equation-set knob + five triage STOPs | DEAD - HEC-RAS atticked | DELETE |
| 0158 | Strict worker spec parsers + offline-suite hermeticity (Atlas-14) | BINDING | KEEP |
| 0159 | SFINCS native quadtree mesh output + draw-a-geometry gate supply path | BINDING | KEEP |
| 0160 | Pelicun DL_calculation CLI harness + the two 0146 STOP rows land on it | DEAD - Pelicun atticked | DELETE |
| 0161 | ELMFIRE transient-weather + crown-fire machinery fronts | DEAD - ELMFIRE atticked | DELETE |
| 0162 | SFINCS wind timeseries + wind-drag curve knobs | DEAD - SFINCS atticked | DELETE |
| 0163 | MODFLOW PRT capture-zone + BUY/Henry: two V&V cases fold onto ADR 0153 | DEAD - MODFLOW atticked | DELETE |
| 0164 | OpenQuake scenario ground-motion field + earthquake secondary-perils front | DEAD - OpenQuake atticked | DELETE |
| 0165 | MODFLOW real-AOI georeferenced capture zone (wellhead protection) | DEAD - MODFLOW atticked | DELETE |
| 0166 | Capture-zone regional gradient from MEASURED heads | DEAD - MODFLOW atticked; its data seam survives as the 0297 / 0298 specs | DELETE |
| 0167 | MODFLOW advanced-package MVR/SFR front (Glover + Mover V&V gates) | DEAD - MODFLOW atticked | DELETE |
| 0168 | GeoClaw parametric-Holland storm-surge front | DEAD - GeoClaw atticked | DELETE |
| 0169 | TELEMAC WAQTEL O2 dissolved-oxygen sag front (`telemac_do_sag`) | SUPERSEDED by 0303 | DELETE |
| 0170 | HEC-RAS 1D steady / RasSteady front: machinery characterized, STOP on the missing steady reference deck | DEAD - HEC-RAS atticked | DELETE |
| 0171 | HEC-RAS 2D structure-authoring front: HDF schema mapped, STOP on the SA/2D-connection face-pairing frontier + the inert Muncie weir | DEAD - superseded by 0249, then atticked | DELETE |
| 0172 | HEC-RAS reference-fixture seed: real public decks in hand for both fronts; the missing-plan-HDF wall generalizes and survives | DEAD - HEC-RAS atticked | DELETE |
| 0173 | HEC-RAS plan-HDF skeleton: the h5py surgery is proven and reduced to practice, but the shared blocker is TWO artifacts not one; RasSteady walls a ... | DEAD - HEC-RAS atticked | DELETE |
| 0174 | HEC-RAS SA/2D connection gate CRACKED: the g09 mesh seeded, the Sayers Dam connection solves with nonzero weir flow, and the weir coefficient ... | DEAD - HEC-RAS atticked | DELETE |
| 0175 | Showcase-Case seeding through the product `!run` path | BINDING | KEEP |
| 0176 | SFINCS real quadtree run: cht_sfincs generator + native mesh, off the fixture | DEAD - SFINCS atticked | DELETE |
| 0177 | Category-taxonomy vocabulary audit - "hazard" is not the product identity | BINDING | KEEP |
| 0178 | SFINCS quadtree: real composer dispatch + coast-following refinement + native-mesh CRS | DEAD - SFINCS atticked | DELETE |
| 0179 | QGIS plugin zip: on-demand fresh-build endpoint | BINDING | KEEP |
| 0180 | Layer-emission audit: emitted layers are georeferenced map citizens | BINDING | KEEP |
| 0181 | order-dependent SFINCSSetupError reload flake: root fix + victim hardening | DEAD - the flake's victim and its root fix both left with SFINCS | DELETE |
| 0182 | OpenQuake shortlist batch: disaggregation + event-based PSHA + Vs30 A/B fold | DEAD - OpenQuake atticked | DELETE |
| 0183 | MODFLOW postprocess georef hardening: identity-affine fallback removed | DEAD - MODFLOW atticked; the honesty rule is stated by 0180 and the ladders | DELETE |
| 0184 | Landlab shortlist grind batch 2 (channel incision, chi-map, storm generator) | DEAD - Landlab atticked | DELETE |
| 0185 | GeoClaw shortlist grind batch 3 (Thacker V&V + fgout animation + front-away trio triage) | DEAD - GeoClaw atticked | DELETE |
| 0186 | GeoClaw fgout smooth-animation engine knob (LANDED) + Thacker V&V (corrected recipe, deferred) | DEAD - GeoClaw atticked | DELETE |
| 0187 | GeoClaw completion: fgout smooth-animation PROMOTION + Thacker V&V template (LANDED) | DEAD - GeoClaw atticked | DELETE |
| 0188 | HEC-RAS shortlist batch 4: the DW-vs-SWE inertial regression + the 2D stability-diagnostic sweep, on the fresh-AOI flood_2d surface | DEAD - HEC-RAS atticked | DELETE |
| 0189 | SCHISM shortlist batch 5: the parametric-JONSWAP wave-forcing knobs + the 3D baroclinic estuary template | DEAD - SCHISM atticked | DELETE |
| 0190 | final shortlist singles: TELEMAC rainfall, ELMFIRE Hirsch POC, SWAN nonstationary storm, SWMM RTK RDII | DEAD - 3 of 4 atticked; the TELEMAC rainfall row was re-decided by 0316 | DELETE |
| 0191 | spot-check fixes: TELEMAC mesh render, SWAN AOI bathymetry void, SCHISM baroclinic shoreline mesh + circulation | DEAD - 2 of 3 atticked; the mesh render is now `emission/mesh_display.py` | DELETE |
| 0192 | OceanMesh2D coastal meshing: standalone-first | SUPERSEDED by the one mesh router; om2d is one of two meshers | DELETE |
| 0193 | pysheds watershed coverage + watershed-first meshing | SUPERSEDED by 0316 | DELETE |
| 0194 | coastal water-edge re-mesh (OSM coastline; CUSP verdict) | SUPERSEDED - the water-edge prep folded into om2d | DELETE |
| 0195 | TELEMAC-2D rain-on-grid foundation (validation primitives + CN infiltration + precip/mesh verdicts) | SUPERSEDED by 0316 | DELETE |
| 0196 | TELEMAC-2D rain-on-grid build wave (mesh-acquisition step + runoff-path selector; worker/template/live-proof spec) | SUPERSEDED by 0316 | DELETE |
| 0197 | Coastal/watershed mesh proof-render vertical misalignment fix | DEAD - both meshers it fixed are atticked | DELETE |
| 0198 | Honesty-floor fixes: dev-tool-invoke error surfacing + shared watershed delineation | BINDING | KEEP |
| 0199 | HEC-RAS 2D rain-on-grid front + the TELEMAC-vs-HEC-RAS cross-engine comparison | DEAD - HEC-RAS atticked; no cross-engine comparison surface exists | DELETE |
| 0200 | Mesh as an optional user-supplied precondition (standalone builder + gate) | SUPERSEDED by 0322 (`Data.supplied()`) | DELETE |
| 0201 | Coastal water-edge narrow-pass connectivity + NHD retry (sandbox polish) | DEAD - subject atticked | DELETE |
| 0202 | Rain-on-grid replication vs the Coweeta gauge: STOPPED on a data coverage gap | SUPERSEDED by 0203 / 0204 | DELETE |
| 0203 | AORC precipitation + LTER/EDI record fetchers (the RoG replication unblock) | BINDING | KEEP |
| 0204 | Rain-on-grid replication on the Ball Creek fork (Coweeta), executed | SUPERSEDED - the template was re-authored by 0316 and the outlet re-decided by 0325 | DELETE |
| 0205 | HEC-RAS 2D rain-on-grid: the headless precip-interpolation decode + the hydrology residual | DEAD - HEC-RAS atticked | DELETE |
| 0206 | TELEMAC-2D rain-on-grid: TRUE time-varying hyetographs (RAINDEF=3 per-case FORTRAN) | BINDING | KEEP |
| 0207 | HEC-RAS 2D rain-on-grid: the preprocessing-shim hunt (the 2025 managed engine solves on Linux) | DEAD - HEC-RAS atticked | DELETE |
| 0208 | Mesh precondition gate: SCHISM adoption, SWAN honest-decline, full-results publishing | DEAD - both adopters atticked and the gate deleted | DELETE |
| 0209 | HEC-RAS 2025 rain-on-grid, productionized on the managed engine | DEAD - HEC-RAS atticked | DELETE |
| 0210 | HEC-RAS 2025 rain-on-grid: paper-style channel-refined mesh | DEAD - HEC-RAS atticked | DELETE |
| 0211 | generate_mesh mode=hecras: the refined RAS mesh as a standalone, consumable artifact | DEAD - the hecras mesher and its tests left with the purge | DELETE |
| 0212 | Mesh precondition gate: schism_tidal_hydro (coastal_tin) adoption | DEAD - SCHISM atticked and the gate deleted | DELETE |
| 0213 | Rain-on-grid fidelity ladder II: continuous soil-moisture store + channel-resolving mesh | SUPERSEDED by 0316 | DELETE |
| 0214 | Landlab groundwater front (GroundwaterDupuitPercolator) | DEAD - Landlab atticked | DELETE |
| 0215 | MODFLOW wellhead reeval: soil-derived K + kriged water table (data seams) | DEAD - MODFLOW atticked; the data seam survives as the 0297 / 0298 specs | DELETE |
| 0216 | TELEMAC GAIA v2 erodible-bed scour morphodynamics | SUPERSEDED - `modules/gaia.py` under the declarative templates is the live surface | DELETE |
| 0217 | SCHISM parametric-hurricane storm surge (standalone Holland-1980 sflux) | DEAD - superseded by 0219, then atticked | DELETE |
| 0218 | SWMM snowmelt (degree-day, rain-on-snow) + two-zone aquifer baseflow | DEAD - SWMM atticked | DELETE |
| 0219 | SCHISM PaHM surge on REAL Galveston geography (fixes the 0217 synthetic-shelf showcase) | DEAD - SCHISM atticked | DELETE |
| 0220 | OpenQuake site-model batch: discrete NEHRP site-class amplification fold | DEAD - OpenQuake atticked | DELETE |
| 0222 | Provenance-transparency audit of the tool surface (post surge-arc) | SUPERSEDED by 0223 | DELETE |
| 0223 | Provenance-transparency fix batch (audit 0222 remediation) | BINDING | KEEP |
| 0224 | Resolution doctrine: native default, explicit coarsening, sampled payload estimation, honest CUDEM fallback | BINDING | KEEP |
| 0225 | Declared resolutions: tools declare their valid ranges, out-of-range asks are quoted back | BINDING | KEEP |
| 0226 | GeoClaw Okada seafloor-deformation product + real-event tsunami source | DEAD - GeoClaw atticked; okada grep = 0 | DELETE |
| 0227 | Bathymetry-consuming templates surface their fetched topobathy as a Case input layer | SUPERSEDED by 0231 | DELETE |
| 0228 | MODFLOW UZT (vadose transport) + CSUB (delay/effective-stress upgrade) | DEAD - MODFLOW atticked | DELETE |
| 0229 | Deep-water rung: the ETOPO full column survives the 3DEP land ocean-fill on a rupture-scale topobathy fetch | SUPERSEDED - the rung became a declared row under `fallbacks/ladder.py` | DELETE |
| 0230 | Slab2 scenario source: the SCENARIO rung of the earthquake-source ladder | DEAD - slab2 grep = 0 | DELETE |
| 0231 | Input-layer parity: every fetched input that shapes a run surfaces as a Case layer | BINDING | KEEP |
| 0232 | resolve_resolution: the one resolution-resolve seam | BINDING | KEEP |
| 0233 | runs-prefix retention: reap raw solver scratch after a successful postprocess | BINDING | KEEP |
| 0235 | MODFLOW GWE (Groundwater Energy / heat transport) archetype family | DEAD - MODFLOW atticked | DELETE |
| 0236 | TOMAWAC spectral-wave engine: local-first physics proof + productionization recipe | DEAD - no TOMAWAC module and no registered template | DELETE |
| 0237 | ARTEMIS phase-resolving harbour-agitation engine: local-first physics proof + productionization recipe | SUPERSEDED - the physics proof stands as history; the template was re-authored declaratively (0314) | DELETE |
| 0238 | SFINCS SnapWave + nesting: substrate gate (binary-capable) + productionization STOP-recipe | DEAD - SFINCS atticked | DELETE |
| 0239 | ELMFIRE ember spotting: the barrier-jump question class + the spotting-knob-bounds trap | DEAD - ELMFIRE atticked | DELETE |
| 0240 | GAIA v3 multi-class graded sediment (grain sorting); cohesive-mud + 3D rows adjudicated | SUPERSEDED - `modules/gaia.py` + the scour / sediment-plume templates are the live surface | DELETE |
| 0241 | TELEMAC-3D stratified/3D-hydrodynamics engine leg: local-first physics proof + productionization recipe | SUPERSEDED - `telemac3d_stratified_flow` registers through the workflow skeleton (0312) | DELETE |
| 0242 | SCHISM ICM + SED3D/SED2D substrate gate: flags PRESENT (full-monty only), productionization STOP-recipe (targeted-variant image rebuild) | DEAD - SCHISM atticked | DELETE |
| 0243 | SWAN computational grid & nesting: binary gate (all four capable) + two-level nesting PHYSICS-PROVEN + scoped productionization recipes | DEAD - SWAN atticked | DELETE |
| 0244 | Emit-on-fetch: the render declaration IS the visualization intent | BINDING | KEEP |
| 0245 | TELEMAC-2D Structures + misc triage sweep (all STOP-RECIPE) | DEAD - a STOP-recipe sheet; no row landed and the module surface moved to the dico path | DELETE |
| 0246 | Triage sweep 2: GeoClaw boussinesq + MODFLOW PRT + HAZUS lifelines | DEAD - all three families atticked | DELETE |
| 0247 | HAZUS earthquake lifeline-network DL template | DEAD - Pelicun atticked | DELETE |
| 0248 | TOMAWAC + TELEMAC-3D coverage close-out (board reconciliation, one STOP) | DEAD - a reconciliation record for a coverage board the purge invalidated | DELETE |
| 0249 | HEC-RAS coverage wave: the 2025 engine is 2D-only (all 1D rows STAY STOP) but its headless structure solver + engine-side face-pairing SUPERSEDE ... | DEAD - superseded by 0250, then atticked | DELETE |
| 0250 | HEC-RAS 2025 2D structure-authoring seam: the StructureLayer authoring path is LIVE and the deck prepares+solves, but the beta wires ONLY Culverts ... | DEAD - HEC-RAS atticked | DELETE |
| 0251 | HEC-RAS 2025 2D culvert-through-embankment seam: PROVEN LIVE. The one drivable 2D structure ADR 0250 named (the Culvert, via CulvertBarrelLayer) ... | DEAD - HEC-RAS atticked | DELETE |
| 0252 | Landlab coverage clusters (Tectonics/Flexure, Vegetation/Ecohydrology) + SFINCS Infiltration adjudication. One landing: ... | DEAD - Landlab and SFINCS both atticked | DELETE |
| 0253 | Mixed-clusters triage sweep (NESTOR / ELMFIRE Suppression / SFINCS Wavemaker / HEC-RAS Water Quality). Four families adjudicated knob-or-STOP ... | DEAD - zero landings; all four families atticked | DELETE |
| 0254 | NESTOR channel-maintenance dredging (dig/dump on the GAIA erodible bed) | DEAD - no NESTOR template registers; only keyword support survives | DELETE |
| 0255 | Long-tail triage sweep (12 rows, 6 families) | DEAD - all subjects atticked | DELETE |
| 0256 | Knob-eligible trio from the 0255 sweep (SFINCS structures + ELMFIRE crown-fire V&V; GMPETable deferred) | DEAD - both landings atticked | DELETE |
| 0257 | GeoClaw Boussinesq (SGN) dispersive solver image wave | DEAD - GeoClaw atticked | DELETE |
| 0258 | MODFLOW gridgen in the worker image; DISV quad-refined backward PRT proven through-image | DEAD - MODFLOW atticked; `workers/` holds only `mesh` and `telemac` | DELETE |
| 0259 | TELEMAC-2D coastal tidal/surge substrate (LIQUID BOUNDARIES FILE + open-water domain) | SUPERSEDED by 0315 | DELETE |
| 0260 | SCHISM ICM + SED3D targeted binaries BAKED; both modules SOLVE through the image; substrate rows PROVEN, template registration is the remaining ... | DEAD - SCHISM atticked; the promised registration never happened | DELETE |
| 0261 | server.py refactor wave 1: package skeleton + errors/config extraction + dispatcher rename | SUPERSEDED by 0277 / 0278 | DELETE |
| 0262 | server-refactor wave 2: cloud-shaped-seam chop (aws-batch / TiTiler / Vertex / DynamoDB) | BINDING | KEEP |
| 0263 | server-refactor wave 3: interactions / styles / spatial extraction | SUPERSEDED - the styles half moved to 0313 then 0326 | DELETE after lifting the surviving clause (`server/{interactions,spatial}.py` are live) into the package map |
| 0264 | server-refactor wave 4: reuse shim / dispatch helpers / connection registry | BINDING | KEEP |
| 0265 | server-refactor finale: session state layer + turn wire plumbing | BINDING | KEEP |
| 0266 | server-refactor wave 6: severed-consumer chop (case-view snapshot + coldview) | DEAD - subjects gone; no standing constraint | DELETE |
| 0267 | server-refactor wave 7: severed-consumer chop, round 2 | DEAD - subjects gone; no standing constraint | DELETE |
| 0268 | server-refactor waves 8-9: notation sweep (docstrings/comments) | SUPERSEDED by THE DOCSTRING LIMIT RULED / COMMENTS (2026-09-09) | DELETE |
| 0269 | server-refactor wave 10: smell-to-code audit | SUPERSEDED by 0327, which closes its last standing condition by name | DELETE |
| 0270 | server-refactor wave 11: five feature-level cuts | DEAD - subjects gone | DELETE |
| 0271 | server-refactor wave 12: provider-neutral model-dispatch seam | BINDING | KEEP |
| 0272 | repo unnesting: server/src -> src (standard src-layout) | SUPERSEDED by 0274 | DELETE |
| 0273 | Confirm-gate collapse: declarative GateSpec, one generic engine | BINDING | KEEP |
| 0274 | flat layout: src/trid3nt_server -> trid3nt_server, services/workers -> workers | BINDING | KEEP |
| 0275 | flatten the last two single-child nestings: contracts/src -> contracts, qgis-plugin/trid3nt -> plugin | BINDING | KEEP |
| 0276 | chop the category routing layer; hardwire retrieval-enforce | BINDING | KEEP |
| 0277 | fragmentation: the agent/ namespace dies | BINDING | KEEP |
| 0278 | core dissolution: `server/_core.py` goes to zero | BINDING | KEEP |
| 0279 | phase E: standards sweep of workers/plugin/contracts/scripts + the two 0278 relocations | DEAD - no standing constraint of its own | DELETE |
| 0280 | emit-on-solve: the append-only `outputs.json` seam (wave 1 foundation) | BINDING | KEEP |
| 0281 | emit-on-solve: the GeoClaw + SWAN S-class legs | DEAD - GeoClaw and SWAN both atticked | DELETE |
| 0282 | emit-on-solve: the M-class legs (SWMM + Landlab overland) | DEAD - SWMM and Landlab both atticked | DELETE |
| 0283 | emit-on-solve: the L-class TELEMAC native-mesh leg | BINDING | KEEP |
| 0284 | emit-on-solve: the MODFLOW transport leg (L-class) | DEAD - MODFLOW atticked | DELETE |
| 0285 | law 9: the `consequence` tag + refuse-in-auto for invented physics | BINDING | KEEP |
| 0286 | emit-on-solve: the L-class SCHISM native-mesh leg (Option B) | DEAD - SCHISM atticked | DELETE |
| 0287 | emit-on-solve: the HEC-RAS leg (L-class, TWO agent-side producers) | DEAD - HEC-RAS atticked | DELETE |
| 0288 | emit-on-solve: the ELMFIRE leg (derived-from-ToA burned-extent frames) | DEAD - ELMFIRE atticked | DELETE |
| 0289 | fallback ladders, wave F1: the module, the router kwargs, the one gate, the SWAN bathymetry ladder | BINDING | KEEP |
| 0290 | fallback ladders, wave F1b: the adversarial-panel fixes + NATE's migrate-all ruling | BINDING | KEEP |
| 0291 | fallback ladders, wave F1c: the merge consumes its own paint, exemption semantics, honest walker refusals | BINDING | KEEP |
| 0292 | fallback ladders, wave F1d: a fault is not a verdict, every share is measured, the ladder is not the whole account | BINDING | KEEP |
| 0293 | fallback ladders, wave F1e: the tool entrypoint learns the ladder's own errors | BINDING | KEEP |
| 0294 | the publish_manifest FRAME collapse (narrow scope: frames die, the metrics carrier lives) | BINDING | KEEP |
| 0295 | the out-of-process SWMM lane dies; the supervisor stops eating the metrics pointer | DEAD - the SWMM lane atticked | DELETE after lifting the surviving clause (the supervisor must not eat the metrics pointer) into the solver map |
| 0296 | GeoClaw Manning siblings: split-by-domain NLCD wiring | DEAD - GeoClaw atticked; the Manning table was re-decided 2026-09-09 | DELETE |
| 0297 | staged-dataset fetchers; groundwater recharge lands, aquifer thickness parks | BINDING | KEEP |
| 0298 | Zell & Sanford CONUS surficial groundwater: depth to water lands, saturated thickness is recovered from the model | BINDING | KEEP |
| 0299 | fallback ladders, wave F2: the audit migrates, and one of its rows was wrong | BINDING | KEEP |
| 0300 | fallback ladders, wave F2b: the honesty riders | BINDING | KEEP |
| 0301 | the Anthropic Messages API adapter (MODEL_PROVIDER=anthropic) | BINDING | KEEP |
| 0302 | the tool registry as an MCP surface (v1, stdio) | DEAD - `trid3nt_server/mcp_server.py` and its tests / docs are deleted | DELETE |
| 0303 | Declarative library v1 + the do_sag migration | SUPERSEDED by 0322 | DELETE after lifting the surviving clause (the six doors and the plan value) into 0322 / the runtime map |
| 0304 | The FORM and DRAW cards (declarative wave 2) | BINDING | KEEP |
| 0305 | The river_dye migration + the live-run harness (declarative wave 3) | BINDING | KEEP |
| 0306 | The generalization checkpoint: one SWMM and one MODFLOW template | DEAD - both checkpoint templates went with the purge; neither registers | DELETE |
| 0307 (`0307-swmm-campaign-wave-a.md`) | (untitled fragment) SWMM wave-A post-review corrections | DEAD - an untitled post-review fragment duplicating the 0307 number; SWMM atticked | DELETE (resolves the 0307 collision) |
| 0307 (`0307-swmm-engine-campaign-wave-a.md`) | SWMM engine campaign, wave A: the standalone solve templates | DEAD - SWMM atticked four days later | DELETE (resolves the 0307 collision) |
| 0308 | TELEMAC bed-COG manifest gap closed + publish_raster_input_cog existence check | SUPERSEDED by 0317 - the in-worker bed COG was deleted and the bed leaves the container | DELETE |
| 0309 | telemac_do_sag / telemac_river_dye: event_time + cycle-pinned discharge provenance | BINDING | KEEP |
| 0310 | temporal transforms v1: `.resample()` / `.normalize()` on the Data declaration | BINDING | KEEP |
| 0311 | Per-model context budget: runtime window discovery + one client-side trim seam | BINDING | KEEP |
| 0312 | The workflow skeleton (template method), hardened on a two-template cohort | BINDING | KEEP |
| 0313 | Emission is automatic: the publish mechanism moves to emission/, the tool dies | BINDING | KEEP |
| 0314 | The static plan, and the style contract | SUPERSEDED by 0326 - no `styles.yaml` exists in the tree | DELETE after lifting the surviving clause (the static plan, the run journal) into the runtime map |
| 0315 | The coastal split, the resolution label, and the context slot | BINDING | KEEP (AMEND: the coastal-split half died with the coastal template; `workflows/runtime/resolution.py` and the context slot are what survives) |
| 0316 | The catchment shape, and the mesh front the composer was hiding | BINDING | KEEP |
| 0317 | The fetch migration: the open-water bed leaves the container | BINDING | KEEP |
| 0318 | The reach family migration: the meshed river becomes the visible river | BINDING | KEEP |
| 0319 | Rerun-with-overrides: a run derives from a run, and the setter dies | BINDING | KEEP |
| 0320 | The spec format | BINDING | KEEP (AMEND: `docs/model/` is the suite-checked structure surface; 0320 governs HTML specs, and `system-uml.html` is the overlap) |
| 0321 | The suite re-baseline after the purge and the chained domain | SUPERSEDED by 0322 (its own first line says so) | DELETE after lifting its revisit trigger into `REANALYZE_LEDGER.md` |
| 0322 | The DATA class body, binding refusals, and first-class parking | BINDING | KEEP |
| 0323 | The suite re-baseline after the test cull | SUPERSEDED - a measurement superseded by the next measurement | DELETE |
| 0324 | The code-exec box | BINDING | KEEP |
| 0325 | The catchment outlet holds a derived rating curve | BINDING | KEEP |
| 0326 | the preset family: four kinds, and presentation declared where the data is | BINDING | KEEP |
| 0327 | one store, one scheme: a layer reference is an s3:// uri | BINDING | KEEP |
| - (`afk-ledger-2026-08-24.md`) | AFK design-decision ledger, opened 2026-08-24 (CLOSED 2026-08-25) | NOT A DECISION RECORD - a closed AFK ledger | MOVE out of `docs/decisions/` |
| - (`README.md`) | Design decisions - the folder index and its convention | NOT A DECISION RECORD - its "never rewrite history" convention is replaced by the delete-outright ruling | REWRITE (new convention + an index regenerated to the 106 survivors) |

## Verdict rollup

| verdict | rows |
|---|---|
| BINDING | 106 |
| SUPERSEDED | 66 |
| DEAD | 154 |
| not a decision record (`README.md`, `afk-ledger-2026-08-24.md`) | 2 |
| chop set (SUPERSEDED + DEAD) | 220 |

Eight records carry a surviving clause that is lifted before the file goes (0005, 0022, 0055,
0075, 0263, 0295, 0303, 0314); four carry a revisit trigger that goes to `REANALYZE_LEDGER.md`
first (0049, 0050, 0091, 0321); five BINDING records are amended in place (0004, 0015, 0023,
0315, 0320) and `README.md` is rewritten. Both `0307` files are DEAD, which resolves the
numbering collision by deletion.

files listed: 328 / files read: 328 / rows written: 328
