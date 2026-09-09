"""Pre-flight probe and the windowed-COG open context manager.

``open_windowed_cog`` pre-flights the object with a HEAD for its size and an early
typed error, opens the dataset through the coalescing transport, and bridges the
GDAL C-frame exception swallow so the real status survives to the caller."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

import httpx

from .client import get_client, head
from .errors import TransportError, TransportUpstreamError, classify_status
from .range_file import TransportOpener

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.transport.opener"
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
    """HEAD the object; return its byte size or raise a typed early error, with the
    verbatim body recovered by a tiny range GET because an S3 HEAD carries none.
    A 403/405 falls back to a range-GET size probe before the error stands."""
    resp = head(client, url)
    if resp.status_code >= 400:
        if resp.status_code in (403, 405):
            try:
                return _size_from_range(client, url)
            except TransportError:
                pass  # range GET confirmed the failure -> classify the HEAD error
        body = _recover_error_body(client, url, resp.status_code)
        raise classify_status(resp.status_code, body, url)
    cl = resp.headers.get("content-length")
    if cl and cl.isdigit():
        return int(cl)
    return _size_from_range(client, url)


@contextmanager
def open_windowed_cog(url: str) -> Iterator:
    """Open a remote COG for windowed reads, yielding an open rasterio dataset. Any
    failure re-raises the transport's recorded typed error in place of the opaque
    ``RasterioIOError``; a pre-flight error raises before GDAL is invoked."""
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
