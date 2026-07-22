"""Unit tests for the model_flood_habitat_scenario Case 1 composer (job-0118).

Coverage (≥6 unit per kickoff):

1. test_registry_registers_wrapper — the workflow_dispatch atomic-tool wrapper
   lands in TOOL_REGISTRY with the right metadata.
2. test_workflow_happy_path_orchestration_order — mocks every underlying tool,
   verifies WDPA → species → flood → zonal stats call order.
3. test_workflow_empty_species_keys_still_produces_flood_and_wdpa — passes
   ``species_keys=None`` and verifies the composer still returns flood +
   wdpa layers + impact summary.
4. test_workflow_protected_area_designation_forwarded — verifies
   ``protected_area_designation`` is forwarded to ``fetch_wdpa_protected_areas``.
5. test_workflow_place_clip_polygon_fires_clipping_calls — verifies
   ``clip_raster_to_polygon`` + ``clip_vector_to_polygon`` fire when
   ``place_clip_polygon_uri`` is supplied.
6. test_workflow_pipeline_emitter_receives_expected_stage_events — verifies
   the emitter sees one ``emit_tool_call`` per major step.
7. test_case_one_result_round_trip — the CaseOneResult survives
   ``model_dump(mode='json')`` and ``model_validate`` round-trip.
8. test_flood_failure_marks_flood_layer_uri_none — when
   ``model_flood_scenario`` returns a partial-failure envelope, the composer
   returns ``CaseOneResult.flood_layer_uri = None`` and the summary names the
   error code.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Force the workflow module to register its atomic-tool wrapper before we
# inspect TOOL_REGISTRY.
import trid3nt_server.workflows.model_flood_habitat_scenario  # noqa: F401
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.model_flood_habitat_scenario import (
    model_flood_habitat_scenario,
    run_model_flood_habitat_scenario,
    _format_case_summary,
    _parse_return_period,
)
from trid3nt_contracts import new_ulid
from trid3nt_contracts.case_results import CaseOneResult
from trid3nt_contracts.envelope import (
    AssessmentEnvelope,
    DataSource,
    FloodMetrics,
    FloodPayload,
    Provenance,
    ResultLayer,
)
from trid3nt_contracts.execution import LayerURI


# --------------------------------------------------------------------------- #
# Fixtures + helpers
# --------------------------------------------------------------------------- #


_TEST_BBOX = (-81.5, 25.7, -80.7, 26.5)  # Big Cypress / Everglades-ish


def _mk_layer(label: str, layer_type: str = "vector", suffix: str = ".fgb") -> LayerURI:
    return LayerURI(
        layer_id=f"{label}-test",
        name=f"{label} layer",
        layer_type=layer_type,  # type: ignore[arg-type]
        uri=f"gs://test-cache/{label}{suffix}",
        style_preset=f"{label}_style",
        role="primary" if layer_type == "raster" else "context",
    )


def _mk_success_flood_envelope(bbox: tuple[float, float, float, float]) -> AssessmentEnvelope:
    """Build a non-failed flood envelope mock (one raster layer)."""
    run_id = new_ulid()
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name="model_flood_scenario",
        bbox=bbox,
        crs="EPSG:4326",
        forcing=None,
        layers=[
            ResultLayer(
                layer_id=f"flood-depth-{run_id}",
                name="Flood Depth (peak)",
                layer_type="raster",
                uri=f"gs://test-runs/{run_id}/flood_depth.tif",
                style_preset="continuous_flood_depth",
                role="primary",
                units="meters",
            )
        ],
        provenance=Provenance(data_sources=[]),
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        solver_run_ids=[run_id],
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=1.23,
                max_depth_m=2.4,
                mean_depth_m=0.6,
                p95_depth_m=1.9,
                solver_version="sfincs-v2.3.3",
                grid_resolution_m=30.0,
                simulation_duration_hours=24,
            )
        ),
    )


def _mk_failed_flood_envelope(
    bbox: tuple[float, float, float, float],
    error_code: str = "LULC_MAPPING_MISMATCH",
) -> AssessmentEnvelope:
    """Build a partial-failure flood envelope mock (empty layers, failed: prefix)."""
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name="model_flood_scenario",
        bbox=bbox,
        crs="EPSG:4326",
        forcing=None,
        layers=[],
        provenance=Provenance(data_sources=[]),
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        solver_run_ids=[],
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=0.0,
                max_depth_m=0.0,
                mean_depth_m=0.0,
                p95_depth_m=0.0,
                solver_version=f"failed:{error_code}",
                grid_resolution_m=30.0,
                simulation_duration_hours=24,
            )
        ),
    )


class _RecordingEmitter:
    """Minimal stand-in for PipelineEmitter that records emit_tool_call calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.results: list[Any] = []

    async def emit_tool_call(
        self,
        *,
        name: str,
        tool_name: str,
        invoke: Any,
    ) -> Any:
        self.calls.append((name, tool_name))
        result = invoke()
        if asyncio.iscoroutine(result):
            result = await result
        self.results.append(result)
        return result


