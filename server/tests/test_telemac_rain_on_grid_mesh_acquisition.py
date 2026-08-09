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
    validate_catchment_not_degenerate,
    _ipobo_from_cells,
    _resolve_bare_earth_dem,
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
# ADR 0200 precondition-gate consumption: read a case .2dm END TO END so the
# node-field sampler works (populated points_lonlat), pointing the solve at .slf.
# --------------------------------------------------------------------------- #
def test_use_supplied_mesh_2dm_populates_nodes(tmp_path):
    from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
        use_supplied_mesh_2dm,
    )

    # a 2dm in UTM 17N metres (Coweeta), two triangles.
    twodm = tmp_path / "mesh.2dm"
    twodm.write_text(
        "MESH2D\n"
        "E3T 1 1 2 3 1\n"
        "E3T 2 2 4 3 1\n"
        "ND 1 275000.0 3881000.0 610.0\n"
        "ND 2 275100.0 3881000.0 612.0\n"
        "ND 3 275000.0 3881100.0 615.0\n"
        "ND 4 275100.0 3881100.0 611.0\n")
    slf = tmp_path / "mesh.slf"
    slf.write_bytes(b"\x00" * 256)

    wm = use_supplied_mesh_2dm(
        twodm_path=str(twodm), slf_path=str(slf), utm_epsg=32617,
        pour_point=(-83.4, 35.05), outlet_lonlat=(-83.40402, 35.05746))
    assert wm.provenance == "user_supplied"
    assert wm.slf_path == str(slf)
    assert wm.points_utm.shape == (4, 2)
    assert wm.cells.shape == (2, 3)
    # node lon/lat were recovered (needed by the NLCD node-field sampler).
    ll = wm.meta["points_lonlat"]
    assert ll.shape == (4, 2)
    assert (-84.0 < ll[:, 0]).all() and (ll[:, 0] < -83.0).all()
    assert (34.5 < ll[:, 1]).all() and (ll[:, 1] < 35.5).all()


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


# --------------------------------------------------------------------------- #
# Degenerate-catchment guard (bug 1: the 20-cell sliver must fail LOUD).
# --------------------------------------------------------------------------- #
def test_validate_catchment_not_degenerate_raises():
    # the ADR 0196 live bug: a town bbox clipped Coweeta to a 20-cell sliver.
    with pytest.raises(MeshAcquisitionError) as ei:
        validate_catchment_not_degenerate(20, 0.018, (-83.40402, 35.05746))
    assert ei.value.error_code == "TELEMAC_ROG_CATCHMENT_DEGENERATE"
    # the message must name the AOI / pour-point mismatch, not just fail.
    assert "pour point" in str(ei.value).lower()


def test_validate_catchment_not_degenerate_ok():
    # a real catchment (thousands of cells) passes silently.
    validate_catchment_not_degenerate(4854, 28.7, (-83.40402, 35.05746))


# --------------------------------------------------------------------------- #
# Bare-earth DEM pin + LOUD cross-dataset fallback (bug 2).
# --------------------------------------------------------------------------- #
def test_resolve_bare_earth_dem_pins_3dep(tmp_path, monkeypatch):
    """The mesh BED DEM must be requested from 3DEP bare-earth, never the
    Copernicus DSM (canopy inflates node elevations under tree cover)."""
    import types

    from trid3nt_server.agent.tools import TOOL_REGISTRY

    src_dem = tmp_path / "src_dem.tif"
    src_dem.write_bytes(b"GTIFF-bare-earth")
    seen: dict = {}

    def fake_fetch_dem(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(uri=str(src_dem))

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem", types.SimpleNamespace(fn=fake_fetch_dem))

    notes: list[str] = []
    out = _resolve_bare_earth_dem(
        tmp_path, (-83.47, 35.02, -83.36, 35.10), None,
        resolution_m=10, filename="dem_bed.tif", notes=notes)
    assert out.read_bytes() == b"GTIFF-bare-earth"
    assert seen["source"] == "3dep"          # bare-earth, not copernicus
    assert seen["resolution_m"] == 10
    assert any("3DEP bare-earth" in n for n in notes)


def test_resolve_bare_earth_dem_loud_fallback(tmp_path, monkeypatch, caplog):
    """When 3DEP is unavailable the Copernicus swap must be LOUD: a logged
    warning + a typed note (never a silent surface-model substitution)."""
    import logging
    import types

    from trid3nt_server.agent.tools import TOOL_REGISTRY

    cop_dem = tmp_path / "cop_dem.tif"
    cop_dem.write_bytes(b"GTIFF-copernicus-dsm")

    def fetch_dem_down(**kwargs):
        raise RuntimeError("USGS 3DEP DEM fetch failed (service outage)")

    def fetch_copernicus(**kwargs):
        return types.SimpleNamespace(uri=str(cop_dem))

    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem", types.SimpleNamespace(fn=fetch_dem_down))
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_copernicus_dem",
        types.SimpleNamespace(fn=fetch_copernicus))

    notes: list[str] = []
    with caplog.at_level(logging.WARNING):
        out = _resolve_bare_earth_dem(
            tmp_path, (-83.47, 35.02, -83.36, 35.10), None,
            resolution_m=10, filename="dem_bed.tif", notes=notes)
    assert out.read_bytes() == b"GTIFF-copernicus-dsm"
    assert any("CROSS-DATASET FALLBACK" in n for n in notes)
    assert any("Copernicus" in r.message for r in caplog.records)


