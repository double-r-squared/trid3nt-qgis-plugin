"""Unit tests for the ``fetch_census_acs`` atomic tool.

Coverage:
- ``_resolve_variable``: friendly names map to ACS table specs; raw ACS codes
  pass through; unknown / malformed variables raise ``CensusACSInputError``.
- ``_parse_data_census_rows``: data.census.gov backend rows -> {geoid: {code}};
  GEO_ID prefix stripped to 11-digit; ACS jam sentinels mapped to null.
- ``_compute_value``: value-kind returns the named estimate; pct-kind computes
  100 * sum(num) / denom; null denom / missing num -> null.
- ``_features_to_flatgeobuf``: synthetic tract geometry joined to synthetic
  ACS values by GEOID -> FlatGeobuf with the choropleth schema; round-trips
  through geopandas with correct values; unmatched GEOID -> null value.
- Honest-empty path: an empty tract list yields a valid zero-feature FGB with
  the full schema (no exception).
- Input validation: degenerate / inverted / out-of-range / wrong-length bboxes
  and out-of-range years raise ``CensusACSInputError``.
- Registration metadata + payload estimator.

These tests use SYNTHETIC data (no network) so they are deterministic and
offline-safe. The live two-source pipeline (TIGERweb geometry + data.census.gov
estimates, joined by GEOID) + the real S3 read-through round-trip are proven
separately in the prototype captured in the build report.
"""

from __future__ import annotations

import tempfile

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_census_acs import (
    ACS_VARIABLES,
    CensusACSInputError,
    _compute_value,
    _features_to_flatgeobuf,
    _parse_data_census_rows,
    _resolve_variable,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_census_acs,
)

geopandas = pytest.importorskip("geopandas")


# ---------------------------------------------------------------------------
# Synthetic builders.
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


def _tract_feature(geoid: str, cx: float = -95.3, cy: float = 29.7) -> dict:
    """A synthetic TIGERweb tract GeoJSON feature."""
    return {
        "type": "Feature",
        "geometry": _square(cx, cy),
        "properties": {
            "GEOID": geoid,
            "NAME": f"Census Tract {geoid[-4:]}",
            "STATE": geoid[:2],
            "COUNTY": geoid[2:5],
            "TRACT": geoid[5:],
        },
    }


# ---------------------------------------------------------------------------
# _resolve_variable.
# ---------------------------------------------------------------------------


def test_resolve_friendly_value_variable():
    spec = _resolve_variable("median_income")
    assert spec["kind"] == "value"
    assert spec["code"] == "B19013_001E"
    assert spec["table"] == "B19013"
    assert spec["units"] == "usd"
    assert spec["friendly"] == "median_income"


def test_resolve_friendly_pct_variable():
    spec = _resolve_variable("poverty_rate")
    assert spec["kind"] == "pct"
    assert spec["denom"] == "B17001_001E"
    assert "B17001_002E" in spec["num"]
    assert spec["units"] == "percent"


def test_resolve_friendly_is_case_insensitive():
    assert _resolve_variable("Median_Income")["code"] == "B19013_001E"


def test_resolve_raw_acs_code_passthrough():
    spec = _resolve_variable("B25064_001E")  # median gross rent
    assert spec["kind"] == "value"
    assert spec["code"] == "B25064_001E"
    assert spec["table"] == "B25064"
    assert spec["friendly"] == "B25064_001E"


@pytest.mark.parametrize("bad", ["", "   ", "not_a_var", "B19013", "19013_001E", "median_unicorn"])
def test_resolve_unknown_variable_raises(bad):
    with pytest.raises(CensusACSInputError):
        _resolve_variable(bad)


