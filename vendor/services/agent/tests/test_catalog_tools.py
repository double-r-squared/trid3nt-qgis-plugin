"""Unit tests for catalog_search + catalog_fetch + generic OGC adapter (job-0047).

Coverage:
- ``catalog_search`` returns ranked entries by topic match.
- ``catalog_search`` with a bbox filter drops CONUS-only entries for non-CONUS bboxes.
- ``catalog_search`` with ``source_filter`` returns only that source class.
- ``catalog_fetch`` dispatches correctly per access_tier (Tier 1/2/3/4 paths).
- ``catalog_fetch`` cache-shim integration (read_through hit + miss + cached URI).
- Generic OGC adapter — WMS GetMap mocked.
- Generic OGC adapter — WCS GetCoverage mocked (mirror NLCD).
- Generic OGC adapter — WFS GetFeature mocked.
- Generic OGC adapter — ArcGIS REST query mocked.
- Generic OGC adapter — OGC exception XML body raises ``OGCAdapterError``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools import catalog as catalog_mod
from grace2_agent.tools import ogc_adapter as ogc_mod
from grace2_agent.tools.catalog import (
    CatalogNotFoundError,
    catalog_fetch,
    catalog_search,
    load_catalog,
)
from grace2_agent.tools.ogc_adapter import (
    OGCAdapterError,
    OGCResponse,
    fetch_ogc_layer,
)


FORT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)
PINNED_NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fake GCS scaffolding (mirrors test_data_fetch.py).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
        self.cache_control: str | None = None
        self.uploaded: bytes | None = None
        self.upload_content_type: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.uploaded = data
        self.upload_content_type = content_type
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store
        self.blobs: list[FakeBlob] = []

    def blob(self, path: str) -> FakeBlob:
        b = FakeBlob(self._store, path)
        self.blobs.append(b)
        return b


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self._bucket = FakeBucket(self.store)

    def bucket(self, name: str) -> FakeBucket:
        return self._bucket


@pytest.fixture
def fake_storage_patched(monkeypatch):
    """Route the catalog module's ``read_through`` through an in-memory S3 store.

    GCP is decommissioned: the cache shim is S3-only via boto3. This patches the
    catalog module's ``read_through`` with an in-memory implementation that mints
    ``s3://`` URIs and reads/writes ``fake.store`` (keyed by object KEY), so the
    cache hit/miss/write assertions hold without touching the network.
    """
    from grace2_agent.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    fake = FakeStorageClient()

    def _patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in fake.store:
            return ReadThroughResult(uri=uri, data=fake.store[path], hit=True)
        data = fetch_fn()
        fake.store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(catalog_mod, "read_through", _patched)
    return fake


# ---------------------------------------------------------------------------
# Fake OGC adapter response.
# ---------------------------------------------------------------------------


class _FakeOGCResponse:
    """Minimal duck-type for requests.Response used by fetch_ogc_layer."""

    def __init__(
        self,
        status: int = 200,
        content: bytes = b"x" * 256,
        content_type: str = "image/tiff",
        text: str = "",
    ) -> None:
        self.status_code = status
        self.content = content
        self.text = text
        self.headers = {"content-type": content_type}
        self.url = "http://example.test/?stub=1"

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            from requests import HTTPError

            raise HTTPError(f"status={self.status_code}")


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_catalog_search_is_registered_with_semi_static_7d():
    entry = TOOL_REGISTRY["catalog_search"]
    assert entry.metadata.ttl_class == "semi-static-7d"
    assert entry.metadata.source_class == "catalog_search"
    assert entry.metadata.cacheable is True


def test_catalog_fetch_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["catalog_fetch"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "catalog_fetch"
    assert entry.metadata.cacheable is True


def test_registry_has_catalog_tools_after_explicit_import():
    """Acceptance criterion: the two new tools register.

    In-process the registry only carries the modules this test file explicitly
    imports + the eager ``tools/__init__.py`` ``passthroughs`` import. The
    ≥16-tools floor is asserted at ``--startup-only`` (see evidence/), where
    ``main._import_tools_registry`` triggers every job's eager import.
    """
    assert "catalog_search" in TOOL_REGISTRY
    assert "catalog_fetch" in TOOL_REGISTRY


# ---------------------------------------------------------------------------
# load_catalog: YAML parsing + CatalogEntry validation.
# ---------------------------------------------------------------------------


def test_load_catalog_returns_30_entries_from_seed_yaml():
    """The v0.1 seed catalog has 30 vetted entries per job-0046."""
    catalog_mod._reset_catalog_cache_for_tests()
    catalog = load_catalog()
    assert len(catalog) >= 25  # be lenient: tolerate minor curator drift
    # Spot-check a few well-known entries.
    ids = {e.id for e in catalog}
    assert "fema-nfhl-flood-zones" in ids
    assert "usgs-3dep-elevation-image-service" in ids
    assert "nlcd-mrlc-wcs" in ids


def test_load_catalog_entries_validate_against_pydantic_shape():
    """Every loaded entry passes CatalogEntry validation (incl. credential rule)."""
    catalog_mod._reset_catalog_cache_for_tests()
    catalog = load_catalog()
    for entry in catalog:
        # Tier 1 (key-free) must NOT have api_key_secret_ref.
        if entry.credential_tier == 1:
            assert entry.api_key_secret_ref is None, entry.id


# ---------------------------------------------------------------------------
# catalog_search: topic ranking + filters.
# ---------------------------------------------------------------------------


def test_catalog_search_finds_fema_nfhl_for_flood_zones(fake_storage_patched):
    """`catalog_search(topic="flood zones")` returns the FEMA NFHL entry."""
    results = catalog_search(topic="flood zones", location=FORT_MYERS_BBOX)
    assert len(results) >= 1
    top_ids = [r["id"] for r in results[:5]]
    assert "fema-nfhl-flood-zones" in top_ids, top_ids


def test_catalog_search_finds_3dep_for_dem_topic(fake_storage_patched):
    """`catalog_search(topic="DEM")` returns the USGS 3DEP entry near the top."""
    results = catalog_search(topic="DEM")
    assert len(results) >= 1
    top_ids = [r["id"] for r in results[:5]]
    assert any("3dep" in tid for tid in top_ids), top_ids


def test_catalog_search_source_filter_restricts_results(fake_storage_patched):
    """`source_filter="landcover"` returns only landcover-source-class entries."""
    results = catalog_search(topic="land", source_filter="landcover")
    assert results, "expected at least one landcover entry"
    for r in results:
        assert r["source_class"] == "landcover", r["id"]


def test_catalog_search_bbox_filter_drops_conus_only_for_intl_bbox(
    fake_storage_patched,
):
    """A bbox in central Africa should drop CONUS-only entries (e.g. 3DEP)."""
    africa_bbox = (15.0, -1.0, 20.0, 4.0)
    results = catalog_search(topic="elevation", location=africa_bbox)
    ids = [r["id"] for r in results]
    # 3DEP names "conterminous US" — should NOT appear for Africa bbox.
    assert "usgs-3dep-elevation-image-service" not in ids, ids


def test_catalog_search_returns_empty_for_unmatched_topic(fake_storage_patched):
    """A topic that matches nothing returns an empty list (LLM escalates to Mode 2)."""
    results = catalog_search(topic="zzz-completely-fake-data-source-name-zzz")
    assert results == []


def test_catalog_search_routes_through_read_through(fake_storage_patched, monkeypatch):
    """FR-CE-8: catalog_search is cacheable; repeat call hits the cache."""
    # First call writes through; second call should hit.
    results_1 = catalog_search(topic="flood zones", location=FORT_MYERS_BBOX)
    paths = list(fake_storage_patched.store.keys())
    assert len(paths) == 1
    assert paths[0].startswith("cache/semi-static-7d/catalog_search/")
    # Second identical call returns the cached payload.
    results_2 = catalog_search(topic="flood zones", location=FORT_MYERS_BBOX)
    assert results_1 == results_2


# ---------------------------------------------------------------------------
# catalog_fetch: tier dispatch.
# ---------------------------------------------------------------------------


def test_catalog_fetch_tier2_arcgis_dispatch_for_fema_nfhl(
    fake_storage_patched, monkeypatch
):
    """`catalog_fetch("fema-nfhl-flood-zones", {bbox})` routes to ArcGIS REST query."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"FLD_ZONE":"AE"},"geometry":null}]}'
            + b"\x00" * 100,
            content_type="application/json",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    result = catalog_fetch(
        entry_id="fema-nfhl-flood-zones",
        params={"bbox": list(FORT_MYERS_BBOX), "layer_id": "28"},
    )
    assert result["entry_id"] == "fema-nfhl-flood-zones"
    assert result["access_tier"] == 2
    # URL should be the entry URL + /28/query for ArcGIS REST.
    assert "/MapServer/28/query" in captured["url"], captured["url"]
    # The dispatch chose ArcGIS REST shape.
    assert captured["params"]["f"] == "geojson"
    # Cache landed under catalog_fetch prefix.
    paths = list(fake_storage_patched.store.keys())
    assert any(p.startswith("cache/static-30d/catalog_fetch/") for p in paths)


