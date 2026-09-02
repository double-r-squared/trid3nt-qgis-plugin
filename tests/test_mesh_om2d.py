"""Offline tests for the library-wrapping mesher ``om2d``.

It builds in a container, so what runs here is everything AROUND that boundary:
the three registrations it makes, the typed refusals, the CONFIG the box is
handed - which is the recipe's ops list travelling as data - the neutral mesh
assembled from what comes back, the measured conformal offset, and the
determinism a recipe records. The container call and the two world-reads (the bed
fetch, the object store) are the only things stubbed; the composition itself is
the real code. The POLYGON-domain half of the same mesher is covered in
``test_mesh_polygon_domain.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.meshers import (
    POST,
    PRE,
    Mesh,
    MeshToolError,
    get_mesher,
    op_names,
    registered_meshers,
    resolve_op,
)
from trid3nt_server.workflows.mesh.meshers import om2d as OM2D
from trid3nt_server.workflows.mesh.tool import mesh_op, tool

_AOI = (-75.80, 36.10, -75.70, 36.20)

#: Four nodes, two triangles, in lon/lat inside the AOI - the shape the box returns.
_POINTS = np.array([[-75.78, 36.12], [-75.74, 36.12],
                    [-75.78, 36.16], [-75.74, 36.16]])
_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)


def _recipe(**over):
    ask = {"mesher": "om2d", "extent": _AOI, "resolution_m": 60.0, "ops": []}
    ask.update(over)
    return tool.build_mesh(**ask)


# --------------------------------------------------------------------------- #
# What a mesher IS: namespaces, a role adapter, a default recipe.
# --------------------------------------------------------------------------- #
def test_the_roster_is_the_two_meshers_and_nothing_else():
    assert registered_meshers() == ("om2d", "reg_grid")


def test_the_wrapper_registers_the_librarys_own_names_tagged_by_phase():
    """The op vocabulary is VERBATIM and its phase is DERIVED from where it sits."""
    for sizing in ("feature_sizing_function", "wavelength_sizing_function",
                   "enforce_mesh_gradation", "distance_sizing_from_line_function"):
        space, fn = resolve_op(OM2D.OM2D, sizing)
        assert (space.origin, space.phase) == ("oceanmesh", PRE)
        assert fn is None, "the library lives in the image, not in this process"
    for on_a_mesh in ("delete_boundary_faces", "laplacian2", "fix_mesh",
                      "identify_ocean_boundary_sections"):
        space, _ = resolve_op(OM2D.OM2D, on_a_mesh)
        assert (space.origin, space.phase) == ("oceanmesh", POST)


def test_the_shared_primitives_ride_along_for_this_mesher_too():
    for primitive in ("set_bed", "set_boundary_roles"):
        space, fn = resolve_op(OM2D.OM2D, primitive)
        assert (space.origin, space.phase) == ("primitives", POST)
        assert callable(fn), "a primitive binds against its REAL signature"


def test_the_mesher_owns_the_two_domain_primitives_the_library_has_no_word_for():
    for primitive in ("set_obstacle", "set_region_size"):
        space, _ = resolve_op(OM2D.OM2D, primitive)
        assert (space.origin, space.phase) == ("om2d", PRE)


def test_an_unknown_op_refuses_with_the_nearest_names():
    with pytest.raises(MeshToolError) as excinfo:
        _recipe(ops=[mesh_op("feature_sizing_funtcion")])
    assert excinfo.value.error_code == "MESH_OP_UNKNOWN"
    assert "feature_sizing_function" in str(excinfo.value)


def test_the_default_recipe_is_hard_baked_and_visible():
    """An undeclared ask gets the library's own clean chain, then the bed."""
    assert [op.fn for op in get_mesher("om2d").default_ops] == [
        "delete_boundary_faces", "delete_faces_connected_to_one_face",
        "laplacian2", "make_mesh_boundaries_traversable", "fix_mesh", "set_bed"]
    bed = get_mesher("om2d").default_ops[-1]
    assert bed.kwargs["source"] == "fetch_topobathy"
    assert bed.kwargs["interp"] == "nearest"


