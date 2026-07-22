"""Unit tests for the ``fetch_usgs_volcano_alerts`` atomic tool.

Real USGS Volcano Hazards Program HANS public API fetcher (current volcano
alert levels as points). All HTTP is mocked or operates on synthetic JSON
bodies — no live network in the default suite.

Coverage:
- Error classes carry correct ``retryable`` + ``error_code`` attributes.
- Input validation: bad bbox (shape / range / degenerate / non-finite).
- Alert-list parse: synthetic getMonitoredVolcanoes -> {vnum: record}; null-vnum
  aggregate placeholders dropped; alert/color uppercased.
- Coord-list parse: synthetic getUSVolcanoes -> {vnum: record}; null/out-of-
  range lat/lon dropped; elevation coerced.
- Join: inner-join on vnum; alerts with no coordinate dropped; severity sort.
- bbox filter: only points inside the bbox kept.
- Severity ranks: alert/color rank ladders + unknown -> -1.
- FlatGeobuf builder: synthetic records -> valid Point FGB round-trips through
  geopandas with the expected columns.
- Honest-empty path: a bbox with no volcano -> VolcanoAlertsNoVolcanoesError;
  an empty monitored list -> VolcanoAlertsNoVolcanoesError (never an empty
  success-shaped layer).
- Payload estimator returns a positive float.

Live test (gated by TRID3NT_TEST_LIVE_USGS_VOLCANO=1): a real HANS request for a
global US snapshot; confirms >=1 volcano with a valid alert level + coordinate.
"""

from __future__ import annotations

import os

import pytest

from trid3nt_server.tools.fetchers.hazard.fetch_usgs_volcano_alerts import (
    ALERT_LEVELS,
    COLOR_CODES,
    VolcanoAlertsInputError,
    VolcanoAlertsNoVolcanoesError,
    VolcanoAlertsUpstreamError,
    _alert_rank,
    _build_flatgeobuf,
    _color_rank,
    _fetch_usgs_volcano_alerts_bytes,
    _filter_to_bbox,
    _join_alerts_to_coords,
    _parse_alert_list,
    _parse_coord_list,
    _validate_bbox,
    _volcanoes_bbox,
    estimate_payload_mb,
    fetch_usgs_volcano_alerts,
)
from trid3nt_server.tools.fetchers.hazard import fetch_usgs_volcano_alerts as _mod


# ---------------------------------------------------------------------------
# Synthetic fixtures mirroring the live HANS JSON shape.
# ---------------------------------------------------------------------------

_MONITORED = [
    {
        "volcano_name": "Kilauea",
        "vnum": "332010",
        "alert_level": "watch",  # lower-case on purpose -> should uppercase
        "color_code": "orange",
        "obs_abbr": "hvo",
        "sent_utc": "2026-06-27 21:18:38",
        "notice_url": "https://volcanoes.usgs.gov/hans-public/notice/X",
    },
    {
        "volcano_name": "Mauna Loa",
        "vnum": "332020",
        "alert_level": "NORMAL",
        "color_code": "GREEN",
        "obs_abbr": "hvo",
        "sent_utc": "2026-06-01 00:00:00",
        "notice_url": "https://volcanoes.usgs.gov/hans-public/notice/Y",
    },
    {
        "volcano_name": "Great Sitkin",
        "vnum": "311120",
        "alert_level": "WATCH",
        "color_code": "ORANGE",
        "obs_abbr": "avo",
        "sent_utc": "2026-06-27 21:18:38",
        "notice_url": "https://volcanoes.usgs.gov/hans-public/notice/Z",
    },
    # Aggregate placeholder with null vnum -> MUST be dropped.
    {
        "volcano_name": "Alaskan Volcanoes",
        "vnum": None,
        "alert_level": "NORMAL",
        "color_code": "GREEN",
        "obs_abbr": "avo",
    },
]

_US_VOLCANOES = [
    {
        "vnum": "332010",
        "volcano_name": "Kilauea",
        "latitude": 19.421,
        "longitude": -155.287,
        "elevation_meters": 1247,
        "region": "Hawaii",
        "volcano_url": "https://volcano.example/kilauea",
        "nvews_threat": "Very High Threat",
    },
    {
        "vnum": "332020",
        "volcano_name": "Mauna Loa",
        "latitude": 19.475,
        "longitude": -155.608,
        "elevation_meters": 4170,
        "region": "Hawaii",
        "volcano_url": "https://volcano.example/maunaloa",
        "nvews_threat": "Very High Threat",
    },
    {
        "vnum": "311120",
        "volcano_name": "Great Sitkin",
        "latitude": 52.0765,
        "longitude": -176.1109,
        "elevation_meters": 1740,
        "region": "Alaska - Aleutians",
        "volcano_url": "https://volcano.example/greatsitkin",
        "nvews_threat": "High Threat",
    },
    # Volcano with a bad latitude -> MUST be dropped from coords.
    {
        "vnum": "999999",
        "volcano_name": "Bad Coord",
        "latitude": None,
        "longitude": -100.0,
        "elevation_meters": 100,
        "region": "Nowhere",
    },
]


