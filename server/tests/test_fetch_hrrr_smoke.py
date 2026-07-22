"""Unit tests for the ``fetch_hrrr_smoke`` atomic tool (Wave 4.10 job-A13).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata (Wave 1.5 flags:
  ``supports_global_query=False`` + payload-MB estimator).
- Validation: bad bbox / non-CONUS bbox / bad variable / out-of-range
  forecast_hour raise typed errors with ``retryable=False``.
- FR-DC-6 cross-field: cacheable + dynamic-1h + non-empty source_class.
- Payload-MB estimator returns sensible numbers across bbox scales.
- _build_zarr_paths produces the expected outer/inner S3 paths for all
  three supported smoke variables.
- _cycle_key matches the documented mirror layout.
- Description audit gate: docstring carries the 6-point audit shape.

Live tests (env-gated ``TRID3NT_TEST_LIVE_HRRR_SMOKE=1``):
- Live fetch of a small northern-California / southern-Oregon bbox via the
  real S3 mirror. Confirms the published cycle resolves, the slice clips
  inside the bbox, and the returned values are physically plausible
  (e.g. near-surface smoke mass density ≥ 0 kg m-3 with finite finite range).
"""

from __future__ import annotations

import datetime as _dt
import os

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.weather.fetch_hrrr_smoke import (
    HRRRSmokeEmptyError,
    HRRRSmokeError,
    HRRRSmokeInputError,
    HRRRSmokeUpstreamError,
    _build_zarr_paths,
    _cycle_key,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_forecast_hour,
    _validate_variable,
    estimate_payload_mb,
    fetch_hrrr_smoke,
)

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# Northern California / southern Oregon — common wildfire-smoke bbox.
_NORCAL_BBOX = (-124.0, 41.0, -121.0, 43.0)

# Small Fort Myers, FL bbox — used by lighter mocked validation tests.
_FORT_MYERS_BBOX = (-82.0, 26.4, -81.6, 26.8)

# Non-CONUS bbox (Hawaii) — used to verify the CONUS gate.
_HAWAII_BBOX = (-158.0, 21.0, -157.5, 21.5)

_LIVE_HRRR_SMOKE = os.environ.get("TRID3NT_TEST_LIVE_HRRR_SMOKE") == "1"


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_hrrr_smoke appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_hrrr_smoke" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_hrrr_smoke"]
    assert entry.metadata.name == "fetch_hrrr_smoke"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "hrrr_smoke"
    assert entry.metadata.cacheable is True
    # Wave 1.5 flags. supports_global_query=False because HRRR-Smoke is CONUS-only.
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_fr_dc_6_cross_field_consistency():
    """Registered metadata satisfies FR-DC-6: cacheable ⇒ ttl != live, src non-empty."""
    md = TOOL_REGISTRY["fetch_hrrr_smoke"].metadata
    assert md.cacheable is True
    assert md.ttl_class != "live-no-cache"
    assert md.source_class


def test_description_contains_audit_clauses():
    """Description audit gate: docstring carries the 6-point audit shape."""
    doc = fetch_hrrr_smoke.__doc__ or ""
    assert "What it does" in doc
    assert "When to use" in doc
    assert "When NOT to use" in doc
    assert "Parameters" in doc
    assert "Returns" in doc
    assert "Cross-tool dependencies" in doc
    # Target word count 150-300; we don't enforce a strict cap but check
    # the description is substantive.
    words = doc.split()
    assert len(words) > 150, f"docstring too short: {len(words)} words"


def test_error_class_hierarchy():
    """All subclasses derive from HRRRSmokeError so callers can catch broadly."""
    assert issubclass(HRRRSmokeInputError, HRRRSmokeError)
    assert issubclass(HRRRSmokeUpstreamError, HRRRSmokeError)
    assert issubclass(HRRRSmokeEmptyError, HRRRSmokeError)
    # Error codes are FR-AS-11 compliant.
    assert HRRRSmokeInputError.error_code == "HRRR_SMOKE_INPUT_ERROR"
    assert HRRRSmokeInputError.retryable is False
    assert HRRRSmokeUpstreamError.error_code == "HRRR_SMOKE_UPSTREAM_ERROR"
    assert HRRRSmokeUpstreamError.retryable is True
    assert HRRRSmokeEmptyError.error_code == "HRRR_SMOKE_EMPTY"
    assert HRRRSmokeEmptyError.retryable is False


