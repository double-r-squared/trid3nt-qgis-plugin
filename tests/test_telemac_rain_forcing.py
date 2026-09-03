"""TELEMAC distributed on-mesh rainfall / evaporation forcing (ADR 0190 row 1).

Server-side offline coverage for the composer's rain-resolution path: the
signed net-rate resolver, the gridMET-window parser, and the knob surfacing on
the template. The gridMET network fetch itself is exercised live (row 1 driver);
here the pure resolution logic is pinned.
"""
from __future__ import annotations

import inspect

import pytest

from trid3nt_server.workflows.telemac.river_dye import river_dye as M
from trid3nt_server.workflows.telemac.helpers import forcing as F


def _net(rain=None, evap=None, window=None):
    """The signed net rate + its note, off the declared rain producer."""
    out = F._rain_forcing(rain, evap, window)
    return out["mm_per_day"], out["note"]


def test_template_surfaces_rain_knobs():
    sig = inspect.signature(M.telemac_river_dye)
    for knob in ("rainfall_mm_per_day", "evaporation_mm_per_day",
                 "rainfall_gridmet_window"):
        assert knob in sig.parameters, f"missing template knob {knob}"


def test_resolver_none_when_no_forcing():
    rate, note = _net()
    assert rate is None
    assert note is None


def test_resolver_explicit_rain_positive():
    rate, note = _net(150.0)
    assert rate == pytest.approx(150.0)
    assert "net +150.0" in note


def test_resolver_rain_minus_evaporation():
    rate, _ = _net(100.0, 8.0)
    assert rate == pytest.approx(92.0)


def test_resolver_evaporation_only_is_negative():
    rate, note = _net(None, 12.0)
    assert rate == pytest.approx(-12.0)
    assert "net -12.0" in note


def test_resolver_clamps_absurd_rate():
    rate, _ = _net(999999.0)
    assert rate == pytest.approx(2000.0)  # storm ceiling


def test_window_parser_roundtrip():
    assert F._parse_gridmet_window("2017-08-25:2017-08-30") == (
        "2017-08-25", "2017-08-30")


def test_window_parser_rejects_malformed():
    with pytest.raises(F.TelemacDyeScenarioInputError):
        F._parse_gridmet_window("2017-08-25")
    with pytest.raises(F.TelemacDyeScenarioInputError):
        F._parse_gridmet_window("not-a-date:also-bad")


# --- the declared temporal transform on the ``rain`` DATA row --------------- #


def _rain_decl():
    from trid3nt_server.workflows.lib import data_rows

    return next(d for d in data_rows(M.DATA) if d.name == "rain")


def test_the_rain_declaration_states_its_cadence_and_units():
    spec = _rain_decl().producer.temporal
    assert spec.resample.to == "1D" and spec.resample.max_gap == "native*3"
    assert spec.units.units == "mm/day"


def test_the_declared_transform_stamps_the_note_without_moving_the_value():
    spec = _rain_decl().producer.temporal
    plain, _ = _net(150.0)
    out = F._rain_forcing(150.0, None, None, spec)
    assert out["mm_per_day"] == pytest.approx(plain)
    assert out["temporal_note"] == ("native 1D matches the declared 1D rate, "
                                    "no resample; units mm/day (declared, unchanged)")
    assert f"[{out['temporal_note']}]" in out["note"]


def test_an_undeclared_transform_leaves_the_note_byte_identical():
    with_spec = F._rain_forcing(150.0, None, None, None)
    assert with_spec["note"] == _net(150.0)[1]
    assert with_spec["temporal_note"] == ("native 1D rate, no resample declared")


def test_a_sub_daily_target_refuses_rather_than_manufacturing_a_storm_shape():
    from trid3nt_server.workflows.lib import ResampleSpec, TemporalShapeError, TemporalSpec

    with pytest.raises(TemporalShapeError):
        F._rain_forcing(150.0, None, None,
                        TemporalSpec(resample=ResampleSpec(to="1h")))


def test_a_unit_target_the_steering_cannot_carry_refuses():
    from trid3nt_server.workflows.lib import TemporalSpec, UnitsSpec

    with pytest.raises(F.TelemacDyeScenarioInputError):
        F._rain_forcing(150.0, None, None,
                        TemporalSpec(units=UnitsSpec("in/day")))


def test_the_transform_stamp_reaches_the_layers_provenance_row():
    from trid3nt_server.workflows.telemac.products import products as P

    out = F._rain_forcing(150.0, None, None,
                          _rain_decl().producer.temporal)
    row, = P._rain_provenance({"rain_mm_per_day": out["mm_per_day"],
                               "rain_rung": out["rung"],
                               "rain_note": out["note"]})
    assert row.param == "rain_or_evap_mm_per_day" and row.units == "mm/day"
    assert row.basis == "user" and "no resample" in row.note


def test_a_gridmet_rung_row_names_the_real_source():
    from trid3nt_server.workflows.telemac.products import products as P

    row, = P._rain_provenance({"rain_mm_per_day": 12.0,
                               "rain_rung": "gridmet_domain_mean",
                               "rain_note": "gridMET pr domain-mean"})
    assert row.basis == "fetched" and "gridMET" in row.real_source_if_any


def test_a_run_with_no_rain_carries_no_rain_row():
    from trid3nt_server.workflows.telemac.products import products as P

    assert P._rain_provenance({"rain_mm_per_day": None}) == []