# ---------------------------------------------------------------------------
# Error-class contract.
# ---------------------------------------------------------------------------


def test_error_classes_have_codes_and_retryable():
    assert VolcanoAlertsInputError.retryable is False
    assert VolcanoAlertsInputError.error_code == "USGS_VOLCANO_ALERTS_INPUT_ERROR"
    assert VolcanoAlertsUpstreamError.retryable is True
    assert (
        VolcanoAlertsUpstreamError.error_code
        == "USGS_VOLCANO_ALERTS_UPSTREAM_ERROR"
    )
    assert VolcanoAlertsNoVolcanoesError.retryable is False
    assert (
        VolcanoAlertsNoVolcanoesError.error_code
        == "USGS_VOLCANO_ALERTS_NO_VOLCANOES"
    )


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        (1, 2, 3),  # wrong length
        (-200.0, 0.0, 10.0, 10.0),  # lon out of range
        (0.0, -100.0, 10.0, 10.0),  # lat out of range
        (10.0, 0.0, 5.0, 10.0),  # west >= east
        (0.0, 10.0, 10.0, 5.0),  # south >= north
        (float("nan"), 0.0, 10.0, 10.0),  # non-finite
    ],
)
def test_validate_bbox_rejects_bad(bad):
    with pytest.raises(VolcanoAlertsInputError):
        _validate_bbox(bad)


def test_validate_bbox_accepts_good():
    _validate_bbox((-156.5, 18.8, -154.5, 20.5))  # no raise


def test_fetch_rejects_bad_bbox_type():
    with pytest.raises(VolcanoAlertsInputError):
        fetch_usgs_volcano_alerts(bbox="not-a-bbox")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Parsers.
# ---------------------------------------------------------------------------


def test_parse_alert_list_drops_null_vnum_and_uppercases():
    out = _parse_alert_list(_MONITORED)
    # 4 input rows, 1 has null vnum -> 3 keyed.
    assert set(out.keys()) == {"332010", "332020", "311120"}
    # lower-case input was uppercased.
    assert out["332010"]["alert_level"] == "WATCH"
    assert out["332010"]["color_code"] == "ORANGE"
    assert out["332010"]["observatory"] == "hvo"


def test_parse_alert_list_rejects_non_list():
    with pytest.raises(VolcanoAlertsUpstreamError):
        _parse_alert_list({"not": "a list"})


def test_parse_coord_list_drops_bad_coords():
    out = _parse_coord_list(_US_VOLCANOES)
    # 4 input rows, 1 has null latitude -> 3 keyed.
    assert set(out.keys()) == {"332010", "332020", "311120"}
    assert out["332010"]["lat"] == pytest.approx(19.421)
    assert out["332010"]["lon"] == pytest.approx(-155.287)
    assert out["332010"]["elevation_m"] == pytest.approx(1247.0)
    assert "999999" not in out


def test_parse_coord_list_rejects_non_list():
    with pytest.raises(VolcanoAlertsUpstreamError):
        _parse_coord_list("nope")


# ---------------------------------------------------------------------------
# Severity ranks.
# ---------------------------------------------------------------------------


def test_alert_and_color_ranks():
    assert _alert_rank("NORMAL") == 0
    assert _alert_rank("warning") == 3  # case-insensitive
    assert _alert_rank("bogus") == -1
    assert _alert_rank(None) == -1
    assert _color_rank("GREEN") == 0
    assert _color_rank("red") == 3
    assert _color_rank(None) == -1
    # Ladders are the documented 4-stage scales.
    assert ALERT_LEVELS == ("NORMAL", "ADVISORY", "WATCH", "WARNING")
    assert COLOR_CODES == ("GREEN", "YELLOW", "ORANGE", "RED")


# ---------------------------------------------------------------------------
# Join + filter.
# ---------------------------------------------------------------------------


def test_join_inner_joins_on_vnum_and_sorts_by_severity():
    alerts = _parse_alert_list(_MONITORED)
    coords = _parse_coord_list(_US_VOLCANOES)
    merged = _join_alerts_to_coords(alerts, coords)
    # All 3 valid alerts have coords -> 3 merged records.
    assert len(merged) == 3
    names = [r["volcano_name"] for r in merged]
    # Severity-descending: the two ORANGE/WATCH come before the GREEN/NORMAL.
    assert names[-1] == "Mauna Loa"  # NORMAL/GREEN sorts last
    # Every merged record carries a coordinate + alert + ranks.
    for r in merged:
        assert r["lat"] is not None and r["lon"] is not None
        assert r["alert_level"] in ALERT_LEVELS
        assert r["color_code"] in COLOR_CODES
        assert r["alert_rank"] >= 0 and r["color_rank"] >= 0


