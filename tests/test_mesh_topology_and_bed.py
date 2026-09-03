"""The accepted topology a geometry file cannot state, and the bed painted onto it.

Offline: no container, no object store. The pair WRITER is proved through the
image by the mesh drivers; what is pinned here is the record the server keeps
beside the geometry, the contiguous-run matcher ``set_boundary_roles`` IS, and
the node assignment ``set_bed`` composes.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from trid3nt_server.workflows.mesh import topology as T
from trid3nt_server.workflows.mesh.meshers import Mesh, MeshToolError
from trid3nt_server.workflows.mesh.shared import primitives as P
from trid3nt_server.workflows.mesh.shared.nodes import (
    MeshNodeError,
    read_centerline_utm,
)


def test_the_bundle_round_trips_the_roles_and_the_measured_order(tmp_path):
    path = T.write_topology(tmp_path, roles={"inflow": [1, 2], "outflow": [7]},
                            liquid_boundary_order=["outflow", "inflow"],
                            liquid_boundary_prescribes=["elevation", "flowrate"])
    assert path.name == T.TOPOLOGY_FILENAME
    read = T.read_topology(str(path))
    assert read["roles"] == {"inflow": [1, 2], "outflow": [7]}
    assert read["liquid_boundary_order"] == ["outflow", "inflow"]
    assert read["liquid_boundary_prescribes"] == ["elevation", "flowrate"]


def test_a_bundle_that_states_no_prescription_per_boundary_refuses(tmp_path):
    """It was numbered by the superseded row-order rule, and a deck authored
    against it prescribes into codes that never read it."""
    path = tmp_path / T.TOPOLOGY_FILENAME
    path.write_text(json.dumps({"roles": {"outflow": [7]},
                                "liquid_boundary_order": ["outflow"]}))
    with pytest.raises(ValueError, match="rebuild the mesh"):
        T.read_topology(str(path))


def test_a_bundle_with_no_roles_on_its_boundary_refuses(tmp_path):
    """A mesh nobody classified is a mesh no reach deck can be authored against."""
    path = tmp_path / T.TOPOLOGY_FILENAME
    path.write_text(json.dumps({"roles": {}, "liquid_boundary_order": []}))
    with pytest.raises(ValueError, match="no roles"):
        T.read_topology(str(path))


def test_an_empty_role_is_not_a_role(tmp_path):
    path = tmp_path / T.TOPOLOGY_FILENAME
    path.write_text(json.dumps({"roles": {"inflow": []},
                                "liquid_boundary_order": ["inflow"],
                                "liquid_boundary_prescribes": ["flowrate"]}))
    with pytest.raises(ValueError):
        T.read_topology(str(path))


# --------------------------------------------------------------------------- #
# ``set_boundary_roles``: the op, on a mesh.
# --------------------------------------------------------------------------- #
def _lattice_mesh():
    """A 3x3 lon/lat node lattice, two triangles per square, one boundary loop.

    In lon/lat because a declared FACE is what the chain measured - a section's
    end transect, in the coordinates every other tool speaks - and the op is what
    projects both onto the metres a tolerance is a length in.
    """
    xy = np.array([[x, y] for y in (36.12, 36.13, 36.14)
                   for x in (-75.78, -75.77, -75.76)])
    cells = []
    for row in range(2):
        for col in range(2):
            a = row * 3 + col
            cells += [[a, a + 1, a + 4], [a, a + 4, a + 3]]
    return Mesh(points=xy, cells=np.asarray(cells, dtype=np.int64),
                crs_authid="EPSG:4326")


def test_the_op_writes_the_declared_runs_onto_the_mesh_it_was_handed():
    mesh = _lattice_mesh()
    west = {"type": "LineString",
            "coordinates": [[-75.78, 36.12], [-75.78, 36.14]]}
    roled = P.set_boundary_roles(mesh, inflow=west)
    nodes = roled.meta["boundary_roles"]["inflow"]
    assert set(nodes) == {0, 3, 6}
    assert roled.bed is None and roled.points is mesh.points


def test_a_face_the_mesh_never_reaches_refuses_rather_than_going_unprescribed():
    mesh = _lattice_mesh()
    far = {"type": "LineString", "coordinates": [[-70.0, 36.12], [-70.0, 36.14]]}
    with pytest.raises(MeshToolError) as excinfo:
        P.set_boundary_roles(mesh, inflow=far)
    assert excinfo.value.error_code == "MESH_BOUNDARY_ROLE_UNMATCHED"


def test_a_role_declared_as_two_ends_is_read_as_the_transect_between_them():
    mesh = _lattice_mesh()
    roled = P.set_boundary_roles(
        mesh, outflow=[(-75.76, 36.12), (-75.76, 36.14)])
    assert set(roled.meta["boundary_roles"]["outflow"]) == {2, 5, 8}


def test_a_role_named_by_one_point_is_not_a_face():
    mesh = _lattice_mesh()
    with pytest.raises(MeshToolError) as excinfo:
        P.set_boundary_roles(mesh, outflow=[(-75.76, 36.12)])
    assert excinfo.value.error_code == "MESH_BOUNDARY_ROLE_INVALID"


def test_declaring_no_roles_imposes_nothing():
    mesh = _lattice_mesh()
    assert P.set_boundary_roles(mesh) is mesh


def test_a_mesh_whose_cells_the_engine_realizes_has_no_walk_to_name_a_run_of():
    bare = Mesh(points=None, cells=None, crs_authid="EPSG:32617")
    with pytest.raises(MeshToolError) as excinfo:
        P.set_boundary_roles(bare, inflow=[(-75.78, 36.12), (-75.78, 36.14)])
    assert excinfo.value.error_code == "MESH_ROLES_UNSEGMENTABLE"


# --------------------------------------------------------------------------- #
# The contiguous-run matcher the op IS: a role is a RUN of one contour.
# --------------------------------------------------------------------------- #
#: A 200 m x 40 m strip of boundary nodes: the two 40 m end caps are the
#: transects a section cut, and the two long sides are the water between them.
_STRIP = np.array(
    [[0.0, 0.0], [0.0, 20.0], [0.0, 40.0],          # 0,1,2  west cap
     [100.0, 0.0], [100.0, 40.0],                   # 3,4    sides
     [200.0, 0.0], [200.0, 20.0], [200.0, 40.0]])   # 5,6,7  east cap
#: The strip's boundary walked as ONE closed contour, which is the shape a role
#: is resolved against: up the west cap, along the north side, down the east cap,
#: back along the south side.
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

    return {role: [_shape(geometry)] for role, geometry in faces.items()}


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
    """Each end cap is one role, whole; the sides between them are neither."""
    roles = P._runs(
        _STRIP, _STRIP_CONTOUR, _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE),
        tolerance_m=20.0)
    assert roles == {"inflow": [[0, 1, 2]], "outflow": [[5, 6, 7]]}


def test_a_face_that_ends_nowhere_near_the_boundary_carries_no_role():
    """A face and a mesh that describe different domains match nothing.

    The empty slot STAYS, one per declared face, which is what lets the refusal
    name which of a role's faces found no boundary to lie on."""
    roles = P._runs(_STRIP, [[3, 4]],
                    _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE),
                    tolerance_m=20.0)
    assert roles == {"inflow": [[]], "outflow": [[]]}