def test_omitting_ops_takes_the_default_and_declaring_replaces_it_wholesale():
    assert [op.fn for op in tool.build_mesh(mesher="om2d", extent=_AOI).ops] == \
        [op.fn for op in get_mesher("om2d").default_ops]
    assert _recipe(ops=[mesh_op("laplacian2")]).ops[0].fn == "laplacian2"
    assert len(_recipe(ops=[mesh_op("laplacian2")]).ops) == 1


def test_engine_vocabulary_is_not_in_the_op_namespace_either():
    """``bed`` and ``boundaries`` were our words; the primitives use SET verbs."""
    names = op_names(get_mesher("om2d"))
    assert "set_bed" in names and "set_boundary_roles" in names
    assert "bed" not in names and "boundaries" not in names
    assert "match_boundary_roles" not in names
    assert "fit_downstream_bed" not in names


# --------------------------------------------------------------------------- #
# Determinism: a measured claim, journaled where a replay reads it.
# --------------------------------------------------------------------------- #
#: The mesher that shells the OceanMesh2D image, with the flag it registers and
#: the 3-run rebuild-and-diff that flag was MEASURED by. A flag no measurement
#: stands behind is a replayability promise nobody checked, so the evidence is
#: named here rather than the value simply asserted.
_MEASURED_DETERMINISM = ("om2d", False, "3 rebuilds -> 2 distinct meshes")


def test_the_mesher_registers_the_determinism_it_was_measured_at():
    mesher, measured, evidence = _MEASURED_DETERMINISM
    assert get_mesher(mesher).deterministic is measured, evidence


def test_the_journal_carries_the_determinism_a_replay_should_not_assume(tmp_path):
    from trid3nt_server.workflows.mesh.session import MeshSession

    head = MeshSession(_recipe(), workdir=tmp_path).recipe_lines()[0]
    assert head["determinism"] is False

    lattice = tool.build_mesh(mesher="reg_grid", extent=_AOI, resolution_m=100.0)
    assert "determinism" not in MeshSession(
        lattice, workdir=tmp_path).recipe_lines()[0]


# --------------------------------------------------------------------------- #
# The config the box is handed IS the recipe's ops, travelling as data.
# --------------------------------------------------------------------------- #
def _stub_om2d(monkeypatch, tmp_path, *, pfix=None, stats=None,
               points=None, cells=None, results=None):
    """Answer the container calls with a known mesh, and record what was sent."""
    sent: dict[str, object] = {"configs": []}

    def fake_run_op(rundir, op, config_name, produces, *, shoreline_dir=None):
        config = json.loads(Path(rundir, config_name).read_text())
        sent["configs"].append((op, config))
        sent["shoreline_dir"] = str(shoreline_dir)
        if op == "build":
            sent["config"] = config
            np.savez(Path(rundir, "om2d_mesh.npz"),
                     points=(_POINTS if points is None else points),
                     cells=(_CELLS if cells is None else cells),
                     pfix=(np.empty((0, 2)) if pfix is None else np.asarray(pfix)))
            Path(rundir, "om2d_stats.json").write_text(json.dumps(
                stats or {"engine": "oceanmesh(test)",
                          "sizing_functions": ["feature_sizing_function"]}))
            return
        npz = np.load(Path(rundir, config["mesh_npz"].replace("/data/", "")
                           if config["mesh_npz"].startswith("/data/")
                           else config["mesh_npz"]))
        np.savez(Path(rundir, config["out_stem"] + ".npz"),
                 points=npz["points"], cells=npz["cells"])
        Path(rundir, config["out_stem"] + ".json").write_text(json.dumps(
            {"ops": [], "results": results or {}, "clean_notes": []}))

    def fake_pair(rundir, **kw):
        sent["pair"] = {k: v for k, v in kw.items() if k in ("roles", "title")}
        Path(rundir, "mesh.slf").write_bytes(b"slf")
        Path(rundir, "mesh.cli").write_text("2 2 2\n")
        return {"geo_slf": Path(rundir, "mesh.slf"), "cli": Path(rundir, "mesh.cli"),
                "stats": {"nptfr": 4, "n_liquid_boundaries": 1,
                          "liquid_boundary_roles": ["open"]}}

    shoreline = tmp_path / "shoreline" / "GSHHS_i_L1.shp"
    shoreline.parent.mkdir(parents=True, exist_ok=True)
    shoreline.write_bytes(b"shp")
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(shoreline))
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(OM2D, "_run_op", fake_run_op)
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.selafin_cli.write_telemac_pair",
        fake_pair)
    return sent


