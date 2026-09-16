"""The engine-neutral slots on the runtime: the domain, the bed, the runs, and
the context row.

Offline: the declarations are values, so what is proved is what a body DECLARES,
what reaches the wire, and what a run says when a context source held nothing.
"""

from __future__ import annotations

import asyncio
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
from trid3nt_server.workflows.runtime.data import BED, DOMAIN, RUNS
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES, LEVERS, with_levers


def test_a_domain_row_is_one_slot_however_it_is_filled():
    """A drawn polygon, the user's layer and a producer fill ONE row: the slot is
    on the wire AND names its producer, and nothing downstream reads which."""
    class DATA:
        drawn = Data.domain()
        fetched = Data.domain(tool("fetch_river_reach", seed=[1.0, 2.0]))

    rows = {row.name: row for row in data_rows(DATA)}
    assert rows["drawn"].role == rows["fetched"].role == DOMAIN
    assert rows["drawn"].geometry == rows["fetched"].geometry == "polygon"
    # BOTH are on the wire: what the user hands in supersedes the producer.
    assert rows["drawn"].fills_from_user and rows["fetched"].fills_from_user
    assert rows["fetched"].producer.runner == "fetch_river_reach"


def test_a_bed_row_takes_every_source_a_survey_arrives_in_but_a_mesh():
    class DATA:
        bed = Data.bed()

    row = data_rows(DATA)[0]
    assert row.role == BED and row.fills_from_user
    # a raster, a sounding layer and a stated depth all pass the slot's shape
    for supplied in ("s3://b/k/survey.tif", "s3://b/k/soundings.geojson", 2.0):
        row.refuse_wrong_shape(supplied)
    with pytest.raises(Exception, match="reads as a mesh"):
        row.refuse_wrong_shape("s3://b/k/solved.slf")


def test_boundary_runs_are_an_optional_slot_because_a_closed_body_states_none():
    class DATA:
        runs = Data.runs()

    row = data_rows(DATA)[0]
    assert (row.role, row.is_optional, row.geometry) == (RUNS, True, "polyline")


def test_a_row_declared_with_a_producer_binds_its_reads_like_a_bare_one():
    """``Data(tool(...))`` is a producer row with a modifier written on it, so
    its reads of sibling rows resolve the same way a bare row's do."""
    class DATA:
        box = tool("compute_layer_bounds", layer_uri="x")
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


def test_context_needs_a_producer_and_optional_still_refuses_one():
    with pytest.raises(PlanValidationError, match=r"declares \.context\(\) with no producer"):
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

    monkeypatch.setattr(interpreter, "_walk_ladder", _empty)
    env = interpreter._Env(params=None, data={}, results={})
    row = data_rows(DATA)[0]
    assert asyncio.run(interpreter._produce(env, row)) is None
    assert env.absences == [
        "no sample near this domain; the stated value stands (WQP_NO_SITES)"]


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
    """A question whose domain producer measures the edge it cuts between."""

    domain = Data.domain(tool("fetch_river_reach", seed_point=[0.5, 0.5]))
    runs = Data.runs()


def _env_over(value, monkeypatch, body=_RIVER):
    from trid3nt_server.workflows.runtime import interpreter

    async def _answered(env, producer, label):
        return producer, value

    monkeypatch.setattr(interpreter, "_walk_ladder", _answered)
    rows = {row.name: row for row in data_rows(body)}
    return interpreter, interpreter._Env(params=None, data=rows, results={})


def test_the_runs_slot_is_filled_by_the_domains_own_producer(monkeypatch):
    """The reach fetcher returns the polygon and the two faces as ONE artifact,
    so a template that declares the slot restates nothing to be given them."""
    interpreter, env = _env_over(_REACH, monkeypatch)
    runs = asyncio.run(interpreter._produce(env, env.data["runs"]))
    assert [(run.type, run.name) for run in runs] == [("inflow", None),
                                                      ("outflow", None)]


def test_a_domain_that_carries_no_runs_leaves_the_slot_empty(monkeypatch):
    """A drawn outline measures no edge. With no canvas to ask, the absence is
    the answer - which is what a CLOSED body states - and it is labelled."""
    drawn = {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1.0, 0.0],
                                                 [1.0, 1.0], [0.0, 0.0]]]}
    interpreter, env = _env_over(drawn, monkeypatch)
    assert asyncio.run(interpreter._produce(env, env.data["runs"])) is None
    assert env.absences and "runs" in env.absences[0]


