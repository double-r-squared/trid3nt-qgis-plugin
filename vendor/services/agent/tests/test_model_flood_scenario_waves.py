"""E2E wiring tests for the SnapWave WAVE animation in model_flood_scenario (P5).

The quadtree+SnapWave coastal path runs ``postprocess_waves`` AFTER the depth
postprocess + frame emission, GATED on ``quadtree_run_result is not None``, inside
a DEGRADE-not-fail try/except. The peak wave layer rides the publish-or-honest-
drop gate (-> the envelope's ResultLayer set); the wave frames emit OUT-OF-BAND
via the emitter ("Wave height step N" — a separate web scrubber group).

These prove (all mocked — no network, GDAL, solver, S3):

1. A quadtree run calls ``postprocess_waves`` and the peak wave layer reaches the
   success envelope; the wave frames are emitted out-of-band.
2. A forced ``postprocess_waves`` raise still returns a SUCCESS envelope with the
   depth layers intact (degrade-not-fail — the wave failure NEVER sinks depth).
3. The NON-quadtree path never calls ``postprocess_waves`` (regression — the wave
   field only exists on a SnapWave/quadtree solve).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.tools.fetch_topobathy import TopobathyResult
from grace2_agent.tools.publish_layer import PublishLayerError
from grace2_agent.workflows.model_flood_scenario import model_flood_scenario
from grace2_agent.workflows.postprocess_flood import PostprocessError
from grace2_contracts import new_ulid
from grace2_contracts.envelope import AssessmentEnvelope
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult

_COASTAL_BBOX = (-85.45, 29.93, -85.38, 29.98)
_INLAND_BBOX = (-116.30, 43.55, -116.10, 43.70)


# --------------------------------------------------------------------------- #
# Mock builders
# --------------------------------------------------------------------------- #


def _topobathy_result() -> TopobathyResult:
    return TopobathyResult(
        layer_id="topobathy-test",
        name="Merged topo-bathymetry (3DEP + CUDEM)",
        layer_type="raster",
        uri="s3://test-cache/cache/static-30d/topobathy/coastal-test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
        bathymetry_present=True,
        cudem_tile_count=3,
        fallback_warning=None,
    )


def _mock_layer_uri(prefix: str) -> LayerURI:
    return LayerURI(
        layer_id=f"{prefix}-test",
        name=f"{prefix} test layer",
        layer_type="raster",
        uri=f"s3://test-cache/cache/static-30d/{prefix}/test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
    )


def _landcover_result() -> dict:
    return {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }


def _precip_result() -> dict:
    return {
        "precip_inches": 12.1,
        "units": "inches",
        "location": [29.95, -85.41],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 9",
        "project_area": "Southeastern States",
        "source": "noaa-atlas14-pfds",
    }


def _model_setup() -> ModelSetup:
    return ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="s3://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=_COASTAL_BBOX,
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )


def _run_result_ok(run_id: str) -> RunResult:
    return RunResult(
        run_id=run_id,
        handle_id=new_ulid(),
        status="complete",
        output_uri=f"s3://grace-2-hazard-prod-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )


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


def _depth_layers(run_id: str) -> list[LayerURI]:
    return [
        LayerURI(
            layer_id=f"flood-depth-peak-{run_id}",
            name="Peak flood depth",
            layer_type="raster",
            uri=f"s3://grace-2-hazard-prod-runs/{run_id}/flood_depth_peak.tif",
            style_preset="continuous_flood_depth",
            role="primary",
            units="meters",
        ),
    ]


_DEPTH_METRICS = {
    "max_depth_m": 1.8,
    "mean_depth_m": 0.4,
    "p95_depth_m": 1.2,
    "flooded_cell_count": 8_000,
    "crs": "EPSG:32616",
    "units": "meters",
}


def _wave_layers(run_id: str, n_frames: int = 3) -> list[LayerURI]:
    layers = [
        LayerURI(
            layer_id=f"wave-height-peak-{run_id}",
            name="Peak wave height",
            layer_type="raster",
            uri=f"s3://grace-2-hazard-prod-runs/{run_id}/wave_height_peak.tif",
            style_preset="continuous_wave_height",
            role="primary",
            units="meters",
        )
    ]
    for i in range(1, n_frames + 1):
        layers.append(
            LayerURI(
                layer_id=f"wave-height-frame-{i:02d}-{run_id}",
                name=f"Wave height step {i}",
                layer_type="raster",
                uri=(
                    f"s3://grace-2-hazard-prod-runs/{run_id}/"
                    f"wave_height_frame_{i:02d}.tif"
                ),
                style_preset="continuous_wave_height",
                role="context",
                units="meters",
            )
        )
    return layers


_WAVE_METRICS = {
    "max_depth_m": 4.0,
    "mean_depth_m": 1.5,
    "p95_depth_m": 3.5,
    "flooded_cell_count": 5_000,
    "crs": "EPSG:32616",
    "units": "meters",
}


class _FakeEmitter:
    """Captures add_loaded_layer calls (the out-of-band frame emission path)."""

    def __init__(self) -> None:
        self.loaded: list[LayerURI] = []

    async def add_loaded_layer(self, layer) -> None:  # noqa: ANN001
        self.loaded.append(layer)

    async def update_current_progress(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    async def emit_solve_progress(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    # task-168: this partial fake stands in for a PipelineEmitter that has no
    # running top-level parent step, so the nested-sub-step seam is a no-op:
    # begin_substeps does nothing and substep yields None + mints nothing
    # (mirrors PipelineEmitter.substep when _current_parent_step_id is None).
    def begin_substeps(self, *_a, **_k) -> None:  # noqa: ANN002
        return None

    @asynccontextmanager
    async def substep(self, *_a, **_k):  # noqa: ANN002
        yield None


def _quadtree_patches(
    *,
    run_id: str,
    emitter,
    postprocess_waves_mock,
    depth_layers=None,
):
    """The with-block patch tuple for the quadtree wave path (all mocked)."""

    async def _run_quadtree(*_a, **_k):  # noqa: ANN002
        return _run_result_ok(run_id)

    depth = depth_layers if depth_layers is not None else _depth_layers(run_id)

    return (
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_topobathy",
            return_value=_topobathy_result(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_landcover",
            return_value=_landcover_result(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_river_geometry",
            return_value=_mock_layer_uri("rivers"),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period",
            return_value=_precip_result(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.build_sfincs_model",
            return_value=_model_setup(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario._resolve_building_obstacle_uri",
            return_value=None,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario._resolve_quadtree_rivers_uri",
            return_value=None,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario._compose_and_upload_deckbuild_spec",
            return_value="s3://test-cache/cache/static-30d/sfincs_deck/x/build_spec.json",
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.run_sfincs_quadtree",
            side_effect=_run_quadtree,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.make_sfincs_mesh_layer_uri",
            return_value=None,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=(depth, _DEPTH_METRICS),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_waves",
            postprocess_waves_mock,
        ),
        # publish_layer drops in test (no QGIS/TiTiler) so peak layers honest-drop;
        # frames emit out-of-band only when the s3 uri is renderable — patch it to
        # ECHO a renderable https url so the peak survives + frames publish.
        patch(
            "grace2_agent.workflows.model_flood_scenario.publish_layer",
            side_effect=lambda **kw: f"https://cf.example.net/tiles/{kw['layer_id']}",
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.current_emitter",
            return_value=emitter,
        ),
    )


# --------------------------------------------------------------------------- #
# 1. quadtree run emits wave layers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_quadtree_run_emits_wave_layers() -> None:
    run_id = new_ulid()
    emitter = _FakeEmitter()
    waves_mock = MagicMock(return_value=(_wave_layers(run_id, n_frames=3), _WAVE_METRICS))

    for p in _quadtree_patches(
        run_id=run_id, emitter=emitter, postprocess_waves_mock=waves_mock
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX, quadtree=True, return_period_yr=100, duration_hr=24
        )
    finally:
        patch.stopall()

    assert isinstance(envelope, AssessmentEnvelope)
    # postprocess_waves was called once with the run output uri + run_id + bbox.
    waves_mock.assert_called_once()
    _, kwargs = waves_mock.call_args
    assert kwargs["run_id"] == run_id
    assert kwargs["bbox"] == _COASTAL_BBOX

    # The peak wave layer rode into the envelope's ResultLayer set.
    layer_ids = {l.layer_id for l in envelope.layers}
    assert f"wave-height-peak-{run_id}" in layer_ids
    wave_peak = next(l for l in envelope.layers if l.layer_id == f"wave-height-peak-{run_id}")
    assert wave_peak.style_preset == "continuous_wave_height"
    assert wave_peak.role == "primary"

    # The depth peak ALSO survived (waves are additive, never displace depth).
    assert f"flood-depth-peak-{run_id}" in layer_ids

    # The 3 wave frames were emitted OUT-OF-BAND (NOT in the envelope layers).
    emitted_wave_frames = [
        l for l in emitter.loaded
        if l.name.startswith("Wave height step")
    ]
    assert len(emitted_wave_frames) == 3
    for l in emitted_wave_frames:
        assert l.role == "context"
        assert l.style_preset == "continuous_wave_height"
    # Frames are NOT in the envelope (out-of-band only).
    assert not any(l.name.startswith("Wave height step") for l in envelope.layers)


# --------------------------------------------------------------------------- #
# 2. wave postprocess failure degrades — SUCCESS envelope, depth intact
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wave_postprocess_failure_degrades_not_fails() -> None:
    run_id = new_ulid()
    emitter = _FakeEmitter()

    def _raise(*_a, **_k):  # noqa: ANN002
        raise PostprocessError("RUN_OUTPUT_EMPTY", message="no SnapWave field")

    waves_mock = MagicMock(side_effect=_raise)

    for p in _quadtree_patches(
        run_id=run_id, emitter=emitter, postprocess_waves_mock=waves_mock
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX, quadtree=True, return_period_yr=100, duration_hr=24
        )
    finally:
        patch.stopall()

    waves_mock.assert_called_once()
    # SUCCESS envelope (modeled, NOT a failed:CODE solver_version).
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.envelope_type == "modeled"
    assert not envelope.flood.metrics.solver_version.startswith("failed:")
    # Depth peak intact; NO wave layers leaked in.
    layer_ids = {l.layer_id for l in envelope.layers}
    assert f"flood-depth-peak-{run_id}" in layer_ids
    assert not any("wave-height" in lid for lid in layer_ids)
    assert not any(l.name.startswith("Wave height") for l in emitter.loaded)


@pytest.mark.asyncio
async def test_wave_postprocess_unexpected_exception_degrades() -> None:
    """An UNEXPECTED (non-PostprocessError) raise also degrades, not fails."""
    run_id = new_ulid()
    emitter = _FakeEmitter()
    waves_mock = MagicMock(side_effect=RuntimeError("boom"))

    for p in _quadtree_patches(
        run_id=run_id, emitter=emitter, postprocess_waves_mock=waves_mock
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX, quadtree=True, return_period_yr=100, duration_hr=24
        )
    finally:
        patch.stopall()

    assert isinstance(envelope, AssessmentEnvelope)
    assert not envelope.flood.metrics.solver_version.startswith("failed:")
    assert any(l.layer_id == f"flood-depth-peak-{run_id}" for l in envelope.layers)


# --------------------------------------------------------------------------- #
# 3. non-quadtree path never calls postprocess_waves
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_non_quadtree_path_does_not_call_postprocess_waves() -> None:
    run_id = new_ulid()
    waves_mock = MagicMock(return_value=([], {}))

    async def _wfc(_h):  # noqa: ANN001
        return _run_result_ok(run_id)

    patches = (
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_dem",
            return_value=_mock_layer_uri("dem"),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_landcover",
            return_value=_landcover_result(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_river_geometry",
            return_value=_mock_layer_uri("rivers"),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period",
            return_value=_precip_result(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.build_sfincs_model",
            return_value=_model_setup(),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.run_solver",
            return_value=_make_handle(run_id),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.wait_for_completion",
            side_effect=_wfc,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=(_depth_layers(run_id), _DEPTH_METRICS),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_waves",
            waves_mock,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.publish_layer",
            side_effect=PublishLayerError("JOBS_CLIENT_UNAVAILABLE", "no qgis"),
        ),
    )
    for p in patches:
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_INLAND_BBOX, return_period_yr=100, duration_hr=24
        )
    finally:
        patch.stopall()

    assert isinstance(envelope, AssessmentEnvelope)
    waves_mock.assert_not_called()
