# ADR 0287 -- emit-on-solve: the HEC-RAS leg (L-class, TWO agent-side producers)

Status: LANDED (two agent-side frame producers + composer frames-only forks + the
recon-row correction + offline seam tests + BOTH live solves). Date: 2026-08-18.
Builds on ADR 0280 (the seam + the frozen `outputs.json` schema), ADR 0282 (the
M-class host-exec writer + the `frames_only` OPTION-a ruling), ADR 0284 (the MODFLOW
host-exec transport leg -- the `_frame_emit` shared-helper + never-omit pattern this
ADR replicates). See the LIVE CLOSE-OUT for the two proving solves.

The predecessor's recon (`/tasks` STOP-CLEAN report) REFUTED the recon-table's
"HEC-RAS = L-class / peak-only / no per-step field" framing: BOTH HEC-RAS lineages'
result HDFs genuinely carry per-timestep 2D fields at an honest cadence. This is a
real **FRAMES** case (raster, cell-grid native -- NOT mesh). Frames are ADDITIVE (no
prior frame emission existed; the peak COG stays), both producers run AGENT-SIDE, so
**NO image rebuild binds this leg** (verified: `postprocess_hecras` opens the plan
HDF with h5py in-agent; `rog2025_pipeline.build_depth_frames` runs mounted-driver
in-process). The build turned on NATE-reserved forks; the orchestrator ruled them by
precedent under NATE's AFK grant.

## Context -- how HEC-RAS breaks the recon-table's premise

1. **Per-step 2D fields exist on BOTH lineages (verified on real solved HDFs).**
   - **6.x** (`Muncie.p04.tmp.hdf`): `Results/Unsteady/Output/Output Blocks/Base
     Output/Unsteady Time Series/2D Flow Areas/<area>/Water Surface` = **(289, 5765)**
     -- 289 timesteps x 5765 cells; parallel `.../Unsteady Time Series/Time` = (289,)
     in DAYS (0..1.0), an even **5.00-min** step (`Base Output Interval='5MIN'`).
   - **2025 managed** (`rog2025_pipeline`): `/Results/Output Blocks/Base Output/2D
     Flow Areas/Base Mesh/Cell Depth` = **(Nt, Nc)** per-step depth (metres), with
     `/Results/Output Blocks/Base Output/Time` = (Nt,) DAYS -- already read for the
     peak metrics today (`.max(axis=0)`).
   - Frame `t = t_days * 86400` s (honest DAYS->seconds; both lineages store days).
2. **The lineage split is by PATH, not template.** `flood_2d` straddles both: the
   inflow path is 6.x (`postprocess_hecras`), the `design_storm_mm_per_hr` RoG path
   is the 2025 managed engine (`build_depth_cog` / `build_depth_frames`).
3. **Additive, no byte-equivalence baseline.** No frame emission ever existed
   (`postprocess_hecras` read the Summary max-WSE into ONE peak COG; the 2025 path
   `.max(axis=0)` into ONE COG) -- the bar is CORRECTNESS + the live proofs, exactly
   like MODFLOW 0284.
4. **Typed peak, not an envelope.** Each composer returns the `HecrasDepthLayerURI`
   peak with the narration scalars on it (`depth_max_ft` / `wet_cell_count` /
   `peak_inflow_cfs` / `wse_max_ft` / `volume_error_pct`). The seam owns the TEMPORAL
   FRAMES ONLY (`frames_only=True`, OPTION a); the typed peak + charts + the 2D
   mesh-preview vector stay composer-built.

## Decision -- the three forks (orchestrator, by precedent under NATE's AFK grant)

**FORK 1 -- template scope: FRAMES for the four depth-class flood templates.**
`riverine_flood`, `levee_breach`, `flood_2d` (BOTH the 6.x inflow path and the 2025
RoG path) land per-step depth frames. `culvert_embankment_flow` stays
**CHARTS/PEAK-ONLY** -- the 0284 discriminant-deliverable class: the A/B
present-vs-absent question (ponding-vs-steady series + mass-balance bars + the A peak
COG) is answered by the discriminant, NOT an animation (A-case Cell Depth exists so
frames are *possible*, but the question class does not want them). Sub-fork:
`riverine_flood`/`levee_breach` FRAMES ride the ALREADY-opt-in Muncie demonstration
run (`run_demo_geometry=True`, banner-labeled DEMONSTRATION GEOMETRY per law 9) --
additive presentation of a consented run, no new synthetic physics.

