"""Tests for the sprint-18 Wave-2 MODFLOW archetypes (MAR / ASR /
wetland_hydroperiod).

Coverage (mirrors test_modflow_archetypes.py):
  * Pure metric math on synthetic arrays (always runs, no flopy/mf6):
    - ``compute_mounding_metrics`` peak head-RISE (clamped >= 0).
    - ``compute_recharged_volume_m3`` rate*duration with the honesty floor.
    - ``compute_seasonal_head_range_m`` peak per-cell swing + the at-peak series.
    - ``compute_recovery_efficiency`` recovered/injected clamped [0, 1].
  * Composer arg-assembly + the USER_INPUT_REQUIRED honesty gates with EVERY
    registry tool MOCKED (geocode / run_modflow_archetype_job) so no run_solver
    / boto3 / network is touched  -  the composer only assembles args + threads
    typed results, and refuses to fabricate a basin / well / wetland.
  * Real-mf6 end-to-end postprocess for each archetype (gated on an mf6 binary +
    flopy): build the GWF-only deck, run mf6, and prove the postprocess reads the
    head / cbc into the typed headline LayerURI with non-trivial physics (a
    positive mound, a recovered-fraction efficiency + a head sawtooth, a positive
    seasonal head range + a head series) and an EPSG:4326 COG. Skips cleanly.

No LLM calls anywhere; the live path shells out to mf6 directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from grace2_contracts.modflow_contracts import (
    ASRLayerURI,
    HydroperiodLayerURI,
    MoundingLayerURI,
)

from grace2_agent.workflows import postprocess_modflow as pp


# --------------------------------------------------------------------------- #
# mf6 binary + flopy discovery (for the live tests)
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _find_mf6() -> str | None:
    env = os.environ.get("GRACE2_MF6_BIN")
    if env and Path(env).exists():
        return env
    on_path = shutil.which("mf6")
    if on_path:
        return on_path
    for cand in (
        Path("/tmp/mf6bin/mf6"),
        Path.home() / ".local" / "bin" / "mf6",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    for cand in _REPO_ROOT.rglob("mf6.5.0_linux/bin/mf6"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


_MF6_BIN = _find_mf6()
_HAVE_FLOPY = True
try:
    import flopy  # type: ignore[import-not-found]  # noqa: F401
except Exception:  # noqa: BLE001
    _HAVE_FLOPY = False

_LIVE_REASON = "no mf6 binary / flopy  -  live archetype postprocess needs both"


# --------------------------------------------------------------------------- #
# Pure metric math (always runs)
# --------------------------------------------------------------------------- #


def test_compute_mounding_metrics_peak_and_clamp() -> None:
    import numpy as np

    nan = float("nan")
    # A mound grid: peak rise 2.5, a small dip (negative) cell, NaN off-grid.
    grid = np.array([[nan, 2.5, 1.0], [-0.3, nan, 0.5]], dtype="float64")
    assert pp.compute_mounding_metrics(grid) == pytest.approx(2.5)


def test_compute_mounding_metrics_all_negative_clamps_to_zero() -> None:
    import numpy as np

    grid = np.array([[-1.0, -2.0], [-0.5, -3.0]], dtype="float64")
    assert pp.compute_mounding_metrics(grid) == 0.0


def test_compute_mounding_metrics_empty_and_all_nan() -> None:
    import numpy as np

    assert pp.compute_mounding_metrics(np.array([])) == 0.0
    assert pp.compute_mounding_metrics(np.full((3, 3), np.nan)) == 0.0


def test_compute_recharged_volume_rate_times_duration() -> None:
    assert pp.compute_recharged_volume_m3(100.0, 30.0) == pytest.approx(3000.0)


def test_compute_recharged_volume_honesty_floor() -> None:
    # Non-positive rate / duration -> None (never narrate an un-integrable volume).
    assert pp.compute_recharged_volume_m3(0.0, 30.0) is None
    assert pp.compute_recharged_volume_m3(100.0, 0.0) is None
    assert pp.compute_recharged_volume_m3(None, 30.0) is None


def test_compute_seasonal_head_range_peak_swing_and_series() -> None:
    import numpy as np

    # Cell [0,0] swings 12 - 9 = 3 (the peak); the returned series is that cell.
    steps = [
        np.array([[10.0, 10.0], [10.0, 10.0]], dtype="float64"),
        np.array([[12.0, 10.5], [10.0, 10.0]], dtype="float64"),
        np.array([[9.0, 10.0], [10.0, 10.0]], dtype="float64"),
    ]
    rng, ts = pp.compute_seasonal_head_range_m(steps)
    assert rng == pytest.approx(3.0)
    assert ts == [10.0, 12.0, 9.0]


def test_compute_seasonal_head_range_single_step_no_series() -> None:
    import numpy as np

    rng, ts = pp.compute_seasonal_head_range_m(
        [np.array([[5.0, 6.0]], dtype="float64")]
    )
    assert rng == 0.0
    assert ts is None


def test_compute_seasonal_head_range_empty() -> None:
    rng, ts = pp.compute_seasonal_head_range_m([])
    assert rng == 0.0 and ts is None


def test_compute_recovery_efficiency_basic_and_clamp() -> None:
    assert pp.compute_recovery_efficiency(1000.0, 850.0) == pytest.approx(0.85)
    # over-recovery clamps to 1.0; nothing-injected -> None.
    assert pp.compute_recovery_efficiency(1000.0, 1200.0) == 1.0
    assert pp.compute_recovery_efficiency(0.0, 100.0) is None
    assert pp.compute_recovery_efficiency(1000.0, -5.0) == 0.0


# --------------------------------------------------------------------------- #
# Composer honesty gates + arg-assembly (registry tools MOCKED  -  no solver)
# --------------------------------------------------------------------------- #


def _patch_archetype_run(monkeypatch: Any, captured: dict[str, Any], layer: Any) -> None:
    """Patch the archetype run-tool the composers dispatch to (no solver)."""
    import grace2_agent.tools.run_modflow_archetype_tool as run_tool

    async def _fake_run(run_args, *, compute_class="standard"):  # noqa: ANN001
        captured["run_args"] = run_args
        captured["compute_class"] = compute_class
        return layer

    monkeypatch.setattr(run_tool, "run_modflow_archetype_job", _fake_run)


def _patch_geocode(monkeypatch: Any, lat: float, lon: float) -> None:
    """Patch geocode on the SHARED composer module (all three reuse it)."""
    from grace2_agent.workflows import model_sustainable_yield_scenario as shared

    def _fake_geocode(location):  # noqa: ANN001
        return {"latitude": lat, "longitude": lon}

    fake_registry = {
        "geocode_location": type("E", (), {"fn": staticmethod(_fake_geocode)}),
    }
    monkeypatch.setattr(shared, "TOOL_REGISTRY", fake_registry)


# --- MAR -------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_mar_assembles_args_and_threads_result(monkeypatch) -> None:
    from grace2_agent.workflows import model_mar_scenario as mod

    captured: dict[str, Any] = {}
    layer = MoundingLayerURI(
        layer_id="mounding-RUN1",
        name="Recharge Mounding",
        layer_type="raster",
        uri="s3://bucket/RUN1/mounding_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        max_mounding_m=3.42,
        recharged_volume_m3=120000.0,
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    basin = [(-100.003, 40.003), (-99.997, 40.003), (-99.997, 39.997)]
    result = await mod.model_mar_scenario(
        aoi_latlon=(40.0, -100.0),
        basin_footprint_lonlat=basin,
        infiltration_rate_m_day=0.05,
        recharge_months=6,
    )
    run_args = captured["run_args"]
    assert run_args.archetype == "MAR"
    assert run_args.basin_footprint_lonlat == basin
    assert run_args.infiltration_rate_m_day == 0.05
    assert run_args.recharge_months == 6
    assert result.mounding_layer.max_mounding_m == pytest.approx(3.42)
    assert result.summary["max_mounding_m"] == pytest.approx(3.42)
    assert result.summary["recharged_volume_m3"] == pytest.approx(120000.0)
    assert "demo_aquifer_caveat" in result.summary


@pytest.mark.asyncio
async def test_mar_accepts_geojson_polygon(monkeypatch) -> None:
    from grace2_agent.workflows import model_mar_scenario as mod

    captured: dict[str, Any] = {}
    layer = MoundingLayerURI(
        layer_id="mounding-RUN1b",
        name="Recharge Mounding",
        layer_type="raster",
        uri="s3://b/RUN1b/mounding_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        max_mounding_m=1.0,
        recharged_volume_m3=None,
    )
    _patch_archetype_run(monkeypatch, captured, layer)
    geojson = {
        "type": "Polygon",
        "coordinates": [
            [[-100.003, 40.003], [-99.997, 40.003], [-99.997, 39.997], [-100.003, 40.003]]
        ],
    }
    result = await mod.model_mar_scenario(
        aoi_latlon=(40.0, -100.0), basin_footprint_lonlat=geojson
    )
    run_args = captured["run_args"]
    assert run_args.basin_footprint_lonlat[0] == (-100.003, 40.003)
    # A None recharged volume is preserved honestly (not coerced to a number).
    assert result.mounding_layer.recharged_volume_m3 is None


@pytest.mark.asyncio
async def test_mar_no_basin_is_user_input_required() -> None:
    from grace2_agent.workflows import model_mar_scenario as mod

    with pytest.raises(mod.MARInputError):
        await mod.model_mar_scenario(
            aoi_latlon=(40.0, -100.0), basin_footprint_lonlat=None
        )
    out = await mod.run_model_mar_scenario(aoi_latlon=[40.0, -100.0])  # no basin
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


# --- ASR -------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_asr_assembles_args_and_threads_result(monkeypatch) -> None:
    from grace2_agent.workflows import model_asr_scenario as mod

    _patch_geocode(monkeypatch, 40.0, -100.0)
    captured: dict[str, Any] = {}
    layer = ASRLayerURI(
        layer_id="asr-RUN2",
        name="ASR",
        layer_type="raster",
        uri="s3://bucket/RUN2/asr_head_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        recovery_efficiency=0.78,
        head_timeseries=[10.0, 12.0, 13.0, 11.0, 9.5],
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    result = await mod.model_asr_scenario(
        location="Somewhere, USA",
        well_location_latlon=(40.0, -100.0),
        injection_rate_m3_day=1500.0,
        recovery_rate_m3_day=1400.0,
        injection_months=3,
        recovery_months=3,
        n_cycles=2,
    )
    run_args = captured["run_args"]
    assert run_args.archetype == "ASR"
    assert run_args.well_location_latlon == (40.0, -100.0)
    # Both rates passed as POSITIVE magnitudes (the adapter applies the WEL sign).
    assert run_args.injection_rate_m3_day == 1500.0
    assert run_args.recovery_rate_m3_day == 1400.0
    assert run_args.n_cycles == 2
    assert result.asr_layer.recovery_efficiency == pytest.approx(0.78)
    assert result.summary["recovery_efficiency"] == pytest.approx(0.78)
    assert result.summary["head_series_steps"] == 5


@pytest.mark.asyncio
async def test_asr_negative_rate_normalized_to_magnitude(monkeypatch) -> None:
    from grace2_agent.workflows import model_asr_scenario as mod

    captured: dict[str, Any] = {}
    layer = ASRLayerURI(
        layer_id="asr-RUN2b",
        name="ASR",
        layer_type="raster",
        uri="s3://b/RUN2b/asr_head_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        recovery_efficiency=None,
        head_timeseries=[10.0, 12.0],
    )
    _patch_archetype_run(monkeypatch, captured, layer)
    result = await mod.model_asr_scenario(
        aoi_latlon=(40.0, -100.0),
        well_location_latlon=(40.0, -100.0),
        injection_rate_m3_day=-1000.0,  # passed negative -> magnitude
        recovery_rate_m3_day=900.0,
    )
    run_args = captured["run_args"]
    assert run_args.injection_rate_m3_day == 1000.0  # magnitude
    # A None efficiency is preserved honestly (single-cycle, no clean split).
    assert result.asr_layer.recovery_efficiency is None


@pytest.mark.asyncio
async def test_asr_missing_well_or_rate_is_user_input_required() -> None:
    from grace2_agent.workflows import model_asr_scenario as mod

    with pytest.raises(mod.ASRInputError):
        await mod.model_asr_scenario(
            aoi_latlon=(40.0, -100.0),
            well_location_latlon=None,
            injection_rate_m3_day=1000.0,
            recovery_rate_m3_day=900.0,
        )
    out = await mod.run_model_asr_scenario(
        aoi_latlon=[40.0, -100.0],
        well_location_latlon=[40.0, -100.0],
        injection_rate_m3_day=1000.0,  # missing recovery rate
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


# --- wetland_hydroperiod ---------------------------------------------------- #


@pytest.mark.asyncio
async def test_wetland_assembles_args_and_threads_result(monkeypatch) -> None:
    from grace2_agent.workflows import model_wetland_hydroperiod_scenario as mod

    captured: dict[str, Any] = {}
    layer = HydroperiodLayerURI(
        layer_id="hydroperiod-RUN3",
        name="Wetland Hydroperiod",
        layer_type="raster",
        uri="s3://bucket/RUN3/hydroperiod_range_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        seasonal_head_range_m=1.85,
        head_timeseries=[10.0, 10.9, 11.85, 10.4, 10.05],
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    wetland = [(-100.003, 40.003), (-99.997, 40.003), (-99.997, 39.997)]
    result = await mod.model_wetland_hydroperiod_scenario(
        aoi_latlon=(40.0, -100.0),
        wetland_footprint_lonlat=wetland,
        recharge_schedule_m_day=[0.01, 0.002, 0.01, 0.002],
        et_max_rate_m_day=0.003,
    )
    run_args = captured["run_args"]
    assert run_args.archetype == "wetland_hydroperiod"
    assert run_args.wetland_footprint_lonlat == wetland
    assert run_args.recharge_schedule_m_day == [0.01, 0.002, 0.01, 0.002]
    assert run_args.et_max_rate_m_day == 0.003
    assert result.hydroperiod_layer.seasonal_head_range_m == pytest.approx(1.85)
    assert result.summary["seasonal_head_range_m"] == pytest.approx(1.85)
    assert result.summary["head_series_steps"] == 5


@pytest.mark.asyncio
async def test_wetland_specific_yield_override_threads(monkeypatch) -> None:
    from grace2_agent.workflows import model_wetland_hydroperiod_scenario as mod

    captured: dict[str, Any] = {}
    layer = HydroperiodLayerURI(
        layer_id="hydroperiod-RUN3b",
        name="Wetland Hydroperiod",
        layer_type="raster",
        uri="s3://b/RUN3b/hydroperiod_range_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        seasonal_head_range_m=0.5,
        head_timeseries=[10.0, 10.5],
    )
    _patch_archetype_run(monkeypatch, captured, layer)
    wetland = [(-100.001, 40.001), (-99.999, 40.001), (-99.999, 39.999)]
    await mod.model_wetland_hydroperiod_scenario(
        aoi_latlon=(40.0, -100.0),
        wetland_footprint_lonlat=wetland,
        specific_yield=0.15,
    )
    run_args = captured["run_args"]
    # The wetland-specific Sy field (distinct from aquifer_sy) is threaded.
    assert run_args.specific_yield == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_wetland_no_footprint_is_user_input_required() -> None:
    from grace2_agent.workflows import model_wetland_hydroperiod_scenario as mod

    with pytest.raises(mod.WetlandHydroperiodInputError):
        await mod.model_wetland_hydroperiod_scenario(
            aoi_latlon=(40.0, -100.0), wetland_footprint_lonlat=None
        )
    out = await mod.run_model_wetland_hydroperiod_scenario(
        aoi_latlon=[40.0, -100.0]  # no footprint
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


# --------------------------------------------------------------------------- #
# Run-tool honesty floor: empty deliverable reads as an error (Wave-2)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_archetype_run_tool_empty_mounding_is_honest_error(monkeypatch) -> None:
    """The shared run-tool refuses to read a zero-mounding result as success."""
    import grace2_agent.tools.run_modflow_archetype_tool as run_tool
    from grace2_contracts.modflow_contracts import MODFLOWRunArgs

    class _Staging:
        run_id = "RUN0"
        model_crs = "EPSG:32614"
        local_deck_dir = "/tmp/none"

    monkeypatch.setattr(run_tool, "is_local_mode", lambda: True)
    monkeypatch.setattr(run_tool, "build_and_stage_modflow_deck", lambda ra: _Staging())
    monkeypatch.setattr(run_tool, "run_modflow_local", lambda s: "file:///tmp/none")

    async def _noop(**_kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(run_tool, "drive_live_solve_progress", _noop)

    zero_layer = MoundingLayerURI(
        layer_id="mounding-RUN0",
        name="Recharge Mounding",
        layer_type="raster",
        uri="file:///tmp/x.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m",
        max_mounding_m=0.0,  # <- empty deliverable
        recharged_volume_m3=None,
    )
    monkeypatch.setattr(run_tool, "postprocess_mounding", lambda *a, **k: zero_layer)
    run_tool.ARCHETYPE_POSTPROCESS["MAR"] = (
        run_tool.postprocess_mounding,
        "max_mounding_m",
    )

    run_args = MODFLOWRunArgs(
        spill_location_latlon=(40.0, -100.0),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        archetype="MAR",
        basin_footprint_lonlat=[(-100.001, 40.001), (-99.999, 40.001)],
    )
    out = await run_tool.run_modflow_archetype_job(run_args)
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "MODFLOW_ARCHETYPE_EMPTY_RESULT"


# --------------------------------------------------------------------------- #
# Live mf6 end-to-end postprocess (gated on mf6 + flopy)
# --------------------------------------------------------------------------- #


def _run_mf6(td: Path) -> None:
    import flopy  # type: ignore[import-not-found]

    sim = flopy.mf6.MFSimulation.load(
        sim_ws=str(td), exe_name=_MF6_BIN, verbosity_level=0
    )
    ok, buf = sim.run_simulation(silent=True)
    assert ok, "".join(buf[-15:])
    assert "Normal termination of simulation" in (td / "mfsim.lst").read_text()


def _offline_postprocess(monkeypatch) -> None:
    """Force the file:// upload fallback + skip publish so the test is offline."""
    monkeypatch.setattr(pp, "_dispatch_publish_layer", lambda *a, **k: None)

    def _file_upload(local_cog, run_id, runs_bucket, *, cog_filename="x.tif"):
        return f"file://{local_cog}"

    monkeypatch.setattr(pp, "_upload_cog", _file_upload)


