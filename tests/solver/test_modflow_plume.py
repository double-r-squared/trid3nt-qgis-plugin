"""The plume template: every physics value it carries is stated, the source polygon is the recharge that carries the solute, and the wells are read at an instant."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime import fill as fill_mod
from trid3nt_server.workflows.runtime.fill import Fill
from trid3nt_server.workflows.runtime.sheet import fill

_STATED = {
    "drain_conductance": 100.0, "source_rate": 0.04, "source_concentration": 0.45,
    "background_concentration": 0.007, "porosity": 0.39,
    "longitudinal_dispersivity": 1.0, "transverse_dispersivity": 0.1,
    "snapshot": "1994-10-31", "solute": "Boron", "concentration_units": "mg/L",
    "until": "1994-10-31", "time_steps": 118, "input_mode": "auto"}
_BED_RING = {"type": "Polygon", "coordinates": [[[-70.547, 41.64], [-70.545, 41.64],
                                                  [-70.545, 41.641], [-70.547, 41.641],
                                                  [-70.547, 41.64]]]}


def _workflow():
    return TOOL_REGISTRY["modflow_plume"].fn.workflow


def _state(monkeypatch, stated):
    async def _produce(env, decl):
        return decl.name

    monkeypatch.setattr(fill_mod, "_produce", _produce)
    return asyncio.run(fill_mod.fill(Fill(workflow=_workflow()), stated))


def test_a_fully_stated_run_is_ready(monkeypatch):
    state = _state(monkeypatch, _STATED)
    assert state.ready, state.refusal()


@pytest.mark.parametrize("name", [
    "source_rate", "source_concentration", "background_concentration", "porosity",
    "longitudinal_dispersivity", "transverse_dispersivity", "snapshot", "solute",
    "concentration_units", "drain_conductance", "until", "time_steps"])
def test_each_value_the_template_does_not_state_refuses_by_name(monkeypatch, name):
    state = _state(monkeypatch, {k: v for k, v in _STATED.items() if k != name})
    assert not state.ready
    assert name in state.refusal()[1]


def test_the_template_states_no_number_of_its_own():
    steering = _workflow().steering
    stated = [v for v in (list(steering.ASSERTED.values())) if isinstance(v, (int, float))
              and not isinstance(v, bool)]
    # the only numeric keyword is the cell type, a switch
    assert stated == [1]


def test_the_source_polygon_is_the_recharge_that_carries_the_effluent_concentration():
    steering = _workflow().steering
    produced = {"domain": _BED_RING, "source": _BED_RING, "recharge": 5.0e-4,
                "saturated_thickness": 28.0, "transmissivity": 140.0,
                "water_table_depth": 2.0, "bed": 0.0, "streams": None,
                "event_time": "1936-01-01T00:00:00Z", **{
                    k: _STATED[k] for k in _STATED
                    if k not in ("snapshot", "solute", "concentration_units", "input_mode")}}
    sheet = fill(steering, produced=produced)
    natural, source = sheet.filled["RCH_STRESS_PERIOD_DATA"].value["statements"]
    assert natural["values"] == {"rate": 5.0e-4, "concentration": 0.007}
    assert source["values"] == {"rate": 0.04, "concentration": 0.45}
    assert source["geometry"] == _BED_RING and source["dimensions"]["concentration"] == "M/L3"
    assert sheet.filled["RCH_AUXILIARY"].value == ["CONCENTRATION"]
    # The clock: one period from event_time to until, over time_steps equal steps.
    assert sheet.filled["TDIS_NPER"].value == 1
    length, steps, mult = sheet.filled["TDIS_PERIODDATA"].value[0]
    assert steps == 118 and mult == 1.0
    assert length == pytest.approx(21488.5, abs=0.5)
    body = sheet.coupled[0]
    assert body["module"] == "gwt"
    assert "SSM_SOURCES" not in body["slots"]
    assert body["slots"]["MST_POROSITY"] == "porosity"


def test_the_wells_are_read_at_the_snapshot_over_every_site_for_the_solute():
    observe = next(row for row in _workflow().inputs if row.name == "observe")
    assert dict(observe.coercion) == {"near": None, "when": "snapshot", "every": True,
                                      "measured": "solute"}
    assert observe.data_class == "water quality sample" and observe.observes == "CONCENTRATION"


def test_the_run_carries_a_concentration_so_its_unit_is_required():
    workflow = _workflow()
    assert workflow.input("concentration_units").required
    assert workflow.published_units({"concentration_units": "mg/L"}) == {
        "CONCENTRATION": "mg/L"}
    assert workflow.unnamed() == ()
