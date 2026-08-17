# ADR 0280 -- emit-on-solve: the append-only `outputs.json` seam (wave 1 foundation)

Status: FOUNDATION LANDED (schema + contracts + registry + tests); SEAM +
WORKER-PRODUCER + BYTE-EQUIVALENCE LANDED (this wave, 2026-08-17 -- see
"EXECUTED" below); the LIVE close-out (deck-side cadence cap fix + image rebuild
+ live proving solve + row-20 deletion) is staged for the live-testing loop.
Scaffold reconciliation (item 6) unchanged: OPTION A (scaffold stays, migrates
per-engine). Date: 2026-08-16 / amended 2026-08-17.

## Context

The emission campaign (source docs: `docs/design/outputs-manifest-schema.md`
+ `emission-campaign-cadence-recon.md`) replaces every engine's bespoke
worker-side `publish_manifest.json` + register-only agent consumption with ONE
append-only `outputs.json` manifest a solver leg writes under its run prefix,
and ONE emit-on-solve seam that reacts to entries as they land.

NATE's settled rulings (do not relitigate): flat role-free entries
`{kind, quantity, name, uri, t?, units?}`; `completion.json` = finality; v1
publishes AT-EXIT (append-only contract unchanged; streaming is a later
per-engine capability); never omit a frame -- cadence resolves DECK-SIDE, no
post-hoc thinning; unknown quantity -> honest neutral ramp + WARNING; universal
lever `output_interval_min`; failure retracts nothing; TELEMAC rides
`kind=mesh` later; the `output_quantities`/`publish_quantities` scaffold's IDEA
(a proper quantity->style registry) survives, the scaffold itself is to die.

## Decision -- what LANDED this wave (offline-provable foundation)

1. **Schema FROZEN at `schema_version = 1`** (`outputs-manifest-schema.md`):
   the "candidate" language is gone; the at-exit v1 note (Section 0), the
   `kind=mesh`-for-TELEMAC-later note, the byte-based retention paragraph
   (Section 6), and the two-surfaces-one-version marker semantics (Section 4)
   are baked.

2. **Contracts + shared writer** (`trid3nt_contracts.outputs_manifest`): the
   WRITER half (`new_manifest` / `build_entry` / `append_entries` /
   `serialize`) is PURE STDLIB so it is importable from BOTH the host-exec
   agent path (MODFLOW/SWMM run in the agent process -- the writer cannot be
   worker-only) AND a verbatim worker mirror gated on the same
   `OUTPUTS_MANIFEST_SCHEMA_VERSION` (the deploy-boundary precedent from
   `output_quantities.py`: workers ship `workers/**` not `contracts`; the agent
   ships `contracts` not `workers`). The READER half (`OutputEntry` /
   `OutputsManifest` / `parse_outputs_manifest`) is tolerant Pydantic. Unknown
   `schema_version` and foreign `kind` are read-side hard rejects (the
   completion-only fallback trigger). `build_entry` rejects an unknown `kind` at
   WRITE time -- never a silent drop. Schema tests:
   `tests/test_outputs_manifest_schema.py` (8 cases). The worker mirror lands
   WITH the first docker producer (the flood proving case), not before.

