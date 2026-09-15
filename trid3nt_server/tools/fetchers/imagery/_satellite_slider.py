"""Shared SLIDER tile substrate for the rendered-imagery fetcher.

Owns the primitives the fetcher does not re-implement: the JSON time-index reader, the
sector registration that says where a square mosaic sits on the ground, the tile-grid
stitch into one square mosaic, and the reproject and COG write with its all-NaN honesty
guard. Nothing here composites - what is computed from a picture is a derive over the
layer."""

# GEOREFERENCING. A SLIDER mosaic is a SQUARE of pixels in the grid its imagery was
# rendered on, and that grid is never lon/lat: a geostationary sector is linear in the
# satellite's SCAN ANGLES, so a lon/lat box over it stretches everything away from the
# sub-satellite point, and a sub-window sector pads its scan to a square with blank
# rows, so a mapping that spreads the declared latitudes over the whole square lands
# every row in the wrong place. Each registered sector therefore carries the projection
# its pixels are linear in and the extent of the square in that projection, and the AOI
# crop is a reprojection out of it. A sector this module cannot register is REFUSED, not
# approximated. An all-transparent or empty AOI crop NEVER reads as success.

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
    "build_tile_url",
    "build_times_url",
    "fetch_slider_timestamps",
    "ts_int_to_iso",
    "ts_int_to_datetime",
    "sector_registration",
    "registered_sectors",
    "aoi_projected_bounds",
    "pick_zoom_for_aoi",
    "usable_zoom",
    "stitch_slider_mosaic",
    "mosaic_to_cog_bytes",
    "rgb_array_to_cog_bytes",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.imagery._satellite_slider")


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

#: The geodetic frame the scan angles are resolved on. The index states the satellite's
#: distance from the Earth's centre; the perspective height a geostationary projection
#: takes is that distance less the equatorial radius.
_EARTH_A_M = 6378137.0
_EARTH_B_M = 6356752.314140356

#: The scan-angle axis the imagery sweeps along. The viewer renders every satellite it
#: carries on the same convention, which a coastline measurement confirms: the other
#: convention lands the coast kilometres off the coast it is drawn on.
_SWEEP_AXIS = "x"

#: Sub-window sectors the index publishes no ``lat_lon_query`` for, as the square
#: mosaic's LEFT scan angle, TOP scan angle and angular width, in radians on the parent
#: satellite's fixed grid. Measured by carrying the sector's own coastline overlay onto
#: the same satellite's registered full disk, which draws the same coastline: the
#: measurement is what the server itself renders, not a guess. The square is wider than
#: the scan - the sector's imagery is padded to a square with blank rows - and the
#: padding falls out of the registration rather than being stated.
_MEASURED_GEOS_SQUARE: dict[tuple[str, str], tuple[float, float, float]] = {
    ("goes-18", "conus"): (-0.069960, 0.156250, 0.139920),
    ("goes-19", "conus"): (-0.101302, 0.156250, 0.139920),
}

#: Sectors drawn on a grid of their own - a polar pass set remapped onto a conic the
#: server publishes nothing about - as that projection and the square mosaic's extent in
#: it, in metres. Measured by carrying fetched shorelines onto the shoreline the sector's
#: own overlay draws, over landmarks spread to the sector's corners and its middle, and
#: settled on the WORST of them rather than their average. The parameters are what that
#: measurement arrived at, not a standard grid recognised by name, and no landmark it was
#: measured on sits more than about one mosaic pixel - three and a half kilometres - from
#: where the server draws it, which is the accuracy an AOI crop from this sector carries.
_MEASURED_GRID: dict[tuple[str, str], tuple[str, tuple[float, float, float, float]]] = {
    ("jpss", "conus"): (
        "+proj=lcc +lat_1=36.60346 +lat_2=38.52876 +lat_0=36.60346 +lon_0=-99.70693 "
        "+R=6371229 +units=m +no_defs",
        (-3094184.9, -2843213.4, 3309536.9, 3560508.4)),
}


