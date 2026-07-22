"""Unit tests for the ``digitize_water_body`` atomic tool (NDWI water polygons).

Coverage:
- Registration in TOOL_REGISTRY with expected metadata (+ payload estimator).
- Input validation: degenerate / out-of-range / non-finite / too-large bbox,
  bad ndwi_threshold, bad min_area_m2 -> typed ``WaterBodyBboxError`` (not
  retryable).
- Mocked PC STAC + band reads: a synthetic Green/NIR pair where one half of the
  scene is water (NDWI > 0) and the other half is land (NDWI < 0) round-trips
  through read_through to a FlatGeobuf with the right polygon count, the water
  geometry on the correct side, and the expected layer attributes.
- Honest no-imagery: an empty STAC search raises ``WaterBodyNoImageryError``
  (not retryable).
- Honest no-water: a scene that is all land (NDWI < 0 everywhere) raises
  ``WaterBodyNoWaterError`` (not retryable)  --  never an empty success layer.
- min_area_m2 speck filter: water below the area floor raises no-water.

Network is fully mocked: ``_pc_stac`` search/sign + the per-band window reader
are patched so no real Sentinel-2 scene is fetched. The vectorization
(rasterio.features.shapes), area filter (geopandas), and FlatGeobuf write run
for real on the synthetic mask  --  that is the compute-correctness surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools import digitize_water_body as wb_mod
from grace2_agent.tools.digitize_water_body import (
    _METADATA,
    _STYLE_PRESET,
    WaterBodyBboxError,
    WaterBodyNoImageryError,
    WaterBodyNoWaterError,
    digitize_water_body,
    estimate_payload_mb,
)

_PINNED_NOW = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)

# A small reservoir-scale AOI inside the guardrail. Sized so a half-and-half
# synthetic mask yields water polygons comfortably above the 1000 m^2 floor.
_AOI = (-112.30, 33.83, -112.20, 33.92)


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors the sibling compute_ndvi test).
# ---------------------------------------------------------------------------


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


def _fake_item(scene_id: str = "S2_fake", cc: float = 1.0):
    """A minimal STAC-like item with B03/B08 assets."""
    return SimpleNamespace(
        id=scene_id,
        properties={"eo:cloud_cover": cc},
        assets={
            "B03": SimpleNamespace(href="https://blob/green.tif"),
            "B08": SimpleNamespace(href="https://blob/nir.tif"),
        },
    )


# Half-water / half-land synthetic band reader. The LEFT half of every read is
# water (Green high, NIR low -> NDWI > 0); the RIGHT half is land (Green low,
# NIR high -> NDWI < 0).
def _half_water_reader():
    def reader(signed_href, bbox, w, h):
        if "green" in signed_href:
            arr = np.empty((h, w), dtype="float32")
            arr[:, : w // 2] = 4000.0  # water: high green
            arr[:, w // 2 :] = 1000.0  # land: low green
            return np.ma.array(arr)
        # NIR
        arr = np.empty((h, w), dtype="float32")
        arr[:, : w // 2] = 1000.0  # water: low NIR
        arr[:, w // 2 :] = 4000.0  # land: high NIR
        return np.ma.array(arr)

    return reader


# All-land synthetic reader (Green low, NIR high everywhere -> NDWI < 0).
def _all_land_reader():
    def reader(signed_href, bbox, w, h):
        if "green" in signed_href:
            return np.ma.array(np.full((h, w), 1000.0, dtype="float32"))
        return np.ma.array(np.full((h, w), 4000.0, dtype="float32"))

    return reader


def _read_fgb(uri_bytes: bytes):
    import tempfile
    import os
    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(uri_bytes)
        p = tf.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered() -> None:
    assert "digitize_water_body" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["digitize_water_body"].metadata
    assert meta.name == "digitize_water_body"
    assert meta.ttl_class == "static-30d"
    assert meta.source_class == "digitize_water_body"
    assert meta.cacheable is True
    assert meta.payload_mb_estimator_name == "estimate_payload_mb"


def test_metadata_supports_global_query_false() -> None:
    # AOI-scoped Sentinel-2 read; a global query is meaningless here.
    assert getattr(_METADATA, "supports_global_query", False) is False


def test_payload_estimator_scales_with_area_and_floors() -> None:
    small = estimate_payload_mb(bbox=(-112.30, 33.83, -112.29, 33.84))
    big = estimate_payload_mb(bbox=(-112.30, 33.83, -112.00, 34.10))
    assert big > small
    assert estimate_payload_mb(bbox=None) > 0
    # Floored: never zero / negative even for a microscopic bbox.
    assert estimate_payload_mb(bbox=(-112.300, 33.830, -112.2999, 33.8301)) >= 0.05


# ---------------------------------------------------------------------------
# Input validation (typed, non-retryable).
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(WaterBodyBboxError):
        digitize_water_body(bbox=(-112.30, 33.83, -112.30, 33.83))


def test_out_of_range_bbox_raises() -> None:
    with pytest.raises(WaterBodyBboxError, match="lon out of"):
        digitize_water_body(bbox=(-200.0, 33.0, -112.0, 34.0))


def test_nonfinite_bbox_raises() -> None:
    with pytest.raises(WaterBodyBboxError, match="non-finite"):
        digitize_water_body(bbox=(float("nan"), 33.0, -112.0, 34.0))


def test_too_large_bbox_raises() -> None:
    with pytest.raises(WaterBodyBboxError, match="guardrail"):
        digitize_water_body(bbox=(-114.0, 31.0, -111.0, 34.0))  # 9 deg^2


def test_bad_threshold_raises() -> None:
    with pytest.raises(WaterBodyBboxError, match="ndwi_threshold"):
        digitize_water_body(bbox=_AOI, ndwi_threshold=2.0)


def test_bad_min_area_raises() -> None:
    with pytest.raises(WaterBodyBboxError, match="min_area_m2"):
        digitize_water_body(bbox=_AOI, min_area_m2=-5.0)


def test_input_error_not_retryable() -> None:
    try:
        digitize_water_body(bbox=(-112.30, 33.83, -112.30, 33.83))
    except WaterBodyBboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("expected WaterBodyBboxError")


# ---------------------------------------------------------------------------
# Happy path (mocked STAC + band reads; real vectorize + FGB write).
# ---------------------------------------------------------------------------


def test_happy_path_digitizes_water_and_roundtrips() -> None:
    """A half-water/half-land synthetic scene -> a FlatGeobuf whose water polygon
    sits on the LEFT (water) half, with correct layer attributes, round-tripped
    through read_through."""
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    with patch.object(wb_mod._pc_stac, "search_least_cloudy_item", return_value=_fake_item()), \
         patch.object(wb_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
         patch.object(wb_mod, "_read_band_window", _half_water_reader()), \
         patch.object(wb_mod, "read_through", rt):
        layer = digitize_water_body(
            bbox=_AOI, start_date="2025-04-01", end_date="2026-06-01"
        )

    assert layer.layer_type == "vector"
    assert layer.style_preset == _STYLE_PRESET
    assert layer.role == "primary"
    assert layer.uri.startswith("s3://")
    assert layer.units == "m^2"
    assert layer.bbox is not None

    # Decode the cached FGB and check the digitized polygons.
    path = layer.uri[len("s3://"):].split("/", 1)[1]
    gdf = _read_fgb(fake.store[path])
    assert len(gdf) >= 1
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    # Attribute columns from the tool.
    assert "area_m2" in gdf.columns
    assert "water_index" in gdf.columns
    assert (gdf["water_index"] == "NDWI").all()
    assert (gdf["area_m2"] > 0).all()

    # The water polygon(s) must sit on the LEFT (western) half of the bbox.
    # Reproject to a planar CRS before the centroid to avoid the geographic-CRS
    # warning, then compare against the bbox midpoint reprojected the same way.
    mid_lon = 0.5 * (_AOI[0] + _AOI[2])
    import geopandas as gpd
    from shapely.geometry import Point

    centroids_m = gdf.to_crs(3857).geometry.centroid
    mid_x_m = (
        gpd.GeoSeries([Point(mid_lon, 0.5 * (_AOI[1] + _AOI[3]))], crs=4326)
        .to_crs(3857)
        .geometry.x.iloc[0]
    )
    assert (centroids_m.x < mid_x_m).all(), (
        "water polygons should be on the western (water) half"
    )


def test_cache_hit_does_not_refetch() -> None:
    """A second identical call hits the in-memory store and does NOT re-run the
    fetcher (digitization)."""
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)
    calls = {"n": 0}

    real_fetch = wb_mod._digitize_water_fgb_bytes

    def counting_fetch(*a, **k):
        calls["n"] += 1
        return real_fetch(*a, **k)

    with patch.object(wb_mod._pc_stac, "search_least_cloudy_item", return_value=_fake_item()), \
         patch.object(wb_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
         patch.object(wb_mod, "_read_band_window", _half_water_reader()), \
         patch.object(wb_mod, "_digitize_water_fgb_bytes", counting_fetch), \
         patch.object(wb_mod, "read_through", rt):
        layer1 = digitize_water_body(bbox=_AOI, start_date="2025-04-01", end_date="2026-06-01")
        layer2 = digitize_water_body(bbox=_AOI, start_date="2025-04-01", end_date="2026-06-01")

    assert layer1.uri == layer2.uri
    assert calls["n"] == 1  # second call served from cache


# ---------------------------------------------------------------------------
# Honest empty paths (FR-AS-11 / data-source fallback norm).
# ---------------------------------------------------------------------------


def test_no_imagery_raises_typed_not_retryable() -> None:
    """An empty STAC search -> WaterBodyNoImageryError, not a fabricated layer."""
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    def _raise_no_items(**kw):
        raise wb_mod._pc_stac.PCStacNoItemsError("no items")

    with patch.object(wb_mod._pc_stac, "search_least_cloudy_item", side_effect=_raise_no_items), \
         patch.object(wb_mod, "read_through", rt):
        with pytest.raises(WaterBodyNoImageryError) as exc_info:
            digitize_water_body(bbox=_AOI, start_date="2025-04-01", end_date="2026-06-01")
    assert exc_info.value.retryable is False


def test_no_water_raises_typed_not_retryable() -> None:
    """An all-land scene (NDWI < 0 everywhere) -> WaterBodyNoWaterError, never an
    empty FGB that reads as success."""
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    with patch.object(wb_mod._pc_stac, "search_least_cloudy_item", return_value=_fake_item()), \
         patch.object(wb_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
         patch.object(wb_mod, "_read_band_window", _all_land_reader()), \
         patch.object(wb_mod, "read_through", rt):
        with pytest.raises(WaterBodyNoWaterError) as exc_info:
            digitize_water_body(bbox=_AOI, start_date="2025-04-01", end_date="2026-06-01")
    assert exc_info.value.retryable is False


def test_min_area_floor_rejects_specks_as_no_water() -> None:
    """When the only water is below the area floor, the tool honestly reports
    no mappable water rather than emitting a speck layer."""
    fake = _FakeStore()
    rt = _make_read_through_injector(fake)

    # All-water synthetic scene, but an absurdly high min_area_m2 floor so every
    # polygon is dropped -> honest no-water.
    def all_water(signed_href, bbox, w, h):
        if "green" in signed_href:
            return np.ma.array(np.full((h, w), 4000.0, dtype="float32"))
        return np.ma.array(np.full((h, w), 1000.0, dtype="float32"))

    with patch.object(wb_mod._pc_stac, "search_least_cloudy_item", return_value=_fake_item()), \
         patch.object(wb_mod._pc_stac, "sas_sign_href", side_effect=lambda href, c: href), \
         patch.object(wb_mod, "_read_band_window", all_water), \
         patch.object(wb_mod, "read_through", rt):
        with pytest.raises(WaterBodyNoWaterError, match="min_area_m2"):
            digitize_water_body(
                bbox=_AOI,
                start_date="2025-04-01",
                end_date="2026-06-01",
                min_area_m2=1e15,  # impossibly large; drops every polygon
            )
