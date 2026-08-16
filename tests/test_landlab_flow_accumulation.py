"""Offline tests for the ``landlab_flow_accumulation`` template (ADR 0122,
hazard-easy-four #1).

Pins the agent-side flow-accumulation surface in ISOLATION (no solver, no
landlab library, no MinIO):

1. **Contract round-trip** -- the ``flow_accumulation`` analysis + synonyms
   normalize; the new run-arg fields (depression_handler / channel_threshold_cells)
   validate + alias; ``LandlabFlowAccumulationLayerURI`` is a ``LayerURI`` subtype
   carrying the drainage-area scalars. (no IO)
2. **build_spec arg-assembly** -- ``build_landlab_build_spec`` merges the routing
   knobs (flow_director via advanced_physics + depression_handler +
   channel_threshold_cells) onto the worker build_spec. (no IO)
3. **Routing-comparison chart builder** -- a deterministic Vega-Lite spec from a
   comparison list; empty -> None. (no IO)
4. **Channel-mask vectorization (postprocess-vs-fixture)** -- a synthetic channel
   mask COG vectorizes to a EPSG:4326 GeoJSON FeatureCollection. (rasterio)
5. **Tool bbox gate** -- a missing bbox returns a typed error envelope, never a
   crash. (async, no IO)
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabFlowAccumulationLayerURI,
    LandlabRunArgs,
)


# ===========================================================================
# (1) Contract round-trip.
# ===========================================================================
def test_flow_accumulation_analysis_and_field_normalization():
    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="drainage",  # synonym
        depression_handler="pf",  # synonym
        channel_threshold_cells=42,
        advanced_physics={"flow_director": "MFD"},
    )
    assert ra.analysis == "flow_accumulation"
    assert ra.depression_handler == "priority_flood"
    assert ra.channel_threshold_cells == 42
    assert ra.advanced_physics == {"flow_director": "MFD"}
    # JSON round-trip preserves the fields.
    ra2 = LandlabRunArgs.model_validate(ra.model_dump())
    assert ra2.analysis == "flow_accumulation"
    assert ra2.depression_handler == "priority_flood"


def test_flow_accumulation_defaults():
    ra = LandlabRunArgs(bbox=(-105.37, 39.998, -105.33, 40.032), analysis="flow_accumulation")
    assert ra.depression_handler == "fill"
    assert ra.channel_threshold_cells == 100


def test_flow_accumulation_layer_uri_is_layer_subtype():
    layer = LandlabFlowAccumulationLayerURI(
        layer_id="landlab-drainage-area-X",
        name="Drainage area",
        layer_type="raster",
        uri="s3://runs/X/landlab_drainage_area.tif",
        style_preset="continuous_drainage_area",
        role="primary",
        units="m^2",
        max_drainage_area_km2=4.2,
        mean_drainage_area_km2=0.03,
        channelized_area_fraction=0.011,
    )
    assert isinstance(layer, LayerURI)
    assert layer.max_drainage_area_km2 == 4.2
    assert 0.0 <= layer.channelized_area_fraction <= 1.0


def test_channel_threshold_cells_must_be_ge_1():
    with pytest.raises(Exception):
        LandlabRunArgs(
            bbox=(-105.37, 39.998, -105.33, 40.032),
            analysis="flow_accumulation",
            channel_threshold_cells=0,
        )


# ===========================================================================
# (2) build_spec arg-assembly.
# ===========================================================================
def test_build_spec_merges_routing_knobs():
    from trid3nt_server.workflows.landlab.run_landlab import build_landlab_build_spec

    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="flow_accumulation",
        depression_handler="priority_flood",
        channel_threshold_cells=75,
        advanced_physics={"flow_director": "Dinf"},
    )
    spec = build_landlab_build_spec(ra)
    assert spec["analysis"] == "flow_accumulation"
    assert spec["flow_director"] == "Dinf"  # via advanced_physics resolve
    assert spec["depression_handler"] == "priority_flood"
    assert spec["channel_threshold_cells"] == 75


# ===========================================================================
# (3) Routing-comparison chart builder.
# ===========================================================================
def test_routing_comparison_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.workflows.landlab.postprocess_landlab import (
        build_routing_comparison_chart_spec,
    )

    rows = [
        {"flow_director": "D8", "channelized_area_fraction": 0.011, "max_drainage_area_km2": 4.2},
        {"flow_director": "Dinf", "channelized_area_fraction": 0.13, "max_drainage_area_km2": 1.7},
        {"flow_director": "MFD", "channelized_area_fraction": 0.12, "max_drainage_area_km2": 2.3},
    ]
    spec = build_routing_comparison_chart_spec(rows)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert len(spec["data"]["values"]) == 3
    assert spec["encoding"]["x"]["field"] == "flow_director"
    assert build_routing_comparison_chart_spec([]) is None


# ===========================================================================
# (4) Channel-mask vectorization (postprocess-vs-fixture).
# ===========================================================================
def test_channel_mask_vectorizes_to_4326_geojson(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    from trid3nt_server.workflows.landlab.postprocess_landlab import (
        _vectorize_channel_mask,
    )

    # A 1-cell-wide vertical channel down the middle of a UTM grid.
    H, W = 12, 12
    mask = np.full((H, W), np.nan, dtype="float32")
    mask[:, W // 2] = 1.0
    transform = from_origin(500000.0, 4400000.0, 30.0, 30.0)
    cog = tmp_path / "channel.tif"
    with rasterio.open(
        cog, "w", driver="GTiff", height=H, width=W, count=1, dtype="float32",
        crs="EPSG:32613", transform=transform, nodata=-9999.0,
    ) as ds:
        ds.write(np.where(np.isfinite(mask), mask, -9999.0), 1)

    fc = _vectorize_channel_mask(cog)
    assert fc is not None
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) >= 1
    # coords reprojected to EPSG:4326 (lon/lat magnitude).
    lon, lat = fc["features"][0]["geometry"]["coordinates"][0][0]
    assert -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def test_channel_vectorization_empty_mask_returns_none(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    from trid3nt_server.workflows.landlab.postprocess_landlab import (
        _vectorize_channel_mask,
    )

    H, W = 8, 8
    transform = from_origin(500000.0, 4400000.0, 30.0, 30.0)
    cog = tmp_path / "empty.tif"
    with rasterio.open(
        cog, "w", driver="GTiff", height=H, width=W, count=1, dtype="float32",
        crs="EPSG:32613", transform=transform, nodata=-9999.0,
    ) as ds:
        ds.write(np.full((H, W), -9999.0, dtype="float32"), 1)
    assert _vectorize_channel_mask(cog) is None


# ===========================================================================
# (5) Tool bbox gate.
# ===========================================================================
def test_tool_missing_bbox_returns_typed_error():
    from trid3nt_server.workflows.landlab.flow_accumulation.flow_accumulation import (
        landlab_flow_accumulation,
    )

    out = asyncio.run(landlab_flow_accumulation(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "LANDLAB_PARAMS_INCOMPLETE"
