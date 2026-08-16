"""LEHD LODES join VALUES-hook fold parity (trigger wave, ADR 0084): fetch_lehd_jobs.

Migrates the OFFLINE-testable coverage of the deleted twin onto the ``transforms/join``
shape + the ``join.values.values_hook`` seam (per-state LODES bulk gzip-CSV values leg).
The LIVE twin-vs-router value parity (TIGERweb tract geometry + LODES WAC join on GEOID:
feature count + per-tract job value + geometry area value-identical, total + a wage
segment, ocean-empty schema-identical) is proven by the ADR 0084 live drive. Here the
offline surfaces are: spec identity, the pure segment resolution, the pure values_parse
gzip-CSV block->tract aggregation, the values_plan URL build, param validation edges, the
join serialize schema, and the payload estimate.
"""

from __future__ import annotations

import gzip
import os
import tempfile
from pathlib import Path
from typing import Any

import geopandas as gpd
import pytest

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.data.fetchers._router.executors import vector_fgb
from trid3nt_server.data.fetchers._router.hooks import lehd_jobs as LH
from trid3nt_server.data.fetchers._router.spec import load_spec_from_path
from trid3nt_server.data.fetchers._router.transforms import join

SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/data/fetchers/socioeconomic/fetch_lehd_jobs/source.yaml"
)

_HOUSTON = (-95.45, 29.65, -95.25, 29.85)


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(SPEC, raw)


