"""job-0160 zoom-to emission tests.

Two complementary assertions land here:

1. ``test_zoom_on_area_first_emits_map_command_before_compute`` — the
   ``model_flood_scenario`` workflow, run inside a ``PipelineEmitter.emit_tool_call``
   bracket (which binds the active emitter via the new ``_CURRENT_EMITTER``
   ContextVar), fires ``map-command(zoom-to, bbox=resolved_bbox)`` IMMEDIATELY
   after ``_resolve_bbox`` succeeds — BEFORE any compute. Verifies the
   responsive-design seam Part 3 of the kickoff demands.

2. ``test_run_model_flood_scenario_wrapper_includes_bbox_in_layer_uri`` — the
   LLM-facing wrapper (``run_model_flood_scenario``) returns a ``LayerURI``
   carrying ``envelope.bbox`` so ``PipelineEmitter.add_loaded_layer`` fires
   the post-publish ``emit_map_command("zoom-to")``. This guards against the
   Part 2 regression (the bbox was previously dropped by the wrapper, so the
   layer landed but the camera never flew).

3. ``test_current_emitter_context_var_isolation`` — ``current_emitter()``
   returns ``None`` outside an ``emit_tool_call`` scope (smoke harness,
   direct test) and the bound emitter inside the scope; the binding is
   unwound exactly once on exit (token discipline).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from grace2_agent.pipeline_emitter import PipelineEmitter, current_emitter
from grace2_agent.workflows.model_flood_scenario import (
    model_flood_scenario,
    run_model_flood_scenario,
)
from grace2_contracts import new_ulid
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_handle(run_id: str | None = None) -> ExecutionHandle:
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


def _ft_myers_bbox() -> tuple[float, float, float, float]:
    return (-81.92, 26.55, -81.80, 26.68)


def _make_emitter(captured: list[tuple[str, dict]]) -> PipelineEmitter:
    """Build an emitter whose sink records every emission's type + payload."""

    async def _sink(raw: str) -> None:
        import json

        env = json.loads(raw)
        captured.append((env["type"], env["payload"]))

    return PipelineEmitter(session_id=new_ulid(), sink=_sink)


