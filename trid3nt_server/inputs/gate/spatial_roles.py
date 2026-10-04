"""Shared drawn-geometry ROLE vocabulary and parser for the canvas cards.

A pure structural translator: no I/O, no asyncio, no geometry library. A malformed
``FeatureCollection`` raises rather than degrading to a silent success.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SpatialRoleError",
    "SpatialInputParseError",
    "DrawnRoles",
    "CANONICAL_ROLES",
    "ROLE_ALIASES",
    "split_features_by_role",
    "parse_drawn_roles",
    "geometry_bbox",
]


class SpatialRoleError(ValueError):
    """A drawn ``FeatureCollection`` could not be parsed into role inputs.

    The open-set ``error_code`` is what the caller renders as a typed result."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: The same error type under its second accepted name.
SpatialInputParseError = SpatialRoleError


#: The canonical role vocabulary, read off ``properties.role`` on each drawn
#: ``Feature``: ``aoi_clip`` a Polygon that clips the run domain, ``point`` a
#: generic Point, ``line`` a neutral LineString with no mesh semantics.
CANONICAL_ROLES = frozenset({"aoi_clip", "point", "line"})

#: Wire roles accepted as aliases for a canonical role.
ROLE_ALIASES = {"aoi": "aoi_clip"}

#: Every role string this parser accepts on the wire (canonical + legacy alias).
_ACCEPTED_ROLES = frozenset(CANONICAL_ROLES | set(ROLE_ALIASES))


@dataclass
class DrawnRoles:
    """The role-split result of a drawn ``FeatureCollection``, for every engine.

    Every field defaults empty, so an engine reads only the roles it consumes."""

    #: The raw clip polygons, both role spellings merged.
    aoi_clip_features: list[dict[str, Any]] = field(default_factory=list)
    #: Union extent of the clip polygons, or ``None``.
    aoi_bbox: tuple[float, float, float, float] | None = None
    points: list[list[float]] = field(default_factory=list)
    #: The FIRST neutral ``line`` feature's vertices, or ``None``.
    line_coords: list[list[float]] | None = None
    #: How many neutral ``line`` features there were, the first one included.
    n_lines: int = 0