def test_resolve_non_string_variable_raises():
    with pytest.raises(CensusACSInputError):
        _resolve_variable(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _parse_data_census_rows.
# ---------------------------------------------------------------------------


def test_parse_data_census_rows_strips_geoid_and_selects_estimates():
    rows = [
        ["B19013_001M", "GEO_ID", "B19013_001EA", "B19013_001E", "NAME"],
        ["20028", "1400000US48201100001", None, "84213", "Tract 1000.01"],
        ["18580", "1400000US48201210400", None, "51964", "Tract 2104"],
    ]
    out = _parse_data_census_rows(rows, "B19013")
    assert set(out) == {"48201100001", "48201210400"}
    assert out["48201100001"]["B19013_001E"] == 84213.0
    # Margin (M) and annotation (EA) columns are NOT selected as estimates.
    assert "B19013_001M" not in out["48201100001"]
    assert "B19013_001EA" not in out["48201100001"]


def test_parse_data_census_rows_maps_jam_sentinel_to_null():
    rows = [
        ["GEO_ID", "B19013_001E"],
        ["1400000US48201111111", "-666666666"],  # ACS jam value
    ]
    out = _parse_data_census_rows(rows, "B19013")
    assert out["48201111111"]["B19013_001E"] is None


def test_parse_data_census_rows_empty():
    assert _parse_data_census_rows([], "B19013") == {}
    assert _parse_data_census_rows([["B19013_001E"]], "B19013") == {}  # no GEO_ID


# ---------------------------------------------------------------------------
# _compute_value.
# ---------------------------------------------------------------------------


def test_compute_value_kind_returns_named_estimate():
    spec = _resolve_variable("median_income")
    assert _compute_value(spec, {"B19013_001E": 62500.0}) == 62500.0
    assert _compute_value(spec, {"B19013_001E": None}) is None
    assert _compute_value(spec, None) is None


def test_compute_pct_kind():
    spec = _resolve_variable("poverty_rate")  # 100 * B17001_002E / B17001_001E
    val = _compute_value(spec, {"B17001_002E": 150.0, "B17001_001E": 1000.0})
    assert val == pytest.approx(15.0)


def test_compute_pct_multi_numerator():
    spec = _resolve_variable("pct_no_vehicle")  # owner + renter no-vehicle
    val = _compute_value(
        spec, {"B25044_003E": 40.0, "B25044_010E": 60.0, "B25044_001E": 500.0}
    )
    assert val == pytest.approx(20.0)  # (40+60)/500 * 100


def test_compute_pct_zero_or_missing_denominator_is_null():
    spec = _resolve_variable("pct_renters")
    assert _compute_value(spec, {"B25003_003E": 5.0, "B25003_001E": 0.0}) is None
    assert _compute_value(spec, {"B25003_003E": 5.0, "B25003_001E": None}) is None


def test_compute_pct_missing_numerator_is_null():
    spec = _resolve_variable("pct_renters")
    assert _compute_value(spec, {"B25003_001E": 100.0}) is None  # num missing


# ---------------------------------------------------------------------------
# _features_to_flatgeobuf (join + serialize).
# ---------------------------------------------------------------------------


def _read_fgb(fgb_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=True) as f:
        f.write(fgb_bytes)
        f.flush()
        return geopandas.read_file(f.name)


def test_join_and_serialize_value_variable():
    spec = _resolve_variable("median_income")
    tracts = [
        _tract_feature("48201100001", cx=-95.30, cy=29.70),
        _tract_feature("48201210400", cx=-95.32, cy=29.72),
        _tract_feature("48201999999", cx=-95.34, cy=29.74),  # no ACS match -> null
    ]
    values = {
        "48201100001": {"B19013_001E": 84213.0},
        "48201210400": {"B19013_001E": 51964.0},
    }
    fgb = _features_to_flatgeobuf(tracts, values, spec)
    gdf = _read_fgb(fgb)
    assert len(gdf) == 3
    assert set(gdf.columns) >= {
        "geoid", "name", "state", "county", "variable", "value", "units", "geometry"
    }
    by_geoid = {r["geoid"]: r for _, r in gdf.iterrows()}
    assert by_geoid["48201100001"]["value"] == 84213.0
    assert by_geoid["48201210400"]["value"] == 51964.0
    # Unmatched GEOID -> null value (honest, not fabricated).
    assert by_geoid["48201999999"]["value"] is None or (
        by_geoid["48201999999"]["value"] != by_geoid["48201999999"]["value"]  # NaN
    )
    assert (gdf["variable"] == "median_income").all()
    assert (gdf["units"] == "usd").all()
    assert (gdf["state"] == "48").all()


def test_join_and_serialize_pct_variable():
    spec = _resolve_variable("poverty_rate")
    tracts = [_tract_feature("06037123456")]
    values = {"06037123456": {"B17001_002E": 250.0, "B17001_001E": 1000.0}}
    gdf = _read_fgb(_features_to_flatgeobuf(tracts, values, spec))
    assert gdf.iloc[0]["value"] == pytest.approx(25.0)
    assert gdf.iloc[0]["units"] == "percent"
    assert gdf.iloc[0]["variable"] == "poverty_rate"


def test_empty_tracts_yields_valid_zero_feature_fgb():
    spec = _resolve_variable("median_income")
    fgb = _features_to_flatgeobuf([], {}, spec)
    assert isinstance(fgb, bytes) and len(fgb) > 0
    gdf = _read_fgb(fgb)
    assert len(gdf) == 0


def test_feature_with_no_geometry_is_dropped():
    spec = _resolve_variable("median_age")
    tracts = [
        _tract_feature("48201100001"),
        {"type": "Feature", "geometry": None, "properties": {"GEOID": "48201100002"}},
    ]
    values = {"48201100001": {"B01002_001E": 35.0}}
    gdf = _read_fgb(_features_to_flatgeobuf(tracts, values, spec))
    assert len(gdf) == 1
    assert gdf.iloc[0]["value"] == 35.0


# ---------------------------------------------------------------------------
# Bbox + year validation.
# ---------------------------------------------------------------------------


def test_validate_bbox_accepts_valid():
    _validate_bbox((-95.45, 29.65, -95.25, 29.85))  # no raise


@pytest.mark.parametrize("bbox", [
    (-95.25, 29.65, -95.45, 29.85),   # inverted lon
    (-95.45, 29.85, -95.25, 29.65),   # inverted lat
    (-95.45, 29.65, -95.45, 29.85),   # degenerate lon
    (-200.0, 29.65, -95.25, 29.85),   # lon out of range
    (-95.45, -91.0, -95.25, 29.85),   # lat out of range
    (-95.45, 29.65, -95.25),          # wrong length
    (-95.45, float("nan"), -95.25, 29.85),  # non-finite
])
def test_validate_bbox_rejects_invalid(bbox):
    with pytest.raises(CensusACSInputError):
        _validate_bbox(bbox)


def test_round_bbox_to_6dp():
    assert _round_bbox_to_6dp((-95.123456789, 29.1, -95.0, 29.999999999)) == (
        -95.123457, 29.1, -95.0, 30.0
    )


def test_fetch_rejects_unknown_variable_before_network():
    # _resolve_variable runs before any fetch; an unknown variable must raise
    # an input error without touching the network.
    with pytest.raises(CensusACSInputError):
        fetch_census_acs(bbox=(-95.45, 29.65, -95.25, 29.85), variable="median_unicorn")


def test_fetch_rejects_bad_year_before_network():
    with pytest.raises(CensusACSInputError):
        fetch_census_acs(bbox=(-95.45, 29.65, -95.25, 29.85), year=1990)


def test_fetch_rejects_bad_bbox_before_network():
    with pytest.raises(CensusACSInputError):
        fetch_census_acs(bbox=(-95.25, 29.65, -95.45, 29.85))  # inverted lon


# ---------------------------------------------------------------------------
# Metadata + estimator + registration.
# ---------------------------------------------------------------------------


def test_payload_estimator_scales_with_area():
    small = estimate_payload_mb((-95.45, 29.65, -95.25, 29.85))   # ~0.04 sq deg
    big = estimate_payload_mb((-96.0, 29.0, -95.0, 30.0))         # 1 sq deg
    assert 0.02 <= small <= big <= 80.0
    assert estimate_payload_mb(None) > 0.0


def test_acs_variables_registry_well_formed():
    for name, spec in ACS_VARIABLES.items():
        assert spec["kind"] in ("value", "pct")
        assert spec["table"].startswith(("B", "C"))
        if spec["kind"] == "value":
            assert spec["code"].startswith(spec["table"])
        else:
            assert spec["denom"].startswith(spec["table"])
            assert all(c.startswith(spec["table"]) for c in spec["num"])


def test_tool_is_registered():
    assert "fetch_census_acs" in TOOL_REGISTRY
