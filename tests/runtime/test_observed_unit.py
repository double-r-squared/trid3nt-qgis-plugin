"""What an OBSERVE row is read in: the unit of the variable it observes.

Offline. A row states no unit at all now - it NAMES the published variable its
record is a measurement of, and the run's own published table says what that
variable is written in. Covered: a degC record read against a DEGC tracer, the
refusal when nothing the run publishes under that name states a unit, the
published table read off a deck's 32-character tracer text and off the rows a
coupled module appends.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trid3nt_server.inputs.observation import convert, observation
from trid3nt_server.workflows.runtime import Data
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.workflows.runtime.errors import PlanValidationError

_METADATA = SimpleNamespace(name="t", description="", tags=())


def _row(name: str, **need):
    import dataclasses

    return dataclasses.replace(Data.need("water quality sample", **need),
                               name=name)


def _env(**published):
    return fill._Env(params=None, data={}, results={},
                            published_units=dict(published))


def _sample(value: float, unit: str) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Point", "geometry": {"type": "Point",
                                       "coordinates": [-122.68, 45.51]},
         "properties": {"site_id": "A", "site_name": "A site", "value": value,
                        "unit": unit, "result_date": "2026-03-04"}}]}


def test_a_degc_record_is_read_against_a_degc_tracer() -> None:
    env = _env(TEMPERATURE="DEGC", H="m")
    told = fill._what_the_run_calls_it(
        env, _row("observe", of="TEMPERATURE"), "", "TEMPERATURE")
    assert told["to_units"] == "DEGC"
    # The record spells the same unit its own way, and the two are one unit.
    found = observation(_sample(9.5, "deg C"), to_units=told["to_units"],
                        caption="a water temperature")
    assert found.value == pytest.approx(9.5)
    assert found.units == "DEGC"


def test_a_fahrenheit_record_reaches_the_tracer_s_own_scale() -> None:
    told = fill._what_the_run_calls_it(
        _env(TEMPERATURE="DEGC"), _row("observe", of="TEMPERATURE"), "",
        "TEMPERATURE")
    found = observation(_sample(49.1, "degF"), to_units=told["to_units"])
    assert found.value == pytest.approx((49.1 - 32.0) / 1.8)


def test_a_variable_with_no_unit_anywhere_refuses_rather_than_reading_the_record()\
        -> None:
    env = _env(TEMPERATURE="", H="m")
    with pytest.raises(PlanValidationError) as raised:
        fill._what_the_run_calls_it(
            env, _row("observe", of="TEMPERATURE"), "", "TEMPERATURE")
    said = str(raised.value)
    assert "observes 'TEMPERATURE'" in said and "H" in said


def test_a_row_that_observes_nothing_published_is_read_by_its_role() -> None:
    env = _env(TEMPERATURE="DEGC")
    env.slot_units = {"level": "m"}
    import dataclasses

    row = dataclasses.replace(Data.need("water level series"), name="level")
    assert fill._what_the_run_calls_it(
        env, row, "", "")["to_units"] == "m"


def test_the_row_states_no_unit_at_all() -> None:
    with pytest.raises(TypeError):
        Data.need("water quality sample", units="degC")
    assert "to_units" not in Data.need("water quality sample", of="T1").coercion


def test_a_row_observes_only_what_it_went_out_and_measured() -> None:
    with pytest.raises(PlanValidationError):
        Data.need("", of="TEMPERATURE")


def test_the_published_table_is_the_deck_s_own_text_and_its_coupled_rows() -> None:
    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow
    from trid3nt_server.workflows.telemac.templates.water_temperature import (
        water_temperature as warmed)
    from trid3nt_server.workflows.telemac.templates.ice_cover import (
        ice_cover as frozen)

    for template, unit in ((warmed, "DEGC"), (frozen, "oC")):
        published = TelemacWorkflow(
            metadata=_METADATA, params=template.PARAMS, template=template,
            data=template.DATA,
            levers=TelemacWorkflow.levers()).published_units()
        # The deck names its own tracer and keeps its unit; KHIONE appends one
        # behind a deck that names none, and the unit is the module's.
        assert published["TEMPERATURE"] == unit
        assert convert(9.5, "deg C", unit) == pytest.approx(9.5)
