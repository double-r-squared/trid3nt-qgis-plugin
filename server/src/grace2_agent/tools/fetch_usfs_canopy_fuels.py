"""``fetch_usfs_canopy_fuels`` atomic tool — USFS canopy base height / bulk density rasters (job-A14).

Fetches USFS canopy structure rasters — canopy base height (CBH) and canopy
bulk density (CBD) — from the LANDFIRE LF2022 CONUS ImageServer hosted at
``lfps.usgs.gov``. CBH and CBD are the canonical canopy-fuel inputs to
surface-to-crown fire transition models (FlamMap, FARSITE, FSim, ELMFIRE,
QUIC-Fire), characterising how close to the ground the tree canopy begins
and how densely packed the canopy fuel mass is.

Source authority
================

LANDFIRE is a joint USDA Forest Service + USGS programme. The LF2022
vintage (most recent stable nationwide release at time of authoring)
publishes CBH and CBD as nationwide 30 m rasters in Albers Equal-Area
CONUS (EPSG:5070). The ImageServer reprojects to the requested ``imageSR``
on the fly; this tool requests ``imageSR=4326`` so outputs are in geographic
coordinates and can be consumed by HydroMT / rasterio without a client-side
reprojection step.

Relationship to ``fetch_landfire_fuels``
========================================

``fetch_landfire_fuels`` (job-0111) covers all four LANDFIRE fuel-layer
codes (``fbfm40``, ``fbfm13``, ``cbh``, ``cbd``). This tool is a dedicated,
semantically focused surface for the canopy-fuel subset only: it exposes
``cbh`` and ``cbd`` under a name that the LLM will correctly associate with
user intents involving canopy structure, crown-fire risk, or spotting
potential — even when the user does not mention "LANDFIRE". The
``fetch_landfire_fuels`` docstring similarly redirects canopy-specific
queries here to avoid ambiguity.

Pixel-value conventions (LANDFIRE Resource Library)
===================================================

    CBH: canopy base height in metres × 10 (scaled integer S16).
         Value 0  = non-burnable / open water.
         Value 1  = non-burnable land.
         Nodata   = -32768 (ESRI S16 sentinel).
         Example: pixel 50 → 5.0 m base height.

    CBD: canopy bulk density in kg/m³ × 100 (scaled integer S16).
         Value 0  = non-burnable / open water.
         Value 1  = trace (< 0.01 kg/m³).
         Nodata   = -32768.
         Example: pixel 12 → 0.12 kg/m³ bulk density.

Endpoint (verified live 2026-06-09)::

    base:    https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022
    CBH:     LF2022_CBH_CONUS/ImageServer/exportImage
    CBD:     LF2022_CBD_CONUS/ImageServer/exportImage
    method:  GET
    params:
        bbox=<min_lon>,<min_lat>,<max_lon>,<max_lat>
        bboxSR=4326
        size=<W>,<H>          (clamped to [16, 4096] per axis)
        format=tiff
        pixelType=S16
        imageSR=4326
        f=image

The response is a raw GeoTIFF (``image/tiff``). No authentication required.

Cache: ``static-30d`` / ``usfs_canopy_fuels`` prefix. Cache key includes
``(layer, bbox-rounded-to-6dp, year="2022")`` so a future year upgrade
(LF2023/LF2024/LF2025) is a cache-miss, not a silent staleness.

``supports_global_query=False`` — CONUS-only at v0.1; the LF2022 nationwide
CBH/CBD mosaic is ~5 GB per layer; a full-CONUS fetch is out of scope.

FR-TA-2 atomic tool; returns ``LayerURI``.
FR-CE-8 / FR-DC-3/4: routed through ``read_through``.
"""

from __future__ import annotations

import logging
import math
import os
import urllib.parse
from typing import Literal, Any

import requests

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_usfs_canopy_fuels",
    "estimate_payload_mb",
    "USFSCanopyFuelsError",
    "USFSCanopyFuelsBboxError",
    "USFSCanopyFuelsLayerError",
    "USFSCanopyFuelsUpstreamError",
    "USFSCanopyFuelsEmptyError",
]

