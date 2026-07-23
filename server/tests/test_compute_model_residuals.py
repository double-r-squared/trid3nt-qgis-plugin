"""Unit tests for ``compute_model_residuals`` (no network).

Inputs are SYNTHESIZED locally: a UTM "simulated head" raster with a linear
ramp (so bilinear sampling reduces to an exact value at any point, in-bounds
or off-center) + a small EPSG:4326 point GeoJSON of "observed" values with
USGS-groundwater-shaped attributes (``water_level`` / ``parameter_code`` /
``vertical_datum`` / ``unit``), passed via ``observations_layer_uri`` --
mirrors the ``compute_flood_depth_damage`` test pattern.

Coverage:
1.  ``test_registered`` -- TOOL_REGISTRY entry, cacheable=False /
    live-no-cache, open_world_hint=True.
2.  ``test_residuals_matches_hand_computed`` -- exact-offset observed values
    at points colinear with the ramp -> exact mean_error/rmse/mae/bias.
3.  ``test_footprint_filtering`` -- a point outside the raster extent is
    dropped + noted; not counted in n_points.
4.  ``test_no_points_in_footprint_raises`` -- every point outside -> typed
    honest error.
5.  ``test_all_nodata_raises`` -- every in-footprint point lands on nodata.
6.  ``test_small_n_caveat`` -- n=2 points still returns full stats + a
    small-n caveat note and flag.
7.  ``test_units_warning_elevation_pcode`` / ``_depth_pcode`` / ``_mixed`` --
    the honest units/semantics warning for each USGS pcode family, including
    the mixed-fetch filter-and-note behaviour.
8.  ``test_generic_field_auto_detect`` -- a non-USGS layer with a plain
    ``value`` column is auto-detected + gets the generic disclaimer.
9.  ``test_observed_value_field_verbatim`` -- explicit override bypasses
    auto-detection.
10. ``test_missing_field_raises`` -- unresolvable field -> typed input error
    listing available columns.
11. ``test_bbox_fetch_path`` -- no observations_layer_uri; bbox drives the
    shared-core USGS fetch (mocked), correct bbox passed through.
12. ``test_no_selector_raises`` -- neither observations_layer_uri nor bbox.
13. ``test_category_and_corpus`` -- primary/secondary category + routing
    corpus presence.
14. ``test_uri_registry_resolvable_params`` -- model_layer_uri /
    observations_layer_uri are handle-resolvable (registry-handle path).
"""

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
from trid3nt_server.tools.processing.compute_model_residuals import (
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
    assert entry.metadata.open_world_hint is True  # may fetch USGS wells


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
    assert result.style_preset == "model_residuals"
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
    assert result.legend.value_field == "residual"
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


def test_bbox_fetch_path(tmp_path, monkeypatch) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))

    # Build a synthetic USGS-shaped FlatGeobuf the mocked shared core returns.
    lon, lat = _utm_to_lonlat(*_cell_center(5, 20))
    gdf = gpd.GeoDataFrame(
        {
            "site_no": ["12345"],
            "water_level": [BASE + SLOPE * 5 + 4.0],
            "parameter_code": ["72150"],
            "vertical_datum": ["NAVD88"],
            "unit": ["ft"],
        },
        geometry=[Point(lon, lat)],
        crs="EPSG:4326",
    )
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        fgb_path = f.name
    gdf.to_file(fgb_path, driver="FlatGeobuf", engine="pyogrio")
    with open(fgb_path, "rb") as f:
        fgb_bytes = f.read()

    import trid3nt_server.tools.fetchers.hydrology.fetch_usgs_groundwater_levels as gw_mod

    captured: dict = {}

    def _fake_fetch(*, state_fips, bbox, scope_label):
        captured["state_fips"] = state_fips
        captured["bbox"] = bbox
        return fgb_bytes, (lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01)

    monkeypatch.setattr(gw_mod, "_fetch_usgs_groundwater_levels_bytes", _fake_fetch)

    bbox = (-120.0, 34.0, -119.0, 35.0)
    result = compute_model_residuals(
        model_layer_uri=raster, bbox=bbox, _output_dir=str(tmp_path)
    )
    assert result.n_points == 1
    assert result.mean_error == pytest.approx(4.0, abs=1e-3)
    assert captured["state_fips"] is None
    assert captured["bbox"] == pytest.approx(bbox, abs=1e-5)
    assert any("fetch_usgs_groundwater_levels" in n for n in result.notes)


def test_no_selector_raises(tmp_path) -> None:
    raster = _write_head_raster(str(tmp_path / "head.tif"))
    with pytest.raises(ResidualsInputError):
        compute_model_residuals(model_layer_uri=raster, _output_dir=str(tmp_path))


def test_bad_model_uri_raises(tmp_path) -> None:
    with pytest.raises(ResidualsInputError):
        compute_model_residuals(
            model_layer_uri="", bbox=(-120.0, 34.0, -119.0, 35.0), _output_dir=str(tmp_path)
        )


def test_category_and_corpus() -> None:
    import yaml

    from trid3nt_server import categories
    from trid3nt_server.tools.discovery import search_tools as dd

    assert (
        categories.PRIMARY_CATEGORY["compute_model_residuals"]
        == "geographic_primitives"
    )
    assert "hazard_modeling" in categories.SECONDARY_CATEGORIES.get(
        "compute_model_residuals", ()
    )
    corpus_path = pathlib.Path(dd._default_corpus_path())
    corpus = yaml.safe_load(corpus_path.read_text())
    assert len(corpus.get("compute_model_residuals", [])) >= 5


def test_uri_registry_resolvable_params() -> None:
    from trid3nt_server.uri_registry import RESOLVABLE_URI_PARAMS

    assert "model_layer_uri" in RESOLVABLE_URI_PARAMS
    assert "observations_layer_uri" in RESOLVABLE_URI_PARAMS
