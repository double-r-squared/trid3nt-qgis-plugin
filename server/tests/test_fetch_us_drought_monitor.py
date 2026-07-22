"""Unit + live tests for ``fetch_us_drought_monitor``.

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata
  (``supports_global_query=False``, ``ttl_class="semi-static-7d"``,
  ``payload_mb_estimator_name="estimate_payload_mb"``).
- ``_validate_bbox``: bad shape, degenerate, lon/lat out of range.
- ``_normalize_date``: None passthrough, dashed + compact forms, bad shape,
  non-string, impossible calendar date -> InputError.
- ``_build_usdm_url``: current vs archive endpoint selection + where clause.
- ``_fetch_usdm_features``: mocked ArcGIS response -> GeoJSON features;
  HTTP 404 / ArcGIS error envelope / non-JSON / wrong type / network -> Upstream.
- ``_features_to_flatgeobuf``: dm + label + period + valid_date columns;
  empty input -> valid FGB bytes; null geometry skipped; unparseable dm skipped.
- Error classes carry correct ``retryable`` + ``error_code`` attributes.
- ``estimate_payload_mb``: positive float; scales with bbox area; clamped.
- Cache miss -> fetch_fn invoked; cache hit -> fetch_fn NOT invoked (FR-DC-4).
- LayerURI shape: ``layer_type="vector"``, ``role="primary"``,
  ``style_preset="us_drought_monitor"``, ``units="dm_class"``.

Live test (gated by ``GRACE2_TEST_LIVE_USDM=1``):
    Real Esri Living Atlas USDM ArcGIS REST request for the drought-prone US
    Southwest (Arizona). Confirms >=1 feature; FlatGeobuf round-trips; ``dm``
    values are in [0, 4]; ``label`` present.
"""

from __future__ import annotations

import datetime
import os
import tempfile
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_us_drought_monitor import (
    DM_LABELS,
    US_DROUGHT_MONITOREmptyError,
    US_DROUGHT_MONITORInputError,
    US_DROUGHT_MONITORUpstreamError,
    USDM_ARCHIVE_URL,
    USDM_CURRENT_URL,
    _build_usdm_url,
    _features_to_flatgeobuf,
    _fetch_usdm_bytes,
    _fetch_usdm_features,
    _normalize_date,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_us_drought_monitor,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

#: Drought-prone US Southwest bbox (Arizona).
_AZ_BBOX: tuple[float, float, float, float] = (-114.0, 31.3, -109.0, 37.0)

#: Live test gate.
_LIVE_USDM = os.environ.get("GRACE2_TEST_LIVE_USDM") == "1"

#: Pinned time for cache-shim tests.
_PINNED_NOW = datetime.datetime(2026, 6, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_usdm_feature(
    dm: int = 2,
    period: str = "20260623",
    ddate_ms: int = 1782172800000,
    lon_center: float = -111.5,
    lat_center: float = 34.0,
    half_size_deg: float = 0.5,
    object_id: int = 1,
) -> dict[str, Any]:
    """Build one synthetic USDM drought-category polygon feature."""
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
            "dm": dm,
            "period": period,
            "ddate": ddate_ms,
        },
    }


def _make_usdm_response(dms: tuple[int, ...] = (0, 1, 2, 3)) -> dict[str, Any]:
    """Build a synthetic USDM FeatureCollection response (one poly per class)."""
    features = [
        _make_usdm_feature(dm=dm, object_id=i + 1) for i, dm in enumerate(dms)
    ]
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Fake S3 read-through injector (mirrors the SLR test pattern).
# ---------------------------------------------------------------------------


def _make_read_through_injector():
    """In-memory S3 read-through injector keyed by object KEY."""
    from grace2_agent.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store: dict[str, bytes] = {}

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
    assert "fetch_us_drought_monitor" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_us_drought_monitor"]
    assert entry.metadata.name == "fetch_us_drought_monitor"
    assert entry.metadata.ttl_class == "semi-static-7d"
    assert entry.metadata.source_class == "us_drought_monitor"
    assert entry.metadata.cacheable is True


def test_supports_global_query_is_false():
    entry = TOOL_REGISTRY["fetch_us_drought_monitor"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


def test_payload_estimator_name_registered():
    entry = TOOL_REGISTRY["fetch_us_drought_monitor"]
    pmb = getattr(entry.metadata, "payload_mb_estimator_name", None)
    assert pmb in ("estimate_payload_mb", None)


# ---------------------------------------------------------------------------
# _validate_bbox tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_AZ_BBOX)  # no exception


def test_validate_bbox_degenerate():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="degenerate"):
        _validate_bbox((-114.0, 31.3, -114.0, 37.0))  # min_lon == max_lon


