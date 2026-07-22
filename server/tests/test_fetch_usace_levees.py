"""Unit tests for ``fetch_usace_levees`` atomic tool (Wave 4.10 job A4).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata flags.
- bbox / layer validation raises typed input errors.
- Mocked: synthetic NLD response → FlatGeobuf written through cache.
- bbox=None (CONUS sweep) → no geometry param sent on the wire.
- bbox-narrowed call → geometry param in ArcGIS envelope format + inSR=4326.
- Pagination loop walks ``exceededTransferLimit=true`` pages until exhausted.
- Layer selection routes to the right FeatureServer sub-layer id.
- Network failure / 500 / error envelope / non-FeatureCollection → typed
  USACELeveeUpstreamError(retryable=True).
- estimate_payload_mb returns numeric MB; shape sanity-tested.
- Live (env TRID3NT_TEST_LIVE_USACE_LEVEES=1): real NLD fetch over the New
  Orleans bbox returns >=1 leveed_areas feature.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.hazard.fetch_usace_levees import (
    CONUS_BBOX,
    LAYER_TO_FS_ID,
    USACELeveeError,
    USACELeveeInputError,
    USACELeveeUpstreamError,
    _bbox_to_envelope,
    _build_nld_url,
    _fetch_nld_bytes,
    _fetch_nld_page,
    _fetch_nld_geojson,
    _geojson_to_fgb,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_usace_levees,
)


_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

_LIVE = os.environ.get("TRID3NT_TEST_LIVE_USACE_LEVEES") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors existing fetcher-test patterns).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
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
    from trid3nt_server.tools.cache import (
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
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


# ---------------------------------------------------------------------------
# Synthetic feature builders.
# ---------------------------------------------------------------------------


def _polygon_around(lon: float, lat: float, half: float = 0.05) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon - half, lat - half], [lon + half, lat - half],
            [lon + half, lat + half], [lon - half, lat + half],
            [lon - half, lat - half],
        ]],
    }


def _linestring_around(lon: float, lat: float, half: float = 0.05) -> dict:
    return {
        "type": "LineString",
        "coordinates": [
            [lon - half, lat - half],
            [lon + half, lat + half],
        ],
    }


def _make_leveed_area(object_id: int, lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "geometry": _polygon_around(lon, lat),
        "properties": {
            "OBJECTID": object_id,
            "SYSTEM_ID": f"sys-{object_id:06d}",
            "SYSTEM_NAME": f"Test Levee System {object_id}",
            "LEVEED_ID": f"lev-{object_id:06d}",
            "LEVEED_AREA_SQ_MI": 12.5 + object_id,
            "LEVEED_AREA_METHOD": "USACE Standard",
            "STATES": ["LA"],
            "COUNTIES": ["Orleans Parish"],
            "COMMUNITY_NAMES": ["New Orleans"],
            "DISTRICTS": ["MVN"],
            "FEMA_REGION_NAMES": ["Region 6"],
            "FEMA_ACCREDITATION_RATING": "Accredited",
            "OVERTOPPING_ACE": 0.01,
            "REHAB_PROGRAM_STATUS": "Active",
            "RESPONSIBLE_ORGANIZATION": "USACE",
            "SPONSORS": ["Sewerage & Water Board"],
            "SPONSOR_TYPE": "Local",
            "FLOOD_SOURCES": ["Mississippi River"],
            "WARNING_SYSTEM": "Yes",
        },
    }


def _make_system_route(object_id: int, lon: float, lat: float) -> dict:
    return {
        "type": "Feature",
        "geometry": _linestring_around(lon, lat),
        "properties": {
            "OBJECTID": object_id,
            "SYSTEM_ID": f"sys-{object_id:06d}",
            "SYSTEM_NAME": f"Route System {object_id}",
            "ROUTE_ID": f"route-{object_id:06d}",
            "SYSTEM_TYPE": "Levee",
            "SYSTEM_AUTHORIZATION": "Federal",
            "SYSTEM_IS_USACE": "Y",
            "AVERAGE_HEIGHT": 5.2,
            "MAX_HEIGHT": 8.0,
            "MIN_HEIGHT": 2.1,
            "STATES": ["LA"],
            "COUNTIES": ["Orleans Parish"],
            "DISTRICTS": ["MVN"],
            "FEMA_REGION_NAMES": ["Region 6"],
            "FEMA_ACCREDITATION_RATING": "Accredited",
            "OVERTOPPING_ACE": 0.01,
            "REHAB_PROGRAM_STATUS": "Active",
            "RESPONSIBLE_ORGANIZATION": "USACE",
            "SPONSORS": ["Sewerage & Water Board"],
            "FLOOD_SOURCES": ["Mississippi River"],
        },
    }


def _sample_nld_response(layer: str = "leveed_areas", n: int = 5) -> dict:
    builder = _make_leveed_area if layer == "leveed_areas" else _make_system_route
    feats = [builder(i, -90.0 + i * 0.05, 29.9 + i * 0.02) for i in range(n)]
    return {"type": "FeatureCollection", "features": feats}


# ---------------------------------------------------------------------------
# Registration test.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_usace_levees appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_usace_levees" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usace_levees"]
    assert entry.metadata.name == "fetch_usace_levees"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "usace_nld"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Layer / bbox / URL builder tests.
# ---------------------------------------------------------------------------


def test_layer_to_fs_id_mapping_complete():
    """LAYER_TO_FS_ID maps each documented logical name to a FeatureServer id."""
    assert LAYER_TO_FS_ID["leveed_areas"] == 16
    assert LAYER_TO_FS_ID["system_routes"] == 14
    assert LAYER_TO_FS_ID["embankments"] == 10


def test_bbox_to_envelope_format():
    env = _bbox_to_envelope((-90.3, 29.7, -89.7, 30.2))
    assert env == "-90.3,29.7,-89.7,30.2"


def test_build_url_with_bbox_includes_geometry_and_layer_id():
    url, params = _build_nld_url("leveed_areas", (-90.3, 29.7, -89.7, 30.2))
    # FeatureServer/16/query for leveed_areas.
    assert url.endswith("FeatureServer/16/query")
    assert params["f"] == "geojson"
    assert params["outFields"] == "*"
    assert params["outSR"] == "4326"
    assert params["inSR"] == "4326"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["geometry"] == "-90.3,29.7,-89.7,30.2"
    assert params["where"] == "1=1"
    assert params["resultRecordCount"] == "1000"
    assert params["resultOffset"] == "0"


def test_build_url_routes_per_layer():
    url_lines, _ = _build_nld_url("system_routes", (-90.3, 29.7, -89.7, 30.2))
    url_emb, _ = _build_nld_url("embankments", (-90.3, 29.7, -89.7, 30.2))
    assert url_lines.endswith("FeatureServer/14/query")
    assert url_emb.endswith("FeatureServer/10/query")


def test_build_url_without_bbox_omits_geometry():
    url, params = _build_nld_url("leveed_areas", None)
    assert "geometry" not in params
    assert "geometryType" not in params
    assert "inSR" not in params
    assert params["f"] == "geojson"


def test_validate_bbox_rejects_degenerate():
    with pytest.raises(USACELeveeInputError, match="degenerate"):
        _validate_bbox((-90.0, 30.0, -91.0, 29.0))
    with pytest.raises(USACELeveeInputError, match="lon out of"):
        _validate_bbox((-200.0, 30.0, -89.0, 31.0))
    with pytest.raises(USACELeveeInputError, match="non-finite"):
        _validate_bbox((float("nan"), 30.0, -89.0, 31.0))


def test_round_bbox_to_6dp_quantizes():
    q = _round_bbox_to_6dp((-90.123456789, 29.5, -89.1, 30.000001234))
    assert q == (-90.123457, 29.5, -89.1, 30.000001)


# ---------------------------------------------------------------------------
# Input-validation tests.
# ---------------------------------------------------------------------------


def test_invalid_layer_raises_input_error():
    with pytest.raises(USACELeveeInputError, match="layer="):
        fetch_usace_levees(bbox=(-90.3, 29.7, -89.7, 30.2), layer="bogus")  # type: ignore[arg-type]


def test_invalid_bbox_raises_input_error():
    with pytest.raises(USACELeveeInputError, match="degenerate"):
        fetch_usace_levees(bbox=(-90.0, 30.0, -91.0, 29.0))


def test_input_errors_are_not_retryable():
    """USACELeveeInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_usace_levees(bbox=(-90.3, 29.7, -89.7, 30.2), layer="bogus")  # type: ignore[arg-type]
    except USACELeveeInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected USACELeveeInputError")


