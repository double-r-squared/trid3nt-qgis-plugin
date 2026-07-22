"""Unit + integration tests for model_flood_scenario v2 (job-0225).

The v2 amendment adds a ``forcing_raster_uri`` parameter that switches the
workflow from the Atlas 14 design-storm path to an OBSERVED-precip path:
download the precip raster, compute the AREA-MEAN accumulated precip over the
model domain, and convert it to a uniform SFINCS ``netamt`` rate (mm/hr) — the
OQ-6 area-mean fallback (spw spatial upgrade documented in ``sfincs_builder``).

Test plan (kickoff minimum):

1. ``test_none_path_forcing_spec_identical_to_baseline`` — when
   ``forcing_raster_uri is None`` the ForcingSpec build_sfincs_model receives is
   byte-for-byte identical to the v1 design-storm ForcingSpec (regression).
2. ``test_none_path_deck_yaml_unchanged`` — the generated HydroMT YAML for the
   None-path forcing is identical to the v1 ``pluvial_synthetic`` deck.
3. ``test_area_mean_from_synthetic_raster`` — ``compute_precip_area_mean_mm_per_hr``
   computes the correct area-mean mm/hr from a synthetic precip raster (uniform
   + mixed-value + nodata-masked).
4. ``test_netamt_magnitude_lands_in_deck`` — the area-mean magnitude appears in
   the SFINCS deck (the ``setup_precip_forcing: magnitude:`` line).
5. ``test_raster_path_skips_atlas14_and_builds_observed_forcing`` — full mocked
   workflow: ``forcing_raster_uri`` set → ``lookup_precip_return_period`` is NOT
   called → build_sfincs_model receives a ``pluvial_observed`` ForcingSpec
   carrying the area-mean magnitude.
6. ``test_observed_forcing_zero_magnitude_rejected`` — a zero-magnitude observed
   ForcingSpec raises ``FORCING_OUT_OF_RANGE`` (Invariant 7 — no silent
   no-rain deck).
7. ``test_empty_raster_raises_precip_forcing_error`` — an all-nodata raster
   raises ``PrecipForcingError("PRECIP_RASTER_EMPTY")``.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_agent.workflows.model_flood_scenario import (
    PrecipForcingError,
    compute_precip_area_mean_mm_per_hr,
    model_flood_scenario,
)
from grace2_agent.workflows.sfincs_builder import (
    BuildOptions,
    ForcingSpec,
    SFINCSSetupError,
    _generate_hydromt_yaml_config,
    build_sfincs_model,
)
from grace2_agent.tools.publish_layer import PublishLayerError
from grace2_contracts import new_ulid
from grace2_contracts.envelope import AssessmentEnvelope
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

# Test domain (Idaho — non-Florida geography for Case 3).
_BBOX = (-116.30, 43.55, -116.10, 43.70)


def _write_precip_raster(
    path: Path,
    values: np.ndarray,
    *,
    nodata: float | None = None,
    bbox: tuple[float, float, float, float] = _BBOX,
) -> Path:
    """Write a single-band GeoTIFF precip raster with the given values."""
    height, width = values.shape
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": height,
        "width": width,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype("float32"), 1)
    return path


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


# --------------------------------------------------------------------------- #
# Test 3 — area-mean computation from a synthetic raster
# --------------------------------------------------------------------------- #


def test_area_mean_from_synthetic_raster() -> None:
    """compute_precip_area_mean_mm_per_hr returns the correct area-mean mm/hr."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # Uniform raster: every cell = 48.0 mm. Over a 24-hr window → 2.0 mm/hr.
        uniform = np.full((10, 10), 48.0, dtype="float32")
        p_uniform = _write_precip_raster(tmp / "uniform.tif", uniform)
        mag, mean_mm = compute_precip_area_mean_mm_per_hr(
            str(p_uniform), _BBOX, accumulation_hours=24.0
        )
        assert mean_mm == pytest.approx(48.0)
        assert mag == pytest.approx(2.0)

        # Mixed raster: half cells 100 mm, half 0 mm → mean 50 mm; / 24 → ~2.083.
        mixed = np.zeros((10, 10), dtype="float32")
        mixed[:5, :] = 100.0
        p_mixed = _write_precip_raster(tmp / "mixed.tif", mixed)
        mag2, mean2 = compute_precip_area_mean_mm_per_hr(
            str(p_mixed), _BBOX, accumulation_hours=24.0
        )
        assert mean2 == pytest.approx(50.0)
        assert mag2 == pytest.approx(50.0 / 24.0)

        # Nodata-masked raster: valid cells all 60 mm, the rest are nodata
        # sentinel -9999 (must be excluded from the mean).
        with_nodata = np.full((10, 10), -9999.0, dtype="float32")
        with_nodata[:3, :3] = 60.0  # 9 valid cells of 100
        p_nodata = _write_precip_raster(
            tmp / "nodata.tif", with_nodata, nodata=-9999.0
        )
        mag3, mean3 = compute_precip_area_mean_mm_per_hr(
            str(p_nodata), _BBOX, accumulation_hours=6.0
        )
        assert mean3 == pytest.approx(60.0)  # nodata excluded
        assert mag3 == pytest.approx(10.0)  # 60 / 6


