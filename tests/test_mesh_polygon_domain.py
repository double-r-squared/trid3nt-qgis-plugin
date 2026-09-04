"""Offline tests for the POLYGON-DOMAIN path: a domain another tool produced.

A recipe's extent takes a bbox or a polygon. The bbox path cuts the water side of
the GSHHG shoreline; the polygon path meshes the interior of what it is handed,
with the signed distance measured against that polygon's own boundary. Both go to
the same ``om.generate_mesh`` in the same box, so what is tested here is the seam:
the config the box is handed, the refusals each path owns, and the provenance the
mesh travels with.

The container call and the TELEMAC writer are stubbed; the composition is the
real code. The in-container geometry itself is exercised against a stub
``oceanmesh`` module, because the real one is GPL and lives only in the image.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.meshers import om2d as OM2D
from trid3nt_server.workflows.mesh.meshers import reg_grid as REG_GRID
from trid3nt_server.workflows.mesh.tool import mesh_op, tool

_AOI = (-75.80, 36.10, -75.70, 36.20)

#: A basin-shaped domain another tool produced, and the channel inside it.
_BASIN = {"type": "Polygon", "coordinates": [[
    [-75.78, 36.12], [-75.72, 36.12], [-75.72, 36.18], [-75.78, 36.18],
    [-75.78, 36.12]]]}
_CHANNEL = {"type": "LineString", "coordinates": [[-75.77, 36.13], [-75.73, 36.17]]}

_POINTS = np.array([[-75.78, 36.12], [-75.74, 36.12],
                    [-75.78, 36.16], [-75.74, 36.16]])
_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)


def _recipe(**over):
    ask = {"mesher": "om2d", "extent": json.dumps(_BASIN), "resolution_m": 60.0,
           "ops": []}
    ask.update(over)
    return tool.build_mesh(**ask)


def _stub_om2d(monkeypatch, tmp_path, *, stats=None):
    """Answer the container call with a known mesh, and record what was sent."""
    sent: dict[str, object] = {}

    def fake_run_op(rundir, op, config_name, produces, *, shoreline_dir=None):
        sent["config"] = json.loads(Path(rundir, config_name).read_text())
        sent["shoreline"] = None if shoreline_dir is None else str(shoreline_dir)
        sent["rundir"] = str(rundir)
        np.savez(Path(rundir, "om2d_mesh.npz"), points=_POINTS, cells=_CELLS,
                 pfix=np.empty((0, 2)))
        Path(rundir, "om2d_stats.json").write_text(json.dumps(
            stats or {"engine": "oceanmesh(test)",
                      "sizing_functions": ["polygon_sdf(interior)",
                                           "uniform(min_edge)"]}))

    def fake_pair(rundir, **kw):
        sent["pair"] = {k: v for k, v in kw.items() if k in ("roles", "title")}
        Path(rundir, "mesh.slf").write_bytes(b"slf")
        Path(rundir, "mesh.cli").write_text("2 2 2\n")
        return {"geo_slf": Path(rundir, "mesh.slf"), "cli": Path(rundir, "mesh.cli"),
                "stats": {"nptfr": 4, "n_liquid_boundaries": 1}}

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


# --------------------------------------------------------------------------- #
# One extent param, two kinds of value.
# --------------------------------------------------------------------------- #
def test_the_extent_param_takes_a_box_or_a_polygon():
    assert _recipe(extent=_AOI).extent == _AOI
    for supplied in (json.dumps(_BASIN), _BASIN):
        assert _recipe(extent=supplied).extent


def test_the_polygon_domain_is_the_om2d_mesher_not_a_second_one():
    from trid3nt_server.workflows.mesh.meshers import registered_meshers

    assert registered_meshers() == ("om2d", "reg_grid")


# --------------------------------------------------------------------------- #
# The config the box is handed on each path.
# --------------------------------------------------------------------------- #
def test_a_supplied_polygon_is_staged_for_the_box_with_no_shoreline(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe())
    config = sent["config"]
    assert config["domain_geojson"] == "/data/domain.geojson"
    assert config["shoreline_shp"] is None
    # No shoreline to mount: the box gets no /shoreline bind at all.
    assert sent["shoreline"] is None
    staged = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    assert staged["geometries"] == [_BASIN]


def test_the_box_is_seeded_inside_the_polygons_own_bounds(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe())
    assert tuple(sent["config"]["bbox"]) == (-75.78, 36.12, -75.72, 36.18)


@pytest.mark.parametrize("resolution_m", [120.0, 400.0, 1000.0])
def test_the_one_size_word_is_the_uniform_base_a_basin_is_meshed_at(
        monkeypatch, tmp_path, resolution_m):
    """A polygon interior with no sizing op sizes toward nothing, so the one size
    word IS the edge the whole domain gets: the number a template declares reaches
    the sizing function as the base, and a coarser ask is a coarser mesh rather
    than a refusal about a ceiling the caller never wrote."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(resolution_m=resolution_m))
    config = sent["config"]
    assert config["min_edge_length_m"] == pytest.approx(resolution_m)
    assert config["max_edge_length_m"] == pytest.approx(resolution_m * 10.0)
    assert config["pre_ops"] == []


