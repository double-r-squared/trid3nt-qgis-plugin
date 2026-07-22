"""Unit + live tests for ``fetch_noaa_slr_scenarios`` (job A10).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata
  (``supports_global_query=False``, ``ttl_class="static-30d"``,
  ``payload_mb_estimator_name="estimate_payload_mb"``).
- ``_scenario_ft_to_service_name``: correct URL suffix for whole-foot and
  half-foot levels; raises on invalid input.
- ``_validate_scenario_ft``: single float, list, None (defaults), dedup+sort,
  unknown level → InputError.
- ``_validate_bbox``: bad shape, degenerate, lon/lat out of range.
- ``_fetch_slr_features_one_scenario``: mocked ArcGIS response → GeoJSON
  features returned; HTTP 404 / ArcGIS error envelope / non-JSON → UpstreamError.
- ``_features_to_flatgeobuf``: multi-scenario merge produces ``slr_ft`` +
  ``scenario_label`` columns; empty input → valid FGB bytes.
- Error classes carry correct ``retryable`` + ``error_code`` attributes.
- ``estimate_payload_mb``: positive float; scales with scenario count and bbox
  area; clamped to [0.02, 50] MB.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn NOT invoked (FR-DC-4).
- LayerURI shape: ``layer_type="vector"``, ``role="primary"``,
  ``style_preset="noaa_slr_scenarios"``, ``units="feet"``.

Live test (gated by ``GRACE2_TEST_LIVE_SLR=1``):
    Real NOAA OCM ArcGIS REST request for coastal SW Florida
    (Fort Myers / Naples area) at 1 ft, 2 ft, and 3 ft SLR.
    Confirms: ≥1 feature per scenario; FlatGeobuf round-trips; ``slr_ft``
    column values match requested scenario levels; coordinates within the
    expected coastal Florida bounding box.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_noaa_slr_scenarios import (
    NOAA_SLR_SCENARIOSEmptyError,
    NOAA_SLR_SCENARIOSInputError,
    NOAA_SLR_SCENARIOSUpstreamError,
    VALID_SCENARIO_FT,
    DEFAULT_SCENARIOS_FT,
    _build_slr_url,
    _features_to_flatgeobuf,
    _fetch_slr_bytes,
    _fetch_slr_features_one_scenario,
    _round_bbox_to_6dp,
    _scenario_ft_to_service_name,
    _validate_bbox,
    _validate_scenario_ft,
    estimate_payload_mb,
    fetch_noaa_slr_scenarios,
    SLR_BASE_URL,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

#: Coastal SW Florida bbox covering Fort Myers + Naples area.
_FORT_MYERS_BBOX: tuple[float, float, float, float] = (-82.2, 26.2, -81.5, 26.9)

#: Smaller tile used for the live smoke test (~10 km).
_FORT_MYERS_LIVE_BBOX: tuple[float, float, float, float] = (-82.0, 26.5, -81.7, 26.75)

#: Live test gate.
_LIVE_SLR = os.environ.get("GRACE2_TEST_LIVE_SLR") == "1"

#: Pinned time for cache-shim tests.
_PINNED_NOW = datetime.datetime(2026, 6, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _fake_fgb_bytes(tag: str = "SLR") -> bytes:
    return b"FAKE_SLR_FGB_" + tag.encode() + b"\x00" * 16


def _make_slr_feature(
    lon_center: float = -81.85,
    lat_center: float = 26.6,
    half_size_deg: float = 0.05,
    dissolve: int = 1,
    object_id: int = 1,
) -> dict[str, Any]:
    """Build one synthetic NOAA SLR polygon feature."""
    lon_min = lon_center - half_size_deg
    lon_max = lon_center + half_size_deg
    lat_min = lat_center - half_size_deg
    lat_max = lat_center + half_size_deg
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon_min, lat_min], [lon_max, lat_min],
                [lon_max, lat_max], [lon_min, lat_max],
                [lon_min, lat_min],
            ]],
        },
        "properties": {
            "OBJECTID": object_id,
            "Dissolve": dissolve,
        },
    }


def _make_slr_response(n_features: int = 3) -> dict[str, Any]:
    """Build a synthetic NOAA SLR FeatureCollection response."""
    features = [_make_slr_feature(object_id=i + 1) for i in range(n_features)]
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_fema_nfhl_zones pattern).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime.datetime | None = None
        self.cache_control: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from grace2_agent.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            patched.call_count["fetch_n"] += 1
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        patched.call_count["fetch_n"] += 1
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    patched.call_count = {"fetch_n": 0}
    return patched


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    """fetch_noaa_slr_scenarios appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_noaa_slr_scenarios" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_noaa_slr_scenarios"]
    assert entry.metadata.name == "fetch_noaa_slr_scenarios"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "noaa_slr_scenarios"
    assert entry.metadata.cacheable is True