logger = logging.getLogger("grace2_agent.tools.fetch_usfs_canopy_fuels")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class USFSCanopyFuelsError(RuntimeError):
    """Base class for fetch_usfs_canopy_fuels failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "USFS_CANOPY_FUELS_ERROR"
    retryable: bool = True


class USFSCanopyFuelsBboxError(USFSCanopyFuelsError):
    """Malformed / out-of-range / degenerate bbox."""

    error_code = "USFS_CANOPY_FUELS_BBOX_INVALID"
    retryable = False


class USFSCanopyFuelsLayerError(USFSCanopyFuelsError):
    """Unknown layer requested (not ``cbh`` or ``cbd``)."""

    error_code = "USFS_CANOPY_FUELS_LAYER_INVALID"
    retryable = False


class USFSCanopyFuelsUpstreamError(USFSCanopyFuelsError):
    """LANDFIRE ImageServer download or parsing failed (HTTP, network, JSON envelope)."""

    error_code = "USFS_CANOPY_FUELS_UPSTREAM_ERROR"
    retryable = True


class USFSCanopyFuelsEmptyError(USFSCanopyFuelsError):
    """ImageServer returned an all-nodata raster — bbox is outside CONUS coverage."""

    error_code = "USFS_CANOPY_FUELS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: LANDFIRE LF2022 ImageServer root (verified live 2026-06-09).
_LF_BASE = "https://lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022"

#: LANDFIRE vintage pinned for this substrate. Included in the cache key so
#: upgrading to LF2023/2024/2025 is a cache-key bump, not silent staleness.
#: Surfaced as ``OQ-A14-CANOPY-FUELS-YEAR-ADVANCE``.
_LANDFIRE_YEAR = "2022"

#: Mapping from caller-facing layer code to the LF2022 ImageServer service name.
#: CONUS-only at v0.1 — AK / HI / PRVI mosaics exist but regional dispatch is
#: parked as ``OQ-A14-CANOPY-FUELS-REGION-DISPATCH``.
_LAYER_SERVICE: dict[str, str] = {
    "cbh": "LF2022_CBH_CONUS",
    "cbd": "LF2022_CBD_CONUS",
}

_VALID_LAYERS = frozenset(_LAYER_SERVICE.keys())

#: Human-readable label per layer, used in LayerURI.name.
_LAYER_LABEL: dict[str, str] = {
    "cbh": "Canopy Base Height",
    "cbd": "Canopy Bulk Density",
}

#: Units per layer (scaled-integer convention per LANDFIRE Resource Library).
_LAYER_UNITS: dict[str, str] = {
    "cbh": "m * 10",
    "cbd": "kg/m^3 * 100",
}

#: QML style presets — continuous ramp for both (no symbolic classification).
_LAYER_STYLE_PRESET: dict[str, str] = {
    "cbh": "continuous_dem",
    "cbd": "continuous_dem",
}

#: User-Agent per USGS / ESRI ImageServer guidelines.
_USER_AGENT = (
    "grace-2/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/GRACE-2; agent@grace-2.dev)"
)

#: HTTP download timeout (seconds). Allow a generous window for large-ish bboxes.
_DOWNLOAD_TIMEOUT_S = 180.0

#: Per-axis pixel-size clamp: [16, 4096]. Avoids ImageServer rejection on
#: implausibly small or large size requests and prevents oversized responses.
_PX_MIN = 16
_PX_MAX = 4096

#: Native cell size in metres (LF2022 CBH/CBD are 30 m Albers grids).
_NATIVE_CELL_M = 30.0

#: Standard ESRI S16 nodata sentinel used by LF2022 CBH/CBD.
_NODATA_S16 = -32768

#: Payload estimate: ~0.5 MB per square degree of bbox (GeoTIFF at 30 m native).
#: Clipped to [0.05, 50] MB.
_PAYLOAD_MB_PER_SQ_DEG = 0.5
_PAYLOAD_MIN_MB = 0.05
_PAYLOAD_MAX_MB = 50.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_usfs_canopy_fuels",
    ttl_class="static-30d",
    source_class="usfs_canopy_fuels",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# FR-DC-9 payload-MB estimator hook.
# ---------------------------------------------------------------------------


def estimate_payload_mb(**args: Any) -> float:
    """Estimate the GeoTIFF payload size for a canopy-fuels fetch.

    Uses a ~0.5 MB per square degree heuristic, clipped to [0.05, 50] MB.
    A small city bbox (~0.5 x 0.5 deg) is ~0.1 MB; a state-sized bbox
    (~10 x 10 deg) exceeds the 25 MB chat-warning gate.

    Args (read from kwargs):
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
    """
    bbox = args.get("bbox")
    if not bbox or len(bbox) != 4:
        return _PAYLOAD_MAX_MB
    try:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return _PAYLOAD_MAX_MB
    width = max(0.0, max_lon - min_lon)
    height = max(0.0, max_lat - min_lat)
    area_sq_deg = width * height
    est = area_sq_deg * _PAYLOAD_MB_PER_SQ_DEG
    return max(_PAYLOAD_MIN_MB, min(_PAYLOAD_MAX_MB, est))


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``USFSCanopyFuelsBboxError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise USFSCanopyFuelsBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise USFSCanopyFuelsBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise USFSCanopyFuelsBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise USFSCanopyFuelsBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise USFSCanopyFuelsBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox coordinates to 6 decimal places (~0.1 m) for cache-key stability."""
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _bbox_to_pixel_size(
    bbox: tuple[float, float, float, float],
) -> tuple[int, int]:
    """Compute ImageServer width/height (px) for ``bbox`` at the native 30 m grid.

    Approximates m/degree at the bbox midpoint latitude. Clamps to
    ``[_PX_MIN, _PX_MAX]`` per axis.
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
# Core download function.
# ---------------------------------------------------------------------------


def _fetch_canopy_fuels_bytes(
    layer: str,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Download USFS canopy-fuels ``layer`` clipped to ``bbox`` as a GeoTIFF.

    Uses the LF2022 CONUS ImageServer's ``exportImage`` endpoint with
    ``f=image`` so the raw GeoTIFF body is returned (no JSON wrapper).

    Raises:
        ``USFSCanopyFuelsLayerError``: unknown layer code.
        ``USFSCanopyFuelsUpstreamError``: network failure, non-200 HTTP,
            JSON error envelope, or non-TIFF body.
        ``USFSCanopyFuelsEmptyError``: all-nodata raster (bbox outside
            CONUS LANDFIRE coverage).
    """
    service_name = _LAYER_SERVICE.get(layer)
    if service_name is None:
        raise USFSCanopyFuelsLayerError(
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
        "fetch_usfs_canopy_fuels: GET %s (layer=%s bbox=%s size=%dx%d)",
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
        raise USFSCanopyFuelsUpstreamError(
            f"LANDFIRE ImageServer request failed url={url}: {exc}"
        ) from exc

    if resp.status_code != 200:
        raise USFSCanopyFuelsUpstreamError(
            f"LANDFIRE ImageServer returned HTTP {resp.status_code} "
            f"for layer={layer} bbox={bbox}; body preview: {resp.content[:300]!r}"
        )

    body = resp.content

    # ImageServer returns a JSON error envelope (Content-Type
    # ``application/json``) when the request shape is rejected.
    ct = resp.headers.get("Content-Type", "").lower()
    if "json" in ct or body[:1] == b"{":
        raise USFSCanopyFuelsUpstreamError(
            f"LANDFIRE ImageServer returned JSON error for layer={layer} "
            f"bbox={bbox}: {body[:400]!r}"
        )

    # TIFF magic: II*\x00 (little-endian) or MM\x00* (big-endian).
    if not (body.startswith(b"II*\x00") or body.startswith(b"MM\x00*")):
        raise USFSCanopyFuelsUpstreamError(
            f"LANDFIRE ImageServer body is not a TIFF for layer={layer} "
            f"bbox={bbox}; content-type={ct!r}, body preview: {body[:200]!r}"
        )

    logger.info(
        "fetch_usfs_canopy_fuels: layer=%s bbox=%s -> %d bytes",
        layer,
        bbox,
        len(body),
    )

    # Empty-raster gate: if all pixels are the S16 nodata sentinel the bbox
    # is outside CONUS coverage (open ocean, off-shelf).
    if _is_all_nodata(body):
        raise USFSCanopyFuelsEmptyError(
            f"LANDFIRE returned all-nodata raster for layer={layer} "
            f"bbox={bbox}; bbox likely outside CONUS coverage."
        )

    return body


def _is_all_nodata(tiff_bytes: bytes) -> bool:
    """Return True iff every pixel in ``tiff_bytes`` is the S16 nodata sentinel.

    Lazy-imports rasterio so the module is importable in test environments that
    mock the network call. Returns False on any read error (treat as "data
    present" — caller will see a downstream rasterio error instead).
    """
    try:
        import rasterio  # type: ignore[import-not-found]
        from rasterio.io import MemoryFile  # type: ignore[import-not-found]
    except ImportError:
        logger.debug(
            "fetch_usfs_canopy_fuels: rasterio unavailable; skipping nodata-only check"
        )
        return False

    try:
        with MemoryFile(tiff_bytes) as mem:
            with mem.open() as src:
                arr = src.read(1)
                nodata = src.nodata
                if nodata is None:
                    nodata = _NODATA_S16
                if (arr == nodata).all():
                    return True
                if (arr == _NODATA_S16).all():
                    return True
                # Catch the "all-zero" degenerate case (non-burnable CONUS
                # areas such as open-water bodies may return all zeros).
                if (arr == 0).all():
                    return True
                return False
    except Exception as exc:  # noqa: BLE001 — diagnostic
        logger.debug(
            "fetch_usfs_canopy_fuels: nodata check raised %s; "
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
def fetch_usfs_canopy_fuels(
    bbox: tuple[float, float, float, float],
    layer: Literal["cbh", "cbd"] = "cbh",
    # Absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders per job-0164).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch USFS canopy base height (CBH) or canopy bulk density (CBD) raster for a CONUS bbox.

    Fetches a 30 m LANDFIRE LF2022 canopy-fuel raster for the requested area.
    CBH (canopy base height) and CBD (canopy bulk density) are the two canopy
    structural inputs required by surface-to-crown fire transition models
    (FlamMap, FARSITE, FSim, ELMFIRE). CBH quantifies how close to the ground
    the tree canopy begins; CBD quantifies how densely packed the canopy fuel
    mass is. Together they determine whether a surface fire can transition to a
    crown fire and how fast the crown fire runs.

    When to use:
        - User asks for canopy base height, canopy fuel loading, canopy bulk
          density, or crown fuel structure for a CONUS area.
        - Agent needs CBH or CBD as input to a wildfire-spread or crown-fire
          model (FlamMap, FARSITE, FSim, ELMFIRE, QUIC-Fire).
        - User asks about spotting potential, torching probability, or
          crown-fire transition risk for a forested area.
        - Workflow needs to assess how the forest canopy structure affects
          fire behaviour above the surface fuel bed.

    When NOT to use:
        - DO NOT use for surface fuel models (Anderson 13 or Scott-Burgan 40):
          use ``fetch_landfire_fuels`` with ``layer="fbfm40"`` or
          ``layer="fbfm13"`` instead.
        - DO NOT use for active fire perimeters or current fire detections:
          use ``fetch_nifc_fire_perimeters`` or ``fetch_firms_active_fire``.
        - DO NOT use for historic burn severity or burned-area boundaries:
          use ``fetch_mtbs_burn_severity``.
        - DO NOT use for non-CONUS coverage: AK / HI / PRVI LANDFIRE mosaics
          exist but are not supported at v0.1
          (``OQ-A14-CANOPY-FUELS-REGION-DISPATCH``).

    Parameters:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
            Required (``supports_global_query=False``); CONUS-only at v0.1.
            Example for San Diego fire country:
            ``(-117.5, 32.5, -117.0, 33.0)``.
        layer: Which canopy-fuel layer to fetch:
            ``"cbh"`` (default): canopy base height, metres × 10 (S16
                scaled integer). Pixel 50 → 5.0 m height. Value 0 =
                non-burnable / open water; 1 = non-burnable land.
                Used by fire models as the height at which crown-fire
                transition can begin.
            ``"cbd"``: canopy bulk density, kg/m³ × 100 (S16 scaled
                integer). Pixel 12 → 0.12 kg/m³. Value 0 = non-burnable.
                Used by fire models to compute crown-fire rate of spread.

    Returns:
        ``LayerURI`` pointing at a GeoTIFF in the cache bucket:
        ``gs://grace-2-hazard-prod-cache/cache/static-30d/usfs_canopy_fuels/<key>.tif``
        The raster is ``layer_type="raster"``, ``role="primary"``, with
        ``units`` set to ``"m * 10"`` for CBH or ``"kg/m^3 * 100"`` for CBD.
        Downstream fire-spread models consume the raw S16 pixel values;
        divide by 10 (CBH) or 100 (CBD) to get SI units.

    Cross-tool dependencies:
        - Paired with ``fetch_landfire_fuels`` (``layer="fbfm40"`` or
          ``"fbfm13"``) to assemble the full 13-layer FARSITE / FlamMap
          canopy-fuel input deck.
        - Consumed by the fire-spread workflow alongside surface fuel models,
          DEM (``fetch_dem``), and weather forcing (``fetch_hrrr_forecast``
          or ``fetch_raws_weather``) for crown-fire modelling.
        - Can be intersected with ``fetch_nifc_fire_perimeters`` to assess
          whether active fire is entering a high-CBD / low-CBH area.

    Raises:
        ``USFSCanopyFuelsLayerError``: ``layer`` not in ``{"cbh", "cbd"}``;
            non-retryable.
        ``USFSCanopyFuelsBboxError``: bbox malformed / out of range /
            degenerate; non-retryable.
        ``USFSCanopyFuelsUpstreamError``: LANDFIRE ImageServer network failure,
            HTTP non-200, JSON error envelope, or non-TIFF body; retryable.
        ``USFSCanopyFuelsEmptyError``: all-nodata raster — bbox outside CONUS
            LANDFIRE coverage; non-retryable.

    Cache: ``static-30d`` (FR-DC-2). LANDFIRE LF2022 is a static product;
    the 30-day window is well inside the publication cadence. Cache key:
    SHA-256 of ``(layer, bbox-rounded-to-6dp, year="2022")``. A future
    LF2023 upgrade is a cache-key bump, not silent staleness.

    Source-tier: FR-HEP-2 Tier 1 (USDA Forest Service + USGS authoritative
    canopy-fuel programme). No API key required. Payload estimate: ~0.5 MB
    per square degree, clipped to [0.05, 50] MB.
    """
    # Validate inputs.
    if layer not in _VALID_LAYERS:
        raise USFSCanopyFuelsLayerError(
            f"unknown layer={layer!r}; allowed: {sorted(_VALID_LAYERS)}"
        )
    _validate_bbox(bbox)

    # Quantize bbox to 6dp for cache-key stability.
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
        fetch_fn=lambda: _fetch_canopy_fuels_bytes(layer, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_usfs_canopy_fuels is cacheable; uri must be set by read_through"
    )

    label = _LAYER_LABEL[layer]

    return LayerURI(
        layer_id=(
            f"usfs-canopy-{layer}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"USFS LANDFIRE 2022 — {label}",
        layer_type="raster",
        uri=result.uri,
        style_preset=_LAYER_STYLE_PRESET[layer],
        role="primary",
        units=_LAYER_UNITS[layer],
    )
