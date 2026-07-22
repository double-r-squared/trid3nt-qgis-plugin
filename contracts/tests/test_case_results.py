"""Tests for case_results.CaseOneResult (job-0118).

Covers:
- pydantic round-trip
- default factories (empty lists/dicts)
- bbox validation (invariant 4326 ordering)
- schema_version pin
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grace2_contracts.case_results import CaseOneResult
from grace2_contracts.execution import LayerURI


def _mk_layer(label: str, layer_type: str = "raster") -> LayerURI:
    return LayerURI(
        layer_id=f"{label}-test",
        name=f"{label} layer",
        layer_type=layer_type,  # type: ignore[arg-type]
        uri=f"gs://test/{label}.tif" if layer_type == "raster" else f"gs://test/{label}.fgb",
        style_preset=f"{label}_style",
        role="primary",
    )


def test_case_one_result_round_trip_full() -> None:
    """All fields populated → model_dump/model_validate round-trip succeeds."""
    flood = _mk_layer("flood", "raster")
    wdpa = _mk_layer("wdpa", "vector")
    species_a = _mk_layer("panther", "vector")
    species_b = _mk_layer("spoonbill", "vector")
    result = CaseOneResult(
        bbox=(-82.0, 25.0, -80.0, 26.5),
        flood_layer_uri=flood,
        species_layers=[species_a, species_b],
        wdpa_layer_uri=wdpa,
        impact_metrics={"aggregate": {"max": 1.2, "mean": 0.4, "count": 12340}},
        case_summary_text="case 1 summary",
        species_counts={"2435099": 240, "2481008": 89},
    )
    dumped = result.model_dump(mode="json")
    rehydrated = CaseOneResult.model_validate(dumped)
    assert rehydrated == result
    assert rehydrated.flood_layer_uri is not None
    assert rehydrated.flood_layer_uri.uri == flood.uri
    assert len(rehydrated.species_layers) == 2
    assert rehydrated.species_counts == {"2435099": 240, "2481008": 89}


def test_case_one_result_minimum_fields() -> None:
    """Only ``bbox`` + ``case_summary_text`` are mandatory; defaults populate."""
    result = CaseOneResult(
        bbox=(-82.0, 25.0, -80.0, 26.5),
        case_summary_text="bare summary",
    )
    assert result.flood_layer_uri is None
    assert result.wdpa_layer_uri is None
    assert result.species_layers == []
    assert result.impact_metrics == {}
    assert result.species_counts == {}
    assert result.schema_version == "v1"


def test_case_one_result_bbox_validates_4326_ordering() -> None:
    """A degenerate bbox (min > max on either axis) is rejected."""
    with pytest.raises(ValidationError):
        CaseOneResult(
            bbox=(0.0, 0.0, -1.0, 1.0),  # min_lon > max_lon
            case_summary_text="bad",
        )


def test_case_one_result_forbids_extra_fields() -> None:
    """Per GraceModel ``extra='forbid'`` — unknown keys are a defect."""
    with pytest.raises(ValidationError):
        CaseOneResult(
            bbox=(-82.0, 25.0, -80.0, 26.5),
            case_summary_text="x",
            unknown_field="not allowed",  # type: ignore[call-arg]
        )
