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
from trid3nt_server.workflows.runtime.levers import LEVER_NAMES, with_levers


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
    assert LEVER_NAMES == ("mesh_resolution_m", "event_time", "compute_class")
    seated = with_levers((), LEVER_NAMES)
    assert [p.name for p in seated] == list(LEVER_NAMES)
    # a lever nothing takes is never seated: a param with no reader is not one
    assert with_levers(()) == ()


def test_a_templates_own_row_wins_over_the_lever_of_that_name():
    own = Param(name="mesh_resolution_m", door=doors.SCENARIO, default=25.0,
                bounds=(3.0, 500.0), desc="coarser: this answer is reach-scale")
    seated = with_levers((own,), LEVER_NAMES)
    assert [p.name for p in seated] == ["mesh_resolution_m", "event_time",
                                        "compute_class"]
    assert seated[0].default == 25.0


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
