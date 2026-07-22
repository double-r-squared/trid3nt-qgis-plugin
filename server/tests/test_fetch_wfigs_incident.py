"""Unit tests for ``fetch_wfigs_incident`` (fire-animation demo S1/J1).

Coverage:
- Registration in TOOL_REGISTRY with expected metadata.
- ``_normalize_state`` handles UT / ut / US-UT.
- ``_build_wfigs_params`` builds a case-insensitive LIKE + strips trailing
  " Fire" + adds the POOState filter.
- ``_feature_point`` prefers InitialLat/Lon, falls back to geometry, rejects
  null-island.
- ``_select_best_feature`` picks the largest IncidentSize.
- ``_bbox_from_point`` builds a padded, lat-aware bbox.
- ``_epoch_ms_to_iso`` converts Esri epoch-ms -> ISO UTC.
- Name-resolution end to end (mock the ArcGIS response): asserts point + bbox +
  discovery date.
- Empty match raises a typed not-found error.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_wfigs_incident import (
    WFIGS_INCIDENT_BASE,
    WFIGS_INCIDENT_YTD_BASE,
    WFIGSIncidentInputError,
    WFIGSIncidentNotFoundError,
    _bbox_from_point,
    _build_wfigs_params,
    _epoch_ms_to_iso,
    _feature_point,
    _normalize_state,
    _select_best_feature,
    _significant_name_tokens,
    fetch_wfigs_incident,
)


# ---- registration ---------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_wfigs_incident" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_wfigs_incident"]
    assert entry.metadata.name == "fetch_wfigs_incident"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "wfigs_incident"
    assert entry.metadata.cacheable is True


# ---- pure helpers ---------------------------------------------------------


def test_normalize_state_forms():
    assert _normalize_state("UT") == "US-UT"
    assert _normalize_state("ut") == "US-UT"
    assert _normalize_state("US-UT") == "US-UT"
    assert _normalize_state("us-ca") == "US-CA"
    assert _normalize_state(None) is None
    assert _normalize_state("") is None


def test_normalize_state_rejects_bad():
    with pytest.raises(WFIGSIncidentInputError):
        _normalize_state("Utah")
    with pytest.raises(WFIGSIncidentInputError):
        _normalize_state("U1")


def test_build_wfigs_params_like_and_state():
    params = _build_wfigs_params("Iron Fire", "US-UT")
    # trailing " Fire" stripped, case-insensitive LIKE, state filter present.
    assert "UPPER(IncidentName) LIKE '%IRON%'" in params["where"]
    assert "POOState = 'US-UT'" in params["where"]
    assert params["outSR"] == "4326"
    assert params["f"] == "json"
    assert params["returnGeometry"] == "true"


def test_build_wfigs_params_no_state():
    params = _build_wfigs_params("Santa Rosa Island", None)
    assert "LIKE '%SANTA ROSA ISLAND%'" in params["where"]
    assert "POOState" not in params["where"]


def test_build_wfigs_params_escapes_quote():
    params = _build_wfigs_params("O'Brien", None)
    assert "O''BRIEN" in params["where"]


def test_feature_point_prefers_initial_latlon():
    feat = {
        "attributes": {"InitialLatitude": 39.96976, "InitialLongitude": -112.16481},
        "geometry": {"x": -113.0, "y": 40.0},
    }
    pt = _feature_point(feat)
    assert pt == (-112.16481, 39.96976)


def test_feature_point_falls_back_to_geometry():
    feat = {"attributes": {}, "geometry": {"x": -120.06, "y": 33.58}}
    pt = _feature_point(feat)
    assert pt == (-120.06, 33.58)


def test_feature_point_rejects_null_island():
    feat = {"attributes": {"InitialLatitude": 0.0, "InitialLongitude": 0.0}, "geometry": {}}
    assert _feature_point(feat) is None


def test_select_best_feature_picks_largest_size():
    feats = [
        {"attributes": {"IncidentName": "Small", "IncidentSize": 100, "InitialLatitude": 39.0, "InitialLongitude": -112.0}},
        {"attributes": {"IncidentName": "Big", "IncidentSize": 21935, "InitialLatitude": 39.9, "InitialLongitude": -112.1}},
    ]
    best = _select_best_feature(feats)
    assert best is not None
    assert best["attributes"]["IncidentName"] == "Big"


def test_select_best_feature_none_when_no_point():
    feats = [{"attributes": {"IncidentName": "X"}, "geometry": {}}]
    assert _select_best_feature(feats) is None


def test_bbox_from_point_padded_and_ordered():
    bbox = _bbox_from_point(-112.16481, 39.96976, pad_deg=0.25)
    assert len(bbox) == 4
    min_lon, min_lat, max_lon, max_lat = bbox
    assert min_lon < -112.16481 < max_lon
    assert min_lat < 39.96976 < max_lat
    # E-W pad widened by 1/cos(lat) so it is wider than the N-S pad.
    assert (max_lon - min_lon) > (max_lat - min_lat)


def test_epoch_ms_to_iso():
    # 2026-06-20T00:00:00Z = 1781913600000 ms.
    iso = _epoch_ms_to_iso(1781913600000)
    assert iso == "2026-06-20T00:00:00Z"
    assert _epoch_ms_to_iso(None) is None
    assert _epoch_ms_to_iso("not-a-number") is None


# ---- name resolution end to end (mocked ArcGIS) ---------------------------


_IRON_RESPONSE = {
    "features": [
        {
            "attributes": {
                "IncidentName": "Iron",
                "FireDiscoveryDateTime": 1781913600000,  # 2026-06-20T00:00:00Z
                "InitialLatitude": 39.96976,
                "InitialLongitude": -112.16481,
                "IncidentSize": 21935,
                "PercentContained": 10,
                "POOState": "US-UT",
                "POOCounty": "Juab",
                "IrwinID": "abc-123",
                "UniqueFireIdentifier": "2026-UTNFD-000123",
            },
            "geometry": {"x": -112.16481, "y": 39.96976},
        }
    ]
}


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _identity_read_through(metadata, params, ext, fetch_fn, **kw):
    """Bypass the cache: call fetch_fn and wrap its bytes in a result shim."""
    from trid3nt_server.tools.cache import ReadThroughResult

    data = fetch_fn()
    return ReadThroughResult(uri="s3://fake/wfigs.json", data=data, hit=False)


def test_name_resolution_returns_point_bbox_discovery():
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _FakeResp(_IRON_RESPONSE)

    with patch("trid3nt_server.tools.fetch_wfigs_incident.httpx.Client", _FakeClient), patch(
        "trid3nt_server.tools.fetch_wfigs_incident.read_through", _identity_read_through
    ):
        result = fetch_wfigs_incident("Iron", state="UT")

    assert result["incident_name"] == "Iron"
    assert result["lat"] == pytest.approx(39.96976)
    assert result["lon"] == pytest.approx(-112.16481)
    assert result["fire_discovery_datetime"] == "2026-06-20T00:00:00Z"
    assert result["incident_size_acres"] == 21935
    assert result["poo_state"] == "US-UT"
    bbox = result["bbox"]
    assert len(bbox) == 4
    # The resolved point is inside the derived bbox.
    assert bbox[0] < -112.16481 < bbox[2]
    assert bbox[1] < 39.96976 < bbox[3]


def test_empty_match_raises_not_found():
    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return _FakeResp({"features": []})

    with patch("trid3nt_server.tools.fetch_wfigs_incident.httpx.Client", _FakeClient), patch(
        "trid3nt_server.tools.fetch_wfigs_incident.read_through", _identity_read_through
    ):
        with pytest.raises(WFIGSIncidentNotFoundError):
            fetch_wfigs_incident("Nonexistent Fire")


def test_blank_name_raises_input_error():
    with pytest.raises(WFIGSIncidentInputError):
        fetch_wfigs_incident("   ")


# ---- FIX B: loose token-OR name match -------------------------------------


def test_significant_name_tokens_drops_noise():
    # "Fire" and short fragments are dropped; geographic tokens kept.
    assert _significant_name_tokens("Santa Rosa Island Fire") == [
        "SANTA",
        "ROSA",
        "ISLAND",
    ]
    # A single-token name yields a single token.
    assert _significant_name_tokens("Iron Fire") == ["IRON"]


def test_build_wfigs_params_multiword_uses_token_or():
    """A multi-word name ALSO OR-matches each significant token (loose match)."""
    params = _build_wfigs_params("Santa Rosa Island", None)
    where = params["where"]
    # whole-string contains is present ...
    assert "LIKE '%SANTA ROSA ISLAND%'" in where
    # ... plus a token-OR over each significant token.
    assert "LIKE '%SANTA%'" in where
    assert "LIKE '%ROSA%'" in where
    assert "LIKE '%ISLAND%'" in where
    assert " OR " in where


def test_build_wfigs_params_singleword_keeps_whole_match():
    """A single-token name keeps the original whole-string contains (no OR)."""
    params = _build_wfigs_params("Iron", None)
    assert params["where"] == "UPPER(IncidentName) LIKE '%IRON%'"
    assert " OR " not in params["where"]


def test_build_wfigs_params_multiword_with_state_filter():
    params = _build_wfigs_params("Santa Rosa Island", "US-CA")
    where = params["where"]
    assert " OR " in where
    assert "POOState = 'US-CA'" in where


# ---- FIX B: Current-then-YearToDate fallback ------------------------------


_SANTA_ROSA_YTD_RESPONSE = {
    "features": [
        {
            "attributes": {
                "IncidentName": "Santa Rosa Island",
                "FireDiscoveryDateTime": 1781913600000,
                "InitialLatitude": 33.958561,
                "InitialLongitude": -120.106659,
                "IncidentSize": 18379,
                "PercentContained": 100,
                "POOState": "US-CA",
                "POOCounty": "Santa Barbara",
                "IrwinID": "srx-999",
                "UniqueFireIdentifier": "2026-CASTF-000999",
            },
            "geometry": {"x": -120.106659, "y": 33.958561},
        }
    ]
}


def test_contained_fire_resolves_via_yeartodate_when_current_empty():
    """The Santa Rosa Island fix: a contained fire the 'Current' feed has dropped
    resolves against the 'YearToDate' all-incidents sibling."""
    calls: list[str] = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            calls.append(url)
            # 'Current' feed has NO match; 'YearToDate' resolves it.
            if url == WFIGS_INCIDENT_BASE:
                return _FakeResp({"features": []})
            return _FakeResp(_SANTA_ROSA_YTD_RESPONSE)

    with patch(
        "trid3nt_server.tools.fetch_wfigs_incident.httpx.Client", _FakeClient
    ), patch(
        "trid3nt_server.tools.fetch_wfigs_incident.read_through",
        _identity_read_through,
    ):
        result = fetch_wfigs_incident("Santa Rosa Island", state="CA")

    # Both feeds were queried, Current first then YearToDate.
    assert calls[0] == WFIGS_INCIDENT_BASE
    assert WFIGS_INCIDENT_YTD_BASE in calls
    assert result["incident_name"] == "Santa Rosa Island"
    assert result["incident_size_acres"] == 18379
    assert result["lat"] == pytest.approx(33.958561)
    assert result["lon"] == pytest.approx(-120.106659)


def test_not_found_only_after_both_feeds_miss():
    """The typed not-found raises ONLY after BOTH Current and YearToDate miss."""
    calls: list[str] = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            calls.append(url)
            return _FakeResp({"features": []})

    with patch(
        "trid3nt_server.tools.fetch_wfigs_incident.httpx.Client", _FakeClient
    ), patch(
        "trid3nt_server.tools.fetch_wfigs_incident.read_through",
        _identity_read_through,
    ):
        with pytest.raises(WFIGSIncidentNotFoundError):
            fetch_wfigs_incident("Nonexistent Fire")

    # Both endpoints were tried before giving up.
    assert WFIGS_INCIDENT_BASE in calls
    assert WFIGS_INCIDENT_YTD_BASE in calls
