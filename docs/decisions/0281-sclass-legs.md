# ADR 0281 -- emit-on-solve: the GeoClaw + SWAN S-class legs

Status: LANDED + LIVE CLOSE-OUT COMPLETE (producers + composer forks +
byte-equivalence + deletions offline-provable; both legs proven live through
rebuilt images -- see the LIVE CLOSE-OUT section). Date: 2026-08-17. Builds on ADR
0280 (the SFINCS flood proving case + the seam + the frozen `outputs.json` schema).

## Context

ADR 0280 landed the emit-on-solve seam (`trid3nt_server/emission/outputs_seam.py`)
and proved it on SFINCS flood. GeoClaw inundation and SWAN nonstationary waves are
the next two S-class engines (recon `emission-campaign-cadence-recon.md`): both
already write a worker-side `publish_manifest.json` before `completion.json`, both
rasterize ALL solver frames, both expose a user-wired frame-count lever
(`output_frames`). The migration for each is a field-shape rework on an EXISTING
write point + a composer fork, NOT new plumbing -- identical in shape to the
SFINCS flood leg.

## Decision -- what LANDED (per engine)

### 1. The worker producer (`outputs.json` alongside `publish_manifest.json`)

Each engine's worker postprocess now builds `outputs.json` entries from the SAME
ordered frames that build the register manifest, and the entrypoint writes them
alongside `publish_manifest.json` before `completion.json` (the SFINCS ADR-0280
dual-write pattern; best-effort -- a write failure never sinks the run, the
register path still works):

- **GeoClaw** (`workers/_geoclaw_postprocess/postprocess.py` +
  `workers/geoclaw/entrypoint.py::_write_outputs_manifest`): `quantity="flood_depth"`,
  peak non-temporal, frames carry physical seconds-from-start read from the
  `fort.tNNNN` sibling of each discovered `fort.qNNNN` (ordinal fallback keeps `t`
  distinct + monotonic when a `fort.t` is absent).
- **SWAN** (`workers/_swan_postprocess/postprocess.py` +
  `workers/swan/entrypoint.py::_write_outputs_manifest`): `quantity="wave_height"`,
  peak non-temporal, frames carry evenly-spaced seconds-from-start derived from
  `build_spec.sim_duration_s` (nonstationary SWAN writes `output_frames` snapshots
  evenly across `[0, sim_duration_s]`; ordinal fallback when the duration is
  absent).

Entries carry the OPTIONAL render hints (`bbox` + `band_stats`, the ADR-0280
amendment) so the seam resolves the SAME bbox + rescale WITHOUT a COG re-read.
`t` is ADDITIVE replay metadata -- the register path never had it -- so a proxy
`t` never breaks byte-equivalence; its only functional requirement is distinct +
monotonic so the seam's temporal group orders frames correctly.

### 2. The composer seam-or-legacy fork (metrics carrier = publish_manifest)

Each composer gains the SAME fork `flood.py` has:

- **GeoClaw** (`workflows/geoclaw/inundation/inundation.py`): `read_outputs_manifest`
  is consulted FIRST; a present manifest -> the SEAM (`build_layers_from_outputs`)
  owns ALL publication (peak `GeoClawDepthLayerURI` + context animation frames);
  `publish_manifest` is STILL read but ONLY for the top-level narration metrics
  (`max_depth_m` / `flooded_area_km2` / `max_inundation_m` / `arrival_time_s`) --
  the flat `outputs.json` entries carry no aggregates, so publish_manifest is the
  METRICS CARRIER, not a second publication. Absent `outputs.json` -> the legacy
  register-only path; absent both -> the on-box `fort.q` download path.
- **SWAN** (`workflows/swan/wave_field/wave_field.py`): identical fork; the peak
  `WaveFieldLayerURI` pulls its four narration scalars (`max_hs_m` / `mean_tp_s` /
  `mean_dir_deg` / `wave_area_km2`) from the publish_manifest metrics carrier.

Seam context frames are wrapped into the engine `LayerURI` subclass (context
metrics 0.0 -- the peak drives narration) so `_emit_frame_layers`' publish
chokepoint re-wrap has the typed fields (STRICTLY more correct than the register
path, which passed bare `LayerURI` into the same re-wrap).

### 3. Byte-equivalence (the migration bar) -- render stream identical, one
explained divergence

For a fixed run the emitted layer-event RENDER stream (name, layer_type,
style_preset, role, units, bbox, resolved `&rescale/&colormap`, side-band-stashed
legend) from the NEW `outputs.json` + seam path is BYTE-IDENTICAL to the OLD
register path. Tests: `tests/test_geoclaw_outputs_seam.py`,
`tests/test_swan_outputs_seam.py` (synthetic fort.q/fort.t + monkeypatched .mat;
no docker, durable offline regressions).

