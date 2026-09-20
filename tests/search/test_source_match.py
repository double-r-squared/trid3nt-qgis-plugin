"""The match, offline and pure over declarations.

One survivor, a tie, none with the exclusions named, a loosened window, rung
zero, a static survey older than the run, a series outside the window, the kind
and the distance the sort leads on, a station beyond its reach, and the pick a
run states."""

from __future__ import annotations

import pytest

from trid3nt_contracts.coverage import (
    Coverage, CoverageExtent, CoveragePoint, CoverageWindow)

from trid3nt_server.workflows.runtime.errors import PlanValidationError
from trid3nt_server.tools.search.match import (
    LOOSEN_WINDOW,
    Need,
    dropped_from,
    match,
)

CONUS = [[(-125.0, 24.0), (-66.0, 24.0), (-66.0, 50.0), (-125.0, 50.0)]]
EUROPE = [[(-10.0, 36.0), (30.0, 36.0), (30.0, 60.0), (-10.0, 60.0)]]

WILLAMETTE = (-122.67, 45.52)


def surface(data_class="bathymetry", rings=None, res=1.0, datum="NAVD88",
            latest="2024-01-01", note="a stated extent", kind="measured"):
    return Coverage(
        data_class=data_class, kind=kind,
        extent=CoverageExtent(kind="surface", rings=rings or CONUS, note=note),
        window=CoverageWindow(series=False, latest=latest),
        resolution_m=res, datum=datum, units={"elevation": "m"})


def gauges(data_class="discharge series", rings=None, earliest=None,
           latest=None, units=None, kind="measured", reach_km=25.0,
           note="a gauge network"):
    return Coverage(
        data_class=data_class, kind=kind, reach_km=reach_km,
        extent=CoverageExtent(kind="stations", rings=rings or CONUS, note=note,
                              discover="bbox"),
        window=CoverageWindow(series=True, earliest=earliest, latest=latest,
                              cadence="hourly"),
        units=units or {"time_series_csv": "ft3/s"})


def bed_need(**over):
    kwargs = dict(slot="bed", data_class="bathymetry", lon=WILLAMETTE[0],
                  lat=WILLAMETTE[1], frame="NAVD88", mesh_m=20.0)
    kwargs.update(over)
    return Need(**kwargs)


def test_one_survivor_fills_the_slot_and_the_sentence_names_the_facts():
    choice = match(bed_need(), [("fetch_survey", surface())])
    assert choice.picked == "fetch_survey"
    assert not choice.tie
    assert "fetch_survey" in choice.sentence
    assert "1 m" in choice.sentence and "NAVD88" in choice.sentence


def test_a_source_too_coarse_for_the_mesh_ranks_below_every_source_that_resolves_it():
    choice = match(bed_need(mesh_m=20.0), [
        ("fetch_fine", surface(res=1.0)),
        ("fetch_matched", surface(res=10.0)),
        ("fetch_coarse", surface(res=90.0)),
        ("fetch_coarser", surface(res=450.0)),
    ])
    assert choice.picked == "fetch_fine"
    assert [row.fetcher for row in choice.rows] == [
        "fetch_fine", "fetch_matched", "fetch_coarse", "fetch_coarser"]


def test_a_source_already_on_the_run_s_frame_outranks_one_that_needs_a_shift():
    choice = match(bed_need(), [
        ("fetch_other_zero", surface(datum="MLLW")),
        ("fetch_navd", surface(datum="NAVD88")),
    ])
    assert choice.picked == "fetch_navd"
    assert not choice.tie


def test_several_equal_on_every_fact_present_as_a_tie():
    choice = match(bed_need(), [
        ("fetch_a", surface()), ("fetch_b", surface())])
    assert choice.tie
    assert [row.fetcher for row in choice.rows] == ["fetch_a", "fetch_b"]


def test_the_ranked_list_carries_at_most_five_rows():
    choice = match(bed_need(), [(f"fetch_{i}", surface(res=float(i + 1)))
                                for i in range(9)])
    assert len(choice.rows) == 5


def test_a_place_no_source_reaches_refuses_naming_what_each_filter_excluded():
    choice = match(bed_need(), [("fetch_europe", surface(rings=EUROPE,
                                                         note="Europe only"))])
    assert choice.picked == ""
    assert "fetch_europe" in choice.sentence
    assert "Europe only" in choice.sentence
    assert choice.rows[0].excluded


