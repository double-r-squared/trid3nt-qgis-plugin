"""Pre-flight probe + the windowed-COG open context manager (the executor seam).

``open_windowed_cog`` is the single entry point raster_cog uses instead of
``rasterio.open('/vsicurl/'+url)``. It (1) pre-flights the object with a HEAD to
size it and surface a typed EARLY error (status INT + verbatim body -- S3 XML
NoSuchKey vs AccessDenied distinguished; HEAD carries no S3 body, so a tiny
range GET recovers the verbatim ``<Code>`` on a non-2xx); (2) opens the dataset
through the coalescing transport opener; (3) bridges the GDAL C-frame exception
swallow -- on any rasterio failure it re-raises the transport's recorded typed
original so the router classifies retryability from the real status.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import httpx

from .client import get_client, head
from .errors import TransportError, TransportUpstreamError, classify_status
from .range_file import TransportOpener

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.transport.opener"
)

__all__ = ["preflight", "open_windowed_cog"]


def _recover_error_body(client: httpx.Client, url: str, status: int) -> str | None:
    """Recover the verbatim S3 XML error body a HEAD omits, via a 1-byte GET."""
    try:
        r = client.get(url, headers={"Range": "bytes=0-0"})
    except (httpx.TimeoutException, httpx.TransportError):
        return None
    return r.text if r.status_code >= 400 else None


def _size_from_range(client: httpx.Client, url: str) -> int:
    """Size an object whose HEAD omits Content-Length, via a Content-Range GET."""
    r = client.get(url, headers={"Range": "bytes=0-0"})
    if r.status_code >= 400:
        raise classify_status(r.status_code, r.text, url)
    cr = r.headers.get("content-range", "")
    if "/" in cr:
        total = cr.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    cl = r.headers.get("content-length")
    if cl and cl.isdigit():
        return int(cl)
    raise TransportUpstreamError(f"could not determine object size url={url}")


def preflight(url: str, client: httpx.Client) -> int:
    """HEAD the object; return its byte size or raise a typed early error.

    A non-2xx HEAD status is classified with a verbatim body recovered via a tiny
    range GET (S3 HEAD returns no body): 404/NoSuchKey, 403/AccessDenied, 429/5xx.
    """
    resp = head(client, url)
    if resp.status_code >= 400:
        body = _recover_error_body(client, url, resp.status_code)
        raise classify_status(resp.status_code, body, url)
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit():
        return int(cl)
    return _size_from_range(client, url)


@contextmanager
def open_windowed_cog(url: str) -> Iterator:
    """Open a remote COG for windowed reads through the coalescing transport.

    Yields an open rasterio dataset. On any failure the transport's recorded typed
    error (NoSuchKey/AccessDenied/upstream) is re-raised in place of the opaque
    ``RasterioIOError``; a pre-flight error raises before GDAL is ever invoked.
    """
    import rasterio

    client = get_client()
    size = preflight(url, client)  # typed early error; GDAL not yet involved
    opener = TransportOpener(url, client, size)
    try:
        with rasterio.open(url, opener=opener) as src:
            yield src
            # The caller's reads ran inside this block. A mid-read transport error
            # is recorded (not raised) to avoid the unguarded-callback abort, so
            # surface it here even when GDAL swallowed it into a partial read.
            recorded = opener.recorded_error()
            if recorded is not None:
                raise recorded
    except TransportError:
        raise
    except Exception as exc:  # noqa: BLE001 -- GDAL swallowed the real cause
        recorded = opener.recorded_error()
        if recorded is not None:
            raise recorded from exc
        raise TransportUpstreamError(
            f"remote raster open/read failed url={url}: {exc}") from exc
