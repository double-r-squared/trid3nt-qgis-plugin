# ADR 0282 -- emit-on-solve: the M-class legs (SWMM + Landlab overland)

Status: LANDED (host-exec producers + composer frames-only forks +
byte-equivalence + cap deletions offline-provable). Date: 2026-08-17. Builds on
ADR 0280 (the seam + the frozen `outputs.json` schema) and ADR 0281 (the S-class
composer-fork pattern). See the LIVE CLOSE-OUT section for the two proving solves.

## Context

ADR 0280/0281 migrated the S-class engines (SFINCS/GeoClaw/SWAN) -- all
DOCKER-worker engines that write `outputs.json` from inside the container
entrypoint, and all returning an ENVELOPE whose metrics ride a separate
`publish_manifest.json` carrier, so `build_layers_from_outputs` could own ALL
publication as plain `LayerURI`s.

The M-class engines break both premises (verified against the code by the recon):

1. **Host-exec, not docker.** SWMM runs `pyswmm` IN-PROCESS
   (`run_swmm_local`); Landlab overland runs `exec_kind="exec"` from SOURCE
   (`workers/landlab/entrypoint.py`, reading `workers.*` directly). Neither has a
   worker image, so there is NO image-staleness gap -- the agent-side postprocess
   IS the `outputs.json` writer (schema §5.1 "host-exec engines"), and a code
   change is effective immediately.
2. **Typed peak return, not an envelope.** `model_swmm_urban_flood(...) ->
   SWMMDepthLayerURI` and `model_landlab_overland_flow_timeseries(...) ->
   LandlabOverlandTimeseriesLayerURI` return the PEAK layer DIRECTLY, with the
   narration scalars ON the layer object (`max_depth_m` / `flooded_area_km2` /
   `n_buildings_affected`; `wet_area_fraction` / `time_to_peak_s` / `n_frames`).
   If the seam owned the peak, the typed return would break and the scalars would
   vanish.

The recon's caps also did not survive contact: SWMM's cap is
`_select_frame_time_indices(n_steps)` (the shared `frames.MAX_FLOOD_FRAMES=144`)
in `postprocess_swmm`; Landlab's cap is a worker-side INTERVAL FLOOR
(`interval_s = max(output_interval_s, duration_s/48)` in
`component_chain.py:1249`, gated on `_MAX_TIMESERIES_SNAPSHOTS`), NOT a composer
subsample. Neither had a pre-existing ledger row.

## Decision -- NATE's ruling (OPTION a)

> The seam owns the TEMPORAL FRAMES ONLY; the typed peak layer + its narration
> scalars stay composer-built exactly as today. `outputs.json` still carries the
> peak entry for completeness, but the composer does NOT consume it (avoid
> double-registration). Byte-equivalence is measured on the FRAME render stream.

This is a DELIBERATE divergence from the S-class "seam owns all publication".
Concretely:

- `build_layers_from_outputs` grew a `frames_only: bool = False` param. Under
  `frames_only=True` the standalone (peak) and vector entries are NOT built or
  registered -- only the temporal frame groups. The peak COG uri is therefore
  never registered twice (the composer keeps its own typed peak via
  `observe_published_layer` at `publish_layer` time; the seam would otherwise
  register the SAME uri under `flood-depth-peak-*`).
- The peak entry still lands in `outputs.json` (a whole-run record) -- the seam
  just skips it.

## Decision -- what LANDED (per engine)

### The host-exec writer (its first real use)

`trid3nt_server/workflows/shared/outputs_manifest_io.write_outputs_manifest` is
the agent-side producer's object-store shim: it serializes entries via the
PURE-STDLIB `trid3nt_contracts.outputs_manifest` writer and PUTs the whole array
to `<scheme>://<runs_bucket>/<run_id>/outputs.json` -- the EXACT prefix the seam
reader reads back. Scheme-aware (s3 via the solver boto3 client, gs/file via
fsspec); the bucket resolves through the SAME `_get_runs_bucket` the reader uses
so write/read targets never drift. Best-effort by contract: the postprocess
wraps it in try/except and degrades to peak-only on a miss ("failure retracts
nothing"). This is the writer's FIRST real consumer (the S-class engines use the
worker MIRROR `workers/_raster_postprocess/outputs_manifest.py`).

### SWMM (`postprocess_swmm` + `run_swmm` + `raster_cell_mesh` + composers)