def test_a_class_nothing_measures_refuses_without_listing_another_class():
    choice = match(bed_need(data_class="fuels"), [("fetch_survey", surface())])
    assert choice.picked == ""
    assert choice.rows == []
    assert "fuels" in choice.sentence


def test_a_static_survey_older_than_the_run_is_never_dropped_by_the_window():
    choice = match(bed_need(opens="2026-09-13T22:00:00Z"),
                   [("fetch_survey_2014", surface(latest="2014-06-01"))])
    assert choice.picked == "fetch_survey_2014"


def test_a_series_outside_the_window_refuses_with_the_nearest_record_named():
    need = Need(slot="carrier", data_class="discharge series", lon=WILLAMETTE[0],
                lat=WILLAMETTE[1], opens="1994-01-01T00:00:00Z",
                until="1994-01-02T00:00:00Z")
    choice = match(need, [("fetch_gauges", gauges(earliest="2007-10-01"))])
    assert choice.picked == ""
    assert "2007-10-01" in choice.rows[0].excluded
    assert "fetch_gauges" in choice.sentence


def test_one_run_level_statement_loosens_the_window_as_the_user_s_choice():
    need = Need(slot="carrier", data_class="discharge series", lon=WILLAMETTE[0],
                lat=WILLAMETTE[1], opens="1994-01-01T00:00:00Z",
                loosen=LOOSEN_WINDOW)
    choice = match(need, [("fetch_gauges", gauges(earliest="2007-10-01"))])
    assert choice.picked == "fetch_gauges"
    assert choice.loosened == LOOSEN_WINDOW
    assert "the user's choice" in choice.sentence


def test_a_probe_that_found_nothing_moves_the_pick_to_the_next_survivor():
    choice = match(bed_need(), [
        ("fetch_survey", surface(res=10.0)),
        ("fetch_terrain", surface(res=10.0, latest="2020-01-01")),
    ])
    assert choice.picked == "fetch_survey"
    after = dropped_from(choice, "fetch_survey", "held nothing over this domain")
    assert after.picked == "fetch_terrain"
    assert after.rows[0].excluded == "held nothing over this domain"
    assert "fetch_terrain fills the slot" in after.sentence


def test_a_probe_that_exhausts_every_survivor_says_so():
    choice = match(bed_need(), [("fetch_survey", surface())])
    after = dropped_from(choice, "fetch_survey", "held nothing over this domain")
    assert after.picked == ""
    assert "nothing measures bathymetry here" in after.sentence
    assert "fetch_survey held nothing over this domain" in after.sentence


def test_a_need_with_no_data_class_is_refused_at_declaration():
    with pytest.raises(PlanValidationError):
        Need(slot="bed", data_class="")


#: A gauge a tenth of a kilometre off the place, and the coast a hundred
#: kilometres from the nearest tide station - the two distances the sort reads.
AT_THE_PLACE = [[(-122.671, 45.519), (-122.669, 45.519), (-122.669, 45.521),
                 (-122.671, 45.521)]]
DOWN_THE_ESTUARY = [[(-124.5, 45.0), (-124.0, 45.0), (-124.0, 46.0),
                     (-124.5, 46.0)]]


def level_need(**over):
    kwargs = dict(slot="stage", data_class="water level series",
                  lon=WILLAMETTE[0], lat=WILLAMETTE[1])
    kwargs.update(over)
    return Need(**kwargs)


def test_a_measured_gauge_at_the_place_outranks_a_modelled_grid_over_it():
    choice = match(Need(slot="carrier", data_class="discharge series",
                        lon=WILLAMETTE[0], lat=WILLAMETTE[1]), [
        ("fetch_model_grid", Coverage(
            data_class="discharge series", kind="modelled",
            extent=CoverageExtent(kind="surface", rings=CONUS,
                                  note="a modelled channel network"),
            window=CoverageWindow(series=True, cadence="hourly"),
            units={"streamflow_cms": "m3/s"})),
        ("fetch_gauges", gauges(rings=AT_THE_PLACE)),
    ])
    assert choice.picked == "fetch_gauges"
    assert not choice.tie
    assert choice.rows[0].kind == "measured"