def test_validate_bbox_inverted():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="degenerate"):
        _validate_bbox((-109.0, 37.0, -114.0, 31.3))  # min > max


def test_validate_bbox_out_of_range_lon():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="lon"):
        _validate_bbox((-200.0, 31.3, -109.0, 37.0))


def test_validate_bbox_out_of_range_lat():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="lat"):
        _validate_bbox((-114.0, -100.0, -109.0, 37.0))


def test_validate_bbox_wrong_length():
    with pytest.raises(US_DROUGHT_MONITORInputError):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_validate_bbox_non_finite():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="non-finite"):
        _validate_bbox((-114.0, 31.3, float("nan"), 37.0))


# ---------------------------------------------------------------------------
# _normalize_date tests.
# ---------------------------------------------------------------------------


def test_normalize_date_none():
    assert _normalize_date(None) is None


def test_normalize_date_dashed():
    assert _normalize_date("2021-08-17") == "20210817"


def test_normalize_date_compact():
    assert _normalize_date("20210817") == "20210817"


def test_normalize_date_whitespace():
    assert _normalize_date("  2021-08-17  ") == "20210817"


def test_normalize_date_bad_shape():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="8 digits"):
        _normalize_date("Aug 17 2021")


def test_normalize_date_non_string():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="string"):
        _normalize_date(20210817)  # type: ignore[arg-type]


def test_normalize_date_impossible_calendar():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="valid calendar"):
        _normalize_date("2021-13-40")


# ---------------------------------------------------------------------------
# _build_usdm_url tests.
# ---------------------------------------------------------------------------


def test_build_url_current():
    url, params = _build_usdm_url(_AZ_BBOX, None)
    assert url == USDM_CURRENT_URL
    assert params["where"] == "1=1"
    assert params["f"] == "geojson"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["inSR"] == "4326"
    assert params["outSR"] == "4326"
    assert "31.3" in params["geometry"]


def test_build_url_archive_with_period():
    url, params = _build_usdm_url(_AZ_BBOX, "20210817")
    assert url == USDM_ARCHIVE_URL
    assert params["where"] == "period='20210817'"


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_class_attributes():
    cases = [
        (US_DROUGHT_MONITORInputError, False, "US_DROUGHT_MONITOR_INPUT_INVALID"),
        (US_DROUGHT_MONITORUpstreamError, True, "US_DROUGHT_MONITOR_UPSTREAM_ERROR"),
        (US_DROUGHT_MONITOREmptyError, False, "US_DROUGHT_MONITOR_EMPTY"),
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
    mb = estimate_payload_mb(bbox=_AZ_BBOX)
    assert isinstance(mb, float)
    assert mb > 0.0


def test_estimate_payload_mb_scales_with_area():
    mb_small = estimate_payload_mb(bbox=(-111.0, 34.0, -110.5, 34.5))
    mb_large = estimate_payload_mb(bbox=_AZ_BBOX)
    assert mb_large > mb_small


def test_estimate_payload_mb_clamped_lower():
    mb = estimate_payload_mb(bbox=(-111.0, 34.0, -110.99, 34.01))
    assert mb >= 0.05


def test_estimate_payload_mb_clamped_upper():
    mb = estimate_payload_mb(bbox=(-130.0, 20.0, -60.0, 50.0))  # huge CONUS
    assert mb <= 60.0


def test_estimate_payload_mb_none_bbox():
    mb = estimate_payload_mb(bbox=None)
    assert mb > 0.0


# ---------------------------------------------------------------------------
# _fetch_usdm_features — mocked HTTP tests.
# ---------------------------------------------------------------------------


def _patch_client(mock_response):
    return patch("grace2_agent.tools.fetch_us_drought_monitor.httpx.Client")


def test_fetch_features_ok_mocked():
    fake_resp = _make_usdm_response(dms=(0, 1, 2, 3))
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_resp

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        features = _fetch_usdm_features(_AZ_BBOX, None)

    assert len(features) == 4
    assert all(f["type"] == "Feature" for f in features)


def test_fetch_features_empty_collection():
    fake_resp = {"type": "FeatureCollection", "features": []}
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = fake_resp

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        features = _fetch_usdm_features(_AZ_BBOX, None)

    assert features == []


def test_fetch_features_http_404():
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not found"

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        with pytest.raises(US_DROUGHT_MONITORUpstreamError) as exc_info:
            _fetch_usdm_features(_AZ_BBOX, None)

    assert exc_info.value.retryable is True
    assert "404" in str(exc_info.value)


def test_fetch_features_arcgis_error_envelope():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "error": {"code": 400, "message": "Invalid geometry"}
    }

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        with pytest.raises(US_DROUGHT_MONITORUpstreamError, match="error envelope"):
            _fetch_usdm_features(_AZ_BBOX, None)


