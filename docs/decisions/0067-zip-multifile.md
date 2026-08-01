# 0067 - zip/multi-file: the wave-6 ZIP-family deferral folds via WHOLE-OBJECT extract

Context: ADR 0052 (wave-6) DEFERRED the ZIP-member family (ghsl_population,
administrative_boundaries, river_geometry, storm_tracks) with LIVE evidence that a
zip-member RANGE-READ transport does not hold: a DEFLATE inner member forces a
near-whole transfer (GHSL tiles), and a multi-file shapefile / FileGDB needs every
sidecar member co-located. This wave re-reads the three targets IN FULL against the
phase inventory ADR 0063/0064/0065/0066 landed and folds the two whose honest shape
is a WHOLE-OBJECT fetch + in-memory / tmp-dir extract -- the ``gzip_object``
precedent (ADR 0055) at ZIP scale. The zip-member premise is REJECTED-superseded,
not revived: the wave-6 evidence stands; the fold does not need it.

Decision (2026-08-01):

1. **ONE shared step, built once, no-op for all 45 priors: ``transport.get_zip``.**
   Fetch the whole ZIP object through the shared transport (the ONE retry authority
   -- 429/5xx/timeout backoff + ``Retry-After``; a 404/403 classifies to a typed
   ``Transport*`` error) and open it as a ``zipfile.ZipFile`` over the in-memory
   bytes. Callers pick a member (``.read(name)`` -> a rasterio MemoryFile) or
   extract siblings (``.extractall(dir)`` -> a geopandas read). This is the entire
   new transport surface -- ~15 lines, no windowing, no zip-member central-directory
   machinery. The per-source quirks stay PURE hooks; the download + extract + read
   + filter / mosaic + merge is source-agnostic in the executors.

