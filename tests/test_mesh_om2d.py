"""Offline tests for the library-wrapping mesher ``om2d``.

It builds in a container, so what runs here is everything AROUND that boundary:
the declaration it exposes, the typed refusals, the config the box is handed,
the neutral mesh assembled from what it returns, the measured conformal offset,
and the determinism a recipe records. The container call and the two world-reads
(the bed fetch, the object store) are the only things stubbed - the composition
itself is the real code. The POLYGON-domain half of the same mesher is covered
in ``test_mesh_polygon_domain.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.meshers import (
    Mesh,
    MeshToolError,
    checked_refine,
    get_mesher,
    registered_meshers,
)
from trid3nt_server.workflows.mesh.meshers import om2d as OM2D
from trid3nt_server.workflows.mesh.tool import validate_spec

_AOI = (-75.80, 36.10, -75.70, 36.20)

#: Four nodes, two triangles, in lon/lat inside the AOI - the shape the box returns.
_POINTS = np.array([[-75.78, 36.12], [-75.74, 36.12],
                    [-75.78, 36.16], [-75.74, 36.16]])
_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)


# --------------------------------------------------------------------------- #
# Declarations.
# --------------------------------------------------------------------------- #
def test_the_roster_is_the_two_meshers_and_nothing_else():
    assert registered_meshers() == ("om2d", "reg_grid")


def test_the_wrapper_declares_its_spec_signature():
    assert set(get_mesher("om2d").fields) == {"kind", "extent", "refine", "bed"}


def test_the_wrapper_registers_its_edit_vocabulary():
    assert set(get_mesher("om2d").actions) == {
        "add_obstacle", "refine_region", "set_boundary", "apply_layer_edits"}


def test_om2d_declines_the_edge_band_the_other_meshers_take():
    """om2d sizes inside its refine block, so the shared band is refused BY NAME
    rather than accepted and ignored."""
    with pytest.raises(MeshToolError) as excinfo:
        validate_spec("om2d", {"extent": _AOI, "min_edge_length_m": 40.0})
    assert excinfo.value.error_code == "MESH_SPEC_UNKNOWN_FIELD"
    assert "refine" in str(excinfo.value)


def test_a_refine_knob_no_mesher_declared_is_refused_by_name():
    with pytest.raises(MeshToolError) as excinfo:
        checked_refine("mesher 'om2d'", {"gradiation": 0.2},
                       OM2D._REFINE_KNOBS)
    assert excinfo.value.error_code == "MESH_SPEC_UNKNOWN_KNOB"
    assert "gradiation" in str(excinfo.value)
    assert "gradation" in str(excinfo.value)


def test_a_refine_knob_holding_a_late_bound_read_refuses_at_the_build_seam():
    """The field check only sees a mapping, so an unbound P.<name> INSIDE it would
    otherwise reach the mesh library as a placeholder."""
    from trid3nt_server.workflows.lib.plan import ParamRef

    with pytest.raises(MeshToolError) as excinfo:
        checked_refine("mesher 'om2d'", {"max_el": ParamRef("edge")},
                       OM2D._REFINE_KNOBS)
    assert excinfo.value.error_code == "MESH_SPEC_UNBOUND"


def test_refine_defaults_fill_in_and_coerce():
    got = checked_refine("mesher 'om2d'", {"max_el": 300},
                         OM2D._REFINE_KNOBS)
    assert got == {"max_el": 300.0, "resolution_m": 40.0, "gradation": 0.15}


def test_a_refine_knob_that_is_not_a_number_refuses():
    with pytest.raises(MeshToolError) as excinfo:
        checked_refine("mesher 'om2d'", {"gradation": "smooth"},
                       OM2D._REFINE_KNOBS)
    assert excinfo.value.error_code == "MESH_SPEC_BAD_TYPE"


def test_the_boundary_side_vocabulary_carries_seaward_and_the_compass():
    action = get_mesher("om2d").action("set_boundary")
    assert set(action.inputs["side"].choices) == {
        "north", "south", "east", "west", "seaward"}
    assert set(action.inputs["type"].choices) == {"open", "land"}


def test_the_geometry_input_is_hashed_so_the_recipe_records_its_source():
    for action in ("add_obstacle", "refine_region"):
        assert get_mesher("om2d").action(action).inputs["geometry"].hashed


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


def test_the_recipe_carries_the_determinism_a_replay_should_not_assume(tmp_path):
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import tool

    declaration = tool.build_mesh(mesher="om2d", extent=_AOI)
    session = MeshSession(declaration, workdir=tmp_path)
    spec_line = session.recipe_lines()[0]
    assert spec_line["determinism"] is False

    lattice = tool.build_mesh(mesher="reg_grid", extent=_AOI, resolution_m=100.0)
    assert "determinism" not in MeshSession(
        lattice, workdir=tmp_path).recipe_lines()[0]


# --------------------------------------------------------------------------- #
# The config the box is handed, and the mesh assembled from what it returns.
# --------------------------------------------------------------------------- #
def _stub_om2d(monkeypatch, tmp_path, *, pfix=None, stats=None):
    """Answer the container call with a known mesh, and record the config sent."""
    sent: dict[str, object] = {}

    def fake_bed(bed, aoi, rundir):
        Path(rundir, "bed.tif").write_bytes(b"tif")
        return Path(rundir, "bed.tif"), "fetch_topobathy: cudem_nearshore 100%", None

    def fake_run(rundir, shoreline_dir):
        sent["config"] = json.loads(Path(rundir, "om2d_config.json").read_text())
        sent["shoreline_dir"] = str(shoreline_dir)
        np.savez(Path(rundir, "om2d_mesh.npz"), points=_POINTS, cells=_CELLS,
                 pfix=(np.empty((0, 2)) if pfix is None else np.asarray(pfix)))
        Path(rundir, "om2d_stats.json").write_text(json.dumps(
            stats or {"engine": "oceanmesh(test)",
                      "sizing_functions": ["feature_sizing(distance_to_shore)"]}))

    def fake_pair(rundir, **kw):
        sent["pair"] = dict(kw)
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
    monkeypatch.setattr(OM2D, "_bed_raster", fake_bed)
    monkeypatch.setattr(OM2D, "_run_container", fake_run)
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.selafin_cli.write_telemac_pair",
        fake_pair)
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.nodes.sample_raster_at_nodes",
        lambda path, pts: np.full(np.asarray(pts).shape[0], -4.0))
    return sent


def test_the_box_is_handed_the_declared_refine_band_and_the_mounted_shoreline(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": _AOI,
                       "refine": {"resolution_m": 60.0, "max_el": 800.0,
                                  "gradation": 0.22}})
    config = sent["config"]
    assert config["min_edge_length_m"] == 60.0
    assert config["max_edge_length_m"] == 800.0
    assert config["gradation"] == 0.22
    assert config["shoreline_shp"] == "/shoreline/GSHHS_i_L1.shp"
    assert config["dem_path"] == "/data/bed.tif"
    assert config["obstacles"] == [] and config["refine_regions"] == []
    assert mesh.node_count == 4 and mesh.element_count == 2
    assert mesh.crs_authid.startswith("EPSG:326")
    assert mesh.has_bed


def test_a_resolution_coarser_than_the_max_el_refuses(monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build({"extent": _AOI,
                    "refine": {"resolution_m": 900.0, "max_el": 100.0}})
    assert excinfo.value.error_code == "MESH_SPEC_BAD_VALUE"


def test_no_shoreline_dataset_refuses_by_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(tmp_path / "missing.shp"))
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build({"extent": _AOI})
    assert excinfo.value.error_code == "MESH_SHORELINE_UNAVAILABLE"
    assert "TRID3NT_GSHHG_SHP" in str(excinfo.value)


def test_an_obstacle_edit_rebuilds_with_the_geometry_staged_for_the_box(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": _AOI})
    obstacle = tmp_path / "breakwater.geojson"
    obstacle.write_text(json.dumps({
        "type": "Polygon",
        "coordinates": [[[-75.77, 36.13], [-75.75, 36.13], [-75.75, 36.15],
                         [-75.77, 36.15], [-75.77, 36.13]]]}))

    edited = get_mesher("om2d").action("add_obstacle").apply(
        mesh, geometry=str(obstacle))
    config = sent["config"]
    assert len(config["obstacles"]) == 1
    assert config["obstacles"][0]["constrain"] is True
    assert edited.meta["probes"]["obstacles"] == 1
    # The rebuild state carries the obstacle so a second edit stacks on it.
    assert edited.meta["build"]["obstacles"] == [str(obstacle)]


def test_a_region_edit_carries_its_own_target_edge(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": _AOI})
    region = tmp_path / "harbor.geojson"
    region.write_text(json.dumps({
        "type": "Polygon",
        "coordinates": [[[-75.77, 36.13], [-75.75, 36.13], [-75.75, 36.15],
                         [-75.77, 36.15], [-75.77, 36.13]]]}))

    edited = get_mesher("om2d").action("refine_region").apply(
        mesh, geometry=str(region), edge_length=10.0)
    assert sent["config"]["refine_regions"][0]["edge_length_m"] == 10.0
    assert edited.meta["probes"]["refine_regions"] == 1


def test_the_conformal_offset_is_measured_from_the_points_the_box_locked(
        monkeypatch, tmp_path):
    """The claim a constrained breakline makes is a NUMBER, and it is computed from
    the mesh that came back rather than assumed from the ask."""
    # One locked vertex sits exactly on a node; one sits a whole cell away.
    _stub_om2d(monkeypatch, tmp_path,
               pfix=np.array([[-75.78, 36.12], [-75.74, 36.16]]))
    mesh = OM2D.build({"extent": _AOI})
    offset = mesh.meta["probes"]["breakline_offset_m"]
    assert mesh.meta["probes"]["constrained_points"] == 2
    assert offset["max"] < 1.0
    assert "nearest mesh node" in offset["measured"]


def test_a_build_that_constrains_nothing_claims_no_conformality(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": _AOI})
    assert "breakline_offset_m" not in mesh.meta["probes"]


def test_the_cleanup_note_the_box_reported_reaches_the_probes(
        monkeypatch, tmp_path):
    note = ("delete_boundary_faces reverted: it moved the constrained cut 396.7 m")
    _stub_om2d(monkeypatch, tmp_path, pfix=np.array([[-75.78, 36.12]]),
               stats={"engine": "oceanmesh(test)", "sizing_functions": [],
                      "clean_notes": [note]})
    mesh = OM2D.build({"extent": _AOI})
    assert mesh.meta["probes"]["clean_notes"] == [note]


def test_the_sizing_claim_is_copied_from_the_box_never_composed_here(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path,
               stats={"engine": "oceanmesh(test)", "sizing_functions": []})
    mesh = OM2D.build({"extent": _AOI})
    claim = mesh.meta["artifact"]["provenance"]["sizing_source"]
    assert "unreported by the mesher" in claim
    assert "wavelength" not in claim


def test_a_bed_naming_neither_a_fetcher_nor_a_raster_refuses(tmp_path):
    with pytest.raises(MeshToolError) as excinfo:
        OM2D._bed_raster("fetch_nothing_like_this", _AOI, tmp_path)
    assert excinfo.value.error_code == "MESH_BED_UNRESOLVED"


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
    assert OM2D._bed_provenance("fetch_topobathy", as_dict) == (
        "fetch_topobathy: cudem_nearshore 89%, etopo_bathy_base 11%")
    assert "UNMEASURED" not in OM2D._bed_provenance("fetch_topobathy", as_dict)


def test_a_fetch_that_measured_nothing_still_says_so():
    empty = {"uri": "s3://b/x.tif", "fallbacks": [], "fallback_note": None}
    assert "UNMEASURED" in OM2D._bed_provenance("fetch_topobathy", empty)


# --------------------------------------------------------------------------- #
# The boundary blocks: one segment per identified section, land in between.
# --------------------------------------------------------------------------- #
#: A 3x3 lattice, two triangles per square - one loop of 8 boundary nodes.
def _square_mesh():
    xy = np.array([[x, y] for y in (0.0, 1.0, 2.0) for x in (0.0, 1.0, 2.0)])
    cells = []
    for row in range(2):
        for col in range(2):
            a = row * 3 + col
            cells += [[a, a + 1, a + 4], [a, a + 4, a + 3]]
    return xy, np.asarray(cells, dtype=np.int64)


def test_contiguous_runs_splits_a_loop_at_the_open_stretches():
    formats = OM2D._sandbox_formats()
    runs = formats._contiguous_runs([0, 1, 2, 3, 4, 5], {1, 2})
    assert runs == [[3, 4, 5, 0]]
    assert formats._contiguous_runs([0, 1, 2, 3, 4, 5], {1, 4}) == [
        [2, 3], [5, 0]]
    assert formats._contiguous_runs([0, 1, 2], set()) == [[0, 1, 2]]


def test_fort14_writes_one_open_block_per_section():
    points, cells = _square_mesh()
    text = OM2D._sandbox_formats().write_fort14(
        points, cells, depths=5.0, open_sections=[[0, 1], [7, 8]])
    assert "2 = Number of open boundaries" in text
    assert "4 = Total number of open boundary nodes" in text
    assert "2 = Number of nodes for open boundary 1" in text
    assert "2 = Number of nodes for open boundary 2" in text


# --------------------------------------------------------------------------- #
# The open boundary: contiguous sections oceanmesh identified, selected here.
# --------------------------------------------------------------------------- #
def _section(nodes, mean_bed, centroid):
    return {"nodes": list(nodes), "node_count": len(nodes),
            "mean_bed_m": mean_bed, "min_bed_m": mean_bed,
            "centroid": list(centroid)}


def _sections_report(sections):
    return {"library": "oceanmesh.identify_ocean_boundary_sections v(test)",
            "depth_threshold_m": -10.0, "min_nodes_threshold": 10,
            "components": 1, "walk_node_counts": [4],
            "boundary_bed_min_m": -8.4, "boundary_bed_max_m": 0.3,
            "sections": sections}


def _stub_sections(monkeypatch, sections):
    report = _sections_report(sections)
    monkeypatch.setattr(
        OM2D, "_identify_sections",
        lambda rundir, lonlat, cells, bed, threshold, min_nodes: report)
    return report


def test_seaward_takes_the_deepest_identified_section(monkeypatch, tmp_path):
    _stub_sections(monkeypatch, [
        _section([0, 1], -3.0, (-75.78, 36.12)),
        _section([2, 3], -30.0, (-75.74, 36.16))])
    chosen, _ = OM2D._open_sections(
        tmp_path, _POINTS, _CELLS, np.full(4, -5.0), {"side": "seaward"})
    assert [s["nodes"] for s in chosen] == [[2, 3]]


def test_a_compass_name_takes_the_section_furthest_that_way(monkeypatch, tmp_path):
    _stub_sections(monkeypatch, [
        _section([0, 2], -30.0, (-75.79, 36.14)),
        _section([1, 3], -3.0, (-75.71, 36.14))])
    chosen, evidence = OM2D._open_sections(
        tmp_path, _POINTS, _CELLS, np.full(4, -5.0), {"side": "east"})
    # The east section is the SHALLOWER one, so the compass name overrode the bed.
    assert [s["nodes"] for s in chosen] == [[1, 3]]
    assert len(evidence["sections"]) == 2


def test_every_offered_section_rides_in_the_evidence(monkeypatch, tmp_path):
    """A section the choice left out has to be visible, not silently dropped."""
    _stub_sections(monkeypatch, [
        _section([0, 2], -30.0, (-75.79, 36.14)),
        _section([1, 3], -3.0, (-75.71, 36.14))])
    _, _, probes = _emit_with(monkeypatch, tmp_path, {"side": "east"})
    assert probes["open_boundary_sections"] == 1


def test_no_identified_section_refuses_and_states_the_measured_bed_range(
        monkeypatch, tmp_path):
    _stub_sections(monkeypatch, [])
    with pytest.raises(MeshToolError) as excinfo:
        OM2D._open_sections(tmp_path, _POINTS, _CELLS, np.full(4, -5.0),
                            {"side": "east", "depth_threshold": -50.0})
    assert excinfo.value.error_code == "MESH_OPEN_BOUNDARY_UNIDENTIFIED"
    assert "-8.4" in str(excinfo.value) and "0.3" in str(excinfo.value)


def test_an_open_boundary_without_a_bed_refuses(tmp_path):
    with pytest.raises(MeshToolError) as excinfo:
        OM2D._open_sections(tmp_path, _POINTS, _CELLS, None, {"side": "east"})
    assert excinfo.value.error_code == "MESH_OPEN_BOUNDARY_UNMEASURABLE"


def _emit_with(monkeypatch, tmp_path, boundary):
    """Run the format fan-out with the container calls answered."""
    written: dict[str, object] = {}

    def fake_pair(rundir, **kw):
        written["roles"] = {r: list(n) for r, n in kw["roles"].items()}
        Path(rundir, "mesh.slf").write_bytes(b"slf")
        Path(rundir, "mesh.cli").write_text("2 2 2\n")
        return {"geo_slf": Path(rundir, "mesh.slf"), "cli": Path(rundir, "mesh.cli"),
                "stats": {"nptfr": 4, "n_liquid_boundaries": 1,
                          "liquid_boundary_roles": ["open"]}}

    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.selafin_cli.write_telemac_pair",
        fake_pair)
    files, info, probes = OM2D._emit_formats(
        tmp_path, lonlat=_POINTS, cells=_CELLS, points_m=_POINTS,
        bed_up=np.full(4, -5.0), boundary=boundary,
        domain_source="GSHHG land polygons (GSHHS_i_L1.shp)")
    info["_written_roles"] = written.get("roles")
    return files, info, probes


def test_the_cli_is_handed_exactly_the_section_nodes(monkeypatch, tmp_path):
    _stub_sections(monkeypatch, [_section([1, 3], -30.0, (-75.74, 36.14))])
    _, info, _ = _emit_with(monkeypatch, tmp_path, {"side": "east"})
    assert info["_written_roles"] == {"open": [1, 3]}
    assert info["open_node_count"] == 2
    assert info["open_boundary_sections"] == 1


def test_no_fort14_is_written_because_no_engine_reads_one(monkeypatch, tmp_path):
    """SWAN is the only unstructured-mesh consumer this repo could have, and its
    worker is regular-grid only - so an ADCIRC fort.14 was a file the build wrote
    and nothing opened. The shared writer stays; the build stops calling it."""
    _stub_sections(monkeypatch, [_section([1, 3], -30.0, (-75.74, 36.14))])
    files, _, _ = _emit_with(monkeypatch, tmp_path, {"side": "east"})
    assert "fort14_uri" not in files
    assert not (tmp_path / "fort.14").exists()
    # the writer itself is untouched and still writes what it always did
    assert callable(OM2D._sandbox_formats().write_fort14)


def test_a_land_designation_opens_nothing_and_identifies_nothing(
        monkeypatch, tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("a land designation must not identify ocean sections")

    monkeypatch.setattr(OM2D, "_identify_sections", refuse)
    _, info, _ = _emit_with(monkeypatch, tmp_path,
                            {"side": "east", "type": "land"})
    assert info["designation"] == "land"
    assert info["_written_roles"] == {}


# --------------------------------------------------------------------------- #
# The box: a driver in the product tree, shelled with an op.
# --------------------------------------------------------------------------- #
def test_the_drivers_live_in_the_product_tree_beside_their_meshers():
    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    names = {p.name for p in drivers_dir().glob("*_driver.py")}
    assert names == {"om2d_driver.py", "selafin_cli_driver.py"}
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
    OM2D._run_op(tmp_path, "ocean_boundary", "cfg.json", "made.json")
    argv = seen["argv"]
    assert argv[-4:] == ["/drivers/om2d_driver.py", "ocean_boundary",
                         "/data/cfg.json", "/data"]
    assert f"{drivers_dir()}:/drivers:ro" in argv


# --------------------------------------------------------------------------- #
# Geometry sources: one reader, three shapes of source.
# --------------------------------------------------------------------------- #
def test_a_geojson_file_and_inline_geojson_read_the_same(tmp_path):
    doc = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    path = tmp_path / "g.geojson"
    path.write_text(json.dumps(doc))
    assert OM2D.read_geometry(str(path)) == doc
    assert OM2D.read_geometry(json.dumps(doc)) == doc


def test_an_unreadable_geometry_source_refuses(tmp_path):
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.read_geometry(str(tmp_path / "nope.geojson"))
    assert excinfo.value.error_code == "MESH_GEOMETRY_UNREADABLE"



def test_an_om2d_edit_on_a_mesh_with_no_build_state_refuses():
    adopted = Mesh(points=_POINTS, cells=_CELLS, crs_authid="EPSG:4326")
    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("om2d").action("set_boundary").apply(adopted, side="east")
    assert excinfo.value.error_code == "MESH_EDIT_UNSUPPORTED"


# --------------------------------------------------------------------------- #
# What a mesh must survive to be SOLVABLE, and what its bed must not read.
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
    sent = _stub_om2d(monkeypatch, tmp_path)
    monkeypatch.setattr(OM2D, "_run_container", _npz_writer(pinched, cells, sent))
    mesh = OM2D.build({"extent": _AOI})
    assert mesh.meta["probes"]["degenerate_elements_repaired"] >= 1


def _npz_writer(points, cells, sent):
    def fake_run(rundir, shoreline_dir):
        sent["config"] = json.loads(Path(rundir, "om2d_config.json").read_text())
        np.savez(Path(rundir, "om2d_mesh.npz"), points=points, cells=cells,
                 pfix=np.empty((0, 2)))
        Path(rundir, "om2d_stats.json").write_text(json.dumps(
            {"engine": "oceanmesh(test)", "sizing_functions": []}))
    return fake_run


def test_the_bed_is_fetched_past_the_aoi_the_mesh_has_nodes_on():
    grown = OM2D._bed_bbox((-75.80, 36.10, -75.70, 36.20))
    assert grown[0] < -75.80 and grown[1] < 36.10
    assert grown[2] > -75.70 and grown[3] > 36.20


def test_a_node_on_the_rasters_rim_reads_a_whole_cell_not_its_edge(tmp_path):
    """A node ON the AOI corner reads the first WHOLE cell, never off the grid.

    The corner coordinate indexes one row and one column past the grid fetched for
    that AOI, and a sample past the grid comes back as the untagged zero: an
    18 m deep boundary reading as sea level, which every consumer - the
    ocean-boundary identification most of all - takes at face value. A rim deeper
    than the one partial cell is the bed FETCH's margin to cover, not this clamp's.
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
