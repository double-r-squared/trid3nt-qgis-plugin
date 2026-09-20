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

from trid3nt_server.workflows.runtime import (
    Data,
    Param,
    PlanValidationError,
    Ref,
    Step,
    data_rows,
    doors,
    tool,
)
from trid3nt_server.workflows.runtime.data import BED, DOMAIN
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES, LEVERS, with_levers


def test_a_domain_row_is_one_slot_however_it_is_filled():
    """A drawn polygon, the user's layer and a producer fill ONE row: the slot is
    on the wire AND names its producer, and nothing downstream reads which."""
    class DRAWN:
        domain = Data.supplied(geometry="polygon")

    class FETCHED:
        domain = Data(tool("fetch_river_reach", seed=[1.0, 2.0]))

    drawn, fetched = data_rows(DRAWN)[0], data_rows(FETCHED)[0]
    assert drawn.role == fetched.role == DOMAIN
    # BOTH are on the wire: what the user hands in supersedes the producer.
    assert drawn.fills_from_user and fetched.fills_from_user
    assert fetched.producer.runner == "fetch_river_reach"


def test_a_bed_row_takes_every_source_a_survey_arrives_in_but_a_mesh():
    class DATA:
        bed = Data.need("bathymetry")

    row = data_rows(DATA)[0]
    assert row.role == BED and row.fills_from_user
    # A bathymetry is measured as a raster AND as soundings, so both are the
    # class handed in; a stated depth is the number itself and has no shape.
    for supplied in ("s3://b/k/survey.tif", "s3://b/k/soundings.geojson", 2.0):
        row.refuse_wrong_shape(supplied)
    with pytest.raises(Exception, match="reads as mesh"):
        row.refuse_wrong_shape("s3://b/k/solved.slf")


def test_a_terrain_row_refuses_the_shape_no_source_of_terrain_publishes():
    """The supplied twin is the CLASS's: terrain is measured as a surface and
    nothing publishes it as a point layer, so soundings are not a terrain."""
    class DATA:
        bed = Data.need("terrain")

    row = data_rows(DATA)[0]
    row.refuse_wrong_shape("s3://b/k/ground.tif")
    with pytest.raises(Exception, match="reads as vector"):
        row.refuse_wrong_shape("s3://b/k/soundings.geojson")


def test_a_row_declared_with_a_producer_binds_its_reads_like_a_bare_one():
    """``Data(tool(...))`` is a producer row with a modifier written on it, so
    its reads of sibling rows resolve the same way a bare row's do."""
    class DATA:
        box = tool("probe_point", layer_uri="x")
        sample = Data(tool("fetch_usgs_water_quality",
                           bbox=Ref("box.bbox"))).context()

    rows = {row.name: row for row in data_rows(DATA)}
    assert rows["sample"].is_context
    assert rows["sample"].producer.kwargs["bbox"] == Ref("box.bbox")


def test_a_context_row_states_what_the_sheet_says_when_its_source_is_empty():
    class DATA:
        stated = Data(tool("fetch_usgs_water_quality")).context(
            "no water-quality sample near this domain; the stated value stands")
        unstated = Data(tool("fetch_raws_weather")).context()

    rows = {row.name: row for row in data_rows(DATA)}
    assert rows["stated"].context_sentence.startswith("no water-quality sample")
    assert rows["unstated"].context_sentence == (
        "no unstated near this domain; the stated value stands")


def test_context_needs_something_to_ask_and_optional_still_refuses_a_producer():
    with pytest.raises(PlanValidationError,
                       match=r"states neither a need nor a producer"):
        Data.context()
    with pytest.raises(PlanValidationError, match=r"declares a producer AND \.optional"):
        class DATA:
            row = Data(tool("fetch_x")).optional()

        data_rows(DATA)


def test_a_context_rows_absence_continues_the_run_and_says_so(monkeypatch):
    """The whole point: a producer whose source held nothing does not refuse the
    run - it leaves the slot empty and writes its sentence on the record."""
    from trid3nt_server.workflows.runtime import interpreter

    class DATA:
        sample = Data(tool("fetch_usgs_water_quality")).context(
            "no sample near this domain; the stated value stands")

    async def _empty(env, producer, label):
        raise RuntimeError("WQP_NO_SITES")

    monkeypatch.setattr(interpreter, "_produced", _empty)
    env = interpreter._Env(params=None, data={}, results={})
    row = data_rows(DATA)[0]
    assert asyncio.run(interpreter._produce(env, row)) is None
    assert env.absences == [
        "no sample near this domain; the stated value stands (WQP_NO_SITES)"]


