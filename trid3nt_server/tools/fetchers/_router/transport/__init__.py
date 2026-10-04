"""Router remote-FILE transport: the one retry authority and its typed errors.

A pooled httpx client for whole-object reads, the GDAL read policy every remote
raster opens under, staged-object resolution and whole-ZIP reads. An executor
maps the typed ``Transport*`` errors."""

from __future__ import annotations

from .client import get_bytes, get_client, get_once, post_bytes, retried
from .errors import (
    TransportAuthError,
    TransportError,
    TransportNotFound,
    TransportUpstreamError,
    classify_status,
)
from .opener import MAX_PARALLEL, READ_POLICY, open_windowed_cog
from .staged import StagedEndpointNotConfigured, is_staged_uri, staged_object_url
from .zip_object import get_zip

__all__ = [
    "get_client",
    "retried",
    "get_bytes",
    "get_once",
    "post_bytes",
    "get_zip",
    "is_staged_uri",
    "staged_object_url",
    "StagedEndpointNotConfigured",
    "open_windowed_cog",
    "READ_POLICY",
    "MAX_PARALLEL",
    "TransportError",
    "TransportNotFound",
    "TransportAuthError",
    "TransportUpstreamError",
    "classify_status",
]
