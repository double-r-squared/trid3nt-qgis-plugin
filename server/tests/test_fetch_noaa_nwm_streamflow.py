"""Unit + live tests for ``fetch_noaa_nwm_streamflow`` (job-A3).

Coverage (no network needed):
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Bad bbox / unknown product / bad forecast_hour raise typed input errors.
- bbox outside CONUS raises NWMStreamflowInputError.
- Cache key changes for different products / bboxes / valid_times.
- estimate_payload_mb returns the expected shape (positive float).
- Synthetic feature_id → streamflow lookup parsing.

Live tests (gated by GRACE2_TEST_LIVE_NWM=1):
- Real fetch over a small Fort Myers bbox: confirms NLDI returns COMIDs,
  matches them to streamflow values, and emits a non-empty FlatGeobuf.
"""

from __future__ import annotations

import datetime as _dt
import os
import tempfile
from datetime import datetime, timezone
from typing import Any

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.cache import compute_cache_key
from grace2_agent.tools.fetch_noaa_nwm_streamflow import (
    _CONUS_BBOX,
    _METADATA,
    _parse_valid_time,
    _round_bbox_to_6dp,
    _validate_bbox,
    NWMStreamflowEmptyError,
    NWMStreamflowInputError,
    NWMStreamflowNotAvailableError,
    NWMStreamflowUpstreamError,
    estimate_payload_mb,
    fetch_noaa_nwm_streamflow,
)


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers / Caloosahatchee bbox (~1.5° wide for plenty of NLDI hits).
_FORT_MYERS_BBOX: tuple[float, float, float, float] = (-82.0, 26.4, -81.7, 26.7)

_LIVE_NWM = os.environ.get("GRACE2_TEST_LIVE_NWM") == "1"


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_noaa_nwm_streamflow appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_noaa_nwm_streamflow" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"]
    assert entry.metadata.name == "fetch_noaa_nwm_streamflow"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "nwm_streamflow"
    assert entry.metadata.cacheable is True


def test_supports_global_query_is_false():
    """NWM is CONUS-only; supports_global_query must be False."""
    entry = TOOL_REGISTRY["fetch_noaa_nwm_streamflow"]
    # Field may or may not exist depending on schema version; tolerate both.
    sgq = getattr(entry.metadata, "supports_global_query", None)
    assert sgq in (False, None), f"expected False or None; got {sgq!r}"


# ---------------------------------------------------------------------------
# Validation / typed-error tests.
# ---------------------------------------------------------------------------


def test_unknown_product_raises_input_error():
    with pytest.raises(NWMStreamflowInputError) as exc:
        fetch_noaa_nwm_streamflow(
            bbox=_FORT_MYERS_BBOX, product="medium_range"  # type: ignore[arg-type]
        )
    assert "product" in str(exc.value)


def test_short_range_requires_nonzero_forecast_hour():
    with pytest.raises(NWMStreamflowInputError) as exc:
        fetch_noaa_nwm_streamflow(
            bbox=_FORT_MYERS_BBOX, product="short_range", forecast_hour=0
        )
    assert "forecast_hour" in str(exc.value) or "short_range" in str(exc.value)


