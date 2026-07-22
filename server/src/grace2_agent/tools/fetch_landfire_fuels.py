"""``fetch_landfire_fuels`` atomic tool — LANDFIRE fuels & vegetation fetcher (job-0111).

LANDFIRE (https://landfire.gov) is the USDA Forest Service + USGS programme that
publishes a nationwide 30 m raster suite for wildfire modelling: surface fuel
models (Scott & Burgan 40, Anderson 13), canopy base height, canopy bulk
density, plus vegetation cover / height / type rasters. These rasters are the
canonical inputs to FlamMap / FARSITE / FSim / ELMFIRE wildfire spread models.

Substrate strategy
==================

The kickoff offers two implementation paths:

1. The LANDFIRE Product Service (LFPS) async geoprocessing service. This is
   the historically-blessed path but in practice the public ``submitJob`` REST
   route on ``lfps.usgs.gov`` is now intercepted by the LFPS web UI's
   Next.js front-end (the front-end was rewritten — the GP service's
   ``executionType`` advertises ``esriExecutionTypeAsynchronous`` and a
   well-formed ``Layer_List`` / ``Area_of_Interest`` / ``Output_Projection``
   parameter set, but ``POST /submitJob`` returns the UI HTML, not JSON).
   Verified 2026-06-08; surfaced as ``OQ-0111-LFPS-SUBMITJOB-INTERCEPT``.

2. Pre-staged LANDFIRE 2022 nationwide ImageServer endpoints under
   ``lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022/`` (one
   ImageServer per layer per region). These ARE live and accept
   ``exportImage`` requests with a bbox + size, returning pre-clipped
   GeoTIFFs at the native 30 m grid. This is the path the v0.1 substrate
   uses — it matches the kickoff's "pre-staged LANDFIRE 2020 mosaics"
   strategy (with the year bumped to 2022, the most recent stable LF
   release at time of authoring; LF2023/2024/2025 ImageServers also
   exist but 2022 is the cited reference vintage in the kickoff).

API surface (verified live 2026-06-08)::

    base:    https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022
    layer:   LF2022_<LAYER_CODE>_CONUS   (ImageServer)
    request: GET /exportImage
             ?bbox=<min_lon>,<min_lat>,<max_lon>,<max_lat>
             &bboxSR=4326
             &size=<W>,<H>
             &format=tiff
             &imageSR=4326
             &f=image

The ImageServer returns a 16-bit signed GeoTIFF (``pixelType=S16``); the
native grid is EPSG:5070 (Albers Equal-Area CONUS) at 30 m, but the
service reprojects to ``imageSR=4326`` on the fly so the cached blob can
be consumed directly by HydroMT / rasterio without a client-side
reprojection step. Pixel values for ``fbfm40``/``fbfm13`` are the Scott
& Burgan / Anderson fuel-model integer codes (91 = water, 92 = snow/ice,
93 = agriculture, 98 = urban, 99 = barren, 101-204 = fuel models;
Anderson 1-13 + 91/92/98/99 for fbfm13). ``cbh`` / ``cbd`` carry
canopy base height (m × 10) and canopy bulk density (kg/m³ × 100)
scaled integers — see the LANDFIRE Resource Library for the per-layer
units convention.

Layer codes::

    fbfm40 -> Scott & Burgan 40 fire-behavior fuel models
    fbfm13 -> Anderson 13 fire-behavior fuel models
    cbh    -> canopy base height (m × 10)
    cbd    -> canopy bulk density (kg/m³ × 100)
    cc     -> canopy cover (percent)          [FIRE-2, ELMFIRE cc.tif]
    ch     -> canopy height (m × 10)          [FIRE-2, ELMFIRE ch.tif]

FR-TA-2 atomic tool, FR-CE-8 / FR-DC-3/4 routed through ``read_through``
so identical ``(bbox, layer)`` calls reuse the cached GeoTIFF in the
``static-30d`` / ``landfire_fuels`` cache prefix.

Cache-key composition: SHA-256 of ``(layer, bbox-rounded-to-6dp,
year="2022")``. ``supports_global_query=False`` — bbox is required (the
CONUS-wide LF2022 mosaic is ~5 GB per layer; a full-CONUS fetch is out
of scope for the atomic-tool surface, those mosaics belong in the static
data store).

Tier-1 free (no API key, no auth). User-Agent header per USGS guidelines.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import urllib.parse
from typing import Literal, Any

import requests

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = ["fetch_landfire_fuels"]

logger = logging.getLogger("grace2_agent.tools.fetch_landfire_fuels")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class LandfireFuelsError(RuntimeError):
    """Base class for fetch_landfire_fuels failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "LANDFIRE_FUELS_ERROR"
    retryable: bool = True


