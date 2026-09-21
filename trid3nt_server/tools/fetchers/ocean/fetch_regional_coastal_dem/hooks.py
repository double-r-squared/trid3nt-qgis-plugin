"""NOAA NCEI regional coastal DEMs: the collection's STAC items, read.

Each regional collection is a separate published product with its own footprint
and its own item collection, so a collection is a ROW of the table below and the
AOI is intersected against the items that collection actually shipped."""

# These are the FINEST beds on the US coast - a metre or better where a CoNED
# integration exists - and they exist only where one was built, which is why the
# table is explicit: a collection nobody published is not a gap this row closes.

from __future__ import annotations

import logging
import math
from typing import Any

from ..._fetch_common import FetchError
from ..._tile_mosaic import MosaicEmpty, mosaic
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "RegionalCoastalDemError",
    "RegionalCoastalDemInputError",
    "RegionalCoastalDemUpstreamError",
    "RegionalCoastalDemCoverageGapError",
    "COLLECTIONS",
    "select_tiles",
    "validate_regional_coastal_dem",
    "read_regional_coastal_dem",
]


class RegionalCoastalDemError(FetchError):
    """Base class for fetch_regional_coastal_dem failures."""

    error_code: str = "REGIONAL_COASTAL_DEM_ERROR"
    retryable: bool = True


class RegionalCoastalDemInputError(RegionalCoastalDemError):
    """Bad inputs (bbox shape, out-of-range coordinates, non-finite timeout)."""

    error_code = "REGIONAL_COASTAL_DEM_INPUT_INVALID"
    retryable = False


class RegionalCoastalDemUpstreamError(RegionalCoastalDemError):
    """Item-collection read, tile read or merge failure."""

    error_code = "REGIONAL_COASTAL_DEM_UPSTREAM_ERROR"
    retryable = True


class RegionalCoastalDemCoverageGapError(RegionalCoastalDemError):
    """No published regional collection reaches the AOI, or none of the items it
    ships painted it."""

    error_code = "REGIONAL_COASTAL_DEM_COVERAGE_GAP"
    retryable = False


#: The published regional integrated topo-bathy collections, each with the
#: footprint it covers and the STAC item collection listing what it shipped.
COLLECTIONS: tuple[dict[str, Any], ...] = (
    {
        "name": "CA_north_coned_DEM_2020_9181",
        "label": "USGS CoNED Northern California 1 m topo-bathy DEM (2020, NAVD88)",
        "bbox": (-124.5719, 37.7702, -122.4453, 42.0126),
        "items_url": (
            "https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/"
            "dem/CA_north_coned_DEM_2020_9181/stac/noaa_item_collection_m9181.json"
        ),
    },
)

#: Ceiling on the items one AOI may merge, the same ceiling the sibling tile rows
#: hold: past it the request is asking for a regional mosaic.
_MAX_TILES = 64


def select_tiles(bbox: tuple[float, float, float, float],
                 timeout_s: float = 120.0) -> tuple[list[str], list[str]]:
    """The item asset URLs intersecting the AOI, and the collections they came
    from. A collection whose item list cannot be read raises: a fine bed silently
    skipped is a coarser answer nobody was told about."""
    import requests

    west, south, east, north = bbox
    urls: list[str] = []
    collections: list[str] = []
    for collection in COLLECTIONS:
        c_w, c_s, c_e, c_n = collection["bbox"]
        if east < c_w or west > c_e or north < c_s or south > c_n:
            continue
        try:
            resp = requests.get(collection["items_url"], timeout=timeout_s)
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RegionalCoastalDemUpstreamError(
                f"could not read the item collection for "
                f"{collection['name']}: {exc}") from exc
        found = len(urls)
        for item in (items.get("features") or []):
            box = item.get("bbox")
            if not box or len(box) < 4:
                continue
            if box[2] < west or box[0] > east or box[3] < south or box[1] > north:
                continue
            for asset in (item.get("assets") or {}).values():
                href = asset.get("href") if isinstance(asset, dict) else None
                if href and str(href).lower().endswith((".tif", ".tiff")):
                    urls.append(str(href))
                    break
        if len(urls) > found:
            collections.append(str(collection["name"]))
    logger.info("fetch_regional_coastal_dem: %d items from %s intersect bbox=%s",
                len(urls), collections or "[]", bbox)
    return urls, collections


@register_hook("regional_coastal_dem.validate")
def validate_regional_coastal_dem(spec: Any, params: dict[str, Any]) -> None:
    """Bbox finiteness and ordering, and a footprint some collection reaches."""
    raw = params.get("bbox")
    try:
        bbox = tuple(float(v) for v in raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RegionalCoastalDemInputError(
            f"bbox must be four numbers; got {raw!r}") from exc
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise RegionalCoastalDemInputError(
            f"bbox must be four finite numbers; got {raw!r}")
    west, south, east, north = bbox
    if east <= west or north <= south:
        raise RegionalCoastalDemInputError(
            f"degenerate bbox (min must be strictly less than max); got {bbox!r}")
    reached = [c["name"] for c in COLLECTIONS
               if not (east < c["bbox"][0] or west > c["bbox"][2]
                       or north < c["bbox"][1] or south > c["bbox"][3])]
    if not reached:
        raise RegionalCoastalDemCoverageGapError(
            f"no published NCEI regional coastal DEM reaches {bbox!r}. The "
            f"{len(COLLECTIONS)} collection(s) this row serves are built where "
            "one was funded, and this coast has none.")
    timeout = params.get("timeout_s")
    if timeout is not None and (not math.isfinite(float(timeout))
                                or float(timeout) <= 0):
        raise RegionalCoastalDemInputError(
            f"timeout_s must be > 0 and finite; got {timeout!r}")


@register_hook("regional_coastal_dem.read")
def read_regional_coastal_dem(spec: Any, params: dict[str, Any], *,
                              timeout_s: float) -> tuple[Any, Any, Any]:
    """AOI -> the collections reaching it -> their items -> one merged grid."""
    bbox = tuple(float(v) for v in params["bbox"])
    target_crs = str(params.get("target_crs") or "EPSG:32616").strip()
    fetch_timeout = float(params.get("timeout_s") or 120.0)
    asked = params.get("resolution_m")

    urls, collections = select_tiles(bbox, fetch_timeout)
    if not urls:
        raise RegionalCoastalDemCoverageGapError(
            f"the NCEI regional coastal DEM collection(s) over {bbox!r} ship no "
            "item that intersects it")
    if len(urls) > _MAX_TILES:
        raise RegionalCoastalDemInputError(
            f"AOI {bbox!r} intersects {len(urls)} regional DEM items, past the "
            f"{_MAX_TILES}-item ceiling this row serves; narrow the bbox")

    paths = [f"/vsicurl/{url}" for url in urls]
    try:
        grid, transform, crs, painted = mosaic(
            paths, target_crs, bbox,
            resolution_m=(float(asked) if asked is not None else None),
            source="fetch_regional_coastal_dem")
    except MosaicEmpty as exc:
        raise RegionalCoastalDemCoverageGapError(
            f"the regional coastal DEM items over {bbox!r} painted no cell of "
            f"it: {exc}") from exc
    logger.info("fetch_regional_coastal_dem: %d/%d items from %s painted over %s",
                sum(painted), len(paths), collections, bbox)
    return grid, transform, crs
