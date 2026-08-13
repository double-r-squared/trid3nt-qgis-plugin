# ADR 0116 -- Remote streaming: layers register in place, no download

Status: accepted (2026-08-04, NATE remote live-drive milestone)
Follows: 0103 (daemon-hosted plugin repo + remote-mode cull), 0115 (SCHISM
spike -- section 4d named this the load-bearing product dependency).

## Context

NATE's paradigm, verbatim: "we just stream the project to the client, not
download it on their machine" / "hot-swap between cases quickly as if we were on
the machine serving them" / (2026-08-04) "I honestly don't want the download
path to exist ... the remote downloaded data should have a ttl that lives as
long as the session is open." This is the prerequisite for the SCHISM landing
(ADR 0115 quantified 70+ GiB/day of continental netCDF) and for NATE's remote
live-drive of QGIS over the tailnet.

The live materializer ALREADY streamed COG rasters and FlatGeobuf vectors in
place via GDAL `/vsicurl/` (the TiTiler->QGIS swap landed that). What did NOT
exist as a clean discipline: (a) an honest STREAMED-vs-STAGED label per layer,
(b) a session-scoped TTL for the one format that must stage, and (c) removal of
the condemned download/export machinery. This ADR makes streaming THE path,
kills the download path, and pins the SCHISM output contract.

## Decision

### 1. Streaming is the path

A layer registers reading IN PLACE from the advertised MinIO/S3 endpoint
(`data_base`, server-advertised on `auth-ack`, else the dock's tailnet-host
fallback). No project bytes are downloaded to open a case.

Per-format matrix (each verified live 2026-08-04 against the tailnet MinIO at
`http://100.92.163.46:9000`, the exact path a remote client dials):

| format | QGIS provider | mechanism | streams? |
| --- | --- | --- | --- |
| COG raster (`.tif`) | `QgsRasterLayer(/vsicurl/..., "gdal")` | HTTP ranged: header + overviews + windows | **YES** -- live: 4.76 MB DEM opened reading 21% of the object; 4.5 MB layer at 5.3%; small COGs read whole (no overview to skip, but cheap). overviews exposed, no local file created |
| FlatGeobuf (`.fgb`) | `QgsVectorLayer(/vsicurl/..., "ogr")` | HTTP ranged: spatial-index + intersecting features | **YES** -- live: index-ranged, feature count read, no local copy |
| GeoJSON as s3 object | `QgsVectorLayer(/vsicurl/..., "ogr")` | whole-object over vsicurl (no spatial index) | YES (as a URL; whole object read into GDAL memory, still no local file) |
| inline GeoJSON | temp `.geojson` -> ogr | agent's additive `inline_geojson` merge is INLINE DATA, not a remote object | STAGES (small; labeled) |
| MDAL mesh (`.nc` UGRID / SFINCS `sfincs_map.nc`) | `QgsMeshLayer(local_path, "mdal")` | -- | **NO** -- MDAL rejects a `/vsicurl/` OR plain-URL source ("could not be found"); it demands a local path (proven live). This is the ONE streaming fallback: STAGES to the session temp dir, labeled |

Every layer note is labeled STREAMED (`streamed via /vsicurl (no local copy)`)
or STAGED (`staged to session temp ...`) -- NATE's no-silent-downloads floor.

### 2. Auth: anonymous ranged HTTP on the tailnet (the trust boundary)

MinIO serves its objects as plain anonymous HTTP GETs on the tailnet
(`200`/`206`, `Accept-Ranges: bytes` -- verified with no credentials). The
tailnet IS the trust boundary; there is no per-object presign or bucket policy
to satisfy. `/vsicurl/` mirrors exactly what the pre-existing download path did
(`urllib` GET with no auth) -- no GDAL http credentials, no presigned URLs, no
`/vsis3` signing. This is the honest minimal: the same access the plugin already
used, now ranged instead of whole-object. (If the store is ever moved off the
tailnet trust boundary, presigned `/vsicurl/` URLs or a GDAL http-header token
is the additive next step -- not needed now.)

### 3. The fallback: session-scoped TTL, cleaned on close

The staged mesh (the only non-streamable format) lands in a per-SESSION subdir
`trid3nt_session_<tag>` under the platform temp (one materializer = one dock
connection = one session):

