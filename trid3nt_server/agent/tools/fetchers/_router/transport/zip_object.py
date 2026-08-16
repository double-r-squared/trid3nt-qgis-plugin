"""Whole-object ZIP fetch -- the ONE shared step for the multi-file / DEFLATE-member
family.

A ZIP member that is DEFLATE-compressed (GHSL tiles) or part of a multi-file
sidecar set (TIGER shapefile: .shp/.dbf/.shx/.prj, an NHDPlus FileGDB directory)
cannot be windowed by a byte-range read -- decoding any member forces a near-whole
transfer, and a shapefile needs every sibling co-located. The honest shape is a
WHOLE-OBJECT GET (the ``gzip_object`` precedent at ZIP scale) then in-memory /
tmp-dir extraction. This module is that single step: fetch the object through the
shared transport (the ONE retry authority) and open it as a ``zipfile.ZipFile``
over the in-memory bytes. Callers pick members (``.read(name)`` -> a MemoryFile
raster) or extract siblings (``.extractall(dir)`` -> a geopandas read). Transport
status (404/403/5xx) surfaces as a typed ``Transport*`` error from ``get_bytes``;
a non-ZIP body surfaces as ``zipfile.BadZipFile`` for the caller to classify.
"""

from __future__ import annotations

import io
import zipfile

import httpx

from .client import get_bytes

__all__ = ["get_zip"]


def get_zip(
    client: httpx.Client, url: str, *, headers: dict[str, str] | None = None
) -> zipfile.ZipFile:
    """GET a whole ZIP object through the transport and open it in memory.

    The object is fetched with the shared retry authority (429/5xx/timeout backoff
    + ``Retry-After``); a 404/403 classifies to a typed transport error in
    ``get_bytes`` before this returns. The returned ``ZipFile`` reads from an
    in-memory buffer, so the caller may extract member bytes or ``extractall`` to
    a tmp dir with no further network. Raises ``zipfile.BadZipFile`` on a non-ZIP
    body (the caller maps it to a source-stamped upstream error).
    """
    body, _ct, _final_url = get_bytes(client, url, headers=headers)
    return zipfile.ZipFile(io.BytesIO(body))