def test_a_context_row_no_step_reads_is_asked_all_the_same(monkeypatch):
    """The sentence IS the product: a context row no step and no sibling row
    dereferences is still asked, while a plain row nothing reads costs no fetch."""
    from trid3nt_server.workflows.runtime import interpreter

    class DATA:
        read = Data(tool("fetch_ehydro_surveys")).context("no survey here")
        unread = Data(tool("fetch_usgs_water_quality")).context(
            "no water-quality site near this domain; the stated value stands")
        plain = Data(tool("fetch_dem"))

    asked: list[str] = []

    async def _asked(env, producer, label):
        asked.append(producer.runner)
        raise RuntimeError("WQP_NO_SITES")

    monkeypatch.setattr(interpreter, "_produced", _asked)
    rows = {row.name: row for row in data_rows(DATA)}
    env = interpreter._Env(params=None, data=rows, results={})
    node = interpreter.PlanNode(index=0, label="step", runner="r", kind="step",
                                step=Step(runner="r",
                                          kwargs={"survey": Ref("read")}))
    asyncio.run(interpreter._ask_unread_context(env, (node,)))
    assert asked == ["fetch_usgs_water_quality"]
    assert env.absences == [
        "no water-quality site near this domain; the stated value stands "
        "(WQP_NO_SITES)"]


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

    domain = Data(tool("fetch_river_reach", seed_point=[0.5, 0.5]))
    line = Data.supplied(geometry="polyline")


def _env_over(value, monkeypatch, body=_RIVER):
    from trid3nt_server.workflows.runtime import interpreter

    async def _answered(env, producer, label):
        return {}, value

    monkeypatch.setattr(interpreter, "_produced", _answered)
    rows = {row.name: row for row in data_rows(body)}
    return interpreter, interpreter._Env(params=None, data=rows, results={})


def test_the_line_slot_is_filled_by_the_domains_own_centerline(monkeypatch):
    """A placed profile reads a LINE, and the reach producer measured one beside
    its polygon: the template restates nothing to be given it."""
    reach = {"type": "FeatureCollection", "features": [
        *_REACH["features"],
        {"type": "Feature", "properties": {"part": "centerline"},
         "geometry": {"type": "LineString",
                      "coordinates": [[0.1, 0.5], [0.9, 0.5]]}}]}
    interpreter, env = _env_over(reach, monkeypatch)
    line = asyncio.run(interpreter._produce(env, env.data["line"]))
    assert line == {"type": "LineString", "coordinates": [[0.1, 0.5], [0.9, 0.5]]}


def test_a_domain_with_no_centerline_leaves_the_line_to_be_drawn(monkeypatch):
    """A lake has no companion line down it. With no canvas to ask, the slot is
    unsatisfied and says so - which is a user drawing the line they mean."""
    interpreter, env = _env_over(_REACH, monkeypatch)
    with pytest.raises(Exception, match="DATA_SLOT_UNSATISFIED|producer-less"):
        asyncio.run(interpreter._produce(env, env.data["line"]))


def test_a_line_the_user_draws_is_read_as_one_geometry(monkeypatch):
    """Every shape a line arrives in reads the same after the slot: a drawn
    collection, a geometry, or the vertices themselves."""
    interpreter, env = _env_over(_REACH, monkeypatch)
    env.supplied["line"] = [[0.2, 0.2], [0.8, 0.8]]
    assert asyncio.run(interpreter._produce(env, env.data["line"])) == {
        "type": "LineString", "coordinates": [[0.2, 0.2], [0.8, 0.8]]}


def test_a_malformed_ask_on_a_context_row_refuses_rather_than_reading_absent(
        monkeypatch):
    """ONLY AN EMPTY SOURCE IS AN ABSENCE. A window, a bbox or a unit the caller
    stated wrong is the ASK being wrong, and a run that swallowed it would report
    "nothing was there" about a question nobody managed to put."""
    from trid3nt_server.tools.fetchers._router.errors import router_input_error
    from trid3nt_server.workflows.runtime import interpreter

    class DATA:
        survey = Data(tool("fetch_ehydro_surveys")).context(
            "no published channel survey over this domain")

    async def _refused(env, producer, label):
        raise router_input_error("EHYDRO", "31 surveys end on or after "
                                 "2015-01-01 over this extent; the window is "
                                 "the ask's", "INPUT_INVALID")

    monkeypatch.setattr(interpreter, "_produced", _refused)
    env = interpreter._Env(params=None, data={}, results={})
    with pytest.raises(Exception) as exc:
        asyncio.run(interpreter._produce(env, data_rows(DATA)[0]))
    assert exc.value.error_code == "EHYDRO_INPUT_INVALID"
    assert env.absences == []


