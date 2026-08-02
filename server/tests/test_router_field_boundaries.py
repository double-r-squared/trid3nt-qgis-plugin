"""FTW/fiboa GeoParquet-pushdown fold parity (ADR 0083): fetch_field_boundaries.

Migrates the OFFLINE-testable coverage of the deleted twin onto the VECTOR
library_delegate mode. The live GeoParquet row-group pushdown read (geopandas over an
fsspec HTTPS handle -- the library owns the range reads) is proven by the ADR 0083 live
twin-vs-router parity harness (select + feature-count + geometry-area + crop_name +
error-edge value-identical). Here the offline surfaces are: spec identity, the pure
pre_resolve dataset-selection (auto + explicit + no-coverage + unknown-key), and the
payload estimate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from trid3nt_server.agent.tools.fetchers._router import router
from trid3nt_server.agent.tools.fetchers._router.errors import RouterInputError
from trid3nt_server.agent.tools.fetchers._router.hooks import field_boundaries as FB
from trid3nt_server.agent.tools.fetchers._router.spec import load_spec_from_path

SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "src/trid3nt_server/agent/tools/fetchers/socioeconomic/fetch_field_boundaries/source.yaml"
)

_AMES = (-93.70, 42.00, -93.60, 42.08)  # US cropland (USDA CSB)
_OCEAN = (-30.0, 0.0, -29.0, 1.0)        # mid-Atlantic, no coverage


def _vp(**raw: Any) -> dict[str, Any]:
    return router.validate_params(SPEC, raw)


def test_spec_identity():
    assert SPEC.name == "fetch_field_boundaries" and SPEC.source_class == "ftw_field_boundaries"
    assert SPEC.error_code_prefix == "FIELDS"
    assert SPEC.shape == "vector-fgb" and SPEC.output.layer_type == "vector"
    assert SPEC.output.role == "context" and SPEC.output.style_preset == "field_boundaries"
    assert SPEC.hooks.delegate == "field_boundaries.read"
    assert SPEC.hooks.pre_resolve == "field_boundaries.select"
    keys = {d["key"] for d in SPEC.ingest["field_boundaries"]["datasets"]}
    assert keys == {"us_usda_cropland", "japan", "denmark"}
    assert SPEC.docstring and "Fields of The World" in SPEC.docstring


def test_select_auto_picks_covering_dataset():
    assert FB.select_dataset(SPEC, _vp(bbox=list(_AMES))) == {"dataset": "us_usda_cropland"}


def test_select_explicit_key_honored():
    assert FB.select_dataset(SPEC, _vp(bbox=list(_AMES), dataset="us_usda_cropland")) == {
        "dataset": "us_usda_cropland"}


def test_select_no_coverage_raises():
    with pytest.raises(RouterInputError) as ei:
        FB.select_dataset(SPEC, _vp(bbox=list(_OCEAN)))
    assert ei.value.error_code == "FIELDS_NO_COVERAGE" and ei.value.retryable is False


def test_select_explicit_key_non_intersecting_is_no_coverage():
    # japan key + a US bbox -> the explicit dataset does not intersect
    with pytest.raises(RouterInputError) as ei:
        FB.select_dataset(SPEC, _vp(bbox=list(_AMES), dataset="japan"))
    assert ei.value.error_code == "FIELDS_NO_COVERAGE"


def test_select_unknown_key_is_input_invalid():
    with pytest.raises(RouterInputError) as ei:
        FB.select_dataset(SPEC, _vp(bbox=list(_AMES), dataset="atlantis"))
    assert ei.value.error_code == "FIELDS_INPUT_INVALID"


def test_select_merges_into_cache_key():
    # pre_resolve return merges into params so the resolved key enters the cache key
    merged = {**_vp(bbox=list(_AMES)), **FB.select_dataset(SPEC, _vp(bbox=list(_AMES)))}
    assert merged["dataset"] == "us_usda_cropland"


def test_payload_estimate_never_warns():
    est = router.synthesize_payload_estimator(SPEC)
    # per_feature tiny coefficient: a county-scale bbox stays well under the 25 MB warn
    assert est(bbox=list(_AMES)) < 25.0