3. **Quantity -> style registry** (`trid3nt_server.emission.quantity_styles`):
   the seam-side `quantity -> style_preset` map, seeded from the presets already
   in `publish_layer._QGIS_STYLE_REGISTRY` (flood_depth, wave_height,
   plume/dye, MODFLOW head/drawdown/mounding/..., Landlab, SWMM, seismic, fire).
   An unregistered quantity resolves to `NEUTRAL_FALLBACK_PRESET` ("neutral
   ramp") which the publish_layer resolver renders via its generic band-stats
   percentile-viridis path (the honest neutral ramp reading the COG's own
   range), plus a `logger.warning` and a process-lifetime fallback counter
   (`unknown_quantity_fallback_count`). This is the "keeping only its idea as a
   proper quantity->style registry" ruling. Test:
   `tests/test_quantity_styles_registry.py` (3 cases -- fallback pinned, counter
   pinned, every seeded preset is a real registry key).

## Decision -- what is SPECIFIED but gated on the live loop

The remaining wave-1 items each require live infra (a real SFINCS solve +
worker image rebuild) or touch live regression-critical code, so they are
specified here and executed under NATE's live-testing loop, not in this
offline build turn.

### The seam consumer (item 4)

The consumer is the Section 3 watch-loop generalized: on the existing
completion path (`solver.py wait_for_completion`, `DEFAULT_POLL_INTERVAL_S=10`),
read `outputs.json` from the run prefix and publish every entry ->
`raster` with no `t` = one layer; `raster` entries sharing a `quantity` with a
`t` = a temporal group with stamps (the existing flood temporal-grouping
machinery -- `detectSequentialGroups` on the `name` token -- generalized);
`vector` = a vector layer; `mesh`/`scalar` = parse+validate, log-only in v1.
Idempotence: mint `layer_id` deterministically from `(quantity, t, run_id)` so
a re-poll of an already-published entry is a no-op on `observe_published_layer`
(Section 5.2). MISSING manifest -> no-op, so legacy engines are byte-unchanged.

DELIBERATE SEQUENCING: the consumer WIRING into `server/dispatch/results.py`
lands WITH the flood producer so the two are proven TOGETHER against the
byte-equivalence bar -- wiring a live consumer with zero producers this wave
would add regression surface for zero behavior. The contracts + registry it
depends on are shipped and tested now.

### Flood proving case + the 144-cap fix (item 5)

The SFINCS worker postprocess (`workers/_raster_postprocess` +
`postprocess_sfincs`) writes `outputs.json` from its existing frame
rasterization (entries instead of bespoke `publish_manifest.json` layers); the
seam publishes; the BYTE-EQUIVALENCE BAR captures the layer-event stream
(names, layer_ids modulo run-id, styles, temporal stamps, group structure)
old-path vs new-path for the SAME solved `sfincs_map.nc` -- identical or
per-field explained -- BEFORE flood's bespoke frame-emission (the S2 pattern)
is deleted. THEN the 144-cap fix: cadence resolves DECK-SIDE (`dtout`/`dtmaxout`
derived from `output_interval_min` with a sane default), no post-hoc thinning,
and the pluvial path -- pinned OFF today
(`run_sfincs.py:_resolve_output_interval_min`, recon confirms) -- gets the
lever wired. IMAGE LAW: the worker postprocess change requires a rebuild +
provenance check + smoke THROUGH the image. This item needs docker + a live
solve + MinIO; it is the primary live close-out.

### Scaffold reconciliation (item 6) -- CRITICAL SCOPE CORRECTION

NATE's item-6 ruling ("the OpenQuake wiring that consumes them -- migrate
OpenQuake's actual styling need, delete") was premised on the recon's stale
claim that `get_output_registry` returns `()` for every engine EXCEPT
OpenQuake. VERIFIED AGAINST THE CODE (2026-08-16): FALSE. The scaffold is a
LIVE, TESTED, FOUR-engine feature. `OUTPUT_QUANTITIES` carries `default_on=True`
quantities for `openquake` (hazard-curves, uhs), `modflow` (plume-ts,
water-table, drawdown, dewatering-rate, budget-partition, mounding,
recovery-efficiency, hydroperiod), `swmm` (flooding-losses, ponded-volume,
conduit-flow, conduit-velocity), `landlab` (drainage-area, slope,
relative-wetness, discharge, factor-of-safety). Each engine's postprocess
(`postprocess_openquake/_modflow/_swmm/_landlab`) binds readers and calls
`publish_quantities`, producing REAL product layers, guarded by
`test_{modflow,swmm,landlab,openquake}_step3_quantities` +
`test_publish_quantities` + `test_output_quantity_style_presets`.

Deleting `output_quantities.py` + `publish_quantities.py` therefore RETIRES A
LIVE 4-ENGINE FEATURE producing dozens of product layers -- not the narrow
"OpenQuake styling need" the ruling anticipated. Per the accumulate-and-wait +
NATE-first-methodology + deletion-ledger norms, this build does NOT execute a
destructive deletion the settled ruling did not actually account for. The
deletion is REGISTERED as QUEUED with the CONDITION "all four live consumers
migrate onto the `outputs.json` writer + the quantity->style registry, one
engine at a time with byte-equivalence per engine, then the scaffold is
superseded." This is flagged to NATE for a re-scope decision (fold the four
consumers into the emission campaign engine-by-engine, vs. keep the scaffold as
the agent-side quantity registry and only delete its manifest-assembly overlap).

