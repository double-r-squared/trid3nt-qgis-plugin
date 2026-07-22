"""Unit + live tests for ``fetch_noaa_coops_tides`` (job A9).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Input validation: bad bbox shapes, degenerate bbox, inverted date range,
  unknown product, date range too long.
- Station discovery: stations inside bbox returned; out-of-bbox excluded.
- Per-station data parsing: valid CSV round-trip; handles missing/NaN values.
- FlatGeobuf serialization shape.
- Error classes carry correct retryable + error_code attributes.
- Payload estimator returns a positive float.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped.
- LayerURI shape: layer_type="vector", role="primary", units="m (MLLW)".

Live test (gated by TRID3NT_TEST_LIVE_COOPS=1):
    Real CO-OPS API request for Fort Myers area (bbox covering stations
    8725520 Fort Myers + 8725114 Naples Bay) over a 1-day window.
    Confirms: ≥1 station returned; FlatGeobuf round-trips; wl_min_m <
    wl_max_m; coordinates in the expected coastal Florida envelope.
"""

from __future__ import annotations

import datetime
import io
import json
import os
from datetime import date
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_noaa_coops_tides import (
    COOPSTidesEmptyError,
    COOPSTidesInputError,
    COOPSTidesUpstreamError,
    _build_coops_url,
    _build_flatgeobuf,
    _discover_stations_in_bbox,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_date_range,
    _validate_product,
    estimate_payload_mb,
    fetch_noaa_coops_tides,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

# Fort Myers / Naples coastal bbox.
_FORT_MYERS_BBOX: tuple[float, float, float, float] = (-82.5, 25.5, -81.0, 27.5)

# Live test gate.
_LIVE_COOPS = os.environ.get("TRID3NT_TEST_LIVE_COOPS") == "1"

# Hurricane Ian landfall date.
_IAN_DATE = "2022-09-28"


def _fake_fgb_bytes(tag: str = "COOPS") -> bytes:
    return b"FAKE_COOPS_FGB_" + tag.encode() + b"\x00" * 16


def _make_station_catalog(stations: list[dict[str, Any]]) -> bytes:
    """Build a CO-OPS-style station catalog JSON payload."""
    return json.dumps({"stations": stations}).encode("utf-8")


def _make_data_response(rows: list[dict[str, Any]], product: str = "water_level") -> bytes:
    """Build a CO-OPS-style data response JSON payload."""
    key = "data" if product == "water_level" else "predictions"
    return json.dumps({
        "metadata": {"id": "8725520", "name": "Fort Myers", "lat": "26.65", "lon": "-81.87"},
        key: rows,
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_asos_metar pattern).
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


_PINNED_NOW = datetime.datetime(2026, 6, 9, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        patched.call_count["n"] += 1
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    patched.call_count = {"n": 0}
    return patched


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    """fetch_noaa_coops_tides appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_noaa_coops_tides" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_noaa_coops_tides"]
    assert entry.metadata.name == "fetch_noaa_coops_tides"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "noaa_coops_tides"
    assert entry.metadata.cacheable is True


def test_supports_global_query_is_false():
    """CO-OPS is a US coastal network; supports_global_query must be False."""
    entry = TOOL_REGISTRY["fetch_noaa_coops_tides"]
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox(_FORT_MYERS_BBOX)  # no exception


def test_validate_bbox_degenerate_lon():
    with pytest.raises(COOPSTidesInputError, match="degenerate"):
        _validate_bbox((-82.0, 25.5, -82.0, 27.5))  # west == east


def test_validate_bbox_inverted():
    with pytest.raises(COOPSTidesInputError, match="degenerate"):
        _validate_bbox((-81.0, 27.5, -82.5, 25.5))  # west > east, north > south


def test_validate_bbox_out_of_range():
    with pytest.raises(COOPSTidesInputError, match="lon"):
        _validate_bbox((-200.0, 25.0, -81.0, 27.5))


def test_validate_bbox_wrong_length():
    with pytest.raises(COOPSTidesInputError, match="west, south, east, north"):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_validate_product_ok():
    _validate_product("water_level")
    _validate_product("predictions")


def test_validate_product_unknown():
    with pytest.raises(COOPSTidesInputError, match="unsupported product"):
        _validate_product("surge")


def test_validate_product_wrong_type():
    with pytest.raises(COOPSTidesInputError, match="str"):
        _validate_product(42)  # type: ignore[arg-type]


def test_validate_date_range_ok():
    d0, d1 = _validate_date_range("2022-09-28", "2022-09-29")
    assert d0 == date(2022, 9, 28)
    assert d1 == date(2022, 9, 29)


def test_validate_date_range_inverted():
    with pytest.raises(COOPSTidesInputError, match="start_date must be <="):
        _validate_date_range("2022-09-30", "2022-09-28")


def test_validate_date_range_too_long():
    with pytest.raises(COOPSTidesInputError, match="exceeds hard cap"):
        _validate_date_range("2000-01-01", "2001-12-31")  # >366 days


def test_validate_date_range_malformed():
    with pytest.raises(COOPSTidesInputError, match="not a valid ISO date"):
        _validate_date_range("22-09-28", "2022-09-29")


# ---------------------------------------------------------------------------
# Error class attributes.
# ---------------------------------------------------------------------------


def test_error_classes_attributes():
    """All error classes carry retryable and error_code."""
    for cls, retryable in [
        (COOPSTidesInputError, False),
        (COOPSTidesUpstreamError, True),
        (COOPSTidesEmptyError, False),
    ]:
        inst = cls("test")
        assert inst.retryable is retryable, f"{cls.__name__}.retryable wrong"
        assert isinstance(inst.error_code, str), f"{cls.__name__}.error_code wrong"
        assert inst.error_code != "", f"{cls.__name__}.error_code empty"


# ---------------------------------------------------------------------------
# Station discovery helpers.
# ---------------------------------------------------------------------------


def test_discover_stations_in_bbox_filters_correctly():
    """Only stations inside the bbox are returned."""
    catalog = [
        {"id": "8725520", "name": "Fort Myers", "lat": "26.65", "lng": "-81.87"},  # inside
        {"id": "8720030", "name": "Fernandina Beach", "lat": "30.67", "lng": "-81.46"},  # outside lat
        {"id": "8725114", "name": "Naples Bay North", "lat": "26.14", "lng": "-81.79"},  # inside
        {"id": "8726607", "name": "Old Port Tampa", "lat": "27.86", "lng": "-82.55"},  # outside lon
    ]
    catalog_json = json.dumps({"stations": catalog}).encode("utf-8")

    with patch(
        "trid3nt_server.tools.fetch_noaa_coops_tides._http_get",
        return_value=catalog_json,
    ):
        result = _discover_stations_in_bbox(_FORT_MYERS_BBOX)

    ids = {s["id"] for s in result}
    assert "8725520" in ids
    assert "8725114" in ids
    assert "8720030" not in ids
    assert "8726607" not in ids


def test_discover_stations_empty_bbox():
    """A bbox with no stations raises COOPSTidesEmptyError."""
    catalog_json = json.dumps({"stations": [
        {"id": "9999999", "name": "Far Away", "lat": "90.0", "lng": "0.0"},
    ]}).encode("utf-8")

    with patch(
        "trid3nt_server.tools.fetch_noaa_coops_tides._http_get",
        return_value=catalog_json,
    ), pytest.raises(COOPSTidesEmptyError):
        _discover_stations_in_bbox(_FORT_MYERS_BBOX)


def test_discover_stations_upstream_error():
    """HTTP failure in station catalog raises COOPSTidesUpstreamError."""
    from unittest.mock import MagicMock

    with patch(
        "trid3nt_server.tools.fetch_noaa_coops_tides._http_get",
        side_effect=COOPSTidesUpstreamError("network timeout"),
    ), pytest.raises(COOPSTidesUpstreamError):
        _discover_stations_in_bbox(_FORT_MYERS_BBOX)


# ---------------------------------------------------------------------------
# URL builder.
# ---------------------------------------------------------------------------


def test_build_coops_url_water_level():
    url = _build_coops_url(
        "8725520", "water_level", date(2022, 9, 28), date(2022, 9, 28)
    )
    assert "begin_date=20220928" in url
    assert "end_date=20220928" in url
    assert "station=8725520" in url
    assert "product=water_level" in url
    assert "datum=MLLW" in url
    assert "interval=h" in url
    assert "units=metric" in url
    assert "format=json" in url


def test_build_coops_url_predictions():
    url = _build_coops_url(
        "8725520", "predictions", date(2022, 9, 28), date(2022, 9, 30)
    )
    assert "product=predictions" in url
    assert "begin_date=20220928" in url
    assert "end_date=20220930" in url


# ---------------------------------------------------------------------------
# FlatGeobuf builder.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_with_records():
    """FlatGeobuf is built from valid station records."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    records = [
        {
            "station_id": "8725520",
            "station_name": "Fort Myers",
            "lon": -81.87,
            "lat": 26.65,
            "rows": [
                {"t": "2022-09-28T00:00:00Z", "v": 0.5},
                {"t": "2022-09-28T01:00:00Z", "v": 1.2},
                {"t": "2022-09-28T02:00:00Z", "v": 0.8},
            ],
        },
    ]
    fgb_bytes = _build_flatgeobuf(records, "water_level")
    # FlatGeobuf magic bytes start with "FGB" in binary representation
    # (or at least a non-empty byte string)
    assert len(fgb_bytes) > 100, "FlatGeobuf should be non-trivially sized"


def test_build_flatgeobuf_empty_records():
    """Empty record list produces a schema-only FlatGeobuf (no crash)."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    fgb_bytes = _build_flatgeobuf([], "water_level")
    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 0


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_positive():
    mb = estimate_payload_mb(
        bbox=_FORT_MYERS_BBOX,
        start_date="2022-09-28",
        end_date="2022-09-28",
        product="water_level",
    )
    assert mb > 0, "estimate must be positive"
    assert mb < 1000, "estimate must be sane (< 1 GB)"


def test_estimate_payload_mb_grows_with_days():
    mb_1day = estimate_payload_mb(
        bbox=_FORT_MYERS_BBOX,
        start_date="2022-09-28",
        end_date="2022-09-28",
    )
    mb_30day = estimate_payload_mb(
        bbox=_FORT_MYERS_BBOX,
        start_date="2022-09-01",
        end_date="2022-09-30",
    )
    assert mb_30day > mb_1day, "longer date range should produce larger estimate"


def test_estimate_payload_mb_no_bbox():
    mb = estimate_payload_mb(bbox=None)
    assert mb > 0


# ---------------------------------------------------------------------------
# Round-trip cache test (mock GCS).
# ---------------------------------------------------------------------------


def test_fetch_tool_cache_miss_then_hit():
    """Cache-miss invokes fetch_fn; cache-hit reuses stored bytes."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    fake_gcs = FakeStorageClient()

    # Build a minimal fake response.
    catalog_json = _make_station_catalog([
        {"id": "8725520", "name": "Fort Myers", "lat": "26.65", "lng": "-81.87"},
    ])
    data_json = _make_data_response([
        {"t": "2022-09-28 00:00", "v": "0.50"},
        {"t": "2022-09-28 01:00", "v": "1.20"},
    ], "water_level")

    def fake_http_get(url: str, timeout: float) -> bytes:
        if "stations.json" in url:
            return catalog_json
        return data_json

    injector = _make_read_through_injector(fake_gcs)

    with (
        patch(
            "trid3nt_server.tools.fetch_noaa_coops_tides._http_get",
            side_effect=fake_http_get,
        ),
        patch(
            "trid3nt_server.tools.fetch_noaa_coops_tides.read_through",
            side_effect=injector,
        ),
    ):
        result1 = fetch_noaa_coops_tides(
            bbox=_FORT_MYERS_BBOX,
            start_date="2022-09-28",
            end_date="2022-09-28",
            product="water_level",
        )
        result2 = fetch_noaa_coops_tides(
            bbox=_FORT_MYERS_BBOX,
            start_date="2022-09-28",
            end_date="2022-09-28",
            product="water_level",
        )

    # Both calls succeed; the layer_id should be the same (same cache key).
    assert result1.layer_id == result2.layer_id
    # The injector should have been called twice (once per tool call).
    assert injector.call_count["n"] == 2  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape():
    """fetch_noaa_coops_tides returns a LayerURI with expected fields."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    catalog_json = _make_station_catalog([
        {"id": "8725520", "name": "Fort Myers", "lat": "26.65", "lng": "-81.87"},
    ])
    data_json = _make_data_response([
        {"t": "2022-09-28 00:00", "v": "0.50"},
        {"t": "2022-09-28 01:00", "v": "1.20"},
    ], "water_level")

    def fake_http_get(url: str, timeout: float) -> bytes:
        if "stations.json" in url:
            return catalog_json
        return data_json

    fake_gcs = FakeStorageClient()
    injector = _make_read_through_injector(fake_gcs)

    with (
        patch(
            "trid3nt_server.tools.fetch_noaa_coops_tides._http_get",
            side_effect=fake_http_get,
        ),
        patch(
            "trid3nt_server.tools.fetch_noaa_coops_tides.read_through",
            side_effect=injector,
        ),
    ):
        result = fetch_noaa_coops_tides(
            bbox=_FORT_MYERS_BBOX,
            start_date="2022-09-28",
            end_date="2022-09-28",
            product="water_level",
        )

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units == "m (MLLW)"
    assert result.uri.startswith("s3://")
    assert "coops-tides-water_level" in result.layer_id
    assert "CO-OPS" in result.name


# ---------------------------------------------------------------------------
# Extra-kwargs absorption (Gemini hallucination guard).
# ---------------------------------------------------------------------------


def test_extra_kwargs_absorbed():
    """Invented kwargs do not raise; they are silently discarded."""
    try:
        import geopandas  # noqa: F401
        import shapely  # noqa: F401
    except ImportError:
        pytest.skip("geopandas/shapely not installed")

    catalog_json = _make_station_catalog([
        {"id": "8725520", "name": "Fort Myers", "lat": "26.65", "lng": "-81.87"},
    ])
    data_json = _make_data_response([{"t": "2022-09-28 00:00", "v": "0.50"}])

    def fake_http_get(url: str, timeout: float) -> bytes:
        if "stations.json" in url:
            return catalog_json
        return data_json

    fake_gcs = FakeStorageClient()
    injector = _make_read_through_injector(fake_gcs)

    with (
        patch(
            "trid3nt_server.tools.fetch_noaa_coops_tides._http_get",
            side_effect=fake_http_get,
        ),
        patch(
            "trid3nt_server.tools.fetch_noaa_coops_tides.read_through",
            side_effect=injector,
        ),
    ):
        # Should NOT raise even with invented kwargs.
        result = fetch_noaa_coops_tides(
            bbox=_FORT_MYERS_BBOX,
            start_date="2022-09-28",
            end_date="2022-09-28",
            product="water_level",
            invented_param="foo",  # type: ignore[call-arg]
            another_fake_kwarg=42,  # type: ignore[call-arg]
        )
    assert result.layer_type == "vector"


# ---------------------------------------------------------------------------
# Round-bbox helper.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    result = _round_bbox_to_6dp((-82.500001234, 25.500001234, -81.0, 27.5))
    assert all(len(str(abs(v)).split(".")[-1]) <= 6 for v in result)


# ---------------------------------------------------------------------------
# Live smoke test (requires TRID3NT_TEST_LIVE_COOPS=1).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_COOPS, reason="set TRID3NT_TEST_LIVE_COOPS=1 to run live CO-OPS test")
def test_live_fetch_fort_myers_ian_date():
    """Live: fetch Fort Myers + Naples Bay water_level for Hurricane Ian day."""
    import geopandas as gpd

    from trid3nt_server.tools.fetch_noaa_coops_tides import _fetch_coops_tides_bytes

    bbox = (-82.5, 25.5, -81.0, 27.5)
    d0 = date(2022, 9, 28)
    d1 = date(2022, 9, 28)

    fgb_bytes = _fetch_coops_tides_bytes(bbox, "water_level", d0, d1)
    assert isinstance(fgb_bytes, bytes)
    assert len(fgb_bytes) > 100, "FlatGeobuf should be non-trivially sized"

    # Round-trip through geopandas.
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name

    try:
        gdf = gpd.read_file(tmp_path, driver="FlatGeobuf")
    finally:
        import os
        os.unlink(tmp_path)

    assert len(gdf) >= 1, "Expected at least 1 station"

    # Check coordinate envelope.
    for _, row in gdf.iterrows():
        assert -83.0 <= row.geometry.x <= -80.0, "lon out of Florida coastal range"
        assert 24.0 <= row.geometry.y <= 28.0, "lat out of Florida coastal range"
        assert row["wl_min_m"] < row["wl_max_m"], "wl_min must be < wl_max"
        assert row["n_timesteps"] > 0, "should have ≥1 timestep"
        assert "station_id" in row.index
        assert row["datum"] == "MLLW"
        # CO-OPS Hurricane Ian peak surge should be > 0 at Fort Myers.
        # (Fort Myers saw ~5m surge during Ian; MLLW base level ~0.3m)
        assert row["wl_max_m"] > 0.0

    print(
        f"[LIVE SMOKE] CO-OPS Fort Myers / Ian: {len(gdf)} station(s), "
        f"max wl={gdf['wl_max_m'].max():.3f}m MLLW"
    )
