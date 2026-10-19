"""An observation read at a stated instant, over a set of sites, in the unit a run states.

Offline. SYNTHETIC: the sites, their values and dates are hand stated for these unit
gates and claim nothing about any place. Covered: a row naming its own instant is
asked and read there and spans no forcing window, an unstated one reads at the run's
moment, an unreadable or unnamed one refuses by name; a stated set takes every site
in the window, the nearest alone by default; a MODFLOW run that compares a
concentration requires its unit and publishes under it.
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType, SimpleNamespace

import pytest

from trid3nt_server.errors import DeclarationError
from trid3nt_server.inputs.input import Input, declared
from trid3nt_server.inputs.observation import Observation, observation, sited
from trid3nt_server.inputs.slots import DOMAIN, OBSERVE
from trid3nt_server.inputs.user_input import WireArgsError
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.workflows.runtime.errors import StepFailedError

_START = "1936-01-01T00:00:00+00:00"


def _row(**need):
    return dataclasses.replace(
        Input(OBSERVE).need("water quality sample", **need), name="observe")


def _env(**run):
    env = fill._Env(data={}, moment="event_time", window_s=86400.0 * 365)
    env.run.update({"event_time": _START, **run})
    return env


def _site(site_id, lon, value, unit, date):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, 41.6]},
            "properties": {"site_id": site_id, "value": value, "unit": unit,
                           "result_date": date}}


def _wells():
    return {"type": "FeatureCollection", "features": [
        _site("FAR", -70.50, 400.0, "ug/L", "1994-05-20"),
        _site("NEAR", -70.56, 900.0, "ug/L", "1994-05-25"),
        _site("STALE", -70.55, 50.0, "ug/L", "1990-01-01")]}


def test_a_row_read_when_its_snapshot_says_is_read_there_over_no_window():
    told = fill._what_the_record_reports(
        _env(snapshot="1994-06-01"), _row(when="snapshot"), None)
    assert told["at"] == "1994-06-01T00:00:00+00:00"
    assert told["window_s"] is None


def test_an_unstated_snapshot_reads_at_the_run_s_moment():
    told = fill._what_the_record_reports(
        _env(snapshot=None), _row(when="snapshot"), None)
    assert told["at"] == _START
    assert told["window_s"] == 86400.0 * 365


def test_an_unreadable_snapshot_refuses_by_its_name():
    with pytest.raises(WireArgsError) as refused:
        fill._what_the_record_reports(
            _env(snapshot="mid-1994"), _row(when="snapshot"), None)
    assert "snapshot" in str(refused.value)


def test_a_snapshot_naming_no_input_refuses():
    with pytest.raises(StepFailedError) as refused:
        fill._what_the_record_reports(_env(), _row(when="snapshto"), None)
    assert refused.value.error_code == "INPUT_UNNAMED"


def test_the_reading_carries_the_instant_it_was_asked_at():
    found = observation(_wells(), near=(-70.56, 41.6), at="1994-06-01T00:00:00Z")
    assert found.site_id == "NEAR"
    assert found.asked == "1994-06-01T00:00:00+00:00"


def test_a_stated_set_takes_every_site_its_window_holds_nearest_first():
    found = observation(_wells(), near=(-70.56, 41.6), at="1994-06-01T00:00:00Z",
                        to_units="mg/L", every=True)
    assert isinstance(found, tuple) and sited(found)
    assert [one.site_id for one in found] == ["NEAR", "FAR"]
    assert [one.value for one in found] == pytest.approx([0.9, 0.4])
    assert {one.units for one in found} == {"mg/L"}


def test_the_nearest_site_alone_is_read_by_default():
    found = observation(_wells(), near=(-70.56, 41.6), at="1994-06-01T00:00:00Z")
    assert isinstance(found, Observation)


def test_an_instant_and_a_set_are_stated_on_the_need():
    assert _row(when="snapshot", every=True).coercion == {
        "near": None, "when": "snapshot", "every": True}
    assert dict(_row().coercion) == {"near": None}


def test_an_instant_or_a_set_on_a_row_read_off_no_record_refuses():
    with pytest.raises(DeclarationError):
        Input(DOMAIN).need("hydrography", every=True)
    with pytest.raises(DeclarationError):
        Input(DOMAIN).need("hydrography", when="snapshot")


def _workflow(*extra, transport: bool = False):
    from trid3nt_server.workflows.modflow.templates.water_table import water_table
    from trid3nt_server.workflows.modflow.workflow import ModflowWorkflow

    template = water_table
    if transport:
        steering = water_table.STEERING

        class carrier:
            """The template's body as one that also states a Transport."""
            ASSERTED = MappingProxyType({**steering.ASSERTED, "Transport": []})

            def __getattr__(self, name):
                return getattr(steering, name)

        carrier = carrier()
        template = SimpleNamespace(**{name: getattr(water_table, name) for name in dir(water_table)
                                      if not name.startswith("__") and name != "STEERING"}, STEERING=carrier)
    return ModflowWorkflow(metadata=SimpleNamespace(name="t", engine="modflow"),
                           inputs=[*declared(water_table.INPUTS), *extra],
                           template=template)


def test_a_run_carrying_a_concentration_requires_the_unit_it_is_counted_in():
    carries = _workflow(transport=True)
    assert carries.input("concentration_units").required
    assert carries.published_units({"concentration_units": "mg/L"}) == {
        "CONCENTRATION": "mg/L"}
    assert carries.published_units({}) == {}
    # A run carrying no concentration has no such input.
    assert "concentration_units" not in {row.name for row in _workflow().inputs}