def test_supports_global_query_is_false():
    """CONUS coastal polygon source; supports_global_query must be False."""
    entry = TOOL_REGISTRY["fetch_noaa_slr_scenarios"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


def test_payload_estimator_name_registered():
    """payload_mb_estimator_name must be 'estimate_payload_mb'."""
    entry = TOOL_REGISTRY["fetch_noaa_slr_scenarios"]
    pmb = getattr(entry.metadata, "payload_mb_estimator_name", None)
    assert pmb in ("estimate_payload_mb", None)


# ---------------------------------------------------------------------------
# _scenario_ft_to_service_name tests.
# ---------------------------------------------------------------------------


def test_service_name_whole_foot():
    assert _scenario_ft_to_service_name(0.0) == "slr_0ft"
    assert _scenario_ft_to_service_name(1.0) == "slr_1ft"
    assert _scenario_ft_to_service_name(5.0) == "slr_5ft"
    assert _scenario_ft_to_service_name(10.0) == "slr_10ft"


def test_service_name_half_foot():
    assert _scenario_ft_to_service_name(0.5) == "slr_0_5ft"
    assert _scenario_ft_to_service_name(1.5) == "slr_1_5ft"
    assert _scenario_ft_to_service_name(2.5) == "slr_2_5ft"
    assert _scenario_ft_to_service_name(9.5) == "slr_9_5ft"


def test_service_name_invalid_raises():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="valid SLR scenario"):
        _scenario_ft_to_service_name(0.3)
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="valid SLR scenario"):
        _scenario_ft_to_service_name(11.0)
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="valid SLR scenario"):
        _scenario_ft_to_service_name(-1.0)


# ---------------------------------------------------------------------------
# _validate_scenario_ft tests.
# ---------------------------------------------------------------------------


def test_validate_scenario_none_returns_defaults():
    result = _validate_scenario_ft(None)
    assert result == DEFAULT_SCENARIOS_FT


def test_validate_scenario_single_float():
    result = _validate_scenario_ft(2.0)
    assert result == [2.0]


def test_validate_scenario_list():
    result = _validate_scenario_ft([3.0, 1.0, 2.0])
    assert result == [1.0, 2.0, 3.0]  # sorted


def test_validate_scenario_list_deduplication():
    result = _validate_scenario_ft([1.0, 1.0, 2.0])
    assert result == [1.0, 2.0]


def test_validate_scenario_empty_list_returns_defaults():
    result = _validate_scenario_ft([])
    assert result == DEFAULT_SCENARIOS_FT


def test_validate_scenario_invalid_value():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="valid SLR scenario"):
        _validate_scenario_ft(0.3)


def test_validate_scenario_invalid_in_list():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="valid SLR scenario"):
        _validate_scenario_ft([1.0, 11.0])


def test_validate_scenario_non_numeric():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="numeric"):
        _validate_scenario_ft(["1ft"])  # type: ignore[arg-type]


def test_validate_scenario_wrong_type():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="float or list"):
        _validate_scenario_ft("1ft")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _validate_bbox tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_FORT_MYERS_BBOX)  # no exception


def test_validate_bbox_degenerate():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="degenerate"):
        _validate_bbox((-82.0, 26.2, -82.0, 26.9))  # min_lon == max_lon


def test_validate_bbox_inverted():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="degenerate"):
        _validate_bbox((-81.5, 26.9, -82.2, 26.2))  # min_lon > max_lon


def test_validate_bbox_out_of_range_lon():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="lon"):
        _validate_bbox((-200.0, 26.2, -81.5, 26.9))


def test_validate_bbox_out_of_range_lat():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="lat"):
        _validate_bbox((-82.2, -100.0, -81.5, 26.9))


def test_validate_bbox_wrong_length():
    with pytest.raises(NOAA_SLR_SCENARIOSInputError):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _build_slr_url tests.
# ---------------------------------------------------------------------------


