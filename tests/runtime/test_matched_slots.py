"""The slots the match fills: the bed's one row, and a run's series.

Offline. The rows are values and the fetchers are stubs, so what is
proved is the RULE - the ONE row the match ranked first, a wet hole refused at
the fill naming the steps that compose a bed over it, and the next survivor when
the top one held nothing."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from trid3nt_contracts.coverage import (
    Coverage,
    CoverageExtent,
    CoverageWindow,
)

from trid3nt_server.tools.fetchers._router.errors import (
    router_upstream_error,
)
from trid3nt_server.inputs.input import Input, declared
from trid3nt_server.workflows.runtime import fill
from trid3nt_server.inputs.bound_domain import (
    Domain,
    bind_domain,
    current_domain,
    reset_domain,
)
from trid3nt_server.workflows.runtime.journal import (
    bind_choices,
    drain_choices,
    run_choices,
)
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL, OBSERVE

def _hole(code="BED_WET_HOLE"):
    from trid3nt_server.inputs.user_input import UserInputError

    return UserInputError("49.4% of the water in this domain is measured by nothing in the bed", code=code)


def _walk(world, monkeypatch, holes, *, own_bed=False):
    """Walk the bed with an ingestion that refuses ``holes[i]`` on its i-th call (None accepts); returns what it saw."""
    seen = []

    async def ingest(laid):
        seen.append(laid)
        refusal = holes[len(seen) - 1] if len(seen) <= len(holes) else None
        if refusal is not None:
            raise refusal
        return laid

    env = _env()
    env.lays_own_bed = own_bed
    bed = _row(Input(BED).need("bathymetry"), "bed")
    return env, bed, ingest, seen


def test_a_first_surface_that_covers_leaves_the_second_unasked(world, monkeypatch):
    env, bed, ingest, seen = _walk(world, monkeypatch, [None])
    out = asyncio.run(fill._bed_surface(env, bed, ingest))
    assert out == "s3://b/fetch_soundings.geojson" and len(seen) == 1
    assert [runner for runner, _kw in world] == ["fetch_soundings"]


def test_a_wet_hole_walks_to_the_next_surface_and_hands_the_ordered_list(world, monkeypatch):
    env, bed, ingest, seen = _walk(world, monkeypatch, [_hole(), None])
    out = asyncio.run(fill._bed_surface(env, bed, ingest))
    assert out == ["s3://b/fetch_soundings.geojson", "s3://b/fetch_bed_raster.tif"]
    assert [runner for runner, _kw in world] == ["fetch_soundings", "fetch_bed_raster"]


def test_every_surface_leaving_a_hole_refuses_the_last_hole_naming_each_surface(world, monkeypatch):
    from trid3nt_server.inputs.user_input import UserInputError

    env, bed, ingest, seen = _walk(world, monkeypatch, [_hole(), _hole()])
    with pytest.raises(UserInputError) as refused:
        asyncio.run(fill._bed_surface(env, bed, ingest))
    assert refused.value.error_code == "BED_WET_HOLE"
    assert "fetch_soundings, fetch_bed_raster" in str(refused.value)
    assert len(seen) == 2


def test_a_refusal_other_than_a_wet_hole_is_not_walked_past(world, monkeypatch):
    from trid3nt_server.inputs.user_input import UserInputError

    env, bed, ingest, seen = _walk(world, monkeypatch, [_hole("BED_CLIFF")])
    with pytest.raises(UserInputError) as refused:
        asyncio.run(fill._bed_surface(env, bed, ingest))
    assert refused.value.error_code == "BED_CLIFF"
    assert [runner for runner, _kw in world] == ["fetch_soundings"]


def test_an_engine_laying_its_own_bed_takes_the_one_row_and_never_walks(world, monkeypatch):
    from trid3nt_server.inputs.user_input import UserInputError

    env, bed, ingest, seen = _walk(world, monkeypatch, [_hole()], own_bed=True)
    with pytest.raises(UserInputError):
        asyncio.run(fill._bed_surface(env, bed, ingest))
    assert [runner for runner, _kw in world] == ["fetch_soundings"]


def test_two_surfaces_each_measuring_half_the_water_make_a_bed_through_the_slot(world, monkeypatch, tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    west = tmp_path / "west.tif"
    values = np.full((12, 20), -6.0, dtype="float32")
    values[:, 10:] = -9999.0
    with rasterio.open(west, "w", driver="GTiff", width=20, height=12, count=1, dtype="float32",
                       crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(-122.72, 45.56, 0.005, 0.005)) as dst:
        dst.write(values, 1)
    monkeypatch.setitem(SPECS, "fetch_soundings",
                        _Spec(coverage("bathymetry", res=1.0), "raster", {"bbox": None}))

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        uri = _half_measured(tmp_path) if runner == "fetch_soundings" else str(west)
        return {"uri": uri, "vertical_datum": "NAVD88"}

    monkeypatch.setattr(fill, "_call_runner", _runner)
    water = {"type": "Polygon", "coordinates": [[
        [-122.72, 45.50], [-122.62, 45.50], [-122.62, 45.56], [-122.72, 45.56], [-122.72, 45.50]]]}
    from trid3nt_server.settings import bind_run, reset_run

    token = bind_domain(Domain(bbox=WILLAMETTE, geometry=water, label="reach"))
    reach = bind_run({"FILL_MAX_DISTANCE": 3})
    try:
        asyncio.run(fill._matched(_env(), _row(Input(BED).need("bathymetry"), "bed")))
    finally:
        reset_run(reach)
        reset_domain(token)
    assert [runner for runner, _kw in world] == ["fetch_soundings", "fetch_bed_raster"]


WILLAMETTE = (-122.72, 45.50, -122.62, 45.56)
CONUS = [[(-125.0, 24.0), (-66.0, 24.0), (-66.0, 50.0), (-125.0, 50.0)]]

#: The point the question names, on the half of the reach the survey measured.
_SEED = [-122.64, 45.53]


def _half_measured(tmp_path) -> str:
    """A survey measuring only the east half of its own rectangle."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "survey.tif"
    values = np.full((12, 20), -8.0, dtype="float32")
    values[:, :10] = -9999.0
    with rasterio.open(path, "w", driver="GTiff", width=20, height=12, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(-122.72, 45.56, 0.005, 0.005)) as dst:
        dst.write(values, 1)
    return str(path)


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


