"""The settle fills its own keywords straight onto the sheet: no template line
routes them, a template's own stated value wins, and a sheet with a required
slot still empty refuses by name before anything is written."""

from __future__ import annotations

import asyncio
import importlib

import pytest

from trid3nt_server.workflows.telemac.modules import T2D
from trid3nt_server.workflows.telemac.modules.sheet import (SheetIncomplete,
                                                            fill, run)

_SETTLED = {"TITLE": "reach DOMAIN", "TIME_STEP": 0.7, "DURATION": 3600.0,
            "INITIAL_CONDITIONS": "CONSTANT DEPTH", "INITIAL_DEPTH": 1.2,
            "INITIAL_ELEVATION": 4.5}

_TEMPLATES = ("agitation", "bed_scour", "channel_dredging", "do_sag",
              "dye_release", "eutrophication", "ice_cover",
              "micropollutant_release", "nearshore_waves", "oil_spill",
              "rain_on_grid", "sediment_plume", "stratified_flow",
              "water_temperature", "wave_driven_currents")


def test_the_settle_s_six_values_reach_the_steering_file_with_no_template_line():
    class BARE(T2D):
        SOLVER = 1

    sheet = fill(BARE, settled=_SETTLED)
    written = dict(sheet.resolved())
    assert {keyword: written[keyword] for keyword in (
        "TITLE", "TIME STEP", "DURATION", "INITIAL CONDITIONS", "INITIAL DEPTH",
        "INITIAL ELEVATION")} == {
        "TITLE": "reach DOMAIN", "TIME STEP": 0.7, "DURATION": 3600.0,
        "INITIAL CONDITIONS": "CONSTANT DEPTH", "INITIAL DEPTH": 1.2,
        "INITIAL ELEVATION": 4.5}
    assert sheet.state()["filled"]["TIME_STEP"]["provenance"] \
        == "derived: the settle"


@pytest.mark.parametrize("name", _TEMPLATES)
def test_no_template_routes_a_settle_value_by_a_line_of_its_own(name):
    steering = importlib.import_module(
        f"trid3nt_server.workflows.telemac.templates.{name}.{name}").STEERING
    routed = [keyword for keyword in _SETTLED
              if "settled" in repr(steering.ASSERTED.get(keyword))]
    assert routed == []


def test_a_template_s_own_stated_value_wins_and_the_floor_beats_both():
    class STEPPED(T2D):
        TIME_STEP = 1.0

    sheet = fill(STEPPED, settled=_SETTLED)
    assert sheet.stated()["TIME_STEP"] == 1.0
    assert sheet.state()["filled"]["TIME_STEP"]["provenance"] == "template: STEPPED"
    assert fill(STEPPED, settled=_SETTLED, TIME_STEP=0.2).stated()["TIME_STEP"] \
        == 0.2


def test_a_settle_value_the_module_does_not_spell_is_not_written():
    class BARE(T2D):
        SOLVER = 1

    sheet = fill(BARE, settled={"NO_SUCH_KEYWORD": 1.0, "INITIAL_DEPTH": None})
    assert "NO_SUCH_KEYWORD" not in sheet.stated()
    assert "INITIAL_DEPTH" not in sheet.stated()


def test_a_required_slot_left_empty_refuses_by_name_before_writing(monkeypatch):
    from trid3nt_server.workflows.telemac.authoring import staging

    def _written(*_args, **_kwargs):
        raise AssertionError("nothing is written for an incomplete sheet")

    async def _never(**_kwargs):
        raise AssertionError("nothing dispatches on an incomplete sheet")

    monkeypatch.setattr(staging, "new_rundir", _written)
    sheet = fill(T2D, settled=_SETTLED, BOUNDARY_CONDITIONS_FILE="geo.cli")
    with pytest.raises(SheetIncomplete, match="GEOMETRY FILE"):
        asyncio.run(run(sheet, dispatch=_never, mesh_inputs=(), outputs=(),
                        results=("r2d.slf",), prefix="telemac", server_facts={}))
