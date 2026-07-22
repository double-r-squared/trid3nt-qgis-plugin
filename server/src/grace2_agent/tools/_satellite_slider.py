"""Shared CIRA/RAMMB SLIDER tile substrate for the satellite fire-animation fetchers.

Both ``fetch_goes_animation`` (GOES geostationary) and ``fetch_viirs_day_fire``
(JPSS polar) pull READY-MADE pre-rendered RGB imagery from the CIRA/RAMMB SLIDER
tile service and reproject it to an EPSG:4326 COG over an AOI. This module owns
the shared primitives so neither fetcher re-implements them:

1. The SLIDER JSON time-index reader (``fetch_slider_timestamps``) -- reads
   ``latest_times.json`` -> the ``timestamps_int`` array (14-digit YYYYMMDDHHMMSS
   ints, reverse-chronological) for a (sat, sector, product).
2. The tile-grid stitch (``stitch_slider_mosaic``) -- downloads the
   ``2^zoom x 2^zoom`` PNG tile grid for one timestamp and pastes it into one
   square mosaic in the satellite fixed grid pixel space.
3. The approximate fixed-grid -> EPSG:4326 reproject + COG write
   (``mosaic_to_cog_bytes``) -- lifts the rasterio warp + COG-write + all-NaN
   honesty guard CORE from ``fetch_goes_satellite._reproject_and_clip`` and
   applies it to a stitched RGB mosaic using a documented per-sector lat/lon
   extent (see GEOREFERENCING below).

GEOREFERENCING (honest accuracy statement -- read this):
    SLIDER itself carries NO projection metadata -- it is a pure pixel-mosaic
    service (the SLIDER-cli source exposes no proj4 / no scan-angle extents).
    The PRECISE georeference for the GOES sectors is the ABI fixed-grid
    geostationary projection (recoverable from the GOES-R PUG); the JPSS polar
    sectors are a CIRA remap whose exact projection is not published. To keep
    BOTH demos honest AND working without an unrecoverable extent table, this
    module uses an APPROXIMATE linear pixel -> lon/lat mapping over a documented
    per-sector lat/lon bounding box (``_SECTOR_LATLON_EXTENT``). The error is
    small for CONUS-interior / well-inside-sector AOIs (the fire demos) and
    large near the limb. The emitted layer is explicitly labelled "approximate
    georeferencing" so the honesty floor holds: the imagery is the real CIRA
    product at the real cadence, but the pixel-to-ground registration is a
    sector-extent approximation pending the exact fixed-grid extents
    (LIVE-VERIFY against a matching ABI fixed-grid NetCDF for sub-pixel
    accuracy). An all-transparent / empty AOI crop NEVER reads as success.

ASCII only.
"""

from __future__ import annotations

import io
import logging
import math
import os
import tempfile
from typing import Any

import requests

__all__ = [
    "SliderError",
    "SliderUpstreamError",
    "SliderEmptyError",
    "SLIDER_BASE",
    "TILE_SIZE",
    "SECTOR_MAX_ZOOM",
    "build_tile_url",
    "build_times_url",
    "fetch_slider_timestamps",
    "ts_int_to_iso",
    "ts_int_to_datetime",
    "pick_zoom_for_aoi",
    "stitch_slider_mosaic",
    "mosaic_to_cog_bytes",
    "rgb_cog_bytes_to_array",
    "rgb_array_to_cog_bytes",
    "reproject_rgb_to_grid",
    "blend_geocolor_fire_temperature",
    "FIRE_BLEND_RED_FLOOR",
    "FIRE_BLEND_RED_OVER_BLUE",
    "FIRE_BLEND_MAX_ALPHA",
    "_SECTOR_LATLON_EXTENT",
]

logger = logging.getLogger("grace2_agent.tools._satellite_slider")


class SliderError(RuntimeError):
    """Base class for SLIDER substrate failures."""

    error_code: str = "SLIDER_ERROR"
    retryable: bool = True


class SliderUpstreamError(SliderError):
    """SLIDER tile / JSON request failed (network, HTTP, parse)."""

    error_code = "SLIDER_UPSTREAM_ERROR"
    retryable = True