def test_a_cut_corner_does_not_cost_the_face_its_role():
    """A triangulator conforms along a polygon's sides and cuts its corners, so
    the anchors are the NEAREST nodes rather than nodes inside a tolerance: the
    end caps here are chamfered well past one mean boundary edge and the face
    still lands whole."""
    chamfered = np.array(
        [[8.0, 0.0], [0.0, 12.0], [0.0, 28.0], [8.0, 40.0],   # 0..3 west cap
         [100.0, 40.0],                                       # 4    north side
         [192.0, 40.0], [200.0, 28.0], [200.0, 12.0],         # 5..7 east cap
         [192.0, 0.0], [100.0, 0.0]])                         # 8,9  south side
    roles = P._runs(
        chamfered, [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9]],
        _shapes(inflow=_WEST_FACE, outflow=_EAST_FACE), tolerance_m=20.0)
    # each run is walked from the face's own first end, so a role's list may run
    # either way round the contour; what it may not do is skip a node.
    assert roles == {"inflow": [[0, 1, 2, 3]], "outflow": [[8, 7, 6, 5]]}


def test_a_mesh_with_no_declared_boundaries_carries_no_roles():
    """Nothing is inferred: an undeclared boundary is entirely solid wall, which
    is what makes a deck against it refuse rather than solve on a guess."""
    assert P._runs(_STRIP, _STRIP_CONTOUR, {}, tolerance_m=20.0) == {}


def test_a_scattered_candidate_boundary_resolves_into_two_contiguous_runs():
    """The holes the measured scatter left are INSIDE the declared stretch."""
    points = _ring_on_a_circle(len(_SCATTER))
    roles = P._runs(
        points, [list(range(len(_SCATTER)))],
        _shapes(inflow=_face_across(points, 15, 3),
                outflow=_face_across(points, 7, 11)),
        tolerance_m=1.0)
    assert roles == {"inflow": [[15, 16, 17, 18, 0, 1, 2, 3]],
                     "outflow": [[7, 8, 9, 10, 11]]}
    size = len(_SCATTER)
    for runs in roles.values():
        for run in runs:
            assert all((b - a) % size == 1 for a, b in zip(run, run[1:]))


