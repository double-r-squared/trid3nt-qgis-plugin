"""``fetch_ehydro_surveys``: the survey index, and what each survey states about itself.

The surveys are picked by COVERAGE over the bbox - newest first over each part
of it nothing newer reached - and the package's own points carry the datum and
the unit the depths are in, its metadata the shift between that datum and a
national frame. Covered with that, the refusals: an unparseable window, an
extent with no survey, a stated window newer than every survey, and a survey
that states no datum or a unit nothing here can convert."""

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


#: A footprint big enough to cover any bbox a test states, where the test is
#: about the window rather than about which survey reaches which ground.
_EVERYWHERE = (-180.0, -85.0, 180.0, 85.0)


def _feature(date: str, survey_id: str = "X", location: str = "https://example/x.ZIP",
             extent: tuple = _EVERYWHERE):
    west, south, east, north = extent
    ring = [[west, south], [east, south], [east, north], [west, north], [west, south]]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"surveyjobidpk": survey_id, "surveydateend": _epoch_ms(date),
                           "sourcedatalocation": location}}


#: THE RECORDED INDEX over the run's own bbox on the St. Clair River: every one
#: of the 23 features the FeatureServer returned, as (end date, id, extent). The
#: northern 1.7 km is surveyed every year; the southern 8 km was last measured
#: in 2021 and 2020, which is why age may not filter.
PORT_HURON_AOI = (-82.475404, 42.886362, -82.404648, 42.99302)
PORT_HURON_INDEX = (
    ("2026-06-16", "PH_01_BRV_20260616_CS", (-82.4581, 42.9726, -82.4186, 43.0178)),
    ("2026-06-08", "CR_01_SCR_20260608_CS_BRS", (-82.4222, 42.9627, -82.4121, 42.9781)),
    ("2025-10-27", "CR_01_SCR_20251027_AD_BRS", (-82.4203, 42.9683, -82.4158, 42.976)),
    ("2025-08-28", "PH_01_BRV_20250828_CS", (-82.4581, 42.9726, -82.4186, 43.0178)),
    ("2025-06-17", "CR_01_SCR_20250617_CS_BRS", (-82.4221, 42.9627, -82.4125, 42.9764)),
    ("2024-10-21", "CR_01_SCR_20241021_AD_BRS", (-82.4222, 42.9624, -82.4124, 42.9765)),
    ("2024-08-14", "CR_01_SCR_20240814_BD_BRS", (-82.4222, 42.9627, -82.4122, 42.9782)),
    ("2023-10-10", "CR_01_SCR_20231010_CS_BRS", (-82.4225, 42.9625, -82.412, 42.9791)),
    ("2023-04-27", "PH_01_BRV_20230427_CS", (-82.4581, 42.9726, -82.4187, 43.0178)),
    ("2022-09-01", "CR_01_SCR_20220810_CS_BRS", (-82.4207, 42.9667, -82.4121, 42.9769)),
    ("2021-11-30", "CR_01_SCR_20211123_CS", (-82.4851, 42.8197, -82.4121, 42.9768)),
    ("2020-11-25", "PH_01_BRV_20201120_CS", (-82.4583, 42.9725, -82.4185, 43.0179)),
    ("2020-10-21", "CR_01_SCR_20201020_AD", (-82.4208, 42.967, -82.417, 42.9749)),
    ("2020-09-15", "CR_01_SCR_20200909_BD", (-82.4208, 42.9669, -82.417, 42.9748)),
    ("2020-08-20", "CR_01_SCR_20200819_CS", (-82.4258, 42.9771, -82.413, 43.0095)),
    ("2020-07-27", "CR_01_SCR_20200724_CS", (-82.4747, 42.8699, -82.4122, 42.9793)),
    ("2020-03-02", "CR_01_SCR_20191018_CS", (-82.4203, 42.9672, -82.4121, 42.9778)),
    ("2020-03-02", "CR_01_SCR_20161004_CS", (-82.4206, 42.9673, -82.4121, 42.9768)),
    ("2020-01-23", "PH_01_BRV_20160817_CS", (-82.458, 42.9726, -82.4186, 43.0178)),
    ("2020-01-23", "PH_01_BRV_20150812_CS", (-82.434, 42.9726, -82.4187, 42.9811)),
    ("2020-01-23", "PH_01_BRV_20150810_AD", (-82.4579, 42.9807, -82.4336, 42.9985)),
    ("2020-01-23", "PH_01_BRV_20150605_BD", (-82.4581, 42.9913, -82.4424, 43.0004)),
    ("2019-12-16", "PH_01_BRV_20170808_CS", (-82.4581, 42.9726, -82.4186, 43.0178)),
)


