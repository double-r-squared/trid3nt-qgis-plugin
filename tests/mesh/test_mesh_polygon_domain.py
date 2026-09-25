"""Offline tests for the ONE domain path: the closed polygon a run solves over.

A recipe's extent is that polygon - drawn, the user's layer, or one a producer
measured - and the mesher meshes its interior against its own boundary. THE
SHORELINE IS THAT BOUNDARY: the sizing functions measure the polygon's edge minus
the stretches an inflow, an outflow or an open run names. What is tested is the
seam: the config, the refusals it owns, the provenance, and which edge is shore."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.mesh.meshers import MeshToolError
from trid3nt_server.mesh.meshers import om2d as OM2D
from trid3nt_server.mesh.meshers import reg_grid as REG_GRID
from trid3nt_server.mesh.tool import mesh_op, tool

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

    def fake_run_op(rundir, op, config_name, produces):
        sent["config"] = json.loads(Path(rundir, config_name).read_text())
        sent["rundir"] = str(rundir)
        np.savez(Path(rundir, "om2d_mesh.npz"), points=_POINTS, cells=_CELLS,
                 pfix=np.empty((0, 2)))
        Path(rundir, "om2d_stats.json").write_text(json.dumps(
            stats or {"engine": "oceanmesh(test)",
                      "sizing_functions": ["polygon_sdf(interior)",
                                           "uniform(min_edge)"]}))

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(OM2D, "_run_op", fake_run_op)
    return sent


def test_the_extent_param_takes_the_polygon_however_it_was_written():
    for supplied in (json.dumps(_BASIN), _BASIN):
        assert _recipe(extent=supplied).extent


def test_the_polygon_domain_is_the_om2d_mesher_not_a_second_one():
    from trid3nt_server.mesh.meshers import registered_meshers

    assert registered_meshers() == ("om2d", "reg_grid")


def test_the_polygon_is_staged_and_nothing_else_is_mounted(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe())
    config = sent["config"]
    assert config["domain_geojson"] == "/data/domain.geojson"
    assert config["open_runs_geojson"] is None
    staged = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    assert staged["geometries"] == [_BASIN]


def test_a_domains_own_runs_travel_as_the_edge_the_water_crosses(
        monkeypatch, tmp_path):
    """The shoreline is the edge MINUS the runs, so which stretches the water
    crosses has to reach the box; a wall run prescribes nothing and stays shore."""
    from trid3nt_server.inputs.domain import domain as ingest

    sent = _stub_om2d(monkeypatch, tmp_path)
    reach = ingest({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"part": "reach"}, "geometry": _BASIN},
        {"type": "Feature", "properties": {"part": "inflow"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-75.78, 36.12], [-75.78, 36.18]]}},
        {"type": "Feature", "properties": {"part": "outflow"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-75.72, 36.12], [-75.72, 36.18]]}}]})
    OM2D.build(_recipe(extent=reach))
    assert sent["config"]["open_runs_geojson"] == "/data/open_runs.geojson"
    staged = json.loads(Path(sent["rundir"], "open_runs.geojson").read_text())
    assert [f["properties"]["type"] for f in staged["features"]] == [
        "inflow", "outflow"]


def test_a_drawn_pond_states_no_run_so_its_whole_edge_is_shore(
        monkeypatch, tmp_path):
    from trid3nt_server.inputs.domain import domain as ingest

    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(extent=ingest(_BASIN)))
    assert sent["config"]["open_runs_geojson"] is None


def test_the_box_is_seeded_inside_the_polygons_own_bounds(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe())
    assert tuple(sent["config"]["bbox"]) == (-75.78, 36.12, -75.72, 36.18)


@pytest.mark.parametrize("resolution_m", [120.0, 400.0, 1000.0])
def test_the_one_size_word_is_the_uniform_base_a_basin_is_meshed_at(
        monkeypatch, tmp_path, resolution_m):
    """A polygon interior with no sizing op sizes toward nothing, so the one size word
    IS the edge the whole domain gets, and a coarser ask is a coarser mesh rather
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


def test_the_mesh_states_the_polygon_it_was_cut_from(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe())
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["domain_source"] == "domain polygon (1 part(s))"
    assert "GSHHG" not in provenance["sizing_source"]
    assert provenance["sizing_source"].startswith("domain polygon")
    assert "polygon_sdf(interior)" in provenance["sizing_source"]


