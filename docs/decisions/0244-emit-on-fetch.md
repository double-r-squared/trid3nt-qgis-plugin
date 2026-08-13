# ADR 0244 - Emit-on-fetch: the render declaration IS the visualization intent

Status: SEAM LANDED (2026-08-13). The IN-COMPOSER input-surfacing gap is closed
at the shared universal-fetcher router seam (`route()`): a fetch whose spec
returns a renderable `LayerURI` (a raster COG or a vector FGB, never a `record`
dict) is auto-surfaced as a `role="context"` "Input: ..." row whenever it is
fetched NESTED inside a composer, riding the existing `layer_uri_emit` machinery.
The direct-chat path is unchanged (the tool-wrapper already emits the returned
LayerURI). The S2 collapse (deleting the 18 per-family `_surface_*` helpers + the
~54 composer emission call sites + the uri-threading plumbing) and the live flood
canary are SCOPED to the NATE live-verification loop (per "flood canary after
LARGE changes" + "NATE tests live"); until then the seam and the legacy helpers
COEXIST, de-duped by uri (no visible double rows).
Date: 2026-08-13

## Context

Every engine consumes renderable inputs (DEM/topobathy, rivers, land cover,
fault traces, building footprints), but only the RESULT layer was published
automatically. Input surfacing was bolted on per-family through hand-written
`_surface_*` helpers + explicit `publish_input_layer` / `publish_raster_input_cog`
call sites threaded through composer bodies (task #207, ADR 0231). That is ~54
call sites across 34 workflow files plus the uri-threading plumbing that existed
only to feed them (3-tuple returns, `uri_sink` params, `WatershedMesh` input-uri
fields). It is repetitive, easy to forget on a new template, and it duplicates a
decision the spec already encodes.

## Decision (settled semantics, docs/IDEAS.md 2026-08-13)

No boolean flag. The spec's RENDER DECLARATION is the intent:

- **Presence** (the source returns a renderable `LayerURI`) = the data has a
  visual form and WILL be surfaced WHEREVER fetched, in BOTH calling modes. The
  direct-chat tool-wrapper already honours this; the in-composer bare-function
  path was the gap.
- **Absence** (a `record` source, `layer_type=record`) = the data genuinely has
  no visual form (records/series) and nothing tries.
- `visualize=False` is a per-CALL belt-and-suspenders reserved ONLY for PROBE
  fetches of visualizable data (AOI candidate scans); using it on consumed data
  is re-hiding a layer (sweep-test policeable in S2).
- `purpose=` lets a composer contribute ONE word to the surfaced layer's name
  (a label, never a pathway) - e.g. `purpose="mesh bed"`.

This is pipeline-library brick 2: `load()` = fetch + declared-emit.

## The seam as landed (S1)

`server/.../fetchers/_router/emit_on_fetch.py::maybe_emit_input_on_fetch`, called
from `route()` immediately after a successful `LayerURI` build. It fires IFF:

1. an emitter is bound (`current_emitter()`; ambient, the same one composers use);
2. this is NOT the direct dispatch of the fetcher itself. A new
   `dispatched_tool_name()` contextvar (bound by `emit_tool_call` alongside
   `current_emitter`) discriminates: name == the fetcher -> direct chat, the
   wrapper emits, seam stays silent; name == a composer -> nested, seam surfaces.
   This is robust under `substep(...)` (which does not touch the contextvar);
3. `visualize` is not `False`;
4. the spec declares a renderable output (`layer_type in {raster, vector}`);
5. the uri has not already been surfaced this session (dedup on
   `emitter._emitted_input_uris`).

Provenance name: `Input: <what> (<source>[, <resolution>])` (+ the `purpose`
word). Role forced to `context`, bbox dropped (no competing zoom-to). Rasters
ride `publish_raster_input_cog` (registers the preset + returns a
plugin-renderable uri), vectors ride `publish_input_layer` (inline server-side).
Emission is BEST-EFFORT: a failure NEVER fails the fetch (logged once).

### The sync/async bridge (load-bearing)

`route()` is SYNCHRONOUS and a fetcher is frequently off-loaded to a worker
thread (`_ALWAYS_OFFLOAD_SYNC_TOOLS`), while the emit machinery is async. The
seam drives its coroutine onto the emitter's captured loop (`_bound_loop`,
captured by `emit_tool_call`):

- **Worker thread** (no running loop there; the loop is free, awaiting the
  `to_thread`): `run_coroutine_threadsafe(...).result()` - WAITED, so ordering +
  WS framing are preserved (the composer task is parked on the thread, no
  concurrent sink write). This is the common composer path.
- **Loop thread** (a composer that did not off-load): fire-and-forget task (a
  blocking wait would deadlock); runs the instant the sync stack unwinds, still
  before the long solve. A strong ref is kept.
- **No loop** (verify/CI/direct): run inline.

## Consequences

- New templates get input surfacing for FREE - fetch through the router and the
  declared-renderable input appears. No per-composer call site to remember.
- `visualize` / `purpose` are router-level kwargs (absorbed by the promoted
  signature's `**_extra_ignored`, popped in `route()` before validation/cache),
  so EVERY spec inherits them with zero schema change.
- S2 (the collapse) removes the now-redundant `_surface_*` helpers + call sites
  + uri-threading plumbing and adds the name-pattern + call-pattern SWEEP TEST
  (0232 style). Until S2 lands atomically the two paths coexist (uri-deduped).
- LOOSE ENDS (audit S3): the seam covers AGENT-SIDE router fetches. IN-WORKER
  bathymetry sampled inside a solver container never touches `route()` and stays
  on the bespoke "recorded-COG" surface (river_dye's `bed_bathymetry.tif` ->
  `publish_raster_input_cog` is the template). artemis (agitation) + tomawac
  (telemac/wave_field) do NOT yet emit an in-worker bed COG - a worker-image
  change (write + record `bed_cog`), scoped to the worker wave, not this landing.
