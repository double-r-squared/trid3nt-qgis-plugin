"""The slots the match fills: the bed's two classes and a run's series.

Offline. The coverage rows are values and the fetchers are stubs, so what is
proved is the RULE - the measurement where it measured, the terrain under the
rest, the next survivor when the top one held nothing."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from trid3nt_contracts.coverage import Coverage, CoverageExtent, CoverageWindow

from trid3nt_server.workflows.runtime import Data, data_rows
from trid3nt_server.workflows.runtime import interpreter
from trid3nt_server.workflows.runtime.domain import Domain, bind_domain, reset_domain
from trid3nt_server.workflows.runtime.journal import (
    bind_choices, drain_choices, run_choices)

WILLAMETTE = (-122.72, 45.50, -122.62, 45.56)
CONUS = [[(-125.0, 24.0), (-66.0, 24.0), (-66.0, 50.0), (-125.0, 50.0)]]


def coverage(data_class, *, res=None, datum="NAVD88", series=False,
             latest="2024-01-01", units=None, kind="surface", value="",
             window_column="", above="", provenance="measured"):
    return Coverage(
        data_class=data_class, kind=provenance,
        reach_km=25.0 if kind == "stations" else None,
        extent=CoverageExtent(kind=kind, rings=CONUS, note="CONUS",
                              discover="bbox" if kind == "stations" else ""),
        window=CoverageWindow(series=series, latest=latest, cadence="hourly"),
        resolution_m=res, datum=datum, units=units or {},
        value_column=value or next(iter(units or {}), ""),
        series_column=window_column, above_column=above)


class _Spec:
    def __init__(self, cov, layer_type, params):
        self.coverage = [cov] if not isinstance(cov, list) else cov
        self.output = type("O", (), {"layer_type": layer_type})()
        self.params = params


SPECS = {
    "fetch_soundings": _Spec(
        coverage("bathymetry", res=1.0, datum=None,
                 units={"depth_below_datum_m": "m"}),
        "vector", {"bbox": None}),
    "fetch_bed_raster": _Spec(
        coverage("bathymetry", res=2.0, latest="2020-01-01",
                 units={"elevation": "m"}),
        "raster", {"bbox": None}),
    "fetch_terrain": _Spec(
        coverage("terrain", res=10.0, units={"elevation": "m"}),
        "raster", {"bbox": None}),
    "fetch_gauges": _Spec(
        coverage("discharge series", series=True, latest=None, datum=None,
                 kind="stations", units={"time_series_csv": "ft3/s"}),
        "vector", {"bbox": None, "start_date": None, "end_date": None}),
    # The offset fetch measures no class, so it matches nothing; what the
    # runtime reads off it is the enum of frames the service transforms.
    "fetch_vertical_datum_offset": _Spec(
        [], "record",
        {"point": None,
         "from_frame": type("P", (), {"values": ["navd88", "igld85"]})(),
         "to_frame": type("P", (), {"values": ["navd88", "igld85"]})()}),
}


class _Params:
    def __init__(self, **values):
        self._values = values

    def value_of(self, name):
        return self._values.get(name)


@pytest.fixture
def world(monkeypatch):
    """A bound domain, the stub coverage rows, and a record of what was called."""
    called: list[tuple[str, dict]] = []

    async def _runner(runner, kwargs, label):
        # A source publishes the SHAPE its spec states, suffix and all: what a
        # reader may open a URI as is read off that suffix and nothing else.
        called.append((runner, dict(kwargs)))
        ext = "geojson" if getattr(SPECS.get(runner), "output", None) and \
            SPECS[runner].output.layer_type == "vector" else "tif"
        return f"s3://b/{runner}.{ext}"

    monkeypatch.setattr(interpreter, "_call_runner", _runner)
    monkeypatch.setattr(interpreter, "sources_with_coverage",
                        lambda: [(n, row) for n, s in SPECS.items()
                                 for row in s.coverage])
    monkeypatch.setattr(interpreter, "_spec_of", lambda name: SPECS[name])
    # The ask closes through the match, which reads the source's own params off
    # the router's registry: the stubs stand in there too.
    from trid3nt_server.tools.fetchers._router import registration

    monkeypatch.setattr(registration, "_SPEC_REGISTRY", dict(SPECS))
    token = bind_domain(Domain(bbox=WILLAMETTE, geometry={}, label="reach"))
    chosen = bind_choices()
    try:
        yield called
    finally:
        drain_choices(chosen)
        reset_domain(token)


def _env(**values):
    return interpreter._Env(params=_Params(mesh_resolution_m=14.0, **values),
                            data={}, results={})


def _row(decl, name):
    return dataclasses.replace(decl, name=name)


def test_the_bed_is_the_measurement_gridded_over_the_terrain_under_it(world):
    env = _env()
    bed = _row(Data.need("bathymetry"), "bed")
    out = asyncio.run(interpreter._matched_bed(env, bed))
    ran = [runner for runner, _kw in world]
    assert ran == ["fetch_terrain", "fetch_soundings",
                   "trid3nt_server.inputs.bed.survey_surface",
                   "trid3nt_server.inputs.bed.merged_surface"]
    assert out.endswith("merged_surface.tif")
    # the soundings are gridded at the run's own edge, on the column the
    # coverage row names.
    _runner, grid = world[2]
    assert grid["value_field"] == "depth_below_datum_m"
    assert grid["resolution_m"] == 14.0


def test_a_raster_measurement_reaches_the_merge_without_being_gridded(world,
                                                                     monkeypatch):
    monkeypatch.setitem(SPECS, "fetch_soundings",
                        _Spec(coverage("bathymetry", res=1.0, latest="1990-01-01"),
                              "raster", {"bbox": None}))
    env = _env()
    out = asyncio.run(interpreter._matched_bed(
        env, _row(Data.need("bathymetry"), "bed")))
    ran = [runner for runner, _kw in world]
    assert "trid3nt_server.inputs.bed.survey_surface" not in ran
    assert ran[-1] == "trid3nt_server.inputs.bed.merged_surface"
    assert out.endswith("merged_surface.tif")


def test_the_runtime_declares_the_offset_row_a_merge_source_owes(world,
                                                                 monkeypatch):
    """Two surfaces on two zeros meet at the merge before any slot sees them, so
    the shift each owes onto the run's frame is the RUNTIME's own row - the same
    declaration a slot's source gets, and none for the one already on it."""
    monkeypatch.setitem(SPECS, "fetch_soundings",
                        _Spec(coverage("bathymetry", res=1.0), "raster",
                              {"bbox": None}))
    record = {"offset_m": 0.013, "from_frame": "IGLD85", "to_frame": "NAVD88",
              "source": "NOAA VDatum"}

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        if runner == "fetch_soundings":
            return {"uri": "s3://b/survey.tif", "vertical_datum": "IGLD85"}
        if runner == "fetch_terrain":
            return {"uri": "s3://b/dem.tif", "vertical_datum": "NAVD88"}
        if runner == "fetch_vertical_datum_offset":
            return record
        return f"s3://b/{runner}.tif"

    monkeypatch.setattr(interpreter, "_call_runner", _runner)
    env = _env()
    asyncio.run(interpreter._matched_bed(env, _row(Data.need("bathymetry"), "bed")))
    asked = {runner: kwargs for runner, kwargs in world}
    assert asked["fetch_vertical_datum_offset"]["from_frame"] == "igld85"
    assert asked["fetch_vertical_datum_offset"]["to_frame"] == "navd88"
    merge = asked["trid3nt_server.inputs.bed.merged_surface"]
    assert merge["frame"] == "NAVD88"
    assert merge["primary_offset"] == record and merge["fallback_offset"] is None
    # ON the run: the measurement's row is declared and the terrain, already on
    # the frame, owes none.
    assert "bed_measurement_datum_offset" in env.data
    assert "bed_terrain_datum_offset" not in env.data