def test_the_recipes_ops_reach_the_box_verbatim_in_declared_order(
        monkeypatch, tmp_path):
    """CODE AS DATA: the names travel as written and the phases split them."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe(ops=[
        mesh_op("feature_sizing_function", r=5),
        mesh_op("enforce_mesh_gradation", gradation=0.22),
        mesh_op("delete_boundary_faces"),
        mesh_op("fix_mesh", delete_unused=True)]))
    config = sent["config"]
    assert [entry["fn"] for entry in config["pre_ops"]] == [
        "feature_sizing_function", "enforce_mesh_gradation"]
    assert config["pre_ops"][0]["kwargs"] == {"r": 5}
    assert config["pre_ops"][1]["kwargs"] == {"gradation": 0.22}
    assert [entry["fn"] for entry in config["post_ops"]] == [
        "delete_boundary_faces", "fix_mesh"]
    assert config["post_ops"][1]["kwargs"] == {"delete_unused": True}
    assert mesh.node_count == 4 and mesh.element_count == 2
    assert mesh.crs_authid.startswith("EPSG:326")


def test_the_one_size_word_threads_as_the_librarys_own_edge_defaults(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(resolution_m=60.0))
    assert sent["config"]["min_edge_length_m"] == 60.0
    assert sent["config"]["max_edge_length_m"] == 600.0
    assert sent["config"]["seed"] == OM2D._SEED


def test_the_mounted_shoreline_is_the_declared_dataset(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe())
    assert sent["config"]["shoreline_shp"] == "/shoreline/GSHHS_i_L1.shp"
    assert sent["config"]["domain_geojson"] is None
    assert sent["shoreline_dir"].endswith("shoreline")


def test_no_shoreline_dataset_refuses_by_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(tmp_path / "missing.shp"))
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build(_recipe())
    assert excinfo.value.error_code == "MESH_SHORELINE_UNAVAILABLE"
    assert "TRID3NT_GSHHG_SHP" in str(excinfo.value)


def test_a_recipe_with_no_extent_refuses_before_anything_is_staged():
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build(_recipe(extent=None))
    assert excinfo.value.error_code == "MESH_EXTENT_MISSING"


def test_a_projected_extent_is_named_for_what_it_is(monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build(_recipe(extent=(430000.0, 3990000.0, 440000.0, 4000000.0)))
    assert excinfo.value.error_code == "MESH_DOMAIN_NOT_LONLAT"
    assert "projected" in str(excinfo.value)


def test_a_data_valued_op_kwarg_is_staged_into_the_box_as_a_path(
        monkeypatch, tmp_path):
    """The ONE typed conversion: a layer becomes geometry, written where the box
    can read it, and the op names the path the CONTAINER sees."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    lines = tmp_path / "channels.geojson"
    lines.write_text(json.dumps({"type": "LineString",
                                 "coordinates": [[-75.78, 36.12], [-75.74, 36.16]]}))
    OM2D.build(_recipe(ops=[
        mesh_op("distance_sizing_from_line_function", line_file=str(lines),
                rate=0.05)]))
    entry = sent["config"]["pre_ops"][0]
    assert entry["kwargs"]["line_file"].startswith("/data/op0_line_file")
    assert entry["kwargs"]["rate"] == 0.05


# --------------------------------------------------------------------------- #
# The ops after the first primitive run over the mesh the host already holds.
# --------------------------------------------------------------------------- #
def _no_fetch_bed(monkeypatch, value=-4.0):
    """set_bed, with the world-read answered and the node assignment real."""
    import dataclasses

    def fake_set_bed(mesh, source, interp="nearest", condition=None):
        return dataclasses.replace(
            mesh, bed=np.full(mesh.node_count, value),
            meta={**dict(mesh.meta), "bed_source": f"{source} (stubbed)"})

    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.primitives.set_bed", fake_set_bed)


