"""Unit + integration tests for model_flood_scenario workflow (job-0042, M5 capstone).

Coverage maps to the kickoff's 8-test minimum + the headline NLCD-validation
gate (OQ-4 §4 / Invariant 7 mitigation):

1. ``test_registry_registers_run_model_flood_scenario_wrapper`` — the LLM-facing
   wrapper appears in ``TOOL_REGISTRY`` as ``run_model_flood_scenario`` with
   the workflow_dispatch metadata.
2. ``test_nlcd_validation_gate_raises_on_unmapped_class`` — when the fetched
   landcover has a class integer the mapping CSV doesn't cover,
   ``build_sfincs_model`` raises ``SFINCSSetupError("LULC_MAPPING_MISMATCH")``
   with full details (the OQ-4 §4 headline gate).
3. ``test_nlcd_validation_gate_passes_when_subset_of_mapping`` — when the
   fetched classes are a subset of the mapping, the gate passes through
   silently (so HydroMT proceeds).
4. ``test_load_manning_mapping_returns_expected_classes`` — the version-pinned
   CSV loads cleanly with the documented NLCD 2021 class set.
5. ``test_workflow_happy_path_returns_flood_envelope`` — full mocked happy
   path: workflow returns ``AssessmentEnvelope`` with ``hazard_type="flood"``,
   populated ``FloodPayload``, ``layers`` list with the depth COG.
6. ``test_workflow_returns_failed_envelope_when_run_solver_fails`` — when
   ``wait_for_completion`` returns ``RunResult(status="failed",
   error_code="SOLVER_FAILED")`` the workflow returns a typed failed
   envelope carrying the error code in ``flood.metrics.solver_version``.
7. ``test_workflow_returns_failed_envelope_when_nlcd_gate_fires`` —
   end-to-end: the workflow's response to a vintage mismatch is a typed
   failed envelope (not an uncaught exception).
8. ``test_workflow_geocode_fallback_when_bbox_missing`` — ``bbox=None`` +
   ``location_query="Fort Myers, FL"`` routes through ``geocode_location``
   and uses the resolved bbox for the fetcher chain.
9. ``test_workflow_direct_bbox_path_skips_geocode`` — ``bbox`` supplied
   directly, no geocode call.
10. ``test_workflow_bbox_wins_when_both_supplied`` — precedence: direct bbox
    overrides location_query.
11. ``test_workflow_cancellation_propagates`` — ``asyncio.CancelledError``
    raised inside ``wait_for_completion`` propagates out of the workflow.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.model_flood_scenario import (
    model_flood_scenario,
    run_model_flood_scenario,
)
from trid3nt_server.workflows.sfincs_builder import (
    BuildOptions,
    ForcingSpec,
    MANNING_MAPPING_PATH,
    MANNING_MAPPING_VERSION,
    SFINCSSetupError,
    build_sfincs_model,
    load_manning_mapping,
    validate_nlcd_vintage_against_mapping,
)
from trid3nt_contracts import new_ulid
from trid3nt_contracts.envelope import AssessmentEnvelope
from trid3nt_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult


# --------------------------------------------------------------------------- #
# Test 1 — registration of the wrapper atomic tool
# --------------------------------------------------------------------------- #


def test_registry_registers_run_model_flood_scenario_wrapper() -> None:
    """The LLM-facing wrapper is registered with workflow_dispatch metadata."""
    assert "run_model_flood_scenario" in TOOL_REGISTRY, (
        f"workflow wrapper not in TOOL_REGISTRY; keys={sorted(TOOL_REGISTRY)}"
    )
    entry = TOOL_REGISTRY["run_model_flood_scenario"]
    assert entry.metadata.cacheable is False, "workflow wrapper must be uncacheable"
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "workflow_dispatch"
    assert entry.fn is run_model_flood_scenario


# --------------------------------------------------------------------------- #
# Test 2 — NLCD validation gate FAIL path (the OQ-4 §4 headline)
# --------------------------------------------------------------------------- #


def test_nlcd_validation_gate_raises_on_unmapped_class() -> None:
    """A fetched class not covered by the mapping fires LULC_MAPPING_MISMATCH."""
    # Mapping covers classes {11, 41, 81}; the fetched raster has class 99
    # (not in mapping) — gate must fire.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False
    ) as fh:
        fh.write("nlcd_class,manning_n,description\n")
        fh.write("11,0.025,Open Water\n")
        fh.write("41,0.150,Deciduous Forest\n")
        fh.write("81,0.035,Pasture/Hay\n")
        fixture_path = Path(fh.name)
    try:
        mapping = load_manning_mapping(fixture_path)
        assert set(mapping) == {11, 41, 81}
        with pytest.raises(SFINCSSetupError) as excinfo:
            validate_nlcd_vintage_against_mapping(
                fetched_classes={11, 41, 99},  # 99 is unmapped
                nlcd_vintage_year=2021,
                mapping=mapping,
                mapping_version="test-1.0",
                mapping_csv_path=str(fixture_path),
            )
        err = excinfo.value
        assert err.error_code == "LULC_MAPPING_MISMATCH"
        assert err.details["unmapped_classes"] == [99]
        assert err.details["nlcd_vintage_year"] == 2021
        assert err.details["mapping_version"] == "test-1.0"
    finally:
        fixture_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Test 3 — NLCD validation gate PASS path
# --------------------------------------------------------------------------- #


def test_nlcd_validation_gate_passes_when_subset_of_mapping() -> None:
    """When every fetched class is in the mapping, the gate is silent."""
    mapping = load_manning_mapping(MANNING_MAPPING_PATH)
    # NLCD 2021 Fort Myers area: water, dev open, pasture, woody wetlands.
    fetched = {11, 21, 82, 90}
    # Should not raise.
    validate_nlcd_vintage_against_mapping(
        fetched_classes=fetched,
        nlcd_vintage_year=2021,
        mapping=mapping,
    )
    # And: class 0 (nodata) is filtered out even if it appears.
    validate_nlcd_vintage_against_mapping(
        fetched_classes=fetched | {0},
        nlcd_vintage_year=2021,
        mapping=mapping,
    )


# --------------------------------------------------------------------------- #
# Test 4 — version-pinned CSV loads with expected NLCD 2021 classes
# --------------------------------------------------------------------------- #


def test_load_manning_mapping_returns_expected_classes() -> None:
    """Production manning_mapping.csv covers the NLCD 2021 L48 class set."""
    mapping = load_manning_mapping(MANNING_MAPPING_PATH)
    # NLCD 2021 publishes these class integers in the CONUS L48 product;
    # every one must be in our mapping (gate would fire otherwise).
    expected = {11, 12, 21, 22, 23, 24, 31, 41, 42, 43, 51, 52, 71, 72, 73, 74, 81, 82, 90, 95}
    missing = expected - set(mapping.keys())
    assert not missing, f"manning_mapping.csv missing classes {missing}"
    # Sanity: every Manning's value is positive + plausible (<= 0.30).
    for cls, n in mapping.items():
        assert 0.0 < n <= 0.30, f"implausible manning_n={n} for nlcd_class={cls}"
    assert MANNING_MAPPING_VERSION == "1.0.0"


# --------------------------------------------------------------------------- #
# Fixtures for full-workflow tests — mocked atomic tools + GCS-aware shims
# --------------------------------------------------------------------------- #


def _make_handle(run_id: str | None = None) -> ExecutionHandle:
    """Construct a valid ExecutionHandle for tests."""
    return ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id or new_ulid(),
        solver="sfincs",
        compute_class="standard",
        workflows_execution_id=(
            "projects/test/locations/us-central1/workflows/"
            "model_flood_scenario/executions/test-exec"
        ),
        workflow_name="model_flood_scenario",
        workflow_location="us-central1",
        submitted_at=datetime.now(timezone.utc),
    )


def _mock_layer_uri(prefix: str) -> LayerURI:
    return LayerURI(
        layer_id=f"{prefix}-test",
        name=f"{prefix} test layer",
        layer_type="raster",
        uri=f"gs://test-cache/cache/static-30d/{prefix}/test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
    )


# --------------------------------------------------------------------------- #
# Test 5 — happy path: full workflow returns Flood AssessmentEnvelope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_happy_path_returns_flood_envelope() -> None:
    """Mocked happy chain returns AssessmentEnvelope with Flood subtype + layers."""
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_layer = _mock_layer_uri("landcover")
    landcover_result = {
        "layer": landcover_layer,
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4,
        "mean_depth_m": 0.6,
        "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345,
        "crs": "EPSG:3857",
        "units": "meters",
    }

    async def _wfc(handle):  # noqa: ANN001 — mock
        return run_result_ok

    # job-0254: the happy path means publish_layer SUCCEEDS (returns a WMS URL).
    # Previously this test left publish_layer unpatched and relied on the old
    # gs:// fallback masking the test-env GCS publish failure — but that
    # fallback was the leak this job closes. Patch publish to a WMS URL so the
    # primary layer legitimately lands (the genuine happy path).
    wms_url = (
        "https://qgis.test.example.com/ogc/wms?MAP=/mnt/qgs/grace2-sample.qgs"
        f"&LAYERS=flood-depth-peak-{run_id}"
    )
    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            return_value=wms_url,
        ),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
            return_period_yr=100,
            duration_hr=24,
            compute_class="medium",
        )
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.envelope_type == "modeled"
    assert envelope.hazard_type == "flood"
    assert envelope.workflow_name == "model_flood_scenario"
    assert envelope.flood is not None
    assert envelope.flood.metrics.max_depth_m == pytest.approx(2.4)
    assert envelope.flood.metrics.p95_depth_m == pytest.approx(1.9)
    assert envelope.flood.metrics.solver_version == "sfincs-v2.3.3"
    assert envelope.flood.metrics.grid_resolution_m == 30.0
    assert envelope.flood.metrics.simulation_duration_hours == 24
    assert len(envelope.layers) == 1
    assert envelope.layers[0].style_preset == "continuous_flood_depth"
    assert envelope.layers[0].role == "primary"
    # job-0254: the landed layer carries the renderable WMS URL (not gs://).
    assert envelope.layers[0].uri == wms_url
    assert envelope.forcing is not None
    assert envelope.forcing.forcing_type == "pluvial_synthetic"
    assert envelope.solver_run_ids == [run_id]


# --------------------------------------------------------------------------- #
# Test 6 — SOLVER_FAILED returns a typed failed envelope
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_returns_failed_envelope_when_run_solver_fails() -> None:
    """RunResult(status='failed', error_code='SOLVER_FAILED') → failed envelope."""
    handle = _make_handle()
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={},
        created_at=datetime.now(timezone.utc),
    )
    run_result_failed = RunResult(
        run_id=handle.run_id,
        handle_id=handle.handle_id,
        status="failed",
        output_uri=None,
        error_code="SOLVER_FAILED",
        error_message="sfincs exited with non-zero code 2",
    )

    async def _wfc(handle):  # noqa: ANN001
        return run_result_failed

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.hazard_type == "flood"
    assert envelope.layers == []
    assert envelope.flood is not None
    assert envelope.flood.metrics.solver_version == "failed:SOLVER_FAILED"
    assert envelope.flood.metrics.max_depth_m == 0.0
    assert envelope.solver_run_ids == [handle.run_id]


# --------------------------------------------------------------------------- #
# Test 7 — NLCD gate firing surfaces as failed envelope end-to-end
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_returns_failed_envelope_when_nlcd_gate_fires() -> None:
    """build_sfincs_model raises LULC_MAPPING_MISMATCH → workflow returns failed envelope."""
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2099,
        "dataset": "nlcd_2099",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }

    def _raising_build_sfincs_model(**kwargs: Any) -> ModelSetup:
        raise SFINCSSetupError(
            "LULC_MAPPING_MISMATCH",
            message="vintage 2099 introduced class 200 not in mapping",
            details={
                "nlcd_vintage_year": 2099,
                "mapping_version": "1.0.0",
                "unmapped_classes": [200],
            },
        )

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", side_effect=_raising_build_sfincs_model),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.layers == []
    assert envelope.flood is not None
    assert envelope.flood.metrics.solver_version == "failed:LULC_MAPPING_MISMATCH"
    # No solver runs dispatched since build failed.
    assert envelope.solver_run_ids == []


# --------------------------------------------------------------------------- #
# Test 8 — geocode fallback (no bbox, only location_query)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_geocode_fallback_when_bbox_missing() -> None:
    """``bbox=None`` + ``location_query="Fort Myers, FL"`` routes through geocode."""
    geocode_result = {
        "name": "Fort Myers, Lee County, Florida, USA",
        "latitude": 26.6,
        "longitude": -81.9,
        "bbox": [-81.92, 26.55, -81.80, 26.68],
        "source": "nominatim",
        "query": "Fort Myers, FL",
        "osm_type": "relation",
        "osm_id": 12345,
        "place_id": 1,
    }
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }

    def _raise_after_fetch(**_kwargs: Any) -> ModelSetup:
        # short-circuit so we don't try to run the solver in this test
        raise SFINCSSetupError("HYDROMT_UNAVAILABLE", message="test stub")

    with (
        patch(
            "trid3nt_server.workflows.model_flood_scenario.geocode_location",
            return_value=geocode_result,
        ) as mock_geocode,
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")) as mock_dem,
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", side_effect=_raise_after_fetch),
    ):
        envelope = await model_flood_scenario(
            location_query="Fort Myers, FL",
            return_period_yr=100,
            duration_hr=24,
        )
    mock_geocode.assert_called_once_with("Fort Myers, FL")
    # fetch_dem was called with the geocoded bbox.
    args, kwargs = mock_dem.call_args
    used_bbox = args[0] if args else kwargs.get("bbox")
    assert tuple(used_bbox) == (-81.92, 26.55, -81.80, 26.68)
    # Failed envelope shape — Hydromt unavailable surfaced as the test stub.
    assert envelope.flood.metrics.solver_version == "failed:HYDROMT_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# Test 9 — direct bbox path: geocode is NOT called
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_direct_bbox_path_skips_geocode() -> None:
    """When ``bbox`` is supplied directly, ``geocode_location`` is not called."""
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }

    def _raise(**_kwargs: Any) -> ModelSetup:
        raise SFINCSSetupError("HYDROMT_UNAVAILABLE", message="test stub")

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.geocode_location") as mock_geocode,
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", side_effect=_raise),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )
    mock_geocode.assert_not_called()
    assert envelope.bbox == (-81.92, 26.55, -81.80, 26.68)


# --------------------------------------------------------------------------- #
# Test 10 — both supplied → bbox wins (Decision K precedence)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_bbox_wins_when_both_supplied() -> None:
    """``bbox`` + ``location_query`` → bbox takes precedence; geocode NOT called."""
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }

    def _raise(**_kwargs: Any) -> ModelSetup:
        raise SFINCSSetupError("HYDROMT_UNAVAILABLE", message="test stub")

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.geocode_location") as mock_geocode,
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")) as mock_dem,
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", side_effect=_raise),
    ):
        await model_flood_scenario(
            bbox=(-95.0, 29.0, -94.5, 29.5),
            location_query="Fort Myers, FL",  # should be ignored
        )
    mock_geocode.assert_not_called()
    args, kwargs = mock_dem.call_args
    used_bbox = args[0] if args else kwargs.get("bbox")
    assert tuple(used_bbox) == (-95.0, 29.0, -94.5, 29.5)


# --------------------------------------------------------------------------- #
# Test 11 — asyncio.CancelledError propagates
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_cancellation_propagates() -> None:
    """asyncio.CancelledError raised inside wait_for_completion propagates."""
    handle = _make_handle()
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={},
        created_at=datetime.now(timezone.utc),
    )

    async def _wfc_cancelled(handle):  # noqa: ANN001
        raise asyncio.CancelledError()

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc_cancelled),
    ):
        with pytest.raises(asyncio.CancelledError):
            await model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
            )


# --------------------------------------------------------------------------- #
# Test 12 — OQ-49 hotfix (job-0052): SfincsModel.build receives a parsed dict,
# NOT the raw YAML text blob. This is the regression guard for the
# ``'str' object has no attribute 'keys'`` failure surfaced by job-0049's
# M5 smoke run against hydromt-sfincs 1.2.2.
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_passes_parsed_dict_to_hydromt_build(
    tmp_path: Path,
) -> None:
    """``model.build(opt=...)`` must receive a Dict[str, Dict], not a YAML string.

    hydromt-sfincs 1.2.x's ``SfincsModel.build`` parses ``opt`` by calling
    ``.keys()`` on every step value, so a raw YAML text blob raises
    ``'str' object has no attribute 'keys'`` deep inside ``_parse_steps``.
    The OQ-49 fix is ``yaml.safe_load(yaml_text)`` before passing — this test
    asserts the corrected path: ``model.build`` is called exactly once with
    ``opt`` shaped as a parsed mapping carrying the expected top-level step
    keys (``setup_config``, ``setup_grid_from_region``, ...).
    """
    # Subset Manning's CSV the gate will accept against fetched_classes={11, 41}.
    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n"
        "11,0.025,Open Water\n"
        "41,0.150,Deciduous Forest\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    class _FakeSfincsModel:
        def __init__(self, root: str, mode: str) -> None:  # noqa: D401
            captured["root"] = root
            captured["mode"] = mode

        def build(self, opt: Any) -> None:  # noqa: D401
            captured["opt"] = opt
            captured["opt_type"] = type(opt).__name__

        def write(self) -> None:  # noqa: D401
            captured["write_called"] = True

    fake_module = MagicMock()
    fake_module.SfincsModel = _FakeSfincsModel

    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=11.9,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"source": "noaa-atlas14"},
    )

    with (
        patch.dict(
            "sys.modules",
            {"hydromt_sfincs": fake_module},
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._extract_unique_nlcd_classes",
            return_value={11, 41},
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
            side_effect=lambda uri: (
                "/tmp/staged-" + uri[len("gs://"):].replace("/", "_")
                if uri.startswith("gs://") else uri
            ),
        ),
        # fsspec upload is best-effort — let it fail and fall back to file://.
        patch.dict(
            "sys.modules",
            {"fsspec": MagicMock(filesystem=MagicMock(side_effect=RuntimeError("no gcs in test")))},
            clear=False,
        ),
    ):
        setup = build_sfincs_model(
            dem_uri="gs://test/dem.tif",
            landcover_uri="gs://test/landcover.tif",
            river_geometry_uri=None,
            forcing=forcing,
            bbox=(-81.92, 26.55, -81.80, 26.68),
            options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
            nlcd_vintage_year=2021,
            manning_mapping_csv=mapping_path,
        )

    # The fix: opt is a parsed dict (not the YAML string) — the type that
    # hydromt-sfincs 1.2.x ``_parse_steps`` actually accepts.
    assert "opt" in captured, "model.build was not called"
    assert captured["opt_type"] == "dict", (
        f"OQ-49 regression: model.build received {captured['opt_type']!r} "
        f"(expected 'dict'); raw YAML string would re-trigger "
        f"'str' object has no attribute 'keys' inside hydromt-sfincs 1.2.x."
    )
    opt = captured["opt"]
    assert isinstance(opt, dict)
    # The parsed step keys our YAML config emits; nested values are dicts too.
    assert len(opt) > 0, "parsed opt dict is empty — YAML config generation broke"
    for step_name, step_kwargs in opt.items():
        assert isinstance(step_name, str)
        # hydromt-sfincs calls .keys() on every step value — must be a mapping.
        assert hasattr(step_kwargs, "keys"), (
            f"step {step_name!r} value is not a mapping; this is exactly the "
            f"'str' object has no attribute 'keys' shape that OQ-49 hit."
        )
    assert captured.get("write_called") is True
    assert setup.solver == "sfincs"


# --------------------------------------------------------------------------- #
# Test 13 — OQ-49 hotfix: malformed YAML surfaces as typed HYDROMT_BUILD_FAILED
# (FR-FR-2 substrate-integrity routing). yaml.safe_load is the seam where a
# bad config raises; the broad except wraps it into a SFINCSSetupError carrying
# the underlying message — never an uncaught crash.
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_malformed_yaml_surfaces_typed_error(
    tmp_path: Path,
) -> None:
    """Malformed YAML from the config generator → HYDROMT_BUILD_FAILED."""
    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n"
        "11,0.025,Open Water\n"
        "41,0.150,Deciduous Forest\n",
        encoding="utf-8",
    )

    # Patch the YAML generator to emit a string yaml.safe_load cannot parse.
    # The fake hydromt_sfincs module should never be reached (the parse fails
    # before model.build is invoked).
    fake_module = MagicMock()
    fake_module.SfincsModel = MagicMock(
        side_effect=AssertionError("SfincsModel must NOT be constructed on parse failure")
    )

    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=11.9,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"source": "noaa-atlas14"},
    )

    malformed_yaml = "this: is: not: valid: yaml: ::: ["

    with (
        patch.dict("sys.modules", {"hydromt_sfincs": fake_module}, clear=False),
        patch(
            "trid3nt_server.workflows.sfincs_builder._extract_unique_nlcd_classes",
            return_value={11, 41},
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
            side_effect=lambda uri: (
                "/tmp/staged-" + uri[len("gs://"):].replace("/", "_")
                if uri.startswith("gs://") else uri
            ),
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._generate_hydromt_yaml_config",
            return_value=malformed_yaml,
        ),
    ):
        with pytest.raises(SFINCSSetupError) as excinfo:
            build_sfincs_model(
                dem_uri="gs://test/dem.tif",
                landcover_uri="gs://test/landcover.tif",
                river_geometry_uri=None,
                forcing=forcing,
                bbox=(-81.92, 26.55, -81.80, 26.68),
                options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
                nlcd_vintage_year=2021,
                manning_mapping_csv=mapping_path,
            )

    err = excinfo.value
    assert err.error_code == "HYDROMT_BUILD_FAILED", (
        f"malformed YAML must surface as HYDROMT_BUILD_FAILED for FR-FR-2 "
        f"substrate-integrity routing; got {err.error_code!r}"
    )
    # Provenance: the wrapped error carries the bbox + URIs so the failed
    # envelope's pipeline strip can render a meaningful failure.
    assert err.details["bbox"] == [-81.92, 26.55, -81.80, 26.68]
    assert err.details["dem_uri"] == "gs://test/dem.tif"
    assert err.details["landcover_uri"] == "gs://test/landcover.tif"
    assert "underlying" in err.details


# --------------------------------------------------------------------------- #
# Test 14 — OQ-52 hotfix (job-0053): the setup_manning_roughness step emits
# the hydromt-sfincs 1.2.x-accepted kwarg shape. The live signature is
# ``setup_manning_roughness(datasets_rgh, manning_land, manning_sea,
# rgh_lev_land)`` — there is NO top-level ``map_fn`` keyword. The LULC →
# Manning's reclass CSV is threaded INSIDE each ``datasets_rgh`` entry
# under the key ``reclass_table`` (per ``_parse_datasets_rgh``), and the
# CSV itself must have first column = LULC class (index_col=0) plus a
# column literally named ``N``. This test is the regression guard.
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_emits_v1_2_x_manning_roughness_kwargs(
    tmp_path: Path,
) -> None:
    """``setup_manning_roughness`` kwargs must match hydromt-sfincs 1.2.x.

    Failure modes this guards against:
      * Re-emitting a top-level ``map_fn`` key (1.2.x rejects with
        ``TypeError: setup_manning_roughness() got an unexpected keyword
        argument 'map_fn'`` — the OQ-52 blocker observed by job-0052).
      * Forgetting ``reclass_table`` inside each ``datasets_rgh`` entry —
        without it, ``_parse_datasets_rgh`` raises ``IOError("Manning
        roughness 'reclass_table' csv file must be provided")``.
      * Writing the reclass CSV without an ``N`` column — HydroMT
        reclassifies via ``df_map[["N"]]``; the wrong column header is a
        silent-wrong-answer in waiting.
    """
    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n"
        "11,0.025,Open Water\n"
        "41,0.150,Deciduous Forest\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    class _FakeSfincsModel:
        def __init__(self, root: str, mode: str) -> None:  # noqa: D401
            captured["root"] = root

        def build(self, opt: Any) -> None:  # noqa: D401
            captured["opt"] = opt

        def write(self) -> None:  # noqa: D401
            captured["write_called"] = True

    fake_module = MagicMock()
    fake_module.SfincsModel = _FakeSfincsModel

    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=11.9,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"source": "noaa-atlas14"},
    )

    def _fake_stage(uri: str) -> str:
        # job-0249: mirror _stage_gcs_local without network — gs:// inputs
        # become deterministic staged-local paths, locals pass through.
        if uri.startswith("gs://"):
            return str(tmp_path / "staged" / uri[len("gs://"):].replace("/", "_"))
        if uri.startswith("file://"):
            return uri[len("file://"):]
        return uri

    with (
        patch.dict("sys.modules", {"hydromt_sfincs": fake_module}, clear=False),
        patch(
            "trid3nt_server.workflows.sfincs_builder._extract_unique_nlcd_classes",
            return_value={11, 41},
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
            side_effect=lambda uri: (
                "/tmp/staged-" + uri[len("gs://"):].replace("/", "_")
                if uri.startswith("gs://") else uri
            ),
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
            side_effect=_fake_stage,
        ),
    ):
        build_sfincs_model(
            dem_uri="gs://test/dem.tif",
            landcover_uri="gs://test/landcover.tif",
            river_geometry_uri=None,
            forcing=forcing,
            bbox=(-81.92, 26.55, -81.80, 26.68),
            options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
            nlcd_vintage_year=2021,
            manning_mapping_csv=mapping_path,
        )

    opt = captured.get("opt")
    assert isinstance(opt, dict), "model.build was not called with a parsed dict"
    assert "setup_manning_roughness" in opt, (
        f"setup_manning_roughness step missing from build opt; got keys {list(opt)}"
    )
    rgh_kwargs = opt["setup_manning_roughness"]
    assert isinstance(rgh_kwargs, dict)

    # The OQ-52 regression: ``map_fn`` MUST NOT appear as a top-level key.
    assert "map_fn" not in rgh_kwargs, (
        "OQ-52 regression: setup_manning_roughness emitted top-level 'map_fn' "
        "kwarg — hydromt-sfincs 1.2.x raises "
        "'TypeError: got an unexpected keyword argument map_fn'. The reclass "
        "table belongs INSIDE each datasets_rgh entry as 'reclass_table'."
    )

    # Verify the v1.2.x-accepted kwarg names are present (every key here is
    # a parameter of the live 1.2.2 SfincsModel.setup_manning_roughness
    # signature: datasets_rgh, manning_land, manning_sea, rgh_lev_land).
    assert "datasets_rgh" in rgh_kwargs
    valid_top_level_keys = {
        "datasets_rgh",
        "manning_land",
        "manning_sea",
        "rgh_lev_land",
    }
    extra = set(rgh_kwargs.keys()) - valid_top_level_keys
    assert not extra, (
        f"setup_manning_roughness has unexpected top-level kwargs {extra}; "
        f"hydromt-sfincs 1.2.x accepts only {valid_top_level_keys}."
    )

    # datasets_rgh is a list[dict]; each entry must carry lulc + reclass_table
    # (the only path through ``_parse_datasets_rgh`` that hits a
    # reclassification). Without reclass_table the parser raises IOError.
    datasets = rgh_kwargs["datasets_rgh"]
    assert isinstance(datasets, list) and len(datasets) == 1
    entry = datasets[0]
    assert "lulc" in entry, f"datasets_rgh entry missing 'lulc'; got {entry}"
    assert "reclass_table" in entry, (
        f"datasets_rgh entry missing 'reclass_table' (this was 'map_fn' "
        f"pre-fix); hydromt-sfincs 1.2.x ``_parse_datasets_rgh`` requires "
        f"this key alongside ``lulc``. Got: {entry}"
    )

    # The reclass_table the YAML points at must exist and carry the v1.2.x
    # column shape (first column = LULC class index; column named ``N``).
    reclass_csv_path = Path(entry["reclass_table"])
    assert reclass_csv_path.exists() or reclass_csv_path.name == "manning_reclass.csv", (
        f"reclass_table CSV path {reclass_csv_path} should be the temp file "
        f"written by _write_hydromt_reclass_table_csv"
    )

    # Independent unit check on the writer itself — round-trip the in-memory
    # mapping through the CSV format hydromt-sfincs 1.2.x reads. This is the
    # behavior of the helper that supplies the on-disk substrate the YAML
    # references.
    from trid3nt_server.workflows.sfincs_builder import (
        _write_hydromt_reclass_table_csv,
    )

    out = _write_hydromt_reclass_table_csv(
        {11: 0.025, 41: 0.150}, tmp_path / "rt.csv"
    )
    text = out.read_text(encoding="utf-8")
    # First row is the header: first column = index, then ``N``.
    header_line = text.splitlines()[0]
    cols = [c.strip() for c in header_line.split(",")]
    assert cols[0] in {"nlcd_class", "lulc", "class"}, (
        f"reclass_table first column must be the LULC class index; got {cols[0]!r}"
    )
    assert "N" in cols, (
        f"reclass_table must have a column literally named 'N' — "
        f"hydromt-sfincs 1.2.x ``_parse_datasets_rgh`` indexes ``df_map[['N']]``. "
        f"Got header columns: {cols}"
    )


# --------------------------------------------------------------------------- #
# Test 15 — v0.1 scope guard (job-0055, OQ-54 routing recommendation b):
# ``setup_river_inflow`` must NOT appear in the YAML for ``pluvial_synthetic``
# mode. The v0.1 M5 demo is pluvial-only (Atlas 14 design storm); river inflow
# is M5+ / sprint-9+ scope. Additionally, hydromt-sfincs 1.2.2's
# ``set_forcing_1d`` (sfincs.py:1858) calls ``pd.RangeIndex.is_integer()``
# which was removed in pandas ≥ 2.0 (we run 3.0.3); this upstream bug is
# exercised by the river-inflow path. Dropping the step bypasses
# ``set_forcing_1d`` entirely.
#
# Historical note (job-0054, OQ-53): this was previously a guard that
# ``setup_river_inflow`` was present but WITHOUT ``hydrography: merit_hydro``.
# Job-0055 advances this to a complete step-omission guard for v0.1 pluvial.
# --------------------------------------------------------------------------- #


def _build_with_capture(
    *,
    tmp_path: Path,
    river_geometry_uri: str | None,
) -> dict[str, Any]:
    """Run ``build_sfincs_model`` against a fake hydromt-sfincs, return captured opt.

    Helper used by the migration-audit tests. Mocks the dep extraction, the
    SfincsModel constructor, and the build()/write() calls; returns the parsed
    ``opt`` dict that ``SfincsModel.build`` would receive.
    """
    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n"
        "11,0.025,Open Water\n"
        "41,0.150,Deciduous Forest\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    class _FakeSfincsModel:
        def __init__(self, root: str, mode: str) -> None:  # noqa: D401
            captured["root"] = root

        def build(self, opt: Any) -> None:  # noqa: D401
            captured["opt"] = opt

        def write(self) -> None:  # noqa: D401
            captured["write_called"] = True

    fake_module = MagicMock()
    fake_module.SfincsModel = _FakeSfincsModel

    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=11.9,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"source": "noaa-atlas14"},
    )

    def _fake_stage(uri: str) -> str:
        # job-0249: mirror _stage_gcs_local without network — gs:// inputs
        # become deterministic staged-local paths, locals pass through.
        if uri.startswith("gs://"):
            return str(tmp_path / "staged" / uri[len("gs://"):].replace("/", "_"))
        if uri.startswith("file://"):
            return uri[len("file://"):]
        return uri

    with (
        patch.dict("sys.modules", {"hydromt_sfincs": fake_module}, clear=False),
        patch(
            "trid3nt_server.workflows.sfincs_builder._extract_unique_nlcd_classes",
            return_value={11, 41},
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
            side_effect=lambda uri: (
                "/tmp/staged-" + uri[len("gs://"):].replace("/", "_")
                if uri.startswith("gs://") else uri
            ),
        ),
        patch(
            "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
            side_effect=_fake_stage,
        ),
    ):
        build_sfincs_model(
            dem_uri="gs://test/dem.tif",
            landcover_uri="gs://test/landcover.tif",
            river_geometry_uri=river_geometry_uri,
            forcing=forcing,
            bbox=(-81.92, 26.55, -81.80, 26.68),
            options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
            nlcd_vintage_year=2021,
            manning_mapping_csv=mapping_path,
        )
    return captured


def test_build_sfincs_model_river_inflow_not_emitted_in_pluvial_synthetic(
    tmp_path: Path,
) -> None:
    """v0.1 scope guard: ``setup_river_inflow`` MUST NOT appear in pluvial_synthetic YAML.

    Failure modes this guards against:
      * Re-introducing the ``setup_river_inflow`` block for v0.1 — the river-
        inflow path triggers hydromt-sfincs 1.2.2's ``set_forcing_1d``
        (sfincs.py:1858) which calls ``pd.RangeIndex.is_integer()``, removed
        in pandas ≥ 2.0 (we run 3.0.3). This upstream bug blocks the chain
        from reaching solver dispatch (job-0054 honest outcome disclosure).
      * Scope creep: the v0.1 M5 demo is pluvial-only (Atlas 14 design storm);
        river inflow is M5+ / sprint-9+ scope (real ATCF + storm surge).

    The ``river_geometry_uri`` is still passed to ``build_sfincs_model`` (the
    FGB is fetched and cached for future use); only the YAML step is omitted.

    Historical context: job-0054 (OQ-53) fixed ``setup_river_inflow`` to omit
    the ``hydrography: merit_hydro`` kwarg (CONUS bboxes raised
    ``NoDataException`` against the Italy-only artifact_data tile). Job-0055
    (OQ-54 routing recommendation b) completes the v0.1 remediation by
    dropping the entire block.
    """
    # Case 1: river_geometry_uri supplied (the FGB is available) — step still omitted.
    captured_with_river = _build_with_capture(
        tmp_path=tmp_path,
        river_geometry_uri="gs://test/river.fgb",
    )
    opt_with = captured_with_river.get("opt")
    assert isinstance(opt_with, dict)
    assert "setup_river_inflow" not in opt_with, (
        "job-0055 v0.1 scope violation: setup_river_inflow was re-introduced "
        "into the pluvial_synthetic YAML. This step triggers hydromt-sfincs "
        "1.2.2's set_forcing_1d which calls pd.RangeIndex.is_integer() — "
        "removed in pandas ≥ 2.0 (we run 3.0.3). The v0.1 M5 demo is "
        "pluvial-only; river inflow is M5+ scope. "
        f"Opt keys found: {list(opt_with)}"
    )

    # Case 2: river_geometry_uri=None — step also omitted (same code path).
    captured_no_river = _build_with_capture(
        tmp_path=tmp_path,
        river_geometry_uri=None,
    )
    opt_none = captured_no_river.get("opt")
    assert isinstance(opt_none, dict)
    assert "setup_river_inflow" not in opt_none, (
        "setup_river_inflow appeared when river_geometry_uri=None — "
        f"unexpected; opt keys: {list(opt_none)}"
    )


# --------------------------------------------------------------------------- #
# Test 16 — OQ-54 hotfix: setup_precip_forcing emits the v1.2.x-accepted
# kwarg shape. Live signature is ``setup_precip_forcing(timeseries=None,
# magnitude=None)`` — accepts EITHER a tabulated timeseries CSV path OR a
# constant rate in mm/hr. The previous YAML emitted ``precip`` +
# ``duration_hr`` (neither is a valid 1.2.x kwarg).
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_precip_forcing_emits_magnitude_kwarg(
    tmp_path: Path,
) -> None:
    """``setup_precip_forcing`` kwargs must match hydromt-sfincs 1.2.x.

    Failure modes this guards against:
      * Emitting ``precip`` or ``duration_hr`` (the pre-OQ-54 shape) — 1.2.x
        raises ``TypeError: setup_precip_forcing() got an unexpected keyword
        argument 'precip'`` / ``'duration_hr'``.
      * Forgetting to convert Atlas 14's depth-over-duration to the mm/hr
        rate ``magnitude`` expects — the source builds a constant series
        at ``magnitude`` and SFINCS would receive the wrong forcing.
    """
    captured = _build_with_capture(
        tmp_path=tmp_path,
        river_geometry_uri=None,
    )

    opt = captured.get("opt")
    assert isinstance(opt, dict)
    assert "setup_precip_forcing" in opt, (
        f"setup_precip_forcing step missing from build opt; got keys {list(opt)}"
    )
    p_kwargs = opt["setup_precip_forcing"]
    assert isinstance(p_kwargs, dict)

    # The OQ-54 regression: ``precip`` + ``duration_hr`` MUST NOT appear.
    assert "precip" not in p_kwargs, (
        "OQ-54 regression: setup_precip_forcing emitted 'precip' kwarg — "
        "hydromt-sfincs 1.2.x raises TypeError. Use 'magnitude' (mm/hr)."
    )
    assert "duration_hr" not in p_kwargs, (
        "OQ-54 regression: setup_precip_forcing emitted 'duration_hr' "
        "kwarg — hydromt-sfincs 1.2.x raises TypeError."
    )

    # Only ``timeseries`` and ``magnitude`` are the v1.2.x-accepted kwargs.
    valid_keys = {"timeseries", "magnitude"}
    extra = set(p_kwargs.keys()) - valid_keys
    assert not extra, (
        f"setup_precip_forcing has unexpected kwargs {extra}; "
        f"hydromt-sfincs 1.2.x accepts only {valid_keys}."
    )

    # The conversion math: Atlas 14 (11.9 in over 24 hr) → mm/hr.
    # Expected: 11.9 * 25.4 / 24 = 12.5916666... mm/hr
    assert "magnitude" in p_kwargs, (
        f"setup_precip_forcing missing 'magnitude'; got {p_kwargs}"
    )
    expected_mm_per_hr = (11.9 * 25.4) / 24.0
    assert abs(p_kwargs["magnitude"] - expected_mm_per_hr) < 1e-6, (
        f"Atlas 14 conversion incorrect: got {p_kwargs['magnitude']}, "
        f"expected {expected_mm_per_hr} mm/hr"
    )


# --------------------------------------------------------------------------- #
# Test 17 — job-0054 comprehensive migration audit: every setup_* step our
# YAML emits has kwargs that are a subset of the live 1.2.2 signature
# parameter set. This is the ALL-STEPS regression guard against drift —
# if hydromt-sfincs adds/renames a kwarg in a future release, this test
# fires on the offending step.
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_all_setup_steps_match_live_signatures(
    tmp_path: Path,
) -> None:
    """Every setup_* step's kwargs must match the live 1.2.2 SfincsModel signature.

    Iterates the parsed ``opt`` dict, looks up the matching ``SfincsModel``
    method, calls ``inspect.signature``, and asserts the emitted kwargs are
    a subset of the live parameter names. Skips ``setup_config`` (takes
    ``**cfdict``) and steps not present in the parsed opt.

    This is the comprehensive migration audit's residual guard — the
    individual ``map_fn``/``hydrography``/``magnitude`` tests cover the
    known mismatches; this catches anything we'd otherwise miss.
    """
    import inspect as _inspect

    try:
        import hydromt_sfincs as _hms  # type: ignore[import-not-found]
    except Exception:
        pytest.skip("hydromt_sfincs not installed; live-signature audit cannot run")

    captured = _build_with_capture(
        tmp_path=tmp_path,
        river_geometry_uri="gs://test/river.fgb",
    )

    opt = captured.get("opt")
    assert isinstance(opt, dict)

    # Steps whose live signature is ``**kwargs``-only (any key is accepted).
    permissive_steps = {"setup_config"}

    for step_name, step_kwargs in opt.items():
        if step_name in permissive_steps:
            continue
        method = getattr(_hms.SfincsModel, step_name, None)
        assert method is not None, (
            f"job-0054 audit: YAML emits unknown setup step {step_name!r} — "
            f"hydromt-sfincs 1.2.x SfincsModel has no method by that name."
        )
        live_sig = _inspect.signature(method)
        # Strip ``self`` and any ``**kwargs`` catch-all (which would accept
        # any extra kwarg, so we don't need to enforce subset there).
        live_params = {
            name
            for name, p in live_sig.parameters.items()
            if name != "self" and p.kind is not _inspect.Parameter.VAR_KEYWORD
        }
        has_var_kw = any(
            p.kind is _inspect.Parameter.VAR_KEYWORD
            for p in live_sig.parameters.values()
        )
        if has_var_kw:
            continue  # method accepts arbitrary kwargs; nothing to enforce.
        emitted = set(step_kwargs.keys()) if isinstance(step_kwargs, dict) else set()
        extra = emitted - live_params
        assert not extra, (
            f"job-0054 audit: YAML step {step_name!r} emits kwargs {extra} "
            f"not in live 1.2.2 signature {live_params}. Update "
            f"_generate_hydromt_yaml_config to match the v1.2.x API."
        )


# --------------------------------------------------------------------------- #
# Test 18 — job-0057: build_sfincs_model emits a manifest.json that conforms
# to the worker contract (services/workers/sfincs/entrypoint.py:9-23).
#
# Schema the worker reads:
#   {
#     "inputs": [{"gs_uri": "gs://...", "dest": "<filename>"}, ...],
#     "sfincs_args": [],
#     "outputs": ["sfincs_map.nc", "*.nc", "*.tif"]
#   }
#
# The worker calls ``blob.download_as_text()`` on the manifest URI then
# ``json.loads(text)`` — so the manifest MUST be a JSON FILE, not a
# directory. This was the exact bug that caused SOLVER_FAILED in job-0056.
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_emits_manifest_json_with_input_list(
    tmp_path: Path,
) -> None:
    """``build_sfincs_model`` writes a manifest.json with the worker-contract shape.

    Asserts:
    - A ``manifest.json`` is emitted alongside the deck build.
    - Its ``inputs`` list contains at least one entry for every deck file
      produced by HydroMT (mocked to produce sfincs.inp + dep.tif).
    - Each input entry has both ``gs_uri`` and ``dest`` keys.
    - ``sfincs_args`` is a list (empty for v0.1).
    - ``outputs`` contains ``"sfincs_map.nc"`` (the headline output the
      postprocessing step reads).
    - The ``gs_uri`` values start with ``gs://`` and include the deck base
      prefix; ``dest`` values are bare filenames (no path separators).
    """
    import json as _json

    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n"
        "11,0.025,Open Water\n"
        "41,0.150,Deciduous Forest\n",
        encoding="utf-8",
    )

    # We'll capture what files the fake HydroMT writes into the deck dir so
    # we can assert the manifest covers them all.
    captured_manifest: dict[str, Any] = {}

    class _FakeSfincsModel:
        def __init__(self, root: str, mode: str) -> None:  # noqa: D401
            self._root = root

        def build(self, opt: Any) -> None:  # noqa: D401
            # Simulate HydroMT writing deck files into the root directory.
            deck_dir = Path(self._root)
            deck_dir.mkdir(parents=True, exist_ok=True)
            (deck_dir / "sfincs.inp").write_text("[sfincs input]\n", encoding="utf-8")
            (deck_dir / "dep.tif").write_bytes(b"FAKE_GEOTIFF")

        def write(self) -> None:  # noqa: D401
            pass  # write() is already called inside build above in our stub

    fake_module = MagicMock()
    fake_module.SfincsModel = _FakeSfincsModel

    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=11.9,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"source": "noaa-atlas14"},
    )

    # Use a fixed setup URI so we can assert the gs_uri prefix in the manifest.
    # GCP is decommissioned: the manifest lands on S3 via boto3.
    fixed_manifest_uri = (
        "s3://trid3nt-cache/cache/static-30d/sfincs_setup/"
        "TESTID01/manifest.json"
    )

    # Inject an in-memory S3 client (the boto3 put_object seam the deck upload
    # uses via tools.simulation.solver._get_s3_client). Capture the manifest.json body.
    from trid3nt_server.tools.simulation.solver import set_s3_client

    uploaded_files: dict[str, Any] = {}

    class _FakeS3:
        def put_object(self, *, Bucket, Key, Body, ContentType=None):
            data = Body.read() if hasattr(Body, "read") else Body
            if Key.endswith("manifest.json"):
                uploaded_files["manifest_content"] = _json.loads(
                    data.decode("utf-8") if isinstance(data, bytes) else data
                )
                uploaded_files["manifest_uri"] = f"s3://{Bucket}/{Key}"
            return {}

    set_s3_client(_FakeS3())
    try:
        with (
            patch.dict("sys.modules", {"hydromt_sfincs": fake_module}, clear=False),
            patch(
                "trid3nt_server.workflows.sfincs_builder._extract_unique_nlcd_classes",
                return_value={11, 41},
            ),
            patch(
                "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
                side_effect=lambda uri: (
                    "/tmp/staged-" + uri.split("://", 1)[-1].replace("/", "_")
                    if "://" in uri else uri
                ),
            ),
            patch(
                "trid3nt_server.workflows.sfincs_builder._default_setup_uri",
                return_value=fixed_manifest_uri,
            ),
        ):
            setup = build_sfincs_model(
                dem_uri="s3://test/dem.tif",
                landcover_uri="s3://test/landcover.tif",
                river_geometry_uri=None,
                forcing=forcing,
                bbox=(-81.92, 26.55, -81.80, 26.68),
                options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
                nlcd_vintage_year=2021,
                manning_mapping_csv=mapping_path,
            )
    finally:
        set_s3_client(None)

    # The manifest file should have been captured by our fake S3 client.
    assert "manifest_content" in uploaded_files, (
        "build_sfincs_model did not upload a manifest.json via boto3; "
        "the worker would hit 404 on the manifest URI."
    )
    manifest = uploaded_files["manifest_content"]

    # Shape assertions — must match the worker contract schema.
    assert isinstance(manifest, dict), "manifest.json must be a JSON object"
    assert "inputs" in manifest, "manifest missing 'inputs' key"
    assert "sfincs_args" in manifest, "manifest missing 'sfincs_args' key"
    assert "outputs" in manifest, "manifest missing 'outputs' key"

    inputs = manifest["inputs"]
    assert isinstance(inputs, list), "'inputs' must be a list"
    assert len(inputs) >= 1, (
        "manifest 'inputs' list is empty — worker would download nothing "
        "and SFINCS would fail to find sfincs.inp"
    )

    # Every input entry must have both 'gs_uri' and 'dest'.
    for entry in inputs:
        assert "gs_uri" in entry, f"input entry missing 'gs_uri': {entry}"
        assert "dest" in entry, f"input entry missing 'dest': {entry}"
        assert entry["gs_uri"].startswith("s3://"), (
            f"input gs_uri must be an s3:// URI; got {entry['gs_uri']!r}"
        )
        # dest may be a relative path (e.g. "gis/dep.tif" for subdirectory
        # files); the worker does ``scratch / item["dest"]`` which handles
        # POSIX relative paths correctly.
        assert entry["dest"]  # non-empty
        assert not entry["dest"].startswith("/"), (
            f"input dest must be relative, not absolute; got {entry['dest']!r}"
        )

    # sfincs.inp must appear in inputs (SFINCS reads it from CWD).
    dest_names = {e["dest"] for e in inputs}
    assert "sfincs.inp" in dest_names, (
        f"manifest 'inputs' does not include 'sfincs.inp'; "
        f"SFINCS requires this file in CWD. Found: {sorted(dest_names)}"
    )

    # dep.tif must also appear (the DEM the model was built with).
    assert "dep.tif" in dest_names, (
        f"manifest 'inputs' does not include 'dep.tif'; found: {sorted(dest_names)}"
    )

    # gs_uri values must include the expected deck-base/deck/ prefix.
    # fsspec.upload(deck_dir, deck_base_uri, recursive=True) uploads the
    # "deck" directory as a child of deck_base_uri, so files land at
    # deck_base_uri/deck/<relative>.
    expected_prefix = (
        "s3://trid3nt-cache/cache/static-30d/sfincs_setup/TESTID01/deck/"
    )
    for entry in inputs:
        assert entry["gs_uri"].startswith(expected_prefix), (
            f"input gs_uri {entry['gs_uri']!r} does not start with the "
            f"expected deck prefix {expected_prefix!r}. The worker "
            "downloads each input by its gs_uri; a mismatched prefix means "
            "the files are not where the manifest says they are."
        )

    # sfincs_args must be a list (empty for v0.1).
    assert isinstance(manifest["sfincs_args"], list), (
        "'sfincs_args' must be a list"
    )

    # outputs must include the headline flood-depth file.
    assert "sfincs_map.nc" in manifest["outputs"], (
        f"'sfincs_map.nc' missing from outputs; "
        f"postprocess_flood looks for this file. Got: {manifest['outputs']}"
    )

    # Regression: setup_uri must be the manifest file URI (not the directory).
    assert setup.setup_uri == fixed_manifest_uri, (
        f"ModelSetup.setup_uri should be the manifest file URI "
        f"{fixed_manifest_uri!r}; got {setup.setup_uri!r}. "
        "The worker passes this to _read_manifest → blob.download_as_text(); "
        "a directory URI hits 404."
    )


# --------------------------------------------------------------------------- #
# Test 19 — job-0057: ModelSetup.setup_uri returned by build_sfincs_model
# ends with ``/manifest.json`` — confirming the agent hands the worker a
# file URI, not a trailing-slash directory URI.
#
# This is the regression guard for the exact 404 observed in job-0056:
#   "ERROR google.api_core.exceptions.NotFound: 404 GET .../sfincs_setup/
#    01KTHQP54XVAAF2NPGKTAMP4PV/: No such object"
# --------------------------------------------------------------------------- #


def test_build_sfincs_model_setup_uri_points_at_manifest_file(
    tmp_path: Path,
) -> None:
    """``ModelSetup.setup_uri`` must end with ``/manifest.json``, never with ``/``.

    The worker contract (entrypoint.py:9-23) requires ``--manifest-uri`` to be
    a single JSON file URI.  The agent passes ``ModelSetup.setup_uri`` as that
    URI.  A trailing-slash directory URI causes:

        ``404 GET .../sfincs_setup/<id>/: No such object``

    because GCS has no object with that exact key.

    This test exercises the default path (no ``output_setup_uri`` override)
    and an override path where the caller supplies a directory URI, verifying
    that the normalisation logic appends ``manifest.json`` in both cases.
    """
    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n"
        "11,0.025,Open Water\n"
        "41,0.150,Deciduous Forest\n",
        encoding="utf-8",
    )

    class _FakeSfincsModel:
        def __init__(self, root: str, mode: str) -> None:  # noqa: D401
            self._root = root

        def build(self, opt: Any) -> None:  # noqa: D401
            deck_dir = Path(self._root)
            deck_dir.mkdir(parents=True, exist_ok=True)
            (deck_dir / "sfincs.inp").write_text("[sfincs input]\n", encoding="utf-8")

        def write(self) -> None:  # noqa: D401
            pass

    fake_module = MagicMock()
    fake_module.SfincsModel = _FakeSfincsModel

    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=11.9,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"source": "noaa-atlas14"},
    )

    fake_fsspec = MagicMock()
    fake_fsspec.filesystem.return_value = MagicMock(
        upload=MagicMock()  # swallow uploads silently
    )

    def _run_build(options: BuildOptions) -> "ModelSetup":
        with (
            patch.dict("sys.modules", {"hydromt_sfincs": fake_module, "fsspec": fake_fsspec}, clear=False),
            patch(
                "trid3nt_server.workflows.sfincs_builder._extract_unique_nlcd_classes",
                return_value={11, 41},
            ),
            patch(
                "trid3nt_server.workflows.sfincs_builder._stage_gcs_local",
                side_effect=lambda uri: (
                    "/tmp/staged-" + uri[len("gs://"):].replace("/", "_")
                    if uri.startswith("gs://") else uri
                ),
            ),
        ):
            return build_sfincs_model(
                dem_uri="gs://test/dem.tif",
                landcover_uri="gs://test/landcover.tif",
                river_geometry_uri=None,
                forcing=forcing,
                bbox=(-81.92, 26.55, -81.80, 26.68),
                options=options,
                nlcd_vintage_year=2021,
                manning_mapping_csv=mapping_path,
            )

    # --- Case 1: default path (no output_setup_uri override) ---
    setup_default = _run_build(BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0))
    assert setup_default.setup_uri.endswith("/manifest.json"), (
        f"Default path: ModelSetup.setup_uri must end with '/manifest.json'; "
        f"got {setup_default.setup_uri!r}. A directory URI (trailing '/') "
        "causes 404 in the worker's _read_manifest call."
    )
    assert not setup_default.setup_uri.endswith("//manifest.json"), (
        "Double-slash in URI: deck_base + manifest.json produced '//' — "
        f"check the URI composition logic. Got {setup_default.setup_uri!r}"
    )

    # --- Case 2: output_setup_uri override as a directory URI (trailing /) ---
    # Callers that previously passed a directory override must still work;
    # the normalisation logic should append 'manifest.json'.
    setup_dir_override = _run_build(
        BuildOptions(
            grid_resolution_m=30.0,
            simulation_hours=24.0,
            output_setup_uri="gs://legacy-cloud-cache/cache/custom-run/test-setup/",
        )
    )
    assert setup_dir_override.setup_uri.endswith("/manifest.json"), (
        f"Directory-override path: setup_uri must end with '/manifest.json'; "
        f"got {setup_dir_override.setup_uri!r}."
    )
    assert setup_dir_override.setup_uri == (
        "gs://legacy-cloud-cache/cache/custom-run/test-setup/manifest.json"
    ), (
        f"Directory override did not normalise correctly; "
        f"got {setup_dir_override.setup_uri!r}"
    )

    # --- Case 3: output_setup_uri already ends with /manifest.json ---
    setup_manifest_override = _run_build(
        BuildOptions(
            grid_resolution_m=30.0,
            simulation_hours=24.0,
            output_setup_uri=(
                "gs://legacy-cloud-cache/cache/custom-run/test-setup/manifest.json"
            ),
        )
    )
    assert setup_manifest_override.setup_uri == (
        "gs://legacy-cloud-cache/cache/custom-run/test-setup/manifest.json"
    ), (
        f"Manifest override was mutated unexpectedly; "
        f"got {setup_manifest_override.setup_uri!r}"
    )


# --------------------------------------------------------------------------- #
# Test 20 — job-0058 OQ-58: postprocess_flood squeezes singleton timemax dim
# before COG write. HydroMT-SFINCS 1.2.2 emits hmax with shape
# (timemax=1, n, m); rasterio.write(arr, 1) expects exactly 2D.
# This test constructs a fake sfincs_map.nc with hmax shape (1, 8, 8) and
# asserts _extract_peak_depth_geotiff succeeds + produces a 2D raster.
# --------------------------------------------------------------------------- #


def test_extract_peak_depth_geotiff_squeezes_singleton_timemax_dim(
    tmp_path: Path,
) -> None:
    """``_extract_peak_depth_geotiff`` handles hmax shape (1, n, m) without error.

    HydroMT-SFINCS 1.2.2 emits ``hmax`` with an extra leading ``timemax=1``
    dimension.  Before the OQ-58 fix, rasterio raised:

        ``Source shape (1, 1, 527, 540) is inconsistent with given indexes 1``

    because ``dst.write(arr, 1)`` expects a 2D array when the band index is
    supplied as an int.  After the fix, the singleton dim is squeezed before the
    write so the COG is a valid 2D single-band raster.

    Regression guard: if the squeeze is removed, this test re-triggers the
    ``COG_WRITE_FAILED`` error.
    """
    import numpy as np

    try:
        import xarray as xr
    except ImportError:
        pytest.skip("xarray not installed; skipping COG-squeeze integration test")

    try:
        import rasterio
    except ImportError:
        pytest.skip("rasterio not installed; skipping COG-squeeze integration test")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff

    # Build a synthetic sfincs_map.nc with hmax shape (timemax=1, n=8, m=8).
    n, m = 8, 8
    rng = np.random.default_rng(42)
    # Some dry (0.0) and some flooded cells (0.1 – 3.5 m) to exercise metrics.
    hmax_data = rng.uniform(0.0, 3.5, (1, n, m)).astype("float32")
    hmax_data[0, :2, :] = 0.0  # force some dry cells

    x_vals = np.linspace(-81.92, -81.80, m, dtype="float64")
    y_vals = np.linspace(26.55, 26.68, n, dtype="float64")

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(
                hmax_data,
                dims=["timemax", "y", "x"],
                attrs={"units": "m"},
            ),
        },
        coords={
            "x": xr.DataArray(x_vals, dims=["x"]),
            "y": xr.DataArray(y_vals, dims=["y"]),
        },
        attrs={"crs": "EPSG:4326"},
    )

    netcdf_path = tmp_path / "sfincs_map.nc"
    ds.to_netcdf(str(netcdf_path))
    ds.close()

    # Should not raise (the pre-fix code raised COG_WRITE_FAILED here).
    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)

    try:
        # Verify the COG is a valid single-band 2D raster.
        with rasterio.open(str(cog_path)) as src:
            assert src.count == 1, (
                f"OQ-58 regression: COG has {src.count} band(s); expected 1"
            )
            assert src.width == m, (
                f"OQ-58: COG width {src.width} != expected {m}"
            )
            assert src.height == n, (
                f"OQ-58: COG height {src.height} != expected {n}"
            )
            band = src.read(1)
            assert band.shape == (n, m), (
                f"OQ-58: band shape {band.shape} != expected ({n}, {m})"
            )

        # Metrics must reflect the flooded cells.
        assert metrics["max_depth_m"] > 0.0, "max_depth_m should be > 0 (some cells flooded)"
        assert metrics["flooded_cell_count"] > 0, "flooded_cell_count should be > 0"
        assert metrics["units"] == "meters"
    finally:
        cog_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Test 21 — job-0060: run_model_flood_scenario returns LayerURI on success.
#
# Layer-emission contract (docs/decisions/layer-emission-contract.md,
# ADOPTED 2026-06-07): the atomic-tool wrapper must return LayerURI so the
# PipelineEmitter gate at pipeline_emitter.py:517 fires add_loaded_layer and
# populates session-state.loaded_layers. Returning a dict misses that branch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_model_flood_scenario_returns_layer_uri() -> None:
    """``run_model_flood_scenario`` returns ``LayerURI`` (not a dict) on success.

    Guards the layer-emission contract pin introduced by job-0060:
    ``docs/decisions/layer-emission-contract.md`` (ADOPTED 2026-06-07).
    The PipelineEmitter gate ``isinstance(result, LayerURI)`` at
    ``pipeline_emitter.py:517`` only fires when the tool returns a
    ``LayerURI`` instance — a dict return misses it entirely.
    """
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4,
        "mean_depth_m": 0.6,
        "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345,
        "crs": "EPSG:3857",
        "units": "meters",
    }

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        # sprint-14-aws: publish_layer must succeed for the workflow to return a
        # LayerURI. Mock it to a renderable tile URL (the AWS TiTiler form) — the
        # gs:// fixture uri is the publish INPUT, which this mock replaces. Without
        # this patch the real publish runs, the gs:// object doesn't exist, and the
        # layer is dropped (returns the dict envelope), failing the contract below.
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            return_value=(
                "https://d125yfbyjrpbre.cloudfront.net/cog/tiles/WebMercatorQuad/"
                "{z}/{x}/{y}.png?url=s3://trid3nt-runs/"
                + run_id
                + "/flood_depth_peak.tif&rescale=0,3"
            ),
        ),
    ):
        result = await run_model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
            return_period_yr=100,
            duration_hr=24,
            compute_class="medium",
        )

    # The critical assertion: LayerURI (not dict) so PipelineEmitter fires.
    assert isinstance(result, LayerURI), (
        f"job-0060 layer-emission contract: run_model_flood_scenario must return "
        f"LayerURI on success (not {type(result).__name__!r}). The PipelineEmitter "
        f"gate at pipeline_emitter.py:517 only fires add_loaded_layer when "
        f"isinstance(result, LayerURI) is True. See "
        f"docs/decisions/layer-emission-contract.md (ADOPTED 2026-06-07)."
    )
    # After job-0062, publish_layer substitutes the WMS URL into LayerURI.uri;
    # the gs:// URI is no longer returned directly (see test 28 for the same
    # assertion with an explicit WMS URL mock — test corrected by job-0071).
    assert result.uri.startswith("https://"), (
        f"job-0062: run_model_flood_scenario must return a WMS URL in LayerURI.uri "
        f"(publish_layer substitutes the WMS URL); got {result.uri!r}"
    )
    assert result.style_preset == "continuous_flood_depth"
    assert result.role == "primary"
    assert result.layer_type == "raster"
    assert result.units == "meters"


# --------------------------------------------------------------------------- #
# Test 22 — job-0060: PipelineEmitter.add_loaded_layer is invoked when
# run_model_flood_scenario returns a LayerURI and emit_tool_call wraps it.
#
# This guards the full emission chain: tool returns LayerURI →
# emit_tool_call's isinstance gate fires → add_loaded_layer appends to
# _loaded_layers → emit_session_state emits a session-state envelope with
# non-empty loaded_layers.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_model_flood_scenario_triggers_loaded_layers_emit() -> None:
    """``add_loaded_layer`` is called and ``loaded_layers`` is populated on success.

    Verifies the full emission chain mandated by the layer-emission contract
    (``docs/decisions/layer-emission-contract.md``, ADOPTED 2026-06-07):

        run_model_flood_scenario → LayerURI
            → PipelineEmitter.emit_tool_call isinstance gate (pipeline_emitter.py:517)
            → add_loaded_layer (pipeline_emitter.py:413)
            → session-state envelope with non-empty loaded_layers

    The test mocks ``PipelineEmitter.add_loaded_layer`` directly and asserts
    it is called exactly once with the COG LayerURI.  It then asserts that
    the emitter's ``_loaded_layers`` list would be non-empty with the correct
    URI (simulating what ``emit_session_state`` would serialise).
    """
    import json as _json

    from trid3nt_server.pipeline_emitter import PipelineEmitter

    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    expected_cog_uri = f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif"
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=expected_cog_uri,
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4,
        "mean_depth_m": 0.6,
        "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345,
        "crs": "EPSG:3857",
        "units": "meters",
    }

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    # Build a minimal PipelineEmitter with a capturing sink.
    # The sink receives JSON-serialised Envelope strings (as server.py does).
    captured_frames: list[dict[str, Any]] = []
    test_session_id = new_ulid()

    async def _sink(json_str: str) -> None:
        captured_frames.append(_json.loads(json_str))

    emitter = PipelineEmitter(session_id=test_session_id, sink=_sink)

    # Spy on add_loaded_layer — capture calls while still executing the real
    # implementation so _loaded_layers is actually populated.
    add_loaded_layer_calls: list[LayerURI] = []
    original_add_loaded_layer = emitter.add_loaded_layer

    async def _spy_add_loaded_layer(layer: LayerURI) -> None:
        add_loaded_layer_calls.append(layer)
        await original_add_loaded_layer(layer)

    emitter.add_loaded_layer = _spy_add_loaded_layer  # type: ignore[method-assign]

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        # sprint-14-aws: publish_layer must succeed so the workflow returns a
        # LayerURI and the emitter fires add_loaded_layer (the assertion below).
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            return_value=(
                "https://d125yfbyjrpbre.cloudfront.net/cog/tiles/WebMercatorQuad/"
                "{z}/{x}/{y}.png?url=s3://trid3nt-runs/flood_depth_peak.tif&rescale=0,3"
            ),
        ),
    ):
        result = await emitter.emit_tool_call(
            name="M5 flood scenario",
            tool_name="run_model_flood_scenario",
            invoke=lambda: run_model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
                return_period_yr=100,
                duration_hr=24,
                compute_class="medium",
            ),
        )

    # 1. add_loaded_layer must have been called exactly once FOR THE RESULT.
    # task #207: engine INPUT layers (DEM/landcover, layer_id prefix "input-")
    # ALSO surface via add_loaded_layer now; this assertion is about the RESULT
    # wrapper gate, so filter the inputs out.
    result_layer_calls = [
        l for l in add_loaded_layer_calls if not l.layer_id.startswith("input-")
    ]
    assert len(result_layer_calls) == 1, (
        f"job-0060: add_loaded_layer must be called once for the result on "
        f"success; called {len(result_layer_calls)} time(s) (excluding inputs). "
        f"The emit_tool_call gate at pipeline_emitter.py:517 fires only when "
        f"isinstance(result, LayerURI) is True."
    )
    # After job-0062, publish_layer substitutes the WMS URL into LayerURI.uri.
    # Assert the URI is a WMS URL (not gs://) — corrected by job-0071.
    assert result_layer_calls[0].uri.startswith("https://"), (
        f"add_loaded_layer called with wrong URI (expected WMS URL after job-0062): "
        f"{result_layer_calls[0].uri!r}"
    )

    # 2. The emitter's _loaded_layers list carries the RESULT after the call.
    # task #207: input rows (layer_id prefix "input-") may also be present; the
    # RESULT-layer contract is one non-input row carrying the WMS URL.
    result_loaded = [
        l for l in emitter._loaded_layers if not l.layer_id.startswith("input-")
    ]
    assert len(result_loaded) == 1, (
        f"_loaded_layers should have 1 RESULT entry after the successful run; "
        f"got {len(result_loaded)} (excluding inputs)."
    )
    assert result_loaded[0].uri.startswith("https://"), (
        f"result _loaded_layers uri should be WMS URL after job-0062; "
        f"got {result_loaded[0].uri!r}"
    )

    # 3. A session-state envelope was emitted with non-empty loaded_layers.
    # Frames are JSON-parsed wire envelopes: {"type": "session-state", "payload": {...}}
    session_state_frames = [f for f in captured_frames if f.get("type") == "session-state"]
    assert session_state_frames, (
        "No session-state envelope was emitted; emit_session_state should have "
        "been called by add_loaded_layer."
    )
    last_payload = session_state_frames[-1]["payload"]
    loaded_layers_wire = last_payload.get("loaded_layers", [])
    assert loaded_layers_wire, (
        "session-state.loaded_layers is empty; the COG LayerURI was not "
        "serialised into the session-state envelope."
    )
    # loaded_layers is a list of dicts (model_dump output from emit_session_state).
    first_layer = loaded_layers_wire[0]
    assert first_layer.get("uri", "").startswith("https://"), (
        f"session-state.loaded_layers[0].uri should be WMS URL after job-0062; "
        f"got {first_layer.get('uri')!r}"
    )


# =========================================================================== #
# Tests 22-24 — job-0063 OQ-59: postprocess_flood reads CRS from the dataset's
# 'crs' data variable (CF-convention) instead of ds.attrs.
#
# SFINCS stores its CRS in a data variable named 'crs', not in .attrs.  Before
# the fix, ds.attrs.get("crs", "EPSG:3857") always returned the fallback, so
# the COG was tagged EPSG:3857 while its pixel coordinates were in UTM 17N
# (EPSG:32617) — a ~10 000 km geolocation error in any CRS-aware GIS client.
#
# Test 22: crs variable with epsg_code="EPSG:32617" → COG tagged EPSG:32617
# Test 23: crs variable with spatial_ref=<UTM 17N WKT> → COG tagged EPSG:32617
# Test 24: no crs variable; ds.attrs["crs"]="EPSG:3857" → EPSG:3857 (fallback)
# =========================================================================== #


def _build_synthetic_sfincs_nc_oq59(
    tmp_path: Path,
    *,
    crs_var_attrs: "dict | None" = None,
    ds_attrs: "dict | None" = None,
    filename: str = "sfincs_map.nc",
) -> Path:
    """Write a minimal sfincs_map.nc with hmax (1, 8, 8) to tmp_path.

    If ``crs_var_attrs`` is provided a ``crs`` data variable is added carrying
    those attributes (mimicking SFINCS CF encoding).  ``ds_attrs`` go on the
    Dataset itself.
    """
    import numpy as np
    import xarray as xr

    n, m = 8, 8
    rng = np.random.default_rng(99)
    hmax_data = rng.uniform(0.1, 2.0, (1, n, m)).astype("float32")

    # Plausible UTM 17N easting/northing coords for Fort Myers FL
    x_vals = np.linspace(409000.0, 425000.0, m, dtype="float64")
    y_vals = np.linspace(2937000.0, 2952000.0, n, dtype="float64")

    data_vars: dict = {
        "hmax": xr.DataArray(
            hmax_data,
            dims=["timemax", "y", "x"],
            attrs={"units": "m"},
        ),
    }
    if crs_var_attrs is not None:
        data_vars["crs"] = xr.DataArray(np.int32(32617), attrs=crs_var_attrs)

    ds = xr.Dataset(
        data_vars,
        coords={
            "x": xr.DataArray(x_vals, dims=["x"]),
            "y": xr.DataArray(y_vals, dims=["y"]),
        },
        attrs=ds_attrs or {},
    )
    path = tmp_path / filename
    ds.to_netcdf(str(path))
    ds.close()
    return path


def test_extract_peak_depth_geotiff_reads_crs_from_epsg_code_var(
    tmp_path: Path,
) -> None:
    """OQ-59 Test 22: crs variable with epsg_code attr → COG tagged EPSG:32617.

    SFINCS emits ``ds['crs'].attrs['epsg_code'] = 'EPSG:32617'`` (string with
    prefix).  Before the OQ-59 fix, ds.attrs.get("crs", "EPSG:3857") fired the
    fallback and tagged the COG EPSG:3857 while coordinates were UTM 17N.
    After the fix the CRS tag matches the dataset's crs variable.
    """
    try:
        import xarray as xr  # noqa: F401
    except ImportError:
        pytest.skip("xarray not installed")
    try:
        import rasterio
    except ImportError:
        pytest.skip("rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff

    netcdf_path = _build_synthetic_sfincs_nc_oq59(
        tmp_path,
        crs_var_attrs={"EPSG": "-", "epsg_code": "EPSG:32617"},
    )

    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    try:
        with rasterio.open(str(cog_path)) as src:
            assert src.crs is not None, "OQ-59: COG has no CRS tag"
            assert "32617" in src.crs.to_string(), (
                f"OQ-59 (epsg_code): expected EPSG:32617 in CRS string, "
                f"got {src.crs.to_string()!r}"
            )
        assert metrics["crs"] == "EPSG:32617", (
            f"OQ-59: metrics['crs'] should be 'EPSG:32617', got {metrics['crs']!r}"
        )
    finally:
        cog_path.unlink(missing_ok=True)


def test_extract_peak_depth_geotiff_reads_crs_from_spatial_ref_wkt(
    tmp_path: Path,
) -> None:
    """OQ-59 Test 23: crs variable with spatial_ref WKT → COG tagged EPSG:32617.

    Some SFINCS / GDAL variants write a WKT string under the ``spatial_ref``
    attr rather than an EPSG code.  The fix resolves via
    ``pyproj.CRS.from_wkt`` and returns the authority string.  pyproj is a
    rasterio dependency so it is always present when rasterio is installed.
    """
    try:
        import xarray as xr  # noqa: F401
    except ImportError:
        pytest.skip("xarray not installed")
    try:
        import rasterio
        import pyproj
    except ImportError:
        pytest.skip("rasterio/pyproj not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff

    utm17n_wkt = pyproj.CRS.from_epsg(32617).to_wkt()
    netcdf_path = _build_synthetic_sfincs_nc_oq59(
        tmp_path,
        crs_var_attrs={"spatial_ref": utm17n_wkt},
        filename="sfincs_map_wkt.nc",
    )

    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    try:
        with rasterio.open(str(cog_path)) as src:
            assert src.crs is not None, "OQ-59 (spatial_ref): COG has no CRS tag"
            assert src.crs.to_epsg() == 32617, (
                f"OQ-59 (spatial_ref WKT): expected EPSG 32617, got {src.crs}"
            )
    finally:
        cog_path.unlink(missing_ok=True)


def test_extract_peak_depth_geotiff_falls_back_to_attrs_crs_when_no_var(
    tmp_path: Path,
) -> None:
    """OQ-59 Test 24: no crs variable; ds.attrs['crs']='EPSG:3857' → EPSG:3857.

    Backward-compat guard: datasets that store CRS in .attrs (old encoding)
    must still produce a correctly tagged COG.  Exercises the fallback branch
    of ``_read_crs_from_dataset``.
    """
    try:
        import xarray as xr  # noqa: F401
    except ImportError:
        pytest.skip("xarray not installed")
    try:
        import rasterio
    except ImportError:
        pytest.skip("rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff

    # No crs_var_attrs → no 'crs' variable in the dataset; fall back to .attrs.
    netcdf_path = _build_synthetic_sfincs_nc_oq59(
        tmp_path,
        crs_var_attrs=None,
        ds_attrs={"crs": "EPSG:3857"},
        filename="sfincs_map_attrs.nc",
    )

    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    try:
        with rasterio.open(str(cog_path)) as src:
            assert src.crs is not None, "OQ-59 fallback: COG has no CRS tag"
            assert src.crs.to_epsg() == 3857, (
                f"OQ-59 fallback: expected EPSG:3857, got {src.crs}"
            )
        assert metrics["crs"] == "EPSG:3857", (
            f"OQ-59 fallback: metrics['crs'] should be 'EPSG:3857', "
            f"got {metrics['crs']!r}"
        )
    finally:
        cog_path.unlink(missing_ok=True)


# =========================================================================== #
# Tests 25-28 — job-0062: publish_layer integration into model_flood_scenario
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Test 25 — model_flood_scenario calls publish_layer after postprocess_flood
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_model_flood_scenario_calls_publish_layer_after_postprocess() -> None:
    """``publish_layer`` is called exactly once after ``postprocess_flood`` succeeds.

    Guards the Step 9 integration point in ``model_flood_scenario``
    (job-0062): after ``postprocess_flood`` returns the flood-depth COG
    ``LayerURI``, ``publish_layer`` is invoked to add the COG to the ``.qgs``
    project so QGIS Server can serve it as WMS.
    """
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4, "mean_depth_m": 0.6, "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345, "crs": "EPSG:32617", "units": "meters",
    }
    expected_wms_url = (
        "https://qgis.test.example.com/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs"
        f"&LAYERS=flood-depth-peak-{run_id}"
    )

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    publish_layer_calls: list[dict] = []

    def _mock_publish_layer(layer_uri, layer_id, style_preset, **kwargs):
        publish_layer_calls.append({
            "layer_uri": layer_uri,
            "layer_id": layer_id,
            "style_preset": style_preset,
        })
        return expected_wms_url

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            side_effect=_mock_publish_layer,
        ),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
            return_period_yr=100,
            duration_hr=24,
            compute_class="medium",
        )

    # publish_layer must have been called once for the primary raster layer.
    assert len(publish_layer_calls) == 1, (
        f"job-0062: publish_layer must be called once for the primary raster; "
        f"called {len(publish_layer_calls)} time(s)"
    )
    call = publish_layer_calls[0]
    assert call["layer_uri"] == f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif"
    assert call["layer_id"] == f"flood-depth-peak-{run_id}"
    assert call["style_preset"] == "continuous_flood_depth"


# --------------------------------------------------------------------------- #
# Test 26 — LayerURI returned by workflow carries WMS URL (not gs://)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_model_flood_scenario_layer_uri_carries_wms_url() -> None:
    """The ``LayerURI`` returned from ``model_flood_scenario`` carries the WMS URL.

    After job-0062's publish_layer integration, the primary layer's ``uri``
    is the WMS URL returned by ``publish_layer``, not the gs:// COG URI.
    This ensures the client gets a renderable URL directly from
    ``session-state.loaded_layers``.

    Guards OQ-62-LAYERURI-URI-FIELD: ``LayerURI.uri`` is substituted with
    the WMS URL because the contract has no validator rejecting non-gs:// values.
    """
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4, "mean_depth_m": 0.6, "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345, "crs": "EPSG:32617", "units": "meters",
    }
    expected_wms_url = (
        "https://legacy-qgis-server.example.com/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs"
        f"&LAYERS=flood-depth-peak-{run_id}"
    )

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            return_value=expected_wms_url,
        ),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )

    assert isinstance(envelope, AssessmentEnvelope)
    assert len(envelope.layers) == 1, (
        f"Expected 1 layer; got {len(envelope.layers)}"
    )
    primary_layer = envelope.layers[0]
    assert primary_layer.uri == expected_wms_url, (
        f"job-0062 OQ-62-LAYERURI-URI-FIELD: LayerURI.uri must be the WMS URL "
        f"after publish_layer succeeds; got {primary_layer.uri!r}. "
        f"The client uses this URI to render the layer in MapLibre."
    )
    # Other fields should be preserved.
    assert primary_layer.style_preset == "continuous_flood_depth"
    assert primary_layer.role == "primary"
    assert primary_layer.layer_type == "raster"


# --------------------------------------------------------------------------- #
# Test 27 — publish_layer failure DROPS the layer (job-0254 §1, Decision 11)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_model_flood_scenario_publish_layer_failure_drops_layer() -> None:
    """When ``publish_layer`` raises ``PublishLayerError``, the primary
    flood-depth layer is DROPPED — NOT fallen back to its raw gs:// uri
    (job-0254 §1, Decision 11).

    A raw gs:// uri never renders (MapLibre cannot fetch gs://); emitting it
    only paints a broken layer row. The publish step stays non-fatal: the
    envelope is still a SUCCESS envelope carrying the depth metrics and
    provenance (so narration is truthful and the job-0177 retry-on-failure
    loop can act), but it carries ZERO renderable layers — the gs:// COG is
    kept off the map.

    (Supersedes the prior "falls back to gs://" contract, which codified the
    leak this job closes.)
    """
    from trid3nt_server.tools.publish_layer import PublishLayerError

    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    cog_uri = f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif"
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=cog_uri,
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4, "mean_depth_m": 0.6, "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345, "crs": "EPSG:32617", "units": "meters",
    }

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            side_effect=PublishLayerError(
                "WORKER_JOB_FAILED",
                "pyqgis worker execution reached FAILED state (no runs bucket grant)",
            ),
        ),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )

    # Envelope is still built (not a failed envelope) — publish_layer failure is non-fatal.
    assert isinstance(envelope, AssessmentEnvelope)
    # job-0254 §1: the primary raster is DROPPED (not gs://-fallback). No
    # renderable layer reaches the client; the broken gs:// row is gone.
    assert len(envelope.layers) == 0, (
        f"publish_layer failure must DROP the primary raster, not fall back to "
        f"its gs:// uri; envelope still carries {len(envelope.layers)} layer(s): "
        f"{[lyr.uri for lyr in envelope.layers]}"
    )
    # Crucially, the dropped layer's raw gs:// uri must NOT appear anywhere in
    # the emitted layers (the leak is closed).
    assert not any(lyr.uri == cog_uri for lyr in envelope.layers)
    # The envelope is still a success envelope (not failed) — metrics survive,
    # so narration stays truthful and the retry loop has a real result to act on.
    assert envelope.flood is not None
    assert not envelope.flood.metrics.solver_version.startswith("failed:"), (
        "publish_layer failure must NOT produce a failed envelope; "
        "it remains a success envelope whose metrics survive — only the "
        "non-renderable gs:// layer is dropped"
    )
    assert envelope.flood.metrics.max_depth_m == 2.4  # metrics intact


@pytest.mark.asyncio
async def test_wrapper_publish_failure_returns_truthful_dict_not_layer_uri() -> None:
    """job-0254 §1 wrapper contract: when publish fails and the envelope ends
    up with zero layers, the LLM-facing ``run_model_flood_scenario`` wrapper
    returns the envelope DICT (not a ``LayerURI``).

    Consequences (the retry contract, job-0177):
      * No ``LayerURI`` return → the ``emit_tool_call`` isinstance gate never
        fires ``add_loaded_layer`` → no renderable raw gs:// reaches the client.
      * The dict carries the depth metrics + provenance so the agent narrates
        the publish failure honestly and can retry.
    """
    from trid3nt_server.tools.publish_layer import PublishLayerError

    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1, "units": "inches", "location": [26.6, -81.9],
        "return_period_years": 100, "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States", "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(), solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0, bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021}, created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id, handle_id=handle.handle_id, status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc), duration_seconds=120.0,
    )
    cog_uri = f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif"
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}", name="Flood Depth (peak)",
        layer_type="raster", uri=cog_uri,
        style_preset="continuous_flood_depth", role="primary", units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4, "mean_depth_m": 0.6, "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345, "crs": "EPSG:32617", "units": "meters",
    }

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            side_effect=PublishLayerError(
                "WORKER_JOB_FAILED",
                "pyqgis worker execution reached FAILED state (no runs bucket grant)",
            ),
        ),
    ):
        result = await run_model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )

    # No LayerURI → no add_loaded_layer → no renderable gs:// reaches the client.
    assert not isinstance(result, LayerURI), (
        f"publish failure must NOT yield a LayerURI (would leak gs://); "
        f"got {type(result).__name__}"
    )
    assert isinstance(result, dict)
    # The dict is the serialized envelope: metrics present, no layers, hazard flood.
    assert result.get("hazard_type") == "flood"
    assert result.get("layers") == []
    # The raw gs:// COG uri must not appear anywhere in the LLM-visible result.
    assert cog_uri not in json.dumps(result)


# --------------------------------------------------------------------------- #
# Test 28 — run_model_flood_scenario wrapper returns WMS URL via LayerURI.uri
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_model_flood_scenario_wrapper_uri_is_wms_url() -> None:
    """The thin wrapper's returned ``LayerURI.uri`` is the WMS URL after job-0062.

    The atomic-tool wrapper ``run_model_flood_scenario`` returns
    ``LayerURI(uri=primary.uri)``; after job-0062's integration,
    ``primary.uri`` is the WMS URL returned by ``publish_layer``.
    This guards the client contract: ``session-state.loaded_layers[0].uri``
    is the WMS URL, not a gs:// URI.
    """
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9 Version 2",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 2.4, "mean_depth_m": 0.6, "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345, "crs": "EPSG:32617", "units": "meters",
    }
    expected_wms_url = (
        "https://legacy-qgis-server.example.com/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs"
        f"&LAYERS=flood-depth-peak-{run_id}"
    )

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            return_value=expected_wms_url,
        ),
    ):
        result = await run_model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
        )

    assert isinstance(result, LayerURI), (
        f"run_model_flood_scenario must return LayerURI; got {type(result).__name__!r}"
    )
    assert result.uri == expected_wms_url, (
        f"job-0062: LayerURI.uri must be the WMS URL after publish_layer "
        f"integration; got {result.uri!r}. "
        f"The client needs a WMS URL in session-state.loaded_layers[0].uri "
        f"to render via MapLibre."
    )


# =========================================================================== #
# Tests 29-34 — job-0071: rotation fix + transparency belt-and-suspenders
# + CRS_TAG_MISMATCH guard
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Test 29 — Rotation fix: hmax with transposed axes (m, n) → north-up COG
#
# SFINCS netCDF convention: ds["x"].dims = ("m",), ds["y"].dims = ("n",) where
# m=x-cols and n=y-rows.  HydroMT-SFINCS 1.2.2 Fort Myers run emitted hmax with
# dims (timemax, m, n) instead of (timemax, n, m).  After squeeze this gives
# arr.shape = (m, n) — rows and cols are swapped.  The rotation fix detects
# this (arr.shape[-1] == len(y) AND arr.shape[-2] == len(x)) and transposes.
#
# Verified transforms:
#   transform.a > 0 → positive pixel width (west→east, correct for geographic x)
#   transform.e < 0 → negative pixel height (north→south, north-up COG)
# --------------------------------------------------------------------------- #


def test_extract_peak_depth_geotiff_rotation_fix_transposed_axes(
    tmp_path: Path,
) -> None:
    """Rotation fix (job-0071): hmax with dims (timemax, m, n) → north-up COG.

    Regression guard for the 90° CW rotation observed in the job-0070 Fort Myers
    screenshot.  The SFINCS grid had hmax dims (timemax, m, n) — x-cols in the
    leading spatial axis — so after squeeze arr.shape = (m, n) where m > n
    (landscape grid).  The pre-fix code computed from_bounds with
    width=arr.shape[-1]=n (wrong: should be m) and height=arr.shape[-2]=m
    (wrong: should be n), producing a rotated raster.

    The fix checks: if arr.shape[-1] == len(y) AND arr.shape[-2] == len(x),
    transpose before writing.
    """
    try:
        import numpy as np
        import xarray as xr
        import rasterio
    except ImportError:
        pytest.skip("numpy/xarray/rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff

    # Use a non-square grid (m=12 cols, n=8 rows) so transposition is detectable.
    n, m = 8, 12  # n=y-rows, m=x-cols
    # SFINCS transposed convention: hmax dims = (timemax, m, n)
    rng = np.random.default_rng(42)
    hmax_data = rng.uniform(0.5, 3.0, (1, m, n)).astype("float32")  # (timemax, m, n) — transposed!

    # Plausible UTM 17N coords for Fort Myers
    x_vals = np.linspace(409000.0, 425000.0, m, dtype="float64")  # m x-coords
    y_vals = np.linspace(2937000.0, 2952000.0, n, dtype="float64")  # n y-coords

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(hmax_data, dims=["timemax", "m", "n"], attrs={"units": "m"}),
            "crs": xr.DataArray(
                __import__("numpy").int32(32617),
                attrs={"epsg_code": "EPSG:32617"},
            ),
        },
        coords={
            "x": xr.DataArray(x_vals, dims=["m"]),  # x varies over m (cols)
            "y": xr.DataArray(y_vals, dims=["n"]),  # y varies over n (rows)
        },
    )
    netcdf_path = tmp_path / "sfincs_map_transposed.nc"
    ds.to_netcdf(str(netcdf_path))
    ds.close()

    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    try:
        with rasterio.open(str(cog_path)) as src:
            # After the fix the COG must be (n rows, m cols) = (8, 12).
            assert src.height == n, (
                f"job-0071 rotation fix: COG height={src.height} should be n={n} "
                f"(y-rows). Rotation produces height=m={m} — swap detected."
            )
            assert src.width == m, (
                f"job-0071 rotation fix: COG width={src.width} should be m={m} "
                f"(x-cols). Rotation produces width=n={n} — swap detected."
            )
            # North-up transform: a > 0 (E-W positive pixel width),
            # e < 0 (N-S negative pixel height).
            assert src.transform.a > 0, (
                f"job-0071: transform.a={src.transform.a} should be > 0 "
                f"(positive E-W pixel width for a north-up COG)"
            )
            assert src.transform.e < 0, (
                f"job-0071: transform.e={src.transform.e} should be < 0 "
                f"(negative N-S pixel height for a north-up COG)"
            )
    finally:
        cog_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Test 30 — Rotation: correct axis order (n, m) → no transpose, still north-up
# --------------------------------------------------------------------------- #


def test_extract_peak_depth_geotiff_rotation_correct_axis_order(
    tmp_path: Path,
) -> None:
    """Rotation fix (job-0071): correct axis order (timemax, n, m) → north-up, no transpose.

    When hmax dims are already (timemax, n, m) — the correct SFINCS convention
    with n=y-rows, m=x-cols — the code must NOT transpose (identity path).
    The resulting COG must still be north-up: transform.a > 0, transform.e < 0.
    """
    try:
        import numpy as np
        import xarray as xr
        import rasterio
    except ImportError:
        pytest.skip("numpy/xarray/rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff

    n, m = 8, 12  # n=y-rows, m=x-cols
    rng = np.random.default_rng(7)
    hmax_data = rng.uniform(0.5, 3.0, (1, n, m)).astype("float32")  # (timemax, n, m) — correct

    x_vals = np.linspace(409000.0, 425000.0, m, dtype="float64")
    y_vals = np.linspace(2937000.0, 2952000.0, n, dtype="float64")

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(hmax_data, dims=["timemax", "n", "m"], attrs={"units": "m"}),
            "crs": xr.DataArray(
                __import__("numpy").int32(32617),
                attrs={"epsg_code": "EPSG:32617"},
            ),
        },
        coords={
            "x": xr.DataArray(x_vals, dims=["m"]),
            "y": xr.DataArray(y_vals, dims=["n"]),
        },
    )
    netcdf_path = tmp_path / "sfincs_map_correct.nc"
    ds.to_netcdf(str(netcdf_path))
    ds.close()

    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    try:
        with rasterio.open(str(cog_path)) as src:
            assert src.height == n, (
                f"correct axis order: COG height={src.height} should be n={n}"
            )
            assert src.width == m, (
                f"correct axis order: COG width={src.width} should be m={m}"
            )
            assert src.transform.a > 0, (
                f"job-0071: transform.a={src.transform.a} must be > 0 (north-up COG)"
            )
            assert src.transform.e < 0, (
                f"job-0071: transform.e={src.transform.e} must be < 0 (north-up COG)"
            )
    finally:
        cog_path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Test 31 — Transparency data-side: sub-threshold depths masked to NaN
# --------------------------------------------------------------------------- #


def test_extract_peak_depth_geotiff_transparency_threshold(
    tmp_path: Path,
) -> None:
    """Transparency belt-and-suspenders (job-0071): depth < 0.05 m → NaN in COG.

    NODATA_DEPTH_M = 0.05 m. Values [0.0, 0.03, 0.10, 1.5] → NaN exactly where
    depth < 0.05.  The COG carries no sub-threshold values so the renderer's
    alpha=0 stop is redundant (belt-and-suspenders, not the only guard).
    """
    try:
        import numpy as np
        import xarray as xr
        import rasterio
    except ImportError:
        pytest.skip("numpy/xarray/rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import (
        _extract_peak_depth_geotiff,
        NODATA_DEPTH_M,
    )

    assert NODATA_DEPTH_M == pytest.approx(0.05), (
        f"NODATA_DEPTH_M constant changed from 0.05 to {NODATA_DEPTH_M!r}; "
        "update this test and the QML bottom stop."
    )

    # 2×2 grid with the four canonical depth values: 0.0, 0.03, 0.10, 1.50 m.
    # Expected mask: 0.0 → NaN, 0.03 → NaN, 0.10 → 0.10, 1.50 → 1.50.
    hmax_data = np.array([[0.0, 0.03], [0.10, 1.50]], dtype="float32").reshape(1, 2, 2)

    x_vals = np.array([409000.0, 410000.0], dtype="float64")  # 2 x-coords
    y_vals = np.array([2937000.0, 2938000.0], dtype="float64")  # 2 y-coords

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(hmax_data, dims=["timemax", "n", "m"], attrs={"units": "m"}),
            "crs": xr.DataArray(
                np.int32(32617),
                attrs={"epsg_code": "EPSG:32617"},
            ),
        },
        coords={
            "x": xr.DataArray(x_vals, dims=["m"]),
            "y": xr.DataArray(y_vals, dims=["n"]),
        },
    )
    netcdf_path = tmp_path / "sfincs_map_threshold.nc"
    ds.to_netcdf(str(netcdf_path))
    ds.close()

    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    try:
        with rasterio.open(str(cog_path)) as src:
            band = src.read(1)
            # Two cells are below threshold (0.0 m and 0.03 m < 0.05 m = NODATA_DEPTH_M)
            # → NaN in the COG.  Two cells above threshold (0.10 m and 1.50 m) → preserved.
            # We count NaNs rather than asserting specific pixel positions because
            # rasterio's north-up convention may reorder rows during COG write.
            nan_count = int(np.sum(np.isnan(band)))
            assert nan_count == 2, (
                f"job-0071 transparency: expected 2 NaN cells (depth < {NODATA_DEPTH_M} m); "
                f"got {nan_count}. Cells: {band.tolist()}"
            )
            # The two surviving values must be exactly 0.10 and 1.50 m.
            valid_vals = sorted(band[~np.isnan(band)].tolist())
            assert len(valid_vals) == 2, (
                f"job-0071: expected 2 valid depth cells; got {valid_vals}"
            )
            assert valid_vals[0] == pytest.approx(0.10, abs=1e-4), (
                f"job-0071: lower valid depth should be ≈0.10 m; got {valid_vals[0]!r}"
            )
            assert valid_vals[1] == pytest.approx(1.50, abs=1e-4), (
                f"job-0071: upper valid depth should be ≈1.50 m; got {valid_vals[1]!r}"
            )
    finally:
        cog_path.unlink(missing_ok=True)

    # flooded_cell_count must reflect only the above-threshold cells.
    assert metrics["flooded_cell_count"] == 2, (
        f"job-0071: flooded_cell_count should be 2 (0.10 m + 1.50 m); "
        f"got {metrics['flooded_cell_count']}"
    )


# --------------------------------------------------------------------------- #
# Tests 32-34 — CRS_TAG_MISMATCH guard (job-0071)
# --------------------------------------------------------------------------- #


def _build_netcdf_for_crs_guard(
    tmp_path: Path,
    *,
    epsg_code: str,
    x_min: float,
    x_max: float,
    y_min: float,
    y_max: float,
    filename: str = "sfincs_map.nc",
) -> Path:
    """Write a minimal sfincs_map.nc for CRS guard tests."""
    import numpy as np
    import xarray as xr

    n, m = 4, 4
    hmax_data = np.full((1, n, m), 1.0, dtype="float32")
    x_vals = np.linspace(x_min, x_max, m, dtype="float64")
    y_vals = np.linspace(y_min, y_max, n, dtype="float64")

    ds = xr.Dataset(
        {
            "hmax": xr.DataArray(hmax_data, dims=["timemax", "n", "m"], attrs={"units": "m"}),
            "crs": xr.DataArray(
                np.int32(int(epsg_code.split(":")[-1])),
                attrs={"epsg_code": epsg_code},
            ),
        },
        coords={
            "x": xr.DataArray(x_vals, dims=["m"]),
            "y": xr.DataArray(y_vals, dims=["n"]),
        },
    )
    path = tmp_path / filename
    ds.to_netcdf(str(path))
    ds.close()
    return path


def test_crs_tag_mismatch_guard_correct_case_no_raise(tmp_path: Path) -> None:
    """CRS_TAG_MISMATCH guard (job-0071): correct projected CRS + projected coords → no raise.

    UTM 17N (EPSG:32617) with easting/northing coords around Fort Myers
    (|x| > 1000) must pass both guard checks and produce a valid COG.
    """
    try:
        import xarray as xr  # noqa: F401
        import rasterio  # noqa: F401
    except ImportError:
        pytest.skip("xarray/rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff, PostprocessError

    netcdf_path = _build_netcdf_for_crs_guard(
        tmp_path,
        epsg_code="EPSG:32617",
        x_min=409000.0, x_max=425000.0,
        y_min=2937000.0, y_max=2952000.0,
        filename="sfincs_map_correct_crs.nc",
    )

    # Must NOT raise PostprocessError(CRS_TAG_MISMATCH)
    cog_path, metrics = _extract_peak_depth_geotiff(netcdf_path)
    cog_path.unlink(missing_ok=True)
    assert metrics["crs"] == "EPSG:32617"


def test_crs_tag_mismatch_guard_geographic_tag_projected_coords(tmp_path: Path) -> None:
    """CRS_TAG_MISMATCH guard (job-0071): geographic-tag with projected coords → raises.

    EPSG:4326 is geographic (|x| ≤ 180 for valid lon), but the dataset's x
    coordinates are UTM eastings (~409 000 m, i.e. |x| >> 360).  The guard
    detects this inconsistency and raises PostprocessError("CRS_TAG_MISMATCH")
    before the mistagged COG is uploaded.
    """
    try:
        import xarray as xr  # noqa: F401
        import rasterio  # noqa: F401
    except ImportError:
        pytest.skip("xarray/rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff, PostprocessError

    netcdf_path = _build_netcdf_for_crs_guard(
        tmp_path,
        epsg_code="EPSG:4326",        # geographic tag
        x_min=409000.0, x_max=425000.0,  # projected coords — mismatch!
        y_min=2937000.0, y_max=2952000.0,
        filename="sfincs_map_geo_tag_proj_coords.nc",
    )

    with pytest.raises(PostprocessError) as excinfo:
        _extract_peak_depth_geotiff(netcdf_path)

    assert excinfo.value.error_code == "CRS_TAG_MISMATCH", (
        f"job-0071 CRS guard: geographic-tag + projected-coords must raise "
        f"CRS_TAG_MISMATCH; got error_code={excinfo.value.error_code!r}"
    )


def test_crs_tag_mismatch_guard_projected_tag_geographic_coords(tmp_path: Path) -> None:
    """CRS_TAG_MISMATCH guard (job-0071): projected-tag with geographic coords → raises.

    EPSG:32617 is a projected CRS (coords in metres, |x| > 1000 for any
    non-degenerate CONUS extent), but the dataset's x coordinates are lon
    values in the range -81.92 to -81.80 (|x| << 1000).  The guard detects
    this inconsistency and raises PostprocessError("CRS_TAG_MISMATCH").
    """
    try:
        import xarray as xr  # noqa: F401
        import rasterio  # noqa: F401
    except ImportError:
        pytest.skip("xarray/rasterio not installed")

    from trid3nt_server.workflows.postprocess_flood import _extract_peak_depth_geotiff, PostprocessError

    netcdf_path = _build_netcdf_for_crs_guard(
        tmp_path,
        epsg_code="EPSG:32617",          # projected tag
        x_min=-81.92, x_max=-81.80,      # geographic coords — mismatch!
        y_min=26.55,  y_max=26.68,
        filename="sfincs_map_proj_tag_geo_coords.nc",
    )

    with pytest.raises(PostprocessError) as excinfo:
        _extract_peak_depth_geotiff(netcdf_path)

    assert excinfo.value.error_code == "CRS_TAG_MISMATCH", (
        f"job-0071 CRS guard: projected-tag + geographic-coords must raise "
        f"CRS_TAG_MISMATCH; got error_code={excinfo.value.error_code!r}"
    )


# --------------------------------------------------------------------------- #
# job-0170 — rasterio /vsigs/ migration (eliminate gcsfs segfault)
#
# Codified lessons (from the kickoff): the agent crashed mid-run with
# ``SystemError: <cyfunction DatasetBase.stop> returned a result with an
# exception set`` from rasterio reading remote rasters via gcsfs (HydroMT's
# setup_manning_roughness → rioxarray.open_rasterio path). job-0170 replaces
# the gcsfs read path with rasterio's native /vsigs/ virtual filesystem.
#
# The four guards below cover:
#   1. ``_to_vsigs`` URI rewriting — pure helper, no I/O.
#   2. The YAML config plumbs ``/vsigs/`` (not ``gs://``) into HydroMT's
#      ``setup_dep`` + ``setup_manning_roughness`` blocks. This is the
#      load-bearing assertion — if it regresses, HydroMT picks up gcsfs
#      again and the segfault returns.
#   3. ``GDAL_NUM_THREADS=1`` set at module import (the second half of
#      the segfault fix — single-threaded GDAL avoids the cyfunction
#      destructor race even when ``/vsigs/`` is used).
#   4. ``_rasterio_open_with_retry`` retries transient failures.
# --------------------------------------------------------------------------- #


def test_to_vsigs_rewrites_gs_uri_to_vsigs_path() -> None:
    """``_to_vsigs('s3://bucket/key')`` → ``/vsis3/bucket/key`` (GCP decommissioned).

    Failure mode this guards against: regressing to passing remote URIs to
    HydroMT / rasterio in a way that dispatches the read through a fragile
    fsspec backend (job-0170 root cause). With GCP retired, the only remote
    scheme is ``s3://`` → GDAL ``/vsis3/``; the gs:// rewrite is gone (gs URIs
    pass through as local paths).
    """
    from trid3nt_server.workflows.sfincs_builder import _to_vsigs

    # The headline rewrite (s3 → /vsis3/).
    assert _to_vsigs("s3://trid3nt-cache/cache/static-30d/landcover/x.tif") == (
        "/vsis3/trid3nt-cache/cache/static-30d/landcover/x.tif"
    )

    # Idempotence — calling twice does not double-prefix.
    once = _to_vsigs("s3://bucket/key.tif")
    twice = _to_vsigs(once)
    assert twice == once == "/vsis3/bucket/key.tif"

    # gs:// is no longer special-cased — passes through unchanged.
    assert _to_vsigs("gs://bucket/key.tif") == "gs://bucket/key.tif"

    # ``file://`` prefix stripped — local fixtures still readable.
    assert _to_vsigs("file:///tmp/local.tif") == "/tmp/local.tif"

    # Bare local paths pass through unchanged.
    assert _to_vsigs("/tmp/local.tif") == "/tmp/local.tif"
    assert _to_vsigs("relative/path.tif") == "relative/path.tif"


def test_hydromt_yaml_emits_staged_local_paths_for_gs_inputs(tmp_path: Path) -> None:
    """The YAML config plumbed into HydroMT must use STAGED LOCAL paths for
    ``gs://`` inputs — never ``gs://`` (gcsfs segfault, job-0170) and never
    ``/vsigs/`` (HydroMT's data adapter stats catalog paths with fsspec's
    LOCAL filesystem before GDAL opens them, so a /vsigs/ GDAL-ism raises
    "No such file found" — proven live in the Stage 3 round-5 gate,
    OQ-0248-FLOOD-BUILD-VSIGS / job-0249). ``_stage_gcs_local`` downloads
    the object and hands HydroMT a real local file.
    """
    captured = _build_with_capture(
        tmp_path=tmp_path,
        river_geometry_uri=None,
    )

    opt = captured.get("opt")
    assert isinstance(opt, dict)

    setup_dep = opt.get("setup_dep")
    assert isinstance(setup_dep, dict), (
        f"setup_dep missing or wrong shape; got {setup_dep!r}"
    )
    datasets_dep = setup_dep.get("datasets_dep")
    assert isinstance(datasets_dep, list) and datasets_dep, (
        f"datasets_dep missing or empty; got {datasets_dep!r}"
    )
    elevtn_path = datasets_dep[0].get("elevtn")
    assert isinstance(elevtn_path, str)
    assert "gs://" not in elevtn_path, (
        f"job-0170 regression: elevtn still contains 'gs://' — gcsfs "
        f"dispatch segfaults. Got: {elevtn_path!r}"
    )
    assert not elevtn_path.startswith("/vsigs/"), (
        f"job-0249 regression: elevtn is a /vsigs/ GDAL-ism — HydroMT's "
        f"adapter fails fs.exists() on it ('No such file found', proven "
        f"live round-5). Catalog paths must be staged LOCAL files. "
        f"Got: {elevtn_path!r}"
    )
    assert "staged" in elevtn_path, (
        f"expected the fake staged-local path from _fake_stage; got "
        f"{elevtn_path!r}"
    )

    setup_rgh = opt.get("setup_manning_roughness")
    assert isinstance(setup_rgh, dict), (
        f"setup_manning_roughness missing; got {setup_rgh!r}"
    )
    datasets_rgh = setup_rgh.get("datasets_rgh")
    assert isinstance(datasets_rgh, list) and datasets_rgh, (
        f"datasets_rgh missing or empty; got {datasets_rgh!r}"
    )
    lulc_path = datasets_rgh[0].get("lulc")
    assert isinstance(lulc_path, str)
    assert "gs://" not in lulc_path and not lulc_path.startswith("/vsigs/"), (
        f"lulc must be a staged LOCAL path (no gs://, no /vsigs/). "
        f"Got: {lulc_path!r}"
    )
    assert "staged" in lulc_path


def test_gdal_num_threads_pinned_at_module_import() -> None:
    """``GDAL_NUM_THREADS=1`` must be set when the sfincs_builder module imports.

    The second half of the job-0170 cure. Multi-threaded GDAL reads
    through /vsigs/ have historically interacted badly with rasterio's
    dataset finaliser — the same cyfunction destructor that segfaulted
    under gcsfs. Pinning to 1 thread eliminates the race; bandwidth, not
    CPU, is the bottleneck for our typical bbox-sized NLCD/DEM reads.

    This test imports the module (idempotent — already imported by the
    test suite) and verifies the env key is set. Uses ``setdefault``
    semantics so a caller can override by setting the env BEFORE import.
    """
    import os
    import trid3nt_server.workflows.sfincs_builder  # noqa: F401 — triggers env setup

    assert os.environ.get("GDAL_NUM_THREADS") == "1", (
        f"job-0170 env regression: GDAL_NUM_THREADS must be '1' at module "
        f"import time to avoid the cyfunction destructor race that "
        f"segfaulted under gcsfs. Got: {os.environ.get('GDAL_NUM_THREADS')!r}"
    )
    # Defensive: at least one of the GS-relevant HTTP retry keys must be set
    # so transient GCS hiccups don't surface as raw exceptions to HydroMT.
    assert os.environ.get("GDAL_HTTP_MAX_RETRY") is not None, (
        "job-0170: GDAL_HTTP_MAX_RETRY must be set at module import for "
        "transient GS resilience (NFR-R-1)."
    )


def test_rasterio_open_with_retry_backs_off_and_succeeds() -> None:
    """``_rasterio_open_with_retry`` retries on transient failures.

    Simulates two transient ``RuntimeError`` failures followed by a
    success, mimicking a flaky /vsigs/ HTTP read. The retry wrapper must
    swallow the first two failures (with backoff) and return the third
    attempt's result.
    """
    from trid3nt_server.workflows import sfincs_builder
    from unittest.mock import patch, MagicMock

    fake_open = MagicMock(
        side_effect=[
            RuntimeError("transient HTTP 503"),
            RuntimeError("transient HTTP 503"),
            "ok-dataset",
        ]
    )
    with patch("rasterio.open", fake_open), patch("time.sleep") as fake_sleep:
        result = sfincs_builder._rasterio_open_with_retry(
            "/vsigs/bucket/key.tif", max_attempts=3
        )

    assert result == "ok-dataset"
    assert fake_open.call_count == 3, (
        f"_rasterio_open_with_retry should retry 3 times before succeeding; "
        f"called rasterio.open {fake_open.call_count} times"
    )
    # Exponential backoff: 1s then 2s (no sleep after the final attempt).
    fake_sleep.assert_any_call(1)
    fake_sleep.assert_any_call(2)


def test_rasterio_open_with_retry_exhausts_attempts_and_reraises() -> None:
    """``_rasterio_open_with_retry`` re-raises the underlying exception after exhaustion.

    On final failure the wrapper must surface the real cause unwrapped so
    the caller's typed-error translation (``SFINCSSetupError("LANDCOVER_
    READ_FAILED", ...)``) carries the actual error message. Wrapping in a
    fresh exception would lose the libcurl details the caller needs to
    diagnose.
    """
    from trid3nt_server.workflows import sfincs_builder
    from unittest.mock import patch, MagicMock

    underlying = RuntimeError("persistent /vsigs/ 503")
    fake_open = MagicMock(side_effect=underlying)

    with patch("rasterio.open", fake_open), patch("time.sleep"):
        with pytest.raises(RuntimeError, match=r"persistent /vsigs/ 503"):
            sfincs_builder._rasterio_open_with_retry(
                "/vsigs/bucket/key.tif", max_attempts=2
            )
    assert fake_open.call_count == 2


# --------------------------------------------------------------------------- #
# sprint-14-aws (Track C / Case 3) — NLCD validation-gate s3:// read.
# _extract_unique_nlcd_classes is reached on EVERY model_flood_scenario call
# (OQ-4 headline gate). Under TRID3NT_STORAGE_BACKEND=s3 the landcover_uri is
# s3://, and GDAL's /vsis3/ creds don't resolve the EC2 instance role
# (job-0293c) — so the s3 branch must boto3 stage-then-open. These tests
# monkeypatch the boto3 reader to return a synthetic local NLCD COG's bytes.
# --------------------------------------------------------------------------- #


def _write_nlcd_raster(path: Path, classes: "list[list[int]]") -> Path:
    """Write a single-band uint8 NLCD-style GeoTIFF with the given class grid."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    arr = np.array(classes, dtype="uint8")
    height, width = arr.shape
    bbox = (-116.30, 43.55, -116.10, 43.70)  # Idaho (Case 3 geography)
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "count": 1,
        "height": height,
        "width": width,
        "crs": "EPSG:4326",
        "transform": transform,
        "nodata": 255,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)
    return path