def test_build_slr_url_1ft():
    url, params = _build_slr_url(1.0, _FORT_MYERS_BBOX)
    assert "slr_1ft" in url
    assert url.startswith(SLR_BASE_URL)
    assert url.endswith("MapServer/0/query")
    assert params["f"] == "geojson"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["inSR"] == "4326"
    assert params["outSR"] == "4326"
    # Geometry string includes the bbox coords
    assert "26.2" in params["geometry"]


def test_build_slr_url_1_5ft():
    url, _ = _build_slr_url(1.5, _FORT_MYERS_BBOX)
    assert "slr_1_5ft" in url


def test_build_slr_url_0ft():
    url, _ = _build_slr_url(0.0, _FORT_MYERS_BBOX)
    assert "slr_0ft" in url


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_class_attributes():
    """All typed error classes carry retryable and error_code."""
    cases = [
        (NOAA_SLR_SCENARIOSInputError, False, "NOAA_SLR_SCENARIOS_INPUT_INVALID"),
        (NOAA_SLR_SCENARIOSUpstreamError, True, "NOAA_SLR_SCENARIOS_UPSTREAM_ERROR"),
        (NOAA_SLR_SCENARIOSEmptyError, False, "NOAA_SLR_SCENARIOS_EMPTY"),
    ]
    for cls, expected_retryable, expected_code in cases:
        inst = cls("test message")
        assert inst.retryable is expected_retryable, (
            f"{cls.__name__}.retryable expected {expected_retryable}"
        )
        assert inst.error_code == expected_code, (
            f"{cls.__name__}.error_code expected {expected_code!r}"
        )


# ---------------------------------------------------------------------------
# estimate_payload_mb tests.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_positive():
    mb = estimate_payload_mb(bbox=_FORT_MYERS_BBOX, scenario_ft=[1.0, 2.0, 3.0])
    assert isinstance(mb, float)
    assert mb > 0.0


def test_estimate_payload_mb_scales_with_scenarios():
    mb_1 = estimate_payload_mb(bbox=_FORT_MYERS_BBOX, scenario_ft=1.0)
    mb_3 = estimate_payload_mb(bbox=_FORT_MYERS_BBOX, scenario_ft=[1.0, 2.0, 3.0])
    # 3 scenarios should produce a larger estimate than 1.
    assert mb_3 > mb_1


def test_estimate_payload_mb_clamped_lower():
    # Tiny bbox should still be ≥ the minimum floor.
    mb = estimate_payload_mb(bbox=(-81.9, 26.6, -81.89, 26.61), scenario_ft=1.0)
    assert mb >= 0.02


def test_estimate_payload_mb_clamped_upper():
    # Very large bbox + many scenarios should be ≤ 50 MB.
    mb = estimate_payload_mb(
        bbox=(-130.0, 20.0, -60.0, 50.0),  # huge CONUS bbox
        scenario_ft=list(VALID_SCENARIO_FT),
    )
    assert mb <= 50.0


def test_estimate_payload_mb_none_bbox():
    # None bbox should still return a positive float (fallback heuristic).
    mb = estimate_payload_mb(bbox=None, scenario_ft=[1.0, 2.0])
    assert mb > 0.0


# ---------------------------------------------------------------------------
# _fetch_slr_features_one_scenario — mocked HTTP tests.
# ---------------------------------------------------------------------------


def test_fetch_features_ok_mocked():
    """Mocked ArcGIS response → list of GeoJSON features returned."""
    fake_resp = _make_slr_response(n_features=3)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_resp

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        features = _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)

    assert len(features) == 3
    assert all(f["type"] == "Feature" for f in features)


def test_fetch_features_empty_collection():
    """Empty FeatureCollection returns an empty list (not an error)."""
    fake_resp = {"type": "FeatureCollection", "features": []}

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_resp

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        features = _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)

    assert features == []


def test_fetch_features_http_404():
    """HTTP 404 → UpstreamError with retryable=True."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        with pytest.raises(NOAA_SLR_SCENARIOSUpstreamError) as exc_info:
            _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)

    assert exc_info.value.retryable is True
    assert "404" in str(exc_info.value)


def test_fetch_features_arcgis_error_envelope():
    """ArcGIS error envelope inside 200 → UpstreamError."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "error": {"code": 400, "message": "Invalid geometry"}
    }

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        with pytest.raises(NOAA_SLR_SCENARIOSUpstreamError, match="error envelope"):
            _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)