### Replay fields (item 7)

The persisted layer record gains OPTIONAL temporal fields (`t`, group id +
the seam-resolved style key, per Section 5.3); case reopen rebuilds the
temporal group from them; old records load untouched (the tolerance rule -- a
record whose `uri` no longer resolves degrades to replay-metadata-only). This
touches `persistence` + the case-layer rehydration path and is built + pinned
by a reopen tolerance test alongside the seam wiring (item 4), since the
replay fields are what the consumer stamps at publish time.

## Consequences

- The append-only wire is frozen and typed NOW; producers migrate against a
  stable contract, one engine at a time, byte-equivalence per engine.
- Zero behavior change this wave: nothing writes `outputs.json` yet, the seam
  is not wired, the scaffold is untouched. The offline suite baseline is
  unmoved (EXACTLY 4 fetch_resolution + 2 river_dye).
- Two premises in the campaign kickoff did not survive contact with the code
  (the scaffold is live 4-engine, not empty-except-OpenQuake; and the live
  proving case needs a solve + image rebuild). Both are surfaced to NATE rather
  than resolved by executing a destructive or unverifiable step blind.

## EXECUTED (2026-08-17) -- seam + producer + byte-equivalence

Under the OPTION-A scope ruling (the `output_quantities`/`publish_quantities`
scaffold is NOT deleted this wave -- ledger row 18 stays QUEUED; each engine
migrates during its own leg). This wave lands the offline-provable, additive,
fully-revertible foundation for the SFINCS flood leg and PROVES the migration
bar; the destructive step (ledger row 20 deletion + the deck-side cap fix) stays
gated on the live proving solve through a rebuilt image (the live-testing loop).

### Landed

1. **Worker writer mirror** (`workers/_raster_postprocess/outputs_manifest.py`):
   pure-stdlib `build_entry` / `new_manifest` / `append_entries` / `serialize`,
   gated on `OUTPUTS_MANIFEST_SCHEMA_VERSION = 1`, behaviourally identical to the
   `trid3nt_contracts` writer half (the deploy-boundary precedent).

2. **The seam consumer** (`trid3nt_server/emission/outputs_seam.py`):
   `read_outputs_manifest` (the missing/unknown-schema -> `None` no-op) +
   `build_layers_from_outputs`. Routes per Section 5 (raster no-`t` = standalone
   primary; raster+`t` sharing a `quantity` = a temporal group ordered by `t`,
   role context; vector = a vector layer; mesh/scalar = log-only). Style resolves
   via `quantity_styles.resolve_style_preset`; `layer_id` is deterministic +
   idempotent from `(quantity, t-ordinal, run_id)` reproducing the register
   path's stems EXACTLY (`flood_depth` -> `flood-depth-peak` / `flood-depth-frame-NN`).
   The data-driven legend is STASHED side-band via `_stash_legend_for_uri`
   (leaving `LayerURI.legend=None`), byte-identical to `register_manifest_layers`
   -- NOT attached to the LayerURI (the one divergence the capture caught + fixed).