# ---------------------------------------------------------------------------
# Mocked end-to-end: synthetic NLD response.
# ---------------------------------------------------------------------------


def test_synthetic_response_writes_fgb_with_polygons():
    """Mocked 5-feature NLD response → 5-polygon FlatGeobuf written through cache."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nld_response("leveed_areas", n=5)

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees._fetch_nld_geojson",
        return_value=fake_geojson,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_levees(
            bbox=(-90.3, 29.7, -89.7, 30.2),
            layer="leveed_areas",
        )

    assert result.uri.startswith("s3://")
    assert "usace_nld" in result.uri
    assert "static-30d" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert "USACE NLD" in result.name
    assert "Leveed Areas" in result.name
    assert len(fake_gcs.store) == 1

    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5
        assert "SYSTEM_ID" in gdf.columns
        assert "SYSTEM_NAME" in gdf.columns
        assert "FEMA_ACCREDITATION_RATING" in gdf.columns
        # Geometries survived as polygons.
        assert (gdf.geometry.geom_type == "Polygon").all()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_system_routes_layer_writes_lines():
    """system_routes layer produces a LineString FlatGeobuf."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nld_response("system_routes", n=3)

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees._fetch_nld_geojson",
        return_value=fake_geojson,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_levees(layer="system_routes")

    assert "System Routes" in result.name
    fgb_bytes = next(iter(fake_gcs.store.values()))

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 3
        assert "ROUTE_ID" in gdf.columns
        assert "AVERAGE_HEIGHT" in gdf.columns
        assert (gdf.geometry.geom_type == "LineString").all()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_global_sweep_omits_geometry_param():
    """bbox=None call routes through _fetch_nld_page without geometry on the wire."""
    captured_params: list[dict[str, str]] = []
    fake_gcs = FakeStorageClient()

    def fake_page(url, params):
        captured_params.append(dict(params))
        return {"type": "FeatureCollection", "features": []}

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees._fetch_nld_page",
        side_effect=fake_page,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r = fetch_usace_levees(bbox=None, layer="leveed_areas")

    assert len(captured_params) >= 1
    p = captured_params[0]
    assert "geometry" not in p
    assert "global" in r.layer_id
    assert "CONUS+AK+HI" in r.name