def test_nlcd_gate_s3_read_extracts_classes_via_boto3() -> None:
    """s3:// landcover → bytes staged via boto3 reader → unique classes extracted.

    Proves the NLCD validation-gate read survives the boto3 stage-then-open seam
    on AWS (the gate that runs before the flood solver on every call). The
    nodata sentinel (255) is excluded just like the local/gs:// path.
    """
    from trid3nt_server.workflows import sfincs_builder

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Classes 11 (water), 41 (forest), 81 (pasture) + 255 nodata sentinel.
        p = _write_nlcd_raster(
            tmp / "nlcd.tif",
            [[11, 41, 81], [11, 255, 81], [41, 41, 11]],
        )
        raster_bytes = p.read_bytes()

        def _fake_read_object_bytes_s3(uri: str) -> bytes:
            assert uri == "s3://test-cache/cache/landcover/nlcd.tif"
            return raster_bytes

        with patch(
            "trid3nt_server.tools.cache.read_object_bytes_s3",
            side_effect=_fake_read_object_bytes_s3,
        ) as mock_s3:
            classes = sfincs_builder._extract_unique_nlcd_classes(
                "s3://test-cache/cache/landcover/nlcd.tif"
            )
        mock_s3.assert_called_once()
        # 255 (nodata) excluded; the three real classes present.
        assert classes == {11, 41, 81}


