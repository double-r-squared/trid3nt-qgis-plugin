"""SWAN nonstationary storm-evolution composer wiring (ADR 0190 row 3).

Server-side offline coverage: the storm-hydrograph builder, the storm_peak_hs_m
knob forcing nonstationary + threading a boundary_timeseries onto the build_spec.
The worker deck render (ISO times / BLOCK OUTPUT / PROP BSBT / TPAR) is covered
in services/workers/swan/test_deck_builder.py.
"""
from __future__ import annotations

import inspect

import pytest

from trid3nt_server.agent.workflows.swan.wave_field.wave_field import (
    build_storm_hydrograph, swan_wave_field,
)
from trid3nt_server.agent.workflows.swan.run_swan import build_swan_build_spec
from trid3nt_contracts.swan_contracts import SwanRunArgs, SwanWaveBoundary


def test_template_surfaces_storm_knobs():
    sig = inspect.signature(swan_wave_field)
    assert "storm_peak_hs_m" in sig.parameters
    assert "storm_peak_hour" in sig.parameters


def test_hydrograph_builds_to_peak_then_decays():
    h = build_storm_hydrograph(1.0, 6.0, 8.0, 180.0, 25.0, 86400.0, None, 9)
    hs = [r[1] for r in h]
    assert hs[0] == pytest.approx(1.0)          # baseline start
    assert max(hs) == pytest.approx(6.0)        # peak reached
    assert hs[len(hs) // 2] == pytest.approx(6.0)  # peak at mid (default)
    assert hs[-1] == pytest.approx(1.0)         # decays back to baseline
    # monotone up to peak, monotone down after
    peak_i = hs.index(max(hs))
    assert all(hs[i] <= hs[i + 1] for i in range(peak_i))
    assert all(hs[i] >= hs[i + 1] for i in range(peak_i, len(hs) - 1))


def test_hydrograph_honors_peak_hour():
    h = build_storm_hydrograph(1.0, 5.0, 8.0, 180.0, 25.0, 86400.0, 6.0, 25)
    peak_row = max(h, key=lambda r: r[1])
    assert peak_row[0] == pytest.approx(6.0 * 3600.0, rel=0.1)  # peak near hour 6


def test_hydrograph_tp_grows_with_hs():
    h = build_storm_hydrograph(1.0, 6.0, 8.0, 180.0, 25.0, 86400.0, None, 9)
    peak = max(h, key=lambda r: r[1])
    base = h[0]
    assert peak[2] > base[2]  # longer period at the storm peak


def test_storm_series_threads_onto_build_spec():
    h = build_storm_hydrograph(1.0, 6.0, 8.0, 180.0, 25.0, 86400.0, None, 9)
    args = SwanRunArgs(
        mode="nonstationary", bbox=[-84.3, 29.7, -83.9, 30.05],
        boundary=SwanWaveBoundary(hs_m=1.0, tp_s=8.0, dir_deg=180.0,
                                  spread_deg=25.0, side="S"),
        storm_boundary_timeseries=h,
    )
    spec = build_swan_build_spec(args)
    assert "boundary_timeseries" in spec
    assert len(spec["boundary_timeseries"]) == 9


def test_no_storm_knob_leaves_spec_without_timeseries():
    args = SwanRunArgs(mode="nonstationary", bbox=[-84.3, 29.7, -83.9, 30.05])
    spec = build_swan_build_spec(args)
    assert "boundary_timeseries" not in spec
