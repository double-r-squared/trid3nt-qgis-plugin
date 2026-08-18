# ADR 0284 -- emit-on-solve: the MODFLOW transport leg (L-class)

Status: LANDED (host-exec transport-frame producers + composer frames-only forks
+ the dead-scaffold deletion + offline seam tests). Date: 2026-08-17. Builds on
ADR 0280 (the seam + the frozen `outputs.json` schema), ADR 0281 (the S-class
composer-fork pattern), ADR 0282 (the M-class host-exec writer). See the LIVE
CLOSE-OUT section for the two proving solves.

NATE confirmed the recon's forks A/A/A (fork 1A-scoped / 2A / 3A). The recon
(`/tasks/w8am67bb8`) verified the kickoff's load-bearing premises against the
code; three did not hold as framed, and the leg turned on design decisions the
charter reserves for NATE. This ADR carries the confirmed rulings + the FULL
per-template verdict table (the coverage law's denominator).

## Context -- how MODFLOW breaks the prior legs' premises

1. **Host-exec, not docker.** `mf6` runs `exec_kind="exec"` on the agent host;
   the rasterizer is the agent-side `workflows/modflow/postprocess_modflow.py`.
   `workers/modflow/*` serves ONLY the heavy-compute deck-BUILD path, never the
   transient output/postprocess. So the M-class host-exec writer pattern (0282)
   applies -- `workflows/shared/outputs_manifest_io.write_outputs_manifest` is the
   agent-as-its-own-worker producer; **no image law binds this leg**.
2. **No live frame emission existed at all, and NO subsampling to delete.** The
   live peak path reads the FINAL step directly (`nanmax` over layers of the last
   totim) -- it never subsampled a stack, so there is **no cap and NO ledger row**
   (the kickoff's "ledger row only if real" gate fails: it isn't real here). The
   only step-stack->frames machinery that existed was `publish_modflow_quantities`
   -- defined + unit-tested but **NEVER called by any composer** (grep-to-zero):
   the dormant `output_quantities`/`publish_quantities` scaffold (DELETION_LEDGER
   row 18). So MODFLOW transport frames are **ADDITIVE**, with **no live baseline
   to be byte-equivalent to**; the bar is CORRECTNESS + the live proofs.
3. **Typed peak return, not an envelope** (like the M-class): each composer
   returns the PEAK layer directly with the narration scalars ON it
   (`max_concentration_mgl`/`plume_area_km2`; `peak_excess_temperature_c` ...). The
   seam owns the TEMPORAL FRAMES ONLY (OPTION a); the typed peak + charts stay
   composer-built.
4. **Cadence is a stress-period SCHEDULE, not an interval.** `TIME_UNITS="DAYS"`;
   the schedule is `sim_years`->nper / `n_periods`x90d / seasonal (or monthly for
   MAR/ASR), each period `DEFAULT_STEPS_PER_TRANSIENT_PERIOD` steps, OC
   `saverecord=[("CONCENTRATION","ALL")]` = save EVERY step. Frame `t` is
   `totim_days * 86400` s (honest DAYS->seconds). Mapping `output_interval_min`
   onto a multi-period DAYS schedule is a worse fit than either precedent.

## Decision -- NATE's rulings (the three forks)

**FORK 1 (1A-scoped) -- scaffold overlap.** Build the concentration frames via
the `outputs.json` SEAM for the transport family (plume + multi_species + thermal)
ONLY; leave the dormant `output_quantities` scaffold to its queued full deletion
(DELETION_LEDGER row 18). Because MODFLOW's scaffold half
(`publish_modflow_quantities`) was DEAD (zero callers, unlike the LIVE
`publish_swmm_quantities`/`publish_landlab_quantities`/`publish_openquake_quantities`),
it is deleted now for MODFLOW scope with the row-18 annotation.

**FORK 2 (2A) -- head-based templates get NO frames.** drawdown / mounding /
hydroperiod / subsidence publish a t0-difference or a max-min RANGE reduction;
raw per-step head rides a large static component NATE flagged. The quantities
stay unchanged (NATE ruling); their temporal signal stays in their existing
composer CHARTS. No progressive-difference quantity is invented.

**FORK 3 (3A) -- no `output_interval_min` for MODFLOW.** Frame density IS the
existing period-schedule levers (`sim_years`/`n_periods`/`n_months`), already
user-driven on the gates. No minute-interval param is threaded (honest "no reader,
no feature"); the schedule levers ARE the documented cadence control.

## Per-template verdict table (the coverage-law denominator)

| Template | Native temporal field | Status-quo raster | Verdict |
|---|---|---|---|
| contaminant_plume (single) | UCN concentration stack | peak COG + chart | **FRAMES** -- concentration animates; quantity unchanged |
| contaminant_plume multi_species | N UCN stacks | N peak COGs + chart | **FRAMES** (per species, distinct group) |
| thermal_plume (GWE injection) | temperature stack | peak excess COG + chart | **FRAMES** -- temperature-excess animates; quantity unchanged |
| thermal_storage / ATES (GWE) | temperature stack | peak excess COG + recovery chart | **FRAMES** -- same stack; recovery chart untouched |
| sustainable_yield (drawdown) | HDS head stack | peak drawdown COG + chart | CHARTS-ONLY (fork 2A) -- status quo = head(t0)-head(t_last), a t0-difference reduction; raw head rides a big static component |
| managed_recharge (mounding) | HDS head stack | peak mounding COG + chart | CHARTS-ONLY (2A) -- same head-difference structure |
| wetland_hydroperiod | HDS head stack | seasonal RANGE (max-min) COG + chart | CHARTS-ONLY (2A) -- range is inherently a reduction, no per-step field |
| land_subsidence (CSUB) | zdisp stack (reads final only) | cumulative bowl COG + chart | CHARTS-ONLY (2A) -- status-quo reads final-only; accumulation is a reduction |
| mine_dewatering | CBC DRN last step | peak dewatering-rate COG | CHARTS-ONLY -- per-step flux, not a depth-class field; steady-ish peak |
| river_seepage | CBC RIV last step | peak seepage COG (+ best-effort plume context) | CHARTS-ONLY for the seepage headline; the best-effort plume rides `postprocess_modflow` (single), OUT of the transport-composer frame scope this wave |
| asr | WEL budget | rep-head COG + sawtooth chart | NO -- the deliverable is the sawtooth chart |
| saltwater_intrusion | UCN | wedge vector + heatmap chart | NO -- the deliverable is the cross-section heatmap chart |
| vadose_transport | 1-D column | breakthrough chart + point | NO -- 1-D, no 2-D raster |
| regional_water_budget | CBC | budget scalar + partition chart | NO -- scalar/chart |
| capture_zone / wellhead_protection | PRT (steady flow field) | pathlines + capture polygon | NO -- not temporal (steady flow field) |
| package_validation | -- | validation, no physics run | N/A |

The FRAMES set = the GWT/GWE transport family (contaminant_plume single/multi +
thermal_plume/thermal_storage) -- exactly NATE's "depth-class quantities animate
fine." Everything else is charts-only (2A) or non-temporal.

## What LANDED

### The transport-frame producer (`postprocess_modflow.py`, host-exec)

- `_read_concentration_steps(ucn) -> (grids, totim_days, peak)` and
  `_read_temperature_series(ucn) -> (alldata, kstpkper, totim_days)` now return
  the parallel totim in DAYS (`get_times()`), the honest source of each frame `t`.
- `_write_transport_frame_entries` writes + uploads EVERY saved step's render COG
  (never-omit -- OC saves ALL, no cap ever existed) on the SAME grid georef the
  peak uses (reprojected via `_write_reprojected_cog`, byte-identical masking to
  the peak: plume masks at/below the detection floor; thermal renders the
  pre-floored excess), and builds one `outputs.json` entry per step
  (`t = totim_days * 86400`, per-frame `bbox`, `band_stats` OMITTED -- the
  quantity is a REGISTERED seam quantity/family so the seam resolves style without
  it). `_write_modflow_outputs_manifest` PUTs the whole array host-side
  (best-effort -- a miss degrades to peak-only, "failure retracts nothing").
- **`postprocess_multi_species`** (serves contaminant_plume, 1..N species):
  per-species quantity `plume_concentration__<slug>` so N species never collide on
  the seam's `(quantity, t)` grouping (they share ONE time discretization ->
  identical save-times); each species is its OWN temporal group. Returns
  `MultiSpeciesPlumeResult(plumes=..., run_id=run_id)` (new additive field).
- **`postprocess_gwe_thermal`**: per-step temperature-EXCESS frames (bare
  registered `temperature` quantity, one stack -> one group); stashes `_run_id` on
  the returned layer for the composer fork. The ATES recovery chart is untouched.

### Cadence -- the family fallback in `emission/quantity_styles`

`resolve_style_preset` gains a per-instance FAMILY fallback: a quantity
`<family>__<slug>` that is not itself registered resolves to the `<family>`
preset (one registry row styles every sibling stack). `plume_concentration__tce`
-> `continuous_plume_concentration`, is_fallback=False (NOT the neutral ramp). The
double-underscore never appears in a base quantity key, so the split is
unambiguous. `plume_concentration` and `temperature` were already registered.

### The composer forks (`_frame_emit.py`, shared)

`workflows/modflow/_frame_emit.read_and_emit_modflow_frames` reads `outputs.json`
back (`build_layers_from_outputs(frames_only=True)` -> the peak entries are
skipped, the composer keeps its typed peak), publishes each frame COG through the
render chokepoint, and emits it as a `context` `LayerURI` out-of-band so the web
`detectSequentialGroups` scrubber group forms with the peak's physical colormap.
Both transport composers call it: **contaminant_plume** (`result.run_id` +
`plumes[0].bbox`), **thermal_plume/thermal_storage** (`getattr(layer,"_run_id")` +
`layer.bbox`). Best-effort: absent manifest / no emitter -> peak-only.

### Deletion (dead scaffold, MODFLOW scope)

`publish_modflow_quantities` + its ONE unit test
(`test_modflow_step3_quantities::test_publish_modflow_quantities_emits_timeseries_and_head`)
are DELETED (grep-to-zero confirmed: zero live callers -- it was never wired to a
composer). `_modflow_src_transform` is RETAINED (pinned by
`test_modflow_georef_hardening` as the georef-hardening honesty guard). The
`OUTPUT_QUANTITIES` modflow registry specs remain (the scaffold; the full deletion
is DELETION_LEDGER row 18, still gated on the SWMM/Landlab/OpenQuake live halves).
Net: -113 LOC (the dead function) + -47 LOC (its test), +~150 LOC producer/fork.

## Offline tests

`tests/test_modflow_outputs_seam.py` (pure/offline -- entries built by the same
`build_entry` the producer uses, no mf6 solve): family style resolution, thermal
style resolution, `frames_only` skips the peak, never-omit (40 frames), `t` in
seconds + monotonic, and the KEY correctness guard -- N species with IDENTICAL
save-times form N DISTINCT groups (a bare shared quantity would collapse them via
the `(quantity, t)` dedup). `test_modflow_step3_quantities` keeps the still-live
deck-physics + style-preset checks (the dead-function test removed).

## Consequences

- The transport family joins the `outputs.json` seam; head-based templates keep
  their charts (2A). NO image rebuild (host-exec mf6; the writer's first MODFLOW
  use). Legacy behaviour is byte-unchanged when `outputs.json` is absent
  (peak-only, an honest degrade).
- NOTE for the ledger: unlike MODFLOW, the SWMM/Landlab/OpenQuake scaffold halves
  (`publish_swmm_quantities` / `publish_landlab_quantities` /
  `publish_openquake_quantities`) are STILL LIVE (called by their composers), so
  they are NOT touched this wave -- row 18 stays QUEUED for them.

## LIVE CLOSE-OUT (2026-08-17) -- both classes green, NO image rebuild

Both solves ran through the REGISTERED composers (host-exec mf6 `bin/mf6`, real
US settings, MinIO s3), a capturing `PipelineEmitter` bound as `current_emitter`.
NO image rebuild anywhere (host-exec mf6; the `write_outputs_manifest` writer's
first MODFLOW use). `TMPDIR` was pointed at the larger `/` disk for the thermal
solve (the `/tmp` tmpfs filled with accumulated test-run deck staging -- an env
resource issue, not a code issue).

**CONTAMINANT PLUME (multi-species, multi-year)** -- `model_contaminant_plume`,
Woburn MA (historic TCE groundwater contamination), species `[TCE, cis-DCE]`,
`duration_days=1825` (5 yr), run **`01M09CANJ7X299TZD90N0D75KE`**:
`outputs.json` = schema 1, engine modflow, **734 entries** (2 peak `t=None` + 732
frames = **366 per species**). Frame `t` = MF6 totim DAYS -> seconds
`[86400, 518400, 950400, ..., 157766400]` (days `[1, 6, 11, ..., 1825.5]`,
honest totim-derived). The SEAM (`frames_only=True`) built **732 frame layers, NO
peak**, **2 temporal groups** -- `plume-concentration--tce-<run>` +
`plume-concentration--cis-dce-<run>` -- each 366 frames, `t` monotonic + distinct,
style **`continuous_plume_concentration`** (the per-species `plume_concentration__<slug>`
quantity resolves via the family fallback -- NOT the neutral ramp). **All 732
frames published (never-omit)**; the composer emitted **732/732** out-of-band
(session-state `loaded_layers` = 734 incl. the 2 typed peaks) + the per-species
summary chart (1 chart frame). Typed peaks intact:
`plume-concentration-tce-<run>` `max_concentration_mgl=1.293e5`,
`plume_area_km2=0.23`; `...-cis_dce-<run>` `5.171e4` / `0.215` (role primary).
**REOPEN** (re-read `outputs.json` + rebuild) -> **identical 732 layer_ids**
(idempotent).

**GWE THERMAL (ATES, multi-cycle)** -- `compose_thermal_scenario`, St. Paul MN
(cold-climate ATES), `mode="ates"`, `n_cycles=2`, inject 25 degC / ambient 10
degC, run **`01M09CN8D67E3SFQ04NJ2DX8GD`**: `outputs.json` = schema 1, engine
modflow, **482 entries** (1 peak + 481 frames). Frame `t` = totim DAYS -> seconds
`[86400, 86416.78, 86435.23, ..., 62294400]` -- UNEVEN sub-day steps (the ATES
seasonal cycling drives fine time-stepping), which proves the `t` is real solver
timing, not a fabricated even grid. The SEAM built **481 frame layers, 1 temporal
group** `temperature-<run>`, `t` monotonic + distinct, style
**`continuous_temperature_c`**; all 481 published (never-omit); the ATES recovery
chart still emitted (1 chart frame). Typed peak intact:
`peak_excess_temperature_c=10.60`, `gwe_mode="ates"`,
`recovery_efficiency_series=[0.566, 0.673]` (the recovery chart data is
untouched).

### Gates (all green)

- Offline four-slice suite at EXACT baseline: [a-e] 1468 passed, 5 skipped;
  [f-o] 4 failed (the 4 `fetch_resolution` baseline) 6390 passed, 1 xfailed;
  [p-r] 3 failed (the 2 `river_dye` baseline + the 1 PRE-EXISTING
  `test_naip_passthrough` rasterio-COG env failure) 2014 passed; [s-z] 1402
  passed. ZERO new failures.
- `contracts/tests` 721 passed. New `tests/test_modflow_outputs_seam.py` +
  the trimmed `test_modflow_step3_quantities` + `test_modflow_georef_hardening`
  green (the broader modflow/emission/composer selection: 565 passed, 1 skipped).
- Daemon restarted (`make agent`, PID 3034766) + `scripts/ws_smoke.py`
  **`all_passed=True`** (TEST A chat + TEST B tool-call).
- NO images built (host-exec mf6; state explicitly).
