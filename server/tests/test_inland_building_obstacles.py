"""Inland (regular-grid) building-obstacles OPT-IN wiring tests.

Building obstacles let the SFINCS 2D flood route AROUND building footprints for
a rough urban-flood estimate. They are OPT-IN and default OFF. The workflow body
(``model_flood_scenario``) already resolves + threads ``building_obstacles`` on
the SHARED regular-grid build (coastal AND inland), and ``sfincs_builder`` already
emits the ``exclude_mask`` on the plain regular grid (no subgrid required). The
real gap closed here is the LLM-facing wrapper ``run_model_flood_scenario``: it
now EXPOSES ``building_obstacles`` + ``building_obstacle_mode`` and threads them
into the inner workflow call.

These tests PROVE, with NO live solve:

1. Wrapper passthrough — ``run_model_flood_scenario(building_obstacles=...)``
   forwards ``building_obstacles`` + ``building_obstacle_mode`` into the inner
   ``model_flood_scenario`` call.
2. Inland honors buildings — an INLAND (``coastal=False, quadtree=False``) run
   with ``building_obstacles=True`` fetches OSM footprints (mocked
   ``fetch_buildings``) and hands ``build_sfincs_model`` a ``BuildOptions`` with
   ``building_obstacle_uri`` set AND ``enable_subgrid=True``.
3. Default OFF (regression) — the same inland run with ``building_obstacles``
   unset / ``False`` does NOT fetch buildings and hands ``build_sfincs_model`` a
   ``BuildOptions`` with ``building_obstacle_uri is None`` and
   ``enable_subgrid=False`` (byte-identical to the v0.1 pluvial deck path).
4. Honest degrade — when the OSM ``fetch_buildings`` fetch FAILS, the flood does
   NOT abort: ``building_obstacle_uri`` resolves to ``None`` and the build runs
   without obstacles (a warning is logged, no exception escapes).
5. Builder emission — ``build_sfincs_model`` (via
   ``_generate_hydromt_yaml_config``) emits the regular-grid
   ``exclude_mask`` + ``all_touched`` under ``setup_mask_active`` when given a
   building URI in ``"exclude"`` mode, the ``"raise"`` mode burns
   ``datasets_riv`` under ``setup_subgrid``, and the default (no URI) deck emits
   NEITHER (the regression-critical byte-identical baseline).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grace2_agent.workflows.model_flood_scenario import (
    model_flood_scenario,
    run_model_flood_scenario,
)
from grace2_agent.workflows.sfincs_builder import (
    BuildOptions,
    ForcingSpec,
    ModelSetup,
    _generate_hydromt_yaml_config,
)
from grace2_contracts import new_ulid
from grace2_contracts.envelope import AssessmentEnvelope
from grace2_contracts.execution import ExecutionHandle, LayerURI, RunResult


# Inland (NON-coastal) AOI — a high-and-dry city core, no shoreline.
_INLAND_BBOX = (-86.82, 36.14, -86.76, 36.18)  # downtown Nashville-ish

_FORCING = ForcingSpec(
    forcing_type="pluvial_synthetic",
    precip_inches=8.0,
    duration_hours=24.0,
    return_period_years=100,
)


def _mock_layer_uri(prefix: str) -> LayerURI:
    return LayerURI(
        layer_id=f"{prefix}-{new_ulid()}",
        name=f"{prefix} layer",
        layer_type="raster",
        uri=f"gs://test-cache/{prefix}.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
    )


def _buildings_layer() -> LayerURI:
    return LayerURI(
        layer_id=f"buildings-{new_ulid()}",
        name="OSM building footprints",
        layer_type="vector",
        uri="gs://test-cache/osm_buildings.fgb",
        style_preset="vector_outline",
        role="context",
    )


def _make_handle(run_id: str) -> ExecutionHandle:
    return ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
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


# --------------------------------------------------------------------------- #
# CHANGE 3a — wrapper passthrough: building_obstacles + mode reach the workflow
# --------------------------------------------------------------------------- #


def _empty_envelope_stub() -> MagicMock:
    """A stand-in workflow return: no layers (forces the wrapper's dict branch).

    The wrapper only reads ``.layers`` (empty → ``model_dump`` fallback), so we
    avoid constructing a fully-validated AssessmentEnvelope (which would need a
    populated flood subtype) and keep these tests focused on arg forwarding.
    """
    env = MagicMock()
    env.layers = []
    env.model_dump.return_value = {"envelope_type": "modeled", "layers": []}
    return env


@pytest.mark.asyncio
async def test_wrapper_forwards_building_obstacles_to_workflow() -> None:
    """run_model_flood_scenario(building_obstacles=True) forwards into the call."""
    fake_envelope = _empty_envelope_stub()
    with patch(
        "grace2_agent.workflows.model_flood_scenario.model_flood_scenario",
        new=AsyncMock(return_value=fake_envelope),
    ) as mock_wf:
        await run_model_flood_scenario(
            bbox=_INLAND_BBOX,
            return_period_yr=100,
            duration_hr=24,
            building_obstacles=True,
            building_obstacle_mode="exclude",
        )
    assert mock_wf.await_count == 1
    kwargs = mock_wf.await_args.kwargs
    assert kwargs["building_obstacles"] is True
    assert kwargs["building_obstacle_mode"] == "exclude"
    # Inland defaults are preserved (no coastal / quadtree coupling).
    assert kwargs["coastal"] is False
    assert kwargs["quadtree"] is False


@pytest.mark.asyncio
async def test_wrapper_default_off_forwards_false() -> None:
    """Default (no building_obstacles kwarg) forwards building_obstacles=False."""
    fake_envelope = _empty_envelope_stub()
    with patch(
        "grace2_agent.workflows.model_flood_scenario.model_flood_scenario",
        new=AsyncMock(return_value=fake_envelope),
    ) as mock_wf:
        await run_model_flood_scenario(bbox=_INLAND_BBOX)
    kwargs = mock_wf.await_args.kwargs
    assert kwargs["building_obstacles"] is False
    assert kwargs["building_obstacle_mode"] == "exclude"


@pytest.mark.asyncio
async def test_wrapper_forwards_string_obstacle_uri() -> None:
    """A verbatim footprint URI string is forwarded unchanged."""
    fake_envelope = _empty_envelope_stub()
    uri = "gs://my-bucket/prior_buildings.fgb"
    with patch(
        "grace2_agent.workflows.model_flood_scenario.model_flood_scenario",
        new=AsyncMock(return_value=fake_envelope),
    ) as mock_wf:
        await run_model_flood_scenario(bbox=_INLAND_BBOX, building_obstacles=uri)
    assert mock_wf.await_args.kwargs["building_obstacles"] == uri


# --------------------------------------------------------------------------- #
# Helpers for the full inland-workflow runs
# --------------------------------------------------------------------------- #


def _inland_chain_patches(build_sfincs_mock):  # noqa: ANN001, ANN201 — test helper
    """Patch the inland fetch+solve chain so model_flood_scenario reaches the
    build seam without touching the network. ``build_sfincs_mock`` captures the
    BuildOptions handed to build_sfincs_model.
    """
    run_id = new_ulid()
    handle = _make_handle(run_id)
    model_setup = ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri="gs://test-cache/sfincs_setup/test/",
        grid_resolution_m=30.0,
        bbox=_INLAND_BBOX,
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
    )
    run_result_ok = RunResult(
        run_id=run_id,
        handle_id=handle.handle_id,
        status="complete",
        output_uri=f"gs://test-runs/{run_id}/",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        duration_seconds=12.0,
    )
    landcover_result = {
        "layer": _mock_layer_uri("landcover"),
        "nlcd_vintage_year": 2021,
        "dataset": "nlcd_2021",
        "source": "mrlc-wms",
    }
    precip_result = {
        "precip_inches": 8.0,
        "units": "inches",
        "location": [36.16, -86.79],
        "return_period_years": 100,
        "duration_hours": 24.0,
        "vintage_volume": "NOAA Atlas 14",
        "project_area": "Ohio River Basin",
        "source": "noaa-atlas14-pfds",
    }
    flood_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"gs://test-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )
    depth_metrics = {
        "max_depth_m": 1.2,
        "mean_depth_m": 0.3,
        "p95_depth_m": 0.9,
        "flooded_cell_count": 4242,
        "crs": "EPSG:3857",
        "units": "meters",
    }
    wms_url = "https://qgis.test/ogc/wms?LAYERS=flood"

    async def _wfc(_handle):  # noqa: ANN001
        return run_result_ok

    mod = "grace2_agent.workflows.model_flood_scenario"
    return [
        patch(f"{mod}.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch(f"{mod}.fetch_landcover", return_value=landcover_result),
        patch(f"{mod}.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch(f"{mod}.lookup_precip_return_period", return_value=precip_result),
        patch(f"{mod}.build_sfincs_model", side_effect=build_sfincs_mock),
        patch(f"{mod}.run_solver", return_value=handle),
        patch(f"{mod}.wait_for_completion", side_effect=_wfc),
        patch(f"{mod}.postprocess_flood", return_value=([flood_layer], depth_metrics)),
        patch(f"{mod}.publish_layer", return_value=wms_url),
    ], model_setup


# --------------------------------------------------------------------------- #
# CHANGE 3b — inland run with building_obstacles=True burns footprints
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inland_with_buildings_passes_uri_and_subgrid() -> None:
    """Inland building_obstacles=True → fetch + build_sfincs_model gets URI+subgrid."""
    captured: dict[str, BuildOptions] = {}

    def _build(**kwargs):  # noqa: ANN003
        captured["options"] = kwargs["options"]
        return model_setup

    patches, model_setup = _inland_chain_patches(_build)

    import contextlib

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        # fetch_buildings is a LOCAL import inside _resolve_building_obstacle_uri,
        # so patch it at its source module.
        stack.enter_context(
            patch(
                "grace2_agent.tools.data_fetch.fetch_buildings",
                return_value=_buildings_layer(),
            )
        )
        envelope = await model_flood_scenario(
            bbox=_INLAND_BBOX,
            return_period_yr=100,
            duration_hr=24,
            coastal=False,
            quadtree=False,
            building_obstacles=True,
            building_obstacle_mode="exclude",
        )

    assert isinstance(envelope, AssessmentEnvelope)
    opts = captured["options"]
    assert opts.building_obstacle_uri == "gs://test-cache/osm_buildings.fgb"
    assert opts.building_obstacle_mode == "exclude"
    # Subgrid is auto-enabled when buildings are present.
    assert opts.enable_subgrid is True


# --------------------------------------------------------------------------- #
# CHANGE 3c — default OFF: inland run does NOT fetch / burn buildings
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inland_default_off_no_buildings_no_subgrid() -> None:
    """Default inland run: no fetch_buildings, no obstacle URI, no subgrid."""
    captured: dict[str, BuildOptions] = {}

    def _build(**kwargs):  # noqa: ANN003
        captured["options"] = kwargs["options"]
        return model_setup

    patches, model_setup = _inland_chain_patches(_build)

    import contextlib

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        fb = stack.enter_context(
            patch(
                "grace2_agent.tools.data_fetch.fetch_buildings",
                return_value=_buildings_layer(),
            )
        )
        await model_flood_scenario(
            bbox=_INLAND_BBOX,
            return_period_yr=100,
            duration_hr=24,
            coastal=False,
            quadtree=False,
            # building_obstacles unset → default False
        )

    # No footprint fetch happened at all (default OFF).
    assert fb.call_count == 0
    opts = captured["options"]
    assert opts.building_obstacle_uri is None
    # Byte-identical to the v0.1 pluvial deck path — subgrid stays OFF.
    assert opts.enable_subgrid is False


# --------------------------------------------------------------------------- #
# CHANGE 3d — honest degrade: OSM fetch fails → no obstacles, flood still runs
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_inland_buildings_fetch_failure_degrades_to_no_obstacles() -> None:
    """fetch_buildings raises → building_obstacle_uri None, build still runs."""
    captured: dict[str, BuildOptions] = {}

    def _build(**kwargs):  # noqa: ANN003
        captured["options"] = kwargs["options"]
        return model_setup

    patches, model_setup = _inland_chain_patches(_build)

    import contextlib

    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "grace2_agent.tools.data_fetch.fetch_buildings",
                side_effect=RuntimeError("Overpass 504 gateway timeout"),
            )
        )
        envelope = await model_flood_scenario(
            bbox=_INLAND_BBOX,
            return_period_yr=100,
            duration_hr=24,
            coastal=False,
            quadtree=False,
            building_obstacles=True,
        )

    # The flood did NOT abort — an envelope came back.
    assert isinstance(envelope, AssessmentEnvelope)
    opts = captured["options"]
    # Degraded: no obstacles burned, and no subgrid auto-enabled (URI is None).
    assert opts.building_obstacle_uri is None
    assert opts.enable_subgrid is False


# --------------------------------------------------------------------------- #
# CHANGE 3e — builder deck emission (regular grid)
# --------------------------------------------------------------------------- #


def test_builder_exclude_mode_emits_exclude_mask_no_subgrid() -> None:
    """exclude mode on the REGULAR grid emits exclude_mask + all_touched, no subgrid."""
    yaml_text = _generate_hydromt_yaml_config(
        bbox=_INLAND_BBOX,
        options=BuildOptions(
            grid_resolution_m=30.0,
            simulation_hours=24.0,
            building_obstacle_uri="/tmp/osm_buildings.fgb",
            building_obstacle_mode="exclude",
            enable_subgrid=False,  # exclude works WITHOUT subgrid
        ),
        dem_local_path="/tmp/dep.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=_FORCING,
        mapping_csv_path="/tmp/manning.csv",
    )
    assert "setup_mask_active:" in yaml_text
    assert "exclude_mask: '/tmp/osm_buildings.fgb'" in yaml_text
    assert "all_touched: true" in yaml_text
    # The exclude path needs NO subgrid block.
    assert "setup_subgrid:" not in yaml_text


def test_builder_default_off_emits_no_obstacle_blocks() -> None:
    """No building URI → NO exclude_mask AND NO subgrid (byte-identical baseline)."""
    yaml_text = _generate_hydromt_yaml_config(
        bbox=_INLAND_BBOX,
        options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
        dem_local_path="/tmp/dep.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=_FORCING,
        mapping_csv_path="/tmp/manning.csv",
    )
    assert "exclude_mask" not in yaml_text
    assert "all_touched" not in yaml_text
    assert "setup_subgrid:" not in yaml_text
    # The plain pluvial deck path is intact.
    assert "setup_dep:" in yaml_text
    assert "setup_manning_roughness:" in yaml_text


def test_builder_raise_mode_emits_datasets_riv_under_subgrid() -> None:
    """raise mode (requires subgrid) burns footprints as datasets_riv raised banks."""
    yaml_text = _generate_hydromt_yaml_config(
        bbox=_INLAND_BBOX,
        options=BuildOptions(
            grid_resolution_m=30.0,
            simulation_hours=24.0,
            building_obstacle_uri="/tmp/osm_buildings.fgb",
            building_obstacle_mode="raise",
            enable_subgrid=True,  # raise requires subgrid
        ),
        dem_local_path="/tmp/dep.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=_FORCING,
        mapping_csv_path="/tmp/manning.csv",
    )
    assert "setup_subgrid:" in yaml_text
    assert "datasets_riv:" in yaml_text
    assert "/tmp/osm_buildings.fgb" in yaml_text
    # raise keeps cells active — it does NOT carve an exclude_mask.
    assert "exclude_mask" not in yaml_text