def _gzip_csv(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def _to_gdf(b: bytes) -> gpd.GeoDataFrame:
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(b)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


# --------------------------------------------------------------------------- #
# Spec identity.
# --------------------------------------------------------------------------- #


def test_spec_identity():
    assert SPEC.name == "fetch_lehd_jobs" and SPEC.source_class == "lehd_lodes"
    assert SPEC.error_code_prefix == "LEHD_JOBS" and SPEC.input_error_suffix == "INPUT_INVALID"
    assert SPEC.shape == "vector-fgb" and SPEC.output.layer_type == "vector"
    assert SPEC.output.role == "primary" and SPEC.output.style_preset == "lehd_jobs_choropleth"
    j = SPEC.join
    assert j["variable_param"] == "segment" and j["value_field"] == "segment"
    assert j["allow_raw_code"] is False
    assert j["values"]["scope_by"] == ["STATE"]
    assert j["values"]["values_hook"] == {"plan": "lehd_jobs.values_plan", "parse": "lehd_jobs.values_parse"}
    assert set(j["values"]["variables"]) == {
        "total", "low_wage", "mid_wage", "high_wage", "goods", "trade_transport",
        "services", "public", "retail", "manufacturing", "health",
    }
    assert SPEC.docstring and "LODES" in SPEC.docstring
    assert SPEC.corpus  # lifted from sibling corpus.yaml


def test_executor_is_join_transform():
    assert router.select_executor(SPEC).__module__.endswith("transforms.join")


def test_promoted_signature_matches_twin():
    from trid3nt_server.data.fetchers._router import registration
    sig, _ = registration.promoted_signature(SPEC)
    assert list(sig.parameters) == ["bbox", "segment", "year", "_extra_ignored"]
    assert sig.parameters["segment"].default == "total"
    assert sig.parameters["year"].default == 2022


# --------------------------------------------------------------------------- #
# Segment resolution (select_variable over join.values.variables).
# --------------------------------------------------------------------------- #


def test_resolve_total_segment():
    name, vs = join.select_variable(SPEC, _vp(bbox=list(_HOUSTON)))
    assert name == "total" and vs["cols"] == ["C000"] and vs["units"] == "jobs"


def test_resolve_wage_and_multicolumn_segments():
    assert join.select_variable(SPEC, _vp(bbox=list(_HOUSTON), segment="low_wage"))[1]["cols"] == ["CE01"]
    assert join.select_variable(SPEC, _vp(bbox=list(_HOUSTON), segment="high_wage"))[1]["cols"] == ["CE03"]
    _, vs = join.select_variable(SPEC, _vp(bbox=list(_HOUSTON), segment="goods"))
    assert vs["cols"] == ["CNS01", "CNS02", "CNS03", "CNS04", "CNS05"]


def test_resolve_is_case_insensitive():
    assert join.select_variable(SPEC, _vp(bbox=list(_HOUSTON), segment="HIGH_WAGE"))[0] == "high_wage"


def test_resolve_unknown_segment_raises_input_invalid():
    with pytest.raises(RouterInputError) as ei:
        join.select_variable(SPEC, {"segment": "not_a_segment"})
    assert ei.value.error_code == "LEHD_JOBS_INPUT_INVALID" and ei.value.retryable is False


def test_unknown_segment_is_not_raw_code_passthrough():
    # allow_raw_code=False: a value that LOOKS like an ACS code is still rejected
    # (closed vocabulary), unlike census.
    with pytest.raises(RouterInputError):
        join.select_variable(SPEC, {"segment": "C000_001E"})


# --------------------------------------------------------------------------- #
# values_parse -- gzip-CSV block -> tract aggregation (the pure hook).
# --------------------------------------------------------------------------- #

_VS_TOTAL = {"cols": ["C000"], "code": "value", "kind": "value", "units": "jobs"}


def test_values_parse_aggregates_blocks_to_tract():
    csv = (
        "w_geocode,C000\n"
        "480011000010001,20\n"
        "480011000010002,30\n"
        "482012104000001,93\n"
    )
    out = LH.values_parse(SPEC, {"48": _gzip_csv(csv)}, _VS_TOTAL, {})
    assert out["48001100001"]["value"] == pytest.approx(50.0)
    assert out["48201210400"]["value"] == pytest.approx(93.0)


def test_values_parse_sums_multiple_columns():
    vs = {"cols": ["CNS01", "CNS02", "CNS03"], "code": "value", "kind": "value", "units": "jobs"}
    csv = "w_geocode,CNS01,CNS02,CNS03\n480011000010001,4,6,10\n"
    out = LH.values_parse(SPEC, {"48": _gzip_csv(csv)}, vs, {})
    assert out["48001100001"]["value"] == pytest.approx(20.0)


def test_values_parse_drops_out_of_state_and_short_geocodes():
    csv = (
        "w_geocode,C000\n"
        "480011000010001,20\n"
        "060750100000001,999\n"   # CA, wrong state fips
        "4800110,5\n"             # too short
    )
    out = LH.values_parse(SPEC, {"48": _gzip_csv(csv)}, _VS_TOTAL, {})
    assert set(out) == {"48001100001"} and out["48001100001"]["value"] == pytest.approx(20.0)


def test_values_parse_handles_blank_and_nonnumeric_cells():
    csv = "w_geocode,C000\n480011000010001,\n480011000010002,7\n480011000010003,abc\n"
    out = LH.values_parse(SPEC, {"48": _gzip_csv(csv)}, _VS_TOTAL, {})
    assert out["48001100001"]["value"] == pytest.approx(7.0)


def test_values_parse_missing_value_column_raises_upstream():
    csv = "w_geocode,CE01\n480011000010001,5\n"
    with pytest.raises(RouterUpstreamError):
        LH.values_parse(SPEC, {"48": _gzip_csv(csv)}, _VS_TOTAL, {})


def test_values_parse_missing_geocode_column_raises_upstream():
    csv = "id,C000\n480011000010001,5\n"
    with pytest.raises(RouterUpstreamError):
        LH.values_parse(SPEC, {"48": _gzip_csv(csv)}, _VS_TOTAL, {})


# --------------------------------------------------------------------------- #
# values_plan -- per-state LODES WAC URL build.
# --------------------------------------------------------------------------- #


def test_values_plan_builds_lodes_url_per_state():
    plans = LH.values_plan(SPEC, [("48",), ("22",)], _VS_TOTAL, {"year": 2022})
    urls = {fips: plan.url for fips, plan in plans}
    assert urls["48"].endswith("LODES8/tx/wac/tx_wac_S000_JT00_2022.csv.gz")
    assert urls["22"].endswith("LODES8/la/wac/la_wac_S000_JT00_2022.csv.gz")


def test_values_plan_skips_no_coverage_fips():
    # A FIPS with no LODES universe (e.g. American Samoa 60) emits no plan.
    plans = LH.values_plan(SPEC, [("60",), ("48",)], _VS_TOTAL, {"year": 2022})
    assert [f for f, _ in plans] == ["48"]


# --------------------------------------------------------------------------- #
# Param validation edges (pre-network).
# --------------------------------------------------------------------------- #


def test_bbox_required():
    with pytest.raises(RouterInputError):
        _vp(segment="total")


@pytest.mark.parametrize("bad", [[0, 0, 0, 0], [-181, 0, 1, 1], [1, 1, 0, 0]])
def test_bbox_invalid_rejected(bad):
    with pytest.raises(RouterInputError):
        _vp(bbox=bad)


def test_year_out_of_range_rejected():
    with pytest.raises(RouterInputError) as ei:
        _vp(bbox=list(_HOUSTON), year=1990)
    assert ei.value.error_code == "LEHD_JOBS_INPUT_INVALID"


def test_bbox_quantized_to_6dp():
    vp = _vp(bbox=[-95.123456789, 29.1, -95.0, 29.999999999])
    assert vp["bbox"] == [-95.123457, 29.1, -95.0, 30.0]


# --------------------------------------------------------------------------- #
# join_on_key serialize schema (the twin's join+serialize test).
# --------------------------------------------------------------------------- #

_GEOM = [
    {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
        "properties": {"GEOID": "48201100001", "NAME": "T1", "STATE": "48", "COUNTY": "201"},
    },
    {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]]},
        "properties": {"GEOID": "48201210400", "NAME": "T2", "STATE": "48", "COUNTY": "201"},
    },
    {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[[2, 2], [3, 2], [3, 3], [2, 3], [2, 2]]]},
        "properties": {"GEOID": "48201999999", "NAME": "T3", "STATE": "48", "COUNTY": "201"},
    },
]


