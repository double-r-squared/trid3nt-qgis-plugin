"""``event_time`` - the NWM discharge-cycle selector shared by telemac_do_sag and
telemac_river_dye (ADR 0309).

Offline coverage for the coercion (the outfall-coordinate precedent: garbage
refuses typed, never falls back), the door threading onto both templates, and
the discharge resolver's cycle-pinning (a "latest" request never rides
unpinned into provenance). The live NWM fetch itself is exercised by the A/B
driver; here the pure resolution logic and the wire-arg refusal are pinned.
"""
from __future__ import annotations

import inspect

import pytest

from trid3nt_server.workflows.lib import param_rows
from trid3nt_server.workflows.telemac.do_sag import do_sag as do_sag_mod
from trid3nt_server.workflows.telemac.river_dye import river_dye as dye_mod
from trid3nt_server.workflows.telemac.steps import forcing as F


# --- coerce_event_time: the outfall-coercion precedent ----------------------- #
def test_coerce_event_time_absent_is_latest():
    assert F.coerce_event_time(None) is None
    assert F.coerce_event_time("") is None


def test_coerce_event_time_accepts_a_bare_date():
    out = F.coerce_event_time("2026-08-20")
    assert out.startswith("2026-08-20T00:00:00")


def test_coerce_event_time_accepts_a_zulu_datetime():
    out = F.coerce_event_time("2026-08-20T06:00:00Z")
    assert out.startswith("2026-08-20T06:00:00")


def test_coerce_event_time_rejects_garbage_it_never_falls_back_to_latest():
    with pytest.raises(F.TelemacDyeScenarioError) as ei:
        F.coerce_event_time("last tuesday")
    assert ei.value.error_code == "TELEMAC_PARAMS_INVALID"
    assert "event_time" in str(ei.value)


# --- both templates surface the knob ----------------------------------------- #
@pytest.mark.parametrize("mod, fn_name", [
    (do_sag_mod, "telemac_do_sag"), (dye_mod, "telemac_river_dye")])
def test_template_surfaces_event_time_knob(mod, fn_name):
    fn = getattr(mod, fn_name)
    assert "event_time" in inspect.signature(fn).parameters
    assert "event_time" in {p.name for p in param_rows(mod.PARAMS)}
    row = next(p for p in param_rows(mod.PARAMS) if p.name == "event_time")
    assert row.optional is True
    assert row.derived_when_absent  # unset leaves a derived-basis provenance row


@pytest.mark.asyncio
@pytest.mark.parametrize("mod, fn_name, extra", [
    (do_sag_mod, "telemac_do_sag", {}),
    (dye_mod, "telemac_river_dye", {}),
])
async def test_malformed_event_time_refuses_it_never_falls_back_to_latest(
        mod, fn_name, extra):
    """A garbage event_time must not silently read the latest cycle instead."""
    fn = getattr(mod, fn_name)
    out = await fn(location="Eel River near Scotia, California",
                   event_time="not-a-date", **extra)
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC_PARAMS_INVALID"
    assert "event_time" in out["error_message"]


# --- resolve_carrier_discharge: threading + cycle pinning -------------------- #
@pytest.mark.asyncio
async def test_resolve_carrier_discharge_threads_event_time_to_the_fetch(monkeypatch):
    captured: dict = {}

    def _fake(lon, lat, valid_time=None):
        captured["valid_time"] = valid_time
        return {"m3s": 5.0, "reference_time": "2026-08-01T00:00:00+00:00",
                "product": "analysis_assim", "layer": None}

    monkeypatch.setattr(F, "_nwm_nearest_streamflow", _fake)
    await F.resolve_carrier_discharge(seed={"lon": -124.1, "lat": 40.5},
                                      explicit=None,
                                      event_time="2026-08-01T00:00:00+00:00")
    assert captured["valid_time"] == "2026-08-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_default_latest_pins_the_resolved_cycle_never_bare_latest(monkeypatch):
    """EITHER WAY (set or unset) the note pins the RESOLVED timestamp - a
    "latest" request never rides unpinned into provenance."""
    def _fake(lon, lat, valid_time=None):
        assert valid_time is None  # unset event_time -> unset valid_time (latest)
        return {"m3s": 2.1, "reference_time": "2026-08-24T01:00:00+00:00",
                "product": "analysis_assim", "layer": None}

    monkeypatch.setattr(F, "_nwm_nearest_streamflow", _fake)
    out = await F.resolve_carrier_discharge(seed={"lon": -124.1, "lat": 40.5},
                                            explicit=None, event_time=None)
    assert out["reference_time"] == "2026-08-24T01:00:00+00:00"
    assert out["product"] == "analysis_assim"
    assert "2026-08-24T01:00Z" in out["note"]
    assert "latest" not in out["note"]


@pytest.mark.asyncio
async def test_explicit_event_time_pins_its_own_resolved_cycle(monkeypatch):
    def _fake(lon, lat, valid_time=None):
        return {"m3s": 3.4, "reference_time": "2026-08-10T06:00:00+00:00",
                "product": "analysis_assim", "layer": None}

    monkeypatch.setattr(F, "_nwm_nearest_streamflow", _fake)
    out = await F.resolve_carrier_discharge(
        seed={"lon": -124.1, "lat": 40.5}, explicit=None,
        event_time="2026-08-10T06:00:00Z")
    assert out["reference_time"] == "2026-08-10T06:00:00+00:00"
    assert "2026-08-10T06:00Z" in out["note"]


@pytest.mark.asyncio
async def test_out_of_retention_event_time_refuses_typed(monkeypatch):
    """A cycle outside the ~30-day NWM PDS retention window is a MISS, not a
    fabricated discharge - the refusal names the retention bound."""
    monkeypatch.setattr(F, "_nwm_nearest_streamflow",
                        lambda lon, lat, valid_time=None: None)
    with pytest.raises(F.TelemacDyeScenarioError) as ei:
        await F.resolve_carrier_discharge(
            seed={"lon": -124.1, "lat": 40.5}, explicit=None,
            event_time="2020-01-01T00:00:00Z")
    assert ei.value.error_code == "TELEMAC_DISCHARGE_INPUT_REQUIRED"
    assert "retention" in str(ei.value) or "30 days" in str(ei.value)
    assert "2020-01-01" in str(ei.value)


@pytest.mark.asyncio
async def test_a_user_supplied_discharge_never_calls_the_fetch(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("explicit discharge must short-circuit the NWM fetch")

    monkeypatch.setattr(F, "_nwm_nearest_streamflow", _boom)
    out = await F.resolve_carrier_discharge(
        seed={"lon": -124.1, "lat": 40.5}, explicit=850.0,
        event_time="2026-08-10T06:00:00Z")
    assert out["basis"] == "user" and out["m3s"] == 850.0
    assert out["reference_time"] is None
