# 0060 - opening a case restores its layers with its chat, in one gesture

NATE decision A (2026-07-31): opening a case must restore its persisted
LAYERS as well as its chat, in ONE gesture. The separate right-click
"Export GeoTIFFs" layer-load action is redundant and dies. Zero
user-facing export remains -- native QGIS covers any file export a user
wants.

## Finding: the fold already existed

``Trid3ntDock._on_case_open_event`` (the ``case-open`` WS envelope handler,
fired on every select/new/startup-reuse case-open) already restored a
case's persisted ``loaded_layer_summaries`` via
``materializer.materialize(info.layers)`` alongside the chat replay --
predates this job, proven live by ``headless_case_switch_proof.py``. That
data is the SAME ``case.loaded_layer_summaries`` the ``/api/case-layers``
manifest route (``build_case_layers_manifest``, ADR 0058) also serves, so
the manual "Open in QGIS" action's local branch (added in ADR 0058, same
day) was fully redundant with the automatic WS replay -- no new HTTP fetch
was needed to satisfy decision A.

## Decision

1. **No new fold code.** ``_on_case_open_event`` stays the single
   chokepoint. Added a ``MODE_LOCAL`` guard around its
   ``materializer.materialize(info.layers)`` call: the by-URI materializer
   needs the store directly reachable (MinIO), which remote mode cannot do
   -- unguarded, every remote case-open would attempt (and fail) a by-URI
   add per persisted layer. Chat restore is unaffected (both modes).
2. **Deleted** the redundant LOCAL manifest-fetch path this job's
   predecessor (ADR 0058) added for the manual action: plugin
   ``case_export.fetch_case_layers_manifest``, ``net.tasks._CaseLayersTask``,
   ``dock._on_case_layers_finished``, and ``open_case_in_qgis``'s local
   branch. The server's ``/api/case-layers`` route + ``build_case_layers_
   manifest`` are UNTOUCHED (still valid, just no longer called by the
   plugin) -- left in place as a general manifest API, not added to
   DELETION_LEDGER (not redundant in principle, just currently unexercised
   by this UI flow).
3. **Deleted** the "Export GeoTIFFs" context-menu action
   (``cases_dialog.py``) -- opening a case now does this automatically.
4. **Renamed + shrank** ``open_case_in_qgis`` -> ``hydrate_case_layers``
   (matching the server's ADR-0058 naming) to a REMOTE-mode-only helper
   (the old ``_ExportTask`` materialize+download fallback). Not deleted --
   condemned in DELETION_LEDGER pending remote store access (existing
   entry, ADR 0058 amendment) -- but now unreached from any UI trigger
   (no menu action calls it). Kept callable (not deleted outright) so the
   remote code path is not lost, and as the natural fold-in point for an
   automatic remote restore once store access lands.

## Known gap (not fixed here, disclosed)

Native MDAL mesh layers (SFINCS quadtree / MODFLOW / TELEMAC SELAFIN) ride
ONLY ``materializer.materialize_export`` (``plan.mesh_entries``), never
``materializer.materialize`` (the by-URI path both ``session-state`` and
``case-open`` use) -- this was already true as of ADR 0058 (mesh
materialize "survives only as the remote fallback"), predating decision A.
Reopening a LOCAL-mode case with a mesh-carrying run therefore restores its
raster/vector layers automatically but NOT its native mesh layer -- no
regression from this job, but the gap is now more visible since the manual
action that (only in remote mode) could reach mesh materialization has no
UI trigger left. Flagged for a follow-up (fold mesh discovery into
``build_case_layers_manifest`` so it rides the same by-URI case-open
replay).
