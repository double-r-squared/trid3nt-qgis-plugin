"""Offline tests for the FloPy facade ``triangle`` / ``voronoi`` / ``gridgen``.

It builds in a container, so what runs here is everything AROUND that boundary:
the three registrations, the typed refusals, the CONFIG the box is handed (the
domain projected to the mesh's own UTM zone, geometry-valued op kwargs staged
beside it), the neutral mesh read back, and the driver's own pure surface - the
normalisation, the signature binding and the refusals only knowable where the
library is. Only the container call and the world-reads are stubbed."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.tools.mesh.meshers import (
    POST,
    PRE,
    MeshToolError,
    get_mesher,
    op_names,
    registered_meshers,
    resolve_op,
)
from trid3nt_server.tools.mesh.meshers import flopy_grid as FLOPY
from trid3nt_server.tools.mesh.tool import MeshTool, mesh_op

_AOI = (-75.80, 36.10, -75.70, 36.20)

#: The DOMAIN every recipe here is cut from, in lon/lat.
_DOMAIN = {"type": "Polygon", "coordinates": [[
    [-75.79, 36.11], [-75.71, 36.11], [-75.71, 36.19], [-75.79, 36.19],
    [-75.79, 36.11]]]}

#: Four nodes, two triangles, in UTM metres - the shape the box returns.
_POINTS = np.array([[500000.0, 3880000.0], [500100.0, 3880000.0],
                    [500000.0, 3880100.0], [500100.0, 3880100.0]])
_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)

#: The stats the box writes back, with every key the readback lifts.
_STATS = {"mesher": "triangle", "library": "flopy 3.10.0",
          "element_count": 2, "node_count": 4, "crs_epsg": 32617,
          "resolution_m": 60.0, "domain_source": "domain polygon (1 part(s))",
          "ops": [],
          "maximum_area": 3600.0, "maximum_area_source": "resolution_m squared",
          "probes": {"library": "flopy 3.10.0", "index_base": "0",
                     "dropped_degenerate_cells": 0,
                     "cell_vertex_counts": {"3": 2}}}


def _recipe(**over):
    ask = {"mesher": "triangle", "extent": json.dumps(_DOMAIN),
           "resolution_m": 60.0, "ops": []}
    ask.update(over)
    return MeshTool.build_mesh(**ask)


def test_the_roster_names_all_five_meshers():
    assert registered_meshers() == ("gridgen", "om2d", "reg_grid",
                                    "triangle", "voronoi")


def test_the_triangle_names_are_triangles_own_and_phase_derived():
    for name in ("add_polygon", "add_hole", "add_region"):
        space, fn = resolve_op(get_mesher("triangle"), name)
        assert (space.origin, space.phase) == ("triangle", PRE)
        assert fn is None, "the library lives in the image, not in this process"
        space, _ = resolve_op(get_mesher("voronoi"), name)
        assert (space.origin, space.phase) == ("triangle", PRE)


def test_the_gridgen_names_are_gridgens_own():
    for name in ("add_active_domain", "add_refinement_features"):
        space, fn = resolve_op(get_mesher("gridgen"), name)
        assert (space.origin, space.phase) == ("gridgen", PRE)
        assert fn is None


def test_the_shared_primitives_ride_along_for_these_meshers_too():
    for mesher in ("triangle", "voronoi", "gridgen"):
        space, fn = resolve_op(get_mesher(mesher), "set_bed")
        assert (space.origin, space.phase) == ("primitives", POST)
        assert callable(fn), "a primitive binds against its REAL signature"


def test_the_build_params_are_each_constructors_own_keywords():
    assert get_mesher("triangle").build_params == (
        "maximum_area", "angle", "nodes", "additional_args")
    assert get_mesher("voronoi").build_params == (
        "maximum_area", "angle", "nodes", "additional_args")
    assert get_mesher("gridgen").build_params == (
        "delr", "delc", "xoff", "yoff", "angrot")


def test_none_of_these_meshers_has_an_implicit_default_program():
    for mesher in ("triangle", "voronoi", "gridgen"):
        assert get_mesher(mesher).default_ops == ()


def test_an_unknown_op_refuses_with_the_nearest_names():
    with pytest.raises(MeshToolError) as excinfo:
        _recipe(ops=[mesh_op("add_polyg")])
    assert excinfo.value.error_code == "MESH_OP_UNKNOWN"
    assert "add_polygon" in str(excinfo.value)


def test_an_op_another_flopy_mesher_owns_does_not_cross():
    with pytest.raises(MeshToolError) as excinfo:
        MeshTool.build_mesh(mesher="gridgen", extent=_AOI, resolution_m=60.0,
                            ops=[mesh_op("add_polygon")])
    assert excinfo.value.error_code == "MESH_OP_UNKNOWN"


def test_a_build_word_the_constructor_never_took_is_refused():
    with pytest.raises(MeshToolError) as excinfo:
        _recipe(build={"bogus": 1})
    assert excinfo.value.error_code == "MESH_BUILD_UNKNOWN_PARAM"


def _stub(monkeypatch, tmp_path, *, stats=None, ragged=False):
    """Answer the container calls with a known mesh, and record what was sent."""
    sent: dict[str, object] = {}

    def fake_run_op(rundir, op, config_name, produces):
        config = json.loads(Path(rundir, config_name).read_text())
        sent["config"] = config
        if ragged:
            np.savez(Path(rundir, "flopy_mesh.npz"), points=_POINTS,
                     cells_flat=np.array([0, 1, 2, 1, 3, 2, 0, 2, 3, 1, 2, 0],
                                         dtype=np.int64),
                     cell_counts=np.array([3, 3, 3, 3], dtype=np.int64))
        else:
            np.savez(Path(rundir, "flopy_mesh.npz"), points=_POINTS,
                     cells=_CELLS)
        Path(rundir, "flopy_stats.json").write_text(
            json.dumps(stats if stats is not None else _STATS))

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(FLOPY, "_run_op", fake_run_op)
    return sent


def test_the_domain_reaches_the_box_projected_to_the_meshes_own_zone(
        monkeypatch, tmp_path):
    """Triangle is planar: the rings travel in UTM metres, not in lon/lat."""
    sent = _stub(monkeypatch, tmp_path)
    mesh = FLOPY.build(_recipe())
    config = sent["config"]
    assert config["mesher"] == "triangle"
    easting, northing = config["outer_rings"][0][0]
    assert 100_000 < easting < 900_000 and northing > 3_000_000
    assert config["crs_epsg"] == mesh.meta["utm_epsg"]
    assert config["domain_source"] == "domain polygon (1 part(s))"
    assert mesh.crs_authid.startswith("EPSG:326")


def test_a_box_extent_is_a_domain_for_these_meshers(monkeypatch, tmp_path):
    """A rectangular aquifer is a rectangle: unlike om2d, the box needs no coast."""
    sent = _stub(monkeypatch, tmp_path)
    FLOPY.build(_recipe(extent=_AOI))
    assert sent["config"]["domain_source"].startswith("extent box")
    assert len(sent["config"]["outer_rings"]) == 1
    assert sent["config"]["holes"] == []


def test_an_unstated_maximum_area_is_the_size_word_squared(monkeypatch, tmp_path):
    sent = _stub(monkeypatch, tmp_path)
    mesh = FLOPY.build(_recipe(resolution_m=60.0))
    assert sent["config"]["maximum_area"] == 3600.0
    assert sent["config"]["maximum_area_source"] == "resolution_m squared"
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["maximum_area_m2"] == 3600.0
    assert provenance["maximum_area_source"] == "resolution_m squared"


def test_a_stated_maximum_area_travels_verbatim_and_says_it_is_stated(
        monkeypatch, tmp_path):
    sent = _stub(monkeypatch, tmp_path)
    FLOPY.build(_recipe(build={"maximum_area": 100.0, "angle": 27.5}))
    assert sent["config"]["maximum_area"] == 100.0
    assert sent["config"]["maximum_area_source"] == "stated"
    assert sent["config"]["angle"] == 27.5


def test_a_geometry_valued_op_kwarg_is_staged_projected_beside_the_domain(
        monkeypatch, tmp_path):
    """Code as data: the op name travels verbatim, its geometry in the same
    metre frame the domain was cut in."""
    sent = _stub(monkeypatch, tmp_path)
    FLOPY.build(_recipe(ops=[mesh_op("add_region", point=_DOMAIN,
                                     maximum_area=900.0)]))
    entry, = sent["config"]["ops"]
    assert entry["fn"] == "add_region"
    assert entry["kwargs"]["point"] == "/data/op0_point.geojson"
    assert entry["kwargs"]["maximum_area"] == 900.0
    staged, = tmp_path.rglob("op0_point.geojson")
    ring = json.loads(staged.read_text())["coordinates"][0]
    easting, northing = ring[0]
    assert 100_000 < easting < 900_000 and northing > 3_000_000


def test_the_gridgen_base_is_the_extent_through_the_size_word(monkeypatch,
                                                              tmp_path):
    """No delr/delc stated: the extent and resolution lay the rectilinear base,
    and the box reports where each word came from."""
    sent = _stub(monkeypatch, tmp_path, stats={
        **_STATS, "mesher": "gridgen",
        "sources": {"delr": "resolution_m", "delc": "resolution_m",
                    "xoff": "extent", "yoff": "extent"}})
    mesh = FLOPY.build(_recipe(mesher="gridgen", extent=_AOI))
    config = sent["config"]
    assert config["delr"][0] == config["delc"][0] == 60.0
    assert len(config["delr"]) > 1 and len(config["delc"]) > 1
    assert config["sources"]["delr"] == "resolution_m"
    assert config["sources"]["xoff"] == "extent"
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["build_sources"]["delc"] == "resolution_m"


def test_gridgens_stated_words_travel_verbatim(monkeypatch, tmp_path):
    sent = _stub(monkeypatch, tmp_path)
    FLOPY.build(_recipe(mesher="gridgen", extent=_AOI,
                        build={"delr": 50.0, "xoff": 400000.0}))
    config = sent["config"]
    assert config["delr"] == [50.0] * len(config["delr"])
    assert config["xoff"] == 400000.0
    assert config["sources"]["delr"] == "stated"
    assert config["sources"]["xoff"] == "stated"


def test_the_readback_carries_the_boxes_own_probes_and_provenance(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    mesh = FLOPY.build(_recipe())
    assert mesh.node_count == 4 and mesh.element_count == 2
    assert mesh.meta["probes"]["index_base"] == "0"
    assert mesh.meta["probes"]["cell_vertex_counts"] == {"3": 2}
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["mesher_library"] == "flopy 3.10.0"
    assert provenance["mesher"] == "triangle"
    assert mesh.meta["synthetic_inputs"][0] == {
        "param": "resolution_m", "value": 60.0, "units": "m", "basis": "user"}
    assert mesh.meta["synthetic_inputs"][1]["value"] == "4 nodes / 2 elements"


def test_a_ragged_readback_is_cells_of_their_own_lengths(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path, ragged=True)
    mesh = FLOPY.build(_recipe())
    assert mesh.element_count == 4
    assert [len(cell) for cell in mesh.cells] == [3, 3, 3, 3]


def test_the_drivers_own_refusal_reaches_the_caller_with_its_escalation(tmp_path):
    (tmp_path / "flopy_refusal.json").write_text(json.dumps({
        "code": "MESH_HOLE_HAS_NO_INTERIOR",
        "message": "this hole ring encloses no unmeshed area of its own ...",
        "escalation": {"tool": "draw_extent"}}))
    with pytest.raises(MeshToolError) as excinfo:
        FLOPY._refusal(tmp_path)
    assert excinfo.value.error_code == "MESH_HOLE_HAS_NO_INTERIOR"
    assert excinfo.value.escalation["tool"] == "draw_extent"


def test_a_recipe_with_no_extent_refuses_before_anything_is_staged():
    with pytest.raises(MeshToolError) as excinfo:
        FLOPY.build(_recipe(extent=None))
    assert excinfo.value.error_code == "MESH_EXTENT_MISSING"


def test_an_extent_that_is_no_polygon_refuses_by_naming_the_outline(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        FLOPY.build(_recipe(extent={"type": "LineString",
                                    "coordinates": [[-75.78, 36.12],
                                                    [-75.74, 36.16]]}))
    assert excinfo.value.error_code == "MESH_DOMAIN_NOT_A_POLYGON"
    assert "closed outline" in str(excinfo.value)


def test_a_projected_extent_is_named_for_what_it_is(monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        FLOPY.build(_recipe(extent=json.dumps({
            "type": "Polygon", "coordinates": [[
                [430000.0, 3990000.0], [440000.0, 3990000.0],
                [440000.0, 4000000.0], [430000.0, 4000000.0],
                [430000.0, 3990000.0]]]})))
    assert excinfo.value.error_code == "MESH_DOMAIN_NOT_LONLAT"
    assert "projected" in str(excinfo.value)


def test_no_size_word_and_no_raster_is_asked_for_by_name():
    with pytest.raises(MeshToolError) as excinfo:
        FLOPY.build(_recipe(resolution_m=None, ops=[]))
    assert excinfo.value.error_code == "MESH_EDGE_UNSTATED"


def test_the_scripts_live_in_the_worker_tree_beside_their_dockerfiles():
    from trid3nt_server.workflows.solver.image_script import scripts_dir

    assert FLOPY._INCONTAINER_SCRIPT == "flopy_grid.py"
    assert (scripts_dir("mesh") / "flopy_grid.py").exists()


def test_the_box_is_shelled_with_a_named_op(monkeypatch, tmp_path):
    from trid3nt_server.workflows.solver import image_script

    seen: dict[str, object] = {}

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        Path(tmp_path, "flopy_mesh.npz").write_text("{}")
        return _Done()

    monkeypatch.setattr(image_script.subprocess, "run", fake_run)
    FLOPY._run_op(tmp_path, "build", "flopy_config.json", "flopy_mesh.npz")
    argv = seen["argv"]
    assert argv[-4:] == ["/drivers/flopy_grid.py", "build",
                         "/data/flopy_config.json", "/data"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"


def test_a_failed_box_without_a_refusal_document_is_a_build_failure(
        monkeypatch, tmp_path):
    import subprocess

    from trid3nt_server.workflows.solver import image_script

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 9, "", "exec not found")

    monkeypatch.setattr(image_script.subprocess, "run", fake_run)
    with pytest.raises(MeshToolError) as excinfo:
        FLOPY._run_op(tmp_path, "build", "flopy_config.json", "flopy_mesh.npz")
    assert excinfo.value.error_code == "MESH_BUILD_FAILED"
    assert "flopy_grid mesher build failed" in str(excinfo.value)


def _post_bed(monkeypatch, value=-4.0):
    import dataclasses

    def fake_set_bed(mesh, source, interp="nearest", condition=None):
        return dataclasses.replace(mesh, bed=np.full(mesh.node_count, value))

    monkeypatch.setattr(
        "trid3nt_server.tools.mesh.shared.primitives.set_bed", fake_set_bed)


def test_a_primitive_after_the_build_runs_over_the_mesh_that_came_back(
        monkeypatch, tmp_path):
    _stub(monkeypatch, tmp_path)
    _post_bed(monkeypatch)
    mesh = FLOPY.build(_recipe(ops=[mesh_op("set_bed", source="fetch_cudem")]))
    assert mesh.has_bed and mesh.bed.tolist() == [-4.0] * 4


def _driver():
    """The in-container driver, imported here - it needs only numpy and shapely."""
    import importlib.util

    from trid3nt_server.workflows.solver.image_script import scripts_dir

    spec = importlib.util.spec_from_file_location(
        "_flopy_driver_under_test", scripts_dir("mesh") / "flopy_grid.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def _exes_on_path(monkeypatch, tmp_path):
    """Executable stand-ins on PATH: FloPy resolves the exe name at CONSTRUCTION
    and never runs it in the tests that only register rings."""
    for name in ("triangle", "gridgen"):
        stub = tmp_path / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")


def test_the_driver_flips_clockwise_cells_and_drops_the_closing_vertex():
    driver = _driver()
    points, cells, norm = driver._neutenize(
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
        [[0, 3, 2, 1], [0, 1, 2, 0]])
    assert norm["index_base"] == "0"
    assert norm["dropped_degenerate_cells"] == 0
    assert [len(cell) for cell in cells] == [4, 3]
    for cell in cells:
        xy = points[cell]
        area = 0.5 * np.sum(
            xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1])
        assert area > 0.0, "every cell is counter-clockwise"


def test_the_driver_normalises_one_based_indices_and_says_so():
    driver = _driver()
    points, cells, norm = driver._neutenize(
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]], [[1, 2, 3]])
    assert cells == [[0, 1, 2]]
    assert norm["index_base"] == "normalised from 1"


def test_an_index_that_is_neither_base_refuses_with_the_extent_of_the_error():
    driver = _driver()
    with pytest.raises(driver._Refusal) as excinfo:
        driver._neutenize([[0.0, 0.0], [10.0, 0.0]], [[0, 9]])
    assert excinfo.value.document["code"] == "MESH_CELL_INDEX_OUT_OF_RANGE"


def test_a_build_where_every_cell_is_degenerate_refuses_as_empty():
    driver = _driver()
    with pytest.raises(driver._Refusal) as excinfo:
        driver._neutenize([[0.0, 0.0], [10.0, 0.0], [5.0, 0.0]],
                          [[0, 1, 2]])
    assert excinfo.value.document["code"] == "MESH_BUILD_EMPTY"
    assert "no usable cells" in excinfo.value.document["message"]


def test_an_unreferenced_node_drops_from_the_mesh_without_a_count():
    driver = _driver()
    points, cells, _ = driver._neutenize(
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [99.0, 99.0]],
        [[0, 1, 2]])
    assert points.shape[0] == 3 and cells == [[0, 1, 2]]


def test_a_degenerate_cell_is_dropped_and_counted():
    driver = _driver()
    points, cells, norm = driver._neutenize(
        [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [20.0, 0.0]],
        [[0, 1, 2], [0, 1, 3]])
    assert cells == [[0, 1, 2]]
    assert norm["dropped_degenerate_cells"] == 1


def test_a_kwarg_the_librarys_method_never_took_refuses_by_signature():
    driver = _driver()

    def add_region(point, attribute=0, maximum_area=None):
        return None

    with pytest.raises(ValueError) as excinfo:
        driver._bind_call(add_region, {"point": (1.0, 2.0), "bogus": 1})
    assert "bogus" in str(excinfo.value)


def test_a_staged_geometry_that_is_empty_or_unreadable_is_refused(tmp_path):
    driver = _driver()
    missing = str(tmp_path / "nope.geojson")
    with pytest.raises(driver._Refusal) as excinfo:
        driver._load_geoms(missing)
    assert excinfo.value.document["code"] == "MESH_OP_GEOMETRY_EMPTY"

    empty = tmp_path / "empty.geojson"
    empty.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    with pytest.raises(driver._Refusal) as excinfo:
        driver._load_geoms(str(empty))
    assert excinfo.value.document["code"] == "MESH_OP_GEOMETRY_EMPTY"


def test_the_driver_reads_a_bare_geometry_as_well_as_a_feature_collection(tmp_path):
    driver = _driver()
    bare = tmp_path / "bare.geojson"
    bare.write_text(json.dumps({"type": "Polygon", "coordinates": [[
        [0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 0.0]]]}))
    geoms, = driver._load_geoms(str(bare))
    assert geoms.geom_type == "Polygon"


def test_a_hole_staged_as_a_bare_point_refuses_by_explaining_the_flood(
        tmp_path, _exes_on_path):
    """A point hole empties every triangle reaching it - the whole domain."""
    driver = _driver()
    point = tmp_path / "point.geojson"
    point.write_text(json.dumps({"type": "Point",
                                 "coordinates": [500000.0, 3880050.0]}))
    builder = driver._TriangleBuild(
        {"outer_rings": [[[500000.0, 3880000.0], [500100.0, 3880000.0],
                          [500100.0, 3880100.0], [500000.0, 3880100.0]]],
         "holes": []}, str(tmp_path))
    with pytest.raises(driver._Refusal) as excinfo:
        builder.entry({"fn": "add_hole", "kwargs": {"hole": str(point)}})
    assert excinfo.value.document["code"] == "MESH_OP_HOLE_NOT_ENCLOSING"


def test_a_hole_that_encloses_no_interior_of_its_own_refuses(
        tmp_path, _exes_on_path):
    driver = _driver()
    ring = [[500000.0, 3880000.0], [500100.0, 3880000.0],
            [500100.0, 3880100.0], [500000.0, 3880100.0]]
    builder = driver._TriangleBuild(
        {"outer_rings": [ring], "holes": [ring]}, str(tmp_path))
    with pytest.raises(driver._Refusal) as excinfo:
        builder.build()
    assert excinfo.value.document["code"] == "MESH_HOLE_HAS_NO_INTERIOR"


def test_an_island_inside_a_hole_is_ground_not_pocket(tmp_path, _exes_on_path):
    """The pocket point must land between the island and the hole's edge: an
    island nested in a hole is subtracted before the point is chosen, so the
    executable's own hole point never sits on ground."""
    driver = _driver()
    outer = [[500000.0, 3880000.0], [500400.0, 3880000.0],
             [500400.0, 3880400.0], [500000.0, 3880400.0]]
    hole = [[500100.0, 3880100.0], [500300.0, 3880100.0],
            [500300.0, 3880300.0], [500100.0, 3880300.0]]
    island = [[500150.0, 3880150.0], [500250.0, 3880150.0],
              [500250.0, 3880250.0], [500150.0, 3880250.0]]
    builder = driver._TriangleBuild(
        {"outer_rings": [outer, island], "holes": [hole]}, str(tmp_path))
    builder.tri.build = lambda: None          # registration only, no executable
    builder.build()
    (hole_point,) = builder.tri._holes
    from shapely.geometry import Point, Polygon

    assert not Polygon(island).intersects(Point(hole_point)), \
        "the hole point landed on the island"
    assert Polygon(hole).contains(Point(hole_point)), \
        "the hole point must sit inside the hole ring"


