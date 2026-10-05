"""Unit tests for the shared drawn-geometry role vocabulary.

Covers the generalized role parser in ``trid3nt_server.inputs.gate.spatial_roles`` --
the canonical DOMAIN stage every engine consumes. The adapter surface over it is
covered by ``test_spatial_input_gate.py`` / ``test_spatial_input_neutral_line.py``;
here we exercise the roles + the alias + the honesty floor.
"""
from __future__ import annotations

import pytest

from trid3nt_server.inputs.gate.spatial_roles import (
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


def test_canonical_vocabulary_is_the_declared_roles() -> None:
    assert CANONICAL_ROLES == frozenset({"aoi_clip", "point", "line", "breakline",
                                         "refine_region", "boundary", "breach"})
    assert ROLE_ALIASES == {"aoi": "aoi_clip"}


_RIDGE = [[0.0, 0.0], [0.01, 0.0]]
_BOX = [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]


def test_a_drawn_breakline_routes_to_the_mesher_s_line_constraint() -> None:
    from trid3nt_server.tools.mesh.op_tool import drawn_ops

    roles = parse_drawn_roles(_fc(_feat("breakline", "LineString", _RIDGE)))
    assert drawn_ops(roles) == [{"fn": "set_obstacle", "kwargs": {
        "geometry": {"type": "LineString", "coordinates": _RIDGE},
        "constrain": True}}]


def test_a_drawn_refine_region_routes_to_a_size_inside_the_polygon() -> None:
    from trid3nt_server.tools.mesh.op_tool import drawn_ops

    roles = parse_drawn_roles(_fc(_feat("refine_region", "Polygon", _BOX,
                                        target_size_m=15.0)))
    (op,) = drawn_ops(roles)
    assert op["fn"] == "set_region_size"
    assert op["kwargs"]["edge_length_m"] == 15.0
    assert op["kwargs"]["geometry"]["type"] == "Polygon"
    with pytest.raises(SpatialRoleError, match="positive"):
        parse_drawn_roles(_fc(_feat("refine_region", "Polygon", _BOX,
                                    target_size_m=-1)))


def test_a_drawn_boundary_routes_to_a_typed_run_of_the_edge() -> None:
    from trid3nt_server.inputs.boundary import boundary_runs
    from trid3nt_server.tools.mesh.op_tool import drawn_ops

    roles = parse_drawn_roles(_fc(_feat("boundary", "LineString", _RIDGE,
                                        boundary_type="inflow")))
    (op,) = drawn_ops(roles)
    assert op["fn"] == "set_boundary_roles"
    (run,) = boundary_runs(op["kwargs"]["runs"])
    assert run.type == "inflow"
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(_fc(_feat("boundary", "LineString", _RIDGE)))
    assert exc.value.error_code == "SPATIAL_INPUT_BAD_BOUNDARY_TYPE"


def test_a_drawn_breach_is_a_line_on_the_crest() -> None:
    roles = parse_drawn_roles(_fc(_feat("breach", "LineString", _RIDGE)))
    assert roles.breach_lines == [_RIDGE]
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(_fc(_feat("breach", "Point", [0, 0])))
    assert exc.value.error_code == "SPATIAL_INPUT_BREACH_NOT_LINESTRING"


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


def test_point_wrong_geometry_raises() -> None:
    with pytest.raises(SpatialRoleError) as exc:
        parse_drawn_roles(_fc(_feat("point", "LineString", [[0, 0], [1, 1]])))
    assert exc.value.error_code == "SPATIAL_INPUT_POINT_NOT_POINT"


def test_mixed_roles_coexist() -> None:
    poly = [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]
    roles = parse_drawn_roles(
        _fc(
            _feat("line", "LineString", [[0, 0], [0, 1]]),
            _feat("point", "Point", [0.5, 0.5]),
            _feat("aoi_clip", "Polygon", poly),
        )
    )
    assert roles.line_coords == [[0, 0], [0, 1]]
    assert roles.points == [[0.5, 0.5]]
    assert roles.aoi_bbox == (0, 0, 1, 1)
