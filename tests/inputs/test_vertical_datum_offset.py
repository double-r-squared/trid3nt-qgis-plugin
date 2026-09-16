"""Unit tests for the OFFSET that brings two differing vertical datums together.

Covered: a fetched offset record, an ``Offset`` and a bare number all reading as
one; the frames checked against the two datums and reversed where the offset was
measured the other way; an offset between two OTHER frames refusing; two
differing datums with no offset still refusing; a frame named inside a source's
own prose; and what the run says it did."""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.vertical_datum import (
    DatumError,
    Offset,
    align,
    names_frame,
    offset_row,
)

_CRD = {"vertical_datum": "SD (Columbia River Datum: CRD)", "name": "the survey"}
_NAVD88 = {"vertical_datum": "NAVD88 (metres, positive up)", "name": "3DEP"}
_EGM2008 = {"vertical_datum": "EGM2008 geoid (metres, positive up)", "name": "GLO-30"}

_FETCHED = {"offset_m": -1.131, "from_frame": "NAVD88", "to_frame": "EGM2008",
            "source": "NOAA VDatum", "uncertainty_m": 0.165}


def test_a_fetched_record_reads_as_an_offset() -> None:
    read = offset_row(_FETCHED)
    assert (read.metres, read.from_frame, read.to_frame) == (-1.131, "NAVD88", "EGM2008")
    assert read.uncertainty_m == 0.165
    assert read.names_frames


def test_a_bare_number_is_metres_with_no_frames_on_it() -> None:
    read = offset_row(1.6093)
    assert read.metres == 1.6093
    assert not read.names_frames
    assert "stated on the call" in read.note


def test_nothing_is_no_offset() -> None:
    assert offset_row(None) is None


@pytest.mark.parametrize("given", [True, "a bit", {"metres": 1.0}])
def test_what_is_not_an_offset_refuses_by_name(given: object) -> None:
    with pytest.raises(DatumError) as caught:
        offset_row(given)
    assert caught.value.error_code == "DATUM_OFFSET_INVALID"


def test_one_datum_needs_no_offset() -> None:
    aligned = align(_NAVD88, dict(_NAVD88, name="another 3DEP tile"))
    assert (aligned.shift_m, aligned.note) == (0.0, "")
    assert aligned.datum == "NAVD88 (metres, positive up)"


def test_the_offset_moves_the_source_onto_the_target() -> None:
    aligned = align(_NAVD88, _EGM2008, offset=_FETCHED)
    assert aligned.shift_m == -1.131
    assert aligned.datum == "EGM2008 geoid (metres, positive up)"
    assert "NOAA VDatum" in aligned.note and "-1.131 m" in aligned.note


def test_an_offset_measured_the_other_way_is_read_the_other_way() -> None:
    aligned = align(_EGM2008, _NAVD88, offset=_FETCHED)
    assert aligned.shift_m == 1.131
    assert aligned.datum == "NAVD88 (metres, positive up)"


def test_a_project_datum_reaches_through_the_shift_its_survey_publishes() -> None:
    survey = Offset(metres=1.6093, from_frame="CRD", to_frame="NAVD88",
                    source="the survey's own metadata")
    aligned = align(_CRD, _NAVD88, offset=survey)
    assert aligned.shift_m == 1.6093
    assert "between CRD and NAVD88" in aligned.note


def test_an_offset_between_two_other_frames_refuses() -> None:
    with pytest.raises(DatumError) as caught:
        align(_CRD, _NAVD88, offset=_FETCHED, code_prefix="MERGE_RASTERS_")
    assert caught.value.error_code == "MERGE_RASTERS_DATUM_OFFSET_MISMATCH"
    assert "NAVD88 and EGM2008" in str(caught.value)


def test_two_datums_and_no_offset_still_refuses() -> None:
    with pytest.raises(DatumError) as caught:
        align(_CRD, _NAVD88, code_prefix="MERGE_RASTERS_")
    assert caught.value.error_code == "MERGE_RASTERS_DATUMS_DIFFER"


def test_a_bare_shift_is_applied_and_said_out_loud() -> None:
    aligned = align(_CRD, _NAVD88, offset=1.6093)
    assert aligned.shift_m == 1.6093
    assert "stated on the call" in aligned.note


def test_an_unstated_datum_refuses_before_any_offset_is_read() -> None:
    with pytest.raises(DatumError) as caught:
        align({"vertical_datum": "", "name": "a user's raster"}, _NAVD88,
              offset=_FETCHED)
    assert caught.value.error_code == "DATUM_UNSTATED"


@pytest.mark.parametrize("datum,frame,found", [
    ("NAVD88 (metres, positive up)", "NAVD88", True),
    ("EGM2008 geoid (metres, positive up)", "EGM2008", True),
    ("SD (Columbia River Datum: CRD)", "CRD", True),
    ("NAVD88 (metres, positive up)", "NGVD29", False),
    ("MLLW", "MLW", False),
    ("MHHW", "MHW", False),
    ("", "NAVD88", False),
    ("NAVD88", "", False),
])
def test_a_source_names_the_frame_in_its_own_words(datum: str, frame: str,
                                                   found: bool) -> None:
    assert names_frame(datum, frame) is found