def test_the_boundary_record_carries_the_domain_it_was_walked_on(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build(_recipe())
    info = mesh.meta["artifact"]["open_boundary_info"]
    assert info["source"] == "domain polygon (1 part(s))"


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


def test_a_stretch_the_domain_slot_cut_meshes_as_the_domain(monkeypatch, tmp_path):
    """The domain slot cuts the mapped water surface square to the line it was
    handed; the mesher takes that polygon the way it takes any fetched vector."""
    from shapely.geometry import LineString, shape

    from trid3nt_server.inputs.domain import _cut_square

    sent = _stub_om2d(monkeypatch, tmp_path)
    water = {"type": "Polygon", "coordinates": [[
        [-75.80, 36.12], [-75.70, 36.12], [-75.70, 36.18], [-75.80, 36.18],
        [-75.80, 36.12]]]}
    geom, _faces = _cut_square(
        [shape(water)], LineString([(-75.78, 36.15), (-75.72, 36.15)]),
        "domain", "DOMAIN_INVALID")
    path = tmp_path / "stretch.geojson"
    path.write_text(json.dumps(geom))
    OM2D.build(_recipe(extent=str(path)))
    assert sent["config"]["domain_geojson"] == "/data/domain.geojson"
    staged = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    assert staged["geometries"][0]["type"] == "Polygon"


@pytest.fixture()
def driver(monkeypatch):
    """The om2d driver with a stub ``oceanmesh`` - the real one is GPL, in the image."""
    stub = types.ModuleType("oceanmesh")

    class Domain:
        def __init__(self, bbox, func):
            self.bbox, self.func = bbox, func

    stub.Domain = Domain
    monkeypatch.setitem(sys.modules, "oceanmesh", stub)
    from trid3nt_server.workflows.solver.image_script import scripts_dir
    monkeypatch.syspath_prepend(str(scripts_dir("mesh")))
    sys.modules.pop("om2d", None)
    import om2d

    yield om2d
    sys.modules.pop("om2d", None)


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
    build.smoothed = None
    build.region = None
    build.domain_rings = [ring for ring in _BASIN["coordinates"]]
    build.shoreline = driver._EdgeShoreline(build.domain_rings, [], build.min_deg,
                                            build.bbox)
    build.sdf = driver._PolygonDomain([shape(_BASIN)], 0.002, build.bbox)
    return build


def test_the_shoreline_the_sizing_functions_measure_is_the_polygons_own_edge(driver):
    """A sizing function reads four things off a shoreline; the polygon's edge
    answers all four, so feature sizing binds on any domain, coastal or not."""
    build = _stub_build(driver)
    shore = build.shoreline
    assert shore.bbox == build.bbox and shore.h0 == build.min_deg
    assert shore.points.shape[1] == 2 and shore.points.shape[0] > 4
    assert shore.mainland.shape[0] == shore.points.shape[0]
    assert shore.inner.shape == (0, 2)
    assert build.environment()["shoreline"] is shore


def test_a_reach_with_two_open_runs_drops_those_stretches_from_the_shore(driver):
    """The banks are shore and the two end faces are not: what the water crosses
    is not where it meets land."""
    rings = list(_BASIN["coordinates"])
    inflow = [(-75.78, 36.12), (-75.78, 36.18)]
    outflow = [(-75.72, 36.12), (-75.72, 36.18)]
    whole = driver._EdgeShoreline(rings, [], 0.0005, (-75.78, -75.72, 36.12, 36.18))
    cut = driver._EdgeShoreline(rings, [inflow, outflow], 0.0005,
                                (-75.78, -75.72, 36.12, 36.18))
    assert cut.dropped > 0
    assert cut.points.shape[0] < whole.points.shape[0]
    # Nothing ALONG either face survives: a face's own end is a corner the bank
    # shares with it, and that is the only point of it that can still be shore.
    for face in (inflow, outflow):
        middle = np.array([0.5 * (face[0][0] + face[1][0]),
                           0.5 * (face[0][1] + face[1][1])])
        assert np.hypot(*(cut.points - middle).T).min() > 0.02


def test_an_island_ring_is_shore_too(driver):
    """Islands are land: the water meets them, so they size the mesh."""
    lake = [_BASIN["coordinates"][0],
            [[-75.755, 36.148], [-75.745, 36.148], [-75.745, 36.152],
             [-75.755, 36.152], [-75.755, 36.148]]]
    shore = driver._EdgeShoreline(lake, [], 0.0005, (-75.78, -75.72, 36.12, 36.18))
    assert shore.inner.shape[0] > 0
    assert shore.points.shape[0] == shore.inner.shape[0] + shore.mainland.shape[0]


def test_a_domain_whose_whole_edge_is_crossed_refuses_by_name(driver):
    """No shore is no domain: a sizing function would measure nothing."""
    ring = [[[0.0, 0.0], [0.01, 0.0], [0.01, 0.01], [0.0, 0.01], [0.0, 0.0]]]
    corners = [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)]
    whole_edge = [[corners[i], corners[(i + 1) % 4]] for i in range(4)]
    with pytest.raises(driver._Refusal) as excinfo:
        driver._EdgeShoreline(ring, whole_edge, 0.005, (0.0, 0.01, 0.0, 0.01))
    assert excinfo.value.document["code"] == "MESH_DOMAIN_HAS_NO_SHORELINE"