- **Writer**: `postprocess_swmm` builds the peak entry (`t=None`) + one entry per
  reporting step (`quantity="flood_depth"`, `name="Flood depth step N"`,
  `t` = elapsed seconds from the `.out` report steps read via
  `_read_node_depth_snapshots`, per-frame `bbox`) and writes `outputs.json`
  host-side. `band_stats` is OMITTED: `flood_depth` is a REGISTERED seam quantity
  (`continuous_flood_depth`, the pinned `0,3`/`ylgnbu`), so the seam resolves
  style WITHOUT consulting band stats and byte-equivalence holds without the field.
- **Cap killed**: `_select_frame_time_indices` is gone; every step is written
  (`_write_frame_cogs_and_entries`, never-omit). The bespoke frame-layer builder
  (`_emit_frame_layers` returning `SWMMDepthLayerURI` frames) is DELETED --
  `postprocess_swmm` returns `[peak]` only.
- **Cadence deck-side**: `output_interval_min` (new `SWMMRunArgs` field, the
  universal name) -> `REPORT_STEP` via `_report_step_hms` (interval-shaped, direct
  mapping). `None` keeps the legacy `"00:05:00"` (byte-identical deck). This is the
  SOLE frame-count control.
- **Composer forks**: `urban_flood` + `dual_drainage` read `outputs.json` back
  (`_read_swmm_frame_layers` -> `build_layers_from_outputs(frames_only=True)`),
  publish + emit the CONTEXT frames out-of-band. `dual_drainage` gains the depth
  animation for the FIRST time (it previously discarded `layers[1:]`). Absent
  `outputs.json` -> peak-only (an honest degrade). The typed peak is unchanged.

### Landlab overland (`postprocess_landlab` + `component_chain` + composer)

- **Writer**: `postprocess_landlab_overland_timeseries` reprojects/uploads each
  `depth_step_NN` COG and records it in `outputs.json` (`quantity="flood_depth"`,
  `name="Overland depth step N"`, `t` = the worker's REAL snapshot elapsed seconds
  from `result.max_cell_series[i].time_s`, ordinal fallback keeps `t` distinct +
  monotonic). Peak entry `t=None`. Returns `[peak]` only.
- **Cap killed**: the worker interval FLOOR `max(output_interval_s, duration_s/48)`
  + `_MAX_TIMESERIES_SNAPSHOTS` are RETIRED in
  `component_chain._run_overland_flow_timeseries` -- `output_interval_s` is honored
  EXACTLY (never-omit). Runs exec-from-source, so this is effective immediately
  (NO image rebuild).
- **Cadence vocabulary**: the native lever `output_interval_s` STAYS; the
  universal `output_interval_min` ALIASES it (`output_interval_s =
  output_interval_min * 60`) per the 0281 precedent -- documented in the worker
  docstring + this ADR, NOT double-threaded (no redundant param with no distinct
  reader).
- **Composer fork**: `overland_flow_timeseries` reads `outputs.json` back
  (`_read_overland_frame_layers` -> `frames_only=True`), publishes + emits the
  frames. The typed peak is unchanged.

## Byte-equivalence (the migration bar) -- FRAME render stream identical

For a fixed run the emitted FRAME layer-event render stream (name, layer_type,
style_preset, role, units, bbox, resolved `&rescale/&colormap`) from the NEW
`outputs.json` + seam(frames_only) path is BYTE-IDENTICAL to the OLD bespoke
frame layers. Tests: `tests/test_swmm_outputs_seam.py`,
`tests/test_landlab_outputs_seam.py` (pure/offline -- entries built by the same
host-exec writer, no solve needed). The ONE explained divergence is the internal
`layer_id` STEM: the seam mints `flood-depth-frame-*`, the register/bespoke path
used `swmm-depth-frame-*` / `landlab-overland-depth-frame-*`. The layer_id is an
idempotence key -- web temporal grouping rides the `name` token
(`detectSequentialGroups`, unchanged) -- so the stem swap renders identically.
This is the same explained divergence ADR 0281 set.

The end-to-end round-trip (real pyswmm solve -> `postprocess_swmm` writes
`outputs.json` to a stateful fake S3 -> the composer reads it back via the seam
and emits the frames) is pinned live-in-process by
`tests/test_run_swmm_local_chain.py::test_full_local_chain_emits_peak_plus_frames`.

## Fallback-audit proximity (rows 18/20) -- NOT worsened