def test_bbox_narrowed_call_sends_geometry_param():
    captured_params: list[dict[str, str]] = []
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nld_response("leveed_areas", n=2)

    def fake_page(url, params):
        captured_params.append(dict(params))
        return fake_geojson

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees._fetch_nld_page",
        side_effect=fake_page,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_levees(
            bbox=(-90.3, 29.7, -89.7, 30.2),
            layer="leveed_areas",
        )

    p = captured_params[0]
    assert p["geometry"] == "-90.3,29.7,-89.7,30.2"
    assert p["geometryType"] == "esriGeometryEnvelope"
    assert p["inSR"] == "4326"
    assert "bbox" in result.name
    assert "global" not in result.layer_id


def test_pagination_walks_until_exhausted():
    """exceededTransferLimit=true on page 1 → _fetch_nld_page called twice."""
    page_calls = {"n": 0}

    def fake_page(url, params):
        page_calls["n"] += 1
        if page_calls["n"] == 1:
            return {
                "type": "FeatureCollection",
                "exceededTransferLimit": True,
                "features": [_make_leveed_area(i, -90.0, 29.9) for i in range(3)],
            }
        return {
            "type": "FeatureCollection",
            "features": [_make_leveed_area(99, -90.0, 29.9)],
        }

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees._fetch_nld_page",
        side_effect=fake_page,
    ):
        gj = _fetch_nld_geojson("leveed_areas", (-90.3, 29.7, -89.7, 30.2))

    assert page_calls["n"] == 2
    assert len(gj["features"]) == 4


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_500_raises_typed_upstream_error():
    class FakeResponse:
        status_code = 500
        text = "Internal Server Error"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.httpx.Client", FakeClient
    ):
        with pytest.raises(USACELeveeUpstreamError, match="500"):
            _fetch_nld_page(
                "https://services2.arcgis.com/.../FeatureServer/16/query",
                {"f": "geojson"},
            )


