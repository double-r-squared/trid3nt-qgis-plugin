# emission/ -- map-layer emission to the plugin

`trid3nt_server/emission/` (unchanged location) turns tool/composer outputs
into the layer + pipeline frames the QGIS plugin renders over the WebSocket.

## What lives here

- `pipeline_emitter.py` -- the `PipelineEmitter`: per-step pipeline-state
  frames, budget-partition chart, vector densification, legend handoff.
- `layer_uri_emit.py` -- the emit-on-fetch seam: a router fetch with
  `purpose=` auto-emits its input layer (the job-0254 guardrail drops a
  renderable raster URI into the map without hand-emitting).
- `uri_registry.py` -- the published-URI registry.
- `quantity_styles.py` -- the emit-on-SOLVE seam's `quantity -> style_preset`
  registry (ADR 0280). An `outputs.json` entry carries a physical `quantity`
  and NO style; `resolve_style_preset(quantity)` maps it to a preset key in
  `publish_layer._QGIS_STYLE_REGISTRY`. An unregistered quantity degrades to
  `NEUTRAL_FALLBACK_PRESET` (an honest band-stats neutral ramp) + a WARNING +
  a process fallback counter -- never a silent physically-wrong colormap.
- `outputs_seam.py` -- the emit-on-SOLVE CONSUMER (ADR 0280 item 4).
  `read_outputs_manifest(run_result)` reads `outputs.json` from the run prefix
  (missing/unknown-schema -> `None`, the byte-identical no-op);
  `build_layers_from_outputs(manifest, run_id, bbox)` turns each entry into the
  SAME registered, styled, legend-stashed `LayerURI` the register-only
  `publish_manifest.json` path produced -- raster no-`t` = standalone primary,
  raster+`t` sharing a `quantity` = a temporal group (frames in `t` order),
  vector = a vector layer, mesh = a native `layer_type="mesh"` layer (ADR 0283),
  scalar = log-only. `layer_id` is
  deterministic + idempotent from `(quantity, t-ordinal, run_id)`; the legend is
  STASHED side-band (byte-identical to `register_manifest_layers`). Proven
  field-for-field byte-equivalent in `tests/test_outputs_seam.py`. Carries the
  parallel `PublishedFrame` replay meta (`t` / `group_id`) for the item-7
  persistence stamp. WIRED into the flood composer (`flood/flood.py`): a
  seam-or-legacy fork -- `outputs.json` present -> the seam owns ALL publication,
  `publish_manifest.json` supplies ONLY the `FloodMetrics` narration scalars
  (the metrics carrier); absent -> the legacy register/on-box paths run
  byte-unchanged. Fork contract pinned in `tests/test_flood_seam_fork.py`.

## emit-on-solve (`outputs.json`) -- FROZEN schema, foundation landed

The append-only `outputs.json` manifest (`trid3nt_contracts.outputs_manifest`,
`schema_version=1`) is the emit-on-solve wire: a solver leg appends flat
`{kind, quantity, name, uri, t?, units?}` entries under its run prefix; the
seam consumer reads them on the existing completion poll and publishes each
(raster+`t` sharing a `quantity` = a temporal group; raster with no `t` = one
layer; vector = a vector layer; mesh = a native SELAFIN `layer_type="mesh"` layer,
ADR 0283; scalar = log-only). v1 is AT-EXIT;
a MISSING manifest is a no-op (legacy engines byte-unchanged). See
`docs/design/outputs-manifest-schema.md` (frozen schema; v1 gained OPTIONAL
`bbox` + `band_stats` render-hint fields, ADR 0280 EXECUTED) + ADR 0280 (the
seam, the flood proving case, the scaffold reconciliation, the cap fix). The seam
consumer (`outputs_seam.py`), the SFINCS worker producer (`outputs.json` written
alongside `publish_manifest.json`), and the byte-equivalence bar are LANDED (ADR
0280 EXECUTED). The SFINCS flood composer is WIRED to the seam and the deck-side
cadence cap fix landed (ADR 0280 live close-out).