The recon flagged the fallback inventory rows near this work: row 18 (SWMM outfall
relocation, `mesh/raster_cell_mesh.py`) and row 20 (Landlab CRS assumption,
`workers/_landlab_postprocess/postprocess.py:134`). NEITHER is in scope. This
landing touches `raster_cell_mesh` ONLY at the `REPORT_STEP` line (a new
deck-cadence key) -- not the outfall relocation -- and touches
`component_chain.py` (the cap) + the agent postprocess (the writer) -- not the
CRS reproject line. Proximity noted; not worsened.

## Consequences

- SWMM (urban_flood + dual_drainage) + Landlab overland join the `outputs.json`
  seam; the register/on-box paths stay the one-release fallback + metrics carrier
  (row-19 collapse gated on the LAST engine). Legacy behaviour is byte-unchanged
  when `outputs.json` is absent (peak-only).
- NO IMAGE REBUILD anywhere: SWMM is in-process pyswmm; Landlab is
  `exec_kind="exec"` from source. offline-green == deploy-green.
- Two NEW ledger rows registered + DELETED (the SWMM `_select_frame_time_indices`
  cap; the Landlab interval floor + `_MAX_TIMESERIES_SNAPSHOTS`).
- Offline four-slice suite at EXACT baseline (4 fetch_resolution + 2 river_dye);
  all swmm/landlab suites green.

## LIVE CLOSE-OUT (2026-08-17) -- both legs green, NO image rebuild

Gates (foreground): daemon restarted (`make agent`) + `scripts/ws_smoke.py`
`all_passed=True`. Offline four-slice suite at EXACT baseline -- [a-e] 1468
passed; [f-o] 4 failed (the 4 `fetch_resolution` baseline) 6383 passed; [p-r] 2
`river_dye` baseline (+ 1 PRE-EXISTING `test_naip_passthrough` rasterio-COG
environment failure, proven unrelated by re-running with the change set stashed)
2013 passed; [s-z] 1402 passed. `contracts/tests` 721 passed. The two new seam
suites + the updated `test_postprocess_swmm` + `test_run_swmm_local_chain`
(end-to-end write->seam-read round-trip through a stateful fake S3) green.

**SWMM** -- live solve through the registered composer (`model_swmm_urban_flood`,
Old Town Alexandria VA, `output_interval_min=10`), run
**`01M08MVWRDT9FX7YA94GXT9DYE`**: `outputs.json` = schema 1, engine swmm, **13
entries** (1 peak `t=None` + 12 frames), frame `t` = 0..6600 s at a **uniform
600 s cadence** (= `output_interval_min=10` -> `REPORT_STEP 00:10:00`, deck-side
cadence PROVEN). The SEAM (`frames_only=True`) built **12 frame layers, NO peak**
(no double-registration), one temporal group `flood-depth-<run_id>`, replay
stamps; **all 12 frames published (never-omit)**; the composer emitted 12/12
through the publish chokepoint. Typed peak intact: `swmm-depth-peak-<run_id>`
role primary, `max_depth_m=0.2061`, `flooded_area_km2=0.0164`,
`n_buildings_affected=0`. Reopen (re-read + rebuild) -> identical `layer_id`s
(idempotent). NO IMAGE REBUILD (in-process pyswmm; host-exec writer's first live
use).

**Landlab overland** -- live solve through the registered composer
(`model_landlab_overland_flow_timeseries`, Boulder CO foothills,
`output_interval_s=300`), run **`01M08MYKT549RFJ9F10TFDMVFV`**: `outputs.json` =
schema 1, engine landlab, **13 entries** (1 peak + 12 frames), frame `t` = the
worker's REAL snapshot elapsed seconds **`[300, 603.58, 903.18, 1203.62, 1502,
..., 3600]`** -- UNEVEN (CFL-driven), which proves the `t` is the real worker
timing, not a fabricated even grid. The SEAM (`frames_only=True`) built **12
frame layers, NO peak**, one temporal group; all 12 published (never-omit); 12/12
emitted. Typed peak intact: `landlab-overland-depth-peak-<run_id>`,
`max_depth_m=6.074`, `wet_area_fraction=0.096`, `n_frames=12`,
`time_to_peak_s=3600`. Reopen -> identical `layer_id`s. NO IMAGE REBUILD
(`exec_kind="exec"` from source; the cap change took effect immediately).

Both ledger rows (the SWMM `_select_frame_time_indices` cap; the Landlab interval
floor + `_MAX_TIMESERIES_SNAPSHOTS`) are DELETED + proven live.
