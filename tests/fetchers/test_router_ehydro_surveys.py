"""``fetch_ehydro_surveys``: the survey index, and what each survey states about itself.

The index picks the window - the last year where none is stated - and the package's
own points carry the datum and the unit the depths are in, its metadata the shift
between that datum and a national frame. Covered with that, the refusals: an
unparseable window, a window wider than the download cap, an extent with no survey,
and a survey that states no datum or a unit nothing here can convert."""

from __future__ import annotations

import datetime as _dt

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_ehydro_surveys import hooks as eh


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_ehydro_surveys"]


def _epoch_ms(iso: str) -> float:
    return _dt.datetime.fromisoformat(iso).replace(
        tzinfo=_dt.timezone.utc).timestamp() * 1000.0


def _feature(date: str, survey_id: str = "X", location: str = "https://example/x.ZIP"):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []},
            "properties": {"surveyjobidpk": survey_id, "surveydateend": _epoch_ms(date),
                           "sourcedatalocation": location}}


def _points(datum="CRD", uom="usSurveyFoot", depth=10.0):
    import geopandas as gpd
    from shapely.geometry import Point

    return gpd.GeoDataFrame(
        {eh._DATUM_FIELD: [datum, datum], eh._UOM_FIELD: [uom, uom],
         eh._DEPTH_FIELD: [depth, depth + 1.0]},
        geometry=[Point(-122.67, 45.52), Point(-122.68, 45.53)], crs="EPSG:4326")


def test_the_source_states_no_datum_of_its_own_because_each_survey_states_one(spec):
    # A consumer asking this source for ONE datum must be refused by name rather
    # than handed the first survey's - the rows carry the truth.
    assert spec.vertical_datum is None
    assert "vertical_datum" in (spec.ingest or {})["properties"]


def test_an_unparseable_window_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        eh.validate(spec, {"bbox": [-122.7, 45.5, -122.6, 45.6], "since": "last week"})
    assert excinfo.value.error_code == "EHYDRO_INPUT_INVALID"


def test_an_unstated_window_is_the_last_year(spec):
    """The window lives here and not in the templates that ask for a bed."""
    asked = eh._since(spec, {"bbox": [-122.7, 45.5, -122.6, 45.6]})
    assert _dt.date.today() - asked == _dt.timedelta(days=eh._WINDOW_DAYS)


def test_a_window_returns_every_survey_that_ends_in_it_newest_first(spec):
    picked = eh._selected(spec, [_feature("2024-01-01", "old"),
                                 _feature("2026-01-01", "mid"),
                                 _feature("2026-09-09", "new")],
                          _dt.date(2025, 1, 1))
    assert [f["properties"]["surveyjobidpk"] for f in picked] == ["new", "mid"]


def test_a_window_past_the_download_cap_refuses_rather_than_truncating(spec):
    many = [_feature("2026-01-%02d" % n, f"s{n}")
            for n in range(1, eh._MAX_SURVEYS + 2)]
    with pytest.raises(RouterInputError) as excinfo:
        eh._selected(spec, many, _dt.date(2025, 1, 1))
    message = str(excinfo.value)
    assert str(eh._MAX_SURVEYS) in message
    assert f"{eh._MAX_SURVEYS + 1} surveys" in message
    # The ceiling is the ASK's, not an absence: the window is what moves.
    assert excinfo.value.error_code == "EHYDRO_INPUT_INVALID"
    assert "Move since forward" in message


def test_a_full_index_page_states_the_count_as_the_floor_it_is(spec):
    """The index answers one page, so a window that fills it holds AT LEAST that."""
    page = [_feature("2026-01-%02d" % (n % 28 + 1), f"s{n}")
            for n in range(eh._INDEX_RECORDS)]
    with pytest.raises(RouterInputError) as excinfo:
        eh._selected(spec, page, _dt.date(2025, 1, 1))
    assert f"at least {eh._INDEX_RECORDS} surveys" in str(excinfo.value)


def test_an_extent_with_no_survey_refuses_by_name(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._selected(spec, [], _dt.date(2025, 1, 1))
    assert excinfo.value.error_code == "EHYDRO_NO_SURVEY"


def test_a_window_newer_than_every_survey_names_the_newest_there_is(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._selected(spec, [_feature("2024-01-01")], _dt.date(2026, 1, 1))
    assert "2024-01-01" in str(excinfo.value)


def test_the_stated_unit_is_what_the_depths_are_converted_from(spec):
    datum, uom, scale = eh._stated(spec, _points(), "X")
    assert (datum, uom) == ("CRD", "usSurveyFoot")
    assert scale == pytest.approx(0.3048006, abs=1e-6)
    assert eh._stated(spec, _points(uom="meter"), "X")[2] == 1.0


def test_a_survey_that_states_no_datum_refuses_by_name(spec):
    """The SURVEY is what cannot be read, not the ask, so it holds nothing here:
    nothing chooses a zero, and a slot matched to this source takes the next one."""
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._stated(spec, _points(datum=None), "WR_03")
    assert "WR_03" in str(excinfo.value)


def test_a_unit_nothing_here_converts_refuses_by_name(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._stated(spec, _points(uom="fathom"), "WR_03")
    assert "fathom" in str(excinfo.value)


def test_two_datums_over_one_survey_are_refused_rather_than_chosen_between(spec):
    points = _points()
    points.loc[1, eh._DATUM_FIELD] = "MLLW"
    with pytest.raises(RouterEmptyError):
        eh._stated(spec, points, "WR_03")


def test_the_survey_is_found_through_its_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("bathymetry", "fetch_ehydro_surveys")


def test_the_published_offset_is_read_off_the_package_metadata(spec):
    """The district writes the shift as a sentence and nowhere machine-readable."""
    archive = _Archive({"WR_03.XML": (
        "Soundings are shown in feet and indicate depths below Columbia River "
        "Datum. CRD is 5.28 feet above the North American Vertical Datum of 1988 "
        "(NAVD 88 Geoid 09) at Willamette River Mile 9.7.")})
    assert eh._published_offset(archive, "WR_03") == (1.6093, "NAVD88")


def test_a_package_that_publishes_no_offset_states_none(spec):
    archive = _Archive({"X.XML": "Soundings are in feet below MLLW.",
                        "X.txt": "CRD is 5.28 feet above NAVD 88"})
    assert eh._published_offset(archive, "X") == (None, "")


def test_a_datum_stated_below_the_frame_reads_the_other_way(spec):
    archive = _Archive({"X.XML": "LWRP is 1.5 meters below NAVD 88 at the gauge."})
    assert eh._published_offset(archive, "X") == (-1.5, "NAVD88")


class _Archive:
    """A ZIP stand-in: the member names and the bytes behind each."""

    def __init__(self, members: dict[str, str]) -> None:
        self._members = members

    def namelist(self) -> list[str]:
        return list(self._members)

    def read(self, name: str) -> bytes:
        return self._members[name].encode("utf-8")