def test_a_run_the_user_hands_over_supersedes_the_producers_own(monkeypatch):
    """The slot reads the same whatever fills it: what the caller supplies is
    what the mesh prescribes on, and the producer is never asked."""
    interpreter, env = _env_over(_REACH, monkeypatch)
    env.supplied["runs"] = [{"start": [0.0, 0.0], "end": [1.0, 0.0],
                             "type": "open", "name": "the sea"}]
    runs = asyncio.run(interpreter._produce(env, env.data["runs"]))
    assert [(run.type, run.name) for run in runs] == [("open", "the sea")]


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

    monkeypatch.setattr(interpreter, "_walk_ladder", _refused)
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

    monkeypatch.setattr(interpreter, "_walk_ladder", _answered)
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

    monkeypatch.setattr(interpreter, "_walk_ladder", _empty)
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

    monkeypatch.setattr(interpreter, "_walk_ladder", _empty)
    env = interpreter._Env(params=None, data={}, results={})
    with pytest.raises(RuntimeError, match="WQP_NO_SITES"):
        asyncio.run(interpreter._produce(env, data_rows(DATA)[0]))


def test_the_runtime_declares_the_levers_a_template_no_longer_restates():
    assert LEVER_NAMES == ("mesh_resolution_m", "sim_duration_s", "event_time",
                           "compute_class")
    seated = with_levers((), LEVER_NAMES)
    assert [p.name for p in seated] == list(LEVER_NAMES)
    # a lever nothing takes is never seated: a param with no reader is not one
    assert with_levers(()) == ()


def test_a_templates_own_row_wins_over_the_lever_of_that_name():
    own = Param(name="mesh_resolution_m", door=doors.SCENARIO, default=25.0,
                bounds=(3.0, 500.0), desc="coarser: this answer is reach-scale")
    seated = with_levers((own,), LEVER_NAMES)
    assert [p.name for p in seated] == ["mesh_resolution_m", "sim_duration_s",
                                        "event_time", "compute_class"]
    assert seated[0].default == 25.0


def test_a_lever_states_only_this_questions_opinion_of_it():
    """The row a question writes for a lever it differs on is the DIFFERENCE:
    the type, the unit and the help are the runtime's and are not restated."""
    from trid3nt_server.workflows.runtime import lever

    own = lever("sim_duration_s", default=604800.0, bounds=(3600.0, 1209600.0))
    assert (own.name, own.units, own.consequence) == ("sim_duration_s", "s",
                                                      "numerical")
    assert (own.default, own.bounds) == (604800.0, (3600.0, 1209600.0))
    assert own.desc == next(row.desc for row in LEVERS
                            if row.name == "sim_duration_s")
    with pytest.raises(PlanValidationError, match="is not a runtime lever"):
        lever("friction_law", default=3)


def test_a_lever_the_runtime_does_not_declare_refuses_by_name():
    with pytest.raises(PlanValidationError, match="is not a runtime lever"):
        with_levers((), ("friction_law",))


_SITES = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.1, 0.1]},
     "properties": {"value": 50.0, "unit": "degF", "site_id": "NEAR",
                    "site_name": "a gauge", "result_date": "2026-03-04"}}]}


class _OBSERVED:
    """A row whose PRODUCER is a record and whose VALUE is one reading."""

    opening = Data.observation(
        tool("fetch_usgs_water_quality", characteristic="temperature"),
        near=Ref("station"), units="degC", measures="a water temperature",
        opens="the water opens at")


def _answered(monkeypatch, value: Any):
    from trid3nt_server.workflows.runtime import interpreter

    async def _found(env, producer, label):
        return producer, value

    monkeypatch.setattr(interpreter, "_walk_ladder", _found)
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
    env = interpreter._Env(params=None, data={}, results={"station": (0.11, 0.1)})
    found = asyncio.run(interpreter._produce(env, data_rows(_OBSERVED)[0]))
    assert found.value == pytest.approx(10.0)
    assert found.units == "degC" and found.site_id == "NEAR"


def test_the_coercion_a_row_declares_is_bound_before_the_ingestion_runs():
    """``near`` is a late-bound read like any producer kwarg: the point the
    nearest site is ranked against is measured by a step, not written by hand."""
    row = data_rows(_OBSERVED)[0]
    assert row.coercion["near"] == Ref("station")
    assert row.coercion["opens"] == "the water opens at"


def test_a_supplied_number_supersedes_the_record_through_the_same_slot(monkeypatch):
    from trid3nt_server.workflows.runtime import interpreter

    env = interpreter._Env(params=None, data={}, results={"station": (0.11, 0.1)},
                           supplied={"opening": 11.5})
    found = asyncio.run(interpreter._produce(env, data_rows(_OBSERVED)[0]))
    assert found.value == pytest.approx(11.5) and found.units == "degC"
