"""Unit tests for the ``fetch_lehd_jobs`` atomic tool.

Coverage:
- ``_resolve_segment``: friendly names map to WAC column specs; unknown /
  malformed segments raise ``LehdJobsInputError``; case-insensitive.
- ``_parse_wac_csv``: block-level WAC rows aggregate to tract sums (block ->
  tract by ``w_geocode[:11]``); multi-column segments sum; out-of-state /
  short geocodes are dropped; a header missing the requested column raises an
  honest upstream error (not a silent zero).
- ``_features_to_flatgeobuf``: synthetic tract geometry joined to synthetic
  LODES tract sums by GEOID -> FlatGeobuf with the choropleth schema; round-
  trips through geopandas with correct values; unmatched GEOID -> null value.
- Honest-empty path: an empty tract list yields a valid zero-feature FGB with
  the full schema (no exception).
- Input validation: degenerate / inverted / out-of-range / wrong-length bboxes,
  unknown segments, and out-of-range years raise ``LehdJobsInputError`` BEFORE
  any network call.
- FIPS -> abbreviation registry well-formedness; segment registry well-formed.
- Registration metadata + payload estimator.

These tests use SYNTHETIC data (no network) so they are deterministic and
offline-safe. The live two-source pipeline (TIGERweb geometry + LODES WAC
block-to-tract aggregation, joined by 11-digit GEOID) + the real S3 read-through
round-trip are proven separately in the prototype captured in the build report.
"""

from __future__ import annotations

