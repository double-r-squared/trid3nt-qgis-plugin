"""Shared drawn-geometry ROLE vocabulary + parser for the mesh authoring layer.

Every engine that lets the user draw spatial input (``request_spatial_input`` ->
a role-tagged GeoJSON ``FeatureCollection``) routes that drawing through ONE
canonical role vocabulary defined here, so a breakline, a breach point, a refine
region or an AOI clip means the same thing to every engine. This is the mesh
layer's DOMAIN stage: user-drawn geometry enters HERE, once, for every engine.

Canonical roles (``properties.role`` on each drawn ``Feature``):

  * ``breakline``      -- a LineString that CONSTRAINS mesh edges (a ridge, a
                          channel bank); a tin/quadtree mesher forces edges along
                          it.
  * ``breach``         -- a Point interior levee/dam-breach source. Rides the
                          drawn-POINT role; SFINCS/GeoClaw accept it as their
                          ``breach_point`` (drawn value PREFERRED over a plain
                          tuple arg when a breach point is drawn).
  * ``refine_region``  -- a Polygon over which the mesh is refined to a finer
                          target size (``properties.target_size_m``); consumed by
                          the MODFLOW DISV/gridgen generator and (worker-side)
                          TELEMAC gmsh sizing fields.
  * ``aoi_clip``       -- a Polygon that CLIPS the run domain (mask cells outside
                          it). Legacy wire role ``aoi`` is an accepted alias.
  * ``boundary``       -- a LineString open-boundary segment
                          (``properties.boundary_type`` in {inflow, outflow}).
  * ``point``          -- a generic Point (ELMFIRE ignition, a probe).
  * ``line``           -- a NEUTRAL elevation/section LineString
                          (compute_terrain_profile / compute_cross_section); no
                          mesh semantics.

The module is a PURE structural translator -- no I/O, no asyncio, no geometry
library -- so it is trivially unit-testable and importable in any context (it
must NOT trigger the heavy ``workflows`` package). Honesty floor: a malformed
``FeatureCollection`` NEVER degrades to a silent success -- every parser raises
:class:`SpatialRoleError` (typed ``error_code``) so the caller narrates an
honest error instead of fabricating geometry the user did not draw.
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

    Carries an open-set ``error_code`` so the caller renders a typed error
    result the LLM narrates honestly (never a silent success).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Backwards-compatible alias -- the original name from ``gates.spatial_input``.
SpatialInputParseError = SpatialRoleError


# Kept local so this module has no contracts dep at import time and stays a
# pure-structure translator.
_VALID_BOUNDARY_TYPES = frozenset({"inflow", "outflow"})

#: The canonical role vocabulary every engine shares.
CANONICAL_ROLES = frozenset(
    {
        "breakline",
        "breach",
        "refine_region",
        "aoi_clip",
        "boundary",
        "point",
        "line",
    }
)

#: Legacy wire roles still accepted from older clients, aliased to canonical.
#: ``aoi`` was the pre-generalization AOI role -> canonical ``aoi_clip``.
ROLE_ALIASES = {"aoi": "aoi_clip"}

#: Every role string this parser accepts on the wire (canonical + legacy alias).
_ACCEPTED_ROLES = frozenset(CANONICAL_ROLES | set(ROLE_ALIASES))


@dataclass
class DrawnRoles:
    """The role-split result of a drawn ``FeatureCollection`` (all engines).

    Every field defaults empty so an engine reads only the roles it consumes.

    Fields:
        breaklines: ``[[[lon,lat],...], ...]`` -- each breakline's vertices.
        breach_points: ``[[lon,lat], ...]`` -- interior breach sources
            (SFINCS/GeoClaw ``breach_point``).
        refine_regions: ``[{"polygon": Feature, "target_size_m": float|None,
            "bbox": (..4..)}]`` -- per-region mesh sizing.
        aoi_clip_features: raw clip polygons (``aoi_clip`` + legacy ``aoi``).
        aoi_bbox: union extent of the clip polygons, or ``None``.
        boundary_lines: ``[{"coords": [[lon,lat],...], "boundary_type":
            "inflow"|"outflow"|None}]``.
        points: ``[[lon,lat], ...]`` from the generic ``point`` role.
        line_coords: the FIRST neutral ``line`` feature's vertices, or ``None``.
        n_lines: count of neutral ``line`` features.
    """

    breaklines: list[list[list[float]]] = field(default_factory=list)
    breach_points: list[list[float]] = field(default_factory=list)
    refine_regions: list[dict[str, Any]] = field(default_factory=list)
    aoi_clip_features: list[dict[str, Any]] = field(default_factory=list)
    aoi_bbox: tuple[float, float, float, float] | None = None
    boundary_lines: list[dict[str, Any]] = field(default_factory=list)
    points: list[list[float]] = field(default_factory=list)
    line_coords: list[list[float]] | None = None
    n_lines: int = 0


def split_features_by_role(
    fc: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket a drawn ``FeatureCollection``'s features by ``properties.role``.

    Buckets are keyed by the RAW role string as received (legacy ``aoi`` stays
    ``aoi``); the returned dict pre-seeds every accepted role (canonical + legacy
    alias) with an empty list so a consumer can index any role safely. Raises
    :class:`SpatialRoleError` if the top-level shape is not a
    ``FeatureCollection`` with a ``features`` list, or if any feature carries an
    unknown / missing ``role`` (honesty floor -- we never silently drop a feature
    the user drew).
    """
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

    Walks the coordinate tree (Point / LineString / Polygon / multi-*) and
    returns ``(min_lon, min_lat, max_lon, max_lat)``, or ``None`` if no valid
    ``[lon, lat]`` position is found.
    """
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

    Returns ``[[[lon,lat],...], ...]`` (one vertex list per feature). Raises
    :class:`SpatialRoleError` (role-labelled ``error_code``) on a malformed line.
    """
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