- **Created** on first stage, with an `.owner_pid` marker.
- **Cleaned up** on dock disconnect AND on dock close / plugin unload
  (`LayerMaterializer.cleanup_session`) -- the TTL "lives as long as the session
  is open," exactly NATE's policy.
- **Stale sweep at plugin start** (`sweep_stale_session_dirs`, called from the
  dock constructor): a crash-leftover `trid3nt_session_*` dir is removed only
  when its owner PID is DEAD (or unreadable). A dir owned by a LIVE process --
  a concurrent QGIS instance -- is never touched, so no running session ever
  loses what it staged.

Nothing ever lands outside the session temp dir. There is no persistent
download and no download path to opt into.

### 4. Condemned machinery removed (DELETION_LEDGER conditions met)

The ledger's remote-hydration rows were conditioned on "remote store access
ships (presigned or agent-proxied ranges)." This wave IS that: direct anonymous
ranged `/vsicurl/` reads over the tailnet. Deleted (delete-don't-disable):

- Plugin: `render/layers.py::materialize_export` + `_apply_named_style` +
  `last_added_export_extent`; `net/tasks.py::_ExportTask`; `case/case_export.py`
  (whole module: `post_export_case` / `localize_remote_export` /
  `download_export_file` / `download_mesh_file` / `localize_mesh_entries` /
  `plan_export_layers` / `ExportPlan` / `ws_url_to_http_base` -- the 0103
  orphan); `ui/dock.py::hydrate_case_layers` + `_on_export_finished` +
  `_on_export_errored` + `_export_tasks`.
- Server: `cases/hydrate_case_layers.py` (whole module); the `POST
  /api/export-qgis` + `GET /api/export-qgis/file` routes and their helpers in
  `tool_catalog_http.py`.
- Tests: `test_export_qgis_http_route.py`, `test_open_case_in_qgis.py`,
  `test_open_case_in_qgis_mesh.py`; the export test classes in plugin
  `test_milestone2.py` / `test_milestone3.py`; the stale ADR-0103
  `test_remote_mode_is_honest_skip` (already red at HEAD).

Meshes previously reached QGIS ONLY through this (UI-unreached) export path;
they now stream-or-stage through the LIVE materializer via a new `_add_mesh`
branch, so no live behavior regresses and the MDAL know-how (peak-depth / tracer
dataset-group selection, explicit CRS) is preserved.

### 5. SCHISM output contract (the landing prerequisite)

SCHISM (ADR 0115) produces the biggest layers the product will ever emit: raw
per-variable scribed netCDF at ~1.2-1.4 GiB per 12h chunk per variable, order
70+ GiB/day at continental (STOFS-3D-Atlantic) scale. Contract, enforced at the
SCHISM engine-landing wave:

> **A SCHISM output MUST ride the same clip-to-AOI + COG discipline every other
> engine's raster output already does. No raw continental netCDF is ever
> published as a layer.** 2D surface fields (elevation, max-envelope) are
> clipped to the case AOI and encoded as COG-tiled rasters via the existing
> raster pipeline (which the live publish path already COGs + clips for every
> engine today); full 3D netCDF is reserved for on-demand subsetting, never a
> whole-domain layer. A small-domain run (a Test_CORIE / Test_WWM_Duck clip, not
> the full STOFS grid) is the realistic default product size.

This makes the streaming path safe at SCHISM scale: a clipped 2D COG streams
ranged (5-21% of object as measured here); a 70 GiB continental netCDF never
becomes a layer in the first place.

## Consequences

- The plugin has no download/export code and no user-facing export action
  (native QGIS covers file export). Layer notes are honestly labeled.
- The QGIS-dependent behavior (MDAL open, `/vsicurl` GDAL/OGR) was verified
  headless against the live tailnet MinIO; NATE's in-app visual check in his
  remote macOS QGIS is the final live confirmation (the daemon is not restarted
  by the specialist).
- The mesh live-stream fallback is ready for the SCHISM landing to emit a `mesh`
  layer row; today the live `loaded_layers` contract is `raster|vector`, so the
  mesh branch activates when the server adds `layer_type="mesh"` (a small,
  additive server change owned by the SCHISM landing).
