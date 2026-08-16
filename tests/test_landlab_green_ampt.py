"""Offline tests for the ``landlab_green_ampt_overland_flow`` template (ADR 0123,
hazard-easy-four continuation #1).

Pins the agent-side Green-Ampt storm-partition surface in ISOLATION (no solver,
no landlab library, no MinIO):

1. **Contract round-trip** -- the ``green_ampt_overland_flow`` analysis + synonyms
   normalize; the new run-arg fields (soil_hydraulic_conductivity_m_s /
   initial_soil_moisture_content / green_ampt_soil_type) validate;
   ``LandlabGreenAmptLayerURI`` is a ``LayerURI`` subtype carrying the partition
   scalars. (no IO)
2. **build_spec arg-assembly** -- ``build_landlab_build_spec`` merges the
   Green-Ampt knobs onto the worker build_spec. (no IO)
3. **Partition-chart builder** -- a deterministic Vega-Lite spec from the two
   fractions; both-zero -> None. (no IO)
4. **Infiltration COG reproject (postprocess-vs-fixture)** -- a synthetic UTM
   infiltration COG reprojects to EPSG:4326. (rasterio)
5. **Tool bbox gate** -- a missing bbox returns a typed error envelope. (async)
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabGreenAmptLayerURI,
    LandlabRunArgs,
)


# ===========================================================================
# (1) Contract round-trip.
# ===========================================================================
def test_green_ampt_analysis_and_field_normalization():
    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="infiltration",  # synonym
        soil_hydraulic_conductivity_m_s=3e-5,
        initial_soil_moisture_content=0.2,
        green_ampt_soil_type="loam",
    )
    assert ra.analysis == "green_ampt_overland_flow"
    assert ra.soil_hydraulic_conductivity_m_s == 3e-5
    assert ra.initial_soil_moisture_content == 0.2
    assert ra.green_ampt_soil_type == "loam"
    ra2 = LandlabRunArgs.model_validate(ra.model_dump())
    assert ra2.analysis == "green_ampt_overland_flow"


def test_green_ampt_defaults():
    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="green_ampt_overland_flow",
    )
    assert ra.soil_hydraulic_conductivity_m_s > 0.0
    assert 0.0 <= ra.initial_soil_moisture_content < 1.0
    assert ra.green_ampt_soil_type


def test_green_ampt_layer_uri_is_layer_subtype():
    layer = LandlabGreenAmptLayerURI(
        layer_id="landlab-infiltration-depth-X",
        name="Infiltration depth",
        layer_type="raster",
        uri="s3://runs/X/landlab_infiltration_depth.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
        infiltrated_fraction=0.79,
        runoff_fraction=0.21,
        mean_infiltration_mm=35.5,
        mean_runoff_mm=9.5,
        total_rainfall_mm=45.0,
    )
    assert isinstance(layer, LayerURI)
    assert 0.0 <= layer.infiltrated_fraction <= 1.0
    assert 0.0 <= layer.runoff_fraction <= 1.0
    assert layer.total_rainfall_mm == 45.0


def test_initial_soil_moisture_must_be_lt_1():
    with pytest.raises(Exception):
        LandlabRunArgs(
            bbox=(-105.37, 39.998, -105.33, 40.032),
            analysis="green_ampt_overland_flow",
            initial_soil_moisture_content=1.0,
        )


# ===========================================================================
# (2) build_spec arg-assembly.
# ===========================================================================
def test_build_spec_merges_green_ampt_knobs():
    from trid3nt_server.workflows.landlab.run_landlab import build_landlab_build_spec

    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="green_ampt_overland_flow",
        rainfall_intensity_mm_hr=90.0,
        storm_duration_hr=0.5,
        soil_hydraulic_conductivity_m_s=2e-5,
        initial_soil_moisture_content=0.18,
        green_ampt_soil_type="silt loam",
    )
    spec = build_landlab_build_spec(ra)
    assert spec["analysis"] == "green_ampt_overland_flow"
    assert spec["soil_hydraulic_conductivity_m_s"] == 2e-5
    assert spec["initial_soil_moisture_content"] == 0.18
    assert spec["green_ampt_soil_type"] == "silt loam"
    assert spec["rainfall_intensity_mm_hr"] == 90.0


# ===========================================================================
# (3) Partition-chart builder.
# ===========================================================================
def test_partition_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.workflows.landlab.postprocess_landlab import (
        build_infiltration_partition_chart_spec,
    )

    spec = build_infiltration_partition_chart_spec(0.79, 0.21)
    assert is_structurally_valid_vega_lite_spec(spec)
    vals = {v["partition"]: v["fraction"] for v in spec["data"]["values"]}
    assert vals["infiltration"] == 0.79
    assert vals["runoff"] == 0.21
    assert build_infiltration_partition_chart_spec(0.0, 0.0) is None


# ===========================================================================
# (4) Infiltration COG reproject (postprocess-vs-fixture).
# ===========================================================================
def test_infiltration_cog_reprojects_to_4326(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    from trid3nt_server.workflows.landlab.postprocess_landlab import (
        _reproject_field_cog_4326,
    )

    H, W = 16, 16
    arr = np.linspace(0.0, 0.045, H * W, dtype="float32").reshape(H, W)
    transform = from_origin(500000.0, 4400000.0, 30.0, 30.0)
    cog = tmp_path / "infil.tif"
    with rasterio.open(
        cog, "w", driver="GTiff", height=H, width=W, count=1, dtype="float32",
        crs="EPSG:32613", transform=transform, nodata=float("nan"),
    ) as ds:
        ds.write(arr, 1)

    dst, bbox = _reproject_field_cog_4326(cog)
    try:
        assert bbox is not None
        min_lon, min_lat, max_lon, max_lat = bbox
        assert -180.0 <= min_lon < max_lon <= 180.0
        assert -90.0 <= min_lat < max_lat <= 90.0
    finally:
        try:
            dst.unlink()
        except OSError:
            pass


# ===========================================================================
# (5) Tool bbox gate.
# ===========================================================================
def test_tool_missing_bbox_returns_typed_error():
    from trid3nt_server.workflows.landlab.green_ampt.green_ampt import (
        landlab_green_ampt_overland_flow,
    )

    out = asyncio.run(landlab_green_ampt_overland_flow(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "LANDLAB_PARAMS_INCOMPLETE"
