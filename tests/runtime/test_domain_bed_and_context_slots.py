"""The engine-neutral slots on the runtime: the domain, the bed, the runs, and
the context row.

Offline: the declarations are values, so what is proved is what a body DECLARES,
what reaches the wire, and what a run says when a context source held nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from trid3nt_server.inputs.input import Input, declared
from trid3nt_server.workflows.runtime import DeclarationError, tool
from trid3nt_server.workflows.runtime.data import refuse_wrong_shape
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LINE, OBSERVE


def test_a_domain_row_is_one_slot_however_it_is_filled():
    """A drawn polygon, the user's layer and a producer fill ONE row: the slot is
    on the wire AND names its producer, and nothing downstream reads which."""
    class DRAWN:
        domain = Input(DOMAIN).need(geometry="polygon")

    class FETCHED:
        domain = Input(DOMAIN, producer=tool("fetch_nhd_waterbody_at_point", seed_point=[1.0, 2.0]))

    drawn, fetched = declared(DRAWN)[0], declared(FETCHED)[0]
    assert drawn.slot == fetched.slot == DOMAIN
    # BOTH are on the wire: what the user hands in supersedes the producer.
    assert drawn.fills_from_user and fetched.fills_from_user
    assert fetched.producer.runner == "fetch_nhd_waterbody_at_point"


def test_a_bed_row_takes_every_source_a_survey_arrives_in_but_a_mesh():
    class DATA:
        bed = Input(BED).need("bathymetry")

    row = declared(DATA)[0]
    assert row.slot == BED and row.fills_from_user
    # A bathymetry is measured as a raster AND as soundings, so both are the
    # class handed in; a stated depth is the number itself and has no shape.
    for supplied in ("s3://b/k/survey.tif", "s3://b/k/soundings.geojson", 2.0):
        refuse_wrong_shape(row, supplied)
    with pytest.raises(Exception, match="reads as mesh"):
        refuse_wrong_shape(row, "s3://b/k/solved.slf")


def test_a_terrain_row_refuses_the_shape_no_source_of_terrain_publishes():
    """The supplied twin is the CLASS's: terrain is measured as a surface and
    nothing publishes it as a point layer, so soundings are not a terrain."""
    class DATA:
        bed = Input(BED).need("terrain")

    row = declared(DATA)[0]
    refuse_wrong_shape(row, "s3://b/k/ground.tif")
    with pytest.raises(Exception, match="reads as vector"):
        refuse_wrong_shape(row, "s3://b/k/soundings.geojson")


def test_a_bare_input_is_a_value_and_a_value_states_its_own():
    """With no slot, need, producer or shape, an Input is a VALUE - and a value
    with no prose refuses the declaration."""
    with pytest.raises(DeclarationError, match="declares no desc"):
        class DATA:
            row = Input()

        declared(DATA)


#: What a reach producer returns: the section it cut, and the two faces it was
#: cut between, each row naming which stretch it is.
_REACH = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"part": "domain", "name": "a river"},
     "geometry": {"type": "Polygon",
                  "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0],
                                   [0.0, 1.0], [0.0, 0.0]]]}},
    {"type": "Feature", "properties": {"part": "inflow", "type": "inflow"},
     "geometry": {"type": "LineString", "coordinates": [[0.0, 0.0], [0.0, 1.0]]}},
    {"type": "Feature", "properties": {"part": "outflow", "type": "outflow"},
     "geometry": {"type": "LineString", "coordinates": [[1.0, 0.0], [1.0, 1.0]]}}]}


class _RIVER:
    """A question whose domain producer measures the edge it cuts between and
    the line down the middle of what it cut."""

    domain = Input(DOMAIN, producer=tool("fetch_river_reach", seed_point=[0.5, 0.5]))
    line = Input(LINE).need(geometry="polyline")


def _env_over(value, monkeypatch, body=_RIVER):
    from trid3nt_server.workflows.runtime import fill

    async def _answered(env, producer, label):
        return value

    monkeypatch.setattr(fill, "_produced", _answered)
    rows = {row.name: row for row in declared(body)}
    return fill, fill._Env(data=rows)


def test_the_line_slot_is_filled_by_the_domains_own_centerline(monkeypatch):
    """A placed profile reads a LINE, and the reach producer measured one beside
    its polygon: the template restates nothing to be given it."""
    reach = {"type": "FeatureCollection", "features": [
        *_REACH["features"],
        {"type": "Feature", "properties": {"part": "centerline"},
         "geometry": {"type": "LineString",
                      "coordinates": [[0.1, 0.5], [0.9, 0.5]]}}]}
    fill, env = _env_over(reach, monkeypatch)
    line = asyncio.run(fill._produce(env, env.data["line"]))
    assert line == {"type": "LineString", "coordinates": [[0.1, 0.5], [0.9, 0.5]]}


def test_a_domain_with_no_centerline_leaves_the_line_to_be_drawn(monkeypatch):
    """A lake has no companion line down it. With no canvas to ask, the slot is
    unsatisfied and says so - which is a user drawing the line they mean."""
    fill, env = _env_over(_REACH, monkeypatch)
    with pytest.raises(Exception, match="DATA_SLOT_UNSATISFIED|producer-less"):
        asyncio.run(fill._produce(env, env.data["line"]))


def test_a_line_the_user_draws_is_read_as_one_geometry(monkeypatch):
    """Every shape a line arrives in reads the same after the slot: a drawn
    collection, a geometry, or the vertices themselves."""
    fill, env = _env_over(_REACH, monkeypatch)
    env.supplied["line"] = [[0.2, 0.2], [0.8, 0.8]]
    assert asyncio.run(fill._produce(env, env.data["line"])) == {
        "type": "LineString", "coordinates": [[0.2, 0.2], [0.8, 0.8]]}


def test_a_malformed_ask_on_a_producer_row_refuses_rather_than_reading_absent(
        monkeypatch):
    """A window, a bbox or a unit the caller stated wrong is the ASK being wrong,
    and a run that swallowed it would report "nothing was there" about a question
    nobody managed to put."""
    from trid3nt_server.tools.fetchers._router.errors import router_input_error
    from trid3nt_server.workflows.runtime import fill

    class DATA:
        survey = Input(tool("fetch_ehydro_surveys"))

    async def _refused(env, producer, label):
        raise router_input_error("EHYDRO", "31 surveys end on or after "
                                 "2015-01-01 over this extent; the window is "
                                 "the ask's", "INPUT_INVALID")

    monkeypatch.setattr(fill, "_produced", _refused)
    env = fill._Env(data={})
    with pytest.raises(Exception) as exc:
        asyncio.run(fill._produce(env, declared(DATA)[0]))
    assert exc.value.error_code == "EHYDRO_INPUT_INVALID"


def test_a_malformed_value_handed_to_a_producer_rows_slot_refuses_too(monkeypatch):
    """A value the slot cannot read as what it is asked for is the same wrong ask."""
    from trid3nt_server.inputs.user_input import UserInputError
    from trid3nt_server.workflows.runtime import fill

    class DATA:
        sample = Input(tool("fetch_usgs_water_quality"))

    async def _answered(env, producer, label):
        raise UserInputError("a reading is a number or a layer of sites",
                             code="OBSERVATION_INVALID")

    monkeypatch.setattr(fill, "_produced", _answered)
    env = fill._Env(data={})
    row = declared(DATA)[0]
    with pytest.raises(UserInputError) as exc:
        asyncio.run(fill._produce(env, row))
    assert exc.value.error_code == "OBSERVATION_INVALID"


def test_a_hard_producer_row_still_refuses_when_its_source_is_empty(monkeypatch):
    from trid3nt_server.workflows.runtime import fill

    class DATA:
        sample = tool("fetch_usgs_water_quality")

    async def _empty(env, producer, label):
        raise RuntimeError("WQP_NO_SITES")

    monkeypatch.setattr(fill, "_produced", _empty)
    env = fill._Env(data={})
    with pytest.raises(RuntimeError, match="WQP_NO_SITES"):
        asyncio.run(fill._produce(env, declared(DATA)[0]))


def _skeleton(*inputs: Any) -> Any:
    from types import SimpleNamespace

    from trid3nt_server.workflows.runtime import Workflow

    return Workflow(metadata=SimpleNamespace(name="probe", engine=""),
                    inputs=inputs, template=SimpleNamespace())


def test_the_runtime_declares_no_input_of_its_own():
    """A skeleton run with no typed row carries exactly what its template wrote."""
    assert _skeleton().inputs == ()
    own = Input(name="depth_m", default=2.0, bounds=(0.1, 50.0), desc="a depth")
    assert _skeleton(own).inputs == (own,)


def test_a_bed_row_declares_the_frame_and_a_templates_own_row_wins():
    """The frame is a real Input the bed's type states; values lead the body and rows follow."""
    bed = Input(BED, name="bed").need("bathymetry")
    seated, row = _skeleton(bed).inputs
    assert (seated.name, seated.default, row) == ("vertical_frame", "NAVD88", bed)
    assert seated is BED.states[0] and isinstance(seated, Input)
    own = Input(name="vertical_frame", default="IGLD85", desc="the lakes' datum")
    assert _skeleton(bed, own).inputs == (own, bed)


