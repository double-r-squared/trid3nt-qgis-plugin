"""Router remote-FILE transport: one httpx module owning every socket and error.

Pooled client, 1 MiB coalescing plus a parallel range opener, HEAD pre-flight
typed errors, one retry authority, and the GDAL C-frame exception bridge. An
executor calls ``open_windowed_cog`` and maps the typed ``Transport*`` errors."""

from __future__ import annotations

from .client import get_bytes, get_client, get_once, head, post_bytes, range_get
from .errors import (
    TransportAuthError,
    TransportError,
    TransportNotFound,
    TransportTruncatedError,
    TransportUpstreamError,
    classify_status,
)
from .opener import open_windowed_cog, preflight
from .range_file import BLOCK, MAX_PARALLEL, CoalescedRangeFile, TransportOpener
from .staged import StagedEndpointNotConfigured, is_staged_uri, staged_object_url
from .zip_object import get_zip

__all__ = [
    "get_client",
    "head",
    "range_get",
    "get_bytes",
    "get_once",
    "post_bytes",
    "get_zip",
    "is_staged_uri",
    "staged_object_url",
    "StagedEndpointNotConfigured",
    "preflight",
    "open_windowed_cog",
    "CoalescedRangeFile",
    "TransportOpener",
    "BLOCK",
    "MAX_PARALLEL",
    "TransportError",
    "TransportNotFound",
    "TransportAuthError",
    "TransportUpstreamError",
    "TransportTruncatedError",
    "classify_status",
]
