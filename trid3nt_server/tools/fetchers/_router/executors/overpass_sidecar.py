"""overpass_sidecar executor (trigger wave): vector FGB + a tags SIDECAR.

The router is read-through-ONLY; fetch_buildings needs the ONE sanctioned side write:
a ``.tags.json`` object keyed off the SAME cache key as the ``.fgb`` (the full OSM tag
bag per footprint), read back cross-module by ``/api/building-detail`` so the inline FGB
stays slim. This executor is the minimal constrained write extension -- constrained like
the library_delegate:

- ONE declared sidecar object (``ingest.sidecar_write.ext``), a SIBLING of the ``.fgb``
  (recomputes the exact ``read_through`` key: same metadata + params + ttl vintage).
- BEST-EFFORT + telemetry-marked: a sidecar fault NEVER fails the fetch (the slim layer
  still renders; enrich degrades to a live Overpass-by-id query) -- the honesty floor is
  untouched (``read_through`` still owns the ``.fgb`` success/error).

The transport (endpoint_fallback mirror chain) + serialization stay shared; the source's
QL + polygon decode + tag capture are the ``hooks.build_request`` + the
``ingest.sidecar_write.parse`` pure hooks.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error
from ..hooks import resolve_hook
from .http_json import _fetch_endpoint_fallback
from .vector_fgb import features_to_fgb_bytes

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.overpass_sidecar"
)

__all__ = ["execute", "sidecar_uri"]


def sidecar_uri(spec: SourceSpec, params: dict[str, Any], ext: str) -> str:
    """The ``s3://`` URI of the sidecar SIBLING of this call's ``.fgb`` cache object.

    Recomputes the EXACT key ``read_through`` derives (``source_id = source_class or
    name``; ``compute_cache_key(source_id, params, ttl)``; ``cache_path(source_class,
    ttl, key, ext)``) so the sidecar shares the ``.fgb``'s ``<key>``, only the ext
    differs -- the twin's ``buildings_cache_uri`` contract, now spec-derived.
    """
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
        import boto3

        uri = sidecar_uri(spec, params, ext)
        rest = uri[len("s3://"):]
        bucket, _, obj_key = rest.partition("/")
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))
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
    """Overpass fetch -> (features, tags) -> serialize FGB + write the tags sidecar."""
    build = resolve_hook(spec.hooks.build_request)  # type: ignore[union-attr]
    bodies = _fetch_endpoint_fallback(spec, build(spec, params))

    sw = (spec.ingest or {}).get("sidecar_write") or {}
    parse = resolve_hook(sw["parse"])
    features, tags_by_fid = parse(spec, params, bodies)

    if not features:
        # The twin raised on an empty AOI (it triggered the dead msft fallback);
        # OSM-only, a bbox with no mapped building footprints is a typed empty.
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