def test_a_malformed_value_handed_to_a_context_rows_slot_refuses_too(monkeypatch):
    """The ingestion is inside the absence for an empty SOURCE; a value the slot
    cannot read as what it is asked for is the same wrong ask."""
    from trid3nt_server.inputs.user_input import UserInputError
    from trid3nt_server.workflows.runtime import interpreter

    class DATA:
        sample = Data(tool("fetch_usgs_water_quality")).context(
            "nothing sampled near this domain")

    async def _answered(env, producer, label):
        raise UserInputError("a reading is a number or a layer of sites",
                             code="OBSERVATION_INVALID")

    monkeypatch.setattr(interpreter, "_produced", _answered)
    env = interpreter._Env(params=None, data={}, results={})
    row = data_rows(DATA)[0]
    with pytest.raises(UserInputError) as exc:
        asyncio.run(interpreter._produce(env, row))
    assert exc.value.error_code == "OBSERVATION_INVALID"
    assert env.absences == []


def test_a_tail_read_off_a_wholly_absent_row_is_nothing(monkeypatch):
    """A row that is absent reads like a field that is present and empty. What
    a context row EXISTS for is that the run continues, so the keyword reading
    it states nothing rather than refusing on a field nobody wrote."""
    from trid3nt_server.workflows.runtime import interpreter

    class DATA:
        sample = Data(tool("fetch_usgs_water_quality")).context()

    async def _empty(env, producer, label):
        raise RuntimeError("WQP_NO_SITES")

    monkeypatch.setattr(interpreter, "_produced", _empty)
    rows = {row.name: row for row in data_rows(DATA)}
    env = interpreter._Env(params=None, data=rows, results={})
    assert asyncio.run(interpreter._deref(Ref("sample.value"), env)) is None
    # a field the row HAS but does not carry still refuses by name
    env = interpreter._Env(params=None, data=rows, results={},
                           artifacts={"sample": {"value": 3.0}})
    with pytest.raises(Exception, match="REF_FIELD_MISSING|reads 'units'"):
        asyncio.run(interpreter._deref(Ref("sample.units"), env))


def test_the_sheet_reads_a_wholly_absent_row_as_nothing():
    """Same rule where the keywords are set: an absent row states nothing, and
    the composite reading it expands to no keyword at all."""
    from trid3nt_server.workflows.telemac.modules.sheet import _read

    assert _read(Ref("sample.value"), {"sample": None}, {}) is None


def test_a_hard_producer_row_still_refuses_when_its_source_is_empty(monkeypatch):
    from trid3nt_server.workflows.runtime import interpreter

    class DATA:
        sample = tool("fetch_usgs_water_quality")

    async def _empty(env, producer, label):
        raise RuntimeError("WQP_NO_SITES")

    monkeypatch.setattr(interpreter, "_produced", _empty)
    env = interpreter._Env(params=None, data={}, results={})
    with pytest.raises(RuntimeError, match="WQP_NO_SITES"):
        asyncio.run(interpreter._produce(env, data_rows(DATA)[0]))


def test_the_runtime_declares_the_levers_a_template_no_longer_restates():
    # The CLOCK is not among them: DURATION is a keyword telemac2d and telemac3d
    # both carry, so the deck states it and the user overrides it by that name.
    assert LEVER_NAMES == ("mesh_resolution_m", "event_time", "compute_class",
                           "vertical_frame")
    seated = with_levers((), LEVER_NAMES)
    assert [p.name for p in seated] == list(LEVER_NAMES)
    # a lever nothing takes is never seated: a param with no reader is not one
    assert with_levers(()) == ()


def test_a_templates_own_row_wins_over_the_lever_of_that_name():
    own = Param(name="mesh_resolution_m", door=doors.SCENARIO, default=25.0,
                bounds=(3.0, 500.0), desc="coarser: this answer is reach-scale")
    seated = with_levers((own,), LEVER_NAMES)
    assert [p.name for p in seated] == ["mesh_resolution_m", "event_time",
                                        "compute_class", "vertical_frame"]
    assert seated[0].default == 25.0


