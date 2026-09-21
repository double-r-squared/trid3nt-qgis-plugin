"""NOAA NCEI CUDEM 1/9 arc-second tiles: the tile index, read.

The collection publishes one URL manifest of every delivered tile, and a tile's
NW corner is encoded in its filename, so the AOI is intersected against the index
the programme states rather than against any assumption about the grid."""

# The per-tile DATUM GATE is why the index read is bespoke rather than declared:
# the collection is NAVD88 and a tile stating a tidal datum is refused rather than
# merged, because a silent cross-datum merge is the substitution the
# correct-data-class law exists to prevent. A tile stating no vertical CS at all
# is accepted on the collection's own NAVD88 statement, which is the coverage row.

from __future__ import annotations

import logging
import math
import re
import time
from typing import Any

from ..._fetch_common import FetchError
from ..._tile_mosaic import VSICURL_ENV, MosaicEmpty, mosaic
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "CudemError",
    "CudemInputError",
    "CudemUpstreamError",
    "CudemDatumError",
    "CudemCoverageGapError",
    "COLLECTION_ROOT",
    "URLLIST_URL",
    "TILE_DEG",
    "tile_box",
    "select_tiles",
    "assert_navd88",
    "validate_cudem",
    "read_cudem",
]


class CudemError(FetchError):
    """Base class for fetch_cudem failures."""

    error_code: str = "CUDEM_ERROR"
    retryable: bool = True


class CudemInputError(CudemError):
    """Bad inputs (bbox shape, out-of-range coordinates, non-finite timeout)."""

    error_code = "CUDEM_INPUT_INVALID"
    retryable = False


class CudemUpstreamError(CudemError):
    """Manifest download, tile read or merge failure."""

    error_code = "CUDEM_UPSTREAM_ERROR"
    retryable = True


class CudemDatumError(CudemError):
    """A tile states a vertical datum that is not NAVD88. It is refused rather
    than merged onto a NAVD88 bed."""

    error_code = "CUDEM_DATUM_MISMATCH"
    retryable = False


class CudemCoverageGapError(CudemError):
    """No delivered tile intersects the AOI, or none of them painted it. The
    hosted 1/9 arc-second collection omits stretches of US coast, so an empty
    answer here is a real statement about the collection."""

    error_code = "CUDEM_COVERAGE_GAP"
    retryable = False


#: The "Topobathy 2014" 1/9 arc-second collection root (public S3, anonymous read).
COLLECTION_ROOT = (
    "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/"
    "dem/NCEI_ninth_Topobathy_2014_8483/"
)

#: The authoritative per-tile URL manifest, one https://...tif per line.
URLLIST_URL = COLLECTION_ROOT + "urllist8483.txt"

#: Each tile is a quarter-degree square whose filename encodes its NW corner.
TILE_DEG = 0.25

_TILE_NAME = re.compile(
    r"ncei19_n(?P<lat_i>\d{2})X(?P<lat_f>\d{2})_w(?P<lon_i>\d{2,3})X(?P<lon_f>\d{2})",
    re.IGNORECASE)

#: The US coastal envelope - a coarse pre-screen so a clearly inland or foreign
#: bbox fails before the manifest is downloaded.
_US_COASTAL = (-180.0, 13.0, -64.0, 72.0)

#: Ceiling on the tiles one AOI may merge. Past it the request is asking for a
#: regional mosaic, and the honest answer is to say so.
_MAX_TILES = 64

#: The manifest is one static file the programme republishes on its own cadence,
#: so it is read once per process rather than once per request.
_MANIFEST_TTL_S = 600.0

_manifest: tuple[float, list[str]] | None = None


def _corner(name: str) -> tuple[float, float] | None:
    """A tile's NW corner as ``(lat, lon)``, or ``None`` where the name has none."""
    found = _TILE_NAME.search(name)
    if found is None:
        return None
    lat = float(found.group("lat_i")) + float(found.group("lat_f")) / 100.0
    lon = float(found.group("lon_i")) + float(found.group("lon_f")) / 100.0
    return (lat, -lon)


def tile_box(name: str) -> tuple[float, float, float, float] | None:
    """A tile's quarter-degree footprint as ``(west, south, east, north)``."""
    corner = _corner(name)
    if corner is None:
        return None
    lat, lon = corner
    return (lon, lat - TILE_DEG, lon + TILE_DEG, lat)


def _manifest_urls(timeout_s: float) -> list[str]:
    """Every delivered tile URL the collection's manifest names."""
    global _manifest
    import requests

    if _manifest is not None and (time.monotonic() - _manifest[0]) < _MANIFEST_TTL_S:
        return list(_manifest[1])
    try:
        resp = requests.get(URLLIST_URL, timeout=timeout_s)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise CudemUpstreamError(
            f"could not download the CUDEM tile manifest {URLLIST_URL}: {exc}"
        ) from exc
    urls = [line.strip() for line in resp.text.splitlines()
            if line.strip().lower().endswith(".tif")]
    if not urls:
        raise CudemUpstreamError(
            f"the CUDEM tile manifest {URLLIST_URL} parsed to zero .tif URLs; "
            "the manifest format has moved")
    _manifest = (time.monotonic(), list(urls))
    return urls


