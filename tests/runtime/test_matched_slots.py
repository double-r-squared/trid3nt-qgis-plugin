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

WILLAMETTE = (-122.72, 45.50, -122.62, 45.56)
CONUS = [[(-125.0, 24.0), (-66.0, 24.0), (-66.0, 50.0), (-125.0, 50.0)]]


def coverage(data_class, *, res=None, datum="NAVD88", series=False,
             latest="2024-01-01", units=None, kind="surface"):
    return Coverage(
        data_class=data_class,
        extent=CoverageExtent(kind=kind, rings=CONUS, note="CONUS"),
        window=CoverageWindow(series=series, latest=latest, cadence="hourly"),
        resolution_m=res, datum=datum, units=units or {})


class _Spec:
    def __init__(self, cov, layer_type, params):
        self.coverage = cov
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
        called.append((runner, dict(kwargs)))
        if runner in ("fetch_soundings", "fetch_bed_raster", "fetch_terrain",
                      "fetch_gauges"):
            return f"s3://b/{runner}.out"
        return f"s3://b/{runner}.tif"

    monkeypatch.setattr(interpreter, "_call_runner", _runner)
    monkeypatch.setattr(interpreter, "sources_with_coverage",
                        lambda: [(n, s.coverage) for n, s in SPECS.items()])
    monkeypatch.setattr(interpreter, "_spec_of", lambda name: SPECS[name])
    token = bind_domain(Domain(bbox=WILLAMETTE, geometry={}, label="reach"))
    try:
        yield called
    finally:
        reset_domain(token)


def _env(**values):
    return interpreter._Env(params=_Params(mesh_resolution_m=14.0, **values),
                            data={}, results={})


def _row(decl, name):
    return dataclasses.replace(decl, name=name)


def test_the_bed_is_the_measurement_gridded_over_the_terrain_under_it(world):
    env = _env()
    bed = _row(Data.bed(need="bathymetry"), "bed")
    out = asyncio.run(interpreter._matched_bed(env, bed))
    ran = [runner for runner, _kw in world]
    assert ran == ["fetch_terrain", "fetch_soundings", "derive_survey_surface",
                   "derive_merge_rasters"]
    assert out.endswith("derive_merge_rasters.tif")
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
        env, _row(Data.bed(need="bathymetry"), "bed")))
    ran = [runner for runner, _kw in world]
    assert "derive_survey_surface" not in ran
    assert ran[-1] == "derive_merge_rasters"
    assert out.endswith("derive_merge_rasters.tif")


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
        env, _row(Data.bed(need="bathymetry"), "bed")))
    assert out == "s3://b/fetch_terrain.tif"
    assert "derive_merge_rasters" not in [runner for runner, _kw in world]
    # BOTH measurements were tried, in rank order, and the sheet says so.
    sentence = env.choices[-1].sentence
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
        env, _row(Data.bed(need="bathymetry"), "bed"), "bathymetry", "bed"))
    assert choice.picked == "fetch_bed_raster"
    assert value == "s3://b/fetch_bed_raster.tif"
    assert choice.rows[0].excluded.startswith("held nothing here")


def test_a_run_series_is_asked_for_the_window_the_deck_will_solve(world):
    env = interpreter._Env(
        params=_Params(mesh_resolution_m=14.0, event_time="2026-09-13T22:00:00Z"),
        data={}, results={}, window_s=172800.0)
    carrier = _row(Data.discharge(need="discharge series"), "carrier")
    choice, value = asyncio.run(interpreter._probe(
        env, carrier, "discharge series", "carrier"))
    assert choice.picked == "fetch_gauges"
    _runner, ask = world[-1]
    assert ask["start_date"] == "2026-09-13"
    assert ask["end_date"] == "2026-09-15"
    assert ask["bbox"] == list(WILLAMETTE)
    assert value == "s3://b/fetch_gauges.out"


def test_a_need_and_a_producer_on_one_row_is_refused_at_declaration():
    from trid3nt_server.workflows.runtime import PlanValidationError, tool

    with pytest.raises(PlanValidationError, match="is a PIN"):
        class DATA:
            bed = Data.bed(tool("fetch_dem"), need="bathymetry")

        data_rows(DATA)