2. **fetch_ghsl_population FOLDED -- raster ``fixed_tile_grid`` access mode.** The
   global GHS-POP grid is cut into a regular 10-deg lattice of per-tile ZIPs, each
   wrapping ONE DEFLATE ``.tif`` member. The new ``_fixed_tile_grid_to_array`` maps
   the bbox to its covering tiles (the twin's exact offset-lattice row/col math),
   ``get_zip``s each intersecting tile, reads the named member into a MemoryFile,
   windows it (the twin's floor-offset / ceil-length / clip window), collapses a
   negative source fill to NaN, and NaN-merges the tiles -- value-identical to the
   twin's ``/vsizip//vsicurl/`` windowed read (same member bytes, same window math).
   A missing tile (ocean-only R/C the archive omits -> 404) is a coverage gap
   (skip); an all-NaN / no-tile window -> typed EMPTY; a window over the pixel cap
   -> typed INPUT. LIVE twin-vs-router value-identical: Lagos bounds / min / max /
   mean / dtype / crs / nodata all equal; ocean -> GHSL_POPULATION_EMPTY; degenerate
   bbox + unsupported epoch -> GHSL_POPULATION_INPUT_INVALID (retryable False) --
   every code identical.

3. **fetch_administrative_boundaries FOLDED -- ``zip_vector`` executor + a PURE
   ``build_request`` FIPS planner.** TIGER/Line 2024 publishes each level as a
   ZIP-wrapped multi-file shapefile (.shp + .dbf + .shx + .prj). The
   ``admin_boundaries.build_request`` hook (pure, no I/O) plans the URL(s): one
   whole-US file for state / county / zcta, or a per-state fan-out over the
   bespoke state-FIPS envelope table (+ the antimeridian Aleutian tail) for
   ``place``. The ``zip_vector`` executor owns the I/O: ``get_zip`` each plan,
   ``extractall``, read the ``.shp`` with geopandas, reproject to EPSG:4326,
   bbox spatial-filter (``gdf[gdf.intersects(box)]`` -- whole intersecting features,
   the twin's contract), and for a ``merge_levels`` source (place) concatenate,
   skipping a per-state EMPTY. A nationwide EMPTY / a not-routable place propagate.
   LIVE value-identical over Lee County FL: state 1, place 43 (multi-file fan-out +
   merge), county 5 -- feature counts, geometry types, CRS, and the full TIGER
   column set identical; ocean bbox -> ADMIN_BOUNDARY_EMPTY; ocean place +
   unknown level -> ADMIN_BOUNDARY_LEVEL_INVALID. One consumer re-point:
   region_choice's candidate builder imported the twin's ``_fetch_admin_boundaries_bytes``
   for in-process (no-publish) geometry; re-pointed to a self-contained
   ``_admin_boundaries_fgb_bytes`` local that runs the router executor directly
   (``registration.get_spec`` + ``router.validate_params`` + ``select_executor``) --
   the exact no-cache raw-bytes semantics the twin helper had.

4. **fetch_river_geometry STOP-RULED / DEFERRED WHOLE.** Its PRIMARY path is an
   Overpass QL waterway query (closed-vocab ``waterway_type`` resolution + POST +
   JSON -> LineString), an Overpass-family shape shared with fetch_roads_osm that no
   router mode yet covers; the ZIP capability touches ONLY its NHDPlus HR HUC4
   FileGDB-zip FALLBACK leg, so folding half a fetcher (fallback via zip_vector,
   primary still coded) cannot produce a single byte-identical twin-replacement
   surface -- a bad fit for THIS wave. It is a DO-NOT-REGRESS flood leg
   (``sfincs/flood/flood.py`` imports the twin for river-burning DEM conditioning),
   left entirely untouched; no flood-consumer seam was re-pointed, so no flood
   canary was required this wave (the river_dye offline tests remain the pre-existing
   baseline failures, unrelated). Deferred to the Overpass-family wave (fold with
   fetch_roads_osm / fetch_overpass_pois under one Overpass mode).

5. **Metrics.** Coded tools -2; coded fetchers -2. Spec-served data sources 45 -> 47
   (+2). Registry total unchanged (two twins died, two spec-driven surfaces took
   their names). Twin py + test LOC removed = 546 + 691 (twins) + 419 + 536 (tests);
   value-bearing coverage migrated to ``test_router_zip_multifile.py`` (14 offline
   tests: GHSL grid math + synthetic-tile extract/window/negative-nodata/missing-tile,
   admin FIPS planner + place fan-out + not-routable, zip_vector read/filter/serialize
   + nationwide-empty + place-merge-skips-empty). Docstrings carried VERBATIM
   (``inspect.getdoc`` at fold time) + the sibling corpus.yaml files untouched, so the
   retrieval index is UNSHIFTED: 14/15 corpus phrasings rank the tool in the model-free
   top-8 (the one miss -- an ambiguous "how many people live outside the US" that
   ranks compute_exposure_summary -- is a pre-existing property of the identical
   document text, not a shift; same class as ADR 0065). ``test_catalog_surfacing``
   spec-served count 45 -> 47, arm2/arm3 declarable delta -44 -> -46, stratum tool
   count 44 -> 46 (the expected metric, not a regression). Offline suite FAILED set ==
   9 exactly (test_fetch_resolution_gate x4 + test_run_river_dye_scenario x5; no new
   failure), run in four foreground quarters. Daemon import clean; both spec-served +
   registry-resolvable.

Non-gating divergences flagged (REPORTED, never fudged):
(a) **admin bad-bbox retryable class (router more correct).** The twin's malformed-bbox
    guard raises its BASE ``AdminBoundaryError`` (error_code ADMIN_BOUNDARY_ERROR,
    ``retryable=True`` -- a defect: a degenerate bbox is not retryable). The router's
    ``input_error_suffix: ERROR`` reproduces the ADMIN_BOUNDARY_ERROR code but stamps
    ``retryable=False`` (the honest input-error class). The code is identical; the
    retryable flag is corrected, not copied (standing do-not-copy-a-defect directive).
(b) **ghsl bbox=None vs malformed-bbox suffix collapse.** The router stamps a MISSING
    required bbox and a MALFORMED bbox with the SAME suffix (INPUT_INVALID), where the
    twin split bbox=None -> BBOX_REQUIRED. The parity-tested malformed case matches
    (INPUT_INVALID both); bbox=None is unreachable on the realistic agent surface (bbox
    is required-in-schema). Non-gating.
(c) **admin cache-key omits the constant year; ghsl estimator synthesized.** The twin
    keyed on (level, bbox, year=2024); the router keys on (level, bbox) -- year is a
    constant, so (level, bbox) is already unique (value-identical). The GHSL twin
    carried a bespoke estimator (bbox area x 4 MB/deg^2), reproduced as the bbox_area
    model; admin carried NONE, so the router synthesizes one (no realistic query warns).
    Same class as every prior fold's estimator/cache-key divergence.
(d) **LayerURI cosmetics.** The router synthesizes ``layer_id`` / ``name`` from
    source_class where the twins hand-built labelled strings; the layer DATA (COG /
    FGB) is value-identical, and ``output.emit_bbox`` reproduces admin's bbox-less
    LayerURI + ghsl's request-bbox stamp. Unchanged from every prior fold wave.

Consequence: the ZIP family's honest fold is the WHOLE-OBJECT get_zip step (the
gzip_object precedent at ZIP scale) plus two thin executor extensions -- a raster
``fixed_tile_grid`` tile-mosaic mode and a ``zip_vector`` shapefile-extract executor
-- each proven byte-value-identical on live data, the shared step no-op for all 45
priors. The zip-member range-read transport ADR 0052 deferred is REJECTED-superseded
(the evidence stands; the fold never needed it). river_geometry is honestly STOP-RULED
to the Overpass-family wave (its primary path is not a ZIP shape; its fallback is a
do-not-regress flood leg). Supersedes the ADR 0052 DEFER verdicts for ghsl_population
and administrative_boundaries; extends the transport with one whole-object ZIP step
and the raster/vector executors with the two extract modes.