@pytest.fixture
def world(monkeypatch):
    """A bound domain, the stub rows, and a record of what was called."""
    called: list[tuple[str, dict]] = []

    async def _runner(runner, kwargs, label):
        # A source publishes the SHAPE its row states, suffix and all: what a
        # reader may open a URI as is read off that suffix and nothing else.
        called.append((runner, dict(kwargs)))
        ext = "geojson" if getattr(SPECS.get(runner), "output", None) and \
            SPECS[runner].output.layer_type == "vector" else "tif"
        return f"s3://b/{runner}.{ext}"

    monkeypatch.setattr(fill, "_call_runner", _runner)
    monkeypatch.setattr(fill, "sources_with_coverage",
                        lambda: [(n, row) for n, s in SPECS.items()
                                 for row in s.coverage])
    monkeypatch.setattr(fill, "_spec_of", lambda name: SPECS[name])
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
    return fill._Env(run=dict(mesh_resolution_m=14.0, **values),
                            data={}, moment="event_time")


def _row(decl, name):
    return dataclasses.replace(decl, name=name)


def test_the_bed_lays_the_one_row_the_match_ranked_first(world):
    """Nothing paints on the run's behalf and nothing grids: the bed calls the
    top row of its own class and no other, and hands on what it returned."""
    env = _env()
    bed = _row(Input(BED).need("bathymetry"), "bed")
    out = asyncio.run(fill._bed_surface(env, bed))
    assert [runner for runner, _kw in world] == ["fetch_soundings"]
    assert out == "s3://b/fetch_soundings.geojson"


