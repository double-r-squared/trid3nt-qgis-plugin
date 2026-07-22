"""Unit tests for the FIRMS historical-date positional (fire-animation demo S2/J2).

Covers the additive ``date`` argument on ``fetch_firms_active_fire``:
- ``_validate_date`` accepts YYYY-MM-DD, rejects malformed, passes None through.
- ``_build_firms_url`` appends the trailing /{YYYY-MM-DD} only when a date is
  given (rolling-window URL is byte-identical without it).
- The rolling path is unchanged (backward compat).
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire import (
    FirmsArgError,
    _build_firms_url,
    _validate_date,
)

_BBOX = (-113.346, 39.57, -111.765, 41.115)


def test_validate_date_accepts_iso():
    assert _validate_date("2026-06-22") == "2026-06-22"
    assert _validate_date(" 2026-05-15 ") == "2026-05-15"


def test_validate_date_none_passthrough():
    assert _validate_date(None) is None
    assert _validate_date("") is None


def test_validate_date_rejects_malformed():
    with pytest.raises(FirmsArgError):
        _validate_date("06/22/2026")
    with pytest.raises(FirmsArgError):
        _validate_date("2026-13-99")


def test_build_url_rolling_has_no_date_segment():
    url = _build_firms_url(_BBOX, 1, "VIIRS_SNPP_NRT", "KEY")
    assert url.endswith("/VIIRS_SNPP_NRT/-113.346,39.57,-111.765,41.115/1")
    assert url.count("/") == _build_firms_url(_BBOX, 1, "VIIRS_SNPP_NRT", "KEY").count("/")


def test_build_url_historical_appends_date():
    url = _build_firms_url(_BBOX, 1, "VIIRS_NOAA20_NRT", "KEY", date="2026-06-22")
    assert url.endswith(
        "/VIIRS_NOAA20_NRT/-113.346,39.57,-111.765,41.115/1/2026-06-22"
    )


def test_build_url_historical_is_byte_additive():
    rolling = _build_firms_url(_BBOX, 1, "VIIRS_SNPP_NRT", "KEY")
    historical = _build_firms_url(_BBOX, 1, "VIIRS_SNPP_NRT", "KEY", date="2026-05-15")
    # The historical URL is exactly the rolling URL + the /<date> segment.
    assert historical == f"{rolling}/2026-05-15"


def test_build_url_contains_key_and_source():
    url = _build_firms_url(_BBOX, 1, "VIIRS_SNPP_NRT", "MYKEY", date="2026-06-22")
    assert "/MYKEY/VIIRS_SNPP_NRT/" in url
