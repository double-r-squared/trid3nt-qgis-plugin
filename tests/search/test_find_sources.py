"""``find_sources`` and ``fetch`` by handle, offline and pure over declarations.

A row comes back filled and held under an ``S<n>`` the case never reuses; ``fetch``
sends it as shown, merges edits, and refuses a name the source does not take or an
edit that leaves what the source reaches. The fixtures are the match's."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from importlib import import_module

from datetime import datetime, timezone

from trid3nt_server.tools.search.fetch import (
    FetchArgError, FetchReachError, FetchSourceError, resolve_fetch)
from trid3nt_server.tools.search.find_sources import find_sources

# The package re-exports the tool under its own name, so the MODULE the world
# fixture patches is reached by import rather than by attribute.
fs = import_module("trid3nt_server.tools.search.find_sources.find_sources")

from tests.search.test_source_match import CONUS, EUROPE, WILLAMETTE, surface

PORTLAND_BOX = [-122.72, 45.48, -122.62, 45.56]
#: Open water off the Oregon shelf: outside every Great Lakes ring.
OREGON_SHELF_BOX = [-124.6, 45.9, -124.5, 46.0]


@pytest.fixture(autouse=True)
def case():
    """One case's registry active, as the dispatch binds it, so every row is held under a handle."""
    from trid3nt_server.render.uri_registry import (
        SessionUriRegistry, activate_registry, deactivate_registry)

    reg = SessionUriRegistry(session_id="case-one")
    token = activate_registry(reg)
    yield reg
    deactivate_registry(token)


@pytest.fixture
def world(monkeypatch):
    """Register a set of (source, row) pairs as the whole world, with the params
    each source states, so the ask is built off declarations alone."""
    from trid3nt_contracts.tool_registry import AtomicToolMetadata
    from trid3nt_server.tools import TOOL_REGISTRY, RegisteredTool
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
            monkeypatch.setitem(TOOL_REGISTRY, name, RegisteredTool(
                metadata=AtomicToolMetadata(name=name, ttl_class="live-no-cache", source_class=None),
                fn=lambda **kw: kw, module=__name__))
        monkeypatch.setattr(fs, "sources_with_coverage", lambda: list(pairs))

    return _register


def held(case, handle):
    return case.held_request(handle)["args"]


@pytest.mark.asyncio
async def test_one_survivor_comes_back_filled_under_a_handle(world, case):
    world([("fetch_survey", surface())])
    found = await find_sources("bathymetry", PORTLAND_BOX)
    assert not found["tie"]
    row = found["results"][0]
    assert row["source"] == "fetch_survey" and row["handle"] == "S1"
    assert row["status"] == "READY" and row["box"] == PORTLAND_BOX
    assert held(case, "S1") == {"purpose": "bathymetry", "bbox": PORTLAND_BOX}
    assert "1 m" in row["what"]
    assert "fetch_survey" in found["sentence"]


@pytest.mark.asyncio
async def test_a_point_is_read_as_the_ground_it_stands_on(world):
    world([("fetch_survey", surface())])
    found = await find_sources("bathymetry", list(WILLAMETTE))
    west, south, east, north = found["results"][0]["box"]
    assert west < WILLAMETTE[0] < east and south < WILLAMETTE[1] < north
    assert east - west < 0.01


@pytest.mark.asyncio
async def test_a_tie_says_so_and_every_survivor_carries_its_own_ask(world, case):
    world([("fetch_east", surface()), ("fetch_west", surface())])
    found = await find_sources("bathymetry", PORTLAND_BOX)
    assert found["tie"] is True
    assert [row["source"] for row in found["results"]] == ["fetch_east",
                                                              "fetch_west"]
    assert all(held(case, row["handle"])["bbox"] == PORTLAND_BOX for row in found["results"])


@pytest.mark.asyncio
async def test_nothing_measuring_it_here_names_every_source_and_why(world):
    world([("fetch_europe", surface(rings=EUROPE, note="Europe only"))])
    found = await find_sources("bathymetry", PORTLAND_BOX)
    assert found["results"] == []
    assert "fetch_europe" in found["sentence"]
    assert "State the value on the call" in found["sentence"]
    away, = found["not reaching"]
    assert away.startswith("fetch_europe: ") and "Europe only" in away
    assert away.endswith("edit: a box inside Europe only")


@pytest.mark.asyncio
async def test_a_window_a_series_does_not_cover_drops_it(world):
    from tests.search.test_source_match import gauges

    world([("fetch_gauges", gauges(earliest="2020-01-01",
                                   latest="2021-01-01"))],
          params={"bbox": {}, "start_date": {}, "end_date": {}})
    found = await find_sources("discharge series", PORTLAND_BOX,
                               ["2024-05-01", "2024-05-08"])
    assert found["results"] == []
    assert "reports to 2021-01-01" in found["sentence"]
    assert found["not reaching"][0].endswith("edit: a window inside 2020-01-01 to 2021-01-01")


