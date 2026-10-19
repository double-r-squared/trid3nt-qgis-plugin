"""A station is the data's site: the series reads where the record was taken.

Offline: the result a read opens is stated through the ``telemac_result``
fixture, the publisher is stood in for, and what is proved is where the series
is read from, what a site that misses the mesh does, that a template refuses a
station argument as unknown, and that a run whose only matched buoy is the one
it was forced with drops the circular read and says so."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from trid3nt_server.inputs.observation import Observation
from trid3nt_server.render.formats import Published
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.runtime.fill import (
    REJECTED,
    Fill,
    fill,
)
from trid3nt_server.workflows.telemac.modules import T2D, series
from trid3nt_server.workflows.telemac import workflow as door


def _reach(telemac_result) -> dict[str, Any]:
    """Five nodes in UTM zone 10, four frames: the tracer arrives, peaks, drains."""
    x = [500000.0, 500120.0, 500000.0, 500120.0, 500060.0]
    y = [4400000.0, 4400000.0, 44000110.0, 44000110.0, 44000055.0]
    ikle = [[0, 1, 4], [1, 3, 4], [2, 3, 4], [0, 2, 4]]
    dye = [[0.0, 0.0, 0.0, 0.0, 0.0],
           [10.0, 80.0, 5.0, 40.0, 60.0],
           [2.0, 30.0, 1.0, 50.0, 20.0],
           [0.0, 0.0, 0.0, 0.0, 0.0]]
    return telemac_result(varnames=["WATER DEPTH", "DYE"],
                          x=x, y=y, ikle=ikle, times=[0.0, 60.0, 120.0, 180.0],
                          data={"WATER DEPTH": [[2.0] * 5] * 4, "DYE": dye})


@pytest.fixture()
def solved(monkeypatch, telemac_result):
    _reach(telemac_result)
    monkeypatch.setattr(
        "trid3nt_server.workflows.solver.solver.download_result",
        lambda run_id, basename, error_code=None: "/tmp/does-not-matter.slf")
    from trid3nt_server.workflows.telemac.modules.outputs import Solved

    return Solved({"run_id": "RID", "utm_epsg": 32610,
                   "result_basename": "r2d.slf",
                   "tracer_names": ["DYE             MG/L"], "name": "reach",
                   "module": "telemac2d",
                   "reference_time": "2026-01-01T00:00:00+00:00"}, T2D)


@pytest.fixture()
def published(monkeypatch):
    """The publisher's own reply, captured: what the run's record got."""
    from trid3nt_contracts.execution import LayerURI

    seen: dict[str, Any] = {}

    async def _publish(**kwargs):
        seen.update(kwargs)
        return Published(layers=(LayerURI(
            layer_id="L", name="Water temperature (reach)",
            layer_type="raster", uri="s3://runs/RID/temperature.tif",
            quantity="water_temperature"),), charts={})

    monkeypatch.setattr("trid3nt_server.workflows.runtime.primitives.publish",
                        _publish)
    return seen


def _water_temperature_outputs(obs: Observation, solved):
    """telemac_water_temperature's own OUTPUTS, charted and as a station."""
    return ([series("T1", at="observe").chart(),
             series("T1", at="observe").station()],
            [{"at": obs, "along": None}, {"at": obs, "along": None}])


def _publish(run, outputs, anchors, solved, **kwargs):
    return asyncio.run(door.publish_outputs(
        run=run, outputs=outputs, anchors=anchors,
        captions={"T1": "water temperature"},
        params={"location": "reach", **kwargs}))


def _gauge(solved, *, site_id: str = "SCOC1") -> Observation:
    """The matched gauge, standing on the mesh's second node."""
    lon, lat = solved.lonlat
    return Observation(value=22.0, units="deg C", site_id=site_id,
                       site_name="Siuslaw gauge", lon=float(lon[1]),
                       lat=float(lat[1]))


def test_the_series_is_read_at_the_matched_gauges_site(solved, published):
    """water_temperature's T1 answers at the gauge's own site: the node the
    result file holds the gauge at, in the chart and on the station."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    obs = _gauge(solved)
    outputs, anchors = _water_temperature_outputs(obs, solved)
    token = bind_notes()
    result = _publish(solved.run, outputs, anchors, solved)
    drain_notes(token)
    charts = [d for d in published["items"]
              if type(d.product).__name__ == "Chart"]
    assert charts, "the gauge's series published no chart"
    rows = charts[0].product.payload["vega_lite_spec"]["data"]["values"]
    assert [row["value"] for row in rows] == [0.0, 80.0, 30.0, 0.0]
    lon, lat = solved.lonlat
    station = [d for d in published["items"]
               if type(d.product).__name__ == "Vector"]
    assert station, "the gauge's series published no station point"
    coordinates = station[0].product.features["features"][0]["geometry"][
        "coordinates"]
    assert coordinates == [pytest.approx(float(lon[1]), abs=1e-6),
                           pytest.approx(float(lat[1]), abs=1e-6)]
    assert result.layer_id == "telemac-RID"


def test_a_gauge_off_the_mesh_composes_no_series_and_journals_the_miss(
        solved, published):
    """A gauge whose site the mesh does not hold composes nothing and says so
    on the journal with its site and its distance; the run does not refuse."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    far = Observation(value=22.0, units="deg C", site_id="FAR-OUT",
                      site_name="the far gauge",
                      lon=-122.9, lat=45.9)
    outputs, anchors = _water_temperature_outputs(far, solved)
    token = bind_notes()
    _publish(solved.run, outputs, anchors, solved)
    said = drain_notes(token)
    assert not [d for d in published["items"]
                if type(d.product).__name__ == "Chart"], \
        "an off-mesh gauge still composed a series"
    assert not [d for d in published["items"]
                if type(d.product).__name__ == "Vector"], \
        "an off-mesh gauge still composed a station point"
    joined = " ".join(said)
    assert "FAR-OUT" in joined, joined
    assert "outside the mesh" in joined or "does not hold" in joined, joined


