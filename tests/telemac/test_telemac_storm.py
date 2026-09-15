"""The STORM composite: one value standing for the rain over a domain.

Whether the run is driven by a measured record or by a constant design rate is
the ASK's, stated where the value is; what the two become in a steering file -
the block file and the routine that reads it, or the engine's own constant
branch - is the wrapper's, once, for every template that asks the question.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.modules import telemac2d as T


def _storm(**over):
    ask = {"mm_per_hr": 12.0, "hours": 2.0, "until_s": 36000.0,
           "tracers": 0, "fortran": "/opt/trid3nt/user_fortran/raindef3"}
    ask.update(over)
    return T._storm(T.Storm(**ask))


def test_a_design_rate_is_the_engines_own_constant_branch():
    keywords, files = _storm()
    assert keywords["RAIN_OR_EVAPORATION"] is True
    assert keywords["RAIN_OR_EVAPORATION_IN_MM_PER_DAY"] == pytest.approx(288.0)
    assert keywords["DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS"] == 2.0
    assert files == {}


def test_a_storm_that_outlasts_the_run_states_no_end_it_never_reaches():
    """The window exists so the recession limb appears; a storm that never stops
    inside the horizon has no window to state."""
    keywords, _files = _storm(hours=20.0, until_s=36000.0)
    assert "DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS" not in keywords


def test_a_run_with_no_tracer_states_no_rainwater_concentrations():
    """An EMPTY list is a keyword with nothing after it, which DAMOCLES reads as
    the next line's business."""
    assert "VALUES_OF_TRACERS_IN_THE_RAIN" not in _storm(tracers=0)[0]
    assert _storm(tracers=2)[0]["VALUES_OF_TRACERS_IN_THE_RAIN"] == [0.0, 0.0]


def test_a_measured_record_becomes_the_blocks_the_engine_reads_per_timestep():
    """The block assembly is the wrapper's: hourly gross millimetres in, the
    file the routine reads out."""
    keywords, files = _storm(series=[1.5, 4.0, 0.25], until_s=18000.0)
    assert keywords["FORMATTED_DATA_FILE_1"] == T.HYETOGRAPH_FILENAME
    assert keywords["FORTRAN_FILE"].endswith("raindef3")
    rows = [line.split() for line in files[T.HYETOGRAPH_FILENAME].splitlines()
            if line and not line.startswith("#") and " " in line]
    assert rows[0] == ["3600.000", "1.50000"]
    assert rows[1] == ["7200.000", "4.00000"]
    assert rows[2] == ["10800.000", "0.25000"]
    # The tail past the last simulated instant is DRY, so a storm that stops
    # inside the run stops in the file too.
    assert rows[-1] == ["21600.000", "0.00000"]


def test_a_measured_record_leaves_the_constant_branch_alone():
    keywords, _files = _storm(series=[1.0, 2.0], until_s=7200.0)
    assert "RAIN_OR_EVAPORATION_IN_MM_PER_DAY" not in keywords


def test_a_record_of_one_interval_is_not_a_shape_and_refuses():
    with pytest.raises(ValueError, match="at least two"):
        _storm(series=[3.0], until_s=7200.0)


def test_a_measured_storm_with_no_routine_to_read_it_refuses_by_name():
    with pytest.raises(ValueError, match="user Fortran"):
        _storm(series=[1.0, 2.0], until_s=7200.0, fortran=None)


def test_a_run_that_states_no_rain_at_all_writes_no_keyword():
    assert _storm(mm_per_hr=None, hours=None) == ({}, {})


def test_the_storm_is_a_composite_the_wrapper_expands():
    assert "storm" in T.T2D.COMPOSITES