def select_tiles(bbox: tuple[float, float, float, float],
                 timeout_s: float = 120.0) -> list[str]:
    """The tile URLs whose quarter-degree footprint intersects the AOI."""
    west, south, east, north = bbox
    chosen: list[str] = []
    for url in _manifest_urls(timeout_s):
        box = tile_box(url)
        if box is None:
            continue
        t_w, t_s, t_e, t_n = box
        if not (t_e < west or t_w > east or t_n < south or t_s > north):
            chosen.append(url)
    logger.info("fetch_cudem: %d tiles intersect bbox=%s", len(chosen), bbox)
    return chosen


def assert_navd88(path: str) -> None:
    """Refuse a tile that states a TIDAL vertical datum.

    A tile stating no vertical CS carries the collection's own NAVD88, which is
    what this row's coverage states; a tile stating MLLW or MHW is a different
    zero, and no offset this row holds bridges it."""
    import rasterio

    stated = ""
    try:
        with rasterio.Env(**VSICURL_ENV):
            with rasterio.open(path) as ds:
                if ds.crs is not None:
                    stated += " " + (ds.crs.to_wkt() or "")
                for key, value in (ds.tags() or {}).items():
                    stated += f" {key}={value}"
    except Exception as exc:  # noqa: BLE001
        raise CudemUpstreamError(
            f"could not read the CUDEM tile header for the datum check "
            f"({path}): {exc}") from exc

    lowered = stated.lower()
    if any(word in lowered for word in ("navd88", "navd 88", "navd_88")):
        return
    tidal = ("mhhw", "mhw", "mllw", "mlw", "lmsl", "msl", "mean sea level",
             "mean high water", "mean low water", "tidal")
    if any(word in lowered for word in tidal):
        raise CudemDatumError(
            f"CUDEM tile {path} states a tidal vertical datum "
            f"({stated.strip()[:200]!r}), not the NAVD88 this collection "
            "publishes; refusing to merge two zeros as one surface")


@register_hook("cudem.validate")
def validate_cudem(spec: Any, params: dict[str, Any]) -> None:
    """Bbox finiteness, ordering and the US coastal envelope, before any fetch."""
    raw = params.get("bbox")
    try:
        bbox = tuple(float(v) for v in raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CudemInputError(f"bbox must be four numbers; got {raw!r}") from exc
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise CudemInputError(f"bbox must be four finite numbers; got {raw!r}")
    west, south, east, north = bbox
    if east <= west or north <= south:
        raise CudemInputError(
            f"degenerate bbox (min must be strictly less than max); got {bbox!r}")
    u_w, u_s, u_e, u_n = _US_COASTAL
    if east < u_w or west > u_e or north < u_s or south > u_n:
        raise CudemInputError(
            f"bbox {bbox!r} is outside the US coastal envelope {_US_COASTAL!r}; "
            "the NCEI CUDEM collection is US-coast-only")
    timeout = params.get("timeout_s")
    if timeout is not None and (not math.isfinite(float(timeout))
                                or float(timeout) <= 0):
        raise CudemInputError(f"timeout_s must be > 0 and finite; got {timeout!r}")


@register_hook("cudem.read")
def read_cudem(spec: Any, params: dict[str, Any], *,
               timeout_s: float) -> tuple[Any, Any, Any]:
    """AOI -> manifest -> per-tile NAVD88 gate -> one merged grid."""
    bbox = tuple(float(v) for v in params["bbox"])
    target_crs = str(params.get("target_crs") or "EPSG:32616").strip()
    fetch_timeout = float(params.get("timeout_s") or 120.0)
    asked = params.get("resolution_m")

    urls = select_tiles(bbox, fetch_timeout)
    if not urls:
        raise CudemCoverageGapError(
            f"no NOAA NCEI CUDEM 1/9 arc-second tile intersects {bbox!r}. The "
            "hosted collection omits stretches of US coast and the Great Lakes "
            "entirely, so there is no nearshore topo-bathymetry here.")
    if len(urls) > _MAX_TILES:
        raise CudemInputError(
            f"AOI {bbox!r} intersects {len(urls)} CUDEM tiles, past the "
            f"{_MAX_TILES}-tile ceiling this row serves; narrow the bbox")

    paths = [f"/vsicurl/{url}" for url in urls]
    for path in paths:
        assert_navd88(path)
    try:
        grid, transform, crs, painted = mosaic(
            paths, target_crs, bbox,
            resolution_m=(float(asked) if asked is not None else None),
            source="fetch_cudem")
    except MosaicEmpty as exc:
        raise CudemCoverageGapError(
            f"the CUDEM tiles over {bbox!r} painted no cell of it: {exc}") from exc
    logger.info("fetch_cudem: %d/%d tiles painted over %s",
                sum(painted), len(paths), bbox)
    return grid, transform, crs
