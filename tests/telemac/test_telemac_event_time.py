"""``event_time``: the moment a scenario is read at, seated once on the runtime.

Offline coverage that the lever reaches every template that takes it and that a
malformed moment is refused by the LEVER - typed, before any template sees the
value - rather than falling back to the latest cycle a source happens to hold.
"""
from __future__ import annotations

import inspect

import pytest

from trid3nt_server.workflows.telemac.templates.do_sag import do_sag as do_sag_mod
from trid3nt_server.workflows.telemac.templates.dye_release import dye_release as dye_mod


@pytest.mark.parametrize("mod, fn_name", [
    (do_sag_mod, "telemac_do_sag"), (dye_mod, "telemac_dye_release")])
def test_template_surfaces_event_time_knob(mod, fn_name):
    fn = getattr(mod, fn_name)
    assert "event_time" in inspect.signature(fn).parameters
    # The row is the RUNTIME's, seated onto the workflow rather than restated in
    # the template's own class body.
    rows = {p.name: p for p in fn.workflow.params}
    assert "event_time" in rows
    assert rows["event_time"].optional is True
    # unset leaves a derived-basis provenance row
    assert rows["event_time"].derived_when_absent


@pytest.mark.asyncio
@pytest.mark.parametrize("mod, fn_name", [
    (do_sag_mod, "telemac_do_sag"), (dye_mod, "telemac_dye_release")])
async def test_malformed_event_time_refuses_it_never_falls_back_to_latest(
        mod, fn_name):
    """A garbage event_time must not silently read the latest cycle instead."""
    fn = getattr(mod, fn_name)
    out = await fn(event_time="not-a-date")
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "EVENT_TIME_INVALID"
    assert "event_time" in out["error_message"]
