# Engine Coverage Inventory

Deterministic API-surface / keyword-space coverage for every simulation engine wired into the
TRID3NT local stack, plus one green-lit-but-not-yet-integrated engine (Delft3D FM). Every count in
this document comes from an introspection command, grep, or the engine's own dictionary/manual file
that was actually run against the pinned build in `venvs/agent` (or the pinned worker container
image); no count is an estimate. The exact commands are reproduced per engine for auditability.

Two numbers are reported for each engine, computed identically so they are comparable:

- **raw %** = distinct API points our code touches (`direct + nested`) / the package's public-callable
  or keyword-space denominator.
- **capability %** = covered capability areas (partial counts 0.5) / total capability areas, where the
  areas are taken from the engine's own docs/module structure.

`direct` = a registered LLM-reachable tool exposes roughly this capability as a tool argument the
model can pick. `nested` = the point is used internally inside a tool/workflow/worker but is not
individually reachable by the LLM.

---

## 1. Summary

| Engine | Packages / deck | API points (denom + method) | Direct | Nested | Raw % | Capability % | Top-3 uncovered capabilities |
|---|---|---|---:|---:|---:|---:|---|
| SFINCS | hydromt-sfincs==1.2.2 + sfincs.inp keyword space | 230 (introspect+dico) | 9 | 52 | 26.5 | 54.2 | structures/drainage/storage; observation points/lines; hydromt's own setup_tiles |
| SWMM | pyswmm==2.1.0 + swmm-api==0.4.74 | 1167 (introspect) | 4 | 29 | 2.83 | 20.83 | LID / green-infrastructure controls; swmm_api macros (GIS/plot/compare); native .rpt + hotstart |
| MODFLOW | flopy (wraps MF6) | 309 (introspect, depth-2) | 0 | 40 | 12.9 | 35.0 | GWE heat transport; all legacy pre-MF6 engines; plot/export/PEST |
| TELEMAC-2D | telemac2d.dico (opentelemac 9.0.0) | 376 (dico) | 10 | 42 | 13.83 | 45.8 | TIDES; hydraulic structures; advanced turbulence closures |
| GeoClaw | clawpack.geoclaw==5.14.0 (batch-only) | 392 (introspect+rundata) | 6 | 21 | 6.89 | 16.67 | fgout time-history/animation; storm-surge wind/pressure; multilayer + Boussinesq solvers |
| Landlab | landlab==2.11.0 | 215 (registry+introspect) | 2 | 8 | 4.65 | 30.0 | fluvial landscape evolution; hillslope diffusion; sediment-transport networks |
| SWAN | swan (Fortran, .swn deck) | 54 (manual) | 7 | 10 | 31.5 | 40.0 | nested boundary spectra (BOUNDNEST1/2/3); vegetation/mud/ice/turbulence/Bragg; wave setup (SWAN->SFINCS coupling) |
| OpenQuake | openquake.engine 3.25.1 (CLI/deck) | 170 (registry+dico) | 7 | 16 | 13.5 | 37.5 | event_based/scenario/disaggregation calculators; risklib (exposure/fragility/vuln); the entire in-process Python API (CLI-only) |
| Pelicun | pelicun 3.9.0 | 12 (introspect) | 0 | 0 | 0.0 | 5.0 | DamageModel/LossModel engine; DLCalculationAssessment pipeline; pelicun.uq |
| Delft3D FM | dfm (green-lit, NOT integrated) | n/a | 0 | 0 | 0.0 | 0.0 | entire engine unintegrated -- no deck author, no worker, no tool |

Notes on the summary numbers:

- SFINCS and GeoClaw denominators are two summed axes (a Python build/config API plus the underlying
  solver's own keyword / rundata surface); see their sections.
- SWMM's very low raw % is dominated by swmm-api's `SwmmInput` auto-generating ~55 per-section property
  accessors that inflate the denominator; its capability % (20.8) is the honest read.
- MODFLOW's `direct = 0` is real: flopy is never LLM-reachable at the API level; all 40 covered points
  live inside the internal deck-build/postprocess chain behind 16 high-level composer tools.
- OpenQuake and Landlab are pip-installed but driven as deck/CLI engines; OpenQuake is invoked purely
  through the `oq` CLI (`oq engine --run job.ini`), never the Python API.
- Delft3D FM is listed at 0/0 for completeness only -- it is green-lit but has zero touchpoints in
  `server/src/trid3nt_server` or `services/workers`.

---

## 2. Per-engine detail

### 2.1 SFINCS

**Packages:** `hydromt_sfincs` (hydromt-sfincs==1.2.2) + the SFINCS engine's own `sfincs.inp` keyword space.
**Denominator: 230** = 147 (hydromt_sfincs Python API) + 83 (`sfincs.inp` keywords).
**Direct 9 / Nested 52 -> Raw 26.5% | Capability 54.2%.**

Denominator method (two lanes summed):
- LANE A (Python API, introspected in `venvs/agent`): 1 (`SfincsModel` class) + 56 (public methods
  defined directly on `SfincsModel`, i.e. `SfincsModel.__dict__` entries excluding those inherited from
  `hydromt.models.model_grid.GridModel`/`Model`) + 53 (unique public functions/classes across
  `hydromt_sfincs.{plots,quadtree,regulargrid,sfincs_input,subgrid,utils,workflows}`, deduped by
  defining module, restricted to `__module__` starting with `hydromt_sfincs` to exclude vendored
  re-exports) + 37 (public methods on the 5 helper classes: `QuadtreeGrid`=9, `RegularGrid`=14,
  `SubgridTableQuadtree`=2, `SubgridTableRegular`=7, `SfincsInput`=5) = **147**.
- LANE B (`sfincs.inp` keyword space): **83**, from `SfincsInput().__dict__` -- hydromt_sfincs's own
  canonical machine-readable encoding of every `sfincs.inp` keyword it can read/write.

Capability areas:

| Area | Status | Note |
|---|---|---|
| Grid setup + active/boundary mask (setup_grid[/_from_region], setup_dep, setup_mask_active/bounds; mmax/nmax/dx/dy/x0/y0/rotation/epsg/utmzone) | covered | driven inside `build_sfincs_model`'s generated setup_config/grid/dep/mask YAML (nested); grid keywords also directly manipulated in the quadtree path `services/workers/sfincs_deckbuilder/entrypoint.py`. |
| Subgrid tables (setup_subgrid; sbgfile) | covered | emitted when `options.enable_subgrid` set (`sfincs_builder.py`). |
| Roughness + infiltration (setup_manning_roughness, setup_cn_infiltration[_with_ks], setup_constant_infiltration; manning*/rgh_lev_land/qinf) | covered | setup_manning_roughness + setup_constant_infiltration are DIRECT (`set_sfincs_parameters` args manning_land/manning_sea/qinf); setup_cn_infiltration nested; setup_cn_infiltration_with_ks unused. |
| Meteo forcing: precip/wind/pressure (setup_precip_forcing[_from_grid], setup_wind_forcing[_from_grid], setup_pressure_forcing_from_grid) | covered | all 5 emitted nested via `_emit_surge_forcing_blocks`/setup_config. |
| Waterlevel/discharge/river forcing (setup_waterlevel_forcing/_bnd_from_mask, setup_river_inflow/_outflow, setup_discharge_forcing[_from_grid]; bzsfile/bndfile/srcfile/disfile) | partial | waterlevel_forcing, river_inflow, discharge_forcing used nested (coastal surge path); waterlevel_bnd_from_mask, river_outflow, discharge_forcing_from_grid never referenced. |
| Structures + storage (setup_structures, setup_drainage_structures, setup_storage_volume; thdfile/wfpfile/whifile/wtifile/wstfile) | uncovered | no grep hits anywhere in server/ or services/workers/. |
| Observation points/lines (setup_observation_points, setup_observation_lines; obsfile/crsfile) | uncovered | no grep hits. |
| Tiles / web-map output (setup_tiles) | uncovered | web tiling runs through our own TiTiler/COG postprocess (`_raster_postprocess`), not hydromt's setup_tiles. |
| Model I/O: read/write config/grid/forcing/geoms/states/subgrid/results/raster/vector | partial | read()/write()/write_grid()/write_config() used; all format-specific read_*/write_* unused -- `diagnostics/sfincs.py` re-parses raw `sfincs_map.nc` netCDF instead. |
| Plotting (plot_basemap, plot_forcing) | uncovered | no grep hits. |
| Advanced physics/numerics via setup_config passthrough (theta/alpha/huthresh/advection/baro/crsgeo/viscosity/dtmax/stopdepth, wind-drag cdnrb/cdwnd/cdval) | covered | dedicated `workflows/physics_registry.py` maps an advanced_physics override surface onto these exact keywords, emitted nested through setup_config. |
| Low-level grid/subgrid classes + workflows/utils helpers (QuadtreeGrid, RegularGrid, SubgridTable*, hydromt_sfincs.workflows.*, hydromt_sfincs.utils.*) | partial | QuadtreeGrid instantiated + .read()/.crs used only in a dev spike (`services/workers/sfincs_snapwave_spike/validate_deck.py`); RegularGrid, SubgridTable*, and every workflows/utils helper unused -- our code reimplements bathymetry/forcing/tiling independently. |

Notable uncovered: setup_structures / setup_drainage_structures / setup_storage_volume (weirs, drainage,
storage); setup_observation_points / setup_observation_lines; setup_tiles (hydromt's own web tiles);
plot_basemap / plot_forcing; setup_waterlevel_bnd_from_mask, setup_river_outflow,
setup_discharge_forcing_from_grid; setup_cn_infiltration_with_ks; most format-specific I/O
(read_config/forcing/geoms/grid/results/states/subgrid, write_forcing/geoms/raster/states/subgrid/vector);
RegularGrid, SubgridTableQuadtree/Regular classes; all of `hydromt_sfincs.workflows.*` and
`hydromt_sfincs.utils.*` (tiling/merge/discharge/bathymetry/curvenumber/flwdir/landuse) -- zero direct
imports anywhere; ~46 unused `sfincs.inp` keywords (tspinup, t0out, dtmapout, dthisout, dtrstout,
trstout, dtwnd, pavbnd, gapres, stopdepth, btfilter, viscosity, dtmax, inputformat, outputformat,
mskfile, indexfile, cstfile, bzifile, bwvfile, bhsfile, btpfile, bwdfile, bdsfile, bcafile, corfile,
inifile, amufile, amvfile, ampfile, amprfile, wndfile, precipfile, obsfile, crsfile, thdfile,
manningfile, rstfile, wfpfile, whifile, wtifile, wstfile).

Counting commands:
```
venvs/agent/bin/python -c "import hydromt_sfincs; print(hydromt_sfincs.__version__)"   # -> 1.2.2
venvs/agent/bin/python -c "import hydromt_sfincs; print([n for n in dir(hydromt_sfincs) if not n.startswith('_')])"
# inspect.getmembers(SfincsModel) filtered to SfincsModel.__dict__[name] is not None (own-defined) and isfunction/staticmethod/classmethod/property -> 56
# for sm in [plots,quadtree,regulargrid,sfincs_input,subgrid,utils,workflows]: dir(getattr(hydromt_sfincs,sm)) filtered to isfunction/isclass and __module__.startswith('hydromt_sfincs'), deduped (module,name) -> 53
# own-defined-method filter on QuadtreeGrid/RegularGrid/SubgridTableQuadtree/SubgridTableRegular/SfincsInput -> 9+14+2+7+5 = 37
venvs/agent/bin/python -c "from hydromt_sfincs.sfincs_input import SfincsInput; print(len(SfincsInput().__dict__))"   # -> 83
grep -rn 'hydromt_sfincs' --include=*.py server/src/trid3nt_server services/workers | grep -v __pycache__
grep -n 'register_tool' server/src/trid3nt_server/tools/simulation/set_sfincs_parameters.py server/src/trid3nt_server/workflows/model_flood_scenario.py
# grep -lw for each of the 56 methods + 83 keywords across the sfincs source set, each hit manually verified for false positives
```

Key notes: only `set_sfincs_parameters` exposes individual hydromt_sfincs capabilities as tool args
(setup_manning_roughness/setup_constant_infiltration + manning_land/manning_sea/qinf). Everything else
is deterministically driven by `build_sfincs_model` inside `sfincs_flood` (bbox/forcing-spec
in, deck out -- no LLM choice), hence nested. `services/workers/_sfincs_build/deck.py` is a
near-byte-identical older duplicate of `workflows/sfincs_builder.py` and was counted once.
`services/workers/sfincs_quadtree_spike/ref/*.py` is a DIFFERENT vendored SFINCS Python codebase (its own
local QuadtreeGrid/SfincsInput via relative imports) and was correctly excluded. Biggest honest gaps:
hydromt's own result readers are never used (diagnostics re-parses `sfincs_map.nc` itself), and the entire
workflows/utils helper surface is reimplemented rather than imported.

---

### 2.2 SWMM

**Packages:** pyswmm==2.1.0; swmm-api==0.4.74 (swmm-toolkit==0.17.0 is pyswmm's binary `.out` reader
backend, out of this lane's two-package scope).
**Denominator: 1167** = 350 (pyswmm) + 817 (swmm-api).
**Direct 4 / Nested 29 -> Raw 2.83% | Capability 20.83%.**

Denominator method (per-package venv introspection, summed):
- pyswmm: recursively imported every public submodule under `pyswmm.__path__` (`pkgutil.iter_modules`),
  counted module-level public functions/classes (`obj.__module__==modname`) plus one-level-deep public
  methods/staticmethod/classmethod/property; `pyswmm.reader` excluded (raises
  `OutReaderNotImplementedYet` at class-body evaluation, cannot be imported at all). Result **350**.
- swmm-api: same walk via `pkgutil.walk_packages`, skipping any submodule whose dotted path has an
  underscore-prefixed component; 2 optional-dependency submodules (plotting_map_bokeh,
  plotting_map_plotly) failed to import (bokeh/plotly absent) and were excluded. Result **817**.

Capability areas:

| Area | Status | Note |
|---|---|---|
| Simulation execution / time control (pyswmm.Simulation: context manager, iteration, current_time, step_advance, callbacks, SimulationPreConfig) | partial | we open `Simulation(inp_path)` and drive `for _ in sim` reading `.current_time`; step_advance, before/after-step callbacks, SimulationPreConfig untouched. |
| Live runtime Node/Link/Subcatchment objects (mid-run inspection) | partial | only `Nodes(sim)[name].depth`; Links/Subcatchments runtime objects and every other Node property (head, volume, flooding, inflow, statistics, total_inflow) untouched. |
| LID controls / groups (green infrastructure) | uncovered | pyswmm.lidcontrols/lidgroups/lidunits/lidlayers never imported. |
| RainGage authoring vs runtime | partial | swmm_api.RainGage authors the deck's rain-gage section; pyswmm.raingages live-inspection module never touched. |
| System-wide runtime statistics (pyswmm.system) | uncovered | never imported. |
| Binary .out results reading (pyswmm.output.Output + swmm_api.output_file) | partial | Output.times/nodes/pollutants/node_attribute/link_attribute/node_series used; Output.subcatch_series/subcatch_attribute/link_series/system_series/*_result and essentially all of swmm_api.output_file untouched. |
| Input-file (.inp) section authoring (swmm_api.input_file.sections.*) | partial | a working quasi-2D + WQ subset used (Storage, Outfall, Conduit, Orifice, CrossSection, SubCatchment, SubArea, Coverage, 3 Infiltration types, RainGage, TimeseriesData, BuildUp/LandUse/Pollutant/WashOff); Curves, Patterns, Controls, Pumps, Weirs, Streets, Inlets, LID, Snowpacks, Aquifers/Groundwater, Transects, Tags, Dividers untouched. |
| Input-file macros: GIS export/import, plotting, model diff, cut/split/combine, curve-simplify, tag mgmt (swmm_api.input_file.macros.*) | uncovered | none imported anywhere. |
| Report-file (.rpt) parsing (swmm_api.report_file) | uncovered | we hand-roll a regex `.rpt` parser in `tools/simulation/diagnostics/swmm.py` instead. |
| Hotstart file (.hsf warm-start) (swmm_api.hotstart_file) | uncovered | never referenced; every run is cold. |
| Run-orchestration wrappers (swmm_api.run_swmm.run_epaswmm/run_pyswmm/run_swmm_toolkit/run_temporary) | uncovered | we call pyswmm.Simulation directly. |
| Model analysis / validation (swmm_api.analyse_model, analyse_simulation) | uncovered | input_summary, instability_index, gis_export never imported. |

Notable uncovered: pyswmm LID controls/groups/units/layers (entire green-infrastructure BMP family);
pyswmm.system; pyswmm live Node properties beyond `.depth` and Links/Subcatchments runtime objects;
swmm_api.input_file.macros.* (GIS/plotting/compare/split/combine/tags); swmm_api.report_file (native
`.rpt` parser); swmm_api.hotstart_file; swmm_api.run_swmm.* wrappers; swmm_api.analyse_model /
analyse_simulation; Output.subcatch_series/subcatch_attribute/link_series/system_series/*_result and all
of swmm_api.output_file.

**BUG FOUND (aside, not scored):** `services/workers/_swmm_postprocess/postprocess.py` calls
`out.period_count`, which does not exist on `pyswmm.output.Output` (`hasattr` = False) -- an
AttributeError silently swallowed by a broad `except` returning None. This worker postprocess path is
presently dead/non-functional whenever real `.out` data is read.

Counting commands:
```
source venvs/agent/bin/activate && python3 -c "import pyswmm, swmm_api; print(pyswmm.__file__, swmm_api.__file__)"
python3 -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version) for d in m.distributions() if 'swmm' in (d.metadata['Name'] or '').lower()]"  # pyswmm 2.1.0, swmm-api 0.4.74, swmm-toolkit 0.17.0
# introspect_pyswmm.py: pkgutil.iter_modules over pyswmm.__path__, module-level funcs/classes + one-level methods, dedupe -> 350 (pyswmm.reader unimportable, excluded)
# introspect_swmm_api.py: pkgutil.walk_packages skipping underscore-prefixed paths -> 817 (plotting_map_bokeh/plotly excluded, deps absent)
grep -rn 'pyswmm' server/src/trid3nt_server services/workers --include='*.py' | grep -v __pycache__
grep -rn 'swmm_api' server/src/trid3nt_server services/workers --include='*.py' | grep -v __pycache__
grep -n 'swmm' server/src/trid3nt_server/tools/__init__.py   # registered: run_swmm (door), swmm_urban_flood (template), set_swmm_parameters
python3 -c "import pyswmm.output as o; print(hasattr(o.Output,'period_count'))"   # False -> the postprocess bug
```

Key notes (engine-door refactor, SWMM slice): registered tools = the `run_swmm`
DOOR (`tools/simulation/swmm/run_swmm/`, read-only concierge) + the
`swmm_urban_flood` TEMPLATE (`workflows/swmm/urban_flood/urban_flood.py`, pure
orchestration, no pyswmm/swmm_api import; engine=swmm, tier=template,
pool-excluded) and `set_swmm_parameters`
(`tools/simulation/swmm/set_swmm_parameters/`). All 12 pyswmm touches (Simulation, Nodes, Node.depth,
Output.times/nodes/pollutants/node_attribute/link_attribute/node_series) live only in
`workflows/swmm_mesh_builder.py`, `workflows/postprocess_swmm.py`, and `services/workers/swmm/*` +
`_swmm_postprocess/postprocess.py` -> all nested. `set_swmm_parameters.py` directly touches
swmm_api.SwmmInput (class), SwmmInput.read_file (the read_inp_file alias -- confirmed
`read_inp_file = SwmmInput.read_file`), SwmmInput.write_file, and InfiltrationHorton (isinstance) = 4
direct. The other 17 section-object classes used to author the quasi-2D deck are nested. Raw 2.83% reads
harsh mainly because `SwmmInput` alone auto-generates ~55 per-section property accessors as separate
"methods" in the denominator -- the capability metric (20.8%) is the honest read: a real, live-proven
quasi-2D deck-build + headless-solve + depth/WQ postprocess pipeline exists, it just never touches
LID/green-infrastructure, live runtime stats beyond node depth, swmm_api's GIS/plotting/compare macros,
hotstart, or its native report parser.

---

### 2.3 MODFLOW

**Package:** `flopy` (wraps USGS MODFLOW 6 / `mf6` binary).
**Denominator: 309** (depth-2 cut).
**Direct 0 / Nested 40 -> Raw 12.9% | Capability 35.0%.**

Denominator method: flopy is huge (mf6 alone auto-generates ~100 per-package input classes), so per the
engine-lane instruction the count stops at MODULE DEPTH 2 -- for each of flopy's 16 non-underscore
top-level submodules (datbase, discretization, export, mbase, mf6, mfusg, modflow, modflowlgr, modpath,
mt3d, pakbase, pest, plot, seawat, utils, version) count public classes+functions visible via
`dir(flopy.<submodule>)` (does NOT separately walk `flopy.mf6.modflow.mfgwfdis`-style implementation
modules). Per-submodule: datbase 4, discretization 3, export 2, mbase 13, mf6 111, mfusg 21, modflow 50,
modflowlgr 2, modpath 14, mt3d 12, pakbase 11, pest 6, plot 7, seawat 3, utils 50, version 0. Sum = **309**.

Capability areas:

| Area | Status | Note |
|---|---|---|
| MF6 GWF (dis/npf/wel/riv/chd/drn/ghb/rch/rcha/sfr/sto/oc/buy/csub/ic/ims/tdis/gwfgwt + MFSimulation) | covered | 20 distinct `flopy.mf6.ModflowGwf*` + MFSimulation/ModflowTdis/ModflowIms across `gwt_adapter.py` and 16 composer tools (drawdown, dewatering, water-budget, MAR, ASR, wetland, saltwater-intrusion, river-seepage). |
| MF6 GWT (solute/species transport: adv/dsp/mst/ssm/src/ic/oc) | covered | 7 `flopy.mf6.ModflowGwt*` for spill-plume and N-species transport chains. |
| MF6 PRT (native particle tracking: dis/fmi/mip/oc/prp) | covered | 5 `flopy.mf6.ModflowPrt*` for capture-zone / wellhead-protection backward tracking. |
| MF6 GWE (energy/heat transport) | uncovered | zero `ModflowGwe*` usage -- a real flopy 3.10 family never touched. |
| Classic MODFLOW-2005/NWT/USG legacy (flopy.modflow, flopy.mfusg, flopy.modflowlgr) | uncovered | all decks are MF6-native. |
| Legacy transport: MT3D/MT3D-USGS + SEAWAT (flopy.mt3d, flopy.seawat) | uncovered | superseded by MF6 GWT/BUY. |
| MODPATH legacy particle tracking (flopy.modpath) | uncovered | we use native MF6 PRT instead. |
| Utilities: binary output readers + grid objects (flopy.utils, flopy.discretization) | partial | HeadFile (heads/concentration/CSUB z-displacement), CellBudgetFile (flows), get_modflow (test-only) covered; StructuredGrid/VertexGrid/UnstructuredGrid never touched -- our deck-builder manages grid geometry itself. |
| Export & plotting (flopy.export shapefile/netcdf/vtk, flopy.plot PlotMapView/PlotCrossSection) | uncovered | all raster/vector postprocessing via our own rasterio/numpy in `postprocess_modflow.py`. |
| PEST parameter estimation (flopy.pest) | uncovered | no calibration workflow. |

Notable uncovered: flopy.mf6 GWE; all legacy engines (flopy.modflow/mfusg/modflowlgr/mt3d/seawat/modpath
-- MF6-only); flopy.plot + flopy.export; flopy.pest; flopy.discretization grid classes; and within the
covered MF6-GWF family several package types stay untouched: ModflowGwfhfb (barrier), ModflowGwfmaw
(multi-aquifer well), ModflowGwflak (lake), ModflowGwfuzf (UZF), ModflowGwfvsc (viscosity),
ModflowGwfmvr/mvt (water mover), ModflowGnc (ghost-node correction), and the ModflowUtl* auxiliary
packages (obs/ts/tas/spc) -- the "covered" verdict is family-level (37 of 111 mf6-namespace depth-2
members actually used), not exhaustive.

Counting commands:
```
venvs/agent/bin/python -c "import flopy, pkgutil; print([m.name for m in pkgutil.iter_modules(flopy.__path__)])"
# for each of 16 top-level submodules: importlib the module, keep dir(mod) members that are isclass/isfunction and not _-prefixed -> per-module counts summed to 309
venvs/agent/bin/python -c "import flopy,inspect; print([a for a in dir(flopy.mf6) if not a.startswith('_') and (inspect.isclass(getattr(flopy.mf6,a)) or inspect.isfunction(getattr(flopy.mf6,a)))])"  # 111 mf6 depth-2 members
grep -rln 'flopy' --include='*.py' server/src/trid3nt_server services/workers | grep -v __pycache__
# regex r'\bflopy(?:\.[A-Za-z_][A-Za-z0-9_]*){2,}' over 5 production + 8 test files, reduced to <mod>.<attr> depth-2, deduped -> 39 production + 1 test-only = 40
grep -n 'run_modflow_job|set_modflow_parameters|...' server/src/trid3nt_server/tools/categories.py  # 16 registered MODFLOW-family tools under hazard_modeling
# zero hits: grep 'UcnFile|flopy.discretization|StructuredGrid|VertexGrid|UnstructuredGrid|flopy.plot|flopy.export|flopy.pest|flopy.mt3d|flopy.seawat|flopy.modpath|flopy.mfusg|flopy.modflowlgr|flopy.modflow\b|ModflowGwe'
```

Key notes: 16 registered MODFLOW-family tools (all in `categories.py` hazard_modeling): run_modflow_job,
set_modflow_parameters, run_model_groundwater_contamination_scenario, run_model_contamination_affected_fields,
run_river_seepage_job, run_model_river_seepage_scenario, run_model_sustainable_yield_scenario,
run_model_mine_dewatering_scenario, run_model_regional_water_budget_scenario, run_model_mar_scenario,
run_model_asr_scenario, run_model_wetland_hydroperiod_scenario, run_model_multi_species_scenario,
run_model_capture_zone_scenario, run_model_wellhead_protection_scenario, run_model_saltwater_intrusion_scenario.
`covered_direct = 0` because flopy itself is never LLM-reachable at the API level -- all 40 covered depth-2
points live inside the internal deck-build/postprocess chain (`services/workers/modflow/gwt_adapter.py`,
`workflows/postprocess_modflow.py`, `workflows/modflow_mesh.py`, `set_modflow_parameters.py`'s lazy
`import flopy`); the LLM only ever sees the 16 tool signatures. Capability (35%, 3 of 10 areas fully
covered: MF6 GWF/GWT/PRT) is the honest signal -- we exercise the entire modern MF6 stack but touch none
of flopy's legacy engines, GWE, or plotting/export/PEST layers.

---

### 2.4 TELEMAC-2D

**Deck:** `telemac2d.dico` (opentelemac v9.0.0, from the `trid3nt-local/telemac:latest` image at
`/opt/conda/opentelemac/sources/telemac2d/telemac2d.dico`). No Python API surface counted -- deck-driven.
**Denominator: 376** T2D keyword entries.
**Direct 10 / Nested 42 -> Raw 13.83% | Capability 45.8%.**

Denominator method: extracted the dico from the local image, counted keyword blocks 4 independent ways,
all agreeing at **376**: `grep -c '^NOM = '` (French name), `grep -c '^NOM1 = '` (English name),
`grep -c '^MNEMO = '` (fortran variable), `grep -c '^NIVEAU = '` (keyword-level marker). Capability areas
= the dico's own 12 top-level RUBRIQUE1 (English) sections (source-derived taxonomy, not invented).

Capability areas (RUBRIQUE1 sections):

| Area (kw count) | Status | Note |
|---|---|---|
| TRACERS (21) | covered | 7/21: NUMBER OF TRACERS, NAMES OF TRACERS, INITIAL/PRESCRIBED TRACERS VALUES, VALUES AT THE SOURCES, COEFFICIENT FOR DIFFUSION OF TRACERS, SCHEME FOR ADVECTION OF TRACERS -- this IS the tool's core (dissolved dye/contaminant); every essential single-tracer keyword wired. |
| TIDAL FLATS INFO (7) | covered | 4/7: TIDAL FLATS, OPTION FOR TREATMENT OF TIDAL FLATS, TREATMENT OF NEGATIVE DEPTHS, H CLIPPING -- wetting/drying functionally complete. |
| HYDRO (93) | partial | 11/93: boundary Q/stage, point-source discharge, friction, SW equations, linear-system treatment; weirs, rain/evap, wind stress, density/salinity, Coriolis untouched. |
| COMPUTATION ENVIRONMENT (75) | partial | 11/75: file I/O wiring, title, printout cadence; parallel processing, mesh-checking, binary/restart formats, vector length untouched. |
| NUMERICAL PARAMETERS (59) | partial | 9/59: solver choice/accuracy/iteration cap, advection scheme/type, SUPG, mass-lumping, continuity correction, depth/velocity implicitation; most limiter/preconditioner/matrix-storage options unexposed. |
| COUPLING (24) | partial | 3/24: COUPLING WITH + GAIA/WAQTEL STEERING FILE (sediment/decay); SISYPHE, TOMAWAC/wave, ice untouched. |
| PARTICLE TRANSPORT (24) | partial | 4/24: MAXIMUM NUMBER OF DROGUES, PRINTOUT PERIOD FOR DROGUES, ASCII DROGUES FILE, OIL SPILL STEERING FILE; most drogue config untouched. |
| GENERAL PARAMETERS (19) | partial | 2/19: TIME STEP, DURATION; gravity, density, water temp, Coriolis left at defaults. |
| TURBULENCE (17) | partial | 1/17: VELOCITY DIFFUSIVITY only (single constant-eddy-viscosity lever); k-epsilon/Smagorinsky untouched. |
| TIDES (23) | uncovered | 0/23 -- no tidal forcing (expected: river dye-plume tool, not coastal). |
| HYDRAULIC STRUCTURES (9) | uncovered | 0/9 -- no weirs/culverts/gates. |
| INTERNAL (5) | uncovered | 0/5 -- internal/debug keywords, expected untouched. |

capability_pct = (2 covered + 7 partial x 0.5 + 3 uncovered) / 12 = 45.8%.

Notable uncovered: TIDES (23 kw, no coastal/tidal forcing); HYDRAULIC STRUCTURES (9 kw, no
weir/culvert/gate); TURBULENCE closures beyond one constant eddy-viscosity knob (16 of 17); zone-based
spatially-varying friction (FRICTION DATA FILE, explicitly out-of-scope per `set_telemac_parameters.py`);
COUPLING limited to GAIA + WAQTEL (no SISYPHE/TOMAWAC/ice); PARALLEL PROCESSORS / mesh-checking / restart
(PREVIOUS COMPUTATION FILE) untouched.

Counting commands:
```
docker run --rm --entrypoint sh trid3nt-local/telemac:latest -c 'find / -name telemac2d.dico'
docker run --rm --entrypoint sh trid3nt-local/telemac:latest -c 'cat /opt/conda/opentelemac/sources/telemac2d/telemac2d.dico' > telemac2d.dico
grep -c '^NOM = '    telemac2d.dico   # 376
grep -c '^NOM1 = '   telemac2d.dico   # 376
grep -c '^MNEMO = '  telemac2d.dico   # 376
grep -c '^NIVEAU = ' telemac2d.dico   # 376
# python: split dico into per-keyword blocks, extract NOM1 + RUBRIQUE1, build nom1->rubrique1 map + per-area totals
# for each of 376 NOM1 strings, substring-grep against services/workers/telemac/* + server tools/simulation/{run_telemac_tool,set_telemac_parameters,diagnostics/telemac}.py + workflows/{run_telemac,postprocess_telemac,model_river_dye_release_scenario,physics_registry}.py
# manual Read of author_deck() in services/workers/telemac/telemac_river_dye_build.py to enumerate every literal 'KEYWORD = value' line -> 52, all exact-matched against the 376 NOM1 list
```

Key notes: numerator = 52 distinct T2D keywords, all verified as literal lines emitted by `author_deck()`
in `services/workers/telemac/telemac_river_dye_build.py` (the sole `.cas` author), including the base
hydro/tracer/numerics block plus oil/decay(WAQTEL)/sediment(GAIA) coupling-activation appendices.
`set_telemac_parameters.py` touches exactly 2 of those 52 (LAW OF BOTTOM FRICTION, FRICTION COEFFICIENT).
`diagnostics/telemac.py` parses solver LISTING text + completion.json (run-health/mass-balance) -- zero
keyword coverage. Registered tools (engine-door refactor, TELEMAC slice - name flip): the `run_telemac`
DOOR (`tools/simulation/telemac/run_telemac/`, read-only concierge) + the `telemac_river_dye` TEMPLATE
(`workflows/telemac/river_dye/river_dye.py`, async, 26 params, direct; engine=telemac, tier=template,
pool-excluded; was the old `run_telemac` engine tool) and `set_telemac_parameters`
(`tools/simulation/telemac/set_telemac_parameters/`, direct); `read_run_diagnostics` is a shared 5-engine
dispatcher whose telemac parser is nested.
Direct/nested split of the 52: 10 direct (DURATION<-sim_duration_s, FRICTION COEFFICIENT, LAW OF BOTTOM
FRICTION, VELOCITY DIFFUSIVITY, COEFFICIENT FOR DIFFUSION OF TRACERS, PRESCRIBED FLOWRATES<-source_q_m3s,
and the 4 substance-class activation keywords COUPLING WITH / WAQTEL STEERING FILE / GAIA STEERING FILE /
OIL SPILL STEERING FILE), 42 nested (fixed pipeline plumbing). GAIA/WAQTEL/oil-spill sub-modules have
their own separate dico files, not counted here. Raw (13.8%) undercounts because the dico is dominated by
coastal/tidal/turbulence/structure keywords irrelevant to a river dye-plume tool; capability (45.8%) is
the honest signal that the one hazard family this tool targets is substantively wired.

---

### 2.5 GeoClaw

**Package:** `clawpack.geoclaw` (clawpack==5.14.0, pinned in `services/workers/geoclaw/Dockerfile`).
**BATCH-ONLY -- NOT installed in `venvs/agent`;** Fortran+Python live only in the worker container.
**Denominator: 392** = 304 (public callables) + 88 (rundata/setrun params).
**Direct 6 / Nested 21 -> Raw 6.89% | Capability 16.67%.**

Denominator method (two axes, both against clawpack==5.14.0 matching the Dockerfile pin, run against the
build extracted from a docker containerd overlay snapshot with a host py3.11 matching the venv ABI, plus
matplotlib/pandas/scipy on PYTHONPATH so all 26 public submodules import):
- AXIS A (public callables): `inspect.getmembers()` over all 26 public `clawpack.geoclaw` submodules
  (`pkgutil.iter_modules` recursed one level into datatools/multilayer/surge), module-level public
  functions + classes + own-`__dict__` methods one level deep: 163 functions + 36 classes + 105 methods
  = **304**.
- AXIS B (rundata/setrun params): instantiated all 15 classes in `clawpack.geoclaw.data` and summed
  `len(instance.attributes())` per class = **88**.

Capability areas:

| Area | Status | Note |
|---|---|---|
| Core rundata/deck authoring (geoclaw.data: GeoClawData/RefinementData/TopographyData/QinitData/DTopoData/FGmaxData + unused FrictionData/SurgeData/MultilayerData/BoussData/GridData1D/BoussData1D/ForceDry/FGoutData) | partial | `setrun_builder.py` sets 18/88 rundata attrs across 6/15 data classes; 9 of 15 classes entirely untouched. |
| Topography/DEM ingestion (topotools.Topography + get_topo/fetch_topo_url/crop/read/plot) | partial | `entrypoint.py` uses only Topography.set_xyZ + .write to emit topotype-3 ASCII from a rasterio-reprojected DEM; get_topo, fetch_topo_url, create_topo_func, read, crop, plot + 10 others unused. |
| Seismic source / dtopo generation (dtopotools: Fault/SubFault/DTopography + CSVFault/UCSBFault/SiftFault/TensorProductFault/SubdividedPlaneFault) | partial | maketopo.py does a single-subfault synthetic Okada source; multi-subfault, triangular, dynamic-slip, plotting all unused. |
| fgmax fixed-grid max-value monitoring (fgmax_tools.FGmaxGrid) | partial | only rectangular point_style=2 authored; adjust/read_output/interp_dz/bounding_box/transect styles unused -- readback is hand-rolled numpy in `_geoclaw_postprocess`. |
| fgout fixed-grid full time-history output (fgout_tools: FGoutGrid/FGoutFrame/netcdf I/O) | uncovered | `postprocess_geoclaw.py` explicitly: "NO clawpack import here, only numpy.loadtxt" -- animation frames come from hand-parsed fort.q. |
| Storm surge modeling (surge.storm.Storm + SurgeData, Holland/CLE wind, ATCF/HURDAT/IBTrACS) | uncovered | the "surge" scenario is a v0.1 fallback applying only a flat geo_data.sea_level offset. |
| Multilayer shallow water (multilayer.data.MultilayerData, multilayer.plot) | uncovered | single-layer only. |
| Boussinesq dispersive solver (BoussData/BoussData1D) | uncovered | not referenced. |
| Regression/quality testing (geoclaw.test.GeoClawRegressionTest, surge.quality) | uncovered | our pytest suites test the deck strings directly. |
| Native visualization (geoplot, kmltools, plotfg, multilayer.plot, surge.plot) | uncovered | rendering/COG hand-rolled outside clawpack. |
| Geodesy/units utilities (util.py haversine/bearing/gctransect/fetch_noaa_tide_data, units.py convert) | uncovered | our geodesy math is hand-rolled. |
| Data conversion/fix tools (datatools.fixdata/iotools, most2geoclaw, etopotools, marching_front) | uncovered | not referenced. |

Notable uncovered: fgout_tools time-history/animation pipeline (we hand-parse fort.q instead); storm-surge
wind/pressure field construction (Holland/CLE, ATCF/HURDAT/IBTrACS); multilayer shallow water; Boussinesq
dispersive solver; multi-subfault / finite-fault seismic sources (SiftFault/UCSBFault/CSVFault/
TensorProductFault/dynamic slip -- only a single synthetic Okada SubFault authored); native GeoClaw
visualization (geoplot/kmltools/plotfg) and regression harness.

Counting commands:
```
find / -maxdepth 6 -iname '*clawpack*'   # located the docker overlay snapshot's clawpack==5.14.0
PYTHONPATH=<snapshot site-packages> /home/nate/.local/bin/python3.11 -c "import clawpack.geoclaw; print(clawpack.geoclaw.__file__)"
/home/nate/.local/bin/python3.11 -m venv scratch_venv && scratch_venv/bin/pip install matplotlib pandas scipy   # optional deps geoclaw submodules import
# geoclaw_callables.py: inspect.getmembers + pkgutil.iter_modules over 26 submodules -> 163 funcs + 36 classes + 105 methods = 304
# geoclaw_rundata.py: instantiate all 15 clawpack.geoclaw.data classes, sum len(inst.attributes()) -> 88
grep -rn 'clawpack' server/src/trid3nt_server --include=*.py   # zero real imports (only disclaiming comments)
grep -rn '^import clawpack\|^from clawpack\|from clawpack\.' server/src/trid3nt_server services/workers --include=*.py
# -> 1 real import (services/workers/geoclaw/entrypoint.py: from clawpack.geoclaw import topotools) + 2 rendered-string imports in setrun_builder.py (dtopotools / fgmax_tools) landing in generated maketopo.py/setrun.py
grep -n 'geoclaw' server/src/trid3nt_server/tools/__init__.py   # sole registered tool: run_geoclaw_inundation
```

Key notes: GeoClaw is architected as a BATCH-ONLY, deliberately clawpack-FREE authoring layer --
`setrun_builder.py` is documented as "deliberately does NOT import clawpack" and every function is a
"PURE string render -- unit-testable with NO clawpack import." Actual clawpack.geoclaw API usage is
almost entirely INDIRECT: it lives inside Python source TEXT our code renders (maketopo.py:
dtopotools.SubFault/Fault; setrun.py: fgmax_tools.FGmaxGrid + the geo_data/refinement/topo/qinit/dtopo
attribute sets) that only executes when the worker later runs `make .output`. The one REAL immediate
import is `entrypoint.py`'s `from clawpack.geoclaw import topotools` for DEM->topotype-3 staging.
Postprocess is deliberately hand-rolled with no clawpack import. The single registered tool
`run_geoclaw_inundation` exposes the whole capability as one atomic call; direct=6 are the named kwargs
(manning_n, sea_level_m, extra_topo_uris, tsunami_dtopo_uri, fault_strike/dip/rake/depth_km,
fgmax_arrival_tol_m) that map ~1:1 onto a covered rundata attr or dtopotools/fgmax_tools class; nested=21
are internal glue.

---

### 2.6 Landlab

**Package:** `landlab==2.11.0` (`services/workers/landlab`; installed in `venvs/agent`).
**Denominator: 215** = 87 registered Components + 128 RasterModelGrid public methods (depth 1).
**Direct 2 / Nested 8 -> Raw 4.65% | Capability 30.0%.**

Denominator method: `landlab.components.COMPONENTS` (the package's own component registry) has 87 entries;
`RasterModelGrid` has 128 public methods at depth 1 (our code exclusively builds RasterModelGrid, never
HexModelGrid/VoronoiDelaunayGrid/NetworkModelGrid). Properties (185, e.g. at_node/status_at_node/
number_of_nodes) and class constants (BC_NODE_IS_CLOSED) were excluded from the methods-only denominator
for an apples-to-apples comparison. 87 + 128 = **215**.

Capability areas:

| Area | Status | Note |
|---|---|---|
| Grid construction + field bookkeeping (RasterModelGrid + add_field/add_zeros/at_node/at_link/status_at_node) | covered | `component_chain._build_grid` builds RasterModelGrid from the DEM, sets closed-boundary nodata via status_at_node/BC_NODE_IS_CLOSED, add_field/add_zeros populate all input fields. |
| Flow routing + drainage-network (FlowAccumulator, FlowDirectorD8/DINF/MFD, DepressionFinderAndRouter) | covered | FlowAccumulator with configurable flow_director (D8 default, DINF/MFD via advanced_physics, live end-to-end) + depression_finder=DepressionFinderAndRouter. |
| Slope stability / landslide hazard (LandslideProbability, BedrockLandslider, MassWastingRunout, HeightAboveDrainageCalculator) | partial | LandslideProbability is the flagship `analysis='landslide_probability'` path; BedrockLandslider and MassWastingRunout unused. |
| Surface-water hydrology / overland flow (OverlandFlow, Kinwave*, GroundwaterDupuitPercolator, SoilMoisture, SoilInfiltrationGreenAmpt, PotentialEvapotranspiration) | partial | OverlandFlow (de Almeida) drives `analysis='overland_flow'`; Kinwave, groundwater/soil-moisture/PET unused. |
| Landscape evolution / fluvial erosion-deposition (FastscapeEroder, StreamPowerEroder, Space, GravelBedrockEroder, ErosionDeposition, SedDepEroder, ChiFinder, SteepnessFinder, HackCalculator, DrainageDensity, ChannelProfiler) | uncovered | none wired. |
| Hillslope diffusion + weathering (LinearDiffuser, DepthDependentDiffuser/TaylorDiffuser, TaylorNonLinearDiffuser, PerronNLDiffuse, ExponentialWeatherer, TransportLengthHillslopeDiffuser) | uncovered | landslide chain derives its own infinite-slope FoS in numpy. |
| Sediment transport + river networks (NetworkSedimentTransporter, GravelRiverTransporter, LateralEroder, RiverFlowDynamics, BedParcelInitializer*, SedimentPulser*) | uncovered | none. |
| Tectonics + flexure (NormalFault, Flexure, Flexure1D, ListricKinematicExtender, Lithology/LithoLayers, FractureGridGenerator) | uncovered | none. |
| Ecohydrology, climate, vegetation (Vegetation, VegCA, SpeciesEvolver, PrecipitationDistribution, Radiation, FireGenerator, CarbonateProducer, TidalFlowCalculator) | uncovered | rainfall is a plain uniform mm/hr pulse in numpy. |
| Visualization + IO/serialization (imshow_grid family, grid.save/load/to_netcdf/from_netcdf/to_dict/from_dict/to_json, native_landlab io) | uncovered | worker writes fields via rasterio GeoTIFFs directly. |

Notable uncovered: FastscapeEroder/StreamPowerEroder/Space (fluvial evolution); LinearDiffuser/
TaylorNonLinearDiffuser (hillslope diffusion); NetworkSedimentTransporter/GravelRiverTransporter
(sediment routing); Flexure/NormalFault (tectonics); Vegetation/VegCA/SpeciesEvolver (ecohydrology);
GroundwaterDupuitPercolator/SoilMoisture/PET (subsurface hydrology); BedrockLandslider/MassWastingRunout
(coupled runout); imshow_grid/imshowhs_grid; grid.to_netcdf/from_netcdf; HexModelGrid/
VoronoiDelaunayGrid/NetworkModelGrid.

Counting commands:
```
source venvs/agent/bin/activate && python3 -c "import landlab; print(landlab.__file__, landlab.__version__)"
python3 -c "import landlab.components as comp; print(len(comp.COMPONENTS))"   # -> 87
python3 -c "from landlab import RasterModelGrid; import inspect; m=inspect.getmembers(RasterModelGrid); print(len([n for n,v in m if not n.startswith('_') and (inspect.isfunction(v) or inspect.ismethod(v))]))"   # -> 128
grep -n 'run_landlab' server/src/trid3nt_server/tools/__init__.py
grep -noE 'from landlab(\.[a-zA-Z_]+)? import [A-Za-z_, ]+' services/workers/landlab/component_chain.py
grep -noE 'grid\.[a-zA-Z_]+' services/workers/landlab/component_chain.py | sort -u
python3 -c "import inspect; from landlab.components.flow_accum.flow_accumulator import FlowAccumulator; print(inspect.getsource(FlowAccumulator.__init__))"   # confirmed string flow_director resolves to FlowDirectorD8/DINF/MFD
```

Key notes: exactly one registered tool -- `run_landlab_susceptibility`
(`tools/simulation/run_landlab_tool.py`), a thin dispatcher that never imports landlab itself. All real
landlab API usage lives in `services/workers/landlab/component_chain.py` (the numerical core run on the
solver worker); `workflows/run_landlab.py` and `postprocess_landlab.py` never import landlab. Covered
points (10): components FlowAccumulator, LandslideProbability, OverlandFlow, DepressionFinderAndRouter,
FlowDirectorD8, FlowDirectorDINF, FlowDirectorMFD (7, all nested except LandslideProbability/OverlandFlow
which are DIRECT since they are the tool's own `analysis` selector values) + grid methods add_field,
add_zeros, map_max_of_node_links_to_node (3, nested). FlowDirectorDINF/MFD count as covered (not just
present as strings) because FlowAccumulator._add_director resolves the string to the real component and
the advanced_physics['flow_director'] lever is wired live. Engine is proven live: `data/runs/` and
`data/minio/trid3nt-runs/` hold completed run dirs with landlab_field.tif / landlab_result.json /
completion.json. The two exposed analyses (landslide susceptibility, overland flow) are genuinely two of
Landlab's ~10 major domains, each pulling in only the minimum component chain; the other 8 are untouched.

---

### 2.7 SWAN

**Deck:** `swan` (TU Delft, Fortran-90, GPL-3.0 command-driven; ASCII `.swn` keyword file run via
swanrun/swan.exe). No Python package.
**Denominator: 54** documented command keywords.
**Direct 7 / Nested 10 -> Raw 31.5% | Capability 40.0%.**

Denominator method: taken verbatim from the official SWAN User Manual "List of available commands" page
(swanmodel.sourceforge.io/online_doc/swanuse/node20.html). Fetched raw HTML (curl), stripped tags, counted
every top-level command in the manual's own 10 categories (a)-(j): (a) Start-up 4; (b) Computational grid
2; (c) Input fields 4; (d) Boundary/initial 6; (e) Physics 19; (f) Numerics 2; (g) Output locations 7;
(h) Output write/plot 6; (i) Intermediate output 1; (j) Lock-up 3. Total = **54** (corroborated by a
separate WebFetch summary pass over the same page). Capability areas = the manual's own (a)-(j) groupings.

Capability areas:

| Area | Status | Note |
|---|---|---|
| (a) Start-up: PROJECT, SET, MODE, COORD | covered | all 4 emitted; MODE is LLM-direct (stationary/nonstationary), PROJECT/SET/COORD fixed. |
| (b) Computational grid: CGRID, READGRID | partial | CGRID emitted (direct via bbox + n_dir/n_freq/freq range); READGRID (curvilinear/unstructured) unused -- REGULAR only. |
| (c) Input fields: INPGRID, READINP, WIND, ICE | partial | INPGRID/READINP for BOTTOM (always) + WIND (gridded ERA5, optional via wind_uri); standalone uniform WIND and ICE unused. |
| (d) Boundary/initial: BOUND, BOUNDSPEC, BOUNDNEST1/2/3, INITIAL | partial | only parametric BOUND SHAPE JONSWAP + BOUNDSPEC SIDE...PAR (LLM-direct hs/tp/dir/spread/side); BOUNDNEST1/2/3 and INITIAL explicitly deferred. |
| (e) Physics: GEN1/2/3, WCAP, QUAD, BREAKING, FRICTION, TRIAD, VEGETAT, MUD, SICE, TURBULE, BRAGG, LIMITER, OBSTACLE, SETUP, DIFFRAC, SURFBEAT, OFF | partial | 5/19: GEN3 (always-on), FRICTION/BREAKING/TRIAD (LLM-direct toggles), OFF (auto quad-off when no wind). WCAP only in a docstring comment. Vegetation/mud/ice/turbulence/Bragg/limiter/obstacles/setup/diffraction/infragravity unused. |
| (f) Numerics: PROP, NUMERIC | uncovered | neither ever emitted; relies on SWAN defaults. |
| (g) Output locations: FRAME, GROUP, CURVE, RAY, ISOLINE, POINTS, NGRID | uncovered | no named output-location subsetting; always full COMPGRID via BLOCK. |
| (h) Output write/plot: QUANTITY, OUTPUT, BLOCK, TABLE, SPECOUT, NESTOUT | partial | only BLOCK, hardcoded HSIGN/RTP/DIR (not LLM-tunable); TABLE/SPECOUT/NESTOUT/QUANTITY/OUTPUT unused. |
| (i) Intermediate output: TEST | uncovered | never emitted. |
| (j) Lock-up: COMPUTE, HOTFILE, STOP | partial | COMPUTE (LLM-direct via mode/sim_duration_s/time_step_s/output_frames) + STOP always emitted; HOTFILE (restart) unused. |

capability_pct = (1 covered + 6 partial x 0.5 + 3 uncovered) / 10 = 40.0%.

Notable uncovered: BOUNDNEST1/2/3 (nested boundary spectra from coarse SWAN/WAM/WAVEWATCH III -- explicitly
deferred in `swan_contracts.py`/`deck_builder.py`); WCAP (mentioned only in a comment); VEGETAT/MUD/SICE/
TURBULE/BRAGG (5 advanced dissipation processes); SETUP (wave-induced set-up, the flagged later
SWAN->SFINCS coupling seam); OBSTACLE (sub-grid structures, relevant to the overtopping/seawall use
case); DIFFRAC/SURFBEAT; PROP/NUMERIC; all 7 named output-location commands; TABLE/SPECOUT/NESTOUT/
QUANTITY/OUTPUT/TEST; READGRID/uniform WIND/ICE/INITIAL/GEN1/GEN2/HOTFILE.

Counting commands:
```
find /home/nate/Documents/trid3nt-local -iname '*swan*' -not -path '*/node_modules/*' -not -path '*/.git/*' | sort
curl -s https://swanmodel.sourceforge.io/online_doc/swanuse/node20.html -o scratch/swan_node20.html
python3 -c "import re; t=re.sub('<[^>]+>',' ',open('scratch/swan_node20.html').read()); print(re.sub(r'\s+',' ',t))"   # ground-truth 54-command list with (a)-(j) groupings
grep -n -i swan server/src/trid3nt_server/tools/__init__.py   # registered: run_swan (door), swan_wave_field (template)
# for kw in <all 54 manual keywords>: grep -rn (word/quote-boundary) across tools/workflows/services/workers/swan_contracts.py; every hit manually triaged
# Read services/workers/swan/deck_builder.py (the sole .swn author, 692 lines) to enumerate every 'KEYWORD = value' line -> 17
```

Key notes: 17 of 54 commands genuinely emitted, all inside `services/workers/swan/deck_builder.py`:
PROJECT, SET, MODE, COORD, CGRID, INPGRID (BOTTOM + optional WIND), READINP (BOTTOM + optional WIND),
GEN3, OFF (QUAD), FRICTION, BREAKING, TRIAD, BOUND (SHAPE JONSWAP), BOUNDSPEC, BLOCK, COMPUTE, STOP.
One engine template `swan_wave_field` (`workflows/swan/wave_field/wave_field.py`, was `run_swan_waves`) behind the read-only `run_swan` door (engine-door refactor, SWAN slice) -- other `swan` grep
hits are narrative cross-references. 7 DIRECT commands driven by named params (bbox, mode,
boundary_hs_m/tp_s/dir_deg/spread_deg/side, n_dir/n_freq/freq_low_hz/freq_high_hz, sim_duration_s/
time_step_s/output_frames, friction/breaking/triads, cross-checked against `SwanRunArgs` in
`contracts/swan_contracts.py`): MODE, CGRID, FRICTION, BREAKING, TRIAD, BOUNDSPEC, COMPUTE. The other 10
(PROJECT, SET, COORD, INPGRID, READINP, GEN3, OFF, BOUND, BLOCK, STOP) are hardcoded/derived deck mechanics
in the worker (mesh_cells=(100,100) hardcoded, output_quantities hardcoded HSIGN/RTP/DIR, GEN3 always-on)
-> nested. v0.1 explicitly documents deferring BOUNDNEST3 (WAVEWATCH III nested spectra) and the
SWAN->SFINCS wave-setup coupling -- an intentional scope line, not an oversight.

---

### 2.8 OpenQuake

**Package:** `openquake.engine` 3.25.1 (installed, importable) -- but used exclusively as a CLI/deck
engine (`oq engine --run job.ini --exports csv`), never the Python API.
**Denominator: 170** = 14 calculators + 156 job.ini (OqParam Param-typed) keys.
**Direct 7 / Nested 16 -> Raw 13.5% | Capability 37.5%.**

Denominator method: `openquake.engine`'s own top-level namespace is a bare `__init__.py`
(`dir(openquake.engine)` = `['OPENQUAKE_ROOT','os']`, zero public callables) and our code never imports
`openquake.*` in Python, so the denominator is the engine's documented command/keyword space:
- (a) 14 calculator types from `from openquake.calculators.base import calculators; len(calculators)`
  (classical, classical_bcr, classical_damage, classical_risk, disaggregation, event_based,
  event_based_damage, event_based_risk, multi_risk, post_risk, preclassical, scenario, scenario_damage,
  scenario_risk).
- (b) 156 job.ini parameters from `OqParam` Param-typed descriptors (`from
  openquake.commonlib.oqvalidation import OqParam; from openquake.hazardlib.valid import Param`) -- the
  engine's own job.ini validation dictionary, the source the manual's parameter tables generate from.
- Total 14 + 156 = **170**.

Capability areas:

| Area | Status | Note |
|---|---|---|
| Classical PSHA hazard (curves/maps) | covered | calculation_mode hardcoded to classical (1/14); poes/investigation_time/hazard_maps/mean rendered in job.ini. |
| Seismic source modeling (area + fault sources, GR MFD) | covered | render_source_model_xml (synthetic GR area source) + render_fault_source_model_xml (real GEM GAF simpleFaultSource with moment-balanced MFD, fed by registered `fetch_fault_sources`), both NRML 0.4. |
| Hazard-output post-processing / export (curves, maps, UHS) | covered | `workflows/postprocess_openquake.py` parses hazard_map-mean/hazard_curve-mean/hazard_uhs-mean CSV into typed scalars; charts_common.py builds chart data. |
| GMPE / GSIM selection | partial | gmpe is a free-form string (default BooreAtkinson2008) written into a 1-branch GMPE logic-tree XML; no validation against `hazardlib.gsim`, no multi-GMPE. |
| Logic trees (source-model + GMPE uncertainty) | partial | only trivial 1-branch probability-1.0 trees emitted -- file mechanism exercised, uncertainty-branching not. |
| Site conditions / amplification | partial | reference_vs30_type/value/depth set but hardcoded literals (760 m/s, 'measured'); no amplification_method, no per-site vs30 grid. |
| Event-based / stochastic hazard and risk | uncovered | event_based/event_based_damage/event_based_risk calculators never referenced. |
| Scenario hazard/risk (deterministic single-event) | uncovered | scenario/scenario_damage/scenario_risk and hazardlib/shakemap never referenced. |
| Disaggregation | uncovered | disaggregation calculator + disagg_by_src/disagg_outputs/disagg_bin_edges never referenced. |
| Risk / vulnerability / fragility / exposure (risklib) | uncovered | classical_risk/classical_bcr/classical_damage/multi_risk/post_risk + `openquake.risklib` unreferenced -- damage work goes through a separate Pelicun tool. |
| Geospatial utilities (hazardlib.geo) | uncovered | our code reimplements its own bbox/haversine math instead. |
| HMTK (Hazard Modeller's Toolkit) | uncovered | present in the venv, never referenced. |

capability_pct = (3 covered + 3 partial x 0.5) / 12 = 37.5%.

Notable uncovered: 13 of 14 calculators unused (only classical/PSHA; no event_based/scenario/
disaggregation/risk/damage); `openquake.risklib` entirely unused (risk delegated to Pelicun); GMPE is an
unvalidated free string, no multi-GMPE or multi-source logic-tree uncertainty (both logic trees always
trivial 1-branch); no Python API usage at all -- every invocation is a CLI subprocess.

Counting commands:
```
./venvs/agent/bin/python -c "import openquake.engine as oe; print(oe.__file__); print(len(dir(oe)))"
./venvs/agent/bin/python -c "from openquake.calculators.base import calculators; print(len(calculators)); print(sorted(calculators))"   # 14
./venvs/agent/bin/python -c "from openquake.commonlib.oqvalidation import OqParam; from openquake.hazardlib.valid import Param; params=[n for n in vars(OqParam) if not n.startswith('_') and isinstance(getattr(OqParam,n,None),Param)]; print(len(params))"   # 156
grep -rn '^import openquake\|^from openquake\|import openquake' server/src/trid3nt_server services/workers --include=*.py   # zero real imports
grep -n 'openquake\|fault\|hazard' server/src/trid3nt_server/tools/__init__.py   # registered: fetch_fault_sources, run_openquake (door), openquake_psha (template)
grep -rn 'calculation_mode' server/src/trid3nt_server services/workers --include=*.py
```

Key notes: 2 direct OpenQuake-lane tools -- `fetch_fault_sources` (GEM Global Active Faults fetcher) and
`openquake_psha` (PSHA dispatch template behind the `run_openquake` door; engine-door refactor, OPENQUAKE slice; was `run_seismic_hazard_psha`); `query_point_hazard` is a generic
any-raster point-sampler with zero openquake import and was excluded. Nested usage:
`services/workers/openquake/job_ini.py` (pure deck templating, no openquake import -- writes job.ini/NRML
XML by hand), `run_oq.py` (CLI subprocess shim), postprocess parsing the CLI's CSV exports. Of 156 OqParam
keys, render_job_ini emits 24, 22 matching an OqParam Param name (2 -- gsim/source_model_logic_tree_file
-- validated outside the Param pattern, kept OUT of the ratio conservatively). Of those 22: 6 DIRECT
(region<-bbox, poes<-poe, investigation_time<-investigation_time_years, region_grid_spacing<-
site_grid_spacing_km, maximum_distance<-max_distance_km, intensity_measure_types_and_levels<-imt), 16
NESTED (hardcoded/workflow-internal). Plus 1 calculator covered/direct (classical). Total raw = 23/170 =
13.5%. Raw and capability diverge sharply because OpenQuake's job.ini surface (156 knobs) is enormous and
largely risk/event-based/disaggregation-specific, while the single PSHA-classical demo path needs only a
modest fixed subset that happens to span most CORE PSHA capability areas.

---

### 2.9 Pelicun

**Package:** `pelicun` 3.9.0.
**Denominator: 12** unique public callables across the assessment workflow classes.
**Direct 0 / Nested 0 -> Raw 0.0% | Capability 5.0%.**

Denominator method: enumerated `pelicun.assessment` workflow classes (Assessment, AssessmentBase,
DLCalculationAssessment, TimeBasedAssessment), `inspect.getmembers` on each, filtered to non-underscore
functions/methods/properties, took the UNIQUE set (inherited methods not double-counted).
TimeBasedAssessment is an unimplemented stub (0 members). Result: **12** -- aggregate_loss,
bldg_repair(property), calc_unit_scale_factor, calculate_asset, calculate_damage, calculate_demand,
calculate_loss, get_default_data, get_default_metadata, load_consequence_info, repair(property),
scale_factor. (Plain `__slots__` sub-model handles asset/damage/demand/loss/log/options/stories/
unit_conversion_factors are not callables and are excluded per the counting method.)

Capability areas:

| Area | Status | Note |
|---|---|---|
| Demand model (DemandModel / calculate_demand) | uncovered | `damage_assessment.py` (the pelicun_damage_assessment template) samples the hazard raster itself with rasterio, bypassing pelicun's demand pipeline. |
| Asset model (AssetModel / calculate_asset) | uncovered | asset/component definition is a plain GeoDataFrame column lookup (component_type). |
| Damage model (DamageModel / calculate_damage, fragility) | uncovered | DS0..DS4 binning is hand-coded threshold logic on loss ratios, not pelicun.model.damage_model. |
| Loss/repair model (LossModel / calculate_loss, aggregate_loss, repair) | uncovered | repair_cost_mean/p95 = hand-rolled loss ratio x hardcoded replacement-value dict; no LossModel call. |
| Default data/metadata library (get_default_data/get_default_metadata -> DamageAndLossModelLibrary) | partial | the bundled HAZUS v6.1 loss_repair.csv IS consumed (real reference data), but via a hand-built filesystem path from `pelicun.__file__`, not the documented `get_default_data(...)` method. |
| Unit conversion (calc_unit_scale_factor/scale_factor) | uncovered | metre->feet is a literal `*3.28084` constant, not pelicun's unit registry. |
| DL_calculation end-to-end (DLCalculationAssessment) | uncovered | zero references anywhere. |
| Auto-population (pelicun.auto) | uncovered | no references. |
| Uncertainty quantification (pelicun.uq: RandomVariable/RandomVariableRegistry, correlation) | uncovered | Monte Carlo uses a bare `numpy.random.default_rng().lognormal()`. |
| File I/O standard formats (pelicun.file_io) | uncovered | our I/O is geopandas FlatGeobuf, independent of pelicun's format. |

capability_pct = 0.5 / 10 = 5.0% (only the bundled-data-library area partially covered, via a non-API path).

Notable uncovered: entire DamageModel/LossModel computation engine (calculate_damage/calculate_loss/
aggregate_loss -- our tool reimplements a parallel simplified HAZUS interpolation + Monte Carlo binning in
numpy); DLCalculationAssessment (the class purpose-built to drive pelicun's canonical end-to-end
pipeline); pelicun.uq; get_default_data/get_default_metadata (bypassed for a hand-built path);
pelicun.auto and pelicun.file_io.

Counting commands:
```
venvs/agent/bin/python -c "import pelicun; print(pelicun.__file__, pelicun.__version__)"
venvs/agent/bin/python -c "import pelicun.assessment as a; import inspect; [print(n) for n,o in inspect.getmembers(a) if inspect.isclass(o) and o.__module__=='pelicun.assessment']"
venvs/agent/bin/python -c "import pelicun.assessment as a, inspect; classes=['AssessmentBase','Assessment','DLCalculationAssessment']; seen=set(); [seen.add(n) for c in classes for n,o in inspect.getmembers(getattr(a,c)) if not n.startswith('_') and (inspect.isfunction(o) or inspect.ismethod(o) or isinstance(o,property))]; print(sorted(seen), len(seen))"   # 12
grep -rn '^import pelicun|^from pelicun|    import pelicun|    from pelicun' server/src/trid3nt_server services/workers
grep -n 'pelicun' server/src/trid3nt_server/tools/__init__.py   # registered: postprocess_pelicun, run_pelicun door, pelicun_damage_assessment + pelicun_damage_with_buildings templates
```

Key notes: numerator is effectively zero. The ONLY touchpoint anywhere is a single `import pelicun` in
`workflows/pelicun/damage_assessment/damage_assessment.py`, used solely to read `pelicun.__file__` and hand-construct a
path to the bundled `resources/DamageAndLossModelLibrary/flood/building/portfolio/Hazus v6.1/
loss_repair.csv`. That CSV's contents ARE genuinely used (real HAZUS v6.1 depth-damage curves), but zero
methods of Assessment/AssessmentBase/DLCalculationAssessment are ever invoked -- no calculate_demand/asset/
damage/loss, no get_default_data/get_default_metadata, no aggregate_loss, no pelicun.model.*, no
pelicun.uq. The tool's docstring calls itself "Pelicun-backed," but the actual computation (loss-ratio
interpolation via np.interp, Monte Carlo via default_rng().lognormal(), DS0-DS4 binning via fixed
thresholds) is a hand-rolled independent reimplementation of a subset of what pelicun's DamageModel/
LossModel already do -- it only borrows pelicun's bundled reference data file, not its computational
engine. `postprocess_pelicun.py` does not import pelicun at all (pure geopandas/pandas post-aggregation).
`services/workers` has zero pelicun references. Raw = 0/12 = 0.0%.

---

### 2.10 Delft3D FM (green-lit, NOT integrated)

**Status:** green-lit for future integration; **zero touchpoints today.** No deck author, no worker, no
registered tool, no `import`/deck reference anywhere in `server/src/trid3nt_server` or `services/workers`.
Listed here for roadmap completeness only.

- Packages / deck: n/a (not integrated).
- API points: n/a (no denominator established -- when integrated, the natural denominator would be the
  Delft3D FM `.mdu` master-definition keyword space plus any Python bindings, following the same
  dico-based method used for TELEMAC/SWAN).
- Direct 0 / Nested 0 -> Raw 0.0% | Capability 0.0%.

---

## 3. Methodology -- what each number can and cannot tell you

Every engine was measured with an identical two-metric method so the columns are comparable. Both metrics
have honest limits; they are reported together on purpose.

**The denominator is not one thing across engines, and that matters.**
- For pip-installed Python packages with a real API (SFINCS's hydromt_sfincs, SWMM's pyswmm/swmm-api,
  MODFLOW's flopy, GeoClaw's clawpack.geoclaw, Landlab, Pelicun) the denominator is the *importable
  public-callable surface* -- functions + classes + one-level-deep public methods, dunders/_private/
  vendored re-exports excluded.
- For deck/command-driven engines (TELEMAC, SWAN, and OpenQuake in practice) the denominator is the
  engine's *own dictionary or manual keyword space* -- `telemac2d.dico`, the SWAN User Manual command
  list, `OqParam`'s job.ini validation dictionary.
- Two engines are *summed two-axis* denominators (SFINCS = Python API + sfincs.inp keywords; GeoClaw =
  public callables + rundata params) because a single axis would misrepresent the surface our code
  actually spans.

Because the denominator basis differs, **raw % is only meaningful within an engine, never as a
cross-engine ranking.** SWAN's 31.5% and SWMM's 2.83% do not mean SWAN is "10x better covered" -- SWMM's
denominator (1167) is inflated by swmm-api auto-generating ~55 per-section property accessors and by
pyswmm's large runtime-object surface, while SWAN's denominator (54) is a hand-curated command list. A
huge auto-generated binding (flopy's MF6, swmm-api) will always show a low raw % even when the *modern,
load-bearing* subset is fully exercised.

**What raw % genuinely cannot see:**
- *kwargs depth.* One covered method can hide a rich or a trivial argument surface; both count as a single
  point. `setup_config` passthrough or a single `run_*` tool can drive dozens of underlying keywords that
  the count credits as one or a handful.
- *dead / irrelevant parameter space.* Much of every large denominator is capability our tools have no
  reason to touch (TELEMAC's TIDES for a river dye tool; OpenQuake's risklib when damage is delegated to
  Pelicun; flopy's pre-MF6 legacy engines). Those inflate the denominator and depress raw % without
  representing a real gap.
- *reimplementation vs. non-use.* A raw 0 can mean "we ignore this capability" OR "we do this capability
  ourselves in numpy/rasterio and never call the library's version." Pelicun (0.0% raw) is the extreme
  case: the damage/loss science is genuinely performed, just hand-rolled -- the library is used only as a
  data file. GeoClaw, SWMM's `.rpt` parser, and OpenQuake's geo utilities are milder versions of the same
  pattern. Raw % cannot distinguish these; the capability table and per-engine notes do.
- *direct vs. nested.* Raw % counts a point the same whether the LLM can reach it or not. The
  direct/nested split (and `direct = 0` for MODFLOW, Landlab, Pelicun) is where the LLM-facing surface is
  actually visible.

**Why capability % is the decision-grade number.** The capability metric is scored against each engine's
*own* documented capability areas (its module structure, its manual's section groupings, its dico's
RUBRIQUE1 taxonomy), with partial = 0.5. It answers the question that actually matters -- "of the things
this engine is *for*, how many does our pipeline substantively exercise?" -- and it is robust to the three
distortions above: auto-generated binding bloat, dead parameter space, and self-reimplementation. When raw
and capability diverge (TELEMAC 13.8 vs 45.8; OpenQuake 13.5 vs 37.5; SFINCS 26.5 vs 54.2) the capability
number is the honest read; when they agree and are both low (Landlab 4.65 / 30; Pelicun 0 / 5) that
agreement is itself a signal that the engine is exercised narrowly.

**Auditability.** Every denominator, numerator, and split in this document traces to a command reproduced
in the per-engine "Counting commands" block, run against the pinned build in `venvs/agent` or the pinned
worker container image (GeoClaw against the docker overlay snapshot of clawpack==5.14.0; TELEMAC against
the `trid3nt-local/telemac:latest` image dico; SWAN against the official manual page). Numerators come
from greps over `server/src/trid3nt_server` (tools/ + workflows/) and `services/workers`, with every hit
manually triaged for false positives (English-word/other-engine keyword collisions were excluded).

This document is inventory only. It records what is and is not covered; it makes no recommendation about
which gaps to close or in what order.
