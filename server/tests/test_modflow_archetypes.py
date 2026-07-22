"""Tests for the sprint-18 Wave-1 MODFLOW archetypes (sustainable_yield /
mine_dewatering / regional_water_budget).

Coverage:
  * Pure metric math on synthetic arrays (always runs, no flopy/mf6):
    - ``compute_drawdown_metrics`` peak head-decline (clamped >= 0).
    - ``compute_cbc_term_metrics`` magnitude + active-cell-count.
    - ``compute_budget_partition`` FLOW-JA-FACE exclusion + near-zero drop +
      honest sign preservation.
  * Composer arg-assembly + the USER_INPUT_REQUIRED honesty gates with EVERY
    registry tool MOCKED (geocode / run_modflow_archetype_job) so no run_solver
    / boto3 / network is touched  -  the composer only assembles args + threads
    typed results, and refuses to fabricate a well / pit.
  * Real-mf6 end-to-end postprocess for each archetype (gated on an mf6 binary +
    flopy): build the GWF-only deck, run mf6, and prove the postprocess reads
    the head / cbc into the typed headline LayerURI with non-trivial physics
    (a positive drawdown cone, a positive dewatering rate, a balanced CHD budget
    with separate IN/OUT legs) and an EPSG:4326 COG. Skips cleanly otherwise.

No LLM calls anywhere; the live path shells out to mf6 directly.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from grace2_contracts.modflow_contracts import (
    BudgetPartitionLayerURI,
    DewaterLayerURI,
    DrawdownLayerURI,
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


def test_compute_drawdown_metrics_peak_and_clamp() -> None:
    import numpy as np

    nan = float("nan")
    # A drawdown grid: peak decline 3.5, a small mounding (negative) cell, NaN.
    grid = np.array([[nan, 3.5, 1.0], [-0.2, nan, 2.1]], dtype="float64")
    assert pp.compute_drawdown_metrics(grid) == pytest.approx(3.5)


def test_compute_drawdown_metrics_all_negative_clamps_to_zero() -> None:
    import numpy as np

    grid = np.array([[-1.0, -2.0], [-0.5, -3.0]], dtype="float64")
    assert pp.compute_drawdown_metrics(grid) == 0.0


def test_compute_drawdown_metrics_empty_and_all_nan() -> None:
    import numpy as np

    assert pp.compute_drawdown_metrics(np.array([])) == 0.0
    assert pp.compute_drawdown_metrics(np.full((3, 3), np.nan)) == 0.0


def test_compute_cbc_term_metrics_magnitude_and_count() -> None:
    import numpy as np

    nan = float("nan")
    # A DRN grid: drains remove water (negative q); magnitude is the pump rate.
    grid = np.array([[nan, -10.0, nan], [-5.0, nan, -2.0]], dtype="float64")
    total_mag, cells = pp.compute_cbc_term_metrics(grid)
    assert total_mag == pytest.approx(17.0)  # |−10| + |−5| + |−2|
    assert cells == 3


def test_compute_cbc_term_metrics_empty() -> None:
    import numpy as np

    assert pp.compute_cbc_term_metrics(np.array([])) == (0.0, 0)
    assert pp.compute_cbc_term_metrics(np.full((4, 4), np.nan)) == (0.0, 0)


def test_compute_budget_partition_signs_exclude_and_drop() -> None:
    # FLOW-JA-FACE excluded; near-zero dropped; honest signs preserved; lowercased.
    totals = {
        "CHD_IN": 1200.0,
        "CHD_OUT": -1180.0,
        "WEL": -2000.0,  # extraction stays negative
        "STORAGE": -1e-12,  # near-zero -> dropped
        "FLOW-JA-FACE": 999999.0,  # internal term -> excluded
    }
    part = pp.compute_budget_partition(totals)
    assert part == {
        "chd_in": 1200.0,
        "chd_out": -1180.0,
        "wel": -2000.0,
    }
    assert "flow-ja-face" not in part
    assert "storage" not in part


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


def _patch_geocode(monkeypatch: Any, mod: Any, lat: float, lon: float) -> None:
    """Patch the geocode_location registry entry on a composer module."""

    def _fake_geocode(location):  # noqa: ANN001
        return {"latitude": lat, "longitude": lon}

    fake_registry = {
        "geocode_location": type("E", (), {"fn": staticmethod(_fake_geocode)}),
    }
    monkeypatch.setattr(mod, "TOOL_REGISTRY", fake_registry)


@pytest.mark.asyncio
async def test_sustainable_yield_assembles_args_and_threads_result(monkeypatch) -> None:
    from grace2_agent.workflows import model_sustainable_yield_scenario as mod

    _patch_geocode(monkeypatch, mod, 40.0, -100.0)
    captured: dict[str, Any] = {}
    layer = DrawdownLayerURI(
        layer_id="drawdown-RUN1",
        name="Pumping Drawdown",
        layer_type="raster",
        uri="s3://bucket/RUN1/drawdown_4326.tif",
        style_preset="continuous_drawdown_m",
        role="primary",
        units="m",
        max_drawdown_m=6.13,
        head_decline_timeseries=[0.0, 3.0, 6.13],
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    result = await mod.model_sustainable_yield_scenario(
        location="Somewhere, USA",
        well_location_latlon=(40.0, -100.0),
        pumping_rate_m3_day=2000.0,  # positive -> negated to extraction
    )

    run_args = captured["run_args"]
    assert run_args.archetype == "sustainable_yield"
    assert run_args.well_location_latlon == (40.0, -100.0)
    # A positive rate is negated to the MF6 WEL extraction sign.
    assert run_args.pumping_rate_m3_day == -2000.0
    assert result.drawdown_layer.max_drawdown_m == pytest.approx(6.13)
    assert result.summary["max_drawdown_m"] == pytest.approx(6.13)
    assert result.summary["head_decline_steps"] == 3
    assert "demo_aquifer_caveat" in result.summary


@pytest.mark.asyncio
async def test_sustainable_yield_no_well_is_user_input_required(monkeypatch) -> None:
    from grace2_agent.workflows import model_sustainable_yield_scenario as mod

    # A run with no well must NOT reach the solver; it raises the honesty gate.
    with pytest.raises(mod.SustainableYieldInputError):
        await mod.model_sustainable_yield_scenario(
            aoi_latlon=(40.0, -100.0),
            well_location_latlon=None,
            pumping_rate_m3_day=2000.0,
        )
    # The LLM-facing wrapper maps it to a typed USER_INPUT_REQUIRED envelope.
    out = await mod.run_model_sustainable_yield_scenario(
        aoi_latlon=[40.0, -100.0],
        pumping_rate_m3_day=2000.0,  # missing well
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_mine_dewatering_assembles_args_and_threads_result(monkeypatch) -> None:
    from grace2_agent.workflows import model_mine_dewatering_scenario as mod

    captured: dict[str, Any] = {}
    layer = DewaterLayerURI(
        layer_id="dewatering-rate-RUN2",
        name="Mine Dewatering Rate",
        layer_type="raster",
        uri="s3://bucket/RUN2/dewatering_rate_4326.tif",
        style_preset="continuous_dewatering_rate",
        role="primary",
        units="m^3/day",
        dewatering_rate_m3_day=10636.1,
        drain_cell_count=168,
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    pit = [(-100.003, 40.003), (-99.997, 40.003), (-99.997, 39.997)]
    result = await mod.model_mine_dewatering_scenario(
        aoi_latlon=(40.0, -100.0),
        pit_footprint_lonlat=pit,
    )
    run_args = captured["run_args"]
    assert run_args.archetype == "mine_dewatering"
    assert run_args.pit_footprint_lonlat == pit
    assert result.dewater_layer.dewatering_rate_m3_day == pytest.approx(10636.1)
    assert result.summary["drain_cell_count"] == 168


@pytest.mark.asyncio
async def test_mine_dewatering_accepts_geojson_polygon(monkeypatch) -> None:
    from grace2_agent.workflows import model_mine_dewatering_scenario as mod

    captured: dict[str, Any] = {}
    layer = DewaterLayerURI(
        layer_id="dewatering-rate-RUN3",
        name="Mine Dewatering Rate",
        layer_type="raster",
        uri="s3://b/RUN3/dewatering_rate_4326.tif",
        style_preset="continuous_dewatering_rate",
        role="primary",
        units="m^3/day",
        dewatering_rate_m3_day=5000.0,
        drain_cell_count=40,
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    geojson = {
        "type": "Polygon",
        "coordinates": [
            [[-100.003, 40.003], [-99.997, 40.003], [-99.997, 39.997], [-100.003, 40.003]]
        ],
    }
    result = await mod.model_mine_dewatering_scenario(
        aoi_latlon=(40.0, -100.0), pit_footprint_lonlat=geojson
    )
    run_args = captured["run_args"]
    # The GeoJSON ring is normalized to a (lon, lat) vertex list.
    assert run_args.pit_footprint_lonlat[0] == (-100.003, 40.003)
    assert result.dewater_layer.drain_cell_count == 40


@pytest.mark.asyncio
async def test_mine_dewatering_no_pit_is_user_input_required() -> None:
    from grace2_agent.workflows import model_mine_dewatering_scenario as mod

    with pytest.raises(mod.MineDewateringInputError):
        await mod.model_mine_dewatering_scenario(
            aoi_latlon=(40.0, -100.0), pit_footprint_lonlat=None
        )
    out = await mod.run_model_mine_dewatering_scenario(
        aoi_latlon=[40.0, -100.0]  # no pit
    )
    assert out["status"] == "error"
    assert out["error_code"] == "USER_INPUT_REQUIRED"


@pytest.mark.asyncio
async def test_regional_water_budget_assembles_args_and_threads_result(monkeypatch) -> None:
    from grace2_agent.workflows import model_regional_water_budget_scenario as mod

    captured: dict[str, Any] = {}
    layer = BudgetPartitionLayerURI(
        layer_id="budget-partition-RUN4",
        name="Regional Water Budget",
        layer_type="raster",
        uri="s3://b/RUN4/water_table_4326.tif",
        style_preset="continuous_head_m",
        role="primary",
        units="m^3/day",
        budget_partition_m3_day={"chd_in": 1063.4, "chd_out": -1063.4},
    )
    _patch_archetype_run(monkeypatch, captured, layer)

    result = await mod.model_regional_water_budget_scenario(
        aoi_latlon=(40.0, -100.0), zone_partition="upgradient_downgradient"
    )
    run_args = captured["run_args"]
    assert run_args.archetype == "regional_water_budget"
    assert run_args.zone_partition == "upgradient_downgradient"
    assert result.summary["budget_partition_m3_day"]["chd_in"] == pytest.approx(1063.4)
    assert result.budget_layer.budget_partition_m3_day["chd_out"] == pytest.approx(-1063.4)


@pytest.mark.asyncio
async def test_archetype_run_tool_empty_result_is_honest_error(monkeypatch) -> None:
    """The shared run-tool refuses to read a zero-drawdown result as success
    (the honesty floor: a 'modeled' layer with an empty deliverable is an error)."""
    import grace2_agent.tools.run_modflow_archetype_tool as run_tool
    from grace2_contracts.modflow_contracts import MODFLOWRunArgs

    # Stub the whole solver chain so the postprocess returns a ZERO drawdown.
    class _Staging:
        run_id = "RUN0"
        model_crs = "EPSG:32614"
        local_deck_dir = "/tmp/none"

    monkeypatch.setattr(run_tool, "is_local_mode", lambda: True)
    monkeypatch.setattr(
        run_tool, "build_and_stage_modflow_deck", lambda ra: _Staging()
    )
    monkeypatch.setattr(run_tool, "run_modflow_local", lambda s: "file:///tmp/none")
    # drive_live_solve_progress is awaited; stub it to a no-op coroutine.

    async def _noop(**_kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(run_tool, "drive_live_solve_progress", _noop)

    zero_layer = DrawdownLayerURI(
        layer_id="drawdown-RUN0",
        name="Pumping Drawdown",
        layer_type="raster",
        uri="file:///tmp/x.tif",
        style_preset="continuous_drawdown_m",
        role="primary",
        units="m",
        max_drawdown_m=0.0,  # <- empty deliverable
        head_decline_timeseries=None,
    )
    monkeypatch.setattr(
        run_tool, "postprocess_drawdown", lambda *a, **k: zero_layer
    )
    run_tool.ARCHETYPE_POSTPROCESS["sustainable_yield"] = (
        run_tool.postprocess_drawdown,
        "max_drawdown_m",
    )

    run_args = MODFLOWRunArgs(
        spill_location_latlon=(40.0, -100.0),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        archetype="sustainable_yield",
        well_location_latlon=(40.0, -100.0),
        pumping_rate_m3_day=-2000.0,
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
def test_postprocess_drawdown_from_real_run(tmp_path, monkeypatch) -> None:
    import numpy as np
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
        archetype="sustainable_yield",
        well_location_latlon=(40.0, -100.0),
        pumping_rate_m3_day=-2000.0,
    )
    assert deck.archetype == "sustainable_yield" and deck.transient
    assert not deck.gwt_present  # GWF-only
    _run_mf6(tmp_path)
    _offline_postprocess(monkeypatch)

    layer = pp.postprocess_drawdown(
        str(tmp_path),
        run_id="SY",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(layer, DrawdownLayerURI)
    assert layer.units == "m"
    assert layer.style_preset == "continuous_drawdown_m"
    assert layer.max_drawdown_m > 0.5, "a 2000 m3/day extraction must draw down"
    assert layer.head_decline_timeseries and len(layer.head_decline_timeseries) > 1
    # The timeseries is monotonic-ish increasing from ~0 at t0 to the peak.
    assert layer.head_decline_timeseries[0] == pytest.approx(0.0, abs=1e-6)
    with rasterio.open(layer.uri.replace("file://", "")) as ds:
        assert str(ds.crs) == "EPSG:4326"
        arr = ds.read(1)
        finite = arr[np.isfinite(arr)]
        assert finite.size > 0 and finite.max() > 0.5


@pytest.mark.skipif(_MF6_BIN is None or not _HAVE_FLOPY, reason=_LIVE_REASON)
def test_postprocess_dewatering_from_real_run(tmp_path, monkeypatch) -> None:
    import numpy as np
    import rasterio

    from grace2_agent.workflows.run_modflow import build_modflow_deck

    pit = [
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
        archetype="mine_dewatering",
        pit_footprint_lonlat=pit,
    )
    assert deck.archetype == "mine_dewatering" and deck.drain_cell_count > 0
    assert deck.npf_icelltype == 1  # unconfined water table
    _run_mf6(tmp_path)
    _offline_postprocess(monkeypatch)

    layer = pp.postprocess_dewatering(
        str(tmp_path),
        run_id="MD",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(layer, DewaterLayerURI)
    assert layer.units == "m^3/day"
    assert layer.style_preset == "continuous_dewatering_rate"
    assert layer.drain_cell_count == deck.drain_cell_count
    assert layer.dewatering_rate_m3_day > 1.0, "the pit must pump a positive rate"
    with rasterio.open(layer.uri.replace("file://", "")) as ds:
        assert str(ds.crs) == "EPSG:4326"
        arr = ds.read(1)
        finite = arr[np.isfinite(arr)]
        # The rendered dewatering magnitude is non-negative everywhere.
        assert finite.size > 0 and finite.min() >= 0.0 and finite.max() > 0.0


@pytest.mark.skipif(_MF6_BIN is None or not _HAVE_FLOPY, reason=_LIVE_REASON)
def test_postprocess_budget_partition_from_real_run(tmp_path, monkeypatch) -> None:
    from grace2_agent.workflows.run_modflow import build_modflow_deck

    deck = build_modflow_deck(
        spill_location_latlon=(40.0, -100.0),
        contaminant="n/a",
        release_rate_kg_s=1.0,
        duration_days=1.0,
        aquifer_k_ms=1e-4,
        porosity=0.3,
        workdir=tmp_path,
        archetype="regional_water_budget",
        zone_partition="upgradient_downgradient",
    )
    assert deck.archetype == "regional_water_budget"
    _run_mf6(tmp_path)
    _offline_postprocess(monkeypatch)

    layer = pp.postprocess_budget_partition(
        str(tmp_path),
        run_id="WB",
        model_crs=deck.model_crs,
        deck_dir=str(tmp_path),
        publish=False,
    )
    assert isinstance(layer, BudgetPartitionLayerURI)
    part = layer.budget_partition_m3_day
    # The regional CHD gradient drives throughflow IN one side, OUT the other.
    assert "chd_in" in part and "chd_out" in part
    assert part["chd_in"] > 0.0 and part["chd_out"] < 0.0
    # Steady single-layer flow with only CHD boundaries balances (in + out ~ 0).
    assert abs(part["chd_in"] + part["chd_out"]) < 0.05 * abs(part["chd_in"])
    # The internal inter-cell term is never narrated.
    assert "flow-ja-face" not in part
