"""Unit tests for the INSTANT typed input.

Covered: a bare date read as midnight UTC, a trailing-Z timestamp, an offset
timestamp moved onto UTC, nothing asked for reading as latest, an unparseable
value refusing by name rather than falling back, the calendar day, and the
wire coercion the runtime calls."""

from __future__ import annotations

import datetime as dt

import pytest

from trid3nt_server.inputs.instant import day, event_time, instant
from trid3nt_server.workflows.runtime import WireArgsError


def test_a_bare_date_is_midnight_utc() -> None:
    assert instant("2026-08-20") == "2026-08-20T00:00:00+00:00"


def test_a_trailing_z_timestamp() -> None:
    assert instant("2026-08-20T06:00:00Z") == "2026-08-20T06:00:00+00:00"


def test_an_offset_timestamp_moves_onto_utc() -> None:
    assert instant("2026-08-20T06:00:00-07:00") == "2026-08-20T13:00:00+00:00"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_nothing_asked_for_reads_as_latest(value: object) -> None:
    assert instant(value) is None


def test_an_unparseable_value_refuses_by_name() -> None:
    with pytest.raises(WireArgsError) as caught:
        instant("last tuesday")
    assert caught.value.error_code == "EVENT_TIME_INVALID"
    assert "last tuesday" in str(caught.value)


def test_the_code_and_label_are_the_callers() -> None:
    with pytest.raises(WireArgsError) as caught:
        instant("nope", label="sample_date", code="SAMPLE_DATE_INVALID")
    assert caught.value.error_code == "SAMPLE_DATE_INVALID"
    assert "sample_date" in str(caught.value)


def test_the_day_of_a_stated_instant() -> None:
    assert day("2026-08-20T23:30:00Z") == "2026-08-20"


def test_an_unstated_day_is_today_in_utc() -> None:
    assert day(None) == dt.datetime.now(dt.timezone.utc).date().isoformat()


def test_an_unparseable_day_refuses_rather_than_reading_today() -> None:
    with pytest.raises(WireArgsError):
        day("whenever")


def test_the_wire_coercion_pins_the_cycle() -> None:
    assert event_time()({"event_time": "2026-01-02"}) == {
        "event_time": "2026-01-02T00:00:00+00:00"}
    assert event_time()({}) == {"event_time": None}
