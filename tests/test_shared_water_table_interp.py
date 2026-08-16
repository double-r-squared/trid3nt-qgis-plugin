"""Tests for the shared water-table interpolation seam (regression kriging / plane).

Offline, pure (numpy only). Asserts the tiered decision rule, kriging exactness at
the observations, gradient recovery, and the honest INSUFFICIENT fallback.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from trid3nt_server.agent.workflows.shared.water_table_interp import (
    KRIGE_MIN_WELLS,
    TREND_MIN_WELLS,
    WaterTableSurface,
    interpolate_water_table,
)


def _wells_on_plane(n: int, slope_e: float, slope_n: float, *, bump: bool = False,
                    spread: float = 2000.0, seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    e = rng.uniform(-spread, spread, n)
    nn = rng.uniform(-spread, spread, n)
    h = 100.0 + slope_e * e + slope_n * nn
    if bump:
        h = h + 2.0 * np.exp(-((e - 500) ** 2 + (nn + 300) ** 2) / (600.0 ** 2))
    return [
        {"east": float(e[i]), "north": float(nn[i]), "head_m": float(h[i])}
        for i in range(n)
    ]


def test_dense_uses_kriging_and_is_exact() -> None:
    """>= KRIGE_MIN_WELLS with spread -> regression kriging, exact at the wells."""
    wells = _wells_on_plane(20, 0.003, -0.001, bump=True)
    s = interpolate_water_table(wells)
    assert isinstance(s, WaterTableSurface)
    assert s.method == "regression_kriging"
    assert s.variogram and s.variogram.get("range_m", 0.0) > 0.0
    # Ordinary kriging with zero nugget interpolates the observations exactly.
    errs = [abs(float(s.sample(w["east"], w["north"])[0]) - w["head_m"]) for w in wells]
    assert max(errs) < 1e-3, max(errs)


def test_gradient_recovers_true_plane() -> None:
    wells = _wells_on_plane(16, 0.003, -0.001)
    s = interpolate_water_table(wells)
    assert s.gradient_x == pytest.approx(0.003, rel=0.15)
    assert s.gradient_y == pytest.approx(-0.001, abs=5e-4)
    # Azimuth is the compass bearing groundwater FLOWS toward (down-gradient).
    assert 0.0 <= s.gradient_azimuth_deg < 360.0


def test_sparse_uses_trend_plane() -> None:
    """TREND_MIN_WELLS <= n < KRIGE_MIN_WELLS -> trend plane (no kriging)."""
    wells = _wells_on_plane(5, 0.003, 0.0)
    s = interpolate_water_table(wells)
    assert s.method == "trend_plane"
    assert not s.variogram
    assert "trend plane" in s.reason


def test_too_few_wells_insufficient() -> None:
    assert interpolate_water_table(_wells_on_plane(2, 0.003, 0.0)) is None


def test_collinear_is_insufficient() -> None:
    """Wells strung on one line -> cross-gradient unconstrained -> None."""
    wells = [
        {"east": float(x), "north": float(0.5 * x), "head_m": 100.0 + 0.002 * x}
        for x in np.linspace(0.0, 3000.0, 10)
    ]
    assert interpolate_water_table(wells) is None


def test_provenance_dict_shape() -> None:
    s = interpolate_water_table(_wells_on_plane(10, 0.002, 0.001))
    p = s.provenance()
    assert p["method"] in ("regression_kriging", "trend_plane")
    assert p["n_wells"] == 10
    assert math.isfinite(p["gradient_magnitude_m_per_m"])
