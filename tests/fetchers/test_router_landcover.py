"""``fetch_landcover``: the borrowed WCS provider with a post-emit envelope.

The session opens the coverage and exports the asked window at the asked
spacing; the export step folds the background class into the dataset's nodata,
the pre-resolve puts the dataset alias, the parsed vintage, the coverage
identifier, the resolution asked for and the quantized bbox into the cache key,
and the envelope writes the roughness sidecar a solver builder later reads.
Covered with those, the pixel-budget refusal the row's own max_px states."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_contracts.execution import LandcoverResult
from trid3nt_server.tools.fetchers._fetch_common import (
    PixelBudgetExceededError,
    round_bbox_to_resolution,
)
from trid3nt_contracts.ws import LayerResponsePayload
from trid3nt_server.tools.fetchers._router import router as _router
from trid3nt_server.tools.fetchers._router.executors import qgis_provider as qp
from trid3nt_server.tools.fetchers.terrain.fetch_landcover import hooks as lch
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

_FORT_MYERS = [-81.95, 26.55, -81.80, 26.70]

@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_landcover"]


def _synth_nlcd_bytes(bounds, arr: np.ndarray) -> bytes:
    """What the session exports: a single-band uint8 NLCD GeoTIFF in EPSG:4326,
    background class and all, with no palette of its own."""
    h, w = arr.shape
    transform = rasterio.transform.from_bounds(*bounds, w, h)
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=h, width=w, count=1, dtype="uint8",
                      crs="EPSG:4326", transform=transform) as dst:
            dst.write(arr, 1)
        return mem.read()


def _classified_nlcd(bounds) -> bytes:
    """A 20x20 NLCD raster: real classes + a background(0) patch (over 'ocean')."""
    arr = np.empty((20, 20), dtype="uint8")
    codes = [11, 21, 41, 82, 95]
    for r in range(20):
        arr[r, :] = codes[r % len(codes)]
    arr[:4, :4] = 0  # a background/no-coverage corner (must remap to nodata=255)
    return _synth_nlcd_bytes(bounds, arr)


def _patch_session(monkeypatch, exported: bytes) -> list:
    """Stand in for the bound QGIS session: record the request it was asked and
    hand back the export it staged."""
    asked: list = []

    def fake_ask(payload):
        asked.append(payload)
        return LayerResponsePayload(key=payload.key, uri="s3://cache/staged/x.tif")

    monkeypatch.setattr(qp, "ask_session_for_layer", fake_ask)
    monkeypatch.setattr(qp, "_store_bytes", lambda uri: exported)
    return asked


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
    assert spec.output.style["kind"] == "classed"
    assert [11, 11, "#476ba0", "Open Water"] in spec.output.style["classes"]
    assert spec.output.role == "input"
    assert spec.normalize.units == "nlcd_class_code"
    assert spec.hooks.pre_resolve == "landcover.pre_resolve"
    assert spec.hooks.envelope == "landcover.envelope"
    assert spec.cache.ttl_class == "static-30d"
    assert (spec.ingest or {}).get("access") == "qgis_provider"
    block = spec.ingest["qgis_provider"]
    assert block["provider"] == "wcs" and block["mode"] == "materialise"
    assert 2021 in {int(k) for k in block["coverage_by_year"]}
    assert spec.ingest["serialize"] == {"nodata": 255, "dtype": "uint8"}


def test_pre_resolve_alias_and_vintage(spec):
    r = lch.pre_resolve(spec, {"bbox": list(_FORT_MYERS), "dataset": "nlcd", "resolution_m": 30})
    assert r["dataset"] == "nlcd_2021"
    assert r["vintage_year"] == 2021
    assert r["resolution_m"] == 30
    assert r["coverage"] == "mrlc_display:NLCD_2021_Land_Cover_L48"
    assert r["downsampled"] is False
    r2 = lch.pre_resolve(spec, {"bbox": list(_FORT_MYERS), "dataset": "nlcd_2019", "resolution_m": 30})
    assert r2["vintage_year"] == 2019


def test_past_budget_refuses_before_the_session_is_asked(spec):
    """A ~8-degree-wide AOI at 30 m blows the 4000 px/axis budget the row states:
    REFUSED naming the budget and the spacing that fits, never handed back
    coarser than asked."""
    wa = [-124.8, 45.5, -116.9, 49.0]
    block = spec.ingest["qgis_provider"]
    with pytest.raises(PixelBudgetExceededError) as ei:
        qp._within_pixel_budget(spec, block, wa, 30)
    assert "4000 px/axis" in str(ei.value)
    assert "resolution_m=30" in str(ei.value)
    qp._within_pixel_budget(spec, block, wa, 300)


def test_pre_resolve_at_fitting_resolution_serves(spec):
    wa = [-124.8, 45.5, -116.9, 49.0]
    r = lch.pre_resolve(spec, {"bbox": wa, "dataset": "nlcd_2021", "resolution_m": 300})
    assert r["resolution_m"] == 300
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


def test_full_route_asks_the_session_and_folds_the_background(spec, monkeypatch):
    q = round_bbox_to_resolution(tuple(_FORT_MYERS), 30)
    asked = _patch_session(monkeypatch, _classified_nlcd(q))
    store: dict[str, bytes] = {}
    _patch_router_cache(monkeypatch, store)

    out = _router.route(spec, {"bbox": list(_FORT_MYERS), "dataset": "nlcd_2021", "resolution_m": 30})
    assert isinstance(out, LandcoverResult)
    assert out.layer_type == "raster"
    assert out.style["kind"] == "classed"
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

    request = asked[-1]
    assert request.provider == "wcs"
    assert "identifier=mrlc_display:NLCD_2021_Land_Cover_L48" in request.uri
    assert request.mode == "materialise"
    assert request.bbox == q
    assert request.resolution_m == 30.0

    cog = next(iter(store.values()))
    with MemoryFile(cog) as mem, mem.open() as ds:
        assert str(ds.dtypes[0]) == "uint8"
        assert ds.nodata == 255
        arr = ds.read(1)
        # Background (0) folded into the dataset's nodata (255); classes kept.
        assert 0 not in np.unique(arr)
        assert {11, 21, 41, 82, 95} & set(np.unique(arr).tolist())