def _port_huron_index():
    return [_feature(date, survey_id, extent=extent)
            for date, survey_id, extent in PORT_HURON_INDEX]


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


def test_an_unstated_window_filters_nothing(spec):
    """A BED is static: an old pass over ground nothing newer measured is the
    best bed there is, so age ranks and never drops a survey."""
    assert eh._since(spec, {"bbox": [-122.7, 45.5, -122.6, 45.6]}) is None


def test_the_newest_survey_over_each_part_of_the_bbox_is_the_one_taken(spec):
    picked = eh._selected(
        spec, [_feature("2024-01-01", "old"), _feature("2026-01-01", "mid"),
               _feature("2026-09-09", "new")], None, [-122.7, 45.5, -122.6, 45.6])
    # The newest covers the whole bbox, so the two under it buy no ground.
    assert [f["properties"]["surveyjobidpk"] for f in picked] == ["new"]


def test_the_southern_reachs_old_surveys_enter_where_nothing_newer_measured_it(spec):
    """On the recorded St. Clair River index the recent passes cover 1.7 km of a
    12 km reach; the southern 8 km is measured only by 2021 and 2020."""
    picked = [f["properties"]["surveyjobidpk"]
              for f in eh._selected(spec, _port_huron_index(), None, PORT_HURON_AOI)]
    # 2021 is the newest pass over the southern 8 km, and 2020 over the north
    # bank the recent St. Clair River passes stop short of.
    assert "CR_01_SCR_20211123_CS" in picked
    assert "CR_01_SCR_20200819_CS" in picked
    assert picked[0] == "PH_01_BRV_20260616_CS"
    assert len(picked) <= eh._MAX_SURVEYS
    # A survey lying wholly inside a newer one's footprint buys no bed.
    assert "CR_01_SCR_20251027_AD_BRS" not in picked
    assert "PH_01_BRV_20250828_CS" not in picked


def test_the_selected_surveys_cover_the_southern_reach_the_window_left_bare(spec):
    """What the coverage rule buys: the 8 km the year-long window never reached."""
    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    painted = unary_union([shape(f["geometry"]) for f in eh._selected(
        spec, _port_huron_index(), None, PORT_HURON_AOI)])
    # The navigation channel only: no eHydro survey on this river reaches east
    # of -82.4121, which is the Ontario bank.
    west, south, _east, _north = PORT_HURON_AOI
    southern = box(west, south, -82.4121, 42.94)
    assert southern.difference(painted).area < 0.01 * southern.area


def test_a_stated_window_is_the_callers_own_filter(spec):
    """The year-long window the fetch used to apply on its own is what kept the
    southern reach unmeasured; stated deliberately, it still does."""
    picked = [f["properties"]["surveyjobidpk"]
              for f in eh._selected(spec, _port_huron_index(),
                                    _dt.date(2025, 9, 20), PORT_HURON_AOI)]
    assert picked == ["PH_01_BRV_20260616_CS", "CR_01_SCR_20260608_CS_BRS"]


def test_a_survey_reaching_none_of_the_bbox_refuses_rather_than_downloading(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._selected(spec, [_feature("2026-01-01", extent=(-70.0, 40.0, -69.9, 40.1))],
                     None, [-122.7, 45.5, -122.6, 45.6])
    assert excinfo.value.error_code == "EHYDRO_NO_SURVEY"


def test_an_extent_with_no_survey_refuses_by_name(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._selected(spec, [], None, [-122.7, 45.5, -122.6, 45.6])
    assert excinfo.value.error_code == "EHYDRO_NO_SURVEY"


def test_a_window_newer_than_every_survey_names_the_newest_there_is(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        eh._selected(spec, [_feature("2024-01-01")], _dt.date(2026, 1, 1),
                     [-122.7, 45.5, -122.6, 45.6])
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


def test_a_depth_on_igld85_is_counted_from_the_low_water_datum(spec):
    """A DEPTH is counted from a water surface and never from a reference system:
    a Great Lakes survey writes IGLD85 on every point and its plot sheet says the
    soundings are referenced to IGLD85 L.W.D., so the zero is the low water datum
    expressed on that system - the surface an offset service serves by its own
    name, 176 m above IGLD85's zero on Lake Huron."""
    assert spec.normalize.quantity == "depth_below_datum"
    assert eh._stated(spec, _points(datum="IGLD85"), "CR_01")[0] == "LWD_IGLD85"
    # A coastal survey already names the surface its soundings hang below.
    assert eh._stated(spec, _points(datum="MLLW"), "NY_02")[0] == "MLLW"
    # The same word over ELEVATIONS names the system they stand on, not a
    # surface hanging under them, and nothing re-reads it.
    assert eh._counted_from("IGLD85", "bed_elevation_m") == "IGLD85"


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
