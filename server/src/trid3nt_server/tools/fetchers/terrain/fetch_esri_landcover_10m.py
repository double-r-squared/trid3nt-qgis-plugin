"""``fetch_esri_landcover_10m`` atomic tool  --  Esri / Impact Observatory 10m LULC.

Fetches the Esri / Impact Observatory **10 m annual global Land-Use Land-Cover**
classification for a bbox + year via the Microsoft Planetary Computer (PC) STAC
catalog (collection ``io-lulc-annual-v02``, a 9-class categorical raster) and
returns a single-band categorical COG with the official class color table baked
in, so ``publish_layer``'s categorical-palette passthrough colorizes it directly.

This is the GLOBAL complement to the US-only NLCD ``fetch_landcover``: Impact
Observatory derives the layer from Sentinel-2 at 10 m for every land mass on
Earth, refreshed annually (2017..2023 available), so it answers "what land cover
is here?" anywhere  --  Africa, Asia, South America, not just CONUS.

**Area routing (model-facing docstring surface):**

- ``fetch_esri_landcover_10m`` -- high-detail 10 m global land cover, best for
  small to medium AOIs (up to ~8 deg^2 / multi-county scale). Uses tiled fetching
  internally: the bbox is split into <=0.5 deg^2 sub-bboxes, each fetched
  independently, then mosaicked into one output COG. Above 8 deg^2 use
  ``fetch_landcover`` instead.
- ``fetch_landcover`` (NLCD) -- US-only, coarser (~30 m), but no area ceiling;
  prefer it for state-wide or larger CONUS analysis.

Classes (``file:values`` on the PC ``data`` asset; sparse integer codes):

    0  = No Data        1  = Water          2  = Trees
    4  = Flooded veg.   5  = Crops          7  = Built area
    8  = Bare ground    9  = Snow/Ice       10 = Clouds
    11 = Rangeland

(3 and 6 are intentionally absent  --  the v02 schema collapsed grass/scrub into
Rangeland.)

Data source
===========

PC collection ``io-lulc-annual-v02`` (10m Annual LULC, 9-class, V2):

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    asset:   ``data``  --  a uint8 single-band palette COG (nodata=0), tiled by
             UTM zone (items like ``37M-2023``) in the tile's native UTM CRS,
             with an EMBEDDED 256-entry GDAL color table holding the official
             Esri/IO class colors.

A bbox can straddle UTM-zone boundaries, so we select EVERY item whose footprint
geographically intersects the bbox in the requested year, warp+window-read each
to EPSG:4326 at 10 m (nearest, categorical), and mosaic them (first non-nodata
wins). The embedded color table from the first real tile is carried onto the
output COG so the categorical palette renders downstream.

For bboxes larger than the per-request safe tile size (``_TILE_DEG2``), the bbox
is automatically subdivided into a grid of sub-tiles, each fetched independently,
and the results are mosaicked via ``rasterio.merge`` (nearest/categorical; class
codes are never interpolated). The combined area must stay under ``_MAX_BBOX_DEG2``
(8 deg^2); above that the tool raises ``EsriLandcoverBboxError`` with a
recommendation to use ``fetch_landcover`` (NLCD, coarser but unlimited-area).

Assets are Azure-Blob COGs behind SAS tokens; this tool signs each asset href
via the PC SAS REST endpoint (see ``_pc_stac.sas_sign_href``) and reads through
GDAL ``/vsicurl/``  --  the same path as ``fetch_sentinel2_truecolor`` /
``compute_ndvi``.

Rendering
=========

The output is a single-band uint8 COG with the source palette stamped on band 1
(``write_colormap`` + ``ColorInterp.palette``). ``publish_layer``'s
``_resolve_titiler_style_params`` step 1 detects the embedded palette and returns
empty style params, so TiTiler colorizes from the table with NO rescale/colormap
override (the same path NLCD ``fetch_landcover`` relies on). ``style_preset`` is
the existing categorical family token ``"categorical_landcover"``.

Honesty (data-source fallback norm): if NO item covers the bbox in the requested
year (e.g. an unsupported year, or a footprint with no real coverage so the
mosaic is all-nodata), a typed ``EsriLandcoverNoCoverageError`` is raised  --
never a fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical ``(bbox, year)``
calls reuse the cached COG in the ``static-30d`` / ``esri_landcover_10m`` cache
prefix.

Tier-1 free (no API key). Heavy emit-free sync raster work  --  runs entirely in
plain sync functions so the agent loop can off-load the body via
``asyncio.to_thread`` and never stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.fetchers.imagery import _pc_stac
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_esri_landcover_10m",
    "estimate_payload_mb",
    "EsriLandcoverError",
    "EsriLandcoverBboxError",
    "EsriLandcoverYearError",
    "EsriLandcoverNoCoverageError",
    "EsriLandcoverUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.terrain.fetch_esri_landcover_10m")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class EsriLandcoverError(RuntimeError):
    """Base class for fetch_esri_landcover_10m failures."""

    error_code = "ESRI_LANDCOVER_ERROR"
    retryable = True


class EsriLandcoverBboxError(EsriLandcoverError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "ESRI_LANDCOVER_BBOX_INVALID"
    retryable = False


class EsriLandcoverYearError(EsriLandcoverError):
    """Requested year is outside the available range / not an integer."""

    error_code = "ESRI_LANDCOVER_YEAR_INVALID"
    retryable = False


class EsriLandcoverNoCoverageError(EsriLandcoverError):
    """No io-lulc item covers the bbox in the requested year.

    Honest no-coverage signal (data-source fallback norm)  --  never fabricate.
    Also raised when items exist but every mosaic pixel is nodata.
    """

    error_code = "ESRI_LANDCOVER_NO_COVERAGE"
    retryable = False


class EsriLandcoverUpstreamError(EsriLandcoverError):
    """A PC STAC search / asset read / COG write failed at the network layer."""

    error_code = "ESRI_LANDCOVER_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "io-lulc-annual-v02"
_DATA_ASSET = "data"

#: Impact Observatory native grid (10 m); used to size the bbox-windowed read.
_NATIVE_CELL_M = 10.0

#: Categorical nodata value (matches the source raster:bands nodata).
_NODATA = 0

#: Available annual vintages for io-lulc-annual-v02 (PC, verified 2026-06-27).
_MIN_YEAR = 2017
_MAX_YEAR = 2023

#: Class code -> human-readable label (PC ``file:values`` on the data asset).
#: 3 and 6 are intentionally absent in the v02 schema.
_CLASS_LABELS: dict[int, str] = {
    0: "No Data",
    1: "Water",
    2: "Trees",
    4: "Flooded vegetation",
    5: "Crops",
    7: "Built area",
    8: "Bare ground",
    9: "Snow/Ice",
    10: "Clouds",
    11: "Rangeland",
}

#: Per-sub-tile area cap (deg^2). Each sub-tile sent to the PC STAC source stays
#: at or below this size; the fetch path is the same per-request path that was
#: previously the hard ceiling for the whole bbox.
_TILE_DEG2 = 0.5

#: Hard ceiling for the ENTIRE bbox (deg^2). Above this the tool raises
#: EsriLandcoverBboxError recommending fetch_landcover (NLCD) instead.
#: 8 deg^2 ~ a few counties / small state chunk; ~16 tiles at 0.5 deg^2 each.
_MAX_BBOX_DEG2 = 8.0

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Existing categorical land-cover style family token. publish_layer's
#: _resolve_titiler_style_params detects the embedded band-1 palette and applies
#: NO rescale/colormap, letting TiTiler colorize from the baked table (same path
#: NLCD fetch_landcover uses).
_STYLE_PRESET = "categorical_landcover"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_esri_landcover_10m",
    ttl_class="static-30d",
    source_class="esri_landcover_10m",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted single-band categorical COG size in MB.

    A 1-band uint8 DEFLATE-COG of a categorical land-cover patch at 10 m
    compresses very hard (large same-class runs); empirically ~5 MB / sq-deg.
    Scale linearly with bbox area, floored.
    """
    if bbox is None:
        return 1.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 1.0
    return max(0.2, sq_deg * 5.0)


