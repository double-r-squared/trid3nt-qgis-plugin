"""buildings: OSM footprints, slim inline, with the full tag bag beside them.

The inline FGB stays SLIM so the frontend payload is tiny, and the full tag bag per
footprint goes to the sidecar the executor writes next to it. EVERY footprint
intersecting the bbox is kept WHOLE: half a building is not a smaller building."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.hooks import register_hook
from ..._router.hooks.osm import osm_id_of, overpass_features

__all__ = ["features", "building_fid", "tags_from_sidecars",
           "tags_from_overpass"]

logger = logging.getLogger("trid3nt_server.tools.fetchers.fetch_buildings")

#: Columns the library adds that are not OSM tags, so the tag bag stays the tags.
_NON_TAG_COLUMNS: frozenset[str] = frozenset({"geometry", "nodes", "ways", "element"})


def building_fid(el_type: Any, osm_id: Any) -> str:
    """Stable composite id ``"<first-letter-of-osm_type><osm_id>"`` (w123456 / r222)."""
    return f"{str(el_type or '')[:1]}{osm_id}"


@register_hook("buildings.features")
def features(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(geojson_features, tags_by_fid)`` for every footprint in the bbox. The library
    assembles the multipolygon relations -- a courtyard block is one footprint with its
    hole -- and hands back the tags as columns, so both halves come off one read."""
    import pandas as pd
    from shapely.geometry import mapping

    gdf = overpass_features(spec, params, {"building": True}, timeout_s=timeout_s)
    out: list[dict[str, Any]] = []
    tags_by_fid: dict[str, dict[str, Any]] = {}
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        el_type = str(idx[0]) if isinstance(idx, tuple) else None
        fid = building_fid(el_type, osm_id_of(idx))
        out.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {"osm_id": osm_id_of(idx), "osm_type": el_type, "fid": fid},
        })
        tags = {
            str(k): str(v)
            for k, v in row.items()
            if k not in _NON_TAG_COLUMNS and not pd.isna(v)
        }
        if tags:
            tags_by_fid[fid] = tags
    return out, tags_by_fid


def tags_from_sidecars(fid: str) -> dict[str, Any] | None:
    """Scan the buildings tag sidecars for ``fid`` and return its tag bag, or
    ``None``. SYNC: the caller off-loads it. The request carries no bbox, so the
    bounded sidecar prefix is listed; a storage fault degrades to the live read."""
    try:

        from trid3nt_server.tools.cache import CACHE_BUCKET, cache_path
        # fetch_buildings is folded to the router: the sidecar identity
        # (source_class / ttl / .tags.json ext) now lives in the promoted spec, not a
        # coded twin. Read it from the spec, falling back to the load-bearing literals
        # so a cold spec registry never breaks the enrich read.
        from trid3nt_server.tools.fetchers._router.registration import get_spec
    except Exception:  # noqa: BLE001 -- import wiring fault -> live fallback
        logger.warning("building-detail: sidecar import wiring failed", exc_info=True)
        return None

    _spec = get_spec("fetch_buildings")
    if _spec is not None:
        source_class = _spec.source_class
        ttl_class = _spec.cache.ttl_class
        sidecar_ext = str(((_spec.ingest or {}).get("sidecar_write") or {}).get("ext", "tags.json"))
    else:
        source_class, ttl_class, sidecar_ext = "buildings", "static-30d", "tags.json"

    bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    # Derive the buildings/<...> prefix from cache_path with a placeholder key.
    sentinel = cache_path(source_class, ttl_class, "KEY", sidecar_ext)
    prefix = sentinel.rsplit("KEY", 1)[0]  # cache/static-30d/buildings/
    suffix = f".{sidecar_ext}"
    try:
        from trid3nt_server import storage

        s3 = storage.client()
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "")
                if not key.endswith(suffix):
                    continue
                try:
                    raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                    data = json.loads(raw)
                except Exception:  # noqa: BLE001 -- skip an unreadable sidecar
                    continue
                if isinstance(data, dict):
                    bag = data.get(fid)
                    if isinstance(bag, dict):
                        return bag
    except Exception:  # noqa: BLE001 -- S3 fault -> live fallback
        logger.warning("building-detail: sidecar scan degraded", exc_info=True)
        return None
    return None


def tags_from_overpass(osm_type: str, osm_id: str) -> dict[str, Any] | None:
    """Live by-id fallback for one OSM element's tag bag, or ``None`` when the
    element is unknown, untagged or unreachable, in which case the handler emits
    a typed 404. SYNC: the caller off-loads it."""
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return None
    ql = f"[out:json][timeout:25];{osm_type}({osm_id});out tags;"
    try:
        with httpx.Client(
            timeout=30.0, headers={"User-Agent": "trid3nt-building-detail/1.0"}
        ) as client:
            resp = client.post(
                "https://overpass-api.de/api/interpreter", data={"data": ql}
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001 -- Overpass unreachable / non-JSON
        logger.warning("building-detail: live Overpass-by-id failed", exc_info=True)
        return None
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return None
    for el in elements:
        if not isinstance(el, dict):
            continue
        tags = el.get("tags")
        if isinstance(tags, dict) and tags:
            return tags
    return None
