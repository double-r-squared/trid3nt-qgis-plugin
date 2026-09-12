"""Unit tests for ``compute_model_residuals``, with no network.

Inputs are SYNTHESIZED locally - a UTM ramp raster, so bilinear sampling is exact
at any point, and a small point layer of observed values - giving exact error
statistics. Covered: footprint filtering and its refusals, the small-n caveat,
the units warning, field auto-detection and its override, the bbox fetch path."""

from __future__ import annotations

import json
import pathlib
import tempfile

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from shapely.geometry import Point

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.compute_model_residuals.compute_model_residuals import (
    ModelResidualsLayerURI,
    ResidualsAllNodataError,
    ResidualsInputError,
    ResidualsNoObservationsError,
    compute_model_residuals,
)

# Synthetic simulated-head grid: 40x40 at 30 m in UTM zone 11N.
N = 40
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"
NODATA = -9999.0

BASE = 100.0  # ft
SLOPE = 0.5  # ft per column -- head varies linearly in x only


def _cell_center(col: int, row: int) -> tuple[float, float]:
    """UTM coordinates of the center of grid cell (col, row from top)."""
    return X0 + (col + 0.5) * RES, Y0 + (N - row - 0.5) * RES


def _utm_to_lonlat(x: float, y: float) -> tuple[float, float]:
    import pyproj

    tf = pyproj.Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    return tf.transform(x, y)


def _write_head_raster(path: str, nodata_cells: list[tuple[int, int]] | None = None) -> str:
    """Linear-ramp head raster: value(row, col) = BASE + SLOPE * col."""
    data = np.zeros((N, N), dtype="float64")
    for row in range(N):
        for col in range(N):
            data[row, col] = BASE + SLOPE * col
    for row, col in (nodata_cells or []):
        data[row, col] = NODATA
    transform = from_bounds(X0, Y0, X0 + N * RES, Y0 + N * RES, N, N)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=N,
        width=N,
        count=1,
        dtype="float64",
        crs=CRS,
        transform=transform,
        nodata=NODATA,
    ) as dst:
        dst.write(data, 1)
    return path


def _write_observations(path: str, records: list[dict]) -> str:
    """Write a GeoJSON FeatureCollection of observed points.

    Each record must carry ``col``/``row`` (grid cell, converted to lon/lat)
    plus arbitrary properties.
    """
    features = []
    for rec in records:
        rec = dict(rec)
        col, row = rec.pop("col"), rec.pop("row")
        lon, lat = _utm_to_lonlat(*_cell_center(col, row))
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": rec,
            }
        )
    fc = {"type": "FeatureCollection", "features": features}
    pathlib.Path(path).write_text(json.dumps(fc))
    return path


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_model_residuals"]
    assert entry.fn is compute_model_residuals
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is False  # reads the layers it is handed


def test_residuals_matches_hand_computed(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    # Three points colinear with the ramp, each observed = simulated + 2.0 ft.
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": BASE + SLOPE * 5 + 2.0,
             "parameter_code": "72150", "vertical_datum": "NAVD88", "unit": "ft"},
            {"col": 10, "row": 20, "id": 2, "water_level": BASE + SLOPE * 10 + 2.0,
             "parameter_code": "72150", "vertical_datum": "NAVD88", "unit": "ft"},
            {"col": 15, "row": 20, "id": 3, "water_level": BASE + SLOPE * 15 + 2.0,
             "parameter_code": "72150", "vertical_datum": "NAVD88", "unit": "ft"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster,
        observations_layer_uri=obs,
        _output_dir=str(tmp_path),
    )

    assert isinstance(result, ModelResidualsLayerURI)
    assert isinstance(result, LayerURI)
    assert result.layer_type == "vector"
    assert result.style == {"kind": "reference", "geometry": "point"}
    assert result.name == "Model residuals (3 points)"
    assert result.n_points == 3
    assert result.mean_error == pytest.approx(2.0, abs=1e-3)
    assert result.bias == pytest.approx(2.0, abs=1e-3)
    assert result.rmse == pytest.approx(2.0, abs=1e-3)
    assert result.mae == pytest.approx(2.0, abs=1e-3)
    assert result.min_residual == pytest.approx(2.0, abs=1e-3)
    assert result.max_residual == pytest.approx(2.0, abs=1e-3)
    assert result.small_n_caveat is False
    assert "low by 2" in result.interpretation
    assert "ELEVATION" in result.units_warning

    gdf = gpd.read_file(result.uri)
    assert len(gdf) == 3
    assert set(["observed", "simulated", "residual"]).issubset(gdf.columns)
    assert gdf["residual"].apply(lambda v: abs(v - 2.0) < 1e-3).all()

    assert result.legend is not None
    assert result.legend.kind == "continuous"
    assert result.legend.colormap == "rdbu"
    assert result.legend.vmin == pytest.approx(-2.0, abs=1e-3)
    assert result.legend.vmax == pytest.approx(2.0, abs=1e-3)

    exp_bbox = transform_bounds(CRS, "EPSG:4326", X0, Y0, X0 + N * RES, Y0 + N * RES)
    assert result.bbox == pytest.approx(exp_bbox, abs=1e-4)