3. **The worker producer** (`workers/_raster_postprocess/postprocess.py`
   `build_entry` loop + `PostprocessResult.outputs_entries`; reader `t_seconds`):
   the SAME ordered frames that build `publish_manifest.json` also build the
   `outputs.json` entries (peak non-temporal; frames carry seconds-from-start).
   `workers/sfincs/entrypoint.py` writes `outputs.json` ALONGSIDE
   `publish_manifest.json` (additive; the register path is byte-unchanged -- INERT
   until the image rebuild). Per-frame `t` = the `time` coord's seconds-from-start.

4. **Replay fields** (`outputs_seam.PublishedFrame`): the parallel `t` / `group_id`
   / seam-resolved `style_preset` for the persistence stamp (item 7). The
   live-emitted `LayerURI` is byte-identical to the register path; the replay meta
   rides ALONGSIDE it (the persistence + reopen wiring is part of the live
   close-out).

### THE FLAGGED EQUIVALENCE RISK -- resolved WITH a schema amendment

The kickoff flagged that the old register path carried `band_stats` per entry for
legend/rescale and the frozen schema dropped it. Verified against the code: for a
REGISTERED quantity (`flood_depth` -> `continuous_flood_depth`, the pinned
registry preset `0,3` / `ylgnbu`), `style_params_from_band_stats` resolves at the
registry step and NEVER consults `band_stats` -- so band_stats parity holds for
flood WITHOUT the field. But the byte-equivalence bar (Section 7.1) ALSO lists
`bbox` (the per-COG EPSG:4326 extent the worker precomputes) and `band_stats` is
still needed for the UNREGISTERED-quantity neutral ramp. Rather than force a
per-COG re-read on the agent (which would regress the register-only-no-COG-read
fast path) OR ship a bbox-drift render regression, the schema is AMENDED (v1 is
young; the task authorizes amending WITH the proving case):

> **SCHEMA AMENDMENT (schema_version 1, ADR 0280 EXECUTED):** `OutputEntry`
> gains two OPTIONAL render-hint fields -- `bbox: [minlon,minlat,maxlon,maxlat]`
> and `band_stats: {is_categorical, is_rgba, p2, p98}`. The flat
> `{kind,quantity,name,uri,t?,units?}` core stays the ONLY required shape. A
> producer that precomputed them (every docker raster worker) writes them so the
> seam resolves the SAME bbox + rescale WITHOUT a COG re-read; a host-exec engine
> that omits them degrades to the workflow AOI bbox + a lazy per-COG stats touch
> (the neutral-ramp fallback). Tolerant-read: an old producer that omits them is
> byte-unchanged. Landed in BOTH `trid3nt_contracts.outputs_manifest` (writer +
> reader `OutputBandStats`) and the worker mirror.

### The byte-equivalence capture (verbatim)

`register_manifest_layers` (OLD) vs `outputs.json` + `build_layers_from_outputs`
(NEW), same worker postprocess on a solved `sfincs_map.nc` (25-timestep pluvial
run `01KYDRQC...`; also a self-contained synthetic map in
`tests/test_outputs_seam.py::test_byte_equivalence_seam_vs_register`). Diff over
`{layer_id (modulo run-id), name, layer_type, style_preset, role, units, bbox,
resolved &rescale/&colormap, side-band-stashed legend}`:

```
=== OLD register_manifest_layers stream (26 layers) ===
flood-depth-peak      name='Peak flood depth'  raster continuous_flood_depth primary meters
                      bbox=(-95.611266,29.731714,-95.428762,29.793319)
                      rescale='&rescale=0,3&colormap_name=ylgnbu'
                      stashed_legend=('continuous','ylgnbu',0.0,3.0,'meters')
flood-depth-frame-01..25  name='Flood depth step N' raster continuous_flood_depth context meters
                      (identical bbox / rescale / stashed_legend on every frame)
=== NEW outputs.json + seam stream (26 layers) ===
   (byte-identical rows)
=== FIELD-BY-FIELD DIFF ===
IDENTICAL: every field of every layer matches (byte-equivalent stream).
=== NEW-ONLY replay metadata (additive, item 7) ===
   flood-depth-peak      t=None    group_id=None
   flood-depth-frame-01  t=0.0     group_id=flood-depth
   flood-depth-frame-25  t=172800.0 group_id=flood-depth
```

