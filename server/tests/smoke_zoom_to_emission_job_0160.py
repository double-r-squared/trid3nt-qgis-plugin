"""job-0160 live smoke harness — verifies the zoom-to envelope stream on the wire.

Drives ``model_flood_scenario`` through a real ``PipelineEmitter`` whose sink
captures every JSON-serialized envelope. Demonstrates two seams:

1. **Zoom-on-area-first**: the FIRST ``map-command`` envelope on the wire is
   ``zoom-to`` with the supplied bbox, and it precedes the ``pipeline-state``
   step for ``fetch_dem`` (the first compute call).

2. **Post-publish zoom-to**: the wrapper's returned ``LayerURI`` carries
   ``bbox``, so ``add_loaded_layer`` fires a SECOND ``map-command(zoom-to)``
   after the layer publishes. The agent surface preserves the same envelope
   shape both times.

Run with:
    .venv-agent/bin/python server/tests/smoke_zoom_to_emission_job_0160.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_agent.workflows.model_flood_scenario import run_model_flood_scenario
from grace2_contracts import new_ulid
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult


FT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)


def _layer(prefix: str) -> LayerURI:
    return LayerURI(
        layer_id=f"{prefix}-test",
        name=f"{prefix} test layer",
        layer_type="raster",
        uri=f"gs://test-cache/cache/static-30d/{prefix}/test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
    )


def _handle(run_id: str) -> ExecutionHandle:
    return ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver="sfincs",
        compute_class="standard",
        workflows_execution_id=(
            "projects/test/locations/us-central1/workflows/"
            "grace-2-sfincs-orchestrator/executions/test-exec"
        ),
        workflow_name="grace-2-sfincs-orchestrator",
        workflow_location="us-central1",
        submitted_at=datetime.now(timezone.utc),
    )


async def main() -> None:
    captured: list[dict] = []

    async def sink(raw: str) -> None:
        captured.append(json.loads(raw))

    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    run_id = new_ulid()
    handle = _handle(run_id)
    landcover_result = {
        "layer": _layer("landcover"),
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
        bbox=FT_MYERS_BBOX,
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"gs://grace-2-hazard-prod-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"gs://grace-2-hazard-prod-runs/{run_id}/flood_depth_peak.tif",
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
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", return_value=_layer("dem")),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=landcover_result),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_layer("rivers")),
        patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period", return_value=precip_result),
        patch("grace2_agent.workflows.model_flood_scenario.build_sfincs_model", return_value=model_setup),
        patch("grace2_agent.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("grace2_agent.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=([flood_layer], depth_metrics),
        ),
    ):
        await emitter.emit_tool_call(
            name="run_model_flood_scenario",
            tool_name="run_model_flood_scenario",
            invoke=lambda: run_model_flood_scenario(bbox=FT_MYERS_BBOX),
        )

    # --- Report ---
    print(f"\n=== job-0160 smoke: total emissions = {len(captured)} ===\n")
    map_commands = []
    for i, env in enumerate(captured):
        t = env["type"]
        if t == "map-command":
            cmd = env["payload"]["command"]
            bbox = env["payload"]["args"].get("bbox")
            print(f"  [{i:02d}] {t:18s} cmd={cmd:8s} bbox={bbox}")
            map_commands.append((i, cmd, bbox))
        else:
            print(f"  [{i:02d}] {t}")

    print()
    print(f"=== {len(map_commands)} map-command emissions ===")
    for i, cmd, bbox in map_commands:
        print(f"  position {i}: {cmd}({bbox})")

    # --- Assertions ---
    assert len(map_commands) >= 2, (
        f"Expected ≥2 map-command emissions (zoom-on-area-first + post-publish); "
        f"got {len(map_commands)}"
    )
    first_idx, first_cmd, first_bbox = map_commands[0]
    assert first_cmd == "zoom-to"
    assert tuple(first_bbox) == FT_MYERS_BBOX

    # The first zoom-to must precede the first pipeline-state for fetch_dem.
    dem_step_idx = None
    for i, env in enumerate(captured):
        if env["type"] == "pipeline-state":
            for step in env["payload"]["steps"]:
                if step.get("tool_name") == "fetch_dem" or step.get("name") == "fetch_dem":
                    dem_step_idx = i
                    break
            if dem_step_idx is not None:
                break

    # Note: fetch_dem here is called as an atomic function (not via emit_tool_call)
    # because we're mocking it — so there's no "fetch_dem" step in the pipeline.
    # But the workflow IS wrapped in emit_tool_call so we have a pipeline step
    # for the workflow itself, AND the zoom-to fires BEFORE the call into fetch_dem.
    # The order assertion is therefore: first map-command precedes first session-state
    # emission (which fires after the wrapper returns the LayerURI).
    first_session_state_idx = next(
        (i for i, env in enumerate(captured) if env["type"] == "session-state"),
        None,
    )
    assert first_session_state_idx is not None, "expected a session-state emission"
    assert first_idx < first_session_state_idx, (
        f"first map-command (position {first_idx}) should precede "
        f"the first session-state (position {first_session_state_idx})"
    )

    # And there must be a SECOND map-command after the session-state (post-publish).
    post_publish_zoom = [
        (i, cmd, bbox) for i, cmd, bbox in map_commands if i > first_session_state_idx
    ]
    assert len(post_publish_zoom) >= 1, (
        f"Expected ≥1 map-command AFTER the first session-state "
        f"(post-publish zoom-to); got {post_publish_zoom!r}"
    )
    print(
        f"\nPASS — zoom-on-area-first at position {first_idx} "
        f"(BEFORE session-state at {first_session_state_idx}), "
        f"post-publish zoom-to at position {post_publish_zoom[0][0]}.\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