def test_one_lookup_answers_for_a_value_and_a_row_and_refuses_the_rest():
    from trid3nt_server.inputs.user_input import WireArgsError

    bed = Input(BED, name="bed").need("bathymetry")
    wf = _skeleton(bed)
    assert wf.input("bed") is bed and wf.input("vertical_frame") is BED.states[0]
    with pytest.raises(WireArgsError, match="'depth'"):
        wf.input("depth")


_SITES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.1, 0.1]},
     "properties": {"value": 50.0, "unit": "degF", "site_id": "NEAR",
                    "site_name": "a gauge", "result_date": "2026-03-04"}}]}


class _OBSERVED:
    """A row whose PRODUCER is a record and whose VALUE is one reading."""

    observe = Input(OBSERVE, producer=tool("fetch_usgs_water_quality",
                        characteristic="temperature"))


def _answered(monkeypatch, value: Any):
    from trid3nt_server.workflows.runtime import fill

    async def _found(env, producer, label):
        return value

    monkeypatch.setattr(fill, "_produced", _found)
    return fill


def test_an_observation_row_yields_the_reading_not_the_record(monkeypatch):
    """The reading is what reaches a keyword, which is the whole seam: the
    nearest site, the unit and the sample's age are the slot's own ingestion."""
    import dataclasses

    fill = _answered(monkeypatch, _SITES)
    monkeypatch.setattr(dataclasses, "replace", lambda obj, **kw: obj)
    # NO ROW STATES A UNIT: what this slot reads is the unit of the keyword its
    # ROLE fills, which the workflow answers for.
    env = fill._Env(data={}, run={},
                           slot_units={OBSERVE: "degC"})
    found = asyncio.run(fill._produce(env, declared(_OBSERVED)[0]))
    assert found.value == pytest.approx(10.0)
    assert found.units == "degC" and found.site_id == "NEAR"