def test_footprint_filtering(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": BASE + SLOPE * 5 + 1.0,
             "parameter_code": "72150"},
            # Far outside the raster's UTM extent.
            {"col": -5000, "row": -5000, "id": 2, "water_level": 999.0,
             "parameter_code": "72150"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster,
        observations_layer_uri=obs,
        _output_dir=str(tmp_path),
    )
    assert result.n_points == 1
    assert any("outside the model raster" in n for n in result.notes)


def test_no_points_in_footprint_raises(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [{"col": -5000, "row": -5000, "id": 1, "water_level": 1.0, "parameter_code": "72150"}],
    )
    with pytest.raises(ResidualsNoObservationsError):
        compute_model_residuals(
            model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
        )


def test_all_nodata_raises(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"), nodata_cells=[(20, 5), (20, 10)])
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": 1.0, "parameter_code": "72150"},
            {"col": 10, "row": 20, "id": 2, "water_level": 2.0, "parameter_code": "72150"},
        ],
    )
    with pytest.raises(ResidualsAllNodataError):
        compute_model_residuals(
            model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
        )


def test_small_n_caveat(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": BASE + SLOPE * 5 + 3.0,
             "parameter_code": "72150"},
            {"col": 10, "row": 20, "id": 2, "water_level": BASE + SLOPE * 10 + 3.0,
             "parameter_code": "72150"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    assert result.n_points == 2
    assert result.small_n_caveat is True
    assert any("Small sample" in n for n in result.notes)
    assert "CAVEAT" in result.interpretation
    # Stats are still fully computed, not suppressed.
    assert result.mean_error == pytest.approx(3.0, abs=1e-3)


def test_units_warning_elevation_pcode(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": BASE + SLOPE * 5,
             "parameter_code": "62611", "vertical_datum": "NAVD88"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    assert "ELEVATION" in result.units_warning
    assert "NAVD88" in result.units_warning


def test_units_warning_depth_pcode(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": 12.0, "parameter_code": "72019"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    assert "DEPTH-TO-WATER" in result.units_warning
    assert "NOT directly comparable" in result.units_warning or "NOT a head elevation" in result.units_warning


def test_units_warning_mixed_pcodes_filters(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": BASE + SLOPE * 5 + 1.0,
             "parameter_code": "72150"},
            {"col": 10, "row": 20, "id": 2, "water_level": 15.0, "parameter_code": "72019"},
            {"col": 15, "row": 20, "id": 3, "water_level": BASE + SLOPE * 15 + 1.0,
             "parameter_code": "62611"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    # Only the two elevation-referenced points survive the filter.
    assert result.n_points == 2
    assert "ELEVATION" in result.units_warning
    assert any("dropped" in n or "mixed" in n.lower() for n in result.notes)


def test_generic_field_auto_detect(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "value": BASE + SLOPE * 5 + 0.5},
            {"col": 10, "row": 20, "id": 2, "value": BASE + SLOPE * 10 + 0.5},
            {"col": 15, "row": 20, "id": 3, "value": BASE + SLOPE * 15 + 0.5},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster, observations_layer_uri=obs, _output_dir=str(tmp_path)
    )
    assert result.n_points == 3
    assert result.mean_error == pytest.approx(0.5, abs=1e-3)
    assert "no known field-semantics metadata" in result.units_warning


def test_observed_value_field_verbatim(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [
            {"col": 5, "row": 20, "id": 1, "water_level": 999.0,
             "custom_head": BASE + SLOPE * 5 + 1.5, "parameter_code": "72019"},
        ],
    )
    result = compute_model_residuals(
        model_layer_uri=raster,
        observations_layer_uri=obs,
        observed_value_field="custom_head",
        _output_dir=str(tmp_path),
    )
    # Uses custom_head verbatim, NOT water_level (999.0 would give a huge residual).
    assert result.mean_error == pytest.approx(1.5, abs=1e-3)


def test_missing_field_raises(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    obs = _write_observations(
        str(tmp_path / "obs.geojson"),
        [{"col": 5, "row": 20, "id": 1, "water_level": 1.0}],
    )
    with pytest.raises(ResidualsInputError):
        compute_model_residuals(
            model_layer_uri=raster,
            observations_layer_uri=obs,
            observed_value_field="does_not_exist",
            _output_dir=str(tmp_path),
        )


def test_missing_observations_refuse_never_fetch(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    with pytest.raises(ResidualsInputError, match="fetch_usgs_groundwater_levels"):
        compute_model_residuals(model_layer_uri=raster, observations_layer_uri=None,
                                _output_dir=str(tmp_path))


def test_bad_model_uri_raises(tmp_path) -> None:
    with pytest.raises(ResidualsInputError):
        compute_model_residuals(
            model_layer_uri="", observations_layer_uri="obs.fgb", _output_dir=str(tmp_path)
        )


def test_corpus() -> None:
    from trid3nt_server.tools.search.search_tools import search_tools as dd

    corpus = dd._load_corpus()
    assert len(corpus.get("compute_model_residuals", [])) >= 5


def test_uri_registry_resolvable_params() -> None:
    from trid3nt_server.render.uri_registry import RESOLVABLE_URI_PARAMS

    assert "model_layer_uri" in RESOLVABLE_URI_PARAMS
    assert "observations_layer_uri" in RESOLVABLE_URI_PARAMS
