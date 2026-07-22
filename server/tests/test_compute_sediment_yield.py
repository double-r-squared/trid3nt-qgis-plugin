"""Unit tests for the ``compute_sediment_yield`` RUSLE composer (no network).

All inputs are SYNTHESIZED locally and passed via the override URIs
(``dem_uri`` / ``k_uri`` / ``landcover_uri``), so the full RUSLE pipeline
(DEM gradient -> LS -> K -> C -> A = R*K*LS*C*P COG) runs for real without
touching Copernicus / STATSGO / Planetary Computer.

Coverage:
1.  ``test_registered`` -- tool in TOOL_REGISTRY with cacheable=False /
    ttl_class="live-no-cache".
2.  ``test_rusle_matches_hand_computed_cell`` -- a uniform inclined-plane DEM
    (exactly-known slope) + constant K + all-crops land cover: an interior
    output cell equals the HAND-COMPUTED A = R*K*(lambda/22.13)^m*(65.41
    sin^2(theta)+4.56 sin(theta)+0.065)*C*1.
3.  ``test_default_r_is_honest`` -- omitting rainfall_erosivity uses the
    documented constant 300 with an honest note (values scale accordingly).
4.  ``test_k_fallback_constant_with_note`` -- STATSGO unavailable -> constant
    0.2 K with a note; output matches the re-hand-computed cell.
5.  ``test_water_class_yields_zero`` -- water land cover (C=0) -> A=0.
6.  ``test_unknown_class_is_nodata`` -- cloud class (10) carries no C -> NaN
    (masked) in the output, never a fabricated value.
7.  ``test_aoi_clamp_raises`` -- AOI over 0.2 deg per side raises the typed
    ``SedimentYieldAoiTooLargeError``.
8.  ``test_bad_bbox_raises`` / ``test_bad_erosivity_raises`` -- typed input
    validation.
9.  ``test_style_preset_resolves_log_colormap`` -- the publish seam resolves
    ``sediment_yield_t_ha_yr`` to a TiTiler interval ``&colormap=`` built from
    the log-spaced class table (the log-scaled colormap requirement).
"""

from __future__ import annotations

import json
import math
import os
from urllib.parse import parse_qsl

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_contracts.execution import LayerURI

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.compute_sediment_yield import (
    C_BY_IO_LULC_CLASS,
    SEDIMENT_YIELD_LOG_CLASSES,
    SedimentYieldAoiTooLargeError,
    SedimentYieldInputError,
    SedimentYieldLayerURI,
    compute_sediment_yield,
)

# Small legal AOI (bbox is used for validation/labeling only when all three
# override URIs are supplied; the synthetic rasters carry their own UTM grid).
BBOX = (-117.05, 34.00, -116.95, 34.05)

# Synthetic grid: 40x40 at 30 m in UTM zone 11N.
N = 40
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"

# Inclined plane: 2 m drop per 30 m row southward -> slope = 2/30 (6.67%).
DROP_PER_ROW_M = 2.0

R_TEST = 200.0
K_TEST = 0.3


def _write_raster(path: str, data: np.ndarray, nodata: float) -> str:
    transform = from_bounds(X0, Y0, X0 + N * RES, Y0 + N * RES, N, N)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=N,
        width=N,
        count=1,
        dtype=data.dtype,
        crs=CRS,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)
    return path


def _expected_a(r: float, k: float, c: float) -> float:
    """Hand-compute A for the inclined-plane DEM (same formulas, by hand).

    grad = 2/30 rise/run; slope_pct = 6.67 >= 5 -> m = 0.5;
    L = (30/22.13)^0.5; S = 65.41 sin^2(theta) + 4.56 sin(theta) + 0.065.
    """
    grad = DROP_PER_ROW_M / RES
    theta = math.atan(grad)
    m = 0.5
    length = (RES / 22.13) ** m
    steep = 65.41 * math.sin(theta) ** 2 + 4.56 * math.sin(theta) + 0.065
    return r * k * length * steep * c * 1.0


