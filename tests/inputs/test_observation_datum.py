"""Unit tests for a reading reaching a slot that counts from another zero.

Covered: a reading already on the slot's datum passing through; a reading on
another datum moved by the offset row the template names and the shift said on
the note; the same reading with NO offset refusing by name; a row stating its own
datum over the layer's; a slot that names no datum checking nothing; and a value
the caller STATED standing on the slot's own datum."""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.observation import ObservationError, note, observation

_NAVD88 = "NAVD88 (metres, positive up)"
_NGVD29_TO_NAVD88 = {"offset_m": 1.057, "from_frame": "NGVD29",
                     "to_frame": "NAVD88", "source": "NOAA VDatum",
                     "uncertainty_m": 0.053}


def _gauge(value: float, layer_datum: str | None = None, **props: object) -> dict:
    """One gauge as it reaches a slot: the feature collection, and the datum the
    LAYER states where its rows state none."""
    return {"type": "FeatureCollection", "vertical_datum": layer_datum,
            "features": [{
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [-122.669, 45.518]},
                "properties": {"site_id": "14211720", "value": value,
                               "unit": "m", "result_date": "2026-09-15",
                               **props}}]}


def test_a_reading_already_on_the_slot_s_datum_is_not_shifted() -> None:
    found = observation(_gauge(1.719, vertical_datum=_NAVD88), to_datum=_NAVD88)
    assert found.value == pytest.approx(1.719)
    assert found.datum == _NAVD88
    assert found.datum_note == ""


def test_a_reading_on_another_datum_moves_through_the_offset_row() -> None:
    found = observation(_gauge(1.719, vertical_datum="NGVD29"), to_datum=_NAVD88,
                        offset=_NGVD29_TO_NAVD88)
    assert found.value == pytest.approx(2.776)
    assert found.datum == _NAVD88
    assert "NOAA VDatum" in found.datum_note and "+1.057 m" in found.datum_note
    assert found.datum_note in note(found, opens="the outflow holds at")


def test_the_same_reading_with_no_offset_refuses_by_name() -> None:
    with pytest.raises(ObservationError) as caught:
        observation(_gauge(1.719, vertical_datum="NGVD29"), to_datum=_NAVD88)
    assert caught.value.error_code == "OBSERVATION_DATUMS_DIFFER"
    assert "NGVD29" in str(caught.value)


def test_an_offset_between_two_other_frames_refuses() -> None:
    with pytest.raises(ObservationError) as caught:
        observation(_gauge(1.719, vertical_datum="NGVD29"), to_datum=_NAVD88,
                    offset={"offset_m": -1.131, "from_frame": "NAVD88",
                            "to_frame": "EGM2008", "source": "NOAA VDatum"})
    assert caught.value.error_code == "OBSERVATION_DATUM_OFFSET_MISMATCH"


def test_the_row_s_own_datum_stands_over_the_layer_s() -> None:
    found = observation(_gauge(1.719, layer_datum=_NAVD88, vertical_datum="NGVD29"),
                        to_datum=_NAVD88, offset=_NGVD29_TO_NAVD88)
    assert found.value == pytest.approx(2.776)


def test_the_layer_s_datum_is_read_where_the_rows_state_none() -> None:
    found = observation(_gauge(1.719, layer_datum="NGVD29"), to_datum=_NAVD88,
                        offset=_NGVD29_TO_NAVD88)
    assert found.value == pytest.approx(2.776)


def test_a_reading_with_no_datum_anywhere_refuses_when_a_slot_asks_for_one() -> None:
    with pytest.raises(ObservationError) as caught:
        observation(_gauge(1.719), to_datum=_NAVD88)
    assert caught.value.error_code == "OBSERVATION_DATUM_UNSTATED"


def test_a_slot_that_names_no_datum_checks_nothing() -> None:
    found = observation(_gauge(9.5, vertical_datum="NGVD29"))
    assert found.value == pytest.approx(9.5)
    assert found.datum is None


def test_a_stated_value_is_already_on_the_slot_s_datum() -> None:
    found = observation(2.776, to_datum=_NAVD88, to_units="m")
    assert found.value == pytest.approx(2.776)
    assert found.datum == _NAVD88
