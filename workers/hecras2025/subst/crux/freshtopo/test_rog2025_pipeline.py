"""Offline tests for the HEC-RAS 2025 rain-on-grid pipeline.

Exercises the PURE-PYTHON authoring-prep + metric-extraction + depth-COG paths (no
Docker / no server): terrain georef math, subgrid-volume metric extraction against a
saved real Coweeta result HDF, and the 4326 depth-COG rasterize. The docker author/
prepare/solve is proven live in the ADR (not re-run here). ASCII only.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import rog2025_pipeline as rp

# A saved real solve (the Coweeta DWE run); tests skip if absent (the HDF is
# a session artifact outside the repo, like the other proprietary-adjacent fixtures).
_COWEETA_JSON = Path("/tmp/rog2025_coweeta_dwe.json")
_CATCHMENT = Path("/tmp/rog_coweeta/catchment.geojson")


def _load():
    if not _COWEETA_JSON.is_file():
        pytest.skip("Coweeta run JSON not present (session artifact)")
    d = json.loads(_COWEETA_JSON.read_text())
    if not Path(d["result_h5"]).is_file():
        pytest.skip("Coweeta result HDF not present (session artifact)")
    return d


def test_utm_epsg_pick_conus():
    # western NC -> UTM 17N (32617)
    assert rp._pick_utm_epsg(-83.40, 35.06) == 32617


def test_outlet_edge_from_pour_point():
    bbox = [-83.47, 35.02, -83.36, 35.10]
    # pour point near the east edge
    assert rp._edge_from_pour_point((-83.365, 35.06), bbox) == "e"
    # near the south edge
    assert rp._edge_from_pour_point((-83.41, 35.021), bbox) == "s"


def test_metrics_conservation_and_bounds():
    d = _load()
    m = rp.extract_metrics(
        d["result_h5"], rp.Rog2025Prep(**d["prep"]),
        precip_mm_hr=d["precip_mm_hr"], storm_hours=d["sim_hours"],
        catchment_geojson=str(_CATCHMENT) if _CATCHMENT.is_file() else None)
    # rain-only mass balance closes: rain ~= runoff + final storage
    rain = m["total_rain_volume_1e3_m3"]
    closed = m["runoff_volume_1e3_m3"] + m["final_storage_1e3_m3"]
    assert abs(rain - closed) / rain < 0.05
    # physical envelope on the steep catchment
    assert 0.0 < m["peak_outlet_q_m3s"] < 400.0
    assert 0.0 < m["max_depth_m"] < 20.0
    assert 0.0 < m["runoff_coeff"] <= 1.0
    assert m["n_catchment_cells"] > 0


def test_depth_cog_is_4326_and_finite(tmp_path):
    d = _load()
    out = tmp_path / "depth.tif"
    info = rp.build_depth_cog(d["result_h5"], d["prep"], str(out),
                              catchment_geojson=str(_CATCHMENT) if _CATCHMENT.is_file() else None)
    import rasterio
    with rasterio.open(out) as r:
        assert r.crs.to_epsg() == 4326
        assert r.width > 0 and r.height > 0
    assert info["depth_max"] > 0.0
    # bbox over Coweeta Creek NC
    w, s, e, n = info["bbox4326"]
    assert -84.0 < w < e < -83.0 and 34.9 < s < n < 35.2


def test_depth_scale_feet(tmp_path):
    d = _load()
    im = rp.build_depth_cog(d["result_h5"], d["prep"], str(tmp_path / "m.tif"))
    ift = rp.build_depth_cog(d["result_h5"], d["prep"], str(tmp_path / "ft.tif"),
                             depth_scale=1.0 / 0.3048)
    assert ift["depth_max"] == pytest.approx(im["depth_max"] / 0.3048, rel=1e-3)
