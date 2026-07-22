"""Unit tests for the ``fetch_snotel_snow`` atomic tool.

Real NRCS SNOTEL/SCAN snow-station fetcher (observed Snow Water Equivalent +
snow depth) from the AWDB REST API. All HTTP is mocked — no live network in the
default run.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Error classes carry correct retryable + error_code attributes.
- Input validation: missing bbox, malformed bbox, degenerate bbox.
- Stations parse: only SNTL/SCAN networks kept; SNOW/USGS/COOP leaked rows and
  non-finite-coord rows dropped.
- bbox filter: only stations inside the bbox survive.
- DATA parse: latest non-null WTEQ/SNWD per station; off-season 0.0 PRESERVED as
  an honest reading; null sample skipped.
- merge: stations with no reading keep null swe/depth (locations survive).
- Happy path through fetch_snotel_snow (mocked HTTP + read_through): LayerURI
  shape (vector / primary / style_preset / bbox set).
- No stations in bbox -> SnotelNoStationsError (honest typed error).
- DATA unreachable but stations exist -> locations with null readings (degrade).
- Payload estimator returns a positive float.

Live test (gated by TRID3NT_TEST_LIVE_SNOTEL=1): real AWDB request for a small
Colorado Rockies bbox; confirms >=1 SNOTEL station returned.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_snotel_snow import (
    SnotelError,
    SnotelInputError,
    SnotelNoStationsError,
    SnotelUpstreamError,
    _build_data_url,
    _build_stations_url,
    _filter_stations_to_bbox,
    _merge_readings,
    _parse_data_json,
    _parse_stations_json,
    _records_bbox,
    _validate_bbox,
    estimate_payload_mb,
    fetch_snotel_snow,
)

_LIVE_SNOTEL = os.environ.get("TRID3NT_TEST_LIVE_SNOTEL") == "1"

# Colorado Front Range / Berthoud Pass mountain bbox — known SNOTEL coverage.
_CO_BBOX = (-106.5, 39.0, -105.5, 40.0)

# A lowland bbox with no SNOTEL coverage (central Kansas).
_KS_BBOX = (-98.5, 38.0, -97.5, 39.0)


# ---------------------------------------------------------------------------
# Synthetic AWDB payloads.
# ---------------------------------------------------------------------------


def _station(
    triplet: str,
    name: str,
    state: str,
    network: str,
    lat: float,
    lon: float,
    elevation: float | None = 10000.0,
) -> dict[str, Any]:
    return {
        "stationTriplet": triplet,
        "stationId": triplet.split(":")[0],
        "stateCode": state,
        "networkCode": network,
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "elevation": elevation,
    }


def _stations_json(stations: list[dict[str, Any]]) -> bytes:
    return json.dumps(stations).encode("utf-8")


def _data_block(
    triplet: str,
    *,
    swe: list[tuple[str, float | None]] | None = None,
    depth: list[tuple[str, float | None]] | None = None,
) -> dict[str, Any]:
    data: list[dict[str, Any]] = []
    if swe is not None:
        data.append(
            {
                "stationElement": {"elementCode": "WTEQ", "storedUnitCode": "in"},
                "values": [{"date": d, "value": v} for d, v in swe],
            }
        )
    if depth is not None:
        data.append(
            {
                "stationElement": {"elementCode": "SNWD", "storedUnitCode": "in"},
                "values": [{"date": d, "value": v} for d, v in depth],
            }
        )
    return {"stationTriplet": triplet, "data": data}


def _data_json(blocks: list[dict[str, Any]]) -> bytes:
    return json.dumps(blocks).encode("utf-8")


# Two SNOTEL stations inside the CO bbox, plus leaked non-snow / out-of-bbox.
_BERTHOUD = _station("335:CO:SNTL", "Berthoud Summit", "CO", "SNTL", 39.80, -105.78)
_COPPER = _station("415:CO:SNTL", "Copper Mountain", "CO", "SNTL", 39.49, -106.15)
_SCAN_SITE = _station("2057:CO:SCAN", "Some SCAN", "CO", "SCAN", 39.50, -105.90)
_MANUAL_SNOW = _station("05K12:CO:SNOW", "Arrow #2", "CO", "SNOW", 39.91, -105.76)
_OUT_OF_BBOX = _station("999:WA:SNTL", "Far Away", "WA", "SNTL", 47.0, -121.0)


# ---------------------------------------------------------------------------
# Registration + metadata.
# ---------------------------------------------------------------------------


def test_tool_registered() -> None:
    assert "fetch_snotel_snow" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_snotel_snow"]
    md = entry.metadata if hasattr(entry, "metadata") else entry[1]
    assert md.name == "fetch_snotel_snow"
    assert md.ttl_class == "dynamic-1h"
    assert md.source_class == "snotel_snow"
    assert md.cacheable is True


def test_error_taxonomy() -> None:
    assert issubclass(SnotelInputError, SnotelError)
    assert issubclass(SnotelUpstreamError, SnotelError)
    assert issubclass(SnotelNoStationsError, SnotelError)
    assert SnotelInputError.retryable is False
    assert SnotelUpstreamError.retryable is True
    assert SnotelNoStationsError.retryable is False
    assert SnotelInputError.error_code == "SNOTEL_INPUT_ERROR"
    assert SnotelNoStationsError.error_code == "SNOTEL_NO_STATIONS"
    assert SnotelUpstreamError.error_code == "SNOTEL_UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_missing_bbox_raises_input_error() -> None:
    with pytest.raises(SnotelInputError):
        fetch_snotel_snow()


@pytest.mark.parametrize(
    "bad",
    [
        (1.0, 2.0, 3.0),  # wrong arity
        (-200.0, 0.0, 10.0, 1.0),  # lon out of range
        (0.0, -100.0, 1.0, 1.0),  # lat out of range
        (10.0, 0.0, 5.0, 1.0),  # west >= east
        (0.0, 5.0, 1.0, 1.0),  # south >= north
        (float("nan"), 0.0, 1.0, 1.0),  # non-finite
    ],
)
def test_validate_bbox_rejects_bad(bad: Any) -> None:
    with pytest.raises(SnotelInputError):
        _validate_bbox(bad)


def test_validate_bbox_accepts_good() -> None:
    _validate_bbox(_CO_BBOX)  # no raise


# ---------------------------------------------------------------------------
# URL builders.
# ---------------------------------------------------------------------------


def test_build_stations_url() -> None:
    url = _build_stations_url()
    assert url.startswith("https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/stations?")
    assert "networkCds=SCAN%2CSNTL" in url or "networkCds=SNTL%2CSCAN" in url
    assert "activeOnly=true" in url


def test_build_data_url() -> None:
    url = _build_data_url(["335:CO:SNTL", "415:CO:SNTL"], begin_date="2026-06-17", end_date="2026-06-27")
    assert url.startswith("https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1/data?")
    assert "elements=WTEQ%2CSNWD" in url
    assert "duration=DAILY" in url
    assert "beginDate=2026-06-17" in url
    assert "endDate=2026-06-27" in url


# ---------------------------------------------------------------------------
# Station parsing + bbox filter.
# ---------------------------------------------------------------------------


def test_parse_stations_keeps_only_snow_networks() -> None:
    raw = _stations_json([_BERTHOUD, _SCAN_SITE, _MANUAL_SNOW, _OUT_OF_BBOX])
    parsed = _parse_stations_json(raw)
    nets = {s["network"] for s in parsed}
    assert nets == {"SNTL", "SCAN"}  # SNOW manual course dropped
    assert all(isinstance(s["lat"], float) for s in parsed)


def test_parse_stations_drops_bad_coords() -> None:
    bad = _station("000:CO:SNTL", "No Coord", "CO", "SNTL", lat=None, lon=None)  # type: ignore[arg-type]
    raw = _stations_json([_BERTHOUD, bad])
    parsed = _parse_stations_json(raw)
    assert {s["triplet"] for s in parsed} == {"335:CO:SNTL"}


def test_parse_stations_empty_body() -> None:
    assert _parse_stations_json(b"") == []
    assert _parse_stations_json(b"{}") == []  # non-list top level


def test_filter_to_bbox() -> None:
    stations = _parse_stations_json(_stations_json([_BERTHOUD, _COPPER, _OUT_OF_BBOX]))
    inside = _filter_stations_to_bbox(stations, _CO_BBOX)
    assert {s["triplet"] for s in inside} == {"335:CO:SNTL", "415:CO:SNTL"}


# ---------------------------------------------------------------------------
# Data parsing (latest non-null; off-season zero preserved).
# ---------------------------------------------------------------------------


def test_parse_data_latest_value() -> None:
    raw = _data_json(
        [
            _data_block(
                "335:CO:SNTL",
                swe=[("2026-03-01", 16.6), ("2026-03-02", 17.0), ("2026-03-03", 17.4)],
                depth=[("2026-03-01", 60.0), ("2026-03-03", 68.0)],
            )
        ]
    )
    readings = _parse_data_json(raw)
    rec = readings["335:CO:SNTL"]
    assert rec["swe_in"] == 17.4  # latest
    assert rec["snow_depth_in"] == 68.0
    assert rec["date"] == "2026-03-03"


def test_parse_data_offseason_zero_preserved() -> None:
    raw = _data_json([_data_block("335:CO:SNTL", swe=[("2026-06-27", 0.0)], depth=[("2026-06-27", 0.0)])])
    rec = _parse_data_json(raw)["335:CO:SNTL"]
    assert rec["swe_in"] == 0.0  # honest zero, NOT None
    assert rec["snow_depth_in"] == 0.0
    assert rec["date"] == "2026-06-27"


def test_parse_data_skips_null_sample() -> None:
    raw = _data_json(
        [_data_block("335:CO:SNTL", swe=[("2026-06-26", 1.2), ("2026-06-27", None)])]
    )
    rec = _parse_data_json(raw)["335:CO:SNTL"]
    # Latest NON-null wins; the trailing null is skipped.
    assert rec["swe_in"] == 1.2
    assert rec["date"] == "2026-06-26"


def test_parse_data_empty_body() -> None:
    assert _parse_data_json(b"") == {}


# ---------------------------------------------------------------------------
# Merge.
# ---------------------------------------------------------------------------


def test_merge_attaches_readings_and_keeps_null() -> None:
    stations = _parse_stations_json(_stations_json([_BERTHOUD, _COPPER]))
    readings = {"335:CO:SNTL": {"swe_in": 17.4, "snow_depth_in": 68.0, "date": "2026-03-03"}}
    merged = _merge_readings(stations, readings)
    by_trip = {m["triplet"]: m for m in merged}
    assert by_trip["335:CO:SNTL"]["swe_in"] == 17.4
    # Copper had no reading -> nulls, but the station LOCATION survives.
    assert by_trip["415:CO:SNTL"]["swe_in"] is None
    assert by_trip["415:CO:SNTL"]["lon"] is not None


# ---------------------------------------------------------------------------
# records_bbox.
# ---------------------------------------------------------------------------


def test_records_bbox_extent() -> None:
    recs = [{"lon": -105.78, "lat": 39.80}, {"lon": -106.15, "lat": 39.49}]
    ext = _records_bbox(recs)
    assert ext == (-106.15, 39.49, -105.78, 39.80)


def test_records_bbox_pads_single_point() -> None:
    ext = _records_bbox([{"lon": -105.78, "lat": 39.80}])
    assert ext is not None
    assert ext[0] < -105.78 < ext[2]
    assert ext[1] < 39.80 < ext[3]


def test_records_bbox_empty() -> None:
    assert _records_bbox([]) is None


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive() -> None:
    assert estimate_payload_mb(bbox=_CO_BBOX) > 0
    assert estimate_payload_mb() > 0


# ---------------------------------------------------------------------------
# End-to-end (mocked HTTP + read_through).
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, uri: str) -> None:
        self.uri = uri


def _fake_read_through(*, metadata: Any, params: Any, ext: str, fetch_fn: Any) -> _FakeResult:
    # Drive the real fetch_fn so the FGB build + extent capture run, then return
    # a deterministic URI (no S3 in the unit test).
    fetch_fn()
    return _FakeResult(f"s3://bucket/cache/dynamic-1h/snotel_snow/abc123.{ext}")


def test_happy_path_layeruri_shape() -> None:
    stations_raw = _stations_json([_BERTHOUD, _COPPER, _OUT_OF_BBOX])
    data_raw = _data_json(
        [
            _data_block("335:CO:SNTL", swe=[("2026-06-27", 0.0)], depth=[("2026-06-27", 0.0)]),
            _data_block("415:CO:SNTL", swe=[("2026-06-27", 0.2)], depth=[("2026-06-27", 1.0)]),
        ]
    )

    def fake_http_get(url: str, timeout: float = 0) -> bytes:
        return data_raw if "/data?" in url else stations_raw

    with patch("trid3nt_server.tools.fetch_snotel_snow._http_get", side_effect=fake_http_get), patch(
        "trid3nt_server.tools.fetch_snotel_snow.read_through", side_effect=_fake_read_through
    ):
        layer = fetch_snotel_snow(bbox=_CO_BBOX)

    assert layer.layer_type == "vector"
    assert layer.role == "primary"
    assert layer.style_preset == "snotel_snow"
    assert layer.uri.endswith(".fgb")
    assert layer.bbox is not None
    # Extent should hug the two in-bbox stations (not the WA outlier).
    w, s, e, n = layer.bbox
    assert -106.5 <= w and e <= -105.0
    assert 39.0 <= s and n <= 40.0


def test_no_stations_in_bbox_raises() -> None:
    stations_raw = _stations_json([_OUT_OF_BBOX])  # nothing inside KS bbox

    def fake_http_get(url: str, timeout: float = 0) -> bytes:
        return stations_raw

    with patch("trid3nt_server.tools.fetch_snotel_snow._http_get", side_effect=fake_http_get), patch(
        "trid3nt_server.tools.fetch_snotel_snow.read_through", side_effect=_fake_read_through
    ):
        with pytest.raises(SnotelNoStationsError):
            fetch_snotel_snow(bbox=_KS_BBOX)


def test_data_unreachable_degrades_to_locations() -> None:
    stations_raw = _stations_json([_BERTHOUD, _COPPER])

    def fake_http_get(url: str, timeout: float = 0) -> bytes:
        if "/data?" in url:
            raise SnotelUpstreamError("DATA service down")
        return stations_raw

    captured: dict[str, Any] = {}

    def capturing_read_through(*, metadata: Any, params: Any, ext: str, fetch_fn: Any) -> _FakeResult:
        fetch_fn()
        return _FakeResult(f"s3://bucket/x.{ext}")

    with patch("trid3nt_server.tools.fetch_snotel_snow._http_get", side_effect=fake_http_get), patch(
        "trid3nt_server.tools.fetch_snotel_snow.read_through", side_effect=capturing_read_through
    ):
        # Should NOT raise — locations survive with null readings.
        layer = fetch_snotel_snow(bbox=_CO_BBOX)
    assert layer.layer_type == "vector"
    assert layer.bbox is not None


# ---------------------------------------------------------------------------
# Live (opt-in).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_SNOTEL, reason="set TRID3NT_TEST_LIVE_SNOTEL=1 for live AWDB")
def test_live_colorado_rockies() -> None:
    from trid3nt_server.tools.fetch_snotel_snow import _fetch_snotel_snow_bytes

    fgb, extent = _fetch_snotel_snow_bytes(bbox=_CO_BBOX, now=datetime.date(2024, 3, 5))
    assert len(fgb) > 0
    assert extent is not None