def _geostationary_crs(lat_lon_query: dict[str, Any]) -> str:
    """The satellite's own projection, from the parameters the index publishes for it."""
    height = float(lat_lon_query["sat_alt"]) * 1000.0 - _EARTH_A_M
    return (
        f"+proj=geos +lon_0={float(lat_lon_query['lon0'])} +h={height} "
        f"+a={_EARTH_A_M} +b={_EARTH_B_M} +sweep={_SWEEP_AXIS} +units=m +no_defs"
    )


def _full_disk_square(lat_lon_query: dict[str, Any], tile_size: int) -> tuple[float, ...]:
    """The full-disk mosaic's extent in projected metres. The index states the disk's
    RADIUS in zoom-0 pixels and the scan angle that radius subtends, so the square's own
    half-width is that angle scaled by however far the square runs past the disk."""
    height = float(lat_lon_query["sat_alt"]) * 1000.0 - _EARTH_A_M
    half_x = (tile_size / 2.0) / float(
        lat_lon_query["disk_radius_x_z0"]) * float(lat_lon_query["max_rad_x"]) * height
    half_y = (tile_size / 2.0) / float(
        lat_lon_query["disk_radius_y_z0"]) * float(lat_lon_query["max_rad_y"]) * height
    return (-half_x, -half_y, half_x, half_y)


def sector_registration(
    satellite: str, sector: str, sat_entry: dict[str, Any]
) -> dict[str, Any] | None:
    """Where a sector's square mosaic sits on the ground: the projection its pixels are
    linear in and the square's extent in that projection. ``None`` for a sector nothing
    registers - a steerable mesoscale box, a remap whose grid the server does not
    publish - which the caller must refuse rather than approximate."""
    sectors = sat_entry.get("sectors") or {}
    entry = sectors.get(sector) or {}
    tile_size = int(entry.get("tile_size") or 0)
    own_query = entry.get("lat_lon_query")
    if own_query and tile_size:
        return {"crs": _geostationary_crs(own_query),
                "extent": list(_full_disk_square(own_query, tile_size))}
    square = _MEASURED_GEOS_SQUARE.get((satellite, sector))
    if square is not None:
        parent = next(
            (s.get("lat_lon_query") for s in sectors.values() if s.get("lat_lon_query")),
            None)
        if parent is None:
            return None
        height = float(parent["sat_alt"]) * 1000.0 - _EARTH_A_M
        west, north, span = (v * height for v in square)
        return {"crs": _geostationary_crs(parent),
                "extent": [west, north - span, west + span, north]}
    grid = _MEASURED_GRID.get((satellite, sector))
    if grid is not None:
        return {"crs": grid[0], "extent": list(grid[1])}
    return None


def registered_sectors(index: dict[str, Any]) -> list[str]:
    """``satellite/sector`` for every sector this substrate can place on the ground."""
    return sorted(
        f"{satellite}/{sector}"
        for satellite, sat_entry in index.items()
        for sector in (sat_entry.get("sectors") or {})
        if sector_registration(satellite, sector, sat_entry) is not None
    )


#: How finely an AOI's outline is walked before it is projected. A lon/lat box is a
#: curve in a satellite's scan angles, so its corners alone would miss the bulge.
_AOI_RING_STEPS = 48