def test_a_lever_states_only_this_questions_opinion_of_it():
    """The row a question writes for a lever it differs on is the DIFFERENCE:
    the type, the unit and the help are the runtime's and are not restated."""
    from trid3nt_server.workflows.runtime import lever

    own = lever("mesh_resolution_m", default=25.0, bounds=(3.0, 500.0))
    assert (own.name, own.units, own.consequence) == ("mesh_resolution_m", "m",
                                                      "numerical")
    assert (own.default, own.bounds) == (25.0, (3.0, 500.0))
    assert own.desc == next(row.desc for row in LEVERS
                            if row.name == "mesh_resolution_m")
    with pytest.raises(PlanValidationError, match="is not a runtime lever"):
        lever("friction_law", default=3)
    # The clock was a lever until the module's own DURATION was read as what it
    # is; asking for it by the old name says so rather than answering.
    with pytest.raises(PlanValidationError, match="is not a runtime lever"):
        lever("sim_duration_s", default=600.0)


def test_a_lever_the_runtime_does_not_declare_refuses_by_name():
    with pytest.raises(PlanValidationError, match="is not a runtime lever"):
        with_levers((), ("friction_law",))


_SITES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.1, 0.1]},
     "properties": {"value": 50.0, "unit": "degF", "site_id": "NEAR",
                    "site_name": "a gauge", "result_date": "2026-03-04"}}]}


class _OBSERVED:
    """A row whose PRODUCER is a record and whose VALUE is one reading."""

    observe = Data(tool("fetch_usgs_water_quality",
                        characteristic="temperature"))


def _answered(monkeypatch, value: Any):
    from trid3nt_server.workflows.runtime import interpreter

    async def _found(env, producer, label):
        return {}, value

    monkeypatch.setattr(interpreter, "_produced", _found)
    monkeypatch.setattr(interpreter, "_record_for",
                        lambda *a, **k: _Record())
    return interpreter


class _Record:
    """The ledger row a produced artifact writes; nothing here reads it."""

    index = -1
    node = ""


def test_an_observation_row_yields_the_reading_not_the_record(monkeypatch):
    """``Ref("<row>.value")`` reaches a keyword, which is the whole seam: the
    nearest site, the unit and the sample's age are the slot's own ingestion."""
    import dataclasses

    interpreter = _answered(monkeypatch, _SITES)
    monkeypatch.setattr(dataclasses, "replace", lambda obj, **kw: obj)
    # NO ROW STATES A UNIT: what this slot reads is the unit of the keyword its
    # ROLE fills, which the workflow answers for.
    env = interpreter._Env(params=None, data={}, results={"station": (0.11, 0.1)},
                           slot_units={"observe": "degC"})
    found = asyncio.run(interpreter._produce(env, data_rows(_OBSERVED)[0]))
    assert found.value == pytest.approx(10.0)
    assert found.units == "degC" and found.site_id == "NEAR"


def test_the_coercion_a_row_declares_is_bound_before_the_ingestion_runs():
    """``near`` is a late-bound read like any producer kwarg: the point the
    nearest site is ranked against is measured by a step, not written by hand."""
    class RANKED:
        observe = Data.need("water quality sample", at=Ref("station"))

    row = data_rows(RANKED)[0]
    assert row.coercion["near"] == Ref("station")


def test_a_supplied_number_supersedes_the_record_through_the_same_slot(monkeypatch):
    from trid3nt_server.workflows.runtime import interpreter

    env = interpreter._Env(params=None, data={}, results={"station": (0.11, 0.1)},
                           slot_units={"observe": "degC"},
                           supplied={"observe": 11.5})
    found = asyncio.run(interpreter._produce(env, data_rows(_OBSERVED)[0]))
    assert found.value == pytest.approx(11.5) and found.units == "degC"