The ONE explained divergence is the internal `layer_id` STEM: the seam mints
`layer_id` off the PHYSICAL quantity (`flood-depth-*` / `wave-height-*`), while the
register path used the engine-prefixed worker stem (`geoclaw-depth-*` /
`swan-wave-height-*`). The layer_id is an idempotence/dedup key -- web temporal
grouping rides the `name` token (`detectSequentialGroups`, unchanged), NOT the
layer_id -- so the stem swap changes NO rendered output. It is deliberate: the seam
standardizes the id on the physical quantity, so GeoClaw depth shares the SFINCS
`flood-depth` family and SWAN Hs shares the SFINCS SnapWave `wave-height` family
(SFINCS coastal waves ALREADY used the `wave-height` stem -- the register path's
`swan-wave-height` was the outlier). This is the "identical or per-field explained"
bar ADR 0280 set.

### 4. Cadence vocabulary (count-vs-interval decision)

Per the campaign's universal `output_interval_min` vocabulary and the kickoff
DECISION RULE ("if converting count->interval is clean deck-side, do it; if the
engine's native control is genuinely count-based, keep count but ALIAS the
gate-card label to the universal vocabulary and note it in the ADR -- do not force
a bad fit silently"):

- **GeoClaw**: the native solver control is `clawdata.output_style=1` +
  `clawdata.num_output_times` -- GENUINELY count-based (an evenly-spaced frame
  COUNT across `[0, sim_duration_s]`). KEEP the count lever (`output_frames`, a
  first-class user-wired tool arg on every geoclaw template). The universal
  cadence maps as `output_frames = round(sim_duration_min / output_interval_min)`;
  the vocabulary is aliased here, no redundant parallel param is added (the count
  lever is the user's control and already gate-carded on the consequential-solve
  confirm).
- **SWAN**: the user-facing lever is `output_frames` (a COUNT); SWAN's nonstationary
  BLOCK output does take an interval `dt` under the hood, but the composer already
  derives `dt = sim_duration_s / output_frames` from the count the user drives.
  KEEP the count lever; alias the vocabulary as above.

No new `output_interval_min` parameter is threaded through either composer: both
levers are already user-wired by count, adding a parallel interval param would be
redundant surface with no distinct reader (charter "no reader, no feature"). The
universal-vocabulary MAPPING is documented here; the gate-card cadence concept
reads in the universal vocabulary.

### 5. Deletions (the bespoke frame publication -- ledger rows)

The GeoClaw/SWAN analogue of SFINCS row 20 (the post-hoc `MAX_FLOOD_FRAMES`
subsample thinning) is the per-engine `_select_frame_indices` subsample-to-cap
(`MAX_FRAMES=144`) in each postprocess. With cadence resolving DECK-SIDE
(`output_frames` is the SOLE frame-count control, user-wired) there is NO need for
an agent-side subsample. Both `_select_frame_indices` now return
`list(range(n_steps))` (NEVER omit); the dead `MAX_FRAMES` constant + the now-unused
`import os` are removed from both. Grep-to-zero: no `MAX_FRAMES` remains in
`workers/_geoclaw_postprocess` or `workers/_swan_postprocess`. Pinned by
`test_geoclaw_select_frame_indices_never_omits` /
`test_swan_select_frame_indices_never_omits`. Registered as two DELETED ledger
rows (GeoClaw + SWAN) in the emission cluster after the SFINCS row.

### 6. SWAN image gap fixes (required for the producer to exist in-image)

Two latent SWAN-worker gaps blocked the producer from ever running in-image (the
composer comment "the SWAN worker does NOT emit a manifest yet, so today this
always falls back" was literally true):

- The SWAN Dockerfile NEVER copied `workers/_swan_postprocess/` or
  `workers/_raster_postprocess/`, so `from workers._swan_postprocess import
  run_swan_postprocess` failed at runtime (caught non-fatal) -- no manifest ever
  written. FIXED: the two COPY lines added (mirroring the GeoClaw Dockerfile) +
  an in-image build-smoke import check. The SWAN image already carries the geo
  stack (numpy/scipy/rasterio) so no dependency change was needed.
- The SWAN entrypoint swept `*.tif` outputs BEFORE the postprocess wrote them, so
  the COGs were never uploaded. FIXED: the freshly-written `pp.cog_paths` are
  uploaded explicitly after the postprocess (mirroring the GeoClaw entrypoint's
  sweep-after-postprocess ordering).

## Consequences

- GeoClaw + SWAN join SFINCS on the `outputs.json` seam; the register path stays
  as the one-release fallback + the metrics carrier (its removal is ledger row 19,
  the Section-7.3 collapse, gated on the LAST engine migrating). Legacy behaviour
  is byte-unchanged when `outputs.json` is absent.
- Zero offline-suite movement: baseline EXACTLY 4 fetch_resolution + 2 river_dye,
  unchanged, plus the two new seam test files green.
- The SWAN worker's postprocess offload -- shipped in code but never in-image --
  is now genuinely in-image and live-provable.

## LIVE CLOSE-OUT (2026-08-17) -- both legs green through rebuilt images

### GeoClaw

- IMAGE LAW: `trid3nt-local/geoclaw:latest` rebuilt (`-f workers/geoclaw/Dockerfile`,
  repo-root context; heavy PETSc/clawpack layers cached, the COPY layers +
  in-image smoke re-ran). In-image provenance verified: `outputs_manifest` schema
  1, `_select_frame_indices(500) == list(range(500))` (cap retired), no `MAX_FRAMES`
  attr, `GEOCLAW_DEPTH_QUANTITY == "flood_depth"`, `_read_fort_t_seconds` present,
  entrypoint `_write_outputs_manifest` present, producer `outputs_entries` wired.
- LIVE SOLVE through the image: `scripts/run_geoclaw_direct.py`, run
  **`01M089JY3DWBZ9ZREE0TWG9ZQN`** (Crescent City tsunami, `output_frames=6`).
  `completion.json` status=ok; `outputs.json` = schema 1, engine geoclaw, **8
  entries** (1 peak `t=None` + 7 frames), physical `t` read from `fort.t` =
  0/300/600/900/1200/1500/1800 s, every entry carries `bbox` + `band_stats`. **All
  7 solver dumps published (never-omit -- output_frames=6 -> 7 dumps incl. t=0, no
  thinning).** The SEAM path was taken (`model_geoclaw_inundation (seam path)`): all
  8 layers registered by the seam as `flood-depth-peak/-frame-NN` (preset
  `continuous_flood_depth`); the metrics carrier (`publish_manifest`) threaded
  `max_depth_m=32.27`, `flooded_area_km2=14.56`, `max_inundation_m=0.62`,
  `arrival_time_s=36.78`. (`frames_emitted=0/7` -- the direct driver binds no WS
  emitter; frames are built, not emitted out-of-band, which is correct.)

### SWAN

- IMAGE LAW: `trid3nt-local/swan:latest` rebuilt (`-f workers/swan/Dockerfile`,
  repo-root context). The rebuild also SHIPPED the postprocess for the first time
  (`_swan_postprocess` + `_raster_postprocess` COPY lines added -- they were never
  in the image) and FIXED the COG-upload ordering gap. In-image provenance
  verified: `run_swan_postprocess` + `outputs_manifest` schema 1 import in-image,
  cap retired, no `MAX_FRAMES`, `SWAN_WAVE_HEIGHT_QUANTITY == "wave_height"`,
  entrypoint `_write_outputs_manifest` + the `pp.cog_paths` upload present.
- LIVE SOLVE through the image: `scripts/run_swan_storm_direct.py`, run
  **`01M08ACMKWQ7XV23ZFJ06SND76`** (nonstationary storm, `output_frames=18`).
  status=ok; `outputs.json` = schema 1, engine swan, **20 entries** (1 peak
  `t=None` + 19 frames), evenly-spaced physical `t` = 0..129600 s, every entry
  carries `bbox` + `band_stats`. **All 19 snapshots published (never-omit).** **20
  COGs uploaded to the runs bucket** (the entrypoint cog-upload fix -- without it
  zero COGs would resolve). The SEAM path was taken (`model_swan_wave_field
  complete (seam)`): the seam built 20 layers, 1 temporal group, registered as
  `wave-height-peak/-frame-NN` (preset `continuous_wave_height`); the metrics
  carrier threaded `max_hs_m=6.02`, `mean_tp_s=10.11`.

### Gates (all green)

- Worker suites: `workers/{geoclaw,_geoclaw_postprocess,swan,_swan_postprocess}`
  140 passed, 1 skipped. New seam tests: `tests/test_geoclaw_outputs_seam.py` +
  `tests/test_swan_outputs_seam.py` (6 + 5) green.
- Offline four-slice suite at EXACT baseline: [a-e] 1468 passed; [f-o] 4 failed
  (the 4 `fetch_resolution` baseline) 6379 passed; [p-r] 2 failed (the 2
  `river_dye` baseline) 2014 passed; [s-z] 1397 passed. Zero new failures.
- Contracts: `contracts/tests` 721 passed.
- Daemon restarted (`make agent`) + `scripts/ws_smoke.py` `all_passed=True`.
- Image provenance: both images verified in-image (above).