def _refine_regions(feats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse ``refine_region`` polygons into ``{polygon, target_size_m, bbox}``.

    ``target_size_m`` is read from ``properties.target_size_m`` (or the alias
    ``mesh_size_m``) when a positive number, else ``None`` (the consumer applies
    its default refinement). Raises :class:`SpatialRoleError` on a polygon with
    no valid coordinates or a non-positive ``target_size_m``.
    """
    out: list[dict[str, Any]] = []
    for idx, feat in enumerate(feats):
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") not in (
            "Polygon",
            "MultiPolygon",
        ):
            raise SpatialRoleError(
                "SPATIAL_INPUT_REFINE_NOT_POLYGON",
                f"refine_region[{idx}] geometry must be a Polygon/MultiPolygon "
                f"(got {geom.get('type') if isinstance(geom, dict) else geom!r})",
            )
        bbox = geometry_bbox(geom)
        if bbox is None:
            raise SpatialRoleError(
                "SPATIAL_INPUT_REFINE_BAD_GEOMETRY",
                f"refine_region[{idx}].geometry has no valid coordinates",
            )
        props = feat.get("properties") or {}
        raw_size = props.get("target_size_m", props.get("mesh_size_m"))
        target_size_m: float | None = None
        if raw_size is not None:
            if not isinstance(raw_size, (int, float)) or not raw_size > 0:
                raise SpatialRoleError(
                    "SPATIAL_INPUT_REFINE_BAD_SIZE",
                    f"refine_region[{idx}].properties.target_size_m must be a "
                    f"positive number, got {raw_size!r}",
                )
            target_size_m = float(raw_size)
        out.append(
            {"polygon": feat, "target_size_m": target_size_m, "bbox": bbox}
        )
    return out


def _boundary_lines(feats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse ``boundary`` LineStrings into ``{coords, boundary_type}``.

    ``boundary_type`` is read from ``properties.boundary_type`` (in {inflow,
    outflow}) or ``None`` when unset. Raises :class:`SpatialRoleError` on a
    malformed line or an unknown ``boundary_type``.
    """
    coords_list = _linestring_coords(feats, role_label="boundary")
    out: list[dict[str, Any]] = []
    for idx, (feat, coords) in enumerate(zip(feats, coords_list)):
        props = feat.get("properties") or {}
        btype = props.get("boundary_type")
        if btype is not None and btype not in _VALID_BOUNDARY_TYPES:
            raise SpatialRoleError(
                "SPATIAL_INPUT_BAD_BOUNDARY_TYPE",
                f"boundary[{idx}].properties.boundary_type must be one of "
                f"{sorted(_VALID_BOUNDARY_TYPES)}, got {btype!r}",
            )
        out.append({"coords": coords, "boundary_type": btype})
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

    The single entry point every engine's DOMAIN stage calls: splits by role,
    then translates each bucket into its typed shape. Legacy wire role ``aoi``
    is merged into ``aoi_clip``. Raises :class:`SpatialRoleError` (typed
    ``error_code``) on any structurally invalid input so the caller surfaces an
    honest typed error instead of a silently-wrong success.
    """
    buckets = split_features_by_role(fc)

    # aoi_clip absorbs the legacy ``aoi`` bucket (ROLE_ALIASES).
    aoi_feats = buckets["aoi_clip"] + buckets["aoi"]
    line_lists = _linestring_coords(buckets["line"], role_label="line")

    return DrawnRoles(
        breaklines=_linestring_coords(buckets["breakline"], role_label="breakline"),
        breach_points=_point_positions(buckets["breach"], role_label="breach"),
        refine_regions=_refine_regions(buckets["refine_region"]),
        aoi_clip_features=aoi_feats,
        aoi_bbox=_aoi_bbox(aoi_feats),
        boundary_lines=_boundary_lines(buckets["boundary"]),
        points=_point_positions(buckets["point"], role_label="point"),
        line_coords=line_lists[0] if line_lists else None,
        n_lines=len(line_lists),
    )
