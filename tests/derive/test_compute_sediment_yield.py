"""Unit tests for the ``compute_sediment_yield`` RUSLE composer, with no network.

All inputs are SYNTHESIZED locally and passed through the override uris, so the
full pipeline runs for real. An inclined-plane DEM with constant K and one cover
class gives a HAND-COMPUTED interior cell; each fallback carries an honest note;
water yields zero and an unknown class is masked rather than invented."""

from __future__ import annotations

import json
import math
import os
from urllib.parse import parse_qsl

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.compute_sediment_yield.compute_sediment_yield import (
    C_BY_IO_LULC_CLASS,
    SEDIMENT_YIELD_LOG_CLASSES,
    SedimentYieldAoiTooLargeError,
    SedimentYieldInputError,
    SedimentYieldLayerURI,
    compute_sediment_yield,
)


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
    assert result.style["kind"] == "classed"
    assert result.style["units"] == "t/ha/yr"
    assert result.units == "t/ha/yr"
    exp = transform_bounds(CRS, "EPSG:4326", X0, Y0, X0 + N * RES, Y0 + N * RES)
    assert tuple(result.bbox) == pytest.approx(exp, abs=1e-6)
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

    # The legend rides on the LayerURI, resolved from the log-class table.
    assert result.legend is not None
    assert result.legend.kind == "classed"
    assert all(color in result.legend.qml
               for _lo, _hi, color, _label in SEDIMENT_YIELD_LOG_CLASSES)


def test_default_r_is_honest(synthetic_inputs, tmp_path) -> None:
    dem_path, k_path, lc_path = synthetic_inputs
    out_dir = tmp_path / "out_default_r"
    out_dir.mkdir()

    result = compute_sediment_yield(
        dem_uri=dem_path,
        k_uri=k_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )
    assert result.rainfall_erosivity == 300.0
    assert any("R-factor DEFAULT" in note for note in result.notes)
    expected = _expected_a(300.0, K_TEST, C_BY_IO_LULC_CLASS[5])
    assert np.isclose(result.mean_soil_loss_t_ha_yr, expected, rtol=1e-3)


def test_k_fallback_constant_with_note(synthetic_inputs, tmp_path) -> None:
    dem_path, _k_path, lc_path = synthetic_inputs
    out_dir = tmp_path / "out_kfb"
    out_dir.mkdir()

    # No k_uri -> the documented constant 0.2 with a note naming the fetch.
    result = compute_sediment_yield(
        rainfall_erosivity=R_TEST,
        dem_uri=dem_path,
        landcover_uri=lc_path,
        _output_dir=str(out_dir),
    )
    assert any("K-factor FALLBACK" in note and "fetch_statsgo_soils" in note
               for note in result.notes)
    expected = _expected_a(R_TEST, 0.2, C_BY_IO_LULC_CLASS[5])
    assert np.isclose(result.mean_soil_loss_t_ha_yr, expected, rtol=1e-3)


def test_water_class_yields_zero(synthetic_inputs, tmp_path) -> None:
    dem_path, k_path, _lc_path = synthetic_inputs
    water = np.full((N, N), 1, dtype="int16")  # Water: C = 0
    water_path = _write_raster(str(tmp_path / "water.tif"), water, nodata=0.0)
    out_dir = tmp_path / "out_water"
    out_dir.mkdir()

    result = compute_sediment_yield(
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


def test_aoi_clamp_raises(synthetic_inputs, tmp_path) -> None:
    _dem, _k, lc_path = synthetic_inputs
    wide = str(tmp_path / "wide.tif")
    with rasterio.open(
        wide, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_bounds(-117.5, 34.0, -117.0, 34.05, 10, 10),
        nodata=-9999.0,
    ) as dst:
        dst.write(np.zeros((10, 10), dtype="float32"), 1)
    with pytest.raises(SedimentYieldAoiTooLargeError):
        compute_sediment_yield(dem_uri=wide, landcover_uri=lc_path)


def test_missing_layers_refuse_never_fetch(synthetic_inputs) -> None:
    dem_path, _k, lc_path = synthetic_inputs
    with pytest.raises(SedimentYieldInputError, match="fetch_copernicus_dem"):
        compute_sediment_yield(dem_uri=None, landcover_uri=lc_path)
    with pytest.raises(SedimentYieldInputError, match="fetch_esri_landcover_10m"):
        compute_sediment_yield(dem_uri=dem_path, landcover_uri="")


def test_bad_erosivity_raises(synthetic_inputs) -> None:
    dem_path, _k, lc_path = synthetic_inputs
    for bad in (0.0, 1e9, "wet"):
        with pytest.raises(SedimentYieldInputError):
            compute_sediment_yield(dem_uri=dem_path, landcover_uri=lc_path,
                                   rainfall_erosivity=bad)


def test_the_declared_breaks_are_the_paint() -> None:
    """ONE table: the declared row's classes are the swatches the legend shows
    and the ranges the .qml paints."""
    from trid3nt_server.emission import presets
    from trid3nt_server.tools.derive.compute_sediment_yield.compute_sediment_yield import (
        _STYLE,
    )

    legend = presets.legend_key(_STYLE)
    assert legend.kind == "classed"
    # ONE table: the .qml paints the same upper bounds and colours the legend
    # shows, as DISCRETE bands (a linear ramp over orders of magnitude paints
    # everything below the worst gullies one flat colour).
    assert 'colorRampType="DISCRETE"' in legend.qml
    from xml.sax.saxutils import escape

    for _lo, hi, color, label in SEDIMENT_YIELD_LOG_CLASSES:
        assert f'value="{hi:.10g}" color="{color}"' in legend.qml
        assert escape(label) in legend.qml