def test_a_context_row_over_a_window_nobody_stated_is_not_asked(monkeypatch):
    """A row whose producer reads a param the caller left unset has no question
    to put: asking anyway is a refusal about a window nobody chose."""
    import asyncio

    from trid3nt_server.workflows.runtime import ParamRef, interpreter

    class DATA:
        rain = Data(tool("fetch_aorc_precip", bbox=[0.0, 0.0, 1.0, 1.0],
                         start_date=ParamRef("event_time"))
                    ).context("no hourly rainfall record over this catchment "
                              "for that window")

    asked: list[str] = []

    async def _never(env, producer, label):
        asked.append(label)
        raise AssertionError("the source must not be asked")

    monkeypatch.setattr(interpreter, "_produced", _never)
    env = interpreter._Env(params=_Params({"event_time": None}), data={},
                           results={})
    row = data_rows(DATA)[0]
    assert asyncio.run(interpreter._produce(env, row)) is None
    assert asked == []
    assert env.absences == [
        "no hourly rainfall record over this catchment for that window "
        "(event_time (the start_date this row reads) was not stated)"]


def _no_moment_env(**over):
    """A run standing on a place with NO moment stated - the one fact these two
    rows turn on."""
    from trid3nt_server.workflows.runtime import interpreter

    _standing_on()
    return interpreter._Env(params=_Params({"event_time": None}), data={},
                            results={}, **over)


def test_a_context_need_with_no_moment_stated_is_absent_with_its_sentence():
    """The row states a NEED rather than a producer, so the match is what
    refuses to ask: a series source reached over no window answers with its
    latest record, which is a storm nobody asked about."""
    from trid3nt_server.workflows.runtime import interpreter
    from trid3nt_server.tools.search.match import NO_MOMENT

    class DATA:
        rain = Data.need("precipitation series").context(
            "no hourly rainfall record over this catchment for that window; "
            "the design storm drives the run")

    env = _no_moment_env()
    row = data_rows(DATA)[0]
    assert asyncio.run(interpreter._produce(env, row)) is None
    assert len(env.absences) == 1
    assert env.absences[0].startswith(
        "no hourly rainfall record over this catchment for that window; "
        "the design storm drives the run (rain: " + NO_MOMENT)


def test_a_required_series_need_with_no_moment_stated_refuses_naming_the_slot():
    """A run that cannot stand without the record refuses instead: the slot by
    name, and that no moment was stated."""
    from trid3nt_server.workflows.runtime import interpreter
    from trid3nt_server.workflows.runtime.errors import StepFailedError
    from trid3nt_server.tools.search.match import NO_MOMENT

    class DATA:
        discharge = Data.need("discharge series")

    env = _no_moment_env()
    with pytest.raises(StepFailedError) as caught:
        asyncio.run(interpreter._produce(env, data_rows(DATA)[0]))
    assert caught.value.error_code == "DATA_NEED_UNMATCHED"
    assert str(caught.value).startswith(f"discharge: {NO_MOMENT}")


def _standing_on(bbox=(-123.22, 45.48, -123.19, 45.50)):
    """The domain the run is standing on - the point an offset row is asked at."""
    from trid3nt_server.workflows.runtime.domain import Domain, bind_domain

    return bind_domain(Domain(bbox=tuple(bbox), geometry={}, label="the lake"))


def _elevation_slot(role: str):
    """A row whose NAME is the slot it plays - which is the only way to be one."""
    from trid3nt_server.workflows.runtime.data import DataDecl

    return DataDecl(name=role)


def test_every_elevation_slot_is_read_on_the_runs_own_vertical_frame():
    """One frame per run, stated once as a runtime lever: the bed is read on it
    and the level is read on it, and nothing else a run ingests is an elevation."""
    from trid3nt_server.workflows.runtime import interpreter
    from trid3nt_server.workflows.runtime.data import BED, DISCHARGE, LEVEL

    def _told(env, role, source=None):
        return asyncio.run(interpreter._on_the_run_s_frame(
            env, _elevation_slot(role), source))

    env = interpreter._Env(params=_Params({}), data={}, results={})
    assert _told(env, BED) == {"frame": "NAVD88"}
    assert _told(env, LEVEL) == {"to_datum": "NAVD88"}
    assert _told(env, DISCHARGE) == {}
    stated = interpreter._Env(params=_Params({"vertical_frame": "IGLD85"}),
                              data={}, results={})
    assert _told(stated, BED) == {"frame": "IGLD85"}