MIGRATED ENGINES (S-class, each: worker producer + composer seam-or-legacy fork +
byte-equivalence + deck-side cadence + post-hoc thinning deleted + live proof
through a rebuilt image):

- **SFINCS flood** -- the proving case (ADR 0280).
- **GeoClaw inundation** (ADR 0281): `quantity="flood_depth"`, per-frame `t` read
  from the `fort.t` sibling; composer fork in `workflows/geoclaw/inundation`;
  `publish_manifest` = metrics carrier.
- **SWAN nonstationary waves** (ADR 0281): `quantity="wave_height"`, evenly-spaced
  `t` from `sim_duration_s`; composer fork in `workflows/swan/wave_field`. The
  rebuild also shipped the SWAN postprocess in-image for the first time + fixed a
  COG-upload ordering gap.

Each seam mints `layer_id` off the PHYSICAL quantity (`flood-depth-*` /
`wave-height-*`), the one explained non-rendering divergence from the register
path's engine-prefixed stems; web temporal grouping rides the `name` token,
unchanged. Cadence stays the count-native `output_frames` lever (aliased to the
universal `output_interval_min` vocabulary in ADR 0281; no redundant param added).

MIGRATED ENGINES (M-class, HOST-EXEC writer -- the agent postprocess is the
producer; NO worker image, so offline-green == deploy-green, effective
immediately). These use the OPTION (a) ruling: the seam owns the TEMPORAL FRAMES
ONLY (`build_layers_from_outputs(..., frames_only=True)`); the TYPED PEAK layer +
its narration scalars stay composer-built exactly as before, and the composer does
NOT consume the seam's peak entry (avoids double-registering the same COG uri).
Byte-equivalence is measured on the FRAME render stream (ADR 0282):