import tempfile

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.socioeconomic.fetch_lehd_jobs import (
    FIPS_TO_ABBR,
    LODES_SEGMENTS,
    LehdJobsInputError,
    LehdJobsUpstreamError,
    _aggregate_wac_to_tract,
    _features_to_flatgeobuf,
    _parse_wac_csv,
    _resolve_segment,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_lehd_jobs,
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


# A minimal WAC CSV with two tracts' worth of work blocks. Block geocode =
# 15 digits; tract = first 11. Columns mirror the real WAC schema (subset).
_WAC_HEADER = "w_geocode,C000,CE01,CE02,CE03,CNS05,CNS07,CNS15"


def _wac_csv(rows: list[str]) -> str:
    return "\n".join([_WAC_HEADER, *rows]) + "\n"


# ---------------------------------------------------------------------------
# _resolve_segment.
# ---------------------------------------------------------------------------


def test_resolve_total_segment():
    spec = _resolve_segment("total")
    assert spec["cols"] == ["C000"]
    assert spec["friendly"] == "total"
    assert "label" in spec


def test_resolve_wage_segment():
    assert _resolve_segment("low_wage")["cols"] == ["CE01"]
    assert _resolve_segment("mid_wage")["cols"] == ["CE02"]
    assert _resolve_segment("high_wage")["cols"] == ["CE03"]


def test_resolve_multicolumn_sector_segment():
    spec = _resolve_segment("goods")
    assert spec["cols"] == ["CNS01", "CNS02", "CNS03", "CNS04", "CNS05"]
    assert spec["friendly"] == "goods"


def test_resolve_is_case_insensitive():
    assert _resolve_segment("HIGH_WAGE")["cols"] == ["CE03"]
    assert _resolve_segment(" Total ")["cols"] == ["C000"]


@pytest.mark.parametrize("bad", ["", "   ", "not_a_segment", "C000", "wage"])
def test_resolve_unknown_segment_raises(bad):
    with pytest.raises(LehdJobsInputError):
        _resolve_segment(bad)


def test_resolve_non_string_segment_raises():
    with pytest.raises(LehdJobsInputError):
        _resolve_segment(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _parse_wac_csv (block -> tract aggregation).
# ---------------------------------------------------------------------------


def test_parse_wac_aggregates_blocks_to_tract():
    # Two blocks in tract 48201100001, one block in tract 48201210400.
    csv_text = _wac_csv([
        "480011000010001,20,4,6,10,0,5,3",
        "480011000010002,30,6,9,15,2,7,4",   # same tract (48201... wait, see below)
        "482012104000001,93,13,43,37,18,9,20",
    ])
    # NOTE block geocodes use state 48 county 001 tract 100001 above; align fips.
    out = _parse_wac_csv(csv_text, "48", ["C000"])
    # First two blocks share tract 48001100001; third is 48201210400.
    assert out["48001100001"] == pytest.approx(50.0)   # 20 + 30
    assert out["48201210400"] == pytest.approx(93.0)


def test_parse_wac_sums_multiple_columns():
    csv_text = _wac_csv([
        "480011000010001,20,4,6,10,3,5,2",
    ])
    # "goods"-like multi-col sum on the subset header columns we have:
    out = _parse_wac_csv(csv_text, "48", ["CE01", "CE02", "CE03"])
    assert out["48001100001"] == pytest.approx(20.0)  # 4 + 6 + 10


def test_parse_wac_drops_out_of_state_and_short_geocodes():
    csv_text = _wac_csv([
        "480011000010001,20,4,6,10,0,5,3",
        "060371000010001,99,1,2,3,0,1,1",   # state 06 -- dropped when fips=48
        "12345,5,1,1,1,0,0,0",               # too short -- dropped
    ])
    out = _parse_wac_csv(csv_text, "48", ["C000"])
    assert set(out) == {"48001100001"}
    assert out["48001100001"] == pytest.approx(20.0)


def test_parse_wac_missing_column_raises_upstream():
    # Requesting a column that is not in the header is an honest upstream error,
    # never a silent zero.
    csv_text = _wac_csv(["480011000010001,20,4,6,10,0,5,3"])
    with pytest.raises(LehdJobsUpstreamError):
        _parse_wac_csv(csv_text, "48", ["CNS20"])  # not in our subset header


def test_parse_wac_missing_geocode_column_raises_upstream():
    bad = "C000,CE01\n20,4\n"
    with pytest.raises(LehdJobsUpstreamError):
        _parse_wac_csv(bad, "48", ["C000"])


def test_parse_wac_handles_blank_and_nonnumeric_cells():
    csv_text = _wac_csv([
        "480011000010001,,4,6,10,0,5,3",     # blank C000 -> 0 contribution
        "480011000010002,abc,1,1,1,0,0,0",   # non-numeric C000 -> skipped
        "480011000010003,7,1,1,1,0,0,0",
    ])
    out = _parse_wac_csv(csv_text, "48", ["C000"])
    assert out["48001100001"] == pytest.approx(7.0)  # only the valid row


# ---------------------------------------------------------------------------
# _aggregate_wac_to_tract: empty-states short-circuit (no network).
# ---------------------------------------------------------------------------


def test_aggregate_empty_states_returns_empty_no_network():
    assert _aggregate_wac_to_tract(set(), ["C000"], 2022) == {}


# ---------------------------------------------------------------------------
# _features_to_flatgeobuf (join + serialize).
# ---------------------------------------------------------------------------


def _read_fgb(fgb_bytes: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=True) as f:
        f.write(fgb_bytes)
        f.flush()
        return geopandas.read_file(f.name)


def test_join_and_serialize_total_segment():
    spec = _resolve_segment("total")
    tracts = [
        _tract_feature("48201100001", cx=-95.30, cy=29.70),
        _tract_feature("48201210400", cx=-95.32, cy=29.72),
        _tract_feature("48201999999", cx=-95.34, cy=29.74),  # no LODES -> null
    ]
    values = {"48201100001": 74083.0, "48201210400": 51964.0}
    fgb = _features_to_flatgeobuf(tracts, values, spec, 2022)
    gdf = _read_fgb(fgb)
    assert len(gdf) == 3
    assert set(gdf.columns) >= {
        "geoid", "name", "state", "county", "segment",
        "value", "units", "year", "geometry",
    }
    by_geoid = {r["geoid"]: r for _, r in gdf.iterrows()}
    assert by_geoid["48201100001"]["value"] == 74083.0
    assert by_geoid["48201210400"]["value"] == 51964.0
    # Unmatched GEOID -> null value (honest, not fabricated).
    v = by_geoid["48201999999"]["value"]
    assert v is None or v != v  # None or NaN
    assert (gdf["segment"] == "total").all()
    assert (gdf["units"] == "jobs").all()
    assert (gdf["year"] == 2022).all()
    assert (gdf["state"] == "48").all()


def test_empty_tracts_yields_valid_zero_feature_fgb():
    spec = _resolve_segment("total")
    fgb = _features_to_flatgeobuf([], {}, spec, 2022)
    assert isinstance(fgb, bytes) and len(fgb) > 0
    gdf = _read_fgb(fgb)
    assert len(gdf) == 0


def test_feature_with_no_geometry_is_dropped():
    spec = _resolve_segment("high_wage")
    tracts = [
        _tract_feature("48201100001"),
        {"type": "Feature", "geometry": None, "properties": {"GEOID": "48201100002"}},
    ]
    values = {"48201100001": 1234.0}
    gdf = _read_fgb(_features_to_flatgeobuf(tracts, values, spec, 2021))
    assert len(gdf) == 1
    assert gdf.iloc[0]["value"] == 1234.0
    assert gdf.iloc[0]["segment"] == "high_wage"


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
    with pytest.raises(LehdJobsInputError):
        _validate_bbox(bbox)


def test_round_bbox_to_6dp():
    assert _round_bbox_to_6dp((-95.123456789, 29.1, -95.0, 29.999999999)) == (
        -95.123457, 29.1, -95.0, 30.0
    )


def test_fetch_rejects_unknown_segment_before_network():
    with pytest.raises(LehdJobsInputError):
        fetch_lehd_jobs(bbox=(-95.45, 29.65, -95.25, 29.85), segment="banana")


def test_fetch_rejects_bad_year_before_network():
    with pytest.raises(LehdJobsInputError):
        fetch_lehd_jobs(bbox=(-95.45, 29.65, -95.25, 29.85), year=1990)


def test_fetch_rejects_bad_bbox_before_network():
    with pytest.raises(LehdJobsInputError):
        fetch_lehd_jobs(bbox=(-95.25, 29.65, -95.45, 29.85))  # inverted lon


# ---------------------------------------------------------------------------
# Registries + estimator + registration.
# ---------------------------------------------------------------------------


def test_payload_estimator_scales_with_area():
    small = estimate_payload_mb((-95.45, 29.65, -95.25, 29.85))   # ~0.04 sq deg
    big = estimate_payload_mb((-96.0, 29.0, -95.0, 30.0))         # 1 sq deg
    assert 0.02 <= small <= big <= 80.0
    assert estimate_payload_mb(None) > 0.0


def test_segment_registry_well_formed():
    for name, spec in LODES_SEGMENTS.items():
        assert isinstance(spec["cols"], list) and spec["cols"]
        assert all(
            c.startswith(("C000", "CE", "CNS")) for c in spec["cols"]
        ), f"segment {name} has an unexpected WAC column"
        assert isinstance(spec["label"], str) and spec["label"]


def test_fips_to_abbr_registry_well_formed():
    # 50 states + DC + PR = 52 entries; all 2-char lowercase.
    assert len(FIPS_TO_ABBR) == 52
    assert FIPS_TO_ABBR["48"] == "tx"
    assert FIPS_TO_ABBR["06"] == "ca"
    assert FIPS_TO_ABBR["11"] == "dc"
    assert FIPS_TO_ABBR["72"] == "pr"
    for fips, abbr in FIPS_TO_ABBR.items():
        assert len(fips) == 2 and fips.isdigit()
        assert len(abbr) == 2 and abbr.islower()


def test_tool_is_registered():
    assert "fetch_lehd_jobs" in TOOL_REGISTRY