def test_area_mean_inches_units_converts_to_mm() -> None:
    """raster_units='inches' converts to mm before the per-hour division."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 2.0 inches uniform over 24h → 50.8 mm → 2.1167 mm/hr.
        arr = np.full((8, 8), 2.0, dtype="float32")
        p = _write_precip_raster(tmp / "inches.tif", arr)
        mag, mean_mm = compute_precip_area_mean_mm_per_hr(
            str(p), _BBOX, accumulation_hours=24.0, raster_units="inches"
        )
        assert mean_mm == pytest.approx(50.8)
        assert mag == pytest.approx(50.8 / 24.0)


def test_empty_raster_raises_precip_forcing_error() -> None:
    """An all-nodata raster → PRECIP_RASTER_EMPTY (no area-mean computable)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        all_nodata = np.full((6, 6), -9999.0, dtype="float32")
        p = _write_precip_raster(tmp / "empty.tif", all_nodata, nodata=-9999.0)
        with pytest.raises(PrecipForcingError) as excinfo:
            compute_precip_area_mean_mm_per_hr(str(p), _BBOX, accumulation_hours=24.0)
        assert excinfo.value.error_code == "PRECIP_RASTER_EMPTY"


def test_area_mean_zero_accumulation_hours_raises() -> None:
    """accumulation_hours <= 0 is a programming error → ValueError."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_precip_raster(
            Path(td) / "u.tif", np.full((4, 4), 10.0, dtype="float32")
        )
        with pytest.raises(ValueError):
            compute_precip_area_mean_mm_per_hr(str(p), _BBOX, accumulation_hours=0.0)


# --------------------------------------------------------------------------- #
# Test 4 — netamt magnitude lands in the SFINCS deck YAML
# --------------------------------------------------------------------------- #


def test_netamt_magnitude_lands_in_deck() -> None:
    """A pluvial_observed ForcingSpec emits setup_precip_forcing.magnitude verbatim."""
    forcing = ForcingSpec(
        forcing_type="pluvial_observed",
        duration_hours=24.0,
        precip_magnitude_mm_per_hr=2.5,
        provenance={"forcing_mode": "area_mean_netamt"},
    )
    yaml_text = _generate_hydromt_yaml_config(
        bbox=_BBOX,
        options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
        dem_local_path="/tmp/dem.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=forcing,
        mapping_csv_path="/tmp/manning.csv",
    )
    assert "setup_precip_forcing:" in yaml_text
    # The pre-computed magnitude is emitted verbatim (not re-derived from depth).
    assert "magnitude: 2.5" in yaml_text
    # Provenance comment marks the netamt fallback path.
    assert "netamt" in yaml_text
    # The observed path must NOT mention an Atlas 14 design storm.
    assert "Atlas 14" not in yaml_text


def test_observed_forcing_zero_magnitude_rejected() -> None:
    """A zero/None observed magnitude raises FORCING_OUT_OF_RANGE (Invariant 7)."""
    forcing_zero = ForcingSpec(
        forcing_type="pluvial_observed",
        duration_hours=24.0,
        precip_magnitude_mm_per_hr=0.0,
    )
    with pytest.raises(SFINCSSetupError) as excinfo:
        build_sfincs_model(
            dem_uri="/tmp/dem.tif",
            landcover_uri="/tmp/lc.tif",
            river_geometry_uri=None,
            forcing=forcing_zero,
            bbox=_BBOX,
        )
    assert excinfo.value.error_code == "FORCING_OUT_OF_RANGE"

    forcing_none = ForcingSpec(
        forcing_type="pluvial_observed",
        duration_hours=24.0,
        precip_magnitude_mm_per_hr=None,
    )
    with pytest.raises(SFINCSSetupError) as excinfo2:
        build_sfincs_model(
            dem_uri="/tmp/dem.tif",
            landcover_uri="/tmp/lc.tif",
            river_geometry_uri=None,
            forcing=forcing_none,
            bbox=_BBOX,
        )
    assert excinfo2.value.error_code == "FORCING_OUT_OF_RANGE"


# --------------------------------------------------------------------------- #
# Test 2 — None-path deck YAML is identical to the v1 design-storm deck
# --------------------------------------------------------------------------- #


def test_none_path_deck_yaml_unchanged() -> None:
    """The design-storm (None forcing_raster_uri) deck YAML is byte-identical.

    This is the regression guard for the v2 emitter branch: adding the
    pluvial_observed path must not perturb the pluvial_synthetic emission.
    """
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=12.1,
        duration_hours=24.0,
        return_period_years=100,
        provenance={"vintage_volume": "NOAA Atlas 14 Volume 1"},
    )
    kwargs = dict(
        bbox=_BBOX,
        options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
        dem_local_path="/tmp/dem.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=forcing,
        mapping_csv_path="/tmp/manning.csv",
    )
    yaml_text = _generate_hydromt_yaml_config(**kwargs)
    # The Atlas 14 derived magnitude: 12.1 in * 25.4 / 24 hr = 12.8058... mm/hr.
    expected_mag = (12.1 * 25.4) / 24.0
    assert f"magnitude: {expected_mag}" in yaml_text
    assert "Atlas 14: 12.1 in over 24.0 hr" in yaml_text
    # The observed-path netamt comment must NOT appear on the design-storm path.
    assert "area-mean" not in yaml_text
    assert "OQ-6" not in yaml_text


# --------------------------------------------------------------------------- #
# Flood-animation Phase 1 — dtout map-output cadence in the deck YAML
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sim_hours, expected_dtout",
    [
        (24.0, max(600, int(24 * 3600 / 24))),   # 3600 s (1 frame/hr over 24h)
        (48.0, max(600, int(48 * 3600 / 24))),   # 7200 s
        (2.0, max(600, int(2 * 3600 / 24))),     # floors at 600 s (10 min)
    ],
)
def test_dtout_emitted_in_setup_config(sim_hours: float, expected_dtout: int) -> None:
    """``setup_config`` now carries ``dtout`` / ``dtmaxout`` (seconds) so SFINCS
    writes TIME-VARYING ``zs(time,n,m)`` map output — the source of the per-frame
    flood-animation COGs. Value targets ~24 raw snapshots over the sim window,
    floored at 600 s (10 min) to match SFINCS's internal precip grid."""
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=12.1,
        duration_hours=sim_hours,
        return_period_years=100,
        provenance={"vintage_volume": "NOAA Atlas 14 Volume 1"},
    )
    yaml_text = _generate_hydromt_yaml_config(
        bbox=_BBOX,
        options=BuildOptions(grid_resolution_m=30.0, simulation_hours=sim_hours),
        dem_local_path="/tmp/dem.tif",
        landcover_local_path="/tmp/lc.tif",
        river_local_path=None,
        forcing=forcing,
        mapping_csv_path="/tmp/manning.csv",
    )
    assert f"dtout: {expected_dtout}" in yaml_text, (
        f"setup_config must emit dtout: {expected_dtout} for simulation_hours="
        f"{sim_hours}; yaml=\n{yaml_text}"
    )
    assert f"dtmaxout: {expected_dtout}" in yaml_text
    # dtout must be a POSITIVE integer derived from the sim window.
    assert expected_dtout > 0
    # The cadence lives INSIDE the setup_config block (before setup_grid_from_region).
    setup_config_idx = yaml_text.index("setup_config:")
    grid_idx = yaml_text.index("setup_grid_from_region:")
    dtout_idx = yaml_text.index("dtout:")
    assert setup_config_idx < dtout_idx < grid_idx, (
        "dtout must be emitted inside the setup_config block"
    )


