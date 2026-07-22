"""Unit tests for the ``fetch_naip`` atomic tool (conservation micro-North-Star).

Coverage:
- Registration in TOOL_REGISTRY with expected metadata.
- bbox validation: degenerate / out-of-range / too-large -> typed errors.
- Mocked PC STAC + a synthetic source RGBA COG: round-trips to a cached 3-band
  uint8 RGB COG (the multiband passthrough base layer).
- No-coverage path (US-only): an empty STAC search raises NAIPNoCoverageError
  (honest, not fabricated) and is NOT retryable.
- The published style preset is a multiband-passthrough token, NOT a single-band
  registry entry (so publish_layer renders the baked RGB directly).

Network is fully mocked: the PC STAC search + SAS sign are patched and the
source asset is a local in-memory COG read through GDAL (no Azure blob fetch).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.cache import compute_cache_key
from trid3nt_server.tools import fetch_naip as naip_mod
from trid3nt_server.tools.fetch_naip import (
    NAIPBboxError,
    NAIPNoCoverageError,
    fetch_naip,
    estimate_payload_mb,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Small US AOI inside the sub-meter guardrail.
_US_BBOX = (-80.02, 32.78, -80.0, 32.80)


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


def _write_source_naip_cog(bbox) -> str:
    """Write a tiny 4-band uint8 (R,G,B,NIR) EPSG:4326 GeoTIFF covering ``bbox``.

    Used as the local stand-in for the signed Azure-blob NAIP ``image`` asset:
    the tool opens it via /vsicurl/<href>  --  we patch the href to this local path
    so rasterio reads it directly.
    """
    import rasterio

    w, h = 32, 24
    transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    arr = np.zeros((4, h, w), dtype="uint8")
    arr[0] = 200  # R
    arr[1] = 120  # G
    arr[2] = 60   # B
    arr[3] = 30   # NIR
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_naip_src_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=4, dtype="uint8",
        crs="EPSG:4326", transform=transform,
    ) as dst:
        dst.write(arr)
    return path


def _fake_item_local(local_path: str):
    return SimpleNamespace(
        id="naip_fake",
        properties={},
        assets={"image": SimpleNamespace(href=local_path)},
    )


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_naip" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_naip"].metadata
    assert meta.name == "fetch_naip"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "naip"
    assert meta.cacheable is True


def test_style_preset_is_multiband_passthrough_token() -> None:
    """``naip_rgb`` must NOT resolve in the single-band registry  --  RGB COGs go
    through the multiband passthrough (baked colors render directly)."""
    from trid3nt_server.tools.publish_layer import _registry_style_params

    assert _registry_style_params("naip_rgb") is None


def test_payload_estimator_scales_with_area() -> None:
    # Both above the 1.0 MB floor so the area scaling is observable.
    small = estimate_payload_mb(bbox=(-80.0, 32.0, -79.9, 32.1))
    big = estimate_payload_mb(bbox=(-80.0, 32.0, -79.8, 32.2))
    assert big > small


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(NAIPBboxError):
        fetch_naip(bbox=(-80.0, 32.0, -80.0, 32.0))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(NAIPBboxError, match="lon out of"):
        fetch_naip(bbox=(-200.0, 32.0, -79.0, 33.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(NAIPBboxError, match="guardrail"):
        fetch_naip(bbox=(-81.0, 32.0, -79.0, 33.0))  # 2 deg^2 >> 0.06


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + local source COG).
# ---------------------------------------------------------------------------


def test_naip_happy_path_roundtrips_to_rgb_cog() -> None:
    import rasterio
    from rasterio.io import MemoryFile

    src_path = _write_source_naip_cog(_US_BBOX)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item_local(src_path)

        # The tool opens "/vsicurl/<href>". We patch rasterio.open so a
        # /vsicurl/-prefixed LOCAL path opens the real file directly (capturing
        # the UNPATCHED open first to avoid infinite recursion).
        real_open = rasterio.open

        def open_side_effect(p, *a, **k):
            if isinstance(p, str) and p.startswith("/vsicurl/"):
                p = p[len("/vsicurl/"):]
            return real_open(p, *a, **k)

        with patch.object(naip_mod._pc_stac, "search_least_cloudy_item", return_value=item), \
             patch.object(naip_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(naip_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=open_side_effect):
            layer = fetch_naip(bbox=_US_BBOX)

        assert layer.layer_type == "raster"
        assert layer.role == "context"
        assert layer.style_preset == "naip_rgb"
        assert layer.uri.startswith("s3://")

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            assert out.count == 3
            assert str(out.dtypes[0]) == "uint8"
            r = out.read(1)
            assert int(r.max()) == 200  # the R band constant we wrote
    finally:
        os.unlink(src_path)


# ---------------------------------------------------------------------------
# No-coverage honesty (US-only).
# ---------------------------------------------------------------------------


def test_no_coverage_raises_typed_error() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    def raise_no_items(**kw):
        raise naip_mod._pc_stac.PCStacNoItemsError("no items")

    with patch.object(naip_mod._pc_stac, "search_least_cloudy_item", side_effect=raise_no_items), \
         patch.object(naip_mod, "read_through", rt):
        with pytest.raises(NAIPNoCoverageError):
            fetch_naip(bbox=_US_BBOX)


def test_no_coverage_error_not_retryable() -> None:
    try:
        raise NAIPNoCoverageError("x")
    except NAIPNoCoverageError as exc:
        assert exc.retryable is False


# ---------------------------------------------------------------------------
# Cache-key determinism.
# ---------------------------------------------------------------------------


def test_distinct_bbox_distinct_cache_key() -> None:
    p1 = {"bbox": list(_US_BBOX), "collection": "naip"}
    p2 = {"bbox": [-80.0, 32.0, -79.99, 32.01], "collection": "naip"}
    k1 = compute_cache_key("naip", p1, "static-30d", now=_PINNED_NOW)
    k2 = compute_cache_key("naip", p2, "static-30d", now=_PINNED_NOW)
    assert k1 != k2
