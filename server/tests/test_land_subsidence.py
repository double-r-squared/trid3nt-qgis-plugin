"""Unit tests for the CSUB land-subsidence postprocess math (module wave).

``compute_subsidence_metrics`` + ``_read_csub_zdisplacement`` run directly on the
Phase-1 mf6 6.5.0 smoke fixture (services/workers/modflow/fixtures/csub_smoke),
so the SIGN convention + the magnitude metrics + the dz=Ssv*b*dh analytical
cross-check are pinned on REAL engine output - NO new mf6 run required. The sign
is the #1 silent-error risk (a wrong sign narrates subsidence as uplift and
passes every structural test): subsidence is POSITIVE-DOWN (the z-displacement
grid is positive at the pumped cell).

Run:
    <agent-venv>/bin/python -m pytest server/tests/test_land_subsidence.py -q
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from trid3nt_server.workflows.postprocess_modflow import (
    CSUB_ZDISP_TEXT_TAG,
    _read_csub_zdisplacement,
    compute_subsidence_metrics,
)

# The Phase-1 CSUB smoke fixture (40x40 confined GWF + 9 no-delay HEAD_BASED
# interbeds; 4000 m^3/day well over 10 yearly periods; converged on bin/mf6 6.5.0).
FIXTURE_DIR = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "workers"
    / "modflow"
    / "fixtures"
    / "csub_smoke"
    / "csub_run"
)
ZDISP = FIXTURE_DIR / "csubsmoke.csub.zdisp.bin"
OBS = FIXTURE_DIR / "csubsmoke.csub.obs.csv"

# Smoke deck constants (see fixtures/csub_smoke/csub_smoke.py) for the analytical
# cross-check dz = Ssv * b * dh.
SSV = 2e-3          # inelastic specific storage, m^-1
THICK_FRAC = 0.5    # interbed thickness fraction
LAYER_THICK_M = 30.0
B_INTERBED_M = THICK_FRAC * LAYER_THICK_M   # 15 m
DELR = DELC = 50.0                          # cell size, m
CELL_AREA_M2 = DELR * DELC


def _load_obs() -> list[dict]:
    with OBS.open() as fh:
        return list(csv.DictReader(fh))


def test_fixture_present():
    assert ZDISP.is_file(), f"missing CSUB z-displacement fixture: {ZDISP}"
    assert OBS.is_file(), f"missing CSUB obs fixture: {OBS}"


def test_zdisplacement_text_tag_is_truncated():
    # PINNED: mf6 6.5.0 writes CSUB-ZDISPLACE (16-char truncation), NOT
    # "CSUB-ZDISPLACEMENT" - reading with the full name raises EOFError.
    assert CSUB_ZDISP_TEXT_TAG == "CSUB-ZDISPLACE"


def test_subsidence_is_positive_down_on_the_fixture():
    zdisp = _read_csub_zdisplacement(ZDISP)
    import numpy as np

    finite = zdisp[np.isfinite(zdisp)]
    assert finite.size > 0
    # Subsidence POSITIVE-DOWN: the peak z-displacement at the pumped cell is > 0.
    assert float(np.max(finite)) > 0.0, "subsidence must be reported positive-down"


def test_max_subsidence_cm_and_area_from_the_fixture():
    zdisp = _read_csub_zdisplacement(ZDISP)
    obs_rows = _load_obs()
    m = compute_subsidence_metrics(
        zdisp, cell_area_m2=CELL_AREA_M2, obs_rows=obs_rows
    )
    # The 10-year 4000 m^3/day demo run produces an order-of-tens-of-cm bowl.
    assert m["max_subsidence_cm"] > 0.0
    assert 5.0 < m["max_subsidence_cm"] < 100.0, m["max_subsidence_cm"]
    # The bowl covers a positive area (>= the 9-cell footprint scale).
    assert m["subsidence_area_km2"] > 0.0


def test_inelastic_fraction_near_one_on_the_fixture():
    # pcs0 = 0 with HEAD_BASED makes ALL drawdown inelastic (permanent) -> the
    # inelastic fraction is ~1.0 (the physically-correct permanence signature).
    obs_rows = _load_obs()
    zdisp = _read_csub_zdisplacement(ZDISP)
    m = compute_subsidence_metrics(
        zdisp, cell_area_m2=CELL_AREA_M2, obs_rows=obs_rows
    )
    assert m["inelastic_fraction"] == pytest.approx(1.0, abs=0.05)


def test_dz_analytic_cross_check_ssv_b_dh():
    """The final compaction dz should approach the closed-form ultimate
    dz = Ssv * b * dh (the analytical yardstick the postprocess narrates)."""
    zdisp = _read_csub_zdisplacement(ZDISP)
    import numpy as np

    dz_model_m = float(np.nanmax(zdisp))
    # Head decline at the pumped cell from the fixture obs is ~12.35 m (the smoke
    # printed decline). Use the peak-compaction interbed's total compaction to
    # infer dh via dz = Ssv * b * dh -> dh = dz / (Ssv * b).
    dh_inferred = dz_model_m / (SSV * B_INTERBED_M)
    # The smoke recorded ~12.35 m of decline at the well; the inferred dh must be
    # in that neighbourhood (the model dz slightly EXCEEDS Ssv*b*dh because the
    # coarse-grained elastic compaction adds on top of the interbed inelastic).
    assert 8.0 < dh_inferred < 16.0, dh_inferred
    # And the analytical ultimate for a 12.35 m decline is ~0.37 m = 37 cm, which
    # the transient 10-year model should be within a small factor of.
    dz_analytic_m = SSV * B_INTERBED_M * 12.35
    assert dz_model_m == pytest.approx(dz_analytic_m, rel=0.15), (
        dz_model_m,
        dz_analytic_m,
    )


def test_subsidence_series_is_monotonic_non_decreasing():
    """Permanence: the per-step cumulative subsidence rises and does not recover."""
    obs_rows = _load_obs()
    zdisp = _read_csub_zdisplacement(ZDISP)
    m = compute_subsidence_metrics(
        zdisp, cell_area_m2=CELL_AREA_M2, obs_rows=obs_rows
    )
    series = m["subsidence_series_cm"]
    assert len(series) >= 2
    for a, b in zip(series[:-1], series[1:]):
        assert b >= a - 1e-6, f"subsidence must not recover: {series}"


def test_metrics_on_a_hand_built_grid():
    """A synthetic 3x3 z-displacement grid: sign + area + fraction math directly."""
    import numpy as np

    grid = np.array(
        [
            [0.0, 0.01, 0.0],
            [0.01, 0.20, 0.01],   # 0.20 m = 20 cm peak
            [0.0, 0.01, 0.0],
        ],
        dtype="float64",
    )
    obs = [
        {"time": "1.0", "COMPACTION_R0": "0.05", "INE_R0": "0.05", "ELA_R0": "0.0"},
        {"time": "2.0", "COMPACTION_R0": "0.20", "INE_R0": "0.19", "ELA_R0": "0.01"},
    ]
    m = compute_subsidence_metrics(grid, cell_area_m2=100.0, obs_rows=obs, n_interbeds=1)
    assert m["max_subsidence_cm"] == pytest.approx(20.0)
    # 5 cells exceed the 1 mm floor (the peak + the 4 edge-mid 0.01 m cells).
    assert m["subsidence_area_km2"] == pytest.approx(5 * 100.0 / 1e6)
    # inelastic fraction = 0.19 / (0.19 + 0.01) = 0.95.
    assert m["inelastic_fraction"] == pytest.approx(0.95)