def test_no_measurement_over_this_domain_leaves_the_terrain_as_the_whole_bed(
        world, monkeypatch):
    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        if runner in ("fetch_soundings", "fetch_bed_raster"):
            raise RuntimeError("EHYDRO_NO_SURVEYS")
        return f"s3://b/{runner}.tif"

    monkeypatch.setattr(interpreter, "_call_runner", _runner)
    env = _env()
    out = asyncio.run(interpreter._matched_bed(
        env, _row(Data.need("bathymetry"), "bed")))
    assert out == "s3://b/fetch_terrain.tif"
    assert "trid3nt_server.inputs.bed.merged_surface" not in [
        runner for runner, _kw in world]
    # BOTH measurements were tried, in rank order, and the sheet says so.
    sentence = run_choices()[-1].sentence
    assert "fetch_soundings" in sentence and "fetch_bed_raster" in sentence


def test_the_next_survivor_takes_its_turn_when_the_top_one_held_nothing(
        world, monkeypatch):
    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        if runner == "fetch_soundings":
            raise RuntimeError("EHYDRO_NO_SURVEYS")
        return f"s3://b/{runner}.tif"

    monkeypatch.setattr(interpreter, "_call_runner", _runner)
    env = _env()
    choice, value = asyncio.run(interpreter._probe(
        env, _row(Data.need("bathymetry"), "bed"), "bathymetry", "bed"))
    assert choice.picked == "fetch_bed_raster"
    assert value == "s3://b/fetch_bed_raster.tif"
    assert choice.rows[0].excluded.startswith("held nothing here")


