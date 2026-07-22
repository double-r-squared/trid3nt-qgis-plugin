"""Unit tests for the COASTAL-AOI branch in ``model_flood_scenario`` (SFINCS
North Star P1 — fetch_topobathy wire seam).

The workflow routes the terrain fetch through ``fetch_topobathy`` (a SEAMLESS
land-plus-seafloor DEM merging USGS 3DEP land with NOAA NCEI CUDEM bathymetry)
INSTEAD of the land-only ``fetch_dem`` when the AOI is coastal. The coastal
signal is an explicit ``coastal=True`` flag OR the presence of ``surge_forcing``
(a water-level boundary is physically incoherent without a nearshore bed).

These tests prove (all mocked — no network, no GDAL, no solver):

1. ``coastal=True`` → ``fetch_topobathy`` is called and ``fetch_dem`` is NOT
   (the merged topobathy COG flows into ``build_sfincs_model(dem_uri=...)``).
2. ``surge_forcing`` present → coastal auto-implied → ``fetch_topobathy`` called.
3. The NON-coastal path (no flag, no surge) still calls ``fetch_dem`` and NEVER
   ``fetch_topobathy`` (regression — v0.1 land/pluvial path unchanged).
4. The topobathy COG's ``.uri`` is the one handed to ``build_sfincs_model``.
5. A bathymetry-ABSENT topobathy fallback still completes (degrade, not abort).
6. A hard ``TopobathyError`` surfaces as a typed failed envelope (error_code
   threaded), never raises.
7. The LLM-facing ``run_model_flood_scenario`` forwards ``coastal`` through.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from grace2_agent.tools.fetch_topobathy import (
    TopobathyResult,
    TopobathyUpstreamError,
)
from grace2_agent.workflows.model_flood_scenario import (
    model_flood_scenario,
    run_model_flood_scenario,
)
from grace2_contracts import new_ulid
from grace2_contracts.envelope import (
    AssessmentEnvelope,
    FloodMetrics,
    FloodPayload,
    Provenance,
)
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult


# --------------------------------------------------------------------------- #
# Test domains
# --------------------------------------------------------------------------- #

# Coastal AOI — Florida panhandle / Mexico Beach (the SFINCS North Star demo).
_COASTAL_BBOX = (-85.75, 29.55, -85.25, 30.20)
# Inland AOI — Idaho (Case 3; no coast, pure pluvial).
_INLAND_BBOX = (-116.30, 43.55, -116.10, 43.70)


# --------------------------------------------------------------------------- #
# Mock builders (mirror test_model_flood_scenario_v2.py)
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


def _topobathy_result(
    *, bathymetry_present: bool = True, cudem_tile_count: int = 3
) -> TopobathyResult:
    """A merged-topobathy LayerURI subclass — byte-contract identical to
    ``fetch_dem``'s LayerURI plus the honesty fields."""
    return TopobathyResult(
        layer_id="topobathy-test",
        name="Merged topo-bathymetry (3DEP + CUDEM)",
        layer_type="raster",
        # A distinct URI so we can prove THIS is what build_sfincs_model gets.
        uri="gs://test-cache/cache/static-30d/topobathy/coastal-test.tif",
        style_preset="continuous_dem",
        role="input",
        units="meters",
        bathymetry_present=bathymetry_present,
        cudem_tile_count=cudem_tile_count,
        fallback_warning=(
            None
            if bathymetry_present
            else "BATHYMETRY ABSENT: the elevation surface is 3DEP LAND-ONLY"
        ),
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
        "location": [29.9, -85.5],
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
        setup_uri="gs://test-cache/cache/static-30d/sfincs_setup/test/manifest.json",
        grid_resolution_m=30.0,
        bbox=_COASTAL_BBOX,
        parameters={"nlcd_vintage_year": 2021},
        created_at=datetime.now(timezone.utc),
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
    return LayerURI(
        layer_id=f"flood-depth-peak-{run_id}",
        name="Flood Depth (peak)",
        layer_type="raster",
        uri=f"s3://trid3nt-runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth",
        role="primary",
        units="meters",
    )


_DEPTH_METRICS = {
    "max_depth_m": 1.8,
    "mean_depth_m": 0.4,
    "p95_depth_m": 1.2,
    "flooded_cell_count": 8_000,
    "crs": "EPSG:3857",
    "units": "meters",
}


def _empty_envelope(bbox: tuple[float, float, float, float]) -> AssessmentEnvelope:
    """A minimal valid (layer-less) AssessmentEnvelope for wrapper tests."""
    now = datetime.now(timezone.utc)
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=new_ulid(),
        session_id=new_ulid(),
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name="model_flood_scenario",
        bbox=bbox,
        crs="EPSG:4326",
        layers=[],
        provenance=Provenance(data_sources=[]),
        created_at=now,
        completed_at=now,
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=0.0,
                max_depth_m=0.0,
                mean_depth_m=0.0,
                p95_depth_m=0.0,
                solver_version="test:wrapper",
                grid_resolution_m=30.0,
                simulation_duration_hours=24,
            )
        ),
    )