def test_join_and_serialize_total_segment():
    values = {"48201100001": {"value": 74083.0}, "48201210400": {"value": 51964.0}}
    _, vs = join.select_variable(SPEC, _vp(bbox=list(_HOUSTON), segment="total"))
    joined = join.join_on_key(_GEOM, values, SPEC.join, "total", vs, params={"year": 2022})
    gdf = _to_gdf(vector_fgb.features_to_fgb_bytes(joined, SPEC, {"year": 2022}))
    assert len(gdf) == 3
    assert {"geoid", "segment", "value", "units", "name", "state", "county", "year"} <= set(gdf.columns)
    by = {r["geoid"]: r for _, r in gdf.iterrows()}
    assert by["48201100001"]["value"] == 74083.0
    assert by["48201210400"]["value"] == 51964.0
    v = by["48201999999"]["value"]
    assert v is None or v != v  # unmatched tract -> None/NaN (never fabricated)
    assert (gdf["segment"] == "total").all()
    assert (gdf["units"] == "jobs").all()
    assert (gdf["year"] == 2022).all()
    assert (gdf["state"] == "48").all()


def test_empty_join_yields_valid_zero_feature_fgb_with_full_schema():
    fgb = vector_fgb.features_to_fgb_bytes([], SPEC, {})
    gdf = _to_gdf(fgb)
    assert len(gdf) == 0
    assert {"geoid", "name", "state", "county", "segment", "value", "units", "year"} <= set(gdf.columns)


# --------------------------------------------------------------------------- #
# Payload estimate.
# --------------------------------------------------------------------------- #


def test_payload_estimator_scales_with_area_and_clamps():
    est = router.synthesize_payload_estimator(SPEC)
    small = est([-95.30, 29.75, -95.29, 29.76])
    big = est([-96.0, 29.0, -95.0, 30.0])
    assert 0.02 <= small <= big <= 80.0
    assert est(None) > 0.0
