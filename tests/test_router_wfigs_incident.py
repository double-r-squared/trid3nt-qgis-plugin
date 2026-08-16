"""WFIGS incident record fold parity (ADR 0076): fetch_wfigs_incident via the router.

Migrates the value-bearing coverage of the deleted twin's tests onto the spec-driven
record surface (shape=record): the PURE wfigs_incident hooks (state/pad validation +
token-OR LIKE build + 2-endpoint ordered plans + best-feature discovery record), the
Current->YearToDate short-circuit, the typed not-found, and the end-to-end record dict.
Offline: synthetic WFIGS ArcGIS JSON bodies + the in-memory read_through injector; the
real ArcGIS network path is unchanged (the router transport). Proof-by-migration for
the record-return output shape (route() -> dict, not a LayerURI).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.data.fetchers._router.executors import http_json
from trid3nt_server.data.fetchers._router.hooks import wfigs_incident as wfh
from trid3nt_server.data.fetchers._router.spec import load_spec_from_path

WFIGS_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/data/fetchers/hazard/fetch_wfigs_incident/source.yaml"
)
_CURRENT = WFIGS_SPEC.endpoints["current"].url
_YTD = WFIGS_SPEC.endpoints["year_to_date"].url
_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _inject_read_through(monkeypatch, store: dict[str, bytes]):
    from trid3nt_server.data.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path, compute_cache_key, is_cacheable,
    )

    def patched(metadata, params, ext, fetch_fn, **kw):
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        source_id = metadata.source_class or metadata.name
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{CACHE_BUCKET}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(router, "read_through", patched)


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


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #


def test_wfigs_promoted_as_router_spec():
    from trid3nt_server.data import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_wfigs_incident"]
    assert entry.metadata.source_class == "wfigs_incident"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.cacheable is True
    assert entry.fn.__module__.endswith("_promoted.fetch_wfigs_incident")


def test_wfigs_shape_is_record():
    assert WFIGS_SPEC.shape == "record"
    assert WFIGS_SPEC.output.layer_type == "record"
    assert WFIGS_SPEC.output.ext == "json"


# --------------------------------------------------------------------------- #
# Pure hooks.
# --------------------------------------------------------------------------- #


def test_normalize_state_forms():
    assert wfh._normalize_state("WFIGS_INCIDENT", "UT") == "US-UT"
    assert wfh._normalize_state("WFIGS_INCIDENT", "ut") == "US-UT"
    assert wfh._normalize_state("WFIGS_INCIDENT", "US-UT") == "US-UT"
    assert wfh._normalize_state("WFIGS_INCIDENT", "us-ca") == "US-CA"
    assert wfh._normalize_state("WFIGS_INCIDENT", None) is None
    assert wfh._normalize_state("WFIGS_INCIDENT", "") is None


def test_normalize_state_rejects_bad():
    for bad in ("Utah", "U1"):
        with pytest.raises(RouterInputError) as exc:
            wfh._normalize_state("WFIGS_INCIDENT", bad)
        assert exc.value.error_code == "WFIGS_INCIDENT_INPUT_INVALID"


def test_build_query_like_and_state():
    q = wfh._build_wfigs_query("Iron Fire", "US-UT")
    assert "UPPER(IncidentName) LIKE '%IRON%'" in q["where"]
    assert "POOState = 'US-UT'" in q["where"]
    assert q["outSR"] == "4326" and q["f"] == "json" and q["returnGeometry"] == "true"


def test_build_query_multiword_token_or():
    q = wfh._build_wfigs_query("Santa Rosa Island", None)
    where = q["where"]
    assert "LIKE '%SANTA ROSA ISLAND%'" in where
    assert "LIKE '%SANTA%'" in where and "LIKE '%ROSA%'" in where and "LIKE '%ISLAND%'" in where
    assert " OR " in where


def test_build_query_singleword_no_or():
    q = wfh._build_wfigs_query("Iron", None)
    assert q["where"] == "UPPER(IncidentName) LIKE '%IRON%'"
    assert " OR " not in q["where"]


def test_build_query_escapes_quote():
    assert "O''BRIEN" in wfh._build_wfigs_query("O'Brien", None)["where"]


def test_significant_name_tokens_drops_noise():
    assert wfh._significant_name_tokens("Santa Rosa Island Fire") == ["SANTA", "ROSA", "ISLAND"]
    assert wfh._significant_name_tokens("Iron Fire") == ["IRON"]


def test_feature_point_prefers_initial_latlon():
    feat = {"attributes": {"InitialLatitude": 39.96976, "InitialLongitude": -112.16481},
            "geometry": {"x": -113.0, "y": 40.0}}
    assert wfh._feature_point(feat) == (-112.16481, 39.96976)


def test_feature_point_geometry_fallback_and_null_island():
    assert wfh._feature_point({"attributes": {}, "geometry": {"x": -120.06, "y": 33.58}}) == (-120.06, 33.58)
    assert wfh._feature_point({"attributes": {"InitialLatitude": 0.0, "InitialLongitude": 0.0}, "geometry": {}}) is None


def test_select_best_feature_largest_size():
    feats = [
        {"attributes": {"IncidentName": "Small", "IncidentSize": 100, "InitialLatitude": 39.0, "InitialLongitude": -112.0}},
        {"attributes": {"IncidentName": "Big", "IncidentSize": 21935, "InitialLatitude": 39.9, "InitialLongitude": -112.1}},
    ]
    assert wfh._select_best_feature(feats)["attributes"]["IncidentName"] == "Big"
    assert wfh._select_best_feature([{"attributes": {"IncidentName": "X"}, "geometry": {}}]) is None


def test_bbox_from_point_padded_lat_aware():
    min_lon, min_lat, max_lon, max_lat = wfh._bbox_from_point(-112.16481, 39.96976, 0.25)
    assert min_lon < -112.16481 < max_lon and min_lat < 39.96976 < max_lat
    assert (max_lon - min_lon) > (max_lat - min_lat)


def test_epoch_ms_to_iso():
    assert wfh._epoch_ms_to_iso(1781913600000) == "2026-06-20T00:00:00Z"
    assert wfh._epoch_ms_to_iso(None) is None
    assert wfh._epoch_ms_to_iso("not-a-number") is None


def test_build_request_two_ordered_plans():
    plans = wfh.build_request(WFIGS_SPEC, {"incident_name": "Iron", "state": "UT", "bbox_pad_deg": 0.25})
    assert [p.url for p in plans] == [_CURRENT, _YTD]
    assert "POOState = 'US-UT'" in plans[0].params["where"]


def test_build_request_rejects_bad_pad_and_name():
    with pytest.raises(RouterInputError):
        wfh.build_request(WFIGS_SPEC, {"incident_name": "Iron", "bbox_pad_deg": 0.0})
    with pytest.raises(RouterInputError):
        wfh.build_request(WFIGS_SPEC, {"incident_name": "   "})


# --------------------------------------------------------------------------- #
# End-to-end route() -> record dict (not a LayerURI).
# --------------------------------------------------------------------------- #


def test_route_returns_discovery_record(monkeypatch):
    store: dict[str, bytes] = {}
    _inject_read_through(monkeypatch, store)
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: _body(_IRON_RESPONSE))

    result = router.route(WFIGS_SPEC, {"incident_name": "Iron", "state": "UT"})
    assert isinstance(result, dict)  # record shape: a dict, never a LayerURI
    assert result["incident_name"] == "Iron"
    assert result["lat"] == pytest.approx(39.96976)
    assert result["lon"] == pytest.approx(-112.16481)
    assert result["fire_discovery_datetime"] == "2026-06-20T00:00:00Z"
    assert result["incident_size_acres"] == 21935
    assert result["poo_state"] == "US-UT"
    assert result["irwin_id"] == "abc-123"
    assert result["unique_fire_identifier"] == "2026-UTNFD-000123"
    bbox = result["bbox"]
    assert bbox[0] < -112.16481 < bbox[2] and bbox[1] < 39.96976 < bbox[3]


def test_route_current_then_ytd_short_circuit(monkeypatch):
    _inject_read_through(monkeypatch, {})
    calls: list[str] = []

    def fake(plan):
        calls.append(plan.url)
        if plan.url == _CURRENT:
            return _body({"features": []})
        return _body(_SANTA_ROSA_YTD_RESPONSE)

    monkeypatch.setattr(http_json, "_get_raw", fake)
    result = router.route(WFIGS_SPEC, {"incident_name": "Santa Rosa Island", "state": "CA"})
    assert calls == [_CURRENT, _YTD]  # Current first, then YearToDate
    assert result["incident_name"] == "Santa Rosa Island"
    assert result["incident_size_acres"] == 18379
    assert result["lat"] == pytest.approx(33.958561)


def test_route_current_hit_skips_ytd(monkeypatch):
    _inject_read_through(monkeypatch, {})
    calls: list[str] = []

    def fake(plan):
        calls.append(plan.url)
        return _body(_IRON_RESPONSE)

    monkeypatch.setattr(http_json, "_get_raw", fake)
    router.route(WFIGS_SPEC, {"incident_name": "Iron"})
    assert calls == [_CURRENT]  # Current matched -> YTD never queried (short-circuit)


def test_route_not_found_after_both_feeds_miss(monkeypatch):
    _inject_read_through(monkeypatch, {})
    calls: list[str] = []

    def fake(plan):
        calls.append(plan.url)
        return _body({"features": []})

    monkeypatch.setattr(http_json, "_get_raw", fake)
    with pytest.raises(RouterEmptyError) as exc:
        router.route(WFIGS_SPEC, {"incident_name": "Nonexistent Fire"})
    assert exc.value.error_code == "WFIGS_INCIDENT_NOT_FOUND"
    assert exc.value.retryable is False
    assert calls == [_CURRENT, _YTD]  # both tried before the typed dead-end


def test_route_bad_state_raises_input(monkeypatch):
    _inject_read_through(monkeypatch, {})
    monkeypatch.setattr(http_json, "_get_raw", lambda plan: _body(_IRON_RESPONSE))
    with pytest.raises(RouterInputError) as exc:
        router.route(WFIGS_SPEC, {"incident_name": "Iron", "state": "Utah"})
    assert exc.value.error_code == "WFIGS_INCIDENT_INPUT_INVALID"
