"""Offline tests for the Landlab groundwater templates (ADR 0214).

Pins the agent-side GroundwaterDupuitPercolator surface in ISOLATION (no solver,
no landlab library, no MinIO) for both templates:

- ``landlab_groundwater_water_table`` (analysis ``groundwater_steady``): the
  depth-to-water + water-table + seepage state under constant recharge.
- ``landlab_groundwater_storm_recession`` (analysis ``groundwater_storm``): the
  storm-driven seepage/baseflow hydrograph + recession.

1. Contract round-trip -- the analyses + synonyms normalize; the aquifer + storm
   run-arg fields validate; the two LayerURI carriers are LayerURI subtypes.
2. build_spec arg-assembly -- ``build_landlab_build_spec`` merges the groundwater
   knobs onto the worker build_spec.
3. Chart builders -- deterministic Vega-Lite specs from scalars/series.
4. COG reproject -- a synthetic UTM COG reprojects to EPSG:4326.
5. Tool bbox gate -- a missing bbox returns a typed error envelope. (async)
6. Registration + categories.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabGroundwaterLayerURI,
    LandlabGroundwaterStormLayerURI,
    LandlabRunArgs,
)


# ===========================================================================
# (1) Contract round-trip.
# ===========================================================================
def test_groundwater_analysis_synonyms_normalize():
    for syn in ("water_table", "depth_to_water", "aquifer", "groundwater", "seepage"):
        ra = LandlabRunArgs(bbox=(-83.5, 35.0, -83.4, 35.08), analysis=syn)
        assert ra.analysis == "groundwater_steady"
    for syn in ("groundwater_recession", "seepage_hydrograph", "aquifer_recession"):
        ra = LandlabRunArgs(bbox=(-83.5, 35.0, -83.4, 35.08), analysis=syn)
        assert ra.analysis == "groundwater_storm"


def test_groundwater_run_arg_fields_validate():
    ra = LandlabRunArgs(
        bbox=(-83.5, 35.0, -83.4, 35.08),
        analysis="groundwater_steady",
        gw_hydraulic_conductivity_m_s=5e-4,
        gw_porosity=0.25,
        gw_aquifer_thickness_m=30.0,
        gw_recharge_mm_yr=150.0,
        gw_storm_aquifer_thickness_m=6.0,
        gw_storm_mean_depth_mm=25.0,
        gw_storm_total_days=90.0,
    )
    assert ra.gw_hydraulic_conductivity_m_s == 5e-4
    assert ra.gw_porosity == 0.25
    assert ra.gw_aquifer_thickness_m == 30.0
    assert ra.gw_recharge_mm_yr == 150.0
    assert ra.gw_storm_total_days == 90.0
    ra2 = LandlabRunArgs.model_validate(ra.model_dump())
    assert ra2.analysis == "groundwater_steady"


def test_groundwater_porosity_must_be_lt_1():
    with pytest.raises(Exception):
        LandlabRunArgs(
            bbox=(-83.5, 35.0, -83.4, 35.08),
            analysis="groundwater_steady",
            gw_porosity=1.0,
        )


def test_groundwater_layer_uris_are_layer_subtypes():
    steady = LandlabGroundwaterLayerURI(
        layer_id="landlab-depth-to-water-X",
        name="Depth to water table",
        layer_type="raster",
        uri="s3://runs/X/landlab_depth_to_water.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
        mean_depth_to_water_m=4.2,
        max_depth_to_water_m=12.0,
        min_depth_to_water_m=0.0,
        baseflow_discharge_m3s=0.058,
        seeping_area_fraction=0.13,
        mass_balance_rel_error=-3e-4,
        recharge_mm_yr=200.0,
    )
    assert isinstance(steady, LayerURI)
    assert abs(steady.mass_balance_rel_error) < 0.01

    storm = LandlabGroundwaterStormLayerURI(
        layer_id="landlab-peak-seepage-X",
        name="Peak groundwater seepage",
        layer_type="raster",
        uri="s3://runs/X/landlab_peak_seepage.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="m/s",
        peak_baseflow_m3s=0.26,
        final_baseflow_m3s=0.05,
        recession_timescale_days=3.1,
        seeping_area_fraction=0.12,
        mass_balance_rel_error=-9e-4,
        n_storms=34,
        total_days=120.0,
    )
    assert isinstance(storm, LayerURI)
    assert storm.recession_timescale_days >= 0.0


# ===========================================================================
# (2) build_spec arg-assembly.
# ===========================================================================
def test_build_spec_merges_groundwater_knobs():
    from trid3nt_server.agent.workflows.landlab.run_landlab import (
        build_landlab_build_spec,
    )

    ra = LandlabRunArgs(
        bbox=(-83.5, 35.0, -83.4, 35.08),
        analysis="groundwater_steady",
        gw_hydraulic_conductivity_m_s=2e-4,
        gw_porosity=0.28,
        gw_aquifer_thickness_m=18.0,
        gw_recharge_mm_yr=250.0,
        gw_storm_aquifer_thickness_m=7.0,
        gw_storm_mean_depth_mm=22.0,
        gw_storm_total_days=100.0,
    )
    spec = build_landlab_build_spec(ra)
    assert spec["analysis"] == "groundwater_steady"
    assert spec["gw_hydraulic_conductivity_m_s"] == 2e-4
    assert spec["gw_porosity"] == 0.28
    assert spec["gw_aquifer_thickness_m"] == 18.0
    assert spec["gw_recharge_mm_yr"] == 250.0
    assert spec["gw_storm_aquifer_thickness_m"] == 7.0
    assert spec["gw_storm_mean_depth_mm"] == 22.0
    assert spec["gw_storm_total_days"] == 100.0


# ===========================================================================
# (3) Chart builders.
# ===========================================================================
def test_baseflow_partition_chart_spec():
    from trid3nt_contracts.chart_contracts import (
        is_structurally_valid_vega_lite_spec,
    )
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_baseflow_partition_chart_spec,
    )

    spec = build_baseflow_partition_chart_spec(0.04, 0.02)
    assert is_structurally_valid_vega_lite_spec(spec)
    vals = {v["pathway"]: v["discharge_m3s"] for v in spec["data"]["values"]}
    assert vals["groundwater underflow"] == 0.04
    assert vals["surface seepage"] == 0.02
    assert build_baseflow_partition_chart_spec(0.0, 0.0) is None


def test_baseflow_hydrograph_chart_spec():
    from trid3nt_contracts.chart_contracts import (
        is_structurally_valid_vega_lite_spec,
    )
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_baseflow_hydrograph_chart_spec,
    )

    series = [
        {"time_days": 0.0, "discharge_m3s": 0.01},
        {"time_days": 1.0, "discharge_m3s": 0.05},
        {"time_days": 2.0, "discharge_m3s": 0.03},
    ]
    spec = build_baseflow_hydrograph_chart_spec(series)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert len(spec["data"]["values"]) == 3
    assert build_baseflow_hydrograph_chart_spec([]) is None
    assert build_baseflow_hydrograph_chart_spec([{"time_days": 0.0}]) is None


# ===========================================================================
# (4) COG reproject (postprocess-vs-fixture).
# ===========================================================================
def test_depth_to_water_cog_reprojects_to_4326(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        _reproject_field_cog_4326,
    )

    H, W = 16, 16
    arr = np.linspace(0.0, 12.0, H * W, dtype="float32").reshape(H, W)
    transform = from_origin(280000.0, 3880000.0, 30.0, 30.0)
    cog = tmp_path / "dtw.tif"
    with rasterio.open(
        cog, "w", driver="GTiff", height=H, width=W, count=1, dtype="float32",
        crs="EPSG:32617", transform=transform, nodata=float("nan"),
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
def test_water_table_tool_missing_bbox_returns_typed_error():
    from trid3nt_server.agent.workflows.landlab.groundwater_water_table.groundwater_water_table import (
        landlab_groundwater_water_table,
    )

    out = asyncio.run(landlab_groundwater_water_table(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "LANDLAB_PARAMS_INCOMPLETE"


def test_storm_recession_tool_missing_bbox_returns_typed_error():
    from trid3nt_server.agent.workflows.landlab.groundwater_storm_recession.groundwater_storm_recession import (
        landlab_groundwater_storm_recession,
    )

    out = asyncio.run(landlab_groundwater_storm_recession(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "LANDLAB_PARAMS_INCOMPLETE"


# ===========================================================================
# (6) Registration + categories.
# ===========================================================================
def test_groundwater_templates_registered_as_landlab_templates():
    import trid3nt_server.main as m

    m._import_tools_registry()
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    for name in (
        "landlab_groundwater_water_table",
        "landlab_groundwater_storm_recession",
    ):
        assert name in TOOL_REGISTRY
        md = TOOL_REGISTRY[name].metadata
        assert getattr(md, "engine", None) == "landlab"
        assert getattr(md, "tier", None) == "template"