@pytest.mark.asyncio
async def test_a_series_source_is_called_with_a_window_bracketing_the_run(world, case):
    """An hourly row is asked from the hour before the window opens, which is
    the day before where the window opens at a midnight."""
    from tests.search.test_source_match import gauges

    world([("fetch_gauges", gauges(rings=CONUS))],
          params={"bbox": {}, "start_date": {}, "end_date": {}})
    found = await find_sources("discharge series", PORTLAND_BOX,
                               ["2024-05-01", "2024-05-08"])
    ask = held(case, found["results"][0]["handle"])
    assert (ask["start_date"], ask["end_date"]) == ("2024-04-30", "2024-05-08")
    assert found["results"][0]["window"] == ["2024-05-01T00:00:00+00:00",
                                             "2024-05-08T00:00:00+00:00"]


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
async def test_a_source_on_the_list_twice_is_called_with_each_rows_own_ask(world, case):
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
    asks = {row["what"].split()[0]: held(case, row["handle"]) for row in found["results"]}
    assert asks["measured"] == {"purpose": "water level series",
                                "bbox": PORTLAND_BOX}
    assert asks["predicted"]["product"] == "predictions"


@pytest.mark.asyncio
async def test_the_gauges_and_the_portal_both_measure_a_sample_at_portland():
    """The REGISTERED world, not a fixture: a water quality sample asked for at
    Portland lists the gauge network beside the sample portal, and the sort puts
    the gauges first. Both stand there, so a slot that opens on a temperature
    the gauges report continuously is no longer answered only by the portal's
    discrete archive."""
    found = await find_sources("water quality sample", PORTLAND_BOX,
                               ["2026-09-17T00:00:00Z", "2026-09-20T00:00:00Z"])
    named = [row["source"] for row in found["results"]]
    assert "fetch_usgs_nwis_gauges" in named and "fetch_usgs_water_quality" in named
    assert named.index("fetch_usgs_nwis_gauges") < named.index("fetch_usgs_water_quality")


@pytest.mark.asyncio
async def test_a_bed_asked_for_off_the_oregon_coast_excludes_the_lakes_by_name():
    """The REGISTERED world through the EXCLUDED path: every row the place
    filter drops renders its extent note behind the match's own prefix, and a
    note longer than the wire field takes makes the whole answer unsendable -
    so the ocean box the lake sources do not reach is what proves the notes fit.
    """
    found = await find_sources("bathymetry", OREGON_SHELF_BOX)
    dropped = {line.split(":", 1)[0]: line for line in found["not reaching"]}
    assert "does not reach this place" in dropped["fetch_chs_nonna"]
    assert "the Great Lakes and the connecting channels" in dropped["fetch_chs_nonna"]
    named = [row["source"] for row in found["results"]]
    assert "fetch_bluetopo" in named and "fetch_cudem" in named
    # The global relief MODEL reaches everywhere and still ranks under every
    # measured row, so the coarse bed is offered and never picked first.
    assert named[-1] == "fetch_etopo"


@pytest.mark.asyncio
async def test_the_gauge_row_is_called_by_the_code_its_vocabulary_names():
    """A slot naming the variable it observes reaches the gauges through the
    same ``need:of`` the portal is reached through: the row maps TEMPERATURE
    onto the source's own word, which here is the NWIS parameter code."""
    from trid3nt_server.tools.search.match import (
        Need, ask_for, base_ask, match, sources_with_coverage)

    need = Need(slot="observe", data_class="water quality sample",
                lon=-122.67, lat=45.51, of="TEMPERATURE",
                opens="2026-09-17T00:00:00Z", until="2026-09-20T00:00:00Z",
                pick="fetch_usgs_nwis_gauges")
    choice = match(need, sources_with_coverage())
    ask = ask_for(choice, base_ask(choice, "observe", PORTLAND_BOX,
                                   need.lon, need.lat, need.opens, need.until),
                  need.lon, need.lat, {"of": need.of})
    assert ask["parameter"] == "00010"
    # The gauges publish instantaneous samples from five minutes apart, so the
    # five minutes before the opening fall on the day before it.
    assert (ask["start_date"], ask["end_date"]) == ("2026-09-16", "2026-09-20")