class LandfireFuelsLayerError(LandfireFuelsError):
    """Unknown layer requested (not in fbfm40/fbfm13/cbh/cbd)."""

    error_code = "LANDFIRE_FUELS_LAYER_INVALID"
    retryable = False


class LandfireFuelsBboxError(LandfireFuelsError):
    """Malformed / out-of-range / degenerate bbox."""

    error_code = "LANDFIRE_FUELS_BBOX_INVALID"
    retryable = False


class LandfireFuelsUpstreamError(LandfireFuelsError):
    """LANDFIRE ImageServer download or parsing failed."""

    error_code = "LANDFIRE_FUELS_UPSTREAM_ERROR"
    retryable = True


class LandfireFuelsEmptyError(LandfireFuelsError):
    """ImageServer returned a raster of all-nodata for the requested bbox
    (e.g. the bbox lies outside CONUS coverage — open ocean, off-shelf)."""

    error_code = "LANDFIRE_FUELS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: LANDFIRE LF2022 ImageServer root (verified live 2026-06-08).
_LF_BASE = "https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022"

#: LANDFIRE vintage pinned by this v0.1 substrate. Year is included in the
#: cache key so a future year upgrade (e.g. LF2023 / 2024 / 2025) is a
#: cache-key bump, not a silent staleness. Surfaced as
#: ``OQ-0111-LANDFIRE-YEAR-AUTO-ADVANCE``.
_LANDFIRE_YEAR = "2022"

#: Mapping from caller-facing layer code to the LF2022 ImageServer name.
#: CONUS-only at v0.1 — AK / HI / PRVI mosaics also exist but bbox-driven
#: regional dispatch is parked as ``OQ-0111-LANDFIRE-REGION-DISPATCH``.
_LAYER_SERVICE: dict[str, str] = {
    "fbfm40": "LF2022_FBFM40_CONUS",
    "fbfm13": "LF2022_FBFM13_CONUS",
    "cbh": "LF2022_CBH_CONUS",
    "cbd": "LF2022_CBD_CONUS",
    # FIRE-2 (ELMFIRE engine design 2026-07-07): canopy cover + canopy height —
    # the two remaining rasters of the ELMFIRE fuels stack (cc.tif / ch.tif).
    # Same LF2022 CONUS ImageServer family as the four layers above.
    "cc": "LF2022_CC_CONUS",
    "ch": "LF2022_CH_CONUS",
}

_VALID_LAYERS = frozenset(_LAYER_SERVICE.keys())

#: Per-layer one-line description for docstring / log messages.
_LAYER_DESCRIPTION: dict[str, str] = {
    "fbfm40": "Scott & Burgan 40 fire-behavior fuel models",
    "fbfm13": "Anderson 13 fire-behavior fuel models",
    "cbh": "canopy base height (m x 10, scaled int)",
    "cbd": "canopy bulk density (kg/m^3 x 100, scaled int)",
    "cc": "canopy cover (percent, int)",
    "ch": "canopy height (m x 10, scaled int)",
}

#: Per-layer units string for LayerURI.units. None for fuel-model categories
#: (no scalar unit — these are class codes).
_LAYER_UNITS: dict[str, str | None] = {
    "fbfm40": None,
    "fbfm13": None,
    "cbh": "m * 10",
    "cbd": "kg/m^3 * 100",
    "cc": "percent",
    "ch": "m * 10",
}

#: Per-layer QML style preset. Until the engine adds dedicated LANDFIRE
#: presets, fuel-model categorical layers reuse ``categorical_landcover``
#: (the existing palette is broadly category-friendly) and the continuous
#: canopy layers reuse ``continuous_dem`` (gradient ramp). Surfaced as
#: ``OQ-0111-LANDFIRE-QML-PRESETS``.
_LAYER_STYLE_PRESET: dict[str, str] = {
    "fbfm40": "categorical_landcover",
    "fbfm13": "categorical_landcover",
    "cbh": "continuous_dem",
    "cbd": "continuous_dem",
    "cc": "continuous_dem",
    "ch": "continuous_dem",
}

#: User-Agent per USGS / ESRI ImageServer guidelines.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: HTTP timeouts (seconds). ImageServer responses are quick (small TIFFs)
#: but a CONUS-region request still has to traverse a 5 GB native raster
#: server-side, so allow a generous window.
_DOWNLOAD_TIMEOUT_S = 180.0

