"""Offline tests for the TELEMAC rain-on-grid mesh-acquisition step + CN-path
selector.

Only the PURE helpers are exercised here (config building, the exterior/river
extraction, UTM projection, node-field assembly, the supplied-mesh precondition
gate, the SELAFIN writer round-trip, and the automatic native-vs-preprocessing
runoff-path selection). The container-driven ``acquire_watershed_mesh`` needs the
mesh image + network and is proven live.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest
from shapely.geometry import LineString, Polygon

from trid3nt_server.agent.workflows.telemac.rain_on_grid.cn_infiltration import (
    CNInfiltrationError,
    RunoffPathDecision,
    select_runoff_path,
)
from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
    MeshAcquisitionError,
    assemble_node_fields,
    build_mesh_config,
    catchment_exterior_and_river_coords,
    reproject_nodes_to_utm,
    use_supplied_mesh,
    _ipobo_from_cells,
    _write_bottom_selafin,
)


# --------------------------------------------------------------------------- #
# CN-path selection (native vs preprocessing).
# --------------------------------------------------------------------------- #
def test_constant_intensity_selects_native():
    d = select_runoff_path(constant_intensity_mm_per_hr=12.5)
    assert isinstance(d, RunoffPathDecision)
    assert d.path == "native"
    assert d.time_varying is False
    assert "RAINFALL-RUNOFF MODEL=1" in d.reason


def test_time_varying_hyetograph_selects_preprocessing():
    d = select_runoff_path(hyetograph_mm=[2.0, 8.0, 15.0, 6.0, 1.0])
    assert d.path == "preprocessing"
    assert d.time_varying is True
    assert "RAINFALL-RUNOFF MODEL=0" in d.reason


def test_flat_hyetograph_selects_native():
    # a hyetograph that is one flat non-zero rate is NOT time-varying.
    d = select_runoff_path(hyetograph_mm=[5.0, 5.0, 5.0, 0.0])
    assert d.path == "native"
    assert d.time_varying is False


def test_no_forcing_raises():
    with pytest.raises(CNInfiltrationError):
        select_runoff_path()


# --------------------------------------------------------------------------- #
# Mesh config building.
# --------------------------------------------------------------------------- #
def test_build_mesh_config_ok():
    box = [[-83.5, 35.0], [-83.4, 35.0], [-83.4, 35.09], [-83.5, 35.09], [-83.5, 35.0]]
    river = [[-83.45, 35.04], [-83.44, 35.05]]
    cfg = build_mesh_config(box, river, min_edge_length_m=40.0, max_edge_length_m=400.0)
    assert cfg["min_edge_length_m"] == 40.0
    assert cfg["max_edge_length_m"] == 400.0
    assert len(cfg["boubox_coords"]) == 5
    assert len(cfg["river_coords"]) == 2
    assert cfg["grade"] == 0.20


def test_build_mesh_config_bad_edge_band():
    box = [[0, 0], [1, 0], [1, 1], [0, 0]]
    with pytest.raises(MeshAcquisitionError) as ei:
        build_mesh_config(box, [], min_edge_length_m=400.0, max_edge_length_m=40.0)
    assert ei.value.error_code == "TELEMAC_ROG_MESH_EDGE_BAND_INVALID"


def test_build_mesh_config_degenerate_ring():
    with pytest.raises(MeshAcquisitionError) as ei:
        build_mesh_config([[0, 0], [1, 1]], [], min_edge_length_m=40.0, max_edge_length_m=400.0)
    assert ei.value.error_code == "TELEMAC_ROG_MESH_DOMAIN_DEGENERATE"


# --------------------------------------------------------------------------- #
# Exterior + river extraction.
# --------------------------------------------------------------------------- #
def test_exterior_and_river_clip():
    catch = Polygon([(0, 0), (0.02, 0), (0.02, 0.02), (0, 0.02)])
    # one line crossing the catchment, one fully outside.
    inside = LineString([(0.005, 0.005), (0.015, 0.015)])
    outside = LineString([(1.0, 1.0), (1.1, 1.1)])

    class _GDF:
        geometry = [inside, outside]

    boubox, river = catchment_exterior_and_river_coords(
        catch, _GDF(), min_edge_length_m=40.0)
    assert len(boubox) >= 4
    # only the inside line contributes sizing points.
    assert river
    assert all(0.0 <= x <= 0.02 and 0.0 <= y <= 0.02 for x, y in river)


def test_exterior_no_flowlines():
    catch = Polygon([(0, 0), (0.02, 0), (0.02, 0.02), (0, 0.02)])
    boubox, river = catchment_exterior_and_river_coords(
        catch, None, min_edge_length_m=40.0)
    assert len(boubox) >= 4
    assert river == []


# --------------------------------------------------------------------------- #
# UTM projection.
# --------------------------------------------------------------------------- #
def test_reproject_to_utm_coweeta():
    # Coweeta NC ~ (-83.4, 35.05) -> UTM 17N = EPSG 32617.
    pts = np.array([[-83.40, 35.05], [-83.41, 35.06], [-83.39, 35.04]])
    xy, epsg = reproject_nodes_to_utm(pts)
    assert epsg == 32617
    assert xy.shape == (3, 2)
    # eastings ~ a few hundred km, northings ~ 3.88 M m in zone 17N.
    assert 2e5 < xy[:, 0].mean() < 8e5
    assert 3.8e6 < xy[:, 1].mean() < 3.95e6


# --------------------------------------------------------------------------- #
# Node-field assembly (CN2 + Manning).
# --------------------------------------------------------------------------- #
def test_assemble_node_fields_distributed():
    # 41 = deciduous forest -> CN 80 / n 0.20; 22 = developed low -> CN 89 / n 0.10
    cn2, manning = assemble_node_fields(
        node_nlcd=[41, 22, 41], uniform_cn=None,
        slopes_m_per_m=None, steep_slope_correction=False)
    assert cn2 == [80.0, 89.0, 80.0]
    assert manning == [0.20, 0.10, 0.20]


def test_assemble_node_fields_uniform_cn_keeps_landcover_manning():
    cn2, manning = assemble_node_fields(
        node_nlcd=[41, 22], uniform_cn=75.0,
        slopes_m_per_m=None, steep_slope_correction=False)
    assert cn2 == [75.0, 75.0]           # uniform override
    assert manning == [0.20, 0.10]       # Manning still from land cover


def test_assemble_node_fields_needs_landcover():
    with pytest.raises(MeshAcquisitionError) as ei:
        assemble_node_fields(
            node_nlcd=None, uniform_cn=75.0,
            slopes_m_per_m=None, steep_slope_correction=False)
    assert ei.value.error_code == "TELEMAC_ROG_NODE_FIELDS_MISSING"


# --------------------------------------------------------------------------- #
# Supplied-mesh precondition gate.
# --------------------------------------------------------------------------- #
def test_use_supplied_mesh_missing(tmp_path):
    with pytest.raises(MeshAcquisitionError) as ei:
        use_supplied_mesh(
            mesh_path=str(tmp_path / "nope.slf"),
            pour_point=(-83.4, 35.05), utm_epsg=32617)
    assert ei.value.error_code == "TELEMAC_ROG_SUPPLIED_MESH_MISSING"


def test_use_supplied_mesh_wrong_type(tmp_path):
    f = tmp_path / "mesh.txt"
    f.write_text("not a mesh")
    with pytest.raises(MeshAcquisitionError) as ei:
        use_supplied_mesh(
            mesh_path=str(f), pour_point=(-83.4, 35.05), utm_epsg=32617)
    assert ei.value.error_code == "TELEMAC_ROG_SUPPLIED_MESH_UNSUPPORTED"


def test_use_supplied_mesh_ok(tmp_path):
    f = tmp_path / "mesh.slf"
    f.write_bytes(b"\x00" * 128)
    wm = use_supplied_mesh(
        mesh_path=str(f), pour_point=(-83.4, 35.05), utm_epsg=32617,
        outlet_lonlat=(-83.40402, 35.05746))
    assert wm.provenance == "user_supplied"
    assert wm.utm_epsg == 32617
    assert wm.outlet_lonlat == (-83.40402, 35.05746)


# --------------------------------------------------------------------------- #
# SELAFIN writer round-trip (header integrity).
# --------------------------------------------------------------------------- #
def test_bottom_selafin_header(tmp_path):
    # one unit triangle in metres.
    pts = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
    cells = np.array([[0, 1, 2]])
    z = np.array([10.0, 12.0, 15.0])
    path = str(tmp_path / "t.slf")
    _write_bottom_selafin(path, pts, cells, z)
    raw = open(path, "rb").read()
    assert b"TRID3NT WATERSHED RAIN-ON-GRID TIN" in raw
    assert b"BOTTOM" in raw
    # NELEM/NPOIN record is >4i = 1,3,3,1 wrapped in Fortran length markers.
    marker = struct.pack(">4i", 1, 3, 3, 1)
    assert marker in raw


def test_ipobo_all_boundary_for_single_triangle():
    # a lone triangle: every edge is a boundary edge, so all 3 nodes number 1..3.
    ipobo = _ipobo_from_cells(3, np.array([[0, 1, 2]]))
    assert sorted(ipobo.tolist()) == [1, 2, 3]