class SliderEmptyError(SliderError):
    """A stitch / crop produced no usable (non-transparent) pixels over the AOI."""

    error_code = "SLIDER_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants (confirmed from the SLIDER-cli source + live probes 2026-06-22).
# ---------------------------------------------------------------------------

#: SLIDER host. The rammb host 302-redirects to slider.cira.colostate.edu; we
#: follow redirects so either resolves.
SLIDER_BASE = "https://rammb-slider.cira.colostate.edu"

#: Tile-image URL template. The date directory is YYYY/MM/DD (slashes), the
#: zoom is 2-digit zero-padded, the tile index is tileY_tileX (row_col) each
#: 3-digit zero-padded. (CONFIRMED from SLIDER-cli request.go TileImageURI.)
_TILE_TEMPLATE = (
    SLIDER_BASE
    + "/data/imagery/{yyyy}/{mm}/{dd}/{sat}---{sector}/{product}/"
    + "{ts}/{zoom:02d}/{tiley:03d}_{tilex:03d}.png"
)

#: JSON time-index URL template. Note the json path uses /json/<sat>/<sector>/
#: <product>/ with NO '---' join (unlike the imagery path).
_TIMES_TEMPLATE = (
    SLIDER_BASE + "/data/json/{sat}/{sector}/{product}/latest_times.json"
)

#: Per-sector tile pixel size (px). CONFIRMED from define-products.js + live PNG
#: dims. Keyed by (sat, sector).
TILE_SIZE: dict[tuple[str, str], int] = {
    ("goes-18", "conus"): 625,
    ("goes-18", "full_disk"): 678,
    ("goes-19", "conus"): 625,
    ("goes-19", "full_disk"): 678,
    ("jpss", "conus"): 500,
    ("jpss", "northern_hemisphere"): 1000,
    ("jpss", "southern_hemisphere"): 1000,
}

#: Per-sector max zoom level (CONFIRMED from define-products.js).
SECTOR_MAX_ZOOM: dict[tuple[str, str], int] = {
    ("goes-18", "conus"): 4,
    ("goes-18", "full_disk"): 5,
    ("goes-19", "conus"): 4,
    ("goes-19", "full_disk"): 5,
    ("jpss", "conus"): 5,
    ("jpss", "northern_hemisphere"): 5,
    ("jpss", "southern_hemisphere"): 5,
}

#: APPROXIMATE per-sector lat/lon extent (west, south, east, north) used for the
#: linear pixel -> lon/lat georeference (see module GEOREFERENCING note). These
#: are sector coverage envelopes, NOT exact fixed-grid corners -- they bound the
#: square SLIDER mosaic. Accurate for AOIs well inside the sector; LIVE-VERIFY
#: for limb-edge AOIs. The GOES CONUS sector for GOES-West is actually the PACUS
#: window; the envelope below covers the western CONUS + eastern Pacific the
#: GOES-18 "conus" product spans.
_SECTOR_LATLON_EXTENT: dict[tuple[str, str], tuple[float, float, float, float]] = {
    # GOES-18 (West) CONUS / PACUS sector approx envelope.
    ("goes-18", "conus"): (-152.1, 14.6, -52.4, 56.8),
    ("goes-19", "conus"): (-152.1, 14.6, -52.4, 56.8),
    # Full disk: the visible Earth disk from the sub-satellite point. We bound a
    # generous square; only AOIs near disk center reproject acceptably.
    ("goes-18", "full_disk"): (-180.0, -81.3, -8.0, 81.3),
    ("goes-19", "full_disk"): (-141.0, -81.3, -9.0, 81.3),
    # JPSS CONUS remap envelope (approx; LIVE-VERIFY).
    ("jpss", "conus"): (-152.1, 14.6, -52.4, 56.8),
    ("jpss", "northern_hemisphere"): (-180.0, 0.0, 180.0, 90.0),
    ("jpss", "southern_hemisphere"): (-180.0, -90.0, 180.0, 0.0),
}

_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_TILE_TIMEOUT_S = 30.0
_JSON_TIMEOUT_S = 30.0