@pytest.fixture()
def synthetic_inputs(tmp_path):
    """Inclined-plane DEM + constant K + all-crops (class 5) land cover."""
    rows = np.arange(N, dtype=np.float64)
    z = (1000.0 - rows[:, None] * DROP_PER_ROW_M) * np.ones((1, N))
    dem_path = _write_raster(
        str(tmp_path / "dem.tif"), z.astype("float32"), nodata=-9999.0
    )
    k = np.full((N, N), K_TEST, dtype="float32")
    k_path = _write_raster(str(tmp_path / "k.tif"), k, nodata=-1.0)
    lc = np.full((N, N), 5, dtype="int16")  # Crops
    lc_path = _write_raster(str(tmp_path / "landcover.tif"), lc, nodata=0.0)
    return dem_path, k_path, lc_path


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_sediment_yield"]
    assert entry.fn is compute_sediment_yield
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"


def test_rusle_matches_hand_computed_cell(synthetic_inputs, tmp_path) -> None:
    dem_path, k_path, lc_path = synthetic_inputs
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = compute_sediment_yield(
        bbox=BBOX,
        rainfall_erosivity=R_TEST,
        dem_uri=dem_path,
        k_uri=k_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )

    # Typed LayerURI subclass -> the emit_tool_call wrap-site persists it and
    # the auto-publish path renders the COG (same path as other compute_*).
    assert isinstance(result, SedimentYieldLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "raster"
    assert result.style_preset == "sediment_yield_t_ha_yr"
    assert result.units == "t/ha/yr"
    assert tuple(result.bbox) == BBOX
    assert result.rainfall_erosivity == R_TEST
    assert isinstance(result.notes, list) and result.notes

    # Open the artifact and hand-check an INTERIOR cell (uniform plane, so the
    # numpy gradient is exact everywhere, but stay off the edges anyway).
    assert os.path.exists(result.uri)
    with rasterio.open(result.uri) as src:
        a = src.read(1)
        nodata = src.nodata
    cell = float(a[N // 2, N // 2])
    assert cell != nodata
    expected = _expected_a(R_TEST, K_TEST, C_BY_IO_LULC_CLASS[5])
    assert np.isclose(cell, expected, rtol=1e-4), (cell, expected)

    # Whole uniform plane matches (excluding nothing: no nodata inputs).
    valid = a[a != nodata]
    assert valid.size == N * N
    assert np.allclose(valid, expected, rtol=1e-4)

    # Summary scalars agree with the raster.
    assert np.isclose(result.mean_soil_loss_t_ha_yr, expected, rtol=1e-3)
    assert np.isclose(result.max_soil_loss_t_ha_yr, expected, rtol=1e-3)

    # The legend rides on the LayerURI, built from the log-class table.
    assert result.legend is not None
    assert result.legend.kind == "categorical"
    assert len(result.legend.classes) == len(SEDIMENT_YIELD_LOG_CLASSES)


def test_default_r_is_honest(synthetic_inputs, tmp_path) -> None:
    dem_path, k_path, lc_path = synthetic_inputs
    out_dir = tmp_path / "out_default_r"
    out_dir.mkdir()

    result = compute_sediment_yield(
        bbox=BBOX,
        dem_uri=dem_path,
        k_uri=k_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )
    assert result.rainfall_erosivity == 300.0
    assert any("R-factor DEFAULT" in note for note in result.notes)
    expected = _expected_a(300.0, K_TEST, C_BY_IO_LULC_CLASS[5])
    assert np.isclose(result.mean_soil_loss_t_ha_yr, expected, rtol=1e-3)


def test_k_fallback_constant_with_note(
    synthetic_inputs, tmp_path, monkeypatch
) -> None:
    dem_path, _k_path, lc_path = synthetic_inputs
    out_dir = tmp_path / "out_kfb"
    out_dir.mkdir()

    # No k_uri + STATSGO down -> documented constant 0.2 fallback with a note.
    import grace2_agent.tools.fetch_statsgo_soils as statsgo_mod

    def _boom(**_kw):
        raise RuntimeError("STATSGO offline (test)")

    monkeypatch.setattr(statsgo_mod, "fetch_statsgo_soils", _boom)

    result = compute_sediment_yield(
        bbox=BBOX,
        rainfall_erosivity=R_TEST,
        dem_uri=dem_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )
    assert any("K-factor FALLBACK" in note for note in result.notes)
    expected = _expected_a(R_TEST, 0.2, C_BY_IO_LULC_CLASS[5])
    assert np.isclose(result.mean_soil_loss_t_ha_yr, expected, rtol=1e-3)


def test_water_class_yields_zero(synthetic_inputs, tmp_path) -> None:
    dem_path, k_path, _lc_path = synthetic_inputs
    water = np.full((N, N), 1, dtype="int16")  # Water: C = 0
    water_path = _write_raster(str(tmp_path / "water.tif"), water, nodata=0.0)
    out_dir = tmp_path / "out_water"
    out_dir.mkdir()

    result = compute_sediment_yield(
        bbox=BBOX,
        rainfall_erosivity=R_TEST,
        dem_uri=dem_path,
        k_uri=k_path,
        landcover_uri=water_path,
        _output_dir=str(out_dir),
    )
    assert result.max_soil_loss_t_ha_yr == 0.0


def test_unknown_class_is_nodata(synthetic_inputs, tmp_path) -> None:
    dem_path, k_path, _lc_path = synthetic_inputs
    lc = np.full((N, N), 5, dtype="int16")
    lc[:10, :] = 10  # Clouds: no cover information -> nodata, never fabricated
    lc_path = _write_raster(str(tmp_path / "cloudy.tif"), lc, nodata=0.0)
    out_dir = tmp_path / "out_clouds"
    out_dir.mkdir()

    result = compute_sediment_yield(
        bbox=BBOX,
        rainfall_erosivity=R_TEST,
        dem_uri=dem_path,
        k_uri=k_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )
    with rasterio.open(result.uri) as src:
        a = src.read(1)
        nodata = src.nodata
    assert (a[:10, :] == nodata).all()
    assert (a[10:, :] != nodata).all()


def test_aoi_clamp_raises() -> None:
    with pytest.raises(SedimentYieldAoiTooLargeError):
        compute_sediment_yield(bbox=(-117.5, 34.0, -117.0, 34.05))  # 0.5 wide
    with pytest.raises(SedimentYieldAoiTooLargeError):
        compute_sediment_yield(bbox=(-117.1, 34.0, -117.0, 34.5))  # 0.5 tall


def test_bad_bbox_raises() -> None:
    with pytest.raises(SedimentYieldInputError):
        compute_sediment_yield(bbox=(-117.0, 34.05, -117.05, 34.00))  # reversed
    with pytest.raises(SedimentYieldInputError):
        compute_sediment_yield(bbox=(-117.0, 34.0, -116.99))  # wrong arity
    with pytest.raises(SedimentYieldInputError):
        compute_sediment_yield(bbox=("a", 34.0, -116.99, 34.05))  # non-numeric


def test_bad_erosivity_raises(synthetic_inputs) -> None:
    with pytest.raises(SedimentYieldInputError):
        compute_sediment_yield(bbox=BBOX, rainfall_erosivity=0.0)
    with pytest.raises(SedimentYieldInputError):
        compute_sediment_yield(bbox=BBOX, rainfall_erosivity=1e9)
    with pytest.raises(SedimentYieldInputError):
        compute_sediment_yield(bbox=BBOX, rainfall_erosivity="wet")


def test_style_preset_resolves_log_colormap() -> None:
    """The publish seam turns the preset into a log-spaced interval colormap."""
    from grace2_agent.tools.publish_layer import _registry_style_params

    params = _registry_style_params("sediment_yield_t_ha_yr")
    assert params is not None and params.startswith("&colormap=")
    (key, encoded), = parse_qsl(params.lstrip("&"))
    assert key == "colormap"
    intervals = json.loads(encoded)
    assert len(intervals) == len(SEDIMENT_YIELD_LOG_CLASSES)
    # Breaks are the log-spaced 1/5/10/50/100/500 table, colors are RGBA.
    assert [iv[0][0] for iv in intervals] == [
        lo for lo, _hi, _c, _l in SEDIMENT_YIELD_LOG_CLASSES
    ]
    for iv in intervals:
        assert len(iv[1]) == 4
        assert all(0 <= ch <= 255 for ch in iv[1])
