"""Unit tests for the ``compute_ndvi`` atomic tool (conservation micro-North-Star).

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- bbox validation: degenerate / out-of-range / non-finite / too-large -> typed.
- Mocked PC STAC + band reads: a synthetic Red/NIR pair round-trips to a cached
  single-band float32 NDVI COG with the right values (-1..1) and style preset.
- No-imagery path: an empty STAC search raises NDVINoImageryError (honest, not
  fabricated) and is NOT retryable.
- Cache-key determinism: different bbox / window -> different cache keys; a
  cache hit on the second identical call does not re-invoke the fetcher.
- Style preset registry: the ``ndvi`` preset resolves to a real rescale+colormap.

Network is fully mocked: ``_pc_stac`` search/sign + the per-band window reader
are patched so no real Sentinel-2 scene is fetched.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.cache import compute_cache_key
from trid3nt_server.tools import compute_ndvi as ndvi_mod
from trid3nt_server.tools.compute_ndvi import (
    _METADATA,
    _STYLE_PRESET,
    NDVIBboxError,
    NDVINoImageryError,
    compute_ndvi,
    estimate_payload_mb,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Charleston, SC area (SC-DNR territory)  --  small AOI inside the guardrail.
_SC_BBOX = (-80.05, 32.75, -79.95, 32.82)


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors sibling test pattern).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key as ck,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _fake_item(scene_id: str = "S2_fake", cc: float = 1.0):
    """A minimal STAC-like item with B04/B08 assets."""
    return SimpleNamespace(
        id=scene_id,
        properties={"eo:cloud_cover": cc},
        assets={
            "B04": SimpleNamespace(href="https://blob/red.tif"),
            "B08": SimpleNamespace(href="https://blob/nir.tif"),
        },
    )


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "compute_ndvi" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["compute_ndvi"].metadata
    assert meta.name == "compute_ndvi"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "ndvi"
    assert meta.cacheable is True
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_style_preset_is_in_titiler_registry() -> None:
    """The ndvi preset must resolve to a real (rescale, colormap) so a
    single-band NDVI COG is never published as bare grayscale."""
    from trid3nt_server.tools.publish_layer import _registry_style_params

    params = _registry_style_params(_STYLE_PRESET)
    assert params is not None
    assert "rescale=-1,1" in params
    assert "colormap_name=rdylgn" in params


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(-80.0, 32.0, -79.99, 32.01))
    big = estimate_payload_mb(bbox=(-80.0, 32.0, -79.5, 32.5))
    assert big > small
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(NDVIBboxError):
        compute_ndvi(bbox=(-80.0, 32.0, -80.0, 32.0))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(NDVIBboxError, match="lon out of"):
        compute_ndvi(bbox=(-200.0, 32.0, -79.0, 33.0))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(NDVIBboxError, match="non-finite"):
        compute_ndvi(bbox=(float("nan"), 32.0, -79.0, 33.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(NDVIBboxError, match="guardrail"):
        compute_ndvi(bbox=(-82.0, 30.0, -79.0, 33.0))  # 9 deg^2


def test_county_ish_bbox_does_not_raise_validation() -> None:
    """NATE 2026-06-26: a ~0.77 deg^2 county-ish AOI must NOT be rejected by the
    bbox guardrail -- the 4096px grid clamp auto-coarsens it (effective cell
    ~= bbox_m/4096) so the COG stays bounded. The old 0.5 deg^2 cap wrongly
    rejected it with no recourse. _validate_bbox proceeds (does not raise)."""
    # (-80.44, 32.56, -79.56, 33.44): area = 0.88 * 0.88 = ~0.774 deg^2.
    bbox = (-80.44, 32.56, -79.56, 33.44)
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    assert 0.5 < area <= 1.0  # in the auto-coarsen window, under the new cap
    # Must not raise NDVIBboxError (validation passes for the coarsened AOI).
    ndvi_mod._validate_bbox(bbox)


def test_bbox_error_not_retryable() -> None:
    try:
        compute_ndvi(bbox=(-80.0, 32.0, -80.0, 32.0))
    except NDVIBboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("expected NDVIBboxError")


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + band reads).
# ---------------------------------------------------------------------------


def _patched_band_read(width=20, height=14):
    """Return a fake _read_band_window that yields constant Red/NIR arrays so
    the resulting NDVI is a known constant."""
    def reader(signed_href, bbox, w, h):
        if "red" in signed_href:
            return np.ma.array(np.full((h, w), 1000.0, dtype="float32"))
        return np.ma.array(np.full((h, w), 4000.0, dtype="float32"))
    return reader


def test_ndvi_happy_path_roundtrips_to_cog() -> None:
    """A synthetic Red=1000 / NIR=4000 pair -> NDVI = 0.6 everywhere, written as
    a single-band float32 COG and round-tripped through read_through."""
    import rasterio
    from rasterio.io import MemoryFile

    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    with patch.object(ndvi_mod._pc_stac, "search_least_cloudy_item", return_value=_fake_item()), \
         patch.object(ndvi_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
         patch.object(ndvi_mod, "_read_band_window", _patched_band_read()), \
         patch.object(ndvi_mod, "read_through", rt):
        layer = compute_ndvi(bbox=_SC_BBOX, start_date="2024-04-01", end_date="2024-09-30")

    assert layer.layer_type == "raster"
    assert layer.style_preset == _STYLE_PRESET
    assert layer.role == "primary"
    assert layer.uri.startswith("s3://")
    assert layer.units == "NDVI (-1..1)"

    # Decode the cached COG and confirm NDVI == 0.6 (within float tol).
    cog = next(iter(fake.store.values()))
    with MemoryFile(cog) as mem, mem.open() as src:
        assert src.count == 1
        assert str(src.dtypes[0]) == "float32"
        arr = src.read(1, masked=True)
        valid = arr.compressed()
        assert valid.size > 0
        assert np.allclose(valid, 0.6, atol=1e-4)


def test_cache_hit_does_not_refetch() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)
    reader = _patched_band_read()
    calls = {"n": 0}

    def counting_reader(href, bbox, w, h):
        calls["n"] += 1
        return reader(href, bbox, w, h)

    with patch.object(ndvi_mod._pc_stac, "search_least_cloudy_item", return_value=_fake_item()), \
         patch.object(ndvi_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
         patch.object(ndvi_mod, "_read_band_window", counting_reader), \
         patch.object(ndvi_mod, "read_through", rt):
        compute_ndvi(bbox=_SC_BBOX, start_date="2024-04-01", end_date="2024-09-30")
        first = calls["n"]
        compute_ndvi(bbox=_SC_BBOX, start_date="2024-04-01", end_date="2024-09-30")
        second = calls["n"]

    assert first > 0
    assert second == first, "second identical call must hit the cache, not re-read"


# ---------------------------------------------------------------------------
# No-imagery honesty (data-source fallback norm).
# ---------------------------------------------------------------------------


def test_no_imagery_raises_typed_error() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    def raise_no_items(**kw):
        raise ndvi_mod._pc_stac.PCStacNoItemsError("no items")

    with patch.object(ndvi_mod._pc_stac, "search_least_cloudy_item", side_effect=raise_no_items), \
         patch.object(ndvi_mod, "read_through", rt):
        with pytest.raises(NDVINoImageryError):
            compute_ndvi(bbox=_SC_BBOX, start_date="2024-04-01", end_date="2024-09-30")


def test_no_imagery_error_not_retryable() -> None:
    try:
        raise NDVINoImageryError("x")
    except NDVINoImageryError as exc:
        assert exc.retryable is False


# ---------------------------------------------------------------------------
# Cache-key determinism.
# ---------------------------------------------------------------------------


def test_distinct_bbox_distinct_cache_key() -> None:
    p1 = {"bbox": list(_SC_BBOX), "datetime_range": "a/b", "max_cloud_cover": 30.0, "collection": "sentinel-2-l2a"}
    p2 = {"bbox": [-80.0, 32.0, -79.9, 32.05], "datetime_range": "a/b", "max_cloud_cover": 30.0, "collection": "sentinel-2-l2a"}
    k1 = compute_cache_key("ndvi", p1, "static-30d", now=_PINNED_NOW)
    k2 = compute_cache_key("ndvi", p2, "static-30d", now=_PINNED_NOW)
    assert k1 != k2


def test_distinct_window_distinct_cache_key() -> None:
    base = {"bbox": list(_SC_BBOX), "max_cloud_cover": 30.0, "collection": "sentinel-2-l2a"}
    k1 = compute_cache_key("ndvi", {**base, "datetime_range": "2024-01-01/2024-06-30"}, "static-30d", now=_PINNED_NOW)
    k2 = compute_cache_key("ndvi", {**base, "datetime_range": "2024-07-01/2024-12-31"}, "static-30d", now=_PINNED_NOW)
    assert k1 != k2
