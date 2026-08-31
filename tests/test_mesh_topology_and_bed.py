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
_WEST_FACE = {"type": "LineString", "coordinates": [[0.0, 0.0], [0.0, 40.0]]}
_EAST_FACE = {"type": "LineString", "coordinates": [[200.0, 0.0], [200.0, 40.0]]}


def _shapes(**faces):
    from shapely.geometry import shape as _shape

    return {role: _shape(geometry) for role, geometry in faces.items()}


def test_both_transect_faces_land_their_own_role():
    """Each end cap is one role, whole; the banks between them are neither."""
    roles = T.match_boundary_roles(
        _STRIP, range(8), _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE),
        tolerance_m=20.0)
    assert roles == {"inflow": [0, 1, 2], "outflow": [5, 6, 7]}


def test_a_node_off_every_face_carries_no_role_and_is_solid_wall():
    """A bank node takes the role of neither cap, however near one it is."""
    roles = T.match_boundary_roles(
        _STRIP, [3, 4], _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE),
        tolerance_m=20.0)
    assert roles == {}


def test_a_mesh_with_no_declared_boundaries_carries_no_roles():
    """Nothing is inferred: an undeclared boundary is entirely solid wall, which
    is what makes a deck against it refuse rather than solve on a guess."""
    assert T.match_boundary_roles(_STRIP, range(8), {}, tolerance_m=20.0) == {}


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