# ---------------------------------------------------------------------------
# Timestamp helpers.
# ---------------------------------------------------------------------------


def ts_int_to_datetime(ts_int: int) -> Any:
    """Convert a 14-digit YYYYMMDDHHMMSS SLIDER timestamp int -> aware UTC datetime."""
    from datetime import datetime, timezone

    s = f"{int(ts_int):014d}"
    return datetime(
        int(s[0:4]),
        int(s[4:6]),
        int(s[6:8]),
        int(s[8:10]),
        int(s[10:12]),
        int(s[12:14]),
        tzinfo=timezone.utc,
    )


def ts_int_to_iso(ts_int: int) -> str:
    """Convert a 14-digit SLIDER timestamp int -> ISO-8601 UTC string."""
    return ts_int_to_datetime(ts_int).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def build_times_url(sat: str, sector: str, product: str) -> str:
    """Build the SLIDER latest_times.json URL for a (sat, sector, product)."""
    return _TIMES_TEMPLATE.format(sat=sat, sector=sector, product=product)


def build_tile_url(
    sat: str,
    sector: str,
    product: str,
    ts_int: int,
    zoom: int,
    tiley: int,
    tilex: int,
) -> str:
    """Build a single SLIDER tile URL (date dir derived from the timestamp)."""
    s = f"{int(ts_int):014d}"
    return _TILE_TEMPLATE.format(
        yyyy=s[0:4],
        mm=s[4:6],
        dd=s[6:8],
        sat=sat,
        sector=sector,
        product=product,
        ts=s,
        zoom=zoom,
        tiley=tiley,
        tilex=tilex,
    )


# ---------------------------------------------------------------------------
# Time-index fetch.
# ---------------------------------------------------------------------------


def fetch_slider_timestamps(
    sat: str,
    sector: str,
    product: str,
    *,
    session: requests.Session | None = None,
) -> list[int]:
    """Return the SLIDER ``timestamps_int`` list (ascending) for a product.

    Reads ``latest_times.json`` (key ``timestamps_int``, reverse-chronological)
    and returns it SORTED ASCENDING so callers can window + order frames
    naturally.

    Raises ``SliderUpstreamError`` on network / parse failure.
    """
    url = build_times_url(sat, sector, product)
    sess = session or requests
    try:
        resp = sess.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_JSON_TIMEOUT_S,
            allow_redirects=True,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise SliderUpstreamError(
            f"SLIDER time index failed (sat={sat}, sector={sector}, "
            f"product={product}): {exc}"
        ) from exc
    except ValueError as exc:
        raise SliderUpstreamError(
            f"SLIDER time index returned non-JSON (url={url}): {exc}"
        ) from exc

    if not isinstance(body, dict) or "timestamps_int" not in body:
        raise SliderUpstreamError(
            f"SLIDER time index missing 'timestamps_int' key (url={url}); "
            f"got keys={list(body) if isinstance(body, dict) else type(body).__name__}"
        )
    raw = body.get("timestamps_int") or []
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    out.sort()
    return out


# ---------------------------------------------------------------------------
# Zoom selection.
# ---------------------------------------------------------------------------


def pick_zoom_for_aoi(
    sat: str,
    sector: str,
    bbox: tuple[float, float, float, float],
    *,
    target_px: int = 768,
    max_tiles: int = 16,
) -> int:
    """Pick a SLIDER zoom level that resolves the AOI at roughly ``target_px``.

    Higher zoom = finer detail but more tiles to stitch. We choose the smallest
    zoom whose AOI-spanning tile count stays at or below ``max_tiles`` (a 4x4
    stitch ceiling that bounds per-frame download cost) while giving at least
    ``target_px`` across the AOI. Always within [0, sector max zoom].

    Pure function (no network).
    """
    max_zoom = SECTOR_MAX_ZOOM.get((sat, sector), 4)
    ext = _SECTOR_LATLON_EXTENT.get((sat, sector))
    tsize = TILE_SIZE.get((sat, sector), 625)
    if ext is None:
        return min(2, max_zoom)
    west, south, east, north = ext
    aoi_w = max(1e-6, bbox[2] - bbox[0])
    aoi_h = max(1e-6, bbox[3] - bbox[1])
    sec_w = max(1e-6, east - west)
    sec_h = max(1e-6, north - south)
    frac = max(aoi_w / sec_w, aoi_h / sec_h)  # AOI fraction of the sector

    best = 0
    for z in range(0, max_zoom + 1):
        side_px = tsize * (2 ** z)
        aoi_px = frac * side_px
        # Tiles spanning the AOI on the longer axis (+1 for boundary overlap).
        tiles_span = math.ceil(frac * (2 ** z)) + 1
        n_tiles = tiles_span * tiles_span
        best = z
        if aoi_px >= target_px and n_tiles >= max_tiles:
            break
        if n_tiles > max_tiles:
            best = max(0, z - 1)
            break
    return min(max(best, 0), max_zoom)


