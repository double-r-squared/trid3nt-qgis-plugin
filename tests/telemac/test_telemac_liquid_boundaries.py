"""The LIQUID BOUNDARIES FILE: a boundary whose record measured a window.

Covered: the column name the engine builds per boundary number, the table's own
shape (one comment, a header the scan splits, a skipped units line, one clock),
the row past the end the reader refuses to run off, the steering list left
standing for every boundary the file does not carry a column for, a run where
nothing measured a window writing no file at all, and the refusals.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime import Series
from trid3nt_server.workflows.telemac.authoring.boundaries import (
    LIQUID_BOUNDARIES_FILENAME,
    column_name,
    liquid_boundaries_file,
)
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules.telemac2d import _boundaries

_FLOW = Series([0.0, 3600.0, 7200.0], [10.0, 20.0, 30.0], units="m3/s")
_LEVEL = Series([0.0, 3600.0, 7200.0], [2.0, 2.5, 3.0], units="m")


def _settled(**over):
    base = {"liquid_boundary_order": ["inflow", "outflow"],
            "liquid_boundary_prescribes": ["flowrate", "elevation"],
            "inflow_q_m3s": 12.0, "outflow_stage_m": 2.2,
            "inflow_q_series": None, "outflow_stage_series": None,
            "start_time_s": 0.0, "until_s": 7200.0, "time_step_s": 5.0}
    base.update(over)
    return base


def test_the_column_is_named_the_way_the_engine_builds_it():
    assert column_name("flowrate", 1) == "Q(1)"
    assert column_name("elevation", 12) == "SL(12)"


def test_a_boundary_the_engine_reads_nothing_at_refuses_a_series():
    with pytest.raises(TelemacError) as err:
        column_name("nothing", 1)
    assert err.value.error_code == "TELEMAC_BOUNDARY_SERIES_UNREAD"


def test_the_table_carries_one_clock_a_header_and_a_skipped_units_line():
    text = liquid_boundaries_file(
        [("Q(1)", "m3/s", _FLOW)], start_s=0.0, until_s=7200.0, tail_s=100.0,
        note="the gauge")
    lines = text.splitlines()
    assert lines[0] == "#the gauge"
    assert lines[1] == "T Q(1)"
    assert lines[2] == "s m3/s"
    assert "\t" not in text
    assert text.endswith("\n")
    instants = [float(line.split()[0]) for line in lines[3:]]
    assert instants == sorted(instants)
    assert instants[0] == 0.0


def test_the_table_closes_past_the_instant_the_run_ends_at():
    text = liquid_boundaries_file(
        [("Q(1)", "m3/s", _FLOW)], start_s=0.0, until_s=7200.0, tail_s=100.0)
    assert float(text.splitlines()[-1].split()[0]) == 7300.0


def test_two_columns_share_the_time_column_the_reader_takes():
    text = liquid_boundaries_file(
        [("Q(1)", "m3/s", _FLOW),
         ("SL(2)", "m", Series([0.0, 1800.0, 7200.0], [2.0, 2.4, 3.0],
                               units="m"))],
        start_s=0.0, until_s=7200.0, tail_s=100.0)
    rows = [line.split() for line in text.splitlines()[3:]]
    assert all(len(row) == 3 for row in rows)
    assert [row[0] for row in rows] == ["0.000", "1800.000", "3600.000",
                                        "7200.000", "7300.000"]


def test_a_file_with_no_column_refuses_rather_than_being_opened_empty():
    with pytest.raises(TelemacError) as err:
        liquid_boundaries_file([], start_s=0.0, until_s=10.0, tail_s=1.0)
    assert err.value.error_code == "TELEMAC_BOUNDARY_SERIES_EMPTY"


def test_a_measured_window_writes_its_column_and_the_list_still_stands():
    keywords, files = _boundaries(
        {"measured": _settled(inflow_q_series=_FLOW), "tracers": [0.0]})
    assert keywords["LIQUID_BOUNDARIES_FILE"] == LIQUID_BOUNDARIES_FILENAME
    # The engine reads the steering list for every column the file omits, so
    # the outflow's number is still written down.
    assert keywords["PRESCRIBED_ELEVATIONS"] == [0.0, 2.2]
    assert keywords["PRESCRIBED_FLOWRATES"] == [12.0, 0.0]
    header = files[LIQUID_BOUNDARIES_FILENAME].splitlines()[1]
    assert header == "T Q(1)"


def test_both_runs_measured_writes_both_columns_at_their_own_numbers():
    _keywords, files = _boundaries(
        {"measured": _settled(inflow_q_series=_FLOW,
                              outflow_stage_series=_LEVEL),
         "tracers": []})
    assert files[LIQUID_BOUNDARIES_FILENAME].splitlines()[1] == "T Q(1) SL(2)"


def test_a_run_nothing_measured_a_window_at_writes_no_file():
    keywords, files = _boundaries({"measured": _settled(), "tracers": [0.0]})
    assert files == {}
    assert "LIQUID_BOUNDARIES_FILE" not in keywords


def test_the_file_opens_where_a_continued_run_s_clock_opens():
    """A record opens at its own first sample; a continued run opens at the
    instant its parent ended, and the window is read on THAT clock."""
    _keywords, files = _boundaries(
        {"measured": _settled(inflow_q_series=_FLOW, start_time_s=3600.0,
                              until_s=10800.0),
         "tracers": []})
    rows = files[LIQUID_BOUNDARIES_FILENAME].splitlines()[3:]
    assert [float(row.split()[0]) for row in rows] == [
        3600.0, 7200.0, 10800.0, 10900.0]
    assert [float(row.split()[1]) for row in rows] == [10.0, 20.0, 30.0, 30.0]