#: ImageServer pixel-size budget. The LANDFIRE service rejects very large
#: ``size`` requests (silently returns an error JSON), and a 30 m native grid
#: can be reconstructed from the source bbox at 1 px / 30 m. We clamp the
#: requested size to [16, 4096] per axis to match the MRLC WCS sibling.
_PX_MIN = 16
_PX_MAX = 4096

#: Native cell size in metres. Used to compute ImageServer size from bbox.
_NATIVE_CELL_M = 30.0

#: GeoTIFF nodata sentinel. ImageServer returns 16-bit signed; values are
#: typed integers >0 for the fuel-model / canopy products; -32768 is the
#: standard ESRI nodata for ``S16`` (and is what shows up when bbox is
#: outside CONUS coverage).
_NODATA_S16 = -32768


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# Built DEFENSIVELY against the parallel job-0114-schema sibling that adds
# ``supports_global_query`` and ``estimate_payload_mb`` to AtomicToolMetadata.
# If the schema job lands first we want this tool to carry the field; if it
# doesn't, we fall back to a kwarg-free construction so registration still
# succeeds. This keeps Wave 1.5 cleanly parallel. See
# OQ-0111-METADATA-FIELDS.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common = dict(
        name="fetch_landfire_fuels",
        ttl_class="static-30d",
        source_class="landfire_fuels",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(
            **common,
            supports_global_query=False,  # type: ignore[call-arg]
        )
    except Exception:  # pydantic ValidationError when field absent (extra="forbid")
        logger.debug(
            "AtomicToolMetadata does not (yet) support supports_global_query; "
            "registering fetch_landfire_fuels without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# bbox helpers (kickoff cache-key spec: 6dp quantization).
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``LandfireFuelsBboxError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise LandfireFuelsBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise LandfireFuelsBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise LandfireFuelsBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise LandfireFuelsBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise LandfireFuelsBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1 m) for cache-key stability.

    Matches the kickoff cache-key spec:
    ``SHA256(layer, bbox-rounded-to-6dp, year="2022")``.
    """
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_pixel_size(
    bbox: tuple[float, float, float, float],
) -> tuple[int, int]:
    """Compute ImageServer width/height (px) for ``bbox`` at the native 30 m grid.

    Approximates m/degree at the bbox midpoint latitude — sufficient for
    sizing the ImageServer request (the server resamples to whatever size
    the client asks; matching the native grid keeps the cached blob honest).

    Clamps to ``[_PX_MIN, _PX_MAX]`` per axis so a large bbox doesn't
    overload the service.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * 111_320.0
    width_px = max(_PX_MIN, min(_PX_MAX, int(round(width_m / _NATIVE_CELL_M))))
    height_px = max(_PX_MIN, min(_PX_MAX, int(round(height_m / _NATIVE_CELL_M))))
    return width_px, height_px


# ---------------------------------------------------------------------------
# Core download function — builds the bytes callable for read_through.
# ---------------------------------------------------------------------------