def test_nlcd_gate_s3_read_boto3_failure_raises_landcover_read_failed() -> None:
    """A boto3 failure on the s3:// NLCD stage → typed LANDCOVER_READ_FAILED.

    Mirrors the local/gs:// failure wrapping so the failed-envelope path still
    threads the typed error (no uncaught crash before the solver).
    """
    from trid3nt_server.workflows import sfincs_builder

    with patch(
        "trid3nt_server.tools.cache.read_object_bytes_s3",
        side_effect=RuntimeError("boto3 get_object failed: AccessDenied"),
    ):
        with pytest.raises(SFINCSSetupError) as excinfo:
            sfincs_builder._extract_unique_nlcd_classes(
                "s3://test-cache/cache/landcover/nlcd.tif"
            )
    assert excinfo.value.error_code == "LANDCOVER_READ_FAILED"
    assert excinfo.value.details["landcover_uri"] == (
        "s3://test-cache/cache/landcover/nlcd.tif"
    )


def test_nlcd_gate_gs_read_unchanged_does_not_call_boto3() -> None:
    """Regression: gs:// (and local) NLCD reads must NOT touch the boto3 seam.

    The s3 branch is gated on the ``s3://`` prefix; a gs:// URI stays on the
    _to_vsigs/_rasterio_open_with_retry path. Assert read_object_bytes_s3 is
    never called and _to_vsigs is used for the gs:// path.
    """
    from trid3nt_server.workflows import sfincs_builder

    gs_uri = "gs://test-cache/cache/landcover/nlcd.tif"
    with (
        patch(
            "trid3nt_server.tools.cache.read_object_bytes_s3",
            side_effect=AssertionError("boto3 reader must not be called for gs://"),
        ),
        patch.object(
            sfincs_builder,
            "_to_vsigs",
            return_value="/vsigs/test-cache/cache/landcover/nlcd.tif",
        ) as mock_to_vsigs,
        patch.object(
            sfincs_builder,
            "_rasterio_open_with_retry",
            side_effect=RuntimeError("vsigs open stubbed"),
        ),
    ):
        with pytest.raises(SFINCSSetupError) as excinfo:
            sfincs_builder._extract_unique_nlcd_classes(gs_uri)
        assert excinfo.value.error_code == "LANDCOVER_READ_FAILED"
        mock_to_vsigs.assert_called_once_with(gs_uri)


