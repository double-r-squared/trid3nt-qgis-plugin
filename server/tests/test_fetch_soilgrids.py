"""Tests for ``fetch_soilgrids`` -- ISRIC SoilGrids 2.0 global soil-property COG.

Coverage:
- Registration: tool present in TOOL_REGISTRY with expected metadata.
- Input validation: None / degenerate / out-of-range / too-large bbox; unknown
  property; unknown depth -> typed errors (not retryable).
- Property/depth normalization: synonyms + loose depth spellings.
- Coverage rejection: Antarctica bbox -> SoilGridsEmptyError.
- Mocked /vsicurl open: synthetic uniform Int16 source -> scaled float32 COG with
  the correct physical-unit magnitude (e.g. clay raw 300 -> 30.0%).
- Honest empty: all-NoData window -> SoilGridsEmptyError (no fabricated layer).
- End-to-end through read_through (in-memory S3 double) returns a LayerURI.

No live network: the source open is patched to read a local synthetic raster.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.soil.fetch_soilgrids import (
    estimate_payload_mb,
    fetch_soilgrids,
    SoilGridsBboxRequiredError,
    SoilGridsEmptyError,
    SoilGridsInputError,
    _fetch_soilgrids_bytes,
    _normalize_depth,
    _normalize_property,
    _PROPERTIES,
    _DEPTHS,
)

_PINNED_NOW = datetime(2026, 6, 27, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# In-memory S3 read-through injector (mirrors sibling tool tests).
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}


def _make_read_through_injector(fake):
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
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
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


# ---------------------------------------------------------------------------
# Synthetic source helper. We build the synthetic SoilGrids "source" in
# EPSG:4326 so the tool's transform_bounds + reproject path is exercised as an
# identity (4326 -> 4326); the scaling/window/NoData logic is what we assert.
# ---------------------------------------------------------------------------


def _synth_soilgrids_tif_bytes(
    raw_value: int = 300,
    bbox: tuple[float, float, float, float] = (-94.0, 41.5, -93.0, 42.5),
    width: int = 64,
    height: int = 64,
    nodata: int = -32768,
    nodata_fraction: float = 0.0,
) -> bytes:
    """Build a tiny synthetic Int16 GeoTIFF (uniform ``raw_value``).

    If ``nodata_fraction`` is 1.0 the whole raster is NoData (honest-empty case).
    EPSG:4326 so the tool's reproject step is identity.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    if nodata_fraction >= 1.0:
        arr = np.full((height, width), nodata, dtype=np.int16)
    else:
        arr = np.full((height, width), raw_value, dtype=np.int16)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_soilgrids_synth_")
    os.close(fd)
    try:
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            dtype="int16",
            count=1,
            height=height,
            width=width,
            crs="EPSG:4326",
            transform=transform,
            nodata=nodata,
        ) as dst:
            dst.write(arr, 1)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _patched_open_factory(src_bytes: bytes, tmp_path):
    """Return a patched rasterio.open that maps /vsicurl/ URLs to a local synth file."""
    import rasterio

    src_path = tmp_path / "synth_soilgrids_src.tif"
    src_path.write_bytes(src_bytes)
    real_open = rasterio.open
    calls: list[str] = []

    def patched_open(path, *a, **kw):
        calls.append(str(path))
        if str(path).startswith("/vsicurl/"):
            return real_open(str(src_path), *a, **kw)
        return real_open(path, *a, **kw)

    return patched_open, calls


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_soilgrids" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_soilgrids"]
    assert entry.metadata.name == "fetch_soilgrids"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "soilgrids"
    assert entry.metadata.cacheable is True


def test_properties_and_depths_tables():
    assert set(_PROPERTIES) == {"clay", "sand", "silt", "soc", "bdod", "phh2o"}
    # Each property carries (scale_div, unit, label, style_preset).
    for prop, (div, unit, label, preset) in _PROPERTIES.items():
        assert div in (10.0, 100.0)
        assert isinstance(unit, str) and unit
        assert preset.startswith("soil_")
    assert _DEPTHS[0] == "0-5cm" and "100-200cm" in _DEPTHS


