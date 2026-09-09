"""Drawn ``FeatureCollection`` -> engine inputs, for the spatial-input card.

An adapter only: the role vocabulary and the structural parsing live in
``spatial_roles``, which this module re-exports alongside its own shape.
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
    """The role-split result of a drawn ``FeatureCollection``, in adapter shape.

    ``line_coords`` is the FIRST neutral line only; ``n_lines`` counts them all."""

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

    Structurally invalid input raises ``SpatialInputParseError``, a typed
    ``error_code`` refusal -- never a silent success."""
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