def test_a_raster_measurement_reaches_the_bed_without_being_gridded(world,
                                                                   monkeypatch):
    monkeypatch.setitem(SPECS, "fetch_soundings",
                        _Spec(coverage("bathymetry", res=1.0, latest="1990-01-01"),
                              "raster", {"bbox": None}))
    out = asyncio.run(fill._bed_surface(
        _env(), _row(Input(BED).need("bathymetry"), "bed")))
    assert [runner for runner, _kw in world] == ["fetch_soundings"]
    assert out == "s3://b/fetch_soundings.tif"


def test_a_wet_hole_past_the_fill_s_reach_is_refused_naming_the_setting(world,
                                                                       monkeypatch,
                                                                       tmp_path):
    """The matched row measures the east half of water cut over the whole of
    it, so the west half is a WET hole the fill reaches only so far: past the
    stated reach the fill refuses by name, naming the setting and a further surface."""
    from trid3nt_server.inputs.user_input import UserInputError
    from trid3nt_server.settings import bind_run, reset_run

    monkeypatch.setitem(SPECS, "fetch_soundings",
                        _Spec(coverage("bathymetry", res=1.0), "raster",
                              {"bbox": None}))

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        return {"uri": _half_measured(tmp_path), "vertical_datum": "NAVD88"}

    monkeypatch.setattr(fill, "_call_runner", _runner)
    water = {"type": "Polygon", "coordinates": [[
        [-122.72, 45.50], [-122.62, 45.50], [-122.62, 45.56],
        [-122.72, 45.56], [-122.72, 45.50]]]}
    token = bind_domain(Domain(bbox=(-122.72, 45.50, -122.62, 45.56),
                               geometry=water, label="reach"))
    reach = bind_run({"FILL_MAX_DISTANCE": 3})
    try:
        with pytest.raises(UserInputError) as refused:
            asyncio.run(fill._matched(_env(), _row(Input(BED).need("bathymetry"), "bed")))
    finally:
        reset_run(reach)
        reset_domain(token)
    said = str(refused.value)
    assert refused.value.error_code == "BED_WET_HOLE"
    assert "FILL_MAX_DISTANCE (now 3 px)" in said and "further surface" in said
    assert "merge_rasters" not in said and "fill_nodata" not in said


