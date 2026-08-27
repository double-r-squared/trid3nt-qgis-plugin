"""Offline tests for the two library-wrapping meshers: ``om2d`` and ``telapy_mesh``.

Both build in a container, so what runs here is everything AROUND that boundary:
the declarations each mesher exposes, the typed refusals, the config the box is
handed, the neutral mesh assembled from what it returns, the measured conformal
offset, and the determinism a recipe records. The container call and the two
world-reads (the bed fetch, the object store) are the only things stubbed - the
composition itself is the real code.
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
from trid3nt_server.workflows.mesh.meshers import telapy_mesh as TELAPY
from trid3nt_server.workflows.mesh.tool import validate_spec

_AOI = (-75.80, 36.10, -75.70, 36.20)

#: Four nodes, two triangles, in lon/lat inside the AOI - the shape the box returns.
_POINTS = np.array([[-75.78, 36.12], [-75.74, 36.12],
                    [-75.78, 36.16], [-75.74, 36.16]])
_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)


# --------------------------------------------------------------------------- #
# Declarations.
# --------------------------------------------------------------------------- #
def test_the_two_wrappers_joined_the_roster():
    assert registered_meshers() == (
        "coastal_edge", "corridor_tin", "hecras_rog", "om2d", "reg_grid",
        "telapy_mesh", "watershed")


@pytest.mark.parametrize("mesher,expected", [
    ("om2d", {"kind", "aoi", "refine", "bed"}),
    ("telapy_mesh", {"kind", "geometry", "crs_authid"}),
])
def test_each_wrapper_declares_the_spec_signature(mesher, expected):
    assert set(get_mesher(mesher).fields) == expected


@pytest.mark.parametrize("mesher", ["om2d", "telapy_mesh"])
def test_both_wrappers_register_the_same_edit_vocabulary(mesher):
    assert set(get_mesher(mesher).actions) == {
        "add_obstacle", "refine_region", "set_boundary", "apply_layer_edits"}


def test_om2d_declines_the_edge_band_the_other_meshers_take():
    """om2d sizes inside its refine block, so the shared band is refused BY NAME
    rather than accepted and ignored."""
    with pytest.raises(MeshToolError) as excinfo:
        validate_spec("om2d", {"aoi": _AOI, "min_edge_length_m": 40.0})
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
        checked_refine("mesher 'om2d'", {"edge_length": ParamRef("edge")},
                       OM2D._REFINE_KNOBS)
    assert excinfo.value.error_code == "MESH_SPEC_UNBOUND"


def test_refine_defaults_fill_in_and_coerce():
    got = checked_refine("mesher 'om2d'", {"edge_length": 300},
                         OM2D._REFINE_KNOBS)
    assert got == {"edge_length": 300.0, "min_spacing": 40.0, "gradation": 0.15}


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
    for mesher in ("om2d", "telapy_mesh"):
        for action in ("add_obstacle", "refine_region"):
            assert get_mesher(mesher).action(action).inputs["geometry"].hashed


# --------------------------------------------------------------------------- #
# Determinism: a measured claim, journaled where a replay reads it.
# --------------------------------------------------------------------------- #
#: Every mesher that shells the OceanMesh2D image, with the flag it registers and
#: the 3-run rebuild-and-diff that flag was MEASURED by (docs/research/
#: om2d-telapy-mesh-recon.md carries the specs and the hashes). A flag no
#: measurement stands behind is a replayability promise nobody checked, so the
#: evidence is named here rather than the value simply asserted.
_MEASURED_DETERMINISM = (
    ("om2d", False, "3 rebuilds -> 2 distinct meshes"),
    ("coastal_edge", True, "3 rebuilds -> sha256 e2025226, 424 nodes/693 elements"),
    ("watershed", True, "3 rebuilds -> sha256 1236ce84, 363 nodes/657 elements"),
)


@pytest.mark.parametrize("mesher,measured,evidence", _MEASURED_DETERMINISM)
def test_each_mesher_registers_the_determinism_it_was_measured_at(
        mesher, measured, evidence):
    assert get_mesher(mesher).deterministic is measured, evidence


def test_the_recipe_carries_the_determinism_a_replay_should_not_assume(tmp_path):
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import tool

    declaration = tool.build_mesh(mesher="om2d", aoi=_AOI)
    session = MeshSession(declaration, workdir=tmp_path)
    spec_line = session.recipe_lines()[0]
    assert spec_line["determinism"] is False

    lattice = tool.build_mesh(mesher="reg_grid", aoi=_AOI, resolution_m=100.0)
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
                "stats": {"nptfr": 4, "n_liquid_boundaries": 1}}

    shoreline = tmp_path / "shoreline" / "GSHHS_i_L1.shp"
    shoreline.parent.mkdir(parents=True, exist_ok=True)
    shoreline.write_bytes(b"shp")
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(shoreline))
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(OM2D, "_bed_raster", fake_bed)
    monkeypatch.setattr(OM2D, "_run_container", fake_run)
    monkeypatch.setattr(TELAPY, "write_telemac_pair", fake_pair)
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.watershed.sample_raster_at_nodes",
        lambda path, pts: np.full(np.asarray(pts).shape[0], -4.0))
    return sent


def test_the_box_is_handed_the_declared_refine_band_and_the_mounted_shoreline(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"aoi": _AOI,
                       "refine": {"min_spacing": 60.0, "edge_length": 800.0,
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


def test_a_min_spacing_coarser_than_the_edge_length_refuses(monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build({"aoi": _AOI,
                    "refine": {"min_spacing": 900.0, "edge_length": 100.0}})
    assert excinfo.value.error_code == "MESH_SPEC_BAD_VALUE"


def test_no_shoreline_dataset_refuses_by_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_GSHHG_SHP", str(tmp_path / "missing.shp"))
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build({"aoi": _AOI})
    assert excinfo.value.error_code == "MESH_SHORELINE_UNAVAILABLE"
    assert "TRID3NT_GSHHG_SHP" in str(excinfo.value)


def test_an_obstacle_edit_rebuilds_with_the_geometry_staged_for_the_box(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"aoi": _AOI})
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
    mesh = OM2D.build({"aoi": _AOI})
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
    mesh = OM2D.build({"aoi": _AOI})
    offset = mesh.meta["probes"]["breakline_offset_m"]
    assert mesh.meta["probes"]["constrained_points"] == 2
    assert offset["max"] < 1.0
    assert "nearest mesh node" in offset["measured"]


def test_a_build_that_constrains_nothing_claims_no_conformality(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"aoi": _AOI})
    assert "breakline_offset_m" not in mesh.meta["probes"]


def test_the_cleanup_note_the_box_reported_reaches_the_probes(
        monkeypatch, tmp_path):
    note = ("delete_boundary_faces reverted: it moved the constrained cut 396.7 m")
    _stub_om2d(monkeypatch, tmp_path, pfix=np.array([[-75.78, 36.12]]),
               stats={"engine": "oceanmesh(test)", "sizing_functions": [],
                      "clean_notes": [note]})
    mesh = OM2D.build({"aoi": _AOI})
    assert mesh.meta["probes"]["clean_notes"] == [note]


def test_the_sizing_claim_is_copied_from_the_box_never_composed_here(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path,
               stats={"engine": "oceanmesh(test)", "sizing_functions": []})
    mesh = OM2D.build({"aoi": _AOI})
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
    from trid3nt_server.workflows.mesh.meshers import coastal_edge as COASTAL

    as_dict = {"uri": "s3://b/x.tif", "fallbacks": _ROWS_DICT,
               "fallback_note": None}
    assert OM2D._bed_provenance("fetch_topobathy", as_dict) == (
        "fetch_topobathy: cudem_nearshore 89%, etopo_bathy_base 11%")
    assert COASTAL._bed_provenance(as_dict) == (
        "topobathy: cudem_nearshore 89%, etopo_bathy_base 11%")
    assert "UNMEASURED" not in OM2D._bed_provenance("fetch_topobathy", as_dict)


def test_a_fetch_that_measured_nothing_still_says_so():
    from trid3nt_server.workflows.mesh.meshers import coastal_edge as COASTAL

    empty = {"uri": "s3://b/x.tif", "fallbacks": [], "fallback_note": None}
    assert "UNMEASURED" in OM2D._bed_provenance("fetch_topobathy", empty)
    assert "UNMEASURED" in COASTAL._bed_provenance(empty)


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


def test_the_gr3_open_block_matches_the_sections_it_was_given():
    from trid3nt_server.workflows.schism.deck_authoring import load_gr3_bridge

    points, cells = _square_mesh()
    text = load_gr3_bridge().tin_to_hgrid(
        points, cells, depth=5.0, open_sections=[[0, 1], [7, 8]],
        clean_boundary=False)
    assert "2 = Number of open boundaries" in text
    assert "4 = Total number of open boundary nodes" in text
    # The land boundary is what is left of the loop, split at the open stretches.
    land = [ln for ln in text.splitlines() if "land boundaries" in ln]
    assert land == ["2 = Number of land boundaries"]


def test_a_gr3_with_no_sections_still_declares_a_closed_boundary():
    from trid3nt_server.workflows.schism.deck_authoring import load_gr3_bridge

    points, cells = _square_mesh()
    text = load_gr3_bridge().tin_to_hgrid(points, cells, depth=5.0,
                                          clean_boundary=False)
    assert "0 = Number of open boundaries" in text
    assert "1 = Number of land boundaries" in text


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
        written["open_nodes"] = list(kw["open_nodes"])
        Path(rundir, "mesh.slf").write_bytes(b"slf")
        Path(rundir, "mesh.cli").write_text("2 2 2\n")
        return {"geo_slf": Path(rundir, "mesh.slf"), "cli": Path(rundir, "mesh.cli"),
                "stats": {"nptfr": 4, "n_liquid_boundaries": 1}}

    monkeypatch.setattr(TELAPY, "write_telemac_pair", fake_pair)
    files, info, probes = OM2D._emit_formats(
        tmp_path, lonlat=_POINTS, cells=_CELLS, points_m=_POINTS,
        bed_up=np.full(4, -5.0), boundary=boundary)
    info["_written_open_nodes"] = written.get("open_nodes")
    return files, info, probes


def test_the_cli_is_handed_exactly_the_section_nodes(monkeypatch, tmp_path):
    _stub_sections(monkeypatch, [_section([1, 3], -30.0, (-75.74, 36.14))])
    _, info, _ = _emit_with(monkeypatch, tmp_path, {"side": "east"})
    assert info["_written_open_nodes"] == [1, 3]
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
    assert info["_written_open_nodes"] == []


# --------------------------------------------------------------------------- #
# The box: a driver in the product tree, shelled with an op.
# --------------------------------------------------------------------------- #
def test_the_drivers_live_in_the_product_tree_beside_their_meshers():
    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    names = {p.name for p in drivers_dir().glob("*_driver.py")}
    assert names == {"om2d_driver.py", "telapy_mesh_driver.py",
                     "coastal_edge_driver.py"}
    assert "sandbox" not in str(drivers_dir())


@pytest.mark.parametrize("module,script", [
    (OM2D, "om2d_driver.py"), (TELAPY, "telapy_mesh_driver.py")])
def test_each_box_mounts_the_product_drivers_dir(module, script):
    from trid3nt_server.workflows.mesh.meshers.drivers import drivers_dir

    assert module._INCONTAINER_SCRIPT == script
    assert (drivers_dir() / script).exists()


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


# --------------------------------------------------------------------------- #
# telapy_mesh: the adoption, its refusals, and its coordinate honesty.
# --------------------------------------------------------------------------- #
def _stub_telapy(monkeypatch, tmp_path, *, bed=True, contours=((0, 1, 3, 2),)):
    calls: list[tuple[str, dict]] = []

    def fake_run(rundir, op, config):
        calls.append((op, dict(config)))
        flat = np.array([n for c in contours for n in c], dtype=np.int64)
        lens = np.array([len(c) for c in contours], dtype=np.int64)
        out = Path(rundir, Path(str(config.get("out_npz", "/data/mesh.npz"))).name)
        np.savez(out, x=_POINTS[:, 0], y=_POINTS[:, 1], ikle=_CELLS,
                 bottom=(np.array([-3.0, -9.0, -2.0, -8.0]) if bed
                         else np.empty(0)),
                 contour_nodes=flat, contour_lengths=lens)
        return {"npoin": 4, "nelem": 2, "nptfr": 4, "n_liquid_boundaries": 1,
                "title": "STUB", "variables": ["BOTTOM"], "elements_removed": 1,
                "nodes_inserted": 7}

    def fake_pair(rundir, **kw):
        calls.append(("write", dict(kw)))
        Path(rundir, "mesh.slf").write_bytes(b"slf")
        Path(rundir, "mesh.cli").write_text("2 2 2\n")
        return {"geo_slf": Path(rundir, "mesh.slf"), "cli": Path(rundir, "mesh.cli"),
                "stats": {"nptfr": 4, "n_liquid_boundaries": 1}}

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(TELAPY, "_run", fake_run)
    monkeypatch.setattr(TELAPY, "write_telemac_pair", fake_pair)
    return calls


def test_an_adopted_geometry_becomes_the_neutral_mesh(monkeypatch, tmp_path):
    calls = _stub_telapy(monkeypatch, tmp_path)
    source = tmp_path / "study.slf"
    source.write_bytes(b"slf")
    mesh = TELAPY.build({"geometry": str(source), "crs_authid": "EPSG:4326"})
    assert mesh.node_count == 4 and mesh.element_count == 2
    assert mesh.has_bed and mesh.crs_authid == "EPSG:4326"
    assert mesh.meta["artifact"]["engine_compat"] == ["telemac"]
    assert mesh.meta["files"]["cli_uri"].endswith("mesh.cli")
    assert calls[0][0] == "read"
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["adopted_from"] == str(source)
    assert "HermesFile" in provenance["reader"]
    assert "set_bnd" in provenance["writer"]


def test_a_geometry_that_is_not_there_refuses_before_the_box(monkeypatch, tmp_path):
    _stub_telapy(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        TELAPY.build({"geometry": str(tmp_path / "absent.slf"),
                      "crs_authid": "EPSG:4326"})
    assert excinfo.value.error_code == "MESH_GEOMETRY_UNREADABLE"


def test_a_projected_geometry_declared_as_lonlat_refuses(monkeypatch, tmp_path):
    """A SELAFIN records no CRS, so the declaration is the only claim there is -
    and coordinates in metres declared as degrees would put the layer off Africa."""
    _stub_telapy(monkeypatch, tmp_path)
    monkeypatch.setattr(TELAPY, "_load", lambda path: (
        np.array([0.0, 11391.0, 0.0, 11391.0]),
        np.array([0.0, 0.0, 12397.0, 12397.0]), _CELLS, None, [[0, 1, 3, 2]]))
    source = tmp_path / "study.slf"
    source.write_bytes(b"slf")
    with pytest.raises(MeshToolError) as excinfo:
        TELAPY.build({"geometry": str(source), "crs_authid": "EPSG:4326"})
    assert excinfo.value.error_code == "MESH_CRS_MISMATCH"


def test_the_punch_reports_what_it_removed_and_how_far_off_the_outline_it_landed(
        monkeypatch, tmp_path):
    calls = _stub_telapy(monkeypatch, tmp_path)
    source = tmp_path / "study.slf"
    source.write_bytes(b"slf")
    mesh = TELAPY.build({"geometry": str(source), "crs_authid": "EPSG:4326"})
    obstacle = tmp_path / "pier.geojson"
    obstacle.write_text(json.dumps({
        "type": "Polygon",
        "coordinates": [[[-75.78, 36.12], [-75.74, 36.12], [-75.74, 36.16],
                         [-75.78, 36.12]]]}))

    edited = get_mesher("telapy_mesh").action("add_obstacle").apply(
        mesh, geometry=str(obstacle))
    assert edited.meta["probes"]["elements_removed"] == 1
    offset = edited.meta["probes"]["outline_offset_m"]
    assert offset["max"] >= 0.0
    assert "nearest mesh node" in offset["measured"]
    assert any(op == "punch" for op, _ in calls)


def test_a_region_refine_states_its_spacing_in_the_mesh_own_units(
        monkeypatch, tmp_path):
    """The action takes metres; a geographic mesh is measured in degrees, so the
    ask is converted before it reaches pretel."""
    calls = _stub_telapy(monkeypatch, tmp_path)
    source = tmp_path / "study.slf"
    source.write_bytes(b"slf")
    mesh = TELAPY.build({"geometry": str(source), "crs_authid": "EPSG:4326"})
    region = tmp_path / "region.geojson"
    region.write_text(json.dumps({
        "type": "Polygon",
        "coordinates": [[[-75.78, 36.12], [-75.74, 36.12], [-75.74, 36.16],
                         [-75.78, 36.12]]]}))

    get_mesher("telapy_mesh").action("refine_region").apply(
        mesh, geometry=str(region), edge_length=100.0)
    refine = [cfg for op, cfg in calls if op == "refine"][0]
    assert 0.0009 < refine["edge_length"] < 0.0012


def test_set_boundary_classifies_a_side_and_hands_the_open_nodes_to_the_writer(
        monkeypatch, tmp_path):
    calls = _stub_telapy(monkeypatch, tmp_path)
    source = tmp_path / "study.slf"
    source.write_bytes(b"slf")
    mesh = TELAPY.build({"geometry": str(source), "crs_authid": "EPSG:4326"})

    edited = get_mesher("telapy_mesh").action("set_boundary").apply(
        mesh, side="south", type="open")
    info = edited.meta["artifact"]["open_boundary_info"]
    assert info["open_boundary_side"] == "south"
    assert info["open_node_count"] >= 1
    written = [cfg for op, cfg in calls if op == "write"][-1]
    assert written["open_nodes"]


def test_a_side_classified_as_land_opens_nothing(monkeypatch, tmp_path):
    calls = _stub_telapy(monkeypatch, tmp_path)
    source = tmp_path / "study.slf"
    source.write_bytes(b"slf")
    mesh = TELAPY.build({"geometry": str(source), "crs_authid": "EPSG:4326"})

    edited = get_mesher("telapy_mesh").action("set_boundary").apply(
        mesh, side="south", type="land")
    info = edited.meta["artifact"]["open_boundary_info"]
    assert info["designation"] == "land"
    assert "open_boundary_side" not in info
    assert [cfg for op, cfg in calls if op == "write"][-1]["open_nodes"] == []


def test_an_edit_on_a_mesh_with_no_build_state_refuses(monkeypatch, tmp_path):
    _stub_telapy(monkeypatch, tmp_path)
    adopted = Mesh(points=_POINTS, cells=_CELLS, crs_authid="EPSG:4326")
    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("telapy_mesh").action("set_boundary").apply(
            adopted, side="south")
    assert excinfo.value.error_code == "MESH_EDIT_UNSUPPORTED"


def test_an_om2d_edit_on_a_mesh_with_no_build_state_refuses():
    adopted = Mesh(points=_POINTS, cells=_CELLS, crs_authid="EPSG:4326")
    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("om2d").action("set_boundary").apply(adopted, side="east")
    assert excinfo.value.error_code == "MESH_EDIT_UNSUPPORTED"