def test_network_failure_wraps_to_upstream_error():
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.httpx.Client", FakeClient
    ):
        with pytest.raises(USACELeveeUpstreamError, match="request failed"):
            _fetch_nld_page(
                "https://services2.arcgis.com/.../FeatureServer/16/query",
                {"f": "geojson"},
            )


def test_arcgis_error_envelope_in_200_body_raises():
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"error": {"code": 400, "message": "Invalid query"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.httpx.Client", FakeClient
    ):
        with pytest.raises(USACELeveeUpstreamError, match="error envelope"):
            _fetch_nld_page(
                "https://services2.arcgis.com/.../FeatureServer/16/query",
                {"f": "geojson"},
            )


def test_non_feature_collection_raises():
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"type": "Feature", "geometry": None}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.httpx.Client", FakeClient
    ):
        with pytest.raises(USACELeveeUpstreamError, match="FeatureCollection"):
            _fetch_nld_page(
                "https://services2.arcgis.com/.../FeatureServer/16/query",
                {"f": "geojson"},
            )


def test_upstream_error_is_retryable():
    """USACELeveeUpstreamError is retryable=True."""
    err = USACELeveeUpstreamError("test")
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Payload-estimate shape.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_float_for_global():
    """Global sweep estimate returns a positive float for all valid layers."""
    for layer in ["leveed_areas", "system_routes", "embankments"]:
        mb = estimate_payload_mb(layer=layer)
        assert isinstance(mb, float)
        assert mb > 0.0
    # embankments is the largest national footprint.
    assert estimate_payload_mb(layer="embankments") > estimate_payload_mb(layer="system_routes")


def test_estimate_payload_mb_scales_with_bbox():
    """A state-sized bbox produces a smaller estimate than the CONUS sweep."""
    state_mb = estimate_payload_mb(
        layer="leveed_areas",
        bbox=(-90.3, 29.7, -89.7, 30.2),
    )
    conus_mb = estimate_payload_mb(layer="leveed_areas")
    assert state_mb < conus_mb
    assert state_mb >= 0.0


def test_estimate_payload_mb_accepts_unknown_kwargs():
    """The estimator absorbs **args so the chat-warning gate passes call kwargs unchanged."""
    mb = estimate_payload_mb(
        layer="leveed_areas",
        bbox=(-90.3, 29.7, -89.7, 30.2),
        extra_invented_kwarg="ignored",
    )
    assert isinstance(mb, float)


# ---------------------------------------------------------------------------
# Cache-layer test.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_then_hit_skips():
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def patched_fetch_bytes(layer, bbox):
        fetch_count["n"] += 1
        return b"FAKE_LEVEES_FGB" + b"\x00" * 16

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees._fetch_nld_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_usace_levees.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_usace_levees(bbox=(-90.3, 29.7, -89.7, 30.2))
        r2 = fetch_usace_levees(bbox=(-90.3, 29.7, -89.7, 30.2))

    assert fetch_count["n"] == 1
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# Live smoke (gated on env flag).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE, reason="TRID3NT_TEST_LIVE_USACE_LEVEES not set")
def test_live_new_orleans_leveed_areas_returns_features():
    """Live: NOLA bbox over leveed_areas returns >=1 feature."""
    gj = _fetch_nld_geojson(
        "leveed_areas",
        (-90.3, 29.7, -89.7, 30.2),
    )
    assert gj["type"] == "FeatureCollection"
    assert len(gj["features"]) >= 1
    # Spot-check a property survived the fetch.
    props = gj["features"][0].get("properties") or {}
    assert "SYSTEM_ID" in props or "OBJECTID" in props