# --------------------------------------------------------------------------- #
# Pre-solver phase timeouts (terminal-pipeline-card hardening)
#
# Before this fix the fetcher chain + build_sfincs_model could hang FOREVER
# (a wedged data endpoint, a GDAL VSI read with no overall timeout) with the
# card stuck 'running' and no progress — NATE's "120 min, never finished"
# silent hang. Each pre-solver phase is now bounded; a hang surfaces as a typed
# PRESOLVER_TIMEOUT failed envelope instead of an infinite await.
# --------------------------------------------------------------------------- #

import time as _time  # noqa: E402

from trid3nt_server.workflows import model_flood_scenario as _mfs  # noqa: E402


@pytest.mark.asyncio
async def test_fetcher_phase_timeout_returns_failed_envelope(monkeypatch) -> None:
    """A wedged fetcher (blocks longer than the phase budget) surfaces as a
    PRESOLVER_TIMEOUT failed envelope — NOT an infinite silent await."""
    monkeypatch.setattr(_mfs, "_FETCHER_PHASE_TIMEOUT_S", 0.2)

    def _hang_fetch_dem(*a, **k):  # noqa: ANN001, ANN002, ANN003
        # Block well past the 0.2s budget (runs in the to_thread worker).
        _time.sleep(3.0)
        return _mock_layer_uri("dem")

    with patch(
        "trid3nt_server.workflows.model_flood_scenario.fetch_dem",
        side_effect=_hang_fetch_dem,
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
            return_period_yr=100,
            duration_hr=24,
            compute_class="medium",
        )

    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.envelope_type == "modeled"
    # The :FAILED: anchor carries the timeout code so the card flips + narration
    # is honest. (model_flood_scenario:FAILED:PRESOLVER_TIMEOUT)
    assert ":FAILED:PRESOLVER_TIMEOUT" in envelope.workflow_name
    assert envelope.layers == []
    assert envelope.solver_run_ids == []


@pytest.mark.asyncio
async def test_build_phase_timeout_returns_failed_envelope(monkeypatch) -> None:
    """A wedged build_sfincs_model surfaces as PRESOLVER_TIMEOUT (the fetcher
    chain succeeds; the build hangs)."""
    monkeypatch.setattr(_mfs, "_BUILD_PHASE_TIMEOUT_S", 0.2)

    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [26.6, -81.9],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }

    def _hang_build(*a, **k):  # noqa: ANN001, ANN002, ANN003
        _time.sleep(3.0)

    with (
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", side_effect=_hang_build),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
            return_period_yr=100,
            duration_hr=24,
            compute_class="medium",
        )

    assert isinstance(envelope, AssessmentEnvelope)
    assert ":FAILED:PRESOLVER_TIMEOUT" in envelope.workflow_name
    assert envelope.layers == []
