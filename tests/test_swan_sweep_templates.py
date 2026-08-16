"""Offline MODULE tests for the SWAN multi-run templates (physics sensitivity
sweep + stationary snapshot batch), with the DEM fetch + per-run solve MOCKED.

These pin the agent-side pure logic the templates own -- axis/value validation,
snapshot resolution, and the chart specs -- plus the async orchestration shape
(schemes collected + chart built) without touching the DEM fetcher, docker, or S3.

ASCII only.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trid3nt_server.agent.workflows.swan._sweep_common import PHYSICS_AXES, SwanSweepError
from trid3nt_server.agent.workflows.swan.physics_sensitivity_sweep.physics_sensitivity_sweep import (
    build_sweep_chart_spec,
    resolve_axis_values,
    swan_physics_sensitivity_sweep,
)
from trid3nt_server.agent.workflows.swan.stationary_snapshot_batch.stationary_snapshot_batch import (
    build_snapshot_chart_spec,
    resolve_snapshots,
    swan_stationary_snapshot_batch,
)

_AOI = (-118.05, 33.60, -117.95, 33.70)  # Huntington Beach-ish coastal box


def _fake_layer(i: int) -> SimpleNamespace:
    """A WaveFieldLayerURI stand-in with the scalars the templates read."""
    return SimpleNamespace(
        max_hs_m=3.0 + 0.1 * i,
        mean_hs_m=2.6 - 0.2 * i,
        wave_area_km2=5.0 - 0.3 * i,
        mean_tp_s=9.0,
        mean_dir_deg=270.0,
        layer_id=f"swan-peak-{i}",
        uri=f"s3://trid3nt-runs/run-{i}/swan_wave_height_peak.tif",
    )


# ===========================================================================
# resolve_axis_values
# ===========================================================================
def test_resolve_axis_values_defaults_and_dedup():
    for axis, spec in PHYSICS_AXES.items():
        vals = resolve_axis_values(axis, None)
        assert vals == list(spec["defaults"])
        assert len(vals) >= 2
    # user values are coerced + de-duplicated, order preserved.
    assert resolve_axis_values("breaking_gamma", [0.5, 0.5, 0.9]) == [0.5, 0.9]
    assert resolve_axis_values("gen_formulation", ["KOMEN", "westhuysen"]) == [
        "komen", "westhuysen"]


def test_resolve_axis_values_rejects_bad_axis_and_thin_sweep():
    with pytest.raises(SwanSweepError):
        resolve_axis_values("nonsense", None)
    with pytest.raises(SwanSweepError):
        resolve_axis_values("breaking_gamma", [0.7])  # < 2 distinct
    with pytest.raises(SwanSweepError):
        resolve_axis_values("friction_cfjon", ["not-a-number", "x"])


def test_build_sweep_chart_spec_normalizes_to_baseline():
    results = [
        {"scheme": "0.55", "max_hs_m": 3.0, "mean_hs_m": 2.6, "wave_area_km2": 5.0},
        {"scheme": "0.9", "max_hs_m": 3.0, "mean_hs_m": 2.4, "wave_area_km2": 4.0},
    ]
    spec = build_sweep_chart_spec("breaking_gamma", results)
    vals = spec["data"]["values"]
    # baseline scheme both metrics == 1.0; two metrics per scheme (color series).
    base = [v for v in vals if v["scheme"] == "0.55"]
    assert all(abs(v["value"] - 1.0) < 1e-9 for v in base)
    assert {v["metric"] for v in vals} == {"mean Hs (rel.)", "peak Hs (rel.)"}
    # the dissipation-sensitive mean-Hs series moves; the boundary-pinned peak holds.
    mean_09 = [v for v in vals if v["scheme"] == "0.9" and v["metric"] == "mean Hs (rel.)"][0]
    peak_09 = [v for v in vals if v["scheme"] == "0.9" and v["metric"] == "peak Hs (rel.)"][0]
    assert mean_09["value"] < 1.0 and abs(peak_09["value"] - 1.0) < 1e-9
    assert spec["encoding"]["color"]["field"] == "metric"


# ===========================================================================
# resolve_snapshots
# ===========================================================================
def test_resolve_snapshots_hs_sequence_and_defaults():
    snaps = resolve_snapshots(
        [2.0, 4.0, 2.0], None,
        default_tp_s=8.0, default_dir_deg=270.0, default_spread_deg=25.0,
        default_side="W")
    assert [s["hs_m"] for s in snaps] == [2.0, 4.0, 2.0]
    assert all(s["tp_s"] == 8.0 and s["side"] == "W" for s in snaps)
    assert [s["label"] for s in snaps] == ["t0", "t1", "t2"]


def test_resolve_snapshots_explicit_wins_and_validates():
    snaps = resolve_snapshots(
        None,
        [{"hs_m": 3.0, "tp_s": 10.0, "label": "peak"}, {"hs_m": 1.5}],
        default_tp_s=8.0, default_dir_deg=None, default_spread_deg=None,
        default_side="S")
    assert snaps[0]["tp_s"] == 10.0 and snaps[0]["label"] == "peak"
    assert snaps[1]["tp_s"] == 8.0  # filled from default
    with pytest.raises(SwanSweepError):
        resolve_snapshots([2.0], None, default_tp_s=None, default_dir_deg=None,
                          default_spread_deg=None, default_side=None)
    with pytest.raises(SwanSweepError):
        resolve_snapshots(None, [{"hs_m": -1.0}, {"hs_m": 2.0}], default_tp_s=None,
                          default_dir_deg=None, default_spread_deg=None,
                          default_side=None)


def test_build_snapshot_chart_spec_two_series():
    results = [
        {"label": "t0", "max_hs_m": 1.5, "wave_area_km2": 2.0},
        {"label": "t1", "max_hs_m": 3.0, "wave_area_km2": 4.0},
    ]
    spec = build_snapshot_chart_spec(results)
    assert {v["metric"] for v in spec["data"]["values"]} == {
        "peak Hs (m)", "wave footprint (km2)"}
    assert spec["encoding"]["color"]["field"] == "metric"


# ===========================================================================
# Async orchestration (DEM fetch + solve mocked).
# ===========================================================================
_SWEEP_MOD = "trid3nt_server.agent.workflows.swan.physics_sensitivity_sweep.physics_sensitivity_sweep"
_BATCH_MOD = "trid3nt_server.agent.workflows.swan.stationary_snapshot_batch.stationary_snapshot_batch"


@pytest.mark.asyncio
async def test_sweep_orchestration_collects_schemes():
    calls = {"n": 0}

    async def _fake_solve(**kwargs):
        i = calls["n"]; calls["n"] += 1
        assert "breaking_gamma" in kwargs["overrides"]
        return _fake_layer(i)

    with patch(f"{_SWEEP_MOD}.fetch_swan_dem_once", return_value="s3://dem.tif"), \
         patch(f"{_SWEEP_MOD}.run_stationary_solve", _fake_solve):
        out = await swan_physics_sensitivity_sweep(bbox=_AOI, axis="breaking_gamma")
    assert out["status"] == "ok"
    assert out["axis"] == "breaking_gamma" and out["wind_dependent"] is False
    assert len(out["schemes"]) == 3
    assert out["schemes"][0]["scheme"] == "0.55"


@pytest.mark.asyncio
async def test_sweep_bad_axis_returns_typed_error():
    out = await swan_physics_sensitivity_sweep(bbox=_AOI, axis="bogus")
    assert out["status"] == "error" and out["error_code"] == "SWAN_SWEEP_AXIS_INVALID"


@pytest.mark.asyncio
async def test_sweep_missing_bbox_returns_typed_error():
    out = await swan_physics_sensitivity_sweep(bbox=None)
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_snapshot_batch_orchestration_collects_snapshots():
    calls = {"n": 0}

    async def _fake_solve(**kwargs):
        i = calls["n"]; calls["n"] += 1
        assert kwargs["overrides"] == {}
        assert "hs_m" in kwargs["boundary_kwargs"]
        return _fake_layer(i)

    with patch(f"{_BATCH_MOD}.fetch_swan_dem_once", return_value="s3://dem.tif"), \
         patch(f"{_BATCH_MOD}.run_stationary_solve", _fake_solve):
        out = await swan_stationary_snapshot_batch(
            bbox=_AOI, hs_sequence=[2.0, 4.0], boundary_side="W")
    assert out["status"] == "ok"
    assert len(out["snapshots"]) == 2
    assert out["snapshots"][0]["hs_boundary_m"] == 2.0
