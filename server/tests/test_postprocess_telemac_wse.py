"""Offline tests for postprocess_telemac_wse (max FREE-SURFACE / WSE raster).

Covers the free-surface / depth variable pickers, the WET-MASK discipline (dry
terrain must NOT leak into the water-surface raster), the mesh-CRS COG write +
quantity tag, and the returned contract scalars. Uses a synthetic big-endian
SELAFIN this test writes (mirrors test_postprocess_telemac.py) -- no docker /
TELEMAC / S3 / case data. Also covers the driver's friction deck edit.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows import postprocess_telemac as P


def _rec(payload: bytes) -> bytes:
    n = struct.pack(">i", len(payload))
    return n + payload + n


def _write_synthetic_selafin(path, varnames, x, y, ikle, times, data):
    npoin = len(x)
    nelem = len(ikle)
    ndp = len(ikle[0])
    title = b"MALPASSET WSE TEST".ljust(72) + b"SERAFIN "
    with open(path, "wb") as fh:
        fh.write(_rec(title))
        fh.write(_rec(struct.pack(">2i", len(varnames), 0)))
        for v in varnames:
            fh.write(_rec(v.encode("latin-1").ljust(32)))
        fh.write(_rec(struct.pack(">10i", *([0] * 10))))
        fh.write(_rec(struct.pack(">4i", nelem, npoin, ndp, 1)))
        fh.write(_rec(np.asarray(ikle, dtype=">i4").tobytes()))
        fh.write(_rec(np.arange(1, npoin + 1, dtype=">i4").tobytes()))
        fh.write(_rec(np.asarray(x, dtype=">f4").tobytes()))
        fh.write(_rec(np.asarray(y, dtype=">f4").tobytes()))
        for ti, t in enumerate(times):
            fh.write(_rec(struct.pack(">f", float(t))))
            for v in varnames:
                fh.write(_rec(np.asarray(data[v][ti], dtype=">f4").tobytes()))


VARS = ["VELOCITY U      M/S", "WATER DEPTH     M", "FREE SURFACE    M", "BOTTOM          M"]


def _malpasset_like_slf(path):
    """7 nodes: 5 wet (peak FS <= 12), 1 node dry-with-HIGH-terrain at f0 (FS=25),
    1 far node never wet (FS=30). If the wet mask works, wse_max == 12 (the dry
    FS=25/30 terrain values are excluded); if it leaks, wse_max would be 25 or 30.
    """
    # x, y in local metres (mesh frame).
    x = [5000.0, 5100.0, 5000.0, 5100.0, 5050.0, 5050.0, 7000.0]
    y = [4000.0, 4000.0, 4100.0, 4100.0, 4050.0, 3950.0, 4000.0]
    ikle = [[1, 2, 5], [2, 4, 5], [3, 4, 5], [1, 3, 5], [1, 5, 6], [2, 5, 6]]
    times = [0.0, 60.0]
    depth = {
        # node:            n1    n2    n3    n4    n5    n6    n7(far)
        0: np.array([0.0, 0.0, 4.0, 2.0, 3.0, 2.5, 0.0]),   # frame 0
        1: np.array([3.0, 2.0, 1.0, 0.0, 2.0, 1.5, 0.0]),   # frame 1
    }
    fs = {
        0: np.array([25.0, 10.0, 12.0, 11.0, 12.0, 11.5, 30.0]),  # n1 dry-high(25), n7 dry(30)
        1: np.array([12.0, 11.0, 9.0, 8.0, 11.0, 10.5, 30.0]),
    }
    bottom = np.array([25.0, 8.0, 8.0, 9.0, 9.0, 9.0, 30.0])
    data = {
        "VELOCITY U      M/S": [np.zeros(7), np.zeros(7)],
        "WATER DEPTH     M": [depth[0], depth[1]],
        "FREE SURFACE    M": [fs[0], fs[1]],
        "BOTTOM          M": [bottom, bottom],
    }
    _write_synthetic_selafin(path, VARS, x, y, ikle, times, data)


def test_pick_named_var():
    assert P._pick_named_var(VARS, P._WSE_VAR_KEYS, "S") == "FREE SURFACE    M"
    assert P._pick_named_var(VARS, P._DEPTH_VAR_KEYS, "H") == "WATER DEPTH     M"
    # French deck names.
    fr = ["SURFACE LIBRE   M", "HAUTEUR D'EAU   M"]
    assert P._pick_named_var(fr, P._WSE_VAR_KEYS, "S") == "SURFACE LIBRE   M"
    assert P._pick_named_var(fr, P._DEPTH_VAR_KEYS, "H") == "HAUTEUR D'EAU   M"
    assert P._pick_named_var(["VELOCITY U", "BOTTOM"], P._WSE_VAR_KEYS, "S") is None


def test_wse_wet_mask_excludes_dry_terrain(tmp_path):
    slf = tmp_path / "wse.slf"
    _malpasset_like_slf(slf)
    layers, metrics = P.postprocess_telemac_wse(
        slf,
        run_id="TESTWSE0000000000000000AA",
        mesh_epsg=32632,
        reach_name="malpasset",
        vertical_datum="NGF",
        _output_dir=str(tmp_path),
    )
    wse = layers[0]
    # THE masking assertion: dry-terrain FS (25 at n1 frame0, 30 at the never-wet
    # far node) must be excluded -> the peak WATER surface is 12, not 25/30.
    assert metrics["wse_max_m"] == pytest.approx(12.0)
    assert wse.wse_max_m == pytest.approx(12.0)
    # the never-wet far node is dropped; 6 near-cluster nodes were wet.
    assert metrics["n_wet_nodes"] == 6
    assert wse.quantity == "water_surface_elevation"
    assert wse.mesh_epsg == 32632
    assert wse.vertical_datum == "NGF"
    assert wse.n_frames == 2


def test_wse_cog_crs_and_quantity_tag(tmp_path):
    import rasterio

    slf = tmp_path / "wse.slf"
    _malpasset_like_slf(slf)
    layers, _ = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000000BB", mesh_epsg=32632,
        vertical_datum="NGF", _output_dir=str(tmp_path),
    )
    with rasterio.open(layers[0].uri) as src:
        assert src.crs is not None and src.crs.to_epsg() == 32632
        tags = dict(src.tags())
        tags.update(src.tags(1))
        # the quantity tag drives extract_model_at_observations' model-quantity
        # resolution (elevation family) for a like-for-like WSE pairing.
        assert tags.get("quantity") == "water_surface_elevation"
        # projected metric bounds (not lon/lat degrees): pixels are metres.
        assert abs(src.bounds.left) > 360


def test_wse_depth_mode(tmp_path):
    slf = tmp_path / "wse.slf"
    _malpasset_like_slf(slf)
    layers, metrics = P.postprocess_telemac_wse(
        slf, run_id="TESTWSE0000000000000000CC", mesh_epsg=32632,
        quantity="depth", _output_dir=str(tmp_path),
    )
    assert metrics["quantity"] == "water_depth"
    assert layers[0].quantity == "water_depth"
    # max depth over the wet frames (n3 frame0 depth 4.0 is the deepest).
    assert metrics["wse_max_m"] == pytest.approx(4.0)


def test_wse_empty_dry_solve_raises(tmp_path):
    slf = tmp_path / "dry.slf"
    x = [0.0, 100.0, 0.0]
    y = [0.0, 0.0, 100.0]
    ikle = [[1, 2, 3]]
    times = [0.0]
    data = {
        "VELOCITY U      M/S": [np.zeros(3)],
        "WATER DEPTH     M": [np.zeros(3)],          # everywhere dry
        "FREE SURFACE    M": [np.array([5.0, 6.0, 7.0])],
        "BOTTOM          M": [np.array([5.0, 6.0, 7.0])],
    }
    _write_synthetic_selafin(slf, VARS, x, y, ikle, times, data)
    with pytest.raises(P.PostprocessTelemacError) as ei:
        P.postprocess_telemac_wse(
            slf, run_id="TESTWSE0000000000000000DD", mesh_epsg=32632,
            _output_dir=str(tmp_path),
        )
    assert ei.value.error_code == "TELEMAC_OUTPUT_EMPTY"


# --------------------------------------------------------------------------- #
# Driver friction deck edit (the honest bundled-deck "setter").
# --------------------------------------------------------------------------- #
def _load_driver():
    path = Path("scripts/run_l2_malpasset.py").resolve()
    spec = importlib.util.spec_from_file_location("run_l2_malpasset", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_l2_malpasset"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_adjust_deck_friction_colon_and_equals():
    drv = _load_driver()
    # TELEMAC accepts both separators; the Malpasset deck uses a colon.
    colon = "LAW OF BOTTOM FRICTION = 3\nFRICTION COEFFICIENT : 30.\nVELOCITY DIFFUSIVITY = 1.\n"
    new, old = drv.adjust_deck_friction(colon, 40.0)
    assert old == pytest.approx(30.0)
    assert "FRICTION COEFFICIENT : 40." in new
    # LAW OF BOTTOM FRICTION (also contains 'FRICTION') is untouched.
    assert "LAW OF BOTTOM FRICTION = 3" in new
    assert "VELOCITY DIFFUSIVITY = 1." in new

    equals = "FRICTION COEFFICIENT = 30.\n"
    new2, old2 = drv.adjust_deck_friction(equals, 35.0)
    assert old2 == pytest.approx(30.0)
    assert "FRICTION COEFFICIENT = 35" in new2


def test_adjust_deck_friction_missing_raises():
    drv = _load_driver()
    with pytest.raises(ValueError):
        drv.adjust_deck_friction("LAW OF BOTTOM FRICTION = 3\n", 40.0)
