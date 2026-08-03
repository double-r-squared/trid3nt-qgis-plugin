"""Unit tests for the shared drawn-geometry role vocabulary (ADR 0099, mesh M2).

Covers the generalized 7-role parser in
``trid3nt_server.agent.mesh.spatial_roles`` -- the canonical DOMAIN stage every
engine consumes. Legacy-compat (aoi/barrier/point/line) is covered by
``test_spatial_input_barriers.py`` / ``test_spatial_input_neutral_line.py``
through the adapter; here we exercise the NEW roles + the alias + honesty floor.
"""
from __future__ import annotations

import pytest

from trid3nt_server.agent.mesh.spatial_roles import (
    CANONICAL_ROLES,
    ROLE_ALIASES,
    SpatialRoleError,
    parse_drawn_roles,
)


def _fc(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def _feat(role, geom_type, coords, **props):
    return {
        "type": "Feature",
        "geometry": {"type": geom_type, "coordinates": coords},
        "properties": {"role": role, **props},
    }


def test_canonical_vocabulary_is_the_seven_roles() -> None:
    assert CANONICAL_ROLES == frozenset(
        {
            "barrier",
            "breakline",
            "breach",
            "refine_region",
            "aoi_clip",
            "boundary",
            "point",
            "line",
        }
    )
    assert ROLE_ALIASES == {"aoi": "aoi_clip"}


def test_breach_point_role_parses_to_lonlat() -> None:
    roles = parse_drawn_roles(_fc(_feat("breach", "Point", [-95.1, 29.7])))
    assert roles.breach_points == [[-95.1, 29.7]]


def test_refine_region_reads_target_size_and_bbox() -> None:
    poly = [[[-95.2, 29.6], [-95.0, 29.6], [-95.0, 29.8], [-95.2, 29.8], [-95.2, 29.6]]]
    roles = parse_drawn_roles(
        _fc(_feat("refine_region", "Polygon", poly, target_size_m=25.0))
    )
    assert len(roles.refine_regions) == 1
    r = roles.refine_regions[0]
    assert r["target_size_m"] == 25.0
    assert r["bbox"] == (-95.2, 29.6, -95.0, 29.8)


def test_refine_region_size_optional_defaults_none() -> None:
    poly = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    roles = parse_drawn_roles(_fc(_feat("refine_region", "Polygon", poly)))
    assert roles.refine_regions[0]["target_size_m"] is None


def test_refine_region_rejects_nonpositive_size() -> None:
    poly = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(_fc(_feat("refine_region", "Polygon", poly, target_size_m=-5)))
    assert exc.value.error_code == "SPATIAL_INPUT_REFINE_BAD_SIZE"


def test_breakline_role_parses_vertices() -> None:
    roles = parse_drawn_roles(
        _fc(_feat("breakline", "LineString", [[-95.1, 29.7], [-95.0, 29.72]]))
    )
    assert roles.breaklines == [[[-95.1, 29.7], [-95.0, 29.72]]]


def test_boundary_role_reads_type() -> None:
    roles = parse_drawn_roles(
        _fc(
            _feat("boundary", "LineString", [[0, 0], [1, 1]], boundary_type="inflow"),
            _feat("boundary", "LineString", [[2, 2], [3, 3]]),
        )
    )
    assert roles.boundary_lines[0]["boundary_type"] == "inflow"
    assert roles.boundary_lines[1]["boundary_type"] is None


def test_boundary_rejects_unknown_type() -> None:
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(
            _fc(_feat("boundary", "LineString", [[0, 0], [1, 1]], boundary_type="side"))
        )
    assert exc.value.error_code == "SPATIAL_INPUT_BAD_BOUNDARY_TYPE"


def test_legacy_aoi_alias_maps_to_aoi_clip() -> None:
    poly = [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]]
    roles = parse_drawn_roles(_fc(_feat("aoi", "Polygon", poly)))
    assert roles.aoi_bbox == (-1, -1, 1, 1)
    assert len(roles.aoi_clip_features) == 1


def test_aoi_clip_canonical_role() -> None:
    poly = [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]]
    roles = parse_drawn_roles(_fc(_feat("aoi_clip", "Polygon", poly)))
    assert roles.aoi_bbox == (-1, -1, 1, 1)


def test_unknown_role_raises_honestly() -> None:
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(_fc(_feat("hazard_zone", "Point", [0, 0])))
    assert exc.value.error_code == "SPATIAL_INPUT_BAD_ROLE"


def test_breach_wrong_geometry_raises() -> None:
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(_fc(_feat("breach", "LineString", [[0, 0], [1, 1]])))
    assert exc.value.error_code == "SPATIAL_INPUT_BREACH_NOT_POINT"


def test_mixed_roles_coexist() -> None:
    poly = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    roles = parse_drawn_roles(
        _fc(
            _feat("barrier", "LineString", [[0, 0], [0, 1]], barrier_type="wall"),
            _feat("breach", "Point", [0.5, 0.5]),
            _feat("refine_region", "Polygon", poly, target_size_m=10.0),
            _feat("aoi_clip", "Polygon", poly),
        )
    )
    assert roles.n_walls == 1
    assert roles.breach_points == [[0.5, 0.5]]
    assert len(roles.refine_regions) == 1
    assert roles.aoi_bbox == (0, 0, 1, 1)
