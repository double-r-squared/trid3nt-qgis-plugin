"""Parse a drawn GeoJSON ``FeatureCollection`` into engine-shaped inputs
(FR-WC-16 urban vector-draw -> FR-AS-10 ``request_spatial_input``).

This is the AGENT-side consumer of the FR-WC-16 drawn output. The web client
opens a terra-draw surface and sends back a ``spatial-input-response`` whose
``features`` is a role-tagged ``FeatureCollection`` (see
``grace2_contracts.ws.SpatialInputResponsePayload`` /
``_validate_spatial_input_feature_collection``). Each ``Feature.properties``
carries a ``role`` in {"aoi", "barrier", "point"}.

The job of this module is purely structural translation (no I/O, no asyncio, no
geometry library) so it is trivially unit-testable: split the drawn FC by role
and emit the EXACT shapes the existing urban PySWMM engine already accepts, so
the drawn output round-trips with ZERO engine re-architecture:

  - ``role == "barrier"`` LineStrings  ->  a clean ``FeatureCollection`` that
    validates field-for-field against ``swmm_contracts.SWMMRunArgs.barriers``
    (every feature is a ``LineString`` tagged ``barrier_type`` in
    {"wall", "flap_gate"}; ``protected_side`` / ``flap_direction`` ride through
    untouched so the engine's ``_snap_barriers_to_edges`` /
    ``_resolve_protected`` seam reads them). A ``wall`` becomes an omitted
    overland conduit (a hard dam); a ``flap_gate`` becomes a one-way SWMM
    orifice oriented protected -> wet.
  - ``role == "aoi"`` polygons/rectangles  ->  a derived
    ``(min_lon, min_lat, max_lon, max_lat)`` bbox (the AOI the SWMM run covers)
    plus the raw polygon features (for clip-to-polygon downstream).
  - ``role == "point"`` features  ->  a list of ``[lon, lat]`` positions.

Honesty floor (data-source / best-effort norm): a malformed FeatureCollection
NEVER degrades to a silent success. ``parse_spatial_input_features`` raises
``SpatialInputParseError`` (carrying a typed ``error_code``) on structurally
invalid input; the caller turns that into a ``{status: "error", error_code,
error_message}`` result the LLM narrates honestly — it never fabricates
barriers or an AOI that the user did not draw.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SpatialInputParseError",
    "ParsedSpatialInput",
    "parse_spatial_input_features",
    "split_features_by_role",
    "barriers_feature_collection",
]

# The barrier tags the urban PySWMM engine understands (mirrors
# swmm_contracts.BarrierType). Kept local so this module has no contracts dep at
# import time and stays a pure-structure translator.
_VALID_BARRIER_TYPES = frozenset({"wall", "flap_gate"})
# "line" is a NEUTRAL elevation/section LineString (compute_terrain_profile /
# compute_cross_section): a drawn line with no barrier semantics -- never tagged
# wall/flap_gate. ADDITIVE -- the barrier role's parsing is untouched.
_VALID_ROLES = frozenset({"aoi", "barrier", "point", "line"})
_VALID_FLAP_DIRECTIONS = frozenset({"in", "out"})
_VALID_PROTECTED_SIDES = frozenset({"left", "right"})


class SpatialInputParseError(ValueError):
    """A drawn ``FeatureCollection`` could not be parsed into engine inputs.

    Carries an open-set ``error_code`` so the caller renders a typed error
    result the LLM narrates honestly (never a silent success).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class ParsedSpatialInput:
    """The role-split result of a drawn ``FeatureCollection``.

    Fields:
        barriers: a clean GeoJSON ``FeatureCollection`` of the ``role=="barrier"``
            LineStrings, shaped EXACTLY as ``SWMMRunArgs.barriers`` accepts
            (``barrier_type`` on each feature; the ``role`` property dropped).
            ``None`` when no barriers were drawn (so a plain run is passed
            ``barriers=None``, never an empty-but-present FC).
        aoi_bbox: the AOI extent ``(min_lon, min_lat, max_lon, max_lat)`` derived
            from the union of the ``role=="aoi"`` features, or ``None`` when no
            AOI was drawn.
        aoi_features: the raw ``role=="aoi"`` features (for clip-to-polygon).
        points: ``[[lon, lat], ...]`` from the ``role=="point"`` features.
        line_coords: the FIRST ``role=="line"`` feature's vertices
            ``[[lon, lat], ...]`` (a NEUTRAL elevation/section line, e.g. for
            ``compute_terrain_profile``), or ``None`` when no neutral line was
            drawn. Untagged -- never a barrier.
        n_lines: count of ``role=="line"`` features.
        n_walls: count of ``barrier_type=="wall"`` features.
        n_flap_gates: count of ``barrier_type=="flap_gate"`` features.
    """

    barriers: dict[str, Any] | None = None
    aoi_bbox: tuple[float, float, float, float] | None = None
    aoi_features: list[dict[str, Any]] = field(default_factory=list)
    points: list[list[float]] = field(default_factory=list)
    line_coords: list[list[float]] | None = None
    n_lines: int = 0
    n_walls: int = 0
    n_flap_gates: int = 0