**FORK 2 -- the `output_interval_min` lever DEFERS ENTIRELY (the 0284-3A precedent).**
The lever is ASYMMETRIC: reachable on 6.x (the `Base Output Interval` HDF5 attr on
`Plan Data/Plan Information`, e.g. `'5MIN'` -- distinct from `Computation Time Step
Base`=`'10SEC'`, the solver step `deck_edit.py` already patches), NOT reachable on
2025 (the managed engine's default mapping interval is inside `synthdrv.dll`/
`Ras.Engine`, a decompile-level hunt). Wiring ONE surface but not the other under a
"universal" name is the **hidden-inconsistency class** an asymmetric lever silently
no-oping introduces. So the lever is DEFERRED; frames land at the deck-default
cadence first. BOTH cadence items are QUEUED for NATE:
  - the 6.x `Base Output Interval` attr-patch (`output_interval_min=N -> "{N}MIN"`,
    h5py attr write pre-stage; `None` byte-identical), WITH the same empirical A/B
    cadence proof `deck_edit.py` documents for the `.bNN` (does patching the attr
    actually move the WRITTEN cadence? -- a build-time gate, not yet proven);
  - the 2025 managed-engine mapping-interval decompile hunt (`synthdrv.dll`/
    `Ras.Engine`; no output/mapping-interval author found in `hecras_deck2d.py` /
    `hecras_pure2d_deck.py` / `hecras_meteorology.py` / `Driver.cs`).

**FORK 3 -- two agent-side producers.** 6.x frames in `postprocess_hecras` (serves
riverine_flood + levee_breach + flood_2d inflow); 2025 frames as `build_depth_frames`
in `rog2025_pipeline` driven by the RoG composer. Both agent-side, NO rebuild.

## Per-template verdict table (the coverage-law denominator)

| Template | Lineage | Native per-step field | Verdict |
|---|---|---|---|
| `hecras_riverine_flood` | 6.6 RasUnsteady (Muncie) | WS (Nt,Nc) 5-min | **FRAMES** -- depth = WS[t]-bed, masked identically to the peak; rides the run_demo_geometry opt-in |
| `hecras_levee_breach` | 6.6 RasUnsteady (Muncie lateral-struct) | WS (Nt,Nc) 5-min | **FRAMES** (breach-fill is inherently temporal); levee-HELD (dry) = honest empty, no frames |
| `hecras_flood_2d` inflow | 6.6 RasUnsteady (authored AOI mesh) | WS (Nt,Nc) | **FRAMES** -- shares the 6.x `postprocess_hecras` producer |
| `hecras_flood_2d` RoG | 2025 managed | Cell Depth (Nt,Nc) | **FRAMES** -- via the 2025 `build_depth_frames` producer (same georef as `build_depth_cog`) |
| `culvert_embankment_flow` | 2025 managed (A/B) | Cell Depth (Nt,Nc), A-case | **CHARTS/PEAK-ONLY** -- the A/B present-vs-absent discriminant + the A peak COG is the deliverable (0284 fork class) |

## What LANDED

### The 6.x producer (`postprocess_hecras.py`, host-exec)

- `_read_wse_steps(plan_hdf, area_name) -> (wse[Nt,Nc], bed, t_days)` reads the
  Unsteady Time Series WS stack + the parallel Time (DAYS) + the cell bed; returns
  `None` for a summary-only solve (an honest peak-only degrade).
- `_depth_for_step(wse_step, bed)` masks EXACTLY like `_read_depth_per_cell` (HDF
  fill -> NaN, WSE<=0 -> NaN, `WSE-bed` kept only where finite and > 0).
- `_write_6x_frame_entries(...)` rasterizes a cell->pixel LABEL raster ONCE (the
  peak's `all_touched=False` footprint) then indexes every step into it (289 frames
  cost 289 COG writes, not 289 polygon rasterizations), writing one `outputs.json`
  entry per step (`quantity="flood_depth"`, `name="Flood depth step N"`,
  `t=t_days*86400`, per-frame `bbox`=the peak bbox, `band_stats` OMITTED -- the
  quantity is REGISTERED). **NEVER-OMIT**: every one of the 289 steps publishes;
  there is NO subsample cap (the `shared/frames.MAX_FLOOD_FRAMES` selector is dead
  for the host-exec producers, exactly as MODFLOW 0284 and SWMM 0282 -- verified: the
  producer never calls `select_frame_time_indices`). A step that fails to encode
  stops the loop; the frames already written + the peak stand.
- `postprocess_hecras` calls the producer + `_write_hecras_outputs_manifest`
  (`engine="hecras"`, best-effort host-side PUT via the shared
  `outputs_manifest_io.write_outputs_manifest`) for a WET solve; a levee-HELD dry run
  writes NO frames (honest empty). `metrics` gains `run_id` + `frame_count`.