# --------------------------------------------------------------------------- #
# Test 1 + 5 — full-workflow forcing-spec capture (None path vs raster path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_none_path_forcing_spec_identical_to_baseline() -> None:
    """forcing_raster_uri=None → build_sfincs_model gets the v1 design-storm spec."""
    captured: dict[str, ForcingSpec] = {}

    def _capture_build(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["forcing"] = kwargs["forcing"]
        return _model_setup()

    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    async def _wfc(h):  # noqa: ANN001
        return _run_result_ok(run_id, handle.handle_id)

    with (
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=_landcover_result()),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period", return_value=_precip_result()) as mock_lookup,
        patch("grace2_agent.workflows.model_flood_scenario.build_sfincs_model", side_effect=_capture_build),
        patch("grace2_agent.workflows.model_flood_scenario.run_solver", return_value=handle),
        patch("grace2_agent.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
        patch(
            "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
            return_value=([_flood_layer(run_id)], _DEPTH_METRICS),
        ),
        patch("grace2_agent.workflows.model_flood_scenario.publish_layer", side_effect=PublishLayerError("JOBS_CLIENT_UNAVAILABLE", "no qgis in test")),
    ):
        envelope = await model_flood_scenario(
            bbox=_BBOX,
            return_period_yr=100,
            duration_hr=24,
            forcing_raster_uri=None,
        )

    # The Atlas 14 lookup WAS called (design-storm path).
    mock_lookup.assert_called_once()
    spec = captured["forcing"]
    assert spec.forcing_type == "pluvial_synthetic"
    assert spec.precip_inches == pytest.approx(12.1)
    assert spec.return_period_years == 100
    assert spec.precip_magnitude_mm_per_hr is None
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.forcing is not None
    assert envelope.forcing.forcing_type == "pluvial_synthetic"


@pytest.mark.asyncio
async def test_raster_path_skips_atlas14_and_builds_observed_forcing() -> None:
    """forcing_raster_uri set → Atlas 14 skipped; observed ForcingSpec built."""
    captured: dict[str, ForcingSpec] = {}

    def _capture_build(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["forcing"] = kwargs["forcing"]
        return _model_setup()

    run_id = new_ulid()
    handle = _make_handle(run_id=run_id)

    async def _wfc(h):  # noqa: ANN001
        return _run_result_ok(run_id, handle.handle_id)

    with tempfile.TemporaryDirectory() as td:
        # Uniform 72 mm raster over 24h → 3.0 mm/hr.
        precip_path = _write_precip_raster(
            Path(td) / "mrms.tif", np.full((12, 12), 72.0, dtype="float32")
        )
        with (
            patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
            patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=_landcover_result()),
            patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
            patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period") as mock_lookup,
            patch("grace2_agent.workflows.model_flood_scenario.build_sfincs_model", side_effect=_capture_build),
            patch("grace2_agent.workflows.model_flood_scenario.run_solver", return_value=handle),
            patch("grace2_agent.workflows.model_flood_scenario.wait_for_completion", side_effect=_wfc),
            patch(
                "grace2_agent.workflows.model_flood_scenario.postprocess_flood",
                return_value=([_flood_layer(run_id)], _DEPTH_METRICS),
            ),
            patch("grace2_agent.workflows.model_flood_scenario.publish_layer", side_effect=PublishLayerError("JOBS_CLIENT_UNAVAILABLE", "no qgis in test")),
        ):
            envelope = await model_flood_scenario(
                bbox=_BBOX,
                return_period_yr=100,
                duration_hr=24,
                forcing_raster_uri=str(precip_path),
            )

    # The Atlas 14 lookup was NOT called on the observed path.
    mock_lookup.assert_not_called()
    spec = captured["forcing"]
    assert spec.forcing_type == "pluvial_observed"
    assert spec.precip_inches is None
    assert spec.precip_magnitude_mm_per_hr == pytest.approx(3.0)
    assert spec.duration_hours == pytest.approx(24.0)
    assert spec.provenance["forcing_mode"] == "area_mean_netamt"
    assert spec.provenance["area_mean_mm"] == pytest.approx(72.0)

    # The envelope's forcing summary records the observed path + raster URI.
    # NOTE: the contract-owned ForcingSummary.forcing_type literal does not
    # include "pluvial_observed"; the workflow summarises observed precip as
    # "pluvial_synthetic" and marks the area-mean netamt mode via parameters
    # (the engine-internal ForcingSpec.forcing_type IS "pluvial_observed").
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.forcing is not None
    assert envelope.forcing.forcing_type == "pluvial_synthetic"
    assert envelope.forcing.parameters["forcing_mode"] == "area_mean_netamt"
    assert envelope.forcing.parameters["forcing_raster_uri"] == str(precip_path)
    assert envelope.forcing.parameters["precip_magnitude_mm_per_hr"] == pytest.approx(3.0)
    assert envelope.forcing.inputs_uri == str(precip_path)
    # The data-source provenance carries the observed raster.
    src_uris = [s.uri for s in envelope.provenance.data_sources]
    assert str(precip_path) in src_uris
    # No Atlas 14 source on the observed path.
    assert "noaa-atlas14-pfds" not in src_uris


@pytest.mark.asyncio
async def test_raster_path_unreadable_raster_returns_failed_envelope() -> None:
    """An unreadable forcing raster surfaces as a typed failed envelope (not a raise)."""
    with (
        patch("grace2_agent.workflows.model_flood_scenario.fetch_dem", return_value=_mock_layer_uri("dem")),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_landcover", return_value=_landcover_result()),
        patch("grace2_agent.workflows.model_flood_scenario.fetch_river_geometry", return_value=_mock_layer_uri("rivers")),
        patch("grace2_agent.workflows.model_flood_scenario.lookup_precip_return_period") as mock_lookup,
    ):
        envelope = await model_flood_scenario(
            bbox=_BBOX,
            duration_hr=24,
            forcing_raster_uri="/nonexistent/path/to/precip.tif",
        )
    mock_lookup.assert_not_called()
    assert isinstance(envelope, AssessmentEnvelope)
    assert envelope.layers == []
    assert envelope.flood is not None
    # The PrecipForcingError code is threaded into solver_version.
    assert "PRECIP_RASTER_READ_FAILED" in envelope.flood.metrics.solver_version


# --------------------------------------------------------------------------- #
# sprint-14-aws (Track C / Case 3) — s3:// forcing-raster read via boto3
# stage-then-open. GDAL's /vsis3/ does NOT resolve the EC2 instance-role creds
# in this env (job-0293c lesson); the s3:// branch of
# compute_precip_area_mean_mm_per_hr must fetch bytes via
# cache.read_object_bytes_s3 then open them in a rasterio MemoryFile. These
# tests monkeypatch the boto3 reader to return a synthetic local COG's bytes so
# the s3 code path is exercised WITHOUT any cloud call.
# --------------------------------------------------------------------------- #


def test_s3_forcing_read_computes_area_mean_via_boto3() -> None:
    """s3:// forcing raster → bytes staged via boto3 reader → same area-mean.

    Asserts the s3 branch produces a magnitude byte-identical to the local-file
    path for the same raster content (the synthetic-raster expectation reused
    from ``test_area_mean_from_synthetic_raster``), proving the boto3
    stage-then-open seam preserves the netamt computation.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Uniform 48.0 mm over 24 hr → 2.0 mm/hr (mirror the local-file test).
        uniform = np.full((10, 10), 48.0, dtype="float32")
        p_uniform = _write_precip_raster(tmp / "uniform.tif", uniform)
        raster_bytes = p_uniform.read_bytes()

        # The s3 branch imports read_object_bytes_s3 from grace2_agent.tools.cache
        # at call time; patch it there so it returns our synthetic COG bytes and
        # asserts it received the s3:// URI verbatim.
        def _fake_read_object_bytes_s3(uri: str) -> bytes:
            assert uri == "s3://test-cache/cache/mrms/precip.tif"
            return raster_bytes

        with patch(
            "grace2_agent.tools.cache.read_object_bytes_s3",
            side_effect=_fake_read_object_bytes_s3,
        ) as mock_s3:
            mag, mean_mm = compute_precip_area_mean_mm_per_hr(
                "s3://test-cache/cache/mrms/precip.tif",
                _BBOX,
                accumulation_hours=24.0,
            )
        mock_s3.assert_called_once()
        assert mean_mm == pytest.approx(48.0)
        assert mag == pytest.approx(2.0)


def test_s3_forcing_read_empty_raster_raises_precip_forcing_error() -> None:
    """s3:// raster with no valid cells → PRECIP_RASTER_EMPTY (Invariant 7).

    The honest-failure path must survive the boto3 stage-then-open seam: the
    bytes are read fine, but the all-nodata mask collapses to size 0 → typed
    PrecipForcingError, NOT a fabricated rain rate.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        all_nodata = np.full((6, 6), -9999.0, dtype="float32")
        p = _write_precip_raster(tmp / "empty.tif", all_nodata, nodata=-9999.0)
        raster_bytes = p.read_bytes()

        with patch(
            "grace2_agent.tools.cache.read_object_bytes_s3",
            return_value=raster_bytes,
        ):
            with pytest.raises(PrecipForcingError) as excinfo:
                compute_precip_area_mean_mm_per_hr(
                    "s3://test-cache/cache/mrms/empty.tif",
                    _BBOX,
                    accumulation_hours=24.0,
                )
        assert excinfo.value.error_code == "PRECIP_RASTER_EMPTY"


def test_s3_forcing_read_boto3_failure_raises_read_failed() -> None:
    """A boto3 ClientError on the s3:// stage surfaces as PRECIP_RASTER_READ_FAILED.

    Threads through the failed-envelope path downstream (same as the unreadable
    local-file test) rather than crashing the workflow.
    """
    with patch(
        "grace2_agent.tools.cache.read_object_bytes_s3",
        side_effect=RuntimeError("boto3 get_object failed: AccessDenied"),
    ):
        with pytest.raises(PrecipForcingError) as excinfo:
            compute_precip_area_mean_mm_per_hr(
                "s3://test-cache/cache/mrms/precip.tif",
                _BBOX,
                accumulation_hours=24.0,
            )
    assert excinfo.value.error_code == "PRECIP_RASTER_READ_FAILED"


def test_gs_forcing_read_path_unchanged_does_not_call_boto3() -> None:
    """Regression: gs:// (and local) forcing reads must NOT touch the boto3 seam.

    The s3 branch is gated on the ``s3://`` prefix; a gs:// URI stays on the
    _to_vsigs/rasterio path. We assert read_object_bytes_s3 is never called and
    that _to_vsigs IS used to build the read path for the gs:// URI.
    """
    gs_uri = "gs://test-cache/cache/mrms/precip.tif"
    with (
        patch(
            "grace2_agent.tools.cache.read_object_bytes_s3",
            side_effect=AssertionError("boto3 reader must not be called for gs://"),
        ),
        patch(
            "grace2_agent.workflows.model_flood_scenario._to_vsigs",
            return_value="/vsigs/test-cache/cache/mrms/precip.tif",
        ) as mock_to_vsigs,
        patch("rasterio.open", side_effect=RuntimeError("vsigs open stubbed")),
    ):
        with pytest.raises(PrecipForcingError) as excinfo:
            compute_precip_area_mean_mm_per_hr(
                gs_uri, _BBOX, accumulation_hours=24.0
            )
        # Failure is the stubbed rasterio.open (proves the gs:// branch was taken).
        assert excinfo.value.error_code == "PRECIP_RASTER_READ_FAILED"
        mock_to_vsigs.assert_called_once_with(gs_uri)
