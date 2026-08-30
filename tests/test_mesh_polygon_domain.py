"""Offline tests for the POLYGON-DOMAIN path: a domain another tool produced.

``build_mesh``'s extent takes a bbox or a polygon. The bbox path cuts the water
side of the GSHHG shoreline; the polygon path meshes the interior of what it is
handed, with the signed distance measured against that polygon's own boundary.
Both go to the same ``om.generate_mesh`` in the same box, so what is tested here
is the seam: the config the box is handed, the refusals each path owns, and the
provenance the mesh travels with.

The container call and the two world reads (the bed fetch, the TELEMAC writer)
are stubbed; the composition is the real code. The in-container geometry itself
is exercised against a stub ``oceanmesh`` module, because the real one is GPL
and lives only in the image.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.mesh.meshers import MeshToolError, get_mesher
from trid3nt_server.workflows.mesh.meshers import om2d as OM2D
from trid3nt_server.workflows.mesh.meshers import reg_grid as REG_GRID
from trid3nt_server.workflows.mesh.tool import validate_spec

_AOI = (-75.80, 36.10, -75.70, 36.20)

#: A basin-shaped domain another tool produced, and the channel inside it.
_BASIN = {"type": "Polygon", "coordinates": [[
    [-75.78, 36.12], [-75.72, 36.12], [-75.72, 36.18], [-75.78, 36.18],
    [-75.78, 36.12]]]}
_CHANNEL = {"type": "LineString", "coordinates": [[-75.77, 36.13], [-75.73, 36.17]]}

_POINTS = np.array([[-75.78, 36.12], [-75.74, 36.12],
                    [-75.78, 36.16], [-75.74, 36.16]])
_CELLS = np.array([[0, 1, 2], [1, 3, 2]], dtype=np.int64)


def _stub_om2d(monkeypatch, tmp_path, *, stats=None):
    """Answer the container call with a known mesh, and record what was sent."""
    sent: dict[str, object] = {}

    def fake_bed(bed, aoi, rundir):
        sent["bed_aoi"] = tuple(aoi)
        Path(rundir, "bed.tif").write_bytes(b"tif")
        return Path(rundir, "bed.tif"), "fetch_topobathy: cudem_nearshore 100%", None

    def fake_run(rundir, shoreline):
        sent["config"] = json.loads(Path(rundir, "om2d_config.json").read_text())
        sent["shoreline"] = None if shoreline is None else str(shoreline)
        sent["rundir"] = str(rundir)
        np.savez(Path(rundir, "om2d_mesh.npz"), points=_POINTS, cells=_CELLS,
                 pfix=np.empty((0, 2)))
        Path(rundir, "om2d_stats.json").write_text(json.dumps(
            stats or {"engine": "oceanmesh(test)",
                      "sizing_functions": ["polygon_sdf(interior)",
                                           "uniform(min_edge)"]}))

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
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.selafin_cli.write_telemac_pair",
        fake_pair)
    monkeypatch.setattr(
        "trid3nt_server.workflows.mesh.shared.nodes.sample_raster_at_nodes",
        lambda path, pts: np.full(np.asarray(pts).shape[0], -4.0))
    return sent


# --------------------------------------------------------------------------- #
# The declaration: one extent field, two kinds of value.
# --------------------------------------------------------------------------- #
def test_the_extent_field_takes_a_box_or_a_polygon():
    assert validate_spec("om2d", {"extent": _AOI}).fields["extent"] == _AOI
    for supplied in (json.dumps(_BASIN), _BASIN):
        assert validate_spec("om2d", {"extent": supplied}).fields["extent"]


def test_the_polygon_domain_is_the_om2d_mesher_not_a_second_one():
    assert set(get_mesher("om2d").fields) == set(
        get_mesher("om2d").fields) and "domain" not in get_mesher("om2d").fields


# --------------------------------------------------------------------------- #
# The config the box is handed on each path.
# --------------------------------------------------------------------------- #
def test_a_supplied_polygon_is_staged_for_the_box_with_no_shoreline(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build({"extent": json.dumps(_BASIN)})
    config = sent["config"]
    assert config["domain_geojson"] == "/data/domain.geojson"
    assert config["shoreline_shp"] is None
    # No shoreline to mount: the box gets no /shoreline bind at all.
    assert sent["shoreline"] is None
    staged = json.loads(Path(sent["rundir"], "domain.geojson").read_text())
    assert staged["geometries"] == [_BASIN]


def test_the_box_is_seeded_inside_the_polygons_own_bounds(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build({"extent": json.dumps(_BASIN)})
    assert tuple(sent["config"]["bbox"]) == (-75.78, 36.12, -75.72, 36.18)
    # The bed is fetched over the domain, not over some wider AOI.
    assert sent["bed_aoi"] == (-75.78, 36.12, -75.72, 36.18)


def test_polylines_supplied_with_the_domain_become_its_sizing_source(
        monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build({"extent": json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": _BASIN, "properties": {}},
        {"type": "Feature", "geometry": _CHANNEL, "properties": {}}]})})
    assert sent["config"]["sizing_coords"] == [[-75.77, 36.13], [-75.73, 36.17]]
    assert sent["config"]["domain_geojson"] == "/data/domain.geojson"


def test_a_polygon_with_no_polyline_sizes_toward_nothing(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build({"extent": json.dumps(_BASIN)})
    assert sent["config"]["sizing_coords"] == []


def test_the_shoreline_path_is_untouched_by_the_polygon_path(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    OM2D.build({"extent": _AOI})
    config = sent["config"]
    assert config["shoreline_shp"] == "/shoreline/GSHHS_i_L1.shp"
    assert config["domain_geojson"] is None and config["sizing_coords"] == []
    assert tuple(config["bbox"]) == _AOI
    assert sent["shoreline"].endswith("GSHHS_i_L1.shp")


def test_a_polygon_domain_can_come_from_a_file_a_tool_wrote(monkeypatch, tmp_path):
    sent = _stub_om2d(monkeypatch, tmp_path)
    written = tmp_path / "basin.geojson"
    written.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": _BASIN, "properties": {}}]}))
    OM2D.build({"extent": str(written)})
    assert sent["config"]["domain_geojson"] == "/data/domain.geojson"


# --------------------------------------------------------------------------- #
# The provenance a polygon-domain mesh travels with.
# --------------------------------------------------------------------------- #
def test_the_mesh_states_that_its_domain_was_supplied_not_cut_from_a_shoreline(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": json.dumps(_BASIN)})
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["domain_source"] == "supplied polygon domain (1 part(s))"
    assert "GSHHG" not in provenance["sizing_source"]
    assert provenance["sizing_source"].startswith("supplied polygon domain")
    assert "polygon_sdf(interior)" in provenance["sizing_source"]


def test_the_shoreline_mesh_still_names_the_shoreline_it_was_cut_from(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path,
               stats={"sizing_functions": ["feature_sizing(distance_to_shore)"]})
    mesh = OM2D.build({"extent": _AOI})
    provenance = mesh.meta["artifact"]["provenance"]
    assert provenance["domain_source"] == "GSHHG land polygons (GSHHS_i_L1.shp)"


def test_the_boundary_record_carries_the_domain_it_was_walked_on(
        monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": json.dumps(_BASIN)})
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
        OM2D.build({"extent": json.dumps(_CHANNEL)})
    assert excinfo.value.error_code == "MESH_DOMAIN_NOT_A_POLYGON"
    assert "no polygon" in str(excinfo.value)


def test_a_region_refine_on_a_polygon_domain_refuses_by_name(monkeypatch, tmp_path):
    _stub_om2d(monkeypatch, tmp_path)
    mesh = OM2D.build({"extent": json.dumps(_BASIN)})
    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("om2d").action("refine_region").apply(
            mesh, geometry=json.dumps(_BASIN), edge_length=20.0)
    assert excinfo.value.error_code == "MESH_REGION_ON_POLYGON_DOMAIN"


def test_a_lattice_refuses_a_polygon_and_escalates_to_the_mesher_that_takes_one():
    with pytest.raises(MeshToolError) as excinfo:
        REG_GRID.build({"extent": json.dumps(_BASIN), "resolution_m": 30.0})
    assert excinfo.value.error_code == "MESH_POLYGON_DOMAIN_UNSUPPORTED"
    assert excinfo.value.escalation["tool"] == "build_mesh"
    assert excinfo.value.escalation["overrides"]["mesher"] == "om2d"


def test_a_lattice_still_builds_from_a_box():
    mesh = REG_GRID.build({"extent": _AOI, "resolution_m": 2000.0})
    assert mesh.node_count > 0


# --------------------------------------------------------------------------- #
# The chain: one tool's polygon is the next tool's domain.
# --------------------------------------------------------------------------- #
def test_a_section_the_tool_produced_meshes_as_the_domain(monkeypatch, tmp_path):
    from trid3nt_server.tools.processing.section.section import section

    sent = _stub_om2d(monkeypatch, tmp_path)
    banks = json.dumps({"type": "Polygon", "coordinates": [[
        [-75.80, 36.12], [-75.70, 36.12], [-75.70, 36.18], [-75.80, 36.18],
        [-75.80, 36.12]]]})
    reach = section(banks, between=[(-75.78, 36.15), (-75.72, 36.15)],
                    _output_dir=str(tmp_path))
    OM2D.build({"extent": reach.uri})
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
    monkeypatch.syspath_prepend(
        str(Path(OM2D.__file__).parent / "drivers"))
    sys.modules.pop("om2d_driver", None)
    import om2d_driver

    yield om2d_driver
    sys.modules.pop("om2d_driver", None)


def test_the_polygon_signed_distance_is_negative_inside_and_positive_outside(driver):
    from shapely.geometry import shape

    domain = driver._PolygonDomain([shape(_BASIN)], 0.002, (-75.80, -75.70, 36.10, 36.20))
    signed = domain.signed(np.array([[-75.75, 36.15],   # middle
                                     [-75.60, 36.15]]))  # well outside
    assert signed[0] < 0.0 and signed[1] > 0.0
    # The distance is to the boundary, so the middle of a 0.06-deg-tall basin
    # reads roughly a half-height in.
    assert abs(signed[0]) == pytest.approx(0.03, abs=0.005)


def test_a_polygon_with_nothing_to_size_toward_meshes_at_the_finest_edge(driver):
    active: list[str] = []
    edge = driver._polygon_sizing(None, 0.001, 0.010, 0.15, None, active)
    values = edge(np.array([[-75.75, 36.15], [-75.73, 36.17]]))
    assert np.allclose(values, 0.001)
    assert active == ["polygon_sdf(interior)", "uniform(min_edge)"]


def test_the_edge_grows_away_from_the_polylines_and_is_clamped_to_the_band(driver):
    active: list[str] = []
    edge = driver._polygon_sizing([[-75.75, 36.15]], 0.001, 0.010, 0.15, None, active)
    on_line, far = edge(np.array([[-75.75, 36.15], [-75.00, 36.15]]))
    assert on_line == pytest.approx(0.001)
    assert far == pytest.approx(0.010)  # clamped at the coarse end
    assert any("distance_to_sizing_polylines" in note for note in active)


def test_a_nan_probe_is_answered_rather_than_propagated(driver):
    from shapely.geometry import shape

    domain = driver._PolygonDomain([shape(_BASIN)], 0.002, (-75.80, -75.70, 36.10, 36.20))
    assert np.isfinite(domain.signed(np.array([[np.nan, np.nan]]))).all()
    edge = driver._polygon_sizing([[-75.75, 36.15]], 0.001, 0.010, 0.15, None, [])
    assert np.isfinite(edge(np.array([[np.nan, np.nan]]))).all()