def test_a_gauge_stated_as_a_number_composes_no_series_and_journals_the_miss(
        solved, published):
    """No site matched reads nothing: an observation stated as a number stands
    nowhere, so no series is composed and the journal says so."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes

    placeless = Observation(value=22.0, units="deg C", site_id=None,
                            site_name=None, lon=None, lat=None)
    outputs, anchors = _water_temperature_outputs(placeless, solved)
    token = bind_notes()
    _publish(solved.run, outputs, anchors, solved)
    said = drain_notes(token)
    assert not [d for d in published["items"]
                if type(d.product).__name__ in ("Chart", "Vector")], \
        "an observation with no site still composed a series"
    assert any("no site" in note for note in said), said


@pytest.mark.parametrize(("tool", "argument"), [
    ("telemac_water_temperature", "station"),
    ("telemac_micropollutant_release", "monitoring" + "_point"),
])
def test_a_station_argument_refuses_as_unknown(tool, argument):
    """The template takes no station argument: a call carrying one is refused
    by name before anything else answers."""
    workflow = TOOL_REGISTRY[tool].fn.workflow
    state = asyncio.run(fill(Fill(workflow=workflow),
                             {argument: [-123.0, 45.0]}))
    verdict = state.inputs[argument]
    assert (verdict.state, verdict.code) == (REJECTED, "INPUT_UNNAMED")
    assert f"{argument!r} is not an input of {tool}" in verdict.reason
    assert not state.ready


def _sea_state(site_id: str):
    from trid3nt_server.inputs.wave import Wave

    return Wave(height_m=1.6, peak_period_s=8.0, from_direction_deg=220.0,
                site_id=site_id)


def _matched(site_id: str) -> Observation:
    return Observation(value=1.5, units="m", site_id=site_id,
                       site_name="the matched buoy",
                       lon=-75.714, lat=36.200)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["tomawac_nearshore_waves",
                                  "tomawac_wave_driven_currents"])
async def test_the_only_buoy_matched_is_the_one_the_run_was_forced_with(tool):
    """The observe row matched the same station the sea state came from, so a
    read there would answer with what the run was handed: the observation is
    dropped from the run's reads, taken out of what may be charted, and the
    journal says why there is no independent check."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
    from trid3nt_server.workflows.runtime.fill import production

    workflow = TOOL_REGISTRY[tool].fn.workflow
    env = production(Fill(workflow=workflow))
    run = {"wave": _sea_state("44056"), "observe": _matched("44056")}
    untaken: set[str] = set()
    token = bind_notes()
    await workflow._no_independent_check(env, run, untaken)
    said = drain_notes(token)
    assert run["observe"] is None
    assert untaken == {"observe"}
    assert any("forced with" in note and "independent" in note for note in said), \
        said


@pytest.mark.asyncio
async def test_a_buoy_other_than_the_forcing_one_is_kept_for_the_run_to_read_at():
    """The observe row matched a different station than the sea state came from,
    so the observation stays held for every read and chart to answer at."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
    from trid3nt_server.workflows.runtime.fill import production

    workflow = TOOL_REGISTRY["tomawac_nearshore_waves"].fn.workflow
    env = production(Fill(workflow=workflow))
    independent = _matched("44056")
    run = {"wave": _sea_state("44100"), "observe": independent}
    untaken: set[str] = set()
    token = bind_notes()
    await workflow._no_independent_check(env, run, untaken)
    said = drain_notes(token)
    assert run["observe"] is independent
    assert untaken == set()
    assert not [note for note in said if "forced with" in note], said


@pytest.mark.asyncio
async def test_a_run_that_has_not_held_the_buoy_reads_it_before_dropping_it(
        monkeypatch):
    """The coupled host holds no wave-series fill of its own, so the drop reads
    the observe row the way any other read does - and drops that result too when
    it turns out to be the forcing station itself."""
    from trid3nt_server.inputs.run_notes import bind_notes, drain_notes
    from trid3nt_server.workflows.runtime import fill as fill_mod
    from trid3nt_server.workflows.runtime.fill import production

    async def _buoy(_env, _row) -> Observation:
        return _matched("44097")

    monkeypatch.setattr(fill_mod, "_produce", _buoy)
    workflow = TOOL_REGISTRY["tomawac_wave_driven_currents"].fn.workflow
    env = production(Fill(workflow=workflow))
    run = {"wave": _sea_state("44097")}
    untaken: set[str] = set()
    token = bind_notes()
    await workflow._no_independent_check(env, run, untaken)
    said = drain_notes(token)
    assert run["observe"] is None
    assert untaken == {"observe"}
    assert any("forced with" in note for note in said), said
