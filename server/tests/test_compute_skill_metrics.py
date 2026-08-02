"""Unit tests for ``compute_skill_metrics`` (no network).

All fixtures are hand-built numeric series (a few also stage a synthesized
FlatGeobuf paired table, mirroring the lane-C ``extract_model_at_observations``
storage format) so every expected metric is independently hand-computed.

Coverage:
1.  ``test_registered`` -- TOOL_REGISTRY entry, cacheable=False /
    live-no-cache, open_world_hint=False (pure compute, no external API).
2.  ``test_identical_series_perfect_scores`` -- identical non-constant
    series -> NSE=1.0, KGE=1.0, PBIAS=0.0, R2=1.0, RSR=0.0, RMSE=0.0.
3.  ``test_constant_offset_hand_computed`` -- observed=[10..50],
    simulated=observed+5 -> hand-computed NSE/KGE/PBIAS/RSR/RMSE/R2 +
    suggested_verdict="satisfactory" + peak_error=10.0.
4.  ``test_peak_timing_error`` -- distinct peak indices + explicit ISO8601
    times -> exact peak_error / peak_timing_error (seconds, signed).
5.  ``test_variable_head_adds_srms`` -- SRMS populated (hand-computed) only
    for variable="head"; null for variable="generic" on the same data.
6.  ``test_constant_observed_metrics_null`` -- zero-variance observed series
    -> NSE/RSR/KGE/R2 all null (never -inf/inf/nan), each with a caveat.
7.  ``test_small_n_indeterminate`` -- n=3 -> suggested_verdict="indeterminate"
    with a caveat, metric values still populated.
8.  ``test_paired_table_uri_single_station`` -- FlatGeobuf paired table,
    single obs_id -> no pooling caveat; metrics match the direct-array path.
9.  ``test_paired_table_uri_multi_station_pools_with_caveat`` -- 2 distinct
    obs_id groups -> pooling caveat present.
10. ``test_no_selector_raises`` -- neither paired_table_uri nor arrays.
11. ``test_mismatched_length_raises``.
12. ``test_all_nonfinite_raises`` -- SkillMetricsNoDataError.
13. ``test_units_passthrough``.
14. ``test_kge_band_always_null_with_caveat``.
15. ``test_dependency_missing_raises`` -- spotpy import failure -> typed
    SkillMetricsDependencyMissingError (sys.modules poisoning trick).
"""

from __future__ import annotations

import math
import sys

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.tools.processing.compute_skill_metrics.compute_skill_metrics import (
    SkillMetricsDependencyMissingError,
    SkillMetricsInputError,
    SkillMetricsNoDataError,
    compute_skill_metrics,
)


def _write_paired_table(path: str, records: list[dict]) -> str:
    """Write a lane-C-shaped paired FlatGeobuf (obs_id/observed/simulated/time)."""
    rows = []
    for i, rec in enumerate(records):
        rec = dict(rec)
        rows.append(
            {
                "obs_id": rec.get("obs_id", f"STN{i}"),
                "observed": rec["observed"],
                "simulated": rec["simulated"],
                "time": rec.get("time"),
                "geometry": Point(-81.0 + 0.01 * i, 27.0),
            }
        )
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")
    return path


def test_registered() -> None:
    entry = TOOL_REGISTRY["compute_skill_metrics"]
    assert entry.fn is compute_skill_metrics
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.open_world_hint is False


def test_identical_series_perfect_scores() -> None:
    series = [10.0, 12.0, 9.0, 15.0, 11.0]
    result = compute_skill_metrics(observed=list(series), simulated=list(series))
    m = result["metrics"]
    assert result["n"] == 5
    assert m["NSE"] == pytest.approx(1.0)
    assert m["KGE"] == pytest.approx(1.0)
    assert m["PBIAS"] == pytest.approx(0.0)
    assert m["R2"] == pytest.approx(1.0)
    assert m["RSR"] == pytest.approx(0.0)
    assert m["RMSE"] == pytest.approx(0.0)
    assert result["verdict_is_heuristic"] is True
    assert result["suggested_verdict"] == "very_good"