class _Captured(dict):
    """Captures the dem_uri build_sfincs_model received."""


def _patched_chain(
    captured: _Captured,
    *,
    dem_mock,
    topobathy_mock,
    run_id: str,
    handle: ExecutionHandle,
):
    """Return the standard with-block patch tuple used by every coastal test.

    ``dem_mock`` / ``topobathy_mock`` are the (Mock / side_effect) bound to
    ``fetch_dem`` / ``fetch_topobathy`` so each test asserts call counts.
    """

    def _capture_build(*_args, **kwargs):  # noqa: ANN002, ANN003
        captured["dem_uri"] = kwargs["dem_uri"]
        captured["forcing"] = kwargs["forcing"]
        return _model_setup()

    async def _wfc(_h):  # noqa: ANN001
        return _run_result_ok(run_id, handle.handle_id)

    from grace2_agent.tools.publish_layer import PublishLayerError

    return (
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", dem_mock),
        patch(
            "grace2_agent.workflows.model_flood_scenario.fetch_topobathy",
            topobathy_mock,
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
            side_effect=_capture_build,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.run_solver",
            return_value=handle,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.wait_for_completion",
            side_effect=_wfc,
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=([_flood_layer(run_id)], _DEPTH_METRICS),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario.publish_layer",
            side_effect=PublishLayerError("JOBS_CLIENT_UNAVAILABLE", "no qgis in test"),
        ),
        # OFFLINE: the coastal auto-wire's live surge fetchers (CO-OPS/GTSM)
        # must never hit the network or ambient object storage from a unit
        # test - stub both so the ladder degrades to the parametric
        # design-storm surge (rung 3, key-free and fully offline).
        patch(
            "grace2_agent.tools.fetch_noaa_coops_tides.fetch_noaa_coops_tides",
            side_effect=RuntimeError("offline test - no live CO-OPS"),
        ),
        patch(
            "grace2_agent.tools.fetch_gtsm_tide_surge.fetch_gtsm_tide_surge",
            side_effect=RuntimeError("offline test - no live GTSM"),
        ),
    )


