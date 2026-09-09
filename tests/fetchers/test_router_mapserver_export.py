"""Offline tests for the router ``mapserver_export`` RGBA mode + the NOAA SLR
siblings (fetch_noaa_slr_confidence + fetch_noaa_slr_marsh), migrated from the
deleted twin test file when the twins folded to source.yaml (ADR 0068).

Network is monkeypatched: a synthetic PNG32 is fed through the real
georeference -> 4-band RGBA COG path; the service map + res_deg grid + typed
errors are exercised without a live call. ASCII only.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
import rasterio
from PIL import Image

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as R
from trid3nt_server.tools.fetchers._router import router as router_mod
from trid3nt_server.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.tools.fetchers._router.executors import raster_cog
from trid3nt_server.tools.fetchers._router.transport import TransportUpstreamError

_BBOX = (-82.2, 26.2, -81.5, 26.9)  # coastal Lee County FL
_NAMES = ("fetch_noaa_slr_confidence", "fetch_noaa_slr_marsh")


def _png_bytes(opaque_frac=0.5, w=32, h=24):
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    n_op = int(w * h * opaque_frac)
    rgba.reshape(-1, 4)[:n_op] = (14, 96, 218, 255)  # blue, opaque
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _run(monkeypatch, name, slr_ft, res_deg=0.02, png=None, transport_exc=None):
    spec = R.get_spec(name)
    params = router_mod.validate_params(
        spec, {"bbox": list(_BBOX), "slr_ft": slr_ft, "res_deg": res_deg}
    )

    def fake_get_bytes(*a, **k):
        if transport_exc is not None:
            raise transport_exc
        return (png if png is not None else _png_bytes(), "image/png", "http://x")

    import trid3nt_server.tools.fetchers._router.transport as T
    monkeypatch.setattr(T, "get_bytes", fake_get_bytes)
    return raster_cog.execute(spec, params)


# --------------------------------------------------------------------------- #
# Registration + spec + retrieval surface.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,source", list(zip(_NAMES, ("noaa_slr_confidence", "noaa_slr_marsh"))))
def test_registered_spec_driven(name, source):
    assert name in TOOL_REGISTRY
    m = TOOL_REGISTRY[name].metadata
    assert m.name == name and m.source_class == source
    assert m.ttl_class == "static-30d" and m.cacheable is True
    assert m.supports_global_query is False
    spec = R.get_spec(name)
    assert spec is not None and spec.ingest.get("access") == "mapserver_export"
    assert spec.error_code_prefix == "NOAA_SLR_RASTER"


def test_corpus_present():
    from trid3nt_server.tools.search.search_tools.search_tools import _load_corpus

    corpus = _load_corpus()
    for n in _NAMES:
        assert n in corpus and len(corpus[n]) >= 3


# --------------------------------------------------------------------------- #
# array_to_cog_bytes RGBA branch (the serializer extension, no-op for priors).
# --------------------------------------------------------------------------- #
def test_array_to_cog_bytes_rgba_branch():
    arr = np.zeros((4, 8, 10), dtype=np.uint8)
    arr[3] = 255  # opaque alpha
    from rasterio.transform import from_bounds

    tf = from_bounds(*_BBOX, 10, 8)
    cog = raster_cog.array_to_cog_bytes(
        arr, tf, "EPSG:4326", nodata=None, dtype="uint8", colorinterp="rgba"
    )
    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 4 and ds.dtypes[0] == "uint8"
        assert ds.nodata is None  # RGBA carries transparency in alpha, not nodata
        from rasterio.enums import ColorInterp

        assert list(ds.colorinterp) == [
            ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha
        ]


def test_array_to_cog_bytes_singleband_unchanged():
    # The default path is byte-shape-identical for priors (nan nodata, no rgba).
    arr = np.array([[1.0, 2.0], [3.0, 4.0]], dtype="float32")
    from rasterio.transform import from_bounds

    tf = from_bounds(*_BBOX, 2, 2)
    cog = raster_cog.array_to_cog_bytes(arr, tf, "EPSG:4326")
    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 1 and ds.dtypes[0] == "float32"
        assert np.isnan(ds.nodata)


# --------------------------------------------------------------------------- #
# mapserver_export mode: PNG32 -> georeferenced 4-band RGBA COG.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,slr_ft", [("fetch_noaa_slr_confidence", 3.0),
                                         ("fetch_noaa_slr_marsh", 1.5)])
def test_export_produces_valid_rgba_cog(monkeypatch, name, slr_ft):
    cog = _run(monkeypatch, name, slr_ft, png=_png_bytes(0.5))
    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 4 and ds.dtypes[0] == "uint8"
        assert ds.crs is not None and ds.crs.to_epsg() == 4326
        assert int((ds.read(4) > 0).sum()) > 0  # some lit (non-transparent) pixels


def test_export_fully_transparent_still_valid(monkeypatch):
    # honesty floor: a no-coverage export is a valid transparent overlay, NOT EMPTY.
    cog = _run(monkeypatch, "fetch_noaa_slr_marsh", 3.0, png=_png_bytes(0.0))
    with rasterio.open(io.BytesIO(cog)) as ds:
        assert ds.count == 4
        assert int((ds.read(4) > 0).sum()) == 0  # fully transparent, no raise


def test_export_http_error_raises_upstream(monkeypatch):
    with pytest.raises(RouterUpstreamError):
        _run(monkeypatch, "fetch_noaa_slr_confidence", 3.0,
             transport_exc=TransportUpstreamError("boom", status=500, body="boom"))


def test_export_undecodable_body_raises_upstream(monkeypatch):
    with pytest.raises(RouterUpstreamError):
        _run(monkeypatch, "fetch_noaa_slr_confidence", 3.0, png=b'{"error":{"code":400}}')


# --------------------------------------------------------------------------- #
# Service-name resolution via the declarative map + typed input errors.
# --------------------------------------------------------------------------- #
def test_service_resolution_and_query(monkeypatch):
    seen = {}

    def cap_get_bytes(client, url, *, headers=None, params=None):
        seen["url"] = url
        seen["params"] = params
        return (_png_bytes(), "image/png", url)

    import trid3nt_server.tools.fetchers._router.transport as T
    monkeypatch.setattr(T, "get_bytes", cap_get_bytes)
    spec = R.get_spec("fetch_noaa_slr_marsh")
    params = router_mod.validate_params(spec, {"bbox": list(_BBOX), "slr_ft": 3.0, "res_deg": 0.02})
    raster_cog.execute(spec, params)
    assert seen["url"].endswith("/marsh_300/MapServer/export")
    assert seen["params"]["format"] == "png32" and seen["params"]["transparent"] == "true"
    assert seen["params"]["f"] == "image"


@pytest.mark.parametrize("name,badft", [
    ("fetch_noaa_slr_confidence", 3.5),   # half-foot invalid for confidence
    ("fetch_noaa_slr_confidence", 11.0),  # above the published range
    ("fetch_noaa_slr_marsh", 0.25),       # not a 0.5-ft step
])
def test_invalid_level_input_invalid(monkeypatch, name, badft):
    with pytest.raises(RouterInputError) as ei:
        _run(monkeypatch, name, badft)
    assert ei.value.error_code == "NOAA_SLR_RASTER_INPUT_INVALID"
    assert ei.value.retryable is False


def test_nonpositive_res_deg_input_invalid(monkeypatch):
    with pytest.raises(RouterInputError) as ei:
        _run(monkeypatch, "fetch_noaa_slr_confidence", 3.0, res_deg=0.0)
    assert ei.value.error_code == "NOAA_SLR_RASTER_INPUT_INVALID"


def test_res_deg_controls_grid(monkeypatch):
    # finer res_deg -> a larger pixel grid (bounded by px_max).
    fine = _run(monkeypatch, "fetch_noaa_slr_marsh", 3.0, res_deg=0.005, png=_png_bytes(0.3))
    coarse = _run(monkeypatch, "fetch_noaa_slr_marsh", 3.0, res_deg=0.05, png=_png_bytes(0.3))
    # PNG size is fixed by the stub, so the emitted COG grid follows the PNG, but
    # the requested export size is res_deg-driven -- assert the grid function here.
    w_f, h_f = raster_cog._mapserver_export_grid(_BBOX, 0.005, {})
    w_c, h_c = raster_cog._mapserver_export_grid(_BBOX, 0.05, {})
    assert w_f > w_c and h_f > h_c
