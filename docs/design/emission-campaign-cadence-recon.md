# Emission-campaign cadence recon: per-engine fact table

Scout pass for the emit-on-solve seam (append-only `outputs.json`,
`completion.json` as finality, `output_interval_min` as the universal
cadence lever). This is a **recon**, not a design doc: every row is read
from the actual worker/composer code as of this pass, never inferred. See
`outputs-manifest-schema.md` for the frozen-candidate schema and the
migration plan this table feeds.

Columns:

- **(a) per-timestep output today** -- does the solver itself write
  per-timestep artifacts, and what files.
- **(b) during-run or at-exit** -- can the seam observe (a) while the
  solver is still running, or only after the process/container exits.
- **(c) deck cadence keyword** -- the native deck keyword controlling
  output interval, and its current value/source.
- **(d) wired to a user lever** -- is (c) reachable from a tool arg/gate
  today, under ANY name (not necessarily `output_interval_min`).
- **(e) postprocess rasterizes** -- all steps / peak-only / final-only.
- **(f) work class** -- effort to bring the leg's OWN full-frame manifest
  up to the settled `outputs.json` shape. Defined at the bottom.

## The fact table

| Engine leg | (a) per-timestep output today | (b) during-run / at-exit | (c) cadence keyword (value/source) | (d) user lever? | (e) postprocess rasterizes | (f) work class |
|---|---|---|---|---|---|---|
| SFINCS flood (pluvial, regular grid) | `sfincs_map.nc` -- one NetCDF, N time-indexed layers | **At-exit.** `subprocess.run` blocks; postprocess (NetCDF->COG) runs after SFINCS exits, writes `publish_manifest.json` before `completion.json` | `dtout`/`dtmaxout` (seconds), fixed `max(600, duration_s/24)` (~hourly) | **No** -- pluvial path is pinned to `output_interval_min=None` deliberately (regression-critical, `run_sfincs.py:_resolve_output_interval_min`) | **All steps**, subsampled to `MAX_FLOOD_FRAMES` (144) -- peak COG + `flood_depth_frame_NN.tif` | **S** |
| SFINCS surge/coastal + SnapWave waves | Same `sfincs_map.nc`, waves field via `kind="waves"` pass | **At-exit**, same worker | Same `dtout`, but resolved via `_resolve_output_interval_min(is_coastal=True)`: explicit `output_interval_min` honored (floor 1 min / 60 s deck floor), else a 5-min default | **Yes** -- `output_interval_min` is a real tool/gate arg for coastal/quadtree/wave runs; `solver_confirm.py` surfaces `flood_output_interval_min` as a pre-solve lever card | All steps, subsampled to 144 -- peak + `wave_height_frame_NN.tif` | **S** |
| TELEMAC river_dye | `r2d_river.slf` -- ONE SELAFIN mesh result; ALL `GRAPHIC PRINTOUT PERIOD` frames live inside this single file (no per-frame files) | **At-exit** -- local-docker; agent supervisor reads the mounted rundir only after the container exits (blocking `docker run`) | `GRAPHIC PRINTOUT PERIOD` = `graphic_period`, a STEP COUNT (not minutes), hardcoded default 200 in the dataclass | **No** -- not exposed on `run_telemac.py` or any template composer arg | **Peak-only**: one `telemac_dye_peak.tif` (per-node max-over-time). Animation is instead played from the SELAFIN mesh sibling via QGIS's native mesh (MDAL) temporal reader -- bypasses the raster-frame pattern entirely | **L** |
| TELEMAC rain_on_grid (ROG) | Same SELAFIN-mesh pattern; `graphic_period` default 100 | At-exit, same local-docker envelope | `graphic_period` (rog_build.py, default 100) | No | Peak/hydrograph scalars (`peak_Q`/`vol`/`maxH`) parsed from the listing + mesh sibling for spatial; no raster frame series | **M** |
| TELEMAC coastal (`res_coastal.slf`) | Same SELAFIN-mesh pattern | At-exit | `graphic_period` (computed default in `telemac_coastal_build.py`) | No | `postprocess_coastal`: per-node MAX-over-time depth -> ONE `coastal_depth_max.tif` | **L** |
| TELEMAC WSE / dissolved-oxygen (WAQTEL) | Same SELAFIN-mesh pattern | At-exit | `graphic_period` | No | `postprocess_telemac_wse` / `postprocess_telemac_do`: peak/final field, one COG | **L** |
| TELEMAC GAIA sediment | `gaia_river.slf` | At-exit | `graphic_period` (500 listing period, same file) | No | `postprocess_telemac_deposition`: FINAL frame only (cumulative bed evolution = total event deposition) | **L** |
| TELEMAC TOMAWAC / ARTEMIS (wave agitation) | `res_agitation.slf` / TOMAWAC result | At-exit | Not temporal -- ARTEMIS is a stationary agitation solve (single WAVE HEIGHT field, `agit_field.slf` is explicitly single-frame) | N/A (non-temporal leg) | Single field, one COG | **N/A** (flag only, not a temporal leg) |
| TELEMAC 3D | Result SELAFIN, surface + bottom variables | At-exit | `GRAPHIC PRINTOUT PERIOD`/`graprd`, `LISTING PRINTOUT PERIOD = max(1, nit//10)` | No | `postprocess_telemac3d`: two peak/final COGs (surface, bottom) | **L** |
| GeoClaw inundation (tsunami/surge) | `_output/fort.q*`/`fort.t*` (Clawpack AMR ASCII dumps, one pair per `output_frames` snapshot) + optional `fgout` uniform-grid dumps | **At-exit** -- `subprocess.run` blocks; worker-side `workers/_geoclaw_postprocess` runs AFTER exit, writes `publish_manifest.json` before `completion.json` (same Phase-4 pattern as SFINCS) | `output_frames` (an evenly-spaced FRAME COUNT, not minutes), default 24, `clawdata.output_style=1` | **Yes** -- `output_frames` is a first-class LLM tool arg on every geoclaw template (`inundation`, `storm_surge`, `regional_manning`, `gauge_timeseries`, `amr_regions`, `thacker_validation`) | All frames, subsampled to `_geoclaw_postprocess.MAX_FRAMES` (144) -- peak + per-frame depth COGs | **S** |
| ELMFIRE spread/spotting | `time_of_arrival_*.bil` (+ `flame_length`/`vs`/`flin` .bil) -- ONE cumulative field per quantity; `DTDUMP` controls the solver's own internal dump cadence but only ONE ToA raster is discovered/read at exit | **At-exit** -- `subprocess.run` blocks; postprocess is agent-side (`trid3nt_server/workflows/elmfire/postprocess_elmfire.py`), no worker-side manifest offload | `DTDUMP` (seconds), default hourly (3600 s); a `dtdump_s` param exists on `run_elmfire.py`/sensitivity composers | Partially -- `dtdump_s` is a function param with a default, but is not consistently surfaced as an LLM-facing tool arg across templates | **Derived, not native**: animation "frames" are POST-HOC thresholds of the single ToA field (`toa_frame_grids`, hourly buckets, subsampled to `MAX_FLOOD_FRAMES`) -- the solver does not write literal per-frame files at all | **L** |
| SWMM urban_flood / dual-drainage | SWMM `.out` binary -- EVERY `REPORT_STEP` reporting timestep, natively | **At-exit** for the dev-primary path: `pyswmm.Simulation` runs **IN-PROCESS on the agent host** (`run_swmm_local`), not in a container; postprocess reads the finished `.out` after the `with Simulation(...)` block exits. A symmetric AWS-Batch `entrypoint.py` (pyswmm in a worker image, `publish_manifest.json`-capable) exists but is not the path production traffic takes | `REPORT_STEP` (HH:MM:SS), hardcoded per-template (e.g. `01:00:00`, or a `dt_min`-derived string in some templates); no `output_interval_min` name anywhere | **No** -- no template wires `REPORT_STEP` to a shared cadence arg | **All** reporting steps, subsampled to `MAX_FLOOD_FRAMES` (shared `frames.py` selector) -- peak + `swmm_depth_frame_NN.tif` | **M** |
| MODFLOW transient legs (sustainable_yield, land_subsidence, ASR, mine_dewatering, regional_water_budget, wetland_hydroperiod, GWT plume/transport) | MF6 binary output (`.hds`/`.ucn`/`.cbc`) carries EVERY transient stress-period timestep for GWT (`saverecord=[("CONCENTRATION", conc_save)]`, "typically many steps"); the GWF flow OC package by contrast usually saves `HEAD "LAST"` only (one step) | **At-exit, and the execution model itself is the biggest gotcha**: `mf6` runs via `exec_kind="exec"` **directly on the agent host** (`solver.py`: "no public MODFLOW image exists"), not in a container. There is no separate "worker" process boundary -- the agent daemon IS the solver driver. A worker-side `_modflow_postprocess` package exists and DOES write `publish_manifest.json`, but only on the `--build-spec-uri` heavy-compute-offload BUILD path (deck authoring), never for the transient OUTPUT/postprocess step of the default host-exec solve | Per-archetype transient schedule (`_resolve_transient_periods` / `_resolve_monthly_periods`): N stress periods of `perlen_days`/`nstp`, months-driven for most archetypes; no minute-scale interval concept at all | **No** -- no `output_interval_min`-shaped lever anywhere in MODFLOW; cadence is periods-per-archetype, not a dial | **Final/peak-only**: `_read_final_concentration` takes `nanmax(arr, axis=0)` over ALL transient steps into ONE 2D field (or `get_data(totim=last)` for head); no per-period raster frames are ever written despite the UCN carrying many steps | **L** |
| SCHISM tidal/surge/ICM/sed | `out2d_*.nc` scribed output stack; `nspool` (map cadence) IS wired but HARDCODED to `~1/hour` (`hourly = max(1, round(3600/dt_s))`), `ihfskip` forces the WHOLE run into ONE output stack | **At-exit** -- `subprocess.run` with a timeout; postprocess is agent-side only (no `workers/_schism_postprocess`) | `nspool`/`nspool_sta`/`ihfskip`, all derived+substituted by regex in `deck_authoring.py`, never a caller-supplied value | **No** -- zero user-facing cadence lever; the "~hourly" is baked into the deck template | **Peak/min-only**: `read_out2d_elevation` reads peak+min surface elevation from the ENTIRE out2d stack into ONE field. `postprocess_schism.py` imports NOTHING from `shared/frames.py` -- **there is no animation frame emission for SCHISM today, at all** | **L** |
| HEC-RAS riverine_flood + levee_breach + flood_2d (inflow=6.x, RoG=2025) + culvert_embankment_flow | **CORRECTION (ADR 0287): per-step 2D fields exist on BOTH lineages, NOT peak-only.** 6.x: `Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/2D Flow Areas/<area>/Water Surface` = (Nt,Nc) at the plan's `Base Output Interval` (verified (289, 5765) at a 5-min step on the real Muncie deck); parallel `.../Unsteady Time Series/Time` = (Nt,) DAYS. 2025 managed: `/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh/Cell Depth` = (Nt,Nc) + `/Results/Output Blocks/Base Output/Time` = (Nt,) DAYS | **At-exit** -- `subprocess.run`/managed solve blocks; BOTH postprocess producers run AGENT-SIDE (`postprocess_hecras.py` opens the plan HDF with h5py; `rog2025_pipeline.build_depth_frames` runs mounted-driver in-process) -- **no image rebuild binds the frame legs** | 6.x cadence keyword = `Base Output Interval` (an HDF5 attr on `Plan Data/Plan Information`, e.g. `'5MIN'`), DISTINCT from `Computation Time Step Base` (`'10SEC'`, the solver step `deck_edit.py` patches). 2025 = the managed engine's default mapping interval (NOT authored by us; decompile territory) | **No -- the `output_interval_min` lever DEFERS (ADR 0287, the 0284-3A precedent).** An asymmetric lever (6.x `Base Output Interval` attr-patch reachable; 2025 not reachable without a managed-engine decompile) silently no-oping on one path is the hidden-inconsistency class. BOTH cadence items are QUEUED for NATE (the 6.x attr-patch + its empirical proof; the 2025 mapping-interval decompile hunt) | **CORRECTION (was "peak-only"): frames NOW emit via the `outputs.json` seam (ADR 0287).** ALL Unsteady Time Series / Cell Depth steps publish (NEVER-OMIT, `quantity="flood_depth"` -> the peak's `continuous_flood_depth` preset), t = totim DAYS*86400 s; the typed peak COG + 2D mesh-preview vector are retained (composer-built). **culvert_embankment_flow stays CHARTS/PEAK-ONLY** (the 0284 A/B present-vs-absent discriminant class -- the ponding-vs-steady series + mass-balance bars answer the question, not an animation). levee-HELD (dry) = honest empty (no frames) | **L (LANDED, ADR 0287)** |
| SWAN wave_field (nonstationary) | `swan_out.mat` -- ONE Matlab BLOCK file, N gridded snapshots (HSIGN/RTP/DIR) for `nonstationary` mode; `stationary` mode = 1 frame | **At-exit** -- worker-side `workers/_swan_postprocess` runs after the SWAN binary exits, writes `publish_manifest.json` before `completion.json` (same Phase-4 pattern as SFINCS/GeoClaw) | `output_frames` (count, nonstationary only), plus `sim_duration_s`/`time_step_s` | **Yes** -- `output_frames` is a documented composer arg (build_spec `"output_frames": 24`) | All frames, subsampled to `MAX_FRAMES` (144, local mirror of `frames.MAX_FLOOD_FRAMES`) -- peak + `swan_wave_height_frame_NN.tif` | **S** |
| Landlab overland_flow_timeseries | Pure-Python `OverlandFlow` step loop snapshotting `surface_water__depth` every `output_interval_s` INTO MEMORY (no native solver file at all -- Landlab IS the in-process Python "solver") | **At-exit in practice today** (the whole chain runs to completion inside one Python call before any COG is written), but architecturally the closest thing to during-run: the snapshot loop is pure Python in the same process, so a future streaming write is a small, contained change (no subprocess/container boundary to cross) | `output_interval_s`, default 300 s, floored so snapshot count never exceeds `_MAX_TIMESERIES_SNAPSHOTS` (48) | **Yes** -- `output_interval_s` is a documented tool/composer arg (`overland_timeseries.py`) | All snapshots (bounded to 48 in-worker, then re-subsampled to `MAX_FLOOD_FRAMES` by the composer) -- peak + `depth_step_NN` secondary fields | **M** |

**Work-class definitions** (effort to bring the leg's own full-frame
manifest up to the settled `outputs.json` shape):

- **S** -- already on the worker-writes-`publish_manifest.json`-before-`completion.json`
  pattern (SFINCS Phase 4 lineage), already emits ALL solver steps
  (subsampled), already register-only agent-side. Converting to
  `outputs.json` is a field-shape/append-semantics rework, not new
  plumbing. (SFINCS flood, SFINCS surge/waves, GeoClaw, SWAN nonstationary.)
- **M** -- multi-step raw output exists and the shared `frames.py`
  subsample machinery already reads ALL steps, but the write point is
  agent-side (on-box) rather than at a worker/server boundary, or the
  execution model needs a genuine (if contained) architecture change to
  stream. (SWMM, Landlab overland_flow_timeseries, TELEMAC ROG.)
- **L** -- one or more of: no per-timestep raw output exists at all
  (ELMFIRE), postprocess is peak/final-only today with no frame concept
  (MODFLOW, SCHISM, HEC-RAS, most TELEMAC modules), or the native output
  format doesn't map onto "one URI per frame" without discarding a
  superior native-mesh temporal display (TELEMAC).

## The three biggest per-engine gotchas

1. **TELEMAC (all modules) publishes temporal data as ONE mesh file, not
   N frame files.** `r2d_river.slf`/`res_coastal.slf`/etc. carry every
   `GRAPHIC PRINTOUT PERIOD` frame inside a single SELAFIN, and QGIS's
   native mesh (MDAL) temporal reader already animates it directly --
   completely bypassing the raster-COG-per-frame pattern every other
   engine uses. An `outputs.json` entry is `{kind, quantity, name, uri, t?}`
   with ONE uri per timestep; TELEMAC has no clean way to emit that
   without either (a) adding a `kind="mesh"` entry type with no `t`
   (the mesh IS the whole animation), or (b) extracting N COGs from the
   SELAFIN and throwing away the native-mesh advantage. This is the
   single biggest place the settled per-frame-URI design does not fit an
   existing engine's real output shape.

2. **MODFLOW's host-exec path has no worker/agent boundary to hang a
   writer on.** `mf6` runs `exec_kind="exec"` directly on the always-on
   agent host (no public MODFLOW container image exists); the agent
   process IS the solver driver. The existing worker-side
   `_modflow_postprocess` / `publish_manifest.json` machinery only fires
   on the separate `--build-spec-uri` heavy-compute-BUILD offload path
   (deck authoring), never for the transient solve's own output. SWMM's
   dev-primary path (`pyswmm.Simulation` in-process) has the identical
   blurred boundary. The settled design's implicit "worker writes,
   seam/agent reads" split needs an explicit answer for engines where
   worker and agent are the same process.

3. **ELMFIRE and SCHISM currently produce ZERO real per-timestep
   artifacts.** ELMFIRE's ToA raster is a single cumulative field;
   "animation frames" are a postprocess-side derived threshold, not
   literal solver dumps -- `DTDUMP` genuinely drives the solver's
   internal write cadence, but nothing downstream reads intermediate
   dumps today, only the final cumulative field. SCHISM has `nspool`
   wired to a real ~hourly cadence in the deck, and the out2d stack DOES
   carry that many timesteps in the NetCDF, but `postprocess_schism.py`
   imports nothing from `shared/frames.py` and only ever reads a single
   peak/min field across the WHOLE stack -- there is no animation frame
   emission for SCHISM at all right now, despite the raw data existing on
   disk. For both, "unknown quantity -> honest neutral ramp" is not
   enough; these need an actual frame-extraction leg built, not a manifest
   reshape.

## Where the settled design does not survive contact with the code (flag only)

- **A pre-existing, unfinished generalization effort already targets
  this exact goal**, under a different name and vocabulary:
  `trid3nt_contracts.output_quantities` (`OUTPUT_REGISTRY_SCHEMA_VERSION=1`,
  STEP 2 of an "engine-coverage-levers refactor") declares a per-engine
  `OutputQuantitySpec` table (`quantity_id`, `kind: raster|timeseries|scalar`,
  `style_preset`, `role`, `default_on`) and
  `trid3nt_server.workflows.shared.publish_quantities` (STEP 2, DEFAULT-OFF)
  is a generic executor that walks that registry and assembles a
  `PublishManifest` via the SAME `register_manifest_layers` register-only
  path this recon traced through SFINCS/GeoClaw/SWAN. `get_output_registry`
  returns `()` for every engine except OpenQuake today ("STEP 3 migrates
  engines" is explicitly deferred/not started). This scaffold is agent-side
  only (no STEP-4 worker mirror yet) and uses `role`/`style_preset` fields
  the emit-on-solve seam's settled entry shape (`{kind, quantity, name, uri,
  t?, units?}`, "no roles, no flags") explicitly does NOT carry. Landing the
  emission campaign without reconciling against this in-repo scaffold risks
  two parallel, half-built generalization mechanisms for the identical
  problem. Not relitigating which one wins -- flagging that NATE/the
  campaign owner should look at `output_quantities.py` /
  `publish_quantities.py` before design work starts, not after.

- **The universal cadence param name is not actually universal yet.**
  `output_interval_min` exists ONLY for SFINCS (and only on the
  coastal/wave path; the pluvial path is deliberately pinned OFF). Every
  other engine has its own, differently-shaped cadence knob under a
  different name: `output_frames` (a COUNT: GeoClaw, SWAN), `output_interval_s`
  (Landlab), `graphic_period`/`GRAPHIC PRINTOUT PERIOD` (a step count:
  TELEMAC, unexposed), `DTDUMP` (ELMFIRE, partially exposed), `REPORT_STEP`
  (SWMM, unexposed), `nspool` (SCHISM, unexposed, hardcoded), and MODFLOW
  has no interval concept at all (period-count schedules). "Universal
  param name `output_interval_min`" as stated in the settled design is a
  target, not a current fact; the campaign's real first move per engine is
  a units-and-name migration (seconds/steps/counts -> minutes) before any
  seam work.

- **Nothing in the codebase streams frames DURING a run.** Every single
  engine leg surveyed is at-exit: the solve is either a blocking
  `subprocess.run` (SFINCS, GeoClaw, TELEMAC, ELMFIRE, SCHISM, HEC-RAS,
  SWAN) or a blocking in-process call (MODFLOW mf6-exec, SWMM pyswmm,
  Landlab). The settled design's "frames publish as they appear" implies a
  live watch loop observing a GROWING `outputs.json` while the solver is
  still executing; today the closest analogue is the LIVE solve-progress
  heartbeat (`workflows/shared/solve_progress.py`, a SEPARATE side-task on
  a 10 s tick that reports elapsed/ETA, not real solver output) running
  alongside a blocking `asyncio.to_thread` solve. The watch-loop this
  campaign needs would piggyback on that SAME pattern (a side task next to
  the off-loop solve, `DEFAULT_POLL_INTERVAL_S=10` already exists in
  `solver.py`'s `wait_for_completion`), but every engine's SOLVER PROCESS
  ITSELF would need to be changed to append to `outputs.json` as frames
  land -- that is new work in every single worker/deck, not a seam-only
  change.
