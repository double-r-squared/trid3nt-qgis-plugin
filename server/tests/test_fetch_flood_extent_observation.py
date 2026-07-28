"""Offline unit tests for ``fetch_flood_extent_observation``.

Network is fully mocked: ``_resolve_datetime`` / ``_download_tile`` /
``_list_dir_names`` are patched and ``read_through`` uses an in-memory store.
Two data paths are exercised:
- a SYNTHESIZED classified tile carrying all MCDWD classes (0/1/2/3/255), and
- a COMMITTED real MCDWD_L3_F3 window (``fixtures/validation/flood_extent/``,
  captured live once from LANCE nrt3) to prove the parser handles real bytes.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.agent.tools.fetchers.hydrology.fetch_flood_extent_observation import (
    fetch_flood_extent_observation as fe_mod,
)
from trid3nt_server.agent.tools.fetchers.hydrology.fetch_flood_extent_observation.fetch_flood_extent_observation import (
    FloodExtentInputError,
    FloodExtentLayerURI,
    FloodExtentNoCoverageError,
    NODATA,
    estimate_payload_mb,
    fetch_flood_extent_observation,
)

_REAL_FIXTURE = (
    pathlib.Path(__file__).parent
    / "fixtures" / "validation" / "flood_extent" / "mcdwd_l3_f3_h16v06_window.tif"
)


def _make_read_through_injector(store):
    from trid3nt_server.agent.tools.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path,
        compute_cache_key as ck, is_cacheable,
    )

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = ck(source_id, params, metadata.ttl_class)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


def _synth_tile_bytes(bounds, arr: np.ndarray) -> bytes:
    """A classified uint8 GeoTIFF (EPSG:4326, nodata=255) for ``bounds``."""
    h, w = arr.shape
    transform = rasterio.transform.from_bounds(*bounds, w, h)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff", height=h, width=w, count=1, dtype="uint8",
            crs="EPSG:4326", transform=transform, nodata=NODATA,
        ) as dst:
            dst.write(arr, 1)
        return mem.read()


def _classified_tile(h: int, v: int) -> bytes:
    """A tile for (h, v) with classes 1/2/3 in the north band, 0/255 in the south."""
    bounds = fe_mod._tile_bounds(h, v)
    n = 120
    arr = np.zeros((n, n), dtype="uint8")
    for col in range(n):
        arr[:, col] = 1 + (col % 3)  # cycles 1, 2, 3 across columns
    arr[-10:-5, :] = 0     # a dry band in the south
    arr[-5:, :] = NODATA   # a cloud/no-data band in the south
    return _synth_tile_bytes(bounds, arr)


# ---------------------------------------------------------------------------
# Registration + input validation.
# ---------------------------------------------------------------------------


def test_registered() -> None:
    entry = TOOL_REGISTRY["fetch_flood_extent_observation"]
    assert entry.fn is fetch_flood_extent_observation
    m = entry.metadata
    assert m.cacheable is True
    assert m.ttl_class == "semi-static-7d"
    assert m.source_class == "mcdwd_flood_extent"
    assert m.supports_global_query is False
    assert m.open_world_hint is True


def test_missing_bbox_raises() -> None:
    with pytest.raises(FloodExtentInputError):
        fetch_flood_extent_observation(bbox=None)


def test_too_large_bbox_raises() -> None:
    with pytest.raises(FloodExtentInputError):
        fetch_flood_extent_observation(bbox=(-90.0, 20.0, -80.0, 30.0))  # 100 deg^2


def test_bad_date_raises() -> None:
    with pytest.raises(FloodExtentInputError):
        fetch_flood_extent_observation(bbox=(-85.5, 29.5, -85.0, 30.0), date="not-a-date")


def test_payload_estimate() -> None:
    assert estimate_payload_mb(bbox=(-85.5, 29.5, -85.0, 30.0)) > 0
    assert estimate_payload_mb() > 0


# ---------------------------------------------------------------------------
# Synthetic happy path (all classes).
# ---------------------------------------------------------------------------


def _patch_synth(monkeypatch, store):
    monkeypatch.setattr(fe_mod, "read_through", _make_read_through_injector(store))
    monkeypatch.setattr(fe_mod, "_resolve_datetime", lambda date: (2026, 198))

    def _fake_dl(year, doy, h, v):
        return _classified_tile(h, v) if (h, v) == (9, 6) else b""

    monkeypatch.setattr(fe_mod, "_download_tile", _fake_dl)


def test_synthetic_happy(monkeypatch) -> None:
    store: dict[str, bytes] = {}
    _patch_synth(monkeypatch, store)
    bbox = (-85.5, 29.5, -85.0, 30.0)  # inside tile (9, 6)
    layer = fetch_flood_extent_observation(bbox=bbox)

    assert isinstance(layer, FloodExtentLayerURI)
    assert layer.layer_type == "raster"
    assert layer.style_preset == "flood_extent_observed"
    assert layer.uri.startswith("s3://")
    assert layer.observation_date == "2026-07-17"  # doy 198 of 2026
    assert layer.product == "MCDWD_L3_F3_NRT"
    assert layer.flood_pixel_count > 0
    assert layer.flood_area_km2 and layer.flood_area_km2 > 0
    assert "Flood water" in layer.class_breakdown

    # Categorical legend with the three water classes.
    assert layer.legend is not None
    assert layer.legend.kind == "categorical"
    assert {c.value for c in layer.legend.classes} == {1, 2, 3}

    # The REQUIRED SAR/optical detection-limit caveat is present.
    joined = " ".join(layer.caveats)
    assert "UNDER-detects" in joined and "SAR" in joined
    assert "250 m" in joined

    # Read the stored COG: uint8, nodata 255, embedded palette.
    cog = next(iter(store.values()))
    with MemoryFile(cog) as mem, mem.open() as out:
        assert out.count == 1
        assert str(out.dtypes[0]) == "uint8"
        assert out.nodata == 255
        cmap = out.colormap(1)
        assert cmap[3][:3] == (202, 0, 32)  # flood red
        assert cmap[1][:3] == (146, 197, 222)  # reference water light blue
        assert cmap[255][3] == 0            # nodata transparent (only alpha GDAL honors)
        arr = out.read(1)
        assert set(np.unique(arr).tolist()) & {1, 2, 3}


# ---------------------------------------------------------------------------
# Real committed MCDWD fixture.
# ---------------------------------------------------------------------------


def test_real_fixture_parses(monkeypatch) -> None:
    store: dict[str, bytes] = {}
    monkeypatch.setattr(fe_mod, "read_through", _make_read_through_injector(store))
    monkeypatch.setattr(fe_mod, "_resolve_datetime", lambda date: (2026, 198))
    tile_bytes = _REAL_FIXTURE.read_bytes()

    with rasterio.open(_REAL_FIXTURE) as ds:
        b = ds.bounds
    bbox = (b.left, b.bottom, b.right, b.top)

    def _fake_dl(year, doy, h, v):
        return tile_bytes if (h, v) == (16, 6) else b""

    monkeypatch.setattr(fe_mod, "_download_tile", _fake_dl)
    layer = fetch_flood_extent_observation(bbox=bbox)
    # The real window carries class-3 (flood) pixels.
    assert layer.flood_pixel_count > 0
    assert "Flood water" in layer.class_breakdown


# ---------------------------------------------------------------------------
# Honest error paths.
# ---------------------------------------------------------------------------


def test_all_nodata_raises(monkeypatch) -> None:
    store: dict[str, bytes] = {}
    monkeypatch.setattr(fe_mod, "read_through", _make_read_through_injector(store))
    monkeypatch.setattr(fe_mod, "_resolve_datetime", lambda date: (2026, 198))

    def _fake_dl(year, doy, h, v):
        bounds = fe_mod._tile_bounds(h, v)
        arr = np.full((40, 40), NODATA, dtype="uint8")
        return _synth_tile_bytes(bounds, arr)

    monkeypatch.setattr(fe_mod, "_download_tile", _fake_dl)
    with pytest.raises(FloodExtentNoCoverageError):
        fetch_flood_extent_observation(bbox=(-85.5, 29.5, -85.0, 30.0))


def test_no_tiles_raises(monkeypatch) -> None:
    store: dict[str, bytes] = {}
    monkeypatch.setattr(fe_mod, "read_through", _make_read_through_injector(store))
    monkeypatch.setattr(fe_mod, "_resolve_datetime", lambda date: (2026, 198))
    monkeypatch.setattr(fe_mod, "_download_tile", lambda year, doy, h, v: b"")
    with pytest.raises(FloodExtentNoCoverageError):
        fetch_flood_extent_observation(bbox=(-85.5, 29.5, -85.0, 30.0))


def test_latest_available_resolution(monkeypatch) -> None:
    def fake_list(url):
        if url.endswith("MCDWD_L3_F3_NRT/"):
            return ["2025", "2026"]
        if url.endswith("/2026/"):
            return ["197", "198"]
        return []

    monkeypatch.setattr(fe_mod, "_list_dir_names", fake_list)
    assert fe_mod._resolve_datetime(None) == (2026, 198)


def test_explicit_date_to_doy() -> None:
    assert fe_mod._resolve_datetime("2026-07-17") == (2026, 198)
