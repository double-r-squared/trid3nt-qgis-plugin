"""Unit tests for the ``fetch_copernicus_dem`` atomic tool.

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- bbox validation: degenerate / out-of-range / non-finite / too-large -> typed
  (non-retryable).
- Mocked PC STAC tile search + per-tile reads: synthetic single-band elevation
  tiles first-wins mosaic to a cached single-band float32 DEM COG with the right
  LayerURI shape (style preset continuous_dem, role input, units meters, bbox).
- Mosaic correctness: two abutting tiles fill the grid; the first-wins rule keeps
  the first tile's value where both cover a cell.
- Honest no-coverage path: an empty STAC search raises CopernicusDemEmptyError
  (honest, not fabricated) and is NOT retryable; an all-nodata window likewise.
- Cache-key determinism: a cache hit on a second identical call does not
  re-invoke the fetcher; a different bbox -> a different cache key.

Network is fully mocked: the pystac Client + the per-href sign endpoint + the
per-tile window reader are patched so no real Copernicus tile is fetched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.terrain import fetch_copernicus_dem as cop_mod
from trid3nt_server.tools.fetchers.terrain.fetch_copernicus_dem import (
    _METADATA,
    _NODATA,
    _STYLE_PRESET,
    CopernicusDemBboxError,
    CopernicusDemEmptyError,
    estimate_payload_mb,
    fetch_copernicus_dem,
)

_PINNED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=timezone.utc)

# A small non-US AOI in the Alps near Zermatt (matches the prototype).
_ALPS_BBOX = (7.60, 45.90, 7.80, 46.05)


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors the sibling test pattern).
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


# ---------------------------------------------------------------------------
# Fake STAC item / client.
# ---------------------------------------------------------------------------


def _fake_item(tile_id: str = "Copernicus_DSM_fake"):
    """A minimal STAC-like item with the single-band ``data`` asset."""
    return SimpleNamespace(
        id=tile_id,
        assets={"data": SimpleNamespace(href=f"https://blob/{tile_id}.tif")},
    )


def _fake_search_client(items):
    """Patchable pystac Client.open that returns a search yielding ``items``."""
    search = SimpleNamespace(items=lambda: list(items))
    client = SimpleNamespace(search=lambda **kw: search)
    return SimpleNamespace(open=staticmethod(lambda root: client))


# ---------------------------------------------------------------------------
# Synthetic tile reader.
# ---------------------------------------------------------------------------


def _make_tile_reader(per_tile: dict[str, float] | None = None, *, nodata_top: int = 0):
    """Return a fake _read_tile_window.

    Emits a constant-elevation tile per tile-id (default 1000 m). ``per_tile``
    maps a substring of the signed href -> the elevation value so distinct tiles
    carry distinct values (mosaic / first-wins assertions). When ``nodata_top`` >
    0, the top ``nodata_top`` rows are NaN so the nodata path is exercised.
    """
    per_tile = per_tile or {}

    def reader(signed_href, bbox, w, h):
        val = 1000.0
        for sub, v in per_tile.items():
            if sub in signed_href:
                val = v
                break
        arr = np.full((h, w), val, dtype="float32")
        if nodata_top > 0:
            arr[:nodata_top, :] = np.nan
        return arr

    return reader


def _run(items, fake, *, per_tile=None, nodata_top=0, bbox=_ALPS_BBOX):
    rt = _make_read_through_injector(fake)
    with patch.object(cop_mod, "read_through", rt), patch.object(
        cop_mod, "_sign_href", side_effect=lambda href: href
    ), patch.object(
        cop_mod, "_read_tile_window", _make_tile_reader(per_tile, nodata_top=nodata_top)
    ), patch(
        "pystac_client.Client", _fake_search_client(items)
    ):
        return fetch_copernicus_dem(bbox=bbox)


def _decode_dem(cog_bytes):
    from rasterio.io import MemoryFile

    with MemoryFile(cog_bytes) as mem, mem.open() as src:
        assert src.count == 1
        assert str(src.dtypes[0]) == "float32"
        return src.read(1), src.nodata  # (H, W) float32


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_copernicus_dem" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_copernicus_dem"].metadata
    assert meta.name == "fetch_copernicus_dem"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "copernicus_dem"
    assert meta.cacheable is True
    assert meta.supports_global_query is False
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_open_world_hint_is_set() -> None:
    spec = TOOL_REGISTRY["fetch_copernicus_dem"]
    annotations = getattr(spec, "annotations", None)
    if annotations is not None:
        assert getattr(annotations, "open_world_hint", None) is True


def test_payload_estimator_scales_with_area() -> None:
    small = estimate_payload_mb(bbox=(7.6, 45.9, 7.61, 45.91))
    big = estimate_payload_mb(bbox=(7.6, 45.9, 8.6, 46.9))
    assert big > small
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# bbox validation.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(CopernicusDemBboxError):
        fetch_copernicus_dem(bbox=(7.6, 45.9, 7.6, 45.9))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(CopernicusDemBboxError, match="lon out of"):
        fetch_copernicus_dem(bbox=(-200.0, 45.9, 7.8, 46.0))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(CopernicusDemBboxError, match="non-finite"):
        fetch_copernicus_dem(bbox=(float("nan"), 45.9, 7.8, 46.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(CopernicusDemBboxError, match="guardrail"):
        fetch_copernicus_dem(bbox=(7.0, 44.0, 10.0, 46.0))  # 6 deg^2 > 4


def test_bbox_error_not_retryable() -> None:
    try:
        fetch_copernicus_dem(bbox=(7.6, 45.9, 7.6, 45.9))
    except CopernicusDemBboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("expected CopernicusDemBboxError")


# ---------------------------------------------------------------------------
# Happy path: single tile + LayerURI shape.
# ---------------------------------------------------------------------------


def test_single_tile_roundtrips_to_dem_cog() -> None:
    fake = _FakeStore()
    layer = _run([_fake_item("tile_a")], fake, per_tile={"tile_a": 2500.0})

    assert layer.layer_type == "raster"
    assert layer.style_preset == _STYLE_PRESET == "continuous_dem"
    assert layer.role == "input"
    assert layer.units == "meters"
    assert layer.uri.startswith("s3://")
    assert "copdem-glo30" in layer.layer_id
    # bbox is stamped on the layer (AOI-pin reuse / zoom-to).
    assert layer.bbox is not None

    dem, nodata = _decode_dem(next(iter(fake.store.values())))
    assert nodata == _NODATA
    # Every pixel decodes to the synthetic elevation (no nodata in this tile).
    assert np.allclose(dem, 2500.0)


# ---------------------------------------------------------------------------
# Mosaic + first-wins correctness.
# ---------------------------------------------------------------------------


def test_two_tiles_first_wins_mosaic() -> None:
    """Two tiles both cover the whole grid; first-wins keeps tile_a's value."""
    fake = _FakeStore()
    items = [_fake_item("tile_a"), _fake_item("tile_b")]
    _run(items, fake, per_tile={"tile_a": 1500.0, "tile_b": 3000.0})

    dem, _ = _decode_dem(next(iter(fake.store.values())))
    # tile_a is read first and fills the whole grid; tile_b never overwrites it.
    assert np.allclose(dem, 1500.0)