def test_a_library_op_after_the_bed_runs_in_its_own_call_over_the_current_mesh(
        monkeypatch, tmp_path):
    sections = [{"nodes": [1, 3], "node_count": 2, "mean_bed_m": -30.0,
                 "min_bed_m": -31.0, "centroid": [-75.74, 36.14]}]
    sent = _stub_om2d(monkeypatch, tmp_path,
                      results={"identify_ocean_boundary_sections": sections})
    _no_fetch_bed(monkeypatch)
    mesh = OM2D.build(_recipe(ops=[
        mesh_op("delete_boundary_faces"),
        mesh_op("set_bed", source="fetch_topobathy"),
        mesh_op("identify_ocean_boundary_sections", depth_threshold=-10.0)]))

    ops = [op for op, _cfg in sent["configs"]]
    assert ops == ["build", "post"]
    build_config = sent["configs"][0][1]
    tail_config = sent["configs"][1][1]
    assert [e["fn"] for e in build_config["post_ops"]] == ["delete_boundary_faces"]
    assert [e["fn"] for e in tail_config["ops"]] == [
        "identify_ocean_boundary_sections"]
    assert tail_config["ops"][0]["kwargs"] == {"depth_threshold": -10.0}
    assert mesh.meta["artifact"]["open_boundary_info"]["open_boundary_sections"] == 1
    assert sent["pair"]["roles"] == {"open": [1, 3]}


def test_a_recipe_that_never_asks_opens_no_boundary(monkeypatch, tmp_path):
    """An inland domain has no open boundary, which is an answer not an omission."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    _no_fetch_bed(monkeypatch)
    mesh = OM2D.build(_recipe(ops=[mesh_op("set_bed", source="fetch_topobathy")]))
    assert "open_boundary_sections" not in \
        mesh.meta["artifact"]["open_boundary_info"]
    assert sent["pair"]["roles"] == {}


def test_an_op_that_renumbers_after_a_primitive_refuses_by_name(
        monkeypatch, tmp_path):
    """A bed painted before a renumbering would belong to nodes that are gone."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    _no_fetch_bed(monkeypatch)
    real = OM2D._run_op

    def shrinking(rundir, op, config_name, produces, *, shoreline_dir=None):
        real(rundir, op, config_name, produces, shoreline_dir=shoreline_dir)
        if op != "post":
            return
        config = json.loads(Path(rundir, config_name).read_text())
        np.savez(Path(rundir, config["out_stem"] + ".npz"),
                 points=_POINTS[:3], cells=np.array([[0, 1, 2]], dtype=np.int64))

    monkeypatch.setattr(OM2D, "_run_op", shrinking)
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build(_recipe(ops=[
            mesh_op("set_bed", source="fetch_topobathy"),
            mesh_op("laplacian2")]))
    assert excinfo.value.error_code == "MESH_OP_RENUMBERED_AFTER_BED"
    assert "laplacian2" in str(excinfo.value)
    assert sent["configs"][0][0] == "build"


# --------------------------------------------------------------------------- #
# What the mesher measured about its own build.
# --------------------------------------------------------------------------- #
def test_the_conformal_offset_is_measured_from_the_points_the_box_locked(
        monkeypatch, tmp_path):
    """The claim a constrained outline makes is a NUMBER, computed from the mesh
    that came back rather than assumed from the ask."""
    _stub_om2d(monkeypatch, tmp_path,
               pfix=np.array([[-75.78, 36.12], [-75.74, 36.16]]))
    mesh = OM2D.build(_recipe())
    offset = mesh.meta["probes"]["breakline_offset_m"]
    assert mesh.meta["probes"]["constrained_points"] == 2
    assert offset["max"] < 1.0
    assert "nearest mesh node" in offset["measured"]


def test_a_build_that_constrains_nothing_claims_no_conformality(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe())
    assert "breakline_offset_m" not in mesh.meta["probes"]


def test_the_cleanup_note_the_box_reported_reaches_the_probes(
        monkeypatch, tmp_path):
    note = "delete_boundary_faces reverted: it moved the constrained cut 396.7 m"
    _stub_om2d(monkeypatch, tmp_path, pfix=np.array([[-75.78, 36.12]]),
               stats={"engine": "oceanmesh(test)", "sizing_functions": [],
                      "clean_notes": [note]})
    mesh = OM2D.build(_recipe())
    assert mesh.meta["probes"]["clean_notes"] == [note]