# ---------------------------------------------------------------------------
# Tile-grid stitch.
# ---------------------------------------------------------------------------


def _aoi_to_pixel_window(
    sat: str,
    sector: str,
    bbox: tuple[float, float, float, float],
    side_px: int,
) -> tuple[int, int, int, int]:
    """Map an AOI bbox to a pixel window (px_min_x, px_min_y, px_max_x, px_max_y).

    Uses the approximate linear sector lat/lon extent. Row 0 = north (top),
    col 0 = west (left). Clamped to [0, side_px]. Returns an INCLUSIVE-min /
    EXCLUSIVE-max pixel box (a small margin is added by the caller via tile
    rounding).
    """
    west, south, east, north = _SECTOR_LATLON_EXTENT[(sat, sector)]
    sec_w = east - west
    sec_h = north - south

    def _x(lon: float) -> float:
        return (lon - west) / sec_w * side_px

    def _y(lat: float) -> float:
        # north -> row 0; south -> row side_px.
        return (north - lat) / sec_h * side_px

    x0 = _x(bbox[0])
    x1 = _x(bbox[2])
    # north has the smaller row index.
    y_top = _y(bbox[3])
    y_bot = _y(bbox[1])
    px_min_x = int(max(0, math.floor(min(x0, x1))))
    px_max_x = int(min(side_px, math.ceil(max(x0, x1))))
    px_min_y = int(max(0, math.floor(min(y_top, y_bot))))
    px_max_y = int(min(side_px, math.ceil(max(y_top, y_bot))))
    return px_min_x, px_min_y, px_max_x, px_max_y


