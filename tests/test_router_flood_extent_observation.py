"""Router value coverage for the fetch_flood_extent_observation fold (ADR 0082).

The MCDWD observed-flood-extent twin folded to a source.yaml + the categorical_tile_grid
access mode (per-10-deg-tile direct GET + first-valid uint8 mosaic + palette COG), the
pre_resolve LANCE dir-walk (date/None -> year/doy into the cache key), and the post-emit
envelope (class_breakdown/flood_area/legend -> FloodExtentObservationResult). These tests
carry the value-bearing surface the deleted twin's tests carried: the classified-tile
first-valid mosaic, the categorical palette COG (nodata transparent), the observation
envelope, and the honest all-nodata / no-tile no-coverage degrade.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_contracts.execution import FloodExtentObservationResult
from trid3nt_server.tools.fetchers._router import router as _router
from trid3nt_server.tools.fetchers._router import transport as _transport
from trid3nt_server.tools.fetchers._router.hooks import flood_extent_observation as feh
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_NODATA = 255
_BBOX = [-85.5, 29.5, -85.0, 30.0]  # inside MCDWD tile (h=9, v=6)


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_flood_extent_observation"]


def _tile_bounds(h: int, v: int) -> tuple[float, float, float, float]:
    west = -180.0 + 10.0 * h
    north = 90.0 - 10.0 * v
    return (west, north - 10.0, west + 10.0, north)


def _synth_tile_bytes(bounds, arr: np.ndarray) -> bytes:
    h, w = arr.shape
    transform = rasterio.transform.from_bounds(*bounds, w, h)
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=h, width=w, count=1, dtype="uint8",
                      crs="EPSG:4326", transform=transform, nodata=_NODATA) as dst:
            dst.write(arr, 1)
        return mem.read()


def _classified_tile(h: int, v: int) -> bytes:
    """A tile with classes 1/2/3 in the north band, 0/255 in the south (twin fixture)."""
    n = 120
    arr = np.zeros((n, n), dtype="uint8")
    for col in range(n):
        arr[:, col] = 1 + (col % 3)
    arr[-10:-5, :] = 0
    arr[-5:, :] = _NODATA
    return _synth_tile_bytes(_tile_bounds(h, v), arr)


def _install(monkeypatch, store, tile_fn):
    """Patch the router cache (in-memory) + the transport GET (synthetic MCDWD tiles)."""
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path, compute_cache_key as ck, is_cacheable,
    )

    def patched_rt(metadata, params, ext, fetch_fn, **kw):
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(metadata.source_class or metadata.name, params, metadata.ttl_class)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{CACHE_BUCKET}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(_router, "read_through", patched_rt)

    def fake_get_bytes(client, url, *, headers=None, params=None):
        # URL: .../{year}/{doy}/MCDWD_L3_F3_NRT.A{yr}{doy}.h{hh}v{vv}.061.tif
        import re
        m = re.search(r"\.h(\d{2})v(\d{2})\.", url)
        if not m:
            raise _transport.TransportNotFound(f"no tile {url}")
        h, v = int(m.group(1)), int(m.group(2))
        b = tile_fn(h, v)
        if not b:
            raise _transport.TransportNotFound(f"absent {url}")
        return b, "image/tiff", url

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)


def test_spec_identity(spec):
    assert spec.name == "fetch_flood_extent_observation"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "FLOOD_EXTENT"
    assert spec.source_class == "mcdwd_flood_extent"
    assert spec.supports_global_query is False
    assert spec.output.result_model == "FloodExtentObservationResult"
    assert spec.output.style["kind"] == "continuous"
    assert spec.hooks.pre_resolve == "flood_extent_observation.pre_resolve"
    assert spec.hooks.envelope == "flood_extent_observation.envelope"
    assert spec.cache.ttl_class == "semi-static-7d"
    assert (spec.ingest or {}).get("access") == "categorical_tile_grid"


def test_pre_resolve_explicit_and_latest(spec, monkeypatch):
    assert feh.pre_resolve(spec, {"date": "2026-07-17"}) == {"year": 2026, "doy": 198}
    # None -> the dir-walk latest (patched listing).
    monkeypatch.setattr(feh, "_latest_available", lambda s: (2026, 198))
    assert feh.pre_resolve(spec, {"date": None}) == {"year": 2026, "doy": 198}


def test_synthetic_happy(spec, monkeypatch):
    store: dict[str, bytes] = {}
    _install(monkeypatch, store, lambda h, v: _classified_tile(h, v) if (h, v) == (9, 6) else b"")
    layer = _router.route(spec, {"bbox": _BBOX, "date": "2026-07-17"})

    assert isinstance(layer, FloodExtentObservationResult)
    assert layer.layer_type == "raster"
    assert layer.style["kind"] == "continuous"
    assert layer.uri.startswith("s3://")
    assert layer.observation_date == "2026-07-17"
    assert layer.product == "MCDWD_L3_F3_NRT"
    assert layer.flood_pixel_count > 0
    assert layer.flood_area_km2 and layer.flood_area_km2 > 0
    assert "Flood water" in layer.class_breakdown
    assert layer.legend is not None and layer.legend.kind == "classed"
    assert {c.value for c in layer.legend.classes} == {1, 2, 3}
    joined = " ".join(layer.caveats)
    assert "UNDER-detects" in joined and "SAR" in joined and "250 m" in joined

    cog = next(iter(store.values()))
    with MemoryFile(cog) as mem, mem.open() as out:
        assert out.count == 1 and str(out.dtypes[0]) == "uint8" and out.nodata == 255
        cmap = out.colormap(1)
        assert cmap[3][:3] == (202, 0, 32)
        assert cmap[1][:3] == (146, 197, 222)
        assert cmap[255][3] == 0
        assert set(np.unique(out.read(1)).tolist()) & {1, 2, 3}


def test_all_nodata_no_coverage(spec, monkeypatch):
    store: dict[str, bytes] = {}

    def all_nodata(h, v):
        return _synth_tile_bytes(_tile_bounds(h, v), np.full((40, 40), _NODATA, dtype="uint8"))

    _install(monkeypatch, store, lambda h, v: all_nodata(h, v) if (h, v) == (9, 6) else b"")
    with pytest.raises(Exception) as ei:
        _router.route(spec, {"bbox": _BBOX, "date": "2026-07-17"})
    assert getattr(ei.value, "error_code", "") == "FLOOD_EXTENT_NO_COVERAGE"


def test_no_tiles_no_coverage(spec, monkeypatch):
    store: dict[str, bytes] = {}
    _install(monkeypatch, store, lambda h, v: b"")
    with pytest.raises(Exception) as ei:
        _router.route(spec, {"bbox": _BBOX, "date": "2026-07-17"})
    assert getattr(ei.value, "error_code", "") == "FLOOD_EXTENT_NO_COVERAGE"


def test_too_large_bbox_input_error(spec):
    with pytest.raises(Exception) as ei:
        _router.route(spec, {"bbox": [-90.0, 20.0, -80.0, 30.0], "date": "2026-07-17"})  # 100 deg^2
    assert "FLOOD_EXTENT" in getattr(ei.value, "error_code", "")