# --------------------------------------------------------------------------- #
# Test 1 — zoom-on-area-first emits map-command BEFORE compute
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_zoom_on_area_first_emits_map_command_before_compute() -> None:
    """Workflow emits ``map-command(zoom-to)`` immediately after bbox resolves.

    Drive the workflow through ``emit_tool_call`` so the active-emitter
    ContextVar is bound; assert the first ``map-command`` envelope is
    ``zoom-to`` with the supplied bbox, AND that it precedes ``fetch_dem``
    (the first compute step).
    """
    captured: list[tuple[str, dict]] = []
    emitter = _make_emitter(captured)
    fetch_dem_called: list[bool] = []

    def _fetch_dem(bbox, **kwargs):  # noqa: ANN001
        # If we got here, zoom-to must already have been emitted into ``captured``.
        fetch_dem_called.append(True)
        zoom_events = [t for t, _ in captured if t == "map-command"]
        # Capture the snapshot inside the side-effect so we can assert order.
        _fetch_dem.zoom_events_at_call = list(zoom_events)  # type: ignore[attr-defined]
        return _mock_layer_uri("dem")

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
    handle = _make_handle()
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/",
        grid_resolution_m=30.0,
        bbox=_ft_myers_bbox(),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=handle.run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{handle.run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{handle.run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{handle.run_id}/flood_depth_peak.tif",
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
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", side_effect=_fetch_dem),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("grace2_agent.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("grace2_agent.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("grace2_agent.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
    ):
        # Drive the workflow through emit_tool_call so the active-emitter
        # ContextVar is bound for the duration of the invoke.
        async def _invoke():
            return await model_flood_scenario(bbox=_ft_myers_bbox())

        await emitter.emit_tool_call(
            name="model_flood_scenario",
            tool_name="model_flood_scenario",
            invoke=_invoke,
        )

    assert fetch_dem_called, "fetch_dem should have been called"
    # Order assertion: at least one map-command(zoom-to) was emitted BEFORE
    # fetch_dem ran. The side-effect captured the map-command count at that
    # moment.
    zoom_events_before_compute = getattr(_fetch_dem, "zoom_events_at_call", [])
    assert len(zoom_events_before_compute) >= 1, (
        f"Expected ≥1 map-command emission BEFORE fetch_dem; got "
        f"{zoom_events_before_compute!r}. Full captured stream: "
        f"{[t for t, _ in captured]!r}"
    )
    # And the first map-command's payload carries the resolved bbox.
    first_map_command = next(p for t, p in captured if t == "map-command")
    assert first_map_command["command"] == "zoom-to"
    assert tuple(first_map_command["args"]["bbox"]) == _ft_myers_bbox()


# --------------------------------------------------------------------------- #
# Test 2 — wrapper carries envelope.bbox onto returned LayerURI
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_model_flood_scenario_wrapper_includes_bbox_in_layer_uri() -> None:
    """The LLM-facing wrapper return path carries ``envelope.bbox`` on the LayerURI.

    Drives the wrapper directly (not through ``emit_tool_call``) and checks
    that the returned ``LayerURI.bbox`` matches the workflow's resolved
    bbox so the downstream ``PipelineEmitter.add_loaded_layer`` fires
    ``emit_map_command("zoom-to")``.
    """
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
        bbox=_ft_myers_bbox(),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=handle.run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{handle.run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{handle.run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{handle.run_id}/flood_depth_peak.tif",
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
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("grace2_agent.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("grace2_agent.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("grace2_agent.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
        # job-0254: the happy path requires publish_layer to SUCCEED (return a
        # WMS URL). Previously this test left it unpatched and leaned on the old
        # gs:// fallback to produce a LayerURI — that fallback is the leak this
        # job closes (publish failure now DROPS the layer). Patch to a WMS URL
        # so the wrapper legitimately returns a renderable LayerURI carrying bbox.
        patch(
            "grace2_agent.workflows.model_flood_scenario.publish_layer",
            return_value=(
                "https://qgis.test.example.com/ogc/wms"
                "?MAP=/mnt/qgs/grace2-sample.qgs"
                f"&LAYERS=flood-depth-peak-{handle.run_id}"
            ),
        ),
    ):
        result = await run_model_flood_scenario(bbox=_ft_myers_bbox())

    assert isinstance(result, LayerURI), (
        f"wrapper must return LayerURI on success; got {type(result).__name__}"
    )
    assert result.bbox is not None, (
        "wrapper dropped envelope.bbox on the returned LayerURI — "
        "PipelineEmitter.add_loaded_layer will not fire emit_map_command(zoom-to)"
    )
    assert tuple(result.bbox) == _ft_myers_bbox()


# --------------------------------------------------------------------------- #
# Test 3 — ContextVar isolation: current_emitter() = None outside emit_tool_call
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_current_emitter_context_var_isolation() -> None:
    """``current_emitter()`` is ``None`` outside ``emit_tool_call``; bound inside.

    Token discipline: the binding is unwound by the ``finally`` clause even
    when the invoke raises; subsequent calls see ``None`` again.
    """
    # Baseline — outside any emit_tool_call, no emitter is bound.
    assert current_emitter() is None

    captured: list[tuple[str, dict]] = []
    emitter = _make_emitter(captured)

    seen_inside: list[PipelineEmitter | None] = []

    async def _peek():
        seen_inside.append(current_emitter())
        return _mock_layer_uri("peek")

    await emitter.emit_tool_call(name="peek", tool_name="peek", invoke=_peek)

    assert len(seen_inside) == 1
    assert seen_inside[0] is emitter, (
        "current_emitter() inside emit_tool_call must return the bound emitter"
    )

    # After exit — binding is unwound.
    assert current_emitter() is None

    # And the binding is unwound even on exception (token discipline).
    async def _raise():
        raise RuntimeError("intentional")

    with pytest.raises(RuntimeError):
        await emitter.emit_tool_call(name="boom", tool_name="boom", invoke=_raise)

    assert current_emitter() is None


# --------------------------------------------------------------------------- #
# Test 4 — Smoke harness still works (current_emitter() = None → no emit, no crash)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_without_emitter_does_not_crash() -> None:
    """Direct call (no emitter bound) → workflow still runs, no map-command, no crash.

    Guards the ``current_emitter() is None`` fallback branch in the zoom-on-area-first
    block. Smoke harnesses and unit tests that call ``model_flood_scenario`` directly
    must not crash on the optional emit.
    """
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
        bbox=_ft_myers_bbox(),
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=handle.run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{handle.run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{handle.run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{handle.run_id}/flood_depth_peak.tif",
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

    assert current_emitter() is None  # precondition

    with (
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("grace2_agent.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("grace2_agent.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("grace2_agent.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
    ):
        envelope = await model_flood_scenario(bbox=_ft_myers_bbox())

    assert envelope.bbox == _ft_myers_bbox()
    assert envelope.flood is not None
    assert envelope.flood.metrics.max_depth_m == pytest.approx(2.4)
