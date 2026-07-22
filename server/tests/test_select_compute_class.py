"""Auto vertical scaling per case — ``select_compute_class`` + wiring tests
(sprint-16, NATE 2026-06-17).

The Batch compute environment already right-sizes the EC2 instance per job and
scales to zero; the missing piece was the AGENT picking the right
``compute_class`` per case from the AOI/mesh element count instead of always
defaulting to ``"standard"`` (8 vCPU). These tests prove:

1. ``select_compute_class`` maps element-count thresholds onto the vertical vCPU
   ladder small -> standard -> large -> xlarge.
2. A large estimate -> a larger class; a small estimate -> ``"small"``.
3. A missing / zero / None / NaN / non-numeric estimate -> the ``"standard"``
   fallback (NEVER raises — the dispatch can't crash on an absent estimate).
4. The new higher tier ``"xlarge"`` resolves cleanly through ``_aws_batch_sizing``
   (and is present in the alias map) — 48 vCPU / 96 GiB.
5. ``model_flood_scenario`` passes the COMPUTED class (from the autoscale
   estimated_active_cells) to ``run_solver`` — not the caller's default.
6. ``model_urban_flood_swmm`` passes the COMPUTED class (from the built mesh's
   n_active_cells) to ``run_solver`` on the out-of-process lane.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.tools.solver import (
    AWS_BATCH_COMPUTE_CLASS_SIZING,
    COMPUTE_CLASS_FALLBACK,
    COMPUTE_CLASS_LARGE_MAX_ELEMENTS,
    COMPUTE_CLASS_SMALL_MAX_ELEMENTS,
    COMPUTE_CLASS_STANDARD_MAX_ELEMENTS,
    _COMPUTE_CLASS_ALIAS,
    select_compute_class,
)


# --------------------------------------------------------------------------- #
# 1. select_compute_class — the element-count -> class ladder
# --------------------------------------------------------------------------- #


def test_small_estimate_selects_small() -> None:
    # An estimate comfortably under SMALL_MAX -> the cheap 4-vCPU box.
    assert select_compute_class(1_000) == "small"
    assert select_compute_class(COMPUTE_CLASS_SMALL_MAX_ELEMENTS - 1) == "small"


def test_mid_estimate_selects_standard() -> None:
    assert select_compute_class(COMPUTE_CLASS_SMALL_MAX_ELEMENTS) == "standard"
    assert select_compute_class(COMPUTE_CLASS_STANDARD_MAX_ELEMENTS - 1) == "standard"


def test_large_estimate_selects_large() -> None:
    assert select_compute_class(COMPUTE_CLASS_STANDARD_MAX_ELEMENTS) == "large"
    assert select_compute_class(COMPUTE_CLASS_LARGE_MAX_ELEMENTS - 1) == "large"


def test_very_large_estimate_selects_xlarge() -> None:
    # At/above LARGE_MAX the job reaches for the new higher-powered tier.
    assert select_compute_class(COMPUTE_CLASS_LARGE_MAX_ELEMENTS) == "xlarge"
    assert select_compute_class(5_000_000) == "xlarge"


def test_ladder_is_monotonic_non_decreasing() -> None:
    """As the element count rises the chosen vCPU tier never drops."""
    order = {"small": 0, "standard": 1, "large": 2, "xlarge": 3}
    last = -1
    for n in (10, 49_000, 50_000, 200_000, 250_000, 999_999, 1_000_000, 10_000_000):
        rank = order[select_compute_class(n)]
        assert rank >= last, f"ladder regressed at n={n}"
        last = rank


# --------------------------------------------------------------------------- #
# 2. The large-vs-small contrast the kickoff calls out explicitly
# --------------------------------------------------------------------------- #


def test_large_count_yields_larger_class_than_small_count() -> None:
    small = select_compute_class(5_000)          # tiny AOI
    big = select_compute_class(2_000_000)        # huge mesh
    sizing = AWS_BATCH_COMPUTE_CLASS_SIZING
    assert sizing[_COMPUTE_CLASS_ALIAS[small]]["vcpus"] == 4
    assert sizing[_COMPUTE_CLASS_ALIAS[big]]["vcpus"] == 48
    assert (
        sizing[_COMPUTE_CLASS_ALIAS[big]]["vcpus"]
        > sizing[_COMPUTE_CLASS_ALIAS[small]]["vcpus"]
    )


# --------------------------------------------------------------------------- #
# 3. Missing / zero / None / NaN / junk estimate -> standard fallback, no crash
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [None, 0, 0.0, -1, -10_000, float("nan"), "not-a-number", "", object()],
)
def test_missing_or_invalid_estimate_falls_back_to_standard(bad: Any) -> None:
    # MUST NOT raise — the dispatch can never crash on an absent estimate.
    assert select_compute_class(bad) == COMPUTE_CLASS_FALLBACK
    assert COMPUTE_CLASS_FALLBACK == "standard"


def test_numeric_string_estimate_is_coerced() -> None:
    # A stringified count still classifies (defensive coercion).
    assert select_compute_class("5000000") == "xlarge"
    assert select_compute_class("1000") == "small"


# --------------------------------------------------------------------------- #
# 4. The compute-class sizing table (retained for the solve-progress readout)
# --------------------------------------------------------------------------- #


def test_xlarge_tier_present_in_sizing_and_alias() -> None:
    assert "xlarge" in AWS_BATCH_COMPUTE_CLASS_SIZING
    assert "xlarge" in _COMPUTE_CLASS_ALIAS
    sizing = AWS_BATCH_COMPUTE_CLASS_SIZING["xlarge"]
    assert sizing["vcpus"] == 48
    assert sizing["mem_mib"] == 98304  # 96 GiB
    assert sizing["omp_threads"] == 48
    # gpu is unchanged (kept AS-IS per kickoff).
    assert AWS_BATCH_COMPUTE_CLASS_SIZING["gpu"]["vcpus"] == 32


def test_select_then_size_round_trip() -> None:
    """The class select_compute_class returns always resolves in the sizing map."""
    for n in (1_000, 100_000, 500_000, 9_000_000):
        cls = select_compute_class(n)
        sizing = AWS_BATCH_COMPUTE_CLASS_SIZING[_COMPUTE_CLASS_ALIAS[cls]]
        assert sizing["vcpus"] in {4, 8, 16, 48}


# --------------------------------------------------------------------------- #
# 5. model_flood_scenario passes the COMPUTED class to run_solver
# --------------------------------------------------------------------------- #


def _model_setup_with_autoscale(estimated_active_cells: int | None):
    """A ModelSetup-like object carrying autoscale provenance the workflow reads."""
    from trid3nt_contracts import new_ulid
    from trid3nt_contracts.execution import ModelSetup

    autoscale = (
        {"estimated_active_cells": estimated_active_cells, "vcpus": 8}
        if estimated_active_cells is not None
        else None
    )
    return ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="s3://test-cache/cache/setup/manifest.json",
        grid_resolution_m=30.0,
        bbox=(-81.92, 26.55, -81.80, 26.68),
        parameters={"nlcd_vintage_year": 2021, "autoscale": autoscale},
        created_at=datetime.now(timezone.utc),
    )


def _flood_mocks(monkeypatch):
    """Patch the flood workflow's fetcher chain + downstream so only the
    run_solver compute_class hand-off is under test. Returns the captured
    run_solver kwargs holder."""
    from trid3nt_server.workflows import model_flood_scenario as mod
    from trid3nt_contracts import new_ulid
    from trid3nt_contracts.execution import ExecutionHandle, LayerURI, RunResult

    captured: dict[str, Any] = {}
    run_id = new_ulid()

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):  # noqa: ANN001
        captured["compute_class"] = compute_class
        return ExecutionHandle(
            handle_id=new_ulid(),
            run_id=run_id,
            solver=solver,
            compute_class="standard",
            workflows_execution_id="aws-batch:job",
            workflow_name="aws-batch",
            workflow_location="us-west-2",
            submitted_at=datetime.now(timezone.utc),
        )

    async def _fake_wait(handle):  # noqa: ANN001
        return RunResult(
            run_id=run_id,
            handle_id=handle.handle_id,
            status="complete",
            output_uri=f"s3://runs/{run_id}/",
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration_seconds=10.0,
        )

    def _layer(prefix):
        return LayerURI(
            layer_id=f"{prefix}-x",
            name=prefix,
            layer_type="raster",
            uri=f"s3://c/{prefix}.tif",
            style_preset="continuous_dem",
            role="input",
            units="meters",
        )

    flood_layer = LayerURI(
        layer_id=f"flood-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://runs/{run_id}/peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    metrics = {
        "max_depth_m": 1.0,
        "mean_depth_m": 0.4,
        "p95_depth_m": 0.8,
        "flooded_cell_count": 10,
        "crs": "EPSG:3857",
        "units": "meters",
    }
    precip_result = {
        "precip_inches": 12.1,
        "units": "inches",
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14",
        "project_area": "X",
        "source": "noaa-atlas14-pfds",
    }
    landcover_result = {
        "layer": _layer("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }

    patches = [
        patch.object(mod, "fetch_dem", return_value=_layer("dem")),
        patch.object(mod, "fetch_landcover", return_value=landcover_result),
        patch.object(mod, "fetch_river_geometry", return_value=_layer("rivers")),
        patch.object(mod, "lookup_precip_return_period", return_value=precip_result),
        patch.object(mod, "run_solver", side_effect=_fake_run_solver),
        patch.object(mod, "wait_for_completion", side_effect=_fake_wait),
        patch.object(
            mod, "postprocess_flood", return_value=([flood_layer], metrics)
        ),
        patch.object(mod, "publish_layer", return_value="https://wms/x"),
    ]
    return captured, patches


@pytest.mark.asyncio
async def test_flood_workflow_passes_computed_large_class(monkeypatch) -> None:
    """A large estimated_active_cells -> run_solver gets 'large', NOT the
    caller's 'medium' default."""
    from trid3nt_server.workflows import model_flood_scenario as mod

    captured, patches = _flood_mocks(monkeypatch)
    big_setup = _model_setup_with_autoscale(500_000)  # in the LARGE band

    with patch.object(mod, "build_sfincs_model", return_value=big_setup):
        for p in patches:
            p.start()
        try:
            await mod.model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
                return_period_yr=100,
                duration_hr=24,
                compute_class="medium",  # caller default — must be OVERRIDDEN
            )
        finally:
            for p in patches:
                p.stop()

    assert captured["compute_class"] == "large"


