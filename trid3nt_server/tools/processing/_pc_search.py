"""Shared Planetary Computer STAC access for the processing tools.

Signing is applied at the catalog client, so an item's asset hrefs arrive
readable by GDAL. Every call here is plain sync and must never touch the loop.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PC_STAC_ROOT",
    "PCStacError",
    "PCStacNoItemsError",
    "PCStacUpstreamError",
    "VSICURL_ENV_KW",
    "search_least_cloudy_item",
]

#: Planetary Computer STAC API root.
PC_STAC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"

#: GDAL env for signed blob reads: the signature travels in the query string, so
#: no SDK credentials are involved -- only the read tuning a COG window wants.
VSICURL_ENV_KW = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.TIF,.TIFF",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
)


class PCStacError(RuntimeError):
    """Base class for Planetary Computer STAC failures."""

    error_code = "PC_STAC_ERROR"
    retryable = True


class PCStacNoItemsError(PCStacError):
    """Zero items matched the bbox / window -- honest no-coverage, not a failure."""

    error_code = "PC_STAC_NO_ITEMS"
    retryable = False


class PCStacUpstreamError(PCStacError):
    """The search itself failed at the network layer."""

    error_code = "PC_STAC_UPSTREAM_ERROR"
    retryable = True


def search_least_cloudy_item(
    *,
    collection: str,
    bbox: tuple[float, float, float, float],
    datetime_range: str | None = None,
    max_cloud_cover: float | None = None,
    sort_by_cloud: bool = False,
) -> Any:
    """The single best-matching item, hrefs already signed; ``sort_by_cloud`` picks
    the least-cloudy. No match raises PCStacNoItemsError, a network failure PCStacUpstreamError.
    """
    import planetary_computer
    from pystac_client import Client

    query = ({"eo:cloud_cover": {"lt": float(max_cloud_cover)}}
             if max_cloud_cover is not None else None)
    try:
        client = Client.open(PC_STAC_ROOT, modifier=planetary_computer.sign_inplace)
        items = list(client.search(
            collections=[collection], bbox=list(bbox), datetime=datetime_range,
            query=query, limit=100).items())
    except Exception as exc:  # noqa: BLE001 -- translate any pystac/http error
        raise PCStacUpstreamError(
            f"PC STAC search failed (collection={collection!r}, bbox={bbox}): {exc}"
        ) from exc

    if not items:
        raise PCStacNoItemsError(
            f"no {collection!r} items intersect bbox={bbox}"
            + (f" within {datetime_range}" if datetime_range else "")
            + (f" under {max_cloud_cover}% cloud cover"
               if max_cloud_cover is not None else ""))

    if sort_by_cloud:
        items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100.0))
    return items[0]