def test_fetch_features_non_json():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.side_effect = ValueError("No JSON")

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        with pytest.raises(US_DROUGHT_MONITORUpstreamError, match="non-JSON"):
            _fetch_usdm_features(_AZ_BBOX, None)


def test_fetch_features_not_feature_collection():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"type": "Feature"}

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        with pytest.raises(US_DROUGHT_MONITORUpstreamError, match="FeatureCollection"):
            _fetch_usdm_features(_AZ_BBOX, None)


def test_fetch_features_network_error():
    import httpx as _httpx

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.side_effect = _httpx.ConnectError("connection refused")
        with pytest.raises(US_DROUGHT_MONITORUpstreamError):
            _fetch_usdm_features(_AZ_BBOX, None)


# ---------------------------------------------------------------------------
# _features_to_flatgeobuf tests.
# ---------------------------------------------------------------------------


def test_features_to_flatgeobuf_columns():
    pytest.importorskip("geopandas")
    feats = [
        _make_usdm_feature(dm=0, object_id=1),
        _make_usdm_feature(dm=2, object_id=2),
        _make_usdm_feature(dm=4, object_id=3),
    ]
    fgb_bytes = _features_to_flatgeobuf(feats)
    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
        assert "dm" in gdf.columns
        assert "label" in gdf.columns
        assert "period" in gdf.columns
        assert "valid_date" in gdf.columns
        assert set(gdf["dm"].unique()) == {0, 2, 4}
        assert "D2 Severe Drought" in gdf["label"].values
        assert "D4 Exceptional Drought" in gdf["label"].values
        # ddate epoch ms -> ISO date
        assert (gdf["valid_date"] == "2026-06-23").all()
        assert len(gdf) == 3
    finally:
        os.unlink(tmp_path)


def test_features_to_flatgeobuf_empty():
    pytest.importorskip("geopandas")
    fgb_bytes = _features_to_flatgeobuf([])
    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0


def test_features_to_flatgeobuf_null_geometry_skipped():
    pytest.importorskip("geopandas")
    null_geom = {
        "type": "Feature",
        "geometry": None,
        "properties": {"dm": 1, "period": "20260623", "ddate": 1782172800000},
    }
    valid = _make_usdm_feature(dm=1, object_id=1)
    fgb_bytes = _features_to_flatgeobuf([null_geom, valid])
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
        assert len(gdf) == 1
    finally:
        os.unlink(tmp_path)


def test_features_to_flatgeobuf_unparseable_dm_skipped():
    pytest.importorskip("geopandas")
    bad_dm = {
        "type": "Feature",
        "geometry": _make_usdm_feature()["geometry"],
        "properties": {"dm": "not-a-number", "period": "20260623", "ddate": None},
    }
    valid = _make_usdm_feature(dm=3, object_id=1)
    fgb_bytes = _features_to_flatgeobuf([bad_dm, valid])
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
        assert len(gdf) == 1
        assert gdf["dm"].iloc[0] == 3
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Cache deduplication tests (FR-DC-4).
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit():
    pytest.importorskip("geopandas")
    patched_rt = _make_read_through_injector()

    fake_response = _make_usdm_response(dms=(0, 2))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_us_drought_monitor.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp

        result1 = fetch_us_drought_monitor(_AZ_BBOX)
        after_first = patched_rt.call_count["fetch_n"]
        result2 = fetch_us_drought_monitor(_AZ_BBOX)
        after_second = patched_rt.call_count["fetch_n"]

    assert after_first == 1, "Expected 1 fetch on cache miss"
    assert after_second == 1, "Expected no second fetch on cache hit"
    assert result1.layer_type == "vector"
    assert result2.layer_type == "vector"
    assert result1.uri == result2.uri


# ---------------------------------------------------------------------------
# LayerURI shape tests.
# ---------------------------------------------------------------------------