def test_the_sizing_claim_is_copied_from_the_box_never_composed_here(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path,
               stats={"engine": "oceanmesh(test)", "sizing_functions": []})
    mesh = OM2D.build(_recipe())
    claim = mesh.meta["artifact"]["provenance"]["sizing_source"]
    assert "unreported by the mesher" in claim
    assert "wavelength" not in claim


def test_a_library_op_records_the_note_that_its_kwargs_bound_elsewhere(
        monkeypatch, tmp_path):
    """A function this process cannot import has no signature here to bind."""
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe(ops=[mesh_op("laplacian2")]))
    notes = mesh.meta["artifact"]["provenance"]["op_notes"]
    assert any("laplacian2" in note and "cannot import" in note for note in notes)


# --------------------------------------------------------------------------- #
# The box: a driver in the product tree, shelled with an op.
# --------------------------------------------------------------------------- #
def test_the_drivers_live_in_the_product_tree_beside_their_callers():
    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    names = {p.name for p in drivers_dir().glob("*_driver.py")}
    assert names == {"om2d_driver.py", "selafin_cli_driver.py",
                     "telemac_cas_driver.py"}
    assert "sandbox" not in str(drivers_dir())


def test_the_box_mounts_the_product_drivers_dir():
    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    assert OM2D._INCONTAINER_SCRIPT == "om2d_driver.py"
    assert (drivers_dir() / "om2d_driver.py").exists()


def test_the_om2d_box_is_shelled_with_a_named_op(monkeypatch, tmp_path):
    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    seen: dict[str, object] = {}

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        Path(tmp_path, "made.json").write_text("{}")
        return _Done()

    monkeypatch.setattr(OM2D.subprocess, "run", fake_run)
    OM2D._run_op(tmp_path, "post", "cfg.json", "made.json")
    argv = seen["argv"]
    assert argv[-4:] == ["/drivers/om2d_driver.py", "post",
                         "/data/cfg.json", "/data"]
    assert f"{drivers_dir()}:/drivers:ro" in argv
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"


# --------------------------------------------------------------------------- #
# What a mesh must survive to be SOLVABLE.
# --------------------------------------------------------------------------- #
def test_two_nodes_a_fraction_of_a_metre_apart_are_fused_into_one():
    """A pair the geometry file would write as one point IS one point.

    A SELAFIN stores single-precision coordinates and a UTM northing eats the
    mantissa, so the element between such a pair reaches the solver with a zero
    determinant and takes the whole run down.
    """
    points = np.array([[-75.780000, 36.120000], [-75.740000, 36.120000],
                       [-75.780000, 36.160000], [-75.780001, 36.120001]])
    cells = np.array([[0, 1, 2], [0, 3, 1]], dtype=np.int64)
    fused, remapped, depths, merged = OM2D._merge_coincident(
        points, cells, np.zeros(4))
    assert merged == 1
    assert (remapped == 0).sum() >= 2      # node 3 became node 0
    assert fused.shape[0] == 4             # re-indexing is the clean pass's job
    assert depths.shape[0] == 4


def test_an_element_with_no_area_is_not_kept():
    points = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [2.0, 0.0]])
    cells = np.array([[0, 1, 2], [0, 1, 3]], dtype=np.int64)   # second is collinear
    keep = OM2D._has_area(points, cells)
    assert keep.tolist() == [True, False]


def test_a_mesh_carrying_a_collapsed_element_reports_the_repair(
        monkeypatch, tmp_path):
    """The count is a PROBE, not a silence: a repaired mesh says it was repaired."""
    pinched = np.array([[-75.78, 36.12], [-75.74, 36.12], [-75.78, 36.16],
                        [-75.780000005, 36.120000005]])
    cells = np.array([[0, 1, 2], [0, 3, 1]], dtype=np.int64)
    _stub_om2d(monkeypatch, tmp_path, points=pinched, cells=cells)
    mesh = OM2D.build(_recipe())
    assert mesh.meta["probes"]["degenerate_elements_repaired"] >= 1