def test_a_measured_gauge_outranks_a_prediction_a_hundred_kilometres_away():
    choice = match(level_need(), [
        ("fetch_tides", gauges(data_class="water level series",
                               rings=DOWN_THE_ESTUARY, kind="predicted",
                               reach_km=200.0, units={"water_level": "m"})),
        ("fetch_gauges", gauges(data_class="water level series",
                                rings=AT_THE_PLACE, units={"water_level": "m"})),
    ])
    assert choice.picked == "fetch_gauges"
    assert not choice.tie
    assert choice.rows[1].distance == "about 104 km away"


def test_a_station_beyond_its_reach_is_not_a_survivor_at_all():
    coast = level_need(lon=-124.4, lat=45.5)
    choice = match(coast, [
        ("fetch_gauges", gauges(data_class="water level series",
                                rings=AT_THE_PLACE, units={"water_level": "m"})),
        ("fetch_tides", gauges(data_class="water level series",
                               rings=DOWN_THE_ESTUARY, kind="predicted",
                               units={"water_level": "m"})),
    ])
    assert choice.picked == "fetch_tides"
    excluded = next(row for row in choice.rows if row.fetcher == "fetch_gauges")
    assert "km away" in excluded.excluded and "serves 25 km" in excluded.excluded


def test_a_run_that_picks_a_survivor_takes_it_as_the_user_s_choice():
    choice = match(level_need(pick="fetch_tides"), [
        ("fetch_gauges", gauges(data_class="water level series",
                                rings=AT_THE_PLACE, units={"water_level": "m"})),
        ("fetch_tides", gauges(data_class="water level series", kind="predicted",
                               units={"water_level": "m"})),
    ])
    assert choice.picked == "fetch_tides"
    assert choice.picked_by_user and not choice.tie
    assert "the user's choice" in choice.sentence
    assert [row.fetcher for row in choice.rows] == ["fetch_tides", "fetch_gauges"]


def test_a_run_that_picks_a_source_the_filters_excluded_is_refused():
    with pytest.raises(PlanValidationError) as caught:
        match(level_need(pick="fetch_europe"), [
            ("fetch_europe", gauges(data_class="water level series",
                                    rings=EUROPE, note="Europe only",
                                    units={"water_level": "m"}))])
    assert "fetch_europe" in str(caught.value)
    assert "not a survivor" in str(caught.value)


def test_a_source_called_by_station_with_no_stations_listed_is_not_a_survivor(monkeypatch):
    from types import SimpleNamespace

    from trid3nt_server.tools.fetchers._router import registration
    from trid3nt_server.tools.search import match as m

    spec = SimpleNamespace(params={"station": {"required": True},
                                   "start_date": {"required": True}})
    monkeypatch.setitem(registration._SPEC_REGISTRY, "fetch_by_station", spec)
    discovered = gauges(data_class="water level series")
    assert "called by station" in m._unaskable("fetch_by_station", discovered)
    listed = gauges(data_class="water level series")
    listed.extent.points = [CoveragePoint(id="ARROWROCK", lon=-115.92,
                                          lat=43.59)]
    assert m._unaskable("fetch_by_station", listed) == ""
    assert m._unaskable("fetch_nobody_registered", discovered) == ""


def test_the_probe_names_the_nearest_listed_station_and_the_rows_own_ask(
        monkeypatch):
    """A row that lists its stations answers the station the source is called by,
    and the values that make the source answer with THAT row travel with it."""
    from types import SimpleNamespace

    from trid3nt_server.tools.fetchers._router import registration
    from trid3nt_server.tools.search import match as m

    row = gauges(data_class="water level series", kind="predicted")
    row.ask = {"product": "predictions"}
    row.extent.points = [CoveragePoint(id="far", lon=-120.0, lat=44.0),
                         CoveragePoint(id="near", lon=-122.66, lat=45.50)]
    spec = SimpleNamespace(params={"station": {"required": True}},
                           coverage=[row])
    monkeypatch.setitem(registration._SPEC_REGISTRY, "fetch_by_station", spec)
    choice = match(Need(slot="level", data_class="water level series",
                        lon=WILLAMETTE[0], lat=WILLAMETTE[1]),
                   [("fetch_by_station", row)])
    assert choice.picked == "fetch_by_station"
    ask = m.ask_for(choice, {"bbox": [0, 0, 1, 1]}, *WILLAMETTE)
    assert ask == {"bbox": [0, 0, 1, 1], "product": "predictions",
                   "station": "near"}