def test_the_coercion_a_row_declares_is_bound_before_the_ingestion_runs():
    """``near`` is a late-bound read like any producer kwarg: the point the
    nearest site is ranked against is measured by a step, not written by hand."""
    class RANKED:
        observe = Input(OBSERVE).need("water quality sample", at="gauge")

    row = declared(RANKED)[0]
    assert row.coercion["near"] == "gauge"


def test_a_supplied_number_supersedes_the_record_through_the_same_slot(monkeypatch):
    from trid3nt_server.workflows.runtime import fill

    env = fill._Env(data={}, run={},
                           slot_units={OBSERVE: "degC"},
                           supplied={"observe": 11.5})
    found = asyncio.run(fill._produce(env, declared(_OBSERVED)[0]))
    assert found.value == pytest.approx(11.5) and found.units == "degC"


def _no_moment_env(**over):
    """A run standing on a place with NO moment stated - the one fact these two
    rows turn on."""
    from trid3nt_server.workflows.runtime import fill

    _standing_on()
    return fill._Env(run={"event_time": None}, data={}, **over)


def test_a_required_series_need_with_no_moment_stated_refuses_naming_the_slot():
    """A run that cannot stand without the record refuses instead: the slot by
    name, and that no moment was stated."""
    from trid3nt_server.workflows.runtime import fill
    from trid3nt_server.workflows.runtime.errors import StepFailedError
    from trid3nt_server.tools.search.match import NO_MOMENT

    class DATA:
        discharge = Input(DISCHARGE).need("discharge series")

    env = _no_moment_env()
    with pytest.raises(StepFailedError) as caught:
        asyncio.run(fill._produce(env, declared(DATA)[0]))
    assert caught.value.error_code == "DATA_NEED_UNMATCHED"
    assert str(caught.value).startswith(f"discharge: {NO_MOMENT}")


def _standing_on(bbox=(-123.22, 45.48, -123.19, 45.50)):
    """The domain the run is standing on - the point an offset row is asked at."""
    from trid3nt_server.inputs.bound_domain import Domain, bind_domain

    return bind_domain(Domain(bbox=tuple(bbox), geometry={}, label="the lake"))


def _elevation_slot(slot):
    """A row that STATES the slot it is - which is the only way to be one."""
    from trid3nt_server.inputs.input import Input

    return Input(name=slot.name, slot=slot)


def test_every_elevation_slot_is_read_on_the_runs_own_vertical_frame():
    """One frame per run, stated once as a runtime lever: the bed is read on it
    and the level is read on it, and nothing else a run ingests is an elevation."""
    from trid3nt_server.workflows.runtime import fill
    from trid3nt_server.inputs.slots import BED, DISCHARGE, LEVEL

    def _told(env, role, source=None):
        return asyncio.run(fill._on_the_run_s_frame(
            env, _elevation_slot(role), source))

    env = fill._Env(run={}, data={})
    assert _told(env, BED) == {"frame": "NAVD88"}
    assert _told(env, LEVEL) == {"to_datum": "NAVD88"}
    assert _told(env, DISCHARGE) == {}
    stated = fill._Env(run={"vertical_frame": "IGLD85"},
                              data={})
    assert _told(stated, BED) == {"frame": "IGLD85"}


