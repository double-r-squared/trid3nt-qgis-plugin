"""The shared CIRA/RAMMB SLIDER substrate: URLs, the time index, and REGISTRATION.

Pure-helper coverage, no network: the URL builders (date YYYY/MM/DD slashes,
tileY_tileX order, zoom %02d), the timestamp round-trip, the time-index reader against a
mocked latest_times.json, and the registration - that a geostationary sector is built
from the parameters the index publishes, that a known lon/lat lands where the closed-form
scan-angle geometry says it lands, that a measured sub-window sits on its parent's grid,
and that an unregistered sector is None rather than approximated. ASCII only."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from trid3nt_server.tools.fetchers.imagery import _satellite_slider as ss

#: The registration the index publishes for GOES-West, verbatim.
_GOES18_QUERY = {
    "lon0": -137.0, "sat_alt": 42171.7, "max_rad_x": 0.151337, "max_rad_y": 0.150988,
    "disk_radius_x_z0": 338, "disk_radius_y_z0": 337,
}

_INDEX = {
    "goes-18": {
        "sectors": {
            "full_disk": {"lat_lon_query": _GOES18_QUERY, "tile_size": 678,
                          "max_zoom_level": 5},
            "conus": {"tile_size": 625, "max_zoom_level": 4},
            "mesoscale_01": {"tile_size": 500, "max_zoom_level": 2},
        },
    },
    "jpss": {"sectors": {"northern_hemisphere": {"tile_size": 1000,
                                                 "max_zoom_level": 5}}},
}


def _registration(sector: str):
    return ss.sector_registration("goes-18", sector, _INDEX["goes-18"])


def _scan_angles(registration, lon, lat):
    """The point's position on the square, in the projection's own units."""
    from pyproj import CRS, Transformer

    transformer = Transformer.from_crs(
        CRS.from_epsg(4326), CRS.from_user_input(registration["crs"]), always_xy=True)
    return transformer.transform(lon, lat)


def test_ts_round_trip():
    ts = 20260622192600
    dt = ss.ts_int_to_datetime(ts)
    assert dt == datetime(2026, 6, 22, 19, 26, 0, tzinfo=timezone.utc)
    assert ss.ts_int_to_iso(ts) == "2026-06-22T19:26:00Z"


def test_build_tile_url_date_slashes_and_order():
    url = ss.build_tile_url("goes-18", "conus", "geocolor", 20260622192600, 2, 1, 3)
    assert "/data/imagery/2026/06/22/goes-18---conus/geocolor/20260622192600/" in url
    # zoom is 2-digit zero-padded; tile index is tileY_tileX, 3-digit padded.
    assert url.endswith("/02/001_003.png")


def test_build_times_url_no_dashdash_join():
    url = ss.build_times_url("jpss", "conus", "cira_natural_fire_color")
    assert url.endswith("/data/json/jpss/conus/cira_natural_fire_color/latest_times.json")
    assert "---" not in url


def test_fetch_slider_timestamps_parses_and_sorts():
    payload = {"timestamps_int": [20260622231121, 20260622230621, 20260622230121]}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    out = ss.fetch_slider_timestamps("goes-18", "conus", "geocolor", session=_Sess())
    # Returned ASCENDING.
    assert out == [20260622230121, 20260622230621, 20260622231121]


def test_fetch_slider_timestamps_missing_key_raises():
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"nope": []}

    class _Sess:
        def get(self, *a, **k):
            return _Resp()

    with pytest.raises(ss.SliderUpstreamError):
        ss.fetch_slider_timestamps("goes-18", "conus", "geocolor", session=_Sess())


def test_a_disk_is_registered_from_the_published_parameters():
    registration = _registration("full_disk")
    assert "+proj=geos" in registration["crs"]
    assert "+lon_0=-137.0" in registration["crs"]
    west, south, east, north = registration["extent"]
    assert west == pytest.approx(-east) and south == pytest.approx(-north)


def test_the_sub_satellite_point_lands_at_the_centre_of_the_disk():
    registration = _registration("full_disk")
    x, y = _scan_angles(registration, _GOES18_QUERY["lon0"], 0.0)
    assert x == pytest.approx(0.0, abs=1.0) and y == pytest.approx(0.0, abs=1.0)


def test_a_known_lon_lat_lands_where_the_scan_geometry_says_it_does():
    """The closed form for a point on the equator: the satellite is
    ``sat_alt`` from the Earth's centre, the point is one equatorial radius from it, and
    the angle the satellite turns through to see it is what the square is linear in."""
    registration = _registration("full_disk")
    height = _GOES18_QUERY["sat_alt"] * 1000.0 - ss._EARTH_A_M
    side_px = 678 * (2 ** 3)
    west, south, east, north = registration["extent"]
    for degrees_east in (5.0, 20.0, 45.0, -45.0):
        separation = math.radians(degrees_east)
        expected_rad = math.atan(
            ss._EARTH_A_M * math.sin(separation)
            / (_GOES18_QUERY["sat_alt"] * 1000.0
               - ss._EARTH_A_M * math.cos(separation)))
        x, _y = _scan_angles(registration, _GOES18_QUERY["lon0"] + degrees_east, 0.0)
        column = (x - west) / (east - west) * side_px
        expected_column = (expected_rad * height - west) / (east - west) * side_px
        assert column == pytest.approx(expected_column, abs=1.0)


def test_a_measured_sub_window_sits_on_its_parent_grid():
    """The CONUS square is a window on the same fixed grid as the disk, so a point is at
    the same scan angle in both - only the square it is measured from differs."""
    disk = _registration("full_disk")
    conus = _registration("conus")
    assert conus["crs"] == disk["crs"]
    for lon, lat in ((-122.45, 37.81), (-95.0, 29.3), (-117.2, 32.7)):
        assert _scan_angles(conus, lon, lat) == pytest.approx(
            _scan_angles(disk, lon, lat))
    west, south, east, north = conus["extent"]
    assert (east - west) == pytest.approx(north - south)
    assert (east - west) < (disk["extent"][2] - disk["extent"][0])


def test_a_steerable_or_unmeasured_sector_is_not_registered():
    assert _registration("mesoscale_01") is None
    assert ss.sector_registration(
        "jpss", "northern_hemisphere", _INDEX["jpss"]) is None


def test_registered_sectors_lists_only_what_can_be_placed():
    assert ss.registered_sectors(_INDEX) == ["goes-18/conus", "goes-18/full_disk"]


def test_an_aoi_beyond_the_limb_is_refused():
    registration = _registration("full_disk")
    with pytest.raises(ss.SliderEmptyError):
        # The far side of the Earth from GOES-West.
        ss.aoi_projected_bounds(registration, (40.0, 10.0, 41.0, 11.0))


def test_pick_zoom_within_bounds():
    bbox = (-113.346, 39.57, -111.765, 41.115)
    entry = _INDEX["goes-18"]["sectors"]["conus"]
    zoom = ss.pick_zoom_for_aoi(_registration("conus"), entry, bbox)
    assert 0 <= zoom <= entry["max_zoom_level"]


def test_aoi_pixel_window_inside_sector():
    bbox = (-113.346, 39.57, -111.765, 41.115)
    side = 625 * (2 ** 2)
    win = ss._aoi_to_pixel_window(_registration("conus"), bbox, side)
    px_min_x, px_min_y, px_max_x, px_max_y = win
    assert 0 <= px_min_x < px_max_x <= side
    assert 0 <= px_min_y < px_max_y <= side


def test_usable_zoom_steps_down_to_what_the_product_is_tiled_at():
    """The index states the zoom the viewer offers; the deepest zoom a product is
    rendered to is a question only the tile store answers."""
    asked: list[str] = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    class _Sess:
        def get(self, url, **kwargs):
            asked.append(url)
            return _Resp(404 if "/05/" in url or "/04/" in url else 200)

    assert ss.usable_zoom(
        "goes-18", "full_disk", "geocolor", 20260914210020, 5, session=_Sess()) == 3
    assert asked[0].endswith("/05/016_016.png")
    assert asked[-1].endswith("/03/004_004.png")


def test_a_product_tiled_at_no_zoom_is_empty_not_a_blank_frame():
    class _Sess:
        def get(self, url, **kwargs):
            class _R:
                status_code = 404
            return _R()

    with pytest.raises(ss.SliderEmptyError):
        ss.usable_zoom("goes-18", "conus", "geocolor", 20260914210020, 2,
                       session=_Sess())
