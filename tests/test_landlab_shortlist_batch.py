"""ADR 0184 - Landlab shortlist batch: channel_incision + chi_map + storm_sequence.

Covers:
1. build_spec arg-assembly -- ``build_landlab_build_spec`` carries the new
   channel_incision + chi_map knobs.
2. Pure chart builders -- slope-area V&V, chi-elevation, storm-sequence,
   storm-statistics specs build (and degrade to None on empty input).
3. Contract layer classes -- the three new LayerURIs validate their scalars.
4. Worker chains (require landlab) -- channel_incision reproduces the analytical
   slope-area steady state (fitted concavity ~= m/n, K recovered within a
   tolerance, high R^2); chi_map populates chi + ksn over a channel network.
5. storm_sequence generator (requires landlab) -- deterministic Poisson draw +
   climatology statistics.

ASCII only.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_contracts.landlab_contracts import (
    LandlabChannelIncisionLayerURI,
    LandlabChiMapLayerURI,
    LandlabRunArgs,
    LandlabStormSequenceLayerURI,
)


# --------------------------------------------------------------------------- #
# (1) build_spec arg-assembly.
# --------------------------------------------------------------------------- #
def test_build_spec_carries_channel_incision_and_chi_knobs():
    from trid3nt_server.agent.workflows.landlab.run_landlab import (
        build_landlab_build_spec,
    )

    ra = LandlabRunArgs(
        bbox=(-105.3, 39.8, -105.2, 39.85),
        analysis="channel_incision",
        target_resolution_m=90.0,
        k_bedrock=2.0e-5,
        m_sp=0.45,
        n_sp=1.0,
        uplift_rate_m_yr=2.0e-3,
        incision_run_duration_yr=5.0e5,
        incision_n_timesteps=300,
        reference_concavity=0.45,
    )
    spec = build_landlab_build_spec(ra)
    assert spec["analysis"] == "channel_incision"
    assert spec["k_bedrock"] == pytest.approx(2.0e-5)
    assert spec["m_sp"] == pytest.approx(0.45)
    assert spec["uplift_rate_m_yr"] == pytest.approx(2.0e-3)
    assert spec["incision_run_duration_yr"] == pytest.approx(5.0e5)
    assert spec["incision_n_timesteps"] == 300
    assert spec["reference_concavity"] == pytest.approx(0.45)


def test_analysis_synonyms_normalize():
    assert LandlabRunArgs(bbox=(-1, 0, 1, 1), analysis="stream power").analysis == (
        "channel_incision"
    )
    assert LandlabRunArgs(bbox=(-1, 0, 1, 1), analysis="chi-finder").analysis == (
        "chi_map"
    )
    assert LandlabRunArgs(
        bbox=(-1, 0, 1, 1), analysis="stochastic-storm"
    ).analysis == "storm_sequence"


# --------------------------------------------------------------------------- #
# (2) Pure chart builders.
# --------------------------------------------------------------------------- #
def test_slope_area_chart_builds_and_degrades():
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_slope_area_chart_spec,
    )

    assert (
        build_slope_area_chart_spec(
            [], k_input=1e-5, k_recovered=1e-5, uplift_rate_m_yr=1e-3,
            m_sp=0.5, n_sp=1.0, fit_r2=0.9,
        )
        is None
    )
    scatter = [{"area_m2": 1e4 * i, "slope": 0.5 / (i ** 0.5)} for i in range(1, 30)]
    spec = build_slope_area_chart_spec(
        scatter, k_input=1e-5, k_recovered=1.1e-5, uplift_rate_m_yr=1e-3,
        m_sp=0.5, n_sp=1.0, fit_r2=0.98,
    )
    assert spec is not None and "layer" in spec and len(spec["layer"]) == 2


def test_chi_elevation_chart_builds_and_degrades():
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_chi_elevation_chart_spec,
    )

    assert build_chi_elevation_chart_spec([]) is None
    pts = [{"chi": 0.1 * i, "elevation_m": 100.0 + 5.0 * i} for i in range(10)]
    spec = build_chi_elevation_chart_spec(pts)
    assert spec is not None and spec["encoding"]["x"]["field"] == "chi"


def test_storm_chart_builders():
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        build_storm_sequence_chart_spec,
        build_storm_statistics_chart_spec,
    )

    assert build_storm_sequence_chart_spec([]) is None
    assert build_storm_statistics_chart_spec([]) is None
    seq = [
        {"start_day": float(i), "depth_mm": 10.0 + (i % 7)}
        for i in range(30)
    ]
    s1 = build_storm_sequence_chart_spec(seq)
    s2 = build_storm_statistics_chart_spec(seq)
    assert s1 is not None and s1["encoding"]["y"]["field"] == "depth_mm"
    assert s2 is not None and s2["encoding"]["y"]["aggregate"] == "count"


# --------------------------------------------------------------------------- #
# (3) Contract layer classes.
# --------------------------------------------------------------------------- #
def test_contract_layers_validate():
    inc = LandlabChannelIncisionLayerURI(
        layer_id="x", name="n", layer_type="raster", uri="s3://b/k.tif",
        style_preset="continuous_dem", role="primary",fitted_concavity=0.49, analytical_concavity=0.5,
        k_input=1e-5, k_recovered=1.05e-5, uplift_rate_m_yr=1e-3,
        run_duration_yr=1e6, fit_r2=0.98, n_channel_nodes=200,
    )
    assert inc.analytical_concavity == 0.5
    chi = LandlabChiMapLayerURI(
        layer_id="x", name="n", layer_type="raster", uri="s3://b/k.tif",
        style_preset="continuous_dem", role="primary",max_chi=4.0, max_ksn=20.0, mean_ksn=2.0,
        reference_concavity=0.5, n_channel_nodes=150,
    )
    assert chi.max_ksn == 20.0
    ss = LandlabStormSequenceLayerURI(
        layer_id="x", name="n", layer_type="vector", uri="s3://b/m.geojson",
        style_preset="mesh_grid", role="primary", n_storms=100, total_years=5.0,
        total_rainfall_mm=1500.0,
        mean_storm_depth_mm=15.0, mean_storm_intensity_mm_hr=6.0,
        mean_storm_duration_hr=2.0, mean_interstorm_duration_hr=48.0,
        max_storm_depth_mm=60.0,
    )
    assert ss.n_storms == 100


# --------------------------------------------------------------------------- #
# (4) Worker chains (require landlab).
# --------------------------------------------------------------------------- #
def _synthetic_dem(n: int = 45) -> np.ndarray:
    rng = np.random.default_rng(1)
    return rng.random((n, n)) * 2.0 + np.linspace(0.0, 3.0, n)[None, :]


def test_channel_incision_reproduces_analytical_slope_area():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    res = 90.0
    r = run_component_chain(
        _synthetic_dem(),
        resolution_m=res,
        build_spec={
            "analysis": "channel_incision",
            "target_resolution_m": res,
            "k_bedrock": 1.0e-5,
            "m_sp": 0.5,
            "n_sp": 1.0,
            "uplift_rate_m_yr": 1.0e-3,
            "incision_run_duration_yr": 1.0e6,
            "incision_n_timesteps": 500,
            "channel_threshold_cells": 30,
        },
    )
    e = r.extra
    assert r.analysis == "channel_incision"
    # Fitted concavity recovers the analytical m/n = 0.5 to within 0.06.
    assert e["analytical_concavity"] == pytest.approx(0.5)
    assert abs(e["fitted_concavity"] - 0.5) < 0.06
    # K recovered within a factor of ~1.5 of the input (steady-state intercept).
    assert 0.66e-5 < e["k_recovered"] < 1.5e-5
    assert e["fit_r2"] > 0.9
    assert e["n_channel_nodes"] > 20
    assert len(e["scatter"]) > 10
    # Primary field is finite evolved elevation; ksn secondary present.
    assert np.isfinite(r.field).all()
    assert "channel_steepness" in r.secondary_fields


def test_chi_map_populates_chi_and_ksn():
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    res = 90.0
    r = run_component_chain(
        _synthetic_dem(),
        resolution_m=res,
        build_spec={
            "analysis": "chi_map",
            "target_resolution_m": res,
            "reference_concavity": 0.5,
            "channel_threshold_cells": 30,
        },
    )
    e = r.extra
    assert r.analysis == "chi_map"
    assert e["max_chi"] > 0.0
    assert e["max_ksn"] > 0.0
    assert e["mean_ksn"] > 0.0
    assert e["reference_concavity"] == pytest.approx(0.5)
    assert e["n_channel_nodes"] > 10
    assert len(e["scatter"]) > 5
    # chi field is masked to the channel network (a minority of cells finite).
    finite_frac = float(np.isfinite(r.field).mean())
    assert 0.0 < finite_frac < 0.6
    assert "channel_network" in r.secondary_fields
    assert "channel_steepness" in r.secondary_fields


def test_channel_incision_higher_uplift_steepens_channels():
    """Physical monotonicity: at steady state S ~ (U/K)^(1/n), so a higher uplift
    rate yields steeper channels (larger mean ksn)."""
    pytest.importorskip("landlab")
    from workers.landlab.component_chain import run_component_chain

    res = 90.0
    dem = _synthetic_dem()

    def _mean_ksn(U: float) -> float:
        r = run_component_chain(
            dem,
            resolution_m=res,
            build_spec={
                "analysis": "channel_incision", "target_resolution_m": res,
                "k_bedrock": 1.0e-5, "m_sp": 0.5, "n_sp": 1.0,
                "uplift_rate_m_yr": U, "incision_run_duration_yr": 1.0e6,
                "incision_n_timesteps": 500, "channel_threshold_cells": 30,
                "reference_concavity": 0.5,
            },
        )
        ksn = r.secondary_fields["channel_steepness"]
        return float(np.nanmean(ksn))

    assert _mean_ksn(2.0e-3) > _mean_ksn(1.0e-3)


# --------------------------------------------------------------------------- #
# (5) storm_sequence generator (requires landlab).
# --------------------------------------------------------------------------- #
def test_storm_sequence_generator_deterministic_and_statistics():
    pytest.importorskip("landlab")
    from trid3nt_server.agent.workflows.landlab.storm_sequence.storm_sequence import (
        generate_storm_sequence,
    )

    kw = dict(
        mean_storm_duration_hr=2.0,
        mean_interstorm_duration_hr=48.0,
        mean_storm_depth_mm=15.0,
        total_years=3.0,
        random_seed=7,
    )
    seq1, stats1 = generate_storm_sequence(**kw)
    seq2, stats2 = generate_storm_sequence(**kw)
    # Deterministic (fixed seed).
    assert stats1 == stats2
    assert stats1["n_storms"] > 100
    assert stats1["total_rainfall_mm"] > 0.0
    # Mean depth is near the generator mean (Poisson exponential ~15 mm).
    assert 8.0 < stats1["mean_storm_depth_mm"] < 25.0
    assert stats1["max_storm_depth_mm"] >= stats1["mean_storm_depth_mm"]
    # Sequence carries monotone non-decreasing start times.
    days = [s["start_day"] for s in seq1]
    assert days == sorted(days)
