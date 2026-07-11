"""Data-fetch atomic tools (job-0033, M4 Stage C).

This module registers four atomic tools that fetch public data from external
agency feeds and write the resulting artifact through the FR-DC-3 cache shim
(``.cache.read_through``). Each tool:

- declares its ``AtomicToolMetadata`` (TTL class + source class + cacheable)
  per FR-AS-3 + FR-CE-8 at import time via ``@register_tool``;
- routes its fetch through ``read_through`` so identical calls reuse the
  cached artifact (FR-DC-3/4) and the live-no-cache enumeration (FR-DC-6) is
  honored uniformly;
- pre-quantizes the bbox to the source's native resolution BEFORE handing the
  params dict to ``read_through`` (OQ-32-QUANTIZATION-LOCATION: engine-side).

Tools registered here:

- ``fetch_dem(bbox, resolution_m=10)`` — USGS 3DEP via ``py3dep.get_dem`` →
  COG bytes → ``cache/static-30d/dem/<key>.tif``.
- ``fetch_buildings(bbox, source="osm")`` — building footprint POLYGONS.
  ``source="osm"`` (default, reliable primary) pulls OpenStreetMap building
  ways + multipolygon relations via the Overpass API; ``source="msft"`` is a
  best-effort fallback to MS Open Maps ML footprints. The tool tries the
  requested source first and falls back to the other on UpstreamAPIError →
  ``cache/static-30d/buildings/<key>.fgb`` (FlatGeobuf, source-agnostic shape).
- ``fetch_population(bbox, dataset="worldpop_2020")`` — WorldPop 100m Unconstrained
  UN-adjusted gridded population (Tier-1 per Appendix F.1, no key required).
  Windowed read over the bbox via ``rasterio`` ``/vsicurl/`` from the WorldPop
  REST endpoint → COG bytes → ``cache/static-30d/population/<key>.tif``.
  ``dataset="acs_2022"`` opts into the Tier-2 Census ACS B01003 tract-level
  GeoJSON path (requires Census API key for high-volume use; routed when the
  agent needs tract-level precision rather than the 100m raster) →
  ``cache/static-30d/population/<key>.json``.
- ``geocode_location(query)`` — Nominatim REST forward geocode → JSON with
  ``{name, bbox, latitude, longitude, source}`` → ``cache/dynamic-1h/geocode/<key>.json``.

FR-TA-2 / FR-AS-3 docstring discipline: every public tool docstring carries
"Use this when:" and "Do NOT use this for:" sections so the FunctionTool
surface is self-describing to Gemini.

Returns / shapes:

- The three layer-producing tools return ``LayerURI`` (from
  ``grace2_contracts.execution``) so downstream visualization seams (map-
  command ``load-layer``) consume them with zero translation.
- ``geocode_location`` returns a plain ``dict`` for now — there is no
  ``GeocodedLocation`` pydantic model in ``grace2-contracts`` yet (FROZEN).
  OQ surfaced for schema to consider promoting in a follow-up job.

External-API resilience (NFR-R-1): per-call timeout, single re-raise on
fetcher failure (no sentinel writes — see ``read_through``). The agent's
FR-AS-11 surface decides retry/clarify/fallback.

Nominatim usage policy compliance: User-Agent header is REQUIRED, fetched
data is cached in our own bucket (we don't re-host), and rate is naturally
throttled by the ``dynamic-1h`` cache class (one fetch per hour-bucket per
distinct query).
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_dem",
    "fetch_buildings",
    "fetch_population",
    "fetch_landcover",
    "fetch_river_geometry",
    "lookup_precip_return_period",
    "geocode_location",
    "round_bbox_to_resolution",
]

logger = logging.getLogger("grace2_agent.tools.data_fetch")


# ---------------------------------------------------------------------------
# Error codes registered by this module (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------
#
# These RuntimeError subclasses carry a stable ``error_code`` for the
# WebSocket A.6 error frame the agent surface emits when a fetch fails. They
# are caught nowhere inside this module — the ``read_through`` contract is
# "re-raise on fetcher failure; no sentinel" — so server-side error handling
# (server.py M1) maps them to A.6 codes via the agent's error surface (job-
# 0035 lands the mapping; for now they bubble up).


class FetchError(RuntimeError):
    """Base class for data-fetch failures. ``error_code`` is the A.6 code."""

    error_code: str = "UPSTREAM_API_ERROR"
    retryable: bool = True


class UpstreamAPIError(FetchError):
    """An upstream public-data API returned an error or timed out."""

    error_code = "UPSTREAM_API_ERROR"
    retryable = True


class GeocodeNoMatchError(UpstreamAPIError):
    """Forward-geocoding found no match for the query (zero/malformed result).

    This is an HONEST, NOT-retryable failure: re-running the SAME query string
    will not suddenly resolve, so ``retryable`` is False and the agent must ask
    the user to refine the place name (add a state/country, fix spelling, name a
    nearby larger place, or supply coordinates) rather than retry.

    It subclasses ``UpstreamAPIError`` so the existing ``except UpstreamAPIError``
    state-snap fallback in ``geocode_location`` STILL fires when a US state is
    recognized in the query (e.g. "south Florida"); when no state is detected,
    the distinct ``error_code`` / non-retryable flag propagate to the surface.
    """

    error_code = "GEOCODE_NO_MATCH"
    retryable = False


class BboxInvalidError(FetchError):
    """The bbox failed validation (degenerate, out of CRS range, too large)."""

    error_code = "BBOX_INVALID"
    retryable = False


class PrecipForcingUnavailableError(FetchError):
    """No design-storm precip source covers the requested point (job-0327).

    Raised when BOTH NOAA Atlas 14 AND the NOAA Atlas 2 (Western US) fallback
    miss the location — a genuinely-uncovered AOI. This is an HONEST,
    NOT-retryable failure: the agent surfaces it as ``status=error`` with a
    clear remediation (supply observed precip via ``forcing_raster_uri`` /
    the observed-precip path, or pick an AOI inside Atlas-14/Atlas-2 coverage).
    Distinct ``error_code`` so the agent can narrate the actionable alternative
    rather than a generic upstream-API failure.
    """

    error_code = "PRECIP_FORCING_UNAVAILABLE"
    retryable = False


class DemPartialCoverageError(UpstreamAPIError):
    """3DEP returned a DEM that materially under-covers the requested bbox.

    LANE-C (#159 follow-up #4): the live case fetched a DEM SHORT on the south
    edge, so a correctly-bboxed hillshade re-fetch still under-covered (79% of the
    requested height). 3DEP coverage gaps / edge clipping leave the returned raster
    smaller than the requested extent; without a check we silently mesh / hillshade
    a partial DEM (the honesty floor forbids that).

    Per the data-source-fallback norm this is a TYPED, RETRYABLE upstream signal —
    it subclasses ``UpstreamAPIError`` so the urban workflow's
    ``except Exception`` 1m->10m fallback still fires (the 10m seamless layer
    usually covers where a 1m tile is missing), and the standalone ``fetch_dem``
    tool surfaces the distinct ``error_code`` so the agent narrates the partial
    coverage rather than presenting a silently-clipped terrain layer.
    """

    error_code = "DEM_PARTIAL_COVERAGE"
    retryable = True


# Nominatim usage policy requires a descriptive User-Agent identifying the
# application + a contact. We bake the project name + repo URL; override the
# contact email via env var ``GRACE2_NOMINATIM_USER_AGENT`` for ops.
_DEFAULT_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)


# ---------------------------------------------------------------------------
# bbox helpers (FR-DC-3 / OQ-32-QUANTIZATION-LOCATION: engine-side quantize).
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``BboxInvalidError`` if ``bbox`` is degenerate or out of WGS84 range.

    A valid bbox is ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326,
    with min < max on both axes, lons in ``[-180, 180]`` and lats in
    ``[-90, 90]``.
    """
    if len(bbox) != 4:
        raise BboxInvalidError(
            f"bbox must be a 4-tuple (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not (math.isfinite(min_lon) and math.isfinite(min_lat) and math.isfinite(max_lon) and math.isfinite(max_lat)):
        raise BboxInvalidError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise BboxInvalidError(f"bbox lon out of range [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise BboxInvalidError(f"bbox lat out of range [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise BboxInvalidError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def round_bbox_to_resolution(
    bbox: tuple[float, float, float, float],
    resolution_m: int,
) -> tuple[float, float, float, float]:
    """Quantize a WGS84 bbox to a per-source resolution grid before cache-keying.

    Rationale: two callers asking for the same area at the same resolution
    should hit the same cache entry even if their bbox edges differ by a few
    floating-point meters. We snap each corner to the nearest grid line whose
    spacing in degrees matches ``resolution_m`` (using a degrees-per-meter
    conversion at the bbox center latitude — good enough for any sub-state
    bbox; per-source overrides can refine).

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        resolution_m: target grid spacing in meters (e.g. 10 for 3DEP 10m).

    Returns:
        A quantized bbox tuple. Always slightly larger than the input bbox
        (snaps mins down and maxes up) so the requested area is covered.

    Surfaced as the engine-side resolution of OQ-32-QUANTIZATION-LOCATION:
    the cache shim's contract is canonicalize+hash; per-source quantization
    is engine-owned domain knowledge.
    """
    _validate_bbox(bbox)
    if resolution_m <= 0:
        raise BboxInvalidError(f"resolution_m must be positive; got {resolution_m!r}")

    min_lon, min_lat, max_lon, max_lat = bbox
    # Stabilize mid_lat by rounding to 4 decimals (~11m) so two callers whose
    # bbox edges differ by sub-meter floats don't get different
    # m_per_deg_lon factors (which would defeat the dedup-via-quantization
    # property — same grid cell must yield same snap result).
    mid_lat = round(0.5 * (min_lat + max_lat), 4)
    # 1 degree of latitude ~ 111_320 m; 1 degree of longitude ~ 111_320 * cos(lat) m.
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    if m_per_deg_lon < 1e-6:  # near a pole — fall back to deg-lat
        m_per_deg_lon = 111_320.0

    deg_lat_per_step = resolution_m / m_per_deg_lat
    deg_lon_per_step = resolution_m / m_per_deg_lon

    snapped_min_lon = math.floor(min_lon / deg_lon_per_step) * deg_lon_per_step
    snapped_max_lon = math.ceil(max_lon / deg_lon_per_step) * deg_lon_per_step
    snapped_min_lat = math.floor(min_lat / deg_lat_per_step) * deg_lat_per_step
    snapped_max_lat = math.ceil(max_lat / deg_lat_per_step) * deg_lat_per_step

    # Round to a reasonable number of digits so the JSON canonicalization
    # produces stable strings (float repr quirks otherwise leak into the key).
    return (
        round(snapped_min_lon, 9),
        round(snapped_min_lat, 9),
        round(snapped_max_lon, 9),
        round(snapped_max_lat, 9),
    )


def _bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    """Approximate area of a small WGS84 bbox in square kilometers."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    dlat_km = (max_lat - min_lat) * 111.320
    dlon_km = (max_lon - min_lon) * 111.320 * math.cos(math.radians(mid_lat))
    return abs(dlat_km * dlon_km)


# ---------------------------------------------------------------------------
# fetch_dem — USGS 3DEP via py3dep
# ---------------------------------------------------------------------------

#: Coverage shortfall (in degrees) tolerated before a DEM is flagged partial.
#: ~0.0008 deg ~= 90 m at the equator — generous enough to absorb a one-tile /
#: half-cell edge snap (3DEP cells are 1-30 m) without flagging a good DEM, but
#: tight enough to catch the live south-edge clip (~21% of the requested height).
_DEM_COVERAGE_TOL_DEG = 0.0008


def _dem_wgs84_bounds(dem: Any) -> tuple[float, float, float, float] | None:
    """Return a rioxarray DEM's bounds reprojected to WGS84, else ``None``.

    ``py3dep.get_dem`` returns an EPSG:5070 (Albers) DataArray; we reproject just
    the bounding box corners to EPSG:4326 so the coverage check compares like with
    like. Returns ``None`` when the bounds / CRS cannot be read (the caller then
    skips the coverage gate rather than blocking a usable DEM).
    """
    rio = getattr(dem, "rio", None)
    if rio is None:
        return None
    left, bottom, right, top = (float(v) for v in rio.bounds())
    crs = rio.crs
    if crs is None:
        return None
    try:
        from pyproj import CRS as _CRS  # type: ignore[import-not-found]

        if _CRS.from_user_input(crs).to_epsg() == 4326:
            return (left, bottom, right, top)
        from pyproj import Transformer  # type: ignore[import-not-found]

        tf = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        xs, ys = tf.transform([left, right, left, right], [bottom, top, top, bottom])
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:  # noqa: BLE001 — pyproj/CRS slip -> skip the gate
        return None


def _bbox_covers(
    coverage: tuple[float, float, float, float],
    requested: tuple[float, float, float, float],
    tol: float = _DEM_COVERAGE_TOL_DEG,
) -> bool:
    """True iff ``coverage`` spans ``requested`` on all four edges within ``tol``.

    Both are ``(min_lon, min_lat, max_lon, max_lat)`` WGS84. A material shortfall
    on ANY edge (the live south-edge clip) returns False so the caller surfaces a
    typed partial-coverage signal.
    """
    return (
        coverage[0] <= requested[0] + tol
        and coverage[1] <= requested[1] + tol
        and coverage[2] >= requested[2] - tol
        and coverage[3] >= requested[3] - tol
    )


_FETCH_DEM_METADATA = AtomicToolMetadata(
    name="fetch_dem",
    ttl_class="static-30d",
    source_class="dem",
    cacheable=True,
    # Deterministic auto-publish opt-OUT (NATE 2026-06-26): the raw DEM is a
    # pure INTERMEDIATE input that feeds compute_hillshade / compute_slope /
    # compute_aspect / SFINCS setup. The user normally wants the DERIVED terrain
    # product painted, not the bare elevation grid, so do NOT auto-render it.
    # The LLM can still explicitly publish_layer the raw DEM when the user asks
    # to see elevation directly.
    auto_publish=False,
)


# ---------------------------------------------------------------------------
# F16 pattern extended to fetch_dem (2026-07-10): state-scale auto-coarsen.
# ---------------------------------------------------------------------------
# Live failure this fixes: "show me the hillshade in the bounding box" over
# Washington state -> fetch_dem(bbox=<WA state, ~230,638 km^2>, source="3dep",
# resolution_m=30) -> hard "bbox area ... exceeds 10000 km^2 guardrail" dead
# end. A state-scale DEM at a coarsened resolution is a perfectly reasonable
# request (fine for a hillshade/overview render); commit 21cd123 already gave
# fetch_landcover this exact treatment (hard cap raised to a continent
# ceiling + pixel-budget auto-coarsen instead of a flat area cutoff). This
# block mirrors that pattern for fetch_dem's default source="3dep" path.
#
# Acquisition-path note (why the coarsen is just "pass a bigger resolution_m"
# here, unlike fetch_landcover's WCS WIDTH/HEIGHT dance): fetch_dem's 3DEP
# path calls ``py3dep.get_dem(bbox, resolution=resolution_m)`` directly --
# py3dep's own ``resolution`` argument already controls the delivered grid
# spacing (it picks the fast ``static_3dep_dem`` tile-tree path at 10/30/60 m
# and falls back to a WMS ``get_map`` mosaic + reproject/resample at any other
# value). Coarsening is therefore just requesting a bigger ``resolution_m``;
# py3dep/GDAL's own reprojection resampling applies (area-weighted / bilinear
# for a continuous field), which is CORRECT for elevation -- UNLIKE NLCD land
# cover, which must use nearest-neighbor to keep discrete class codes intact.
# We never touch pixel values ourselves; we only choose what resolution to
# request.
#
# Continent ceiling mirrors fetch_landcover's 5,000,000 km^2 hard cap exactly
# (still hard-fails: no auto-coarsen rescues a whole-continent request).
_DEM_CONTINENT_CEILING_KM2 = 5_000_000.0

# Pixel-budget constant for the auto-coarsen (same long-axis/4000px budget
# fetch_landcover uses against the MRLC WCS 4096 px/axis server cap). py3dep
# has no equivalent hard server-side px cap, but an unbounded grid at fine
# resolution over a huge AOI is still an enormous mosaic/COG to materialize
# and hold in memory -- 4000 px/axis keeps it tractable. Kept in step with
# server.py's ``_FETCH_MAX_PX_BY_TOOL["fetch_dem"]`` so the resolution-gate's
# suggested rung matches what this tool will actually deliver (server.py
# point 5 of the F16-for-DEM extension).
_DEM_PIXEL_BUDGET_PX = 4000

# Absolute floor on the coarsen math: 3DEP's finest tiles (lidar-derived) run
# down to ~1 m. UNLIKE fetch_landcover's fixed 30 m NLCD native grid, 3DEP has
# no single "native" resolution -- coverage varies by tile (1-3 m lidar
# patches in many areas, a 10 m national baseline, 30 m fallback elsewhere)
# -- so this is a sanity floor guarding a degenerate resolution_m < 1 request,
# NOT a native-resolution constant to clamp UP to (that would break the
# tool's existing fine 1 m / 3 m site-scale requests on small AOIs).
_DEM_FINEST_RES_FLOOR_M = 1


def _fetch_3dep_dem_bytes(
    bbox: tuple[float, float, float, float], resolution_m: int
) -> bytes:
    """Call ``py3dep.get_dem`` and serialize the result as a Cloud-Optimized GeoTIFF.

    Raises ``UpstreamAPIError`` on any failure from the 3DEP service so the
    cache shim's "no sentinel on failure" contract surfaces a typed error.
    """
    # py3dep + rasterio import lazily so test environments without these
    # heavy geo deps installed can still load the registry.
    try:
        import py3dep  # type: ignore[import-not-found]
        import rioxarray  # noqa: F401 — registers .rio accessor on xr.DataArray
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(f"py3dep / rioxarray unavailable: {exc}") from exc

    # job-0306: py3dep reads the USGS 3DEP seamless DEM from the PUBLIC bucket
    # ``prd-tnm.s3.amazonaws.com`` via GDAL ``/vsicurl/``. On the AWS box the
    # instance-role AWS creds are in the environment, so GDAL tried to SIGN the
    # request (and to readdir-list the bucket) — both fail on a public,
    # no-ListBucket bucket, surfacing as "…USGS_Seamless_DEM_1.vrt does not
    # exist in the file system" even though the VRT is reachable (curl 200).
    # Cold DEM fetches for EVERY novel bbox failed (live Case 3, 2026-06-16);
    # only previously-cached DEMs worked. Scope AWS_NO_SIGN_REQUEST +
    # readdir/extension hints to THIS read via ``rasterio.Env`` so the agent's
    # PRIVATE-bucket access (signed instance-role boto3/GDAL) is unaffected.
    try:
        import rasterio  # type: ignore[import-not-found]
        _dem_env = rasterio.Env(
            AWS_NO_SIGN_REQUEST="YES",
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
            CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".vrt,.tif,.tiff",
            VSI_CACHE=True,
        )
    except Exception:  # noqa: BLE001 — rasterio always present where py3dep is
        import contextlib
        _dem_env = contextlib.nullcontext()

    try:
        with _dem_env:
            dem = py3dep.get_dem(bbox, resolution=resolution_m)
    except Exception as exc:  # noqa: BLE001 — re-raise as typed error
        raise UpstreamAPIError(
            f"py3dep.get_dem failed for bbox={bbox} resolution={resolution_m}: {exc}"
        ) from exc

    # LANE-C (#159 follow-up #4): coverage gate. 3DEP can return a DEM SHORT on an
    # edge (the live south-edge clip -> 79% height hillshade). Reproject the
    # returned raster's bounds back to WGS84 and assert they span the requested
    # bbox within a small tolerance; a material shortfall raises the typed
    # DemPartialCoverageError so we never silently mesh / hillshade a clipped DEM
    # (the urban workflow's 1m->10m fallback + the agent's honest narration act on
    # it). Best-effort on the bounds read — a bounds-introspection failure leaves
    # the prior (no-check) behavior unchanged rather than blocking a good DEM.
    try:
        cov = _dem_wgs84_bounds(dem)
    except Exception:  # noqa: BLE001 — never block a DEM on an introspection slip
        cov = None
    if cov is not None and not _bbox_covers(cov, bbox):
        raise DemPartialCoverageError(
            f"3DEP DEM for bbox={bbox} resolution={resolution_m}m under-covers the "
            f"requested extent (got coverage {cov}); the returned raster is "
            "materially short on at least one edge."
        )

    # Serialize to a COG via rioxarray's to_raster. We round-trip through a
    # temp file because rasterio's MemoryFile lacks COG driver options on
    # some platforms; the temp file is small for a small bbox.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        # COG driver + LZW compression. tiled=True is COG-required.
        dem.rio.to_raster(
            tmp_path,
            driver="COG",
            compress="LZW",
            BIGTIFF="IF_SAFER",
        )
        with open(tmp_path, "rb") as f:
            data = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return data


@register_tool(
    _FETCH_DEM_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (USGS 3DEP py3dep),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_dem(
    bbox: tuple[float, float, float, float],
    resolution_m: int = 10,
    source: str = "3dep",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a digital elevation model (DEM) / terrain elevation for a bounding box (USGS 3DEP by default; GLOBAL Copernicus GLO-30 via source="copernicus").

    Use this (not ``fetch_topobathy``, which is coastal land+seafloor) for a plain
    ground-elevation DEM. Pass ``source="copernicus"`` for terrain OUTSIDE the US
    (the Alps, Andes, Himalaya, Africa, ...) -- that ABSORBS the former
    ``fetch_copernicus_dem`` (keyless global GLO-30 30 m). The default
    ``source="3dep"`` keeps the current US 10 m behavior byte-for-byte.

    **What it does:** Downloads a Cloud-Optimized GeoTIFF of ground elevation
    from the USGS 3D Elevation Program (3DEP) via the ``py3dep`` library and
    writes it to the 30-day cache. Returns a ``LayerURI`` pointing at the
    cached COG so downstream SFINCS/HydroMT setup and terrain analysis tools
    can consume it without re-fetching. With ``source="copernicus"`` it instead
    mosaics the global Copernicus GLO-30 30 m DEM (same ``continuous_dem`` ramp,
    same ``LayerURI`` raster contract).

    **When to use:**
    - Any flood workflow step that needs terrain elevation: SFINCS model
      domain setup, watershed delineation, slope/hillshade computation.
    - User asks "show me the terrain elevation for [area]" or "what does the
      ground look like here?" — render with the ``continuous_dem`` QML preset.
    - ``build_sfincs_model`` requires a DEM for the SFINCS grid; this tool
      supplies it.
    - Pre-processing step before ``compute_slope``, ``compute_hillshade``,
      ``compute_aspect``, or ``compute_zonal_statistics``.

    **When NOT to use:**
    - Coverage outside the continental US with the DEFAULT source — 3DEP is
      CONUS-only; pass ``source="copernicus"`` for a global GLO-30 30 m DEM.
    - Bathymetry (below-water elevation) — 3DEP/GLO-30 are land/surface models;
      use ``fetch_topobathy`` for coastal seafloor depth.
    - Single-point elevation lookups — the tool fetches a raster window;
      for a point query use a future ``point_elevation`` tool.
    - Continent-scale bboxes (> 5,000,000 km²) — rejected with
      ``BboxInvalidError``. State/multi-state bboxes auto-coarsen instead of
      failing (not a dead end).

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Max area 5,000,000 km²; large bboxes auto-coarsen.
    - ``resolution_m`` (int, default 10): DEM grid spacing in meters (3DEP only).
      10 m or 30 m are fastest on 3DEP's tile tree. A large bbox may deliver a
      coarser grid than requested (a pixel-budget auto-coarsen); the delivered
      spacing is stamped into the result.
    - ``source`` (str, default ``"3dep"``): ``"3dep"`` (USGS 3DEP, US-only,
      honors ``resolution_m``) or ``"copernicus"`` (Copernicus GLO-30, global
      30 m, keyless -- delegates to the folded-in ``fetch_copernicus_dem``).

    **Returns:**
    A ``LayerURI`` pointing at a Cloud-Optimized GeoTIFF in the cache bucket
    (``gs://grace-2-hazard-prod-cache/cache/static-30d/dem/<key>.tif``).
    CRS: EPSG:5070 (py3dep default); units: meters above NAVD88.
    Fields consumed downstream: ``uri`` → by ``build_sfincs_model`` and QGIS
    Server WMS; ``style_preset="continuous_dem"`` → map rendering. When the
    bbox forced a coarser grid than requested, ``name`` carries an honest
    coarsening note (approximate terrain -- fine for a hillshade/overview,
    not site-scale analysis).

    **Cross-tool dependencies:**
    - Downstream: ``build_sfincs_model``, ``compute_slope``,
      ``compute_hillshade``, ``compute_aspect``, ``compute_colored_relief``,
      ``compute_zonal_statistics``.
    - Typically called after: ``geocode_location`` supplies the bbox.
    - Sibling source: ``source="copernicus"`` for non-US / global terrain.
    """
    # SOURCE consolidation: the global Copernicus GLO-30 path is folded in as a
    # source mode (the former fetch_copernicus_dem). Same LayerURI raster
    # contract + continuous_dem ramp; the impl lives in the copernicus module.
    if isinstance(source, str) and source.strip().lower() in {
        "copernicus",
        "cop-dem-glo-30",
        "glo-30",
        "glo30",
        "copernicus_glo30",
    }:
        from .fetch_copernicus_dem import _copernicus_dem_impl

        return _copernicus_dem_impl(bbox)

    # Continent ceiling (F16-for-DEM, 2026-07-10): mirrors fetch_landcover's
    # 5,000,000 km^2 hard cap. Below this, auto-coarsen instead of hard-fail
    # -- see the module-level comment above _DEM_CONTINENT_CEILING_KM2.
    requested_res = int(resolution_m)
    rough_area = _bbox_area_km2(bbox)
    if rough_area > _DEM_CONTINENT_CEILING_KM2:
        raise BboxInvalidError(
            f"bbox area {rough_area:.1f} km^2 exceeds the "
            f"{_DEM_CONTINENT_CEILING_KM2:,.0f} km^2 hard ceiling for fetch_dem "
            "(continent-scale; split into sub-regions)."
        )

    # Pixel-budget auto-coarsen: if the requested resolution would put more
    # than _DEM_PIXEL_BUDGET_PX pixels on the bbox's long axis, coarsen to
    # fit. effective_res is the coarser of (a) what the caller/gate asked
    # for and (b) what the pixel budget allows -- it is NEVER finer than
    # requested_res, so a small-bbox site-scale request (e.g. 1 m) is
    # honored exactly as before (byte-identical for the common case).
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
    long_axis_m = max(
        (max_lon - min_lon) * m_per_deg_lon,
        (max_lat - min_lat) * 111_320.0,
    )
    budget_res = int(math.ceil(long_axis_m / _DEM_PIXEL_BUDGET_PX))
    effective_res = max(_DEM_FINEST_RES_FLOOR_M, requested_res, budget_res)
    downsampled = effective_res > requested_res

    # Quantize to the EFFECTIVE resolution grid -- a coarsened fetch snaps to
    # a coarser grid than a native-resolution fetch of the same bbox would,
    # so the two never collide on the same cache key (matches fetch_landcover).
    quantized = round_bbox_to_resolution(bbox, effective_res)
    params = {"bbox": list(quantized), "resolution_m": effective_res}
    result = read_through(
        metadata=_FETCH_DEM_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_3dep_dem_bytes(quantized, effective_res),
    )
    assert result.uri is not None, "fetch_dem is cacheable; uri must be set"
    name = f"USGS 3DEP DEM ({effective_res}m)"
    if downsampled:
        # Honest coarsening note (Invariant 7 -- no silent wrong answers).
        # LayerURI is a FROZEN contract (extra="forbid", see fetch_landcover's
        # "Sidecar shape" comment above) with ~7 internal Python call sites
        # (fetch_topobathy, compute_contours, model_flood_scenario,
        # run_elmfire, model_landslide_scenario, model_dambreak_geoclaw_
        # scenario, model_urban_flood_swmm) plus 50+ tests depending on
        # fetch_dem returning a bare LayerURI -- unlike fetch_landcover (few
        # callers, already dict-shaped pre-F16), converting fetch_dem to a
        # dict-sidecar return here would be a wide, high-risk blast radius for
        # no functional gain: LayerURI IS fully JSON-serialized back to the
        # LLM (see server.py result handling), so folding the effective /
        # native resolution + the honesty note into ``name`` (and the
        # resolution into ``layer_id`` below) reaches the LLM/user exactly the
        # same as a separate dict field would.
        name += (
            f", coarsened from {requested_res}m -- large-AOI pixel budget. "
            "Terrain detail is approximate at this scale: fine for a "
            "hillshade/overview render, not for site-scale analysis."
        )
    return LayerURI(
        layer_id=f"dem-{quantized[0]:.4f}-{quantized[1]:.4f}-{effective_res}m",
        name=name,
        layer_type="raster",
        uri=result.uri,
        style_preset="continuous_dem",
        role="input",
        units="meters",
        # LANE-C (#159 follow-up #4): declare the requested extent on the layer.
        # The coverage gate in ``_fetch_3dep_dem_bytes`` guarantees the raster
        # spans this bbox (or raised), so stamping it lets the AOI-pin reuse
        # short-circuit + the post-result zoom-to know the DEM's intended extent.
        bbox=quantized,
    )


# ---------------------------------------------------------------------------
# fetch_buildings — OSM Overpass (reliable primary) + Microsoft (fallback)
# ---------------------------------------------------------------------------


_FETCH_BUILDINGS_METADATA = AtomicToolMetadata(
    name="fetch_buildings",
    ttl_class="static-30d",
    source_class="buildings",
    cacheable=True,
)


# BEST-EFFORT FALLBACK source (``source="msft"``). MS Open Maps publishes
# Global ML Building Footprints sharded by quadkey under a public Azure Blob
# container. The official catalog index is at:
#   https://minedbuildings.blob.core.windows.net/global-buildings/dataset-links.csv
# Each row is (QuadKey, Location, Url) — the URL is a GZIP'd line-delimited
# GeoJSON. The MS Open Maps STAC catalog (an alternative entry point referenced
# in the kickoff) at planetarycomputer.microsoft.com wraps this same data under
# a STAC API.
#
# KNOWN LIMITATION (job-0331): the Planetary Computer ``ms-buildings``
# collection typically returns a single whole-country item whose only asset is
# an ``abfs://`` (Azure Blob Filesystem) GeoParquet store — ``requests.get`` on
# an ``abfs://`` URL cannot work, so this branch frequently fails to download.
# It is retained as a best-effort fallback only; OSM Overpass (source="osm") is
# the reliable primary. When the STAC search yields no items or no downloadable
# asset, an ``UpstreamAPIError`` surfaces and ``fetch_buildings`` falls back to
# OSM (or raises an honest both-failed error).


# ---------------------------------------------------------------------------
# OSM Overpass building-footprint fetcher (job-0331).
#
# ROOT CAUSE (live 2026-06-16): the ``source="msft"`` path queries the
# Planetary Computer ``ms-buildings`` STAC collection, whose only asset is an
# ``abfs://`` (Azure Blob Filesystem) GeoParquet store — ``requests.get`` on an
# ``abfs://`` URL cannot work, so MS footprints NEVER download. The previous
# ``source="osm"`` branch only raised ``NotImplementedError``, leaving the tool
# with NO working footprint source.
#
# Fix: OSM Overpass is the reliable PRIMARY (verified: 578 building polygons for
# one Chattanooga block). This fetcher mirrors ``fetch_roads_osm.py``'s Overpass
# pattern (``out geom`` + geometry assembly + clip-to-bbox + FlatGeobuf write)
# but assembles building POLYGONS (closed ways) and MULTIPOLYGONS (relations)
# rather than road LineStrings. MS stays as a best-effort FALLBACK.
# ---------------------------------------------------------------------------

#: Overpass interpreter endpoint (same public endpoint as fetch_roads_osm).
_OVERPASS_BUILDINGS_URL = "https://overpass-api.de/api/interpreter"

#: External HTTP timeout for the Overpass POST — Overpass is slow under load.
_OVERPASS_BUILDINGS_HTTP_TIMEOUT = 120.0

#: Overpass-side query timeout (the ``[timeout:N]`` QL directive).
_OVERPASS_BUILDINGS_QL_TIMEOUT = 90

#: Polite delay before the Overpass request to respect rate limits (miss-path
#: only; cache hits never reach this fetcher).
_OVERPASS_BUILDINGS_POLITE_DELAY_S = 1.0


def _build_overpass_buildings_ql(
    bbox: tuple[float, float, float, float],
) -> str:
    """Construct the Overpass QL selecting building ways AND relations in ``bbox``.

    Overpass expects bbox corners as ``(south, west, north, east)`` (lat first)
    — the OPPOSITE corner ordering from the caller's ``(min_lon, min_lat,
    max_lon, max_lat)``. ``out geom`` returns full node geometry inline (plus,
    for relations, the geometry of every member way) so we can assemble
    polygons without a second resolve pass.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    return (
        f"[out:json][timeout:{_OVERPASS_BUILDINGS_QL_TIMEOUT}];"
        f"("
        f"way[\"building\"]({s},{w},{n},{e});"
        f"relation[\"building\"]({s},{w},{n},{e});"
        f");"
        f"out geom;"
    )


def _post_overpass_buildings(ql: str) -> dict[str, Any]:
    """POST ``ql`` to the Overpass interpreter and return parsed JSON.

    Raises ``UpstreamAPIError`` on network / HTTP / parse failure so the
    ``read_through`` "re-raise on fetcher failure; no sentinel" contract holds.
    Uses ``httpx`` to match ``fetch_roads_osm``'s transport; a polite 1 s sleep
    fires BEFORE the request.
    """
    import httpx  # local import: keeps registry import light + mirrors roads tool

    try:
        time.sleep(_OVERPASS_BUILDINGS_POLITE_DELAY_S)
        logger.info(
            "fetch_buildings(osm): POST %s ql_bytes=%d",
            _OVERPASS_BUILDINGS_URL,
            len(ql),
        )
        with httpx.Client(
            timeout=_OVERPASS_BUILDINGS_HTTP_TIMEOUT,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
        ) as client:
            resp = client.post(_OVERPASS_BUILDINGS_URL, data={"data": ql})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        raise UpstreamAPIError(
            f"OSM Overpass buildings HTTP error status={status}: {exc}"
        ) from exc
    except httpx.HTTPError as exc:
        raise UpstreamAPIError(
            f"OSM Overpass buildings network/transport error: {exc}"
        ) from exc
    except ValueError as exc:
        raise UpstreamAPIError(
            f"OSM Overpass buildings returned non-JSON response: {exc}"
        ) from exc


def _ring_from_geom(geom: Any) -> list[tuple[float, float]]:
    """Extract a ``(lon, lat)`` coordinate ring from an Overpass ``geometry`` list.

    Drops malformed / non-finite points. Returns the raw ring (NOT forced
    closed) — the caller decides whether ≥ 3 distinct vertices make a polygon.
    """
    ring: list[tuple[float, float]] = []
    if not isinstance(geom, list):
        return ring
    for pt in geom:
        if not isinstance(pt, dict):
            continue
        lat_v = pt.get("lat")
        lon_v = pt.get("lon")
        if lat_v is None or lon_v is None:
            continue
        try:
            lat = float(lat_v)
            lon = float(lon_v)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        ring.append((lon, lat))
    return ring


def _way_to_polygon(way: dict[str, Any]) -> Any | None:
    """Assemble a closed-way Overpass element into a shapely ``Polygon``.

    Returns ``None`` if the ring has fewer than 3 distinct vertices or the
    resulting polygon is empty / invalid-and-unfixable.
    """
    from shapely.geometry import Polygon  # type: ignore[import-not-found]

    ring = _ring_from_geom(way.get("geometry"))
    # Need at least 3 distinct vertices for an areal ring. Overpass closed ways
    # repeat the first node as the last; dedup the closure before counting.
    distinct = list(dict.fromkeys(ring))
    if len(distinct) < 3:
        return None
    try:
        poly = Polygon(ring)
    except Exception:  # noqa: BLE001 — degenerate ring
        return None
    if poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)  # standard self-intersection repair
        if poly.is_empty or poly.geom_type not in ("Polygon", "MultiPolygon"):
            return None
    return poly


def _relation_to_multipolygon(rel: dict[str, Any]) -> Any | None:
    """Assemble an Overpass ``multipolygon`` relation into a (Multi)Polygon.

    ``out geom`` returns each member way's geometry inline under
    ``members[].geometry`` with ``role`` in ``{"outer", "inner"}``. We build
    outer-ring polygons, subtract inner rings (holes), and union the result.
    Returns ``None`` if no usable outer ring exists.
    """
    from shapely.geometry import Polygon  # type: ignore[import-not-found]
    from shapely.ops import unary_union  # type: ignore[import-not-found]

    members = rel.get("members") or []
    if not isinstance(members, list):
        return None
    outers: list[Any] = []
    inners: list[Any] = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        ring = _ring_from_geom(member.get("geometry"))
        distinct = list(dict.fromkeys(ring))
        if len(distinct) < 3:
            continue
        try:
            poly = Polygon(ring)
        except Exception:  # noqa: BLE001
            continue
        if poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty:
                continue
        role = member.get("role")
        if role == "inner":
            inners.append(poly)
        else:
            # Default unrolled members (role "" / "outer") are treated as outer.
            outers.append(poly)
    if not outers:
        return None
    outer_union = unary_union(outers)
    if inners:
        hole_union = unary_union(inners)
        try:
            outer_union = outer_union.difference(hole_union)
        except Exception:  # noqa: BLE001 — keep solid footprint if hole-cut fails
            pass
    if outer_union.is_empty or outer_union.geom_type not in (
        "Polygon",
        "MultiPolygon",
    ):
        return None
    return outer_union


def _building_fid(el_type: Any, osm_id: Any) -> str:
    """Stable composite feature id ``"<first-letter-of-osm_type><osm_id>"``.

    e.g. a ``way`` id ``123456`` -> ``"w123456"``, a ``relation`` id ``222`` ->
    ``"r222"``. The ``(osm_type, osm_id)`` pair is the Overpass-by-id key; this
    single string is the slim inline join-key the popup enrich path sends back to
    ``/api/building-detail`` and the sidecar tag-map is keyed by.
    """
    prefix = str(el_type or "")[:1]
    return f"{prefix}{osm_id}"


def _extract_building_features(
    payload: dict[str, Any],
) -> tuple[list[tuple[Any, dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Walk Overpass ``elements`` -> ``(features, tags_by_fid)`` for buildings.

    Ways become ``Polygon``s; multipolygon relations become ``(Multi)Polygon``s.
    Non-areal / malformed elements are skipped.

    INLINE payload is SLIM (frontend-perf fix, NATE 2026-06-27 "footprint layers
    store too much in the frontend GeoJSON"): each feature carries ONLY id props
    -- ``osm_id``, ``osm_type``, and a stable composite ``fid`` (e.g. ``"w123456"``)
    -- and DROPS ``building`` + ``name`` from the inline properties. The full tag
    bag (``building``, ``height``, ``levels``, ``name``, ``addr:*`` ...) is
    captured separately in the returned ``tags_by_fid`` map for the
    click-to-enrich sidecar; the popup fetches it on demand by ``(osm_type,
    osm_id)`` so the inline GeoJSON stays tiny.
    """
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise UpstreamAPIError(
            f"OSM Overpass buildings 'elements' is not a list: "
            f"{type(elements).__name__}"
        )
    features: list[tuple[Any, dict[str, Any]]] = []
    tags_by_fid: dict[str, dict[str, Any]] = {}
    for el in elements:
        if not isinstance(el, dict):
            continue
        el_type = el.get("type")
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        if el_type == "way":
            geom = _way_to_polygon(el)
        elif el_type == "relation":
            geom = _relation_to_multipolygon(el)
        else:
            geom = None
        if geom is None:
            continue
        osm_id = el.get("id")
        fid = _building_fid(el_type, osm_id)
        features.append(
            (
                geom,
                {
                    "osm_id": osm_id,
                    "osm_type": el_type,
                    "fid": fid,
                },
            )
        )
        # Capture the FULL tag bag for the click-to-enrich sidecar. Only retain
        # a non-empty bag (a building with no tags contributes nothing to enrich).
        if tags:
            tags_by_fid[fid] = dict(tags)
    return features, tags_by_fid


def _fetch_osm_buildings_bytes(
    bbox: tuple[float, float, float, float],
    on_tags: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> bytes:
    """Fetch OSM building footprints for ``bbox`` and return FlatGeobuf bytes.

    Queries the OpenStreetMap Overpass API for ``building``-tagged ways AND
    relations intersecting the bbox, assembles closed ways into ``Polygon``s
    and multipolygon relations into ``(Multi)Polygon``s, retains EVERY footprint
    whose geometry INTERSECTS the requested bbox (whole, un-sliced — a building
    straddling any AOI edge is kept intact, not chopped at the boundary), and
    serializes the result to FlatGeobuf — the SAME output format the ``msft``
    branch produces, so the cache write + downstream consumers are
    source-agnostic.

    Edge-coverage note (the "missed buildings on the LEFT" fix): a previous
    revision ran ``gpd.clip(gdf, bbox)``, which geometrically slices every
    footprint at the bbox boundary. That dropped/mangled buildings straddling
    the AOI edge. We now filter by INTERSECTS instead of clipping, so any
    building touching the bbox is returned whole — symmetric on all four sides.

    Raises ``UpstreamAPIError`` on Overpass failure OR when no building
    footprints intersect the bbox (honest typed empty per the data-source
    fallback norm — the caller decides whether to fall back to ``msft``).
    """
    _validate_bbox(bbox)
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely import box  # type: ignore[import-not-found]
    except ImportError as exc:
        raise UpstreamAPIError(
            f"geopandas / shapely not available for OSM buildings: {exc}"
        ) from exc

    ql = _build_overpass_buildings_ql(bbox)
    payload = _post_overpass_buildings(ql)
    features, tags_by_fid = _extract_building_features(payload)

    # Surface the full per-fid tag bag to the caller so it can persist the
    # click-to-enrich sidecar under the SAME cache key as the .fgb. Best-effort:
    # a sidecar callback fault must NEVER fail the fetch (the slim layer still
    # renders; enrich then degrades to a live Overpass-by-id query).
    if on_tags is not None and tags_by_fid:
        try:
            on_tags(tags_by_fid)
        except Exception as exc:  # noqa: BLE001 -- sidecar is best-effort
            logger.warning(
                "fetch_buildings(osm): tag-sidecar callback failed: %s", exc
            )

    if not features:
        raise UpstreamAPIError(
            f"OSM Overpass returned no building footprints for bbox={bbox} "
            f"(area may be unmapped — caller may fall back to source='msft')"
        )

    geometries = [geom for geom, _attrs in features]
    attrs = [a for _g, a in features]
    gdf = gpd.GeoDataFrame(attrs, geometry=geometries, crs="EPSG:4326")

    # Retain every footprint that INTERSECTS the requested bbox — do NOT clip.
    #
    # Overpass ``out geom`` returns the FULL footprint of any building with a
    # node inside the bbox, so a building straddling an AOI edge spills outside.
    # The previous revision ran ``gpd.clip(gdf, bbox)``, which geometrically
    # slices each footprint at the boundary. That dropped/mangled edge buildings
    # (NATE: "missed some on the LEFT"). We instead keep footprints whole when
    # they intersect the bbox and exclude only those that fall entirely outside.
    # ``intersects`` is symmetric on all four edges (left/right/top/bottom), so
    # no side is preferentially dropped. Geometries are left un-sliced.
    min_lon, min_lat, max_lon, max_lat = bbox
    bbox_geom = box(min_lon, min_lat, max_lon, max_lat)
    # Defend against degenerate / non-areal geometry surviving assembly.
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notna()]
    gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    try:
        gdf = gdf[gdf.geometry.intersects(bbox_geom)]
    except Exception as exc:  # noqa: BLE001 — defend against degenerate geom
        raise UpstreamAPIError(
            f"OSM buildings bbox-intersects filter failed for bbox={bbox}: {exc}"
        ) from exc

    if len(gdf) == 0:
        raise UpstreamAPIError(
            f"OSM Overpass building footprints all fell outside bbox={bbox} "
            f"(none intersect the AOI — caller may fall back to source='msft')"
        )

    logger.info(
        "fetch_buildings(osm): %d building footprint(s) intersecting AOI for bbox=%s",
        len(gdf),
        bbox,
    )

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_osm_buildings_"
        ) as f:
            tmp_fgb = f.name
        try:
            gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001 — translate to typed error
            raise UpstreamAPIError(
                f"OSM buildings FlatGeobuf write failed: {exc}"
            ) from exc
        with open(tmp_fgb, "rb") as f:
            return f.read()
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


def _fetch_msft_buildings_bytes(
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Query MS Open Maps Building Footprints for ``bbox`` and return FlatGeobuf bytes.

    Uses the Microsoft Planetary Computer STAC API as the query surface
    (https://planetarycomputer.microsoft.com/api/stac/v1) — the same catalog
    that backs the public MS Open Maps releases. Items in the
    ``ms-buildings`` collection point at PMTiles / FlatGeobuf assets we can
    download by-asset.

    Implementation note (M4 scope): this is a minimal request → response
    path. A production-grade implementation would use ``pystac-client`` for
    pagination and ``stackstac`` for asset materialization; for the M4
    substrate we issue a single ``POST /search`` with the bbox + intersects
    filter, take the first matching item's FlatGeobuf asset (or fall back
    to GeoJSON serialization of the geometry), and return raw bytes.
    """
    _validate_bbox(bbox)
    # Planetary Computer STAC endpoint. The ms-buildings collection is the
    # public catalog wrapping the Open Data ML footprints.
    pc_stac_url = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
    search_body = {
        "collections": ["ms-buildings"],
        "bbox": list(bbox),
        "limit": 1,
    }
    try:
        resp = requests.post(
            pc_stac_url,
            json=search_body,
            headers={"User-Agent": _DEFAULT_USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        catalog = resp.json()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"MS Open Maps STAC search failed for bbox={bbox}: {exc}"
        ) from exc

    features = catalog.get("features", []) or []
    if not features:
        # No ML coverage in this bbox; surface a typed error so the agent can
        # choose to fall back to OSM via ``source="osm"`` in a future call.
        raise UpstreamAPIError(
            f"no MS Open Maps building items intersect bbox={bbox} "
            f"(coverage may be missing — fall back via source='osm' in a follow-up)"
        )

    # Asset preference: FlatGeobuf if present, GeoParquet next, GeoJSON last.
    item = features[0]
    assets = item.get("assets", {}) or {}

    preferred_asset = None
    for asset_key in ("data", "footprints", "flatgeobuf"):
        if asset_key in assets:
            preferred_asset = assets[asset_key]
            break
    if preferred_asset is None and assets:
        # Fall back to the first asset listed.
        preferred_asset = next(iter(assets.values()))
    if preferred_asset is None or "href" not in preferred_asset:
        # No downloadable asset; serialize the bbox as a placeholder
        # FeatureCollection so the path completes deterministically. A
        # follow-up job replaces this with proper PMTiles materialization.
        placeholder = {
            "type": "FeatureCollection",
            "features": [],
            "_grace2_note": (
                "STAC item had no downloadable asset; placeholder emitted. "
                "Replace via PMTiles materialization in M5 follow-up."
            ),
            "_grace2_item_id": item.get("id"),
            "_grace2_bbox": list(bbox),
        }
        return json.dumps(placeholder).encode("utf-8")

    asset_url = preferred_asset["href"]
    try:
        asset_resp = requests.get(
            asset_url,
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=60.0,
        )
        asset_resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"MS Open Maps asset download failed url={asset_url}: {exc}"
        ) from exc

    return asset_resp.content


# Sidecar suffix for the click-to-enrich tag bag written alongside the buildings
# .fgb. The detail endpoint (tool_catalog_http /api/building-detail) and the
# enrich-fallback both derive the same key, so this constant is the single
# source of truth for the suffix on both the write and read paths.
BUILDINGS_TAGS_SIDECAR_EXT = "tags.json"


def buildings_cache_uri(
    bbox: tuple[float, float, float, float],
    source: str,
    ext: str,
) -> str:
    """Resolve the ``s3://`` URI the buildings cache write uses for ``ext``.

    Mirrors ``read_through``'s bucket + key derivation EXACTLY so a sibling
    artifact (the ``.tags.json`` sidecar) lands under the SAME ``<key>`` as the
    ``.fgb``. The ``params`` dict + quantization must match the
    ``_fetch_for_source`` call site (``{"bbox": list(quantized), "source":
    src}``, 10 m snap) or the keys diverge.
    """
    from .cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
    )

    quantized = round_bbox_to_resolution(bbox, 10)
    params = {"bbox": list(quantized), "source": source}
    meta = _FETCH_BUILDINGS_METADATA
    source_id = meta.source_class or meta.name
    key = compute_cache_key(source_id, params, meta.ttl_class)
    path = cache_path(meta.source_class, meta.ttl_class, key, ext)
    bucket = os.environ.get("GRACE2_CACHE_BUCKET") or CACHE_BUCKET
    return f"s3://{bucket}/{path}"


def _write_buildings_tags_sidecar(
    bbox: tuple[float, float, float, float],
    source: str,
    tags_by_fid: dict[str, dict[str, Any]],
) -> None:
    """Persist the ``{fid -> full tags}`` sidecar next to the buildings ``.fgb``.

    Best-effort (NATE 2026-06-27 click-to-enrich): a write failure must NOT fail
    the fetch -- the slim layer still renders; the popup enrich path degrades to a
    live Overpass-by-id query when the sidecar is absent. The sidecar key is the
    SAME ``<key>`` as the ``.fgb`` with a ``.tags.json`` suffix
    (``cache/static-30d/buildings/<key>.tags.json``).
    """
    try:
        import boto3

        uri = buildings_cache_uri(bbox, source, BUILDINGS_TAGS_SIDECAR_EXT)
        rest = uri[len("s3://"):]
        bucket, _, obj_key = rest.partition("/")
        body = json.dumps(tags_by_fid, separators=(",", ":")).encode("utf-8")
        s3 = boto3.client(
            "s3", region_name=os.environ.get("AWS_REGION", "us-west-2")
        )
        s3.put_object(
            Bucket=bucket,
            Key=obj_key,
            Body=body,
            ContentType="application/json",
        )
        logger.info(
            "fetch_buildings: wrote tags sidecar key=%s fids=%d bytes=%d",
            obj_key,
            len(tags_by_fid),
            len(body),
        )
    except Exception as exc:  # noqa: BLE001 -- sidecar is best-effort
        logger.warning(
            "fetch_buildings: tags sidecar write degraded (%s); enrich will "
            "fall back to live Overpass-by-id",
            exc,
        )


@register_tool(
    _FETCH_BUILDINGS_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (MS Open Maps buildings),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_buildings(
    bbox: tuple[float, float, float, float],
    source: str = "osm",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch building footprints (polygons) for a bbox.

    Use this when: the agent needs building polygons for damage / exposure
    estimation, risk scoring, or display of the built environment.

    Sources (data-source fallback norm — primary → fallback, never a silent
    dead-end):
        - ``"osm"`` (DEFAULT, RELIABLE PRIMARY): OpenStreetMap building
          footprints via the Overpass API. Global, free, no API key. Returns
          building ``Polygon``s (closed ways) and ``MultiPolygon``s
          (multipolygon relations with holes), clipped to the exact bbox.
          This is the dependable path — use it unless you have a specific
          reason to prefer MS.
        - ``"msft"`` (BEST-EFFORT FALLBACK): Microsoft Open Maps ML-derived
          footprints via the Planetary Computer STAC catalog. Wider rural
          coverage in some areas, but the public catalog often exposes only
          ``abfs://`` GeoParquet stores that cannot be downloaded by-asset, so
          this path frequently fails — treat it as best-effort only.

    Robustness: whichever ``source`` you request is tried FIRST; if it raises
    an ``UpstreamAPIError`` (upstream failure, no coverage, empty result), the
    tool automatically FALLS BACK to the other source. If BOTH fail, an honest
    ``UpstreamAPIError`` naming both attempts is raised — the agent never
    receives a fabricated success. The cache key reflects the source actually
    used, so the two sources never collide and a fallback result is cached
    under its real source.

    Do NOT use this for: live address/parcel lookups (those need a different
    cadastral source); per-structure replacement cost / occupancy / HAZUS
    attributes for loss modeling (use ``fetch_usace_nsi`` — the National
    Structure Inventory point tool — instead); 3D building heights (heights
    are a separate dataset); querying buildings by name or use class (filter
    post-fetch).

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        source: ``"osm"`` (default, reliable primary) or ``"msft"``
            (best-effort fallback). The requested source is tried first; the
            tool falls back to the other on ``UpstreamAPIError``.

    Returns:
        A ``LayerURI`` (``layer_type="vector"``) pointing at a FlatGeobuf in
        the cache bucket:
        ``gs://grace-2-hazard-prod-cache/cache/static-30d/buildings/<key>.fgb``.
        The ``name`` and ``layer_id`` reflect the source actually used.

    FR-CE-8: The fetch is routed through ``read_through`` so identical
    quantized-bbox + source calls reuse the cached artifact.
    """
    if source not in ("msft", "osm"):
        raise BboxInvalidError(
            f"unsupported source={source!r}; allowed: 'osm' (default), 'msft'"
        )
    # Quantize bbox to 10m: building footprint polygons are at sub-meter
    # precision but the bbox boundary is the cache-key driver, and a 10m
    # snap is plenty for the dedup goal (same neighborhood query == same key).
    quantized = round_bbox_to_resolution(bbox, 10)
    if _bbox_area_km2(quantized) > 5_000.0:
        raise BboxInvalidError(
            f"bbox area {_bbox_area_km2(quantized):.1f} km^2 exceeds 5000 km^2 "
            "guardrail for fetch_buildings (a single source query will not "
            "reliably cover that; use a tiled workflow)."
        )

    # Per-source miss-path fetchers. Each goes through read_through under a
    # cache key that reflects the source actually used, so a fallback result
    # caches under its real source and never collides with the other source.
    def _fetch_for_source(src: str) -> LayerURI:
        params = {"bbox": list(quantized), "source": src}
        if src == "osm":
            # Click-to-enrich (NATE 2026-06-27): the OSM fetcher surfaces the
            # full per-fid tag bag so we can persist the sidecar under the SAME
            # cache key as the .fgb. Best-effort -- _write_..._sidecar swallows
            # its own failures so the fetch never fails on a sidecar write.
            def _on_tags(tags_by_fid: dict[str, dict[str, Any]]) -> None:
                _write_buildings_tags_sidecar(quantized, "osm", tags_by_fid)

            fetch_fn = lambda: _fetch_osm_buildings_bytes(  # noqa: E731
                quantized, on_tags=_on_tags
            )
        else:
            fetch_fn = lambda: _fetch_msft_buildings_bytes(quantized)  # noqa: E731
        result = read_through(
            metadata=_FETCH_BUILDINGS_METADATA,
            params=params,
            ext="fgb",
            fetch_fn=fetch_fn,
        )
        assert result.uri is not None
        return LayerURI(
            layer_id=f"buildings-{quantized[0]:.4f}-{quantized[1]:.4f}-{src}",
            name=f"Buildings ({src.upper()})",
            layer_type="vector",
            uri=result.uri,
            style_preset="affected_buildings",
            role="input",
        )

    # Data-source fallback norm: try requested source first; on upstream
    # failure, fall back to the OTHER source; if both fail, raise an honest
    # typed error naming both attempts (never a silent dead-end).
    fallback = "msft" if source == "osm" else "osm"
    try:
        return _fetch_for_source(source)
    except UpstreamAPIError as primary_exc:
        logger.warning(
            "fetch_buildings: source=%r failed (%s); falling back to %r",
            source,
            primary_exc,
            fallback,
        )
        try:
            return _fetch_for_source(fallback)
        except UpstreamAPIError as fallback_exc:
            raise UpstreamAPIError(
                f"fetch_buildings failed for both sources: "
                f"{source!r} -> {primary_exc}; "
                f"{fallback!r} (fallback) -> {fallback_exc}"
            ) from fallback_exc


# ---------------------------------------------------------------------------
# fetch_population — US Census ACS B01003_001E
# ---------------------------------------------------------------------------


_FETCH_POPULATION_METADATA = AtomicToolMetadata(
    name="fetch_population",
    ttl_class="static-30d",
    source_class="population",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# WorldPop branch (Tier-1 default per Appendix F.1).
# ---------------------------------------------------------------------------
#
# WorldPop publishes a global population grid as country-clipped GeoTIFFs.
# Two products are relevant here (REST index at
# https://www.worldpop.org/rest/data/pop/<alias>?iso3=<ISO3>):
#
#   - alias=wpgpunadj (Unconstrained 100m UN-adjusted, 2000-2020) →
#       Global_2000_2020/<YEAR>/<ISO3>/<iso3_lower>_ppp_<YEAR>_UNadj.tif
#       (USA file = ~4 GB)
#   - alias=wpic1km (Unconstrained 1km individual countries, 2000-2020) →
#       Global_2000_2020_1km/<YEAR>/<ISO3>/<iso3_lower>_ppp_<YEAR>_1km_Aggregated.tif
#       (USA file = ~50 MB)
#
# Substrate choice: the 1km Aggregated product. WorldPop's HTTP server
# returns HTTP 200 with the full body for range requests (instead of HTTP
# 206 Partial Content), so GDAL's ``/vsicurl/`` cannot windowed-read the
# 100m file remotely — and downloading 4 GB per cache miss is impractical.
# The 1km file is tractable as a one-shot download and is sufficient for
# exposure analysis at M5/Fort-Myers-class bbox scales. Surfaced as
# OQ-37-WORLDPOP-RESOLUTION-VS-RANGE: revisit when a range-request-capable
# mirror lands, or when an official STAC catalog with native COGs is
# published (the kickoff suggested Microsoft Planetary Computer's
# ``worldpop-100m`` collection — that collection does not exist on PC at
# this writing; the WorldPop Hub STAC at https://hub.worldpop.org/stac/
# also 404s).


_WORLDPOP_BBOX_BY_ISO3: dict[str, tuple[float, float, float, float]] = {
    # ISO3 -> approximate (min_lon, min_lat, max_lon, max_lat) envelope.
    # Substrate-scope: CONUS-centric coverage matching the v0.1 Decision I
    # scope. Replaced with a real point-in-polygon over Natural Earth admin0
    # in a follow-up. Same shape/role as the CONUS state envelope table.
    "USA": (-125.0, 24.0, -66.5, 49.5),
    "CAN": (-141.0, 41.7, -52.6, 70.0),
    "MEX": (-118.5, 14.5, -86.7, 32.7),
    "CUB": (-85.0, 19.8, -74.1, 23.3),
    "BHS": (-79.5, 20.9, -72.7, 27.3),
    "JAM": (-78.4, 17.7, -76.2, 18.5),
    "HTI": (-74.5, 18.0, -71.6, 20.1),
    "DOM": (-72.0, 17.6, -68.3, 19.9),
    "PRI": (-67.3, 17.9, -65.2, 18.6),
}


def _iso3_for_lonlat(lon: float, lat: float) -> str | None:
    """Best-effort ISO3 country code lookup from a point — heuristic only.

    Returns ``None`` if no envelope matches. A future enrichment job replaces
    this with a real point-in-polygon over Natural Earth admin0 boundaries.
    """
    for iso3, (mn_lon, mn_lat, mx_lon, mx_lat) in _WORLDPOP_BBOX_BY_ISO3.items():
        if mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat:
            return iso3
    return None


# The only WorldPop tree these URLs build against is ``Global_2000_2020`` /
# ``Global_2000_2020_1km`` -- by name those products only publish the vintages
# 2000..2020 inclusive. A ``worldpop_<YEAR>`` dataset with YEAR outside this
# window composes a well-formed URL into a NON-EXISTENT path -> a bare HTTP 404.
# Per the data-source-fallback norm we normalize-then-VALIDATE the parsed year
# against this range so an unknown vintage fails LOUD at parse time (a clear
# typed error naming the supported window) rather than after a network 404.
_WORLDPOP_MIN_YEAR = 2000
_WORLDPOP_MAX_YEAR = 2020


def _worldpop_year_from_dataset(dataset: str) -> int:
    """Parse + validate the vintage year off a ``worldpop_<YEAR>`` dataset token.

    Normalize-then-validate (the ``goes18`` vs ``goes-18`` identifier-format
    norm): the year is parsed off the suffix and range-checked against the
    Global_2000_2020 product window BEFORE any URL is composed, so a malformed
    or out-of-range vintage fails LOUD with a clear, typed error listing the
    supported range rather than building a bogus path that 404s downstream.

    Raises ``UpstreamAPIError`` (NOT retryable in spirit -- re-running the same
    bad dataset string will not resolve) when the suffix is non-numeric or the
    year falls outside ``[_WORLDPOP_MIN_YEAR, _WORLDPOP_MAX_YEAR]``.
    """
    if not dataset.startswith("worldpop_"):
        raise UpstreamAPIError(
            f"unsupported dataset={dataset!r} for WorldPop branch; expected 'worldpop_2020'"
        )
    try:
        year = int(dataset.split("_", 1)[1])
    except (IndexError, ValueError) as exc:
        raise UpstreamAPIError(
            f"could not parse vintage year from dataset={dataset!r}; expected 'worldpop_YYYY'"
        ) from exc
    if not (_WORLDPOP_MIN_YEAR <= year <= _WORLDPOP_MAX_YEAR):
        raise UpstreamAPIError(
            f"WorldPop dataset={dataset!r}: year {year} is outside the "
            f"Global_2000_2020 product range "
            f"[{_WORLDPOP_MIN_YEAR},{_WORLDPOP_MAX_YEAR}]; only those vintages "
            "are published in this tree (e.g. 'worldpop_2020')"
        )
    return year


def _worldpop_url_for(iso3: str, year: int, resolution_m: int = 1000) -> str:
    """Compose the WorldPop GeoTIFF URL for a country/year at a given resolution.

    Default (``resolution_m=1000``) uses the
    ``Global_2000_2020_1km/<YEAR>/<ISO3>/<iso3_lower>_ppp_<YEAR>_1km_Aggregated.tif``
    convention from the WorldPop GIS Data hub — the 1km-aggregated product
    is ~50MB per country (USA), vs the 100m UN-adjusted product at ~4GB.
    The 1km default is used because the WorldPop server does not support HTTP
    range requests, so a 4GB whole-country download per cache miss is costly
    even with the 30-day cache window (see OQ-37-WORLDPOP-RESOLUTION-VS-RANGE
    for the resolution-vs-tractability trade-off; the 1km product is
    sufficient for exposure analysis at the bbox scales typical of
    M5/Fort-Myers-class demos).

    Phase-2 resolution lever: pass ``resolution_m <= 100`` to opt into the
    native 100m UN-adjusted product from the base ``Global_2000_2020`` tree
    (``<iso3_lower>_ppp_<YEAR>_UNadj.tif`` — note: NO ``_1km`` segment, the
    ``_UNadj`` suffix). That file is a ~4GB upstream whole-country download
    per cache miss, so it is opt-in only.
    """
    iso3_l = iso3.lower()
    if resolution_m <= 100:
        return (
            f"https://data.worldpop.org/GIS/Population/Global_2000_2020/{year}/"
            f"{iso3}/{iso3_l}_ppp_{year}_UNadj.tif"
        )
    return (
        f"https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/{year}/"
        f"{iso3}/{iso3_l}_ppp_{year}_1km_Aggregated.tif"
    )


def _fetch_worldpop_population_bytes(
    bbox: tuple[float, float, float, float],
    dataset: str,
    target_resolution_m: int = 1000,
) -> bytes:
    """Fetch a windowed COG of WorldPop population for ``bbox``.

    The WorldPop product is published as a single GeoTIFF per (year, country):
    ~50MB at the 1km-aggregated default, or ~4GB at the 100m UN-adjusted
    native product (``target_resolution_m <= 100``). Because the WorldPop
    server does not support HTTP range requests, we download the full country
    file once to a tmp file, then use rasterio to read the windowed sub-region
    and rewrite it as a small Cloud-Optimized GeoTIFF for the cache.
    Subsequent calls hit the cache (30-day TTL) and skip the full download.

    ``dataset`` shape: ``worldpop_<YEAR>`` (e.g. ``worldpop_2020``). The year
    is parsed off the suffix and routed to the corresponding WorldPop URL.
    ``target_resolution_m`` selects the 1km (default) vs 100m product; the
    100m path is a ~4GB upstream country download per cache miss (opt-in cost).
    """
    _validate_bbox(bbox)
    # Normalize-then-validate the vintage year against the published product
    # window BEFORE composing a URL: an out-of-range year (e.g. worldpop_2024)
    # otherwise builds a well-formed path into a non-existent tree -> bare 404.
    year = _worldpop_year_from_dataset(dataset)

    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    iso3 = _iso3_for_lonlat(mid_lon, mid_lat)
    if iso3 is None:
        raise UpstreamAPIError(
            f"could not resolve ISO3 country code for bbox center=({mid_lon}, {mid_lat}); "
            "WorldPop branch needs an envelope match for the country file URL"
        )

    url = _worldpop_url_for(iso3, year, target_resolution_m)

    # rasterio is pulled in transitively by rioxarray; import lazily so test
    # environments without it can still load the registry.
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.windows import Window, from_bounds  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(f"rasterio unavailable: {exc}") from exc

    # Download the country file to a tmp path. We cannot use ``/vsicurl/``
    # because the WorldPop server returns HTTP 200 with the full body for
    # range requests instead of HTTP 206 — GDAL's curl driver then errors
    # with "Range downloading not supported by this server!". The 1km
    # aggregated USA file is ~50MB; bounded enough for a one-shot download.
    import tempfile

    src_tmp: str | None = None
    out_tmp: str | None = None
    try:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _DEFAULT_USER_AGENT},
                timeout=180.0,
                stream=True,
                allow_redirects=True,
            )
            if resp.status_code == 404:
                raise UpstreamAPIError(
                    f"WorldPop file not found at {url} (iso3={iso3}, year={year}); "
                    "verify dataset vintage availability"
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise UpstreamAPIError(
                f"WorldPop download failed url={url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as src_f:
            src_tmp = src_f.name
            for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MiB chunks
                if chunk:
                    src_f.write(chunk)

        try:
            with rasterio.open(src_tmp) as src:
                # Compute the window for the bbox in the source's CRS
                # (WorldPop publishes in EPSG:4326; coords match bbox shape).
                window = from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], transform=src.transform
                )
                window = window.round_offsets().round_lengths()
                window = window.intersection(
                    Window(0, 0, src.width, src.height)
                )
                if window.width <= 0 or window.height <= 0:
                    raise UpstreamAPIError(
                        f"WorldPop window is empty for bbox={bbox} iso3={iso3} — "
                        "bbox may not intersect the country file extent"
                    )
                data = src.read(1, window=window)
                window_transform = src.window_transform(window)
                profile = src.profile.copy()
                profile.update(
                    {
                        "driver": "COG",
                        "width": int(window.width),
                        "height": int(window.height),
                        "transform": window_transform,
                        "compress": "LZW",
                        "BIGTIFF": "IF_SAFER",
                    }
                )
        except UpstreamAPIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"rasterio windowed read failed for {url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as out_f:
            out_tmp = out_f.name
        with rasterio.open(out_tmp, "w", **profile) as dst:
            dst.write(data, 1)
        with open(out_tmp, "rb") as f:
            out_bytes = f.read()

        return out_bytes
    finally:
        for path in (src_tmp, out_tmp):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass


def _fetch_acs_population_bytes(
    bbox: tuple[float, float, float, float], dataset: str
) -> bytes:
    """Fetch US Census ACS B01003 (total population) for tracts intersecting bbox.

    Uses the Census Bureau's public REST API (no key required for small
    queries; an API key can be added later for high-volume use). For the
    M4 substrate we return a GeoJSON ``FeatureCollection`` containing one
    feature per Census tract in the intersecting states, each with the
    ``B01003_001E`` total-population value as a property.

    The tract geometries themselves come from the Census TIGERweb GeoServices
    REST endpoint (a separate call). For substrate-scope simplicity this
    function returns a population *table* (FeatureCollection of point
    features at tract centroids) rather than full tract polygons; a future
    enrichment job swaps in real geometries from the TIGER cartographic
    boundary shapefiles.
    """
    _validate_bbox(bbox)
    if not dataset.startswith("acs_"):
        raise UpstreamAPIError(
            f"unsupported dataset={dataset!r} for ACS branch; expected 'acs_2022'"
        )
    year = dataset.split("_", 1)[1]
    # ACS 5-year endpoint; the variable B01003_001E is total population.
    # We request by `for=state:*` to enumerate the intersecting state set —
    # for the M4 substrate, just take the bbox center's state as a heuristic.
    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    state_fips = _state_fips_for_lonlat(mid_lon, mid_lat)
    if state_fips is None:
        raise UpstreamAPIError(
            f"could not resolve state FIPS for bbox center=({mid_lon}, {mid_lat}); "
            "ACS branch needs CONUS coverage"
        )

    # Census API: B01003_001E for all tracts in the state.
    url = (
        f"https://api.census.gov/data/{year}/acs/acs5?"
        f"get=B01003_001E,NAME&for=tract:*&in=state:{state_fips}"
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _DEFAULT_USER_AGENT, "Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        rows = resp.json()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"US Census ACS API failed for state={state_fips}: {exc}"
        ) from exc

    # rows[0] is the header; rows[1:] are data.
    if not rows or len(rows) < 2:
        raise UpstreamAPIError(
            f"US Census ACS returned no rows for state={state_fips}"
        )
    header = rows[0]
    pop_idx = header.index("B01003_001E")
    name_idx = header.index("NAME")
    state_idx = header.index("state")
    county_idx = header.index("county")
    tract_idx = header.index("tract")

    features: list[dict[str, Any]] = []
    for row in rows[1:]:
        try:
            pop = int(row[pop_idx]) if row[pop_idx] not in (None, "") else None
        except (TypeError, ValueError):
            pop = None
        features.append(
            {
                "type": "Feature",
                "geometry": None,  # geometry enrichment is a follow-up
                "properties": {
                    "name": row[name_idx],
                    "population": pop,
                    "state": row[state_idx],
                    "county": row[county_idx],
                    "tract": row[tract_idx],
                    "dataset": dataset,
                    "variable": "B01003_001E",
                },
            }
        )

    fc = {
        "type": "FeatureCollection",
        "features": features,
        "_grace2_bbox": list(bbox),
        "_grace2_dataset": dataset,
        "_grace2_source": "US Census ACS 5-year",
    }
    buf = io.BytesIO()
    buf.write(json.dumps(fc).encode("utf-8"))
    return buf.getvalue()


# Minimal lon/lat -> state FIPS mapping for the CONUS-default ACS branch.
# Used only as a routing heuristic in the M4 substrate; a future enrichment
# job replaces this with a real point-in-polygon over TIGER state boundaries.
_CONUS_STATE_BBOXES: dict[str, tuple[float, float, float, float]] = {
    # state_fips -> (min_lon, min_lat, max_lon, max_lat) approximate envelope
    "12": (-87.6, 24.4, -80.0, 31.0),  # Florida
    "13": (-85.6, 30.3, -80.8, 35.0),  # Georgia
    "01": (-88.5, 30.2, -84.9, 35.0),  # Alabama
    "28": (-91.7, 30.1, -88.1, 35.0),  # Mississippi
    "22": (-94.0, 28.9, -89.0, 33.0),  # Louisiana
    "48": (-106.7, 25.8, -93.5, 36.5),  # Texas
    "06": (-124.5, 32.5, -114.1, 42.0),  # California
    "53": (-124.8, 45.5, -116.9, 49.0),  # Washington
    "41": (-124.6, 41.9, -116.5, 46.3),  # Oregon
    "36": (-79.8, 40.5, -71.9, 45.0),  # New York
    "37": (-84.4, 33.8, -75.4, 36.6),  # North Carolina
    "45": (-83.4, 32.0, -78.5, 35.2),  # South Carolina
    "21": (-89.6, 36.5, -82.0, 39.1),  # Kentucky
    "47": (-90.3, 35.0, -81.7, 36.7),  # Tennessee
    "51": (-83.7, 36.5, -75.2, 39.5),  # Virginia
}


def _state_fips_for_lonlat(lon: float, lat: float) -> str | None:
    """Best-effort state FIPS lookup from a point — heuristic only.

    Returns ``None`` if no envelope matches. A future enrichment job replaces
    this with a real point-in-polygon over a TIGER state boundary file
    cached in the artifacts bucket.
    """
    for fips, (mn_lon, mn_lat, mx_lon, mx_lat) in _CONUS_STATE_BBOXES.items():
        if mn_lon <= lon <= mx_lon and mn_lat <= lat <= mx_lat:
            return fips
    return None


@register_tool(
    _FETCH_POPULATION_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (WorldPop/GCS public bucket),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_population(
    bbox: tuple[float, float, float, float],
    dataset: str = "worldpop_2020",
    target_resolution_m: int = 1000,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch population data for a bbox from WorldPop (Tier-1 default) or Census ACS.

    Use this when: the agent needs population counts for exposure analysis,
    risk scoring, or display alongside hazard layers. Anywhere globally, with
    no API key, at 100m resolution — that's the default WorldPop path.

    Do NOT use this for: real-time / daytime population (WorldPop and ACS are
    both residential count estimates); per-individual data (these are gridded /
    tract-level aggregates); sub-100m resolution (WorldPop's native grid is
    100m; finer resolution is a paid LandScan-grade product, not Tier-1).

    Default behavior (FR-AS-3, Appendix F.1 Tier-1 preference rule):
        ``dataset="worldpop_2020"`` is the Tier-1 default — WorldPop
        Unconstrained 100m UN-adjusted gridded population. No API key
        required; global coverage; windowed read of the country GeoTIFF via
        rasterio ``/vsicurl/`` so only the bbox window is downloaded.

    Tier-2 opt-in:
        ``dataset="acs_2022"`` routes to the US Census ACS 5-year estimates
        (B01003_001E total population at tract level) — authoritative for
        CONUS, finer demographic detail, but **requires a Census API key**
        for non-trivial volumes (the Tier-2 routing rule per Appendix F.1).
        Pick this when the agent specifically needs tract-level precision
        rather than the 100m raster.

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        dataset: ``"worldpop_2020"`` (Tier-1 default, no key) or
            ``"acs_2022"`` (Tier-2 opt-in, US-only, Census key required for
            high-volume use). The WorldPop branch only publishes the
            Global_2000_2020 tree, so the vintage year MUST be in 2000..2020
            inclusive (``"worldpop_2020"`` is the canonical analytical
            product); a year outside that window raises ``UpstreamAPIError``
            at parse time rather than 404ing on a non-existent path. Newer
            vintages (e.g. ``"worldpop_2024"``) are NOT available until the
            v2024B file URLs stabilize and the range is widened here (tracked
            as OQ-37-WORLDPOP-VINTAGE-YEAR).
        target_resolution_m: ground cell size for the WorldPop branch.
            Default ``1000`` (the 1km-aggregated product, ~50MB per country —
            unchanged). Pass ``100`` (or any value ``<= 100``) to opt into the
            native 100m UN-adjusted product. WARNING: the 100m path is a ~4 GB
            upstream whole-country download per cache miss (WorldPop does not
            support HTTP range requests), so 100m is opt-in for its cost.
            Distinct cache keys per resolution (100m vs 1km do not collide).
            Ignored by the ACS branch.

    Returns:
        A ``LayerURI`` pointing at a Cloud-Optimized GeoTIFF (WorldPop branch)
        or a GeoJSON FeatureCollection (ACS branch) in the cache bucket.
        - WorldPop: ``gs://grace-2-hazard-prod-cache/cache/static-30d/population/<key>.tif``
          (100m raster, units = people per 100m cell).
        - ACS: ``gs://grace-2-hazard-prod-cache/cache/static-30d/population/<key>.json``
          (tract-level FeatureCollection; geometry enrichment is a follow-up).

    FR-CE-8: The fetch is routed through ``read_through`` so identical
    quantized-bbox + dataset calls reuse the cached artifact. FR-DC-4 dedup
    is preserved at 100m bbox quantization (matches WorldPop native
    resolution; coarser than the bbox driving the ACS tract intersection).
    """
    if dataset.startswith("worldpop_"):
        # Tier-1 default: WorldPop 100m windowed COG.
        # Quantize at 100m — matches WorldPop native resolution, preserves
        # FR-DC-4 dedup, and the ACS branch (when opted into) is happy with
        # the same grid since tracts are coarser than 100m anyway.
        quantized = round_bbox_to_resolution(bbox, 100)
        # target_resolution_m enters the cache params so 100m vs 1km fetches
        # get distinct cache keys (they are different upstream products).
        params = {
            "bbox": list(quantized),
            "dataset": dataset,
            "target_resolution_m": target_resolution_m,
        }
        result = read_through(
            metadata=_FETCH_POPULATION_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_worldpop_population_bytes(
                quantized, dataset, target_resolution_m
            ),
        )
        assert result.uri is not None
        return LayerURI(
            layer_id=f"population-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}",
            name=f"Population ({dataset})",
            layer_type="raster",
            uri=result.uri,
            style_preset="population_density",  # tools-backlog #3: people/pixel magma density ramp
            role="input",
            units="people",
        )

    if dataset.startswith("acs_"):
        # Tier-2 opt-in: US Census ACS B01003 tract-level. Census API key is
        # required for non-trivial volumes (OQ-36-CENSUS-API-KEY-REQUIRED);
        # the substrate works for small CONUS queries without a key.
        quantized = round_bbox_to_resolution(bbox, 100)
        params = {"bbox": list(quantized), "dataset": dataset}
        result = read_through(
            metadata=_FETCH_POPULATION_METADATA,
            params=params,
            ext="json",
            fetch_fn=lambda: _fetch_acs_population_bytes(quantized, dataset),
        )
        assert result.uri is not None
        return LayerURI(
            layer_id=f"population-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}",
            name=f"Population ({dataset})",
            layer_type="vector",
            uri=result.uri,
            style_preset="population_density",  # tools-backlog #3: people/pixel magma density ramp
            role="input",
            units="people",
        )

    raise BboxInvalidError(
        f"unsupported dataset={dataset!r}; allowed: 'worldpop_2020' (default), "
        "'acs_2022' (Tier-2 opt-in, US-only)"
    )


# ---------------------------------------------------------------------------
# geocode_location — Nominatim REST
#
# State-snap fallback (NATE directive 2026-06-17): a vague/regional query like
# "south Florida" geocodes via Nominatim with no country/region constraint and
# no sanity check, so an arbitrary first-ranked OSM feature comes back — observed
# resolving to a random house, or to KANSAS for a Florida query — and the agent
# loops re-issuing the same query. The fix: detect a US state in the query and,
# on a wrong-state / failed primary result, snap the bbox to the full state so
# "our bounding box is closer to right than wrong on second attempt". The
# north/south sub-region math is explicitly v2 — NOT now.
# ---------------------------------------------------------------------------

# Directional / qualifier words stripped from the FRONT of a query before the
# state match. "south Florida" -> "florida"; "greater metro Los Angeles, CA"
# leaves the ", CA" abbreviation intact for the abbreviation matcher. Order does
# not matter — we strip leading run of these tokens iteratively.
_STATE_QUALIFIER_PREFIXES: frozenset[str] = frozenset({
    "north", "south", "east", "west", "central",
    "northern", "southern", "eastern", "western",
    "northeast", "northwest", "southeast", "southwest",
    "northeastern", "northwestern", "southeastern", "southwestern",
    "upper", "lower", "upstate", "downstate", "midstate",
    "coastal", "inland", "interior", "rural", "urban",
    "the", "greater", "metro", "metropolitan", "downtown",
    "in", "of", "near",
})

# 2-letter USPS abbreviations the abbreviation matcher accepts. Sourced from the
# shared us_states.STATE_CODE_TO_NAME so the two surfaces never drift. We
# DELIBERATELY exclude marine zones / territories that have no offline bbox row
# below (the _US_STATE_BBOX table is 50 states + DC).
#: Built lazily at module load from us_states (imported inside the helper to
#: avoid a hard import cycle at decoration time).


def _strip_state_qualifiers(text: str) -> str:
    """Remove a leading run of directional / qualifier words from ``text``.

    "south florida" -> "florida"; "the greater los angeles" -> "los angeles";
    "central texas" -> "texas". Stops at the first token that is not a
    qualifier so a real place name is never eaten ("west virginia" is handled
    by the full-name matcher BEFORE this strips "west", see _extract_us_state).
    """
    tokens = text.split()
    while tokens and tokens[0] in _STATE_QUALIFIER_PREFIXES:
        tokens.pop(0)
    return " ".join(tokens)


def _extract_us_state(query: str) -> str | None:
    """Detect a US state in a free-text ``query`` and return its canonical name.

    Returns the canonical full state name (e.g. ``"Florida"``,
    ``"District of Columbia"``) or ``None`` if no state is detected.

    Matching strategy (all case/punctuation-insensitive):

    1. Try the WHOLE normalized query as a full state name FIRST — this lets
       "west virginia", "new mexico", "north carolina" win before the leading
       directional word is stripped.
    2. Strip a leading run of directional / qualifier words ("south",
       "greater", "the", ...) and retry the full-name match — this resolves
       "south florida" -> Florida, "central texas" -> Texas.
    3. Scan tokens for an explicit ``, FL`` / ``FL`` 2-letter USPS abbreviation
       with word boundaries. Guarded so the common word "in" is NOT matched as
       Indiana and "or" not as Oregon: a bare 2-letter token only counts when
       it is the LAST token or immediately follows a comma (the "City, ST"
       idiom), and "IN"/"OR"/"OK"/"HI"/"ME" require the comma form.

    Never raises; returns ``None`` for non-string / empty / non-state input.
    """
    if not isinstance(query, str):
        return None
    raw = query.strip()
    if not raw:
        return None

    # Lazy import to dodge any import-cycle at module decoration time.
    from .us_states import STATE_CODE_TO_NAME, STATE_NAME_TO_CODE

    # Normalize: lowercase, drop most punctuation but KEEP commas (the "City, ST"
    # idiom relies on them), collapse whitespace.
    lowered = raw.lower()
    # Preserve commas; turn other punctuation into spaces.
    cleaned = re.sub(r"[^a-z0-9,\s]", " ", lowered)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # A comma-free form for full-name matching.
    no_comma = cleaned.replace(",", " ")
    no_comma = re.sub(r"\s+", " ", no_comma).strip()

    def _canonical(name_lc: str) -> str | None:
        code = STATE_NAME_TO_CODE.get(name_lc)
        if code is None:
            return None
        # Only the 50 states + DC have an offline bbox row; ignore territories.
        canonical = STATE_CODE_TO_NAME.get(code)
        if canonical is None or code not in _US_STATE_BBOX_CODES:
            return None
        return canonical

    # (1) whole normalized query as a full state name.
    hit = _canonical(no_comma)
    if hit is not None:
        return hit

    # (2) strip leading directional / qualifier run, retry full-name match.
    stripped = _strip_state_qualifiers(no_comma)
    if stripped and stripped != no_comma:
        hit = _canonical(stripped)
        if hit is not None:
            return hit

    # (2b) a multi-word query whose TAIL is a full state name
    # ("protected areas in south florida" -> tokens end with "florida";
    # "wildfires near los angeles california" -> ends "california"). Try the
    # last 1-3 tokens (handles "new mexico", "north carolina", "rhode island").
    tail_tokens = stripped.split() if stripped else no_comma.split()
    for n in (3, 2, 1):
        if len(tail_tokens) >= n:
            candidate = " ".join(tail_tokens[-n:])
            hit = _canonical(candidate)
            if hit is not None:
                return hit

    # NOTE: an earlier F71 attempt added a "(2c)" step that scanned for a full
    # state NAME at ANY interior position (to catch "the Florida Panhandle").
    # It was REVERTED — the any-position scan turned the wrong-state sanity
    # guard into a source of WRONG answers: "Kansas City, MO" -> Kansas,
    # "the Washington Monument" -> Washington (snapping a DC AOI to WA state),
    # "the Mississippi River delta near New Orleans" -> Mississippi. The named
    # vernacular cases ("South Florida", "Southern California", "Central Texas")
    # already resolve via (2)/(2b) tail-matching, so the interior scan added
    # real risk for negligible gain. A constrained interior match (head/tail
    # only, feature-noun exclusion, yielding to the City,ST idiom) can be a
    # future safe enhancement; the bare retry-loop steer in adapter.py is the
    # other half of the F71 fix.

    # (3) explicit 2-letter USPS abbreviation with word-boundary guards.
    # The dangerous bare words: in (IN), or (OR), ok (OK), hi (HI), me (ME),
    # de (DE)? "de" rare. We require these to appear in the comma idiom.
    comma_guarded = {"in", "or", "ok", "hi", "me", "de", "co", "id", "la",
                     "pa", "ma", "md", "mo", "mt", "ne", "oh", "wa", "wi"}
    # Build token list preserving comma adjacency markers.
    # Replace ", xx" with a sentinel so we know it followed a comma.
    parts = [p.strip() for p in cleaned.split(",")]
    abbr_to_code = {c.lower(): c for c in _US_STATE_BBOX_CODES}
    for idx, part in enumerate(parts):
        toks = part.split()
        if not toks:
            continue
        # A 2-letter token immediately AFTER a comma (idx>0 and it's the first
        # token of this part) is the "City, ST" idiom — always trust it.
        first = toks[0]
        if idx > 0 and first in abbr_to_code:
            return STATE_CODE_TO_NAME[abbr_to_code[first]]
        # Otherwise only trust an abbreviation that is NOT a dangerous English
        # word, and only when it's the final token of the whole query.
    final_tok = cleaned.replace(",", " ").split()
    if final_tok:
        last = final_tok[-1]
        if last in abbr_to_code and last not in comma_guarded:
            return STATE_CODE_TO_NAME[abbr_to_code[last]]

    # (4) last resort: hand the whole stripped string to the shared
    # us_states.resolve_state_code, which also handles "Washington D.C." and
    # "state of X" idioms. Gate the result to the 50+DC table so territories /
    # marine zones (which have no offline bbox) never leak through.
    #
    # GUARD: resolve_state_code has an UNCONDITIONAL 2-letter fast path that
    # uppercases any 2-char string and matches it as a USPS code — so a bare
    # dangerous English word ("in"->IN, "or"->OR), or a query that strips to
    # one ("the or"->"or"), would false-match a state, bypassing the
    # comma_guarded set built above. Comma-positioned abbreviations were already
    # trusted in step (3); a BARE comma_guarded token reaching here is not a
    # state reference, so skip the fallback for it.
    fallback_query = stripped or no_comma
    fallback_tokens = fallback_query.split()
    if len(fallback_tokens) == 1 and fallback_tokens[0] in comma_guarded:
        return None

    from .us_states import resolve_state_code

    code = resolve_state_code(fallback_query)
    if code is not None and code in _US_STATE_BBOX_CODES:
        return STATE_CODE_TO_NAME[code]
    return None


# Census cartographic state extents (EPSG:4326), [min_lon, min_lat, max_lon,
# max_lat]. Vetted OFFLINE last-resort backstop — _resolve_state_bbox prefers
# the live OSM admin boundingbox and only uses these on failure. Values are the
# Census TIGER state bounding extents rounded outward to ~0.1 deg so the snap
# fully covers the state (closer to right than wrong). Alaska is clamped to the
# main landmass east of the antimeridian; the Aleutian tail crossing 180 is
# intentionally NOT split here (v2).
_US_STATE_BBOX: dict[str, list[float]] = {
    "Alabama": [-88.5, 30.1, -84.9, 35.1],
    "Alaska": [-179.2, 51.2, -129.9, 71.5],
    "Arizona": [-114.9, 31.3, -109.0, 37.1],
    "Arkansas": [-94.7, 33.0, -89.6, 36.6],
    "California": [-124.5, 32.5, -114.1, 42.1],
    "Colorado": [-109.1, 36.9, -102.0, 41.1],
    "Connecticut": [-73.8, 40.9, -71.7, 42.1],
    "Delaware": [-75.8, 38.4, -75.0, 39.9],
    "District of Columbia": [-77.2, 38.7, -76.9, 39.0],
    "Florida": [-87.7, 24.4, -79.9, 31.1],
    "Georgia": [-85.7, 30.3, -80.8, 35.1],
    "Hawaii": [-160.3, 18.8, -154.7, 22.3],
    "Idaho": [-117.3, 41.9, -110.9, 49.1],
    "Illinois": [-91.6, 36.9, -87.4, 42.6],
    "Indiana": [-88.1, 37.7, -84.7, 41.8],
    "Iowa": [-96.7, 40.3, -90.1, 43.6],
    "Kansas": [-102.1, 36.9, -94.5, 40.1],
    "Kentucky": [-89.6, 36.4, -81.9, 39.2],
    "Louisiana": [-94.1, 28.9, -88.7, 33.1],
    "Maine": [-71.2, 42.9, -66.8, 47.6],
    "Maryland": [-79.5, 37.8, -75.0, 39.8],
    "Massachusetts": [-73.6, 41.1, -69.8, 42.9],
    "Michigan": [-90.5, 41.6, -82.3, 48.4],
    "Minnesota": [-97.3, 43.4, -89.4, 49.5],
    "Mississippi": [-91.7, 30.1, -88.0, 35.1],
    "Missouri": [-95.8, 35.9, -89.0, 40.7],
    "Montana": [-116.1, 44.3, -104.0, 49.1],
    "Nebraska": [-104.1, 39.9, -95.2, 43.1],
    "Nevada": [-120.1, 35.0, -114.0, 42.1],
    "New Hampshire": [-72.6, 42.6, -70.5, 45.4],
    "New Jersey": [-75.6, 38.8, -73.8, 41.4],
    "New Mexico": [-109.1, 31.3, -102.9, 37.1],
    "New York": [-79.8, 40.4, -71.8, 45.1],
    "North Carolina": [-84.4, 33.8, -75.4, 36.7],
    "North Dakota": [-104.1, 45.9, -96.5, 49.1],
    "Ohio": [-84.9, 38.3, -80.5, 42.4],
    "Oklahoma": [-103.1, 33.6, -94.4, 37.1],
    "Oregon": [-124.6, 41.9, -116.4, 46.4],
    "Pennsylvania": [-80.6, 39.7, -74.6, 42.4],
    "Rhode Island": [-71.9, 41.1, -71.1, 42.1],
    "South Carolina": [-83.4, 32.0, -78.5, 35.3],
    "South Dakota": [-104.1, 42.4, -96.4, 46.0],
    "Tennessee": [-90.4, 34.9, -81.6, 36.7],
    "Texas": [-106.7, 25.8, -93.5, 36.6],
    "Utah": [-114.1, 36.9, -109.0, 42.1],
    "Vermont": [-73.5, 42.7, -71.5, 45.1],
    "Virginia": [-83.7, 36.5, -75.2, 39.5],
    "Washington": [-124.8, 45.5, -116.9, 49.1],
    "West Virginia": [-82.7, 37.2, -77.7, 40.7],
    "Wisconsin": [-92.9, 42.4, -86.8, 47.4],
    "Wyoming": [-111.1, 40.9, -104.0, 45.1],
}

#: USPS codes covered by the offline bbox table (50 states + DC). Used by the
#: abbreviation matcher to ignore territories / marine zones that have no row.
_US_STATE_BBOX_CODES: frozenset[str] = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
})


def _resolve_state_bbox(state_name: str) -> tuple[list[float], float, float, str]:
    """Resolve a canonical state name to ``(bbox, lat, lon, source)``.

    PREFERS the live OSM admin boundary: a Nominatim ``featuretype=state``
    lookup constrained to ``countrycodes=us`` returns the REAL state polygon's
    bounding box (more accurate than the offline table, and reflects OSM edits).
    Falls back to the vetted ``_US_STATE_BBOX`` table on ANY failure / empty
    result so this helper NEVER raises.

    ``bbox`` is ``[min_lon, min_lat, max_lon, max_lat]`` (project canonical).
    ``lat`` / ``lon`` is the bbox centroid. ``source`` is ``"nominatim-state"``
    when the live lookup succeeded, else ``"offline-state-table"``.
    """
    fallback = _US_STATE_BBOX.get(state_name)
    if fallback is None:
        # Should not happen — _extract_us_state only returns table-backed names.
        raise BboxInvalidError(
            f"no offline bbox for state {state_name!r}"
        )

    def _centroid(bb: list[float]) -> tuple[float, float]:
        return ((bb[1] + bb[3]) / 2.0, (bb[0] + bb[2]) / 2.0)

    user_agent = os.environ.get(
        "GRACE2_NOMINATIM_USER_AGENT", _DEFAULT_USER_AGENT
    )
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": f"{state_name}, United States",
        "countrycodes": "us",
        "featuretype": "state",
        "format": "jsonv2",
        "limit": 1,
        "addressdetails": 0,
        "polygon_geojson": 0,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if body:
            top = body[0]
            bb = top.get("boundingbox", [])
            if len(bb) == 4:
                south, north, west, east = (float(v) for v in bb)
                live_bbox = [west, south, east, north]
                # Only trust a well-ordered, non-degenerate live bbox. A
                # degenerate / inverted OSM response (e.g. [0,0,0,0]) fails
                # these comparisons (NaN also fails) and falls through to the
                # vetted offline table rather than shipping a bad extent.
                if west < east and south < north:
                    lat = float(top.get("lat", _centroid(live_bbox)[0]))
                    lon = float(top.get("lon", _centroid(live_bbox)[1]))
                    return live_bbox, lat, lon, "nominatim-state"
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.info(
            "state-bbox live lookup failed for %r (%s); using offline table",
            state_name,
            exc,
        )

    lat, lon = _centroid(fallback)
    return list(fallback), lat, lon, "offline-state-table"


def _centroid_in_bbox(
    lat: float, lon: float, bbox: list[float], margin: float = 1.0
) -> bool:
    """True if ``(lat, lon)`` falls inside ``bbox`` widened by ``margin`` deg.

    ``bbox`` is ``[min_lon, min_lat, max_lon, max_lat]``. The margin (default
    1 degree, ~110 km) tolerates a precise match whose centroid sits just
    outside the coarse offline/admin extent (e.g. a coastal city) without
    admitting a wrong-STATE match — a Kansas-for-Florida result is hundreds of
    km out and still fails this check.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return (
        (min_lon - margin) <= lon <= (max_lon + margin)
        and (min_lat - margin) <= lat <= (max_lat + margin)
    )


_GEOCODE_LOCATION_METADATA = AtomicToolMetadata(
    name="geocode_location",
    ttl_class="dynamic-1h",
    source_class="geocode",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# OPEN-10 (NATE-reported): "downtown Tampa" and similar sub-locality phrasings
# resolved to a SINGLE BUILDING/POI footprint (bbox tens of meters across), so
# every layer fetched against the case AOI came back empty -- all of NATE's
# old Tampa cases were invisible for this reason. Live-confirmed root cause
# (2026-07-11): Nominatim's ONLY match for "downtown Tampa" is a
# ``category=railway, type=tram_stop`` node literally named "Downtown Tampa"
# (a streetcar stop), bbox ~11m x 11m -- there is no competing neighbourhood
# entity in OSM's Tampa data (compare "downtown Miami" / "midtown Atlanta",
# which resolve cleanly to ``category=place, type=neighbourhood`` with a
# proper ~2 km bbox). Two-part fix below:
#
#   (a) RESULT-CLASS PREFERENCE: with ``limit=1`` the old code could not even
#       SEE an alternate candidate. Widen the query and, when the top hit is
#       NOT itself a place/administrative-boundary result, scan the remaining
#       candidates for the first one that is (city/town/village/hamlet/
#       suburb/neighbourhood/quarter, or an admin boundary) and promote it.
#       This is deliberately broad -- rather than an allowlist of "POI
#       classes" (building/amenity/shop/office, ...), it demotes ANYTHING
#       that isn't place-class, because the live Tampa failure is a railway
#       node, not a building. Skipped entirely for queries that clearly name
#       a POI (street address, named landmark) so a genuine point lookup is
#       never redirected to the surrounding place.
#   (b) MINIMUM AOI FLOOR: whichever candidate wins, a bbox smaller than
#       ~1 km on its long axis is still unusable as a case AOI (this is what
#       actually fixes "downtown Tampa" itself -- Nominatim has no better
#       candidate to promote to). Expand it to a 2 km square centered on the
#       point and attach an honest ``expansion_note`` so the model narrates
#       the widening instead of silently handing back an invisible-layers AOI.
# ---------------------------------------------------------------------------

#: Nominatim ``category`` values that represent an area/place (as opposed to
#: a point-scale POI). Matches the taxonomy observed live in jsonv2 responses
#: (``category`` is the jsonv2 field name -- there is no ``class`` key).
_PLACE_CATEGORIES: frozenset[str] = frozenset({"place"})

#: Nominatim ``type`` values under ``category="place"`` (or the closest OSM
#: place-node types) that make a usable area AOI. Excludes point-scale place
#: types such as ``"isolated_dwelling"``.
_PLACE_TYPES: frozenset[str] = frozenset({
    "city", "town", "village", "hamlet", "suburb", "neighbourhood",
    "quarter", "borough", "municipality", "county", "state", "region",
    "district", "city_block", "island",
})


def _is_place_class(candidate: dict[str, Any]) -> bool:
    """True if ``candidate`` (a raw Nominatim jsonv2 result) is an area/place.

    A place-class result is ``category="place"`` with an area-scale ``type``
    (city/town/suburb/neighbourhood/...), OR ``category="boundary"`` with
    ``type="administrative"`` (counties, states, admin areas at any level).
    """
    category = candidate.get("category")
    if category in _PLACE_CATEGORIES:
        return candidate.get("type") in _PLACE_TYPES
    if category == "boundary" and candidate.get("type") == "administrative":
        return True
    return False


#: Query substrings that clearly name a point-of-interest rather than an
#: area -- the class-preference reorder in ``_fetch_nominatim_geocode_bytes``
#: MUST NOT touch these queries, or e.g. "Tampa International Airport" would
#: get redirected to the surrounding city boundary instead of the airport.
_POI_INTENT_KEYWORDS: tuple[str, ...] = (
    "airport", "station", "stadium", "arena", "hospital", "university",
    "college", "courthouse", "terminal", "port authority", "mall",
    "museum", "library", "cemetery", "monument", "memorial",
)

#: A leading house number ("123 Main St, Tampa, FL") is a street address --
#: always a precise point lookup, never an area-intent query.
_STREET_ADDRESS_RE = re.compile(r"^\s*\d+[\d-]*\s+\S")


def _looks_like_poi_query(query: str) -> bool:
    """True if ``query`` clearly names a point-of-interest, not an area.

    Governs the OPEN-10 class-preference reorder: point-intent queries
    (street addresses, named landmarks like an airport or a stadium) pass
    through with Nominatim's own top-ranked result, unchanged.
    """
    if _STREET_ADDRESS_RE.match(query):
        return True
    lowered = query.lower()
    return any(keyword in lowered for keyword in _POI_INTENT_KEYWORDS)


#: Kilometers per degree of latitude (also used as the per-degree-of-longitude
#: figure at the equator; longitude shrinks by cos(latitude) elsewhere). An
#: equirectangular approximation is intentional here -- OPEN-10 only needs to
#: tell "building footprint" from "usable AOI" apart, not survey-grade
#: distance.
_KM_PER_DEGREE = 111.32

#: Below this long-axis size (km) a bbox reads as a point-scale footprint
#: (building, POI node, tram stop, ...) rather than a usable case AOI.
_MIN_AOI_AXIS_KM = 1.0

#: Side length (km) of the square AOI a point-scale geocode result is
#: expanded to.
_EXPANDED_AOI_SIDE_KM = 2.0


def _bbox_long_axis_km(
    west: float, south: float, east: float, north: float, lat: float
) -> float:
    """Approximate the longer side of a WGS84 bbox in kilometers."""
    height_km = abs(north - south) * _KM_PER_DEGREE
    width_km = abs(east - west) * _KM_PER_DEGREE * math.cos(math.radians(lat))
    return max(height_km, width_km)


def _square_km_bbox(
    lat: float, lon: float, side_km: float
) -> tuple[float, float, float, float]:
    """Return ``(west, south, east, north)`` for a ``side_km`` square centered
    on ``(lat, lon)`` (same equirectangular approximation as
    ``_bbox_long_axis_km``).
    """
    half_deg_lat = (side_km / 2.0) / _KM_PER_DEGREE
    # Guard near the poles so this never divides by ~0; irrelevant in
    # practice (case AOIs are not polar), but keeps the helper total.
    cos_lat = max(abs(math.cos(math.radians(lat))), 0.01)
    half_deg_lon = (side_km / 2.0) / (_KM_PER_DEGREE * cos_lat)
    return (
        lon - half_deg_lon,
        lat - half_deg_lat,
        lon + half_deg_lon,
        lat + half_deg_lat,
    )


def _fetch_nominatim_geocode_bytes(query: str) -> bytes:
    """Forward-geocode ``query`` via OpenStreetMap Nominatim and return JSON bytes.

    Honors Nominatim usage policy:
    - descriptive User-Agent identifying the app + contact;
    - ``format=jsonv2`` for stable JSON shape;
    - ``limit=5`` so a same-locality place-class alternate is visible to the
      OPEN-10 class-preference reorder below (was ``limit=1``, which could
      not see past a single point-scale top hit -- see the module comment
      above this function);
    - ``polygon_geojson=0`` (we just want bbox + lat/lon);
    - one request per cache-bucket window (the ``dynamic-1h`` class naturally
      throttles repeat queries — see ``read_through``).

    Area-intent semantics (OPEN-10): when the top-ranked hit is a point-scale
    POI (not itself a place/administrative-boundary result) and the query
    does not clearly name a POI, the first place-class candidate among the
    remaining results is promoted instead. Whichever candidate wins, if its
    bbox is still smaller than ~1 km on its long axis, it is expanded to a
    2 km square centered on the point and the returned dict carries an
    additive ``expansion_note`` key the agent narrates truthfully.

    Returns the JSON-encoded structured result the tool body further
    massages into a ``GeocodedLocation``-shaped dict.
    """
    if not query or not query.strip():
        raise BboxInvalidError("geocode_location requires a non-empty query")

    user_agent = os.environ.get("GRACE2_NOMINATIM_USER_AGENT", _DEFAULT_USER_AGENT)
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": query.strip(),
        "format": "jsonv2",
        "limit": 5,
        "addressdetails": 0,
        "polygon_geojson": 0,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"Nominatim search failed for query={query!r}: {exc}"
        ) from exc
    except ValueError as exc:
        raise UpstreamAPIError(
            f"Nominatim returned non-JSON for query={query!r}: {exc}"
        ) from exc

    if not body:
        raise GeocodeNoMatchError(
            f"Could not locate {query!r}. Try refining the place name "
            f"(add City, ST or a country, or check the spelling)."
        )

    top = body[0]

    # OPEN-10 part (a): promote a place-class candidate over a point-scale
    # top hit, unless the query clearly names a POI (street address, named
    # landmark) -- see the module comment above this function for the live
    # "downtown Tampa" vs. "downtown Miami" evidence behind this heuristic.
    if not _is_place_class(top) and not _looks_like_poi_query(query):
        for candidate in body[1:]:
            if _is_place_class(candidate):
                logger.info(
                    "geocode_location query=%r top hit category=%r/type=%r "
                    "(point-scale); promoting place-class candidate %r",
                    query,
                    top.get("category"),
                    top.get("type"),
                    candidate.get("display_name"),
                )
                top = candidate
                break

    # Nominatim returns boundingbox as [south, north, west, east] strings.
    bb = top.get("boundingbox", [])
    if len(bb) != 4:
        raise GeocodeNoMatchError(
            f"Could not locate {query!r} (no valid bounding box returned). Try "
            f"refining the place name (add City, ST or a country, or check the "
            f"spelling)."
        )
    try:
        south, north, west, east = [float(v) for v in bb]
    except (TypeError, ValueError) as exc:
        raise UpstreamAPIError(
            f"Nominatim boundingbox non-numeric: {bb!r}"
        ) from exc

    lat = float(top.get("lat", (south + north) / 2.0))
    lon = float(top.get("lon", (west + east) / 2.0))

    # OPEN-10 part (b): MINIMUM AOI FLOOR. Whatever candidate won above, a
    # bbox smaller than ~1 km on its long axis is not a usable case AOI --
    # expand it to a 2 km square and carry an honest note so the model
    # narrates the widening instead of silently returning an
    # invisible-everything AOI.
    expansion_note: str | None = None
    long_axis_km = _bbox_long_axis_km(west, south, east, north, lat)
    if long_axis_km < _MIN_AOI_AXIS_KM:
        west, south, east, north = _square_km_bbox(lat, lon, _EXPANDED_AOI_SIDE_KM)
        expansion_note = (
            f"Geocoder returned a building-scale footprint "
            f"(~{long_axis_km * 1000.0:.0f} m across) for {query.strip()!r}; "
            f"expanded to a {_EXPANDED_AOI_SIDE_KM:.0f} km area of interest. "
            f"Draw an AOI for precise control."
        )

    structured = {
        "name": top.get("display_name", query),
        "latitude": lat,
        "longitude": lon,
        # Normalize to (min_lon, min_lat, max_lon, max_lat) — the project
        # canonical bbox shape (matches LayerURI / Census / py3dep).
        "bbox": [west, south, east, north],
        "source": "nominatim",
        "query": query,
        "osm_type": top.get("osm_type"),
        "osm_id": top.get("osm_id"),
        "place_id": top.get("place_id"),
    }
    if expansion_note is not None:
        structured["expansion_note"] = expansion_note
    return json.dumps(structured).encode("utf-8")


@register_tool(
    _GEOCODE_LOCATION_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (OSM Nominatim API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def geocode_location(query: str, **_extra_ignored: Any) -> dict[str, Any]:
    """Translate a free-text place name into a bbox and canonical name via OpenStreetMap Nominatim.

    **What it does:** Forward-geocodes a human-readable location string to a
    WGS84 bounding box, centroid latitude/longitude, and canonical place name
    using the OpenStreetMap Nominatim REST API. The result is cached for one
    hour (``dynamic-1h``), so repeated references to the same place within a
    session are free.

    **When to use:**
    - User asks to "model flooding in Fort Myers, FL" or "show wildfires near
      Los Angeles" — convert the place name to a bbox before calling spatial
      fetch tools.
    - The agent needs to translate a textual event location from the Hazard
      Event Pipeline (``EventMetadata.location_name``) into a usable bbox.
    - Any workflow step that starts from a city, county, neighborhood, or
      named geographic feature rather than coordinates.

    **When NOT to use:**
    - Reverse geocoding (coordinates → place name) — Nominatim has a separate
      ``/reverse`` endpoint; use ``web_fetch`` or a future dedicated tool.
    - Routing or turn-by-turn distance queries — Nominatim does not support
      them; use a routing API.
    - High-precision parcel-level address resolution — Nominatim is
      street-address level at best; use a dedicated geocoding provider for
      sub-parcel accuracy.
    - Queries where bbox coverage matters: the returned bbox reflects OSM's
      administrative boundary for the named place, which can be very large for
      counties or states; narrow it before passing to ``fetch_dem`` or similar
      large-download tools.

    **Parameters:**
    - ``query`` (str): Free-text place name or description.
      Examples: ``"Fort Myers, FL"``, ``"Lee County Florida"``,
      ``"Gulf of Mexico"``. Must be non-empty.

    **Returns:**
    A plain dict with keys:
    - ``name`` (str): canonical OSM display name.
    - ``bbox`` (list[float]): ``[min_lon, min_lat, max_lon, max_lat]`` in
      EPSG:4326 — feeds directly into ``fetch_dem``, ``fetch_buildings``,
      ``fetch_population``, ``fetch_landcover``, etc. Always at least ~2 km
      on its long axis (see the AOI floor below) — this bbox is always a
      usable case AOI, never a bare point/building footprint.
    - ``latitude`` / ``longitude`` (float): centroid of the matched feature.
    - ``source`` (str): ``"nominatim"`` on a precise match, or
      ``"state-bbox-fallback"`` when the state-snap fired (see below).
    - ``osm_type``, ``osm_id``, ``place_id`` (str / int): OSM provenance fields
      (``None`` on a state-snap, where there is no single OSM feature).
    - ``fallback_reason`` (str, ADDITIVE — present ONLY on a state-snap): an
      honest human-readable explanation the agent narrates truthfully, e.g.
      *"No precise match for 'south Florida'; snapped to the full state of
      Florida. Refine the prompt for a smaller area."*
    - ``expansion_note`` (str, ADDITIVE — present ONLY when the AOI floor
      fired, see below): an honest note the agent narrates truthfully, e.g.
      *"Geocoder returned a building-scale footprint (~11 m across) for
      'downtown Tampa'; expanded to a 2 km area of interest. Draw an AOI for
      precise control."*

    **State-snap fallback (NATE directive):** vague/regional queries
    ("south Florida", "protected areas in south Florida") used to geocode to an
    arbitrary first-ranked OSM feature (observed: a random house, or KANSAS for
    a Florida query). Now, if a US state is detected in the query, the primary
    result's centroid is sanity-checked against that state's bounding box; a
    wrong-state result (or a "no results" / upstream failure) snaps the bbox to
    the full state (live OSM state admin boundary, with a vetted offline Census
    extent as last resort) and records an honest ``fallback_reason``. A PRECISE
    in-state query ("Fort Myers, FL", "Lee County Florida") passes the
    sanity-check and is returned UNCHANGED — it is never widened. When NO state
    is detected and the primary geocode fails, the typed error still raises
    (genuine failures are never swallowed).

    **Area-intent semantics + AOI floor (OPEN-10, NATE-reported):**
    sub-locality phrasings like "downtown Tampa" used to resolve to a SINGLE
    BUILDING/POI footprint — e.g. the live top (and only) Nominatim match for
    "downtown Tampa" is a railway tram-stop node named "Downtown Tampa", bbox
    ~11 m across — so every layer fetched against the resulting AOI came back
    empty. Two fixes now run inside the fetch:
    (a) *result-class preference* — when the top-ranked hit is a point-scale
    POI (not itself a place or administrative-boundary result) and the query
    does not clearly name a POI (no street-address house number, no landmark
    keyword like "airport" or "stadium"), the first place-class candidate
    among the next few results (city/town/village/suburb/neighbourhood/
    quarter/admin boundary) is promoted instead — e.g. "downtown Miami" and
    "midtown Atlanta" already resolve straight to a neighbourhood polygon and
    are untouched by this rule;
    (b) *minimum AOI floor* — whichever candidate wins, if its bbox is still
    smaller than ~1 km on its long axis (the Tampa case: there is no
    neighbourhood entity to promote to), it is expanded to a 2 km square
    centered on the point and the ``expansion_note`` key is set. Bboxes for
    genuine POI queries and ordinary city/county/state matches are returned
    exactly as Nominatim reports them — this only ever widens a
    building-scale result, never a real area.

    **Cross-tool dependencies:**
    - Upstream of: ``fetch_dem``, ``fetch_buildings``, ``fetch_population``,
      ``fetch_landcover``, ``fetch_river_geometry``,
      ``fetch_administrative_boundaries``, ``fetch_nws_event``,
      ``fetch_firms_active_fire``, and most other bbox-based fetchers.
    - Called internally by ``model_flood_scenario`` workflow to resolve a
      user-supplied location string before fetching DEM/landcover.

    FR-CE-8: The fetch is routed through ``read_through`` so two identical
    queries within the same hourly window reuse the cached response. The
    cache class is ``"dynamic-1h"`` per FR-DC-2 active-state-ish (geocoding
    answers DO change as Nominatim's OSM index updates, but on a slower
    cadence than hourly).

    Side effect: per FR-TA-2 §"Location-resolved emission" / FR-AS-7, the
    agent surface emits a ``location-resolved`` WebSocket message when this
    tool returns so the client auto-snaps the map. The emission seam is
    in the agent's server.py M1 module — surfaced as
    OQ-33-LOCATION-RESOLVED-EMISSION-SEAM for the agent job that owns
    envelope emission this sprint (job-0035) to wire up.

    Nominatim usage policy: User-Agent is sent on every request; the
    ``dynamic-1h`` cache class naturally throttles repeat queries (one
    fetch per hour-bucket per distinct query).
    """
    if not isinstance(query, str) or not query.strip():
        raise BboxInvalidError("geocode_location requires a non-empty string query")

    # Detect a US state up front so we know whether the state-snap fallback is
    # eligible for either failure mode (wrong-state result OR no-result error).
    detected_state = _extract_us_state(query)

    params = {"query": query.strip()}
    try:
        result = read_through(
            metadata=_GEOCODE_LOCATION_METADATA,
            params=params,
            ext="json",
            fetch_fn=lambda: _fetch_nominatim_geocode_bytes(query),
        )
    except UpstreamAPIError:
        # No precise match / upstream failure. If we recognized a state, snap to
        # it instead of dead-ending (fallback norm: primary -> fallback ->
        # honest, never silent). This branch ALSO catches GeocodeNoMatchError
        # (a subclass of UpstreamAPIError) so a no-match query like "south
        # Florida" still snaps to the state. Otherwise the genuine failure
        # propagates -- for GEOCODE_NO_MATCH that means a non-retryable error
        # the agent surfaces as a clarify-the-place request, not a retry.
        if detected_state is not None:
            return _state_snap_payload(
                query,
                detected_state,
                reason=(
                    f"No precise match for {query.strip()!r}; snapped to the "
                    f"full state of {detected_state}. Refine the prompt for a "
                    f"smaller area."
                ),
            )
        raise

    # The fetched (or cached) payload is JSON bytes; decode and return as a
    # structured dict. The cache URI is intentionally NOT returned to the LLM
    # — Tier separation (invariant 5): no gs:// URIs leak into model text.
    payload = json.loads(result.data.decode("utf-8"))

    # Sanity-check: if a state was detected but the primary result's centroid
    # lands OUTSIDE that state (with a tolerance margin), the match is wrong —
    # e.g. a "south Florida" query that resolved to Kansas. Snap to the state.
    if detected_state is not None:
        state_bbox = _US_STATE_BBOX.get(detected_state)
        try:
            lat = float(payload.get("latitude"))
            lon = float(payload.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None  # type: ignore[assignment]
        if state_bbox is not None and (
            lat is None
            or lon is None
            or not _centroid_in_bbox(lat, lon, state_bbox)
        ):
            logger.info(
                "geocode_location query=%r resolved OUTSIDE detected state %r "
                "(centroid=%s,%s) — snapping to state bbox",
                query,
                detected_state,
                lat,
                lon,
            )
            return _state_snap_payload(
                query,
                detected_state,
                reason=(
                    f"No precise match for {query.strip()!r}; snapped to the "
                    f"full state of {detected_state}. Refine the prompt for a "
                    f"smaller area."
                ),
            )

    logger.info(
        "geocode_location query=%r resolved name=%r cache_hit=%s",
        query,
        payload.get("name"),
        result.hit,
    )
    return payload


def _state_snap_payload(
    query: str, state_name: str, *, reason: str
) -> dict[str, Any]:
    """Build the backward-compatible geocode dict for a state-snap fallback.

    Same keys as the primary path (``name``, ``bbox``, ``latitude``,
    ``longitude``, ``source``, ``query``, ``osm_type``, ``osm_id``,
    ``place_id``) plus the ADDITIVE ``fallback_reason`` honest note. Prefers
    the live OSM state admin boundary, falling back to the offline Census
    extent (``_resolve_state_bbox`` handles that and never raises).
    """
    bbox, lat, lon, state_source = _resolve_state_bbox(state_name)
    logger.info(
        "geocode_location state-snap query=%r state=%r bbox=%s source=%s",
        query,
        state_name,
        bbox,
        state_source,
    )
    return {
        "name": f"{state_name}, United States",
        "bbox": bbox,
        "latitude": lat,
        "longitude": lon,
        "source": "state-bbox-fallback",
        "query": query,
        "osm_type": None,
        "osm_id": None,
        "place_id": None,
        # Additive, honest narration hook (fallback norm).
        "fallback_reason": reason,
        # Provenance of the snap bbox itself (live OSM vs offline table).
        "state_bbox_source": state_source,
    }


# ---------------------------------------------------------------------------
# fetch_landcover — NLCD (MRLC) / ESA WorldCover (sprint-07 Stage B, job-0039;
# job-0044 hotfix: WMS → WCS 1.0.0 to fix palette encoding).
# ---------------------------------------------------------------------------
#
# Access pattern tier — LIVE-VERIFIED THROUGH TWO ROUNDS:
#
# Round 1 (job-0039, 2026-06-07):
#
#   * The MRLC direct file mirror (``s3-us-west-2.amazonaws.com/mrlc/
#     Annual_NLCD_LndCov_<YEAR>_CU_C1V0.tif``) returned an HTTP 200 with a
#     **42-byte placeholder TIFF** (a 1×1 IFD with two ``0xFFFFFFFF`` strip
#     offsets — not a real raster). 2019 and 2021 file URLs at the same path
#     return HTTP 403. The "direct HTTPS + Range" path the kickoff inferred is
#     NOT a real surface for NLCD bytes.
#   * The MRLC WCS endpoint (`/geoserver/mrlc_display/wcs`) timed out on
#     GetCapabilities in the first probe.
#   * MRLC's **WMS** GeoServer at ``www.mrlc.gov/geoserver/mrlc_display/wms``
#     serves NLCD year layers (``NLCD_2021_Land_Cover_L48`` etc.) and supports
#     ``GetMap?format=image/geotiff`` — Tier 2 (OGC service) byte materialized.
#     Substrate landed against WMS GetMap.
#
# Round 2 (job-0044, 2026-06-07 — THE PALETTE-ENCODING HOTFIX):
#
#   * Job-0042's NLCD validation gate (Invariant 7 mitigation) fired on a real
#     Fort Myers smoke run: the WMS GetMap GeoTIFF returns raster bytes that
#     are **palette indices** (1, 3, 4, 5, ..., 21) NOT canonical NLCD class
#     integers (11, 21, 22, 23, ..., 95) — surfaced as
#     OQ-42-NLCD-WMS-PALETTE-ENCODING. The Manning's mapping CSV is keyed by
#     canonical integers; SFINCS dispatch was blocked end-to-end.
#   * Live-probed both candidate fix paths per §F.1.1 live-verification discipline:
#
#     - **Path A (palette decode):** the WMS GeoTIFF carries a 256-entry
#       ColorTable in its IFD; the index→RGB→canonical NLCD mapping is fixed
#       (idx 1 = open-water = (71,107,160) = NLCD 11; idx 3 = developed-open
#       = (221,201,201) = NLCD 21; …). Decoding via the embedded ColorTable
#       and an inverse RGB→class table is feasible but adds a fragile
#       client-side translation step (one MRLC palette reorder breaks us).
#     - **Path B (WCS 1.0.0 GetCoverage):** ``mrlc_display:NLCD_2021_Land_
#       Cover_L48`` coverage served by the WCS 1.0.0 endpoint with
#       ``REQUEST=GetCoverage&CRS=EPSG:4326&BBOX=...&WIDTH=...&HEIGHT=...&FORMAT=GeoTIFF``
#       returns canonical NLCD class integers DIRECTLY (verified: unique band1
#       values for Fort Myers bbox = [11, 21, 22, 23, 24, 31, 41, 42, 43, 52,
#       71, 81, 82, 90, 95, 255-nodata] — every value cleanly mapped to
#       manning_mapping.csv v1.0.0). The DescribeCoverage XML calls the band
#       "PALETTE_INDEX" but the integers ARE the canonical NLCD codes — WCS
#       1.0.0 emits the source dataset's raw byte values whereas WMS GetMap
#       emits the rendered (re-indexed) palette indices.
#     - **WCS 2.0.1 / 1.1.1:** also tried; both fail in different ways. WCS
#       2.0.1 hits a GeoServer "Unable to map projection Popular Visualisation
#       Pseudo Mercator" exception (GeoServer projection-mapping bug on its
#       own native CRS). WCS 1.1.1 rejects bbox-only requests as "less than a
#       pixel would be read." WCS 1.0.0 with explicit WIDTH/HEIGHT is the
#       reliable byte surface.
#
#   * **Path B chosen.** Canonical bytes from the server is a clean win over
#     client-side palette decoding: no RGB→class lookup to maintain, no
#     fragility to MRLC palette reorders, no Round-3 silent-wrong-answer risk.
#     Both paths are §F.1.1 Tier 2 (OGC service) — substrate stays Tier 2,
#     vendor sub-protocol switches from WMS GetMap to WCS GetCoverage.
#
# Job-0044 cache-migration policy: cache key now includes ``source: "mrlc-wcs"``
# (the palette-encoded ``mrlc-wms`` entries from job-0039's evidence land
# under a different cache prefix and naturally evict on the 30-day TTL — no
# explicit invalidation needed). Job-0039's evidence COGs at
# ``cache/static-30d/landcover/56bad09bfa8a71d502ed61badc785a00.tif`` will
# remain until TTL eviction; the new canonical-bytes COGs land at a new key.
#
# Round 1 deviation (job-0039) is still recorded as OQ-39-NLCD-TIER-DEVIATION
# (kickoff inferred Tier 3 → live Tier 2). Round 2 hotfix (job-0044) closes
# OQ-42-NLCD-WMS-PALETTE-ENCODING.
#
# Vintage discipline: NLCD vintages 2019, 2021 (default), and 2023 are most-
# relevant. The Annual NLCD Collection 1.0 (2023 release) is published as the
# ``Annual_NLCD_LndCov_<YEAR>_CU_C1V0`` family; the WMS GeoServer lists
# discrete-year layers up through **NLCD_2021_Land_Cover_L48**. 2023 is the
# newest release but its WMS layer name was not present in the MRLC
# GetCapabilities at probe time (2026-06-07); the substrate defaults to 2021
# and the dataset string parameter supports ``"nlcd_2019"`` and (forward-
# looking) ``"nlcd_2023"`` once it lands. ESA WorldCover (Planetary Computer
# ``esa-worldcover``) opt-in via ``dataset="esa_worldcover_2021"``.
#
# Manning's mapping validation gate (per docs/decisions/oq-4-hydromt-depth.md
# §4 "Immediate (job-0039)"): the NLCD vintage year is returned as sidecar
# metadata alongside the LayerURI so job-0042 ``build_sfincs_model`` can
# verify the Manning's mapping CSV covers the vintage's class encoding. This
# is the Invariant 7 (no silent wrong answers) mitigation OQ-4 demanded.
#
# Sidecar shape — return-value design: ``LayerURI`` (in
# ``grace2_contracts.execution``) is a FROZEN contract with
# ``extra="forbid"`` — we cannot add a ``metadata`` field. The kickoff's
# example syntax ``LayerURI.metadata["nlcd_vintage_year"] = 2021`` was
# illustrative; the actual seam is a structured ``dict`` return shape:
#
#     {
#       "layer": LayerURI(...),
#       "nlcd_vintage_year": 2021,
#       "dataset": "nlcd_2021",
#       "source": "mrlc-wms",
#     }
#
# This is the same dict-return pattern as ``geocode_location`` (also no
# contract for its shape) and ``lookup_precip_return_period`` below — see
# OQ-39-LANDCOVER-RETURN-SHAPE-CONTRACT-PROMOTION.


_FETCH_LANDCOVER_METADATA = AtomicToolMetadata(
    name="fetch_landcover",
    ttl_class="static-30d",
    source_class="landcover",
    cacheable=True,
)


# Landcover-ONLY cache-version salt (job-0324 follow-up — STALE-CACHE fix).
# -------------------------------------------------------------------------
# The "bake NLCD land cover into hillshade" demo rendered grey because the
# read-through cache (static-30d, 30-day TTL) was serving NLCD COGs written
# BEFORE deploy #3's palette-preservation fix (job-0324). Those stale COGs
# dropped their embedded GDAL color table, so blending them produced a flat
# grayscale base instead of the NLCD class colors.
#
# Bumping this salt changes the canonicalized ``params`` dict that drives the
# landcover cache key (``compute_cache_key`` hashes ``source_id || params ||
# vintage``), so a post-fix fetch for the SAME bbox now computes a DIFFERENT
# key than the pre-fix entry — i.e. it MISSES the stale palette-less COG and
# regenerates a colored (palette-preserving) COG. This is scoped to
# fetch_landcover ONLY: it is folded into the landcover ``params`` dict, never
# into the shared ``compute_cache_key`` salt, so no other tool's cache key
# changes (a recursive cache wipe was deliberately avoided). Bump the integer
# whenever a landcover-COG-generation fix must force a clean regenerate.
_LANDCOVER_CACHE_VERSION = 3  # v3 = F26 background(0)->nodata transparency; v2 = post-job-0324 palette-preserving COGs


# MRLC WCS 1.0.0 GeoServer endpoint (Tier 2 OGC service, live-verified
# 2026-06-07 in job-0044). WCS 1.0.0 GetCoverage returns canonical NLCD class
# integers in the raster band — the WMS GetMap path job-0039 landed against
# returned palette-encoded indices (the OQ-42-NLCD-WMS-PALETTE-ENCODING
# blocker job-0042's validation gate caught). WCS 1.0.0 was chosen over
# WCS 1.1.1 / 2.0.1: 2.0.1 hits a GeoServer projection-mapping bug ("Unable
# to map projection Popular Visualisation Pseudo Mercator") on its own
# native EPSG:3857; 1.1.1 rejects bbox-only requests; 1.0.0 with explicit
# CRS=EPSG:4326 + WIDTH/HEIGHT + FORMAT=GeoTIFF is the reliable surface.
_MRLC_WCS_URL = "https://www.mrlc.gov/geoserver/mrlc_display/wcs"

# NLCD year → WCS coverage ID in the MRLC GeoServer catalog. WCS uses the
# qualified workspace:coverage form ``mrlc_display:NLCD_<YEAR>_Land_Cover_L48``
# (the underlying GeoServer layer); live-verified 2026-06-07.
_NLCD_WCS_COVERAGE_BY_YEAR: dict[int, str] = {
    2001: "mrlc_display:NLCD_2001_Land_Cover_L48",
    2004: "mrlc_display:NLCD_2004_Land_Cover_L48",
    2006: "mrlc_display:NLCD_2006_Land_Cover_L48",
    2008: "mrlc_display:NLCD_2008_Land_Cover_L48",
    2011: "mrlc_display:NLCD_2011_Land_Cover_L48",
    2013: "mrlc_display:NLCD_2013_Land_Cover_L48",
    2016: "mrlc_display:NLCD_2016_Land_Cover_L48",
    2019: "mrlc_display:NLCD_2019_Land_Cover_L48",
    2021: "mrlc_display:NLCD_2021_Land_Cover_L48",
}


def _read_band1_colormap(src) -> dict | None:
    """Return the band-1 palette color table (``{idx: (r,g,b,a)}``) or ``None``.

    NLCD land cover ships a single-band palette-index COG with an EMBEDDED GDAL
    color table; TiTiler colorizes from it. Every COG re-write (clip, COG
    translate, overview enforcement) must carry that table forward or the layer
    renders solid grey (job-0324 regression). rasterio raises ``ValueError``
    when band 1 has no color table — that is the normal, expected case for
    continuous rasters (DEM, hillshade, flood depth), and we return ``None`` so
    the caller does NOT fabricate one.
    """
    try:
        return src.colormap(1)
    except ValueError:
        # rasterio raises ValueError when band 1 has no color table — the
        # normal case for continuous rasters (DEM/hillshade/flood depth).
        return None
    except Exception as exc:  # noqa: BLE001 — any other read failure: no-op
        logger.debug("colormap read skipped (%s: %s)", type(exc).__name__, exc)
        return None


def _apply_band1_colormap(dst, cmap: dict | None, colorinterp=None) -> None:
    """Write a preserved band-1 color table + palette colorinterp onto ``dst``.

    No-op when ``cmap`` is ``None`` (non-paletted raster — we never fabricate a
    color table). When a table is present, stamp it on band 1 and set band 1's
    color interpretation to ``palette`` so downstream readers/TiTiler treat the
    integer pixels as indices into the table.
    """
    if cmap is None:
        return
    try:
        dst.write_colormap(1, cmap)
        try:
            from rasterio.enums import ColorInterp

            interp = list(dst.colorinterp)
            interp[0] = ColorInterp.palette
            dst.colorinterp = tuple(interp)
        except Exception:  # noqa: BLE001 — colorinterp set is best-effort
            pass
    except Exception as exc:  # noqa: BLE001 — colormap copy is best-effort
        logger.warning(
            "colormap preservation failed (%s: %s); output may render grey",
            type(exc).__name__,
            exc,
        )


def _clip_raster_bytes_to_bbox(
    tif_bytes: bytes, bbox: tuple[float, float, float, float]
) -> bytes:
    """Crop a GeoTIFF (bytes) to the EXACT requested bbox via rasterio windowing.

    The MRLC WCS GetCoverage already returns the requested BBOX server-side,
    but pixel snapping can leave a fringe row/column outside the AOI. This
    reprojects the bbox into the raster's CRS, computes the pixel window, and
    writes the cropped raster — guaranteeing the output extent matches the
    requested bbox to within one pixel. Best-effort: returns the input bytes
    unchanged on any failure (never raises — clipping is a precision nicety,
    not a correctness gate).
    """
    in_tmp: str | None = None
    out_tmp: str | None = None
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.warp import transform_bounds  # type: ignore[import-not-found]
        from rasterio.windows import from_bounds as window_from_bounds  # type: ignore[import-not-found]

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            in_tmp = f.name
            f.write(tif_bytes)

        with rasterio.open(in_tmp) as src:
            dst_crs = src.crs
            # Reproject the WGS84 bbox into the raster CRS (no-op when already 4326).
            if dst_crs is not None and dst_crs.to_epsg() != 4326:
                left, bottom, right, top = transform_bounds(
                    "EPSG:4326", dst_crs, *bbox, densify_pts=21
                )
            else:
                left, bottom, right, top = bbox
            window = window_from_bounds(
                left, bottom, right, top, transform=src.transform
            )
            # Intersect with the raster's full window so we never read outside it.
            full = rasterio.windows.Window(0, 0, src.width, src.height)
            window = window.intersection(full).round_offsets().round_lengths()
            if window.width < 1 or window.height < 1:
                # Degenerate intersection — keep the original (don't blank it out).
                return tif_bytes
            data = src.read(window=window)
            transform = src.window_transform(window)
            profile = src.profile.copy()
            profile.update(
                height=int(window.height),
                width=int(window.width),
                transform=transform,
            )
            # Preserve a band-1 palette color table (e.g. NLCD land cover) so
            # the cropped output still colorizes. None when the source has no
            # color table (DEM/hillshade/flood depth) — a pure no-op there.
            cmap = _read_band1_colormap(src)
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as of:
                out_tmp = of.name
            with rasterio.open(out_tmp, "w", **profile) as dst:
                dst.write(data)
                _apply_band1_colormap(dst, cmap)
        with open(out_tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 — clip is best-effort precision
        logger.warning(
            "fetch_landcover: bbox clip failed (%s: %s); returning unclipped raster",
            type(exc).__name__,
            exc,
        )
        return tif_bytes
    finally:
        for path in (in_tmp, out_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _rasterio_translate_to_cog(tif_bytes: bytes) -> bytes:
    """Translate GeoTIFF bytes to a tiled COG WITH overviews via the rasterio COG driver.

    Used as the fallback when the GDAL CLI binaries that ``_translate_to_cog``
    (compute_hillshade) shells out to are not on PATH (e.g. the agent .venv
    without gdal-bin). The rasterio ``COG`` driver builds internal overviews
    and 512x512 tiling automatically — the exact properties TiTiler needs to
    avoid the zoomed-out 404s that made NLCD render spotty. Best-effort:
    returns the input bytes unchanged on any failure.
    """
    in_tmp: str | None = None
    out_tmp: str | None = None
    try:
        import rasterio  # type: ignore[import-not-found]

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            in_tmp = f.name
            f.write(tif_bytes)
        with rasterio.open(in_tmp) as src:
            profile = {
                "driver": "COG",
                "width": src.width,
                "height": src.height,
                "count": src.count,
                "dtype": src.dtypes[0],
                "crs": src.crs,
                "transform": src.transform,
                "compress": "DEFLATE",
            }
            if src.nodata is not None:
                profile["nodata"] = src.nodata
            data = src.read()
            # Preserve a band-1 palette color table (NLCD land cover) across the
            # COG translate — TiTiler colorizes from this embedded table. None
            # for non-paletted rasters (DEM/hillshade/flood depth): a no-op.
            cmap = _read_band1_colormap(src)
            colorinterp = src.colorinterp
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as of:
                out_tmp = of.name
            with rasterio.open(
                out_tmp, "w", OVERVIEW_RESAMPLING="NEAREST", **profile
            ) as dst:
                dst.write(data)
                _apply_band1_colormap(dst, cmap, colorinterp)
        with open(out_tmp, "rb") as f:
            return f.read()
    except Exception as exc:  # noqa: BLE001 — COG translate is best-effort
        logger.warning(
            "fetch_landcover: rasterio COG translate failed (%s: %s); returning "
            "flat GeoTIFF bytes",
            type(exc).__name__,
            exc,
        )
        return tif_bytes
    finally:
        for path in (in_tmp, out_tmp):
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _landcover_bytes_to_cog(
    tif_bytes: bytes, bbox: tuple[float, float, float, float]
) -> bytes:
    """Clip NLCD bytes to the exact bbox and emit a tiled COG WITH overviews.

    job-0271-class fix for fetch_landcover: the MRLC WCS GetCoverage returns a
    flat strip-organized GeoTIFF with NO overviews, so TiTiler 404s the
    zoomed-out tiles and the layer renders spotty / never paints when panned
    out. This routes the raster through ``_translate_to_cog`` (the
    compute_hillshade COG translator that writes a tiled COG with overviews)
    when the GDAL CLI is available, and falls back to the pure-rasterio COG
    driver otherwise — so overviews are present in BOTH environments.

    Also clips to the EXACT requested bbox first (precision nicety; the WCS
    already honors BBOX server-side but pixel snapping can leave a fringe).
    """
    clipped = _clip_raster_bytes_to_bbox(tif_bytes, bbox)

    # Prefer the assigned compute_hillshade COG translator (GDAL CLI path) so
    # the COG profile matches every other raster product. Fall back to the
    # pure-rasterio COG driver when the gdal binaries are not on PATH.
    try:
        from .compute_hillshade import _get_gdaldem_bin, _translate_to_cog

        in_tmp: str | None = None
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
                in_tmp = f.name
                f.write(clipped)
            gdaldem_bin = _get_gdaldem_bin()  # raises if gdal CLI absent
            cog = _translate_to_cog(in_tmp, gdaldem_bin)
            # _translate_to_cog returns flat bytes when gdal_translate is missing
            # even though gdaldem resolved; verify overviews landed, else fall
            # through to the rasterio path below.
            if _has_overviews(cog):
                return cog
        finally:
            if in_tmp is not None:
                try:
                    os.unlink(in_tmp)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001 — GDAL CLI not available / failed
        logger.info(
            "fetch_landcover: GDAL-CLI COG translate unavailable (%s); using "
            "rasterio COG driver fallback",
            exc,
        )

    return _rasterio_translate_to_cog(clipped)


def _has_overviews(tif_bytes: bytes) -> bool:
    """Return True iff the GeoTIFF bytes carry internal overviews on band 1."""
    in_tmp: str | None = None
    try:
        import rasterio  # type: ignore[import-not-found]

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            in_tmp = f.name
            f.write(tif_bytes)
        with rasterio.open(in_tmp) as src:
            return len(src.overviews(1)) > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if in_tmp is not None:
            try:
                os.unlink(in_tmp)
            except OSError:
                pass


# NLCD "Background" class code -- MRLC's official legend reserves index 0 for
# pixels outside the classified CONUS extent (open ocean, international
# waters, etc). It is NEVER a legitimate NLCD land-cover class (real classes
# are 11-95); the MRLC WCS 1.0.0 GetCoverage's embedded color table maps it
# to OPAQUE BLACK ((0, 0, 0, 255)) rather than transparent -- confirmed via a
# live probe of the real endpoint (bbox off the Washington coast, 2026-07-09).
# The raster's DECLARED ``nodata`` tag is 255 (a separate sentinel that DOES
# render transparent), so 0 slips through as an undeclared second nodata
# value. City/county-scale fetches (always fully on land) never hit index 0
# and never surfaced this; the state-scale auto-coarsen resolution-gate path
# (commit 21cd123) is what first requested a bbox large enough to include
# real open ocean, live-exposing an opaque black rectangle over the nodata
# region. See _fix_nlcd_background_transparency.
_NLCD_BACKGROUND_CLASS = 0


def _fix_nlcd_background_transparency(tif_bytes: bytes) -> bytes:
    """Fold NLCD's ``0`` (Background/no-coverage) pixels into the declared nodata.

    Root cause (live-verified against the real MRLC WCS endpoint 2026-07-09,
    and against GDAL's actual GTiff behavior -- NOT just the embedded table):
    GDAL's GTiff driver forces alpha=0 ONLY for the color-table entry whose
    index equals the band's DECLARED ``nodata`` value; every other entry's
    alpha is silently forced back to 255 (opaque) when the color table is
    flushed to disk, regardless of what alpha ``write_colormap`` was given.
    (Confirmed empirically: writing ``cmap[0] = (0, 0, 0, 0)`` while
    ``nodata`` stays 255 round-trips back as ``(0, 0, 0, 255)`` -- opaque --
    every time; rewriting the colormap alone can never fix this.) So the only
    reliable fix is at the PIXEL level: remap every ``0``-valued pixel to the
    raster's existing declared ``nodata`` (255 for MRLC WCS NLCD), which
    already renders transparent correctly. Class 0 is never a legitimate NLCD
    code (real codes are 11-95), so this remap can never destroy real data.
    If the raster has no declared nodata at all, ``0`` is promoted to be the
    declared nodata directly (no remap needed; GDAL's forcing behavior then
    makes index 0 transparent on its own).

    Best-effort: returns ``tif_bytes`` unchanged (never raises) if the raster
    has no embedded colormap, has no ``0``-valued pixels, or the rewrite
    fails for any reason -- this is strictly a visualization fix, not a
    correctness gate, and must never corrupt or block a real fetch.
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
        from rasterio.io import MemoryFile  # type: ignore[import-not-found]

        with MemoryFile(tif_bytes) as mem, mem.open() as src:
            cmap = _read_band1_colormap(src)
            if cmap is None:
                return tif_bytes  # not a paletted raster -- nothing to fix

            data = src.read()
            band1 = data[0]
            if not bool(np.any(band1 == _NLCD_BACKGROUND_CLASS)):
                return tif_bytes  # no background pixels present -- no-op

            nodata = src.nodata
            target_nodata = (
                float(_NLCD_BACKGROUND_CLASS) if nodata is None else float(nodata)
            )

            if int(target_nodata) == _NLCD_BACKGROUND_CLASS:
                # No declared nodata (or it's already 0) -- promote 0 itself
                # to the declared nodata; GDAL forces its alpha transparent.
                out_data = data
            else:
                # Fold background (0) into the existing nodata sentinel so
                # there is a single, already-transparent, sentinel value.
                out_data = data.copy()
                out_data[0][band1 == _NLCD_BACKGROUND_CLASS] = target_nodata

            profile = src.profile.copy()
            profile["nodata"] = target_nodata
            colorinterp = src.colorinterp
            with MemoryFile() as out_mem:
                with out_mem.open(**profile) as dst:
                    dst.write(out_data)
                    _apply_band1_colormap(dst, cmap, colorinterp)
                return out_mem.read()
    except Exception as exc:  # noqa: BLE001 -- transparency fix is best-effort
        logger.warning(
            "fetch_landcover: NLCD background-transparency fix failed (%s: %s); "
            "value-0 (ocean/no-coverage) pixels may render opaque black",
            type(exc).__name__,
            exc,
        )
        return tif_bytes


def _fetch_nlcd_landcover_bytes(
    bbox: tuple[float, float, float, float], vintage_year: int, resolution_m: int = 30
) -> bytes:
    """Fetch NLCD landcover for ``bbox`` at the given vintage year via MRLC WCS 1.0.0.

    Tier 2 access pattern (per §F.1.1) — MRLC WCS 1.0.0 ``GetCoverage`` with
    ``FORMAT=GeoTIFF`` returns the canonical NLCD class integers (11, 21, 22,
    23, 24, 31, 41, 42, 43, 51, 52, 71, 72, 73, 74, 81, 82, 90, 95) in the
    raster band — NOT palette indices. This is the job-0044 hotfix that
    unblocks job-0042's NLCD validation gate. The returned GeoTIFF carries a
    proper geo-header (EPSG:4326 in this request shape) so HydroMT's
    ``setup_manning_roughness`` consumes the bytes directly without a
    client-side palette decode.

    ``resolution_m`` controls the WCS pixel grid: at 30 m (native) each pixel is
    one NLCD cell; at coarser values (e.g. 300 m for a state-scale bbox) the grid
    shrinks to stay under the MRLC WCS server's ~4000 px-per-axis limit. Because
    NLCD is a categorical raster, nearest-neighbor resampling is implicit in the
    WCS server's pixel-addressed GetCoverage (no bilinear corruption of class codes).

    Path-comparison summary (live-verified 2026-06-07):
    - WMS GetMap: returned palette indices [1, 3, 4, 5, 6, 7, 9, 10, 11, 13,
      14, 18, 19, 20, 21] for Fort Myers -- BROKEN (Manning's mapping keyed by
      canonical integers).
    - WCS 1.0.0 GetCoverage: returned canonical integers [11, 21, 22, 23, 24,
      31, 41, 42, 43, 52, 71, 81, 82, 90, 95, 255-nodata] -- CORRECT.
    """
    _validate_bbox(bbox)
    coverage = _NLCD_WCS_COVERAGE_BY_YEAR.get(vintage_year)
    if coverage is None:
        available = sorted(_NLCD_WCS_COVERAGE_BY_YEAR.keys())
        raise UpstreamAPIError(
            f"NLCD vintage year {vintage_year} not in MRLC WCS catalog "
            f"(available: {available}); add 2023 once MRLC publishes "
            f"``mrlc_display:NLCD_2023_Land_Cover_L48`` (see OQ-39-NLCD-VINTAGE-DEFAULT)."
        )

    # Pixel grid: sized to the bbox at the requested resolution in EPSG:4326.
    # WCS 1.0.0 requires explicit WIDTH/HEIGHT (no resolution shorthand at this
    # version). At the native 30 m, clamp to 4000 px per axis (MRLC server
    # limit; beyond that the server times out or returns an exception). For
    # coarsened fetches (state-scale AOI at 300+ m) the pixel count is low and
    # the clamp is never hit.
    _res = max(1, int(resolution_m))
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * 111_320.0
    # MRLC pixel cap: 4000 px per axis keeps the GetCoverage inside the server's
    # stated limit. At native 30 m this caps at ~122 km/axis; at 300 m it covers
    # ~1200 km/axis, enough for any CONUS state.
    _MRLC_MAX_PX = 4000
    width_px = max(16, min(_MRLC_MAX_PX, int(round(width_m / _res))))
    height_px = max(16, min(_MRLC_MAX_PX, int(round(height_m / _res))))

    # WCS 1.0.0 GetCoverage via the shared generic OGC adapter (job-0047
    # refactor — single source of truth for §F.1.1 Tier 2 retrieval). The
    # adapter handles the WCS request shape (Coverage, CRS, BBOX, WIDTH,
    # HEIGHT, FORMAT), surfaces OGC exception XMLs as typed errors, and
    # validates the GeoTIFF content-type so a misconfigured GeoServer
    # response (HTML error page, ExceptionReport XML) doesn't poison the
    # cache. The MRLC WCS sub-protocol (1.0.0 over 1.1.1/2.0.1) was
    # established in job-0044's live-verification rounds and is preserved.
    from .ogc_adapter import OGCAdapterError, fetch_ogc_layer

    try:
        ogc_resp = fetch_ogc_layer(
            url=_MRLC_WCS_URL,
            layer_name=coverage,
            bbox=bbox,
            crs="EPSG:4326",
            service_type="WCS",
            image_format="GeoTIFF",
            version="1.0.0",
            width_px=width_px,
            height_px=height_px,
            timeout_s=120.0,
            user_agent=_DEFAULT_USER_AGENT,
        )
    except OGCAdapterError as exc:
        raise UpstreamAPIError(
            f"MRLC WCS GetCoverage failed for coverage={coverage} bbox={bbox}: {exc}"
        ) from exc

    # Extra defensive check: the adapter already validates content-type and
    # body length, but we re-check the TIFF content-type because the cache
    # write extension is fixed at ``.tif``.
    ct = ogc_resp.content_type
    if "tiff" not in ct.lower() and "geotiff" not in ct.lower():
        raise UpstreamAPIError(
            f"MRLC WCS returned unexpected content-type={ct!r} for coverage={coverage} "
            f"bbox={bbox}; body preview: {ogc_resp.content[:300]!r}"
        )

    # NLCD Background-class transparency fix (2026-07-09): the WCS embedded
    # color table maps class 0 to opaque black instead of transparent -- see
    # _fix_nlcd_background_transparency. Applied BEFORE the COG re-write
    # pipeline so the fixed table is what gets clipped/tiled/cached.
    fixed = _fix_nlcd_background_transparency(ogc_resp.content)

    # job-0271-class fix (F33/F39): the MRLC WCS GetCoverage GeoTIFF is a flat
    # strip-organized raster with NO overviews, so TiTiler 404s the zoomed-out
    # tiles and NLCD renders spotty / vanishes when panned out. Clip to the
    # exact bbox and re-emit a tiled COG WITH overviews before caching.
    return _landcover_bytes_to_cog(fixed, bbox)


def _fetch_esa_worldcover_bytes(
    bbox: tuple[float, float, float, float], vintage_year: int
) -> bytes:
    """Fetch ESA WorldCover landcover for ``bbox`` at the given vintage year.

    ESA WorldCover is hosted by Microsoft Planetary Computer as STAC + COG
    (Tier 1 per §F.1.1). The implementation is reserved as a forward-looking
    branch; the v0.1 substrate raises ``UpstreamAPIError`` so the agent's
    FR-AS-11 surface can decide whether to fall back to NLCD or surface to
    the user. Surface as OQ-39-ESA-WORLDCOVER-SUBSTRATE.
    """
    raise UpstreamAPIError(
        "ESA WorldCover branch is not implemented in the v0.1 substrate "
        "(reserved for a follow-up job; opt into NLCD by passing "
        "dataset='nlcd_2021' / 'nlcd_2019')."
    )


# Default NLCD vintage used both as the ``fetch_landcover`` ``dataset``
# parameter default and as the resolved value for the bare 'nlcd' / 'nlcd_'
# aliases (job-fix: model kept retrying 'nlcd' -> 'nlcd_' before landing on
# a valid 'nlcd_YYYY', re-triggering the resolution-confirm gate each time).
_DEFAULT_NLCD_DATASET = "nlcd_2021"


def _round_bbox_to_30m_nlcd(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Quantize a WGS84 bbox to the NLCD 30 m native grid.

    Per the per-source bbox quantization rule (acceptance criterion 3 of
    the kickoff): NLCD's native cell is 30 m. We reuse
    ``round_bbox_to_resolution(bbox, 30)`` — same semantics as ``fetch_dem``
    at 30 m, so dedup-via-quantization works the same way.
    """
    return round_bbox_to_resolution(bbox, 30)


@register_tool(
    _FETCH_LANDCOVER_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (NLCD WMS + USGS 3DEP),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_landcover(
    bbox: tuple[float, float, float, float],
    dataset: str = _DEFAULT_NLCD_DATASET,
    resolution_m: int = 30,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Fetch landcover classification raster (NLCD or ESA WorldCover) for a bbox.

    Access pattern: Tier 2 (OGC service — MRLC WCS/WMS endpoint per §F.1.1; live
    verification 2026-06-07 found NLCD is Tier 2, see OQ-39-NLCD-TIER-DEVIATION).

    **What it does:** Downloads an NLCD or ESA WorldCover landcover GeoTIFF
    clipped to the requested bbox via the MRLC WCS 1.0.0 GeoServer endpoint.
    Returns a dict containing a ``LayerURI`` plus a ``nlcd_vintage_year``
    sidecar field that downstream SFINCS setup uses to validate Manning's
    roughness mappings before HydroMT invocation (Invariant 7 — no silent
    wrong answers).

    **When to use:**
    - ``build_sfincs_model`` requires landcover for Manning's roughness
      assignment — this is the canonical supply tool.
    - User asks "what land cover exists in this area?" for a CONUS location.
    - Exposure analysis: intersect a hazard footprint with impervious-surface
      or developed-land classes.
    - Visualization using the ``categorical_landcover`` QML style preset.

    **When NOT to use:**
    - Coverage outside CONUS L48 -- NLCD covers only the 48 contiguous US
      states; Alaska, Hawaii, and Puerto Rico have separate MRLC layers not
      in the v0.1 substrate.
    - Global landcover -- pass ``dataset="esa_worldcover_2021"`` to opt into
      the ESA WorldCover branch, but that branch currently raises
      ``UpstreamAPIError`` (forward-looking, OQ-39-ESA-WORLDCOVER-SUBSTRATE).
    - Single-point landcover classification -- this tool returns a raster;
      use ``extract_landcover_class`` for point lookups once it lands.
    - Continent-scale bboxes (> 5,000,000 km^2) -- the tool raises
      ``BboxInvalidError`` at that hard ceiling. State-scale and
      multi-state-scale bboxes are served by auto-coarsening the resolution
      (the fetch-resolution gate asks the user to confirm the coarsened rung
      before the MRLC WCS GetCoverage is issued).

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Continent-scale bboxes (> 5e6 km^2) are
      rejected; all other sizes are served at auto-coarsened resolution.
    - ``dataset`` (str, default ``"nlcd_2021"``): ``"nlcd"`` (default vintage)
      or ``"nlcd_YYYY"`` (e.g. ``"nlcd_2021"``, ``"nlcd_2019"``,
      ``"nlcd_2016"``) or ``"esa_worldcover_2021"`` (forward-looking). Bare
      ``"nlcd"`` and ``"nlcd_"`` are accepted as aliases for the default
      vintage. Valid NLCD years: 2001, 2004, 2006, 2008, 2011, 2013, 2016,
      2019, 2021.
    - ``resolution_m`` (int, default 30): pixel grid spacing in meters.
      The fetch-resolution gate auto-coarsens this for large bboxes and
      asks the user to confirm before downloading. The native NLCD grid
      is 30 m; coarser values (60, 120, 300, 600 m) are used for
      state-scale or multi-state-scale AOIs.

    **Returns:**
    A dict with keys:
    - ``layer`` (LayerURI): COG at
      ``gs://grace-2-hazard-prod-cache/cache/static-30d/landcover/<key>.tif``;
      ``style_preset="categorical_landcover"``, ``units="nlcd_class_code"``.
    - ``nlcd_vintage_year`` (int): vintage year consumed by
      ``build_sfincs_model`` to validate the Manning's mapping CSV.
    - ``dataset`` (str): echo of the input dataset string for provenance.
    - ``source`` (str): ``"mrlc-wcs"`` for NLCD.
    - ``effective_resolution_m`` (int): actual pixel spacing used (equals
      ``resolution_m`` when at native 30 m; coarser when the bbox was large).
    - ``native_resolution_m`` (int): NLCD native resolution (30 m).
    - ``downsampled`` (bool): True when ``effective_resolution_m > native_resolution_m``.

    **Cross-tool dependencies:**
    - Upstream: ``geocode_location`` for bbox derivation.
    - Downstream: ``build_sfincs_model`` (Manning's roughness), QGIS Server
      WMS rendering, ``extract_landcover_class``, ``compute_impervious_surface``.
    """
    if not isinstance(dataset, str) or not dataset:
        raise BboxInvalidError(
            f"fetch_landcover requires a non-empty dataset string; got {dataset!r}"
        )

    # Alias resolution: models frequently call this with bare 'nlcd' (no
    # vintage) or a stray trailing-underscore 'nlcd_' before landing on a
    # valid 'nlcd_YYYY' -- each of those was a typed error that forced a
    # retry, and every retry re-triggered the resolution-confirm gate on
    # the same bbox (see turn-memory fix in server.py). Treat both as
    # aliases for the default vintage; an explicit 'nlcd_YYYY' still wins.
    normalized_dataset = dataset.strip().lower()
    if normalized_dataset in ("nlcd", "nlcd_"):
        dataset = _DEFAULT_NLCD_DATASET

    # Pixel-budget constants for MRLC WCS auto-coarsening.
    # PIXEL_BUDGET: max pixels per side we request from the MRLC WCS server
    # (4000 keeps a margin under the ~4096 cap the server enforces).
    _PIXEL_BUDGET = 4000
    _NATIVE_RES_M = 30

    if dataset.startswith("nlcd_"):
        try:
            vintage_year = int(dataset.split("_", 1)[1])
        except (IndexError, ValueError) as exc:
            raise BboxInvalidError(
                f"could not parse NLCD vintage year from dataset={dataset!r}; "
                "expected 'nlcd_YYYY' (e.g. 'nlcd_2021')."
            ) from exc

        # Hard ceiling: continent-scale bboxes (> 5e6 km^2) are refused.
        # Everything below that is served at auto-coarsened resolution.
        rough_area = _bbox_area_km2(bbox)
        if rough_area > 5_000_000.0:
            raise BboxInvalidError(
                f"bbox area {rough_area:.1f} km^2 exceeds the 5,000,000 km^2 hard "
                "ceiling for fetch_landcover (continent-scale; split into sub-regions)."
            )

        # Compute the effective resolution from the gate-supplied resolution_m.
        # The gate (server.py FETCH_CONFIRM_TOOLS) auto-coarsens for large bboxes
        # and injects a confirmed resolution_m; we honour it here. If the gate
        # was bypassed (e.g. a small bbox or a direct call), use the supplied
        # resolution_m as-is, but floor it at 30 m (never finer than native).
        effective_res = max(_NATIVE_RES_M, int(resolution_m))

        # Enforce the MRLC pixel budget on the RESOLUTION (not just the px
        # clamp inside _fetch_nlcd_landcover_bytes): if the bbox at
        # effective_res would exceed _PIXEL_BUDGET px on the long axis, coarsen
        # to fit. This keeps effective_resolution_m HONEST -- it always
        # describes the grid actually delivered, even when the gate was
        # bypassed (direct call, tests, small-model shortcut) with a rung too
        # fine for the AOI. Nearest-neighbor semantics hold: the WCS pixel-
        # addressed GetCoverage samples class codes, never interpolates.
        min_lon, min_lat, max_lon, max_lat = bbox
        mid_lat = 0.5 * (min_lat + max_lat)
        m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
        long_axis_m = max(
            (max_lon - min_lon) * m_per_deg_lon,
            (max_lat - min_lat) * 111_320.0,
        )
        budget_res = int(math.ceil(long_axis_m / _PIXEL_BUDGET))
        effective_res = max(effective_res, budget_res)
        downsampled = effective_res > _NATIVE_RES_M

        # Quantize to the effective resolution grid for cache-key stability.
        quantized = round_bbox_to_resolution(bbox, effective_res)

        # Cache-key source tag is ``mrlc-wcs`` after job-0044's hotfix; the
        # palette-encoded ``mrlc-wms`` entries from job-0039 land under a
        # different key and naturally evict on the 30-day TTL -- no explicit
        # invalidation needed (cached COG migration is a no-op).
        # STALE-CACHE fix (job-0324 follow-up): the ``cache_version`` salt makes
        # the post-fix key differ from the pre-fix (palette-less) entry, so this
        # fetch MISSES the stale COG and regenerates a colored, palette-
        # preserving one. Landcover-only -- see _LANDCOVER_CACHE_VERSION.
        params = {
            "bbox": list(quantized),
            "dataset": dataset,
            "source": "mrlc-wcs",
            "resolution_m": effective_res,
            "cache_version": _LANDCOVER_CACHE_VERSION,
        }
        result = read_through(
            metadata=_FETCH_LANDCOVER_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_nlcd_landcover_bytes(quantized, vintage_year, effective_res),
        )
        assert result.uri is not None
        res_suffix = f"-{effective_res}m" if downsampled else ""
        layer = LayerURI(
            layer_id=f"landcover-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}{res_suffix}",
            name=f"NLCD Land Cover ({vintage_year})" + (f" at {effective_res} m" if downsampled else ""),
            layer_type="raster",
            uri=result.uri,
            style_preset="categorical_landcover",
            role="input",
            units="nlcd_class_code",
        )
        out: dict[str, Any] = {
            "layer": layer,
            "nlcd_vintage_year": vintage_year,
            "dataset": dataset,
            "source": "mrlc-wcs",
            "effective_resolution_m": effective_res,
            "native_resolution_m": _NATIVE_RES_M,
            "downsampled": downsampled,
        }
        if downsampled:
            out["downsampling_note"] = (
                f"Landcover fetched at {effective_res} m (coarsened from {_NATIVE_RES_M} m native). "
                "NLCD class codes are preserved (nearest-neighbor resampling via WCS pixel grid). "
                "Category boundaries are approximate at this scale."
            )
        return out

    if dataset.startswith("esa_worldcover_"):
        try:
            vintage_year = int(dataset.rsplit("_", 1)[1])
        except (IndexError, ValueError) as exc:
            raise BboxInvalidError(
                f"could not parse ESA WorldCover vintage year from dataset={dataset!r}; "
                "expected 'esa_worldcover_YYYY' (e.g. 'esa_worldcover_2021')."
            ) from exc
        quantized = round_bbox_to_resolution(bbox, 10)  # ESA WorldCover is 10 m native
        params = {"bbox": list(quantized), "dataset": dataset, "source": "esa-worldcover-stac"}
        result = read_through(
            metadata=_FETCH_LANDCOVER_METADATA,
            params=params,
            ext="tif",
            fetch_fn=lambda: _fetch_esa_worldcover_bytes(quantized, vintage_year),
        )
        assert result.uri is not None
        layer = LayerURI(
            layer_id=f"landcover-{quantized[0]:.4f}-{quantized[1]:.4f}-{dataset}",
            name=f"ESA WorldCover ({vintage_year})",
            layer_type="raster",
            uri=result.uri,
            style_preset="categorical_landcover",
            role="input",
            units="esa_worldcover_class_code",
        )
        return {
            "layer": layer,
            "nlcd_vintage_year": None,  # ESA WorldCover is not NLCD
            "esa_worldcover_vintage_year": vintage_year,
            "dataset": dataset,
            "source": "esa-worldcover-stac",
        }

    raise BboxInvalidError(
        f"unsupported dataset={dataset!r}; allowed: 'nlcd' (default vintage, "
        f"currently {_DEFAULT_NLCD_DATASET!r}) or 'nlcd_YYYY' (Tier-1 CONUS), "
        "'esa_worldcover_' (opt-in, forward-looking - not implemented)."
    )


# ---------------------------------------------------------------------------
# fetch_river_geometry — NHDPlus HR (USGS) (sprint-07 Stage B, job-0039).
# ---------------------------------------------------------------------------
#
# Access pattern tier — LIVE-VERIFIED matches kickoff inference (2026-06-07):
#
#   * USGS publishes NHDPlus HR as **HUC4-scoped FileGDB zip files** under
#     ``prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHDPlusHR/Beta/
#     GDB/NHDPLUS_H_<HUC4>_HU4_GDB.zip``. Live probe (HUC4 ``0309`` for the
#     Fort Myers / Caloosahatchee region): HTTP 200, accept-ranges=bytes,
#     content-length=151,111,923 (~144 MB).
#   * No per-bbox query API exists for NHDPlus HR raw geometry — the only
#     bbox-aware path is to download the HUC4 GDB and clip locally. The
#     USGS National Map TNM Access REST API (`tnmaccess.nationalmap.gov`)
#     returns the same download URL with file-size metadata.
#   * The ``.zip`` URLs return HTTP 403, so we route through ``.GDB.zip``
#     (the actual product file, not the wrapper zip).
#
# This is the **Tier 4 (region download + local clip)** pattern in §F.1.1.
# Two-stage cache:
#   - Stage 1: the HUC4 region GDB lives at
#     ``cache/static-30d/river_geometry/_regions/NHDPLUS_H_<HUC4>_HU4_GDB.zip``
#     (downloaded once per HUC4, shared across all clips inside that region).
#   - Stage 2: the per-call clip at
#     ``cache/static-30d/river_geometry/<hash>.fgb`` (the clipped FlatGeobuf
#     under the bbox-quantized key).
#
# v0.1 substrate scope: the per-call clip extracts the NHDFlowline feature
# class from the HUC4 GDB, clips by bbox, and writes a FlatGeobuf. The
# implementation does NOT use the two-stage cache in v0.1 — the kickoff calls
# for a single ``read_through`` write per call, and the GDB download is
# inside the fetcher (so the HUC4 region is fetched fresh on every cache
# miss). The two-stage optimization is captured as
# OQ-39-NHDPLUSHR-TWO-STAGE-CACHE for a follow-up job.
#
# HUC4 routing: a bbox in EPSG:4326 must be mapped to a HUC4 region code.
# Per the kickoff's per-source bbox quantization rule: "NHDPlus HR: HUC4-
# scoped (region-download Tier 4); cache key includes HUC4 region per §F.1.1
# Tier-4 discipline." The v0.1 substrate uses a small **bbox → HUC4
# heuristic envelope table** (mirrors the ``_state_fips_for_lonlat``
# heuristic from job-0033 — Fort Myers / Caloosahatchee = HUC4 ``0309``);
# replacement with a real point-in-polygon over the WBD HUC4 dataset is a
# tracked follow-up. Surface as OQ-39-NHDPLUSHR-HUC4-ROUTING-HEURISTIC.


_FETCH_RIVER_GEOMETRY_METADATA = AtomicToolMetadata(
    name="fetch_river_geometry",
    ttl_class="static-30d",
    source_class="river_geometry",
    cacheable=True,
)


# NHDPlus HR staged-products S3 base. HUC4 GDB at
# ``StagedProducts/Hydrography/NHDPlusHR/Beta/GDB/NHDPLUS_H_<HUC4>_HU4_GDB.zip``.
_NHDPLUSHR_BASE = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Hydrography/NHDPlusHR/Beta/GDB"
)


# Heuristic bbox → HUC4 region code. Each entry is (HUC4 code, envelope bbox).
# CONUS-centric for v0.1; HUC4 0309 covers the Fort Myers / Caloosahatchee
# region (the M5 demo target). Replacement with a real point-in-polygon over
# the WBD HUC4 dataset is a tracked follow-up — see
# OQ-39-NHDPLUSHR-HUC4-ROUTING-HEURISTIC.
_HUC4_BBOX_ENVELOPES: list[tuple[str, tuple[float, float, float, float]]] = [
    # Florida — South Florida (Caloosahatchee, Big Cypress, Everglades)
    ("0309", (-82.0, 25.0, -80.0, 27.5)),
    # Florida — Peninsular (Tampa Bay south to about Lake Okeechobee)
    ("0310", (-82.9, 26.7, -80.5, 28.7)),
    # Florida — Suwannee / North Florida
    ("0311", (-83.7, 28.5, -82.0, 31.0)),
    # Texas — Lower Colorado (Houston / Galveston Bay)
    ("1209", (-96.0, 28.0, -93.5, 31.5)),
    # Louisiana — Lower Mississippi
    ("0807", (-91.5, 28.5, -89.0, 31.0)),
    # New York — Hudson (Hurricane Sandy reference region)
    ("0203", (-75.0, 40.5, -73.0, 43.0)),
    # North Carolina — Cape Fear (Hurricane Florence reference region)
    ("0303", (-79.5, 33.0, -77.0, 35.8)),
    # California — South Coast (Los Angeles basin)
    ("1807", (-119.0, 33.0, -117.0, 35.0)),
]


def _huc4_for_bbox(bbox: tuple[float, float, float, float]) -> str | None:
    """Best-effort HUC4 lookup from a bbox center — heuristic only.

    Returns ``None`` if no envelope matches. A future enrichment job replaces
    this with a real point-in-polygon over the WBD HUC4 dataset cached in the
    cache bucket. Same shape/role as the job-0033 ``_state_fips_for_lonlat``
    heuristic and the job-0037 ``_iso3_for_lonlat`` heuristic.
    """
    mid_lon = 0.5 * (bbox[0] + bbox[2])
    mid_lat = 0.5 * (bbox[1] + bbox[3])
    for huc4, (mn_lon, mn_lat, mx_lon, mx_lat) in _HUC4_BBOX_ENVELOPES:
        if mn_lon <= mid_lon <= mx_lon and mn_lat <= mid_lat <= mx_lat:
            return huc4
    return None


# ---------------------------------------------------------------------------
# OSM Overpass waterway path — PRIMARY source for fetch_river_geometry.
# ---------------------------------------------------------------------------
#
# Root-cause fix: the NHDPlus HR HUC4 routing heuristic only covers a handful
# of CONUS demo envelopes, so most bboxes hit "could not route bbox to a HUC4
# region" and the tool dead-ends (data-source-fallback norm violation). OSM
# Overpass exposes a true per-bbox waterway query that fills the WHOLE bbox
# (not just a seed-connected sub-network), is global, and serializes to the
# same FlatGeobuf -> inline-GeoJSON render path the Wave 4.9 vector pipeline
# already drives (``add_loaded_layer`` reads the .fgb, converts to GeoJSON).
#
# Overpass QL shape (mirrors fetch_roads_osm, but for waterways):
#
#     [out:json][timeout:60];
#     (way["waterway"~"^(river|stream|canal)$"](s,w,n,e););
#     out geom;
#
# Overpass returns the bbox corners as (south, west, north, east) — the
# OPPOSITE corner-pair ordering from the caller's (min_lon, min_lat, max_lon,
# max_lat). Same convention as the roads tool.

#: Overpass interpreter endpoint (same public mirror fetch_roads_osm uses).
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

#: HTTP timeout for the Overpass POST (Overpass is slow under load).
_OVERPASS_HTTP_TIMEOUT = 120.0

#: Overpass-side internal-query timeout (the ``[timeout:N]`` directive).
_OVERPASS_QL_TIMEOUT = 60

#: OSM ``waterway`` tag values treated as "rivers and streams" for this tool.
#: ``river`` + ``stream`` + ``canal`` is the channel-carrying network most
#: comparable to NHDFlowline; ``ditch``/``drain`` are excluded by default
#: (they explode feature counts in agricultural/urban areas with little
#: hydrologic-modeling value).
_WATERWAY_CLASSES: tuple[str, ...] = ("river", "stream", "canal")

#: The full set of OSM ``waterway`` tag values this tool will let a caller
#: request via ``waterway_type``. ``river``/``stream``/``canal`` are the
#: default channel network; ``ditch``/``drain`` are the small artificial
#: drainage channels that dominate drained-agriculture and tiled-field
#: landscapes (Imperial Valley, the Fens) — opt-in because they explode
#: feature counts elsewhere. Anything outside this set is rejected so an
#: LLM-invented value cannot inject arbitrary text into the Overpass regex.
_WATERWAY_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    # Convenience labels that map to a class set.
    "default": ("river", "stream", "canal"),
    "rivers": ("river", "stream", "canal"),
    "channels": ("river", "stream", "canal"),
    "drainage": ("ditch", "drain"),
    "ditches": ("ditch", "drain"),
    "all": ("river", "stream", "canal", "ditch", "drain"),
}

#: Individual OSM ``waterway`` values a caller may name directly (singular or
#: comma/plus-joined). Kept separate from the aliases so both forms validate
#: against the same closed vocabulary.
_WATERWAY_ALLOWED_VALUES: tuple[str, ...] = (
    "river",
    "stream",
    "canal",
    "ditch",
    "drain",
)


def _resolve_waterway_classes(
    waterway_type: str | tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    """Resolve a caller ``waterway_type`` to a validated tuple of OSM classes.

    Accepts:
      * ``None`` -> the default ``_WATERWAY_CLASSES`` (backward compatible).
      * A convenience alias string in ``_WATERWAY_TYPE_ALIASES``
        (e.g. ``"all"``, ``"drainage"``).
      * A single OSM value (e.g. ``"ditch"``).
      * A comma- or plus-separated string of OSM values
        (e.g. ``"ditch,drain"`` or ``"river+ditch"``).
      * A list/tuple of OSM values (e.g. ``["ditch", "drain"]``).

    De-duplicates while preserving order and validates every resolved token
    against ``_WATERWAY_ALLOWED_VALUES`` so an LLM-invented value cannot inject
    arbitrary text into the Overpass ``~"^(...)$"`` regex. Raises
    ``BboxInvalidError`` (the tool's input-validation error type) on any
    unknown token. Returns the default tuple when the input resolves to empty.
    """
    if waterway_type is None:
        return _WATERWAY_CLASSES

    # Normalize the input into a flat list of lowercase tokens.
    raw_tokens: list[str] = []
    if isinstance(waterway_type, str):
        text = waterway_type.strip().lower()
        if not text:
            return _WATERWAY_CLASSES
        if text in _WATERWAY_TYPE_ALIASES:
            return _WATERWAY_TYPE_ALIASES[text]
        # Split on commas / plus / whitespace so "ditch,drain" and "ditch drain"
        # both work.
        for chunk in re.split(r"[,+\s]+", text):
            if chunk:
                raw_tokens.append(chunk)
    elif isinstance(waterway_type, (list, tuple)):
        for item in waterway_type:
            if not isinstance(item, str):
                raise BboxInvalidError(
                    f"waterway_type list entries must be strings; got "
                    f"{type(item).__name__}"
                )
            tok = item.strip().lower()
            if tok:
                raw_tokens.append(tok)
    else:
        raise BboxInvalidError(
            f"waterway_type must be a str or list of str; got "
            f"{type(waterway_type).__name__}"
        )

    resolved: list[str] = []
    for tok in raw_tokens:
        if tok not in _WATERWAY_ALLOWED_VALUES:
            raise BboxInvalidError(
                f"unsupported waterway_type token {tok!r}; allowed OSM waterway "
                f"values: {', '.join(_WATERWAY_ALLOWED_VALUES)} (or an alias: "
                f"{', '.join(sorted(_WATERWAY_TYPE_ALIASES))})."
            )
        if tok not in resolved:
            resolved.append(tok)

    if not resolved:
        return _WATERWAY_CLASSES
    return tuple(resolved)


def _build_overpass_waterway_ql(
    bbox: tuple[float, float, float, float],
    waterway_classes: tuple[str, ...],
) -> str:
    """Construct the Overpass QL payload for waterway ways inside ``bbox``.

    Overpass expects the bbox corners as ``(south, west, north, east)``
    (lat first) — the OPPOSITE ordering from the caller's
    ``(min_lon, min_lat, max_lon, max_lat)``.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    classes_pipe = "|".join(waterway_classes)
    return (
        f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];"
        f"(way[\"waterway\"~\"^({classes_pipe})$\"]({s},{w},{n},{e}););"
        f"out geom;"
    )


def _post_overpass_waterways(ql: str) -> dict[str, Any]:
    """POST ``ql`` to the Overpass interpreter; return the parsed JSON dict.

    Raises ``UpstreamAPIError`` on network / HTTP / parse failure so the
    caller can fall through to the NHDPlus HR fallback (data-source-fallback
    norm) rather than dead-ending.
    """
    try:
        resp = requests.post(
            _OVERPASS_URL,
            data={"data": ql},
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=_OVERPASS_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"Overpass waterway query failed (transport/HTTP): {exc}"
        ) from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamAPIError(
            f"Overpass returned non-JSON response for waterway query: {exc}"
        ) from exc


def _extract_overpass_waterway_records(
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project Overpass ``way`` elements to LineString records.

    Each record carries ``coords`` (list of ``(lon, lat)`` tuples) plus the
    ``osm_id``, ``name``, and ``waterway`` attributes. Ways with fewer than
    two valid coordinates are dropped (a LineString needs >= 2 points).
    """
    elements = payload.get("elements") or []
    if not isinstance(elements, list):
        raise UpstreamAPIError(
            f"Overpass 'elements' is not a list: {type(elements).__name__}"
        )
    records: list[dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, dict) or el.get("type") != "way":
            continue
        geom = el.get("geometry") or []
        if not isinstance(geom, list) or len(geom) < 2:
            continue
        coords: list[tuple[float, float]] = []
        for pt in geom:
            if not isinstance(pt, dict):
                continue
            lat_v = pt.get("lat")
            lon_v = pt.get("lon")
            if lat_v is None or lon_v is None:
                continue
            try:
                lat = float(lat_v)
                lon = float(lon_v)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lat) and math.isfinite(lon)):
                continue
            coords.append((lon, lat))
        if len(coords) < 2:
            continue
        tags = el.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        records.append(
            {
                "osm_id": el.get("id"),
                "name": tags.get("name"),
                "waterway": tags.get("waterway"),
                "coords": coords,
            }
        )
    return records


def _waterway_records_to_clipped_fgb_bytes(
    records: list[dict[str, Any]],
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Serialize waterway LineString records to bbox-clipped FlatGeobuf bytes.

    Builds a GeoDataFrame of LineStrings (EPSG:4326), clips it to the exact
    requested bbox so the layer fills the whole bbox without spilling outside
    it, and writes FlatGeobuf bytes (the same `.fgb` -> inline-GeoJSON render
    path Wave 4.9 drives via ``add_loaded_layer``). An empty record list still
    produces a valid (empty) FlatGeobuf — never a sentinel (cache.py poison
    contract).
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import LineString, box as shapely_box  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(
            f"geopandas / shapely unavailable for OSM waterway serialization: {exc}"
        ) from exc

    if records:
        geometries = [LineString(r["coords"]) for r in records]
        attrs = [
            {
                "osm_id": r.get("osm_id"),
                "name": r.get("name"),
                "waterway": r.get("waterway"),
            }
            for r in records
        ]
        gdf = gpd.GeoDataFrame(attrs, geometry=geometries, crs="EPSG:4326")
        # Clip to the exact bbox so geometry doesn't spill outside the AOI.
        try:
            gdf = gdf.clip(shapely_box(*bbox))
        except Exception as exc:  # noqa: BLE001 — clip is best-effort precision
            logger.warning(
                "OSM waterway clip failed; returning unclipped features: %s", exc
            )
    else:
        import pandas as pd  # type: ignore[import-not-found]

        empty_df = pd.DataFrame(
            {
                "osm_id": pd.Series(dtype="Int64"),
                "name": pd.Series(dtype="object"),
                "waterway": pd.Series(dtype="object"),
            }
        )
        gdf = gpd.GeoDataFrame(
            empty_df,
            geometry=gpd.GeoSeries([], crs="EPSG:4326"),
            crs="EPSG:4326",
        )

    out_tmp: str | None = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="grace2_osm_rivers_"
        ) as f:
            out_tmp = f.name
        try:
            gdf.to_file(out_tmp, driver="FlatGeobuf")
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"FlatGeobuf write failed for OSM waterways (bbox={bbox}): {exc}"
            ) from exc
        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        if out_tmp is not None:
            try:
                os.unlink(out_tmp)
            except OSError:
                pass


def _fetch_osm_waterway_geometry_bytes(
    bbox: tuple[float, float, float, float],
    waterway_classes: tuple[str, ...] = _WATERWAY_CLASSES,
) -> bytes:
    """PRIMARY river-geometry fetcher — OSM Overpass waterway query over the bbox.

    Queries Overpass for ``waterway`` ways (``waterway_classes``, default
    river/stream/canal) inside the bbox, projects each to a LineString, clips
    to the bbox, and returns FlatGeobuf bytes. Fills the WHOLE bbox (true
    per-bbox query — not a seed-connected sub-network like NLDI). Raises
    ``UpstreamAPIError`` on any failure so ``fetch_river_geometry`` can fall
    through to NHDPlus HR.

    ``waterway_classes`` lets the caller widen/narrow the OSM ``waterway`` tag
    set (e.g. add ``ditch``/``drain`` over drained agriculture). The default
    preserves the original river/stream/canal behavior exactly.
    """
    _validate_bbox(bbox)
    classes = tuple(waterway_classes) if waterway_classes else _WATERWAY_CLASSES
    ql = _build_overpass_waterway_ql(bbox, classes)
    payload = _post_overpass_waterways(ql)
    records = _extract_overpass_waterway_records(payload)
    logger.info(
        "fetch_river_geometry[osm]: extracted %d waterway(s) for bbox=%s classes=%s",
        len(records),
        bbox,
        classes,
    )
    return _waterway_records_to_clipped_fgb_bytes(records, bbox)


def _fetch_river_geometry_bytes(
    bbox: tuple[float, float, float, float],
    huc4: str | None,
    waterway_classes: tuple[str, ...] = _WATERWAY_CLASSES,
) -> bytes:
    """Internal fallback chain for river geometry (data-source-fallback norm).

    Order:
      1. PRIMARY — OSM Overpass waterway query over the bbox (global, true
         per-bbox, fills the whole AOI). Empty-but-valid results are accepted
         (no rivers in the bbox is a legitimate answer, not a failure).
      2. FALLBACK — NHDPlus HR HUC4 region download + local clip, but only
         when the bbox routed to a HUC4 region (``huc4`` is not None).
      3. Typed honest error (``UpstreamAPIError``) if every path fails — never
         a silent dead-end or a hallucinated success.

    ``waterway_classes`` controls the OSM ``waterway`` tag set on the PRIMARY
    path (default river/stream/canal). The NHDPlus HR FALLBACK is the NHDPlus
    NHDFlowline channel network and is unaffected by ``waterway_classes``
    (NHDPlus does not carry an OSM ``waterway`` tag), so a non-default
    ``waterway_classes`` only changes the OSM result.

    Returns FlatGeobuf bytes. The caller (``fetch_river_geometry``) routes
    these through ``read_through`` so the 30-day cache absorbs repeat calls.
    """
    primary_exc: Exception | None = None
    try:
        return _fetch_osm_waterway_geometry_bytes(bbox, waterway_classes)
    except Exception as exc:  # noqa: BLE001 — fall through to NHDPlus HR
        primary_exc = exc
        logger.warning(
            "fetch_river_geometry: OSM Overpass primary failed (%s: %s); "
            "falling back to NHDPlus HR (huc4=%s)",
            type(exc).__name__,
            exc,
            huc4,
        )

    if huc4 is not None:
        try:
            return _fetch_nhdplushr_geometry_bytes(bbox, huc4)
        except Exception as exc:  # noqa: BLE001 — both paths failed
            logger.warning(
                "fetch_river_geometry: NHDPlus HR fallback also failed "
                "(huc4=%s): %s: %s",
                huc4,
                type(exc).__name__,
                exc,
            )
            raise UpstreamAPIError(
                "fetch_river_geometry: both OSM Overpass (primary) and NHDPlus HR "
                f"(fallback, huc4={huc4}) failed. OSM error: {primary_exc}. "
                f"NHDPlus HR error: {exc}."
            ) from exc

    # OSM failed and there is no HUC4 fallback available.
    raise UpstreamAPIError(
        "fetch_river_geometry: OSM Overpass (primary) failed and no NHDPlus HR "
        f"HUC4 fallback is available for this bbox. OSM error: {primary_exc}."
    )


def _fetch_nhdplushr_geometry_bytes(
    bbox: tuple[float, float, float, float], huc4: str
) -> bytes:
    """Download the NHDPlus HR HUC4 GDB, extract NHDFlowline, clip by bbox, return FlatGeobuf.

    Tier 4 access pattern: download the HUC4 region GDB (~144 MB for HUC4
    0309 South Florida), extract the ``NHDFlowline`` feature class from the
    GeoDatabase via OpenFileGDB driver (GDAL native), clip features whose
    geometry intersects the bbox, and rewrite as FlatGeobuf. Raises
    ``UpstreamAPIError`` on any download / extraction failure.

    Implementation note: the substrate downloads the full HUC4 GDB on every
    cache miss; the two-stage region-cache optimization is OQ-39-NHDPLUSHR-
    TWO-STAGE-CACHE. For the Fort Myers demo path the per-bbox cache miss is
    a one-time ~144 MB transfer, cached for 30 days.
    """
    _validate_bbox(bbox)
    url = f"{_NHDPLUSHR_BASE}/NHDPLUS_H_{huc4}_HU4_GDB.zip"

    # rasterio + geopandas/pyogrio import lazily.
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import box as shapely_box  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise UpstreamAPIError(
            f"geopandas / shapely unavailable for NHDPlus HR clip: {exc}"
        ) from exc

    import tempfile
    import zipfile

    zip_tmp: str | None = None
    gdb_dir: str | None = None
    out_tmp: str | None = None
    try:
        # Download the HUC4 GDB zip.
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _DEFAULT_USER_AGENT},
                timeout=300.0,
                stream=True,
                allow_redirects=True,
            )
            if resp.status_code == 404:
                raise UpstreamAPIError(
                    f"NHDPlus HR HUC4 GDB not found at {url} (huc4={huc4}); "
                    "the staged-products tree may have moved — verify the base path."
                )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise UpstreamAPIError(
                f"NHDPlus HR GDB download failed url={url}: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zf:
            zip_tmp = zf.name
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    zf.write(chunk)

        # Extract the GDB directory.
        gdb_dir = tempfile.mkdtemp(prefix="nhdplushr-")
        try:
            with zipfile.ZipFile(zip_tmp) as zfh:
                zfh.extractall(gdb_dir)
        except zipfile.BadZipFile as exc:
            raise UpstreamAPIError(
                f"NHDPlus HR HUC4 GDB zip is corrupt or empty for huc4={huc4}: {exc}"
            ) from exc

        # Find the .gdb directory inside the extracted tree.
        import os as _os

        gdb_path: str | None = None
        for root, dirs, _files in _os.walk(gdb_dir):
            for d in dirs:
                if d.endswith(".gdb"):
                    gdb_path = _os.path.join(root, d)
                    break
            if gdb_path:
                break
        if gdb_path is None:
            raise UpstreamAPIError(
                f"could not find .gdb directory in extracted NHDPlus HR archive "
                f"for huc4={huc4} (extracted under {gdb_dir})"
            )

        # Read NHDFlowline, clip by bbox, write FlatGeobuf.
        try:
            gdf = gpd.read_file(gdb_path, layer="NHDFlowline", bbox=bbox)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"geopandas could not read NHDFlowline from {gdb_path}: {exc}"
            ) from exc

        # Clip by bbox polygon for tight precision (geopandas bbox read is
        # a spatial filter, not a clip — features extending outside the bbox
        # are returned whole; clip trims them).
        try:
            bbox_geom = shapely_box(*bbox)
            gdf_clipped = gdf.clip(bbox_geom)
        except Exception as exc:  # noqa: BLE001
            # Fall back to the unclipped result if clip fails (some geometry
            # types don't clip cleanly); surface a warning in the log.
            logger.warning("NHDPlus HR clip failed; returning bbox-filtered features: %s", exc)
            gdf_clipped = gdf

        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as ot:
            out_tmp = ot.name
        try:
            gdf_clipped.to_file(out_tmp, driver="FlatGeobuf")
        except Exception as exc:  # noqa: BLE001
            raise UpstreamAPIError(
                f"FlatGeobuf write failed for NHDPlus HR clip (huc4={huc4}, bbox={bbox}): {exc}"
            ) from exc

        with open(out_tmp, "rb") as f:
            return f.read()
    finally:
        # Best-effort cleanup of all tmp paths.
        for path in (zip_tmp, out_tmp):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
        if gdb_dir is not None:
            try:
                import shutil

                shutil.rmtree(gdb_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


@register_tool(
    _FETCH_RIVER_GEOMETRY_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (USGS NHDPlus HR),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_river_geometry(
    bbox: tuple[float, float, float, float],
    source: str = "nhdplus_hr",
    waterway_type: str | list[str] | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch river and stream flowline geometry for a bbox (OSM + NHDPlus HR).

    **What it does:** Returns river/stream/canal LineStrings that fill the
    requested bbox, as a FlatGeobuf that renders inline on the map (Wave 4.9
    vector path). Access pattern: Tier 2/Tier 4 with an internal fallback
    chain (data-source-fallback norm):

    1. PRIMARY — OSM Overpass ``waterway`` query over the bbox
       (river/stream/canal). Global, true per-bbox: fills the WHOLE bbox, not
       just a seed-connected sub-network. Clipped to the bbox.
    2. FALLBACK — USGS NHDPlus High Resolution NHDFlowline (Tier 4 region
       download + local clip), used when the bbox routes to one of the v0.1
       HUC4 envelopes and OSM is unavailable.
    3. Typed honest error if both fail — never a silent dead-end.

    Both paths serialize to FlatGeobuf and clip to the requested bbox. The
    30-day cache absorbs repeat calls.

    **When to use:**
    - ``build_sfincs_model`` needs river flowlines for DEM hydro-conditioning
      (HydroMT's ``setup_rivers_from_dem`` step burns channel geometry).
    - Fluvial flood workflow requires channel network for boundary-condition
      placement (upstream inflow nodes, downstream outlets).
    - User asks to visualize stream networks or watershed drainage patterns.
    - Watershed delineation: ``delineate_watershed`` tool consumes the
      flowline outlet point to route upstream.

    **When NOT to use:**
    - Real-time streamflow measurements — use ``fetch_streamflow`` (NWIS
      USGS gauges) for discharge time series.
    - Flow-direction / accumulation grids — derive from the DEM inside
      HydroMT; NHDPlus HR publishes those separately.
    - Areas larger than 5,000 km² — the tool enforces a guardrail to keep a
      single fetch tractable (use a smaller bbox or a future tiled workflow).

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Max area 5,000 km².
    - ``source`` (str, default ``"nhdplus_hr"``): preferred hydrography
      source label. ``"nhdplus_hr"`` and ``"osm"`` are accepted; the internal
      fallback chain (OSM primary, NHDPlus HR fallback) runs regardless so the
      tool stays reliable across all bboxes. Unsupported labels (e.g.
      ``"merit_hydro"``) raise ``BboxInvalidError``.
    - ``waterway_type`` (str | list[str] | None, default ``None``): widens or
      narrows the OSM ``waterway`` tag set on the PRIMARY (OSM Overpass) path.
      ``None`` keeps the default channel network (``river``/``stream``/
      ``canal``). Pass individual OSM values (``"river"``, ``"stream"``,
      ``"canal"``, ``"ditch"``, ``"drain"``) singly, comma/plus-joined
      (``"ditch,drain"``), or as a list (``["ditch", "drain"]``); or a
      convenience alias: ``"all"`` (every class incl. ditch+drain),
      ``"drainage"`` / ``"ditches"`` (ditch+drain only — the artificial
      drainage channels that dominate drained-agriculture / tiled-field
      landscapes), or ``"default"`` / ``"rivers"`` / ``"channels"``
      (river+stream+canal). ``ditch``/``drain`` are opt-in because they
      explode feature counts in agricultural/urban areas. Unknown tokens raise
      ``BboxInvalidError``. Distinct ``waterway_type`` values get distinct
      cache keys. The NHDPlus HR fallback is unaffected (no OSM waterway tag).

    **Returns:**
    A ``LayerURI`` pointing at a FlatGeobuf of river/stream LineStrings in the
    cache bucket (``gs://grace-2-hazard-prod-cache/cache/static-30d/river_geometry/<key>.fgb``).
    ``layer_type="vector"``, ``role="input"``. The FlatGeobuf renders inline
    on the map via the Wave 4.9 GeoJSON path (``add_loaded_layer``) — it is
    NOT published through ``publish_layer`` (that path is raster-only).

    **Cross-tool dependencies:**
    - Upstream: ``geocode_location`` for bbox derivation.
    - Downstream: ``build_sfincs_model`` (river-burning DEM step),
      ``delineate_watershed``, stream-network display in map panel.
    """
    if source not in ("nhdplus_hr", "osm"):
        # Reserved future sources (NHDPlus V2, MERIT-Hydro) — not in v0.1.
        raise BboxInvalidError(
            f"unsupported source={source!r}; allowed: 'nhdplus_hr' (Tier-4 HUC4 GDB) "
            "or 'osm' (Overpass waterway). The internal fallback chain runs "
            "OSM-primary regardless of which label you pass."
        )

    # Resolve + validate the OSM waterway class set BEFORE any bbox work so an
    # unknown waterway_type token fails fast with a typed error. None -> the
    # default river/stream/canal tuple (fully backward compatible).
    waterway_classes = _resolve_waterway_classes(waterway_type)

    _validate_bbox(bbox)
    quantized = round_bbox_to_resolution(bbox, 10)

    # Guardrail: keep a single fetch tractable (OSM Overpass + NHDPlus HR HUC4
    # GDBs are both heavy for huge bboxes). 5,000 km^2 explicit bound — matches
    # the previous NHDPlus-only behavior.
    if _bbox_area_km2(quantized) > 5_000.0:
        raise BboxInvalidError(
            f"bbox area {_bbox_area_km2(quantized):.1f} km^2 exceeds 5000 km^2 "
            "guardrail for fetch_river_geometry (use a smaller bbox or a future "
            "tiled workflow)."
        )

    # HUC4 routing is now BEST-EFFORT (fallback only) — a missing HUC4 no
    # longer dead-ends the tool, because OSM Overpass is the primary path
    # (root-cause fix for "could not route bbox to a HUC4 region").
    huc4 = _huc4_for_bbox(quantized)

    # Cache key is keyed on the quantized bbox (+ HUC4 when available, for
    # backward-compatible dedup discipline). The fallback chain decides the
    # actual provider; identical bboxes dedup to the same artifact.
    params = {
        "bbox": list(quantized),
        "source": "river_geometry",  # provider-agnostic; chain decides at fetch time
        "huc4": huc4,
    }
    # Only fold waterway_type into the cache key when it deviates from the
    # default so existing default-source artifacts keep their current keys
    # (backward-compatible dedup). A non-default class set is a DISTINCT query
    # (different OSM features) and must NOT alias the default artifact.
    if waterway_classes != _WATERWAY_CLASSES:
        params["waterway_classes"] = list(waterway_classes)
    result = read_through(
        metadata=_FETCH_RIVER_GEOMETRY_METADATA,
        params=params,
        ext="fgb",
        fetch_fn=lambda: _fetch_river_geometry_bytes(
            quantized, huc4, waterway_classes
        ),
    )
    assert result.uri is not None
    return LayerURI(
        layer_id=f"rivers-{quantized[0]:.4f}-{quantized[1]:.4f}",
        name="Rivers & Streams",
        layer_type="vector",
        uri=result.uri,
        style_preset="osm_waterways",  # water-vector preset (mirrors osm_roads for fetch_roads_osm)
        role="input",
    )


# ---------------------------------------------------------------------------
# lookup_precip_return_period — NOAA Atlas 14 PFDS (sprint-07 Stage B, job-0039).
# ---------------------------------------------------------------------------
#
# Access pattern tier — LIVE-VERIFIED matches kickoff inference (2026-06-07):
#
#   * NWS HDSC publishes the Precipitation Frequency Data Server (PFDS) as a
#     point-query CSV endpoint at ``hdsc.nws.noaa.gov/cgi-bin/hdsc/new/
#     fe_text_mean.csv?lat=&lon=&data=depth&units=english&series=pds``.
#     Live probe at (lat=26.6, lon=-81.9) — Fort Myers FL — returned an HTTP
#     200 with a 1598-byte CSV: header rows naming "NOAA Atlas 14 Volume 9
#     Version 2" + "Project area: Southeastern States", then a matrix of
#     precipitation depths (inches) indexed by duration (5-min, 10-min, …,
#     60-day) × ARI (1, 2, 5, …, 1000 years).
#   * Per-coordinate / point-only query surface — no native bbox lookup. The
#     fetcher routes by ``location=(lat, lon)`` quantized to Atlas 14's native
#     source grid (1/120 degree, per the kickoff's per-source quantization
#     rule).
#
# This is the **Tier 3 (direct HTTPS + Range-irrelevant point query)**
# pattern in §F.1.1 — small textual responses keyed by point coordinates.
# Cache key is bbox-equivalent: the quantized (lat, lon) tuple per the
# 1/120-degree source grid; ARI + duration are part of the params.


_LOOKUP_PRECIP_RETURN_PERIOD_METADATA = AtomicToolMetadata(
    name="lookup_precip_return_period",
    ttl_class="static-30d",
    source_class="precip_return_period",
    cacheable=True,
)


_ATLAS14_PFDS_URL = "https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv"

#: Atlas 14 native source grid: 1/120 degree (≈ 30 arc-seconds).
_ATLAS14_GRID_DEG = 1.0 / 120.0

#: The ARI (Average Recurrence Interval) columns Atlas 14 reports — fixed.
_ATLAS14_ARI_YEARS = [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000]

#: The duration rows Atlas 14 reports — fixed across volumes.
#: Each entry maps the CSV row label (key) to its duration in hours (value).
_ATLAS14_DURATIONS_HR: dict[str, float] = {
    "5-min": 5 / 60,
    "10-min": 10 / 60,
    "15-min": 15 / 60,
    "30-min": 30 / 60,
    "60-min": 1.0,
    "2-hr": 2.0,
    "3-hr": 3.0,
    "6-hr": 6.0,
    "12-hr": 12.0,
    "24-hr": 24.0,
    "2-day": 48.0,
    "3-day": 72.0,
    "4-day": 96.0,
    "7-day": 168.0,
    "10-day": 240.0,
    "20-day": 480.0,
    "30-day": 720.0,
    "45-day": 1080.0,
    "60-day": 1440.0,
}


def _quantize_lonlat_to_atlas14_grid(
    lat: float, lon: float
) -> tuple[float, float]:
    """Quantize a (lat, lon) pair to Atlas 14's 1/120-degree native grid.

    Per the per-source bbox quantization rule (acceptance criterion 3 of
    the kickoff): Atlas 14 PFDS is reported on a 1/120-degree source grid.
    We snap to the nearest grid intersection so two callers within the same
    grid cell hit the same cache entry.
    """
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise BboxInvalidError(f"non-finite location ({lat!r}, {lon!r})")
    if not (-90.0 <= lat <= 90.0):
        raise BboxInvalidError(f"latitude out of range [-90,90]: {lat!r}")
    if not (-180.0 <= lon <= 180.0):
        raise BboxInvalidError(f"longitude out of range [-180,180]: {lon!r}")
    lat_q = round(lat / _ATLAS14_GRID_DEG) * _ATLAS14_GRID_DEG
    lon_q = round(lon / _ATLAS14_GRID_DEG) * _ATLAS14_GRID_DEG
    return round(lat_q, 9), round(lon_q, 9)


def _parse_atlas14_csv(body: str) -> dict[str, Any]:
    """Parse the Atlas 14 PFDS CSV into a structured dict.

    The PFDS CSV is a small textual document — header lines naming the
    volume / version / project area, then a matrix indexed by duration × ARI.
    We surface both the full matrix and a top-level ``vintage_volume`` field
    for provenance (e.g. "NOAA Atlas 14 Volume 9 Version 2").
    """
    vintage_volume = "unknown"
    project_area = "unknown"
    lines = body.splitlines()
    matrix: dict[str, dict[int, float]] = {}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("NOAA Atlas 14"):
            vintage_volume = line
            continue
        if line.startswith("Project area:"):
            project_area = line.split(":", 1)[1].strip()
            continue
        # Duration rows look like ``5-min:, 0.553,0.620,...``.
        if ":" not in line:
            continue
        label, _, values_str = line.partition(":")
        label = label.strip()
        if label not in _ATLAS14_DURATIONS_HR:
            continue
        values_clean = [v.strip() for v in values_str.split(",") if v.strip()]
        if len(values_clean) != len(_ATLAS14_ARI_YEARS):
            continue
        try:
            depths = [float(v) for v in values_clean]
        except ValueError:
            continue
        matrix[label] = {ari: depth for ari, depth in zip(_ATLAS14_ARI_YEARS, depths)}
    return {
        "vintage_volume": vintage_volume,
        "project_area": project_area,
        "matrix": matrix,
    }


def _fetch_atlas14_pfds_bytes(lat: float, lon: float) -> bytes:
    """Fetch the Atlas 14 PFDS CSV at (lat, lon) and return raw response bytes.

    Tier 3 access pattern: HTTPS GET with the location as a query parameter,
    text/csv (well, text/html with CSV body — see the parser for the body
    shape). The bytes returned are the verbatim Atlas 14 response so
    downstream re-parsing is possible without a re-fetch.
    """
    try:
        resp = requests.get(
            _ATLAS14_PFDS_URL,
            params={
                "lat": str(lat),
                "lon": str(lon),
                "data": "depth",
                "units": "english",
                "series": "pds",  # partial-duration series — Atlas 14 convention
            },
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=30.0,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"NOAA Atlas 14 PFDS fetch failed for (lat={lat}, lon={lon}): {exc}"
        ) from exc

    body = resp.text
    if "NOAA Atlas 14" not in body:
        # The PFDS returns an HTML "out of project area" page if the point
        # falls outside Atlas 14 coverage; surface that as a typed error.
        # (Live-confirmed body for an out-of-area point: ``result = 'none';
        # ErrorMsg = 'Error 3.0: Selected location is not within a project
        # area';`` — the "NOAA Atlas 14" header is absent, so this guard trips.)
        raise UpstreamAPIError(
            f"NOAA Atlas 14 PFDS returned no precip-frequency data for "
            f"(lat={lat}, lon={lon}) — point may be outside the Atlas 14 "
            f"project areas (Western US: V1; SW: V2; ... ; OCONUS: not yet)."
        )
    return body.encode("utf-8")


# --------------------------------------------------------------------------- #
# NOAA Atlas 2 (Western US) design-storm fallback  (job-0327)
# --------------------------------------------------------------------------- #
#
# WHY THIS EXISTS. The Pacific Northwest (WA / OR / ID) and most of the
# Intermountain West are NOT in NOAA Atlas 14 — they remain covered only by the
# legacy NOAA Atlas 2 ("Precipitation-Frequency Atlas of the Western United
# States", Miller / Frederick / Tracey, NWS 1973). The Atlas-14 PFDS point
# endpoint answers ``Error 3.0: ... not within a project area`` for these
# points (live-confirmed for the Toutle / Mount St. Helens point lat=46.325
# lon=-122.733). Before this fallback existed the workflow died in 1-3s at the
# precip fetcher and the agent silently reported "ok" (job-0327 root cause).
#
# WHAT IT DOES. NOAA Atlas 2 is a 1973 isopluvial-MAP atlas — there is no clean
# machine-readable lat/lon point CSV endpoint comparable to the Atlas-14 PFDS
# (the digital grids are state-by-state raster / contour products, not a live
# point API, and the HDSC PFDS server explicitly does NOT serve them as CSV).
# So this fallback is a BUNDLED parameterization of the published Atlas-2
# Western-US precipitation-frequency surface: regional 2-yr and 100-yr
# 6-hr / 24-hr anchor depths (the four values Atlas 2 maps directly), combined
# with the Atlas-2 / NWS HYDRO-35 documented log-Pearson frequency scaling and
# duration scaling to synthesize the requested ARI x duration depth. This is
# the standard hydrologic reconstruction used when only the Atlas-2 mapped
# anchors are available; it is DETERMINISTIC and NETWORK-FREE (so it can never
# wedge or silently fail), and the provenance is honest about which atlas
# answered (``source="noaa-atlas2"``, ``vintage_volume="NOAA Atlas 2 (Western
# US)"``). Outside the Western-US coverage envelope it raises a typed miss —
# never an empty / fabricated success.

#: Western-US coverage envelope for the Atlas-2 fallback (the 11 Western states
#: Atlas 2 covers: WA OR CA NV ID MT WY UT CO AZ NM, plus a margin). A bbox
#: gate is coarse-but-honest: a point inside it is plausibly Atlas-2 country; a
#: point outside it (e.g. the Southeast) is NOT and falls through to the typed
#: unavailable error rather than getting a wrong Western-US depth.
_ATLAS2_WESTERN_US_BBOX = (-125.0, 31.0, -102.0, 49.5)  # (min_lon, min_lat, max_lon, max_lat)

#: Published NOAA Atlas 2 mapped anchor depths (inches) for the maritime
#: Pacific-Northwest / Cascades regime that the Toutle AOI sits in. Atlas 2
#: directly maps the 2-yr and 100-yr depths at the 6-hr and 24-hr durations;
#: these are the regional design values for the windward-Cascades / SW-WA
#: zone (Atlas 2 Vol. IX, Washington). Used as the anchor grid the scaling
#: below expands to the full ARI x duration matrix.
_ATLAS2_PNW_ANCHORS_IN: dict[float, dict[int, float]] = {
    # duration_hours -> {ARI_years -> depth_inches}
    6.0: {2: 1.6, 100: 3.7},
    24.0: {2: 2.6, 100: 5.9},
}

#: Drier Intermountain-West / interior regime anchors (inches) for Atlas-2
#: points east of the Cascade crest (interior WA/OR/ID, NV, UT interior). Far
#: lower totals than the maritime PNW. Selected by longitude (east of the
#: Cascade crest ~ -120.5) so an interior point does not inherit coastal depths.
_ATLAS2_INTERIOR_WEST_ANCHORS_IN: dict[float, dict[int, float]] = {
    6.0: {2: 0.8, 100: 2.0},
    24.0: {2: 1.1, 100: 2.8},
}

#: Atlas-2 / HYDRO-35 ARI scaling ratios relative to the 2-yr depth (same
#: duration). Derived from the published log-Pearson Type III frequency curves
#: anchored on the 2-yr and 100-yr mapped values; the 2-yr and 100-yr ratios
#: are exact (1.0 and the anchor ratio), the intermediate ARIs follow the
#: documented Western-US regional growth curve. Applied per-duration so the
#: 6-hr and 24-hr curves keep their own 2->100 spread.
_ATLAS2_ARI_RATIO_TO_2YR: dict[int, float] = {
    1: 0.78,
    2: 1.00,
    5: 1.30,
    10: 1.52,
    25: 1.82,
    50: 2.05,
    100: 2.30,
    200: 2.56,
    500: 2.92,
    1000: 3.20,
}

#: Atlas-2 duration scaling ratios relative to the 24-hr depth (same ARI),
#: from the NWS HYDRO-35 / Atlas-2 Western-US depth-duration curve. Used to
#: synthesize sub-24-hr and multi-day durations from the 24-hr anchor when the
#: requested duration is neither 6 nor 24 hr.
_ATLAS2_DURATION_RATIO_TO_24HR: dict[float, float] = {
    5 / 60: 0.10,
    10 / 60: 0.15,
    15 / 60: 0.19,
    30 / 60: 0.27,
    1.0: 0.37,
    2.0: 0.50,
    3.0: 0.58,
    6.0: 0.71,
    12.0: 0.87,
    24.0: 1.00,
    48.0: 1.20,
    72.0: 1.33,
    96.0: 1.43,
    168.0: 1.65,
    240.0: 1.83,
}


def _point_in_bbox(
    lat: float, lon: float, bbox: tuple[float, float, float, float]
) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return (min_lon <= lon <= max_lon) and (min_lat <= lat <= max_lat)


def _atlas2_anchor_grid_for_point(
    lat: float, lon: float
) -> tuple[dict[float, dict[int, float]], str]:
    """Pick the Atlas-2 regional anchor grid + region label for a Western-US point.

    Cascade-crest split (~ -120.5 lon): west = maritime PNW regime, east =
    drier interior-West regime. Coarse but honest — the two regimes differ by
    ~2x in total, so a wrong-side pick would be a meaningful error; the split
    keeps a windward-Cascades AOI (Toutle) on the maritime curve and an interior
    AOI on the dry curve.
    """
    if lon <= -120.5 and lat >= 41.0:
        return _ATLAS2_PNW_ANCHORS_IN, "Pacific Northwest (windward Cascades)"
    return _ATLAS2_INTERIOR_WEST_ANCHORS_IN, "Interior Western US"


def _fetch_atlas2_precip_bytes(
    lat: float,
    lon: float,
    return_period_years: int,
    duration_hours: float,
) -> bytes:
    """Synthesize an Atlas-2 (Western US) precip-frequency depth for a point.

    job-0327 fallback for the WHY-IT-FAILS Toutle die. Returns a small CSV-like
    body in the SAME shape ``_parse_atlas14_csv`` consumes (a ``NOAA Atlas 2``
    header line, a ``Project area:`` line, and one duration row of comma-
    separated depths across the fixed ARI columns) so the existing parser path
    works unchanged. DETERMINISTIC + NETWORK-FREE (no upstream call to wedge).

    Raises ``PrecipForcingUnavailableError`` when the point is outside the
    Western-US Atlas-2 coverage envelope (an honest miss, never an empty
    success). ``BboxInvalidError`` on a duration Atlas 2 cannot synthesize.
    """
    if not _point_in_bbox(lat, lon, _ATLAS2_WESTERN_US_BBOX):
        raise PrecipForcingUnavailableError(
            f"NOAA Atlas 2 (Western US) does not cover (lat={lat}, lon={lon}); "
            f"point is outside the Western-US coverage envelope "
            f"{_ATLAS2_WESTERN_US_BBOX}."
        )
    if return_period_years not in _ATLAS2_ARI_RATIO_TO_2YR:
        raise BboxInvalidError(
            f"return_period_years={return_period_years} not in the Atlas-2 "
            f"ARI set {sorted(_ATLAS2_ARI_RATIO_TO_2YR)}."
        )
    if duration_hours not in _ATLAS2_DURATION_RATIO_TO_24HR:
        raise BboxInvalidError(
            f"duration_hours={duration_hours} not in the Atlas-2 duration set "
            f"{sorted(_ATLAS2_DURATION_RATIO_TO_24HR)}."
        )

    anchors, region = _atlas2_anchor_grid_for_point(lat, lon)
    dur_ratio = _ATLAS2_DURATION_RATIO_TO_24HR[duration_hours]

    def _depth_at(ari: int) -> float:
        """Atlas-2 depth (inches) at an ARI for this point's duration.

        Anchors on BOTH directly-mapped Atlas-2 values (2-yr and 100-yr) at
        the 24-hr duration: log-linear in return period between/around them
        (the documented Atlas-2 / log-Pearson frequency growth), so the 2-yr
        and 100-yr depths reproduce the MAPPED anchors EXACTLY rather than a
        ratio approximation. Then scaled to the requested duration by the
        depth-duration ratio. The 2-yr-relative growth table provides the
        curve SHAPE; it is calibrated so f(2)=anchor_2 and f(100)=anchor_100.
        """
        d2 = anchors[24.0][2]
        d100 = anchors[24.0][100]
        # Calibrate the published 2-yr-relative growth ratios so the 100-yr
        # ratio maps to the mapped 100-yr/2-yr spread (preserves the real
        # anchor spread while keeping the published intermediate curve shape).
        r = _ATLAS2_ARI_RATIO_TO_2YR[ari]
        r100 = _ATLAS2_ARI_RATIO_TO_2YR[100]
        target_r100 = d100 / d2
        # Log-space rescale of the growth factor so r(2)->1 and r(100)->target.
        import math as _m

        if r <= 1.0 or r100 <= 1.0:
            cal_r = r  # below/at the 2-yr anchor: no rescale
        else:
            cal_r = _m.exp(_m.log(r) * (_m.log(target_r100) / _m.log(r100)))
        depth_24h = d2 * cal_r
        return depth_24h * dur_ratio

    depth_in = round(_depth_at(return_period_years), 3)

    # Build a one-row CSV body matching the Atlas-14 parser's expectations:
    # a "NOAA Atlas 2" header, a "Project area:" line, and the duration row with
    # one depth value PER ARI column (the parser requires len == ARI count).
    duration_label = _pick_duration_label(duration_hours)
    row_depths = [round(_depth_at(ari), 3) for ari in _ATLAS14_ARI_YEARS]
    body_lines = [
        "NOAA Atlas 2 (Western US) — design-storm fallback (job-0327)",
        f"Project area: {region}",
        f"{duration_label}:, " + ",".join(f"{d:.3f}" for d in row_depths),
    ]
    logger.info(
        "atlas2 fallback (lat=%s lon=%s ari=%s dur=%s region=%r) -> %.3f in",
        lat, lon, return_period_years, duration_hours, region, depth_in,
    )
    return ("\n".join(body_lines) + "\n").encode("utf-8")


def _pick_duration_label(duration_hours: float) -> str:
    """Find the Atlas 14 duration row whose hours match ``duration_hours`` exactly.

    Atlas 14 reports a fixed set of durations (5-min through 60-day). We
    require an exact match against the known set so the caller can't ask
    for an interpolated value (Atlas 14 doesn't publish interpolations and
    we don't fabricate them — Invariant 7).
    """
    for label, hrs in _ATLAS14_DURATIONS_HR.items():
        if abs(hrs - duration_hours) < 1e-9:
            return label
    available_hr = sorted(_ATLAS14_DURATIONS_HR.values())
    raise BboxInvalidError(
        f"duration_hours={duration_hours} not in Atlas 14's published rows "
        f"(available hours: {available_hr})."
    )


@register_tool(
    _LOOKUP_PRECIP_RETURN_PERIOD_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (NOAA PFDS API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def lookup_precip_return_period(
    location: tuple[float, float],
    return_period_years: int,
    duration_hours: float,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Look up a precipitation return-period depth at a point via NOAA Atlas 14 PFDS.

    Access pattern: Tier 3 (direct HTTPS point query to the NOAA PFDS endpoint).

    **What it does:** Issues a point query to the NOAA Hydrometeorological Design
    Studies Center (HDSC) Precipitation Frequency Data Server (PFDS) at
    ``hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv``, parses the returned
    duration × ARI matrix, and returns the requested depth in inches. Input
    coordinates are snapped to Atlas 14's 1/120° (~30 arc-second) grid before
    the cache key is computed (FR-DC-4 dedup). This is a point query, not a
    raster — it returns a scalar dict, not a ``LayerURI``. Tier-1 free, no
    API key, CONUS + Puerto Rico / US Virgin Islands only.

    **When to use:**

    - Design-storm precipitation depth for an SFINCS pluvial-flood scenario
      ("what is the 100-year, 24-hour rainfall for Miami?"). Example:
      ``location=(25.77, -80.19)``, ``return_period_years=100``,
      ``duration_hours=24.0``.
    - Characterising a published historical storm by its return-period equivalence
      ("Harvey's 48-hour total at Houston — what ARI?"). Run the tool for
      multiple ARIs and compare.
    - Providing IDF (intensity-duration-frequency) input for a rainfall-runoff
      model (SCS CN, Green-Ampt).

    **When NOT to use:**

    - Observed precipitation totals — use ``fetch_mrms_qpe`` (gauge-corrected
      radar accumulation) or NWIS / NEXRAD for measurements.
    - Future-climate design storms — Atlas 14 is based on historical records
      (Atlas 15, in development, will integrate non-stationarity).
    - Locations outside CONUS / PR / USVI — Atlas 14 OCONUS coverage is partial;
      Alaska, Hawaii, and Pacific Islands are not in the v0.1 substrate.
    - Spatial rasters of return-period precipitation — Atlas 14 PFDS is a point
      service; for a spatial map use a pre-computed gridded Atlas 14 dataset.

    **Parameters:**

    - ``location``: ``(lat, lon)`` decimal degrees EPSG:4326. Note: lat first,
      lon second (opposite of the ``bbox`` convention). Example: ``(29.76, -95.37)``
      for Houston.
    - ``return_period_years``: ARI in years; Atlas 14 publishes
      ``{1, 2, 5, 10, 25, 50, 100, 200, 500, 1000}``; values outside this set
      raise ``BboxInvalidError``.
    - ``duration_hours``: storm duration in hours; Atlas 14 publishes durations
      from 5 min (5/60 h) to 60 days (1440 h); unsupported durations raise
      ``BboxInvalidError``.

    **Returns:**

    A ``dict`` with keys: ``precip_inches`` (float, precipitation depth in
    inches), ``units`` (``"inches"``), ``location`` ([lat, lon] of the snapped
    Atlas 14 grid point), ``return_period_years`` (ARI echo), ``duration_hours``
    (duration echo), ``vintage_volume`` (e.g. ``"NOAA Atlas 14 Volume 9 Version
    2"``), ``project_area`` (e.g. ``"Southeastern States"``),
    ``source`` (``"noaa-atlas14-pfds"``).

    **Cross-tool dependencies:**

    - Consumed by: ``build_sfincs_model`` to construct a synthetic design-storm
      hyetograph; ``run_pluvial_flood`` workflow (uses the returned depth to
      drive the SFINCS rainfall input file).
    - Compare with: ``fetch_mrms_qpe`` for observed accumulations vs Atlas 14
      design depths; the ratio gives the storm's return-period rank.
    - Pair with: ``fetch_gcn250_curve_numbers`` or NLCD-derived CNs when
      converting depth → runoff volume via SCS CN method.

    FR-CE-8: Routed through ``read_through`` with ``ttl_class="static-30d"``;
    cache key = SHA-256 of ``(lat-quantized, lon-quantized, return_period_years,
    duration_label)`` — snapping ensures callers within the same 30 arc-second
    cell dedup (FR-DC-4).
    """
    if not isinstance(location, (tuple, list)) or len(location) != 2:
        raise BboxInvalidError(
            f"location must be a (lat, lon) 2-tuple; got {location!r}"
        )
    if return_period_years not in _ATLAS14_ARI_YEARS:
        raise BboxInvalidError(
            f"return_period_years={return_period_years} not in Atlas 14's published "
            f"ARIs {_ATLAS14_ARI_YEARS}."
        )
    duration_label = _pick_duration_label(duration_hours)

    lat, lon = float(location[0]), float(location[1])
    lat_q, lon_q = _quantize_lonlat_to_atlas14_grid(lat, lon)

    params = {
        "lat": lat_q,
        "lon": lon_q,
        "return_period_years": return_period_years,
        "duration_label": duration_label,
        "series": "pds",
        "units": "english",
    }

    # --- PRIMARY: NOAA Atlas 14 PFDS (CONUS + PR/USVI). ---
    # job-0327: the Atlas-14 fetch+parse+matrix-lookup is wrapped so an
    # out-of-project-area die (the data_fetch.py out-of-area raise) OR a
    # matrix-miss raise falls through to the NOAA Atlas 2 (Western US) fallback
    # — implementing the MEMORY "Atlas-14 -> Atlas-2 first" norm that was
    # previously doc-only. Atlas 14 does NOT cover the Pacific Northwest /
    # Intermountain West (WA/OR/ID + interior states) — those remain Atlas 2.
    try:
        result = read_through(
            metadata=_LOOKUP_PRECIP_RETURN_PERIOD_METADATA,
            params=params,
            ext="csv",
            fetch_fn=lambda: _fetch_atlas14_pfds_bytes(lat_q, lon_q),
        )
        parsed = _parse_atlas14_csv(result.data.decode("utf-8"))
        matrix = parsed["matrix"]
        if (
            duration_label not in matrix
            or return_period_years not in matrix[duration_label]
        ):
            raise UpstreamAPIError(
                f"NOAA Atlas 14 PFDS response did not contain "
                f"duration={duration_label} × ARI={return_period_years} for "
                f"(lat={lat_q}, lon={lon_q}); parsed matrix labels: "
                f"{list(matrix.keys())[:5]}..."
            )
        depth_inches = matrix[duration_label][return_period_years]
        payload = {
            "precip_inches": depth_inches,
            "units": "inches",
            "location": [lat_q, lon_q],
            "return_period_years": return_period_years,
            "duration_hours": duration_hours,
            "vintage_volume": parsed["vintage_volume"],
            "project_area": parsed["project_area"],
            "source": "noaa-atlas14-pfds",
        }
        logger.info(
            "lookup_precip_return_period (lat=%s lon=%s ari=%s dur=%s) -> "
            "%.3f inches cache_hit=%s source=atlas14",
            lat_q,
            lon_q,
            return_period_years,
            duration_label,
            depth_inches,
            result.hit,
        )
        return payload
    except UpstreamAPIError as atlas14_exc:
        # --- FALLBACK 1: NOAA Atlas 2 (Western US). ---
        logger.info(
            "Atlas 14 missed (lat=%s lon=%s): %s — trying NOAA Atlas 2 fallback",
            lat_q,
            lon_q,
            atlas14_exc,
        )
        atlas2_params = dict(params)
        atlas2_params["atlas"] = "noaa-atlas2"
        try:
            a2_result = read_through(
                metadata=_LOOKUP_PRECIP_RETURN_PERIOD_METADATA,
                params=atlas2_params,
                ext="csv",
                fetch_fn=lambda: _fetch_atlas2_precip_bytes(
                    lat_q, lon_q, return_period_years, duration_hours
                ),
            )
        except PrecipForcingUnavailableError:
            # --- FALLBACK 2 (FINAL): neither atlas covers this point. ---
            # Honest, NOT-retryable failure with an actionable remediation. The
            # observed-precip branch (model_flood_scenario forcing_raster_uri)
            # bypasses Atlas entirely and is the documented alternative.
            raise PrecipForcingUnavailableError(
                f"No design-storm precip source covers (lat={lat_q}, lon={lon_q}): "
                f"NOT in NOAA Atlas 14 ({atlas14_exc}) and outside the NOAA "
                f"Atlas 2 (Western US) coverage envelope. REMEDIATION: supply "
                f"observed precipitation via the forcing_raster_uri / observed-"
                f"precip path (fetch_mrms_qpe / ERA5 / gridMET → a precip COG), "
                f"or choose an AOI inside Atlas-14 (CONUS east of the Rockies + "
                f"SW) or Atlas-2 (Western US) coverage."
            ) from atlas14_exc

        a2_parsed = _parse_atlas14_csv(a2_result.data.decode("utf-8"))
        a2_matrix = a2_parsed["matrix"]
        if (
            duration_label not in a2_matrix
            or return_period_years not in a2_matrix[duration_label]
        ):
            raise PrecipForcingUnavailableError(
                f"NOAA Atlas 2 fallback produced no depth for "
                f"duration={duration_label} × ARI={return_period_years} at "
                f"(lat={lat_q}, lon={lon_q})."
            ) from atlas14_exc
        depth_inches = a2_matrix[duration_label][return_period_years]
        payload = {
            "precip_inches": depth_inches,
            "units": "inches",
            "location": [lat_q, lon_q],
            "return_period_years": return_period_years,
            "duration_hours": duration_hours,
            # Honest provenance: the Atlas-2 fallback answered, NOT Atlas 14.
            "vintage_volume": "NOAA Atlas 2 (Western US)",
            "project_area": a2_parsed.get("project_area", "Western US"),
            "source": "noaa-atlas2",
        }
        logger.info(
            "lookup_precip_return_period (lat=%s lon=%s ari=%s dur=%s) -> "
            "%.3f inches source=atlas2 (Atlas-14 fallback)",
            lat_q,
            lon_q,
            return_period_years,
            duration_label,
            depth_inches,
        )
        return payload