- **SWMM urban_flood + dual_drainage** (ADR 0282): `postprocess_swmm` writes
  `outputs.json` host-side (the shared `workflows/shared/outputs_manifest_io`
  writer's FIRST real use), `quantity="flood_depth"`, per-frame `t` = elapsed
  seconds from the `.out` report steps; the 144-frame `_select_frame_time_indices`
  cap is GONE (never-omit) and cadence resolves DECK-SIDE via `output_interval_min
  -> REPORT_STEP`. `dual_drainage` gains the depth animation for the first time.
- **Landlab overland_flow_timeseries** (ADR 0282):
  `postprocess_landlab_overland_timeseries` writes `outputs.json` host-side,
  `quantity="flood_depth"` (shares the depth family), per-frame `t` = the worker's
  REAL snapshot elapsed seconds (`max_cell_series`); the worker interval FLOOR
  (`max(output_interval_s, duration_s/48)` + `_MAX_TIMESERIES_SNAPSHOTS`) is GONE
  (honor `output_interval_s` exactly; runs exec-from-source so it takes effect
  immediately). The universal `output_interval_min` aliases the native
  `output_interval_s` (0281 precedent -- documented, not double-threaded).

The M-class seam mints `layer_id` off the physical quantity (`flood-depth-frame-*`),
the same explained non-rendering divergence from the register path's stems
(`swmm-depth-frame-*` / `landlab-overland-depth-frame-*`); grouping rides the
`name` token (`"Flood depth step N"` / `"Overland depth step N"`), unchanged.

MIGRATED ENGINES (L-class, native-mesh temporal -- TELEMAC-2D, ADR 0283). The
result IS a native, time-stepped SELAFIN that QGIS/MDAL animates directly, so the
temporal artifact is a `kind="mesh"` entry, NOT per-frame COGs. The agent-side
postprocess writes `outputs.json` (the peak entry + the mesh SELAFIN entry, the new
OPTIONAL `crs_authid=EPSG:{utm}` because a SELAFIN carries no CRS) via the host-exec
writer; the shared `workflows/telemac/results_mesh_seam.publish_results_mesh_via_seam`
reads it back through the seam (`frames_only=True`) and emits the mesh layer
(`role="context"`). Like the M-class it uses `frames_only=True` so the composer keeps
its OWN typed peak -- but the mesh IS the temporal artifact, so `build_layers_from_outputs`
builds it EVEN under `frames_only` (only standalone rasters + vectors are skipped).
The mesh `LayerURI` carries `style_preset="mesh_grid"` (new registry row
`model_results -> mesh_grid`), `bbox=None` (MDAL derives the extent), and the
`crs_authid` from the entry; `layer_id = {quantity-base}-mesh-{run_id}`.

- **rain_on_grid** (`r2d_rog.slf`): MIGRATED -- the bespoke `_publish_full_results_mesh`
  is DELETED (ledger), byte-equivalent to the seam mesh layer except the `layer_id`
  stem (the same explained divergence).
- **river_dye** (`r2d_river.slf`) + **coastal_tidal_surge** (`res_coastal.slf`):
  ADDITIVE -- the mesh animation is new alongside the existing peak COG.

Cadence for the L-class is the universal `output_interval_min` (minutes ->
`graphic_period`, a timestep count): river_dye + coastal thread it DECK-SIDE (worker
`ReachConfig`/`CoastalConfig`, parser bumps `telemac-reach-10` / `coastal-tidal-2`,
INERT until the image rebuild); rain_on_grid computes it AGENT-SIDE (its `time_step_s`
is a composer constant). `None` = byte-identical current defaults.

MIGRATED ENGINES (L-class, native-mesh temporal -- SCHISM, ADR 0286). Exactly the
TELEMAC 0283 precedent applied to SCHISM: the result IS a native, time-stepped UGRID
netCDF (`out2d_1.nc` elevation/velocity, or the 3D `salinity_1.nc`) that QGIS/MDAL
animates directly (proven live: MDAL loads a real solved out2d valid, 7 dataset
groups, 24-48 temporal steps with hourly reference times -- ADR 0286 gate #1). NO
per-step rasterization. The agent-side postprocess (SCHISM postprocess is agent-side,
so NO image law binds this leg) computes the typed peak COG; the composer then writes
`outputs.json` (peak entry + the `kind="mesh"` netCDF entry, `crs_authid=EPSG:4326`
or absent for the idealized planar QuarterAnnulus) via `workflows/schism/`
`results_mesh_seam.publish_results_mesh_via_seam`, which reads it back through the
seam (`frames_only=True`) and emits the mesh layer (`role="context"`,
`style_preset="mesh_grid"`, `bbox=None`, `layer_id={quantity-base}-mesh-{run_id}`).
The bespoke `publish_input_layer(mesh_layer)` in ALL FOUR composers (tidal_hydro,
pahm_surge, coupled_waves, baroclinic) + the three inline mesh `LayerURI`
constructions in `postprocess_schism` are SUPERSEDED against byte-equivalence
(name/style/role/crs/uri/bbox modulo the `layer_id` stem -- the 0283 bar). Per-template:

- **tidal_hydro** + **pahm_surge** (`out2d_1.nc`, `postprocess_schism`): MIGRATED --
  2D elevation mesh, `EPSG:4326` (or `None` for the QuarterAnnulus verification mesh).
- **coupled_waves** (`out2d_1.nc`, `postprocess_schism_waves`): MIGRATED -- the WWM
  dataset groups animate; the primary deliverable stays the cross-shore Hs/Tp V&V chart.
- **baroclinic_circulation** (mesh sibling `out2d_1.nc`, `postprocess_schism_baroclinic`):
  MIGRATED -- the 3D salinity column's mesh (a 2D COG stack cannot carry it faithfully;
  this case is why Option B beat per-step rasterization). Surface + bottom salinity COGs
  stay as the composer's typed peaks.
- **transport_validation**: CHARTS-ONLY (scheme-contrast + analytical gate; no map
  animation) -- NOT migrated, no mesh emit exists. ICM/SED substrate smokes: OUT of scope.

Cadence is the universal `output_interval_min` (minutes -> `nspool`, a timestep count):
`nspool = round(output_interval_min*60/dt_s)`, wired AGENT-SIDE in `deck_authoring`
(`_substitute_param_nml` / `_patch_transport_param` / `_patch_baroclinic_param`), so
NO image rebuild. SCHISM requires `ihfskip` to be an integer multiple of `nspool`
(else "ABORT: ihfskip/nspool /= integer"); the lever recomputes `ihfskip = ceil(
nsteps/nspool)*nspool` to preserve it. `None` = byte-identical hourly default (proven
live: a 2-day baroclinic at `output_interval_min=30` -> 96 out2d dataset-times vs
`=120` -> 24, exactly the 4x the lever dictates).

MIGRATED ENGINES (L-class, MODFLOW transport family -- ADR 0284, host-exec mf6).
The GWT/GWE TRANSPORT templates have depth-class quantities that animate cleanly;
MF6 OC saves EVERY transport step (never-omit by construction -- no cap ever
existed, and `publish_modflow_quantities` was DEAD scaffold, now deleted). The
agent-side `postprocess_multi_species` / `postprocess_gwe_thermal` read all saved
steps + the totim in DAYS, rasterize each on the SAME grid georef the peak uses,
and write `outputs.json` host-side (frame `t = totim_days * 86400` s, honest
DAYS->seconds -- there is NO `output_interval_min` for MODFLOW; frame density IS
the stress-period schedule levers `sim_years`/`n_periods`/`n_months`, fork 3A).
The shared `workflows/modflow/_frame_emit.read_and_emit_modflow_frames` reads it
back (`frames_only=True`) and both transport composers emit the group; the typed
peak + narration scalars + charts stay composer-built (OPTION a).

- **contaminant_plume** (single + multi species, via `postprocess_multi_species`):
  per-species quantity `plume_concentration__<slug>` so N species never collide on
  the seam's `(quantity, t)` grouping (they share one time discretization ->
  identical save-times); the `emission/quantity_styles` per-instance FAMILY
  fallback (`__`-suffix) styles every one as `continuous_plume_concentration`.