@pytest.mark.asyncio
async def test_flood_workflow_passes_computed_xlarge_class(monkeypatch) -> None:
    """A very large estimate reaches the new xlarge tier."""
    from trid3nt_server.workflows import model_flood_scenario as mod

    captured, patches = _flood_mocks(monkeypatch)
    huge_setup = _model_setup_with_autoscale(3_000_000)

    with patch.object(mod, "build_sfincs_model", return_value=huge_setup):
        for p in patches:
            p.start()
        try:
            await mod.model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
                compute_class="medium",
            )
        finally:
            for p in patches:
                p.stop()

    assert captured["compute_class"] == "xlarge"


@pytest.mark.asyncio
async def test_flood_workflow_falls_back_when_no_estimate(monkeypatch) -> None:
    """No autoscale estimate -> the caller's compute_class is used (no crash)."""
    from trid3nt_server.workflows import model_flood_scenario as mod

    captured, patches = _flood_mocks(monkeypatch)
    setup_no_estimate = _model_setup_with_autoscale(None)

    with patch.object(mod, "build_sfincs_model", return_value=setup_no_estimate):
        for p in patches:
            p.start()
        try:
            await mod.model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
                compute_class="medium",
            )
        finally:
            for p in patches:
                p.stop()

    # No estimate -> fall back to the caller's requested class (not crash).
    assert captured["compute_class"] == "medium"