def test_the_runtime_declares_the_offset_row_a_differing_source_owes(monkeypatch,
                                                                     tmp_path):
    """A source on another frame that publishes no shift of its own: the RUNTIME
    declares the DATA row, asks it where that source MEASURED - the point of its
    own footprint nearest the question's seed - and the row is journaled like any
    producer, with what the slot is told the value it produced."""
    from trid3nt_server.workflows.runtime import fill
    from trid3nt_server.inputs.input import Input
    from trid3nt_server.inputs.slots import BED

    record = {"offset_m": -1.054, "from_frame": "NAVD88", "to_frame": "EGM2008",
              "source": "NOAA VDatum", "uncertainty_m": 0.165}
    asked: list[dict] = []

    async def _call(runner, kwargs, label):
        asked.append({"runner": runner, **kwargs})
        return record

    monkeypatch.setattr(fill, "_call_runner", _call)
    _standing_on()
    seeded = dataclasses.replace(
        Input(DOMAIN).need("hydrography", at=[_MEASURED_EAST_OF + 0.005, 45.49]),
        name="domain")
    env = fill._Env(run={"vertical_frame": "EGM2008"},
                           data={"domain": seeded})
    told = asyncio.run(fill._on_the_run_s_frame(
        env, _elevation_slot(BED),
        {"uri": _half_measured(tmp_path), "vertical_datum": "NAVD88"}))
    assert told == {"frame": "EGM2008", "offset": record}
    (one,) = asked
    assert one["runner"] == "fetch_vertical_datum_offset"
    assert one["from_frame"] == "navd88" and one["to_frame"] == "egm2008"
    assert one["point"] == pytest.approx([_MEASURED_EAST_OF + 0.005, 45.49])
    # the row is ON the run: a declared row with a record, not a hidden call
    assert "bed_datum_offset" in env.data


def test_an_offset_row_is_asked_where_the_source_measured_not_at_the_seed(
        monkeypatch, tmp_path):
    """The seed falls on the half of the DEM that measured nothing, so the ask
    walks to the nearest point the source actually holds."""
    from trid3nt_server.workflows.runtime import fill
    from trid3nt_server.inputs.input import Input
    from trid3nt_server.inputs.slots import BED

    asked: list[dict] = []

    async def _call(runner, kwargs, label):
        asked.append(dict(kwargs))
        return {"offset_m": -1.054, "from_frame": "NAVD88", "to_frame": "EGM2008"}

    monkeypatch.setattr(fill, "_call_runner", _call)
    _standing_on()
    seeded = dataclasses.replace(
        Input(DOMAIN).need("hydrography", at=[_MEASURED_EAST_OF - 0.01, 45.49]),
        name="domain")
    env = fill._Env(run={"vertical_frame": "EGM2008"},
                           data={"domain": seeded})
    asyncio.run(fill._on_the_run_s_frame(
        env, _elevation_slot(BED),
        {"uri": _half_measured(tmp_path), "vertical_datum": "NAVD88"}))
    (one,) = asked
    assert one["point"] == pytest.approx([_MEASURED_EAST_OF, 45.49])


#: The DEM the runtime asks an offset off: it measured only the east half of its
#: own rectangle, inside the domain the run stands on.
_MEASURED_EAST_OF = -123.205


def _half_measured(tmp_path) -> str:
    """A DEM whose west half is nodata, over the domain the run stands on."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "dem.tif"
    values = np.full((10, 12), 4.0, dtype="float32")
    values[:, :6] = -9999.0
    with rasterio.open(path, "w", driver="GTiff", width=12, height=10, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(-123.22, 45.50, 0.0025, 0.002)) as dst:
        dst.write(values, 1)
    return str(path)


def test_a_source_on_the_runs_own_frame_declares_no_offset_row(monkeypatch):
    from trid3nt_server.workflows.runtime import fill
    from trid3nt_server.inputs.slots import BED

    async def _never(runner, kwargs, label):
        raise AssertionError("an offset row was declared for one frame")

    monkeypatch.setattr(fill, "_call_runner", _never)
    _standing_on()
    env = fill._Env(run={}, data={})
    told = asyncio.run(fill._on_the_run_s_frame(
        env, _elevation_slot(BED),
        {"uri": "s3://b/k/dem.tif", "vertical_datum": "NAVD88 (metres, positive up)",
         "bbox": (-123.22, 45.48, -123.19, 45.50)}))
    assert told == {"frame": "NAVD88"} and not env.data
