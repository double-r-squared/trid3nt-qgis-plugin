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

_BBOX = (-95.55, 29.70, -95.30, 29.95)  # Buffalo Bayou, Houston


def test_template_surfaces_rain_knobs():
    sig = inspect.signature(M.telemac_river_dye)
    for knob in ("rainfall_mm_per_day", "evaporation_mm_per_day",
                 "rainfall_gridmet_window"):
        assert knob in sig.parameters, f"missing template knob {knob}"


def test_resolver_none_when_no_forcing():
    rate, note = M._resolve_rain_or_evap_mm_per_day(None, None, None, _BBOX)
    assert rate is None
    assert note is None


def test_resolver_explicit_rain_positive():
    rate, note = M._resolve_rain_or_evap_mm_per_day(150.0, None, None, _BBOX)
    assert rate == pytest.approx(150.0)
    assert "net +150.0" in note


def test_resolver_rain_minus_evaporation():
    rate, _ = M._resolve_rain_or_evap_mm_per_day(100.0, 8.0, None, _BBOX)
    assert rate == pytest.approx(92.0)


def test_resolver_evaporation_only_is_negative():
    rate, note = M._resolve_rain_or_evap_mm_per_day(None, 12.0, None, _BBOX)
    assert rate == pytest.approx(-12.0)
    assert "net -12.0" in note


def test_resolver_clamps_absurd_rate():
    rate, _ = M._resolve_rain_or_evap_mm_per_day(999999.0, None, None, _BBOX)
    assert rate == pytest.approx(2000.0)  # storm ceiling


def test_window_parser_roundtrip():
    assert M._parse_gridmet_window("2017-08-25:2017-08-30") == (
        "2017-08-25", "2017-08-30")


def test_window_parser_rejects_malformed():
    with pytest.raises(M.TelemacDyeScenarioInputError):
        M._parse_gridmet_window("2017-08-25")
    with pytest.raises(M.TelemacDyeScenarioInputError):
        M._parse_gridmet_window("not-a-date:also-bad")