def split_features_by_role(
    fc: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket a drawn ``FeatureCollection``'s features by the RAW role string.

    Every accepted role is pre-seeded empty so a consumer can index any of them;
    a bad shape or an unknown role RAISES, never drops a drawn feature."""
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        raise SpatialRoleError(
            "SPATIAL_INPUT_NOT_FEATURECOLLECTION",
            "drawn geometry must be a GeoJSON FeatureCollection, got "
            f"type={(fc.get('type') if isinstance(fc, dict) else type(fc).__name__)!r}",
        )
    feats = fc.get("features")
    if not isinstance(feats, list):
        raise SpatialRoleError(
            "SPATIAL_INPUT_NO_FEATURES",
            "FeatureCollection.features must be a list",
        )
    buckets: dict[str, list[dict[str, Any]]] = {r: [] for r in _ACCEPTED_ROLES}
    for idx, feat in enumerate(feats):
        if not isinstance(feat, dict) or feat.get("type") != "Feature":
            raise SpatialRoleError(
                "SPATIAL_INPUT_BAD_FEATURE",
                f"features[{idx}] must be a GeoJSON Feature",
            )
        props = feat.get("properties") or {}
        role = props.get("role")
        if role not in _ACCEPTED_ROLES:
            raise SpatialRoleError(
                "SPATIAL_INPUT_BAD_ROLE",
                f"features[{idx}].properties.role must be one of "
                f"{sorted(CANONICAL_ROLES)}, got {role!r}",
            )
        buckets[role].append(feat)
    return buckets


def geometry_bbox(
    geom: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    """Compute a lon/lat bbox over any GeoJSON geometry's coordinate positions.

    ``None`` when the coordinate tree holds no valid ``[lon, lat]`` position."""
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    found = False

    def _walk(node: Any) -> None:
        nonlocal min_lon, min_lat, max_lon, max_lat, found
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            lon, lat = float(node[0]), float(node[1])
            min_lon = min(min_lon, lon)
            min_lat = min(min_lat, lat)
            max_lon = max(max_lon, lon)
            max_lat = max(max_lat, lat)
            found = True
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk(geom.get("coordinates"))
    if not found:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def _linestring_coords(
    feats: list[dict[str, Any]], *, role_label: str
) -> list[list[list[float]]]:
    """Validate + extract vertices from every LineString feature in ``feats``.

    One vertex list per feature; a malformed line raises, role-labelled."""
    out: list[list[list[float]]] = []
    for idx, feat in enumerate(feats):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            raise SpatialRoleError(
                f"SPATIAL_INPUT_{role_label.upper()}_NOT_LINESTRING",
                f"{role_label}[{idx}] geometry must be a LineString (got "
                f"{geom.get('type')!r})",
            )
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            raise SpatialRoleError(
                f"SPATIAL_INPUT_{role_label.upper()}_TOO_SHORT",
                f"{role_label}[{idx}].geometry.coordinates must be a LineString "
                f"with >= 2 positions",
            )
        verts: list[list[float]] = []
        for pidx, pt in enumerate(coords):
            if (
                not isinstance(pt, (list, tuple))
                or len(pt) < 2
                or not all(isinstance(v, (int, float)) for v in pt[:2])
            ):
                raise SpatialRoleError(
                    f"SPATIAL_INPUT_{role_label.upper()}_BAD_COORDS",
                    f"{role_label}[{idx}].geometry.coordinates[{pidx}] must be "
                    f"[lon, lat]",
                )
            verts.append([float(pt[0]), float(pt[1])])
        out.append(verts)
    return out


def _point_positions(
    feats: list[dict[str, Any]], *, role_label: str
) -> list[list[float]]:
    """Extract ``[lon, lat]`` from each Point feature (role-labelled errors)."""
    out: list[list[float]] = []
    for idx, feat in enumerate(feats):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            raise SpatialRoleError(
                f"SPATIAL_INPUT_{role_label.upper()}_NOT_POINT",
                f"{role_label}[{idx}] geometry must be a Point (got "
                f"{geom.get('type')!r})",
            )
        coords = geom.get("coordinates")
        if (
            not isinstance(coords, (list, tuple))
            or len(coords) < 2
            or not all(isinstance(v, (int, float)) for v in coords[:2])
        ):
            raise SpatialRoleError(
                f"SPATIAL_INPUT_{role_label.upper()}_BAD_COORDS",
                f"{role_label}[{idx}].geometry.coordinates must be [lon, lat]",
            )
        out.append([float(coords[0]), float(coords[1])])
    return out


def _aoi_bbox(
    aoi_feats: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Union the bboxes of every clip polygon into one extent, or ``None``."""
    if not aoi_feats:
        return None
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    found = False
    for idx, feat in enumerate(aoi_feats):
        geom = feat.get("geometry")
        if not isinstance(geom, dict):
            raise SpatialRoleError(
                "SPATIAL_INPUT_AOI_BAD_GEOMETRY",
                f"aoi[{idx}] has no GeoJSON geometry",
            )
        b = geometry_bbox(geom)
        if b is None:
            raise SpatialRoleError(
                "SPATIAL_INPUT_AOI_BAD_GEOMETRY",
                f"aoi[{idx}].geometry has no valid coordinates",
            )
        min_lon = min(min_lon, b[0])
        min_lat = min(min_lat, b[1])
        max_lon = max(max_lon, b[2])
        max_lat = max(max_lat, b[3])
        found = True
    if not found:
        return None
    return (min_lon, min_lat, max_lon, max_lat)


def parse_drawn_roles(fc: dict[str, Any]) -> DrawnRoles:
    """Parse a drawn ``FeatureCollection`` into the canonical :class:`DrawnRoles`.

    The single entry point every engine's DOMAIN stage calls; structurally
    invalid input raises, never a silently-wrong success."""
    buckets = split_features_by_role(fc)

    # ``aoi_clip`` absorbs the aliased ``aoi`` bucket.
    aoi_feats = buckets["aoi_clip"] + buckets["aoi"]
    line_lists = _linestring_coords(buckets["line"], role_label="line")

    return DrawnRoles(
        aoi_clip_features=aoi_feats,
        aoi_bbox=_aoi_bbox(aoi_feats),
        points=_point_positions(buckets["point"], role_label="point"),
        line_coords=line_lists[0] if line_lists else None,
        n_lines=len(line_lists),
    )