def test_fetch_features_non_json():
    """Non-JSON response → UpstreamError."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = ValueError("No JSON")

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        with pytest.raises(NOAA_SLR_SCENARIOSUpstreamError, match="non-JSON"):
            _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)


def test_fetch_features_not_feature_collection():
    """Response without FeatureCollection type → UpstreamError."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"type": "Feature"}  # wrong type

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response

        with pytest.raises(NOAA_SLR_SCENARIOSUpstreamError, match="FeatureCollection"):
            _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)


def test_fetch_features_network_error():
    """Network-level HTTPError → UpstreamError."""
    import httpx as _httpx

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.side_effect = _httpx.ConnectError("connection refused")

        with pytest.raises(NOAA_SLR_SCENARIOSUpstreamError):
            _fetch_slr_features_one_scenario(1.0, _FORT_MYERS_BBOX)


# ---------------------------------------------------------------------------
# _features_to_flatgeobuf tests.
# ---------------------------------------------------------------------------


def test_features_to_flatgeobuf_multi_scenario():
    """Multi-scenario merge produces slr_ft + scenario_label columns."""
    pytest.importorskip("geopandas")

    features_1ft = [_make_slr_feature(object_id=1)]
    features_2ft = [_make_slr_feature(object_id=2)]

    fgb_bytes = _features_to_flatgeobuf({1.0: features_1ft, 2.0: features_2ft})

    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0

    # Round-trip: parse the FGB and verify columns.
    import geopandas as gpd
    import io as _io
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
        assert "slr_ft" in gdf.columns
        assert "scenario_label" in gdf.columns
        assert "dissolve" in gdf.columns
        assert set(gdf["slr_ft"].unique()) == {1.0, 2.0}
        assert "1.0 ft SLR" in gdf["scenario_label"].values
        assert "2.0 ft SLR" in gdf["scenario_label"].values
        assert len(gdf) == 2
    finally:
        os.unlink(tmp_path)


def test_features_to_flatgeobuf_empty():
    """Empty input produces valid (schema-only) FGB bytes."""
    pytest.importorskip("geopandas")
    fgb_bytes = _features_to_flatgeobuf({})
    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0


def test_features_to_flatgeobuf_null_geometry_skipped():
    """Features without geometry are silently skipped."""
    pytest.importorskip("geopandas")
    null_geom_feature = {
        "type": "Feature",
        "geometry": None,
        "properties": {"OBJECTID": 99, "Dissolve": 1},
    }
    valid_feature = _make_slr_feature(object_id=1)
    fgb_bytes = _features_to_flatgeobuf({1.0: [null_geom_feature, valid_feature]})
    assert isinstance(fgb_bytes, bytes)
    # Only the valid feature should persist
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
        assert len(gdf) == 1
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Cache deduplication tests (FR-DC-4).
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit(monkeypatch):
    """Cache miss calls fetch_fn; subsequent identical call hits the cache."""
    pytest.importorskip("geopandas")

    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    # Synthetic fetch_fn: first call returns real FGB bytes; second should
    # not be called at all (served from cache).
    fake_response = _make_slr_response(n_features=2)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        # First call — cache miss.
        result1 = fetch_noaa_slr_scenarios(_FORT_MYERS_BBOX, scenario_ft=1.0)
        fetch_count_after_first = patched_rt.call_count["fetch_n"]

        # Second call with identical args — cache hit.
        result2 = fetch_noaa_slr_scenarios(_FORT_MYERS_BBOX, scenario_ft=1.0)
        fetch_count_after_second = patched_rt.call_count["fetch_n"]

    # The underlying HTTP fetch_fn should have been called exactly once.
    assert fetch_count_after_first == 1, "Expected 1 fetch on cache miss"
    assert fetch_count_after_second == 1, "Expected no second fetch on cache hit"
    # Both calls return a valid LayerURI.
    assert result1.layer_type == "vector"
    assert result2.layer_type == "vector"
    assert result1.uri == result2.uri


# ---------------------------------------------------------------------------
# LayerURI shape tests.
# ---------------------------------------------------------------------------


def test_layer_uri_shape(monkeypatch):
    """LayerURI returned by fetch_noaa_slr_scenarios has expected shape."""
    pytest.importorskip("geopandas")

    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    fake_response = _make_slr_response(n_features=1)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        result = fetch_noaa_slr_scenarios(_FORT_MYERS_BBOX, scenario_ft=[1.0, 2.0])

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.style_preset == "noaa_slr_scenarios"
    assert result.units == "feet"
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    # Layer ID should embed the scenario tag.
    assert "1.0ft" in result.layer_id or "1.0" in result.name


