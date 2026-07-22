"""Flood-animation Phase 1 — time-stepped inundation sequential group (engine-agnostic).

These tests pin the workflow-side integration that turns the per-frame depth COGs
``postprocess_flood`` now emits into a SINGLE Wave-1 sequential temporal group on
the map, WITHOUT changing ``run_model_flood_scenario``'s single-LayerURI return
shape (so the published/on_map LLM dedup + the habitat/Pelicun hazard-raster
consumers are untouched).

What is asserted:

1. ``model_flood_scenario`` (with a real ``PipelineEmitter`` bound via the
   ``current_emitter`` contextvar + a mocked chain whose ``postprocess_flood``
   returns ``[peak] + [3 frames]``):
     - ``publish_layer`` is called once for the peak + once per frame (4 total);
     - the emitter's ``loaded_layers`` ends up with the peak + 3 frames as
       SEPARATE rows (distinct ``_layer_identity_key`` → no dedup collapse);
     - the 3 frame rows are named ``"Flood depth step 1..3"`` (the EXACT web
       ``parseFrameToken`` token) and share the flood style preset;
     - the returned envelope's ``layers`` carries ONLY the published primary
       (frames ride out-of-band through the emitter, never the envelope/return).
2. With NO emitter bound (direct call), the same mocked chain emits the primary
   normally and does NOT publish frames (Test-25 parity: publish_layer == 1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from grace2_agent.pipeline_emitter import (
    PipelineEmitter,
    _CURRENT_EMITTER,
    _layer_identity_key,
)
from grace2_agent.workflows.model_flood_scenario import model_flood_scenario
from grace2_contracts import new_ulid
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult


# --------------------------------------------------------------------------- #
# Helpers (mirrors test_model_flood_scenario.py)
# --------------------------------------------------------------------------- #


class _CapturingSink:
    async def __call__(self, text: str) -> None:  # pragma: no cover - trivial
        json.loads(text)


def _make_handle(run_id: str) -> ExecutionHandle:
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


def _build_mocks(run_id: str):
    """Return the standard happy-chain mock objects + a postprocess_flood that
    yields [peak] + [3 frames] and a publish_layer that returns a TiTiler-style
    tile-template URL embedding the source COG as ``url=`` (so the emitter's
    _layer_identity_key keys on the distinct underlying COG per frame)."""
    handle = _make_handle(run_id)
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
        output_uri=f"gs://grace-2-hazard-prod-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )

    peak_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Peak flood depth",
        layer_type="raster",
        uri=f"gs://grace-2-hazard-prod-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    frame_layers = [
        LayerURI(
            layer_id=f"flood-depth-frame-{i:02d}-{run_id}",
            name=f"Flood depth step {i}",
            layer_type="raster",
            uri=f"gs://grace-2-hazard-prod-runs/{run_id}/flood_depth_frame_{i:02d}.tif",
            style_preset="continuous_flood_depth",
            role="context",
            units="meters",
        )
        for i in range(1, 4)  # steps 1, 2, 3
    ]
    depth_metrics = {
        "max_depth_m": 2.4, "mean_depth_m": 0.6, "p95_depth_m": 1.9,
        "flooded_cell_count": 12_345, "crs": "EPSG:32617", "units": "meters",
    }

    return (
        handle,
        landcover_result,
        precip_result,
        model_setup,
        run_result_ok,
        peak_layer,
        frame_layers,
        depth_metrics,
    )


def _titiler_template(cog_uri: str) -> str:
    """A TiTiler tile-template URL embedding the source COG as ``url=`` so the
    emitter's _layer_identity_key resolves to the distinct underlying COG."""
    from urllib.parse import quote

    return (
        "https://titiler.test/cog/tiles/{z}/{x}/{y}.png"
        f"?url={quote(cog_uri, safe='')}&rescale=0,3"
    )


