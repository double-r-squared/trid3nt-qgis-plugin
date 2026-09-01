"""Unit tests for postprocess_telemac's pure pieces (no docker / TELEMAC / S3).

Covers the hand-rolled SELAFIN reader (round-tripped against a synthetic
big-endian single-precision SELAFIN this test writes), the DYE-variable picker,
the adaptive grid sizing, and the channel-clipped scatter rasterization. The
live COG-write + upload path is exercised by the through-the-seam dev proof.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from trid3nt_server.workflows.telemac import postprocess_telemac as P


def _rec(payload: bytes) -> bytes:
    """Wrap a payload as one big-endian Fortran sequential-unformatted record."""
    n = struct.pack(">i", len(payload))
    return n + payload + n


def _write_synthetic_selafin(path, varnames, x, y, ikle, times, data):
    """Write a minimal single-precision big-endian SELAFIN the reader parses."""
    npoin = len(x)
    nelem = len(ikle)
    ndp = len(ikle[0])
    title = b"SYNTHETIC TEST".ljust(72) + b"SERAFIN "  # [72:80] precision tag
    with open(path, "wb") as fh:
        fh.write(_rec(title))
        fh.write(_rec(struct.pack(">2i", len(varnames), 0)))
        for v in varnames:
            fh.write(_rec(v.encode("latin-1").ljust(32)))
        iparam = [0] * 10  # iparam[9]==0 -> no date record
        fh.write(_rec(struct.pack(">10i", *iparam)))
        fh.write(_rec(struct.pack(">4i", nelem, npoin, ndp, 1)))
        fh.write(_rec(np.asarray(ikle, dtype=">i4").tobytes()))
        fh.write(_rec(np.arange(1, npoin + 1, dtype=">i4").tobytes()))
        fh.write(_rec(np.asarray(x, dtype=">f4").tobytes()))
        fh.write(_rec(np.asarray(y, dtype=">f4").tobytes()))
        for ti, t in enumerate(times):
            fh.write(_rec(struct.pack(">f", float(t))))
            for v in varnames:
                fh.write(_rec(np.asarray(data[v][ti], dtype=">f4").tobytes()))


def test_read_selafin_roundtrip(tmp_path):
    path = tmp_path / "synthetic.slf"
    varnames = ["VELOCITY U      M/S", "DYE             MG/L"]
    x = [0.0, 100.0, 0.0, 100.0]
    y = [0.0, 0.0, 100.0, 100.0]
    ikle = [[1, 2, 3], [2, 4, 3]]
    times = [0.0, 60.0]
    data = {
        "VELOCITY U      M/S": [np.full(4, 1.0), np.full(4, 1.5)],
        "DYE             MG/L": [np.array([0.0, 10.0, 0.0, 5.0]), np.array([0.0, 3.0, 0.0, 40.0])],
    }
    _write_synthetic_selafin(path, varnames, x, y, ikle, times, data)

    mesh = P.read_selafin(path)
    assert mesh["npoin"] == 4
    assert mesh["nelem"] == 2
    # IKLE returned 0-based (SELAFIN stores it 1-based) so a mesh-faithful render
    # can triangulate real elements, not an unconstrained Delaunay of the nodes.
    assert mesh["ikle"].shape == (2, 3)
    assert np.array_equal(mesh["ikle"], np.array([[0, 1, 2], [1, 3, 2]]))
    assert [v.strip() for v in mesh["varnames"]] == ["VELOCITY U      M/S", "DYE             MG/L"]
    assert np.allclose(mesh["x"], x)
    assert np.allclose(mesh["y"], y)
    assert np.allclose(mesh["times"], times)
    dye = mesh["data"]["DYE             MG/L"]
    assert dye.shape == (2, 4)
    assert dye[1].max() == pytest.approx(40.0)


def test_pick_dye_var():
    assert P._pick_dye_var(["VELOCITY U      M/S", "DYE             MG/L"]) == "DYE             MG/L"
    # a T-prefixed tracer when no explicit DYE
    assert P._pick_dye_var(["WATER DEPTH     M", "T1              "]) == "T1              "
    assert P._pick_dye_var(["VELOCITY U", "WATER DEPTH"]) is None


def test_no_substance_class_but_the_dye_one_publishes_a_dye_named_product():
    """The product's NAME must not assert more than the field carries: an oil run
    advects the same passive tracer a dye run does, and a sediment run's tracer is
    a suspended grain load. Neither may reach the store as ``telemac_dye_peak``."""
    from trid3nt_contracts.telemac_contracts import TELEMAC_SUBSTANCE_PRODUCTS as T

    assert T["tracer"].cog == T["decay"].cog == "telemac_dye_peak.tif"
    for name in ("oil", "sediment"):
        assert "dye" not in T[name].cog
        assert "dye" not in T[name].quantity
    # each class's raster resolves through the ONE styling seam, never a preset
    # engine code invented.
    from trid3nt_server.emission.styles import resolve_style_preset

    for product in T.values():
        preset, is_fallback = resolve_style_preset(product.quantity)
        assert (preset, is_fallback) == (product.style_preset, False)


def test_the_peak_layer_handle_carries_the_class_that_produced_it():
    """Two classes on one reach publish different rasters; a shared handle would
    register one over the other."""
    dye = P.peak_layer_id("RID", "tracer")
    assert dye == P.peak_layer_id("RID", "")           # an unnamed class IS dye
    assert dye != P.peak_layer_id("RID", "sediment")
    assert " " not in P.peak_layer_id("RID", "sediment")


def test_grid_shape_floor_and_aspect():
    # a tiny AOI floors to the minimum per side.
    nrows, ncols = P._grid_shape((-114.31, 42.57, -114.305, 42.575), P.TELEMAC_TARGET_GROUND_RES_M)
    assert nrows >= P.TELEMAC_MIN_PX_PER_SIDE and ncols >= P.TELEMAC_MIN_PX_PER_SIDE
    assert nrows <= P.TELEMAC_MAX_PX_PER_SIDE and ncols <= P.TELEMAC_MAX_PX_PER_SIDE


def test_rasterize_clips_to_channel_and_masks_subfloor():
    # a small CLUSTER of nodes (griddata needs >= 3 to triangulate) carrying dye
    # near the bbox centre; the far corners must be NaN (channel clip) and a
    # below-floor value must be masked out.
    cx, cy = -114.310, 42.570
    off = 0.0006
    lon = np.array([cx - off, cx + off, cx, cx - off, cx + off, cx])
    lat = np.array([cy - off, cy - off, cy, cy + off, cy + off, cy + 2 * off])
    vals = np.array([50.0, 45.0, 60.0, 40.0, 55.0, 0.2])  # last below the 1 mg/L floor
    bbox = (cx - 0.005, cy - 0.005, cx + 0.005, cy + 0.005)
    shape = (96, 96)
    clip = 1.5 * max((bbox[2] - bbox[0]) / shape[1], (bbox[3] - bbox[1]) / shape[0])
    grid = P._rasterize_nodes_to_grid(lon, lat, vals, bbox, shape, clip)
    assert grid.shape == shape
    finite = np.isfinite(grid)
    # some cells near the strong nodes are wet; the far corners are clipped to NaN.
    assert finite.any()
    assert not finite.all()
    assert np.nanmax(grid) >= P.TELEMAC_DYE_WET_MGL


# --------------------------------------------------------------------------- #
# Barycentric (P1) rasterization of an open-water mesh: the dot-lattice fix.
# --------------------------------------------------------------------------- #
def _open_water_mesh(n=9, span=0.05, origin=(-87.6, 46.7)):
    """A regular triangulated node grid standing in for a coarse open-water mesh
    (nodes ~1 km apart over a lake AOI, the open-water geometry)."""
    lon0, lat0 = origin
    xs = np.linspace(lon0, lon0 + span, n)
    ys = np.linspace(lat0, lat0 + span, n)
    lon = np.repeat(xs, n)                       # node id = i*n + j
    lat = np.tile(ys, n)
    tris = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, (i + 1) * n + j, (i + 1) * n + j + 1, i * n + j + 1
            tris.append([a, b, c])
            tris.append([a, c, d])
    return lon, lat, np.asarray(tris, dtype="int64")


def test_barycentric_fill_replaces_the_dot_lattice():
    """A coarse open-water mesh under the nearest-node halo published ~2% valid
    pixels; the element fill covers the mesh footprint instead."""
    lon, lat, ikle = _open_water_mesh()
    vals = 15.0 + 10.0 * (lat - lat.min()) / (lat.max() - lat.min())
    pad = 0.0009
    bbox = (lon.min() - pad, lat.min() - pad, lon.max() + pad, lat.max() + pad)
    shape = (400, 400)

    clip = 2.0 * max((bbox[2] - bbox[0]) / shape[1], (bbox[3] - bbox[1]) / shape[0])
    dots = P._rasterize_nodes_to_grid(lon, lat, vals, bbox, shape, clip,
                                      wet_floor=-1e30)
    field = P._rasterize_mesh_to_grid(lon, lat, ikle, vals, bbox, shape,
                                      wet_floor=-1e30)

    dot_frac = float(np.isfinite(dots).mean())
    field_frac = float(np.isfinite(field).mean())
    assert dot_frac < 0.10, dot_frac              # the defect: isolated pixels
    assert field_frac > 0.90, field_frac          # the fix: a field
    # the fill is the SOLVER's own P1 solution, so it interpolates, never invents
    assert np.nanmin(field) >= vals.min() - 1e-9
    assert np.nanmax(field) <= vals.max() + 1e-9


def test_barycentric_fill_is_exact_on_a_linear_field():
    """P1 interpolation of a linear field must reproduce it to machine precision -
    the guarantee that makes 'zero invented data' true."""
    lon, lat, ikle = _open_water_mesh()
    vals = 3.0 * lon + 5.0 * lat
    bbox = (lon.min(), lat.min(), lon.max(), lat.max())
    shape = (120, 120)
    grid = P._rasterize_mesh_to_grid(lon, lat, ikle, vals, bbox, shape,
                                     wet_floor=-1e30)
    nrows, ncols = shape
    gx = bbox[0] + (np.arange(ncols) + 0.5) * (bbox[2] - bbox[0]) / ncols
    gy = bbox[3] - (np.arange(nrows) + 0.5) * (bbox[3] - bbox[1]) / nrows
    exact = 3.0 * gx[None, :] + 5.0 * gy[:, None]
    m = np.isfinite(grid)
    assert m.all()
    assert np.allclose(grid[m], exact[m], atol=1e-9)


def test_a_masked_node_nodatas_the_elements_it_touches():
    """A clamped-land / never-wet node is NaN, and its value must not bleed into
    the product through the elements around it (honesty floor)."""
    lon, lat, ikle = _open_water_mesh()
    vals = np.full(lon.size, 20.0)
    vals[0] = np.nan                              # SW corner node masked
    bbox = (lon.min(), lat.min(), lon.max(), lat.max())
    shape = (200, 200)
    grid = P._rasterize_mesh_to_grid(lon, lat, ikle, vals, bbox, shape,
                                     wet_floor=-1e30)
    assert np.isnan(grid[-1, 0])                  # row 0 = north, so SW = last row
    assert np.isfinite(grid).mean() > 0.9         # only the touched elements drop
    assert np.nanmax(grid) == pytest.approx(20.0)


def test_barycentric_rasterizer_refuses_an_element_table_that_does_not_index():
    lon, lat, ikle = _open_water_mesh(n=4)
    with pytest.raises(ValueError, match="does not index"):
        P._rasterize_mesh_to_grid(lon[:3], lat[:3], ikle, np.zeros(3),
                                  (0.0, 0.0, 1.0, 1.0), (16, 16))