def test_the_runtime_declares_the_offset_row_a_differing_source_owes(monkeypatch,
                                                                     tmp_path):
    """A source on another frame that publishes no shift of its own: the RUNTIME
    declares the DATA row, asks it where that source MEASURED - the point of its
    own footprint nearest the question's seed - and the row is journaled like any
    producer, with what the slot is told the value it produced."""
    from trid3nt_server.workflows.runtime import interpreter
    from trid3nt_server.workflows.runtime.data import BED, Data

    record = {"offset_m": -1.054, "from_frame": "NAVD88", "to_frame": "EGM2008",
              "source": "NOAA VDatum", "uncertainty_m": 0.165}
    asked: list[dict] = []

    async def _call(runner, kwargs, label):
        asked.append({"runner": runner, **kwargs})
        return record

    monkeypatch.setattr(interpreter, "_call_runner", _call)
    _standing_on()
    seeded = dataclasses.replace(
        Data.need("hydrography", at=[_MEASURED_EAST_OF + 0.005, 45.49]),
        name="domain")
    env = interpreter._Env(params=_Params({"vertical_frame": "EGM2008"}),
                           data={"domain": seeded}, results={})
    told = asyncio.run(interpreter._on_the_run_s_frame(
        env, _elevation_slot(BED),
        {"uri": _half_measured(tmp_path), "vertical_datum": "NAVD88"}))
    assert told == {"frame": "EGM2008", "offset": record}
    (one,) = asked
    assert one["runner"] == "fetch_vertical_datum_offset"
    assert one["from_frame"] == "navd88" and one["to_frame"] == "egm2008"
    assert one["point"] == pytest.approx([_MEASURED_EAST_OF + 0.005, 45.49])
    # the row is ON the run: a declared row with a record, not a hidden call
    assert "bed_datum_offset" in env.data
    assert [r.node for r in env.data_records] == ["data:bed_datum_offset"]


def test_an_offset_row_is_asked_where_the_source_measured_not_at_the_seed(
        monkeypatch, tmp_path):
    """The seed falls on the half of the DEM that measured nothing, so the ask
    walks to the nearest point the source actually holds."""
    from trid3nt_server.workflows.runtime import interpreter
    from trid3nt_server.workflows.runtime.data import BED, Data

    asked: list[dict] = []

    async def _call(runner, kwargs, label):
        asked.append(dict(kwargs))
        return {"offset_m": -1.054, "from_frame": "NAVD88", "to_frame": "EGM2008"}

    monkeypatch.setattr(interpreter, "_call_runner", _call)
    _standing_on()
    seeded = dataclasses.replace(
        Data.need("hydrography", at=[_MEASURED_EAST_OF - 0.01, 45.49]),
        name="domain")
    env = interpreter._Env(params=_Params({"vertical_frame": "EGM2008"}),
                           data={"domain": seeded}, results={})
    asyncio.run(interpreter._on_the_run_s_frame(
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
    from trid3nt_server.workflows.runtime import interpreter
    from trid3nt_server.workflows.runtime.data import BED

    async def _never(runner, kwargs, label):
        raise AssertionError("an offset row was declared for one frame")

    monkeypatch.setattr(interpreter, "_call_runner", _never)
    _standing_on()
    env = interpreter._Env(params=_Params({}), data={}, results={})
    told = asyncio.run(interpreter._on_the_run_s_frame(
        env, _elevation_slot(BED),
        {"uri": "s3://b/k/dem.tif", "vertical_datum": "NAVD88 (metres, positive up)",
         "bbox": (-123.22, 45.48, -123.19, 45.50)}))
    assert told == {"frame": "NAVD88"} and not env.data


def test_a_beds_ladder_sentence_states_its_rungs_in_rank_order():
    """The bed lays a whole ladder rather than standing on the first answer, so
    what a reader is told names every source it asked, in rank order, and what
    each one held - never one drop and its successor."""
    from trid3nt_contracts.coverage import SourceChoice, SourceOption
    from trid3nt_server.workflows.runtime import interpreter

    asked = ["fetch_bluetopo", "fetch_topobathy", "fetch_ehydro_surveys"]
    choice = SourceChoice(slot="bed", need="bathymetry",
                          rows=[SourceOption(fetcher=name) for name in asked])
    said = interpreter._ladder_sentence(choice, asked,
                                        [("fetch_topobathy", object())])
    assert said == ("bed: the ladder in rank order - fetch_bluetopo held "
                    "nothing; fetch_topobathy laid a rung; "
                    "fetch_ehydro_surveys held nothing.")


class _Params:
    """The param state a run is walked against: what the caller filled."""

    def __init__(self, filled: dict) -> None:
        self._filled = filled

    def value_of(self, name: str):
        return self._filled.get(name)
