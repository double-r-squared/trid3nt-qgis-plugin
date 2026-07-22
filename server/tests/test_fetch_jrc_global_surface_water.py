"""Unit tests for the ``fetch_jrc_global_surface_water`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with the expected metadata.
- bbox validation: degenerate / out-of-range / too-large / malformed -> typed
  ``JrcSurfaceWaterBboxError`` (input-validation).
- band validation: unknown band -> typed ``JrcSurfaceWaterBandError``; ``None``
  defaults to ``occurrence``.
- Happy path on a SYNTHETIC local source tile: the tool warps+window-reads it,
  mosaics, and round-trips to a cached single-band uint8 COG that carries a
  band-appropriate embedded GDAL color table (so publish_layer's embedded-palette
  passthrough colorizes it without touching the single-band style registry). The
  ``change`` band uses 253 as nodata; every other band uses 0.
- Multi-tile mosaic: a second tile fills the nodata holes of the first
  (first-valid-pixel-wins merge).
- Honest-empty paths: an empty STAC search -> ``JrcSurfaceWaterNoCoverageError``
  (NOT retryable); an all-nodata mosaic (a dry / ocean AOI) -> same typed error
  (never fabricate).
- Per-band style tokens + units are wired onto the returned LayerURI.
- Cache-key determinism across distinct bands.

Network is fully mocked: the PC STAC search + the /sign endpoint are patched and
the source tiles are local in-memory COGs read through GDAL (no Azure blob fetch).
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools import fetch_jrc_global_surface_water as gsw_mod
from trid3nt_server.tools.fetch_jrc_global_surface_water import (
    JrcSurfaceWaterBandError,
    JrcSurfaceWaterBboxError,
    JrcSurfaceWaterNoCoverageError,
    estimate_payload_mb,
    fetch_jrc_global_surface_water,
)

# Small AOI well inside the 2.0 deg^2 guardrail (Mississippi floodplain, LA).
_BBOX = (-91.30, 30.30, -91.00, 30.55)


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from trid3nt_server.tools.cache import (
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
        key = ck(source_id, params, metadata.ttl_class)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _write_value_cog(bbox, value_array, *, nodata: int) -> str:
    """Write a tiny single-band uint8 EPSG:4326 GeoTIFF for ``bbox``.

    Local stand-in for a signed Azure-blob jrc-gsw band asset; the tool opens
    ``/vsicurl/<href>`` and we patch rasterio.open so the local path opens.
    """
    import rasterio

    h, w = value_array.shape
    transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_jrc_gsw_src_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype="uint8",
        crs="EPSG:4326", transform=transform, nodata=nodata,
    ) as dst:
        dst.write(value_array.astype("uint8"), 1)
    return path


def _fake_item(href: str, bbox, band: str):
    return SimpleNamespace(
        id="jrc_fake_100W_40N",
        bbox=list(bbox),
        assets={band: SimpleNamespace(href=href)},
    )


def _open_local_vsicurl(real_open):
    def open_side_effect(p, *a, **k):
        if isinstance(p, str) and p.startswith("/vsicurl/"):
            p = p[len("/vsicurl/"):]
        return real_open(p, *a, **k)

    return open_side_effect


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_jrc_global_surface_water" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_jrc_global_surface_water"].metadata
    assert meta.name == "fetch_jrc_global_surface_water"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "jrc_global_surface_water"
    assert meta.cacheable is True
    assert meta.supports_global_query is False


def test_band_specs_and_default() -> None:
    assert gsw_mod._DEFAULT_BAND == "occurrence"
    assert set(gsw_mod._BANDS) == {
        "occurrence",
        "recurrence",
        "seasonality",
        "change",
    }
    # change uses the JRC 253 "not water" sentinel as nodata; the rest use 0.
    assert gsw_mod._BANDS["change"]["nodata"] == 253
    assert gsw_mod._BANDS["occurrence"]["nodata"] == 0


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(-91.30, 30.30, -91.20, 30.40))
    big = estimate_payload_mb(bbox=(-91.30, 30.30, -90.80, 30.80))
    assert big > small
    assert estimate_payload_mb() > 0


# ---------------------------------------------------------------------------
# bbox validation (input-validation test).
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(JrcSurfaceWaterBboxError):
        fetch_jrc_global_surface_water(bbox=(-91.0, 30.0, -91.0, 30.0))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(JrcSurfaceWaterBboxError, match="lat out of"):
        fetch_jrc_global_surface_water(bbox=(-91.30, -91.0, -91.00, 30.55))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(JrcSurfaceWaterBboxError, match="guardrail"):
        fetch_jrc_global_surface_water(bbox=(-92.0, 29.0, -89.0, 31.0))  # 6 deg^2


def test_malformed_bbox_length_raises() -> None:
    with pytest.raises(JrcSurfaceWaterBboxError, match="must be"):
        fetch_jrc_global_surface_water(bbox=(-91.30, 30.30, -91.00))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# band validation.
# ---------------------------------------------------------------------------


def test_unknown_band_raises() -> None:
    with pytest.raises(JrcSurfaceWaterBandError, match="must be one of"):
        fetch_jrc_global_surface_water(bbox=_BBOX, band="salinity")


def test_band_none_defaults_to_occurrence() -> None:
    assert gsw_mod._resolve_band(None) == "occurrence"
    # case-insensitive normalization
    assert gsw_mod._resolve_band("OCCURRENCE") == "occurrence"
    assert gsw_mod._resolve_band(" Change ") == "change"


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + local source tile).
# ---------------------------------------------------------------------------


def test_happy_path_roundtrips_to_palette_cog() -> None:
    import rasterio
    from rasterio.io import MemoryFile

    sh, sw = 40, 40
    # Occurrence patch: half permanent water (100), half no-water (0=nodata).
    vals = np.zeros((sh, sw), dtype="uint8")
    vals[:, :20] = 100  # permanent water
    vals[:20, 20:] = 40  # seasonal water
    src = _write_value_cog(_BBOX, vals, nodata=0)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item(src, _BBOX, "occurrence")
        real_open = rasterio.open

        with patch.object(gsw_mod, "_select_items", return_value=[item]), \
             patch.object(gsw_mod, "_sign_href", side_effect=lambda href: href), \
             patch.object(gsw_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            layer = fetch_jrc_global_surface_water(bbox=_BBOX, band="occurrence")

        assert layer.layer_type == "raster"
        assert layer.role == "input"
        assert layer.style_preset == "water_occurrence_pct"
        assert layer.units == "percent_of_time_water_1984_2021"
        assert layer.uri.startswith("s3://")
        assert layer.name == "JRC Global Surface Water (Water occurrence)"
        assert layer.bbox is not None

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            assert out.count == 1
            assert str(out.dtypes[0]) == "uint8"
            assert out.nodata == 0
            # The band carries a baked blue ramp so publish_layer colorizes it.
            cmap = out.colormap(1)
            assert len(cmap) == 256
            assert cmap[0] == (0, 0, 0, 0)  # nodata transparent
            assert cmap[100][3] == 255  # full occurrence opaque
            # Blue ramp: deeper (higher %) is more blue than lower %.
            assert cmap[100][2] > 0
            # Values survived the warp/mosaic round-trip.
            arr = out.read(1)
            assert int((arr == 100).sum()) > 0
            assert int((arr == 40).sum()) > 0
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def test_change_band_uses_253_nodata_and_diverging_palette() -> None:
    import rasterio
    from rasterio.io import MemoryFile

    sh, sw = 30, 30
    # change: 100 = no change, 50 = loss, 150 = gain, 253 = not water (nodata).
    vals = np.full((sh, sw), 253, dtype="uint8")
    vals[:10, :] = 50   # loss
    vals[10:20, :] = 100  # no change
    vals[20:, :] = 150  # gain
    src = _write_value_cog(_BBOX, vals, nodata=253)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item(src, _BBOX, "change")
        real_open = rasterio.open

        with patch.object(gsw_mod, "_select_items", return_value=[item]), \
             patch.object(gsw_mod, "_sign_href", side_effect=lambda href: href), \
             patch.object(gsw_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            layer = fetch_jrc_global_surface_water(bbox=_BBOX, band="change")

        assert layer.style_preset == "water_change_intensity"
        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            assert out.nodata == 253
            cmap = out.colormap(1)
            assert cmap[253] == (0, 0, 0, 0)  # not-water transparent
            assert cmap[100][3] == 255        # no-change opaque (white-ish)
            # Diverging: loss (50) is reddish, gain (150) is bluish.
            r_loss, _, b_loss, _ = cmap[50]
            r_gain, _, b_gain, _ = cmap[150]
            assert r_loss > b_loss   # loss skews red
            assert b_gain > r_gain   # gain skews blue
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def test_multi_tile_mosaic_fills_nodata() -> None:
    """A second tile fills the nodata holes of the first (first-valid wins)."""
    import rasterio
    from rasterio.io import MemoryFile

    sh, sw = 30, 30
    # Tile A: water (80) on the left half, nodata (0) on the right half.
    a = np.zeros((sh, sw), dtype="uint8")
    a[:, : sw // 2] = 80
    # Tile B: water (30) everywhere (fills A's right-half holes; A wins on left).
    b = np.full((sh, sw), 30, dtype="uint8")
    src_a = _write_value_cog(_BBOX, a, nodata=0)
    src_b = _write_value_cog(_BBOX, b, nodata=0)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        items = [
            _fake_item(src_a, _BBOX, "occurrence"),
            _fake_item(src_b, _BBOX, "occurrence"),
        ]
        real_open = rasterio.open

        with patch.object(gsw_mod, "_select_items", return_value=items), \
             patch.object(gsw_mod, "_sign_href", side_effect=lambda href: href), \
             patch.object(gsw_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            fetch_jrc_global_surface_water(bbox=_BBOX, band="occurrence")

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            arr = out.read(1)
            # No nodata remains (B filled A's holes).
            assert int((arr == 0).sum()) == 0
            present = set(np.unique(arr).tolist())
            assert 80 in present  # from A (left half)
            assert 30 in present  # from B (filled right half)
    finally:
        for p in (src_a, src_b):
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# No-coverage honesty.
# ---------------------------------------------------------------------------


def test_no_items_raises_typed_error() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    def empty_search(*a, **k):
        raise gsw_mod.JrcSurfaceWaterNoCoverageError("no item")

    with patch.object(gsw_mod, "_select_items", side_effect=empty_search), \
         patch.object(gsw_mod, "read_through", rt):
        with pytest.raises(JrcSurfaceWaterNoCoverageError):
            fetch_jrc_global_surface_water(bbox=_BBOX, band="occurrence")


def test_all_nodata_mosaic_raises_typed_error() -> None:
    """Items exist but every pixel is nodata (a dry / ocean AOI) -> honest error."""
    import rasterio

    sh, sw = 20, 20
    dry = np.zeros((sh, sw), dtype="uint8")  # all nodata=0 (no water ever)
    src = _write_value_cog(_BBOX, dry, nodata=0)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item(src, _BBOX, "occurrence")
        real_open = rasterio.open

        with patch.object(gsw_mod, "_select_items", return_value=[item]), \
             patch.object(gsw_mod, "_sign_href", side_effect=lambda href: href), \
             patch.object(gsw_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            with pytest.raises(JrcSurfaceWaterNoCoverageError, match="no-data"):
                fetch_jrc_global_surface_water(bbox=_BBOX, band="occurrence")
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache-key determinism across bands.
# ---------------------------------------------------------------------------


def test_distinct_bands_have_distinct_cache_keys() -> None:
    from trid3nt_server.tools.cache import compute_cache_key

    base = {"bbox": list(_BBOX), "collection": gsw_mod._COLLECTION}
    k_occ = compute_cache_key(
        "jrc_global_surface_water", {**base, "band": "occurrence"}, "static-30d"
    )
    k_chg = compute_cache_key(
        "jrc_global_surface_water", {**base, "band": "change"}, "static-30d"
    )
    assert k_occ != k_chg
