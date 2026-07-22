"""Unit tests for the shared CIRA/RAMMB SLIDER substrate (tools/_satellite_slider.py).

Pure-helper coverage (no network): URL builders (date YYYY/MM/DD slashes,
tileY_tileX order, zoom %02d), timestamp round-trip, the time-index reader
against a mocked latest_times.json, zoom selection bounds, and the AOI pixel
window mapping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from grace2_agent.tools import _satellite_slider as ss


def test_ts_round_trip():
    ts = 20260622192600
    dt = ss.ts_int_to_datetime(ts)
    assert dt == datetime(2026, 6, 22, 19, 26, 0, tzinfo=timezone.utc)
    assert ss.ts_int_to_iso(ts) == "2026-06-22T19:26:00Z"


def test_build_tile_url_date_slashes_and_order():
    url = ss.build_tile_url("goes-18", "conus", "geocolor", 20260622192600, 2, 1, 3)
    # Date dir is YYYY/MM/DD with slashes (CONFIRMED live).
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


def test_pick_zoom_within_bounds():
    bbox = (-113.346, 39.57, -111.765, 41.115)  # Utah cluster
    z = ss.pick_zoom_for_aoi("goes-18", "conus", bbox)
    assert 0 <= z <= ss.SECTOR_MAX_ZOOM[("goes-18", "conus")]


def test_aoi_pixel_window_inside_sector():
    bbox = (-113.346, 39.57, -111.765, 41.115)
    side = ss.TILE_SIZE[("goes-18", "conus")] * (2 ** 2)
    win = ss._aoi_to_pixel_window("goes-18", "conus", bbox, side)
    px_min_x, px_min_y, px_max_x, px_max_y = win
    assert 0 <= px_min_x < px_max_x <= side
    assert 0 <= px_min_y < px_max_y <= side


def test_tile_sizes_confirmed():
    assert ss.TILE_SIZE[("goes-18", "conus")] == 625
    assert ss.TILE_SIZE[("jpss", "conus")] == 500