### The 2025 producer (`rog2025_pipeline.build_depth_frames`, agent-side)

The frame sibling of `build_depth_cog` / `build_depth_cog_unstructured`: same georef
(structured (row,col) OR the graded-mesh KDTree idx + the catchment mask + the
UTM->4326 warp computed ONCE), but reads `Cell Depth[i]` per STEP instead of
`.max(axis=0)`, plus the parallel `Base Output/Time` (DAYS). Writes one COG per step
(`rog_depth_frame_NN.tif`, feet via the `1/0.3048` scale, matching the peak) +
returns `{cog, bbox4326, t_days, depth_max}`. NEVER-OMIT (no cap). Pure rasterio/scipy
(no server deps); the RoG composer (`flood_2d._write_rog_frame_manifest`) uploads each
COG + writes `outputs.json` host-side. The proven peak functions `build_depth_cog` /
`build_depth_cog_unstructured` are UNTOUCHED (regression-safety; the frame function
shares their georef math, noted in its docstring, rather than refactoring them).

### The composer forks (`workflows/hecras/_frame_emit.py`, shared)

`read_and_emit_hecras_frames(emitter, run_id, bbox)` -- the HEC-RAS analogue of
`modflow._frame_emit`: reads `outputs.json` back (`build_layers_from_outputs(
frames_only=True)` -> the peak entry is skipped, the composer keeps its typed peak),
routes each frame COG through `publish_layer` (the render chokepoint), and emits it as
a `context` `LayerURI` out-of-band so the web `detectSequentialGroups` scrubber group
forms with the peak's `continuous_flood_depth` colormap. Best-effort: an absent
manifest / no emitter / a publish miss degrades to peak-only, never sinks the run.
Wired into all four depth-class composers: `riverine_flood`, `levee_breach`,
`flood_2d` (inflow after the inflow chart; RoG after the peak publish).

### Styling -- frames render byte-consistently with the peak

`quantity="flood_depth"` resolves to `continuous_flood_depth`
(`resolve_style_preset`), the SAME preset `HECRAS_DEPTH_STYLE_PRESET` the peak COG
publishes through -- so a frame and the peak share the pinned `0,3 / ylgnbu` style. No
new registry row; no band_stats needed (registered quantity).

## Storage arithmetic (honest, per the schema doc's byte-based retention stance)

289 Muncie frames is the never-omit count (5-min cadence over a 1-day sim). Per the
schema §6 measurement, a depth COG of this grid class is ~1 MiB (constant across
frames -- same grid/overview structure), so a full Muncie run's frame set is
**~289 x ~1 MiB ~= 290 MiB of raster** under the run prefix, plus a sub-30-KiB
`outputs.json`. This is the honest cost of never-omit at a fine deck cadence; the
schema §6 retention stance is a BYTE budget per run/session (age out the WHOLE prefix
on a TTL, the §5.3 replay-field tolerance rule makes that safe), NOT a frame-count
cap. When the DEFERRED `output_interval_min` lever lands (fork 2), a coarser
`Base Output Interval` is the deck-side control that reduces the written step count at
the source -- never a post-hoc thin.

## Offline tests

`tests/test_hecras_outputs_seam.py` (pure/offline -- entries built by the SAME
`build_entry`/`_peak_frame_entry` both producers use, no HEC-RAS solve): `flood_depth`
resolves to the peak's `continuous_flood_depth` preset; `frames_only` skips the peak +
styles every frame as flood_depth + single `flood-depth-{run_id}` group + `t` in
seconds monotonic; NEVER-OMIT (289 frames, no cap); the peak entry publishes as a
standalone primary WITHOUT `frames_only`; and the KEY correctness guard --
`_depth_for_step` masks dry cells (WSE<=0), HDF fill, and WSE<bed to NaN exactly like
the peak reader.

## Consequences

- The four depth-class HEC-RAS templates join the `outputs.json` seam; culvert stays
  charts/peak-only. NO image rebuild (both producers agent-side; state explicitly).
  Legacy behaviour is byte-unchanged when `outputs.json` is absent (peak-only, an
  honest degrade).
- The `output_interval_min` lever is the one deferred item; both cadence sub-items
  (6.x attr-patch + empirical proof; 2025 decompile hunt) are QUEUED for NATE.
- No deletions (additive leg -- DELETION_LEDGER untouched); the `shared/frames`
  subsample selector stays LIVE for the docker-worker S-class producers (SFINCS/
  GeoClaw/SWAN/ELMFIRE), which is why it was not removed.

## LIVE CLOSE-OUT (2026-08-18) -- both lineages green, NO image rebuild