def stitch_slider_mosaic(
    sat: str,
    sector: str,
    product: str,
    ts_int: int,
    zoom: int,
    bbox: tuple[float, float, float, float],
    *,
    session: requests.Session | None = None,
) -> tuple[Any, tuple[float, float, float, float]]:
    """Download + stitch the SLIDER tiles covering an AOI for one timestamp.

    Returns ``(rgb_array, mosaic_latlon_extent)`` where ``rgb_array`` is an
    ``(H, W, 3)`` uint8 numpy array of the STITCHED AOI-covering tile block (a
    sub-rectangle of the full sector square, NOT the whole square -- only the
    tiles that intersect the AOI are fetched), and ``mosaic_latlon_extent`` is
    the ``(west, south, east, north)`` lat/lon box of that stitched block under
    the approximate sector mapping.

    Only the tiles intersecting the AOI are downloaded (bounded by
    ``pick_zoom_for_aoi``'s tile ceiling), so per-frame cost stays small. A tile
    that 404s (sparse polar coverage) is treated as transparent.

    Raises ``SliderUpstreamError`` (all tiles failed) / ``SliderEmptyError``
    (AOI maps outside the tile grid).
    """
    import numpy as np
    from PIL import Image

    tsize = TILE_SIZE.get((sat, sector), 625)
    n_tiles = 2 ** zoom
    side_px = tsize * n_tiles
    sess = session or requests

    px_min_x, px_min_y, px_max_x, px_max_y = _aoi_to_pixel_window(
        sat, sector, bbox, side_px
    )
    if px_max_x <= px_min_x or px_max_y <= px_min_y:
        raise SliderEmptyError(
            f"AOI bbox={bbox} maps outside the {sat}/{sector} sector grid"
        )

    tx_min = px_min_x // tsize
    tx_max = (px_max_x - 1) // tsize
    ty_min = px_min_y // tsize
    ty_max = (px_max_y - 1) // tsize

    block_w = (tx_max - tx_min + 1) * tsize
    block_h = (ty_max - ty_min + 1) * tsize
    canvas = np.zeros((block_h, block_w, 3), dtype=np.uint8)
    got_any = False
    n_upstream_fail = 0

    for ty in range(ty_min, ty_max + 1):
        for tx in range(tx_min, tx_max + 1):
            url = build_tile_url(sat, sector, product, ts_int, zoom, ty, tx)
            try:
                resp = sess.get(
                    url,
                    headers={"User-Agent": _USER_AGENT},
                    timeout=_TILE_TIMEOUT_S,
                    allow_redirects=True,
                )
            except requests.RequestException:
                n_upstream_fail += 1
                continue
            if resp.status_code == 404:
                # Sparse coverage (esp. polar) -> transparent tile, skip.
                continue
            if resp.status_code != 200:
                n_upstream_fail += 1
                continue
            try:
                img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                arr = np.asarray(img, dtype=np.uint8)
            except Exception:  # noqa: BLE001 -- a corrupt tile is skipped
                n_upstream_fail += 1
                continue
            if arr.shape[0] != tsize or arr.shape[1] != tsize:
                # Resize defensively if a sector ships an off-size tile.
                img = img.resize((tsize, tsize))
                arr = np.asarray(img, dtype=np.uint8)
            oy = (ty - ty_min) * tsize
            ox = (tx - tx_min) * tsize
            canvas[oy : oy + tsize, ox : ox + tsize, :] = arr
            got_any = True

    if not got_any:
        if n_upstream_fail > 0:
            raise SliderUpstreamError(
                f"all {n_upstream_fail} SLIDER tiles failed for ts={ts_int} "
                f"({sat}/{sector}/{product} z{zoom})"
            )
        raise SliderEmptyError(
            f"no SLIDER tiles present for ts={ts_int} "
            f"({sat}/{sector}/{product} z{zoom}); likely no coverage this pass"
        )

    # lat/lon extent of the stitched block (the tile-aligned outer box).
    west, south, east, north = _SECTOR_LATLON_EXTENT[(sat, sector)]
    sec_w = east - west
    sec_h = north - south
    block_px_x0 = tx_min * tsize
    block_px_x1 = (tx_max + 1) * tsize
    block_px_y0 = ty_min * tsize
    block_px_y1 = (ty_max + 1) * tsize
    blk_west = west + block_px_x0 / side_px * sec_w
    blk_east = west + block_px_x1 / side_px * sec_w
    blk_north = north - block_px_y0 / side_px * sec_h
    blk_south = north - block_px_y1 / side_px * sec_h
    return canvas, (blk_west, blk_south, blk_east, blk_north)


# ---------------------------------------------------------------------------
# Mosaic -> EPSG:4326 COG (lifts the rasterio warp/COG CORE).
# ---------------------------------------------------------------------------