# --------------------------------------------------------------------------- #
# Test 1 — registry registration
# --------------------------------------------------------------------------- #


def test_registry_registers_wrapper() -> None:
    """``run_model_flood_habitat_scenario`` is registered with workflow_dispatch metadata."""
    assert "run_model_flood_habitat_scenario" in TOOL_REGISTRY, (
        f"workflow wrapper not in TOOL_REGISTRY; keys={sorted(TOOL_REGISTRY)}"
    )
    entry = TOOL_REGISTRY["run_model_flood_habitat_scenario"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.fn is run_model_flood_habitat_scenario


# --------------------------------------------------------------------------- #
# Test 2 — orchestration order on the happy path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_happy_path_orchestration_order() -> None:
    """Mocked happy path: WDPA → species → flood → zonal stats in that order."""
    flood_env = _mk_success_flood_envelope(_TEST_BBOX)
    wdpa_layer = _mk_layer("wdpa")
    species_layer = _mk_layer("panther")
    impact_dict = {
        "aggregate": {"max": 1.2, "mean": 0.4, "count": 1234},
        "by_zone": {},
    }

    call_order: list[str] = []

    def _record(name: str, ret: Any) -> Any:
        def _impl(*args: Any, **kwargs: Any) -> Any:
            call_order.append(name)
            return ret
        return _impl

    async def _flood_async(*args: Any, **kwargs: Any) -> AssessmentEnvelope:
        call_order.append("flood")
        return flood_env

    with (
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_wdpa_protected_areas",
            side_effect=_record("wdpa", wdpa_layer),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_gbif_occurrences",
            side_effect=_record("species", species_layer),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.model_flood_scenario",
            side_effect=_flood_async,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.compute_zonal_statistics",
            side_effect=_record("zonal", impact_dict),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario._count_features_safely",
            return_value=42,
        ),
    ):
        result = await model_flood_habitat_scenario(
            bbox=_TEST_BBOX,
            species_keys=[2435099],
            rainfall_event="atlas14_100yr",
        )
    assert call_order == ["wdpa", "species", "flood", "zonal"], (
        f"unexpected order: {call_order}"
    )
    assert isinstance(result, CaseOneResult)
    assert result.flood_layer_uri is not None
    assert result.wdpa_layer_uri is not None
    assert len(result.species_layers) == 1
    assert result.impact_metrics == impact_dict
    assert result.species_counts == {"2435099": 42}
    assert "max flood depth 1.20 m" in result.case_summary_text


# --------------------------------------------------------------------------- #
# Test 3 — empty species_keys still produces flood + wdpa
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_empty_species_keys_still_produces_flood_and_wdpa() -> None:
    """species_keys=None → no species fetches, but flood + WDPA + impact still run."""
    flood_env = _mk_success_flood_envelope(_TEST_BBOX)
    wdpa_layer = _mk_layer("wdpa")
    impact_dict = {"aggregate": {"max": 0.5, "mean": 0.2, "count": 100}}

    gbif_calls: list[Any] = []

    def _record_gbif(*args: Any, **kwargs: Any) -> Any:
        gbif_calls.append((args, kwargs))
        return _mk_layer("never")

    async def _flood_async(*args: Any, **kwargs: Any) -> AssessmentEnvelope:
        return flood_env

    with (
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_wdpa_protected_areas",
            return_value=wdpa_layer,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_gbif_occurrences",
            side_effect=_record_gbif,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.model_flood_scenario",
            side_effect=_flood_async,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.compute_zonal_statistics",
            return_value=impact_dict,
        ),
    ):
        result = await model_flood_habitat_scenario(
            bbox=_TEST_BBOX,
            species_keys=None,
        )
    assert gbif_calls == []
    assert result.species_layers == []
    assert result.species_counts == {}
    assert result.flood_layer_uri is not None
    assert result.wdpa_layer_uri is not None
    assert result.impact_metrics == impact_dict