# --------------------------------------------------------------------------- #
# 6. model_urban_flood_swmm passes the COMPUTED class to run_solver
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_swmm_workflow_passes_computed_class_on_out_of_process_lane(
    monkeypatch,
) -> None:
    """When the out-of-process lane is taken (is_local_mode()==False), the
    composer stages a manifest + dispatches run_solver with the class COMPUTED
    from the built mesh's n_active_cells — a large mesh -> a larger class — then
    awaits wait_for_completion and postprocesses from the Batch download."""
    from trid3nt_contracts.swmm_contracts import SWMMDepthLayerURI, SWMMRunArgs

    from trid3nt_server.workflows import model_urban_flood_swmm as mod

    # Build-result stub with a LARGE active-cell count (-> 'large' tier).
    build = SimpleNamespace(
        n_active_cells=400_000,
        inp_path="/tmp/swmm-test/mesh.inp",
        resolution_m=10.0,
        barriers_geojson=None,
    )
    staging = SimpleNamespace(run_id="run-x", inp_path=build.inp_path, build=build)

    peak = SWMMDepthLayerURI(
        layer_id="swmm-depth-peak-run-x",
        name="Peak flood depth",
        layer_type="raster",
        uri="s3://runs/run-x/peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
        max_depth_m=1.0,
        flooded_area_km2=0.5,
        n_buildings_affected=3,
    )
    # The Batch lane never calls run_swmm_local; postprocess consumes the
    # downloaded-out shim instead.
    batch_run_shim = SimpleNamespace(
        out_path="/tmp/swmm-batch-out/mesh.out", continuity_error_pct=0.1
    )

    captured: dict[str, Any] = {}

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):  # noqa: ANN001
        captured["solver"] = solver
        captured["compute_class"] = compute_class
        captured["model_setup_uri"] = model_setup_uri
        return SimpleNamespace(
            run_id="run-x", handle_id="h-x", workflow_name="aws-batch"
        )

    async def _fake_wait(handle, *a, **k):  # noqa: ANN001
        captured["awaited"] = True
        return SimpleNamespace(
            status="complete",
            output_uri="s3://runs/run-x/",
            error_code=None,
            error_message=None,
            cancellation_reason=None,
        )

    run_args = SWMMRunArgs(
        bbox=(-88.0, 36.0, -87.99, 36.01),
        total_rain_depth_mm=120.0,
        storm_duration_hr=1.0,
        target_resolution_m=10.0,
    )

    with (
        patch.object(mod, "build_and_stage_swmm_deck", return_value=staging),
        patch.object(mod, "is_local_mode", return_value=False),
        patch.object(
            mod, "stage_swmm_manifest", return_value="s3://cache/swmm_setup/run-x/manifest.json"
        ),
        patch.object(
            mod,
            "_download_batch_swmm_outputs",
            return_value=(batch_run_shim, "/tmp/swmm-batch-out-run-x"),
        ),
        patch.object(mod, "_cleanup_deck_dir", return_value=None),
        patch.object(mod, "postprocess_swmm", return_value=([peak], {})),
        patch("trid3nt_server.tools.solver.run_solver", side_effect=_fake_run_solver),
        patch(
            "trid3nt_server.tools.solver.wait_for_completion", side_effect=_fake_wait
        ),
    ):
        result = await mod.model_urban_flood_swmm(
            run_args,
            dem_path="/tmp/dem.tif",  # skip the fetch
            building_footprints=None,
            run_id="run-x",
            compute_class="standard",  # caller default — must be OVERRIDDEN
            cleanup_deck=False,
        )

    # job AGENT-AOI (#159): the composer stamps the FLOORED AOI onto the returned
    # peak (here the stub peak carried no bbox), so identity is no longer the
    # invariant - content + the authoritative AOI are. The input bbox is already
    # above the urban floor, so the floored AOI == the input bbox.
    assert result.layer_id == peak.layer_id
    assert result.uri == peak.uri
    assert result.max_depth_m == peak.max_depth_m
    assert result.flooded_area_km2 == peak.flooded_area_km2
    assert result.n_buildings_affected == peak.n_buildings_affected
    assert tuple(result.bbox) == tuple(run_args.bbox)  # floored AOI stamped on
    assert captured["solver"] == "swmm"
    assert captured["compute_class"] == "large"  # 400k cells -> large tier
    # The out-of-process lane now stages an s3:// manifest (not file://) and
    # routes through wait_for_completion (the generic Batch seam).
    assert captured["model_setup_uri"].startswith("s3://")
    assert captured["awaited"] is True
