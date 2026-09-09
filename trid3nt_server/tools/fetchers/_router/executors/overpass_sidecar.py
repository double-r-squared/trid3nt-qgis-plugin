"""Vector FGB plus a tags SIDECAR: the one sanctioned side write.

The router is read-through-ONLY. This executor writes ONE declared sidecar object
keyed off the SAME cache key as the ``.fgb``, so the slim layer and its tag bag are
siblings. The write is best-effort: a sidecar fault never fails the fetch."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error
from .library_delegate import invoke
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.overpass_sidecar"
)

__all__ = ["execute", "sidecar_uri"]


def sidecar_uri(spec: SourceSpec, params: dict[str, Any], ext: str) -> str:
    """The ``s3://`` URI of the sidecar SIBLING of this call's ``.fgb`` cache object.
    Recomputes the EXACT key ``read_through`` derives, so the sidecar shares the
    ``.fgb``'s key and only the extension differs."""
    from ....cache import CACHE_BUCKET, cache_path, compute_cache_key

    source_class = spec.source_class
    ttl = spec.cache.ttl_class
    source_id = source_class or spec.name
    key = compute_cache_key(source_id, params, ttl)
    path = cache_path(source_class, ttl, key, ext)
    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    return f"s3://{bucket}/{path}"


def _write_sidecar(spec: SourceSpec, params: dict[str, Any], ext: str, payload: dict[str, Any]) -> None:
    """Best-effort put_object of the declared sidecar (never fails the fetch)."""
    try:

        uri = sidecar_uri(spec, params, ext)
        rest = uri[len("s3://"):]
        bucket, _, obj_key = rest.partition("/")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        s3 = _get_s3_client()
        s3.put_object(Bucket=bucket, Key=obj_key, Body=body, ContentType="application/json")
        logger.info(
            "router.overpass_sidecar: wrote sidecar (side write, library-owned) "
            "%s (%d entries)", uri, len(payload),
        )
    except Exception as exc:  # noqa: BLE001 -- sidecar is best-effort; enrich falls back live
        logger.warning(
            "router.overpass_sidecar: sidecar write failed (%s); enrich will fall back "
            "to live Overpass-by-id", exc,
        )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """(features, tags) -> serialize the FGB + write the tags sidecar beside it."""
    sw = (spec.ingest or {}).get("sidecar_write") or {}
    features, tags_by_fid = invoke(spec, params)

    if not features:
        # A bbox with no mapped building footprints is a typed empty: there is no
        # second source to fall back to.
        raise router_empty_error(
            spec.error_code_prefix,
            f"No OpenStreetMap building footprints intersect bbox={params.get('bbox')!r} "
            f"(the area may be unmapped in OSM).",
            spec.empty_error_suffix,
        )

    fgb_bytes = features_to_fgb_bytes(features, spec, params)

    # Constrained side write: ONE declared sidecar sibling of the .fgb.
    if tags_by_fid and sw.get("ext"):
        _write_sidecar(spec, params, str(sw["ext"]), tags_by_fid)

    return fgb_bytes
