# 0048 - processing de-cloud: trim the GDAL subprocess wiring

Context: the `processing/` terrain compute_* tools carried a subprocess-GDAL
layer designed for a serverless isolation model (compute detached from server
credentials) that the offline local monolith retired. Six tools each
re-implemented the same wiring: a `gdaldem` binary resolver with a dev-Mac
`grace2` conda fallback, a PROJ/GDAL data-dir env builder, a `subprocess.run`
+ returncode->typed-error block, and a `gdal_translate -of COG` shell-out.
Cross-tool imports reached into `compute_hillshade` for `_translate_to_cog` /
`_gdaldem_subprocess_env` / `_download_dem_bytes`. Cloud-era prose (`/vsigs/`,
"the gdaldem subprocess has no GCS creds", "QGIS Server WMS gateway limit")
described infrastructure that no longer exists. NATE picked the TRIM SUBPROCESS
mechanism (gdal-leverage-audit.md): keep the canonical `gdaldem` / `gdal_contour`
binaries (zero algorithm drift; rasterio has no equivalent), collapse the
duplicated wiring, and swap everything rasterio already covers to in-process.
osgeo in-process bindings were rejected (no wheel, ABI-clash risk).

Decision:
- One shared runner module `processing/_gdal_runner.py` owns: binary resolution
  (env override -> PATH; the `grace2` conda fallback is deleted), PROJ/GDAL
  data-dir env wiring, the single subprocess invocation (timeout + returncode ->
  typed error via a caller-supplied error factory), the shared raster-bytes
  reader (S3/local), and the in-process COG encoder.
- `gdaldem` (hillshade/slope/aspect/color-relief) and `gdal_contour` remain the
  ONLY subprocess calls -- rasterio cannot reproduce them. Each tool keeps its
  own typed error class + code by passing an error factory to `run_gdal`.
- `gdal_translate -of COG` is replaced by the rasterio COG driver in-process
  (`translate_to_cog`): tiled + overviews in one pass, preserving dtype, CRS,
  transform, nodata, band color-interpretation, RGBA, and band-1 palette color
  tables. Best-effort (returns flat bytes on failure, never raises).
- The cross-tool imports hoist into the shared runner. `compute_hillshade`
  keeps `_get_gdaldem_bin` and `_translate_to_cog` as thin module-level
  delegations because out-of-scope consumers (publish_layer, fetch_landcover)
  import them; `_translate_to_cog` keeps its 2-arg signature (the binary arg is
  accepted and ignored).
- Behavioral de-cloud: cloud-era prose compressed to constraints; the S3/local
  read is unified through the shared reader. `compute_building_density`'s
  EPSG:3857 output is KEPT (flagged, not changed): its registered docstring
  hard-codes EPSG:3857 (frozen by the identity gate), the cell-size semantics
  are defined on the Web Mercator grid, and the geographic-correctness tests
  assert on it -- native-CRS output would require a docstring edit that breaks
  the gate and re-derivation of the binning. `compute_colored_relief`'s local
  DEM staging is KEPT (irreducible: `gdaldem color-relief` is a subprocess that
  reads a file path; the cloud framing was de-clouded, the staging was not).
- S5 dead code: `fetch_topobathy._gdal_bin` (0 call sites; merge runs in-process
  via `rasterio.merge`) deleted, with the vestigial monkeypatch dropped from its
  live (skipped-offline) test.

Consequence: ~six copies of the resolver + env + subprocess + returncode wiring
collapse to one module; the `grace2` conda fallback and the cloud-era prose are
gone. gdaldem/gdal_contour output stays pixel/geometry-identical to the raw
canonical binary (verified: same binary + same args). The COG encoder no longer
depends on the `gdal_translate` binary being on PATH; it builds overviews +
tiling in-process (verified on city-scale rasters) and is exercised green by the
full publish_layer suite (the flood-depth publish path flows through
`translate_to_cog`). Registry identity (186 tools, names + docstrings) is
byte-identical; the offline suite failure set is unchanged. The S3 raster_cog
404 rider is subsumed by the httpx transport adoption (ADR 0044).