def test_a_node_on_the_rasters_rim_reads_a_whole_cell_not_its_edge(tmp_path):
    """A node ON the AOI corner reads the first WHOLE cell, never off the grid.

    The corner coordinate indexes one row and one column past the grid fetched for
    that AOI, and a sample past the grid comes back as the untagged zero: an
    18 m deep boundary reading as sea level, which every consumer takes at face
    value. A rim deeper than the one partial cell is the bed FETCH's margin to
    cover, not this clamp's.
    """
    import rasterio
    from rasterio.transform import from_origin

    from trid3nt_server.workflows.mesh.shared.nodes import sample_raster_at_nodes

    band = np.full((10, 10), -18.0, dtype="float32")
    band[-1, :] = 0.0           # the partial cell the warp filled
    band[:, -1] = 0.0
    path = tmp_path / "bed.tif"
    with rasterio.open(path, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="float32", crs="EPSG:4326",
                       transform=from_origin(-75.80, 36.20, 0.01, 0.01)) as dst:
        dst.write(band, 1)

    corners = np.array([[-75.80, 36.10], [-75.70, 36.10],
                        [-75.70, 36.20], [-75.80, 36.20]])
    assert sample_raster_at_nodes(str(path), corners).tolist() == [-18.0] * 4
    # The labelled default is nearest; bilinear reads BETWEEN cell centres, so it
    # lands on the same whole cell to float precision rather than exactly.
    assert sample_raster_at_nodes(
        str(path), corners, interp="bilinear") == pytest.approx([-18.0] * 4)


# --------------------------------------------------------------------------- #
# The boundary blocks the shared TIN writer builds from the runs it is handed.
# --------------------------------------------------------------------------- #
def _square_mesh():
    """A 3x3 lattice, two triangles per square - one loop of 8 boundary nodes."""
    xy = np.array([[x, y] for y in (0.0, 1.0, 2.0) for x in (0.0, 1.0, 2.0)])
    cells = []
    for row in range(2):
        for col in range(2):
            a = row * 3 + col
            cells += [[a, a + 1, a + 4], [a, a + 4, a + 3]]
    return xy, np.asarray(cells, dtype=np.int64)


def test_contiguous_runs_splits_a_loop_at_the_open_stretches():
    from trid3nt_server.workflows.mesh.shared.nodes import tin_formats

    formats = tin_formats()
    assert formats._contiguous_runs([0, 1, 2, 3, 4, 5], {1, 2}) == [[3, 4, 5, 0]]
    assert formats._contiguous_runs([0, 1, 2, 3, 4, 5], {1, 4}) == [[2, 3], [5, 0]]
    assert formats._contiguous_runs([0, 1, 2], set()) == [[0, 1, 2]]


def test_fort14_writes_one_open_block_per_section():
    from trid3nt_server.workflows.mesh.shared.nodes import tin_formats

    points, cells = _square_mesh()
    text = tin_formats().write_fort14(
        points, cells, depths=5.0, open_sections=[[0, 1], [7, 8]])
    assert "2 = Number of open boundaries" in text
    assert "4 = Total number of open boundary nodes" in text


def test_no_fort14_is_written_because_no_engine_reads_one(monkeypatch, tmp_path):
    """SWAN is the only unstructured-mesh consumer this repo could have, and its
    worker is regular-grid only - so an ADCIRC fort.14 was a file the build wrote
    and nothing opened. The shared writer stays; the build stops calling it."""
    from trid3nt_server.workflows.mesh.shared.nodes import tin_formats

    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe())
    assert "fort14_uri" not in mesh.meta["files"]
    assert callable(tin_formats().write_fort14)


