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
  vector = a vector layer, mesh/scalar = log-only in v1. `layer_id` is
  deterministic + idempotent from `(quantity, t-ordinal, run_id)`; the legend is
  STASHED side-band (byte-identical to `register_manifest_layers`). Proven
  field-for-field byte-equivalent in `tests/test_outputs_seam.py`. Carries the
  parallel `PublishedFrame` replay meta (`t` / `group_id`) for the item-7
  persistence stamp. WIRING into the flood composer is the live close-out.

## emit-on-solve (`outputs.json`) -- FROZEN schema, foundation landed

The append-only `outputs.json` manifest (`trid3nt_contracts.outputs_manifest`,
`schema_version=1`) is the emit-on-solve wire: a solver leg appends flat
`{kind, quantity, name, uri, t?, units?}` entries under its run prefix; the
seam consumer reads them on the existing completion poll and publishes each
(raster+`t` sharing a `quantity` = a temporal group; raster with no `t` = one
layer; vector = a vector layer; mesh/scalar = log-only in v1). v1 is AT-EXIT;
a MISSING manifest is a no-op (legacy engines byte-unchanged). See
`docs/design/outputs-manifest-schema.md` (frozen schema; v1 gained OPTIONAL
`bbox` + `band_stats` render-hint fields, ADR 0280 EXECUTED) + ADR 0280 (the
seam, the flood proving case, the scaffold reconciliation, the cap fix). The seam
consumer (`outputs_seam.py`), the SFINCS worker producer (`outputs.json` written
alongside `publish_manifest.json`), and the byte-equivalence bar are LANDED (ADR
0280 EXECUTED). REMAINING as the gated live close-out: wiring the flood composer
to the seam, the deck-side cadence cap fix (ledger row 20), the worker image
rebuild + live proving solve, and -- separately, OPTION A -- the per-engine
`output_quantities` scaffold migration.

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