def test_a_run_that_wraps_the_contours_origin_stays_one_run():
    """A contour has no first node; a stretch across position zero is not two."""
    points = _ring_on_a_circle(len(_SCATTER))
    roles = P._runs(points, [list(range(len(_SCATTER)))],
                    _shapes(inflow=_face_across(points, 15, 3)), tolerance_m=1.0)
    assert roles["inflow"][0][0] == 15 and roles["inflow"][0][-1] == 3
    assert 0 in roles["inflow"][0]


def test_the_matcher_reproduces_the_probes_forced_contiguous_result():
    """The hand-closed runs that produced two liquid boundaries, CONSTRUCTED."""
    points = _ring_on_a_circle(len(_SCATTER))
    roles = P._runs(
        points, [list(range(len(_SCATTER)))],
        _shapes(inflow=_face_across(points, 15, 3),
                outflow=_face_across(points, 7, 11)),
        tolerance_m=1.0)
    assert roles["inflow"][0] == _forced_contiguous(_SCATTER, "I")
    assert roles["outflow"][0] == _forced_contiguous(_SCATTER, "O")


def test_a_point_declared_role_is_the_run_it_stands_within():
    """A catchment outlet names a point; what it names on the mesh is a stretch,
    and the stretch stops at the first node past the tolerance rather than
    picking up a node on the far side of the domain."""
    points = _ring_on_a_circle(12, radius=100.0)
    outlet = {"type": "Point", "coordinates": list(points[0])}
    spacing = float(np.hypot(*(points[1] - points[0])))
    roles = P._runs(points, [list(range(12))], _shapes(outflow=outlet),
                    tolerance_m=spacing * 1.2)
    assert roles == {"outflow": [[11, 0, 1]]}


def test_one_role_declared_across_two_faces_lands_as_two_sections():
    """A two-mouth estuary has ONE open boundary in TWO sections.

    A role that could name only one face made the second mouth a wall - and a
    list of faces was read as a list of coordinates and died on an index.
    """
    from shapely.geometry import shape as _shape

    points = _ring_on_a_circle(20)
    roles = P._runs(
        points, [list(range(20))],
        {"open": [_shape(_face_across(points, 1, 4)),
                  _shape(_face_across(points, 11, 14))]},
        tolerance_m=1.0)
    assert roles == {"open": [[1, 2, 3, 4], [11, 12, 13, 14]]}


def test_the_two_sections_reach_the_mesh_as_one_role_and_a_counted_pair():
    """Through the op: the union carries the role, the count says how many."""
    mesh = _lattice_mesh()
    south = {"type": "LineString",
             "coordinates": [[-75.78, 36.12], [-75.76, 36.12]]}
    north = {"type": "LineString",
             "coordinates": [[-75.78, 36.14], [-75.76, 36.14]]}
    roled = P.set_boundary_roles(mesh, open=[south, north])
    assert set(roled.meta["boundary_roles"]["open"]) == {0, 1, 2, 6, 7, 8}
    assert roled.meta["boundary_role_runs"] == {"open": 2}


def test_a_node_two_declared_faces_both_claim_refuses():
    """A node carries ONE boundary condition; overlapping faces are a mistake."""
    mesh = _lattice_mesh()
    west = {"type": "LineString",
            "coordinates": [[-75.78, 36.12], [-75.78, 36.14]]}
    corner = {"type": "LineString",
              "coordinates": [[-75.78, 36.14], [-75.76, 36.14]]}
    with pytest.raises(MeshToolError) as excinfo:
        P.set_boundary_roles(mesh, inflow=west, outflow=corner)
    assert excinfo.value.error_code == "MESH_BOUNDARY_ROLE_CONFLICT"


def test_a_whole_rim_declaration_opens_the_whole_rim():
    """The domain's own outline names every boundary node, not a corner of it."""
    mesh = _lattice_mesh()
    rim = {"type": "Polygon", "coordinates": [[
        [-75.78, 36.12], [-75.76, 36.12], [-75.76, 36.14],
        [-75.78, 36.14], [-75.78, 36.12]]]}
    roled = P.set_boundary_roles(mesh, open=rim)
    assert set(roled.meta["boundary_roles"]["open"]) == {0, 1, 2, 3, 5, 6, 7, 8}
    assert roled.meta["boundary_role_runs"] == {"open": 1}