def test_join_drops_alert_without_coordinate():
    alerts = {"123": {"vnum": "123", "volcano_name": "Orphan",
                      "alert_level": "WATCH", "color_code": "ORANGE",
                      "observatory": "avo", "sent_utc": None,
                      "notice_url": None}}
    coords: dict = {}  # no coordinate for vnum 123
    merged = _join_alerts_to_coords(alerts, coords)
    assert merged == []


def test_filter_to_bbox_keeps_only_inside_points():
    alerts = _parse_alert_list(_MONITORED)
    coords = _parse_coord_list(_US_VOLCANOES)
    merged = _join_alerts_to_coords(alerts, coords)
    # Hawaii bbox excludes Great Sitkin (Aleutians).
    hawaii = _filter_to_bbox(merged, (-156.5, 18.8, -154.5, 20.5))
    names = {r["volcano_name"] for r in hawaii}
    assert names == {"Kilauea", "Mauna Loa"}
    # None bbox -> no filter.
    assert len(_filter_to_bbox(merged, None)) == 3


def test_volcanoes_bbox_pads_single_point():
    one = [{"lon": -155.0, "lat": 19.0}]
    bb = _volcanoes_bbox(one)
    assert bb is not None
    west, south, east, north = bb
    assert west < -155.0 < east and south < 19.0 < north
    assert _volcanoes_bbox([]) is None


# ---------------------------------------------------------------------------
# FlatGeobuf builder round-trip.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_roundtrips():
    gpd = pytest.importorskip("geopandas")
    alerts = _parse_alert_list(_MONITORED)
    coords = _parse_coord_list(_US_VOLCANOES)
    merged = _join_alerts_to_coords(alerts, coords)
    raw = _build_flatgeobuf(merged)
    assert isinstance(raw, bytes) and len(raw) > 0

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        gdf = gpd.read_file(path)
    finally:
        os.unlink(path)

    assert len(gdf) == 3
    for col in (
        "vnum",
        "volcano_name",
        "alert_level",
        "color_code",
        "alert_rank",
        "color_rank",
        "elevation_m",
        "region",
        "observatory",
        "nvews_threat",
    ):
        assert col in gdf.columns
    assert set(gdf.geom_type) == {"Point"}
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326


# ---------------------------------------------------------------------------
# Honest-empty path (mocked HTTP, no network).
# ---------------------------------------------------------------------------


def test_no_volcanoes_in_bbox_is_honest_error(monkeypatch):
    def fake_get(url, timeout=60.0):
        if "getMonitoredVolcanoes" in url:
            return _MONITORED
        return _US_VOLCANOES

    monkeypatch.setattr(_mod, "_http_get_json", fake_get)
    # A bbox over the open Atlantic contains no US volcano.
    with pytest.raises(VolcanoAlertsNoVolcanoesError):
        _fetch_usgs_volcano_alerts_bytes(bbox=(-40.0, 30.0, -30.0, 40.0))


def test_empty_monitored_list_is_honest_error(monkeypatch):
    def fake_get(url, timeout=60.0):
        if "getMonitoredVolcanoes" in url:
            return []
        return _US_VOLCANOES

    monkeypatch.setattr(_mod, "_http_get_json", fake_get)
    with pytest.raises(VolcanoAlertsNoVolcanoesError):
        _fetch_usgs_volcano_alerts_bytes(bbox=None)


def test_full_fetch_bytes_happy_path(monkeypatch):
    pytest.importorskip("geopandas")

    def fake_get(url, timeout=60.0):
        if "getMonitoredVolcanoes" in url:
            return _MONITORED
        return _US_VOLCANOES

    monkeypatch.setattr(_mod, "_http_get_json", fake_get)
    raw, extent = _fetch_usgs_volcano_alerts_bytes(bbox=None)
    assert isinstance(raw, bytes) and len(raw) > 0
    west, south, east, north = extent
    assert west < east and south < north


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive_and_small():
    mb = estimate_payload_mb()
    assert isinstance(mb, float) and mb > 0.0
    # The monitored list is tiny; the layer is well under a megabyte.
    assert mb < 1.0
    assert estimate_payload_mb(bbox=(-156.5, 18.8, -154.5, 20.5)) > 0.0


# ---------------------------------------------------------------------------
# Live test (opt-in via env; hits the real USGS HANS API).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("TRID3NT_TEST_LIVE_USGS_VOLCANO") != "1",
    reason="set TRID3NT_TEST_LIVE_USGS_VOLCANO=1 to hit the live USGS HANS API",
)
def test_live_global_snapshot_has_volcanoes():
    pytest.importorskip("geopandas")
    raw, extent = _fetch_usgs_volcano_alerts_bytes(bbox=None)
    assert isinstance(raw, bytes) and len(raw) > 0
    import tempfile

    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        gdf = gpd.read_file(path)
    finally:
        os.unlink(path)
    assert len(gdf) >= 1
    assert gdf["alert_level"].isin(list(ALERT_LEVELS)).any()