def test_constant_offset_hand_computed() -> None:
    observed = [10.0, 20.0, 30.0, 40.0, 50.0]
    simulated = [v + 5.0 for v in observed]
    result = compute_skill_metrics(observed=observed, simulated=simulated)
    m = result["metrics"]

    # Hand-computed (see module docstring derivation):
    #   PBIAS = 100 * sum(sim-obs) / sum(obs) = 100*25/150 = 16.666667
    #   NSE   = 1 - 125/1000 = 0.875
    #   RMSE  = sqrt(mean(25)) = 5.0
    #   RSR   = RMSE / std(obs, ddof=0) = 5 / sqrt(200) = 0.353553
    #   KGE   = 1 - sqrt((1.166667-1)^2) = 0.833333  (cc=1, alpha=1)
    #   R2    = cc^2 = 1.0
    assert m["PBIAS"] == pytest.approx(16.666667, abs=1e-5)
    assert m["NSE"] == pytest.approx(0.875, abs=1e-6)
    assert m["RMSE"] == pytest.approx(5.0, abs=1e-6)
    assert m["RSR"] == pytest.approx(5.0 / math.sqrt(200.0), abs=1e-6)
    assert m["KGE"] == pytest.approx(0.833333, abs=1e-5)
    assert m["R2"] == pytest.approx(1.0, abs=1e-6)

    # Peak: obs peak=50 (idx4), sim peak=55 (idx4) -> +10% peak error.
    assert m["peak_error"] == pytest.approx(10.0)
    assert m["peak_timing_error"] is None  # no time array supplied
    assert m["SRMS"] is None  # variable defaults to "generic"

    # Moriasi combined grading: NSE>0.75 & RSR<=0.50 but |PBIAS|=16.67 > 10
    # -> fails very_good; also > 15 -> fails good; satisfies satisfactory.
    assert result["suggested_verdict"] == "satisfactory"
    assert result["n"] == 5

    # FIX 3: PBIAS sign convention is documented as a one-line envelope note.
    assert any("PBIAS sign convention" in n for n in result["notes"])
    assert any("over-predicts" in n.lower() for n in result["notes"])


def test_peak_timing_error() -> None:
    observed = [1.0, 2.0, 10.0, 3.0, 1.0]
    simulated = [1.0, 9.0, 2.0, 3.0, 1.0]
    time = [
        "2026-01-01T00:00:00Z",
        "2026-01-01T01:00:00Z",
        "2026-01-01T02:00:00Z",
        "2026-01-01T03:00:00Z",
        "2026-01-01T04:00:00Z",
    ]
    result = compute_skill_metrics(observed=observed, simulated=simulated, time=time)
    m = result["metrics"]
    # obs peak = 10 at idx2 (02:00); sim peak = 9 at idx1 (01:00).
    assert m["peak_error"] == pytest.approx(-10.0)
    assert m["peak_timing_error"] == pytest.approx(-3600.0)


def test_static_pairing_peak_timing_null(tmp_path) -> None:
    # A STATIC spatial scatter: one row per DISTINCT obs_id (like a max-flood
    # raster sampled at surveyed HWMs), with per-point survey dates in `time`.
    # There is no shared model time axis, so peak_timing_error MUST be null --
    # never the fabricated -86400 s sentinel the live Harvey run emitted.
    records = [
        {"obs_id": f"STN{i}", "observed": o, "simulated": s, "time": t}
        for i, (o, s, t) in enumerate(
            [
                (15.0, 5.0, "2017-08-27"),
                (18.0, 6.0, "2017-08-28"),
                (12.0, 4.0, "2017-08-27"),
                (20.0, 7.0, "2017-08-26"),  # observed peak (idx3), sim peak (idx3)
                (14.0, 5.5, "2017-08-28"),
            ]
        )
    ]
    path = str(tmp_path / "static.fgb")
    _write_paired_table(path, records)

    result = compute_skill_metrics(paired_table_uri=path)
    assert result["metrics"]["peak_timing_error"] is None
    assert any("STATIC spatial comparison" in c for c in result["caveats"])
    # Peak MAGNITUDE error is still real: 100*(7-20)/20 = -65.0.
    assert result["metrics"]["peak_error"] == pytest.approx(-65.0)


def test_variable_head_adds_srms() -> None:
    observed = [10.0, 20.0, 30.0, 40.0, 50.0]
    simulated = [v + 5.0 for v in observed]

    generic = compute_skill_metrics(observed=observed, simulated=simulated, variable="generic")
    assert generic["metrics"]["SRMS"] is None
    assert generic["variable"] == "generic"

    head = compute_skill_metrics(observed=observed, simulated=simulated, variable="head")
    # SRMS = RMSE / (max(obs)-min(obs)) = 5/40 = 0.125  (plain ratio, no x100)
    assert head["metrics"]["SRMS"] == pytest.approx(0.125)
    assert head["variable"] == "head"
    assert head["bands"]["SRMS"] is not None
    assert head["bands"]["SRMS"]["satisfactory"] == "<0.10"
    # FIX 4a: no per-band 'source' key; a single top-level bands_source string.
    assert "source" not in head["bands"]["SRMS"]
    assert "source" not in head["bands"]["NSE"]
    assert isinstance(head["bands_source"], str) and "Moriasi" in head["bands_source"]