@pytest.mark.skipif(_MF6_BIN is None or not _HAVE_FLOPY, reason=_LIVE_REASON)
def test_postprocess_mounding_from_real_run(tmp_path, monkeypatch) -> None:
    import numpy as np
    import rasterio

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    basin = [
        (-100.003, 40.003),
        (-99.997, 40.003),
        (-99.997, 39.997),
        (-100.003, 39.997),
    ]
    deck = build_modflow_deck(
        spill_location_latlon=(40.0, -100.0),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        workdir=tmp_path,
        archetype="MAR",
        basin_footprint_lonlat=basin,
        infiltration_rate_m_day=0.05,
        recharge_months=6,
    )
    assert deck.archetype == "MAR" and deck.transient
    assert not deck.gwt_present and deck.recharge_cell_count > 0
    assert deck.npf_icelltype == 1  # unconfined water table (mounding rises)
    _run_mf6(tmp_path)
    _offline_postprocess(monkeypatch)

    layer = pp.postprocess_mounding(
        str(tmp_path),
        run_id="MAR",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(layer, MoundingLayerURI)
    assert layer.units == "m"
    assert layer.style_preset == "continuous_mounding_m"
    assert layer.max_mounding_m > 0.05, "recharge must raise the water table"
    # The recharged volume is the real RCH integral (positive, not fabricated).
    assert layer.recharged_volume_m3 is None or layer.recharged_volume_m3 > 0.0
    with rasterio.open(layer.uri.replace("file://", "")) as ds:
        assert str(ds.crs) == "EPSG:4326"
        arr = ds.read(1)
        finite = arr[np.isfinite(arr)]
        assert finite.size > 0 and finite.max() > 0.05


@pytest.mark.skipif(_MF6_BIN is None or not _HAVE_FLOPY, reason=_LIVE_REASON)
def test_postprocess_asr_from_real_run(tmp_path, monkeypatch) -> None:
    import rasterio

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    deck = build_modflow_deck(
        spill_location_latlon=(40.0, -100.0),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        workdir=tmp_path,
        archetype="ASR",
        well_location_latlon=(40.0, -100.0),
        injection_rate_m3_day=1000.0,
        recovery_rate_m3_day=1000.0,
        injection_months=3,
        recovery_months=3,
        n_cycles=1,
    )
    assert deck.archetype == "ASR" and deck.transient
    assert not deck.gwt_present
    _run_mf6(tmp_path)
    _offline_postprocess(monkeypatch)

    layer = pp.postprocess_asr(
        str(tmp_path),
        run_id="ASR",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(layer, ASRLayerURI)
    assert layer.units == "m"
    # The well-head sawtooth series is the real per-step head (multi-step run).
    assert layer.head_timeseries and len(layer.head_timeseries) > 2
    # The series moves (inject rises, recover falls) -> a non-trivial swing.
    swing = max(layer.head_timeseries) - min(layer.head_timeseries)
    assert swing > 0.01, "the inject/recover cycle must move the well head"
    # Recovery efficiency, when computed, is a clamped fraction.
    if layer.recovery_efficiency is not None:
        assert 0.0 <= layer.recovery_efficiency <= 1.0
    with rasterio.open(layer.uri.replace("file://", "")) as ds:
        assert str(ds.crs) == "EPSG:4326"


@pytest.mark.skipif(_MF6_BIN is None or not _HAVE_FLOPY, reason=_LIVE_REASON)
def test_postprocess_wetland_hydroperiod_from_real_run(tmp_path, monkeypatch) -> None:
    import numpy as np
    import rasterio

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    wetland = [
        (-100.003, 40.003),
        (-99.997, 40.003),
        (-99.997, 39.997),
        (-100.003, 39.997),
    ]
    deck = build_modflow_deck(
        spill_location_latlon=(40.0, -100.0),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        workdir=tmp_path,
        archetype="wetland_hydroperiod",
        wetland_footprint_lonlat=wetland,
        recharge_schedule_m_day=[0.02, 0.0, 0.02, 0.0],
        et_max_rate_m_day=0.005,
    )
    assert deck.archetype == "wetland_hydroperiod" and deck.transient
    assert not deck.gwt_present and deck.wetland_cell_count > 0
    assert deck.npf_icelltype == 1  # unconfined water table (seasonal swing)
    _run_mf6(tmp_path)
    _offline_postprocess(monkeypatch)

    layer = pp.postprocess_wetland_hydroperiod(
        str(tmp_path),
        run_id="WET",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(layer, HydroperiodLayerURI)
    assert layer.units == "m"
    assert layer.style_preset == "continuous_hydroperiod_m"
    assert layer.seasonal_head_range_m > 0.0, "the wet/dry schedule must swing the table"
    assert layer.head_timeseries and len(layer.head_timeseries) > 2
    with rasterio.open(layer.uri.replace("file://", "")) as ds:
        assert str(ds.crs) == "EPSG:4326"
        arr = ds.read(1)
        finite = arr[np.isfinite(arr)]
        # The rendered seasonal-range magnitude is non-negative everywhere.
        assert finite.size > 0 and finite.min() >= 0.0
