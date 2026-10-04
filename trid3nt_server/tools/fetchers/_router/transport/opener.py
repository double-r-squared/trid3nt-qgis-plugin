"""The GDAL read policy and the windowed-COG open over ``/vsicurl/``.

GDAL's own HTTP client reads every remote raster: it merges adjacent ranges, caches
blocks and retries the transport's code set. The upstream STATUS survives on the
error; the body GDAL discards is recovered by one un-retried range GET."""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Iterator

from .client import RETRYABLE_STATUS, get_client, get_once
from .errors import TransportError, TransportUpstreamError, classify_status

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.transport.opener"
)

__all__ = ["READ_POLICY", "MAX_PARALLEL", "open_windowed_cog"]

#: The read policy for every GDAL remote read. Without the range half a windowed
#: read of a large COG issues one request per block, and a high-resolution window
#: costs thousands of round trips instead of a few merged ones.
READ_POLICY: dict[str, str] = {
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_RETRY_CODES": ",".join(str(c) for c in sorted(RETRYABLE_STATUS)),
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "VSI_CACHE": "TRUE",
}

#: How many member objects one mosaic read opens at once.
MAX_PARALLEL = 8

_HTTP_STATUS = re.compile(r"HTTP response code: (\d{3})")


def _typed(url: str, exc: Exception) -> TransportError:
    """GDAL's error as the transport's typed one, with the provider's body read back
    by a single range GET, since GDAL keeps the status and drops the body."""
    m = _HTTP_STATUS.search(str(exc))
    if m is None:
        return TransportUpstreamError(f"remote raster open/read failed url={url}: {exc}")
    status = int(m.group(1))
    try:
        body, _ = get_once(get_client(), url, headers={"Range": "bytes=0-0"})
        text = body.decode("utf-8", "replace")
    except TransportError:
        text = None
    return classify_status(status, text, url)


@contextmanager
def open_windowed_cog(url: str) -> Iterator:
    """Open a remote COG through ``/vsicurl/`` under :data:`READ_POLICY`, yielding the
    rasterio dataset. A GDAL failure in the open or in the caller's reads raises the
    typed transport error in place of the opaque ``RasterioIOError``."""
    import rasterio
    from rasterio.errors import RasterioError

    try:
        with rasterio.Env(**READ_POLICY), rasterio.open(f"/vsicurl/{url}") as src:
            yield src
    except RasterioError as exc:
        raise _typed(url, exc) from exc
