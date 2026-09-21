"""NOAA NCEI ETOPO 2022 15 arc-second global relief: the tile grid, computed.

The 15 arc-second surface-elevation COGs sit on a COMPLETE global grid of
15-degree squares whose filenames are the corner, so which tiles an AOI needs is
arithmetic rather than an index read - there is no manifest to consult."""

from __future__ import annotations

import logging
import math
from typing import Any

from ..._fetch_common import FetchError
from ..._tile_mosaic import MosaicEmpty, mosaic
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "EtopoError",
    "EtopoInputError",
    "EtopoUpstreamError",
    "EtopoCoverageGapError",
    "GLOBAL_ROOT",
    "TILE_DEG",
    "tile_url",
    "select_tiles",
    "validate_etopo",
    "read_etopo",
]


class EtopoError(FetchError):
    """Base class for fetch_etopo failures."""

    error_code: str = "ETOPO_ERROR"
    retryable: bool = True


class EtopoInputError(EtopoError):
    """Bad inputs (bbox shape, out-of-range coordinates, non-finite timeout)."""

    error_code = "ETOPO_INPUT_INVALID"
    retryable = False


class EtopoUpstreamError(EtopoError):
    """Tile read or merge failure."""

    error_code = "ETOPO_UPSTREAM_ERROR"
    retryable = True


class EtopoCoverageGapError(EtopoError):
    """The tiles the AOI resolves to painted none of it."""

    error_code = "ETOPO_COVERAGE_GAP"
    retryable = False


#: The published 15 arc-second surface-relief COG directory.
GLOBAL_ROOT = (
    "https://www.ngdc.noaa.gov/mgg/global/relief/ETOPO2022/data/15s/"
    "15s_surface_elev_gtif/"
)

#: The grid the COGs are cut on: 15-degree squares, complete over the globe.
TILE_DEG = 15.0

#: Ceiling on the tiles one AOI may merge. Every tile is a 15-degree square, so
#: past this the request is asking for a hemisphere.
_MAX_TILES = 24


def tile_url(nw_lat: float, nw_lon: float) -> str:
    """The COG URL for the 15-degree tile whose NW corner is ``(nw_lat, nw_lon)``."""
    ns = "N" if nw_lat >= 0 else "S"
    ew = "W" if nw_lon < 0 else "E"
    return (f"{GLOBAL_ROOT}ETOPO_2022_v1_15s_"
            f"{ns}{abs(int(round(nw_lat))):02d}{ew}{abs(int(round(nw_lon))):03d}"
            "_surface.tif")


def select_tiles(bbox: tuple[float, float, float, float]) -> list[str]:
    """The COG URLs whose 15-degree footprint intersects the AOI."""
    west, south, east, north = bbox
    urls: list[str] = []
    row = math.floor(south / TILE_DEG) * TILE_DEG
    while row < north:
        column = math.floor(west / TILE_DEG) * TILE_DEG
        while column < east:
            urls.append(tile_url(row + TILE_DEG, column))
            column += TILE_DEG
        row += TILE_DEG
    logger.info("fetch_etopo: %d tiles cover bbox=%s", len(urls), bbox)
    return urls


@register_hook("etopo.validate")
def validate_etopo(spec: Any, params: dict[str, Any]) -> None:
    """Bbox finiteness and ordering, before any fetch. The grid is global, so
    there is no envelope to screen against."""
    raw = params.get("bbox")
    try:
        bbox = tuple(float(v) for v in raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise EtopoInputError(f"bbox must be four numbers; got {raw!r}") from exc
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise EtopoInputError(f"bbox must be four finite numbers; got {raw!r}")
    west, south, east, north = bbox
    if east <= west or north <= south:
        raise EtopoInputError(
            f"degenerate bbox (min must be strictly less than max); got {bbox!r}")
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0
            and -90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise EtopoInputError(f"bbox is outside lon/lat range; got {bbox!r}")
    if len(select_tiles(bbox)) > _MAX_TILES:
        raise EtopoInputError(
            f"bbox {bbox!r} spans more than {_MAX_TILES} of the 15-degree ETOPO "
            "tiles; narrow it")
    timeout = params.get("timeout_s")
    if timeout is not None and (not math.isfinite(float(timeout))
                                or float(timeout) <= 0):
        raise EtopoInputError(f"timeout_s must be > 0 and finite; got {timeout!r}")


@register_hook("etopo.read")
def read_etopo(spec: Any, params: dict[str, Any], *,
               timeout_s: float) -> tuple[Any, Any, Any]:
    """AOI -> the 15-degree tiles covering it -> one merged grid."""
    bbox = tuple(float(v) for v in params["bbox"])
    target_crs = str(params.get("target_crs") or "EPSG:32616").strip()
    asked = params.get("resolution_m")

    paths = [f"/vsicurl/{url}" for url in select_tiles(bbox)]
    try:
        grid, transform, crs, painted = mosaic(
            paths, target_crs, bbox,
            resolution_m=(float(asked) if asked is not None else None),
            source="fetch_etopo")
    except MosaicEmpty as exc:
        raise EtopoCoverageGapError(
            f"the ETOPO 2022 tiles over {bbox!r} painted no cell of it: {exc}"
        ) from exc
    logger.info("fetch_etopo: %d/%d tiles painted over %s",
                sum(painted), len(paths), bbox)
    return grid, transform, crs