def _fetch_landfire_bytes(
    layer: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Download LANDFIRE ``layer`` clipped to ``bbox`` as a GeoTIFF.

    Uses the LF2022 CONUS ImageServer's ``exportImage`` endpoint with
    ``f=image`` so the raw GeoTIFF body is returned (not a JSON wrapper).

    Raises:
        ``LandfireFuelsLayerError``: unknown layer.
        ``LandfireFuelsUpstreamError``: download / non-TIFF response /
            HTTP error.
        ``LandfireFuelsEmptyError``: returned raster is entirely
            ``_NODATA_S16`` — typically because the bbox is outside CONUS.
    """
    service_name = _LAYER_SERVICE.get(layer)
    if service_name is None:
        raise LandfireFuelsLayerError(
            f"unknown layer={layer!r}; allowed: {sorted(_VALID_LAYERS)}"
        )

    width_px, height_px = _bbox_to_pixel_size(bbox)
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"

    params = {
        "bbox": bbox_str,
        "bboxSR": "4326",
        "size": f"{width_px},{height_px}",
        "format": "tiff",
        "pixelType": "S16",
        "imageSR": "4326",
        "f": "image",
    }
    url = f"{_LF_BASE}/{service_name}/ImageServer/exportImage"
    qs_url = f"{url}?{urllib.parse.urlencode(params)}"
    logger.info(
        "fetch_landfire_fuels: GET %s (layer=%s bbox=%s size=%dx%d)",
        url,
        layer,
        bbox,
        width_px,
        height_px,
    )

    try:
        resp = requests.get(
            qs_url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_DOWNLOAD_TIMEOUT_S,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        raise LandfireFuelsUpstreamError(
            f"LANDFIRE ImageServer request failed url={url}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise LandfireFuelsUpstreamError(
            f"LANDFIRE ImageServer returned HTTP {resp.status_code} "
            f"for layer={layer} bbox={bbox}; body preview: {resp.content[:300]!r}"
        )

    body = resp.content

    # ImageServer responds with a JSON error envelope (Content-Type
    # ``application/json``) when the request shape is rejected. Detect that
    # before treating the body as a TIFF.
    ct = resp.headers.get("Content-Type", "").lower()
    if "json" in ct or body[:1] == b"{":
        raise LandfireFuelsUpstreamError(
            f"LANDFIRE ImageServer returned JSON error for layer={layer} "
            f"bbox={bbox}: {body[:400]!r}"
        )

    # TIFF magic: II*\x00 (little-endian) or MM\x00* (big-endian).
    if not (body.startswith(b"II*\x00") or body.startswith(b"MM\x00*")):
        raise LandfireFuelsUpstreamError(
            f"LANDFIRE ImageServer body is not a TIFF for layer={layer} "
            f"bbox={bbox}; content-type={ct!r}, body preview: {body[:200]!r}"
        )

    logger.info(
        "fetch_landfire_fuels: layer=%s bbox=%s -> %d bytes",
        layer,
        bbox,
        len(body),
    )

    # Empty-raster gate (codified lesson job-0086, geographic-correctness):
    # if every pixel in the returned raster is the ESRI ``S16`` nodata
    # sentinel, the bbox is outside CONUS coverage (open ocean, off-shelf).
    # Surface that as a typed empty-error rather than caching a useless blob.
    if _is_all_nodata(body):
        raise LandfireFuelsEmptyError(
            f"LANDFIRE returned all-nodata raster for layer={layer} "
            f"bbox={bbox}; bbox likely outside CONUS coverage."
        )

    return body


def _is_all_nodata(tiff_bytes: bytes) -> bool:
    """Return True iff every pixel in ``tiff_bytes`` is the ``S16`` nodata sentinel.

    Lazy-imports rasterio so the module is importable in test environments
    that mock the network call. Returns False on any read error (treat as
    "data present" — caller will see a downstream rasterio error instead).
    """
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.io import MemoryFile  # type: ignore[import-not-found]
    except ImportError:
        logger.debug(
            "fetch_landfire_fuels: rasterio unavailable; skipping nodata-only check"
        )
        return False

    try:
        with MemoryFile(tiff_bytes) as mem:
            with mem.open() as src:
                arr = src.read(1)
                # ImageServer-set nodata may be ``-32768`` or 0; the LF2022
                # CONUS source uses ``-32768`` for "no data". Defensive check
                # for both — if both are absent the raster is dense data.
                nodata = src.nodata
                if nodata is None:
                    nodata = _NODATA_S16
                # Either the registered nodata or the canonical S16 sentinel
                # being the unique pixel value means the bbox missed coverage.
                if (arr == nodata).all():
                    return True
                if (arr == _NODATA_S16).all():
                    return True
                # Also catch the "all zero" degenerate case that ImageServer
                # occasionally returns when the source has no class assignment
                # over open water.
                if (arr == 0).all():
                    return True
                return False
    except Exception as exc:  # noqa: BLE001 — diagnostic
        logger.debug(
            "fetch_landfire_fuels: nodata check raised %s; "
            "assuming data present and continuing",
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_landfire_fuels(
    bbox: tuple[float, float, float, float],
    layer: Literal["fbfm40", "fbfm13", "cbh", "cbd", "cc", "ch"] = "fbfm40",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch LANDFIRE fuels and vegetation raster for a CONUS bounding box.

    **What it does:** Downloads a 30 m raster from the LANDFIRE LF2022 CONUS
    ImageServer (USDA Forest Service + USGS), clips it server-side to the
    requested bbox, and returns a CRS-tagged GeoTIFF via the 30-day cache.
    Six layers are available: Scott and Burgan 40 surface fuel models
    (``fbfm40``), Anderson 13 fuel models (``fbfm13``), canopy base height
    (``cbh``), canopy bulk density (``cbd``), canopy cover (``cc``), and
    canopy height (``ch``) — together the full ELMFIRE canopy-fuels stack.

    **When to use:**
    - User asks for wildfire fuel conditions, fuel maps, or fire-behavior
      inputs for a specific area ("show me the fuel models near Flagstaff").
    - Building a wildfire spread model run with FlamMap, FARSITE, FSim, or
      ELMFIRE — those engines require LANDFIRE fuel rasters as primary inputs.
    - Displaying ground-fuel context or canopy structure as a visualization
      layer in a wildfire-risk narrative.
    - Any workflow step that needs the USDA / USGS canonical nationwide
      wildfire-modelling substrate at 30 m resolution.

    **When NOT to use:**
    - DO NOT use for live fire perimeters — LANDFIRE is a static fuels grid;
      use ``fetch_nifc_fire_perimeters`` or ``fetch_firms_active_fire`` for
      active and recent burn extents.
    - DO NOT use for burn-severity products — LANDFIRE Disturbance services
      carry post-fire severity rasters; they are a separate tool surface not
      covered here.
    - DO NOT use for weather / climate forcing — LANDFIRE is fuels-only;
      use ``fetch_mrms_qpe``, ``fetch_hrrr_forecast``, or
      ``fetch_raws_weather`` for meteorological inputs.
    - DO NOT use outside CONUS at v0.1 — AK/HI/PRVI LANDFIRE mosaics exist
      but regional dispatch is deferred (``OQ-0111-LANDFIRE-REGION-DISPATCH``).

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(min_lon, min_lat,
      max_lon, max_lat)`` in EPSG:4326. Required; CONUS-only.
      Example: ``(-112.0, 34.5, -111.0, 35.5)`` (north-central Arizona).
    - ``layer`` (str, default ``"fbfm40"``): one of ``"fbfm40"`` (Scott and
      Burgan 40; integer codes 101-204 + 91/92/93/98/99 special classes),
      ``"fbfm13"`` (Anderson 13; codes 1-13 + specials), ``"cbh"`` (canopy
      base height, m x 10 scaled int), ``"cbd"`` (canopy bulk density,
      kg/m^3 x 100 scaled int), ``"cc"`` (canopy cover, percent int),
      ``"ch"`` (canopy height, m x 10 scaled int).

    **Returns:** A ``LayerURI`` pointing at a GeoTIFF in the cache bucket
    (``gs://grace-2-hazard-prod-cache/cache/static-30d/landfire_fuels/<key>.tif``).
    ``layer_type="raster"``, ``role="primary"``. ``units`` is populated for
    continuous canopy layers (``"m * 10"`` / ``"kg/m^3 * 100"``), ``None``
    for categorical fuel-model layers. EPSG:4326, 30 m native resolution,
    LANDFIRE 2022 vintage.

    **Cross-tool dependencies:**
    - Feeds: FlamMap / FARSITE / FSim wildfire spread workflows (deferred),
      any engine step that needs fuel category codes per pixel.
    - Pairs with: ``fetch_raws_weather`` or ``fetch_hrrr_forecast`` for the
      meteorological forcing stack; ``fetch_dem`` for terrain-slope inputs.
    - Provides ``fbfm40`` / ``fbfm13`` via ``role="primary"`` so QGIS Server
      renders with the ``categorical_landcover`` style preset; canopy layers
      use ``continuous_dem`` ramp.

    FR-CE-8: Routed through ``read_through`` so identical ``(bbox, layer)``
    calls reuse the cached GeoTIFF. Cache key includes ``(layer,
    bbox-rounded-to-6dp, year="2022")`` so a future year upgrade (e.g.
    LF2023) is a cache-key change, not a silent staleness
    (``OQ-0111-LANDFIRE-YEAR-AUTO-ADVANCE``).
    """
    # Defensive validations on the registered surface.
    if layer not in _VALID_LAYERS:
        raise LandfireFuelsLayerError(
            f"unknown layer={layer!r}; allowed: {sorted(_VALID_LAYERS)}"
        )
    _validate_bbox(bbox)

    # Quantize bbox to 6dp for cache-key stability (kickoff spec).
    q_bbox = _round_bbox_to_6dp(bbox)

    params = {
        "layer": layer,
        "bbox": list(q_bbox),
        "year": _LANDFIRE_YEAR,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_landfire_bytes(layer, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_landfire_fuels is cacheable; uri must be set by read_through"
    )

    layer_label_short = {
        "fbfm40": "FBFM40",
        "fbfm13": "FBFM13",
        "cbh": "Canopy Base Height",
        "cbd": "Canopy Bulk Density",
        "cc": "Canopy Cover",
        "ch": "Canopy Height",
    }.get(layer, layer.upper())

    return LayerURI(
        layer_id=(
            f"landfire-{layer}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"LANDFIRE 2022 - {layer_label_short}",
        layer_type="raster",
        uri=result.uri,
        style_preset=_LAYER_STYLE_PRESET[layer],
        role="primary",
        units=_LAYER_UNITS[layer],
    )
