"""The reach a river domain is cut to is an input the call can state; the template's span is its default."""

from __future__ import annotations

import pytest
from shapely.geometry import shape

from trid3nt_server.inputs.domain import domain
from trid3nt_server.inputs.slots import DOMAIN
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime.fill import _Env, _span_km
from trid3nt_server.workflows.runtime.workflow import REACH_LENGTH

_SPANS = {
    "telemac_bed_scour": 6.0, "telemac_dye_release": 6.0, "telemac_sediment_plume": 6.0,
    "telemac_micropollutant_release": 6.0, "telemac_oil_spill": 6.0,
    "telemac_water_temperature": 12.0, "telemac_eutrophication": 12.0,
    "telemac_ice_cover": 12.0, "telemac_do_sag": 12.0,
    "telemac_channel_dredging": 2.0, "telemac_rain_on_grid": 15.0}

_BANK = {"type": "Polygon", "coordinates": [[
    [-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01], [-83.50, 34.99]]]}


def _row(name):
    workflow = TOOL_REGISTRY[name].fn.workflow
    return workflow, next(r for r in workflow.inputs if r.slot is DOMAIN)


@pytest.mark.parametrize("name,span", sorted(_SPANS.items()))
def test_every_span_stating_template_lists_the_reach_with_its_own_default(name, span):
    workflow, _row_ = _row(name)
    reach = next(r for r in workflow.inputs if r.name == REACH_LENGTH.name)
    assert reach.stated_only and f"reaches {span:g} km" in reach.desc


def test_a_template_stating_no_span_lists_no_reach():
    workflow = TOOL_REGISTRY["telemac3d_stratified_flow"].fn.workflow
    assert all(r.name != REACH_LENGTH.name for r in workflow.inputs)


def test_unstated_the_templates_span_stands():
    _workflow, row = _row("telemac_dye_release")
    assert _span_km(_Env(data={}, run={}), row) == 6.0


def test_a_stated_reach_wins_over_the_templates_span():
    _workflow, row = _row("telemac_dye_release")
    assert _span_km(_Env(data={}, run={"reach_length_km": 2.0}), row) == 2.0


@pytest.mark.parametrize("bad", [0, 0.0, -3.0])
def test_a_reach_that_is_not_positive_refuses_naming_the_range(bad):
    _workflow, row = _row("telemac_dye_release")
    with pytest.raises(UserInputError) as caught:
        _span_km(_Env(data={}, run={"reach_length_km": bad}), row)
    assert caught.value.error_code == "REACH_LENGTH_INVALID"
    assert "reach_length_km" in str(caught.value) and "greater than 0" in str(caught.value)


def test_a_stated_reach_cuts_the_walked_domain_to_that_length():
    _workflow, row = _row("telemac_dye_release")
    line = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {},
            "geometry": {"type": "LineString", "coordinates": [[-83.49, 35.0], [-83.31, 35.0]]}}]}
    banks = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": _BANK}]}

    def east(run):
        span = _span_km(_Env(data={}, run=run), row)
        return shape(domain(line, banks=banks, near=(-83.47, 35.0), span_km=span).geometry).bounds[2]

    assert east({"reach_length_km": 2.0}) < east({}) < -83.31
