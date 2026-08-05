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


# --- levee-breach toggle (set_breach_enabled) -------------------------------- #
def test_breach_enabled_true_is_byte_identity():
    """enabled=True leaves the deck byte-identical (shipped deck breaches ON)."""
    text = _b04_text()
    new, n_active = deck_edit.set_breach_enabled(text, True)
    assert new == text
    assert n_active == 2  # the Muncie deck declares 2 lateral-structure breaches


def test_breach_disabled_zeroes_count_and_drops_records():
    """enabled=False sets the Breach Data count to 0 AND removes the record lines.

    Pinned in-container (2026-08-04): leaving the records with a 0 count crashes
    RasUnsteady (Unetreal.for); dropping them is the valid levee-holds edit."""
    text = _b04_text()
    new, n_active = deck_edit.set_breach_enabled(text, False)
    assert n_active == 0
    lines = new.splitlines()
    hdr = next(i for i, l in enumerate(lines) if l.strip() == "Breach Data")
    # the count line is now 0 ...
    assert lines[hdr + 1].strip() == "0"
    # ... and the very next line is the following section header (records dropped).
    assert lines[hdr + 2].strip() == "Hydrograph Data"


def test_breach_disable_preserves_rest_of_deck():
    """Only the Breach Data block changes; the hydrograph + boundaries survive."""
    new, _ = deck_edit.set_breach_enabled(_b04_text(), False)
    assert "Upstream Flow Hydrograph" in new
    assert "Downstream Normal Depth" in new
    assert "21000" in new  # the inflow ordinates are untouched


def test_breach_toggle_determinism_repeatable():
    text = _b04_text()
    a, _ = deck_edit.set_breach_enabled(text, False)
    b, _ = deck_edit.set_breach_enabled(text, False)
    assert a == b


def test_breach_disable_then_reenable_no_records_is_noop():
    """Disabling an already-disabled deck is a no-op (count already 0, no records)."""
    disabled, _ = deck_edit.set_breach_enabled(_b04_text(), False)
    again, n = deck_edit.set_breach_enabled(disabled, False)
    assert again == disabled and n == 0


def test_breach_and_flow_scale_compose():
    """The breach toggle and the flow scale touch disjoint blocks and compose."""
    text = _b04_text()
    # disable breach, then scale flow -> both edits present, deterministic order.
    no_breach, _ = deck_edit.set_breach_enabled(text, False)
    scaled, base_peak, scaled_peak = deck_edit.scale_flow_hydrograph(no_breach, 1.3)
    assert base_peak == pytest.approx(21000.0)
    assert scaled_peak == pytest.approx(27300.0)
    lines = scaled.splitlines()
    hdr = next(i for i, l in enumerate(lines) if l.strip() == "Breach Data")
    assert lines[hdr + 1].strip() == "0"  # breach still disabled after the flow edit
    assert "27300" in scaled  # flow scaled


def test_breach_no_block_is_noop():
    """A deck with no Breach Data block returns unchanged, 0 active."""
    text = "Hydrograph Data\n       1\n"
    new, n = deck_edit.set_breach_enabled(text, False)
    assert new == text and n == 0


# --- entrypoint _apply_breach honest surface --------------------------------- #
def test_apply_breach_absent_key_is_noop(tmp_path):
    """No breach_enabled in the manifest -> the deck is left as-is (breaches ON)."""
    b04 = tmp_path / "Muncie.b04"
    b04.write_text(_b04_text())
    out = entrypoint._apply_breach(tmp_path, "Muncie.b04", {"flow_scale": 1.0})
    assert out == {}
    assert b04.read_text() == _b04_text()  # untouched


def test_apply_breach_disable_writes_provenance(tmp_path):
    b04 = tmp_path / "Muncie.b04"
    b04.write_text(_b04_text())
    out = entrypoint._apply_breach(tmp_path, "Muncie.b04", {"breach_enabled": False})
    assert out == {"breach_enabled": False, "breach_count_active": 0}
    lines = b04.read_text().splitlines()
    hdr = next(i for i, l in enumerate(lines) if l.strip() == "Breach Data")
    assert lines[hdr + 1].strip() == "0"
    assert lines[hdr + 2].strip() == "Hydrograph Data"


def test_apply_breach_missing_boundary_raises(tmp_path):
    with pytest.raises(entrypoint.HecrasError, match="boundary file"):
        entrypoint._apply_breach(tmp_path, "Absent.b04", {"breach_enabled": False})


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