def aoi_projected_bounds(
    registration: dict[str, Any], bbox: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    """The AOI's outline carried into the sector's projection, as a bounding box in that
    projection. An AOI the projection cannot resolve - beyond the visible limb - has no
    bounds and raises the typed empty error."""
    import numpy as np
    from pyproj import CRS, Transformer

    steps = np.linspace(0.0, 1.0, _AOI_RING_STEPS)
    lons = np.concatenate([
        bbox[0] + steps * (bbox[2] - bbox[0]), np.full(steps.shape, bbox[2]),
        bbox[2] - steps * (bbox[2] - bbox[0]), np.full(steps.shape, bbox[0])])
    lats = np.concatenate([
        np.full(steps.shape, bbox[1]), bbox[1] + steps * (bbox[3] - bbox[1]),
        np.full(steps.shape, bbox[3]), bbox[3] - steps * (bbox[3] - bbox[1])])
    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_user_input(registration["crs"]), always_xy=True)
    x, y = transformer.transform(lons, lats)
    finite = np.isfinite(x) & np.isfinite(y)
    if not finite.any():
        raise SliderEmptyError(
            f"AOI bbox={tuple(bbox)} does not resolve in this sector's projection; it "
            "is beyond the visible limb from this satellite")
    return (float(x[finite].min()), float(y[finite].min()),
            float(x[finite].max()), float(y[finite].max()))


_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

_TILE_TIMEOUT_S = 30.0
_JSON_TIMEOUT_S = 30.0




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




def fetch_slider_timestamps(
    sat: str,
    sector: str,
    product: str,
    *,
    session: requests.Session | None = None,
) -> list[int]:
    """Return the ``timestamps_int`` list for a product, SORTED ASCENDING -- the index
    publishes it reverse-chronological -- so a caller can window and order frames
    naturally. A network or parse failure raises the typed upstream error."""
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




def pick_zoom_for_aoi(
    registration: dict[str, Any],
    sector_entry: dict[str, Any],
    bbox: tuple[float, float, float, float],
    *,
    target_px: int = 768,
    max_tiles: int = 16,
) -> int:
    """Pick the SMALLEST zoom whose AOI-spanning tile count stays at or below
    ``max_tiles`` while giving at least ``target_px`` across the AOI, clamped to the
    sector's own zoom range. The tile ceiling is what bounds per-frame download cost."""
    max_zoom = int(sector_entry.get("max_zoom_level") or 4)
    tile_size = int(sector_entry.get("tile_size") or 625)
    west, south, east, north = registration["extent"]
    aoi = aoi_projected_bounds(registration, bbox)
    frac = max((aoi[2] - aoi[0]) / max(1e-9, east - west),
               (aoi[3] - aoi[1]) / max(1e-9, north - south))
    frac = min(1.0, max(frac, 1e-9))

    best = 0
    for zoom in range(0, max_zoom + 1):
        aoi_px = frac * tile_size * (2 ** zoom)
        # Tiles spanning the AOI on the longer axis (+1 for boundary overlap).
        tiles_span = math.ceil(frac * (2 ** zoom)) + 1
        n_tiles = tiles_span * tiles_span
        best = zoom
        if aoi_px >= target_px and n_tiles >= max_tiles:
            break
        if n_tiles > max_tiles:
            best = max(0, zoom - 1)
            break
    return min(max(best, 0), max_zoom)


def usable_zoom(
    sat: str,
    sector: str,
    product: str,
    ts_int: int,
    zoom: int,
    *,
    session: requests.Session | None = None,
) -> int:
    """The deepest zoom at or below ``zoom`` this product is actually tiled at. The index
    states the zoom the VIEWER offers a sector, which is not the zoom every product is
    rendered to, and asking for one that is not there returns nothing at all. The probe
    is the centre tile, which is the sub-satellite point on a disk and mid-sector
    otherwise, so a transparent corner never reads as an absent zoom."""
    sess = session or requests
    for level in range(zoom, -1, -1):
        middle = (2 ** level) // 2
        url = build_tile_url(sat, sector, product, ts_int, level, middle, middle)
        try:
            resp = sess.get(
                url, headers={"User-Agent": _USER_AGENT}, timeout=_TILE_TIMEOUT_S,
                allow_redirects=True)
        except requests.RequestException as exc:
            raise SliderUpstreamError(
                f"SLIDER tile probe failed ({sat}/{sector}/{product} z{level}): {exc}"
            ) from exc
        if resp.status_code == 200:
            return level
    raise SliderEmptyError(
        f"SLIDER has no {sat}/{sector}/{product} tiles at any zoom for ts={ts_int}")