# --------------------------------------------------------------------------- #
# Inside the driver: the verbatim call, exercised with oceanmesh stubbed out.
# --------------------------------------------------------------------------- #
def _driver():
    """The in-container driver, imported here with its library stubbed.

    ``oceanmesh`` lives only in the mesh image; the binding, the unit conversion
    and the result adoption are ours and belong under the offline suite.
    """
    import importlib.util
    import sys
    import types

    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    stub = types.ModuleType("oceanmesh")
    stub.Domain = type("Domain", (), {"__init__": lambda self, bbox, func: None})
    saved = sys.modules.get("oceanmesh")
    sys.modules["oceanmesh"] = stub
    try:
        spec = importlib.util.spec_from_file_location(
            "_om2d_driver_under_test", drivers_dir() / "om2d_driver.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            del sys.modules["oceanmesh"]
        else:
            sys.modules["oceanmesh"] = saved
    return module


def test_the_environment_fills_what_the_recipe_left_unstated():
    driver = _driver()

    def sized(shoreline, signed_distance_function, r=3, min_edge_length=None):
        return (shoreline, signed_distance_function, r, min_edge_length)

    env = {"shoreline": "SHORE", "signed_distance_function": "SDF",
           "min_edge_length": 0.001}
    bound = driver._bind(sized, {"r": 5}, env, 111_320.0, {})
    assert bound == {"shoreline": "SHORE", "signed_distance_function": "SDF",
                     "r": 5, "min_edge_length": 0.001}


def test_a_required_parameter_the_domain_cannot_supply_refuses_by_name():
    driver = _driver()

    def sized(shoreline, r=3):
        return shoreline

    with pytest.raises(ValueError) as excinfo:
        driver._bind(sized, {}, {"bbox": (0, 1, 0, 1)}, 111_320.0, {})
    assert "'shoreline'" in str(excinfo.value)
    assert "bbox" in str(excinfo.value)


def test_an_edge_length_a_recipe_states_in_metres_reaches_the_library_in_degrees():
    driver = _driver()

    def sized(min_edge_length, max_edge_length=None):
        return None

    report: dict = {}
    bound = driver._bind(sized, {"min_edge_length": 111.32}, {}, 111_320.0, report)
    assert bound["min_edge_length"] == pytest.approx(0.001)
    # A default threaded rather than written is SAID OUT LOUD, in metres.
    assert "threaded_m" not in report
    threaded = driver._bind(sized, {}, {"min_edge_length": 0.002,
                                        "max_edge_length": 0.02}, 111_320.0, report)
    assert threaded["min_edge_length"] == 0.002
    assert report["threaded_m"]["min_edge_length"] == pytest.approx(222.64)


def test_a_triangulation_replaces_the_mesh_and_a_measurement_is_recorded():
    driver = _driver()

    assert driver._is_mesh((_POINTS, _CELLS))
    assert not driver._is_mesh([(0, 3), (5, 9)])
    assert not driver._is_mesh(None)


def test_an_op_that_removes_the_last_element_stops_the_chain_by_name():
    driver = _driver()

    def empties(vertices, entities):
        return vertices[:0], entities[:0]

    driver._PRIMITIVES["empties"] = empties
    try:
        with pytest.raises(driver._EmptyAfterOp) as excinfo:
            driver._run_mesh_ops([{"fn": "empties", "kwargs": {}}],
                                 _POINTS, _CELLS, None, 111_320.0, [], {},
                                 notes=[])
        assert "empties" in str(excinfo.value)
    finally:
        driver._PRIMITIVES.pop("empties")


def test_an_op_that_stops_inside_the_library_is_refused_not_half_applied():
    driver = _driver()

    def explodes(vertices, entities):
        raise RuntimeError("GEOS side-location conflict")

    driver._PRIMITIVES["explodes"] = explodes
    try:
        with pytest.raises(ValueError) as excinfo:
            driver._run_mesh_ops([{"fn": "explodes", "kwargs": {}}],
                                 _POINTS, _CELLS, None, 111_320.0, [], {},
                                 notes=[])
        assert "partially processed" in str(excinfo.value)
        assert "GEOS side-location conflict" in str(excinfo.value)
    finally:
        driver._PRIMITIVES.pop("explodes")


def test_a_name_the_library_does_not_have_refuses_in_the_box_too():
    driver = _driver()

    with pytest.raises(ValueError) as excinfo:
        driver._resolve("no_such_oceanmesh_function")
    assert "no_such_oceanmesh_function" in str(excinfo.value)


def test_a_mesh_that_carries_no_build_state_still_takes_a_primitive():
    """An adopted topology has no om2d build behind it, and a primitive is written
    against the MESH rather than against a rebuild state, so it still applies."""
    from trid3nt_server.workflows.mesh.shared.primitives import set_boundary_roles

    adopted = Mesh(points=_POINTS, cells=_CELLS, crs_authid="EPSG:4326")
    assert set_boundary_roles(adopted) is adopted
