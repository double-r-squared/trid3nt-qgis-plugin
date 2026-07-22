"""Shared Microsoft Planetary Computer STAC helpers (conservation micro-North-Star).

The conservation tool set (``compute_ndvi`` / ``fetch_naip`` / ``fetch_mobi``)
all read Cloud-Optimized GeoTIFF assets from the Microsoft Planetary Computer
(PC) STAC catalog. PC assets live in Azure Blob storage behind short-lived SAS
tokens; an unsigned ``/vsicurl/`` read of a raw blob href 404s. The official
``planetary-computer`` SDK signs assets, but it is NOT installed in the agent
venv, so this module signs assets directly off the PC SAS REST endpoint:

    GET https://planetarycomputer.microsoft.com/api/sas/v1/token/<collection>
        -> {"token": "<sas-query-string>", "msft:expiry": "<iso8601>"}

We append ``?<token>`` to the blob href and hand the signed URL to GDAL via
``/vsicurl/``. The token is cached per-collection for its lifetime so a
multi-asset fetch signs once.

STAC search uses ``pystac-client`` (installed) against the PC root catalog:

    https://planetarycomputer.microsoft.com/api/stac/v1

These helpers are deliberately thin and import-safe (no heavy module-level
imports of rasterio / pystac so the tool modules stay importable in test
environments that mock the network). Every blocking call is a plain sync
function  --  the agent loop off-loads the whole tool body via
``asyncio.to_thread`` (the ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` set in server.py), so
these helpers must never touch the asyncio loop.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger("grace2_agent.tools._pc_stac")

#: Planetary Computer STAC API root (verified live 2026-06-22).
PC_STAC_ROOT = "https://planetarycomputer.microsoft.com/api/stac/v1"

#: Planetary Computer SAS token endpoint (per-collection). Returns a JSON
#: body ``{"token": "...", "msft:expiry": "..."}``; we append ``?<token>`` to a
#: blob href to make it readable by GDAL ``/vsicurl/``.
PC_SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token"

#: User-Agent per PC + general good-citizen guidance.
USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeout for the SAS sign call (fast metadata round-trip).
_SIGN_TIMEOUT_S = 30.0

#: GDAL env for SAS-tokenized blob reads. The SAS token carries the auth in the
#: query string, so we are not using AWS/Azure SDK credentials, just the signed
#: URL handed to ``/vsicurl/``.
VSICURL_ENV_KW = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.TIF,.TIFF",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    VSI_CACHE="TRUE",
)


class PCStacError(RuntimeError):
    """Base class for Planetary Computer STAC helper failures."""

    error_code = "PC_STAC_ERROR"
    retryable = True


class PCStacNoItemsError(PCStacError):
    """The STAC search returned zero items for the requested bbox / window.

    Honest no-coverage / no-imagery signal (data-source fallback norm)  --  the
    caller surfaces a typed no-coverage error rather than fabricating a layer.
    """

    error_code = "PC_STAC_NO_ITEMS"
    retryable = False


class PCStacUpstreamError(PCStacError):
    """A PC STAC search / SAS-sign / asset read failed at the network layer."""

    error_code = "PC_STAC_UPSTREAM_ERROR"
    retryable = True


# --------------------------------------------------------------------------- #
# Per-collection SAS token cache (thread-safe; the tool bodies run in worker
# threads via asyncio.to_thread).
# --------------------------------------------------------------------------- #

_TOKEN_LOCK = threading.Lock()
#: collection -> (token_query_string, monotonic_expiry_seconds)
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

#: Refresh a SAS token this many seconds before its stated expiry so a long
#: multi-asset read never trips an expired token mid-stream.
_TOKEN_REFRESH_SKEW_S = 120.0


def _request_sas_token(collection: str) -> tuple[str, float]:
    """Fetch a fresh SAS token query-string for ``collection``.

    Returns ``(token_query_string, monotonic_expiry_seconds)``. Raises
    ``PCStacUpstreamError`` on any network / parse failure.
    """
    url = f"{PC_SAS_TOKEN_URL}/{collection}"
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=_SIGN_TIMEOUT_S
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise PCStacUpstreamError(
            f"PC SAS token request failed for collection={collection!r}: {exc}"
        ) from exc
    except ValueError as exc:  # JSON decode
        raise PCStacUpstreamError(
            f"PC SAS token response for collection={collection!r} was not JSON: {exc}"
        ) from exc

    token = body.get("token")
    if not token or not isinstance(token, str):
        raise PCStacUpstreamError(
            f"PC SAS token response for collection={collection!r} had no token: "
            f"{str(body)[:200]!r}"
        )
    # We treat tokens as valid for a conservative fixed window from now rather
    # than parsing the (ISO-8601) ``msft:expiry``  --  the endpoint typically
    # mints ~1h tokens; 45 min keeps us well inside that with margin.
    expiry = time.monotonic() + 45 * 60.0 - _TOKEN_REFRESH_SKEW_S
    return token, expiry


def sas_sign_href(href: str, collection: str) -> str:
    """Return ``href`` with a (cached) PC SAS token appended as the query string.

    If ``href`` already carries a query string we append with ``&``; otherwise
    with ``?``. The token is cached per-collection for its lifetime so a
    multi-asset fetch signs once.
    """
    now = time.monotonic()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(collection)
        if cached is None or cached[1] <= now:
            token, expiry = _request_sas_token(collection)
            _TOKEN_CACHE[collection] = (token, expiry)
        else:
            token = cached[0]
    sep = "&" if urlparse(href).query else "?"
    return f"{href}{sep}{token}"


def search_least_cloudy_item(
    *,
    collection: str,
    bbox: tuple[float, float, float, float],
    datetime_range: str | None = None,
    max_cloud_cover: float | None = None,
    sort_by_cloud: bool = False,
) -> Any:
    """Search the PC STAC catalog and return the single best-matching item.

    Args:
        collection: PC collection id (e.g. ``"sentinel-2-l2a"``, ``"naip"``,
            ``"mobi"``).
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
        datetime_range: optional STAC ``datetime`` filter
            (``"2024-04-01/2024-09-30"`` or a single instant). ``None`` for
            time-static collections (mobi).
        max_cloud_cover: optional ``eo:cloud_cover`` upper bound (percent).
        sort_by_cloud: when True, return the LEAST-cloudy item among the
            matches (Sentinel-2 NDVI path). When False, return the first
            (most-recent / best-overlap) match.

    Returns:
        A ``pystac.Item``.

    Raises:
        ``PCStacNoItemsError``: zero items matched (honest no-coverage signal).
        ``PCStacUpstreamError``: the search itself failed at the network layer.
    """
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover  --  pystac_client is a hard dep
        raise PCStacUpstreamError(
            f"pystac-client unavailable; cannot search PC STAC: {exc}"
        ) from exc

    query: dict[str, Any] | None = None
    if max_cloud_cover is not None:
        query = {"eo:cloud_cover": {"lt": float(max_cloud_cover)}}

    try:
        client = Client.open(PC_STAC_ROOT)
        search = client.search(
            collections=[collection],
            bbox=list(bbox),
            datetime=datetime_range,
            query=query,
            limit=100,
        )
        items = list(search.items())
    except PCStacError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any pystac/http error
        raise PCStacUpstreamError(
            f"PC STAC search failed (collection={collection!r}, bbox={bbox}): {exc}"
        ) from exc

    if not items:
        raise PCStacNoItemsError(
            f"no {collection!r} items intersect bbox={bbox}"
            + (f" within {datetime_range}" if datetime_range else "")
            + (
                f" under {max_cloud_cover}% cloud cover"
                if max_cloud_cover is not None
                else ""
            )
        )

    if sort_by_cloud:
        items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100.0))
    return items[0]


# --------------------------------------------------------------------------- #
# bbox <-> grid sizing helpers (shared by the windowed COG read path).
# --------------------------------------------------------------------------- #


def bbox_pixel_dims(
    bbox: tuple[float, float, float, float],
    native_cell_m: float,
    *,
    px_min: int = 16,
    px_max: int = 4096,
) -> tuple[int, int]:
    """Compute ``(width_px, height_px)`` for ``bbox`` at ``native_cell_m``.

    Approximates metres-per-degree at the bbox mid-latitude; clamps each axis
    to ``[px_min, px_max]`` so a large AOI never materializes an enormous grid.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
    width_m = max(0.0, max_lon - min_lon) * m_per_deg_lon
    height_m = max(0.0, max_lat - min_lat) * 111_320.0
    width_px = max(px_min, min(px_max, int(round(width_m / native_cell_m)) or px_min))
    height_px = max(px_min, min(px_max, int(round(height_m / native_cell_m)) or px_min))
    return width_px, height_px
