"""The one decision + transform seam for a vector FeatureCollection on the
inline-GeoJSON emit path. Below ``DENSE_VECTOR_THRESHOLD`` the FC comes back
unchanged; above it, simplification only DROPS vertices or DROPS whole features -
it never invents a coordinate, and the geometry families are unchanged, so nothing
about the styling moves with it."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("trid3nt.agent.vector_tiles")

__all__ = [
    "DENSE_VECTOR_THRESHOLD",
    "MAX_INLINE_FEATURES",
    "DensifyMeta",
    "densify_if_needed",
]


# Tunables (env-overridable so ops can move the line without a redeploy)

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("vector_tiles: %s=%r is not an int; using default %d", name, raw, default)
        return default
    return val if val > 0 else default


#: Above this feature count a vector layer is treated as "dense": it routes to
#: the tiled (preferred) or simplified+capped (fallback) path instead of raw
#: inline GeoJSON. Below it the current inline path is preserved unchanged.
#: Chosen so a few thousand building footprints trip it while typical
#: occurrence / boundary / alert layers (low hundreds) do not.
DENSE_VECTOR_THRESHOLD: int = _env_int("TRID3NT_DENSE_VECTOR_THRESHOLD", 1500)

#: Hard cap on the number of features ever shipped inline on the fallback path.
#: MapLibre draws at most this many polygons for one layer; the cap keeps the
#: largest-area features so the map stays representative. Always >= threshold so
#: a layer just over the threshold is simplified but NOT capped.
MAX_INLINE_FEATURES: int = max(
    _env_int("TRID3NT_MAX_INLINE_FEATURES", 4000), DENSE_VECTOR_THRESHOLD
)



@dataclass(frozen=True)
class DensifyMeta:
    """What ``densify_if_needed`` did to a dense FeatureCollection. Present only
    when the layer was at or above threshold; a below-threshold layer gets
    ``None``."""

    strategy: str  # "simplified" | "capped" | "simplified+capped" | "inline" | "tiled"
    original_feature_count: int
    emitted_feature_count: int
    simplified: bool
    capped: bool
    #: Set only when ``strategy == "tiled"``: the vector-tile LayerURI fields.
    tiles_uri: str | None = None

    def as_wire_tag(self) -> dict[str, Any]:
        """Additive dict merged onto an already-dumped wire layer dict."""
        return {
            "vector_density": {
                "strategy": self.strategy,
                "original_feature_count": self.original_feature_count,
                "emitted_feature_count": self.emitted_feature_count,
                "simplified": self.simplified,
                "capped": self.capped,
            }
        }



def _feature_count(fc: dict[str, Any]) -> int:
    feats = fc.get("features")
    return len(feats) if isinstance(feats, list) else 0


def _fc_bounds(geoms: list[Any]) -> tuple[float, float, float, float] | None:
    """Overall (minx, miny, maxx, maxy) of a list of shapely geometries."""
    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    for g in geoms:
        if g is None or g.is_empty:
            continue
        gminx, gminy, gmaxx, gmaxy = g.bounds
        minx = min(minx, gminx)
        miny = min(miny, gminy)
        maxx = max(maxx, gmaxx)
        maxy = max(maxy, gmaxy)
    if minx == float("inf"):
        return None
    return (minx, miny, maxx, maxy)


def _scaled_tolerance(bounds: tuple[float, float, float, float]) -> float:
    """Douglas-Peucker tolerance in CRS units (EPSG:4326 degrees), scaled to the
    layer extent and clamped so a tiny extent still simplifies and a huge one does
    not over-collapse."""
    minx, miny, maxx, maxy = bounds
    span = max(maxx - minx, maxy - miny)
    # 1/4000 of the larger span keeps building-footprint corners while collapsing
    # redundant vertices; the clamp holds the result between roughly 1 m and 50 m at
    # mid-latitudes (1 deg lat ~= 111 km).
    tol = span / 4000.0
    return min(max(tol, 1e-5), 5e-4)


#: Output coordinate precision (decimal degrees) for dense layers. 6 dp is about
#: 0.11 m at the equator, well below building-footprint fidelity, while
#: shapely.mapping otherwise emits a full float repr (~15 sig digits) - so rounding
#: is a geometry-safe wire-byte win even when Douglas-Peucker drops no vertices.
_COORD_PRECISION: int = _env_int("TRID3NT_DENSE_VECTOR_COORD_DP", 6)


def _count_coords(geom_mapping: Any) -> int:
    """Total coordinate pairs in a GeoJSON geometry mapping (recursive)."""
    if not isinstance(geom_mapping, dict):
        return 0
    coords = geom_mapping.get("coordinates")

    def _walk(node: Any) -> int:
        if not isinstance(node, (list, tuple)) or not node:
            return 0
        # A coordinate pair: [x, y(, z)] of numbers.
        if isinstance(node[0], (int, float)):
            return 1
        return sum(_walk(child) for child in node)

    return _walk(coords)


def _round_coords(node: Any, nd: int) -> Any:
    """Recursively round a GeoJSON coordinate array to ``nd`` decimals."""
    if isinstance(node, (int, float)):
        return round(float(node), nd)
    if isinstance(node, (list, tuple)):
        return [_round_coords(c, nd) for c in node]
    return node


def _round_geom(geom_mapping: Any, nd: int) -> Any:
    """Return the geometry mapping with coordinates rounded to ``nd`` decimals."""
    if not isinstance(geom_mapping, dict) or "coordinates" not in geom_mapping:
        return geom_mapping
    out = dict(geom_mapping)
    out["coordinates"] = _round_coords(geom_mapping["coordinates"], nd)
    return out


def _strategy_label(simplified: bool, capped: bool) -> str:
    """Honest strategy tag reflecting what actually happened to a dense FC."""
    if simplified and capped:
        return "simplified+capped"
    if simplified:
        return "simplified"
    if capped:
        return "capped"
    # Dense, but no vertices dropped and no features cut - only coordinate
    # precision was trimmed. Do NOT claim "simplified".
    return "inline"


def _simplify_and_cap(fc: dict[str, Any]) -> tuple[dict[str, Any], bool, bool, int]:
    """Returns ``(fc_out, simplified, capped, emitted_count)``; ``simplified`` is
    True only when Douglas-Peucker actually removed coordinates. Best-effort: any
    shapely failure returns the ORIGINAL fc with ``simplified=False``."""
    try:
        from shapely.geometry import mapping, shape  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "vector_tiles: shapely unavailable (%s) — emitting dense FC as-is", exc
        )
        feats = fc.get("features") or []
        return fc, False, False, len(feats)

    raw_features = fc.get("features") or []
    parsed: list[tuple[dict[str, Any], Any]] = []
    for f in raw_features:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry")
        if not geom:
            continue
        try:
            g = shape(geom)
        except Exception:  # noqa: BLE001 - skip an unparseable feature, keep going
            continue
        if g.is_empty:
            continue
        parsed.append((f, g))

    if not parsed:
        return fc, False, False, len(raw_features)

    bounds = _fc_bounds([g for _, g in parsed])
    tol = _scaled_tolerance(bounds) if bounds else 1e-4

    from shapely.geometry import mapping as _mapping  # local: scoped to this fn

    simplified_features: list[tuple[dict[str, Any], Any, float]] = []
    any_simplified = False
    for f, g in parsed:
        try:
            sg = g.simplify(tol, preserve_topology=True)
        except Exception:  # noqa: BLE001 - keep the original geometry on failure
            sg = g
        if sg.is_empty:
            sg = g  # never drop a feature to emptiness via simplification
        else:
            # Only flag "simplified" when vertices were actually removed: for a
            # simple footprint (4-8 vertices) Douglas-Peucker frequently drops
            # nothing, and claiming a reduction then would be a false tag.
            try:
                if _count_coords(_mapping(sg)) < _count_coords(_mapping(g)):
                    any_simplified = True
            except Exception:  # noqa: BLE001 - never let the count probe break the path
                pass
        try:
            area = abs(sg.bounds[2] - sg.bounds[0]) * abs(sg.bounds[3] - sg.bounds[1])
        except Exception:  # noqa: BLE001
            area = 0.0
        simplified_features.append((f, sg, area))

    capped = False
    if len(simplified_features) > MAX_INLINE_FEATURES:
        # Keep the largest-area features so the map stays representative.
        simplified_features.sort(key=lambda t: t[2], reverse=True)
        simplified_features = simplified_features[:MAX_INLINE_FEATURES]
        capped = True

    out_features: list[dict[str, Any]] = []
    for f, sg, _area in simplified_features:
        try:
            new_geom = _round_geom(mapping(sg), _COORD_PRECISION)
        except Exception:  # noqa: BLE001
            new_geom = f.get("geometry")
        out_features.append(
            {
                "type": "Feature",
                "properties": f.get("properties") or {},
                "geometry": new_geom,
            }
        )

    fc_out = {"type": "FeatureCollection", "features": out_features}
    return fc_out, any_simplified, capped, len(out_features)



def densify_if_needed(
    fc: dict[str, Any] | None,
    *,
    layer_id: str = "",
) -> tuple[dict[str, Any] | None, DensifyMeta | None]:
    """Returns ``(fc, None)`` below ``DENSE_VECTOR_THRESHOLD`` - byte-for-byte the
    input - and ``(simplified_capped_fc, DensifyMeta)`` at or above it. Always
    returns a renderable FeatureCollection; never raises."""
    if not isinstance(fc, dict):
        return fc, None
    count = _feature_count(fc)
    if count <= DENSE_VECTOR_THRESHOLD:
        return fc, None

    fc_out, simplified, capped, emitted = _simplify_and_cap(fc)
    strategy = _strategy_label(simplified, capped)
    meta = DensifyMeta(
        strategy=strategy,
        original_feature_count=count,
        emitted_feature_count=emitted,
        simplified=simplified,
        capped=capped,
    )
    logger.info(
        "vector_tiles: densified layer_id=%s strategy=%s original=%d "
        "emitted=%d simplified=%s capped=%s",
        layer_id,
        strategy,
        count,
        emitted,
        simplified,
        capped,
    )
    return fc_out, meta