- **thermal_plume / thermal_storage** (GWE, via `postprocess_gwe_thermal`): the
  per-step temperature-EXCESS stack, bare registered `temperature` quantity (one
  group); the ATES recovery chart is untouched. `_run_id` stashed on the layer for
  the composer fork.

Head-based MODFLOW templates (drawdown/mounding/hydroperiod/subsidence/dewatering/
seepage/asr/saltwater/vadose/budget/capture/wellhead) get NO frames (fork 2A) --
their quantity is a t0-difference or max-min RANGE reduction, or non-temporal; the
temporal signal stays in their existing composer CHARTS. See ADR 0284's verdict
table (the coverage-law denominator).

MIGRATED ENGINES (L-class, HEC-RAS depth family -- ADR 0287, TWO agent-side
producers). The recon REFUTED the "peak-only" framing: BOTH HEC-RAS lineages carry
genuine per-step 2D fields. The 6.x lineage's plan HDF stores `Unsteady Time Series/
2D Flow Areas/<area>/Water Surface` (Nt,Nc) at the `Base Output Interval` (verified
(289,5765) at a 5-min step on Muncie); the 2025 managed lineage stores `Base Mesh/
Cell Depth` (Nt,Nc). Both postprocess producers run AGENT-SIDE (the plan HDF is opened
with h5py in-agent; the 2025 `build_depth_frames` runs mounted-driver in-process), so
NO image rebuild binds this leg. Frames are ADDITIVE (no prior emission; the typed
`HecrasDepthLayerURI` peak + charts + 2D mesh-preview vector stay composer-built,
OPTION a). `quantity="flood_depth"` -> the peak's `continuous_flood_depth` preset, so
a frame renders byte-consistently with the peak; frame `t = totim DAYS * 86400` s.

- **riverine_flood** + **levee_breach** + **flood_2d inflow** (6.x, via
  `postprocess_hecras`): `_write_6x_frame_entries` rasterizes EVERY Unsteady Time
  Series step onto the peak's grid (a cell->pixel LABEL raster rasterized ONCE, then a
  cheap per-step index -- masked identically to the peak) and writes `outputs.json`
  host-side. levee-HELD (dry) = honest empty (no frames).
- **flood_2d RoG** (2025 managed, via `rog2025_pipeline.build_depth_frames`): the
  frame sibling of `build_depth_cog` (same georef, reads `Cell Depth[i]` per step);
  the composer `_write_rog_frame_manifest` uploads each COG + writes `outputs.json`.
- **culvert_embankment_flow**: CHARTS/PEAK-ONLY -- the A/B present-vs-absent
  discriminant (ponding-vs-steady series + mass-balance bars + the A peak COG) is the
  deliverable, NOT an animation (the 0284 discriminant fork class). Not migrated.

The shared `workflows/hecras/_frame_emit.read_and_emit_hecras_frames` reads it back
(`frames_only=True`) and all four depth-class composers emit the `flood_depth` group;
NEVER-OMIT (every step, no cap -- the `shared/frames` selector is dead for the
host-exec producers, LIVE only for the docker S-class). Cadence: the universal
`output_interval_min` lever DEFERS ENTIRELY (ADR 0287 fork 2, the 0284-3A precedent) --
it is ASYMMETRIC (6.x `Base Output Interval` attr-patch reachable; 2025 mapping
interval is a managed-engine decompile), and an asymmetric lever silently no-oping on
one path is the hidden-inconsistency class; BOTH cadence items are QUEUED for NATE.

MIGRATED ENGINES (ELMFIRE, derived-from-ToA burned-extent frames -- ADR 0288,
agent-side producer). ELMFIRE writes NO per-step field: its native product is ONE
cumulative `time_of_arrival` (ToA) raster (hours-from-ignition per cell). That single
raster IS the run's complete spatiotemporal solution, so frame N = the ToA masked to
`toa <= N*3600 s` reconstructs the burn frontier at hour N EXACTLY -- a LOSSLESS query
of the solved arrival-time field (the burned extent grows frame to frame; the pixel
value stays the arrival hour, so one colormap tells the where + the when), never an
invented intermediate state. The frame derivation + per-hour COG writes + `outputs.json`
write are 100% AGENT-SIDE in `postprocess_elmfire` (the solver container writes only the
ToA `.bil`), so NO image rebuild binds this leg. Frames are ADDITIVE (the typed
`FireSpreadLayerURI` / `ElmfireSensitivityLayerURI` ToA peak + flame/spread aux COGs +
the sensitivity chart + the river-barrier verdict machinery stay composer-built,
OPTION a). `quantity="fire_arrival"` -> the peak's `continuous_fire_arrival_hr` preset;
frame `t = hour * 3600` s.

- **fire_spread** + **spotting real-mode** (`model_elmfire_river_barrier_crossing`):
  `postprocess_elmfire(write_frames_manifest=True)` derives EVERY hourly bucket over the
  burn duration (`toa_frame_grids`, no cap) and writes `outputs.json`;
  `_frame_emit.read_and_emit_elmfire_frames` reads it back (`frames_only=True`) + emits
  the `fire_arrival` group. The derivation cadence (hourly, = the deck's `DTDUMP=3600`)
  is a NUMERICAL threshold-sweep parameter, honestly labeled -- NOT a solver output
  lever (the solver writes one cumulative ToA regardless).
- **sensitivity / transient / crown / initial_attack / spotting-verification**:
  PEAK-ONLY (`write_frames_manifest` defaults False) -- response-vs-knob sweeps (chart +
  representative peak) or single-case discriminants, not animations.

NEVER-OMIT: `toa_frame_grids` no longer calls `shared/frames._select_frame_time_indices`
(the cap is gone; the hourly derivation cadence is the only frame-count control). The
`shared/frames` selector stays LIVE for the docker S-class producers below.

## Campaign close -- the per-engine emit-on-solve mechanism (all 10 engines)

Every solver engine now surfaces its temporal result through the ONE `outputs.json`
seam. The mechanism differs by where the native field lives and who writes the manifest:

| Engine(s) | Native temporal product | Producer locus | Manifest writer | Frame mechanism | ADR |
|---|---|---|---|---|---|
| **SFINCS** / **GeoClaw** / **SWAN** | per-step raster field (depth / wave) | docker RASTER worker | worker (in-image) | worker rasterizes each saved step -> COG + `outputs.json` | 0280 / 0281 |
| **SWMM** | per-step node/link + surface field | host-exec (pyswmm in-agent) | agent | agent rasterizes each report step | 0282 |
| **landlab** | per-step grid field | host-exec (in-agent) | agent | agent writes each step COG | 0282 |
| **MODFLOW** | per-step concentration / temperature (mf6 OC) | host-exec (mf6 in-agent) | agent | per-species/quantity group, every saved step | 0284 |
| **TELEMAC** / **SCHISM** | native time-stepped UGRID mesh (SELAFIN / schout) | agent-side postprocess | agent | the mesh IS the temporal object (`kind="mesh"`, `crs_authid`), no per-step raster | 0283 / 0286 |
| **HEC-RAS** | per-step 2D depth field, TWO lineages | agent-side (6.x plan HDF via h5py; 2025 managed via mounted driver) | agent | dual-lineage: 6.x `Water Surface`-bed per step; 2025 `Cell Depth[i]` per step | 0287 |
| **ELMFIRE** | ONE cumulative ToA raster (no per-step field) | agent-side postprocess | agent | DERIVED: lossless per-hour `toa<=h` threshold of the single solved field | 0288 |

Common to all: `frames_only=True` on read-back skips the composer's typed peak (no
double registration); NEVER-OMIT (every saved/derived step, no post-hoc cap on the
host-exec + agent-side producers; the `shared/frames` subsample selector is LIVE only
for the docker S-class); each frame quantity resolves to the peak's physical preset so a
frame renders byte-consistently with the peak; a frame publish/read/emit miss degrades
to peak-only, never sinking the run.

FRAME COLLAPSE EXECUTED (ADR 0294, 2026-08-19): the three docker RASTER workers no
longer dual-write their frames into `publish_manifest.json`. A frame now exists in
exactly ONE manifest -- `outputs.json` -- so the two can never disagree.
`publish_manifest.json` keeps the non-frame entries: it is the metrics carrier (the
composers' narration scalars) + the legacy register-only fallback.
`list_run_frames` reads `outputs.json` first, with a LEGACY-run `publish_manifest`
frame fallback.

REMAINING (post-campaign, not blocking): telemac3d + any further L-class module legs,
the REST of the `publish_manifest` collapse (the file, the bespoke schema, and
`register_published_manifest.py` -- still live as the metrics carrier + fallback,
ledger row 19 narrowed), and -- separately, OPTION A -- the per-engine
`output_quantities` scaffold migration
(MODFLOW's DEAD half is deleted; swmm/landlab/openquake halves are still LIVE,
ADR 0284).

## Composition

Consumes `data/publish_layer` (durable vector/raster publish), `data/vector_tiles`
(densify), `data/processing/charts_common` (budget chart), and
`gates/context_budget` (compaction labels). Driven by `server/turn` +
`server/_core` on every tool result. Input layers surface here via the
emit-on-fetch seam rather than being hand-emitted by composers.

## Invariants / extension points

- A "modeled" envelope with empty layers NEVER reads status=ok (honesty floor).
- Input layers surface via `purpose=` on router fetches, never hand-emitted.
- MemoryFile-backed raster reads keep the file alive for the dataset lifetime
  (orphaning = GC-timing corruption).