def _surface(tmp_path, name, value, cols):
    """A lon/lat surface over the reach measuring ``value`` in the columns ``cols``."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / f"{name}.tif"
    values = np.full((12, 20), np.nan, dtype="float32")
    values[:, cols] = value
    with rasterio.open(path, "w", driver="GTiff", width=20, height=12, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=np.nan,
                       transform=from_origin(-122.72, 45.56, 0.005, 0.005)) as dst:
        dst.write(values, 1)
    return {"uri": str(path), "name": name, "vertical_datum": "NAVD88"}


def test_an_ordered_list_handed_the_bed_fetches_each_named_source_in_order(
        world, monkeypatch, tmp_path):
    """A merge is a second fetch into the same slot: each named source is asked
    under the class it serves, in the order handed, and the first wins."""
    import numpy as np
    import rasterio

    from trid3nt_server.tools.derive import _hydrology_common as common

    real = common.write_cog
    monkeypatch.setattr(common, "write_cog", lambda band, **kw: real(
        band, **{**kw, "output_dir": str(tmp_path)}))
    made = {"fetch_bed_raster": _surface(tmp_path, "chart", -8.0, slice(10, 20)),
            "fetch_terrain": _surface(tmp_path, "terrain", 3.0, slice(0, 20))}

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        return made[runner]

    monkeypatch.setattr(fill, "_call_runner", _runner)
    env = _env()
    row = _row(Input(BED).need("bathymetry"), "bed")
    fill._stated(env, row, [{"source": "fetch_bed_raster"}, {"source": "fetch_terrain"}])
    laid = asyncio.run(fill._produce(env, row))
    assert [runner for runner, _kw in world] == ["fetch_bed_raster", "fetch_terrain"]
    assert laid.source.sources[:2] == ["chart", "terrain"]
    with rasterio.open(laid.source.uri) as src:
        values = src.read(1)
    assert np.all(values[:, 10:] == -8.0) and np.all(values[:, :10] == 3.0)


def test_a_named_source_serving_no_bed_class_refuses_rather_than_another_taking_its_place(
        world):
    from trid3nt_server.workflows.runtime.errors import StepFailedError

    env = _env()
    row = _row(Input(BED).need("bathymetry"), "bed")
    fill._stated(env, row, [{"source": "fetch_bed_raster"}, {"source": "fetch_gauges"}])
    with pytest.raises(StepFailedError) as refused:
        asyncio.run(fill._produce(env, row))
    assert "surface 2" in str(refused.value) and "fetch_gauges" in str(refused.value)


def test_no_measurement_over_this_domain_refuses_rather_than_reaching_a_class(
        world, monkeypatch):
    """A bed whose own class measured nothing here is not a bed the terrain
    quietly becomes: the slot refuses, and naming the terrain is the person's."""
    from trid3nt_server.workflows.runtime.errors import StepFailedError

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        if runner in ("fetch_soundings", "fetch_bed_raster"):
            raise RuntimeError("EHYDRO_NO_SURVEYS")
        return f"s3://b/{runner}.tif"

    monkeypatch.setattr(fill, "_call_runner", _runner)
    env = _env()
    with pytest.raises(StepFailedError) as excinfo:
        asyncio.run(fill._bed_surface(
            env, _row(Input(BED).need("bathymetry"), "bed")))
    assert excinfo.value.error_code == "DATA_NEED_UNMATCHED"
    assert "fetch_terrain" not in [runner for runner, _kw in world]
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

    monkeypatch.setattr(fill, "_call_runner", _runner)
    env = _env()
    choice, value = asyncio.run(fill._probe(
        env, _row(Input(BED).need("bathymetry"), "bed"), "bathymetry", "bed"))
    assert choice.picked == "fetch_bed_raster"
    assert value == "s3://b/fetch_bed_raster.tif"
    assert choice.rows[0].excluded.startswith("held nothing here")


def test_a_non_retryable_upstream_error_drops_the_rung(world, monkeypatch):
    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        if runner == "fetch_soundings":
            raise router_upstream_error("EHYDRO", "TransportNotFound: HTTP 404",
                                        False)
        return f"s3://b/{runner}.tif"

    monkeypatch.setattr(fill, "_call_runner", _runner)
    choice, value = asyncio.run(fill._probe(
        _env(), _row(Input(BED).need("bathymetry"), "bed"), "bathymetry", "bed"))
    assert choice.picked == "fetch_bed_raster"
    assert value == "s3://b/fetch_bed_raster.tif"


