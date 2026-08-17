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
  persistence stamp. WIRED into the flood composer (`flood/flood.py`): a
  seam-or-legacy fork -- `outputs.json` present -> the seam owns ALL publication,
  `publish_manifest.json` supplies ONLY the `FloodMetrics` narration scalars
  (the metrics carrier); absent -> the legacy register/on-box paths run
  byte-unchanged. Fork contract pinned in `tests/test_flood_seam_fork.py`.

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
0280 EXECUTED). The SFINCS flood composer is WIRED to the seam and the deck-side
cadence cap fix landed (ADR 0280 live close-out).

MIGRATED ENGINES (S-class, each: worker producer + composer seam-or-legacy fork +
byte-equivalence + deck-side cadence + post-hoc thinning deleted + live proof
through a rebuilt image):

- **SFINCS flood** -- the proving case (ADR 0280).
- **GeoClaw inundation** (ADR 0281): `quantity="flood_depth"`, per-frame `t` read
  from the `fort.t` sibling; composer fork in `workflows/geoclaw/inundation`;
  `publish_manifest` = metrics carrier.
- **SWAN nonstationary waves** (ADR 0281): `quantity="wave_height"`, evenly-spaced
  `t` from `sim_duration_s`; composer fork in `workflows/swan/wave_field`. The
  rebuild also shipped the SWAN postprocess in-image for the first time + fixed a
  COG-upload ordering gap.

Each seam mints `layer_id` off the PHYSICAL quantity (`flood-depth-*` /
`wave-height-*`), the one explained non-rendering divergence from the register
path's engine-prefixed stems; web temporal grouping rides the `name` token,
unchanged. Cadence stays the count-native `output_frames` lever (aliased to the
universal `output_interval_min` vocabulary in ADR 0281; no redundant param added).

REMAINING: the other engine legs (M/L-class), the `publish_manifest` collapse
(ledger row 19, gated on the LAST engine migrating), and -- separately, OPTION A
-- the per-engine `output_quantities` scaffold migration.

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
