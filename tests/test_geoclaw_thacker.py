"""Thacker paraboloid-basin V&V grader + chart (ADR 0187), offline.

Feeds the grader SYNTHETIC gauge files built from the closed form itself, so a
correct grader reports ~zero error (period / amplitude / shoreline) -- the unit
pin that ``compute_thacker_vandv`` measures what it claims. The live end-to-end
solve is proven separately through the rebuilt image.

ASCII only.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from trid3nt_contracts.geoclaw_thacker import (
    THACKER_GRAVITY,
    thacker_depth,
    thacker_eta,
    thacker_reference,
)
from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
    build_thacker_validation_chart_spec,
    compute_thacker_vandv,
)

A_M, H0_M, AMP_A = 1.0, 0.1, 0.5


def _write_gauge(path: Path, rows: list[tuple[float, float, float]]) -> None:
    """Write a GeoClaw-style gauge file: header + [level, t, h, hu, hv, eta]."""
    lines = ["# gauge", "# level t h hu hv eta"]
    for t, h, eta in rows:
        lines.append(f"1 {t:.6f} {h:.8f} 0.0 0.0 {eta:.8f}")
    path.write_text("\n".join(lines) + "\n")


def _synthetic_run(tmp_path: Path) -> Path:
    out = tmp_path / "_output"
    out.mkdir()
    ref = thacker_reference(A_M, H0_M, AMP_A)
    T = ref["period_s"]
    ts = np.linspace(0.0, 2.5 * T, 600)
    # center gauge (id 1): analytic eta(0,t), depth = eta - B(0) = eta + h0
    center = [(float(t), float(thacker_eta(0, 0, t, A_M, H0_M, AMP_A) + H0_M),
               float(thacker_eta(0, 0, t, A_M, H0_M, AMP_A))) for t in ts]
    _write_gauge(out / "gauge00001.txt", center)
    # axis gauges (ids 100..130) at r=0..1.5a: analytic depth(r,t)
    n = 31
    for k in range(n):
        r = 1.5 * A_M * (k / (n - 1))
        rows = [(float(t), float(thacker_depth(r, 0, t, A_M, H0_M, AMP_A)),
                 float(thacker_eta(r, 0, t, A_M, H0_M, AMP_A))) for t in ts]
        _write_gauge(out / f"gauge{100 + k:05d}.txt", rows)
    return tmp_path


def test_thacker_vandv_recovers_analytic_from_synthetic_gauges(tmp_path):
    run = _synthetic_run(tmp_path)
    v = compute_thacker_vandv(run, A_M, H0_M, AMP_A)
    ref = thacker_reference(A_M, H0_M, AMP_A)

    # Period recovered within a few percent (autocorrelation on 600 samples).
    assert v["period_error_pct"] < 4.0
    # Amplitude essentially exact (the input IS the analytic surface).
    assert v["eta_amplitude_error_pct"] < 1.0
    assert math.isclose(v["eta_center_max_analytic_m"], ref["eta_center_max_m"], rel_tol=1e-9)
    # Shoreline excursion within one axis-gauge spacing (0.05a).
    assert v["r_shore_max_error_pct"] < 12.0
    assert v["r_shore_min_error_pct"] < 12.0
    # RMS of (numerical - analytic) is ~0 for the analytic fixture.
    assert v["rms_eta_m"] < 1e-6
    # No fort.q frames in the fixture -> mass drift is honestly NaN, not fabricated.
    assert math.isnan(v["mass_drift_pct"])


def test_thacker_chart_spec_layers_numerical_and_analytic(tmp_path):
    run = _synthetic_run(tmp_path)
    v = compute_thacker_vandv(run, A_M, H0_M, AMP_A)
    spec = build_thacker_validation_chart_spec(v)
    assert spec is not None
    vals = spec["data"]["values"]
    sols = {row["solution"] for row in vals}
    assert sols == {"GeoClaw (numerical)", "Thacker 1981 (analytic)"}
    # Downsampled to fit the inline-row cap WITHOUT truncating the time range.
    assert len(vals) <= 2000
    tmax = max(r["t_s"] for r in vals)
    assert tmax >= 2.0 * thacker_reference(A_M, H0_M, AMP_A)["period_s"]


def test_thacker_reference_matches_recipe_period_formula():
    """period_s == 2*pi*a/sqrt(8 g h0) (the recipe's V&V gate formula)."""
    ref = thacker_reference(A_M, H0_M, AMP_A)
    expected = 2.0 * math.pi * A_M / math.sqrt(8.0 * THACKER_GRAVITY * H0_M)
    assert math.isclose(ref["period_s"], expected, rel_tol=1e-12)


def test_thacker_vandv_missing_center_gauge_raises(tmp_path):
    from trid3nt_server.agent.workflows.geoclaw.postprocess_geoclaw import (
        PostprocessGeoClawError,
    )
    (tmp_path / "_output").mkdir()
    with pytest.raises(PostprocessGeoClawError):
        compute_thacker_vandv(tmp_path, A_M, H0_M, AMP_A)
