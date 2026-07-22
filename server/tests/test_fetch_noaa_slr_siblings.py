"""Unit tests for the NOAA SLR raster siblings (fetch_noaa_slr_confidence +
fetch_noaa_slr_marsh) and their shared _noaa_slr_raster export path.

Network is monkeypatched: the export test feeds a synthetic PNG through the real
georeference->COG path; the tool tests stub the export + read_through. ASCII only.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import _noaa_slr_raster as slr
from trid3nt_server.tools import fetch_noaa_slr_confidence as confmod
from trid3nt_server.tools import fetch_noaa_slr_marsh as marshmod
from trid3nt_server.tools._noaa_slr_raster import (
    NOAASLRRasterInputError,
    NOAASLRRasterUpstreamError,
    round_bbox,
)

_BBOX = (-82.2, 26.2, -81.5, 26.9)  # coastal Lee County FL


class _FakeResult:
    __slots__ = ("uri", "data", "hit")

    def __init__(self, uri, data=b"", hit=False):
        self.uri = uri
        self.data = data
        self.hit = hit


class _FakeResp:
    def __init__(self, status, content, ct="image/png"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": ct}
        self.text = "" if isinstance(content, bytes) else str(content)


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        return self._resp


def _png_bytes(opaque_frac=0.5, w=32, h=24):
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    n_op = int(w * h * opaque_frac)
    flat = rgba.reshape(-1, 4)
    flat[:n_op] = (14, 96, 218, 255)  # blue, opaque
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Registration + category + corpus coverage.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,source", [("fetch_noaa_slr_confidence", "noaa_slr_confidence"),
                    ("fetch_noaa_slr_marsh", "noaa_slr_marsh")]
)
def test_registered(name, source):
    assert name in TOOL_REGISTRY
    m = TOOL_REGISTRY[name].metadata
    assert m.name == name and m.source_class == source
    assert m.ttl_class == "static-30d" and m.cacheable is True
    assert m.supports_global_query is False
    assert m.payload_mb_estimator_name == "estimate_payload_mb"


def test_categories():
    from trid3nt_server.categories import PRIMARY_CATEGORY, SECONDARY_CATEGORIES

    assert PRIMARY_CATEGORY["fetch_noaa_slr_confidence"] == "coastal"
    assert PRIMARY_CATEGORY["fetch_noaa_slr_marsh"] == "coastal"
    assert SECONDARY_CATEGORIES.get("fetch_noaa_slr_marsh") == ("conservation_ecology",)


def test_corpus():
    import pathlib

    import yaml

    p = pathlib.Path(slr.__file__).resolve().parents[1] / "data" / "tool_query_corpus.yaml"
    corpus = yaml.safe_load(p.read_text())
    for n in ("fetch_noaa_slr_confidence", "fetch_noaa_slr_marsh"):
        assert n in corpus and len(corpus[n]) >= 3


# ---------------------------------------------------------------------------
# Service-name mapping.
# ---------------------------------------------------------------------------
def test_conf_service_name_mapping():
    assert confmod._conf_service_name(0.0) == "conf_0ft"
    assert confmod._conf_service_name(3.0) == "conf_3ft"
    assert confmod._conf_service_name(10.0) == "conf_10ft"
    assert len(confmod.VALID_CONF_FT) == 11
    with pytest.raises(NOAASLRRasterInputError):
        confmod._conf_service_name(3.5)  # half-feet invalid for confidence
    with pytest.raises(NOAASLRRasterInputError):
        confmod._conf_service_name(11.0)


def test_marsh_service_name_mapping():
    assert marshmod._marsh_service_name(0.0) == "marsh_000"
    assert marshmod._marsh_service_name(0.5) == "marsh_050"
    assert marshmod._marsh_service_name(3.0) == "marsh_300"
    assert marshmod._marsh_service_name(10.0) == "marsh_1000"
    assert len(marshmod.VALID_MARSH_FT) == 21
    with pytest.raises(NOAASLRRasterInputError):
        marshmod._marsh_service_name(0.25)
    with pytest.raises(NOAASLRRasterInputError):
        marshmod._marsh_service_name(11.0)


# ---------------------------------------------------------------------------
# bbox / res_deg validation.
# ---------------------------------------------------------------------------
def test_bbox_validation():
    assert slr.validate_bbox(_BBOX) == _BBOX
    with pytest.raises(NOAASLRRasterInputError):
        slr.validate_bbox(None)
    with pytest.raises(NOAASLRRasterInputError):
        slr.validate_bbox((1.0, 2.0, 3.0))  # wrong length
    with pytest.raises(NOAASLRRasterInputError):
        slr.validate_bbox((1.0, 2.0, 1.0, 3.0))  # degenerate lon
    with pytest.raises(NOAASLRRasterInputError):
        slr.validate_bbox((-200.0, 2.0, 3.0, 4.0))  # out of range


def test_res_deg_validation():
    assert slr.resolve_res_deg(None) == slr._DEFAULT_RES_DEG
    assert slr.resolve_res_deg(0.001) == 0.001
    with pytest.raises(NOAASLRRasterInputError):
        slr.resolve_res_deg(0.0)
    with pytest.raises(NOAASLRRasterInputError):
        slr.resolve_res_deg(-1.0)


# ---------------------------------------------------------------------------
# Shared export -> georeferenced RGBA COG (synthetic PNG through the real path).
# ---------------------------------------------------------------------------
def test_export_produces_valid_rgba_cog(monkeypatch):
    monkeypatch.setattr(slr.httpx, "Client", lambda *a, **k: _FakeClient(_FakeResp(200, _png_bytes(0.5))))
    cog = slr.export_slr_raster_cog_bytes("conf_3ft", _BBOX, res_deg=0.02)
    import rasterio

    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 4
        assert ds.dtypes[0] == "uint8"
        assert ds.crs is not None and ds.crs.to_epsg() == 4326


def test_export_fully_transparent_still_valid(monkeypatch):
    monkeypatch.setattr(slr.httpx, "Client", lambda *a, **k: _FakeClient(_FakeResp(200, _png_bytes(0.0))))
    cog = slr.export_slr_raster_cog_bytes("marsh_300", _BBOX, res_deg=0.02)
    import rasterio

    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 4  # valid empty overlay, not a raise


def test_export_http_error_raises_upstream(monkeypatch):
    monkeypatch.setattr(slr.httpx, "Client", lambda *a, **k: _FakeClient(_FakeResp(500, b"boom", ct="text/plain")))
    with pytest.raises(NOAASLRRasterUpstreamError):
        slr.export_slr_raster_cog_bytes("conf_3ft", _BBOX, res_deg=0.02)


# ---------------------------------------------------------------------------
# Full-tool happy path (export + read_through stubbed).
# ---------------------------------------------------------------------------
def _wire(monkeypatch, mod):
    cap = {}

    def fake_export(service, bbox, res_deg):
        cap["service"] = service
        cap["bbox"] = bbox
        cap["res_deg"] = res_deg
        return b"FAKE-COG-BYTES"

    def fake_rt(metadata, params, ext, fetch_fn):
        cap["params"] = params
        cap["ext"] = ext
        cap["data"] = fetch_fn()
        return _FakeResult(uri=f"s3://fake/{params['product']}-{params['slr_ft']}.tif", data=cap["data"])

    monkeypatch.setattr(mod, "export_slr_raster_cog_bytes", fake_export)
    monkeypatch.setattr(mod, "read_through", fake_rt)
    return cap


def test_confidence_tool_happy_path(monkeypatch):
    cap = _wire(monkeypatch, confmod)
    layer = confmod.fetch_noaa_slr_confidence(bbox=_BBOX, slr_ft=2.0)
    assert cap["service"] == "conf_2ft"
    assert cap["ext"] == "tif" and cap["params"]["product"] == "slr_confidence"
    assert layer.layer_type == "raster" and layer.role == "context"
    assert layer.style_preset == "noaa_slr_confidence"
    assert tuple(layer.bbox) == round_bbox(_BBOX)
    assert layer.uri.startswith("s3://fake/")


def test_marsh_tool_happy_path(monkeypatch):
    cap = _wire(monkeypatch, marshmod)
    layer = marshmod.fetch_noaa_slr_marsh(bbox=_BBOX, slr_ft=1.5)
    assert cap["service"] == "marsh_150"
    assert cap["params"]["product"] == "slr_marsh"
    assert layer.layer_type == "raster" and layer.style_preset == "noaa_slr_marsh"
    assert tuple(layer.bbox) == round_bbox(_BBOX)


def test_invalid_level_raises_before_network(monkeypatch):
    # a bad level must raise WITHOUT calling export (validation first)
    called = {"n": 0}
    monkeypatch.setattr(confmod, "export_slr_raster_cog_bytes",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    with pytest.raises(NOAASLRRasterInputError):
        confmod.fetch_noaa_slr_confidence(bbox=_BBOX, slr_ft=3.5)
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------
def test_estimate_payload_mb():
    assert confmod.estimate_payload_mb(bbox=None) == 3.0
    mb = confmod.estimate_payload_mb(bbox=_BBOX)
    assert 0.0 < mb < 60.0
    # finer res_deg -> larger estimate
    assert marshmod.estimate_payload_mb(bbox=_BBOX, res_deg=0.0001) > marshmod.estimate_payload_mb(bbox=_BBOX, res_deg=0.01)
