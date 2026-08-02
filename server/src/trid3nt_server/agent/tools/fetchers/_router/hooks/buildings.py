"""OSM buildings hooks (trigger wave, ADR 0084): Overpass polygon fetch + tags leg.

fetch_buildings folds onto the Overpass mode (``build_request`` POST-per-mirror QL +
a polygon decode), but its irreducible extra is a click-to-enrich TAGS SIDECAR: the
full OSM tag bag per footprint is written as a ``.tags.json`` object keyed off the SAME
cache key as the ``.fgb`` and read back cross-module by ``/api/building-detail``. The
inline FGB stays SLIM (osm_id / osm_type / fid) so the frontend GeoJSON is tiny.

Two pure hooks:
- ``build_request`` (pure): the ``building`` way+relation Overpass QL, one POST per mirror.
- ``parse`` (pure): Overpass JSON -> ``(geojson_features, tags_by_fid)`` -- ways ->
  Polygon, multipolygon relations -> (Multi)Polygon, EVERY footprint whose geometry
  INTERSECTS the bbox kept WHOLE (never clipped), slim inline props + the full tag bag
  captured separately for the sidecar.

The transport + the sidecar WRITE stay router-owned (the ``overpass_sidecar`` executor);
these hooks only build the request and decode the bytes.
"""

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_upstream_error

__all__ = ["build_request", "parse"]

#: Overpass-side internal-query timeout (the ``[timeout:N]`` QL directive).
_OVERPASS_QL_TIMEOUT = 90


def _build_ql(bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL selecting ``building`` ways AND relations in ``bbox``.

    Overpass corners are ``(south, west, north, east)`` (lat first); ``out geom``
    returns full node geometry inline (plus each relation member way's geometry).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    return (
        f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];"
        f'(way["building"]({s},{w},{n},{e});'
        f'relation["building"]({s},{w},{n},{e}););'
        f"out geom;"
    )


@_hooks.register_hook("buildings.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Build the ``building`` Overpass QL and plan one POST per declared mirror.

    The dead msft/abfs GeoParquet leg stays flag-not-copy (Overpass is the reliable
    source per the standing decision); ``source`` is echoed only into the cache key /
    layer_id, and every request resolves to the Overpass path.
    """
    ql = _build_ql(tuple(float(v) for v in params["bbox"]))
    ua = spec.auth.user_agent
    return [
        _hooks.RequestPlan(
            url=ep.url or ep.url_template or "",
            method="POST",
            data={"data": ql},
            headers={"User-Agent": ua},
        )
        for ep in spec.endpoints.values()
    ]


def _ring_from_geom(geom: Any) -> list[tuple[float, float]]:
    """Extract a finite ``(lon, lat)`` ring from an Overpass ``geometry`` list."""
    ring: list[tuple[float, float]] = []
    if not isinstance(geom, list):
        return ring
    for pt in geom:
        if not isinstance(pt, dict):
            continue
        lat_v, lon_v = pt.get("lat"), pt.get("lon")
        if lat_v is None or lon_v is None:
            continue
        try:
            lat, lon = float(lat_v), float(lon_v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon):
            ring.append((lon, lat))
    return ring


def _way_to_polygon(way: dict[str, Any]) -> Any | None:
    from shapely.geometry import Polygon

    ring = _ring_from_geom(way.get("geometry"))
    if len(list(dict.fromkeys(ring))) < 3:
        return None
    try:
        poly = Polygon(ring)
    except Exception:  # noqa: BLE001 -- degenerate ring
        return None
    if poly.is_empty:
        return None
    if not poly.is_valid:
        poly = poly.buffer(0)
        if poly.is_empty or poly.geom_type not in ("Polygon", "MultiPolygon"):
            return None
    return poly


def _relation_to_multipolygon(rel: dict[str, Any]) -> Any | None:
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    members = rel.get("members") or []
    if not isinstance(members, list):
        return None
    outers: list[Any] = []
    inners: list[Any] = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        ring = _ring_from_geom(member.get("geometry"))
        if len(list(dict.fromkeys(ring))) < 3:
            continue
        try:
            poly = Polygon(ring)
        except Exception:  # noqa: BLE001
            continue
        if poly.is_empty:
            continue
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.is_empty:
                continue
        if member.get("role") == "inner":
            inners.append(poly)
        else:
            outers.append(poly)
    if not outers:
        return None
    outer_union = unary_union(outers)
    if inners:
        try:
            outer_union = outer_union.difference(unary_union(inners))
        except Exception:  # noqa: BLE001 -- keep solid footprint if hole-cut fails
            pass
    if outer_union.is_empty or outer_union.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return outer_union


def _building_fid(el_type: Any, osm_id: Any) -> str:
    """Stable composite id ``"<first-letter-of-osm_type><osm_id>"`` (w123456 / r222)."""
    return f"{str(el_type or '')[:1]}{osm_id}"


def parse(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Overpass JSON -> ``(geojson_features, tags_by_fid)`` (pure).

    Ways -> Polygon, multipolygon relations -> (Multi)Polygon. EVERY footprint whose
    geometry INTERSECTS the bbox is kept WHOLE (intersects, not clip -- a building
    straddling an AOI edge stays intact). Inline props are SLIM (osm_id / osm_type /
    fid); the full tag bag is captured in ``tags_by_fid`` for the sidecar. Non-areal /
    malformed / fully-outside elements are dropped.
    """
    import json

    from shapely import box
    from shapely.geometry import mapping

    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        return [], {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"Overpass buildings returned non-JSON: {exc}")
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        raise router_upstream_error(
            sc, f"Overpass buildings 'elements' is not a list: {type(elements).__name__}"
        )

    min_lon, min_lat, max_lon, max_lat = (float(v) for v in params["bbox"])
    bbox_geom = box(min_lon, min_lat, max_lon, max_lat)

    features: list[dict[str, Any]] = []
    tags_by_fid: dict[str, dict[str, Any]] = {}
    for el in elements:
        if not isinstance(el, dict):
            continue
        el_type = el.get("type")
        if el_type == "way":
            geom = _way_to_polygon(el)
        elif el_type == "relation":
            geom = _relation_to_multipolygon(el)
        else:
            geom = None
        if geom is None or geom.is_empty:
            continue
        # Keep whole footprints that INTERSECT the bbox (never clipped); drop
        # those entirely outside (symmetric on all four edges).
        try:
            if not geom.intersects(bbox_geom):
                continue
        except Exception:  # noqa: BLE001 -- degenerate geometry drops the footprint
            continue
        osm_id = el.get("id")
        fid = _building_fid(el_type, osm_id)
        features.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {"osm_id": osm_id, "osm_type": el_type, "fid": fid},
        })
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        if tags:
            tags_by_fid[fid] = dict(tags)
    return features, tags_by_fid


# Register the parse under a resolvable name (the sidecar executor reads it via
# ingest.sidecar_write.parse); its (features, tags) tuple return is NOT the plain
# parse_response contract, so it is dispatched by the executor, not the http_json slot.
_hooks.register_hook("buildings.parse")(parse)
