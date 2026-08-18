# ADR 0288 -- emit-on-solve: the ELMFIRE leg (derived-from-ToA burned-extent frames)

Status: LANDED (agent-side frame producer moved onto the seam + `_frame_emit`
composer fork + fire_spread/spotting frames-only forks + never-omit cap removal +
offline seam test + BOTH live solves + one pre-existing bug fixed). Date: 2026-08-18.
Builds on ADR 0280 (the seam + the frozen `outputs.json` schema), ADR 0282 (the
`frames_only` OPTION-a ruling), ADR 0284 (the MODFLOW host-exec transport leg -- the
`_frame_emit` shared-helper + never-omit pattern), ADR 0287 (the HEC-RAS leg -- the
agent-side-producer precedent this ADR mirrors). See the LIVE CLOSE-OUT for the two
proving solves.

The recon was completed by a predecessor build agent (workflow `wf_ff0c0758-fb0`)
that DIED on an API server error mid-build, after finishing its recon and the
producer edit but before writing `_frame_emit.py`. Its load-bearing findings were
re-verified against the code here (cheap greps, cited below) before completion; the
recon credit is the predecessor's where confirmed.

## Context -- ELMFIRE breaks the raster-frames premise the OTHER way

ELMFIRE is a FIRE-spread solver, not a depth solver: its native temporal product is
NOT a per-step field stack. The solver writes ONE cumulative `time_of_arrival`
(ToA) raster -- hours-from-ignition per cell, NaN where never burned. That single
raster IS the run's complete spatiotemporal solution.

**The verdict -- DERIVED FRAMES from ToA (a lossless query, never invented physics).**
Thresholding a complete arrival-time field reconstructs the burn frontier at any hour
EXACTLY: frame N = the ToA raster masked to cells with `toa <= N*3600 s` (the burned
extent GROWS frame to frame; the pixel value stays the arrival HOUR, so one colormap
tells both the burned WHERE and the arrival WHEN). This is a lossless query of the
solved arrival-time field -- NOT an interpolation, NOT an invented intermediate state,
NOT a fabricated intermediate solve. It is the fire-spread analogue of reading a
level-set's sub-level sets: the information was already in the field.

**ToA-semantics verification (re-confirmed here).** The deck runs `DTDUMP=3600`
(`run_elmfire.py:324` -- "`DTDUMP` stays hourly (3600 s)"); the postprocess ALREADY
derived hourly frames from ToA (`toa_frame_grids`, a per-hour `np.where(toa <= h*3600,
toa_hr, nan)` threshold) and shipped them as returned `layers`. So the physics
derivation was already proven correct in-tree; this leg MOVES those exact frames onto
the `outputs.json` seam and removes the post-hoc frame cap. The threshold correctness
is guarded offline by `test_toa_frame_grids_threshold_per_hour` (monotone-growing
extent, `nanmax(frame_N) <= N`, final frame == every burned cell).

