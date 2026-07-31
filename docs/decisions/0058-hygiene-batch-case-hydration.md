# 0058 - hygiene batch: case-hydration redesign + cases/ package + transport/canopy fixes

Context: a four-item NATE-approved hygiene batch on the docstring-refresh (0057)
follow-through, plus a mid-batch NATE scope amendment (2026-07-31) that reshaped
item 1.

## Item 1 - case hydration: manifest-first, materialize as remote fallback

Context: ``open_case_in_qgis`` generated a QGIS ``project.qgz``/``.qgs`` project
XML (hand-built ``xml.etree``) that the plugin NEVER opened -- by the module's own
docstring the plugin adds exported GeoTIFFs STANDALONE, never ``QgsProject.read``
the .qgz (that would replace the user's open project). Standalone project export
is covered by native QGIS (per NATE). The .qgz half was dead weight.

NATE amendment (2026-07-31): the PRIMARY case-hydration path is MANIFEST + STREAM,
not materialize + download. Case hydration must reuse the SAME by-URI mechanism
live-session layers already use.

Decision:

1. **DELETE** the ``.qgz``/``.qgs`` project-XML generation and the inline-style
   half of the dual-styling contract (``_build_qgs_xml``, ``_spatialrefsys_4326``,
   the ``_WGS84_WKT`` const, the zip step, the extent/bounds machinery that only
   fed the project view). KEEP layer materialization (GeoPackage vector
   conversion, raster ``.tif`` downloads) and the ``.qml`` sidecar styling (the
   ``_raster_pipe_element`` -> ``_qml_bytes`` seam -- the sidecars are how a
   standalone-added COG renders the web colormap) and the result manifest.

2. **RENAME + RELOCATE**: ``open_case_in_qgis`` -> ``hydrate_case_layers``,
   moved to ``server/src/trid3nt_server/cases/hydrate_case_layers.py`` (the
   ``cases/`` package is created here -- the FIRST tenant of the
   server-modularization plan's ``cases/`` target; case_lifecycle.py etc do NOT
   move in this batch). Typed errors renamed for honesty
   (``ExportCaseError`` -> ``HydrateCaseError``, ``ExportInputError`` ->
   ``HydrateInputError``; ``CaseNotFoundError`` / ``NoExportableLayersError``
   kept). The result no longer carries ``qgz_path`` (the plugin already branches
   ``if plan.qgz_path`` and tolerates its absence).

3. **PRIMARY path = a manifest** (``build_case_layers_manifest``): reads the
   case's persisted ``loaded_layer_summaries`` through the same persistence seam
   and returns them verbatim under ``{"loaded_layers": [...]}`` (plus
   case_id/title/bbox). The plugin (local mode) fetches it and adds each layer by
   store URI via the SAME materializer (``parse_layer_events`` ->
   ``LayerMaterializer.materialize``) that live-published layers use -- no
   gpkg/tif round trip. ``hydrate_case_layers`` (materialize) SHRINKS to the
   REMOTE-mode fallback (the remote client cannot reach the local MinIO store
   today).

4. **Routes**: ``POST /api/export-qgis`` + ``GET /api/export-qgis/file`` stay
   WIRE-COMPATIBLE (the installed field plugin calls them) but re-point their
   internals to ``cases/hydrate_case_layers``. NEW ``POST /api/case-layers``
   returns the manifest (local-single-user gated exactly like ``/api/case-list``:
   404 on cloud). The NEW plugin calls ``/api/case-layers`` locally and
   ``/api/export-qgis`` only in remote mode.

Consequence: the local "Open case in QGIS" flow is now the same by-URI add live
rendering uses (durable, MinIO-reachable), and the QGIS-XML dead weight is gone.
Remote-mode STREAMING (presigned/proxied store access, plus the per-Case-durability
implications of a URL that can expire) is a SCOPED FOLLOW-UP; the materialize
fallback dies with it, and ``/api/export-qgis`` stays only until then. Wire-compat
rule: the old ``/api/export-qgis`` route survives until NATE's plugin reinstall
confirms the new one, then the follow-up removes the remote-fallback slice.

## Item 2 - register_case_layer relocate; agent/tools/ = registered tools

Decision: the ingest core (``ingest_user_layer`` / ``upload_layer_file`` /
``register_case_layer``) moves to ``cases/ingest_user_layer.py``; the two
``meta/`` folders (``open_case_in_qgis/``, ``register_case_layer/``) are deleted
outright. tool_catalog_http re-points its lazy-import seams. Three agent tools
that imported the shared URI helpers (``_strip_query`` / ``_unwrap_tile_template``)
from the old location -- ``query_point_hazard``, ``compose_case_report``,
``publish_layer`` -- re-point to ``cases.hydrate_case_layers`` (lazy,
function-level imports; no cycle).

Consequence: agent/tools/ no longer holds the two case-serving route-handlers.
Residuals honestly noted (out of THIS batch's named scope): ``meta/probe_point.py``
is another DEREGISTERED route-server (serves ``/api/probe-point``, identical
posture) -- a follow-up ``cases/`` relocation candidate;
``processing/aggregate_claims_across_sources`` is an intentionally-demoted
importable LIBRARY (ADR-0043) whose extractors the MODFLOW contamination composer
imports. FOLLOW-UP flagged: the pure URI helpers ideally hoist to a shared agent
util so agent tools do not import platform ``cases/``.

Package-seam note: ``cases/__init__`` re-exports ONLY the callables whose names do
NOT collide with a submodule name (``build_case_layers_manifest`` /
``upload_layer_file`` / ``register_case_layer``). Re-exporting the two
module-named functions would rebind the same-named submodule attribute and shadow
``import cases.<module>``; consumers import those via the full submodule path.

## Item 3 - _fetch_one_page routes through the shared transport

Context: ``vector_fgb._fetch_one_page`` ran a bare ``httpx.Client(timeout=60)``
with zero retry authority, bypassing ``_router/transport`` (a GDAL-collapse-audit
finding). Decision: route it through ``transport.get_bytes(get_client(), ...)``
(pooled client + the ONE retry authority: backoff + Retry-After on 429/5xx/timeout,
typed errors). The router error framing is preserved -- a ``TransportError`` is
caught and re-raised as ``router_upstream_error`` with the same prefix -- so
success paths are behavior-identical and callers/tests see no change; 429/5xx are
strictly better (real retry). Consequence: the promotion + fanout + executor +
engine + hooks suites stay green; ``test_router_fanout_routing`` now exercises the
real retry-with-backoff path on its 429/500 edge cases (fixture gained the
``httpx.Response`` attributes the pooled client reads), so its runtime grew from
~0.4s to ~18s (intended, inside the 300s timeout).

## Item 4 - canopy solver-spec gap; raster spatial_query rider

Context: ``"canopy"`` is in ``SOLVER_WORKFLOW_REGISTRY`` (a presence gate) but
registers NO ``LOCAL_SOLVER_SPEC_REGISTRY`` entry (its worker does not exist yet),
so ``_run_solver_local_docker`` silently fell back to the SFINCS docker spec --
a wrong-spec dispatch (docstring-refresh finding).

Decision (never-silent rule): the SFINCS docker fallback is scoped to
``solver == "sfincs"`` (the one solver that legitimately uses the docker path
without a registry entry). Any OTHER missing-spec solver raises
``SolverDispatchError`` (its workflow module must call
``register_local_solver_spec``). No canopy spec is invented. Verified only sfincs
(legit) and canopy (the bug) reach that branch -- MODFLOW launches
``launch_local_solver`` directly, every other engine self-registers at import.

Rider (docstring prose only): ``spatial_query`` is vector-only, so RASTER
fetchers that named it as their AOI-summary/zonal downstream were wrong; those
~8 sites (fetch_dem, fetch_hrrr_smoke/forecast, fetch_mrms_qpe, fetch_ghsl/hrsl
population, fetch_gridmet, fetch_chirps) now point at the code_exec playground
(zonal stats compose there). Vector fetchers keep their ``spatial_query`` hints.
Gated by the 0057 retrieval-verification rule (test_tool_retrieval green).

Consequence: no silent wrong-spec dispatch; routing text points rasters at the
right zonal-summary surface. Registry stays 190, zero registered-name changes.
