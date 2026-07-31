"""Router remote-FILE transport: one httpx-based module owning every socket and

every error for remote COG reads (ingest-transport decision, ADR-0044). Pooled
client, 1 MiB coalescing + parallel range opener, HEAD pre-flight typed errors,
one retry authority, and the GDAL C-frame exception bridge live here; executors
call ``open_windowed_cog`` and map the typed ``Transport*`` errors into the A.6
router frame. ``/vsicurl/`` remains only as the documented fallback in the decision.
"""

from __future__ import annotations

from .client import get_bytes, get_client, head, range_get
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

__all__ = [
    "get_client",
    "head",
    "range_get",
    "get_bytes",
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
