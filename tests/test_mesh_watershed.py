"""Offline tests for the shared mesh front's CATCHMENT strategy
(``workflows/mesh/watershed.py``) + the TELEMAC rain-on-grid CN-path selector.

Only the PURE helpers are exercised here (config building, the exterior/river
extraction, UTM projection, the per-node CN/Manning builders, the supplied-mesh
adoption routes, the SELAFIN writer round-trip, and the automatic native-vs-
preprocessing runoff-path selection). The container-driven
``generate_catchment_mesh`` needs the mesh image + network and is proven live.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest
from shapely.geometry import LineString, Polygon

from trid3nt_server.workflows.mesh.telemac_build import (
    _ipobo_from_cells,
    write_bottom_selafin,
)
from trid3nt_server.workflows.mesh.watershed import (
    MeshGenerationError,
    adopt_supplied_mesh,
    adopt_supplied_mesh_2dm,
    build_mesh_config,
    catchment_exterior_and_river_coords,
    delineate_catchment,
    generate_catchment_mesh,
    read_2dm_mesh,
    reproject_nodes_to_utm,
    validate_catchment_not_degenerate,
)
from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
    CNInfiltrationError,
    RunoffPathDecision,
    landcover_cn_manning,
    node_curve_numbers,
    select_runoff_path,
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


def test_time_varying_hyetograph_selects_native_hyetograph():
    d = select_runoff_path(hyetograph_mm=[2.0, 8.0, 15.0, 6.0, 1.0])
    assert d.path == "native_hyetograph"
    assert d.time_varying is True
    assert "RAINDEF=3" in d.reason


def test_flat_hyetograph_selects_native():
    # a hyetograph that is one flat non-zero rate is NOT time-varying.
    d = select_runoff_path(hyetograph_mm=[5.0, 5.0, 5.0, 0.0])
    assert d.path == "native"
    assert d.time_varying is False


def test_no_forcing_raises():
    with pytest.raises(CNInfiltrationError):
        select_runoff_path()


# --------------------------------------------------------------------------- #
# Mesh config building. grade and max_iter are REQUIRED keywords.
# --------------------------------------------------------------------------- #
def test_build_mesh_config_ok():
    box = [[-83.5, 35.0], [-83.4, 35.0], [-83.4, 35.09], [-83.5, 35.09], [-83.5, 35.0]]
    river = [[-83.45, 35.04], [-83.44, 35.05]]
    cfg = build_mesh_config(box, river, min_edge_length_m=40.0, max_edge_length_m=400.0,
                            grade=0.20, max_iter=60)
    assert cfg["min_edge_length_m"] == 40.0
    assert cfg["max_edge_length_m"] == 400.0
    assert len(cfg["boubox_coords"]) == 5
    assert len(cfg["river_coords"]) == 2
    assert cfg["grade"] == 0.20
    assert cfg["max_iter"] == 60


def test_build_mesh_config_bad_edge_band():
    box = [[0, 0], [1, 0], [1, 1], [0, 0]]
    with pytest.raises(MeshGenerationError) as ei:
        build_mesh_config(box, [], min_edge_length_m=400.0, max_edge_length_m=40.0,
                          grade=0.20, max_iter=60)
    assert ei.value.error_code == "MESH_EDGE_BAND_INVALID"


def test_build_mesh_config_degenerate_ring():
    with pytest.raises(MeshGenerationError) as ei:
        build_mesh_config([[0, 0], [1, 1]], [], min_edge_length_m=40.0,
                          max_edge_length_m=400.0, grade=0.20, max_iter=60)
    assert ei.value.error_code == "MESH_CATCHMENT_DOMAIN_DEGENERATE"


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
# Per-node CN2 + Manning (the job ``assemble_node_fields`` used to do, now
# inlined in ``steps/rain_on_grid.py:node_infiltration_fields`` as a direct
# ``node_curve_numbers`` + ``landcover_cn_manning`` composition).
# --------------------------------------------------------------------------- #
def test_node_fields_distributed():
    # 41 = deciduous forest -> CN 80 / n 0.20; 22 = developed low -> CN 89 / n 0.10
    codes = [41, 22, 41]
    cn2 = node_curve_numbers(codes)
    manning = [landcover_cn_manning(c)[1] for c in codes]
    assert cn2 == [80.0, 89.0, 80.0]
    assert manning == [0.20, 0.10, 0.20]


def test_node_fields_uniform_cn_keeps_landcover_manning():
    codes = [41, 22]
    cn2 = node_curve_numbers(codes, uniform_cn=75.0)
    manning = [landcover_cn_manning(c)[1] for c in codes]
    assert cn2 == [75.0, 75.0]           # uniform override
    assert manning == [0.20, 0.10]       # Manning still from land cover


def test_node_fields_needs_a_landcover_list():
    # node_curve_numbers has no code list to look CN up against; a missing list
    # is a bug at the call site, not a value it can silently proceed on.
    with pytest.raises(TypeError):
        node_curve_numbers(None, uniform_cn=75.0)


# --------------------------------------------------------------------------- #
# Supplied-mesh adoption routes (ADR 0200 precondition-gate consumption).
# --------------------------------------------------------------------------- #
def test_adopt_supplied_mesh_missing(tmp_path):
    with pytest.raises(MeshGenerationError) as ei:
        adopt_supplied_mesh(
            mesh_path=str(tmp_path / "nope.slf"), slug="x",
            pour_point=(-83.4, 35.05), utm_epsg=32617)
    assert ei.value.error_code == "MESH_SUPPLIED_MISSING"


def test_adopt_supplied_mesh_wrong_type(tmp_path):
    f = tmp_path / "mesh.txt"
    f.write_text("not a mesh")
    with pytest.raises(MeshGenerationError) as ei:
        adopt_supplied_mesh(
            mesh_path=str(f), slug="x", pour_point=(-83.4, 35.05), utm_epsg=32617)
    assert ei.value.error_code == "MESH_SUPPLIED_UNSUPPORTED"


def test_adopt_supplied_mesh_ok(tmp_path):
    f = tmp_path / "mesh.slf"
    f.write_bytes(b"\x00" * 128)
    m = adopt_supplied_mesh(
        mesh_path=str(f), slug="x", pour_point=(-83.4, 35.05), utm_epsg=32617,
        outlet_lonlat=(-83.40402, 35.05746))
    assert m.provenance == "supplied"
    assert m.utm_epsg == 32617
    assert m.outlet_lonlat == (-83.40402, 35.05746)


def test_adopt_supplied_mesh_2dm_populates_nodes(tmp_path):
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

    m = adopt_supplied_mesh_2dm(
        twodm_path=str(twodm), slug="x", utm_epsg=32617,
        pour_point=(-83.4, 35.05), outlet_lonlat=(-83.40402, 35.05746))
    assert m.provenance == "supplied"
    assert m.points_utm.shape == (4, 2)
    assert m.cells.shape == (2, 3)
    # node lon/lat were recovered (needed by the NLCD node-field sampler).
    ll = m.points_lonlat
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
    write_bottom_selafin(path, pts, cells, z)
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
    with pytest.raises(MeshGenerationError) as ei:
        validate_catchment_not_degenerate(20, 0.018, (-83.40402, 35.05746))
    assert ei.value.error_code == "MESH_CATCHMENT_DEGENERATE"
    # the message must name the AOI / pour-point mismatch, not just fail.
    assert "pour point" in str(ei.value).lower()


def test_validate_catchment_not_degenerate_ok():
    # a real catchment (thousands of cells) passes silently.
    validate_catchment_not_degenerate(4854, 28.7, (-83.40402, 35.05746))


def test_delineate_catchment_index_space_on_synthetic_dem(tmp_path):
    """``delineate_catchment`` traces the catchment in INDEX space off the
    max-accumulation cell, so a convergent bowl draining to its centre yields a
    large, non-degenerate basin (the coordinate-space path collapses to a sliver
    on some alignments; index space does not)."""
    import rasterio
    from rasterio.transform import from_origin

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
    catch, outlet, area_km2, cells = delineate_catchment(
        tmp_path, bbox, pour, str(dem))
    assert cells >= 100          # a broad convergent basin, not a sliver
    assert area_km2 > 0.0
    assert catch is not None and not catch.is_empty


def test_generate_catchment_mesh_guards_degenerate_catchment(tmp_path, monkeypatch):
    """``generate_catchment_mesh`` must fail LOUD, before any meshing, when the
    delineated catchment is a degenerate sliver."""
    from shapely.geometry import Point

    from trid3nt_server.workflows.mesh import watershed as W

    def fake_delineate(rundir, bbox, pour_point, dem_uri=None, **_kw):
        # (polygon, outlet, area_km2, cell_count) -- a 20-cell sliver.
        return Point(pour_point).buffer(0.001), tuple(pour_point), 0.018, 20

    monkeypatch.setattr(W, "delineate_catchment", fake_delineate, raising=True)

    with pytest.raises(MeshGenerationError) as ei:
        generate_catchment_mesh(
            pour_point=(-83.40402, 35.05746),
            bbox=(-83.47, 35.02, -83.36, 35.10),
            slug="watershed", output_dir=str(tmp_path),
            bed_dem={"uri": "s3://cache/bed.tif", "note": ""}, rivers=None)
    assert ei.value.error_code == "MESH_CATCHMENT_DEGENERATE"