# --------------------------------------------------------------------------- #
# Test 4 — protected_area_designation forwarded
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_protected_area_designation_forwarded() -> None:
    """protected_area_designation propagates to fetch_wdpa_protected_areas."""
    wdpa_mock = MagicMock(return_value=_mk_layer("wdpa"))

    async def _flood_async(*args: Any, **kwargs: Any) -> AssessmentEnvelope:
        return _mk_success_flood_envelope(_TEST_BBOX)

    with (
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_wdpa_protected_areas",
            wdpa_mock,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.model_flood_scenario",
            side_effect=_flood_async,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.compute_zonal_statistics",
            return_value={"aggregate": {}, "by_zone": {}},
        ),
    ):
        await model_flood_habitat_scenario(
            bbox=_TEST_BBOX,
            protected_area_designation=["National Park", "National Preserve"],
        )
    wdpa_mock.assert_called_once()
    _, kwargs = wdpa_mock.call_args
    assert kwargs.get("designation_filter") == ["National Park", "National Preserve"]
    assert kwargs.get("bbox") == _TEST_BBOX


# --------------------------------------------------------------------------- #
# Test 5 — place_clip_polygon fires clipping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_place_clip_polygon_fires_clipping_calls() -> None:
    """place_clip_polygon_uri → clip_raster_to_polygon + clip_vector_to_polygon fire."""
    flood_env = _mk_success_flood_envelope(_TEST_BBOX)
    wdpa_layer = _mk_layer("wdpa")
    species_layer = _mk_layer("panther")

    clipped_raster = _mk_layer("clipped_flood", "raster", ".tif")
    clipped_vector = _mk_layer("clipped_vec")

    clip_raster_mock = MagicMock(return_value=clipped_raster)
    clip_vector_mock = MagicMock(return_value=clipped_vector)

    async def _flood_async(*args: Any, **kwargs: Any) -> AssessmentEnvelope:
        return flood_env

    with (
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_wdpa_protected_areas",
            return_value=wdpa_layer,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_gbif_occurrences",
            return_value=species_layer,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.model_flood_scenario",
            side_effect=_flood_async,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.compute_zonal_statistics",
            return_value={"aggregate": {}, "by_zone": {}},
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.clip_raster_to_polygon",
            clip_raster_mock,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.clip_vector_to_polygon",
            clip_vector_mock,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario._count_features_safely",
            return_value=0,
        ),
    ):
        result = await model_flood_habitat_scenario(
            bbox=_TEST_BBOX,
            species_keys=[2435099],
            place_clip_polygon_uri="gs://test-cache/big_cypress.fgb",
            place_label="Big Cypress National Preserve",
        )
    # clip_raster called once (flood); clip_vector called twice (wdpa + species).
    assert clip_raster_mock.call_count == 1
    assert clip_vector_mock.call_count == 2
    assert result.flood_layer_uri == clipped_raster
    assert result.wdpa_layer_uri == clipped_vector
    assert result.species_layers == [clipped_vector]
    assert "Within Big Cypress National Preserve" in result.case_summary_text


# --------------------------------------------------------------------------- #
# Test 6 — pipeline_emitter stage events
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_pipeline_emitter_receives_expected_stage_events() -> None:
    """A pipeline_emitter sees one emit_tool_call per major step."""
    emitter = _RecordingEmitter()
    flood_env = _mk_success_flood_envelope(_TEST_BBOX)

    async def _flood_async(*args: Any, **kwargs: Any) -> AssessmentEnvelope:
        return flood_env

    with (
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_wdpa_protected_areas",
            return_value=_mk_layer("wdpa"),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_gbif_occurrences",
            return_value=_mk_layer("panther"),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.model_flood_scenario",
            side_effect=_flood_async,
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.compute_zonal_statistics",
            return_value={"aggregate": {"max": 1.0, "mean": 0.5, "count": 100}, "by_zone": {}},
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario._count_features_safely",
            return_value=10,
        ),
    ):
        result = await model_flood_habitat_scenario(
            bbox=_TEST_BBOX,
            species_keys=[2435099, "Roseate spoonbill"],
            pipeline_emitter=emitter,
        )
    # 1 WDPA + 2 species + 1 flood + 1 zonal = 5
    assert len(emitter.calls) == 5
    tool_names = [call[1] for call in emitter.calls]
    assert tool_names == [
        "fetch_wdpa_protected_areas",
        "fetch_gbif_occurrences",
        "fetch_gbif_occurrences",
        "model_flood_scenario",
        "compute_zonal_statistics",
    ]
    assert result.species_counts == {"2435099": 10, "Roseate spoonbill": 10}


# --------------------------------------------------------------------------- #
# Test 7 — CaseOneResult pydantic round-trip
# --------------------------------------------------------------------------- #


