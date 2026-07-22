"""Unit tests for the ``fetch_sentinel2_truecolor`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with expected metadata.
- bbox validation: degenerate / out-of-range / too-large -> typed errors
  (input-validation).
- ``_truecolor_from_bands`` compute / shape correctness on synthetic uint16
  bands: stretches a known dynamic range to a full 0..255 uint8 RGB, masks the
  SCL cloud classes to black, and preserves the (3, H, W) shape.
- Honest-empty path: an all-cloud SCL window raises S2TrueColorNoImageryError
  (no clear pixels -> never fabricate); and an empty STAC search likewise raises
  it and is NOT retryable.
- The published style preset is a multiband-passthrough token, NOT a single-band
  registry entry (so publish_layer renders the baked RGB directly).
- Mocked PC STAC + synthetic source bands: round-trips to a cached 3-band uint8
  RGB COG.

Network is fully mocked: the PC STAC search + SAS sign are patched and the
source assets are local in-memory COGs read through GDAL (no Azure blob fetch).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.cache import compute_cache_key
from grace2_agent.tools import fetch_sentinel2_truecolor as s2_mod
from grace2_agent.tools.fetch_sentinel2_truecolor import (
    S2TrueColorBboxError,
    S2TrueColorNoImageryError,
    _truecolor_from_bands,
    estimate_payload_mb,
    fetch_sentinel2_truecolor,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Small AOI well inside the 0.5 deg^2 guardrail (Imperial Valley CA).
_BBOX = (-115.60, 33.00, -115.50, 33.08)
_DT = "2024-06-01/2024-09-30"


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from grace2_agent.tools.cache import (
        CACHE_BUCKET,
        ReadThroughResult,
        cache_path,
        compute_cache_key as ck,
        is_cacheable,
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


def _write_band_cog(bbox, value_array, dtype="uint16") -> str:
    """Write a tiny single-band EPSG:4326 GeoTIFF covering ``bbox``.

    Local stand-in for a signed Azure-blob Sentinel-2 band asset; the tool opens
    ``/vsicurl/<href>`` and we patch rasterio.open so the local path opens.
    """
    import rasterio

    h, w = value_array.shape
    transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="grace2_s2tc_src_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype=dtype,
        crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(value_array.astype(dtype), 1)
    return path


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_sentinel2_truecolor" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_sentinel2_truecolor"].metadata
    assert meta.name == "fetch_sentinel2_truecolor"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "s2_truecolor"
    assert meta.cacheable is True
    assert meta.supports_global_query is False


def test_style_preset_is_multiband_passthrough_token() -> None:
    """``s2_truecolor`` must NOT resolve in the single-band registry  --  RGB COGs
    go through the multiband passthrough (baked colors render directly)."""
    from grace2_agent.tools.publish_layer import _registry_style_params

    assert _registry_style_params("s2_truecolor") is None


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(-115.6, 33.0, -115.5, 33.1))
    big = estimate_payload_mb(bbox=(-115.6, 33.0, -115.3, 33.3))
    assert big > small
    # No-bbox default is finite and positive.
    assert estimate_payload_mb() > 0


# ---------------------------------------------------------------------------
# bbox validation (input-validation test).
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(S2TrueColorBboxError):
        fetch_sentinel2_truecolor(bbox=(-115.5, 33.0, -115.5, 33.0))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(S2TrueColorBboxError, match="lat out of"):
        fetch_sentinel2_truecolor(bbox=(-115.6, -91.0, -115.5, 33.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(S2TrueColorBboxError, match="guardrail"):
        fetch_sentinel2_truecolor(bbox=(-116.0, 32.0, -114.0, 33.0))  # 2 deg^2 >> 0.5


def test_malformed_bbox_length_raises() -> None:
    with pytest.raises(S2TrueColorBboxError, match="must be"):
        fetch_sentinel2_truecolor(bbox=(-115.6, 33.0, -115.5))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Compute / shape correctness of the cloud-mask + stretch kernel.
# ---------------------------------------------------------------------------


def test_truecolor_stretch_and_shape() -> None:
    """A clear (SCL=4 vegetation) window with a known dynamic range stretches to
    a full 0..255 uint8 RGB of shape (3, H, W)."""
    h, w = 4, 5
    # Reflectance gradient 1000..5000 across the row, same per band.
    base = np.linspace(1000, 5000, h * w).reshape(h, w).astype("float32")
    scl = np.full((h, w), 4.0, dtype="float32")  # all clear (vegetation)

    rgb = _truecolor_from_bands(base.copy(), base.copy(), base.copy(), scl)

    assert rgb.shape == (3, h, w)
    assert rgb.dtype == np.uint8
    # 2nd..98th percentile stretch over a linear ramp lands min near 0, max near
    # 255 (clipped). Endpoints clip to the extremes.
    assert int(rgb.min()) == 0
    assert int(rgb.max()) == 255


def test_truecolor_masks_cloud_pixels_to_black() -> None:
    """SCL cloud-class pixels (9 = cloud high prob) are zeroed in every band."""
    h, w = 3, 4
    base = np.full((h, w), 2500.0, dtype="float32")
    # Give a slight spread so the stretch is non-degenerate.
    base[0, 0] = 1200.0
    base[-1, -1] = 4200.0
    scl = np.full((h, w), 5.0, dtype="float32")  # bare soil (clear)
    scl[1, 1] = 9.0  # cloud, high probability -> must be masked

    rgb = _truecolor_from_bands(base.copy(), base.copy(), base.copy(), scl)

    assert rgb[0, 1, 1] == 0 and rgb[1, 1, 1] == 0 and rgb[2, 1, 1] == 0
    # A clear pixel is NOT forced to black.
    assert rgb[:, 0, 1].sum() > 0


def test_truecolor_all_cloud_raises_no_imagery() -> None:
    """Honest-empty path: every pixel masked (all cloud) -> typed no-imagery."""
    h, w = 3, 3
    base = np.full((h, w), 2500.0, dtype="float32")
    scl = np.full((h, w), 9.0, dtype="float32")  # all cloud high-prob
    with pytest.raises(S2TrueColorNoImageryError):
        _truecolor_from_bands(base, base, base, scl)


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + local source bands).
# ---------------------------------------------------------------------------


def test_happy_path_roundtrips_to_rgb_cog() -> None:
    import rasterio
    from rasterio.io import MemoryFile

    # Source grid for the 4 bands at the AOI; values give a usable stretch.
    sh, sw = 24, 32
    red = np.linspace(800, 4500, sh * sw).reshape(sh, sw)
    green = np.linspace(900, 4200, sh * sw).reshape(sh, sw)
    blue = np.linspace(1000, 3900, sh * sw).reshape(sh, sw)
    scl = np.full((sh, sw), 5.0)  # all clear (bare soil)

    red_p = _write_band_cog(_BBOX, red)
    green_p = _write_band_cog(_BBOX, green)
    blue_p = _write_band_cog(_BBOX, blue)
    scl_p = _write_band_cog(_BBOX, scl)
    paths = [red_p, green_p, blue_p, scl_p]
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = SimpleNamespace(
            id="s2_fake",
            properties={"eo:cloud_cover": 1.2},
            assets={
                "B04": SimpleNamespace(href=red_p),
                "B03": SimpleNamespace(href=green_p),
                "B02": SimpleNamespace(href=blue_p),
                "SCL": SimpleNamespace(href=scl_p),
            },
        )

        real_open = rasterio.open

        def open_side_effect(p, *a, **k):
            if isinstance(p, str) and p.startswith("/vsicurl/"):
                p = p[len("/vsicurl/"):]
            return real_open(p, *a, **k)

        with patch.object(s2_mod._pc_stac, "search_least_cloudy_item", return_value=item), \
             patch.object(s2_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(s2_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=open_side_effect):
            layer = fetch_sentinel2_truecolor(bbox=_BBOX, start_date="2024-06-01", end_date="2024-09-30")

        assert layer.layer_type == "raster"
        assert layer.role == "context"
        assert layer.style_preset == "s2_truecolor"
        assert layer.uri.startswith("s3://")
        assert layer.name == "Sentinel-2 True Color"

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            assert out.count == 3
            assert str(out.dtypes[0]) == "uint8"
            # A real stretched RGB spans most of the 0..255 range.
            assert int(out.read(1).max()) > 200
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# No-imagery honesty (STAC search path).
# ---------------------------------------------------------------------------


def test_no_imagery_raises_typed_error() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    def raise_no_items(**kw):
        raise s2_mod._pc_stac.PCStacNoItemsError("no items")

    with patch.object(s2_mod._pc_stac, "search_least_cloudy_item", side_effect=raise_no_items), \
         patch.object(s2_mod, "read_through", rt):
        with pytest.raises(S2TrueColorNoImageryError):
            fetch_sentinel2_truecolor(bbox=_BBOX, start_date="2024-06-01", end_date="2024-09-30")


def test_no_imagery_error_not_retryable() -> None:
    try:
        raise S2TrueColorNoImageryError("x")
    except S2TrueColorNoImageryError as exc:
        assert exc.retryable is False


# ---------------------------------------------------------------------------
# Cache-key determinism.
# ---------------------------------------------------------------------------


def test_distinct_window_distinct_cache_key() -> None:
    p1 = {"bbox": list(_BBOX), "datetime_range": _DT, "max_cloud_cover": 30.0,
          "collection": "sentinel-2-l2a"}
    p2 = {"bbox": list(_BBOX), "datetime_range": "2023-06-01/2023-09-30",
          "max_cloud_cover": 30.0, "collection": "sentinel-2-l2a"}
    k1 = compute_cache_key("s2_truecolor", p1, "static-30d", now=_PINNED_NOW)
    k2 = compute_cache_key("s2_truecolor", p2, "static-30d", now=_PINNED_NOW)
    assert k1 != k2