def mosaic_to_cog_bytes(
    rgb_array: Any,
    mosaic_extent: tuple[float, float, float, float],
    aoi_bbox: tuple[float, float, float, float],
    *,
    out_res_deg: float = 0.01,
) -> bytes:
    """Reproject + clip a stitched RGB mosaic to an EPSG:4326 3-band COG over the AOI.

    Lifts the rasterio warp + COG-write + all-NaN/empty honesty guard CORE from
    ``fetch_goes_satellite._reproject_and_clip``, adapted to a 3-band uint8 RGB
    mosaic that is already (approximately) in EPSG:4326 lon/lat under the linear
    sector mapping. We:

    1. Build the source transform from ``mosaic_extent`` (the stitched block's
       lat/lon box) over the array's (H, W).
    2. ``reproject`` each band onto a regular EPSG:4326 grid clipped to
       ``aoi_bbox`` at ``out_res_deg``.
    3. Write a 3-band uint8 COG (publish_layer's multiband passthrough renders it
       directly, no colormap).

    A crop with NO non-zero pixels raises ``SliderEmptyError`` (the AOI fell on a
    transparent / off-grid region) so the honesty floor holds.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject

    rgb = np.asarray(rgb_array, dtype=np.uint8)
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise SliderEmptyError(
            f"stitched mosaic has unexpected shape {rgb.shape}; expected (H,W,3)"
        )
    src_h, src_w = rgb.shape[0], rgb.shape[1]
    m_west, m_south, m_east, m_north = mosaic_extent
    src_transform = from_bounds(m_west, m_south, m_east, m_north, src_w, src_h)

    a_min_lon, a_min_lat, a_max_lon, a_max_lat = aoi_bbox
    out_w = max(1, int(math.ceil((a_max_lon - a_min_lon) / out_res_deg)))
    out_h = max(1, int(math.ceil((a_max_lat - a_min_lat) / out_res_deg)))
    out_transform = from_bounds(
        a_min_lon, a_min_lat, a_max_lon, a_max_lat, out_w, out_h
    )

    out_rgb = np.zeros((3, out_h, out_w), dtype=np.uint8)
    for b in range(3):
        src_band = np.ascontiguousarray(rgb[:, :, b])
        dst_band = np.zeros((out_h, out_w), dtype=np.uint8)
        try:
            reproject(
                source=src_band,
                destination=dst_band,
                src_transform=src_transform,
                src_crs="EPSG:4326",
                dst_transform=out_transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.bilinear,
            )
        except Exception as exc:  # noqa: BLE001
            raise SliderUpstreamError(
                f"rasterio reproject failed for band {b}: {exc}"
            ) from exc
        out_rgb[b] = dst_band

    # Honesty guard: an all-zero AOI crop is empty (the AOI fell off the imagery).
    if not out_rgb.any():
        raise SliderEmptyError(
            f"AOI bbox={aoi_bbox} produced no imagery pixels (transparent / "
            "off-grid crop); refusing to emit an empty frame"
        )

    return rgb_array_to_cog_bytes(out_rgb, out_transform, out_w, out_h)


# ---------------------------------------------------------------------------
# RGB COG read / write helpers (shared by the GeoColor + Fire Temperature blend).
# ---------------------------------------------------------------------------


def rgb_array_to_cog_bytes(
    out_rgb: Any,
    out_transform: Any,
    out_w: int,
    out_h: int,
) -> bytes:
    """Write a ``(3, H, W)`` uint8 EPSG:4326 RGB array to COG bytes.

    The COG-write CORE (COG driver -> GTiff fallback) lifted out of
    ``mosaic_to_cog_bytes`` so the blend path can re-emit a composited RGB array
    through the identical writer. ``publish_layer``'s multiband passthrough
    renders the 3-band RGB directly (no colormap).
    """
    import numpy as np
    import rasterio

    out_rgb = np.asarray(out_rgb, dtype=np.uint8)
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="grace2_slider_cog_")
    os.close(out_fd)
    try:
        profile = {
            "driver": "COG",
            "dtype": "uint8",
            "count": 3,
            "height": out_h,
            "width": out_w,
            "crs": "EPSG:4326",
            "transform": out_transform,
            "compress": "DEFLATE",
            "photometric": "RGB",
        }
        try:
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(out_rgb)
        except Exception as exc:  # noqa: BLE001 -- COG driver may be unavailable
            logger.warning(
                "_satellite_slider: COG write failed (%s); falling back to GTiff",
                exc,
            )
            profile["driver"] = "GTiff"
            profile["tiled"] = True
            profile.pop("photometric", None)
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(out_rgb)
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def rgb_cog_bytes_to_array(cog_bytes: bytes) -> tuple[Any, Any, int, int]:
    """Read 3-band RGB COG bytes -> ``(rgb (3,H,W) uint8, transform, W, H)``.

    Uses a rasterio in-memory dataset (``MemoryFile``) so no temp file is needed.
    Returns the first three bands as a ``(3, H, W)`` uint8 array plus the affine
    transform + width/height (the georeference the blend preserves). Raises
    ``SliderUpstreamError`` on an unreadable / sub-3-band raster.
    """
    import numpy as np
    import rasterio

    try:
        with rasterio.MemoryFile(cog_bytes) as mem:
            with mem.open() as src:
                if src.count < 3:
                    raise SliderUpstreamError(
                        f"RGB COG has {src.count} band(s); expected >= 3"
                    )
                rgb = src.read([1, 2, 3]).astype(np.uint8)
                return rgb, src.transform, int(src.width), int(src.height)
    except SliderError:
        raise
    except Exception as exc:  # noqa: BLE001 -- a corrupt COG is upstream-bad
        raise SliderUpstreamError(f"could not read RGB COG bytes: {exc}") from exc


def reproject_rgb_to_grid(
    rgb: Any,
    src_transform: Any,
    dst_transform: Any,
    dst_w: int,
    dst_h: int,
) -> Any:
    """Reproject a ``(3,H,W)`` EPSG:4326 RGB array onto a target grid.

    Used only as the DEFENSIVE co-registration step in the blend: the GeoColor
    and Fire Temperature COGs are produced by the same ``mosaic_to_cog_bytes``
    over the identical AOI bbox at the same ``out_res_deg``, so they are already
    pixel-aligned (same transform + shape). When -- for any reason -- the two
    frames differ in grid, this warps the Fire Temperature frame onto the
    GeoColor grid so the per-pixel blend stays valid. Both rasters are EPSG:4326,
    so this is a same-CRS regrid (a resample), never a CRS change.
    """
    import numpy as np
    from rasterio.warp import Resampling, reproject

    rgb = np.asarray(rgb, dtype=np.uint8)
    out = np.zeros((3, dst_h, dst_w), dtype=np.uint8)
    for b in range(3):
        dst_band = np.zeros((dst_h, dst_w), dtype=np.uint8)
        reproject(
            source=np.ascontiguousarray(rgb[b]),
            destination=dst_band,
            src_transform=src_transform,
            src_crs="EPSG:4326",
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.bilinear,
        )
        out[b] = dst_band
    return out


# ---------------------------------------------------------------------------
# GeoColor + Fire Temperature per-timestep blend (the CIRA composite look).
# ---------------------------------------------------------------------------
#
# CIRA publishes a combined "GeoColor and Fire Temperature" product: GeoColor is
# the true-color base (shows the scene + smoke + clouds), and the Fire
# Temperature SWIR composite is overlaid ONLY where there is active fire, so the
# fire glows on top of an otherwise-untouched true-color image. We reproduce that
# look with a MASKED ALPHA-OVERLAY (chosen over a plain screen/lighten max blend
# because max-blend brightens the whole scene -- clouds and bright land -- and
# washes out the true color; the masked overlay keeps smoke/clouds/terrain true-
# color and lets ONLY the fire pixels glow, which is the CIRA result).
#
# Fire mask (from the Fire Temperature RGB recipe R=C07 3.9um BT, G=C06 2.2um,
# B=C05 1.6um): active-fire pixels are bright in the RED channel (hot 3.9um) and
# red dominates over blue (the non-fire SWIR scene -- water/cloud/cool land --
# renders darker / blue-grey). So mask = (red is high) AND (red exceeds blue by a
# margin). A soft alpha ramp over the floor gives the hottest cores full overlay
# and the fire edges a partial glow, matching the CIRA blend's feathered look.
# All three thresholds are tunable module constants (LIVE-VERIFY / tune on box).

#: Red-channel floor (0-255): Fire Temperature pixels at or above this red value
#: are candidate active-fire (hot 3.9um BT). Below it, the GeoColor base shows
#: through untouched.
FIRE_BLEND_RED_FLOOR: int = 110

#: Required red-over-blue margin (0-255): a fire pixel's red must exceed its blue
#: by at least this so cool blue-ish SWIR scene (cloud/water) is NOT masked as
#: fire even when it is moderately bright.
FIRE_BLEND_RED_OVER_BLUE: int = 25

#: Maximum overlay alpha (0..1) at the hottest fire core. < 1.0 keeps a touch of
#: the GeoColor base even under the brightest fire so the composite never looks
#: like a flat paste; 0.92 reads as a near-opaque glow.
FIRE_BLEND_MAX_ALPHA: float = 0.92


def blend_geocolor_fire_temperature(
    geocolor_cog_bytes: bytes,
    fire_temp_cog_bytes: bytes,
) -> bytes:
    """Blend a co-temporal GeoColor + Fire Temperature RGB COG pair into ONE COG.

    GeoColor is the BASE; the Fire Temperature fire color is alpha-overlaid ONLY
    where an active-fire mask is hot (high red 3.9um BT AND red-over-blue), so
    smoke / clouds / terrain stay true-color and the active fire glows -- the
    CIRA "GeoColor and Fire Temperature" composite look. The output preserves the
    GeoColor frame's georeference (same transform / CRS / extent / shape).

    Co-registration: both inputs come from ``mosaic_to_cog_bytes`` over the
    identical AOI bbox at the same resolution, so they are already pixel-aligned;
    if the Fire Temperature grid differs (shape / transform), it is reprojected
    onto the GeoColor grid first (``reproject_rgb_to_grid``) so the per-pixel
    blend is always valid.

    Raises ``SliderEmptyError`` when the composite has no non-zero pixels (both
    inputs empty -> the honesty floor holds at the blend layer too).
    """
    import numpy as np

    base_rgb, base_transform, base_w, base_h = rgb_cog_bytes_to_array(
        geocolor_cog_bytes
    )
    fire_rgb, fire_transform, fire_w, fire_h = rgb_cog_bytes_to_array(
        fire_temp_cog_bytes
    )

    # Defensive co-registration: align the Fire Temperature frame to the GeoColor
    # grid if (for any reason) they are not already pixel-identical. The common
    # case (same AOI + resolution) skips the warp.
    same_grid = (
        fire_w == base_w
        and fire_h == base_h
        and np.allclose(
            np.asarray(fire_transform, dtype=float)[:6],
            np.asarray(base_transform, dtype=float)[:6],
            atol=1e-9,
        )
    )
    if not same_grid:
        logger.info(
            "_satellite_slider: blend co-registering Fire Temperature %sx%s -> "
            "GeoColor grid %sx%s",
            fire_w,
            fire_h,
            base_w,
            base_h,
        )
        fire_rgb = reproject_rgb_to_grid(
            fire_rgb, fire_transform, base_transform, base_w, base_h
        )

    base = base_rgb.astype(np.float32)
    fire = fire_rgb.astype(np.float32)

    red = fire[0]
    blue = fire[2]

    # Active-fire mask: hot red AND red dominates blue. Soft alpha ramps from 0 at
    # the red floor up to FIRE_BLEND_MAX_ALPHA as red saturates, so the hottest
    # cores get a near-opaque glow and the fire edges a partial overlay (feathered
    # CIRA look). Pixels failing either gate get alpha 0 (GeoColor untouched).
    floor = float(FIRE_BLEND_RED_FLOOR)
    denom = max(1.0, 255.0 - floor)
    ramp = np.clip((red - floor) / denom, 0.0, 1.0)
    hot = (red >= floor) & ((red - blue) >= float(FIRE_BLEND_RED_OVER_BLUE))
    alpha = np.where(hot, ramp * float(FIRE_BLEND_MAX_ALPHA), 0.0).astype(np.float32)

    # Per-pixel alpha composite: out = base*(1-a) + fire*a, broadcast over 3 bands.
    a3 = alpha[np.newaxis, :, :]
    out = base * (1.0 - a3) + fire * a3
    out_rgb = np.clip(np.rint(out), 0, 255).astype(np.uint8)

    if not out_rgb.any():
        raise SliderEmptyError(
            "blended GeoColor + Fire Temperature frame has no pixels (both "
            "inputs were empty); refusing to emit an empty blended frame"
        )

    return rgb_array_to_cog_bytes(out_rgb, base_transform, base_w, base_h)
