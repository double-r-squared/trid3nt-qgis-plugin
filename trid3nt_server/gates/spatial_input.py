"""Drawn ``FeatureCollection`` -> engine inputs.

This is the AGENT-side consumer of the drawn output. The canonical role
vocabulary + structural parser live in the mesh authoring layer at
:mod:`trid3nt_server.gates.spatial_roles` so a breakline / breach /
refine-region / aoi-clip means the same thing to every engine. This module is
the ADAPTER over that shared parser: it holds the ``ParsedSpatialInput`` shape +
the ``parse_spatial_input_features`` entry point the spatial-input card and the
server import, and re-exports the shared primitives.

``parse_spatial_input_features`` returns a ``ParsedSpatialInput`` carrying the
AOI bbox, the points and the neutral line PLUS the generalized roles (breach
points, refine regions, breaklines, boundary lines) so the card can surface them
to whichever engine the user is driving.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trid3nt_server.gates.spatial_roles import (
    DrawnRoles,
    SpatialInputParseError,
    SpatialRoleError,
    parse_drawn_roles,
    split_features_by_role,
)

__all__ = [
    "SpatialInputParseError",
    "SpatialRoleError",
    "ParsedSpatialInput",
    "parse_spatial_input_features",
    "split_features_by_role",
]


@dataclass
class ParsedSpatialInput:
    """The role-split result of a drawn ``FeatureCollection`` (adapter shape).

    The drawn fields:
        aoi_bbox: union extent of the clip polygons, or ``None``.
        aoi_features: raw clip polygons (``aoi_clip`` + legacy ``aoi``).
        points: ``[[lon, lat], ...]`` from the generic ``point`` role.
        line_coords: the FIRST neutral ``line`` feature's vertices, or ``None``.
        n_lines: count of neutral ``line`` features.

    Generalized roles (surfaced for every engine):
        breach_points: ``[[lon, lat], ...]`` interior breach sources
            (SFINCS/GeoClaw ``breach_point``).
        refine_regions: ``[{"polygon", "target_size_m", "bbox"}]`` mesh sizing.
        breaklines: ``[[[lon, lat], ...], ...]`` edge-constraining lines.
        boundary_lines: ``[{"coords", "boundary_type"}]`` open boundaries.
    """

    aoi_bbox: tuple[float, float, float, float] | None = None
    aoi_features: list[dict[str, Any]] = field(default_factory=list)
    points: list[list[float]] = field(default_factory=list)
    line_coords: list[list[float]] | None = None
    n_lines: int = 0
    breach_points: list[list[float]] = field(default_factory=list)
    refine_regions: list[dict[str, Any]] = field(default_factory=list)
    breaklines: list[list[list[float]]] = field(default_factory=list)
    boundary_lines: list[dict[str, Any]] = field(default_factory=list)


def parse_spatial_input_features(fc: dict[str, Any]) -> ParsedSpatialInput:
    """Parse a drawn ``FeatureCollection`` into role-split engine inputs.

    Delegates to :func:`trid3nt_server.gates.spatial_roles.parse_drawn_roles`
    and adapts the canonical :class:`DrawnRoles` to :class:`ParsedSpatialInput`. Raises
    :class:`~trid3nt_server.gates.spatial_roles.SpatialRoleError`
    (aliased ``SpatialInputParseError``, typed ``error_code``) on any
    structurally invalid input -- an honest typed error, never a silent success.
    """
    roles: DrawnRoles = parse_drawn_roles(fc)
    return ParsedSpatialInput(
        aoi_bbox=roles.aoi_bbox,
        aoi_features=roles.aoi_clip_features,
        points=roles.points,
        line_coords=roles.line_coords,
        n_lines=roles.n_lines,
        breach_points=roles.breach_points,
        refine_regions=roles.refine_regions,
        breaklines=roles.breaklines,
        boundary_lines=roles.boundary_lines,
    )
