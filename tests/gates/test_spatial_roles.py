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
    assert CANONICAL_ROLES == frozenset({"aoi_clip", "point", "line"})
    assert ROLE_ALIASES == {"aoi": "aoi_clip"}


def test_a_plan_era_role_is_refused_by_name() -> None:
    for gone in ("breach", "refine_region", "breakline", "boundary"):
        with pytest.raises(SpatialRoleError) as exc:
            parse_drawn_roles(_fc(_feat(gone, "Point", [0, 0])))
        assert exc.value.error_code == "SPATIAL_INPUT_BAD_ROLE"


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
