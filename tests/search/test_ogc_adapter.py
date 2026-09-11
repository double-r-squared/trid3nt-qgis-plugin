"""The generic OGC adapter: WMS, WCS, WFS and ArcGIS REST request shapes.

Each service is driven against a mocked ``requests.get`` and asserted on the
parameter dict it builds, including the extent-aware raster grid and its clamp.
An OGC ExceptionReport body raises ``OGCAdapterError`` rather than caching, and
``fetch_landcover``'s NLCD path is held to this one adapter."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.search import ogc_adapter as ogc_mod
from trid3nt_server.tools.search.ogc_adapter import (
    OGCAdapterError,
    OGCResponse,
    fetch_ogc_layer,
)


FORT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)


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
    """No width or height gives an extent-aware grid, clamped at 4096 per axis.

    With both None the adapter derives WIDTH and HEIGHT from the bbox at the default
    30 m cell, so a bigger bbox is more pixels up to ``_OGC_PX_MAX``."""
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


def test_fetch_landcover_routes_through_generic_ogc_adapter(monkeypatch):
    """The NLCD path in ``fetch_landcover`` calls ``fetch_ogc_layer``.

    The shared adapter is the single source of truth for Tier 2, so a forked WCS
    implementation fails here."""
    # fetch_landcover is spec-driven: the WCS GetCoverage GET lives in the
    # router's wcs_getcoverage access mode, still the shared ogc adapter (Tier-2 SoT).
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile

    from trid3nt_server.tools.fetchers._router import router as _router
    from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

    spec = compose_specs_from_tree()["fetch_landcover"]
    captured: dict = {}

    def _synth_nlcd(bbox):
        arr = np.full((16, 16), 41, dtype="uint8")
        tr = rasterio.transform.from_bounds(*bbox, 16, 16)
        with MemoryFile() as mem:
            with mem.open(driver="GTiff", height=16, width=16, count=1, dtype="uint8",
                          crs="EPSG:4326", transform=tr, nodata=255) as dst:
                dst.write(arr, 1)
            return mem.read()

    def _fake_fetch_ogc_layer(url, layer_name, bbox, **kwargs):
        captured.update(url=url, layer_name=layer_name, bbox=bbox,
                        service_type=kwargs.get("service_type"),
                        version=kwargs.get("version"),
                        image_format=kwargs.get("image_format"))
        return OGCResponse(content=_synth_nlcd(bbox), content_type="image/tiff",
                           service_type=kwargs.get("service_type"), url=url, status_code=200)

    monkeypatch.setattr(ogc_mod, "fetch_ogc_layer", _fake_fetch_ogc_layer)

    from trid3nt_server.tools.cache import ReadThroughResult
    monkeypatch.setattr(_router, "read_through",
                        lambda metadata, params, ext, fetch_fn, **kw: ReadThroughResult(
                            uri="s3://fake/landcover.tif", data=fetch_fn(), hit=False))

    out = _router.route(spec, {"bbox": list(FORT_MYERS_BBOX), "dataset": "nlcd_2021", "resolution_m": 30})
    assert out.uri.startswith("s3://")
    assert captured["service_type"] == "WCS"
    assert captured["version"] == "1.0.0"
    assert captured["image_format"] == "GeoTIFF"
    assert captured["layer_name"].startswith("mrlc_display:NLCD_2021_Land_Cover_L48")
    assert "wcs" in captured["url"].lower()