# --------------------------------------------------------------------------- #
# 1. coastal=True → fetch_topobathy (NOT fetch_dem)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_coastal_flag_routes_to_fetch_topobathy() -> None:
    """``coastal=True`` calls fetch_topobathy and NOT fetch_dem; the merged
    topobathy COG is the dem_uri handed to build_sfincs_model."""
    from unittest.mock import MagicMock

    captured = _Captured()
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    dem_mock = MagicMock(return_value=_mock_layer_uri("dem"))
    topobathy_mock = MagicMock(return_value=_topobathy_result())

    for p in _patched_chain(
        captured,
        dem_mock=dem_mock,
        topobathy_mock=topobathy_mock,
        run_id=run_id,
        handle=handle,
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX,
            coastal=True,
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        patch.stopall()

    topobathy_mock.assert_called_once()
    dem_mock.assert_not_called()
    # The merged topobathy COG is what build_sfincs_model consumes.
    assert captured["dem_uri"] == (
        "gs://test-cache/cache/static-30d/topobathy/coastal-test.tif"
    )
    assert isinstance(envelope, AssessmentEnvelope)
    # CUDEM merge appears in the provenance.
    assert any("CUDEM" in ds.name for ds in envelope.provenance.data_sources)


# --------------------------------------------------------------------------- #
# 2. surge_forcing present → coastal auto-implied → fetch_topobathy
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_surge_forcing_auto_implies_coastal() -> None:
    """A ``surge_forcing`` water-level boundary auto-routes to fetch_topobathy
    even without an explicit ``coastal=True`` (a surge needs a nearshore bed)."""
    from unittest.mock import MagicMock

    captured = _Captured()
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    dem_mock = MagicMock(return_value=_mock_layer_uri("dem"))
    topobathy_mock = MagicMock(return_value=_topobathy_result())

    for p in _patched_chain(
        captured,
        dem_mock=dem_mock,
        topobathy_mock=topobathy_mock,
        run_id=run_id,
        handle=handle,
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX,
            # NO explicit coastal flag — surge_forcing presence implies it.
            surge_forcing={
                "waterlevel": {
                    "timeseries_uri": "/tmp/wl.csv",
                    "locations_uri": "/tmp/bnd.fgb",
                }
            },
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        patch.stopall()

    topobathy_mock.assert_called_once()
    dem_mock.assert_not_called()
    assert isinstance(envelope, AssessmentEnvelope)


# --------------------------------------------------------------------------- #
# 3. NON-coastal path → fetch_dem (regression: fetch_topobathy NEVER called)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_noncoastal_path_still_calls_fetch_dem() -> None:
    """Default (no coastal flag, no surge) keeps the v0.1 land/pluvial path:
    fetch_dem is called and fetch_topobathy is NEVER touched."""
    from unittest.mock import MagicMock

    captured = _Captured()
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    dem_layer = _mock_layer_uri("dem")
    dem_mock = MagicMock(return_value=dem_layer)
    topobathy_mock = MagicMock(return_value=_topobathy_result())

    for p in _patched_chain(
        captured,
        dem_mock=dem_mock,
        topobathy_mock=topobathy_mock,
        run_id=run_id,
        handle=handle,
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_INLAND_BBOX,
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        patch.stopall()

    dem_mock.assert_called_once()
    topobathy_mock.assert_not_called()
    # build_sfincs_model received the LAND DEM, not a topobathy COG.
    assert captured["dem_uri"] == dem_layer.uri
    assert isinstance(envelope, AssessmentEnvelope)
    # The land DEM provenance is the plain USGS 3DEP source.
    assert any(ds.name == "USGS 3DEP" for ds in envelope.provenance.data_sources)
    assert not any("CUDEM" in ds.name for ds in envelope.provenance.data_sources)


@pytest.mark.asyncio
async def test_coastal_false_explicit_is_land_path() -> None:
    """Explicit ``coastal=False`` is identical to the default land path."""
    from unittest.mock import MagicMock

    captured = _Captured()
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    dem_mock = MagicMock(return_value=_mock_layer_uri("dem"))
    topobathy_mock = MagicMock(return_value=_topobathy_result())

    for p in _patched_chain(
        captured,
        dem_mock=dem_mock,
        topobathy_mock=topobathy_mock,
        run_id=run_id,
        handle=handle,
    ):
        p.start()
    try:
        await model_flood_scenario(
            bbox=_INLAND_BBOX,
            coastal=False,
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        patch.stopall()

    dem_mock.assert_called_once()
    topobathy_mock.assert_not_called()


# --------------------------------------------------------------------------- #
# 4. Bathymetry-ABSENT topobathy fallback still completes (degrade, not abort)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_coastal_bathy_absent_fallback_completes() -> None:
    """fetch_topobathy degrading to a 3DEP-land-only surface
    (bathymetry_present=False) does NOT abort the coastal run; the workflow
    completes and the honest fallback provenance label is carried."""
    from unittest.mock import MagicMock

    captured = _Captured()
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    dem_mock = MagicMock(return_value=_mock_layer_uri("dem"))
    topobathy_mock = MagicMock(
        return_value=_topobathy_result(bathymetry_present=False, cudem_tile_count=0)
    )

    for p in _patched_chain(
        captured,
        dem_mock=dem_mock,
        topobathy_mock=topobathy_mock,
        run_id=run_id,
        handle=handle,
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX,
            coastal=True,
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        patch.stopall()

    topobathy_mock.assert_called_once()
    dem_mock.assert_not_called()
    assert isinstance(envelope, AssessmentEnvelope)
    # The fallback (bathymetry ABSENT) provenance label is surfaced honestly.
    assert any(
        "fallback" in ds.name.lower() or "bathymetry absent" in ds.name.lower()
        for ds in envelope.provenance.data_sources
    )


# --------------------------------------------------------------------------- #
# 5. Hard TopobathyError → typed failed envelope (never raises)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_coastal_topobathy_hard_error_threads_into_failed_envelope() -> None:
    """A hard TopobathyError (no CUDEM AND no 3DEP, upstream wedge) surfaces as a
    typed failed envelope with the error_code threaded — never raises out of the
    workflow (caller-friendly; Invariant 7 honest failure)."""
    from unittest.mock import MagicMock

    captured = _Captured()
    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)
    dem_mock = MagicMock(return_value=_mock_layer_uri("dem"))
    topobathy_mock = MagicMock(
        side_effect=TopobathyUpstreamError("CUDEM tile read wedged")
    )

    for p in _patched_chain(
        captured,
        dem_mock=dem_mock,
        topobathy_mock=topobathy_mock,
        run_id=run_id,
        handle=handle,
    ):
        p.start()
    try:
        envelope = await model_flood_scenario(
            bbox=_COASTAL_BBOX,
            coastal=True,
            return_period_yr=100,
            duration_hr=24,
        )
    finally:
        patch.stopall()

    topobathy_mock.assert_called_once()
    # The workflow did NOT raise — it returned a failed envelope.
    assert isinstance(envelope, AssessmentEnvelope)
    # No flood layer on the failed path.
    assert envelope.layers == []
    # The TopobathyUpstreamError.error_code is threaded into the envelope's
    # flood metrics solver_version (failed:<CODE>).
    assert envelope.flood is not None
    assert "TOPOBATHY_UPSTREAM_ERROR" in (
        envelope.flood.metrics.solver_version or ""
    )


# --------------------------------------------------------------------------- #
# 6. LLM-facing wrapper forwards the coastal flag
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_wrapper_forwards_coastal_flag() -> None:
    """``run_model_flood_scenario(coastal=True)`` forwards the flag into the
    inner workflow (so the LLM can request a coastal run)."""

    captured_kwargs: dict = {}

    async def _fake_inner(**kwargs):  # noqa: ANN003
        captured_kwargs.update(kwargs)
        return _empty_envelope(_COASTAL_BBOX)

    with patch(
        "grace2_agent.workflows.model_flood_scenario.model_flood_scenario",
        side_effect=_fake_inner,
    ):
        await run_model_flood_scenario(bbox=_COASTAL_BBOX, coastal=True)

    assert captured_kwargs.get("coastal") is True


@pytest.mark.asyncio
async def test_run_wrapper_coastal_defaults_false() -> None:
    """``run_model_flood_scenario`` defaults coastal=False (land path)."""

    captured_kwargs: dict = {}

    async def _fake_inner(**kwargs):  # noqa: ANN003
        captured_kwargs.update(kwargs)
        return _empty_envelope(_INLAND_BBOX)

    with patch(
        "grace2_agent.workflows.model_flood_scenario.model_flood_scenario",
        side_effect=_fake_inner,
    ):
        await run_model_flood_scenario(bbox=_INLAND_BBOX)

    assert captured_kwargs.get("coastal") is False