def test_a_measured_series_holds_no_record_after_now():
    """An instrument has not recorded tomorrow, so a run opening past now takes
    the prediction and not the record."""
    from datetime import datetime, timedelta, timezone

    ahead = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    need = Need(slot="level", data_class="water level series",
                lon=WILLAMETTE[0], lat=WILLAMETTE[1], opens=ahead, until=ahead)
    choice = match(need, [
        ("fetch_record", gauges(data_class="water level series")),
        ("fetch_forecast", gauges(data_class="water level series",
                                  kind="predicted"))])
    assert choice.picked == "fetch_forecast"
    assert any("reports to now" in row.excluded for row in choice.rows
               if row.fetcher == "fetch_record")


#: A short reach of one river, as the domain the run drew it: about 6 km of
#: water, with a gauge on it and another river's gauge 20 km off.
REACH = CoverageExtent(
    kind="surface", note="the domain's own polygon",
    rings=[[(-122.70, 45.50), (-122.64, 45.50), (-122.64, 45.54),
            (-122.70, 45.54), (-122.70, 45.50)]])


def listed(*points, data_class="discharge series"):
    row = gauges(data_class=data_class)
    row.extent = row.extent.model_copy(
        update={"discover": "", "points": list(points)})
    return row


def flow_need(**over):
    kwargs = dict(slot="carrier", data_class="discharge series",
                  lon=WILLAMETTE[0], lat=WILLAMETTE[1], mesh_m=50.0,
                  water=REACH)
    kwargs.update(over)
    return Need(**kwargs)


def test_a_gauge_on_another_river_is_not_this_domain_s_discharge():
    """A discharge is the water passing one section: a gauge 20 km off this
    domain's water reports another flow, however near the reach it stands."""
    other = CoveragePoint(id="14211720", lon=-122.384, lat=45.52)
    choice = match(flow_need(), [("fetch_gauges", listed(other))])
    assert choice.picked == ""
    excluded = choice.rows[0].excluded
    assert "14211720" in excluded and "20 km off" in excluded
    assert "the domain's own polygon" in excluded


def test_a_gauge_on_the_reach_survives_the_place_filter():
    on_it = CoveragePoint(id="14211720", lon=-122.67, lat=45.52)
    choice = match(flow_need(), [("fetch_gauges", listed(on_it))])
    assert choice.picked == "fetch_gauges"


def test_the_reach_still_answers_for_a_water_level_gauge():
    """A level propagates, so the reach rule stands for it: the same gauge off
    the domain's water is within reach and survives."""
    other = CoveragePoint(id="14211720", lon=-122.384, lat=45.52)
    choice = match(flow_need(data_class="water level series"),
                   [("fetch_gauges", listed(other,
                                            data_class="water level series"))])
    assert choice.picked == "fetch_gauges"


def test_a_box_called_source_is_asked_over_a_box_that_reaches_its_station(
        monkeypatch):
    """A station set ranked on its nearest station and called by box: the box the
    source is asked over holds that station, because the reach is what put the
    source on the list."""
    from types import SimpleNamespace

    from trid3nt_server.tools.fetchers._router import registration
    from trid3nt_server.tools.search import match as m

    row = gauges(data_class="water level series", kind="predicted")
    row.extent = row.extent.model_copy(update={
        "discover": "",
        "points": [CoveragePoint(id="8455083", lon=-122.60, lat=45.49)]})
    spec = SimpleNamespace(params={"bbox": {"required": True}}, coverage=[row])
    monkeypatch.setitem(registration._SPEC_REGISTRY, "fetch_tides", spec)
    choice = match(Need(slot="stage", data_class="water level series",
                        lon=WILLAMETTE[0], lat=WILLAMETTE[1]),
                   [("fetch_tides", row)])
    ask = m.ask_for(choice, {"bbox": [-122.68, 45.51, -122.66, 45.53]},
                    *WILLAMETTE)
    west, south, east, north = ask["bbox"]
    assert west <= -122.68 and south <= 45.49 and east >= -122.60
    assert north >= 45.53
