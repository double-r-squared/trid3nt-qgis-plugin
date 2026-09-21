"""The WCS GetCoverage transport: the request shape it builds and what it refuses.

The call is driven against a mocked ``requests.get`` and asserted on the
parameter dict, including the extent-aware raster grid and its clamp. An OGC
ExceptionReport body raises ``OGCAdapterError`` rather than caching, a version
other than 1.0.0 refuses, and ``fetch_landcover``'s NLCD path is held to this one
transport."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers._router.transport import ogc_adapter as ogc_mod
from trid3nt_server.tools.fetchers._router.transport.ogc_adapter import (
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


def _wcs_grid_capture(monkeypatch) -> dict:
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


def test_the_getcoverage_request_carries_the_coverage_the_crs_and_the_grid(monkeypatch):
    """WCS 1.0.0 GetCoverage mocked: mirror the NLCD WCS path."""
    captured = _wcs_grid_capture(monkeypatch)

    resp = fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="mrlc_display:NLCD_2021_Land_Cover_L48",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:4326",
        image_format="GeoTIFF",
        version="1.0.0",
        width_px=512,
        height_px=512,
    )
    assert "tiff" in resp.content_type.lower()
    p = captured["params"]
    assert p["service"] == "WCS"
    assert p["version"] == "1.0.0"
    assert p["request"] == "GetCoverage"
    assert p["Coverage"] == "mrlc_display:NLCD_2021_Land_Cover_L48"
    assert p["CRS"] == "EPSG:4326"
    assert p["BBOX"] == "-81.92,26.55,-81.8,26.68"
    assert p["WIDTH"] == "512" and p["HEIGHT"] == "512"
    assert p["FORMAT"] == "GeoTIFF"


def test_a_version_nobody_probed_refuses_rather_than_sending_its_shape(monkeypatch):
    """1.1.1 and 2.0.1 rename every parameter, so a row naming one is refused."""
    _wcs_grid_capture(monkeypatch)

    with pytest.raises(OGCAdapterError, match="1.0.0"):
        fetch_ogc_layer(
            url="https://example.test/geoserver/wcs",
            layer_name="cov",
            bbox=FORT_MYERS_BBOX,
            version="2.0.1",
        )


def test_an_extra_param_the_row_states_reaches_the_query(monkeypatch):
    """CHS NONNA asks for its answer on the mosaic's own frame through one."""
    captured = _wcs_grid_capture(monkeypatch)

    fetch_ogc_layer(
        url="https://example.test/geoserver/wcs",
        layer_name="cov",
        bbox=FORT_MYERS_BBOX,
        crs="EPSG:3857",
        extra_params={"RESPONSE_CRS": "EPSG:4326"},
    )
    assert captured["params"]["RESPONSE_CRS"] == "EPSG:4326"


def test_no_width_or_height_gives_an_extent_aware_grid_clamped_per_axis(monkeypatch):
    """With both None the adapter derives WIDTH and HEIGHT from the bbox at the
    default 30 m cell, so a bigger bbox is more pixels up to ``_OGC_PX_MAX``."""
    captured = _wcs_grid_capture(monkeypatch)

    fetch_ogc_layer(url="https://example.test/geoserver/wcs", layer_name="cov",
                    bbox=FORT_MYERS_BBOX, crs="EPSG:4326", image_format="GeoTIFF")
    small_w = int(captured["params"]["WIDTH"])
    small_h = int(captured["params"]["HEIGHT"])
    assert 16 <= small_w < 4096, small_w
    assert 16 <= small_h < 4096, small_h

    wide_bbox = (-82.0, 26.55, -80.8, 26.68)  # ~1.2 deg wide vs ~0.12
    fetch_ogc_layer(url="https://example.test/geoserver/wcs", layer_name="cov",
                    bbox=wide_bbox, crs="EPSG:4326", image_format="GeoTIFF")
    assert int(captured["params"]["WIDTH"]) > small_w

    huge_bbox = (-125.0, 25.0, -66.0, 49.0)  # CONUS at 30 m blows past the clamp
    fetch_ogc_layer(url="https://example.test/geoserver/wcs", layer_name="cov",
                    bbox=huge_bbox, crs="EPSG:4326", image_format="GeoTIFF")
    assert int(captured["params"]["WIDTH"]) == 4096
    assert int(captured["params"]["HEIGHT"]) == 4096


def test_a_finer_target_resolution_yields_a_denser_grid(monkeypatch):
    captured = _wcs_grid_capture(monkeypatch)

    fetch_ogc_layer(url="https://example.test/geoserver/wcs", layer_name="cov",
                    bbox=FORT_MYERS_BBOX, crs="EPSG:4326", image_format="GeoTIFF")
    default_w = int(captured["params"]["WIDTH"])

    fetch_ogc_layer(url="https://example.test/geoserver/wcs", layer_name="cov",
                    bbox=FORT_MYERS_BBOX, crs="EPSG:4326", image_format="GeoTIFF",
                    target_resolution_m=10.0)
    assert int(captured["params"]["WIDTH"]) > default_w


def test_explicit_dimensions_win_over_the_target_resolution(monkeypatch):
    captured = _wcs_grid_capture(monkeypatch)

    fetch_ogc_layer(url="https://example.test/geoserver/wcs", layer_name="cov",
                    bbox=FORT_MYERS_BBOX, crs="EPSG:4326", image_format="GeoTIFF",
                    width_px=512, height_px=256, target_resolution_m=10.0)
    assert captured["params"]["WIDTH"] == "512"
    assert captured["params"]["HEIGHT"] == "256"


def test_an_exception_report_body_raises_rather_than_caching_the_xml(monkeypatch):
    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        return _FakeOGCResponse(
            content=b'<?xml version="1.0"?><ows:ExceptionReport><Exception/></ows:ExceptionReport>',
            content_type="application/xml",
        )

    monkeypatch.setattr(ogc_mod.requests, "get", _fake_get)

    with pytest.raises(OGCAdapterError):
        fetch_ogc_layer(url="https://example.test/wcs", layer_name="bogus",
                        bbox=FORT_MYERS_BBOX, image_format="GeoTIFF")


def test_the_landcover_row_reaches_the_service_through_this_one_transport(monkeypatch):
    """The NLCD path in ``fetch_landcover`` calls ``fetch_ogc_layer``, so a forked
    WCS implementation fails here."""
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
                        version=kwargs.get("version"),
                        image_format=kwargs.get("image_format"))
        return OGCResponse(content=_synth_nlcd(bbox), content_type="image/tiff")

    monkeypatch.setattr(ogc_mod, "fetch_ogc_layer", _fake_fetch_ogc_layer)

    from trid3nt_server.tools.cache import ReadThroughResult
    monkeypatch.setattr(_router, "read_through",
                        lambda metadata, params, ext, fetch_fn, **kw: ReadThroughResult(
                            uri="s3://fake/landcover.tif", data=fetch_fn(), hit=False))

    out = _router.route(spec, {"bbox": list(FORT_MYERS_BBOX), "dataset": "nlcd_2021",
                               "resolution_m": 30})
    assert out.uri.startswith("s3://")
    assert captured["version"] == "1.0.0"
    assert captured["image_format"] == "GeoTIFF"
    assert captured["layer_name"].startswith("mrlc_display:NLCD_2021_Land_Cover_L48")
    assert "wcs" in captured["url"].lower()