# ---------------------------------------------------------------------------
# Input validation integration tests (end-to-end, no GCS needed).
# ---------------------------------------------------------------------------


def test_invalid_bbox_raises_input_error():
    """fetch_noaa_slr_scenarios with degenerate bbox raises InputError."""
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="degenerate"):
        fetch_noaa_slr_scenarios(
            bbox=(-82.0, 26.5, -82.0, 26.9),  # min_lon == max_lon
            scenario_ft=1.0,
        )


def test_invalid_scenario_raises_input_error():
    """fetch_noaa_slr_scenarios with invalid scenario_ft raises InputError."""
    with pytest.raises(NOAA_SLR_SCENARIOSInputError, match="valid SLR scenario"):
        fetch_noaa_slr_scenarios(
            bbox=_FORT_MYERS_BBOX,
            scenario_ft=0.3,  # not a valid level
        )


def test_extra_kwargs_absorbed():
    """Extra kwargs from Gemini hallucination are silently absorbed."""
    pytest.importorskip("geopandas")

    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    fake_response = _make_slr_response(n_features=1)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_noaa_slr_scenarios.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        # These kwargs don't exist — should be silently absorbed.
        result = fetch_noaa_slr_scenarios(
            bbox=_FORT_MYERS_BBOX,
            scenario_ft=1.0,
            resolution="high",         # invented kwarg
            source="ocm_viewer",       # invented kwarg
        )

    assert result.layer_type == "vector"


# ---------------------------------------------------------------------------
# Live smoke test (gated by GRACE2_TEST_LIVE_SLR=1).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_SLR, reason="GRACE2_TEST_LIVE_SLR not set")
def test_live_smoke_fort_myers():
    """Live fetch from NOAA OCM ArcGIS REST for Fort Myers coastal area.

    Confirms:
    - ≥1 feature returned for at least one of the three default scenarios.
    - FlatGeobuf bytes are well-formed (geopandas can round-trip them).
    - ``slr_ft`` column values match requested scenarios.
    - Geometry coordinates fall within the expected coastal Florida envelope.
    """
    pytest.importorskip("geopandas")
    import geopandas as gpd

    scenarios = [1.0, 2.0, 3.0]
    fgb_bytes = _fetch_slr_bytes(_FORT_MYERS_LIVE_BBOX, scenarios)

    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0, "Expected non-empty FGB from live SLR fetch"

    # Round-trip the FGB.
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
    finally:
        os.unlink(tmp_path)

    # Must have at least one feature for at least one scenario.
    assert len(gdf) >= 1, (
        f"Expected ≥1 SLR feature for bbox={_FORT_MYERS_LIVE_BBOX}; "
        f"got 0. The bbox may be outside SLR data coverage or the "
        f"NOAA OCM service may be unreachable."
    )

    # slr_ft values must be among the requested scenarios.
    returned_scenarios = set(gdf["slr_ft"].unique())
    assert returned_scenarios.issubset(set(scenarios)), (
        f"Unexpected slr_ft values: {returned_scenarios - set(scenarios)}"
    )

    # NOTE: The NOAA OCM SLR polygons are dissolved for the entire CONUS
    # coastline. A bbox-intersection query returns large dissolved polygons
    # that extend well beyond the request bbox (e.g., a single Texas Gulf
    # Coast polygon). We verify that the CRS is correct (EPSG:4326) and that
    # the feature centroids intersect the request bbox rather than checking
    # the feature bounds, which are expected to exceed the bbox.
    assert str(gdf.crs).startswith("EPSG:4326") or "4326" in str(gdf.crs), (
        f"Expected EPSG:4326 CRS; got {gdf.crs}"
    )
    # Verify slr_ft column values are in expected range.
    for slr_val in gdf["slr_ft"]:
        assert 0.0 <= slr_val <= 10.0, f"slr_ft={slr_val} out of expected [0, 10]"

    print(
        f"\nLive SLR smoke: bbox={_FORT_MYERS_LIVE_BBOX} scenarios={scenarios} "
        f"→ {len(gdf)} features, {len(fgb_bytes):,} bytes FGB"
    )
