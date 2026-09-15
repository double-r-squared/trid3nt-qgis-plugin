"""Post-fetch shaping: keep the ONE feature nearest a point.

A source that returns every station in a box, asked for the one that watches a
place, answers with that one - the pick is the fetch's own option rather than a
choice each caller re-implements. Declared as ``ingest.nearest``; a request that
names no point is returned untouched, so the option costs a source nothing.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error

__all__ = ["apply", "km_between"]

logger = logging.getLogger("trid3nt_server.tools.fetchers._router.transforms.nearest")


def km_between(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    """How far apart two lon/lat points are, in kilometres.

    Ranking only has to ORDER candidates over a local box, so the flat-earth
    distance on the latitude's own scale is the whole of what it needs."""
    east = (float(lon_b) - float(lon_a)) * 111.32 * math.cos(math.radians(float(lat_a)))
    north = (float(lat_b) - float(lat_a)) * 110.57
    return math.hypot(east, north)


def _representative(geometry: Any) -> tuple[float, float] | None:
    """One (lon, lat) standing for a feature's geometry; ``None`` when it has no
    readable coordinate at all."""
    if not isinstance(geometry, dict):
        return None
    coords = geometry.get("coordinates")
    while isinstance(coords, (list, tuple)) and coords and \
            isinstance(coords[0], (list, tuple)):
        coords = coords[0]
    if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
        return None
    try:
        return (float(coords[0]), float(coords[1]))
    except (TypeError, ValueError):
        return None


def apply(features: list[dict[str, Any]], spec: SourceSpec,
          params: dict[str, Any] | None, cfg: Any) -> list[dict[str, Any]]:
    """The declared ``ingest.nearest`` pick over already-parsed features.

    ``cfg`` names the point parameter (``to``), optionally the property a
    candidate must carry a non-null value for (``require``) and the property the
    measured distance is written onto (``distance_property``, default
    ``distance_km``). No point asked for returns ``features`` unchanged."""
    if not isinstance(cfg, dict) or not cfg.get("to"):
        raise router_input_error(
            spec.error_code_prefix,
            "ingest.nearest must name the point parameter it reads, as "
            "`to: <param>`", spec.input_error_suffix)
    point = (params or {}).get(str(cfg["to"]))
    if point is None:
        return features
    try:
        lon, lat = float(point[0]), float(point[1])
    except (TypeError, ValueError, IndexError):
        raise router_input_error(
            spec.error_code_prefix,
            f"{cfg['to']} must be a [lon, lat] pair; got {point!r}",
            spec.input_error_suffix) from None

    require = cfg.get("require")
    ranked: list[tuple[float, dict[str, Any]]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties") or {}
        if require and props.get(str(require)) is None:
            continue
        here = _representative(feature.get("geometry"))
        if here is None:
            continue
        ranked.append((km_between(lon, lat, here[0], here[1]), feature))
    if not ranked:
        raise router_empty_error(
            spec.error_code_prefix,
            f"no feature this fetch returned carries a position"
            + (f" and a {require!r} value" if require else "")
            + f", so the one nearest ({lon:.5f}, {lat:.5f}) is not measurable. "
            "Drop the point to take everything the source returned.",
            spec.empty_error_suffix)
    distance_km, winner = min(ranked, key=lambda row: row[0])
    logger.info("nearest: %d candidate(s) -> one at %.2f km from (%.5f, %.5f)",
                len(ranked), distance_km, lon, lat)
    props = dict(winner.get("properties") or {})
    props[str(cfg.get("distance_property") or "distance_km")] = round(distance_km, 3)
    return [{"type": "Feature", "geometry": winner.get("geometry"),
             "properties": props}]
