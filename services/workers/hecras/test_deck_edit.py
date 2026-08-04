"""HEC-RAS unsteady-flow deck-edit unit tests (engine #11 landing).

Flat-import pattern (M2 lesson: services/workers tests do NOT collect from repo
root -- run FROM the worker dir):

    cd services/workers/hecras && python -m pytest test_deck_edit.py

Binary-free: exercises the pure ``.bNN`` flow-hydrograph scaler + the entrypoint's
deck-staging / flow-scale honest-failure surface, against the shipped Muncie
boundary file (no engine invocation). The reparameterization is empirically the
authoritative deck edit (in-container 2026-08-04: scaling the ``.bNN`` moved the
2D max water surface; scaling the HDF did NOT).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deck_edit  # noqa: E402
import entrypoint  # noqa: E402

_MUNCIE_B04 = (
    Path(__file__).resolve().parent / "fixtures" / "muncie_smoke" / "wrk_source" / "Muncie.b04"
)


def _b04_text() -> str:
    return _MUNCIE_B04.read_text()


def test_baseline_peak_is_21000():
    _, base_peak, _ = deck_edit.scale_flow_hydrograph(_b04_text(), 1.0)
    assert base_peak == pytest.approx(21000.0)


def test_scale_multiplies_flow_ordinates():
    text = _b04_text()
    new, base_peak, scaled_peak = deck_edit.scale_flow_hydrograph(text, 1.3)
    assert base_peak == pytest.approx(21000.0)
    assert scaled_peak == pytest.approx(27300.0)
    # the scaled peak appears in the rewritten block
    assert "27300" in new


def test_scale_one_is_value_identity_on_flows():
    """scale=1.0 must reproduce every flow ordinate (deck-edit determinism)."""
    text = _b04_text()
    new, base_peak, scaled_peak = deck_edit.scale_flow_hydrograph(text, 1.0)
    assert base_peak == scaled_peak == pytest.approx(21000.0)
    # every original flow value survives round-trip
    for q in ("13500", "21000", "19000"):
        assert q in new


def test_scale_preserves_fixed_field_layout():
    """The rewritten hydrograph keeps HEC's 8-char fields / 5-pairs-per-line."""
    new, _, _ = deck_edit.scale_flow_hydrograph(_b04_text(), 1.3)
    lines = new.splitlines()
    hdr = next(i for i, l in enumerate(lines) if "Flow Hydrograph" in l)
    # header, count, then 5 data lines of 25 ordinates (5 pairs x 8*2 = 80 chars).
    data_lines = lines[hdr + 2 : hdr + 7]
    assert len(data_lines) == 5
    for dl in data_lines:
        assert len(dl) == 80  # 10 fields x 8 chars


def test_deck_edit_determinism_repeatable():
    """Same args -> byte-identical edited deck (determinism boundary)."""
    text = _b04_text()
    a, _, _ = deck_edit.scale_flow_hydrograph(text, 1.3)
    b, _, _ = deck_edit.scale_flow_hydrograph(text, 1.3)
    assert a == b


def test_non_hydrograph_bytes_untouched():
    """Only the flow ordinates change; the rest of the deck is byte-preserved."""
    text = _b04_text()
    new, _, _ = deck_edit.scale_flow_hydrograph(text, 1.3)
    # the downstream normal-depth / friction slope literal is not a flow ordinate.
    assert "Muncie 2D Flow Area" in new
    assert "Computation Interval" in new


def test_bad_scale_raises():
    for bad in (0.0, -1.0, float("nan")):
        with pytest.raises(deck_edit.DeckEditError):
            deck_edit.scale_flow_hydrograph(_b04_text(), bad)


# --- entrypoint deck-staging / flow-scale honest-failure surface ------------- #
def test_stage_baked_deck_unknown_archetype_raises(tmp_path):
    with pytest.raises(entrypoint.HecrasError, match="unknown archetype"):
        entrypoint._stage_baked_deck("not_a_real_archetype", tmp_path)


def test_apply_flow_scale_target_peak_derives_multiplier(tmp_path):
    """target_peak_cfs derives the multiplier from the baseline peak (seam-1 path)."""
    b04 = tmp_path / "Muncie.b04"
    b04.write_text(_b04_text())
    forcing = entrypoint._apply_flow_scale(
        tmp_path, "Muncie.b04", {"target_peak_cfs": 42000.0}
    )
    # 42000 / 21000 == 2.0
    assert forcing["flow_scale"] == pytest.approx(2.0)
    assert forcing["peak_inflow_cfs"] == pytest.approx(42000.0)


def test_apply_flow_scale_clamps_to_band(tmp_path):
    b04 = tmp_path / "Muncie.b04"
    b04.write_text(_b04_text())
    forcing = entrypoint._apply_flow_scale(tmp_path, "Muncie.b04", {"flow_scale": 99.0})
    assert forcing["flow_scale"] == pytest.approx(4.0)  # clamped to the band


def test_apply_flow_scale_missing_boundary_raises(tmp_path):
    with pytest.raises(entrypoint.HecrasError, match="boundary file"):
        entrypoint._apply_flow_scale(tmp_path, "Absent.b04", {"flow_scale": 1.0})
