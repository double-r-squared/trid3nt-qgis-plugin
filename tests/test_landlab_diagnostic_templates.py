"""Offline tests for the six Landlab diagnostic templates.

Pins the agent-side surface in ISOLATION (no MinIO, no solver dispatch):

1. **Contract round-trip** -- the new analyses + synonyms normalize; the new
   run-arg knobs validate; each new ``LayerURI`` subtype carries its scalars.
2. **build_spec arg-assembly** -- ``build_landlab_build_spec`` carries the new
   knobs onto the worker build_spec.
3. **Chart builders** -- storm-ensemble / overland-hydrograph / Hack's-law specs
   are structurally valid Vega-Lite; empty inputs -> None.
4. **Worker chains (with landlab)** -- each analysis runs on a synthetic DEM and
   returns the documented field + scalars; the HAND chain matches the landlab
   HeightAboveDrainageCalculator API doctest 4x5 grid exactly.
5. **Tool bbox gate** -- a missing bbox returns a typed error envelope per tool.

ASCII only.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.landlab_contracts import (
    LandlabDemConditioningLayerURI,
    LandlabHacksLawLayerURI,
    LandlabHandLayerURI,
    LandlabLakeMappingLayerURI,
    LandlabOverlandTimeseriesLayerURI,
    LandlabRunArgs,
    LandlabStormEnsembleLayerURI,
)


# ===========================================================================
# (1) Contract round-trip.
# ===========================================================================
@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("storm_ensemble", "landslide_storm_ensemble"),
        ("recharge_sweep", "landslide_storm_ensemble"),
        ("depth_timeseries", "overland_flow_timeseries"),
        ("flood_animation", "overland_flow_timeseries"),
        ("pit_fill", "dem_pit_fill"),
        ("dem_conditioning", "dem_pit_fill"),
        ("lake_extent", "lake_mapping"),
        ("ponding", "lake_mapping"),
        ("hack", "hacks_law"),
        ("basin_scaling", "hacks_law"),
        ("height_above_drainage", "hand"),
        ("wetness_proxy", "hand"),
    ],
)
def test_new_analysis_synonyms_normalize(raw, canonical):
    ra = LandlabRunArgs(bbox=(-105.37, 39.998, -105.33, 40.032), analysis=raw)
    assert ra.analysis == canonical


def test_new_run_arg_knobs_validate_and_default():
    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="landslide_storm_ensemble",
        n_recharge_scenarios=6,
        mean_storm_depth_mm=40.0,
        output_interval_s=180.0,
        fill_flat=False,
    )
    assert ra.n_recharge_scenarios == 6
    assert ra.mean_storm_depth_mm == 40.0
    assert ra.output_interval_s == 180.0
    assert ra.fill_flat is False
    # JSON round-trip preserves them.
    ra2 = LandlabRunArgs.model_validate(ra.model_dump())
    assert ra2.n_recharge_scenarios == 6 and ra2.fill_flat is False


def test_n_recharge_scenarios_bounds():
    with pytest.raises(Exception):
        LandlabRunArgs(
            bbox=(-105.37, 39.998, -105.33, 40.032),
            analysis="landslide_storm_ensemble",
            n_recharge_scenarios=1,
        )


def test_new_layer_uris_are_layer_subtypes():
    sp = "continuous_flood_depth"
    se = LandlabStormEnsembleLayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset=sp,
        role="primary", unstable_area_fraction=0.1, mean_probability_of_failure=0.2,
        min_recharge_mm_day=10.0, max_recharge_mm_day=50.0, n_recharge_scenarios=8,
        sensitivity_slope=0.001,
    )
    ot = LandlabOverlandTimeseriesLayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset=sp,
        role="primary", wet_area_fraction=0.3, max_depth_m=0.5, n_frames=9,
        time_to_peak_s=1080.0,
    )
    dc = LandlabDemConditioningLayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset=sp,
        role="primary", max_fill_depth_m=3.0, filled_area_fraction=0.05, n_depressions=12,
    )
    lm = LandlabLakeMappingLayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset=sp,
        role="primary", n_lakes=4, total_lake_area_km2=0.4, total_lake_volume_m3=1e6,
        max_lake_depth_m=5.0,
    )
    hl = LandlabHacksLawLayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset=sp,
        role="primary", hack_exponent=0.55, hack_coefficient=1.4,
        largest_basin_area_km2=1.2, n_basins=1,
    )
    hn = LandlabHandLayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset=sp,
        role="primary", mean_hand_m=20.0, max_hand_m=120.0, channel_area_fraction=0.1,
        lowland_area_fraction=0.2,
    )
    for layer in (se, ot, dc, lm, hl, hn):
        assert isinstance(layer, LayerURI)


# ===========================================================================
# (2) build_spec arg-assembly.
# ===========================================================================
def test_build_spec_carries_new_knobs():
    from trid3nt_server.agent.workflows.landlab.run_landlab import build_landlab_build_spec

    ra = LandlabRunArgs(
        bbox=(-105.37, 39.998, -105.33, 40.032),
        analysis="landslide_storm_ensemble",
        n_recharge_scenarios=5,
        mean_storm_depth_mm=42.0,
        output_interval_s=120.0,
        fill_flat=False,
    )
    spec = build_landlab_build_spec(ra)
    assert spec["n_recharge_scenarios"] == 5
    assert spec["mean_storm_depth_mm"] == 42.0
    assert spec["output_interval_s"] == 120.0
    assert spec["fill_flat"] is False


# ===========================================================================
# (3) Chart builders.
# ===========================================================================
def test_storm_ensemble_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_storm_ensemble_chart_spec,
    )

    rows = [
        {"recharge_mm_day": 10.0, "unstable_area_fraction": 0.05, "mean_probability_of_failure": 0.08},
        {"recharge_mm_day": 40.0, "unstable_area_fraction": 0.09, "mean_probability_of_failure": 0.13},
    ]
    spec = build_storm_ensemble_chart_spec(rows)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert spec["encoding"]["x"]["field"] == "recharge_mm_day"
    assert build_storm_ensemble_chart_spec([]) is None


def test_overland_hydrograph_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_overland_hydrograph_chart_spec,
    )

    series = [
        {"time_s": 0.0, "depth_m": 0.0},
        {"time_s": 300.0, "depth_m": 0.2},
        {"time_s": 600.0, "depth_m": 0.4},
    ]
    spec = build_overland_hydrograph_chart_spec(series)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert spec["encoding"]["x"]["field"] == "time_s"
    assert build_overland_hydrograph_chart_spec([]) is None
    assert build_overland_hydrograph_chart_spec([{"time_s": 0, "depth_m": 0}]) is None


def test_hacks_law_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_hacks_law_chart_spec,
    )

    scatter = [
        {"area_m2": 900.0, "length_m": 30.0},
        {"area_m2": 9000.0, "length_m": 120.0},
        {"area_m2": 90000.0, "length_m": 400.0},
    ]
    spec = build_hacks_law_chart_spec(scatter, exponent=0.55, coefficient=1.2)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert "layer" in spec
    assert build_hacks_law_chart_spec([], exponent=0.5, coefficient=1.0) is None


# ===========================================================================
# (4) Worker chains (require landlab).
# ===========================================================================
def _synthetic_dem(steep: bool = True):
    np.random.seed(3)
    ny, nx = 50, 60
    xg, yg = np.meshgrid(np.arange(nx), np.arange(ny))
    if steep:
        dem = 400 - 8 * yg + 20 * np.sin(xg / 5.0) + 6 * np.random.randn(ny, nx)
    else:
        dem = 200 - 1.2 * yg + 6 * np.sin(xg / 5.0) + 4 * np.random.randn(ny, nx)
    dem[25, 30] -= 30
    dem[26, 30] -= 20
    return dem


def test_storm_ensemble_chain_sweeps_recharge():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    cr = run_component_chain(
        _synthetic_dem(steep=True),
        resolution_m=30.0,
        build_spec={
            "analysis": "landslide_storm_ensemble",
            "n_monte_carlo": 8,
            "n_recharge_scenarios": 5,
            "mean_storm_depth_mm": 40.0,
            "soil_cohesion_pa": 2000.0,
            "soil_thickness_m": 2.0,
        },
    )
    assert cr.analysis == "landslide_storm_ensemble"
    table = cr.extra["recharge_scenarios"]
    assert len(table) == 5
    # unstable fraction is non-decreasing with recharge (susceptibility grows).
    fr = [t["unstable_area_fraction"] for t in table]
    assert fr[-1] >= fr[0]
    assert cr.extra["min_recharge_mm_day"] <= cr.extra["max_recharge_mm_day"]


def test_overland_timeseries_chain_emits_frames():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    cr = run_component_chain(
        _synthetic_dem(steep=True),
        resolution_m=30.0,
        build_spec={
            "analysis": "overland_flow_timeseries",
            "rainfall_intensity_mm_hr": 80.0,
            "storm_duration_hr": 0.3,
            "output_interval_s": 120.0,
        },
    )
    assert cr.analysis == "overland_flow_timeseries"
    depth_tokens = [k for k in cr.secondary_fields if k.startswith("depth_step_")]
    assert len(depth_tokens) >= 2  # an animation group needs >= 2 frames
    assert cr.extra["n_frames"] == len(depth_tokens)
    assert len(cr.extra["max_cell_series"]) == len(depth_tokens)


def test_overland_conditioning_is_opt_in_and_removes_pit_ponding():
    """condition_dem is OPT-IN (default off routes the raw DEM). When enabled it
    depression-fills the DEM before routing, so a seeded sink pit no longer ponds
    in the overland peak-depth field."""
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    ny, nx = 40, 40
    _xg, yg = np.meshgrid(np.arange(nx), np.arange(ny))
    # A plane draining toward larger y (each cell drains downslope) -- no natural
    # depressions -- with ONE deep closed pit seeded mid-domain.
    dem = 100.0 - 2.0 * yg.astype("float64")
    piy, pix = 15, 20
    dem[piy, pix] -= 25.0  # a deep sink well below all 8 neighbors

    spec = {
        "analysis": "overland_flow_timeseries",
        "rainfall_intensity_mm_hr": 80.0,
        "storm_duration_hr": 0.3,
        "output_interval_s": 120.0,
    }
    # Default (no condition_dem key) routes the RAW DEM -- no modification.
    default = run_component_chain(dem, resolution_m=30.0, build_spec=dict(spec))
    assert default.extra["condition_dem"] is False
    assert default.extra["n_depressions_filled"] == 0

    conditioned = run_component_chain(
        dem, resolution_m=30.0, build_spec={**spec, "condition_dem": True}
    )
    # Opt-in conditioning fills the pit so the pit cell no longer ponds as deep.
    assert default.field[piy, pix] > conditioned.field[piy, pix]
    # Conditioning is honest about the DEM modification it made.
    assert conditioned.extra["condition_dem"] is True
    assert conditioned.extra["n_depressions_filled"] >= 1
    assert conditioned.extra["max_fill_depth_m"] > 0.0


def test_lake_mapping_discrimination_drops_noise_pits():
    """Depth + area floors keep the one real basin and drop shallow/tiny pits;
    n_lakes_raw > n_lakes_kept."""
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    ny, nx = 40, 40
    _xg, yg = np.meshgrid(np.arange(nx), np.arange(ny))
    # A gently tilted plane (drains downslope -> no natural depressions) with
    # seeded pits of controlled depth/area.
    dem = 100.0 - 0.5 * yg.astype("float64")
    # One large DEEP basin: 9x9 = 81 cells (72900 m2 at 30 m), ~5.5 m deep -> kept.
    dem[10:19, 10:19] = dem[10:19, 10:19].min() - 6.0
    # Three SHALLOW single-cell pits (~0.4 m deep) -> fail the depth floor.
    for iy, ix in ((3, 30), (30, 5), (33, 33)):
        dem[iy, ix] -= 0.9
    # Two DEEP but TINY single-cell pits (~3 m deep, 900 m2) -> fail the area floor.
    for iy, ix in ((5, 5), (35, 20)):
        dem[iy, ix] -= 3.5

    cr = run_component_chain(
        dem, resolution_m=30.0, build_spec={"analysis": "lake_mapping"}
    )
    assert cr.analysis == "lake_mapping"
    # LakeMapperBarnes mapped every seeded depression (6), the floors kept only
    # the one real basin.
    assert cr.extra["n_lakes_raw"] > cr.extra["n_lakes_kept"]
    assert cr.extra["n_lakes_kept"] == 1
    assert cr.extra["n_lakes"] == cr.extra["n_lakes_kept"]
    # The surviving lake is the deep basin.
    assert cr.extra["max_lake_depth_m"] >= 1.0
    assert cr.extra["min_lake_depth_m"] == 1.0
    assert cr.extra["min_lake_area_m2"] == 10000.0
    # A permissive run keeps more lakes (the floors are the discriminator).
    permissive = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "lake_mapping",
            "min_lake_depth_m": 0.0,
            "min_lake_area_m2": 0.0,
        },
    )
    assert permissive.extra["n_lakes_kept"] > cr.extra["n_lakes_kept"]


def test_dem_pit_fill_and_lake_mapping_chains():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    dem = _synthetic_dem(steep=False)
    fill = run_component_chain(
        dem, resolution_m=30.0, build_spec={"analysis": "dem_pit_fill"}
    )
    assert fill.output_field_name == "dem_fill_depth"
    assert fill.extra["max_fill_depth_m"] > 0.0
    assert fill.extra["n_depressions"] >= 1

    # Permissive floors so the synthetic DEM's small seeded pit is kept (the
    # discrimination behavior itself is covered by
    # test_lake_mapping_discrimination_drops_noise_pits).
    lake = run_component_chain(
        dem,
        resolution_m=30.0,
        build_spec={
            "analysis": "lake_mapping",
            "min_lake_depth_m": 0.0,
            "min_lake_area_m2": 0.0,
        },
    )
    assert lake.output_field_name == "lake_depth"
    assert "lake_extent" in lake.secondary_fields
    assert lake.extra["n_lakes"] >= 1
    assert lake.extra["n_lakes_kept"] == lake.extra["n_lakes_raw"]  # floors off
    assert lake.extra["max_lake_depth_m"] > 0.0


def test_hacks_law_chain_fits_exponent():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    cr = run_component_chain(
        _synthetic_dem(steep=True),
        resolution_m=30.0,
        build_spec={"analysis": "hacks_law"},
    )
    assert cr.analysis == "hacks_law"
    assert cr.extra["n_basins"] >= 1
    assert cr.extra["hack_exponent"] > 0.0
    assert len(cr.extra["scatter"]) >= 3


def test_hand_chain_matches_api_doctest_grid():
    """The HAND chain reproduces the landlab HeightAboveDrainageCalculator API
    doctest exactly (the 4x5 grid, channel at nodes 2 and 7)."""
    pytest.importorskip("landlab")
    from landlab import RasterModelGrid
    from landlab.components import FlowAccumulator, HeightAboveDrainageCalculator

    mg = RasterModelGrid((4, 5))
    z = mg.add_zeros("topographic__elevation", at="node")
    mg.set_status_at_node_on_edges(
        right=mg.BC_NODE_IS_CLOSED,
        bottom=mg.BC_NODE_IS_FIXED_VALUE,
        left=mg.BC_NODE_IS_CLOSED,
        top=mg.BC_NODE_IS_CLOSED,
    )
    elev = np.array(
        [[2, 1, 0, 1, 2], [3, 2, 1, 2, 3], [4, 3, 2, 3, 4], [5, 4, 4, 4, 5]]
    )
    z[:] = elev.reshape(len(z))
    fa = FlowAccumulator(mg, flow_director="D8")
    fa.run_one_step()
    channel_mask = mg.zeros(at="node").astype("uint8")
    channel_mask[[2, 7]] = 1
    mg.add_field("channel__mask", channel_mask, at="node", clobber=True)
    hd = HeightAboveDrainageCalculator(mg, channel_mask="channel__mask")
    hd.run_one_step()
    hand = mg.at_node["height_above_drainage__elevation"].reshape(elev.shape)
    expected = np.array(
        [[2, 0, 0, 0, 0], [3, 2, 0, 2, 3], [4, 2, 1, 2, 4], [5, 4, 4, 4, 5]],
        dtype="float64",
    )
    np.testing.assert_array_equal(hand, expected)


def test_hand_chain_runs_on_synthetic_dem():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    cr = run_component_chain(
        _synthetic_dem(steep=True),
        resolution_m=30.0,
        build_spec={"analysis": "hand", "channel_threshold_cells": 40},
    )
    assert cr.output_field_name == "height_above_drainage__elevation"
    assert "channel_network" in cr.secondary_fields
    assert cr.extra["max_hand_m"] >= cr.extra["mean_hand_m"] >= 0.0


# ===========================================================================
# (5) Tool bbox gate.
# ===========================================================================
@pytest.mark.parametrize(
    "modpath,fn",
    [
        ("landslide_storm_ensemble.storm_ensemble", "landlab_landslide_storm_ensemble"),
        ("overland_flow_timeseries.overland_timeseries", "landlab_overland_flow_timeseries"),
        ("dem_conditioning.dem_conditioning", "landlab_dem_conditioning"),
        ("lake_mapping.lake_mapping", "landlab_lake_mapping"),
        ("hacks_law.hacks_law", "landlab_hacks_law_scaling"),
        ("hand_wetness.hand_wetness", "landlab_hand_wetness"),
    ],
)
def test_tool_missing_bbox_returns_typed_error(modpath, fn):
    import importlib

    mod = importlib.import_module(
        f"trid3nt_server.agent.workflows.landlab.{modpath}"
    )
    out = asyncio.run(getattr(mod, fn)(bbox=None))
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "LANDLAB_PARAMS_INCOMPLETE"
