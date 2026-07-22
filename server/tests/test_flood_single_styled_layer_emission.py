"""Regression tests: the flood workflow emits EXACTLY ONE styled peak-depth layer.

Bug (live finding): a single "100-year flood Chattanooga" request rendered TWO
map layers of the same peak-depth data:

  * one styled "Peak depth" (white->blue->green, the postprocess output), AND
  * one raw "chattanooga-100-year" rendered in matplotlib viridis (a COG with
    NO style applied -> TiTiler defaults to viridis).

The viridis one is a redundant / raw duplicate. These tests lock the workflow
side of the fix: the ONE layer the workflow surfaces (postprocess_flood's
``LayerURI`` -> the published ``envelope.layers[0]`` -> the
``run_model_flood_scenario`` wrapper return) must ALWAYS carry the canonical
``continuous_flood_depth`` style preset and a clear human-readable name, and the
workflow must never emit a second, styleless (viridis-defaulting) peak-depth
layer of its own.

Scope note: the SECOND, viridis duplicate observed live originates from the LLM
issuing a SEPARATE ``publish_layer`` call (governed by ``adapter.py`` /
``server.py``, outside these owned files). These tests assert the workflow's own
emission is single + styled so that, once the wrap-site dedup is corrected, no
styleless flood layer can come from the workflow path.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from trid3nt_server.tools.publish_layer import PublishLayerError
from trid3nt_server.workflows.model_flood_scenario import (
    model_flood_scenario,
    run_model_flood_scenario,
)
from trid3nt_server.workflows.postprocess_flood import (
    FLOOD_DEPTH_STYLE_PRESET,
    postprocess_flood,
)
from trid3nt_contracts import new_ulid
from trid3nt_contracts.envelope import AssessmentEnvelope
from trid3nt_contracts.execution import (
    ExecutionHandle,
    LayerURI,
    ModelSetup,
    RunResult,
)

# --------------------------------------------------------------------------- #
# Self-contained mocked-workflow fixtures (mirror the v2 integration test so the
# fetch/build/solve chain is identical, without a cross-module test import).
# --------------------------------------------------------------------------- #

# Idaho test domain (non-Florida geography).
_BBOX = (-116.30, 43.55, -116.10, 43.70)

_DEPTH_METRICS = {
    "max_depth_m": 1.8,
    "mean_depth_m": 0.4,
    "p95_depth_m": 1.2,
    "flooded_cell_count": 8_000,
    "crs": "EPSG:3857",
    "units": "meters",
}


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
        "location": [43.6, -116.2],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14 Volume 1",
        "project_area": "Semiarid Southwest",
        "source": "noaa-atlas14-pfds",
    }


def _model_setup() -> ModelSetup:
    return ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=_BBOX,
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )


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


def _run_result_ok(run_id: str, handle_id: str) -> RunResult:
    return RunResult(
        run_id=run_id,
        handle_id=handle_id,
        status="complete",
        output_uri=f"s3://trid3nt-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=120.0,
    )


def _flood_layer(run_id: str) -> LayerURI:
    """The styled peak-depth layer postprocess_flood returns (with the canonical
    white->blue->green preset + clear name, post-fix)."""
    return LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Peak flood depth",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset=FLOOD_DEPTH_STYLE_PRESET,
        role="primary",
        units="meters",
    )


# --------------------------------------------------------------------------- #
# Helper: mocked full workflow with publish_layer SUCCEEDING (the live AWS path
# where the styled raster lands on the map).
# --------------------------------------------------------------------------- #


def _published_wms_url(layer_id: str) -> str:
    """A renderable, STYLED TiTiler/WMS URL (what publish_layer returns when it
    applies the continuous_flood_depth rescale+colormap)."""
    return (
        "https://titiler.example/cog/tiles/{z}/{x}/{y}.png"
        f"?url=s3://runs/{layer_id}.tif&rescale=0,3&colormap_name=blues"
    )


def _patch_chain(publish_side_effect):  # noqa: ANN001, ANN201
    """Patch the fetch/build/solve/postprocess chain; publish_layer behavior is
    the caller's choice (success URL or PublishLayerError)."""
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    async def _wfc(h):  # noqa: ANN001
        return _run_result_ok(run_id, handle.handle_id)

    patches = [
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_landcover", return_value=_landcover_result()),
        patch("trid3nt_server.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("trid3nt_server.workflows.model_flood_scenario.lookup_precip_return_period", return_value=_precip_result()),
        patch("trid3nt_server.workflows.model_flood_scenario.build_sfincs_model", return_value=_model_setup()),
        patch("trid3nt_server.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("trid3nt_server.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.postprocess_flood",
            return_value=([_flood_layer(run_id)], _DEPTH_METRICS),
        ),
        patch(
            "trid3nt_server.workflows.model_flood_scenario.publish_layer",
            side_effect=publish_side_effect,
        ),
    ]
    return run_id, patches


# --------------------------------------------------------------------------- #
# 1. postprocess_flood's layer is styled + clearly named (no styleless layer).
# --------------------------------------------------------------------------- #


def test_postprocess_flood_layer_is_styled_and_single() -> None:
    """postprocess_flood returns exactly ONE primary layer with the canonical
    white->blue->green preset and a clear human name — never a styleless COG."""
    run_id = new_ulid()
    cog_uri = f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif"

    with (
        patch(
            "trid3nt_server.workflows.postprocess_flood._resolve_run_output_to_local",
            return_value=Path("/tmp/fake.nc"),
        ),
        patch(
            # postprocess_flood now extracts via _extract_depth_frames, which
            # returns (peak_cog, peak_metrics, frame_cogs, frame_labels). With NO
            # time-varying output (only hmax/zsmax) frame_cogs/labels are empty,
            # so postprocess_flood emits EXACTLY the single peak layer (the
            # styled-single-layer contract this test guards).
            "trid3nt_server.workflows.postprocess_flood._extract_depth_frames",
            return_value=(Path("/tmp/fake_cog.tif"), dict(_DEPTH_METRICS), [], []),
        ),
        patch(
            "trid3nt_server.workflows.postprocess_flood._upload_cog_to_runs_bucket",
            return_value=cog_uri,
        ),
        patch("pathlib.Path.unlink", return_value=None),
    ):
        layers, _metrics = postprocess_flood(
            f"s3://trid3nt-runs/{run_id}/", run_id=run_id
        )

    # Exactly one layer, and it is the styled peak-depth layer.
    assert len(layers) == 1
    layer = layers[0]
    assert layer.role == "primary"
    assert layer.layer_type == "raster"
    # The canonical white->blue->green preset is set (NOT empty/None -> no viridis).
    assert layer.style_preset == FLOOD_DEPTH_STYLE_PRESET
    assert layer.style_preset  # truthy guard against the styleless duplicate
    # Clear, human-readable name (per the bug ask).
    assert layer.name == "Peak flood depth"


# --------------------------------------------------------------------------- #
# 2. Full workflow, publish SUCCEEDS: envelope has ONE styled layer (no viridis).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_emits_single_styled_layer_on_publish_success() -> None:
    """When publish_layer succeeds, the envelope carries EXACTLY ONE layer: the
    styled peak-depth layer at the renderable WMS URL — no second styleless one."""
    run_id, patches = _patch_chain(
        publish_side_effect=lambda **kw: _published_wms_url(kw["layer_id"])
    )


    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        envelope = await model_flood_scenario(
            bbox=_BBOX, return_period_yr=100, duration_hr=24
        )

    assert isinstance(envelope, AssessmentEnvelope)
    # ONE layer only — not two.
    assert len(envelope.layers) == 1, (
        f"expected exactly 1 flood layer, got {len(envelope.layers)}: "
        f"{[(l.name, l.style_preset, l.uri) for l in envelope.layers]}"
    )
    layer = envelope.layers[0]
    # It is the renderable (published) URL, NOT a raw gs:// COG.
    assert layer.uri.startswith("http")
    assert not layer.uri.startswith("gs://")
    assert not layer.uri.startswith("s3://")
    # It is STYLED (white->blue->green), so TiTiler never falls back to viridis.
    assert layer.style_preset == FLOOD_DEPTH_STYLE_PRESET
    assert layer.style_preset
    # No second layer of any kind carries an empty/None style preset.
    assert all(l.style_preset for l in envelope.layers)


# --------------------------------------------------------------------------- #
# 3. Full workflow, publish FAILS: the styleless raw gs:// COG is DROPPED (not
#    emitted as a second/only layer). The map stays honest — no viridis raster.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_workflow_drops_raw_layer_when_publish_fails() -> None:
    """On publish failure the raw gs:// COG is dropped (job-0254 honest-drop), so
    the envelope carries ZERO layers — never the styleless viridis raster."""
    run_id, patches = _patch_chain(
        publish_side_effect=PublishLayerError("JOBS_CLIENT_UNAVAILABLE", "no qgis in test")
    )


    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        envelope = await model_flood_scenario(
            bbox=_BBOX, return_period_yr=100, duration_hr=24
        )

    assert isinstance(envelope, AssessmentEnvelope)
    # The raw gs:// COG must NOT be emitted (it would render as viridis).
    assert envelope.layers == []
    # And crucially: there is no styleless layer hiding in the set.
    assert all(l.style_preset for l in envelope.layers)


# --------------------------------------------------------------------------- #
# 4. The LLM-facing wrapper returns ONE styled LayerURI (the single map emission
#    the emitter turns into a loaded_layer) — never a styleless one.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_wrapper_returns_single_styled_layer_uri() -> None:
    """run_model_flood_scenario returns a single styled LayerURI on success — the
    one object emit_tool_call feeds to add_loaded_layer (one map layer, styled)."""
    run_id, patches = _patch_chain(
        publish_side_effect=lambda **kw: _published_wms_url(kw["layer_id"])
    )


    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        result = await run_model_flood_scenario(
            bbox=_BBOX, return_period_yr=100, duration_hr=24
        )

    # On success the wrapper returns a SINGLE LayerURI (not a dict, not a list).
    assert isinstance(result, LayerURI)
    assert result.style_preset == FLOOD_DEPTH_STYLE_PRESET
    assert result.style_preset  # styled -> no viridis default
    assert result.uri.startswith("http")
    assert result.bbox is not None  # carries bbox for zoom-to (job-0160)
