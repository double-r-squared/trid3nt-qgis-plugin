"""The match, offline and pure over declarations.

One survivor, a tie, none with the exclusions named, a loosened window, rung
zero, a static survey older than the run, a series outside the window, and the
two unit cases a slot lives or dies on."""

from __future__ import annotations

import pytest

from trid3nt_contracts.coverage import Coverage, CoverageExtent, CoverageWindow

from trid3nt_server.workflows.runtime.errors import PlanValidationError
from trid3nt_server.workflows.runtime.match import (
    LOOSEN_WINDOW,
    Need,
    dropped_from,
    match,
)

CONUS = [[(-125.0, 24.0), (-66.0, 24.0), (-66.0, 50.0), (-125.0, 50.0)]]
EUROPE = [[(-10.0, 36.0), (30.0, 36.0), (30.0, 60.0), (-10.0, 60.0)]]

WILLAMETTE = (-122.67, 45.52)


def surface(data_class="bathymetry", rings=None, res=1.0, datum="NAVD88",
            latest="2024-01-01", note="a stated extent"):
    return Coverage(
        data_class=data_class,
        extent=CoverageExtent(kind="surface", rings=rings or CONUS, note=note),
        window=CoverageWindow(series=False, latest=latest),
        resolution_m=res, datum=datum, units={"elevation": "m"})


def gauges(data_class="discharge series", rings=None, earliest=None,
           latest=None, units=None):
    return Coverage(
        data_class=data_class,
        extent=CoverageExtent(kind="stations", rings=rings or CONUS,
                              note="a gauge network"),
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
    assert "no other source states coverage" in after.sentence


def test_a_need_with_no_data_class_is_refused_at_declaration():
    with pytest.raises(PlanValidationError):
        Need(slot="bed", data_class="")
