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

## emit-on-solve (`outputs.json`) -- FROZEN schema, foundation landed

The append-only `outputs.json` manifest (`trid3nt_contracts.outputs_manifest`,
`schema_version=1`) is the emit-on-solve wire: a solver leg appends flat
`{kind, quantity, name, uri, t?, units?}` entries under its run prefix; the
seam consumer reads them on the existing completion poll and publishes each
(raster+`t` sharing a `quantity` = a temporal group; raster with no `t` = one
layer; vector = a vector layer; mesh/scalar = log-only in v1). v1 is AT-EXIT;
a MISSING manifest is a no-op (legacy engines byte-unchanged). See
`docs/design/outputs-manifest-schema.md` (frozen schema) + ADR 0280 (the seam,
the flood proving case, the scaffold reconciliation, the cap fix). The
consumer WIRING + the SFINCS producer + the `output_quantities` scaffold
reconciliation are the gated live close-out (ADR 0280).

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
