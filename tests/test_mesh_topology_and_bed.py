"""The accepted topology a geometry file cannot state, and the bed fitted onto it.

Offline: no container, no object store. The pair WRITER is proved through the
image by the mesh drivers; what is pinned here is the record the server keeps
beside the geometry and the fit the solve is handed.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from trid3nt_server.workflows.mesh import topology as T
from trid3nt_server.workflows.mesh.shared.nodes import (
    MeshNodeError,
    fit_downstream_bed,
    read_centerline_utm,
)


def test_the_bundle_round_trips_the_roles_and_the_measured_order(tmp_path):
    path = T.write_topology(tmp_path, roles={"inflow": [1, 2], "outflow": [7]},
                            liquid_boundary_order=["outflow", "inflow"])
    assert path.name == T.TOPOLOGY_FILENAME
    read = T.read_topology(str(path))
    assert read["roles"] == {"inflow": [1, 2], "outflow": [7]}
    assert read["liquid_boundary_order"] == ["outflow", "inflow"]


def test_a_bundle_with_no_roles_on_its_boundary_refuses(tmp_path):
    """A mesh nobody classified is a mesh no reach deck can be authored against."""
    path = tmp_path / T.TOPOLOGY_FILENAME
    path.write_text(json.dumps({"roles": {}, "liquid_boundary_order": []}))
    with pytest.raises(ValueError, match="no roles"):
        T.read_topology(str(path))


def test_an_empty_role_is_not_a_role(tmp_path):
    path = tmp_path / T.TOPOLOGY_FILENAME
    path.write_text(json.dumps({"roles": {"inflow": []},
                                "liquid_boundary_order": ["inflow"]}))
    with pytest.raises(ValueError):
        T.read_topology(str(path))


# --------------------------------------------------------------------------- #
# Matching the DECLARED roles onto the mesh the mesher actually built.
# --------------------------------------------------------------------------- #
#: A 200 m x 40 m strip of boundary nodes: the two 40 m end caps are the
#: transects a section cut, and the two long sides are the banks between them.
_STRIP = np.array(
    [[0.0, 0.0], [0.0, 20.0], [0.0, 40.0],          # 0,1,2  west cap
     [100.0, 0.0], [100.0, 40.0],                   # 3,4    banks
     [200.0, 0.0], [200.0, 20.0], [200.0, 40.0]])   # 5,6,7  east cap
#: The strip's boundary walked as ONE closed contour, which is the shape a role
#: is resolved against: up the west cap, along the north bank, down the east cap,
#: back along the south bank.
_STRIP_CONTOUR = [[0, 1, 2, 4, 7, 6, 5, 3]]
_WEST_FACE = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.0, 40.0]]}
_EAST_FACE = {"type": "LineString", "coordinates": [[200.0, 0.0], [200.0, 40.0]]}

#: What the do_sag mesh's boundary MEASURED under the nearest-node rule, walked
#: from the contour's own origin: each declared role landed as several stretches
#: with holes between them, and the inflow's stretches sat either side of the
#: origin. A .cli written from that describes five liquid boundaries where two
#: were declared.
_SCATTER = ".III...OO.OO...IIII"


def _shapes(**faces):
    from shapely.geometry import shape as _shape

    return {role: _shape(geometry) for role, geometry in faces.items()}


def _ring_on_a_circle(size: int, radius: float = 100.0) -> np.ndarray:
    """``size`` boundary nodes in walk order, so node ``i`` IS ring position ``i``."""
    angle = 2.0 * np.pi * np.arange(size) / size
    return radius * np.column_stack([np.cos(angle), np.sin(angle)])


def _face_across(points: np.ndarray, first: int, last: int) -> dict:
    """A transect declared by the two nodes its ends stand on."""
    return {"type": "LineString",
            "coordinates": [list(points[first]), list(points[last])]}


def _forced_contiguous(labels: str, role: str) -> list[int]:
    """The isolating probe's closure: the shortest wrapping window holding a role.

    Reproduced verbatim from the probe that ISOLATED the scatter - it rewrote the
    measured labels into runs by hand and got two clean liquid boundaries out of
    the writer. What the matcher constructs now has to agree with it.
    """
    size = len(labels)
    seats = [i for i, mark in enumerate(labels) if mark == role]
    start, span = min(((seat, max((i - seat) % size for i in seats))
                       for seat in seats), key=lambda pair: pair[1])
    return [(start + step) % size for step in range(span + 1)]


def test_both_transect_faces_land_their_own_role():
    """Each end cap is one role, whole; the banks between them are neither."""
    roles = T.match_boundary_roles(
        _STRIP, _STRIP_CONTOUR, _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE),
        tolerance_m=20.0)
    assert roles == {"inflow": [0, 1, 2], "outflow": [5, 6, 7]}


def test_a_face_that_ends_nowhere_near_the_boundary_carries_no_role():
    """A face and a mesh that describe different domains match nothing."""
    roles = T.match_boundary_roles(
        _STRIP, [[3, 4]], _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE),
        tolerance_m=20.0)
    assert roles == {}


def test_a_cut_corner_does_not_cost_the_face_its_role():
    """A triangulator conforms along a polygon's sides and cuts its corners, so
    the anchors are the NEAREST nodes rather than nodes inside a tolerance: the
    end caps here are chamfered well past one mean boundary edge and the face
    still lands whole."""
    chamfered = np.array(
        [[8.0, 0.0], [0.0, 12.0], [0.0, 28.0], [8.0, 40.0],   # 0..3 west cap
         [100.0, 40.0],                                       # 4    north bank
         [192.0, 40.0], [200.0, 28.0], [200.0, 12.0],         # 5..7 east cap
         [192.0, 0.0], [100.0, 0.0]])                         # 8,9  south bank
    roles = T.match_boundary_roles(
        chamfered, [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]],
        _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE), tolerance_m=20.0)
    # each run is walked from the face's own first end, so a role's list may run
    # either way round the contour; what it may not do is skip a node.
    assert roles == {"inflow": [0, 1, 2, 3], "outflow": [8, 7, 6, 5]}


def test_a_mesh_with_no_declared_boundaries_carries_no_roles():
    """Nothing is inferred: an undeclared boundary is entirely solid wall, which
    is what makes a deck against it refuse rather than solve on a guess."""
    assert T.match_boundary_roles(_STRIP, _STRIP_CONTOUR, {},
                                  tolerance_m=20.0) == {}


def test_a_scattered_candidate_boundary_resolves_into_two_contiguous_runs():
    """The holes the measured scatter left are INSIDE the declared stretch."""
    points = _ring_on_a_circle(len(_SCATTER))
    roles = T.match_boundary_roles(
        points, [list(range(len(_SCATTER)))],
        _shapes(inflow=_face_across(points, 15, 3),
                outflow=_face_across(points, 7, 11)),
        tolerance_m=1.0)
    assert roles == {"inflow": [15, 16, 17, 18, 0, 1, 2, 3],
                     "outflow": [7, 8, 9, 10, 11]}
    size = len(_SCATTER)
    for run in roles.values():
        assert all((b - a) % size == 1 for a, b in zip(run, run[1:]))


def test_a_run_that_wraps_the_contours_origin_stays_one_run():
    """A contour has no first node; a stretch across position zero is not two."""
    points = _ring_on_a_circle(len(_SCATTER))
    roles = T.match_boundary_roles(
        points, [list(range(len(_SCATTER)))],
        _shapes(inflow=_face_across(points, 15, 3)), tolerance_m=1.0)
    assert roles["inflow"][0] == 15 and roles["inflow"][-1] == 3
    assert 0 in roles["inflow"]


def test_the_matcher_reproduces_the_probes_forced_contiguous_result():
    """The hand-closed runs that produced two liquid boundaries, CONSTRUCTED."""
    points = _ring_on_a_circle(len(_SCATTER))
    roles = T.match_boundary_roles(
        points, [list(range(len(_SCATTER)))],
        _shapes(inflow=_face_across(points, 15, 3),
                outflow=_face_across(points, 7, 11)),
        tolerance_m=1.0)
    assert roles["inflow"] == _forced_contiguous(_SCATTER, "I")
    assert roles["outflow"] == _forced_contiguous(_SCATTER, "O")


def test_a_point_declared_role_is_the_run_it_stands_within():
    """A catchment outlet names a point; what it names on the mesh is a stretch,
    and the stretch stops at the first node past the tolerance rather than
    picking up a node on the far side of the domain."""
    points = _ring_on_a_circle(12, radius=100.0)
    outlet = {"type": "Point", "coordinates": list(points[0])}
    spacing = float(np.hypot(*(points[1] - points[0])))
    roles = T.match_boundary_roles(points, [list(range(12))],
                                   _shapes(outflow=outlet),
                                   tolerance_m=spacing * 1.2)
    assert roles == {"outflow": [11, 0, 1]}


# --------------------------------------------------------------------------- #
# The fitted bed.
# --------------------------------------------------------------------------- #
_CENTERLINE = np.array([[0.0, 0.0], [1000.0, 0.0]])


def _nodes(n: int = 21) -> np.ndarray:
    xs = np.linspace(0.0, 1000.0, n)
    return np.column_stack([xs, np.zeros(n)])


def test_the_fitted_bed_runs_downhill_even_when_the_dem_does_not():
    """A surface DEM along a thalweg runs uphill between nodes; a solve on that
    ponds. The fit is monotone, and the clamp that made it so is reported."""
    points = _nodes()
    # Alternating +/- 2 m about a flat 100 m: the raw surface runs UPHILL between
    # every other pair of nodes and carries no downstream trend at all.
    noisy = 100.0 + 2.0 * np.cos(np.arange(points.shape[0]) * np.pi)
    bed, stats = fit_downstream_bed(points, _CENTERLINE, noisy,
                                    min_slope=3.0e-4, max_slope=6.0e-3)
    assert np.all(np.diff(bed) < 0.0)
    assert stats["enforced_slope"] == pytest.approx(3.0e-4)
    assert stats["measured_slope"] < stats["enforced_slope"]
    assert stats["reach_len_m"] == pytest.approx(1000.0)
    assert stats["bed_drop_m"] == pytest.approx(0.3)


def test_a_steep_dem_is_held_under_the_ceiling():
    points = _nodes()
    steep = 100.0 - 0.02 * points[:, 0]
    _, stats = fit_downstream_bed(points, _CENTERLINE, steep,
                                  min_slope=3.0e-4, max_slope=6.0e-3)
    assert stats["measured_slope"] == pytest.approx(0.02)
    assert stats["enforced_slope"] == pytest.approx(6.0e-3)


def test_holes_shrink_the_support_but_are_counted():
    points = _nodes()
    sampled = 100.0 - 0.001 * points[:, 0]
    sampled[::4] = np.nan
    _, stats = fit_downstream_bed(points, _CENTERLINE, sampled,
                                  min_slope=3.0e-4, max_slope=6.0e-3)
    assert stats["n_dem_nan"] == 6
    assert stats["enforced_slope"] == pytest.approx(0.001, rel=1e-3)


def test_a_raster_that_reaches_none_of_the_mesh_refuses():
    points = _nodes()
    with pytest.raises(MeshNodeError) as excinfo:
        fit_downstream_bed(points, _CENTERLINE, np.full(points.shape[0], np.nan),
                           min_slope=3.0e-4, max_slope=6.0e-3)
    assert excinfo.value.error_code == "MESH_BED_UNSAMPLED"


def test_a_node_off_the_line_takes_the_distance_of_its_nearest_point():
    """Along-channel distance is measured on the centerline, not as the crow flies."""
    bend = np.array([[0.0, 0.0], [500.0, 0.0], [500.0, 500.0]])
    points = np.array([[500.0, 250.0], [10.0, 80.0]])
    _, stats = fit_downstream_bed(points, bend, np.array([10.0, 12.0]),
                                  min_slope=1.0e-4, max_slope=1.0)
    assert stats["reach_len_m"] == pytest.approx(750.0)


# --------------------------------------------------------------------------- #
# ONE centerline reading: the row order of a navigated flowline says nothing.
# --------------------------------------------------------------------------- #
def _flowline_collection(order):
    """A three-row navigated flowline as a FeatureCollection, rows in ``order``."""
    rows = [[[-83.40, 35.00], [-83.39, 35.00]],
            [[-83.39, 35.00], [-83.38, 35.00]],
            [[-83.38, 35.00], [-83.37, 35.00]]]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString", "coordinates": rows[i]}}
        for i in order]}


def test_a_shuffled_flowline_normalizes_to_the_same_chainage():
    """The rows arrive in whatever order the navigate listed them; the reading is
    one head-to-tail line either way, so the bed the fit lays down is the same."""
    head = (-83.40, 35.00)
    straight = read_centerline_utm(_flowline_collection([0, 1, 2]), 32617,
                                   start_lonlat=head)
    shuffled = read_centerline_utm(_flowline_collection([2, 0, 1]), 32617,
                                   start_lonlat=head)
    assert np.allclose(straight, shuffled)

    points = straight[:, :]
    sampled = 100.0 - 0.002 * np.arange(points.shape[0], dtype=float)
    stats_a = fit_downstream_bed(points, straight, sampled,
                                 min_slope=3.0e-4, max_slope=6.0e-3)[1]
    stats_b = fit_downstream_bed(points, shuffled, sampled,
                                 min_slope=3.0e-4, max_slope=6.0e-3)[1]
    assert stats_a["reach_len_m"] == pytest.approx(stats_b["reach_len_m"])
    assert stats_a["bed_drop_m"] == pytest.approx(stats_b["bed_drop_m"])


def test_the_declared_head_decides_the_direction_not_the_merge():
    """Orientation is the chain's fact - the end the navigate started from - so a
    collection whose parts merged the other way still runs head-to-tail."""
    downstream = (-83.37, 35.00)
    line = read_centerline_utm(_flowline_collection([0, 1, 2]), 32617,
                               start_lonlat=downstream)
    assert line[0][0] > line[-1][0]


def test_a_network_that_is_not_one_reach_refuses():
    disjoint = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString",
                      "coordinates": [[-83.40, 35.0], [-83.39, 35.0]]}},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "LineString",
                      "coordinates": [[-83.30, 35.2], [-83.29, 35.2]]}}]}
    with pytest.raises(MeshNodeError) as excinfo:
        read_centerline_utm(disjoint, 32617)
    assert excinfo.value.error_code == "MESH_CENTERLINE_NOT_CONTINUOUS"