def test_the_driver_refuses_an_op_the_mesher_does_not_have(tmp_path,
                                                            _exes_on_path):
    driver = _driver()
    builder = driver._TriangleBuild(
        {"outer_rings": [[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]],
         "holes": []}, str(tmp_path))
    with pytest.raises(ValueError) as excinfo:
        builder.entry({"fn": "smooth_mesh", "kwargs": {}})
    assert "smooth_mesh" in str(excinfo.value)


def test_a_refusal_in_the_box_writes_the_document_and_exits_three(
        monkeypatch, tmp_path):
    driver = _driver()

    def refuses(cfg, out):
        raise driver._Refusal("MESH_BUILD_EMPTY", "every cell was degenerate")

    (tmp_path / "cfg.json").write_text(json.dumps({"mesher": "triangle"}))
    monkeypatch.setitem(driver._OPS, "build", refuses)
    monkeypatch.setattr(sys, "argv", ["flopy_grid.py", "build",
                                      str(tmp_path / "cfg.json"),
                                      str(tmp_path)])
    assert driver.main() == 3
    document = json.loads((tmp_path / "flopy_refusal.json").read_text())
    assert document["code"] == "MESH_BUILD_EMPTY"


def test_gridgen_whose_layers_mesh_apart_refuse_with_the_stated_layers(
        monkeypatch, tmp_path, _exes_on_path):
    """A DISV cell is a prism over every layer, so a partial-layer refinement
    cannot be read back as one grid - the driver names the fix."""
    import flopy.utils.gridgen as gridgen_lib

    driver = _driver()
    monkeypatch.setattr(gridgen_lib.Gridgen, "build", lambda self: None)

    def non_uniform(self):
        raise AssertionError("Cannot create DISV properties")

    monkeypatch.setattr(gridgen_lib.Gridgen, "get_gridprops_disv",
                        non_uniform)
    line = tmp_path / "line.geojson"
    line.write_text(json.dumps({"type": "LineString",
                                "coordinates": [[0.5, 0.5], [5.5, 5.5]]}))
    cfg = {
        "delr": [60.0, 60.0], "delc": [60.0, 60.0], "xoff": 0.0,
        "yoff": 0.0, "angrot": 0.0, "crs_epsg": 32618,
        "ops": [{"fn": "add_refinement_features",
                 "kwargs": {"features": str(line), "featuretype": "line",
                            "level": 1, "layers": [1]}}],
        "sources": {},
    }
    with pytest.raises(driver._Refusal) as excinfo:
        driver._gridgen(cfg, str(tmp_path))
    doc = excinfo.value.document
    assert doc["code"] == "MESH_GRIDGEN_DISV_NOT_UNIFORM"
    assert doc["escalation"] == {"layers": [0, 1]}