def test_a_run_series_is_asked_for_the_window_the_deck_will_solve(world):
    env = fill._Env(
        run=dict(mesh_resolution_m=14.0, event_time="2026-09-13T22:00:00Z"),
        data={}, window_s=172800.0, moment="event_time")
    discharge = _row(Input(DISCHARGE).need("discharge series"), "discharge")
    choice, value = asyncio.run(fill._probe(
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


def test_the_fill_reads_the_moment_under_the_name_the_workflow_states(world):
    env = fill._Env(run=dict(mesh_resolution_m=14.0, opens="2026-09-13T22:00:00Z",
                             event_time="1999-01-01T00:00:00Z"),
                    data={}, window_s=3600.0, moment="opens")
    row = _row(Input(DISCHARGE).need("discharge series"), "discharge")
    need = asyncio.run(fill._need(env, row, "discharge", "discharge"))
    assert need.opens == "2026-09-13T22:00:00Z"


def test_a_need_and_a_producer_on_one_row_is_refused_at_declaration():
    from trid3nt_server.workflows.runtime import DeclarationError, tool

    with pytest.raises(DeclarationError, match="a row is satisfied one way"):
        class DATA:
            bed = Input(BED, producer=tool("fetch_dem")).need("bathymetry")

        declared(DATA)


def test_a_gauge_serving_two_classes_fills_each_slot_from_its_own_row(world,
                                                                     monkeypatch):
    """One source, two rows: the discharge slot reads the flow columns
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
    level = _row(Input(LEVEL).need("water level series"), "level")
    told = fill._what_the_record_reports(
        env, level, fill._fetcher_row("fetch_gauges",
                                              "water level series"))
    assert told["field"] == "gage_height_ft"
    assert told["series_field"] == "stage_series_csv"
    assert told["above_field"] == "gauge_datum_ft"
    # the columns the LEVEL row states, and none the flow row states.
    assert told["column_units"] == {"gage_height_ft": "ft",
                                    "stage_series_csv": "ft",
                                    "gauge_datum_ft": "ft"}
    discharge = _row(Input(DISCHARGE).need("discharge series"), "discharge")
    flow = fill._what_the_record_reports(
        env, discharge, fill._fetcher_row("fetch_gauges",
                                                  "discharge series"))
    assert flow["field"] == "discharge_cfs"
    assert flow["series_field"] == "time_series_csv"
    assert flow["column_units"]["time_series_csv"] == "ft3/s"


def test_one_column_name_in_two_units_is_read_off_the_row_that_was_matched(
        world, monkeypatch):
    """A tide service publishing a level in metres and a water temperature in
    degC under ONE column name: the level slot reads m and the observe slot
    reads degC, because each is told what the row ITS match produced states -
    never a flattening across the rows, which would read the temperature's unit
    on the level."""
    measured = coverage("water level series", series=True, latest=None,
                        datum=None, kind="stations",
                        units={"water_level": "m", "time_series_csv": "m"},
                        value="water_level", window_column="time_series_csv")
    tides = _Spec([measured.model_copy(update={
        "kind": "predicted", "units": {"predicted_wl": "m",
                                       "time_series_csv": "m"},
        "value_column": "predicted_wl"}), measured,
        coverage("water quality sample", series=True, latest=None, datum=None,
                 kind="stations",
                 units={"water_temperature": "degC", "time_series_csv": "degC"},
                 value="water_temperature",
                 window_column="time_series_csv").model_copy(
                     update={"vocabulary": {"TEMPERATURE": "water_temperature"}})],
        "vector", {"bbox": None, "start_date": None, "end_date": None})
    monkeypatch.setitem(SPECS, "fetch_tides", tides)
    env = _env(event_time="2026-09-13T22:00:00Z")

    def told(row, data_class):
        choice, _value = asyncio.run(
            fill._probe(env, row, data_class, row.name))
        assert choice.picked == "fetch_tides"
        return fill._what_the_record_reports(
            env, row, fill._matched_row(choice, choice.picked))

    level = told(_row(Input(LEVEL).need("water level series"), "level"),
                 "water level series")
    # the MEASURED row, not the first row of the class the row states.
    assert level["field"] == "water_level"
    assert level["column_units"]["time_series_csv"] == "m"
    observed = told(_row(Input(OBSERVE).need("water quality sample", of="TEMPERATURE"), "observe"), "water quality sample")
    assert observed["field"] == "water_temperature"
    assert observed["column_units"]["time_series_csv"] == "degC"


def _reach_source(monkeypatch):
    """A hydrography source called by a seed and a distance, whose row
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
    row = _row(Input(DOMAIN).need("hydrography", span_km=25.0), "domain")
    choice, _value = asyncio.run(fill._probe(env, row, "hydrography",
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
    asyncio.run(fill._probe(env, _row(Input(DOMAIN).need("hydrography"), "domain"),
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

    monkeypatch.setattr(fill, "_call_runner", _runner)
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
    row = _row(Input(DOMAIN).need("hydrography", of="coastline", geometry="polyline"), "domain")
    out = asyncio.run(fill._matched(env, row))
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
    row = _row(Input(DOMAIN).need("hydrography", of="coastline", geometry="polyline"), "domain")

    async def _fill_then_read():
        await fill._matched(env, row)
        return current_domain()

    bound = asyncio.run(_fill_then_read())
    assert bound.geometry["type"] in ("Polygon", "MultiPolygon")
    assert bound.bbox[0] == pytest.approx(-122.67)


def test_each_handed_surface_on_another_frame_is_asked_its_own_offset_off_its_record(
        world, monkeypatch, tmp_path):
    """A case layer handed by its handle states its zero on its record, so the runtime asks
    the offset row for THAT surface and reads it onto the run's frame before it is laid."""
    import numpy as np
    import rasterio

    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.render.uri_registry import (
        activate_registry, deactivate_registry, get_uri_registry)
    from trid3nt_server.tools.derive import _hydrology_common as common

    real = common.write_cog
    monkeypatch.setattr(common, "write_cog", lambda band, **kw: real(
        band, **{**kw, "output_dir": str(tmp_path)}))
    chart = _surface(tmp_path, "chart", -8.0, slice(10, 20))
    terrain = _surface(tmp_path, "terrain", 3.0, slice(0, 20))

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        return {"offset_m": 1.5, "from_frame": "igld85", "to_frame": "navd88",
                "source": "NOAA VDatum"}

    monkeypatch.setattr(fill, "_call_runner", _runner)
    registry = get_uri_registry("bed-offset-per-surface")
    registry.register_tool_result("fetch_chs_nonna", LayerURI(
        layer_id="chart-1", name="chart", layer_type="raster", uri=chart["uri"],
        vertical_datum="IGLD85"))
    token = activate_registry(registry)
    try:
        env = _env()
        row = _row(Input(BED).need("bathymetry"), "bed")
        fill._stated(env, row, ["chart-1", terrain])
        laid = asyncio.run(fill._produce(env, row))
    finally:
        deactivate_registry(token)
    asked = [kw for runner, kw in world if runner == "fetch_vertical_datum_offset"]
    assert [(kw["from_frame"], kw["to_frame"]) for kw in asked] == [("igld85", "navd88")]
    assert "bed_1_datum_offset" in env.data
    with rasterio.open(laid.source.uri) as src:
        values = src.read(1)
    assert np.allclose(values[:, 10:], -6.5) and np.allclose(values[:, :10], 3.0)


#: The Hawthorne Bridge, where the coastline row holds nothing.
_HAWTHORNE = (-122.6709588, 45.513235)
_NOTHING = {"type": "FeatureCollection", "features": []}
_REACH = {"type": "Polygon", "coordinates": [[
    [-122.68, 45.50], [-122.66, 45.50], [-122.66, 45.53], [-122.68, 45.53],
    [-122.68, 45.50]]]}


@pytest.fixture
def hydrography(world, monkeypatch):
    """Two hydrography rows over one place: a coastline row (no cell stated) and a river-area row (5 m)."""
    from trid3nt_server.tools.fetchers._router import registration

    answers = {"fetch_coastline": _NOTHING, "fetch_reach": _REACH}
    coast = coverage("hydrography", res=None, datum=None, latest=None).model_copy(
        update={"vocabulary": {"coastline": "coastline"}})
    reach = coverage("hydrography", res=5.0, datum=None)
    for name, row in (("fetch_coastline", coast), ("fetch_reach", reach)):
        spec = _Spec(row, "vector", {"bbox": None})
        monkeypatch.setitem(SPECS, name, spec)
        monkeypatch.setitem(registration._SPEC_REGISTRY, name, spec)

    async def _runner(runner, kwargs, label):
        world.append((runner, dict(kwargs)))
        return answers[runner]

    monkeypatch.setattr(fill, "_call_runner", _runner)

    async def _seed(env, decl):
        return list(_HAWTHORNE)

    monkeypatch.setattr(fill, "_seed", _seed)
    return world, answers


def test_the_row_stating_a_cell_is_asked_before_the_coastline_row_and_fills_the_domain(hydrography):
    called, _answers = hydrography
    out = asyncio.run(fill._matched(
        _env(), _row(Input(DOMAIN).need("hydrography", at="release"), "domain")))
    assert out.geometry["type"] == "Polygon"
    assert [runner for runner, _kw in called] == ["fetch_reach"]


def test_a_domain_need_falls_to_the_next_ranked_row_when_the_first_holds_zero_features(hydrography):
    called, answers = hydrography
    answers["fetch_reach"], answers["fetch_coastline"] = _NOTHING, _REACH
    row = _row(Input(DOMAIN).need("hydrography", at="release"), "domain")
    out = asyncio.run(fill._matched(_env(), row))
    assert out.geometry["type"] == "Polygon"
    assert [runner for runner, _kw in called] == ["fetch_reach", "fetch_coastline"]
    assert "fetch_reach held nothing, so fetch_coastline fills the slot" in run_choices()[-1].sentence


def test_every_row_empty_is_one_refusal_naming_each_row_tried(hydrography):
    from trid3nt_server.workflows.runtime.errors import StepFailedError

    _called, answers = hydrography
    answers["fetch_reach"] = _NOTHING
    row = _row(Input(DOMAIN).need("hydrography", at="release"), "domain")
    with pytest.raises(StepFailedError) as refused:
        asyncio.run(fill._matched(_env(), row))
    assert refused.value.error_code == "DATA_NEED_UNMATCHED"
    text = str(refused.value)
    assert "fetch_coastline held nothing here" in text and "fetch_reach held nothing here" in text
    assert "carries no polygon geometry" in text



#: USGS's own answer for the Etowah box on 2026-09-21 (12 sites), recorded as the router wrote it.
_ETOWAH_GAUGES = (__import__("pathlib").Path(__file__).resolve().parents[1]
                  / "fixtures" / "usgs_nwis_gauges" / "etowah_bbox_2026-09-21.fgb")

#: The release the Etowah reach is found from, 224 m above USGS 02395000 on its flowline.
_ETOWAH_RELEASE = [-84.978229605, 34.207328998]


@pytest.mark.parametrize("slot, need, observed", [
    (LEVEL, "water level series", 186.836),
    (DISCHARGE, "discharge series", 15.008),
])
def test_a_gauge_row_naming_no_point_takes_the_site_nearest_the_question_s_seed(
        slot, need, observed):
    """The river templates' level and discharge rows state no ``at``: their sites
    rank against the point the domain is found from, never the source's own row
    order, whose first site here is Richland Creek 30 km up a tributary."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    row = next(r for r in get_spec("fetch_usgs_nwis_gauges").coverage
               if r.data_class == need)
    env = fill._Env(run=dict(release=list(_ETOWAH_RELEASE),
                             event_time="2026-09-21T23:00:00Z"),
                    data={"domain": _row(Input(DOMAIN).need(
                        "hydrography", at="release"), "domain")},
                    moment="event_time", slot_units={LEVEL: "m", DISCHARGE: "m3/s"})
    gauge = _row(Input(slot).need(need), slot.name)
    found = asyncio.run(fill._ingested(env, gauge, str(_ETOWAH_GAUGES), row))
    assert found.site_name == "ETOWAH RIVER NEAR KINGSTON,GA"
    assert found.distance_km == pytest.approx(0.23, abs=0.01)
    assert found.value == pytest.approx(observed, abs=1e-3)