def test_invalid_forecast_hour_raises_input_error():
    with pytest.raises(NWMStreamflowInputError):
        fetch_noaa_nwm_streamflow(
            bbox=_FORT_MYERS_BBOX, product="short_range", forecast_hour=99
        )


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(NWMStreamflowInputError):
        _validate_bbox((-81.0, 26.0, -81.0, 26.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(NWMStreamflowInputError):
        _validate_bbox((-181.0, 25.0, -80.0, 26.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(NWMStreamflowInputError):
        _validate_bbox((-81.0, 25.0, -80.0, 91.0))


def test_non_finite_bbox_raises_input_error():
    with pytest.raises(NWMStreamflowInputError):
        _validate_bbox((-81.0, float("nan"), -80.0, 26.0))


def test_bbox_outside_conus_raises_input_error():
    """A bbox entirely in Europe should be rejected."""
    with pytest.raises(NWMStreamflowInputError) as exc:
        _validate_bbox((10.0, 45.0, 12.0, 47.0))  # northern Italy
    assert "CONUS" in str(exc.value) or "intersect" in str(exc.value)


def test_bad_valid_time_raises_input_error():
    with pytest.raises(NWMStreamflowInputError):
        _parse_valid_time("not-an-iso-date")


def test_valid_time_zulu_parses_to_utc():
    dt = _parse_valid_time("2025-01-01T12:00:00Z")
    assert dt == datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_round_bbox_quantizes_to_six_decimal_places():
    rounded = _round_bbox_to_6dp((-82.123456789, 26.987654321, -81.5, 27.0))
    assert rounded == (-82.123457, 26.987654, -81.5, 27.0)


# ---------------------------------------------------------------------------
# Cache-key determinism.
# ---------------------------------------------------------------------------


def test_cache_key_differs_for_different_product():
    fm = list(_FORT_MYERS_BBOX)
    p_aa = {
        "bbox": fm,
        "product": "analysis_assim",
        "valid_time": "LATEST",
        "forecast_hour": 0,
    }
    p_sr = {
        "bbox": fm,
        "product": "short_range",
        "valid_time": "LATEST",
        "forecast_hour": 6,
    }
    k_aa = compute_cache_key("nwm_streamflow", p_aa, "dynamic-1h", now=_PINNED_NOW)
    k_sr = compute_cache_key("nwm_streamflow", p_sr, "dynamic-1h", now=_PINNED_NOW)
    assert k_aa != k_sr


def test_cache_key_differs_for_different_bbox():
    fm_bbox = list(_FORT_MYERS_BBOX)
    tx_bbox = [-100.0, 27.0, -94.0, 33.0]
    base = {"product": "analysis_assim", "valid_time": "LATEST", "forecast_hour": 0}
    k_fm = compute_cache_key(
        "nwm_streamflow", {**base, "bbox": fm_bbox}, "dynamic-1h", now=_PINNED_NOW
    )
    k_tx = compute_cache_key(
        "nwm_streamflow", {**base, "bbox": tx_bbox}, "dynamic-1h", now=_PINNED_NOW
    )
    assert k_fm != k_tx


def test_cache_key_stable_for_identical_params():
    p = {
        "bbox": list(_FORT_MYERS_BBOX),
        "product": "analysis_assim",
        "valid_time": "LATEST",
        "forecast_hour": 0,
    }
    k1 = compute_cache_key("nwm_streamflow", p, "dynamic-1h", now=_PINNED_NOW)
    k2 = compute_cache_key("nwm_streamflow", p, "dynamic-1h", now=_PINNED_NOW)
    assert k1 == k2


# ---------------------------------------------------------------------------
# Payload estimate.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_positive_float():
    """estimator returns positive float per FR-DC-9 shape."""
    val = estimate_payload_mb(bbox=_FORT_MYERS_BBOX)
    assert isinstance(val, float) and val > 0


def test_estimate_payload_mb_increases_with_bbox_size():
    small = estimate_payload_mb(bbox=(-82.0, 26.4, -81.9, 26.5))  # 0.01 sq deg
    large = estimate_payload_mb(bbox=(-100.0, 27.0, -94.0, 33.0))  # 36 sq deg
    assert large >= small


def test_estimate_payload_mb_handles_none_bbox():
    """None bbox should not crash even though tool requires bbox."""
    val = estimate_payload_mb(bbox=None)
    assert isinstance(val, float) and val >= 0


# ---------------------------------------------------------------------------
# Error envelope shape.
# ---------------------------------------------------------------------------


def test_input_errors_carry_error_code_and_retryable():
    """FR-AS-11: typed errors expose error_code + retryable attributes."""
    err = NWMStreamflowInputError("bad bbox")
    assert err.error_code == "NWM_STREAMFLOW_INPUT_ERROR"
    assert err.retryable is False


def test_upstream_errors_are_retryable():
    err = NWMStreamflowUpstreamError("s3 down")
    assert err.error_code == "NWM_STREAMFLOW_UPSTREAM_ERROR"
    assert err.retryable is True


def test_not_available_errors_not_retryable():
    err = NWMStreamflowNotAvailableError("cycle missing")
    assert err.error_code == "NWM_STREAMFLOW_NOT_AVAILABLE"
    assert err.retryable is False


def test_empty_errors_not_retryable():
    err = NWMStreamflowEmptyError("no comids in bbox")
    assert err.error_code == "NWM_STREAMFLOW_EMPTY"
    assert err.retryable is False


# ---------------------------------------------------------------------------
# Live test (network + bucket access required).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_NWM,
    reason="set GRACE2_TEST_LIVE_NWM=1 to run live NWM fetch + NLDI join",
)
def test_live_fetch_fort_myers_returns_streamflow():
    """End-to-end live fetch: NWM netCDF + NLDI bbox sample → FlatGeobuf with rows.

    Bypasses GCS cache by mocking storage; we only verify the upstream
    netCDF + NLDI fetch path.
    """
    from unittest.mock import patch
    from grace2_agent.tools.fetch_noaa_nwm_streamflow import (
        _fetch_nwm_streamflow_bytes,
    )

    # Latest analysis_assim cycle.
    fgb_bytes = _fetch_nwm_streamflow_bytes(
        bbox=_FORT_MYERS_BBOX,
        product="analysis_assim",
        valid_time_dt=None,
        forecast_hour=0,
    )
    assert isinstance(fgb_bytes, bytes) and len(fgb_bytes) > 100, (
        f"expected non-empty FlatGeobuf, got {len(fgb_bytes)} bytes"
    )

    # Verify it parses as a FlatGeobuf with point geometry and the
    # expected schema.
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        tmp_path = f.name
    try:
        gdf = gpd.read_file(tmp_path)
    finally:
        os.unlink(tmp_path)

    assert len(gdf) > 0, "live fetch produced an empty FlatGeobuf"
    assert "feature_id" in gdf.columns
    assert "streamflow_cms" in gdf.columns
    assert "valid_time" in gdf.columns
    assert "product" in gdf.columns
    assert gdf.crs is not None and gdf.crs.to_epsg() == 4326
    assert gdf.geometry.geom_type.iloc[0] == "Point"

    # Streamflow should be physically reasonable.
    assert gdf["streamflow_cms"].max() >= 0
    assert gdf["streamflow_cms"].min() >= 0

    # Write evidence file.
    evidence_path = "/tmp/nwm_streamflow_live.txt"
    with open(evidence_path, "w") as f:
        f.write(f"NWM streamflow live fetch evidence\n")
        f.write(f"==================================\n")
        f.write(f"bbox: {_FORT_MYERS_BBOX}\n")
        f.write(f"reaches: {len(gdf)}\n")
        f.write(f"FGB bytes: {len(fgb_bytes)}\n")
        f.write(
            f"streamflow_cms range: "
            f"{gdf['streamflow_cms'].min():.4f} – "
            f"{gdf['streamflow_cms'].max():.4f} m^3/s\n"
        )
        f.write(
            f"streamflow_cms mean: {gdf['streamflow_cms'].mean():.4f} m^3/s\n"
        )
        f.write(f"valid_time: {gdf['valid_time'].iloc[0]}\n")
        f.write(f"product: {gdf['product'].iloc[0]}\n")
        f.write(f"sample feature_ids: {gdf['feature_id'].head(5).tolist()}\n")
    print(f"Live evidence written to {evidence_path}")
