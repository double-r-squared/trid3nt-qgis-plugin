# Processing folder de-cloud refactor (FOR NATE REVIEW)

NATE 2026-07-29: the processing folder gets the same treatment as the
repo-wide arc - de-cloud and clean the architecture. The subprocess GDAL
layer was designed for an isolation model (compute on Lambda/serverless,
detached from server credentials) that the offline local monolith retired.
Evidence in-code: compute_colored_relief downloads the DEM "instead of
/vsigs/ - the gdaldem subprocess has no guaranteed GCS credentials";
_translate_to_cog reasons about QGIS Server WMS gateway limits over
/vsigs/; the binary resolver falls back to the old dev-Mac grace2 conda
env. Mechanism (NATE pick 2026-07-29, from gdal-leverage-audit.md):
TRIM SUBPROCESS - keep the canonical gdaldem/gdal_contour binaries
(zero algorithm drift), collapse the duplicated wiring, swap everything
rasterio already covers to in-process. osgeo bindings REJECTED (no
matching wheel, ABI-clash risk); pure-wheel numpy swap DEFERRED, gated
on the Mac 4.0.3 test proving the system-GDAL prerequisite painful.

## Scope

server/src/trid3nt_server/agent/tools/processing/ - ~24.6k lines,
39 tools + charts/ + charts_common.py + _hydrology_common.py.
Riders from the GDAL audit: S3 raster_cog 404 precision fix,
S5 fetch_topobathy._gdal_bin dead-code delete.

## Workstreams

1. SHARED GDAL RUNNER: one module owns binary resolution (env override
   -> PATH; the grace2 conda fallback DIES with its prose), PROJ/GDAL
   data-dir env wiring, subprocess invocation + timeout, rc -> typed
   error mapping. The CLI callers (compute_hillshade, compute_slope,
   compute_aspect, compute_colored_relief, compute_contours,
   compute_blended_composite, enhance_satellite_image,
   clip_raster_to_bbox) call the runner; the ~1,300-1,400 lines of
   6x-duplicated wiring collapse. Cross-tool imports (compute_aspect
   reaching into compute_hillshade for helpers) move to the shared
   module - same pattern as the SFINCS shared/ hoist.
2. IN-PROCESS SWAPS (rasterio-covered, no gdaldem needed):
   gdal_translate -of COG -> rasterio COG driver (write-verified by the
   audit probe); gdalwarp + bbox clip -> rasterio reproject/windowed
   reads; VRT/mosaic stays rasterio.merge. Only gdaldem + gdal_contour
   remain as subprocess.
3. BEHAVIORAL DE-CLOUD (not just comments):
   - compute_colored_relief: the download-to-tempfile step existed
     because the subprocess lacked cloud creds; in-process reads drop it.
   - compute_building_density: outputs EPSG:3857 "for QGIS Server WMS".
     QGIS reprojects layers on the fly, so native-CRS output is likely
     correct now - BEHAVIOR CHANGE, needs a render proof and its own
     line in the report (flagged, not silently changed).
   - Cloud-residue audit in the non-GDAL files the census flagged
     (compute_zonal_statistics, compute_urban_heat_island,
     charts_common, compute_exposure_summary,
     aggregate_claims_across_sources, _hydrology_common): judge each
     /vsigs//GCS/QGIS-Server assumption - comment vs behavior.
4. COMMENT JUDGMENT PASS on every touched file: read every comment,
   constraints stay compressed, narration/root-cause stories/repro
   dates die (git is the archive). Never regex.
5. AUDIT RIDERS: S3 - raster_cog direct_window re-stamps 404 as the
   twin's EMPTY/NOT_AVAILABLE split (fails a forced-404 edge matrix
   today); S5 - delete fetch_topobathy._gdal_bin (0 call sites).
6. Temp-file plumbing -> MemoryFile where the lifetime constraint
   allows (the orphaned-MemoryFile lesson: dataset must not outlive it).

## Identity gates (all mandatory)

- Registered names/docstrings/corpus: byte-identical (registry 200).
- Offline suite FAILED set == baseline exactly, proven pre/post.
- Envelope parity per tool; pixel-identical outputs for gdaldem-backed
  tools on a golden AOI (same binaries -> checksums must match).
- compute_building_density CRS change: render proof + explicit
  before/after screenshots (SendUserFile), called out in the report.
- Daemon boot + catalog identity; flood canary IF clip_raster_to_bbox
  or any touched seam feeds flood consumers (check importers first).

## Out of scope

numpy DEM reimplementation (IDEAS, Mac-test-gated); osgeo bindings
(rejected); fetch_topobathy/fetch_landcover fold-side GDAL (their
fetcher families own them); S2 /vsizip/ router mode (fold campaign);
S4 ArcGIS-via-OGR (DO-NOT-PURSUE per audit).

## Sequencing

Executes AFTER fold wave 3 lands (suite serialization). One
identity-gated wave: execute (opus) -> adversarial identity gate
(opus, refute-by-default) -> canary (sonnet). Orchestrator commits.