def test_constant_observed_metrics_null() -> None:
    observed = [5.0, 5.0, 5.0, 5.0, 5.0]
    simulated = [5.0, 6.0, 4.0, 5.0, 5.0]
    result = compute_skill_metrics(observed=observed, simulated=simulated)
    m = result["metrics"]
    assert m["NSE"] is None
    assert m["RSR"] is None
    assert m["KGE"] is None
    assert m["R2"] is None
    caveat_text = " ".join(result["caveats"])
    assert "NSE is null" in caveat_text
    assert "RSR is null" in caveat_text
    assert "KGE is null" in caveat_text
    assert "R2 is null" in caveat_text
    assert result["suggested_verdict"] == "indeterminate"


def test_small_n_indeterminate() -> None:
    result = compute_skill_metrics(observed=[1.0, 2.0, 3.0], simulated=[1.1, 2.1, 2.9])
    assert result["n"] == 3
    assert result["suggested_verdict"] == "indeterminate"
    assert any("indeterminate" in c for c in result["caveats"])
    # Metric values are still real numbers, not suppressed.
    assert result["metrics"]["RMSE"] is not None


def test_paired_table_uri_single_station(tmp_path) -> None:
    observed = [10.0, 20.0, 30.0, 40.0, 50.0]
    simulated = [v + 5.0 for v in observed]
    times = [f"2026-01-0{i+1}T00:00:00Z" for i in range(5)]
    records = [
        {"obs_id": "STN1", "observed": o, "simulated": s, "time": t}
        for o, s, t in zip(observed, simulated, times)
    ]
    path = str(tmp_path / "paired.fgb")
    _write_paired_table(path, records)

    result = compute_skill_metrics(paired_table_uri=path)
    assert result["n"] == 5
    assert result["metrics"]["PBIAS"] == pytest.approx(16.666667, abs=1e-5)
    assert not any("distinct obs_id groups" in c for c in result["caveats"])
    assert any("paired_table_uri" in n for n in result["notes"])


def test_paired_table_uri_multi_station_pools_with_caveat(tmp_path) -> None:
    records = [
        {"obs_id": "STN1", "observed": 10.0, "simulated": 12.0},
        {"obs_id": "STN1", "observed": 20.0, "simulated": 19.0},
        {"obs_id": "STN2", "observed": 5.0, "simulated": 6.0},
        {"obs_id": "STN2", "observed": 8.0, "simulated": 7.0},
    ]
    path = str(tmp_path / "paired_multi.fgb")
    _write_paired_table(path, records)

    result = compute_skill_metrics(paired_table_uri=path)
    assert result["n"] == 4
    assert any("distinct obs_id groups" in c for c in result["caveats"])


def test_no_selector_raises() -> None:
    with pytest.raises(SkillMetricsInputError):
        compute_skill_metrics()


def test_mismatched_length_raises() -> None:
    with pytest.raises(SkillMetricsInputError):
        compute_skill_metrics(observed=[1.0, 2.0], simulated=[1.0])


def test_all_nonfinite_raises() -> None:
    with pytest.raises(SkillMetricsNoDataError):
        compute_skill_metrics(observed=[float("nan"), float("nan")], simulated=[1.0, 2.0])


def test_units_passthrough() -> None:
    result = compute_skill_metrics(
        observed=[1.0, 2.0, 3.0, 4.0, 5.0],
        simulated=[1.1, 2.1, 2.9, 4.2, 4.8],
        units="m3/s",
    )
    assert result["units"] == "m3/s"


def test_kge_band_always_null_with_caveat() -> None:
    result = compute_skill_metrics(
        observed=[1.0, 2.0, 3.0, 4.0, 5.0],
        simulated=[1.1, 2.1, 2.9, 4.2, 4.8],
    )
    assert result["bands"]["KGE"] is None
    assert any("KGE has no graded acceptance band" in c for c in result["caveats"])


def test_dependency_missing_raises(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "spotpy", None)
    monkeypatch.setitem(sys.modules, "spotpy.objectivefunctions", None)
    with pytest.raises(SkillMetricsDependencyMissingError):
        compute_skill_metrics(observed=[1.0, 2.0, 3.0], simulated=[1.0, 2.0, 3.0])