def test_a_run_series_is_asked_for_the_window_the_deck_will_solve(world):
    env = interpreter._Env(
        params=_Params(mesh_resolution_m=14.0, event_time="2026-09-13T22:00:00Z"),
        data={}, results={}, window_s=172800.0)
    discharge = _row(Data.need("discharge series"), "discharge")
    choice, value = asyncio.run(interpreter._probe(
        env, discharge, "discharge series", "discharge"))
    assert choice.picked == "fetch_gauges"
    _runner, ask = world[-1]
    assert ask["start_date"] == "2026-09-13"
    assert ask["end_date"] == "2026-09-15"
    # The ask reaches PAST the domain: a surface that stopped at its edge
    # would leave the nodes on that edge standing on nothing.
    west, south, east, north = ask["bbox"]
    assert west < WILLAMETTE[0] and south < WILLAMETTE[1]
    assert east > WILLAMETTE[2] and north > WILLAMETTE[3]
    assert value == "s3://b/fetch_gauges.geojson"


def test_a_need_and_a_producer_on_one_row_is_refused_at_declaration():
    from trid3nt_server.workflows.runtime import PlanValidationError, tool

    with pytest.raises(PlanValidationError, match="a row is satisfied one way"):
        class DATA:
            bed = Data(tool("fetch_dem")).need("bathymetry")

        data_rows(DATA)


def test_the_ranked_list_is_one_object_the_card_and_the_sheet_both_read(world):
    """Three views, one text: the card's row, the tool result on a tie and the
    record's own line all carry the sentence the model was given."""
    from trid3nt_contracts.payload_warning import ParamSheetRow
    from trid3nt_server.workflows.runtime.journal import build_record
    from trid3nt_server.workflows.runtime.workflow import _ranked_rows
    from trid3nt_server.workflows.telemac.workflow import _source_rows

    env = _env()
    asyncio.run(interpreter._matched_bed(env, _row(Data.need("bathymetry"), "bed")))
    rows = _source_rows()
    assert [row.name for row in rows] == ["bed terrain source",
                                          "bed bathymetry source"]
    card = rows[-1]
    assert isinstance(card, ParamSheetRow)
    assert card.value == "fetch_soundings"
    assert card.choices.picked == "fetch_soundings"
    # the sentence the card shows IS the sentence the model was given
    assert card.note == card.choices.sentence
    # the sheet stores the pick and its reason under the slot's own name
    record = build_record(
        run_id="r", engine="telemac", module="t2d", sheet=(), answer={},
        provenance=(), result=None, wall_seconds=1.0, origin="test",
        executed=(), replayed=(), notes=(), sources=run_choices())
    assert record["sources"]["bed bathymetry"]["picked"] == "fetch_soundings"
    assert record["sources"]["bed bathymetry"]["reason"] == card.choices.sentence
    # the tool result's TIE view names every row so a model can pick one
    tied = card.choices.model_copy(update={"tie": True})
    assert "fetch_soundings" in _ranked_rows(tied)
    assert "1)" in _ranked_rows(tied)


def test_a_gauge_serving_two_classes_fills_each_slot_from_its_own_row(world,
                                                                     monkeypatch):
    """One source, two coverage rows: the discharge slot reads the flow columns
    and the level slot reads the stage columns, off the row of the class each
    one asked for."""
    gauge = _Spec([coverage("discharge series", series=True, latest=None,
                            datum=None, kind="stations",
                            units={"discharge_cfs": "ft3/s",
                                   "time_series_csv": "ft3/s"},
                            value="discharge_cfs",
                            window_column="time_series_csv"),
                   coverage("water level series", series=True, latest=None,
                            datum=None, kind="stations",
                            units={"gage_height_ft": "ft",
                                   "stage_series_csv": "ft",
                                   "gauge_datum_ft": "ft"},
                            value="gage_height_ft",
                            window_column="stage_series_csv",
                            above="gauge_datum_ft")],
                  "vector", {"bbox": None, "start_date": None, "end_date": None})
    monkeypatch.setitem(SPECS, "fetch_gauges", gauge)
    env = _env()
    level = _row(Data.need("water level series"), "level")
    told = interpreter._what_the_record_reports(env, level, "fetch_gauges")
    assert told["field"] == "gage_height_ft"
    assert told["series_field"] == "stage_series_csv"
    assert told["above_field"] == "gauge_datum_ft"
    # every column the source states a unit for is readable, whichever row
    # stated it.
    assert told["column_units"]["discharge_cfs"] == "ft3/s"
    discharge = _row(Data.need("discharge series"), "discharge")
    flow = interpreter._what_the_record_reports(env, discharge, "fetch_gauges")
    assert flow["field"] == "discharge_cfs"
    assert flow["series_field"] == "time_series_csv"


