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
