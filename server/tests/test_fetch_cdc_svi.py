"""Unit tests for the ``fetch_cdc_svi`` atomic tool.

Coverage:
- ``_features_to_flatgeobuf``: synthetic SVI tract features -> FlatGeobuf with
  the overall + 4-theme columns; CDC ``-999`` null sentinel normalized to null;
  round-trips back through geopandas with correct values (compute/shape).
- Honest-empty path: an empty feature list yields a valid zero-feature FGB with
  the full SVI schema (no exception).
- Input validation: degenerate / inverted / out-of-range / wrong-length bboxes
  raise ``CDC_SVIInputError``.
- Registration metadata, payload estimator, URL building, score normalization.

These tests use SYNTHETIC GeoJSON (no network) so they are deterministic and
offline-safe. The live-source path is proven separately in the prototype +
S3 read-through round-trip captured in the build report.
"""

from __future__ import annotations

import tempfile

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_cdc_svi import (
    CDC_SVIInputError,
    CDC_SVIUpstreamError,
    SVI_NULL_SENTINEL,
    _build_svi_url,
    _features_to_flatgeobuf,
    _normalize_score,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_cdc_svi,
)

geopandas = pytest.importorskip("geopandas")


# ---------------------------------------------------------------------------
# Synthetic feature builders.
# ---------------------------------------------------------------------------