def test_estimate_payload_scales_with_area():
    small = estimate_payload_mb(bbox=(-93.5, 41.9, -93.4, 42.0))
    big = estimate_payload_mb(bbox=(-94.0, 41.5, -93.5, 42.0))
    assert big > small
    assert estimate_payload_mb(bbox=None) >= 0.0


# ---------------------------------------------------------------------------
# Input validation (typed, not retryable).
# ---------------------------------------------------------------------------


def test_none_bbox_raises_bbox_required():
    with pytest.raises(SoilGridsBboxRequiredError):
        fetch_soilgrids(bbox=None)  # type: ignore[arg-type]


def test_invalid_bbox_raises_typed_error():
    with pytest.raises(SoilGridsInputError, match="degenerate"):
        fetch_soilgrids(bbox=(-93.0, 42.0, -94.0, 41.0))
    with pytest.raises(SoilGridsInputError, match="lon"):
        fetch_soilgrids(bbox=(-200.0, 41.0, -93.0, 42.0))
    with pytest.raises(SoilGridsInputError, match="lat"):
        fetch_soilgrids(bbox=(-94.0, -91.0, -93.0, 42.0))
    with pytest.raises(SoilGridsInputError, match="must be"):
        fetch_soilgrids(bbox=(-94.0, 41.0, -93.0))  # type: ignore[arg-type]


def test_too_large_bbox_raises():
    with pytest.raises(SoilGridsInputError, match="guardrail"):
        fetch_soilgrids(bbox=(-95.0, 40.0, -93.0, 42.0))  # 2x2 deg = 4 deg^2


def test_unknown_property_raises():
    with pytest.raises(SoilGridsInputError, match="property"):
        fetch_soilgrids(bbox=(-93.85, 41.85, -93.45, 42.15), soil_property="gold")


def test_unknown_depth_raises():
    with pytest.raises(SoilGridsInputError, match="depth"):
        fetch_soilgrids(
            bbox=(-93.85, 41.85, -93.45, 42.15),
            soil_property="clay",
            depth="7-9cm",
        )


def test_input_errors_not_retryable():
    err = SoilGridsInputError("x")
    assert err.retryable is False
    assert SoilGridsBboxRequiredError("x").retryable is False
    assert SoilGridsEmptyError("x").retryable is False


# ---------------------------------------------------------------------------
# Property / depth normalization.
# ---------------------------------------------------------------------------


def test_property_synonyms_normalize():
    assert _normalize_property("pH") == "phh2o"
    assert _normalize_property("organic_carbon") == "soc"
    assert _normalize_property("Bulk_Density") == "bdod"
    assert _normalize_property("CLAY") == "clay"


def test_depth_loose_spellings_normalize():
    assert _normalize_depth("0-5") == "0-5cm"
    assert _normalize_depth("0-5 cm") == "0-5cm"
    assert _normalize_depth("5_15cm") == "5-15cm"
    assert _normalize_depth("100-200cm") == "100-200cm"


# ---------------------------------------------------------------------------
# Coverage rejection.
# ---------------------------------------------------------------------------


def test_antarctica_bbox_raises_empty():
    # Below the coverage envelope (~-62 S floor).
    with pytest.raises(SoilGridsEmptyError, match="coverage"):
        _fetch_soilgrids_bytes((0.0, -80.0, 1.0, -79.0), "clay", "0-5cm")


# ---------------------------------------------------------------------------
# Mocked /vsicurl open: scaling correctness.
# ---------------------------------------------------------------------------


def test_mocked_open_scales_clay_to_percent(tmp_path):
    """Synthetic clay raw=300 -> 30.0% (scale /10) in the output float32 COG."""
    import numpy as np
    import rasterio

    src_bytes = _synth_soilgrids_tif_bytes(raw_value=300)
    patched_open, calls = _patched_open_factory(src_bytes, tmp_path)

    with patch("rasterio.open", side_effect=patched_open):
        out_bytes = _fetch_soilgrids_bytes(
            (-93.85, 41.85, -93.45, 42.15), "clay", "0-5cm"
        )

    assert any(c.startswith("/vsicurl/") for c in calls)
    fd, p = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        with open(p, "wb") as f:
            f.write(out_bytes)
        with rasterio.open(p) as ds:
            assert ds.dtypes[0] == "float32"
            assert ds.crs.to_epsg() == 4326
            assert abs(ds.nodata - (-9999.0)) < 1e-6
            arr = ds.read(1)
            valid = arr != ds.nodata
            assert valid.any()
            # raw 300 / 10 -> 30.0 percent.
            assert abs(float(arr[valid].mean()) - 30.0) < 0.5
            assert ds.tags().get("units") == "percent"
    finally:
        os.unlink(p)


