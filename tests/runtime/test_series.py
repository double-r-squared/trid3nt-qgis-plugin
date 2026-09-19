"""Unit tests for the runtime SERIES - a stated value that is not one number.

Covered: the sentence a card row and a record both print, stamps read onto the
run's own clock, the unit a slot reads, the datum shift every reading rides,
the engine's own reading between two rows, the resample to a step coarser than
the record and the record left alone under a finer one, and the shapes that
refuse.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime import Series
from trid3nt_server.workflows.runtime.temporal import (
    TemporalShapeError,
    TemporalUnitsError,
)


def test_a_series_prints_as_its_shape_rather_than_its_points():
    assert str(Series([0.0, 60.0, 120.0], [1.0, 2.0, 3.0], units="m3/s")) == \
        "series, 3 points over the window"


def test_stamped_readings_open_the_clock_at_the_first_one_measured():
    found = Series.from_samples(
        [("2026-01-01T01:00:00Z", 5.0), ("2026-01-01T00:00:00Z", 4.0)],
        units="m", start_s=100.0)
    assert found.times_s == (100.0, 3700.0)
    assert found.values == (4.0, 5.0)


def test_a_series_reads_in_the_unit_the_slot_takes():
    found = Series([0.0, 3600.0], [100.0, 200.0], units="ft3/s").in_units("m3/s")
    assert found.units == "m3/s"
    assert found.values[0] == pytest.approx(2.8316846592)


def test_a_cross_dimension_unit_refuses_rather_than_passing_a_number_through():
    with pytest.raises(TemporalUnitsError):
        Series([0.0, 60.0], [1.0, 2.0], units="m3/s").in_units("m")


def test_every_reading_rides_the_one_shift_the_value_rode():
    found = Series([0.0, 60.0], [1.0, 2.0], units="m").shifted(1.61)
    assert found.values == pytest.approx((2.61, 3.61))


def test_a_value_between_two_rows_is_read_the_way_the_engine_reads_its_table():
    found = Series([0.0, 100.0], [10.0, 20.0], units="m")
    assert found.at(50.0) == pytest.approx(15.0)
    assert found.at(-10.0) == 10.0
    assert found.at(500.0) == 20.0


def test_a_record_finer_than_the_step_resamples_onto_it():
    found = Series([0.0, 300.0, 600.0, 900.0], [1.0, 2.0, 3.0, 4.0],
                   units="m3/s").at_step(600.0)
    assert found.times_s == (0.0, 600.0, 900.0)


def test_a_record_coarser_than_the_step_is_everything_the_engine_reads():
    record = Series([0.0, 3600.0, 7200.0], [1.0, 2.0, 3.0], units="m3/s")
    assert record.at_step(60.0) is record


def test_a_series_that_does_not_move_forward_refuses():
    with pytest.raises(TemporalShapeError):
        Series([0.0, 0.0], [1.0, 2.0], units="m")
    with pytest.raises(TemporalShapeError):
        Series([0.0], [1.0], units="m")
    with pytest.raises(TemporalShapeError):
        Series([0.0, 1.0, 2.0], [1.0, 2.0], units="m")


def test_a_series_is_frozen_once_the_record_said_what_it_measured():
    with pytest.raises(AttributeError):
        Series([0.0, 60.0], [1.0, 2.0], units="m").units = "ft"


def test_a_record_is_read_on_the_clock_the_run_opens_on():
    found = Series([0.0, 60.0], [1.0, 2.0], units="m").opening_at(3600.0)
    assert found.times_s == (3600.0, 3660.0)
    assert found.values == (1.0, 2.0)


def test_the_readings_before_the_run_s_moment_stay_ahead_of_it():
    record = Series.from_samples(
        [("2026-09-17T07:00:00Z", 24200.0), ("2026-09-17T18:00:00Z", 9210.0),
         ("2026-09-17T18:05:00Z", 10100.0)],
        units="ft3/s", at="2026-09-17T18:00:00Z")
    assert record.opening_at(0.0).times_s == (-39600.0, 0.0, 300.0)
    assert record.opening_at(0.0).at(0.0) == 9210.0
    assert record.opening_at(3600.0).times_s == (-36000.0, 3600.0, 3900.0)
