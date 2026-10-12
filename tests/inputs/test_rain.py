"""The RAIN typed input: a rainfall record -> the depth per interval on the run's clock.

Covered: the cadence is the record's own stamps and never an assumed hour, the
depths arrive in millimetres whatever unit the source's row states, the series
column is the one the matched row names, and the refusals - a value that is no
record, a column in no stated unit, a record of one interval.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.observation import ObservationError
from trid3nt_server.inputs.rain import Rain, rain
from trid3nt_server.inputs.slots import RAIN, ingest_slot

_QUARTERS = ["2020-06-01T00:00", "2020-06-01T00:15", "2020-06-01T00:30"]


def test_the_interval_is_the_record_s_own_cadence():
    found = rain({"times": _QUARTERS, "precip_mm": [1.0, 4.0, 2.0],
                  "units": "mm"})
    assert found.interval_s == 900.0
    assert found.series.times_s == (0.0, 900.0, 1800.0)
    assert found.peak_mm_per_s == pytest.approx(4.0 / 900.0)


def test_every_series_reads_zero_at_the_moment_the_run_opens_at():
    found = rain({"times": _QUARTERS, "precip_mm": [1.0, 4.0, 2.0],
                  "units": "mm"}, at="2020-06-01T00:15:00Z")
    assert found.series.times_s == (-900.0, 0.0, 900.0)


def test_the_column_and_its_unit_are_the_matched_row_s_own_statement():
    found = ingest_slot(RAIN, {"times": _QUARTERS, "depth": [0.1, 0.0, 1.0]},
                        label="rain", series_field="depth",
                        column_units={"depth": "in"}, window_s=1800.0)
    assert found.series.values == pytest.approx((2.54, 0.0, 25.4))
    assert found.series.units == "mm"


def test_depths_stated_at_a_cadence_open_their_first_interval_at_zero():
    assert Rain.every(3600.0, [1.5, 4.0]).series.times_s == (0.0, 3600.0)


@pytest.mark.parametrize("source, said", [
    ("s3://cache/qpe.tif", "no rainfall record"),
    ({"precip_mm": [1.0, 2.0]}, "no rainfall record"),
    ({"times": _QUARTERS[:2], "precip_mm": [1.0, 2.0]}, "no stated unit"),
    ({"times": _QUARTERS[:1], "precip_mm": [1.0], "units": "mm"},
     "no hyetograph"),
    ({"times": _QUARTERS, "precip_mm": [1.0, 2.0], "units": "mm"},
     "no hyetograph"),
])
def test_what_is_not_a_hyetograph_refuses(source, said):
    with pytest.raises(ObservationError, match=said):
        rain(source)


def test_the_slot_reads_the_class_the_rainfall_analyses_publish():
    assert RAIN.classes == frozenset({"precipitation series"})
    assert RAIN.ingest is rain