# --------------------------------------------------------------------------- #
# Test 1 — frames land as a sequential group via the emitter
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_frames_emitted_as_distinct_loaded_layers_via_emitter() -> None:
    run_id = new_ulid()
    (
        handle,
        landcover_result,
        precip_result,
        model_setup,
        run_result_ok,
        peak_layer,
        frame_layers,
        depth_metrics,
    ) = _build_mocks(run_id)

    publish_calls: list[dict] = []

    def _mock_publish_layer(layer_uri, layer_id, style_preset, **kwargs):  # noqa: ANN001
        publish_calls.append(
            {"layer_uri": layer_uri, "layer_id": layer_id, "style_preset": style_preset}
        )
        return _titiler_template(layer_uri)

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    emitter = PipelineEmitter(session_id=new_ulid(), sink=_CapturingSink())
    token = _CURRENT_EMITTER.set(emitter)
    try:
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
                return_value=([peak_layer] + frame_layers, depth_metrics),
            ),
            patch(
                "grace2_agent.workflows.model_flood_scenario.publish_layer",
                side_effect=_mock_publish_layer,
            ),
        ):
            envelope = await model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
                return_period_yr=100,
                duration_hr=24,
                compute_class="medium",
            )
    finally:
        _CURRENT_EMITTER.reset(token)

    # task #207: input rasters (DEM + landcover) ALSO publish now (role="input"),
    # so filter to the RESULT (flood-depth) publishes for the result-layer
    # assertions. publish_layer fired for the peak + each of the 3 frames = 4
    # result publishes; the 2 input publishes carry an "input-" layer_id prefix.
    result_calls = [c for c in publish_calls if c["layer_id"].startswith("flood-depth-")]
    input_calls = [c for c in publish_calls if c["layer_id"].startswith("input-")]
    assert len(result_calls) == 4, (
        f"expected publish_layer x4 (peak + 3 frames); got {len(result_calls)}: "
        f"{[c['layer_id'] for c in result_calls]}"
    )
    # The DEM + landcover inputs are surfaced via their own publish round-trip.
    assert {c["layer_id"].rsplit('-', 1)[0] for c in input_calls} == {
        "input-dem",
        "input-landcover",
    }, f"expected DEM + landcover input publishes; got {[c['layer_id'] for c in input_calls]}"
    pub_ids = [c["layer_id"] for c in result_calls]
    assert pub_ids[0] == f"flood-depth-peak-{run_id}"
    assert pub_ids[1:] == [f"flood-depth-frame-{i:02d}-{run_id}" for i in range(1, 4)]
    assert all(c["style_preset"] == "continuous_flood_depth" for c in result_calls)

    # The emitter accumulated the 3 frames as SEPARATE loaded_layers (distinct
    # underlying COG url= → distinct _layer_identity_key → no merge).
    #
    # NOTE: the PEAK layer is NOT added here — in production it rides the normal
    # emit_tool_call gate on the wrapper's RETURNED LayerURI (run_model_flood_
    # scenario → add_loaded_layer), which this DIRECT model_flood_scenario call
    # bypasses. The frames are the ONLY layers model_flood_scenario emits
    # out-of-band via the emitter (the peak's add_loaded_layer is the wrapper's
    # job). So loaded_layers here == exactly the 3 frames.
    loaded = emitter._loaded_layers
    names = [l.name for l in loaded]
    for i in range(1, 4):
        assert f"Flood depth step {i}" in names, f"missing frame step {i} in {names}"

    frame_rows = [l for l in loaded if l.name.startswith("Flood depth step")]
    assert len(frame_rows) == 3, f"expected 3 frame rows; got {len(frame_rows)}"
    assert "Peak flood depth" not in names, (
        "peak must NOT be emitted by model_flood_scenario itself — it rides the "
        "wrapper's emit_tool_call gate on the returned LayerURI"
    )

    # Distinct identity keys — the dedup must NOT collapse the frames.
    keys = [_layer_identity_key(l.uri) for l in frame_rows]
    assert len(set(keys)) == 3, (
        f"frame identity keys collided (dedup would merge them); keys={keys}"
    )
    assert all(l.style_preset == "continuous_flood_depth" for l in frame_rows)

    # The returned envelope's layers carry ONLY the published primary — frames
    # are emitted OUT-OF-BAND, never on the envelope/return (so the published/
    # on_map LLM dedup + habitat/Pelicun layers[0] contract are untouched).
    env_layer_names = [l.name for l in envelope.layers]
    assert env_layer_names == ["Peak flood depth"], (
        f"envelope.layers must carry only the primary; got {env_layer_names}"
    )


# --------------------------------------------------------------------------- #
# Test 2 — no emitter bound: frames are NOT published (Test-25 parity)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_emitter_does_not_publish_frames() -> None:
    run_id = new_ulid()
    (
        handle,
        landcover_result,
        precip_result,
        model_setup,
        run_result_ok,
        peak_layer,
        frame_layers,
        depth_metrics,
    ) = _build_mocks(run_id)

    publish_calls: list[str] = []

    def _mock_publish_layer(layer_uri, layer_id, style_preset, **kwargs):  # noqa: ANN001
        publish_calls.append(layer_id)
        return _titiler_template(layer_uri)

    async def _wfc(handle):  # noqa: ANN001
        return run_result_ok

    # Ensure NO emitter is bound (direct-call / smoke / test path).
    assert _CURRENT_EMITTER.get() is None
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
            return_value=([peak_layer] + frame_layers, depth_metrics),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.publish_layer",
            side_effect=_mock_publish_layer,
        ),
    ):
        envelope = await model_flood_scenario(
            bbox=(-81.92, 26.55, -81.80, 26.68),
            return_period_yr=100,
            duration_hr=24,
            compute_class="medium",
        )

    # Only the PEAK layer is published — frames are skipped without an emitter.
    assert publish_calls == [f"flood-depth-peak-{run_id}"], (
        f"with no emitter only the peak should publish; got {publish_calls}"
    )
    assert [l.name for l in envelope.layers] == ["Peak flood depth"]