def _reach_source(monkeypatch):
    """A hydrography source called by a seed and a distance, whose coverage row
    maps the question's generic span onto its own param."""
    from trid3nt_server.tools.fetchers._router import registration

    spec = _Spec(coverage("hydrography", res=5.0, datum=None).model_copy(
        update={"ask": {"distance_km": "need:span_km"}}),
        "vector", {"seed_point": None, "distance_km": None})
    monkeypatch.setitem(SPECS, "fetch_reach", spec)
    monkeypatch.setitem(registration._SPEC_REGISTRY, "fetch_reach", spec)


def test_the_question_s_span_reaches_the_source_in_its_own_word(world,
                                                                monkeypatch):
    """How far a question reaches is one generic attribute; the matched ROW
    maps it onto the param that source states it in."""
    _reach_source(monkeypatch)
    env = _env()
    row = _row(Data.need("hydrography", span_km=25.0), "domain")
    choice, _value = asyncio.run(interpreter._probe(env, row, "hydrography",
                                                    "domain"))
    assert choice.picked == "fetch_reach"
    _runner, called = world[-1]
    assert called["distance_km"] == 25.0


def test_a_question_with_no_opinion_about_its_reach_asks_for_none(world,
                                                                  monkeypatch):
    """A row that states no span leaves the param off the call, so the source's
    own declared default answers rather than a number nobody chose."""
    _reach_source(monkeypatch)
    env = _env()
    asyncio.run(interpreter._probe(env, _row(Data.need("hydrography"), "domain"),
                                   "hydrography", "domain"))
    _runner, called = world[-1]
    assert "distance_km" not in called


#: The land-water edge a stub coastline source publishes over the bound box,
#: drawn SOUTH to NORTH so the land is on its LEFT - the west half.
_COASTLINE = {"type": "LineString",
              "coordinates": [[-122.67, 45.49], [-122.67, 45.57]]}


@pytest.fixture
def coastline(world, monkeypatch):
    """One hydrography source that publishes the edge, and nothing else."""
    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        return dict(_COASTLINE)

    monkeypatch.setattr(interpreter, "_call_runner", _runner)
    row = coverage("hydrography", datum=None).model_copy(
        update={"vocabulary": {"coastline": "coastline"}})
    monkeypatch.setitem(SPECS, "fetch_coastline",
                        _Spec(row, "vector", {"bbox": None}))
    yield world
    SPECS.pop("fetch_coastline", None)


def test_the_domain_slot_cuts_the_window_with_the_edge_it_was_matched(coastline):
    """The cut is the slot's INGESTION: a row that asks for the land-water edge
    is filled with a line, and what the slot yields is the closed polygon the
    equations are solved over."""
    env = _env()
    row = _row(Data.need("hydrography", of="coastline", geometry="polyline"),
               "domain")
    out = asyncio.run(interpreter._matched(env, row))
    assert out.geometry["type"] in ("Polygon", "MultiPolygon")
    # The water is the half the way does not have its land on, cut at the box
    # the question was asked in rather than at the line's own span.
    from shapely.geometry import shape

    minx, miny, maxx, maxy = shape(out.geometry).bounds
    assert (minx, maxx) == pytest.approx((-122.67, WILLAMETTE[2]))
    assert (miny, maxy) == pytest.approx((WILLAMETTE[1], WILLAMETTE[3]))


def test_the_cut_domain_is_what_the_rest_of_the_run_is_bound_to(coastline):
    """A domain slot binds the run's domain the moment it is filled, so the box
    the cut was made in is superseded by the water that left it and every later
    row is asked over the water rather than over the window."""
    env = _env()
    row = _row(Data.need("hydrography", of="coastline", geometry="polyline"),
               "domain")

    async def _fill_then_read():
        await interpreter._matched(env, row)
        return interpreter.current_domain()

    bound = asyncio.run(_fill_then_read())
    assert bound.geometry["type"] in ("Polygon", "MultiPolygon")
    assert bound.bbox[0] == pytest.approx(-122.67)
