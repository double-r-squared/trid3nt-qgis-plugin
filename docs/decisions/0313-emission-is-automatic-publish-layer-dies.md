# 0313 - Emission is automatic: the publish mechanism moves to emission/, the tool dies

Status: LANDED
Date: 2026-08-25
Supersedes: the `auto_publish` opt-out (ADR 0075's intermediate-raster
suppression). Completes the `publish_layer` row of `docs/DELETION_LEDGER.md`
(QUEUED 2026-08-24, NATE ruling b).

## Context

`publish_layer` was a registered tool the model had to remember to call. A
computed raster was invisible until it did, so the failure mode was silent and
recurring: a tool did real work, returned a COG, and nothing appeared.

The system had already half-admitted this. A deterministic auto-publish sat in
the dispatch layer (`server/dispatch/emitter.py` -> `results.py::
_auto_publish_droppable_raster`) firing on any raster `LayerURI` with a raw
object-store uri, so most rasters published without being asked. But it was a
SECOND call site, parallel to the emission seam
(`emission/layer_uri_emit.emit_layer_uri`, reached from
`PipelineEmitter.emit_tool_call`), reachable only from the WS server, gated by
a per-tool `auto_publish` flag, and it re-emitted the layer through the seam
afterwards so `add_loaded_layer` had to dedup by COG identity.

Meanwhile the mechanism itself was in the wrong package. `tools/publish_layer/
publish_layer.py` was 2,273 lines of which roughly 114 were tool shell -
decorator, metadata, wire signature, an 83-line routing docstring - wrapped
around ~1,845 lines of module-level mechanism that `emission/` was already
importing eight symbols of. `emission/outputs_seam.py` imported five styling
functions at module level; `pipeline_emitter` lazily lifted legends out of the
module's own global stash; and `publish_layer` imported
`observe_published_layer` back out of `emission/uri_registry.py`. That is a
package-level import cycle, surviving only because `uri_registry` happens to
import nothing from `tools`.

NATE's ruling (b): emission is automatic everywhere, the "display this" intent
is retired, and the user hides what they do not want to see in QGIS.

## Decision

**1. The mechanism moves to where its consumer is.**
`tools/publish_layer/publish_layer.py` -> `trid3nt_server/emission/publish.py`.
Every edge that used to cross the package boundary is now intra-package, and
the cycle is one directed edge. The tool shell is stripped: no
`@register_tool`, no `AtomicToolMetadata`, and the LLM-facing routing docstring
is replaced by one that describes what the function does to a COG.

**2. Auto-emit rides the ONE seam.**
`layer_uri_emit.publish_for_emission` publishes a raster `LayerURI` carrying a
raw `s3://` COG - overviews, resolved style params, data-driven legend - and
`PipelineEmitter.emit_tool_call`'s LayerURI branch calls it before the
`emit_layer_uri` guardrail. A raster-producing tool's whole obligation is to
return a `LayerURI`. There is no publish call site to add per tool, and a LIST
of layers (a frame series) takes the same trip, which the dispatch site handled
and the seam did not.

**3. The dispatch-layer auto-publish is deleted, not disabled.**
With it go `_auto_publish_droppable_raster`, `_emit_auto_publish_failure`,
`server/styles.py` (`_resolve_publish_wrap_style_preset` becomes
`emission.publish.style_preset_for_publish`, unchanged; `_is_droppable_object_store_raster`
dies with the call site it fed), the publish_layer wrap-site, the per-Case
`.qgs` routing branch, and the small-model `layer_id` injection.

**4. There is no opt-out.**
`SourceSpec.output.auto_publish` and `AtomicToolMetadata.auto_publish` are
removed along with the four `source.yaml` declarations that used them
(`fetch_dem`, `fetch_landcover`, `fetch_3dep_extra`, `fetch_topobathy`). An
intermediate is still a layer.

**5. The registered tool is deleted.** Registration, corpus, and the
`CORE_FLOOR` retrieval entry. Registry 253 -> 252.

## The honesty floor changed shape, and that is the point

The old auto-publish surfaced a typed `LAYER_AUTO_PUBLISH_FAILED` because in
the MapLibre era an unpublished `s3://` raster was unreachable - a failed
publish meant no layer. On the QGIS-native stack the plugin reads a raw COG via
`/vsicurl/`, so publishing ENRICHES a raster rather than making it reachable. A
failed publish is now a DEGRADE - an unstyled layer plus a warning - and
`publish_for_emission` fails open to the original uri. The guardrail that keeps
genuinely un-renderable rasters (`gs://`, `file://`, empty) off the map is
still `emit_layer_uri`, and it still runs after the publish.

## Consequence

Proven live by `scripts/proof_auto_emit_seam.py`
(`docs/proof/auto_emit_seam_evidence.json`): `fetch_dem` then
`compute_hillshade` over real 3DEP terrain, driven through
`emit_tool_call` - no LLM, so a published layer can only have come from the
seam - produce two published overview COGs on the map, with the publish
mechanism called exactly twice (once per raster, unasked) and
`'publish_layer' in TOOL_REGISTRY` False. The DEM is the case that matters
most: it was the flagship `auto_publish: false` opt-out, and it now reaches the
map.

One real bug fell out of the move. `workflows/lib/interpreter.py`'s declared
`RenderSpec` path imported `publish_layer` from the PACKAGE - which binds the
submodule, not the function - and handed it to `asyncio.to_thread`. Any
declared render reaching that line would have raised `TypeError`. Nothing
caught it because nothing had exercised the path; the move made the import
form impossible to write by accident.

Three placement debts are registered in the ledger rather than paid here:
`ensure_case_qgs` now has no caller (its only consumer was the deleted
publish_layer `.qgs` branch) but `qgs_project_uri` is live persisted state;
`emission/quantity_styles.py` still hand-mirrors `_QGIS_STYLE_REGISTRY` even
though the boundary that forced the copy is gone; and two lazy
`emission.publish -> tools.processing` imports remain (the GDAL COG translator
and the sediment-yield log-class table), which are the one wrong-direction edge
the move did not remove.