Both solves ran through the REGISTERED composers with a capturing `PipelineEmitter`
bound as `_CURRENT_EMITTER` (`scripts/proof_hecras_seam_0287.py`), real MinIO s3,
`TRID3NT_SOLVER_BACKEND=local-docker`. NO image rebuild (both producers agent-side --
the docker images `hecras:latest` (6.x solver) + `hecras2025-authoring:latest` (2025
mesh authoring) are the UNCHANGED solve/author steps).

**6.x -- `model_hecras_riverine_flood(run_demo_geometry=True, flow_scale=1.0)`**
(consented Muncie White River, banner-labeled DEMONSTRATION GEOMETRY), run
**`01M0ASQNG5RX91N3ZXN5H973WF`**: `outputs.json` = schema 1, engine hecras, **290
entries** (1 peak + **289 frames**). Frame `t` = the plan HDF Unsteady Time Series
Time DAYS -> seconds `[0.0 .. 86400.0]` (0..1.0 day, the real 5-min cadence). The SEAM
(`frames_only=True`) built **289 frame layers, NO peak**, **1 temporal group**
`flood-depth-01M0ASQNG5RX91N3ZXN5H973WF`, `t` monotonic + distinct, style
**`continuous_flood_depth`** for every frame; names `"Flood depth step 1".."step
289"`. **All 289 frames published (never-omit)**; the composer emitted **289/289**
frame rows out-of-band (session-state `loaded_layers` = 290 incl. the typed peak).
Typed peak intact: `depth_max_ft=20.236`, `wet_cell_count=4881`,
`peak_inflow_cfs=21000.0`, `wse_max_ft=951.927` (role primary). **REOPEN** (re-read
`outputs.json` + rebuild) -> **identical 289 layer_ids** (idempotent).

**2025 -- `model_hecras_flood_2d_rog(bbox=Coweeta Creek NC, design_storm_mm_per_hr=25,
storm_duration_hr=6, resolution_m=60)`** (a real US site; rain-on-grid on the managed
engine), run **`01M0AT1SBCVXKN076DESV7K6BD`**: `outputs.json` = schema 1, engine
hecras, **74 entries** (1 peak + **73 frames**). Frame `t` = the managed-engine Base
Output Time DAYS -> seconds `[0.0 .. 21600.0]` (0..0.25 day = the 6-h storm window; 73
steps = the managed default ~5-min mapping cadence -- real solver timing, not a
fabricated grid). The SEAM built **73 frame layers, 1 temporal group**
`flood-depth-01M0AT1SBCVXKN076DESV7K6BD`, `t` monotonic + distinct, style
**`continuous_flood_depth`** for every frame; names `"Flood depth step 1".."step 73"`.
**All 73 published (never-omit)**; the composer emitted **73/73** out-of-band
(loaded_layers = 74 incl. the peak). Typed peak intact: `depth_max_ft=24.478`,
`wet_cell_count=7462` (role primary). REOPEN -> **identical 73 layer_ids** (idempotent).

FOUND + FIXED (pre-existing, one line): `model_hecras_flood_2d_rog`'s uniform-mesh
branch passed the `_fetch_dem_local` return TUPLE `(local_path, s3_uri)` straight to
`run_rog2025` (which `rasterio.open`s it) instead of unpacking the local path -- the
6.x inflow path unpacks it identically. Broken since commit `67167b98a` (2026-08-09,
when `_fetch_dem_local` gained the s3-uri return); it blocked EVERY RoG uniform-mesh
solve. Fixed to `dem_tif, _dem_s3_uri = ...` (the 6.x path's exact form). This
unblocked the 2025 live proof above.

### Gates (all green)

- Offline four-slice suite at EXACT baseline: a-e 1471 passed / 0 failed; f-o 6411
  passed / **4 failed (the fetch_resolution baseline)** / 1 xfailed; p-r 2015 passed /
  **2 failed (the river_dye baseline)**; s-z 1402 passed / 0 failed. **6 failures
  total, all baseline (4 fetch_resolution + 2 river_dye), ZERO new.**
- `tests/test_hecras_outputs_seam.py` (5) + `test_hecras_landing` +
  `test_hecras_flood2d_template` + `test_outputs_seam` green;
  `test_rog2025_pipeline` green (build_depth_cog UNTOUCHED); contracts (721) green;
  `test_input_layer_surfacing` (20) green (no allowlist change -- frames emit via
  `add_loaded_layer`, not `publish_input_layer`); law9 + outputs-schema green.
- Daemon restarted (`make agent`) + `scripts/ws_smoke.py` **`all_passed=True`**.
- NO images built (both producers agent-side; state explicitly).