def test_layer_uri_shape():
    pytest.importorskip("geopandas")
    patched_rt = _make_read_through_injector()

    fake_response = _make_usdm_response(dms=(0, 1, 2))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_us_drought_monitor.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        result = fetch_us_drought_monitor(_AZ_BBOX)

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.style_preset == "us_drought_monitor"
    assert result.units == "dm_class"
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert "current" in result.layer_id


def test_layer_uri_shape_with_date():
    pytest.importorskip("geopandas")
    patched_rt = _make_read_through_injector()

    fake_response = _make_usdm_response(dms=(0, 1, 2, 3, 4))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_us_drought_monitor.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        result = fetch_us_drought_monitor(_AZ_BBOX, date="2021-08-17")

    assert "20210817" in result.layer_id
    # The archive endpoint must have been used.
    called_url = mock_client.get.call_args[0][0]
    assert called_url == USDM_ARCHIVE_URL


# ---------------------------------------------------------------------------
# Input validation integration tests (end-to-end, no S3 needed).
# ---------------------------------------------------------------------------


def test_invalid_bbox_raises_input_error():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="degenerate"):
        fetch_us_drought_monitor(bbox=(-114.0, 31.3, -114.0, 37.0))


def test_invalid_date_raises_input_error():
    with pytest.raises(US_DROUGHT_MONITORInputError, match="8 digits"):
        fetch_us_drought_monitor(bbox=_AZ_BBOX, date="last tuesday")


def test_extra_kwargs_absorbed():
    pytest.importorskip("geopandas")
    patched_rt = _make_read_through_injector()

    fake_response = _make_usdm_response(dms=(1,))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fake_response

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_us_drought_monitor.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        result = fetch_us_drought_monitor(
            bbox=_AZ_BBOX,
            severity="extreme",   # invented kwarg
            source="ndmc",        # invented kwarg
        )

    assert result.layer_type == "vector"


# ---------------------------------------------------------------------------
# Honest-empty integration test (no drought in bbox -> empty FGB, no raise).
# ---------------------------------------------------------------------------


def test_empty_bbox_returns_empty_layer_not_error():
    pytest.importorskip("geopandas")
    patched_rt = _make_read_through_injector()

    empty_response = {"type": "FeatureCollection", "features": []}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = empty_response

    with patch(
        "grace2_agent.tools.fetch_us_drought_monitor.httpx.Client"
    ) as mock_client_cls, patch(
        "grace2_agent.tools.fetch_us_drought_monitor.read_through",
        side_effect=patched_rt,
    ):
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        # An open-ocean bbox with no drought must NOT raise; it returns a
        # valid (empty) layer per the honest-empty contract.
        result = fetch_us_drought_monitor(bbox=(-40.0, 30.0, -39.0, 31.0))

    assert result.layer_type == "vector"
    assert result.uri is not None


# ---------------------------------------------------------------------------
# DM_LABELS sanity.
# ---------------------------------------------------------------------------


def test_dm_labels_cover_all_classes():
    assert set(DM_LABELS.keys()) == {0, 1, 2, 3, 4}
    assert "Exceptional" in DM_LABELS[4]
    assert "Abnormally" in DM_LABELS[0]


# ---------------------------------------------------------------------------
# Live smoke test (gated by GRACE2_TEST_LIVE_USDM=1).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_USDM, reason="GRACE2_TEST_LIVE_USDM not set")
def test_live_smoke_arizona():
    pytest.importorskip("geopandas")
    import geopandas as gpd

    fgb_bytes = _fetch_usdm_bytes(_AZ_BBOX, None)
    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path, engine="pyogrio")
    finally:
        os.unlink(tmp_path)

    assert len(gdf) >= 1, (
        f"Expected >=1 USDM feature for Arizona bbox={_AZ_BBOX}; got 0. "
        f"The bbox may be outside coverage or the service unreachable."
    )
    for dm_val in gdf["dm"]:
        assert 0 <= dm_val <= 4, f"dm={dm_val} out of expected [0, 4]"
    assert gdf["label"].notna().all()
    assert str(gdf.crs).startswith("EPSG:4326") or "4326" in str(gdf.crs)
    print(
        f"\nLive USDM smoke: bbox={_AZ_BBOX} -> {len(gdf)} features, "
        f"dm classes={sorted(set(gdf['dm']))}, {len(fgb_bytes):,} bytes FGB"
    )