def test_case_one_result_round_trip() -> None:
    """CaseOneResult survives model_dump(mode='json') + model_validate."""
    layer = _mk_layer("rt")
    obj = CaseOneResult(
        bbox=_TEST_BBOX,
        flood_layer_uri=layer,
        species_layers=[layer],
        wdpa_layer_uri=layer,
        impact_metrics={"aggregate": {"max": 3.14}},
        case_summary_text="round-trip",
        species_counts={"99": 7},
    )
    dumped = obj.model_dump(mode="json")
    rehydrated = CaseOneResult.model_validate(dumped)
    assert rehydrated == obj


# --------------------------------------------------------------------------- #
# Test 8 — flood failure → flood_layer_uri None + error code in summary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_flood_failure_marks_flood_layer_uri_none() -> None:
    """A failed flood envelope → CaseOneResult.flood_layer_uri is None."""
    failed_env = _mk_failed_flood_envelope(_TEST_BBOX, error_code="LULC_MAPPING_MISMATCH")

    async def _flood_async(*args: Any, **kwargs: Any) -> AssessmentEnvelope:
        return failed_env

    with (
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.fetch_wdpa_protected_areas",
            return_value=_mk_layer("wdpa"),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_habitat_scenario.model_flood_scenario",
            side_effect=_flood_async,
        ),
    ):
        result = await model_flood_habitat_scenario(
            bbox=_TEST_BBOX,
            species_keys=None,
        )
    assert result.flood_layer_uri is None
    assert "LULC_MAPPING_MISMATCH" in result.case_summary_text
    assert result.wdpa_layer_uri is not None


# --------------------------------------------------------------------------- #
# Helper tests
# --------------------------------------------------------------------------- #


def test_parse_return_period_atlas14_known_forms() -> None:
    assert _parse_return_period("atlas14_100yr") == 100
    assert _parse_return_period("atlas14_500yr") == 500
    assert _parse_return_period("ATLAS14_25YR") == 25


def test_parse_return_period_falls_back_to_100() -> None:
    assert _parse_return_period("unknown") == 100
    assert _parse_return_period("atlas14_xyzyr") == 100
    assert _parse_return_period(123) == 100  # type: ignore[arg-type]


def test_format_case_summary_flood_success_no_species() -> None:
    text = _format_case_summary(
        bbox=_TEST_BBOX,
        species_counts={},
        impact_metrics={"aggregate": {"max": 0.5, "mean": 0.2, "count": 100}},
        wdpa_polygon_count=None,
        flood_failed=False,
        flood_error_code=None,
        rainfall_event="atlas14_100yr",
        place_label=None,
    )
    assert "no species" in text
    assert "0.50 m" in text
    assert "atlas14_100yr" in text


def test_format_case_summary_flood_failed_threads_error_code() -> None:
    text = _format_case_summary(
        bbox=_TEST_BBOX,
        species_counts={"panther": 5},
        impact_metrics={},
        wdpa_polygon_count=None,
        flood_failed=True,
        flood_error_code="SOLVER_FAILED",
        rainfall_event="atlas14_500yr",
        place_label="Big Cypress",
    )
    assert "Big Cypress" in text
    assert "5 panther" in text
    assert "SOLVER_FAILED" in text


# --------------------------------------------------------------------------- #
# LIVE test — env-guarded; produces real flood + GBIF layers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.skipif(
    "1" != __import__("os").environ.get("TRID3NT_TEST_LIVE_CASE1", ""),
    reason="Live Case 1 test requires TRID3NT_TEST_LIVE_CASE1=1 and external network/GCP access",
)
async def test_live_case1_big_cypress_florida_panther() -> None:
    """Live: Big Cypress bbox + Florida panther (taxonKey 2435099) + Roseate spoonbill.

    Requires:
        - TRID3NT_TEST_LIVE_CASE1=1
        - Network access to GBIF + WDPA endpoints
        - GCP credentials for the runs/cache buckets (the flood model
          subworkflow dispatches Cloud Workflows — heavy; this live test
          is opt-in only).
    """
    bbox = (-81.4, 25.8, -80.9, 26.3)  # Big Cypress ~50km box
    result = await model_flood_habitat_scenario(
        bbox=bbox,
        species_keys=[2435099, 2481008],  # Florida panther, Roseate spoonbill
        rainfall_event="atlas14_100yr",
        protected_area_designation=["National Preserve"],
    )
    assert isinstance(result, CaseOneResult)
    # Per-species layers landed (one per species_key).
    assert len(result.species_layers) == 2
    # WDPA layer present.
    assert result.wdpa_layer_uri is not None
    # Summary populated.
    assert result.case_summary_text.strip() != ""
    # Per-species counts populated.
    assert "2435099" in result.species_counts
    assert "2481008" in result.species_counts