def test_the_refusal_names_WHICH_of_a_roles_faces_found_no_boundary():
    mesh = _lattice_mesh()
    west = {"type": "LineString",
            "coordinates": [[-75.78, 36.12], [-75.78, 36.14]]}
    far = {"type": "LineString", "coordinates": [[-70.0, 36.12], [-70.0, 36.14]]}
    with pytest.raises(MeshToolError) as excinfo:
        P.set_boundary_roles(mesh, open=[west, far])
    assert excinfo.value.error_code == "MESH_BOUNDARY_ROLE_UNMATCHED"
    assert "open[1]" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# ``set_bed``: the CORRECT DATA CLASS, and the substitution said out loud.
# --------------------------------------------------------------------------- #
def _bed_raster(tmp_path, value=-18.0):
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "topobathy.tif"
    with rasterio.open(path, "w", driver="GTiff", height=20, width=20, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-75.80, 36.20, 0.01, 0.01)) as dst:
        dst.write(np.full((20, 20), value, dtype="float32"), 1)
    return path


def _lonlat_mesh():
    xy = np.array([[-75.78, 36.12], [-75.74, 36.12],
                   [-75.78, 36.16], [-75.74, 36.16]])
    return Mesh(points=xy, cells=np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64),
                crs_authid="EPSG:4326")


def test_the_bed_is_painted_at_the_nodes_and_the_row_it_came_from_is_named(tmp_path):
    raster = _bed_raster(tmp_path)
    bedded = P.set_bed(_lonlat_mesh(), source=str(raster))
    assert bedded.bed.tolist() == [-18.0] * 4
    assert str(raster) in bedded.meta["bed_source"]
    row = [r for r in bedded.meta["synthetic_inputs"] if r["param"] == "mesh_bed"]
    assert row and str(raster) in row[0]["value"]
    assert row[0]["consequence"] == "physics"


def test_the_interpolation_is_a_visible_default_off_a_declared_roster(tmp_path):
    raster = _bed_raster(tmp_path)
    import inspect

    assert inspect.signature(P.set_bed).parameters["interp"].default == "nearest"
    assert P.set_bed(_lonlat_mesh(), source=str(raster),
                     interp="bilinear").bed == pytest.approx([-18.0] * 4)
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lonlat_mesh(), source=str(raster), interp="kriging")
    assert excinfo.value.error_code == "MESH_OP_BAD_VALUE"


def test_a_conditioning_this_primitive_does_not_perform_refuses(tmp_path):
    raster = _bed_raster(tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lonlat_mesh(), source=str(raster), condition="smooth")
    assert excinfo.value.error_code == "MESH_OP_BAD_VALUE"


def test_a_declared_pit_fill_runs_the_delineators_own_chain_and_says_so(
        monkeypatch, tmp_path):
    """An overland run's bed must carry the same sinks its routing does, or the
    deepest water in the run is a pit the delineation already filled."""
    raster = _bed_raster(tmp_path)
    seen: dict = {}

    def _fake_condition(src, dst):
        seen["src"] = src
        import shutil

        shutil.copyfile(src, dst)
        return dst

    monkeypatch.setattr(
        "trid3nt_server.tools.processing._hydrology_common.write_conditioned_dem",
        _fake_condition)
    bedded = P.set_bed(_lonlat_mesh(), source=str(raster), condition="pit_fill")
    assert seen["src"] == str(raster)
    assert "pit-filled" in bedded.meta["bed_source"]


def test_a_source_naming_nothing_refuses_rather_than_leaving_a_bedless_mesh():
    with pytest.raises(MeshToolError) as excinfo:
        P.set_bed(_lonlat_mesh(), source="")
    assert excinfo.value.error_code == "MESH_BED_UNRESOLVED"


def test_the_bed_op_permits_no_ladder_rung_on_the_authors_behalf(tmp_path):
    """Which substitutions a bed tolerates is the DATA row's declaration.

    A rung this op permitted would be a cross-dataset bed nobody wrote down, and
    the fetch would descend to it without the recipe ever saying so.
    """
    import inspect

    source = inspect.getsource(P._bed_raster)
    assert "fallback=" not in source
    assert 'TOOL_REGISTRY[name].fn(bbox=bbox, target_crs="EPSG:4326")' in source


def test_the_substitution_the_fetch_narrated_rides_under_one_name(tmp_path):
    """One datum, one name: the note the bed's fetch attached is what the deck
    reads as ``bed_fallback_note``, all the way from the op to the provenance."""
    raster = _bed_raster(tmp_path)
    bedded = P.set_bed(_lonlat_mesh(), source=str(raster))
    assert "bed_fallback_note" not in bedded.meta  # a direct raster substitutes nothing

    import inspect

    from trid3nt_server.workflows.mesh import session as S

    assert "bed_fallback_note" in inspect.getsource(S.MeshSession.accept)
    assert 'mesh.meta.get("bed_fallback_note")' in inspect.getsource(S.MeshSession)