def test_resolve_bare_earth_dem_honors_supplied_uri(tmp_path):
    """A caller-supplied dem_uri (bare-earth by contract) is used as-is."""
    supplied = tmp_path / "user_dem.tif"
    supplied.write_bytes(b"user")
    out = _resolve_bare_earth_dem(
        tmp_path, (-83.47, 35.02, -83.36, 35.10), str(supplied),
        resolution_m=10, filename="dem_bed.tif", notes=[])
    assert out == supplied


def test_delineate_catchment_index_space_on_synthetic_dem(tmp_path):
    """_delineate_catchment traces the catchment in INDEX space off the
    max-accumulation cell, so a convergent bowl draining to its centre yields a
    large, non-degenerate basin (the coordinate-space path in the shared tool
    collapses to a sliver on some alignments; index space does not)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from trid3nt_server.agent.workflows.telemac.rain_on_grid.mesh_acquisition import (
        _delineate_catchment,
    )

    # a 40x40 paraboloid bowl, lowest at the centre: every cell drains inward, so
    # the centre outlet captures ~the whole grid (>> the degenerate threshold).
    n = 40
    rows, cols = np.mgrid[0:n, 0:n]
    oi = oj = n // 2
    z = ((rows - oi) ** 2 + (cols - oj) ** 2 + 1.0).astype("float32")
    dem = tmp_path / "bowl.tif"
    dx = 0.001
    top_lat = 35.10
    left_lon = -83.50
    with rasterio.open(
        dem, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32",
        crs="EPSG:4326", nodata=-9999.0,
        transform=from_origin(left_lon, top_lat, dx, dx)) as ds:
        ds.write(z, 1)

    # pour point at the bowl centre (the lowest / highest-accumulation cell).
    pour = (left_lon + oj * dx, top_lat - oi * dx)
    bbox = (left_lon, top_lat - n * dx, left_lon + n * dx, top_lat)
    catch, outlet, area_km2, cells = _delineate_catchment(
        tmp_path, bbox, pour, str(dem))
    assert cells >= 100          # a broad convergent basin, not a sliver
    assert area_km2 > 0.0
    assert catch is not None and not catch.is_empty


def test_acquire_mesh_guards_degenerate_catchment(tmp_path, monkeypatch):
    """acquire_watershed_mesh must fail LOUD, before any meshing, when the
    delineated catchment is a degenerate sliver (the ADR 0196 live failure)."""
    from shapely.geometry import Point

    from trid3nt_server.agent.workflows.telemac.rain_on_grid import (
        mesh_acquisition as MA,
    )

    def fake_delineate(rundir, bbox, pour_point, dem_uri, **_kw):
        # (polygon, outlet, area_km2, cell_count) -- a 20-cell sliver.
        return Point(pour_point).buffer(0.001), tuple(pour_point), 0.018, 20

    monkeypatch.setattr(MA, "_delineate_catchment", fake_delineate, raising=True)

    with pytest.raises(MeshAcquisitionError) as ei:
        MA.acquire_watershed_mesh(
            pour_point=(-83.40402, 35.05746),
            bbox=(-83.47, 35.02, -83.36, 35.10),
            output_dir=str(tmp_path))
    assert ei.value.error_code == "TELEMAC_ROG_CATCHMENT_DEGENERATE"
