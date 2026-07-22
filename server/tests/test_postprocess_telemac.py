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

from trid3nt_server.workflows import postprocess_telemac as P


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
