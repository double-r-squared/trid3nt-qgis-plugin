"""Unit tests for THE ALIGNMENT: one record onto the clock a reader reads it on.

Covered: the quantity class read off a source's own data class, the three moves
each class may make, a record left as measured under a clock no coarser than its
own, the unit conversion, the opening shift, the stamp of what was done, and the
hole that refuses rather than being drawn over.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.series import align, quantity_class
from trid3nt_server.workflows.runtime.temporal import (
    CATEGORICAL,
    RATE,
    STATE,
    Series,
    TemporalGapError,
)


def _hourly() -> Series:
    return Series([0.0, 3600.0, 7200.0, 10800.0], [1.0, 2.0, 3.0, 4.0],
                  units="mm/h")


def test_the_quantity_class_is_read_off_the_source_s_own_data_class():
    assert quantity_class("precipitation series") == RATE
    assert quantity_class("discharge series") == RATE
    assert quantity_class("land cover") == CATEGORICAL
    assert quantity_class("water level series") == STATE
    assert quantity_class(None) == STATE


def test_a_rate_keeps_its_total_across_a_coarser_clock():
    found = align(_hourly(), onto=7200.0, quantity=RATE)
    assert found.series.times_s == (0.0, 7200.0, 10800.0)
    # The two hours the first target interval spans averaged 1.5 and 2.5 mm/h.
    assert found.series.values[0] == pytest.approx(2.0)
    assert "conservative" in found.note


def test_a_state_interpolates_onto_the_clock_it_is_read_at():
    found = align(_hourly(), onto=7200.0, quantity=STATE)
    assert found.series.values == (1.0, 3.0, 4.0)
    assert "linear" in found.note


def test_a_class_label_moves_to_its_nearest_neighbour_and_nowhere_else():
    found = align(Series([0.0, 3600.0, 7200.0], [1.0, 2.0, 3.0], units="m"),
                  onto=5400.0, quantity=CATEGORICAL)
    assert set(found.series.values) <= {1.0, 2.0, 3.0}
    assert "nearest" in found.note


def test_a_clock_no_coarser_than_the_record_leaves_it_as_measured():
    record = _hourly()
    found = align(record, onto=60.0)
    assert found.series is record
    assert "kept" in found.note


def test_the_unit_the_reader_reads_is_converted_and_stamped():
    found = align(_hourly(), units="mm/day")
    assert found.series.units == "mm/day"
    assert found.series.values[0] == pytest.approx(24.0)
    assert found.note == "converted mm/h->mm/day"


def test_the_record_opens_where_the_reader_s_zero_sits():
    found = align(_hourly(), opening_at=1800.0)
    assert found.series.times_s[0] == 1800.0
    assert "opened at" in found.note


def test_a_hole_wider_than_the_bound_refuses_rather_than_being_bridged():
    holed = Series([0.0, 3600.0, 90000.0], [1.0, 2.0, 3.0], units="m")
    with pytest.raises(TemporalGapError, match="hole ending at"):
        align(holed)
    # A bound that admits the hole on purpose takes the record as it is.
    assert align(holed, max_gap_s=100000.0).series is holed