The ONLY differences are ADDITIVE (the NEW path carries per-frame `t` +
`group_id` the OLD path never had -- the item-7 replay capability). Every
render-affecting field is byte-identical. The one divergence the first capture
caught (the seam attaching `legend` to the LayerURI vs the register path's
side-band stash) was fixed to match the register transport before this was
recorded. Tests: `tests/test_outputs_seam.py` (6 cases incl. the equivalence
regression), `workers/sfincs/test_postprocess_wiring.py` (producer entries).

### NOT done this wave -- the live close-out (gated on the live-testing loop)

Per "worker image staleness gap" (worker code is INERT until rebuild),
"never half-wired", and "NATE tests live" -- the following are staged, NOT
applied, so the tree stays fully revertible (all additions; the register path is
byte-unchanged; nothing consumes `outputs.json` live yet):

1. **Wire the flood composer** (`flood.py`): when `read_outputs_manifest` returns
   a manifest, publish via the seam (a clean if/else next to the existing
   register-only vs on-box branches, same one-release-safety pattern). The seam
   output is proven byte-equivalent, so this is a mechanical swap -- but it must
   be proven THROUGH a rebuilt image + a live solve, not offline.
2. **The cap fix** (row-20 deletion): `output_interval_min` resolves
   `dtout`/`dtmaxout` DECK-SIDE (deck builder), retiring the post-hoc
   `MAX_FLOOD_FRAMES=144` thinning + wiring the lever on the pluvial path
   (`run_sfincs.py:_resolve_output_interval_min`, pinned OFF today). Worker/deck
   code -> needs the image rebuild + a live solve to prove the frame counts.
3. **IMAGE LAW**: rebuild sfincs (`-f workers/sfincs/Dockerfile`, repo-root
   context) -> provenance-check the mirror in-image -> live-smoke through it.
4. **Gates**: daemon restart + `ws_smoke.py all_passed`; the flood canary THROUGH
   the seam (status=ok + frames via `outputs.json`); the temporal-group reopen
   check; the NATE QGIS visual.
5. **Row-20 deletion**: only AFTER (1)-(4) pass -- the ledger row stays QUEUED
   with its condition PARTIALLY met (producer + seam + byte-equivalence bar =
   PASS; deck-side cadence + live proving solve = REMAINING).

## LIVE CLOSE-OUT EXECUTED (2026-08-17) -- the staged list, all green

The staged NOT-done list above is now EXECUTED under the live-testing loop. All
changes are additive to the register/on-box paths (legacy engines byte-unchanged;
a missing `outputs.json` is a no-op fallback).

1. **Flood consumer WIRED to the seam** (`flood/flood.py`): the post-solve
   publication is now a clean SEAM-or-LEGACY fork. `read_outputs_manifest(run_result)`
   is consulted FIRST; when it returns a manifest the SEAM
   (`build_layers_from_outputs`) owns ALL publication -- the peak (role `primary`)
   rides the success envelope, the temporal frames (role `context`) emit
   out-of-band, and the item-7 replay meta (`PublishedFrame`: `t` / `group_id` /
   seam-resolved `style_preset`) rides alongside. `publish_manifest.json` is still
   read, but ONLY for the top-level `FloodMetrics` narration scalars
   (`max/mean/p95_depth_m`, `flooded_cell_count`) -- the flat `outputs.json`
   entries carry no aggregates, so publish_manifest is the METRICS CARRIER, not a
   second publication (single publication; no layer registered twice). Absent
   `outputs.json`, the legacy register-only path runs byte-unchanged; absent both,
   the on-box postprocess fallback runs byte-unchanged.

