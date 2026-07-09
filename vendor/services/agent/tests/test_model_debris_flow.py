"""Unit tests for the ``model_debris_flow`` composer tool (no network).

All inputs are SYNTHESIZED locally and passed via the override URIs
(``dem_uri`` / ``severity_uri`` / ``kf_uri``), so the full pfdf pipeline
(watershed -> Segments -> Staley 2017 M1 -> Gartner 2014 -> Cannon 2010)
runs for real without touching Copernicus / MTBS / STATSGO.

Coverage:
1.  ``test_registered`` -- tool in TOOL_REGISTRY with cacheable=False /
    ttl_class="live-no-cache".
2.  ``test_full_pipeline_synthetic`` -- 60x60 UTM valley DEM + BARC4
    moderate/high burn patch + constant KF -> segments GeoJSON with the three
    required properties (likelihood, volume_m3, hazard_class), consistent
    counts, and a typed ``DebrisFlowLayerURI`` return (a ``LayerURI``
    subclass, so the emit_tool_call wrap-site persists the hazard layer to
    the case record -- renders + exports + cold view).
3.  ``test_dnbr_severity_input`` -- a CONTINUOUS dNBR severity_uri raster is
    auto-detected and classified via pfdf.severity.estimate.
4.  ``test_aoi_clamp_raises`` -- AOI over 0.15 deg per side ->
    AoiTooLargeError.
5.  ``test_no_burn_raises`` -- all-unburned severity raster -> NoBurnDataError.
6.  ``test_bad_bbox_raises`` / ``test_bad_intensity_raises`` -- input
    validation is typed.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_contracts.execution import LayerURI

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.model_debris_flow import (
    AoiTooLargeError,
    DebrisFlowInputError,
    DebrisFlowLayerURI,
    NoBurnDataError,
    model_debris_flow,
)

# Small legal AOI (bbox is used for validation/labeling only when all three
# override URIs are supplied; the synthetic rasters carry their own UTM grid).
BBOX = (-117.05, 34.00, -116.95, 34.05)

# Synthetic grid: 60x60 at 30 m in UTM zone 11N.
N = 60
RES = 30.0
X0, Y0 = 500000.0, 4000000.0
CRS = "EPSG:32611"


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


@pytest.fixture()
def synthetic_inputs(tmp_path):
    """DEM with a south-draining valley + moderate/high burn patch + constant KF."""
    rows = np.arange(N, dtype=np.float64)
    cols = np.arange(N, dtype=np.float64)
    # Steep valley: 8 m drop per row southward + V-shaped cross slope funnels
    # flow to the center column (slope gradients well above the 23-degree
    # threshold so the M1 terrain variable is exercised).
    z = 1000.0 - rows[:, None] * 8.0 + np.abs(cols[None, :] - 30.0) * 12.0
    rng = np.random.default_rng(7)
    z = (z + rng.normal(0.0, 0.05, size=z.shape)).astype("float32")
    dem_path = _write_raster(str(tmp_path / "dem.tif"), z, nodata=-9999.0)

    # BARC4 severity: unburned (1) background, a large moderate (3) patch with
    # a high (4) core covering the upper valley.
    sev = np.ones((N, N), dtype="int16")
    sev[2:48, 8:52] = 3
    sev[8:32, 18:42] = 4
    sev_path = _write_raster(str(tmp_path / "severity.tif"), sev, nodata=0.0)

    kf = np.full((N, N), 0.25, dtype="float32")
    kf_path = _write_raster(str(tmp_path / "kf.tif"), kf, nodata=-1.0)
    return dem_path, sev_path, kf_path


def test_registered() -> None:
    entry = TOOL_REGISTRY["model_debris_flow"]
    assert entry.fn is model_debris_flow
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"


def test_full_pipeline_synthetic(synthetic_inputs, tmp_path) -> None:
    dem_path, sev_path, kf_path = synthetic_inputs
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    result = model_debris_flow(
        bbox=BBOX,
        rainfall_intensity_mm_h=40.0,
        dem_uri=dem_path,
        severity_uri=sev_path,
        kf_uri=kf_path,
        _output_dir=str(out_dir),
    )

    # The v2 return type: a typed LayerURI subclass, so the emit_tool_call
    # add_loaded_layer gate (isinstance(result, LayerURI)) persists the layer
    # to the case record. A dict return would render live but never persist.
    assert isinstance(result, DebrisFlowLayerURI)
    assert isinstance(result, LayerURI)
    assert result.segment_count > 0
    assert result.rainfall_intensity_mm_h == 40.0
    assert 0.0 < result.burned_fraction <= 1.0
    assert isinstance(result.notes, list) and result.notes

    assert result.layer_type == "vector"
    assert result.style_preset == "debris_flow_hazard"
    assert tuple(result.bbox) == BBOX

    # The GeoJSON artifact exists locally (offline write path) and carries the
    # three required per-segment properties.
    assert os.path.exists(result.uri)
    with open(result.uri) as f:
        fc = json.load(f)
    feats = fc["features"]
    assert len(feats) == result.segment_count

    allowed = {"Low", "Moderate", "High", "Unknown"}
    classes = set()
    for feat in feats:
        assert feat["geometry"]["type"] == "LineString"
        props = feat["properties"]
        assert 0.0 <= props["likelihood"] <= 1.0
        assert props["volume_m3"] >= 0.0
        assert props["hazard_class"] in allowed
        classes.add(props["hazard_class"])
    # Hazard classes are present (at least one real class label).
    assert classes & {"Low", "Moderate", "High"}

    # Count bookkeeping is consistent with the per-feature classes.
    assert result.high_hazard_count == sum(
        1 for feat in feats if feat["properties"]["hazard_class"] == "High"
    )
    assert (
        result.high_hazard_count
        + result.moderate_hazard_count
        + result.low_hazard_count
        <= result.segment_count
    )
    assert result.likelihood_max is not None
    assert result.volume_max_m3 is not None


def test_dnbr_severity_input(synthetic_inputs, tmp_path) -> None:
    dem_path, _sev_path, kf_path = synthetic_inputs
    # Continuous dNBR raster (values >> 4 so the auto-detection takes the
    # dNBR branch and classifies via pfdf.severity.estimate).
    dnbr = np.zeros((N, N), dtype="float32")
    dnbr[2:48, 8:52] = 350.0  # moderate
    dnbr[8:32, 18:42] = 620.0  # high
    dnbr_path = _write_raster(str(tmp_path / "dnbr.tif"), dnbr, nodata=-32768.0)
    out_dir = tmp_path / "out_dnbr"
    out_dir.mkdir()

    result = model_debris_flow(
        bbox=BBOX,
        dem_uri=dem_path,
        severity_uri=dnbr_path,
        kf_uri=kf_path,
        _output_dir=str(out_dir),
    )
    assert isinstance(result, DebrisFlowLayerURI)
    assert result.segment_count > 0
    assert any("severity.estimate" in note for note in result.notes)


def test_aoi_clamp_raises() -> None:
    with pytest.raises(AoiTooLargeError):
        model_debris_flow(bbox=(-117.3, 34.0, -117.0, 34.05))  # 0.3 deg wide
    with pytest.raises(AoiTooLargeError):
        model_debris_flow(bbox=(-117.1, 34.0, -117.0, 34.3))  # 0.3 deg tall


def test_no_burn_raises(synthetic_inputs, tmp_path) -> None:
    dem_path, _sev_path, kf_path = synthetic_inputs
    unburned = np.ones((N, N), dtype="int16")  # BARC4 class 1 everywhere
    unburned_path = _write_raster(
        str(tmp_path / "unburned.tif"), unburned, nodata=0.0
    )
    with pytest.raises(NoBurnDataError):
        model_debris_flow(
            bbox=BBOX,
            dem_uri=dem_path,
            severity_uri=unburned_path,
            kf_uri=kf_path,
            _output_dir=str(tmp_path),
        )


def test_bad_bbox_raises() -> None:
    with pytest.raises(DebrisFlowInputError):
        model_debris_flow(bbox=(-117.0, 34.05, -117.05, 34.00))  # reversed
    with pytest.raises(DebrisFlowInputError):
        model_debris_flow(bbox=(-117.0, 34.0, -116.99))  # wrong arity
    with pytest.raises(DebrisFlowInputError):
        model_debris_flow(bbox=("a", 34.0, -116.99, 34.05))  # non-numeric


def test_bad_intensity_raises() -> None:
    with pytest.raises(DebrisFlowInputError):
        model_debris_flow(bbox=BBOX, rainfall_intensity_mm_h=0.0)
    with pytest.raises(DebrisFlowInputError):
        model_debris_flow(bbox=BBOX, rainfall_intensity_mm_h=9999.0)
    with pytest.raises(DebrisFlowInputError):
        model_debris_flow(bbox=BBOX, rainfall_intensity_mm_h="wet")
