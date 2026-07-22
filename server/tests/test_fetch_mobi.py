"""Unit tests for the ``fetch_mobi`` atomic tool (conservation micro-North-Star).

Coverage:
- Registration in TOOL_REGISTRY with expected metadata.
- The 5 MoBI layer aliases map to real PC ``mobi`` item-asset keys.
- bbox validation + a non-CONUS bbox raises MoBIEmptyError BEFORE any network
  (honest no-coverage, never fabricated) and is NOT retryable.
- Unknown layer raises MoBILayerError (not retryable).
- Mocked PC STAC + a synthetic CONUS-windowed source COG: round-trips to a
  cached single-band float32 COG with the right style preset + units.
- All-nodata window raises MoBIEmptyError (outside-CONUS leak guard).
- The ``mobi_biodiversity`` style preset resolves in the TiTiler registry.

Network is fully mocked: PC STAC search + SAS sign are patched and the source
asset is a local in-memory COG read via the /vsicurl/ intercept.
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
from grace2_agent.tools import fetch_mobi as mobi_mod
from grace2_agent.tools.fetch_mobi import (
    MOBI_LAYERS,
    MoBIBboxError,
    MoBIEmptyError,
    MoBILayerError,
    fetch_mobi,
    estimate_payload_mb,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# Small CONUS AOI (Charleston, SC).
_CONUS_AOI = (-80.05, 32.75, -79.90, 32.85)
# Outside CONUS (mid-Atlantic ocean / Europe-ish lon).
_NON_CONUS = (10.0, 48.0, 11.0, 49.0)


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from grace2_agent.tools.cache import (
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


def _write_source_mobi_cog(bbox, value: float = 12.0) -> str:
    """Write a tiny single-band float32 EPSG:4326 GeoTIFF over ``bbox``.

    Stand-in for the signed CONUS-wide MoBI asset (the tool windows it to the
    bbox via reproject; here the source already covers the bbox)."""
    import rasterio

    w, h = 20, 14
    transform = rasterio.transform.from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    arr = np.full((h, w), value, dtype="float32")
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="grace2_mobi_src_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
        crs="EPSG:4326", transform=transform, nodata=0.0,
    ) as dst:
        dst.write(arr, 1)
    return path


def _fake_item_local(local_path: str):
    return SimpleNamespace(
        id="mobi_fake",
        properties={},
        assets={k: SimpleNamespace(href=local_path) for k in MOBI_LAYERS.values()},
    )


def _vsicurl_open_intercept():
    import rasterio
    real_open = rasterio.open

    def open_side_effect(p, *a, **k):
        if isinstance(p, str) and p.startswith("/vsicurl/"):
            p = p[len("/vsicurl/"):]
        return real_open(p, *a, **k)

    return open_side_effect


# ---------------------------------------------------------------------------
# Registration / metadata / layer aliases.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "fetch_mobi" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["fetch_mobi"].metadata
    assert meta.name == "fetch_mobi"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "mobi"
    assert meta.cacheable is True


def test_layer_aliases_map_to_pc_asset_keys() -> None:
    assert MOBI_LAYERS["species_richness"] == "SpeciesRichness_All"
    assert MOBI_LAYERS["range_size_rarity"] == "RSR_All"
    assert MOBI_LAYERS["protection_weighted_rsr"] == "PWRSR_GAP12_SUM_All"
    assert len(MOBI_LAYERS) == 5


def test_style_preset_is_in_titiler_registry() -> None:
    from grace2_agent.tools.publish_layer import _registry_style_params

    params = _registry_style_params("mobi_biodiversity")
    assert params is not None
    assert "colormap_name=ylgn" in params


def test_payload_estimator_returns_positive() -> None:
    assert estimate_payload_mb(bbox=_CONUS_AOI) > 0
    assert estimate_payload_mb(bbox=None) > 0


# ---------------------------------------------------------------------------
# Validation + honesty (no network).
# ---------------------------------------------------------------------------


def test_unknown_layer_raises_typed_error() -> None:
    with pytest.raises(MoBILayerError, match="unknown MoBI layer"):
        fetch_mobi(bbox=_CONUS_AOI, layer="bogus")


def test_unknown_layer_not_retryable() -> None:
    try:
        fetch_mobi(bbox=_CONUS_AOI, layer="bogus")
    except MoBILayerError as exc:
        assert exc.retryable is False


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(MoBIBboxError):
        fetch_mobi(bbox=(-80.0, 32.0, -80.0, 32.0))


def test_non_conus_bbox_raises_empty_before_network() -> None:
    """A bbox outside CONUS must fail fast with MoBIEmptyError and never search."""
    with patch.object(mobi_mod._pc_stac, "search_least_cloudy_item") as search:
        with pytest.raises(MoBIEmptyError, match="conterminous"):
            fetch_mobi(bbox=_NON_CONUS, layer="species_richness")
        search.assert_not_called()


def test_empty_error_not_retryable() -> None:
    try:
        raise MoBIEmptyError("x")
    except MoBIEmptyError as exc:
        assert exc.retryable is False


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + local source COG).
# ---------------------------------------------------------------------------


def test_mobi_happy_path_roundtrips_to_cog() -> None:
    from rasterio.io import MemoryFile

    src_path = _write_source_mobi_cog(_CONUS_AOI, value=12.0)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item_local(src_path)

        with patch.object(mobi_mod._pc_stac, "search_least_cloudy_item", return_value=item), \
             patch.object(mobi_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(mobi_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_vsicurl_open_intercept()):
            layer = fetch_mobi(bbox=_CONUS_AOI, layer="species_richness")

        assert layer.layer_type == "raster"
        assert layer.role == "primary"
        assert layer.style_preset == "mobi_biodiversity"
        assert layer.units == "imperiled-species count"
        assert layer.uri.startswith("s3://")

        cog = next(iter(fake.store.values()))
        with MemoryFile(cog) as mem, mem.open() as out:
            assert out.count == 1
            assert str(out.dtypes[0]) == "float32"
            arr = out.read(1, masked=True)
            valid = arr.compressed()
            assert valid.size > 0
            assert np.allclose(valid, 12.0, atol=1e-3)
    finally:
        os.unlink(src_path)


def test_all_nodata_window_raises_empty() -> None:
    """A source whose window is all <=0 raises MoBIEmptyError (outside-CONUS leak
    guard) rather than caching a useless blob."""
    src_path = _write_source_mobi_cog(_CONUS_AOI, value=0.0)
    try:
        fake = _FakeStore()
        rt = _make_read_through_injector(fake)
        item = _fake_item_local(src_path)

        with patch.object(mobi_mod._pc_stac, "search_least_cloudy_item", return_value=item), \
             patch.object(mobi_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
             patch.object(mobi_mod, "read_through", rt), \
             patch("rasterio.open", side_effect=_vsicurl_open_intercept()):
            with pytest.raises(MoBIEmptyError):
                fetch_mobi(bbox=_CONUS_AOI, layer="species_richness")
    finally:
        os.unlink(src_path)


# ---------------------------------------------------------------------------
# Cache-key determinism.
# ---------------------------------------------------------------------------


def test_distinct_layer_distinct_cache_key() -> None:
    base = {"bbox": list(_CONUS_AOI), "collection": "mobi"}
    k1 = compute_cache_key("mobi", {**base, "layer": "species_richness"}, "static-30d", now=_PINNED_NOW)
    k2 = compute_cache_key("mobi", {**base, "layer": "range_size_rarity"}, "static-30d", now=_PINNED_NOW)
    assert k1 != k2