def test_the_journal_names_the_rung_that_ACTUALLY_painted_the_bed(tmp_path):
    """One datum, one name, in BOTH records.

    The accepted artifact's provenance names the rung that served. A reader with
    only the journal beside the mesh files would otherwise see the row the recipe
    ASKED for and no sign of the substitution that answered it, so the line for
    the mesh standing now carries the same measured statement.
    """
    from trid3nt_server.workflows.mesh.recipe import build_recipe
    from trid3nt_server.workflows.mesh.session import MeshSession

    session = MeshSession(
        build_recipe(mesher="reg_grid", extent=(-75.80, 36.10, -75.70, 36.20),
                     resolution_m=100.0, ops=()),
        workdir=tmp_path)
    assert "bed_source" not in session.recipe_lines()[-1]
    painted = "fetch_topobathy: cudem_nearshore 89%, etopo_bathy_base 11%"
    session._mesh = Mesh(points=None, cells=None, crs_authid="EPSG:4326",
                         meta={"bed_source": painted})
    assert session.recipe_lines()[-1]["bed_source"] == painted


def test_the_bed_is_fetched_past_the_extent_the_mesh_has_nodes_on():
    grown = P._grown((-75.80, 36.10, -75.70, 36.20))
    assert grown[0] < -75.80 and grown[1] < 36.10
    assert grown[2] > -75.70 and grown[3] > 36.20


# --------------------------------------------------------------------------- #
# The bed's provenance, in whichever shape the fetch answered.
# --------------------------------------------------------------------------- #
class _Row:
    def __init__(self, rung, coverage):
        self.rung = rung
        self.coverage = coverage


class _Layer:
    def __init__(self, rows, note=None):
        self.uri = "s3://bucket/bed.tif"
        self.fallbacks = rows
        self.fallback_note = note


_ROWS_TYPED = [_Row("cudem_nearshore", 0.89), _Row("etopo_bathy_base", 0.11),
               _Row("unused_rung", 0.0)]
_ROWS_DICT = [{"rung": "cudem_nearshore", "coverage": 0.89},
              {"rung": "etopo_bathy_base", "coverage": 0.11},
              {"rung": "unused_rung", "coverage": 0.0}]


def test_the_activation_rows_read_the_same_from_a_layer_and_from_a_dict():
    from trid3nt_server.workflows.mesh.meshers import (
        fetch_activation_rows,
        fetch_fallback_note,
    )

    typed = fetch_activation_rows(_Layer(_ROWS_TYPED, "swapped"))
    mapping = fetch_activation_rows(
        {"uri": "s3://b/x.tif", "fallbacks": _ROWS_DICT,
         "fallback_note": "swapped"})
    assert typed == mapping == [("cudem_nearshore", 0.89),
                                ("etopo_bathy_base", 0.11)]
    assert fetch_fallback_note({"fallback_note": "swapped"}) == "swapped"
    assert fetch_fallback_note({"fallback_note": None}) is None


def test_a_dict_shaped_fetch_is_not_reported_as_unmeasured():
    """A fetcher may answer with the layer as a mapping; reading only attributes
    calls a MEASURED provenance unmeasured."""
    as_dict = {"uri": "s3://b/x.tif", "fallbacks": _ROWS_DICT,
               "fallback_note": None}
    assert P._provenance("fetch_topobathy", as_dict) == (
        "fetch_topobathy: cudem_nearshore 89%, etopo_bathy_base 11%")
    assert "UNMEASURED" not in P._provenance("fetch_topobathy", as_dict)


def test_a_fetch_that_measured_nothing_still_says_so():
    empty = {"uri": "s3://b/x.tif", "fallbacks": [], "fallback_note": None}
    assert "UNMEASURED" in P._provenance("fetch_topobathy", empty)


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


def test_a_shuffled_flowline_normalizes_to_the_same_line():
    """The rows arrive in whatever order the navigate listed them; the reading is
    one head-to-tail line either way."""
    head = (-83.40, 35.00)
    straight = read_centerline_utm(_flowline_collection([0, 1, 2]), 32617,
                                   start_lonlat=head)
    shuffled = read_centerline_utm(_flowline_collection([2, 0, 1]), 32617,
                                   start_lonlat=head)
    assert np.allclose(straight, shuffled)


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
