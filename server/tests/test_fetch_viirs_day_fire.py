"""Unit tests for ``fetch_viirs_day_fire`` (fire-animation demo J3, the core net-new).

Coverage:
- Registration + metadata + the CONFIRMED Day Fire SLIDER product slug.
- ``_parse_utc`` parsing.
- ``_is_daytime_pass`` keeps daytime / drops night at the AOI longitude.
- ``_build_pass_list`` windows + day-filters + merge/SORTS the irregular polar
  pass timestamps ascending (multi-satellite merged set) + caps.
- bbox-required + unknown satellite/product raise typed errors.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools._satellite_slider import ts_int_to_iso
from grace2_agent.tools.fetch_viirs_day_fire import (
    DAY_FIRE_PRODUCT_SLUG,
    VIIRSDayFireBboxRequiredError,
    VIIRSDayFireInputError,
    _build_pass_list,
    _is_daytime_pass,
    _parse_utc,
    fetch_viirs_day_fire,
)

# Channel Islands AOI (Santa Rosa Island), offshore CA ~ -120.06 lon.
_CI_BBOX = (-120.50, 33.85, -119.50, 34.10)
_CI_CENTER_LON = (-120.50 + -119.50) / 2.0  # ~ -120.0


# ---- registration ---------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_viirs_day_fire" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_viirs_day_fire"]
    assert entry.metadata.name == "fetch_viirs_day_fire"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "viirs_satellite"
    assert entry.metadata.cacheable is True


def test_day_fire_slug_is_confirmed_value():
    # CONFIRMED LIVE 2026-06-22 from the JPSS product list.
    assert DAY_FIRE_PRODUCT_SLUG == "cira_natural_fire_color"


# ---- _parse_utc -----------------------------------------------------------


def test_parse_utc_iso():
    assert _parse_utc("2026-05-15T20:47:00Z") == datetime(2026, 5, 15, 20, 47, tzinfo=timezone.utc)


def test_parse_utc_rejects_garbage():
    with pytest.raises(VIIRSDayFireInputError):
        _parse_utc("xyz")


# ---- day/night filter -----------------------------------------------------


def _ts(y, mo, d, h, mi):
    return int(f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}00")


def test_is_daytime_pass_keeps_local_afternoon():
    # ~21:00Z over -120 lon => local solar ~ 21 - 8 = 13:00 LST -> DAY.
    assert _is_daytime_pass(_ts(2026, 5, 15, 21, 0), _CI_CENTER_LON) is True


def test_is_daytime_pass_drops_local_night():
    # ~09:30Z over -120 lon => local solar ~ 09:30 - 8 = 01:30 LST -> NIGHT.
    assert _is_daytime_pass(_ts(2026, 5, 15, 9, 30), _CI_CENTER_LON) is False


# ---- pass-list assembly (multi-sat merge/sort + day-only + window) --------


def test_build_pass_list_merges_sorts_and_day_filters():
    # An irregular, OUT-OF-ORDER set of passes spanning day + night.
    all_ts = [
        _ts(2026, 5, 16, 21, 0),   # day
        _ts(2026, 5, 15, 21, 30),  # day (earlier date)
        _ts(2026, 5, 15, 9, 30),   # NIGHT (local ~01:30) -> dropped
        _ts(2026, 5, 16, 20, 0),   # day
        _ts(2026, 5, 17, 23, 0),   # OUTSIDE window (after end) -> dropped
    ]
    start = datetime(2026, 5, 15, 20, 47, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 22, 1, tzinfo=timezone.utc)
    passes = _build_pass_list(all_ts, start, end, _CI_CENTER_LON, day_only=True)
    # Day-only, in-window, ASCENDING (merge/sort across the unordered input).
    assert passes == [
        _ts(2026, 5, 15, 21, 30),
        _ts(2026, 5, 16, 20, 0),
        _ts(2026, 5, 16, 21, 0),
    ]
    assert passes == sorted(passes)


def test_build_pass_list_day_only_false_keeps_night():
    all_ts = [_ts(2026, 5, 15, 9, 30), _ts(2026, 5, 15, 21, 0)]
    start = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 16, 0, 0, tzinfo=timezone.utc)
    passes = _build_pass_list(all_ts, start, end, _CI_CENTER_LON, day_only=False)
    assert len(passes) == 2


def test_pass_labels_carry_real_utc():
    assert ts_int_to_iso(_ts(2026, 5, 15, 20, 47)) == "2026-05-15T20:47:00Z"


# ---- typed-error surface --------------------------------------------------


def test_bbox_none_raises_bbox_required():
    with pytest.raises(VIIRSDayFireBboxRequiredError):
        fetch_viirs_day_fire(bbox=None)  # type: ignore[arg-type]


def test_unknown_satellite_raises():
    with pytest.raises(VIIRSDayFireInputError):
        fetch_viirs_day_fire(bbox=_CI_BBOX, satellite="terra")


def test_unknown_product_raises():
    with pytest.raises(VIIRSDayFireInputError):
        fetch_viirs_day_fire(bbox=_CI_BBOX, product="night_microphysics")