# ---------------------------------------------------------------------------
# Tile-grid planning.
# ---------------------------------------------------------------------------


def _plan_tile_grid(
    bbox: tuple[float, float, float, float],
    tile_deg2: float = _TILE_DEG2,
) -> list[tuple[float, float, float, float]]:
    """Split ``bbox`` into a grid of sub-bboxes each with area <= ``tile_deg2``.

    The grid is computed by finding the minimum number of columns/rows needed so
    that each cell's area (cell_dlon * cell_dlat) <= ``tile_deg2``.  Cells on the
    eastern/northern border may be narrower than the interior cells; all cells
    together cover the full bbox with no gaps.

    Returns a list of (min_lon, min_lat, max_lon, max_lat) tuples (at least 1).
    For bboxes whose total area is already <= ``tile_deg2`` the list has exactly
    one entry equal to ``bbox``.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    dlon = max_lon - min_lon
    dlat = max_lat - min_lat
    area = dlon * dlat
    if area <= tile_deg2:
        return [bbox]

    # Compute number of cells along each axis so that each cell stays within the
    # area cap.  We split lon and lat equally:  ncols * nrows >= area / tile_deg2.
    # Use the geometric mean to produce roughly square cells.
    ratio = dlon / dlat if dlat > 0 else 1.0
    nrows = max(1, math.ceil(math.sqrt(area / tile_deg2 / ratio)))
    ncols = max(1, math.ceil(area / tile_deg2 / nrows))

    cell_dlon = dlon / ncols
    cell_dlat = dlat / nrows

    tiles: list[tuple[float, float, float, float]] = []
    for row in range(nrows):
        for col in range(ncols):
            tile_west = min_lon + col * cell_dlon
            tile_south = min_lat + row * cell_dlat
            tile_east = min_lon + (col + 1) * cell_dlon if col < ncols - 1 else max_lon
            tile_north = min_lat + (row + 1) * cell_dlat if row < nrows - 1 else max_lat
            tiles.append((tile_west, tile_south, tile_east, tile_north))

    return tiles


# ---------------------------------------------------------------------------
# bbox / year helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise EsriLandcoverBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise EsriLandcoverBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise EsriLandcoverBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise EsriLandcoverBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise EsriLandcoverBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise EsriLandcoverBboxError(
            f"bbox area {area:.3f} deg^2 exceeds the {_MAX_BBOX_DEG2} deg^2 ceiling "
            "for fetch_esri_landcover_10m. For large areas use fetch_landcover "
            "(NLCD, US-only, ~30 m, no area ceiling) instead."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _resolve_year(year: int | str | None) -> int:
    """Normalize/validate the requested vintage year; default to latest.

    Raises ``EsriLandcoverYearError`` for a non-integer or out-of-range year.
    """
    if year is None:
        return _MAX_YEAR
    try:
        y = int(year)
    except (TypeError, ValueError) as exc:
        raise EsriLandcoverYearError(
            f"year must be an integer in [{_MIN_YEAR}, {_MAX_YEAR}]; got {year!r}"
        ) from exc
    if not (_MIN_YEAR <= y <= _MAX_YEAR):
        raise EsriLandcoverYearError(
            f"year {y} is outside the io-lulc-annual-v02 range "
            f"[{_MIN_YEAR}, {_MAX_YEAR}]."
        )
    return y


def _bbox_intersects(
    item_bbox: Any, bbox: tuple[float, float, float, float]
) -> bool:
    """True iff ``item_bbox`` (min_lon, min_lat, max_lon, max_lat) overlaps bbox."""
    try:
        ib0, ib1, ib2, ib3 = (
            float(item_bbox[0]),
            float(item_bbox[1]),
            float(item_bbox[2]),
            float(item_bbox[3]),
        )
    except (TypeError, ValueError, IndexError):
        return False
    return not (
        ib2 < bbox[0] or ib0 > bbox[2] or ib3 < bbox[1] or ib1 > bbox[3]
    )


# ---------------------------------------------------------------------------
# Core: search -> per-tile warp/window read -> mosaic -> palette COG.
# ---------------------------------------------------------------------------


def _select_items(
    bbox: tuple[float, float, float, float], year: int
) -> list[Any]:
    """Return io-lulc items for ``year`` whose footprint intersects ``bbox``.

    The collection's global-extent UTM tiles (e.g. ``01M``/``60M`` carry a
    -180..180 bbox) pass a coarse bbox-intersection test but hold no real data
    at most longitudes; they contribute only nodata to the mosaic, so the merge
    discards them. Intersecting them is harmless but we still narrow on the
    coarse bbox test to avoid signing+opening obviously-distant zone tiles.

    Raises ``EsriLandcoverNoCoverageError`` when zero items match (honest
    no-coverage signal) and ``EsriLandcoverUpstreamError`` on a search failure.
    """
    dt_range = f"{year}-01-01/{year}-12-31"
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover  --  pystac_client is a hard dep
        raise EsriLandcoverUpstreamError(
            f"pystac-client unavailable; cannot search PC STAC: {exc}"
        ) from exc

    try:
        client = Client.open(_pc_stac.PC_STAC_ROOT)
        search = client.search(
            collections=[_COLLECTION],
            bbox=list(bbox),
            datetime=dt_range,
            limit=100,
        )
        all_items = list(search.items())
    except Exception as exc:  # noqa: BLE001  --  translate any pystac/http error
        raise EsriLandcoverUpstreamError(
            f"PC STAC search failed (collection={_COLLECTION!r}, bbox={bbox}, "
            f"year={year}): {exc}"
        ) from exc

    items = [
        it
        for it in all_items
        if _bbox_intersects(getattr(it, "bbox", None), bbox)
        and str(getattr(it, "properties", {}).get("start_datetime", "")).startswith(
            str(year)
        )
    ]
    if not items:
        raise EsriLandcoverNoCoverageError(
            f"no {_COLLECTION!r} land-cover item covers bbox={bbox} for year {year}."
        )
    return items


def _read_tile_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> tuple[Any, dict | None]:
    """Warp+window-read a tile's band-1 categorical data to EPSG:4326 at ``bbox``.

    Returns ``(uint8 array (H, W), colormap-or-None)``. Nearest resampling
    (categorical); nodata reads back as 0. Raises ``EsriLandcoverUpstreamError``
    on any read failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    vsicurl = "/vsicurl/" + signed_href
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                try:
                    colormap = src.colormap(1)
                except ValueError:
                    colormap = None
                dst_transform = rasterio.transform.from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
                )
                dst = np.zeros((height_px, width_px), dtype="uint8")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.nearest,
                    src_nodata=src.nodata if src.nodata is not None else _NODATA,
                    dst_nodata=_NODATA,
                )
        return dst, colormap
    except EsriLandcoverError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise EsriLandcoverUpstreamError(
            f"Esri land-cover tile read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _fetch_single_tile_mosaic(
    bbox: tuple[float, float, float, float],
    year: int,
) -> tuple[Any, Any, dict | None]:
    """Fetch a single sub-tile bbox and return (mosaic_array, transform, colormap).

    The mosaic is a numpy uint8 array (H, W) in EPSG:4326 at 10 m.  Colormap is
    the GDAL color table from the first source UTM-zone tile that has one.

    Raises:
        ``EsriLandcoverNoCoverageError``: no item / all-nodata mosaic.
        ``EsriLandcoverUpstreamError``: search / read failure.
    """
    import numpy as np
    import rasterio

    items = _select_items(bbox, year)

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)
    dst_transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )

    # Mosaic: first non-nodata class wins (categorical  --  no averaging).
    mosaic = np.zeros((height_px, width_px), dtype="uint8")
    colormap: dict | None = None
    for item in items:
        assets = getattr(item, "assets", {}) or {}
        if _DATA_ASSET not in assets:
            continue
        href = _pc_stac.sas_sign_href(assets[_DATA_ASSET].href, _COLLECTION)
        tile, tile_cmap = _read_tile_window(href, bbox, width_px, height_px)
        if colormap is None and tile_cmap is not None:
            colormap = tile_cmap
        fill = (mosaic == _NODATA) & (tile != _NODATA)
        if fill.any():
            mosaic[fill] = tile[fill]

    if int((mosaic != _NODATA).sum()) == 0:
        raise EsriLandcoverNoCoverageError(
            f"io-lulc items intersected bbox={bbox} for year {year}, but the "
            "mosaic is entirely no-data over the AOI (no land-cover coverage)."
        )

    return mosaic, dst_transform, colormap