#: A box with a coast cut through it: the water east of a line that enters at the
#: south edge and leaves at the north. OSM draws land on the LEFT of the way, so a
#: northward walk leaves the water on its east.
_CUT_BOX = (-75.80, 36.10, -75.70, 36.20)
_COAST = {"type": "LineString",
          "coordinates": [[-75.78, 36.10], [-75.77, 36.20]]}


def _cut_water(tmp_path):
    """The water polygon a coastline leaves in ``_CUT_BOX``, staged as a file."""
    from trid3nt_server.mesh.water import water_polygon

    path = tmp_path / "water.geojson"
    path.write_text(json.dumps(water_polygon(_COAST, _CUT_BOX)))
    return path


def test_the_box_a_cut_leaves_behind_is_open_edge_rather_than_shoreline(
        monkeypatch, tmp_path):
    """A bbox edge stands in open water: the sizing measures the land-water
    segments only, so every stretch of the boundary on the box travels as the
    edge that is not shoreline."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(extent=str(_cut_water(tmp_path))))
    assert sent["config"]["open_runs_geojson"] == "/data/open_runs.geojson"
    staged = json.loads(Path(sent["rundir"], "open_runs.geojson").read_text())
    faces = [f["geometry"]["coordinates"] for f in staged["features"]]
    assert {f["properties"]["type"] for f in staged["features"]} == {"open"}
    assert len(faces) == 3, "the south, east and north edges of the box"
    for (x0, y0), (x1, y1) in faces:
        assert min(abs(x0 - x1), abs(y0 - y1)) < 1.0e-9
        assert x0 in _CUT_BOX[::2] or y0 in _CUT_BOX[1::2]


#: An island standing well inside the box, as a hole ring in the water polygon.
_ISLAND = [[-75.75, 36.14], [-75.74, 36.14], [-75.74, 36.15], [-75.75, 36.14]]


def _with_island(polygon):
    """``polygon`` with the island punched out of it as a second ring."""
    return {"type": "Polygon",
            "coordinates": [[list(xy) for xy in polygon["coordinates"][0]],
                            _ISLAND]}


def test_an_island_hole_does_not_make_the_cut_boxs_edges_shore(
        monkeypatch, tmp_path):
    """The all-shore guard reads the OUTER ring: a coast still cuts the box open
    however many islands stand inside it."""
    from trid3nt_server.mesh.water import water_polygon

    sent = _stub_om2d(monkeypatch, tmp_path)
    path = tmp_path / "island_water.geojson"
    path.write_text(json.dumps(_with_island(water_polygon(_COAST, _CUT_BOX))))
    OM2D.build(_recipe(extent=str(path)))
    staged = json.loads(Path(sent["rundir"], "open_runs.geojson").read_text())
    assert len(staged["features"]) == 3, "the south, east and north edges"


def test_an_island_hole_does_not_open_a_box_that_was_drawn_as_the_box(
        monkeypatch, tmp_path):
    """A drawn box is all shore however many islands stand inside it: the guard
    never counts a hole's segments against the outer ring's."""
    from trid3nt_server.inputs.domain import domain as ingest

    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(extent=ingest(_with_island(_BASIN))))
    assert sent["config"]["open_runs_geojson"] is None


def test_the_shoreline_sizing_is_coarse_on_the_box_and_fine_on_the_coast(
        monkeypatch, tmp_path, driver):
    """The one classification the mesher stages is the one the sizing reads: the
    distance-from-shoreline field that sizes the mesh is a box-width out along the
    open edges and a stone's throw along the coast."""
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build(_recipe(extent=str(_cut_water(tmp_path))))
    staged = json.loads(Path(sent["rundir"], "open_runs.geojson").read_text())
    faces = [f["geometry"]["coordinates"] for f in staged["features"]]
    rings = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    ring = rings["geometries"][0]["coordinates"]

    bbox = (_CUT_BOX[0], _CUT_BOX[2], _CUT_BOX[1], _CUT_BOX[3])
    whole = driver._EdgeShoreline(ring, [], 0.0005, bbox)
    shore = driver._EdgeShoreline(ring, faces, 0.0005, bbox)
    assert 0 < shore.points.shape[0] < whole.points.shape[0]

    def nearest(x, y):
        return float(np.hypot(*(shore.points - np.array([x, y])).T).min())

    assert nearest(-75.774, 36.15) < 0.002, "just off the coast: fine"
    assert nearest(-75.700, 36.15) > 0.05, "the box's east edge: coarse"
    assert nearest(-75.740, 36.10) > 0.02, "the box's south edge: coarse"