**Derivation cadence is a NUMERICAL parameter, honestly labeled -- NOT a solver
lever.** The hourly cadence is the threshold-sweep bucket width (aligned to the deck's
`DTDUMP=3600` so the buckets match the dump grid), a post-solve derivation knob. The
solver writes ONE cumulative ToA raster regardless of cadence; sweeping it at a finer
bucket width would produce more frames from the SAME solved field, changing nothing
physical. The `toa_frame_grids` docstring states this verbatim ("a numerical
DERIVATION parameter ... NOT a solver output lever").

**Agent-side producer -- NO image rebuild (IMAGE LAW does not bind).** The solver
container (`trid3nt/elmfire:dev`) writes the ToA `.bil`; the frame derivation, the
per-hour COG writes, the uploads, and the `outputs.json` write are 100% AGENT-SIDE in
`postprocess_elmfire` (the ELMFIRE postprocess runs in the agent process, like
MODFLOW's host-exec producers). The dead run's diff touched ONLY
`workflows/elmfire/postprocess_elmfire.py`; this completion touched only
`workflows/elmfire/*` + tests + docs -- ZERO `workers/` / `services/` changes, so no
solver image is stale. Verified: `git diff --stat` shows no worker path. The
IMAGE-STALENESS law does NOT bind this leg; stated explicitly.

## Decision -- the forks (orchestrator, by ADR-0284/0287 precedent under NATE's AFK grant)

**FORK 1 -- template scope: FRAMES for the two frame-consuming fire composers.**
`fire_spread` (the surface-spread burned-extent animation) and `spotting`'s real-mode
river-barrier demo (`model_elmfire_river_barrier_crossing` -- the ON-case ember-carried
spread animates over the river) opt into `write_frames_manifest=True`. The
sensitivity/transient/crown/initial-attack/verification composers render the typed
PEAK ALONE (`write_frames_manifest` defaults False): those are response-vs-knob sweeps
(a chart + a representative-run peak COG) or single-case V&V, NOT animations -- the
question class does not want per-hour frames, and writing throwaway frame COGs for
them is waste. This is the 0284/0287 discriminant-class distinction applied to fire.

**FORK 2 -- OPTION a: the seam owns the temporal frames; the typed peak stays
composer-built.** The `FireSpreadLayerURI` / `ElmfireSensitivityLayerURI` peak (with
`burned_area_km2` / `fire_arrival_max_hr` / `max_flame_length_m` /
`max_spread_rate_m_min` narration scalars) + the flame-length/spread-rate aux context
COGs + the sensitivity chart + the river-barrier verdict machinery are UNTOUCHED and
stay composer-built. Only the burned-extent ANIMATION moves to the seam. The frames no
longer ride the returned `layers` list.

## Per-template verdict table (the coverage-law denominator)

| Template | Composer | Native product | Verdict |
|---|---|---|---|
| `elmfire_fire_spread` | `model_elmfire_fire_spread` | cumulative ToA raster | **FRAMES** -- hourly `toa<=h` threshold = the burned-extent animation; the typed ToA peak + flame/spread aux stay composer-built |
| `elmfire_spot_fire_barrier_crossing` (real) | `model_elmfire_river_barrier_crossing` | ToA (ON case) | **FRAMES** -- the ON-case ember-carried burned extent animates; OFF-vs-ON verdict + chart untouched |
| `elmfire_spot_fire_barrier_crossing` (verification) | `model_elmfire_spot_fire_barrier_crossing` | ToA, constant deck | **PEAK-ONLY** -- a synthetic OFF/ON discriminant assertion, not a landscape animation |
| `elmfire_crown_fire` / `crown_ros` | crown composers | ToA + crown flags | **PEAK-ONLY** -- crown-state discriminant + peak |
| `elmfire_initial_attack` | initial-attack | ToA | **PEAK-ONLY** -- containment-time metric + peak |
| sensitivity (`ltw_ceiling`, `wind_fluctuation`, `live_moisture`) | `_sensitivity_common` sweeps | ToA per knob | **PEAK-ONLY** -- response-vs-knob chart + representative peak |
| transient (`wind_schedule`, `dead_fuel_interp`) | transient composers | ToA | **PEAK-ONLY** -- the transient-forcing peak + chart |

## What the dead run did (INHERITED, confirmed sound) vs what this completion REDID

**Inherited + kept:** `postprocess_elmfire.py` -- the producer edit. `toa_frame_grids`
loses its `_select_frame_time_indices` post-hoc cap (never-omit); a new
`_write_burned_frames_manifest` derives every hourly frame, writes each per-hour COG
via the existing `_upload_cog_to_runs_bucket`, and writes `outputs.json` via the shared
`outputs_manifest_io.write_outputs_manifest` (`quantity="fire_arrival"`, `t=hour*3600`
s, name `"Burned area step N"`, + a peak `t=None` entry); the frames leave the returned
`layers`; a new `write_frames_manifest` param (default False) gates it. This edit was
re-read line-by-line and confirmed coherent (the imports it dropped -- `frame_layer_id`,
`_select_frame_time_indices` -- are genuinely now-unused; the `_mk_layer`
`frame_burned_km2` param it removed had no other caller).

**Redone / completed (the dead run never reached these):**
- `workflows/elmfire/_frame_emit.py` -- the composer half it DIED writing. Mirrors
  `hecras._frame_emit` / `modflow._frame_emit` exactly: `read_elmfire_frame_layers`
  (reads `outputs.json` back, `build_layers_from_outputs(frames_only=True)` -> the peak
  entry skipped, one `fire_arrival` group), `emit_elmfire_frames` (routes each frame COG
  through `publish_layer` then `add_loaded_layer` out-of-band; a publish miss is honestly
  dropped), and `read_and_emit_elmfire_frames` (the best-effort composer entry point --
  no run_id / no emitter / any failure degrades to peak-only, never sinks the run).
- `fire_spread.py` -- passes `write_frames_manifest=True` to its postprocess call and
  calls `read_and_emit_elmfire_frames(run_id=solve_run_id)` after the aux emit.
- `_sensitivity_common.publish_primary_from_out_dir` -- a `write_frames_manifest=False`
  param forwarded to postprocess (the sweep templates unchanged); the spotting real-mode
  ON case passes True + calls `read_and_emit_elmfire_frames(run_id=on_run)`.
- Tests -- `test_model_fire_spread_chain.py` migrated off the in-`layers` frame
  contract (frames now assert on the captured `outputs.json` entries + the seam
  read-back); `test_elmfire_outputs_seam.py` added (the pure/offline seam semantics,
  mirroring `test_modflow_outputs_seam.py`).

## FOUND + FIXED (pre-existing, one line -- surfaced by the live proof)

`model_elmfire_river_barrier_crossing` stuffed the string `"verdict": verdict` into the
`ElmfireSensitivityLayerURI.summary` dict, which is typed `dict[str, float]`
(`elmfire_contracts.py:411`) -- so EVERY real-mode river-barrier solve raised a pydantic
`float_parsing` ValidationError at the layer construction (BEFORE any frame code). The
real-mode composer path had never been live-exercised through its own typed contract
(the standalone `proof_elmfire_river_barrier.py` replicates the verdict logic inline and
never builds the contract object with that summary), so the bug sat latent. Fixed by
dropping the redundant string entry: the verdict already rides the layer NAME
("... - jumped") and the numeric `break_jumped` / `off_side_leaks` summary flags. No
consumer read `summary["verdict"]` (grep-confirmed).

## Styling -- frames render byte-consistently with the peak

`quantity="fire_arrival"` resolves to `continuous_fire_arrival_hr`
(`quantity_styles.py:104`, `resolve_style_preset` -- verified non-fallback), the SAME
preset the primary ToA COG publishes through, so a frame and the peak share the pinned
arrival-hours colormap. No new registry row.

## NEVER-OMIT (the cap removal)

`toa_frame_grids` no longer calls `shared/frames._select_frame_time_indices`: it returns
one frame per burn hour over the whole duration, unconditionally. There is NO post-hoc
frame thinning -- the derivation cadence (hourly, = `DTDUMP`) is the ONLY control on
frame count, and it is a numerical parameter, not a cap. The `shared/frames` subsample
selector stays LIVE for the docker S-class producers (SFINCS/GeoClaw/SWAN), which is
why it is not deleted.

## Offline tests

`tests/test_elmfire_outputs_seam.py` (pure/offline -- entries built by the SAME
`build_entry` the producer uses, no ELMFIRE solve): `fire_arrival` resolves to
`continuous_fire_arrival_hr` (non-fallback); `frames_only` skips the peak + styles every
frame as fire_arrival + all context role + `t` (burn-hour seconds) monotonic; ONE
`fire-arrival-{run_id}` group; NEVER-OMIT (30 frames, no cap). Plus the migrated
`test_model_fire_spread_chain.py`: postprocess returns peak+aux ONLY (no in-`layers`
frames); `write_frames_manifest=True` writes 6 hourly + 1 peak entries with the web
`step N` token; the composer captures the `outputs.json` write + the seam read-back.

## Consequences

- `fire_spread` + spotting real-mode join the `outputs.json` seam; the other seven
  fire templates stay peak-only (per-template table). NO image rebuild (agent-side
  producer; stated explicitly). Legacy behaviour is byte-unchanged when `outputs.json`
  is absent (peak-only, an honest degrade).
- The 10-engine emit-on-solve campaign CLOSES with this leg (emission.md per-engine
  mechanism table). No deletions (additive leg -- DELETION_LEDGER untouched; the
  `shared/frames` selector stays LIVE for the S-class docker producers).

## LIVE CLOSE-OUT (2026-08-18) -- both templates green, NO image rebuild

Both solves ran through the REGISTERED composers with a capturing `PipelineEmitter`
bound as `_CURRENT_EMITTER` (`scripts/proof_elmfire_seam_0288.py`), real MinIO s3,
real LANDFIRE fuels + 3DEP topo fetched, `TRID3NT_SOLVER_BACKEND` docker. NO image
rebuild (the `trid3nt/elmfire:dev` solver image is the UNCHANGED solve step; the frame
derivation is agent-side).

**fire_spread -- `model_elmfire_fire_spread`** over a real Sierra-foothill fire-country
AOI (`bbox=[-120.88,38.98,-120.82,39.03]`, ignition `[-120.86,39.00]`, 25 mph W wind,
dry fuels, 6 h -- the wind/moisture scenario-labeled per law 9 P8), run
**`01M0AWXRKP835WNSYADTTQK2Y3`**: `outputs.json` = schema 1, engine elmfire, **7
entries** (1 peak + **6 frames**). Frame `t` = the burn hour -> seconds `[3600 ..
21600]` (1..6 h). The SEAM (`frames_only=True`) built **6 frame layers, NO peak**, **1
temporal group** `fire-arrival-01M0AWXRKP835WNSYADTTQK2Y3`, `t` monotonic + distinct,
style **`continuous_fire_arrival_hr`** for every frame; names `"Burned area step
1".."step 6"`. **All 6 frames published (never-omit)**; the composer emitted **6** frame
rows out-of-band (session-state `loaded_layers` = 14 incl. the typed peak + 2 aux + F33
overview COGs). Typed peak intact: `burned_area_km2=0.7596`,
`fire_arrival_max_hr=6.0008`, `max_flame_length_m=15.545`, `max_spread_rate_m_min=97.13`
(role primary). **REOPEN** (re-read `outputs.json` + rebuild) -> **identical 6 layer_ids**
(idempotent).

**spotting real-mode -- `model_elmfire_river_barrier_crossing`** on a real US river reach
found by the sub-reach search (Sacramento River near Red Bluff CA,
`bbox=[-122.199,40.098,-122.112,40.153]`, ignition `[-122.158,40.125]`, 35 mph W wind,
dry, 6 h; OFF then ON solve on the SAME warp), run **`01M0AXPA9FYZFNFZ39EX29A4E2`**:
verdict **"jumped"** -- OFF far-side = **0.0 km2** (the river HELD the contiguous front),
ON far-side = **2.523 km2** (embers carried the fire ACROSS the river) -- the clean
barrier-jump this template exists to measure. `outputs.json` = schema 1, engine elmfire,
**7 entries** (1 peak + **6 frames**, the ON case). The SEAM built **6 frame layers, 1
group** `fire-arrival-01M0AXPA9FYZFNFZ39EX29A4E2`, `t` monotonic `[3600..21600]`, style
`continuous_fire_arrival_hr`; names `"Burned area step 1".."step 6"`. **All 6 published**;
composer emitted **6** frame rows (loaded_layers = 12). Typed peak intact:
`burned_area_km2=4.068`, `fire_arrival_max_hr=6.0005` (role primary). REOPEN -> **identical
6 layer_ids** (idempotent).

### Gates

- `contracts/tests` + `test_law9_consequence_guard` + `test_elmfire_outputs_seam` +
  the five other elmfire suites + `test_model_fire_spread_chain` + `test_outputs_seam`:
  see the report for counts (all green).
- Offline four-slice baseline: see report (only the known 4 fetch_resolution + 2
  river_dye baseline failures; the fire_spread env class isolated + green).
- Daemon restarted + `scripts/ws_smoke.py` `all_passed=True` (see report).
- NO images built (agent-side producer; stated explicitly).
