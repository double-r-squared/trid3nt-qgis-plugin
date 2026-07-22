"""Unit tests for the ``fetch_esri_landcover_10m`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with the expected metadata.
- bbox validation: degenerate / out-of-range / too-large / malformed -> typed
  ``EsriLandcoverBboxError`` (input-validation).
- year validation: out-of-range / non-integer -> typed ``EsriLandcoverYearError``;
  ``None`` defaults to the latest vintage.
- Happy path on a SYNTHETIC local source tile (with an embedded GDAL color
  table): the tool warps+window-reads it, mosaics, and round-trips to a cached
  single-band uint8 palette COG that preserves the color table (so
  publish_layer's categorical passthrough colorizes it).
- Multi-tile mosaic: a second tile fills the nodata holes left by the first
  (first-non-nodata-wins merge).
- Honest-empty paths: an empty STAC search -> ``EsriLandcoverNoCoverageError``
  (NOT retryable); an all-nodata mosaic -> same typed error (never fabricate).
- The published style preset is the categorical land-cover token shared with
  NLCD ``fetch_landcover``.
- Cache-key determinism across distinct years.

Network is fully mocked: the PC STAC search + SAS sign are patched and the
source tiles are local in-memory COGs read through GDAL (no Azure blob fetch).
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
from trid3nt_server.tools import fetch_esri_landcover_10m as lc_mod
from trid3nt_server.tools.fetch_esri_landcover_10m import (
    EsriLandcoverBboxError,
    EsriLandcoverNoCoverageError,
    EsriLandcoverYearError,
    estimate_payload_mb,
    fetch_esri_landcover_10m,
)

_PINNED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)

# Small non-US AOI well inside the 0.5 deg^2 guardrail (Nairobi, Kenya).
_BBOX = (36.75, -1.40, 36.95, -1.20)

#: Official io-lulc class colors (subset) used to stamp synthetic source tiles.
_PALETTE = {
    0: (0, 0, 0, 0),
    1: (65, 155, 223, 255),  # Water
    2: (57, 125, 73, 255),  # Trees
    5: (228, 150, 53, 255),  # Crops
    7: (196, 40, 27, 255),  # Built area
    11: (227, 226, 195, 255),  # Rangeland
}


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
        key = ck(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _write_class_cog(bbox, class_array, *, with_palette: bool = True) -> str:
    """Write a tiny single-band uint8 EPSG:4326 GeoTIFF of class codes for ``bbox``.

    Local stand-in for a signed Azure-blob io-lulc ``data`` asset; the tool opens
    ``/vsicurl/<href>`` and we patch rasterio.open so the local path opens.
    Embeds the official-color palette on band 1 so the color-table-preservation
    path is exercised.
    """
    import rasterio

    h, w = class_array.shape
    transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_esri_lc_src_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype="uint8",
        crs="EPSG:4326", transform=transform, nodata=0,
    ) as dst:
        dst.write(class_array.astype("uint8"), 1)
        if with_palette:
            # write_colormap stamps the GDAL color table on band 1; on a GTiff
            # this already sets palette photometric interpretation, so we do NOT
            # also force colorinterp (GDAL rejects that tag edit mid-write).
            dst.write_colormap(1, _PALETTE)
    return path


def _fake_item(href: str, bbox, year: int):
    return SimpleNamespace(
        id=f"io_fake_{year}",
        bbox=list(bbox),
        properties={"start_datetime": f"{year}-01-01T00:00:00Z"},
        assets={"data": SimpleNamespace(href=href)},
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
    assert "fetch_esri_landcover_10m" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_esri_landcover_10m"].metadata
    assert meta.name == "fetch_esri_landcover_10m"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "esri_landcover_10m"
    assert meta.cacheable is True
    assert meta.supports_global_query is False


def test_style_preset_is_categorical_landcover() -> None:
    """Output uses the categorical land-cover family token (embedded-palette
    passthrough  --  publish_layer colorizes from the baked table, no rescale)."""
    assert lc_mod._STYLE_PRESET == "categorical_landcover"


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(36.75, -1.40, 36.85, -1.30))
    big = estimate_payload_mb(bbox=(36.75, -1.40, 37.05, -1.10))
    assert big > small
    assert estimate_payload_mb() > 0


# ---------------------------------------------------------------------------
# bbox validation (input-validation test).
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(EsriLandcoverBboxError):
        fetch_esri_landcover_10m(bbox=(36.85, -1.30, 36.85, -1.30))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(EsriLandcoverBboxError, match="lat out of"):
        fetch_esri_landcover_10m(bbox=(36.75, -91.0, 36.95, -1.20))


def test_too_large_bbox_raises() -> None:
    # 27 deg^2 >> 8 deg^2 ceiling; error must mention fetch_landcover.
    with pytest.raises(EsriLandcoverBboxError, match="ceiling"):
        fetch_esri_landcover_10m(bbox=(36.0, -6.0, 42.0, -1.5))  # 6 * 4.5 = 27 deg^2


def test_malformed_bbox_length_raises() -> None:
    with pytest.raises(EsriLandcoverBboxError, match="must be"):
        fetch_esri_landcover_10m(bbox=(36.75, -1.40, 36.95))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# year validation.
# ---------------------------------------------------------------------------


def test_out_of_range_year_raises() -> None:
    with pytest.raises(EsriLandcoverYearError, match="outside"):
        fetch_esri_landcover_10m(bbox=_BBOX, year=2099)


def test_non_integer_year_raises() -> None:
    with pytest.raises(EsriLandcoverYearError, match="must be an integer"):
        fetch_esri_landcover_10m(bbox=_BBOX, year="not-a-year")


def test_year_none_defaults_to_latest() -> None:
    assert lc_mod._resolve_year(None) == lc_mod._MAX_YEAR
    # string-coercible years are accepted
    assert lc_mod._resolve_year("2020") == 2020


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + local source tile).
# ---------------------------------------------------------------------------


def test_happy_path_roundtrips_to_palette_cog() -> None:
    import rasterio
    from rasterio.io import MemoryFile

    sh, sw = 40, 40
    # A patch of mixed classes (Built / Trees / Crops / Water).
    classes = np.full((sh, sw), 7, dtype="uint8")  # Built
    classes[:20, :20] = 2  # Trees
    classes[20:, :20] = 5  # Crops
    classes[:20, 20:] = 1  # Water
    src = _write_class_cog(_BBOX, classes)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item(src, _BBOX, 2023)
        real_open = rasterio.open

        with patch.object(lc_mod, "_select_items", return_value=[item]), \
             patch.object(lc_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(lc_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            layer = fetch_esri_landcover_10m(bbox=_BBOX, year=2023)

        assert layer.layer_type == "raster"
        assert layer.role == "input"
        assert layer.style_preset == "categorical_landcover"
        assert layer.units == "esri_io_lulc_class_code"
        assert layer.uri.startswith("s3://")
        assert layer.name == "Esri 10m Land Cover (2023)"
        assert layer.bbox is not None

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            assert out.count == 1
            assert str(out.dtypes[0]) == "uint8"
            # The embedded palette is preserved so it colorizes downstream.
            cmap = out.colormap(1)
            assert cmap[1][:3] == (65, 155, 223)  # Water blue
            assert cmap[7][:3] == (196, 40, 27)  # Built red
            # The class codes survived the warp/mosaic round-trip.
            present = set(np.unique(out.read(1)).tolist())
            assert {1, 2, 5, 7} <= present
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def test_multi_tile_mosaic_fills_nodata() -> None:
    """A second tile fills the nodata holes of the first (first-non-nodata wins)."""
    import rasterio
    from rasterio.io import MemoryFile

    sh, sw = 30, 30
    # Tile A: Built on the left half, NODATA on the right half.
    a = np.zeros((sh, sw), dtype="uint8")
    a[:, : sw // 2] = 7
    # Tile B: Crops everywhere (fills A's right-half holes; A wins on the left).
    b = np.full((sh, sw), 5, dtype="uint8")
    src_a = _write_class_cog(_BBOX, a)
    src_b = _write_class_cog(_BBOX, b)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        items = [_fake_item(src_a, _BBOX, 2023), _fake_item(src_b, _BBOX, 2023)]
        real_open = rasterio.open

        with patch.object(lc_mod, "_select_items", return_value=items), \
             patch.object(lc_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(lc_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            fetch_esri_landcover_10m(bbox=_BBOX, year=2023)

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            arr = out.read(1)
            # No nodata remains (B filled A's holes).
            assert int((arr == 0).sum()) == 0
            present = set(np.unique(arr).tolist())
            assert 7 in present  # Built (from A, left half)
            assert 5 in present  # Crops (from B, filled right half)
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
        # Mirror _select_items: zero items -> honest no-coverage.
        raise lc_mod.EsriLandcoverNoCoverageError("no item")

    with patch.object(lc_mod, "_select_items", side_effect=empty_search), \
         patch.object(lc_mod, "read_through", rt):
        with pytest.raises(EsriLandcoverNoCoverageError):
            fetch_esri_landcover_10m(bbox=_BBOX, year=2023)


def test_all_nodata_mosaic_raises_typed_error() -> None:
    """Items exist but every pixel is nodata -> honest no-coverage (no fabricate)."""
    import rasterio

    sh, sw = 16, 16
    blank = np.zeros((sh, sw), dtype="uint8")  # all nodata (0)
    src = _write_class_cog(_BBOX, blank)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item(src, _BBOX, 2023)
        real_open = rasterio.open

        with patch.object(lc_mod, "_select_items", return_value=[item]), \
             patch.object(lc_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(lc_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            with pytest.raises(EsriLandcoverNoCoverageError, match="no-data"):
                fetch_esri_landcover_10m(bbox=_BBOX, year=2023)
    finally:
        try:
            os.unlink(src)
        except OSError:
            pass


def test_no_coverage_error_not_retryable() -> None:
    try:
        raise EsriLandcoverNoCoverageError("x")
    except EsriLandcoverNoCoverageError as exc:
        assert exc.retryable is False


# ---------------------------------------------------------------------------
# Cache-key determinism.
# ---------------------------------------------------------------------------


def test_distinct_year_distinct_cache_key() -> None:
    p1 = {"bbox": list(_BBOX), "year": 2023, "collection": "io-lulc-annual-v02"}
    p2 = {"bbox": list(_BBOX), "year": 2020, "collection": "io-lulc-annual-v02"}
    k1 = compute_cache_key("esri_landcover_10m", p1, "static-30d", now=_PINNED_NOW)
    k2 = compute_cache_key("esri_landcover_10m", p2, "static-30d", now=_PINNED_NOW)
    assert k1 != k2


# ---------------------------------------------------------------------------
# Tile-grid planning.
# ---------------------------------------------------------------------------


def test_tile_grid_small_bbox_single_tile() -> None:
    """A bbox within the tile cap produces exactly one tile equal to the input."""
    from trid3nt_server.tools.fetch_esri_landcover_10m import _plan_tile_grid, _TILE_DEG2

    # _BBOX is 0.2 * 0.2 = 0.04 deg^2, well under 0.5 cap.
    tiles = _plan_tile_grid(_BBOX, tile_deg2=_TILE_DEG2)
    assert len(tiles) == 1
    assert tiles[0] == _BBOX


def test_tile_grid_klickitat_county_no_raise() -> None:
    """Klickitat County WA (~0.767 deg^2) must plan a grid without raising.

    This was the live-failure bbox that originally hit the 0.5 deg^2 guard.
    """
    # Approximate Klickitat County WA bbox (0.767 deg^2).
    bbox = (-121.2, 45.6, -120.3, 46.45)
    area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    assert area > 0.5, "test setup: bbox must exceed the old 0.5 cap"
    assert area < 8.0, "test setup: bbox must be within new 8.0 ceiling"

    from trid3nt_server.tools.fetch_esri_landcover_10m import _plan_tile_grid, _TILE_DEG2

    tiles = _plan_tile_grid(bbox, tile_deg2=_TILE_DEG2)
    assert len(tiles) > 1, "bbox above tile cap must produce multiple sub-tiles"
    # No tile should exceed the cap.
    for t in tiles:
        t_area = (t[2] - t[0]) * (t[3] - t[1])
        assert t_area <= _TILE_DEG2 + 1e-9, (
            f"sub-tile {t} area {t_area:.4f} exceeds tile cap {_TILE_DEG2}"
        )


def test_tile_grid_coverage_and_no_gaps() -> None:
    """The union area of all sub-tiles equals the total bbox area (no gaps)."""
    from trid3nt_server.tools.fetch_esri_landcover_10m import _plan_tile_grid

    # A ~3 deg^2 bbox (6 tiles expected at 0.5 cap).
    bbox = (-121.5, 45.0, -119.5, 46.5)
    total_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    tiles = _plan_tile_grid(bbox, tile_deg2=0.5)

    # Sum of tile areas must equal total area (within float precision).
    tile_area_sum = sum((t[2] - t[0]) * (t[3] - t[1]) for t in tiles)
    assert abs(tile_area_sum - total_area) < 1e-9, (
        f"Tile area sum {tile_area_sum:.6f} != total area {total_area:.6f}"
    )

    # Tiles must cover the full bbox extents (min/max across all tiles).
    assert min(t[0] for t in tiles) == pytest.approx(bbox[0])
    assert min(t[1] for t in tiles) == pytest.approx(bbox[1])
    assert max(t[2] for t in tiles) == pytest.approx(bbox[2])
    assert max(t[3] for t in tiles) == pytest.approx(bbox[3])


def test_above_8_deg2_raises_with_nlcd_recommendation() -> None:
    """Bboxes above 8 deg^2 must raise EsriLandcoverBboxError mentioning fetch_landcover."""
    # 27 deg^2 bbox.
    with pytest.raises(EsriLandcoverBboxError, match="fetch_landcover"):
        fetch_esri_landcover_10m(bbox=(36.0, -6.0, 42.0, -1.5))


# ---------------------------------------------------------------------------
# Tiled mosaic: dtype and colormap preservation.
# ---------------------------------------------------------------------------


def test_tiled_mosaic_preserves_dtype_and_colormap() -> None:
    """A multi-sub-tile fetch must produce uint8 output with an embedded colormap.

    We mock _plan_tile_grid to return two sub-bboxes covering the same _BBOX
    (halved along longitude), patch _select_items + sas_sign_href so each sub-tile
    gets a synthetic local source COG with a palette, then verify the merged output
    is uint8 and carries the colormap (not grey).
    """
    import rasterio
    from rasterio.io import MemoryFile
    from unittest.mock import patch

    # Two half-bboxes that together equal _BBOX.
    mid_lon = (_BBOX[0] + _BBOX[2]) / 2.0
    half_a = (_BBOX[0], _BBOX[1], mid_lon, _BBOX[3])
    half_b = (mid_lon, _BBOX[1], _BBOX[2], _BBOX[3])

    sh, sw = 20, 10
    # Half A: Trees.
    arr_a = np.full((sh, sw), 2, dtype="uint8")
    # Half B: Crops.
    arr_b = np.full((sh, sw), 5, dtype="uint8")
    src_a = _write_class_cog(half_a, arr_a)
    src_b = _write_class_cog(half_b, arr_b)

    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item_a = _fake_item(src_a, half_a, 2022)
        item_b = _fake_item(src_b, half_b, 2022)
        real_open = rasterio.open

        def fake_select_items(bbox, year):
            if abs(bbox[0] - half_a[0]) < 1e-6:
                return [item_a]
            return [item_b]

        with patch.object(lc_mod, "_plan_tile_grid", return_value=[half_a, half_b]), \
             patch.object(lc_mod, "_select_items", side_effect=fake_select_items), \
             patch.object(lc_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(lc_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_open_local_vsicurl(real_open)):
            layer = fetch_esri_landcover_10m(bbox=_BBOX, year=2022)

        assert layer.style_preset == "categorical_landcover"

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            # dtype must be uint8 (categorical codes never interpolated).
            assert str(out.dtypes[0]) == "uint8"
            # Colormap must be embedded (not grey output).
            cmap = out.colormap(1)
            assert cmap[2][:3] == (57, 125, 73)   # Trees green
            assert cmap[5][:3] == (228, 150, 53)  # Crops orange
    finally:
        for p in (src_a, src_b):
            try:
                os.unlink(p)
            except OSError:
                pass