def test_no_size_word_declared_keeps_the_meshers_own_visible_default(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(resolution_m=None))
    assert sent["config"]["min_edge_length_m"] == pytest.approx(
        OM2D._DEFAULT_RESOLUTION_M)
    assert sent["config"]["max_edge_length_m"] == pytest.approx(
        OM2D._DEFAULT_RESOLUTION_M * OM2D._MAX_EL_FACTOR)


def test_a_ceiling_an_op_states_is_never_overridden_by_the_multiple(
        monkeypatch, tmp_path):
    """The threading rule fills what an entry left unstated, and nothing else."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    lines = tmp_path / "channels.geojson"
    lines.write_text(json.dumps(_CHANNEL))
    OM2D.build(_recipe(resolution_m=100.0, ops=[
        mesh_op("distance_sizing_from_line_function", line_file=str(lines),
                max_edge_length=250.0)]))
    assert sent["config"]["pre_ops"][0]["kwargs"]["max_edge_length"] == 250.0
    assert sent["config"]["max_edge_length_m"] == pytest.approx(1000.0)


def test_the_shoreline_path_is_untouched_by_the_polygon_path(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(extent=_AOI))
    config = sent["config"]
    assert config["shoreline_shp"] == "/shoreline/GSHHS_i_L1.shp"
    assert config["domain_geojson"] is None
    assert tuple(config["bbox"]) == _AOI
    assert sent["shoreline"].endswith("shoreline")


def test_the_layer_a_chained_row_produced_enters_as_the_geometry_it_carries(
        monkeypatch, tmp_path):
    """The section tool returns a LAYER, and the extent takes it: read through the
    one typed conversion, so the author never writes ``.uri``."""
    from trid3nt_contracts.execution import LayerURI

    path = tmp_path / "section.geojson"
    path.write_text(json.dumps(_BASIN))
    layer = LayerURI(layer_id="section-1", name="Section of a polygon",
                     layer_type="vector", uri=str(path),
                     role="primary")
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(extent=layer))
    assert sent["config"]["domain_geojson"] == "/data/domain.geojson"
    staged = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    assert staged["geometries"] == [_BASIN]


def test_a_polygon_domain_can_come_from_a_file_a_tool_wrote(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    written = tmp_path / "basin.geojson"
    written.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": _BASIN, "properties": {}}]}))
    OM2D.build(_recipe(extent=str(written)))
    assert sent["config"]["domain_geojson"] == "/data/domain.geojson"


# --------------------------------------------------------------------------- #
# The provenance a polygon-domain mesh travels with.
# --------------------------------------------------------------------------- #
def test_the_mesh_states_that_its_domain_was_supplied_not_cut_from_a_shoreline(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe())
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["domain_source"] == "supplied polygon domain (1 part(s))"
    assert "GSHHG" not in provenance["sizing_source"]
    assert provenance["sizing_source"].startswith("supplied polygon domain")
    assert "polygon_sdf(interior)" in provenance["sizing_source"]


def test_the_shoreline_mesh_still_names_the_shoreline_it_was_cut_from(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path,
               stats={"sizing_functions": ["feature_sizing_function"]})
    mesh = OM2D.build(_recipe(extent=_AOI))
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["domain_source"] == "GSHHG land polygons (GSHHS_i_L1.shp)"


def test_the_boundary_record_carries_the_domain_it_was_walked_on(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe())
    info = mesh.meta["artifact"]["open_boundary_info"]
    assert info["source"] == "supplied polygon domain (1 part(s))"


# --------------------------------------------------------------------------- #
# The refusals each path owns.
# --------------------------------------------------------------------------- #
def test_an_extent_carrying_no_polygon_refuses_rather_than_widen_a_line(
        monkeypatch, tmp_path):
    # THE ruling: a flowline is not a domain. Nothing buffers it into one.
    _stub_om2d(monkeypatch, tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build(_recipe(extent=json.dumps(_CHANNEL)))
    assert excinfo.value.error_code == "MESH_DOMAIN_NOT_A_POLYGON"
    assert "no polygon" in str(excinfo.value)


def test_a_domain_in_projected_metres_refuses_before_it_becomes_a_lattice(
        monkeypatch, tmp_path):
    """Every sizing number here is degrees at the domain's own latitude, so an
    extent in metres does not read as a wrong answer - it reads as an allocation
    failure inside the triangulator, tens of GiB wide."""
    _stub_om2d(monkeypatch, tmp_path)
    albers = json.dumps({"type": "Polygon", "coordinates": [[
        [-2_100_000.0, 1_900_000.0], [-2_090_000.0, 1_900_000.0],
        [-2_090_000.0, 1_910_000.0], [-2_100_000.0, 1_910_000.0],
        [-2_100_000.0, 1_900_000.0]]]})
    with pytest.raises(MeshToolError) as excinfo:
        OM2D.build(_recipe(extent=albers))
    assert excinfo.value.error_code == "MESH_DOMAIN_NOT_LONLAT"
    assert "EPSG:4326" in str(excinfo.value)


def test_a_lattice_refuses_a_polygon_and_escalates_to_the_mesher_that_takes_one():
    recipe = tool.build_mesh(mesher="reg_grid", extent=json.dumps(_BASIN),
                             resolution_m=30.0)
    with pytest.raises(MeshToolError) as excinfo:
        REG_GRID.build(recipe)
    assert excinfo.value.error_code == "MESH_POLYGON_DOMAIN_UNSUPPORTED"
    assert excinfo.value.escalation["tool"] == "build_mesh"
    assert excinfo.value.escalation["overrides"]["mesher"] == "om2d"


def test_a_lattice_still_builds_from_a_box():
    recipe = tool.build_mesh(mesher="reg_grid", extent=_AOI, resolution_m=2000.0)
    assert REG_GRID.build(recipe).node_count > 0


# --------------------------------------------------------------------------- #
# The chain: one tool's polygon is the next tool's domain.
# --------------------------------------------------------------------------- #
def test_a_section_the_tool_produced_meshes_as_the_domain(monkeypatch, tmp_path):
    from trid3nt_server.tools.processing.section.section import section

    sent = _stub_om2d(monkeypatch, tmp_path)
    water = json.dumps({"type": "Polygon", "coordinates": [[
        [-75.80, 36.12], [-75.70, 36.12], [-75.70, 36.18], [-75.80, 36.18],
        [-75.80, 36.12]]]})
    reach = section(water, between=[(-75.78, 36.15), (-75.72, 36.15)],
                    _output_dir=str(tmp_path))
    OM2D.build(_recipe(extent=reach.uri))
    assert sent["config"]["domain_geojson"] == "/data/domain.geojson"
    staged = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    assert staged["geometries"][0]["type"] == "Polygon"


# --------------------------------------------------------------------------- #
# The in-container geometry, against a stub oceanmesh.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def driver(monkeypatch):
    """The om2d driver with a stub ``oceanmesh`` - the real one is GPL, in the image."""
    stub = types.ModuleType("oceanmesh")

    class Domain:
        def __init__(self, bbox, func):
            self.bbox, self.func = bbox, func

    stub.Domain = Domain
    monkeypatch.setitem(sys.modules, "oceanmesh", stub)
    monkeypatch.syspath_prepend(str(Path(OM2D.__file__).parent / "drivers"))
    sys.modules.pop("om2d_driver", None)
    import om2d_driver

    yield om2d_driver
    sys.modules.pop("om2d_driver", None)


def test_the_polygon_signed_distance_is_negative_inside_and_positive_outside(driver):
    from shapely.geometry import shape

    domain = driver._PolygonDomain([shape(_BASIN)], 0.002,
                                   (-75.80, -75.70, 36.10, 36.20))
    signed = domain.signed(np.array([[-75.75, 36.15],    # middle
                                     [-75.60, 36.15]]))  # well outside
    assert signed[0] < 0.0 and signed[1] > 0.0
    # The distance is to the boundary, so the middle of a 0.06-deg-tall basin
    # reads roughly a half-height in.
    assert abs(signed[0]) == pytest.approx(0.03, abs=0.005)


def test_a_domain_with_no_sizing_op_meshes_at_the_one_size_word(driver):
    """No sizing op is not a missing sizing function: it is a uniform mesh."""
    build = _stub_build(driver)
    edge = build.edge_length()
    values = edge(np.array([[-75.75, 36.15], [-75.73, 36.17]]))
    assert np.allclose(values, build.min_deg)
    assert "uniform(min_edge)" in build.active


def test_the_uniform_edge_is_clamped_to_the_band_and_answers_a_nan_probe(driver):
    build = _stub_build(driver)
    edge = build.edge_length()
    assert np.isfinite(edge(np.array([[np.nan, np.nan]]))).all()


def test_a_nan_probe_is_answered_rather_than_propagated(driver):
    from shapely.geometry import shape

    domain = driver._PolygonDomain([shape(_BASIN)], 0.002,
                                   (-75.80, -75.70, 36.10, 36.20))
    assert np.isfinite(domain.signed(np.array([[np.nan, np.nan]]))).all()


def _stub_build(driver):
    """A ``_Build`` over the basin, with the domain staged and nothing sized yet."""
    from shapely.geometry import shape

    build = object.__new__(driver._Build)
    build.bbox = (-75.78, -75.72, 36.12, 36.18)
    build.mpd = 111_320.0 * 0.8
    build.min_deg = 0.001
    build.max_deg = 0.010
    build.notes = []
    build.active = []
    build.sizing = []
    build.holes = None
    build.hole_geoms = []
    build.shoreline = None
    build.smoothed = None
    build.sdf = driver._PolygonDomain([shape(_BASIN)], 0.002, build.bbox)
    return build
