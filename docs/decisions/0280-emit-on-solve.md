# ADR 0280 -- emit-on-solve: the append-only `outputs.json` seam (wave 1 foundation)

Status: FOUNDATION LANDED (schema + contracts + registry + tests); LIVE
close-out (flood proving case, cap fix, scaffold reconciliation) SPECIFIED,
gated on the live loop. Date: 2026-08-16.

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