def _square(cx: float, cy: float, h: float = 0.01) -> dict:
    """A small closed square polygon ring centered at (cx, cy)."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [cx - h, cy - h],
            [cx + h, cy - h],
            [cx + h, cy + h],
            [cx - h, cy + h],
            [cx - h, cy - h],
        ]],
    }


def _tract_feature(
    fips: str,
    rpl_themes,
    theme1,
    theme2,
    theme3,
    theme4,
    total_pop,
    cx: float = -95.3,
    cy: float = 29.7,
) -> dict:
    return {
        "type": "Feature",
        "geometry": _square(cx, cy),
        "properties": {
            "FIPS": fips,
            "STATE": "Texas",
            "ST_ABBR": "TX",
            "COUNTY": "Harris County",
            "LOCATION": f"Census Tract {fips}",
            "E_TOTPOP": total_pop,
            "RPL_THEMES": rpl_themes,
            "RPL_THEME1": theme1,
            "RPL_THEME2": theme2,
            "RPL_THEME3": theme3,
            "RPL_THEME4": theme4,
        },
    }


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_cdc_svi" in TOOL_REGISTRY


def test_metadata_fields():
    spec = TOOL_REGISTRY["fetch_cdc_svi"]
    md = spec.metadata
    assert md.name == "fetch_cdc_svi"
    assert md.ttl_class == "static-30d"
    assert md.source_class == "cdc_svi"
    assert md.cacheable is True


# ---------------------------------------------------------------------------
# Compute / shape correctness on synthetic input.
# ---------------------------------------------------------------------------


def _read_fgb(fgb_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    return geopandas.read_file(path)


def test_features_to_fgb_shape_and_values():
    feats = [
        _tract_feature("48201100001", 0.622, 0.7361, 0.0274, 0.647, 0.9106, 5256),
        _tract_feature("48201100002", 0.1003, 0.2, 0.3, 0.4, 0.5, 3120, cx=-95.31),
    ]
    fgb = _features_to_flatgeobuf(feats)
    assert isinstance(fgb, bytes) and len(fgb) > 0

    gdf = _read_fgb(fgb)
    assert len(gdf) == 2
    for col in (
        "fips", "county", "state_abbr", "location", "total_pop",
        "rpl_themes", "rpl_theme1", "rpl_theme2", "rpl_theme3", "rpl_theme4",
    ):
        assert col in gdf.columns, f"missing column {col}"

    row0 = gdf[gdf["fips"] == "48201100001"].iloc[0]
    assert row0["county"] == "Harris County"
    assert row0["state_abbr"] == "TX"
    assert row0["total_pop"] == 5256
    assert abs(row0["rpl_themes"] - 0.622) < 1e-6
    assert abs(row0["rpl_theme4"] - 0.9106) < 1e-6
    # geometry preserved as polygon
    assert row0.geometry.geom_type == "Polygon"


def test_minus_999_sentinel_normalized_to_null():
    # A suppressed tract: all percentile ranks and population are -999.
    feats = [
        _tract_feature(
            "48201990000",
            SVI_NULL_SENTINEL, SVI_NULL_SENTINEL, SVI_NULL_SENTINEL,
            SVI_NULL_SENTINEL, SVI_NULL_SENTINEL, -999,
        ),
        _tract_feature("48201100003", 0.5, 0.5, 0.5, 0.5, 0.5, 1000, cx=-95.29),
    ]
    fgb = _features_to_flatgeobuf(feats)
    gdf = _read_fgb(fgb)

    suppressed = gdf[gdf["fips"] == "48201990000"].iloc[0]
    # All -999 ranks become null (NaN); a choropleth must NOT render these as 0.
    assert suppressed["rpl_themes"] != suppressed["rpl_themes"]  # NaN check
    assert suppressed["rpl_theme1"] != suppressed["rpl_theme1"]
    # total_pop -999 -> null
    assert (
        suppressed["total_pop"] is None
        or suppressed["total_pop"] != suppressed["total_pop"]
    )

    good = gdf[gdf["fips"] == "48201100003"].iloc[0]
    assert abs(good["rpl_themes"] - 0.5) < 1e-6


def test_normalize_score():
    assert _normalize_score(0.622) == 0.622
    assert _normalize_score(0.0) == 0.0
    assert _normalize_score(1.0) == 1.0
    assert _normalize_score(-999.0) is None
    assert _normalize_score(-1000.0) is None
    assert _normalize_score(None) is None
    assert _normalize_score("not-a-number") is None
    assert _normalize_score("0.42") == 0.42  # numeric string coerces


# ---------------------------------------------------------------------------
# Honest-empty path.
# ---------------------------------------------------------------------------


def test_empty_features_yields_valid_empty_fgb():
    fgb = _features_to_flatgeobuf([])
    assert isinstance(fgb, bytes) and len(fgb) > 0
    gdf = _read_fgb(fgb)
    assert len(gdf) == 0
    # Schema still carries the SVI columns so the layer renders a zero-feature
    # notice rather than crashing downstream styling.
    for col in ("fips", "rpl_themes", "rpl_theme1", "rpl_theme4"):
        assert col in gdf.columns


def test_features_with_null_geometry_skipped():
    feats = [
        {"type": "Feature", "geometry": None, "properties": {"FIPS": "x"}},
        _tract_feature("48201100009", 0.3, 0.3, 0.3, 0.3, 0.3, 500),
    ]
    fgb = _features_to_flatgeobuf(feats)
    gdf = _read_fgb(fgb)
    assert len(gdf) == 1
    assert gdf.iloc[0]["fips"] == "48201100009"


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_validate_bbox_ok():
    _validate_bbox((-95.45, 29.65, -95.25, 29.85))  # no raise


def test_validate_bbox_degenerate():
    with pytest.raises(CDC_SVIInputError):
        _validate_bbox((-95.0, 29.0, -95.0, 30.0))


def test_validate_bbox_inverted():
    with pytest.raises(CDC_SVIInputError):
        _validate_bbox((-95.0, 30.0, -95.5, 29.0))


def test_validate_bbox_out_of_range_lon():
    with pytest.raises(CDC_SVIInputError):
        _validate_bbox((-200.0, 29.0, -95.0, 30.0))


def test_validate_bbox_out_of_range_lat():
    with pytest.raises(CDC_SVIInputError):
        _validate_bbox((-95.0, -100.0, -94.0, 30.0))


def test_validate_bbox_wrong_length():
    with pytest.raises(CDC_SVIInputError):
        _validate_bbox((-95.0, 29.0, -94.0))  # type: ignore[arg-type]


def test_validate_bbox_non_finite():
    with pytest.raises(CDC_SVIInputError):
        _validate_bbox((float("nan"), 29.0, -94.0, 30.0))


def test_fetch_rejects_bad_bbox_tuple_coercion():
    with pytest.raises(CDC_SVIInputError):
        fetch_cdc_svi(bbox=(0.0, 0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# URL building / payload / rounding helpers.
# ---------------------------------------------------------------------------


def test_build_svi_url_params():
    url, params = _build_svi_url((-95.45, 29.65, -95.25, 29.85), offset=2000)
    assert "FeatureServer/2/query" in url
    assert params["geometry"] == "-95.45,29.65,-95.25,29.85"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["inSR"] == "4326"
    assert params["outSR"] == "4326"
    assert params["f"] == "geojson"
    assert params["resultOffset"] == "2000"
    assert "RPL_THEMES" in params["outFields"]
    assert "RPL_THEME4" in params["outFields"]


def test_estimate_payload_mb_scales_with_area():
    small = estimate_payload_mb(bbox=(-95.30, 29.70, -95.29, 29.71))
    big = estimate_payload_mb(bbox=(-96.0, 29.0, -95.0, 30.0))
    assert small < big
    assert small >= 0.02  # lower clamp
    assert big <= 80.0  # upper clamp


def test_estimate_payload_mb_none_bbox():
    assert estimate_payload_mb() == 3.0


def test_round_bbox_to_6dp():
    out = _round_bbox_to_6dp((-95.123456789, 29.987654321, -94.0, 30.0))
    assert out == (-95.123457, 29.987654, -94.0, 30.0)


# ---------------------------------------------------------------------------
# Error-class wiring.
# ---------------------------------------------------------------------------


def test_error_class_attributes():
    assert CDC_SVIInputError.retryable is False
    assert CDC_SVIUpstreamError.retryable is True
    assert CDC_SVIInputError.error_code == "CDC_SVI_INPUT_INVALID"
    assert CDC_SVIUpstreamError.error_code == "CDC_SVI_UPSTREAM_ERROR"