def _aoi_to_pixel_window(
    registration: dict[str, Any],
    bbox: tuple[float, float, float, float],
    side_px: int,
) -> tuple[int, int, int, int]:
    """Map an AOI bbox to an inclusive-min, exclusive-max pixel window on the square
    mosaic. Row 0 is the square's top edge and column 0 its left edge, both in the
    sector's own projection, and the window is clamped to the square."""
    west, south, east, north = registration["extent"]
    x_min, y_min, x_max, y_max = aoi_projected_bounds(registration, bbox)
    span_x = max(1e-9, east - west)
    span_y = max(1e-9, north - south)
    px_min_x = int(max(0, math.floor((x_min - west) / span_x * side_px)))
    px_max_x = int(min(side_px, math.ceil((x_max - west) / span_x * side_px)))
    px_min_y = int(max(0, math.floor((north - y_max) / span_y * side_px)))
    px_max_y = int(min(side_px, math.ceil((north - y_min) / span_y * side_px)))
    return px_min_x, px_min_y, px_max_x, px_max_y


def stitch_slider_mosaic(
    sat: str,
    sector: str,
    product: str,
    ts_int: int,
    zoom: int,
    bbox: tuple[float, float, float, float],
    registration: dict[str, Any],
    tile_size: int,
    *,
    session: requests.Session | None = None,
) -> tuple[Any, tuple[float, float, float, float]]:
    """Download and stitch the tiles covering an AOI for one timestamp, returning the
    ``(H, W, 3)`` block and its extent in the sector's own projection. ONLY intersecting
    tiles are fetched, and a 404 tile is treated as transparent; every tile failing
    raises upstream."""
    import numpy as np
    from PIL import Image

    tsize = int(tile_size)
    n_tiles = 2 ** zoom
    side_px = tsize * n_tiles
    sess = session or requests

    px_min_x, px_min_y, px_max_x, px_max_y = _aoi_to_pixel_window(
        registration, bbox, side_px
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

    west, south, east, north = registration["extent"]
    span_x = east - west
    span_y = north - south
    blk_west = west + tx_min * tsize / side_px * span_x
    blk_east = west + (tx_max + 1) * tsize / side_px * span_x
    blk_north = north - ty_min * tsize / side_px * span_y
    blk_south = north - (ty_max + 1) * tsize / side_px * span_y
    return canvas, (blk_west, blk_south, blk_east, blk_north)


def mosaic_to_cog_bytes(
    rgb_array: Any,
    mosaic_extent: tuple[float, float, float, float],
    src_crs: str,
    aoi_bbox: tuple[float, float, float, float],
    *,
    out_res_deg: float = 0.01,
) -> bytes:
    """Reproject and clip a stitched RGB mosaic OUT of the sector's own projection to a
    3-band EPSG:4326 COG over the AOI. A crop with NO non-zero pixel -- the AOI fell on
    a transparent or off-grid region -- raises the typed empty error rather than writing
    a blank layer."""

    # The source transform is built from the stitched block's own extent in the sector's
    # projection over its (H, W); each band then reprojects onto a regular lon/lat grid
    # clipped to the AOI at ``out_res_deg``; the result is written as a 3-band uint8 COG,
    # which the publish seam renders directly with no colormap.
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
                src_crs=src_crs,
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


def rgb_array_to_cog_bytes(
    out_rgb: Any,
    out_transform: Any,
    out_w: int,
    out_h: int,
) -> bytes:
    """Write a ``(3, H, W)`` uint8 EPSG:4326 RGB array to COG bytes, falling back from
    the COG driver to GTiff. The publish seam renders a 3-band RGB directly, with no
    colormap."""
    import numpy as np
    import rasterio

    out_rgb = np.asarray(out_rgb, dtype=np.uint8)
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_slider_cog_")
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