def split_features_by_role(
    fc: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Bucket a drawn ``FeatureCollection``'s features by ``properties.role``.

    Raises ``SpatialInputParseError`` if the top-level shape is not a
    ``FeatureCollection`` with a ``features`` list, or if any feature carries an
    unknown / missing ``role`` (honesty floor — we never silently drop a feature
    the user drew).
    """
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        raise SpatialInputParseError(
            "SPATIAL_INPUT_NOT_FEATURECOLLECTION",
            "drawn geometry must be a GeoJSON FeatureCollection, got "
            f"type={(fc.get('type') if isinstance(fc, dict) else type(fc).__name__)!r}",
        )
    feats = fc.get("features")
    if not isinstance(feats, list):
        raise SpatialInputParseError(
            "SPATIAL_INPUT_NO_FEATURES",
            "FeatureCollection.features must be a list",
        )
    buckets: dict[str, list[dict[str, Any]]] = {r: [] for r in _VALID_ROLES}
    for idx, feat in enumerate(feats):
        if not isinstance(feat, dict) or feat.get("type") != "Feature":
            raise SpatialInputParseError(
                "SPATIAL_INPUT_BAD_FEATURE",
                f"features[{idx}] must be a GeoJSON Feature",
            )
        props = feat.get("properties") or {}
        role = props.get("role")
        if role not in _VALID_ROLES:
            raise SpatialInputParseError(
                "SPATIAL_INPUT_BAD_ROLE",
                f"features[{idx}].properties.role must be one of "
                f"{sorted(_VALID_ROLES)}, got {role!r}",
            )
        buckets[role].append(feat)
    return buckets


def barriers_feature_collection(
    barrier_feats: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, int, int]:
    """Build the engine-ready ``barriers`` FeatureCollection from drawn barriers.

    Validates every barrier feature against the urban engine's contract (a
    ``LineString`` with >= 2 positions tagged ``barrier_type`` in {wall,
    flap_gate}; optional ``flap_direction`` / ``protected_side``), then emits a
    clean ``FeatureCollection`` carrying ONLY the geometry + barrier properties
    the engine reads (the ``role`` property is dropped — the engine FC has no
    ``role`` field). Returns ``(fc_or_None, n_walls, n_flap_gates)``;
    ``fc_or_None`` is ``None`` when the list is empty so a plain run gets
    ``barriers=None`` rather than an empty-features FC.

    Raises ``SpatialInputParseError`` on any malformed barrier (honesty floor).
    """
    if not barrier_feats:
        return None, 0, 0
    clean: list[dict[str, Any]] = []
    n_walls = 0
    n_flap_gates = 0
    for idx, feat in enumerate(barrier_feats):
        geom = feat.get("geometry")
        if not isinstance(geom, dict) or geom.get("type") != "LineString":
            raise SpatialInputParseError(
                "SPATIAL_INPUT_BARRIER_NOT_LINESTRING",
                f"barrier[{idx}] geometry must be a LineString (got "
                f"{geom.get('type') if isinstance(geom, dict) else geom!r})",
            )
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            raise SpatialInputParseError(
                "SPATIAL_INPUT_BARRIER_TOO_SHORT",
                f"barrier[{idx}].geometry.coordinates must be a LineString "
                f"with >= 2 positions",
            )
        props = feat.get("properties") or {}
        btype = props.get("barrier_type")
        if btype not in _VALID_BARRIER_TYPES:
            raise SpatialInputParseError(
                "SPATIAL_INPUT_BAD_BARRIER_TYPE",
                f"barrier[{idx}].properties.barrier_type must be one of "
                f"{sorted(_VALID_BARRIER_TYPES)}, got {btype!r}",
            )
        # Carry ONLY the engine-relevant barrier properties through. role is
        # dropped (engine FC has no role); flap_direction / protected_side ride
        # through so the engine seam can read them.
        out_props: dict[str, Any] = {"barrier_type": btype}
        flap_dir = props.get("flap_direction")
        if flap_dir is not None:
            if (
                flap_dir not in _VALID_FLAP_DIRECTIONS
                and not isinstance(flap_dir, (int, float))
            ):
                raise SpatialInputParseError(
                    "SPATIAL_INPUT_BAD_FLAP_DIRECTION",
                    f"barrier[{idx}].properties.flap_direction must be one of "
                    f"{sorted(_VALID_FLAP_DIRECTIONS)} or a numeric bearing, "
                    f"got {flap_dir!r}",
                )
            out_props["flap_direction"] = flap_dir
        protected = props.get("protected_side")
        if protected is not None:
            if protected not in _VALID_PROTECTED_SIDES:
                raise SpatialInputParseError(
                    "SPATIAL_INPUT_BAD_PROTECTED_SIDE",
                    f"barrier[{idx}].properties.protected_side must be one of "
                    f"{sorted(_VALID_PROTECTED_SIDES)}, got {protected!r}",
                )
            out_props["protected_side"] = protected
        clean.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[float(p[0]), float(p[1])] for p in coords],
                },
                "properties": out_props,
            }
        )
        if btype == "wall":
            n_walls += 1
        else:
            n_flap_gates += 1
    fc = {"type": "FeatureCollection", "features": clean}
    return fc, n_walls, n_flap_gates


def _bbox_of_geometry(geom: dict[str, Any]) -> tuple[float, float, float, float] | None:
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


def _aoi_bbox(
    aoi_feats: list[dict[str, Any]],
) -> tuple[float, float, float, float] | None:
    """Union the bboxes of every AOI feature into one extent, or ``None``."""
    if not aoi_feats:
        return None
    min_lon = min_lat = float("inf")
    max_lon = max_lat = float("-inf")
    found = False
    for idx, feat in enumerate(aoi_feats):
        geom = feat.get("geometry")
        if not isinstance(geom, dict):
            raise SpatialInputParseError(
                "SPATIAL_INPUT_AOI_BAD_GEOMETRY",
                f"aoi[{idx}] has no GeoJSON geometry",
            )
        b = _bbox_of_geometry(geom)
        if b is None:
            raise SpatialInputParseError(
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


def _line_coords(line_feats: list[dict[str, Any]]) -> list[list[float]] | None:
    """Extract the FIRST ``role=="line"`` feature's vertices ``[[lon, lat], ...]``.

    A NEUTRAL elevation/section line (compute_terrain_profile / cross-section):
    a plain LineString with >= 2 positions, no barrier semantics. Returns the
    first such line's coordinates, or ``None`` when no line was drawn. Raises
    ``SpatialInputParseError`` on a malformed line (honesty floor).
    """
    for idx, feat in enumerate(line_feats):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "LineString":
            raise SpatialInputParseError(
                "SPATIAL_INPUT_LINE_NOT_LINESTRING",
                f"line[{idx}] geometry must be a LineString (got "
                f"{geom.get('type')!r})",
            )
        coords = geom.get("coordinates")
        if not isinstance(coords, list) or len(coords) < 2:
            raise SpatialInputParseError(
                "SPATIAL_INPUT_LINE_TOO_SHORT",
                f"line[{idx}].geometry.coordinates must be a LineString with "
                f">= 2 positions",
            )
        out: list[list[float]] = []
        for pidx, pt in enumerate(coords):
            if (
                not isinstance(pt, (list, tuple))
                or len(pt) < 2
                or not all(isinstance(v, (int, float)) for v in pt[:2])
            ):
                raise SpatialInputParseError(
                    "SPATIAL_INPUT_LINE_BAD_COORDS",
                    f"line[{idx}].geometry.coordinates[{pidx}] must be "
                    f"[lon, lat]",
                )
            out.append([float(pt[0]), float(pt[1])])
        return out
    return None


def _points(point_feats: list[dict[str, Any]]) -> list[list[float]]:
    """Extract ``[lon, lat]`` from each ``role=="point"`` feature."""
    out: list[list[float]] = []
    for idx, feat in enumerate(point_feats):
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            raise SpatialInputParseError(
                "SPATIAL_INPUT_POINT_NOT_POINT",
                f"point[{idx}] geometry must be a Point (got {geom.get('type')!r})",
            )
        coords = geom.get("coordinates")
        if (
            not isinstance(coords, (list, tuple))
            or len(coords) < 2
            or not all(isinstance(v, (int, float)) for v in coords[:2])
        ):
            raise SpatialInputParseError(
                "SPATIAL_INPUT_POINT_BAD_COORDS",
                f"point[{idx}].geometry.coordinates must be [lon, lat]",
            )
        out.append([float(coords[0]), float(coords[1])])
    return out


def parse_spatial_input_features(fc: dict[str, Any]) -> ParsedSpatialInput:
    """Parse a drawn ``FeatureCollection`` into role-split engine inputs.

    The single entry point: splits by role, then translates each bucket into the
    exact shape the urban PySWMM engine accepts (barriers FC / AOI bbox /
    points). Raises ``SpatialInputParseError`` (typed ``error_code``) on any
    structurally invalid input so the caller surfaces an honest typed error
    instead of a silently-wrong success.
    """
    buckets = split_features_by_role(fc)
    barriers_fc, n_walls, n_flap_gates = barriers_feature_collection(
        buckets["barrier"]
    )
    aoi_feats = buckets["aoi"]
    line_feats = buckets["line"]
    return ParsedSpatialInput(
        barriers=barriers_fc,
        aoi_bbox=_aoi_bbox(aoi_feats),
        aoi_features=aoi_feats,
        points=_points(buckets["point"]),
        line_coords=_line_coords(line_feats),
        n_lines=len(line_feats),
        n_walls=n_walls,
        n_flap_gates=n_flap_gates,
    )
