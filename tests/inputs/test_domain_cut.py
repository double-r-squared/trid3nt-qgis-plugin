"""THE DOMAIN'S OWN CUT: the mapped water surface sectioned square to a line.

Offline; every geometry is authored here, so there is no world read to stub. A
line is not a domain and a surface is not one either - what is checked is the
CUT the slot does between them: that the two end faces are square to the line
joining the stretch's ends, that a disconnected piece the line misses is
dropped, and that a cut with nothing to measure refuses rather than inventing a
transect.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.domain import _cut_square, domain, needs_beside
from trid3nt_server.inputs.user_input import UserInputError

#: A 0.2 x 0.02 degree band running east along lat 35 - the mapped banks of a
#: river, wide enough that a section of it has real area.
_BANK = {"type": "Polygon", "coordinates": [[
    [-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01],
    [-83.50, 34.99]]]}

_UPSTREAM = [-83.45, 35.0]
_DOWNSTREAM = [-83.40, 35.0]


def _line(*points):
    from shapely.geometry import LineString

    return LineString([tuple(p) for p in points])


def _cut(banks, *points):
    from shapely.geometry import shape

    surfaces = [p for g in (banks["coordinates"]
                            if banks["type"] == "MultiPolygon"
                            else [banks["coordinates"]])
                for p in [shape({"type": "Polygon", "coordinates": g})]]
    return _cut_square(surfaces, _line(*points), "domain", "DOMAIN_INVALID")


def test_a_place_on_a_flowline_declares_the_water_surface_beside_it() -> None:
    """A line reaches the slot with no polygon in it, so the kind that arrives
    open states what it needs to be closed - and a closed kind states none."""
    assert needs_beside("flowline") == (("banks", "water surface", "polygon"),)
    assert needs_beside("waterbody") == ()
    assert needs_beside("") == ()


def test_the_cut_keeps_only_the_water_between_the_two_ends() -> None:
    from shapely.geometry import shape

    geom, _faces = _cut(_BANK, _UPSTREAM, _DOWNSTREAM)
    whole = shape(_BANK).area
    # The band is 0.20 deg long and the stretch 0.05 deg of it: a quarter.
    assert shape(geom).area == pytest.approx(whole / 4.0, rel=0.02)
    assert geom["type"] == "Polygon"


def test_each_end_face_spans_the_water_across_the_cut() -> None:
    """The face is the whole transect the cut left, not the part a probe line
    caught: the cut edge is exactly collinear with such a line, so over a
    domain-sized probe the intersection comes back whole at one end and EMPTY
    at the other."""
    _geom, faces = _cut(_BANK, _UPSTREAM, _DOWNSTREAM)
    for face, lon in zip(faces, (_UPSTREAM[0], _DOWNSTREAM[0])):
        assert len(face) == 2
        assert all(point[0] == pytest.approx(lon, abs=1e-4) for point in face)
        assert abs(face[0][1] - face[1][1]) == pytest.approx(0.02, abs=1e-6)


def test_the_cut_is_square_to_the_line_not_to_the_meridian() -> None:
    from shapely.geometry import shape

    straight = shape(_cut(_BANK, _UPSTREAM, _DOWNSTREAM)[0]).bounds
    slanted = shape(_cut(_BANK, [-83.45, 34.995], [-83.40, 35.005])[0]).bounds
    assert slanted[0] < straight[0]
    assert slanted[2] > straight[2]


def test_a_piece_the_line_never_touches_is_left_out() -> None:
    from shapely.geometry import shape

    two = {"type": "MultiPolygon", "coordinates": [
        [[[-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01],
          [-83.50, 34.99]]],
        [[[-83.50, 35.20], [-83.30, 35.20], [-83.30, 35.22], [-83.50, 35.22],
          [-83.50, 35.20]]]]}
    geom, _faces = _cut(two, _UPSTREAM, _DOWNSTREAM)
    only, _only_faces = _cut(_BANK, _UPSTREAM, _DOWNSTREAM)
    assert shape(geom).geom_type == "Polygon"
    assert shape(geom).area == pytest.approx(shape(only).area)


def test_an_end_the_cut_never_reached_refuses_rather_than_naming_a_bank() -> None:
    with pytest.raises(UserInputError) as excinfo:
        _cut(_BANK, _UPSTREAM, [-83.25, 35.0])
    assert "outflow" in str(excinfo.value)


def test_a_line_off_the_mapped_water_refuses() -> None:
    with pytest.raises(UserInputError):
        _cut(_BANK, [-90.0, 35.0], [-89.9, 35.0])


def test_one_point_twice_has_no_direction_to_be_square_to() -> None:
    with pytest.raises(UserInputError) as excinfo:
        _cut(_BANK, _UPSTREAM, _UPSTREAM)
    assert "one point" in str(excinfo.value)


def test_a_line_with_no_water_surface_beside_it_has_nothing_to_solve_between() -> None:
    """A flowline is not banks. Nothing widens it here."""
    line = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString",
                      "coordinates": [_UPSTREAM, _DOWNSTREAM]}}]}
    with pytest.raises(UserInputError) as excinfo:
        domain(line, banks=dict(line))
    assert "water SURFACE" in str(excinfo.value)


def test_the_cut_domain_carries_its_two_runs_and_the_centerline() -> None:
    line = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString",
                      "coordinates": [[-83.48, 35.0], _UPSTREAM, _DOWNSTREAM]}}]}
    banks = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": _BANK}]}
    cut = domain(line, banks=banks, near=(-83.45, 35.0), span_km=4.5)
    assert [run.type for run in cut.runs] == ["inflow", "outflow"]
    assert cut.companions["centerline"]["type"] == "LineString"
    assert cut.geometry["type"] in ("Polygon", "MultiPolygon")


def test_the_span_is_measured_from_the_seed_along_the_line() -> None:
    """A stretch shorter than the line it was cut from is the stretch asked
    for, and it starts where the seed stands."""
    from shapely.geometry import shape

    line = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString",
                      "coordinates": [[-83.49, 35.0], [-83.31, 35.0]]}}]}
    banks = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": _BANK}]}
    cut = domain(line, banks=banks, near=(-83.45, 35.0), span_km=2.0)
    west, _south, east, _north = shape(cut.geometry).bounds
    assert west == pytest.approx(-83.45, abs=1e-3)
    assert east < -83.41