def test_mocked_open_scales_phh2o(tmp_path):
    """Synthetic phh2o raw=65 -> pH 6.5 (scale /10)."""
    import rasterio

    src_bytes = _synth_soilgrids_tif_bytes(raw_value=65)
    patched_open, _ = _patched_open_factory(src_bytes, tmp_path)
    with patch("rasterio.open", side_effect=patched_open):
        out_bytes = _fetch_soilgrids_bytes(
            (-93.85, 41.85, -93.45, 42.15), "phh2o", "5-15cm"
        )
    fd, p = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        with open(p, "wb") as f:
            f.write(out_bytes)
        with rasterio.open(p) as ds:
            arr = ds.read(1)
            valid = arr != ds.nodata
            assert abs(float(arr[valid].mean()) - 6.5) < 0.1
            assert ds.tags().get("units") == "pH"
    finally:
        os.unlink(p)


def test_mocked_open_scales_bdod(tmp_path):
    """Synthetic bdod raw=144 -> 1.44 kg/dm3 (scale /100)."""
    import rasterio

    src_bytes = _synth_soilgrids_tif_bytes(raw_value=144)
    patched_open, _ = _patched_open_factory(src_bytes, tmp_path)
    with patch("rasterio.open", side_effect=patched_open):
        out_bytes = _fetch_soilgrids_bytes(
            (-93.85, 41.85, -93.45, 42.15), "bdod", "0-5cm"
        )
    fd, p = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    try:
        with open(p, "wb") as f:
            f.write(out_bytes)
        with rasterio.open(p) as ds:
            arr = ds.read(1)
            valid = arr != ds.nodata
            assert abs(float(arr[valid].mean()) - 1.44) < 0.05
            assert ds.tags().get("units") == "kg/dm3"
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# Honest empty: all-NoData window.
# ---------------------------------------------------------------------------


def test_all_nodata_window_raises_empty(tmp_path):
    src_bytes = _synth_soilgrids_tif_bytes(nodata_fraction=1.0)
    patched_open, _ = _patched_open_factory(src_bytes, tmp_path)
    with patch("rasterio.open", side_effect=patched_open):
        with pytest.raises(SoilGridsEmptyError, match="no valid"):
            _fetch_soilgrids_bytes(
                (-93.85, 41.85, -93.45, 42.15), "clay", "0-5cm"
            )


# ---------------------------------------------------------------------------
# End-to-end through read_through -> LayerURI.
# ---------------------------------------------------------------------------


def test_end_to_end_returns_layeruri(tmp_path):
    fake = _FakeStore()
    injector = _make_read_through_injector(fake)
    src_bytes = _synth_soilgrids_tif_bytes(raw_value=300)
    patched_open, _ = _patched_open_factory(src_bytes, tmp_path)

    with patch("trid3nt_server.tools.fetchers.soil.fetch_soilgrids.read_through", side_effect=injector), \
         patch("rasterio.open", side_effect=patched_open):
        layer = fetch_soilgrids(
            bbox=(-93.85, 41.85, -93.45, 42.15),
            soil_property="clay",
            depth="0-5cm",
        )
        # Cache write happened; a second identical call hits the store.
        layer2 = fetch_soilgrids(
            bbox=(-93.85, 41.85, -93.45, 42.15),
            soil_property="clay",
            depth="0-5cm",
        )

    assert layer.layer_type == "raster"
    assert layer.uri and layer.uri.startswith("s3://")
    assert layer.units == "percent"
    assert layer.style_preset == "soil_clay_pct"
    assert layer.role == "input"
    assert "clay" in layer.layer_id and "0-5cm" in layer.layer_id
    assert layer2.uri == layer.uri
    assert len(fake.store) == 1