2. **Cap fix (deck-side cadence, ledger row 20 DELETED)**: the post-hoc
   `MAX_FLOOD_FRAMES` even-subsample thinning is RETIRED in the worker reader
   (`workers/_raster_postprocess/sfincs_reader.select_frame_time_indices` now
   returns every solver-written index -- never-omit; the dead constant + `os`
   import removed). `_resolve_output_interval_min` UNPINS the pluvial lever: an
   explicit `output_interval_min` now flows through to the deck `dtout`/`dtmaxout`
   on BOTH sim types (was silently dropped on pluvial); an UNSPECIFIED pluvial run
   keeps the legacy hourly deck formula (`None` sentinel -> `max(600, total/24)` s,
   byte-identical). Deck-side dtout is now the SOLE frame-count control.

3. **IMAGE LAW**: `trid3nt-local/sfincs:latest` rebuilt (`-f
   workers/sfincs/Dockerfile`, repo-root context). In-image provenance verified:
   `outputs_manifest` schema 1 present, `select_frame_time_indices` returns
   `list(range(n_steps))` (cap retired), producer `outputs_entries` wired,
   `entrypoint._write_outputs_manifest` present. (The prior image was STALE -- the
   pre-refactor `services/` layout, no producer.)

4. **Live gates (foreground)**: daemon restarted; `ws_smoke.py` `all_passed=True`.
   The FLOOD CANARY THROUGH THE SEAM was run as a coastal QUADTREE solve
   (`run_sfincs_quadtree_direct.py`, run `01QT260817055751MEXBEACH`) -- the ONLY
   local path that runs the WRAPPER image + its producer entrypoint (the pluvial
   regular-grid path runs the raw `deltares/sfincs-cpu` image + on-box postprocess
   and writes NO manifests, so it exercises the seam only as a no-op fallback).
   Evidence: completion `status=ok`; `outputs.json` present (schema 1, 26 entries =
   1 peak + 25 frames); 25 frames at the deck-side 30-min cadence over the 12 h
   window (`t=0..43200 s`), ALL 25 published (never-omit, no thinning); each entry
   carries `bbox` + `band_stats` render hints. The flood seam consumer, run against
   the real image-produced prefix, registered all 26 layers (seam publish lines,
   NOT the register path's), one temporal group `flood-depth-<run_id>`, replay
   stamps (peak `t=None`; `frame-01 t=0.0 group_id=flood-depth-<run_id>`; `frame-25
   t=43200.0`), and threaded the metrics carrier's `max_depth_m=19.99`,
   `flooded_cell_count=176473`. Reopen: the temporal group reforms from the
   byte-identical `"Flood depth step N"` name token (`detectSequentialGroups`,
   unchanged from the register path) + the carried replay stamps; no new
   persistence-schema field was needed (name-token grouping already survives
   reopen -- the item-7 explicit-stamp PERSISTENCE wiring is deferred as the
   GC'd-uri tolerance enhancement, not a live-gate blocker). The pluvial canary
   (`run_sfincs_direct.py`) proves the regular path byte-unchanged (seam no-op).

5. **Row-20 DELETED** (ledger). The post-hoc thinning is gone + proven live. The
   `publish_manifest.json` frame entries the producer dual-writes are SUPERSEDED
   (the seam ignores them) but RETAINED as the one-release register-only fallback +
   the metrics carrier -- DECISION: `publish_manifest` "keeps non-frame entries"
   for SFINCS; its slimming/removal is row-19 (the Section-7.3 collapse, gated on
   the LAST engine migrating off `publish_manifest.json`), NOT this wave. New tests:
   `tests/test_flood_seam_fork.py` (fork precedence + metrics-carrier coexistence +
   replay stamps), `test_select_frame_time_indices_never_omits`, the unpinned
   pluvial-lever cadence tests.
