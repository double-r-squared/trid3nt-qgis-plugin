"""Unit tests for the SFR stream-depletion postprocess sign math (module wave).

``compute_stream_depletion_metrics`` is PURE arithmetic over the parsed SFR obs
CSV, so the SIGN conventions (pinned by the Phase-1 mf6 6.5.0 smoke fixture) are
tested directly - NO flopy, NO mf6 binary. The signs are the #1 silent-error
risk (a wrong sign narrates depletion backwards while passing structural checks):

  * FLOW (downstream-flow) is a NEGATIVE outflow magnitude -> feature carries abs.
  * GWF (sfr exchange) is reach-relative: POSITIVE = reach LOSES to the aquifer
    (losing), NEGATIVE = aquifer FEEDS the reach (gaining).
  * depletion = sum(pumped GWF) - sum(baseline GWF); POSITIVE = capture.

Run:
    <agent-venv>/bin/python -m pytest server/tests/test_stream_depletion.py -q
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from trid3nt_server.workflows.postprocess_modflow import (
    _sfr_obs_value,
    compute_stream_depletion_metrics,
)

# The 0-based SFR obs fixture the adapter emits (26 reaches; a 2000 m^3/day well
# 300 m south of an 8-reach-scale Boise flowline; converged on bin/mf6 6.5.0).
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "workers"
    / "modflow"
    / "fixtures"
    / "sfr_smoke"
    / "stream_depletion.sfr.obs.csv"
)


def _load_fixture() -> tuple[list[dict], int]:
    with FIXTURE.open() as fh:
        rows = list(csv.DictReader(fh))
    n = sum(1 for k in rows[0] if k.upper().startswith("GWF_R"))
    return rows, n


def test_fixture_present_and_has_reaches():
    assert FIXTURE.is_file(), f"missing SFR obs fixture: {FIXTURE}"
    rows, n = _load_fixture()
    assert n >= 8, f"expected >= 8 reaches, got {n}"
    assert len(rows) >= 2, "need a baseline + pumped row"


def test_obs_value_reads_uppercased_boundnames():
    row = {"time": "1.0", "STAGE_R0": "28.5", "FLOW_R0": "-5000.0", "GWF_R0": "-10.5"}
    assert _sfr_obs_value(row, "STAGE", 0) == pytest.approx(28.5)
    assert _sfr_obs_value(row, "FLOW", 0) == pytest.approx(-5000.0)
    assert _sfr_obs_value(row, "GWF", 0) == pytest.approx(-10.5)


def test_depletion_is_positive_capture_on_the_fixture():
    rows, n = _load_fixture()
    m = compute_stream_depletion_metrics(rows, n)
    # Pumping DEPLETES the stream -> a positive capture (the >=0 floor holds).
    assert m["total_depletion_m3_day"] > 0.0
    # The fixture pumps 2000 m^3/day; a near-stream well captures 40-80% at demo
    # streambed resistance (48% proven in the smoke; this run recovered ~62%).
    assert 0.3 * 2000.0 < m["total_depletion_m3_day"] < 0.9 * 2000.0


def test_gaining_losing_classification_from_exchange_sign():
    rows, n = _load_fixture()
    m = compute_stream_depletion_metrics(rows, n)
    # Every reach is classified; counts partition the reaches.
    assert m["gaining_reach_count"] + m["losing_reach_count"] <= n
    # Both classes present near a pumping well (some reaches flip toward losing).
    assert m["gaining_reach_count"] > 0
    assert m["losing_reach_count"] > 0
    # Per-reach classification agrees with the exchange sign convention.
    for pr in m["per_reach"]:
        if pr["classification"] == "gaining":
            assert pr["exchange_m3_day"] < 0.0
        elif pr["classification"] == "losing":
            assert pr["exchange_m3_day"] > 0.0
    # Flow magnitude is the ABSOLUTE downstream-flow (never negative).
    assert all(pr["flow_m3_day"] >= 0.0 for pr in m["per_reach"])


def test_sign_convention_on_a_hand_built_case():
    """A synthetic 2-reach obs: reach 0 gains (exch<0), reach 1 loses (exch>0);
    pumping deepens both toward losing -> positive net depletion."""
    base = {
        "time": "1.0",
        "STAGE_R0": "10.0", "STAGE_R1": "9.9",
        "FLOW_R0": "-5010.0", "FLOW_R1": "-5000.0",
        "GWF_R0": "-10.0", "GWF_R1": "5.0",
    }
    pumped = {
        "time": "366.0",
        "STAGE_R0": "9.6", "STAGE_R1": "9.5",
        "FLOW_R0": "-4990.0", "FLOW_R1": "-4900.0",
        "GWF_R0": "-2.0", "GWF_R1": "40.0",
    }
    m = compute_stream_depletion_metrics([base, pumped], 2)
    # depletion = (-2+40) - (-10+5) = 38 - (-5) = 43 (positive capture).
    assert m["total_depletion_m3_day"] == pytest.approx(43.0)
    # Pumped period: reach 0 gains (-2 < 0), reach 1 loses (40 > 0).
    assert m["gaining_reach_count"] == 1
    assert m["losing_reach_count"] == 1
    # Max stage decline = max(10.0-9.6, 9.9-9.5) = 0.4.
    assert m["max_stage_decline_m"] == pytest.approx(0.4)