# ---------------------------------------------------------------------------
# Validation tests.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(HRRRSmokeInputError):
        _validate_bbox((-82.0, 26.0, -82.0, 26.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(HRRRSmokeInputError):
        _validate_bbox((-181.0, 26.0, -81.0, 27.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(HRRRSmokeInputError):
        _validate_bbox((-82.0, 26.0, -81.0, 91.0))


def test_hawaii_bbox_raises_input_error_conus_only():
    """HRRR-Smoke is CONUS-only; non-CONUS bbox raises HRRRSmokeInputError."""
    with pytest.raises(HRRRSmokeInputError, match="CONUS"):
        _validate_bbox(_HAWAII_BBOX)


def test_norcal_bbox_passes_validation():
    """The NorCal bbox is solidly inside CONUS coverage."""
    _validate_bbox(_NORCAL_BBOX)


def test_fort_myers_bbox_passes_validation():
    _validate_bbox(_FORT_MYERS_BBOX)


def test_invalid_variable_raises_input_error():
    with pytest.raises(HRRRSmokeInputError, match="unsupported HRRR-Smoke variable"):
        _validate_variable("PM25_concentration")


def test_known_variables_pass_validation():
    """All three supported smoke variables validate cleanly."""
    for v in (
        "near_surface_smoke",
        "smoke_column_mass",
        "aerosol_optical_depth",
    ):
        _validate_variable(v)


def test_forecast_hour_below_zero_raises_input_error():
    with pytest.raises(HRRRSmokeInputError):
        _validate_forecast_hour(-1, cycle_hour=0)


def test_forecast_hour_exceeds_standard_cycle_raises_input_error():
    """Non-00/06/12/18 cycles cap at 18 h."""
    with pytest.raises(HRRRSmokeInputError, match="exceeds"):
        _validate_forecast_hour(24, cycle_hour=1)


def test_forecast_hour_48_ok_on_extended_cycle():
    """00z cycle accepts up to 48 h forecast lead."""
    for hr in (0, 6, 12, 18):
        _validate_forecast_hour(48, cycle_hour=hr)


def test_forecast_hour_36_blocked_on_standard_cycle():
    with pytest.raises(HRRRSmokeInputError):
        _validate_forecast_hour(36, cycle_hour=5)


def test_input_error_is_not_retryable():
    """HRRRSmokeInputError carries retryable=False for FR-AS-11 mapping."""
    try:
        fetch_hrrr_smoke(
            bbox=_FORT_MYERS_BBOX,
            variable="not_a_real_var",
            forecast_hour=1,
        )
    except HRRRSmokeInputError as exc:
        assert exc.retryable is False
        assert exc.error_code == "HRRR_SMOKE_INPUT_ERROR"
    else:
        pytest.fail("Expected HRRRSmokeInputError")


def test_bad_cycle_iso_raises_input_error():
    with pytest.raises(HRRRSmokeInputError, match="ISO-8601"):
        fetch_hrrr_smoke(
            bbox=_FORT_MYERS_BBOX,
            variable="near_surface_smoke",
            forecast_hour=1,
            cycle="not-a-date",
        )


def test_extra_kwargs_swallowed():
    """LLM-invented kwargs are absorbed by **_extra_ignored without TypeError."""
    # Should raise because of bbox validation (CONUS gate), NOT
    # because of an unknown kwarg.
    with pytest.raises(HRRRSmokeInputError):
        fetch_hrrr_smoke(
            bbox=_HAWAII_BBOX,
            variable="near_surface_smoke",
            forecast_hour=1,
            hallucinated_param="oh_no",  # type: ignore[call-arg]
            another_fake="yes",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-124.123456789, 41.123456789, -120.987654321, 43.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-124.123457, 41.123457, -120.987654, 43.987654)


def test_cycle_key_format():
    d = _dt.date(2026, 6, 9)
    assert _cycle_key(d, 0) == "20260609_00z_fcst.zarr"
    assert _cycle_key(d, 12) == "20260609_12z_fcst.zarr"


def test_build_zarr_paths_for_massden_near_surface_smoke():
    """Outer + inner S3 paths follow the doubly-nested mirror layout."""
    outer, inner = _build_zarr_paths(
        _dt.date(2026, 6, 9), 0, "8m_above_ground", "MASSDEN"
    )
    assert outer == (
        "s3://hrrrzarr/sfc/20260609/20260609_00z_fcst.zarr/8m_above_ground/MASSDEN"
    )
    assert inner == (
        "s3://hrrrzarr/sfc/20260609/20260609_00z_fcst.zarr/8m_above_ground/MASSDEN/"
        "8m_above_ground"
    )


def test_build_zarr_paths_for_colmd_column_mass():
    outer, inner = _build_zarr_paths(
        _dt.date(2026, 6, 9), 12, "entire_atmosphere_single_layer", "COLMD"
    )
    assert outer.endswith("/entire_atmosphere_single_layer/COLMD")
    assert inner.endswith(
        "/entire_atmosphere_single_layer/COLMD/entire_atmosphere_single_layer"
    )


def test_build_zarr_paths_for_aotk_aod():
    outer, inner = _build_zarr_paths(
        _dt.date(2026, 6, 9), 6, "entire_atmosphere_single_layer", "AOTK"
    )
    assert outer.endswith("/entire_atmosphere_single_layer/AOTK")
    assert inner.endswith(
        "/entire_atmosphere_single_layer/AOTK/entire_atmosphere_single_layer"
    )


# ---------------------------------------------------------------------------
# Payload-MB estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_small_bbox_returns_small_number():
    """A small bbox produces a fraction-of-a-MB payload estimate."""
    mb = estimate_payload_mb(
        bbox=_FORT_MYERS_BBOX,
        variable="near_surface_smoke",
        forecast_hour=1,
    )
    assert mb >= 0.05
    assert mb < 1.0


def test_estimate_payload_mb_full_conus_in_meaningful_range():
    """A CONUS-sized bbox lands in the ~3-7 MB range."""
    full = estimate_payload_mb(
        bbox=(-130.0, 22.0, -65.0, 50.0),
        variable="smoke_column_mass",
        forecast_hour=1,
    )
    assert 3.0 <= full <= 8.0


def test_estimate_payload_mb_none_bbox_returns_default():
    """``bbox=None`` is illegal for the tool but estimator should not raise."""
    mb = estimate_payload_mb(bbox=None)
    assert mb > 0.0


def test_estimate_payload_mb_bad_bbox_returns_default():
    """Malformed bbox arg returns the safe default rather than raising."""
    mb = estimate_payload_mb(bbox="not a bbox")  # type: ignore[arg-type]
    assert mb > 0.0


# ---------------------------------------------------------------------------
# Live test (env-gated). Requires network access to AWS S3 (anonymous).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_HRRR_SMOKE,
    reason="set TRID3NT_TEST_LIVE_HRRR_SMOKE=1 to enable the live HRRR-Smoke smoke",
)
def test_live_fetch_norcal_near_surface_smoke(tmp_path, monkeypatch):
    """Live smoke: fetch near-surface smoke over NorCal, confirm shape + sanity.

    Bypasses the GCS cache shim by patching read_through so the test does
    not require Application Default Credentials. We exercise the live S3
    Zarr read, the LCC → EPSG:4326 reprojection, the bbox clip, and the
    COG write — that's the whole upstream-facing surface.
    """
    from trid3nt_server.tools.fetchers.weather import fetch_hrrr_smoke as mod

    captured: dict[str, bytes] = {}

    def fake_read_through(metadata, params, ext, fetch_fn, **_kw):  # noqa: ANN001
        # Invoke the real fetch — that's the part we want to live-test.
        data = fetch_fn()
        captured["bytes"] = data
        out = tmp_path / "live.tif"
        out.write_bytes(data)
        from trid3nt_server.tools.cache import ReadThroughResult

        return ReadThroughResult(
            uri=f"file://{out}", data=data, hit=False
        )

    monkeypatch.setattr(mod, "read_through", fake_read_through)

    result = fetch_hrrr_smoke(
        bbox=_NORCAL_BBOX,
        variable="near_surface_smoke",
        forecast_hour=1,
    )

    assert result.layer_type == "raster"
    assert result.units == "kg m-3"
    assert result.uri and result.uri.startswith("file://")
    assert "bytes" in captured and len(captured["bytes"]) > 1000

    # Verify physical-plausibility of the recovered raster.
    import numpy as np
    import rasterio

    out_path = result.uri.replace("file://", "")
    with rasterio.open(out_path) as ds:
        arr = ds.read(1)
        bounds = ds.bounds
        crs = ds.crs

    # CRS should be EPSG:4326.
    assert crs.to_epsg() == 4326
    # Bounds should be inside our requested bbox (modulo pixel snapping).
    west, south, east, north = _NORCAL_BBOX
    assert bounds.left >= west - 0.1
    assert bounds.right <= east + 0.1
    assert bounds.bottom >= south - 0.1
    assert bounds.top <= north + 0.1
    # Near-surface smoke mass density: typically 0 .. ~1e-5 kg m-3
    # (i.e. 0 .. ~10 g m-3); we sanity-check non-negative + finite range.
    finite = arr[np.isfinite(arr)]
    assert finite.size > 0
    # MASSDEN is a non-negative concentration.
    assert float(np.nanmin(finite)) >= -1e-9
    # Loose upper bound to catch a totally unphysical decode.
    assert float(np.nanmax(finite)) < 1.0

    # Write evidence for the live capture (sprint convention).
    evidence_dir = os.path.join(
        os.path.dirname(__file__), "..", "evidence"
    )
    os.makedirs(evidence_dir, exist_ok=True)
    evidence_file = os.path.join(evidence_dir, "hrrr_smoke_live.txt")
    with open(evidence_file, "w") as f:
        f.write(
            f"fetch_hrrr_smoke live smoke\n"
            f"  bbox={_NORCAL_BBOX}\n"
            f"  variable=near_surface_smoke\n"
            f"  forecast_hour=1\n"
            f"  cog_bytes={len(captured['bytes'])}\n"
            f"  shape={arr.shape}\n"
            f"  bounds={bounds}\n"
            f"  min={float(np.nanmin(finite)):.3e} kg m-3\n"
            f"  max={float(np.nanmax(finite)):.3e} kg m-3\n"
            f"  mean={float(np.nanmean(finite)):.3e} kg m-3\n"
        )
