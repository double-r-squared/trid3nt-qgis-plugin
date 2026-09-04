"""Router value coverage for the fetch_landcover fold (ADR 0082).

The NLCD landcover twin folded to a source.yaml + the wcs_getcoverage access mode
(WCS 1.0.0 GetCoverage -> NLCD background(0)->nodata remap -> palette COG), the
pre_resolve auto-coarsen (dataset alias + vintage parse + effective-resolution +
quantized bbox into the cache key), and the post-emit envelope (the SFINCS Manning's
sidecar -> LandcoverResult). These tests carry the value-bearing surface the twin's
tests carried: the dataset-alias + vintage resolution, the background-transparency
remap, the paletted categorical COG, the auto-coarsen at state scale, and the sidecar
that the SFINCS builder reads (.uri + .nlcd_vintage_year).
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_contracts.execution import LandcoverResult
from trid3nt_server.tools.fetchers._fetch_common import round_bbox_to_resolution
from trid3nt_server.tools.fetchers._router import router as _router
from trid3nt_server.tools.fetchers._router.hooks import landcover as lch
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.search import ogc_adapter

_FORT_MYERS = [-81.95, 26.55, -81.80, 26.70]

#: NLCD palette (a handful of real class codes + background 0).
_NLCD_CMAP = {
    0: (0, 0, 0, 255),        # background -> opaque black (the bug the remap fixes)
    11: (72, 109, 162, 255),  # open water
    21: (222, 202, 202, 255), # developed open space
    41: (56, 129, 78, 255),   # deciduous forest
    82: (220, 217, 61, 255),  # cultivated crops
    95: (112, 163, 186, 255), # emergent wetlands
}


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_landcover"]


def _synth_nlcd_bytes(bounds, arr: np.ndarray) -> bytes:
    """A single-band uint8 NLCD GeoTIFF (EPSG:4326, nodata=255, embedded palette)."""
    h, w = arr.shape
    transform = rasterio.transform.from_bounds(*bounds, w, h)
    cmap = {i: _NLCD_CMAP.get(i, (0, 0, 0, 0)) for i in range(256)}
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=h, width=w, count=1, dtype="uint8",
                      crs="EPSG:4326", transform=transform, nodata=255) as dst:
            dst.write(arr, 1)
            dst.write_colormap(1, cmap)
        return mem.read()


def _classified_nlcd(bounds) -> bytes:
    """A 20x20 NLCD raster: real classes + a background(0) patch (over 'ocean')."""
    arr = np.empty((20, 20), dtype="uint8")
    codes = [11, 21, 41, 82, 95]
    for r in range(20):
        arr[r, :] = codes[r % len(codes)]
    arr[:4, :4] = 0  # a background/no-coverage corner (must remap to nodata=255)
    return _synth_nlcd_bytes(bounds, arr)


def _patch_ogc(monkeypatch, tile_bytes: bytes):
    def fake_fetch(url, layer_name, bbox, **kw):
        return ogc_adapter.OGCResponse(
            content=tile_bytes, content_type="image/tiff", service_type="WCS",
            url=url, status_code=200,
        )

    monkeypatch.setattr(ogc_adapter, "fetch_ogc_layer", fake_fetch)


def _patch_router_cache(monkeypatch, store):
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


def test_spec_identity(spec):
    assert spec.name == "fetch_landcover"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "LANDCOVER"
    assert spec.source_class == "landcover"
    assert spec.output.result_model == "LandcoverResult"
    assert spec.output.style == {"kind": "classed", "label": "Land Cover"}
    assert spec.output.role == "input"
    assert spec.normalize.units == "nlcd_class_code"
    assert spec.hooks.pre_resolve == "landcover.pre_resolve"
    assert spec.hooks.envelope == "landcover.envelope"
    assert spec.cache.ttl_class == "static-30d"
    assert (spec.ingest or {}).get("access") == "wcs_getcoverage"
    assert 2021 in {int(k) for k in spec.ingest["wcs"]["coverage_by_year"]}


def test_pre_resolve_alias_and_vintage(spec):
    r = lch.pre_resolve(spec, {"bbox": list(_FORT_MYERS), "dataset": "nlcd", "resolution_m": 30})
    assert r["dataset"] == "nlcd_2021"
    assert r["vintage_year"] == 2021
    assert r["resolution_m"] == 30
    assert r["downsampled"] is False
    r2 = lch.pre_resolve(spec, {"bbox": list(_FORT_MYERS), "dataset": "nlcd_2019", "resolution_m": 30})
    assert r2["vintage_year"] == 2019


def test_pre_resolve_auto_coarsen_state_scale(spec):
    # A ~4-degree-wide AOI at 30 m blows the 4000-px budget -> coarsened + downsampled.
    wa = [-124.8, 45.5, -116.9, 49.0]
    r = lch.pre_resolve(spec, {"bbox": wa, "dataset": "nlcd_2021", "resolution_m": 30})
    assert r["resolution_m"] > 30
    assert r["downsampled"] is True


def test_pre_resolve_bad_dataset(spec):
    with pytest.raises(Exception) as ei:
        lch.pre_resolve(spec, {"bbox": list(_FORT_MYERS), "dataset": "usgs_nlcd_2023"})
    assert getattr(ei.value, "error_code", "") == "LANDCOVER_INPUT_ERROR"


def test_pre_resolve_esa_not_implemented(spec):
    with pytest.raises(Exception) as ei:
        lch.pre_resolve(spec, {"bbox": list(_FORT_MYERS), "dataset": "esa_worldcover_2021"})
    assert "LANDCOVER" in getattr(ei.value, "error_code", "")


def test_route_continent_scale_ceiling(spec):
    """A continent-scale bbox (> 5e6 km^2) is refused by the gates.max_bbox_km2
    ceiling (the twin's BboxInvalidError, now a typed router input error)."""
    whole_conus = [-125.0, 24.0, -66.0, 50.0]  # ~8e6 km^2
    with pytest.raises(Exception) as ei:
        _router.route(spec, {"bbox": whole_conus, "dataset": "nlcd_2021", "resolution_m": 600})
    assert "LANDCOVER" in getattr(ei.value, "error_code", "")


def test_full_route_paletted_cog_and_sidecar(spec, monkeypatch):
    q = round_bbox_to_resolution(tuple(_FORT_MYERS), 30)
    _patch_ogc(monkeypatch, _classified_nlcd(q))
    store: dict[str, bytes] = {}
    _patch_router_cache(monkeypatch, store)

    out = _router.route(spec, {"bbox": list(_FORT_MYERS), "dataset": "nlcd_2021", "resolution_m": 30})
    assert isinstance(out, LandcoverResult)
    assert out.layer_type == "raster"
    assert out.style == {"kind": "classed", "label": "Land Cover"}
    assert out.role == "input"
    assert out.units == "nlcd_class_code"
    assert out.uri.startswith("s3://")
    assert out.nlcd_vintage_year == 2021
    assert out.dataset == "nlcd_2021"
    assert out.source == "mrlc-wcs"
    assert out.effective_resolution_m == 30
    assert out.native_resolution_m == 30
    assert out.downsampled is False
    assert out.downsampling_note is None

    cog = next(iter(store.values()))
    with MemoryFile(cog) as mem, mem.open() as ds:
        assert str(ds.dtypes[0]) == "uint8"
        arr = ds.read(1)
        # Background (0) folded into nodata (255); real classes preserved.
        assert 0 not in np.unique(arr)
        assert {11, 21, 41, 82, 95} & set(np.unique(arr).tolist())
        cmap = ds.colormap(1)
        assert cmap[41][:3] == (56, 129, 78)  # palette preserved