def test_second_tile_fills_first_tile_nodata() -> None:
    """A first tile with nodata rows is back-filled by a second tile."""
    fake = _FakeStore()
    # tile_a covers everything EXCEPT its top rows (NaN there); tile_b is a
    # constant fill -> the top rows must take tile_b's value, the rest tile_a's.
    items = [_fake_item("tile_a"), _fake_item("tile_b")]
    rt = _make_read_through_injector(fake)

    def reader(signed_href, bbox, w, h):
        if "tile_a" in signed_href:
            arr = np.full((h, w), 1200.0, dtype="float32")
            arr[:2, :] = np.nan  # top 2 rows nodata
            return arr
        return np.full((h, w), 4000.0, dtype="float32")  # tile_b fill

    with patch.object(cop_mod, "read_through", rt), patch.object(
        cop_mod, "_sign_href", side_effect=lambda href: href
    ), patch.object(cop_mod, "_read_tile_window", reader), patch(
        "pystac_client.Client", _fake_search_client(items)
    ):
        fetch_copernicus_dem(bbox=_ALPS_BBOX)

    dem, _ = _decode_dem(next(iter(fake.store.values())))
    assert np.allclose(dem[:2, :], 4000.0)  # top rows from tile_b
    assert np.allclose(dem[2:, :], 1200.0)  # rest from tile_a (first-wins)