def _write_palette_cog(
    mosaic: Any,
    width_px: int,
    height_px: int,
    dst_transform: Any,
    colormap: dict | None,
    bbox: tuple[float, float, float, float],
    year: int,
) -> bytes:
    """Write a single-band uint8 palette COG to a temp file and return its bytes.

    Uses a NamedTemporaryFile with try/finally cleanup so the temp file is
    always removed even on write failure (addresses known temp-dir-leak pattern).
    """
    import rasterio

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_esri_lc_"
        ) as f:
            tmp_path = f.name
        profile = dict(
            driver="COG",
            dtype="uint8",
            count=1,
            height=height_px,
            width=width_px,
            crs="EPSG:4326",
            transform=dst_transform,
            compress="DEFLATE",
            nodata=_NODATA,
        )
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(mosaic, 1)
            if colormap is not None:
                dst.write_colormap(1, colormap)
                try:
                    from rasterio.enums import ColorInterp

                    interp = list(dst.colorinterp)
                    interp[0] = ColorInterp.palette
                    dst.colorinterp = tuple(interp)
                except Exception:  # noqa: BLE001  --  colorinterp set is best-effort
                    pass
            else:
                logger.warning(
                    "fetch_esri_landcover_10m: no embedded color table found on "
                    "any source tile (bbox=%s year=%d); output may render grey.",
                    bbox,
                    year,
                )
        with open(tmp_path, "rb") as fh:
            return fh.read()
    except EsriLandcoverError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise EsriLandcoverUpstreamError(
            f"Esri land-cover COG write failed for bbox={bbox} year={year}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _fetch_landcover_cog_bytes(
    bbox: tuple[float, float, float, float],
    year: int,
) -> bytes:
    """Search + mosaic io-lulc tiles for ``bbox``/``year`` -> palette COG bytes.

    For bboxes larger than ``_TILE_DEG2`` the bbox is automatically subdivided
    into a grid of sub-tiles (each <= ``_TILE_DEG2`` deg^2), each fetched and
    mosaicked independently, then merged into one output COG via
    ``rasterio.merge`` (nearest, categorical  --  class codes never interpolated).
    Temp files for sub-tiles are written to a ``TemporaryDirectory`` and cleaned
    up unconditionally via try/finally.

    Raises:
        ``EsriLandcoverNoCoverageError``: no item / all-nodata mosaic (honest).
        ``EsriLandcoverUpstreamError``: search / read / write failure.
    """
    import numpy as np
    import rasterio
    from rasterio.merge import merge as rio_merge

    sub_bboxes = _plan_tile_grid(bbox, tile_deg2=_TILE_DEG2)
    n_tiles = len(sub_bboxes)

    if n_tiles == 1:
        # Fast path: single tile (original behavior).
        mosaic, dst_transform, colormap = _fetch_single_tile_mosaic(bbox, year)
        height_px, width_px = mosaic.shape
        # Telemetry.
        vals, counts = np.unique(mosaic, return_counts=True)
        hist = {
            _CLASS_LABELS.get(int(v), f"class_{int(v)}"): int(c)
            for v, c in zip(vals, counts)
        }
        cog_bytes = _write_palette_cog(
            mosaic, width_px, height_px, dst_transform, colormap, bbox, year
        )
        logger.info(
            "fetch_esri_landcover_10m: year=%d bbox=%s grid=1x1 -> %d-byte "
            "1-band palette COG (%dx%d) classes=%s",
            year, bbox, len(cog_bytes), width_px, height_px, hist,
        )
        return cog_bytes

    # Multi-tile path: fetch each sub-bbox, write to temp TIFs, merge.
    logger.info(
        "fetch_esri_landcover_10m: year=%d bbox=%s area=%.3f deg^2 -> %d sub-tiles "
        "(tile_cap=%.2f deg^2); fetching ...",
        year,
        bbox,
        (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]),
        n_tiles,
        _TILE_DEG2,
    )

    collected_colormap: dict | None = None
    tile_paths: list[str] = []

    with tempfile.TemporaryDirectory(prefix="trid3nt_esri_lc_tiles_") as tmp_dir:
        try:
            for idx, sub_bbox in enumerate(sub_bboxes):
                try:
                    sub_mosaic, sub_transform, sub_cmap = _fetch_single_tile_mosaic(
                        sub_bbox, year
                    )
                except EsriLandcoverNoCoverageError:
                    # This sub-tile has no coverage; skip it -- overall coverage
                    # check happens after merge.
                    logger.debug(
                        "fetch_esri_landcover_10m: sub-tile %d/%d bbox=%s "
                        "has no coverage; skipping.",
                        idx + 1,
                        n_tiles,
                        sub_bbox,
                    )
                    continue

                if collected_colormap is None and sub_cmap is not None:
                    collected_colormap = sub_cmap

                sub_h, sub_w = sub_mosaic.shape
                tile_path = os.path.join(tmp_dir, f"sub_{idx:04d}.tif")
                sub_profile = dict(
                    driver="GTiff",
                    dtype="uint8",
                    count=1,
                    height=sub_h,
                    width=sub_w,
                    crs="EPSG:4326",
                    transform=sub_transform,
                    compress="DEFLATE",
                    nodata=_NODATA,
                )
                with rasterio.open(tile_path, "w", **sub_profile) as dst:
                    dst.write(sub_mosaic, 1)
                tile_paths.append(tile_path)

                logger.debug(
                    "fetch_esri_landcover_10m: sub-tile %d/%d bbox=%s done "
                    "(%dx%d px).",
                    idx + 1,
                    n_tiles,
                    sub_bbox,
                    sub_w,
                    sub_h,
                )

            if not tile_paths:
                raise EsriLandcoverNoCoverageError(
                    f"No sub-tile had coverage for bbox={bbox} year={year} "
                    "(all sub-bboxes were entirely no-data)."
                )

            # Merge all sub-tile TIFs into the full mosaic using nearest resampling
            # (categorical-safe: no interpolation, first non-nodata wins).
            open_files = [rasterio.open(p) for p in tile_paths]
            try:
                merged, merged_transform = rio_merge(
                    open_files,
                    bounds=(bbox[0], bbox[1], bbox[2], bbox[3]),
                    resampling=rasterio.enums.Resampling.nearest,
                    nodata=_NODATA,
                    dtype="uint8",
                    method="first",
                )
            finally:
                for fh in open_files:
                    try:
                        fh.close()
                    except Exception:  # noqa: BLE001
                        pass

            # merged shape: (bands, H, W) -- extract band 0.
            merged_arr = merged[0]
            height_px, width_px = merged_arr.shape

            if int((merged_arr != _NODATA).sum()) == 0:
                raise EsriLandcoverNoCoverageError(
                    f"Merged mosaic for bbox={bbox} year={year} is entirely "
                    "no-data across all sub-tiles (no land-cover coverage)."
                )

        except EsriLandcoverError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EsriLandcoverUpstreamError(
                f"Esri land-cover tiled mosaic failed for bbox={bbox} year={year}: {exc}"
            ) from exc

        # Write final COG inside the temp dir, then read and return bytes.
        # (The TemporaryDirectory context manager cleans up the dir on exit.)
        cog_bytes = _write_palette_cog(
            merged_arr,
            width_px,
            height_px,
            merged_transform,
            collected_colormap,
            bbox,
            year,
        )

    vals, counts = np.unique(merged_arr, return_counts=True)
    hist = {
        _CLASS_LABELS.get(int(v), f"class_{int(v)}"): int(c)
        for v, c in zip(vals, counts)
    }
    logger.info(
        "fetch_esri_landcover_10m: year=%d bbox=%s grid=%d sub-tiles "
        "-> %d-byte 1-band palette COG (%dx%d) classes=%s",
        year,
        bbox,
        n_tiles,
        len(cog_bytes),
        width_px,
        height_px,
        hist,
    )
    return cog_bytes


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (PC STAC public API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_esri_landcover_10m(
    bbox: tuple[float, float, float, float],
    year: int | str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Global 10 m land cover (Esri / Impact Observatory land-use land-cover) for a bbox -- trees, crops, built area, water, worldwide.

    Use this (not ``fetch_landcover``, which serves US-only NLCD as a dict) when
    the AOI is OUTSIDE CONUS or you want a consistent 10 m land-cover schema
    across borders. Returns a bare ``LayerURI`` (NOT the ``fetch_landcover`` dict).

    **What it does:** Searches the Microsoft Planetary Computer for the Impact
    Observatory ``io-lulc-annual-v02`` 10 m annual Land-Use Land-Cover items
    covering ``bbox`` in ``year``, warps+window-reads each intersecting UTM-zone
    tile to EPSG:4326 at 10 m (nearest, categorical), mosaics them, and returns a
    single-band categorical COG with the official 9-class color table baked in.
    ``publish_layer`` colorizes it directly from the embedded palette (no
    rescale/colormap override), the same path NLCD ``fetch_landcover`` uses.

    For bboxes larger than ~0.5 deg^2 the tool automatically tiles the bbox into
    sub-requests and mosaicks the results, so single-county to multi-county AOIs
    work without narrowing the bbox. The practical ceiling is 8 deg^2 (~a few
    counties / small state chunk); above that use ``fetch_landcover`` (NLCD,
    US-only, ~30 m, no area ceiling).

    Classes: 1=Water, 2=Trees, 4=Flooded vegetation, 5=Crops, 7=Built area,
    8=Bare ground, 9=Snow/Ice, 10=Clouds, 11=Rangeland (0=No Data).

    This is GLOBAL  --  the worldwide complement to the US-only NLCD
    ``fetch_landcover``. Use it for land cover anywhere outside CONUS (Africa,
    Asia, South America, Europe) or when you want a consistent 10 m schema
    across borders.

    **When to use:**
    - User asks "what land cover is here?" / "show forest vs crops vs urban" for
      any area up to multi-county scale (<=8 deg^2).
    - AOI is outside CONUS, or you want 10 m global resolution.
    - A consistent 10 m global land-cover layer for exposure / context anywhere.
    - Compare two years (call twice with different ``year``) to see change.

    **When NOT to use:**
    - AOI > 8 deg^2 (state-wide or larger)  --  use ``fetch_landcover`` (NLCD,
      CONUS, ~30 m, no area ceiling).
    - US analysis that must match the NLCD class schema / Manning's roughness
      mapping  --  use ``fetch_landcover`` (NLCD, CONUS).
    - A natural-color picture of the area  --  use ``fetch_sentinel2_truecolor``.
    - Continuous greenness  --  use ``compute_ndvi``.
    - A single-point class lookup  --  this returns a raster.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. Up to 8 deg^2; larger areas should use ``fetch_landcover``.
    - ``year`` (int, optional): vintage in ``[2017, 2023]``. Default: the latest
      available (2023). An out-of-range year is an honest typed error.

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="input"``)
    pointing at a single-band categorical COG in the ``static-30d`` /
    ``esri_landcover_10m`` cache prefix. ``style_preset="categorical_landcover"``
    (embedded-palette passthrough  --  no single-band rescale),
    ``units="esri_io_lulc_class_code"``.

    **Data source:** Esri / Impact Observatory 10m Annual LULC (9-class) V2 via
    the Microsoft Planetary Computer STAC (``io-lulc-annual-v02``).

    Honesty: no covering item (or an all-nodata mosaic) raises a typed
    ``EsriLandcoverNoCoverageError``  --  never a fabricated layer.

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, year)`` calls
    reuse the cached COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)
    q_year = _resolve_year(year)

    params = {
        "bbox": list(q_bbox),
        "year": q_year,
        "collection": _COLLECTION,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_landcover_cog_bytes(q_bbox, q_year),
    )
    assert result.uri is not None, (
        "fetch_esri_landcover_10m is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"esri-lulc-{q_year}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"Esri 10m Land Cover ({q_year})",
        layer_type="raster",
        uri=result.uri,
        # categorical_landcover: publish_layer detects the embedded band-1
        # palette and applies NO rescale/colormap (TiTiler colorizes from the
        # baked color table), so the 9 classes render in their official colors.
        style_preset=_STYLE_PRESET,
        role="input",
        units="esri_io_lulc_class_code",
        bbox=q_bbox,
    )
