"""Offline tests for the shared rain-on-grid hydrograph-validation primitives.

Covers the two paper-exact metric functions (Godara et al. 2024 eq 13/14) and
the computed-vs-observed hydrograph overlay chart helper. Every metric assertion
is checked against a HAND-COMPUTED fixture (numpy longhand), never against the
implementation under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.tools.processing.charts_common import (
    build_hydrograph_overlay_chart,
)
from trid3nt_server.tools.processing.compute_skill_metrics.compute_skill_metrics import (
    SkillMetricsInputError,
    nash_sutcliffe_efficiency,
    pearson_r2,
)

# Hand fixture: five paired points with a small, deliberate model error.
_OBS = [1.0, 2.0, 3.0, 4.0, 5.0]
_SIM = [1.1, 1.9, 3.2, 3.8, 5.1]


def _hand_nse(obs: list[float], sim: list[float]) -> float:
    o = np.asarray(obs, dtype=float)
    s = np.asarray(sim, dtype=float)
    return 1.0 - np.sum((o - s) ** 2) / np.sum((o - o.mean()) ** 2)


def _hand_r2(obs: list[float], sim: list[float]) -> float:
    o = np.asarray(obs, dtype=float)
    s = np.asarray(sim, dtype=float)
    num = np.sum((o - o.mean()) * (s - s.mean())) ** 2
    den = np.sum((o - o.mean()) ** 2) * np.sum((s - s.mean()) ** 2)
    return num / den


def test_nse_matches_hand_eq14():
    got = nash_sutcliffe_efficiency(_OBS, _SIM)
    assert got is not None
    assert got == pytest.approx(_hand_nse(_OBS, _SIM), abs=1e-6)


def test_r2_matches_hand_eq13():
    got = pearson_r2(_OBS, _SIM)
    assert got is not None
    assert got == pytest.approx(_hand_r2(_OBS, _SIM), abs=1e-6)


def test_perfect_fit_nse_and_r2_are_one():
    assert nash_sutcliffe_efficiency(_OBS, _OBS) == pytest.approx(1.0, abs=1e-6)
    assert pearson_r2(_OBS, _OBS) == pytest.approx(1.0, abs=1e-6)


def test_nse_can_go_negative_when_worse_than_mean():
    # A model that is worse than the observed mean has NSE < 0 (eq 14 property).
    obs = [1.0, 2.0, 3.0, 4.0, 5.0]
    sim = [5.0, 4.0, 3.0, 2.0, 1.0]
    got = nash_sutcliffe_efficiency(obs, sim)
    assert got is not None and got < 0.0
    assert got == pytest.approx(_hand_nse(obs, sim), abs=1e-6)


def test_zero_variance_observed_returns_none_not_fabricated():
    # Flat observed series -> undefined NSE/R2 denominator -> None (honesty floor).
    assert nash_sutcliffe_efficiency([3.0, 3.0, 3.0], [2.0, 3.0, 4.0]) is None
    assert pearson_r2([3.0, 3.0, 3.0], [2.0, 3.0, 4.0]) is None


def test_nonfinite_pairs_dropped_before_scoring():
    obs = [1.0, 2.0, float("nan"), 4.0, 5.0]
    sim = [1.1, 1.9, 3.2, 3.8, 5.1]
    # Dropping index 2 must give the same score as the 4-pair hand computation.
    obs_f = [1.0, 2.0, 4.0, 5.0]
    sim_f = [1.1, 1.9, 3.8, 5.1]
    got = nash_sutcliffe_efficiency(obs, sim)
    assert got == pytest.approx(_hand_nse(obs_f, sim_f), abs=1e-6)


def test_fewer_than_two_pairs_returns_none():
    assert nash_sutcliffe_efficiency([1.0], [1.0]) is None
    assert pearson_r2([1.0], [1.0]) is None


def test_length_mismatch_raises():
    with pytest.raises(SkillMetricsInputError):
        nash_sutcliffe_efficiency([1.0, 2.0], [1.0])
    with pytest.raises(SkillMetricsInputError):
        pearson_r2([1.0, 2.0], [1.0])


# ---------------------------------------------------------------------------
# Hydrograph overlay chart helper.
# ---------------------------------------------------------------------------


def test_overlay_two_series_valid_spec_with_skill_in_caption():
    times = [0.0, 1.0, 2.0, 3.0, 4.0]
    payload = build_hydrograph_overlay_chart(
        times=times,
        computed=_SIM,
        observed=_OBS,
        nse=0.989,
        r2=0.989,
    )
    assert payload is not None
    assert payload["envelope_type"] == "chart-emission"
    spec = payload["vega_lite_spec"]
    series = {row["series"] for row in spec["data"]["values"]}
    assert series == {"computed", "observed"}
    assert spec["encoding"]["x"]["type"] == "quantitative"
    assert "NSE 0.989" in payload["caption"]
    assert "R2 0.989" in payload["caption"]


def test_overlay_iso_time_axis_is_temporal():
    times = ["2021-08-17T00:00:00Z", "2021-08-17T01:00:00Z", "2021-08-17T02:00:00Z"]
    payload = build_hydrograph_overlay_chart(
        times=times, computed=[1.0, 2.0, 1.5]
    )
    assert payload is not None
    assert payload["vega_lite_spec"]["encoding"]["x"]["type"] == "temporal"


def test_overlay_computed_only_when_observed_absent():
    payload = build_hydrograph_overlay_chart(
        times=[0.0, 1.0, 2.0], computed=[1.0, 2.0, 1.5], observed=None
    )
    assert payload is not None
    series = {row["series"] for row in payload["vega_lite_spec"]["data"]["values"]}
    assert series == {"computed"}


def test_overlay_honesty_floor_single_point_returns_none():
    assert build_hydrograph_overlay_chart(times=[0.0], computed=[1.0]) is None
    # All-nonfinite computed -> None too.
    assert (
        build_hydrograph_overlay_chart(
            times=[0.0, 1.0], computed=[float("nan"), float("inf")]
        )
        is None
    )