@pytest.mark.asyncio
async def test_an_unstated_window_is_the_moment_now_shown_on_the_row(world):
    """A series source is asked at now rather than dropped for want of a moment."""
    from tests.search.test_source_match import gauges

    world([("fetch_gauges", gauges(rings=CONUS))],
          params={"bbox": {}, "start_date": {}, "end_date": {}})
    found = await find_sources("discharge series", PORTLAND_BOX)
    row, = found["results"]
    assert row["window"].startswith(datetime.now(timezone.utc).strftime("%Y-%m-%d"))


@pytest.mark.asyncio
async def test_a_moment_leaves_the_series_off_and_a_span_switches_it_on(world, case):
    from tests.search.test_source_match import DURING, UNTIL, gauges

    row = gauges(data_class="water level series", kind="predicted")
    row.window.series_param = "include_series"
    world([("fetch_forecast", row)], params={"bbox": {}, "include_series": {}, "gauge_id": {}})
    moment = (await find_sources("water level series", PORTLAND_BOX, [DURING]))["results"][0]
    span = (await find_sources("water level series", PORTLAND_BOX, [DURING, UNTIL]))["results"][0]
    assert "include_series" not in held(case, moment["handle"])
    assert held(case, span["handle"])["include_series"] is True
    assert span["also accepts"] == "gauge_id"


@pytest.mark.asyncio
async def test_a_required_param_nothing_deduces_is_shown_as_needed(world):
    world([("fetch_survey", surface())], params={"bbox": {}, "layer": {"required": True}})
    row, = (await find_sources("bathymetry", PORTLAND_BOX))["results"]
    assert row["status"] == "needs: layer"


@pytest.mark.asyncio
async def test_fetch_by_handle_sends_what_the_row_showed(world, case):
    world([("fetch_survey", surface())])
    row, = (await find_sources("bathymetry", PORTLAND_BOX))["results"]
    name, kwargs = resolve_fetch({"source": row["handle"]}, case)
    assert name == "fetch_survey"
    assert kwargs["bbox"] == row["box"]


@pytest.mark.asyncio
async def test_an_edit_merges_and_a_moved_box_is_filled_again(world, case):
    world([("fetch_survey", surface())], params={"bbox": {}, "tile": {}})
    row, = (await find_sources("bathymetry", PORTLAND_BOX))["results"]
    moved = [-122.70, 45.50, -122.65, 45.53]
    name, kwargs = resolve_fetch({"source": row["handle"],
                                  "edits": {"box": moved, "tile": "a"}}, case)
    assert kwargs["bbox"] == moved and kwargs["tile"] == "a"
    assert held(case, row["handle"])["bbox"] == PORTLAND_BOX


@pytest.mark.asyncio
async def test_an_edit_the_source_does_not_declare_refuses_naming_the_editable_names(world, case):
    world([("fetch_survey", surface())], params={"bbox": {}, "tile": {}})
    row, = (await find_sources("bathymetry", PORTLAND_BOX))["results"]
    with pytest.raises(FetchArgError) as caught:
        resolve_fetch({"source": row["handle"], "edits": {"resolution": 5}}, case)
    assert str(caught.value) == ("'resolution' is not editable on S1; its editable names: "
                                 "box, window, bbox, tile.")


@pytest.mark.asyncio
async def test_an_edit_outside_what_the_source_reaches_refuses(world, case):
    world([("fetch_survey", surface())])
    row, = (await find_sources("bathymetry", PORTLAND_BOX))["results"]
    with pytest.raises(FetchReachError) as caught:
        resolve_fetch({"source": row["handle"], "edits": {"box": [10.0, 50.0, 10.1, 50.1]}}, case)
    assert str(caught.value).startswith("S1 does not reach that box: ")
    assert str(caught.value).endswith("; find_sources again.")


@pytest.mark.asyncio
async def test_handles_never_repeat_within_a_case_even_after_it_is_reopened(world, case):
    world([("fetch_east", surface()), ("fetch_west", surface())])
    first = [r["handle"] for r in (await find_sources("bathymetry", PORTLAND_BOX))["results"]]
    persisted = case.export_short_handles()
    case.replace_from_layers([], short_handles=persisted)
    second = [r["handle"] for r in (await find_sources("bathymetry", PORTLAND_BOX))["results"]]
    assert first == ["S1", "S2"] and second == ["S3", "S4"]
    assert case.held_request("S1")["source"] == "fetch_east"


@pytest.mark.asyncio
async def test_an_unknown_handle_refuses_and_the_declarative_form_still_calls_by_name(world, case):
    world([("fetch_survey", surface())])
    with pytest.raises(FetchSourceError, match="no row S9 in this case"):
        resolve_fetch({"source": "S9"}, case)
    assert resolve_fetch({"source": "survey", "args": {"bbox": [0, 0, 1, 1]}}) == (
        "fetch_survey", {"bbox": [0, 0, 1, 1]})
