"""``fetch_flood_extent_observation`` atomic tool -- observed (satellite) flood extent.

Fetches an OBSERVED, agency-DERIVED flood-extent classification for a bbox as
a categorical COG -- the benchmark wet/dry/flood raster
``compute_flood_extent_skill`` scores a modeled flood surface against.

DEDUP (checked at build): our existing satellite fetchers do NOT already yield
a derived event flood extent. ``fetch_sentinel1_sar`` returns raw dB
backscatter (thresholding it into wet/dry is a playground analysis, not a
fetch); ``fetch_jrc_global_surface_water`` returns the 1984-2021 long-term
water-occurrence baseline, not an EVENT extent. So this is a genuine gap.

PRIMARY source: NASA LANCE MODIS/Aqua+Terra Global Flood Product L3 3-day
(``MCDWD_L3_F3_NRT``, 250 m, fully anonymous over
``nrt3.modaps.eosdis.nasa.gov``). Native classes are preserved: 0 = no water,
1 = surface (reference) water, 2 = recurring flood, 3 = flood (unusual),
255 = insufficient data / cloud (nodata).

HONEST LIMITS carried in the envelope caveats AND the build report open_issues:
this is the NEAR-REAL-TIME product (a rolling recent window, provisional);
arbitrary HISTORICAL event extents need the reprocessed MCDWD_L3 archive
(LAADS, Earthdata-token-gated) or Copernicus GFM/EMS Sentinel-1 SAR products
(CDSE/EGMS credentials) -- neither is an anonymous programmatic source, so
they are a documented follow-up, not a silent dead end.

``supports_global_query=False`` (a bbox is required).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import tempfile
import urllib.error
import urllib.request
from typing import Any

from trid3nt_contracts.execution import LayerURI, LegendClass, LegendKey
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_flood_extent_observation",
    "estimate_payload_mb",
    "FloodExtentLayerURI",
    "FloodExtentError",
    "FloodExtentInputError",
    "FloodExtentNoCoverageError",
    "FloodExtentUpstreamError",
    "_resolve_datetime",
    "_latest_available",
    "_tiles_for_bbox",
    "_download_tile",
    "_read_tile_window",
    "_tile_bounds",
    "MCDWD_CLASSES",
    "NODATA",
]

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers.hydrology.fetch_flood_extent_observation"
)


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class FloodExtentError(RuntimeError):
    """Base class for fetch_flood_extent_observation failures."""

    error_code: str = "FLOOD_EXTENT_ERROR"
    retryable: bool = True


class FloodExtentInputError(FloodExtentError):
    """Bad inputs -- missing/malformed/too-large bbox, unparseable date."""

    error_code = "FLOOD_EXTENT_INPUT_ERROR"
    retryable = False


class FloodExtentNoCoverageError(FloodExtentError):
    """No flood product covers the bbox/date, or the mosaic is entirely nodata."""

    error_code = "FLOOD_EXTENT_NO_COVERAGE"
    retryable = False


class FloodExtentUpstreamError(FloodExtentError):
    """A LANCE listing / tile download / raster read/write failed (retryable)."""

    error_code = "FLOOD_EXTENT_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: LANCE NRT MODIS Global Flood Product, 3-day composite, collection 61.
_PRODUCT = "MCDWD_L3_F3_NRT"
_LANCE_API = (
    "https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/details/allData/61/"
    + _PRODUCT
)
_LANCE_ARCHIVE = (
    "https://nrt3.modaps.eosdis.nasa.gov/archive/allData/61/" + _PRODUCT
)

#: MCDWD native pixel encoding (RevE user guide). Value -> label.
MCDWD_CLASSES: dict[int, str] = {
    0: "No water",
    1: "Surface water (reference)",
    2: "Recurring flood",
    3: "Flood water",
}

#: Insufficient-data / cloud fill (nodata).
NODATA = 255

#: Native tile grid: 10-degree geographic tiles, 4800 px (~0.00208333 deg cell).
_TILE_DEG = 10.0
_CELL_DEG = 10.0 / 4800.0  # ~232 m

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)
_HTTP_TIMEOUT = 120.0

#: bbox area guardrail (deg^2) -- keeps the covering-tile count bounded.
_MAX_BBOX_DEG2 = 50.0

_STYLE_PRESET = "flood_extent_observed"

#: Categorical render colors. GDAL GTiff/COG color tables preserve RGB but
#: honor ALPHA only on the nodata index (every other index is forced opaque);
#: value 0 stays (0,0,0,0) so it is the repo's opaque-black "filler" convention
#: (publish_layer drops opaque-black filler), leaving only the water classes
#: colorized and nodata (255) transparent.
_COLORS: dict[int, tuple[int, int, int, int]] = {
    0: (0, 0, 0, 0),          # no water -> filler (renders as background)
    1: (146, 197, 222, 255),  # reference surface water -> light blue
    2: (244, 165, 130, 255),  # recurring flood -> orange
    3: (202, 0, 32, 255),     # flood (unusual) -> red
    NODATA: (0, 0, 0, 0),     # insufficient data -> transparent (nodata)
}

_CAVEATS = [
    "Satellite flood mapping UNDER-detects flooding beneath vegetation canopy "
    "and in dense urban areas -- this SAR-and-optical detection limit applies "
    "to this MODIS/MCDWD product too; validate against ground truth (surveyed "
    "high-water marks) before treating the extent as complete.",
    "MODIS MCDWD is 250 m: narrow channels, small ponds, and sub-pixel "
    "flooding are missed, and cloud cover in the optical compositing window "
    "leaves data gaps (nodata=255).",
    "This is the NEAR-REAL-TIME 3-day product (a rolling recent window, "
    "provisional) -- NOT the QA'd/reprocessed archive; a specific historical "
    "event may be unavailable.",
]


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_flood_extent_observation",
    ttl_class="semi-static-7d",
    source_class="mcdwd_flood_extent",
    cacheable=True,
    supports_global_query=False,
)


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    date: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output COG size in MB (uint8, DEFLATE, ~250 m)."""
    if bbox is None:
        return 0.5
    try:
        w, s, e, n = bbox
        wpx = max(1, round((e - w) / _CELL_DEG))
        hpx = max(1, round((n - s) / _CELL_DEG))
        return max(0.01, wpx * hpx * 1.0 * 0.15 / 1_000_000.0)  # ~0.15 B/px compressed
    except (TypeError, ValueError):
        return 0.5


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise FloodExtentInputError(
            f"bbox must be (west, south, east, north); got {bbox!r}"
        )
    try:
        west, south, east, north = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise FloodExtentInputError(f"bbox has non-numeric values: {bbox!r}") from exc
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise FloodExtentInputError(f"bbox has non-finite values: {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise FloodExtentInputError(f"bbox lon out of [-180, 180]: {bbox!r}")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise FloodExtentInputError(f"bbox lat out of [-90, 90]: {bbox!r}")
    if west >= east or south >= north:
        raise FloodExtentInputError(f"bbox is degenerate (min < max): {bbox!r}")
    area = (east - west) * (north - south)
    if area > _MAX_BBOX_DEG2:
        raise FloodExtentInputError(
            f"bbox area {area:.1f} deg^2 exceeds the {_MAX_BBOX_DEG2:.0f} deg^2 "
            "guardrail; request a smaller AOI."
        )
    return (west, south, east, north)


# ---------------------------------------------------------------------------
# LANCE listing + date resolution (network seams; patched offline).
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float = _HTTP_TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        raise FloodExtentUpstreamError(
            f"LANCE returned HTTP {exc.code} for {url}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise FloodExtentUpstreamError(f"Network error fetching {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FloodExtentUpstreamError(f"Timed out after {timeout}s fetching {url}") from exc


def _list_dir_names(url: str) -> list[str]:
    """Return the child directory names under a LANCE content-details URL."""
    raw = _http_get(url)
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FloodExtentUpstreamError(f"LANCE listing is not valid JSON: {exc}") from exc
    return [
        str(e.get("name"))
        for e in obj.get("content", [])
        if e.get("resourceType") == "Directory" and str(e.get("name") or "").isdigit()
    ]


def _latest_available() -> tuple[int, int]:
    """Newest available ``(year, day_of_year)`` in the NRT archive."""
    years = [int(n) for n in _list_dir_names(_LANCE_API + "/")]
    if not years:
        raise FloodExtentNoCoverageError(
            "the LANCE MCDWD flood archive listing returned no years."
        )
    year = max(years)
    days = [int(n) for n in _list_dir_names(f"{_LANCE_API}/{year}/")]
    if not days:
        raise FloodExtentNoCoverageError(
            f"no days available under the {year} MCDWD flood archive."
        )
    return year, max(days)


def _resolve_datetime(date: str | None) -> tuple[int, int]:
    """Resolve ``date`` (ISO ``YYYY-MM-DD``) to ``(year, doy)``; None -> latest."""
    if date is None or not str(date).strip():
        return _latest_available()
    try:
        d = _dt.date.fromisoformat(str(date).strip())
    except ValueError as exc:
        raise FloodExtentInputError(
            f"date={date!r} is not a valid ISO date (YYYY-MM-DD): {exc}"
        ) from exc
    return d.year, d.timetuple().tm_yday


# ---------------------------------------------------------------------------
# Tiling + tile IO.
# ---------------------------------------------------------------------------


def _tile_bounds(h: int, v: int) -> tuple[float, float, float, float]:
    """(west, south, east, north) of MCDWD tile h{hh}v{vv}."""
    west = -180.0 + _TILE_DEG * h
    north = 90.0 - _TILE_DEG * v
    return (west, north - _TILE_DEG, west + _TILE_DEG, north)


def _tiles_for_bbox(
    bbox: tuple[float, float, float, float]
) -> list[tuple[int, int]]:
    """The (h, v) tiles overlapping ``bbox`` (clamped to the valid grid)."""
    west, south, east, north = bbox
    h0 = max(0, int(math.floor((west + 180.0) / _TILE_DEG)))
    h1 = min(35, int(math.floor((east + 180.0) / _TILE_DEG)))
    v0 = max(0, int(math.floor((90.0 - north) / _TILE_DEG)))
    v1 = min(17, int(math.floor((90.0 - south) / _TILE_DEG)))
    return [(h, v) for v in range(v0, v1 + 1) for h in range(h0, h1 + 1)]


def _download_tile(year: int, doy: int, h: int, v: int) -> bytes:
    """Download one MCDWD tile GeoTIFF; ``b""`` when the tile is absent (404)."""
    fname = f"{_PRODUCT}.A{year}{doy:03d}.h{h:02d}v{v:02d}.061.tif"
    url = f"{_LANCE_ARCHIVE}/{year}/{doy:03d}/{fname}"
    logger.info("fetch_flood_extent_observation: GET %s", url)
    return _http_get(url)


def _read_tile_window(
    tile_bytes: bytes,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Reproject+window a tile's band-1 to EPSG:4326 at ``bbox`` (nearest).

    Nearest resampling (categorical classes). Returns a uint8 (H, W) array with
    ``NODATA`` filling pixels with no data. Raises ``FloodExtentUpstreamError``
    on a read failure.
    """
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.warp import Resampling, reproject

    try:
        with MemoryFile(tile_bytes) as mem, mem.open() as src:
            dst_transform = rasterio.transform.from_bounds(
                bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
            )
            dst = np.full((height_px, width_px), NODATA, dtype="uint8")
            src_nodata = src.nodata if src.nodata is not None else NODATA
            reproject(
                source=rasterio.band(src, 1),
                destination=dst,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.nearest,
                src_nodata=src_nodata,
                dst_nodata=NODATA,
            )
        return dst
    except Exception as exc:  # noqa: BLE001
        raise FloodExtentUpstreamError(f"MCDWD tile read failed: {exc}") from exc


# ---------------------------------------------------------------------------
# COG assembly.
# ---------------------------------------------------------------------------


def _fetch_flood_extent_cog_bytes(
    bbox: tuple[float, float, float, float], year: int, doy: int
) -> bytes:
    """Download + mosaic MCDWD tiles for ``bbox`` -> categorical uint8 COG bytes.

    Raises ``FloodExtentNoCoverageError`` when no tile downloads or the mosaic
    is entirely nodata; ``FloodExtentUpstreamError`` on read/write failure.
    """
    import numpy as np
    import rasterio

    width_px = max(1, round((bbox[2] - bbox[0]) / _CELL_DEG))
    height_px = max(1, round((bbox[3] - bbox[1]) / _CELL_DEG))
    dst_transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )

    mosaic = np.full((height_px, width_px), NODATA, dtype="uint8")
    filled = np.zeros((height_px, width_px), dtype=bool)
    tiles_used = 0
    for h, v in _tiles_for_bbox(bbox):
        tile_bytes = _download_tile(year, doy, h, v)
        if not tile_bytes:
            continue
        arr = _read_tile_window(tile_bytes, bbox, width_px, height_px)
        valid = (arr != NODATA) & (~filled)
        if valid.any():
            mosaic[valid] = arr[valid]
            filled |= valid
            tiles_used += 1

    if not bool(filled.any()):
        raise FloodExtentNoCoverageError(
            f"no MCDWD flood coverage over bbox={tuple(round(x, 3) for x in bbox)} "
            f"for {year}-{doy:03d} (no tile downloaded, or every pixel is "
            "insufficient-data/cloud). Try a nearby date or a different AOI."
        )

    colormap = {i: _COLORS.get(i, (0, 0, 0, 0)) for i in range(256)}
    tmp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_flood_extent_"
        ) as f:
            tmp = f.name
        profile = dict(
            driver="COG", dtype="uint8", count=1, height=height_px, width=width_px,
            crs="EPSG:4326", transform=dst_transform, compress="DEFLATE", nodata=NODATA,
        )
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(mosaic, 1)
            dst.write_colormap(1, colormap)
            try:
                from rasterio.enums import ColorInterp

                interp = list(dst.colorinterp)
                interp[0] = ColorInterp.palette
                dst.colorinterp = tuple(interp)
            except Exception:  # noqa: BLE001
                pass
        with open(tmp, "rb") as fh:
            cog = fh.read()
    except Exception as exc:  # noqa: BLE001
        raise FloodExtentUpstreamError(
            f"flood-extent COG write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    logger.info(
        "fetch_flood_extent_observation: %d tile(s) -> %dx%d COG (%d bytes)",
        tiles_used, width_px, height_px, len(cog),
    )
    return cog


# ---------------------------------------------------------------------------
# Result type -- LayerURI subclass carrying the flood-extent envelope.
# ---------------------------------------------------------------------------


class FloodExtentLayerURI(LayerURI):
    """The flood-extent raster ``LayerURI`` plus the observation envelope.

    Extra fields:
    - ``product`` / ``observation_date`` -- source + resolved date.
    - ``class_breakdown`` -- ``{class_label: pixel_count}``.
    - ``flood_pixel_count`` / ``flood_area_km2`` -- classes 2 + 3.
    - ``caveats`` -- detection-limit + resolution + NRT honesty notes.
    - ``notes`` -- provenance detail.
    """

    product: str = _PRODUCT
    observation_date: str | None = None
    class_breakdown: dict[str, int] = {}
    flood_pixel_count: int = 0
    flood_area_km2: float | None = None
    caveats: list[str] = []
    notes: list[str] = []


def _legend() -> LegendKey:
    return LegendKey(
        kind="categorical",
        classes=[
            LegendClass(value=1, color="#92c5de", label="Surface water (reference)"),
            LegendClass(value=2, color="#f4a582", label="Recurring flood"),
            LegendClass(value=3, color="#ca0020", label="Flood water"),
        ],
        label="Observed flood extent (MODIS MCDWD)",
    )


def _summarize_cog(data: bytes, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
    """Class breakdown + flood area from the (hit-or-miss) COG bytes."""
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(data) as mem, mem.open() as src:
        arr = src.read(1)
        h, w = arr.shape
        lat_mid = (bbox[1] + bbox[3]) / 2.0
    vals, counts = np.unique(arr, return_counts=True)
    breakdown: dict[str, int] = {}
    for val, cnt in zip(vals.tolist(), counts.tolist()):
        if val == NODATA:
            continue
        breakdown[MCDWD_CLASSES.get(int(val), f"class_{int(val)}")] = int(cnt)
    flood_px = int(((arr == 2) | (arr == 3)).sum())
    # cell area (km^2): dlon*dlat degrees, dlat~111.32 km/deg, dlon scaled by cos(lat)
    cell_km_lat = _CELL_DEG * 111.32
    cell_km_lon = _CELL_DEG * 111.32 * math.cos(math.radians(lat_mid))
    flood_area = flood_px * cell_km_lat * cell_km_lon
    return {
        "class_breakdown": breakdown,
        "flood_pixel_count": flood_px,
        "flood_area_km2": round(flood_area, 4),
    }


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
    open_world_hint=True,
)
def fetch_flood_extent_observation(
    bbox: tuple[float, float, float, float] | None = None,
    date: str | None = None,
    **_extra_ignored: Any,
) -> FloodExtentLayerURI:
    """Fetch an OBSERVED (satellite-derived) flood extent for a bbox as a categorical COG.

    Returns NASA's MODIS Global Flood Product (MCDWD, 250 m, 3-day) classified
    into no-water / surface-water / recurring-flood / flood -- the benchmark
    OBSERVED extent ``compute_flood_extent_skill`` scores a modeled flood
    surface against (Hit rate / False Alarm / CSI).

    **When to use:**
    - You need an observed flood footprint to validate a model:
      ``compute_flood_extent_skill(model_extent, this)``.
    - "satellite flood extent here", "where was actually flooded", "observed
      inundation for this AOI".

    **When NOT to use:**
    - Raw SAR backscatter (you want to threshold it yourself) ->
      ``fetch_sentinel1_sar``.
    - The long-term where-water-ever-was baseline ->
      ``fetch_jrc_global_surface_water``.
    - Surveyed peak-STAGE point marks -> ``fetch_high_water_marks``.
    - Regulatory flood ZONES -> ``fetch_fema_nfhl_zones``; MODELED depth -> the
      SFINCS / SWMM engines.

    **Parameters:**
    - ``bbox``: REQUIRED ``(west, south, east, north)`` in EPSG:4326.
    - ``date``: OPTIONAL ISO ``YYYY-MM-DD``. Omitted -> the latest available
      3-day composite. NEAR-REAL-TIME only (a rolling recent window); a specific
      historical date may be unavailable (honest ``FloodExtentNoCoverageError``).

    **Returns:** ``FloodExtentLayerURI`` -- a single-band uint8 categorical COG
    (EPSG:4326, nodata=255) with an embedded palette + a categorical
    ``LegendKey`` (1 surface water, 2 recurring flood, 3 flood). Envelope:
    ``product``, ``observation_date``, ``class_breakdown``,
    ``flood_pixel_count``, ``flood_area_km2``, ``caveats`` (satellite
    detection-limit + resolution + NRT-provisional), ``notes``.

    **Honest limits (caveats + report open_issues):** optical/SAR flood
    mapping under-detects flooding under vegetation + in dense urban areas;
    250 m misses narrow/sub-pixel flooding; cloud gaps show as nodata; this is
    the provisional NRT product, not the QA'd archive. Anonymous HISTORICAL
    event extents (reprocessed MCDWD_L3 archive, Copernicus GFM/EMS SAR) need
    credentials -- a documented follow-up, not offered here.

    **Errors (FR-AS-11):** ``FloodExtentInputError`` (missing/bad/too-large
    bbox, bad date); ``FloodExtentNoCoverageError`` (no tile/date, all-nodata
    mosaic); ``FloodExtentUpstreamError`` (LANCE listing/download/read/write).

    Cross-tool dependencies:
        - Downstream: ``compute_flood_extent_skill`` (benchmark extent).
        - Source: NASA LANCE MCDWD_L3_F3_NRT (nrt3.modaps.eosdis.nasa.gov).
    """
    bbox_t = _validate_bbox(bbox)
    year, doy = _resolve_datetime(date)
    obs_date = _dt.date(year, 1, 1) + _dt.timedelta(days=doy - 1)

    params: dict[str, Any] = {
        "bbox": [round(v, 6) for v in bbox_t],
        "product": _PRODUCT,
        "year": year,
        "doy": doy,
    }

    def _fetch_bytes() -> bytes:
        return _fetch_flood_extent_cog_bytes(bbox_t, year, doy)

    result = read_through(
        metadata=_METADATA, params=params, ext="tif", fetch_fn=_fetch_bytes
    )
    assert result.uri is not None, "fetch_flood_extent_observation is cacheable"

    summary = _summarize_cog(result.data, bbox_t)
    notes = [
        f"NASA LANCE {_PRODUCT} 3-day composite for {obs_date.isoformat()} "
        f"(doy {doy}); {summary['flood_pixel_count']} flood pixel(s) "
        f"(~{summary['flood_area_km2']} km^2) in the AOI.",
    ]

    return FloodExtentLayerURI(
        layer_id=f"flood-extent-obs-{_seed(params)}",
        name=f"Observed flood extent (MODIS {obs_date.isoformat()})",
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="MCDWD flood class",
        bbox=bbox_t,
        legend=_legend(),
        product=_PRODUCT,
        observation_date=obs_date.isoformat(),
        class_breakdown=summary["class_breakdown"],
        flood_pixel_count=summary["flood_pixel_count"],
        flood_area_km2=summary["flood_area_km2"],
        caveats=list(_CAVEATS),
        notes=notes,
    )


def _seed(params: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(params, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:8]