def test_the_gridgen_config_words_are_derived_in_one_place():
    words = FLOPY._gridgen_base({}, (500000.0, 3880000.0, 506000.0,
                                     3886000.0), 100.0)
    assert words["delr"] == [100.0] * 60
    assert words["delc"] == [100.0] * 60
    assert words["xoff"] == 500000.0 and words["yoff"] == 3880000.0
    assert words["sources"]["delr"] == "resolution_m"
    assert words["sources"]["xoff"] == "extent"


def test_the_gridgen_config_words_take_stated_scalars_and_lists():
    words = FLOPY._gridgen_base({"delr": 50.0, "delc": [20.0, 20.0, 10.0],
                                 "xoff": 1.0, "yoff": 2.0, "angrot": 15.0},
                                (0.0, 0.0, 1000.0, 1000.0), 100.0)
    assert words["delr"] == [50.0] * 20
    assert words["delc"] == [20.0, 20.0, 10.0]
    assert words["sources"]["delr"] == "stated"
    assert words["angrot"] == 15.0


def test_the_op_names_are_everything_the_box_may_be_asked_to_call():
    assert set(op_names(get_mesher("triangle"))) == {
        "add_polygon", "add_hole", "add_region", "set_bed",
        "set_boundary_roles"}
    assert set(op_names(get_mesher("voronoi"))) == set(
        op_names(get_mesher("triangle")))
    assert set(op_names(get_mesher("gridgen"))) == {
        "add_active_domain", "add_refinement_features", "set_bed",
        "set_boundary_roles"}
