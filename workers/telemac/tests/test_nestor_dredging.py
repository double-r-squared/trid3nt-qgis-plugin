"""NESTOR dredging deck authoring (action + polygon + surface-ref).

Pins the NESTOR own-format grammar against the in-image compiled fortran the
baked libnestor4*.so builds from (sources/nestor/readdigactions.f,
readpolygons.f, isactioncompletelydefined.f, threedigitsnumeral.f,
datestringtoseconds.f, set_by_profiles_values_for.f). These are the exact
format subtleties an in-image direct solve exposed (a guessed format would have
crashed the compiled parser):

  * field/polygon names need a 3-numeral prefix whose FIRST digit is 1-9
    (ThreeDigitsNumeral rejects a leading 0), so ids start at 101/102;
  * RESTART is read as a Fortran LOGICAL (must be F, not DAMOCLES NO);
  * action-file dates are yyyy.mm.dd-hh:mm:ss (exactly 19 chars);
  * the polygon file must end with a bare ENDFILE line;
  * the surface reference file is MANDATORY in BOTH modes (Write_Node_Info logs
    km chainage via the profiles) and every profile line carries 7 reals;
  * criterion mode uses ReferenceLevel = SECTIONS (profile-interpolated grade),
    never GRID.
"""
from __future__ import annotations

import re

import numpy as np

from workers.telemac import telemac_river_dye_build as B


def _mesh():
    """A straight synthetic channel: centerline + node cloud (UTM metres)."""
    cl = np.array([[float(x), 0.0] for x in range(0, 420, 20)], dtype=float)
    xs = np.linspace(0, 400, 41)
    X = np.concatenate([xs, xs, xs])
    Y = np.concatenate([xs * 0 - 30, xs * 0, xs * 0 + 30])
    Z = np.full_like(X, -3.5)
    return {"centerline": cl, "X": X, "Y": Y, "Z": Z, "bed_z": Z}


def _read(path):
    with open(path) as f:
        return f.read()


def _cfg(**kw):
    return B.ReachConfig(name="dredge_test", channel_width_m=60.0,
                         duration_s=7200.0, substance_class="sediment",
                         erodible_bed=True, dredging=True, **kw)


def test_scheduled_action_file_grammar(tmp_path):
    cfg = _cfg(dredge_mode="scheduled", dredge_volume_m3=4000.0)
    info = B.write_nestor_decks(cfg, _mesh(), str(tmp_path))
    act = _read(tmp_path / B.NESTOR_ACTION_FILENAME)
    assert "RESTART = F" in act                     # Fortran logical, not NO
    assert "ActionType      = Dig_by_time" in act
    assert "FieldDig        = 101_channel" in act    # leading digit 1-9
    assert "DigVolume       = 4000" in act
    assert act.rstrip().endswith("ENDFILE")
    # every date is exactly 19 chars yyyy.mm.dd-hh:mm:ss
    dates = re.findall(r"= (\d{4}\.\d\d\.\d\d-\d\d:\d\d:\d\d)\b", act)
    assert dates and all(len(d) == 19 for d in dates)
    assert info["surface_ref"] == B.NESTOR_SURFACE_REF_FILENAME  # mandatory


def test_scheduled_disposal_adds_dump_field(tmp_path):
    cfg = _cfg(dredge_mode="scheduled", dredge_disposal=True)
    info = B.write_nestor_decks(cfg, _mesh(), str(tmp_path))
    act = _read(tmp_path / B.NESTOR_ACTION_FILENAME)
    pol = _read(tmp_path / B.NESTOR_POLYGON_FILENAME)
    assert info["has_dump"] is True
    assert "FieldDump       = 102_spoil" in act       # dump field, first digit !=0
    assert "NAME 101_channel" in pol and "NAME 102_spoil" in pol
    assert pol.rstrip().endswith("ENDFILE")


def test_criterion_action_file_uses_sections(tmp_path):
    cfg = _cfg(dredge_mode="criterion", dredge_crit_depth_m=0.3,
               dredge_dig_depth_m=1.5, dredge_rate_m_per_s=5e-4)
    B.write_nestor_decks(cfg, _mesh(), str(tmp_path))
    act = _read(tmp_path / B.NESTOR_ACTION_FILENAME)
    assert "ActionType      = Dig_by_criterion" in act
    assert "ReferenceLevel  = SECTIONS" in act        # never GRID here
    for kw in ("TimeRepeat", "DigRate", "CritDepth", "DigDepth",
               "MinVolume", "MinVolumeRadius"):
        assert kw in act, kw


def test_polygon_names_have_nonzero_leading_digit(tmp_path):
    cfg = _cfg(dredge_disposal=True)
    B.write_nestor_decks(cfg, _mesh(), str(tmp_path))
    pol = _read(tmp_path / B.NESTOR_POLYGON_FILENAME)
    for name in re.findall(r"NAME (\S+)", pol):
        assert name[0] in "123456789", name           # ThreeDigitsNumeral rule


def test_surface_ref_seven_reals_and_end(tmp_path):
    cfg = _cfg(dredge_mode="criterion")
    B.write_nestor_decks(cfg, _mesh(), str(tmp_path))
    ref = _read(tmp_path / B.NESTOR_SURFACE_REF_FILENAME)
    data = [ln for ln in ref.splitlines()
            if ln.strip() and not ln.startswith("#") and not ln.startswith("END")]
    assert len(data) >= 2                              # >1 profile required
    for ln in data:
        assert len(ln.split()) == 7                    # x1 y1 z1 x2 y2 z2 km
    assert any(ln.startswith("END") for ln in ref.splitlines())


def test_gaia_deck_wires_nestor_keywords(tmp_path):
    cfg = _cfg(dredge_mode="scheduled")
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert "NESTOR                          = YES" in deck
    assert f"NESTOR ACTION FILE              = {B.NESTOR_ACTION_FILENAME}" in deck
    assert f"NESTOR POLYGON FILE             = {B.NESTOR_POLYGON_FILENAME}" in deck
    assert "NESTOR SURFACE REFERENCE FILE" in deck
    # dredging forces the erodible-bed path (NESTOR digs a real bed stock)
    assert "BED LOAD FOR ALL SANDS          = YES" in deck


def test_non_dredging_deck_has_no_nestor(tmp_path):
    cfg = B.ReachConfig(substance_class="sediment", erodible_bed=True,
                        workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert "NESTOR" not in deck                         # byte-identical to v2