# ---------------------------------------------------------------------------
# Honest no-coverage / all-nodata paths.
# ---------------------------------------------------------------------------


def test_no_tile_raises_empty_non_retryable() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)
    with patch.object(cop_mod, "read_through", rt), patch(
        "pystac_client.Client", _fake_search_client([])
    ):
        with pytest.raises(CopernicusDemEmptyError) as ei:
            fetch_copernicus_dem(bbox=_ALPS_BBOX)
    assert ei.value.retryable is False
    assert not fake.store  # nothing fabricated / cached


def test_all_nodata_window_raises_empty() -> None:
    """A tile that is entirely nodata over the AOI -> honest no-coverage."""
    fake = _FakeStore()
    with pytest.raises(CopernicusDemEmptyError):
        # nodata_top huge enough to NaN the whole grid.
        _run([_fake_item("tile_a")], fake, nodata_top=10_000)


# ---------------------------------------------------------------------------
# Cache determinism.
# ---------------------------------------------------------------------------


def test_cache_hit_does_not_refetch() -> None:
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)
    calls = {"n": 0}
    base_reader = _make_tile_reader({"tile_a": 2000.0})

    def counting_reader(href, bbox, w, h):
        calls["n"] += 1
        return base_reader(href, bbox, w, h)

    with patch.object(cop_mod, "read_through", rt), patch.object(
        cop_mod, "_sign_href", side_effect=lambda href: href
    ), patch.object(cop_mod, "_read_tile_window", counting_reader), patch(
        "pystac_client.Client", _fake_search_client([_fake_item("tile_a")])
    ):
        fetch_copernicus_dem(bbox=_ALPS_BBOX)
        first = calls["n"]
        fetch_copernicus_dem(bbox=_ALPS_BBOX)
        second = calls["n"]

    assert first > 0
    assert second == first  # second call served from cache, no re-read


def test_distinct_bbox_distinct_cache_key() -> None:
    fake = _FakeStore()
    _run([_fake_item("tile_a")], fake, per_tile={"tile_a": 1000.0}, bbox=_ALPS_BBOX)
    _run(
        [_fake_item("tile_a")],
        fake,
        per_tile={"tile_a": 1000.0},
        bbox=(7.61, 45.91, 7.81, 46.06),
    )
    # Two distinct bboxes -> two distinct cached objects.
    assert len(fake.store) == 2


# ---------------------------------------------------------------------------
# Consolidation: fetch_dem(source="copernicus") folds in this GLO-30 tool.
# ---------------------------------------------------------------------------


def test_fetch_dem_source_copernicus_delegates_to_impl() -> None:
    """``fetch_dem(source="copernicus")`` routes to the shared GLO-30 impl."""
    from trid3nt_server.tools.fetchers.terrain.fetch_dem import fetch_dem

    sentinel = object()
    with patch.object(cop_mod, "_copernicus_dem_impl", return_value=sentinel) as spy:
        got = fetch_dem(_ALPS_BBOX, source="copernicus")
    assert got is sentinel
    spy.assert_called_once_with(_ALPS_BBOX)


def test_fetch_dem_default_source_does_not_touch_copernicus() -> None:
    """The default 3DEP path never delegates to the Copernicus impl."""
    from trid3nt_server.tools.fetchers.terrain.fetch_dem import fetch_dem

    with patch.object(cop_mod, "_copernicus_dem_impl") as spy:
        with patch("trid3nt_server.tools.fetchers.terrain.fetch_dem._fetch_3dep_dem_bytes",
                   return_value=b"dem"), \
             patch("trid3nt_server.tools.fetchers.terrain.fetch_dem.read_through") as rt:
            from types import SimpleNamespace
            rt.return_value = SimpleNamespace(uri="s3://c/dem.tif", data=b"dem", hit=False)
            fetch_dem((-82.0, 26.0, -81.9, 26.1), resolution_m=10)
    spy.assert_not_called()


def test_deprecated_fetch_copernicus_dem_routes_through_impl() -> None:
    """The deprecated alias still registers and routes through the shared impl."""
    assert "fetch_copernicus_dem" in TOOL_REGISTRY
    sentinel = object()
    with patch.object(cop_mod, "_copernicus_dem_impl", return_value=sentinel) as spy:
        got = fetch_copernicus_dem(bbox=_ALPS_BBOX)
    assert got is sentinel
    spy.assert_called_once_with(_ALPS_BBOX)