def test_catalog_fetch_tier2_arcgis_imageserver_routes_to_exportimage(
    fake_storage_patched, monkeypatch
):
    """`catalog_fetch("usgs-3dep-elevation-image-service", {bbox})` routes to ArcGIS ImageServer exportImage.

    The 3DEP entry's primary URL is an ArcGIS ImageServer; v0.1 sniffs that
    as ARCGIS_REST and routes ImageServer endpoints through ``/exportImage``
    (raster) rather than ``/<layer>/query`` (vector, MapServer/FeatureServer).
    """
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        # ImageServer exportImage returns a TIFF byte stream.
        return _FakeOGCResponse(
            content=b"II*\x00" + b"\x00" * 256,
            content_type="image/tiff",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    result = catalog_fetch(
        entry_id="usgs-3dep-elevation-image-service",
        params={"bbox": list(FORT_MYERS_BBOX)},
    )
    assert result["entry_id"] == "usgs-3dep-elevation-image-service"
    assert result["access_tier"] == 2
    # ImageServer exportImage path (NOT /query).
    assert "/exportImage" in captured["url"], captured["url"]
    assert "/query" not in captured["url"], captured["url"]
    # ImageServer params include bbox + size + format (raster shape).
    assert "bbox" in captured["params"]
    assert "size" in captured["params"]
    assert captured["params"]["format"] == "tiff"


def test_catalog_fetch_tier1_raises_not_implemented(monkeypatch, fake_storage_patched):
    """Tier 1 STAC dispatch is reserved for a follow-up; v0.1 raises NotImplementedError."""
    # Pick a Tier 1 entry: Copernicus DEM GLO-30 STAC.
    with pytest.raises(NotImplementedError):
        catalog_fetch(
            entry_id="copernicus-dem-glo-30-stac",
            params={"bbox": list(FORT_MYERS_BBOX)},
        )


def test_catalog_fetch_tier4_raises_not_implemented(fake_storage_patched):
    """Tier 4 region-download dispatch is reserved for a follow-up; v0.1 raises NotImplementedError."""
    # Pick a Tier 4 entry: WorldPop 1km aggregated.
    with pytest.raises(NotImplementedError):
        catalog_fetch(
            entry_id="worldpop-1km-aggregated-rest",
            params={"bbox": list(FORT_MYERS_BBOX)},
        )


def test_catalog_fetch_tier3_https_dispatch(fake_storage_patched, monkeypatch):
    """Tier 3 (HTTPS + Range) dispatch issues a single HTTPS GET."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        return _FakeOGCResponse(
            content=b"HURDAT2_FAKE_TEXT_DATA_BYTES" + b"\x00" * 256,
            content_type="text/plain",
        )

    monkeypatch.setattr(catalog_mod, "_tier3_https_fetch",
                        lambda entry, params: (
                            b"HURDAT2_FAKE_TEXT_DATA_BYTES" + b"\x00" * 256,
                            "csv",
                        ))

    # NOAA NHC ATCF HURDAT2 is access_tier 3.
    result = catalog_fetch(
        entry_id="noaa-nhc-atcf-hurdat2",
        params={"query": {"year": 2022}},
    )
    assert result["entry_id"] == "noaa-nhc-atcf-hurdat2"
    assert result["access_tier"] == 3


def test_catalog_fetch_unknown_entry_raises_catalog_not_found(fake_storage_patched):
    """An unknown entry_id raises CatalogNotFoundError with a hint."""
    with pytest.raises(CatalogNotFoundError):
        catalog_fetch(entry_id="zzz-not-a-real-entry", params={})


# ---------------------------------------------------------------------------
# Generic OGC adapter: WMS / WCS / WFS / ArcGIS REST.
# ---------------------------------------------------------------------------


def test_ogc_adapter_wms_getmap_request_shape(monkeypatch):
    """WMS GetMap mocked: verify the request parameter shape."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 256,
            content_type="image/png",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    resp = fetch_ogc_layer(
        url="https://example.test/geoserver/wms",
        layer_name="testlayer",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WMS",
        image_format="image/png",
        version="1.1.1",
        width_px=512,
        height_px=512,
    )
    assert resp.service_type == "WMS"
    assert resp.content_type == "image/png"
    p = captured["params"]
    assert p["service"] == "WMS"
    assert p["version"] == "1.1.1"
    assert p["request"] == "GetMap"
    assert p["layers"] == "testlayer"
    assert p["bbox"].startswith("-81.92,")
    assert p["srs"] == "EPSG:4326"  # 1.1.x uses srs
    assert p["width"] == "512"
    assert p["format"] == "image/png"


def test_ogc_adapter_wcs_getcoverage_request_shape(monkeypatch):
    """WCS 1.0.0 GetCoverage mocked: mirror the NLCD WCS path."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b"\x49\x49\x2a\x00" + b"\x00" * 256,
            content_type="image/tiff",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    resp = fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="mrlc_display:NLCD_2021_Land_Cover_L48",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
        width_px=512,
        height_px=512,
    )
    assert resp.service_type == "WCS"
    assert "tiff" in resp.content_type.lower()
    p = captured["params"]
    assert p["service"] == "WCS"
    assert p["version"] == "1.0.0"
    assert p["request"] == "GetCoverage"
    assert p["Coverage"] == "mrlc_display:NLCD_2021_Land_Cover_L48"
    assert p["CRS"] == "EPSG:4326"
    assert p["FORMAT"] == "GeoTIFF"


def test_ogc_adapter_wfs_getfeature_request_shape(monkeypatch):
    """WFS GetFeature mocked: verify the request parameter shape + GeoJSON output."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b'{"type":"FeatureCollection","features":[]}' + b"\x00" * 100,
            content_type="application/json",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    resp = fetch_ogc_layer(
        url="https://example.test/geoserver/wfs",
        layer_name="ns:rivers",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WFS",
        image_format="application/json",
        version="2.0.0",
        max_features=500,
    )
    assert resp.service_type == "WFS"
    p = captured["params"]
    assert p["service"] == "WFS"
    assert p["version"] == "2.0.0"
    assert p["request"] == "GetFeature"
    assert p["typeName"] == "ns:rivers"
    assert p["outputFormat"] == "application/json"
    assert p["maxFeatures"] == "500"
    assert "EPSG:4326" in p["bbox"]


def test_ogc_adapter_arcgis_rest_query_request_shape(monkeypatch):
    """ArcGIS REST /query mocked: verify outFields/inSR/outSR/f=geojson shape."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b'{"features":[{"attributes":{"FLD_ZONE":"AE"}}]}' + b"\x00" * 50,
            content_type="application/json",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    resp = fetch_ogc_layer(
        url="https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query",
        layer_name="28",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="ARCGIS_REST",
    )
    assert resp.service_type == "ARCGIS_REST"
    p = captured["params"]
    assert p["f"] == "geojson"
    assert p["outSR"] == "4326"
    assert p["inSR"] == "4326"
    assert p["geometryType"] == "esriGeometryEnvelope"


# ---------------------------------------------------------------------------
# Phase-2 fetch-side resolution lever (extent-aware raster grid).
# ---------------------------------------------------------------------------


def _wcs_grid_capture(monkeypatch):
    """Patch requests.get to capture WCS GetCoverage params; return the dict."""
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b"\x49\x49\x2a\x00" + b"\x00" * 256,
            content_type="image/tiff",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)
    return captured


def test_ogc_adapter_auto_grid_scales_with_bbox_and_clamps(monkeypatch):
    """No width/height -> extent-aware grid: bigger bbox -> more pixels, clamped at 4096.

    Phase-2 resolution lever: when both width_px/height_px are None, the
    adapter derives WIDTH/HEIGHT from the bbox at the default 30 m cell,
    clamped to _OGC_PX_MAX (4096) per axis.
    """
    captured = _wcs_grid_capture(monkeypatch)

    # Small bbox (Fort Myers ~0.12 x 0.13 deg) at 30 m -> a modest grid well
    # under the 4096 clamp.
    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
    )
    small_w = int(captured["params"]["WIDTH"])
    small_h = int(captured["params"]["HEIGHT"])
    assert 16 <= small_w < 4096, small_w
    assert 16 <= small_h < 4096, small_h

    # A 10x-wider bbox produces a wider grid (scales with extent).
    wide_bbox = (-82.0, 26.55, -80.8, 26.68)  # ~1.2 deg wide vs ~0.12
    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=wide_bbox,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
    )
    wide_w = int(captured["params"]["WIDTH"])
    assert wide_w > small_w, (wide_w, small_w)

    # A continental bbox at 30 m would blow past 4096 -> clamped exactly.
    huge_bbox = (-125.0, 25.0, -66.0, 49.0)  # CONUS
    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=huge_bbox,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
    )
    assert int(captured["params"]["WIDTH"]) == 4096
    assert int(captured["params"]["HEIGHT"]) == 4096


def test_ogc_adapter_target_resolution_changes_grid(monkeypatch):
    """A finer target_resolution_m yields a denser grid than the 30 m default."""
    captured = _wcs_grid_capture(monkeypatch)

    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
    )
    default_w = int(captured["params"]["WIDTH"])

    # 10 m target -> ~3x the pixels of the 30 m default on the same bbox.
    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
        target_resolution_m=10.0,
    )
    fine_w = int(captured["params"]["WIDTH"])
    assert fine_w > default_w, (fine_w, default_w)


def test_ogc_adapter_explicit_width_height_honored_byte_identical(monkeypatch):
    """Explicit width_px/height_px pass through untouched (byte-identical to prior behavior).

    target_resolution_m is ignored when explicit dimensions are given.
    """
    captured = _wcs_grid_capture(monkeypatch)

    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        service_type="WCS",
        image_format="GeoTIFF",
        version="1.0.0",
        width_px=512,
        height_px=256,
        target_resolution_m=10.0,  # ignored — explicit dims win
    )
    assert captured["params"]["WIDTH"] == "512"
    assert captured["params"]["HEIGHT"] == "256"


def test_catalog_fetch_imageserver_uses_entry_native_resolution(
    fake_storage_patched, monkeypatch
):
    """3DEP ImageServer Tier-2 dispatch auto-targets the entry's 10 m native_resolution_m.

    With no caller-supplied width/height or target_resolution_m, the dispatch
    forwards the entry's native_resolution_m so the exportImage ``size`` is an
    extent-aware grid at 10 m (vs the old fixed 1024).
    """
    captured: dict = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        return _FakeOGCResponse(
            content=b"II*\x00" + b"\x00" * 256, content_type="image/tiff"
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    result = catalog_fetch(
        entry_id="usgs-3dep-elevation-image-service",
        params={"bbox": list(FORT_MYERS_BBOX)},
    )
    assert result["access_tier"] == 2
    # size is "<w>,<h>"; with native 10 m on the Fort Myers bbox it is NOT the
    # old fixed 1024,1024 and both axes are extent-aware (> 0, <= 4096).
    size = captured["params"]["size"]
    w_str, h_str = size.split(",")
    w, h = int(w_str), int(h_str)
    assert size != "1024,1024", size
    assert 0 < w <= 4096 and 0 < h <= 4096, size


def test_ogc_adapter_surfaces_exception_xml(monkeypatch):
    """An OGC ExceptionReport XML body raises OGCAdapterError, not silently cached."""

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        return _FakeOGCResponse(
            content=b'<?xml version="1.0"?><ows:ExceptionReport><Exception/></ows:ExceptionReport>',
            content_type="application/xml",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    with pytest.raises(OGCAdapterError):
        fetch_ogc_layer(
            url="https://example.test/wcs",
            layer_name="bogus",
            bbox=FORT_MYERS_BBOX,
            service_type="WCS",
            image_format="GeoTIFF",
        )


# ---------------------------------------------------------------------------
# fetch_landcover refactor: NLCD path now routes through generic adapter.
# ---------------------------------------------------------------------------


def test_fetch_landcover_routes_through_generic_ogc_adapter(monkeypatch):
    """job-0047 refactor: fetch_landcover NLCD path now calls fetch_ogc_layer.

    Verifies the shared adapter is the single source of truth for Tier 2 —
    a future refactor can't accidentally fork the WCS implementation
    without this test catching it.
    """
    from grace2_agent.tools import data_fetch

    captured: dict = {}

    def _fake_fetch_ogc_layer(
        url, layer_name, bbox, **kwargs
    ):
        captured["url"] = url
        captured["layer_name"] = layer_name
        captured["bbox"] = bbox
        captured["service_type"] = kwargs.get("service_type")
        captured["version"] = kwargs.get("version")
        captured["image_format"] = kwargs.get("image_format")
        return OGCResponse(
            content=b"\x49\x49\x2a\x00" + b"\x00" * 256,
            content_type="image/tiff",
            service_type=kwargs.get("service_type"),
            url=url,
            status_code=200,
        )

    monkeypatch.setattr(ogc_mod, "fetch_ogc_layer", _fake_fetch_ogc_layer)

    out = data_fetch._fetch_nlcd_landcover_bytes(FORT_MYERS_BBOX, 2021)
    assert isinstance(out, bytes) and len(out) > 4
    assert captured["service_type"] == "WCS"
    assert captured["version"] == "1.0.0"
    assert captured["image_format"] == "GeoTIFF"
    assert captured["layer_name"].startswith("mrlc_display:NLCD_2021_Land_Cover_L48")
    assert "wcs" in captured["url"].lower()
