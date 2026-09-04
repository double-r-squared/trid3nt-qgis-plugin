"""Offline tests for the SERVER-SIDE release-point pre-flight.

The question this settles is whether a release point can be a source at all, and
it is settled here rather than in the worker: the domain polygon and the flowline
are geometry the run already holds, so the answer is available before anything is
staged and a point that cannot be honored is refused while the user can still
move it.

What the old plumbing did instead - and what these tests exist to keep out - was
accept a point within a couple of channel widths of a mesh node, walk
``spill_fraction`` when it missed, and let the server discover the relocation
from the run's own metrics afterwards.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.telemac.release_point import (
    contain_release_point,
    domain_polygon_of,
)
from trid3nt_server.workflows.telemac.helpers.errors import (
    TelemacDyeScenarioError,
    TelemacReleaseOutsideDomainError,
)

# A ~0.02 deg square of "water" near Twin Falls, Idaho, with a flowline running
# west-east through its middle. Inline GeoJSON so nothing is read or fetched.
_DOMAIN = json.dumps({
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "Polygon",
        "coordinates": [[[-114.33, 42.57], [-114.29, 42.57],
                         [-114.29, 42.59], [-114.33, 42.59],
                         [-114.33, 42.57]]]}}]})

_FLOWLINE = json.dumps({
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "LineString",
        "coordinates": [[-114.34, 42.58], [-114.31, 42.58], [-114.28, 42.58]]}}]})


class _Artifact:
    """The one thing the pre-flight reads off an accepted mesh."""

    def __init__(self, extent):
        self.provenance = {"recipe": {"mesher": "om2d", "extent": extent}}


def test_a_point_on_the_flowline_inside_the_domain_is_honored_unmoved():
    got = contain_release_point(point=(-114.31, 42.58), domain=_DOMAIN,
                                flowline=_FLOWLINE)
    assert got.lon == pytest.approx(-114.31, abs=1e-5)
    assert got.lat == pytest.approx(42.58, abs=1e-5)
    assert got.snap_distance_m < 1.0
    assert "on the flowline" in got.note


def test_a_point_inside_the_domain_but_off_the_river_is_snapped_onto_it():
    """The snap is to the REAL flowline, and the distance it moved is recorded -
    a moved point must never read on the map as a placed one."""
    got = contain_release_point(point=(-114.31, 42.585), domain=_DOMAIN,
                                flowline=_FLOWLINE)
    assert got.lon == pytest.approx(-114.31, abs=1e-4)
    assert got.lat == pytest.approx(42.58, abs=1e-4)
    assert 400.0 < got.snap_distance_m < 700.0  # ~0.005 deg of latitude
    assert "moved" in got.note and "onto the flowline" in got.note


def test_a_point_outside_the_domain_refuses_and_names_the_fix():
    """No band, no nearest-anything: outside the polygon there is nothing to
    place the source on that the user chose."""
    with pytest.raises(TelemacReleaseOutsideDomainError) as excinfo:
        contain_release_point(point=(-114.26, 42.58), domain=_DOMAIN,
                              flowline=_FLOWLINE)
    err = excinfo.value
    assert err.error_code == "TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN"
    assert err.retryable is True and len(err.suggestions) == 3
    assert err.distance_m > 1000.0
    message = str(err)
    assert "not inside the domain polygon" in message
    assert "spill_fraction" in message


def test_the_snap_never_leaves_the_domain_by_following_the_river_out_of_it():
    """The flowline runs on past the modeled stretch. A point near the domain's
    east edge must land on the stretch INSIDE it, not on the nearer length of
    river the run does not solve."""
    got = contain_release_point(point=(-114.2905, 42.5895), domain=_DOMAIN,
                                flowline=_FLOWLINE)
    assert -114.33 <= got.lon <= -114.29
    assert got.lat == pytest.approx(42.58, abs=1e-4)


def test_a_flowline_that_misses_the_domain_refuses_rather_than_reaching_for_it():
    away = json.dumps({"type": "LineString",
                       "coordinates": [[-114.20, 42.58], [-114.18, 42.58]]})
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        contain_release_point(point=(-114.31, 42.58), domain=_DOMAIN,
                              flowline=away)
    assert "different reaches" in str(excinfo.value)


def test_a_domain_source_carrying_no_polygon_refuses():
    line_only = json.dumps({"type": "LineString",
                            "coordinates": [[-114.33, 42.58], [-114.29, 42.58]]})
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        contain_release_point(point=(-114.31, 42.58), domain=line_only,
                              flowline=_FLOWLINE)
    assert "no polygon" in str(excinfo.value)


def test_the_domain_read_is_the_mesh_own_record_of_what_it_was_cut_from():
    assert domain_polygon_of(_Artifact("s3://cache/section/reach.geojson")) == (
        "s3://cache/section/reach.geojson")


@pytest.mark.parametrize("art", [
    _Artifact([-114.33, 42.57, -114.29, 42.59]),  # a box, not a shape
    _Artifact(None),                              # a mesh that states no extent
    None,                                         # no accepted mesh at all
])
def test_a_mesh_with_no_domain_polygon_refuses_rather_than_waving_the_point_through(art):
    """There is ONE containment path and it always has a polygon.

    Four numbers are not a shape a point can be inside of. Answering "no domain"
    let a supplied point ride into the run untested with a note about it, which
    is a release inside a shape nobody mapped - so the read refuses instead.
    """
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        domain_polygon_of(art)
    assert "no mapped shape" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# A DERIVED release is settled against the MESH, not just against the line.
# --------------------------------------------------------------------------- #
def _mesh_holding(x_from: float, x_to: float, monkeypatch):
    """Stand in a mesh whose cells cover only ``x_from..x_to`` of the centerline."""
    import numpy as np

    from trid3nt_server.workflows.mesh.shared import nodes as nodes_mod

    monkeypatch.setattr(
        nodes_mod, "read_accepted_mesh_nodes",
        lambda _uri, utm_epsg=None: (
            np.array([[x_from, -50.0], [x_to, -50.0], [x_to, 50.0], [x_from, 50.0]]),
            np.array([[0, 1, 2], [0, 2, 3]]), None, None))
    return {"display_uri": "s3://m/M/mesh.2dm",
            "artifact": type("A", (), {"utm_epsg": 32611})()}


def test_a_derived_release_inside_the_mesh_is_left_where_it_was(monkeypatch):
    from trid3nt_server.workflows.telemac.release_point import derive_release_on_mesh

    mesh = _mesh_holding(0.0, 1000.0, monkeypatch)
    (_lon, _lat), note = derive_release_on_mesh(
        centerline_utm=[[0.0, 0.0], [1000.0, 0.0]], mesh=mesh, fraction=0.25)
    assert note is None


def test_a_derived_release_above_the_meshed_stretch_walks_downstream(monkeypatch):
    """The centerline runs on past what the mapped banks left; the station has to
    be inside the triangulation or the solver stops with the source outside the
    domain."""
    from trid3nt_server.workflows.telemac.release_point import derive_release_on_mesh

    mesh = _mesh_holding(400.0, 1000.0, monkeypatch)
    _lonlat, note = derive_release_on_mesh(
        centerline_utm=[[0.0, 0.0], [1000.0, 0.0]], mesh=mesh, fraction=0.02)
    assert note is not None and "downstream" in note
    walked = float(note.split("moved ")[1].split(" m")[0])
    assert 370.0 <= walked <= 400.0     # 20 m in, then the first meshed station


def test_a_centerline_the_mesh_never_holds_refuses(monkeypatch):
    from trid3nt_server.workflows.telemac.release_point import derive_release_on_mesh

    mesh = _mesh_holding(5000.0, 6000.0, monkeypatch)
    with pytest.raises(TelemacDyeScenarioError):
        derive_release_on_mesh(centerline_utm=[[0.0, 0.0], [1000.0, 0.0]],
                               mesh=mesh, fraction=0.0)


# --------------------------------------------------------------------------- #
# The third question: is there WATER there when the run opens?
# --------------------------------------------------------------------------- #
#: Four nodes 100 m apart along one bank line. The engine solves a source at the
#: node nearest it (``proxim.f``), so these are the only places a release can be.
_NODES = [[0.0, 0.0], [100.0, 0.0], [200.0, 0.0], [300.0, 0.0]]


def _snap(point, wet):
    from trid3nt_server.workflows.telemac.release_point import snap_release_to_wetted

    return snap_release_to_wetted(point, node_xy=_NODES, wet=wet,
                                  state="a stand-in initial state")


def test_a_release_landing_on_a_wet_node_is_left_exactly_where_it_was():
    """The point the user placed or the centerline derived is the answer; moving
    a release that is already in water would answer a different question."""
    where, moved, node = _snap((110.0, 0.0), [True] * 4)
    assert where == (110.0, 0.0) and moved == 0.0 and node == 1


def test_a_release_landing_on_a_DRY_node_moves_to_the_nearest_wet_one():
    """A source on dry ground discharges into the bed: the plume then starts
    where the water later arrives rather than where it was released."""
    where, moved, node = _snap((110.0, 0.0), [True, False, False, True])
    assert node == 0 and where == (0.0, 0.0)
    assert moved == pytest.approx(110.0)


def test_an_initial_state_with_no_wet_node_anywhere_refuses_typed():
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        _snap((110.0, 0.0), [False] * 4)
    assert excinfo.value.error_code == "TELEMAC_RELEASE_NOWHERE_WET"
    # the refusal names how far the water it could not find would have been
    assert "10 m away and DRY" in str(excinfo.value)
    assert "a stand-in initial state" in str(excinfo.value)
