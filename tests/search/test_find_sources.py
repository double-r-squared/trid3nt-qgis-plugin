"""``find_sources``, offline and pure over declarations.

One survivor with the call it is ready for, a tie stated rather than broken,
nothing measuring it here, and the two refusals that name the class. The same
match a template's need row resolves through, so the fixtures are the match's."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from importlib import import_module

from trid3nt_server.tools.search.find_sources import find_sources

# The package re-exports the tool under its own name, so the MODULE the world
# fixture patches is reached by import rather than by attribute.
fs = import_module("trid3nt_server.tools.search.find_sources.find_sources")

from tests.search.test_source_match import CONUS, EUROPE, WILLAMETTE, surface

PORTLAND_BOX = [-122.72, 45.48, -122.62, 45.56]


@pytest.fixture
def world(monkeypatch):
    """Register a set of (source, row) pairs as the whole world, with the params
    each source states, so the ask is built off declarations alone."""
    from trid3nt_server.tools.fetchers._router import registration

    def _register(pairs, params=None):
        rows: dict[str, list] = {}
        for name, row in pairs:
            rows.setdefault(name, []).append(row)
        for name, coverage in rows.items():
            monkeypatch.setitem(
                registration._SPEC_REGISTRY, name,
                SimpleNamespace(params=dict(params or {"bbox": {}}),
                                coverage=coverage))
        monkeypatch.setattr(fs, "sources_with_coverage", lambda: list(pairs))

    return _register


@pytest.mark.asyncio
async def test_one_survivor_comes_back_with_the_call_it_is_ready_for(world):
    world([("fetch_survey", surface())])
    found = await find_sources("bathymetry", PORTLAND_BOX)
    assert found["picked"] == "fetch_survey"
    assert not found["tie"]
    row = found["results"][0]
    assert row["tool_name"] == "fetch_survey"
    assert row["ask"] == {"purpose": "bathymetry", "bbox": PORTLAND_BOX}
    assert row["resolution"] == "1 m"
    assert "fetch_survey" in found["sentence"]


@pytest.mark.asyncio
async def test_a_point_is_read_as_the_ground_it_stands_on(world):
    world([("fetch_survey", surface())])
    found = await find_sources("bathymetry", list(WILLAMETTE))
    west, south, east, north = found["results"][0]["ask"]["bbox"]
    assert west < WILLAMETTE[0] < east and south < WILLAMETTE[1] < north
    assert east - west < 0.01


@pytest.mark.asyncio
async def test_a_tie_says_so_and_every_survivor_carries_its_own_ask(world):
    world([("fetch_east", surface()), ("fetch_west", surface())])
    found = await find_sources("bathymetry", PORTLAND_BOX)
    assert found["tie"] is True
    assert [row["tool_name"] for row in found["results"]] == ["fetch_east",
                                                              "fetch_west"]
    assert all(row["ask"]["bbox"] == PORTLAND_BOX for row in found["results"])


@pytest.mark.asyncio
async def test_nothing_measuring_it_here_names_every_source_and_why(world):
    world([("fetch_europe", surface(rings=EUROPE, note="Europe only"))])
    found = await find_sources("bathymetry", PORTLAND_BOX)
    assert found["results"] == []
    assert found["picked"] == ""
    assert "fetch_europe" in found["sentence"]
    assert "State the value on the call" in found["sentence"]
    dropped = found["choice"]["rows"][0]
    assert "Europe only" in dropped["excluded"]


@pytest.mark.asyncio
async def test_a_window_a_series_does_not_cover_drops_it(world):
    from tests.search.test_source_match import gauges

    world([("fetch_gauges", gauges(earliest="2020-01-01",
                                   latest="2021-01-01"))],
          params={"bbox": {}, "start_date": {}, "end_date": {}})
    found = await find_sources("discharge series", PORTLAND_BOX,
                               ["2024-05-01", "2024-05-08"])
    assert found["picked"] == ""
    assert "reports to 2021-01-01" in found["sentence"]


@pytest.mark.asyncio
async def test_a_series_source_is_called_with_the_window(world):
    from tests.search.test_source_match import gauges

    world([("fetch_gauges", gauges(rings=CONUS))],
          params={"bbox": {}, "start_date": {}, "end_date": {}})
    found = await find_sources("discharge series", PORTLAND_BOX,
                               ["2024-05-01", "2024-05-08"])
    assert found["results"][0]["ask"]["start_date"] == "2024-05-01"
    assert found["results"][0]["ask"]["end_date"] == "2024-05-08"


@pytest.mark.asyncio
async def test_a_class_outside_the_vocabulary_refuses_naming_it():
    with pytest.raises(fs.FindSourcesError) as caught:
        await find_sources("vibes", PORTLAND_BOX)
    assert "'vibes'" in str(caught.value)
    assert "bathymetry" in str(caught.value)


@pytest.mark.asyncio
async def test_a_class_no_row_serves_refuses_naming_the_class(world):
    world([("fetch_survey", surface())])
    with pytest.raises(fs.FindSourcesError) as caught:
        await find_sources("fuels", PORTLAND_BOX)
    assert "fuels" in str(caught.value)


@pytest.mark.asyncio
async def test_a_source_on_the_list_twice_is_called_with_each_rows_own_ask(world):
    """A source serving a measured and a predicted series of one class is two
    candidates, and each carries the values that make it answer with THAT row."""
    from tests.search.test_source_match import DURING, UNTIL, gauges

    measured = gauges(data_class="water level series")
    predicted = gauges(data_class="water level series", kind="predicted")
    predicted.ask = {"product": "predictions"}
    world([("fetch_tides", measured), ("fetch_tides", predicted)],
          params={"bbox": {}})
    found = await find_sources("water level series", PORTLAND_BOX,
                               window=[DURING, UNTIL])
    asks = {row["kind"]: row["ask"] for row in found["results"]}
    assert asks["measured"] == {"purpose": "water level series",
                                "bbox": PORTLAND_BOX}
    assert asks["predicted"]["product"] == "predictions"
