"""Adaptive grid-resolution autoscale + solve-telemetry tests (sprint-16).

SFINCS per-job autoscale: coarsen the grid resolution for big AOIs so the
solve fits a configurable wall-clock budget. SFINCS cost scales super-linearly
in the ACTIVE-cell count N (cells inside the setup_mask_active window — NOT raw
bbox area; job-0318). These tests PROVE:

1.  The perf model + cap inversion are self-consistent (estimate_solve_seconds
    at the cap is at or under the solve budget net of overhead).
2.  The cell estimator counts only DEM cells inside the active elevation window
    and scales by (native_res / target_res)^2.
3.  A BIG AOI (Chattanooga-scale active-cell count at 30 m) coarsens UP the
    ladder under the cap; a SMALL AOI stays at 30 m.
4.  The autoscaler NEVER produces a degenerate/empty grid: a pathologically
    huge AOI clamps to the coarsest ladder rung (non-empty), and a DEM that
    can't be read degrades to the bbox-area fallback rather than crashing.
5.  The solve-telemetry record carries the kickoff-named fields and the
    structured log + JSONL sink fire (best-effort, never raises).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from grace2_agent.telemetry import (
    build_solve_telemetry_record,
    emit_solve_telemetry,
)
from grace2_agent.workflows.sfincs_builder import (
    SFINCS_RES_LADDER,
    autoscale_grid_resolution,
    compute_cell_cap,
    estimate_active_cells_at_resolution,
    estimate_solve_seconds,
    resolve_solve_vcpus,
)

# A compact (0.1° x 0.07°) inland bbox; the DEM grid inside it carries the
# elevation field we want to count active cells against.
_BBOX = (-85.35, 35.00, -85.25, 35.07)


def _write_dem(
    path: Path,
    values: np.ndarray,
    *,
    nodata: float | None = None,
    bbox: tuple[float, float, float, float] = _BBOX,
) -> Path:
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


# --------------------------------------------------------------------------- #
# 1. Perf model + cap inversion self-consistency
# --------------------------------------------------------------------------- #


def test_estimate_solve_seconds_anchor_order_of_magnitude() -> None:
    """45k cells @ 8 vCPU sits in the ~tens-of-seconds band (anchor 36 s); the
    p>=1.61 floor over-predicts at low N (safe direction)."""
    t = estimate_solve_seconds(45_000, 8)
    assert 20.0 <= t <= 200.0  # anchor 36s; conservative floor allowed to over-predict


def test_estimate_solve_seconds_superlinear_in_cells() -> None:
    """Doubling cells more than doubles time (p > 1 — super-linear)."""
    t1 = estimate_solve_seconds(100_000, 8)
    t2 = estimate_solve_seconds(200_000, 8)
    assert t2 > 2.0 * t1


def test_more_vcpus_lowers_estimate() -> None:
    """Thread speedup: more vCPU => less wall-clock for the same cell count."""
    assert estimate_solve_seconds(500_000, 16) < estimate_solve_seconds(500_000, 8)


def test_cap_inversion_self_consistent() -> None:
    """The estimated solve at the computed cap is at/under the solve budget net
    of overhead — the cap and the perf model agree."""
    from grace2_agent.workflows import sfincs_builder as sb

    for vcpus in (4, 8, 16, 32):
        cap = compute_cell_cap(vcpus)
        assert cap >= sb.SFINCS_MIN_CELL_CAP
        solve_budget = sb.SFINCS_SOLVE_BUDGET_S * (1.0 - sb.SFINCS_OVERHEAD_FRACTION)
        est = estimate_solve_seconds(cap, vcpus)
        # Allow a small slack for the int() floor in the cap inversion.
        assert est <= solve_budget * 1.02


def test_more_vcpus_raises_cap() -> None:
    assert compute_cell_cap(16) > compute_cell_cap(8) > compute_cell_cap(4)


def test_resolve_solve_vcpus_maps_compute_class() -> None:
    assert resolve_solve_vcpus("small") == 4
    assert resolve_solve_vcpus("medium") == 8  # medium == standard
    assert resolve_solve_vcpus("standard") == 8
    assert resolve_solve_vcpus("large") == 16
    assert resolve_solve_vcpus("unknown-class") == 8  # default


def test_resolve_solve_vcpus_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SFINCS_SOLVE_VCPUS", "24")
    assert resolve_solve_vcpus("small") == 24  # env wins outright


# --------------------------------------------------------------------------- #
# 2. Cell estimator
# --------------------------------------------------------------------------- #


def test_estimate_active_cells_scales_by_resolution_squared() -> None:
    """Coarsening halves the linear dim => quarters the count."""
    base = estimate_active_cells_at_resolution(400_000, 30.0, 30.0)
    assert base == 400_000
    coarse = estimate_active_cells_at_resolution(400_000, 30.0, 60.0)
    assert coarse == 100_000  # (30/60)^2 = 1/4


def test_estimate_active_cells_nonempty_floor() -> None:
    """A real (non-zero) active domain never estimates to 0 cells even at a very
    coarse target (would falsely advertise a free solve)."""
    assert estimate_active_cells_at_resolution(10, 30.0, 5000.0) >= 1


def test_estimate_active_cells_zero_for_empty_domain() -> None:
    assert estimate_active_cells_at_resolution(0, 30.0, 30.0) == 0


# --------------------------------------------------------------------------- #
# 3. autoscale_grid_resolution — big coarsens, small stays
# --------------------------------------------------------------------------- #


def test_small_aoi_stays_at_base_30m(tmp_path: Path) -> None:
    """A small DEM (few active cells at 30 m) keeps grid_resolution_m=30."""
    # 40x40 grid, all at +200 m (well inside the active window) → tiny active N
    # at 30 m once scaled from the coarse native pixel.
    dem = _write_dem(tmp_path / "small_dem.tif", np.full((40, 40), 200.0))
    res = autoscale_grid_resolution(
        str(dem),
        _BBOX,
        zmin=-1000.0,
        zmax=9000.0,
        compute_class="medium",
        base_resolution_m=30.0,
    )
    assert res.grid_resolution_m == 30.0
    assert res.coarsened is False
    assert res.estimated_active_cells <= res.cell_cap
    assert res.estimated_active_cells >= 1  # non-degenerate


def test_big_aoi_coarsens_up_the_ladder(tmp_path: Path) -> None:
    """A DEM whose 30 m active-cell estimate blows past the 8-vCPU cap must
    coarsen UP the ladder to a rung that fits — the headline win."""
    # Chattanooga-scale AOI: ~0.5 deg square ≈ 55 km. A 2000x2000 DEM over it is
    # ~27 m native, all-active → ~3M cells at 30 m, well past the ~130k 8-vCPU
    # cap, so the autoscaler MUST coarsen up the ladder.
    big_bbox = (-85.50, 35.00, -85.00, 35.50)
    dem = _write_dem(
        tmp_path / "big_dem.tif", np.full((2000, 2000), 150.0), bbox=big_bbox
    )
    res = autoscale_grid_resolution(
        str(dem),
        big_bbox,
        zmin=-1000.0,
        zmax=9000.0,
        compute_class="medium",
        base_resolution_m=30.0,
    )
    assert res.grid_resolution_m > 30.0  # coarsened
    assert res.coarsened is True
    assert res.grid_resolution_m in SFINCS_RES_LADDER
    # Either it fits under the cap at the chosen rung, or it clamped to the
    # coarsest rung — never a finer rung that overruns.
    assert (
        res.estimated_active_cells <= res.cell_cap
        or res.grid_resolution_m == max(SFINCS_RES_LADDER)
    )
    assert res.estimated_active_cells_at_base > res.cell_cap  # base really did overrun


def test_active_window_excludes_out_of_window_cells(tmp_path: Path) -> None:
    """Only cells inside [zmin, zmax] are counted active — a DEM that is half
    above the window estimates ~half the active cells of an all-in-window DEM."""
    arr = np.full((200, 200), 5.0)
    arr[:100, :] = 5000.0  # top half far above a tight window
    dem = _write_dem(tmp_path / "split_dem.tif", arr)
    res_tight = autoscale_grid_resolution(
        str(dem), _BBOX, zmin=-10.0, zmax=50.0, compute_class="large",
        base_resolution_m=30.0,
    )
    res_wide = autoscale_grid_resolution(
        str(dem), _BBOX, zmin=-10.0, zmax=9000.0, compute_class="large",
        base_resolution_m=30.0,
    )
    # The wide window admits the whole DEM; the tight window only the bottom
    # half → about twice the active cells at base resolution.
    assert res_wide.estimated_active_cells_at_base > res_tight.estimated_active_cells_at_base
    ratio = res_wide.estimated_active_cells_at_base / max(
        1, res_tight.estimated_active_cells_at_base
    )
    assert 1.8 <= ratio <= 2.3


# --------------------------------------------------------------------------- #
# 4. Never degenerate
# --------------------------------------------------------------------------- #


def test_unreadable_dem_degrades_to_bbox_fallback(tmp_path: Path) -> None:
    """A missing/unreadable DEM path falls back to the bbox-area estimate and
    still returns a valid (non-degenerate) resolution — never crashes."""
    res = autoscale_grid_resolution(
        str(tmp_path / "does_not_exist.tif"),
        _BBOX,
        zmin=-1000.0,
        zmax=9000.0,
        compute_class="medium",
        base_resolution_m=30.0,
    )
    assert res.grid_resolution_m in [30.0, *SFINCS_RES_LADDER]
    assert res.estimated_active_cells >= 1
    assert "bbox-area-fallback" in res.reason


def test_pathological_huge_aoi_clamps_to_coarsest_rung(tmp_path: Path, monkeypatch) -> None:
    """An absurdly large AOI that overruns the cap even at 200 m clamps to the
    coarsest ladder rung (non-empty) rather than producing an empty grid."""
    # Force a tiny cap so any real DEM overruns every rung.
    monkeypatch.setenv("GRACE2_SFINCS_MIN_CELL_CAP", "1")
    monkeypatch.setenv("GRACE2_SFINCS_SOLVE_BUDGET_S", "0.0001")
    import importlib

    from grace2_agent.workflows import sfincs_builder as sb

    importlib.reload(sb)
    try:
        dem = _write_dem(tmp_path / "huge.tif", np.full((800, 800), 100.0))
        res = sb.autoscale_grid_resolution(
            str(dem), _BBOX, zmin=-1000.0, zmax=9000.0, compute_class="small",
            base_resolution_m=30.0,
        )
        assert res.grid_resolution_m == max(sb.SFINCS_RES_LADDER)
        assert res.estimated_active_cells >= 1  # never zero/empty
    finally:
        monkeypatch.delenv("GRACE2_SFINCS_MIN_CELL_CAP", raising=False)
        monkeypatch.delenv("GRACE2_SFINCS_SOLVE_BUDGET_S", raising=False)
        importlib.reload(sb)


def test_resolution_ladder_env_override(monkeypatch) -> None:
    monkeypatch.setenv("GRACE2_SFINCS_RES_LADDER", "25, 75, 150")
    import importlib

    from grace2_agent.workflows import sfincs_builder as sb

    importlib.reload(sb)
    try:
        assert sb.SFINCS_RES_LADDER == (25.0, 75.0, 150.0)
    finally:
        monkeypatch.delenv("GRACE2_SFINCS_RES_LADDER", raising=False)
        importlib.reload(sb)


# --------------------------------------------------------------------------- #
# 5. Solve-telemetry record shape + sink
# --------------------------------------------------------------------------- #


def test_solve_telemetry_record_shape() -> None:
    rec = build_solve_telemetry_record(
        run_id="RUN123",
        backend="local-docker",
        active_cell_count=45_000,
        grid_resolution_m=30.0,
        vcpus=8,
        wall_clock_seconds=36.4,
        aoi_km2=18.2,
        estimated_solve_seconds=40.0,
        coarsened=False,
    )
    # The kickoff-named required fields all present.
    for field in (
        "active_cell_count",
        "grid_resolution_m",
        "vcpus",
        "wall_clock_seconds",
        "backend",
        "run_id",
        "aoi_km2",
    ):
        assert field in rec
    assert rec["kind"] == "solve_telemetry"
    assert rec["run_id"] == "RUN123"
    assert rec["backend"] == "local-docker"
    assert rec["active_cell_count"] == 45_000
    assert rec["vcpus"] == 8
    assert rec["ts"].endswith("Z")


def test_emit_solve_telemetry_writes_jsonl(tmp_path: Path, monkeypatch, caplog) -> None:
    out = tmp_path / "solve_telem.jsonl"
    monkeypatch.setenv("GRACE2_SOLVE_TELEMETRY_PATH", str(out))
    import logging

    with caplog.at_level(logging.INFO, logger="grace2_agent.solve_telemetry"):
        rec = emit_solve_telemetry(
            run_id="RUNXYZ",
            backend="aws-batch",
            active_cell_count=130_000,
            grid_resolution_m=50.0,
            vcpus=8,
            wall_clock_seconds=512.0,
            aoi_km2=900.0,
            estimated_solve_seconds=480.0,
            coarsened=True,
        )
    assert out.exists()
    written = json.loads(out.read_text().strip())
    assert written["run_id"] == "RUNXYZ"
    assert written["coarsened"] is True
    assert written["grid_resolution_m"] == 50.0
    # The structured log line always fires (durable scrape-able signal).
    assert any("solve_telemetry" in r.message for r in caplog.records)
    assert rec["run_id"] == "RUNXYZ"


def test_emit_solve_telemetry_never_raises_on_bad_path(monkeypatch) -> None:
    # A directory that does not exist as a writable file path — write fails,
    # but the emit must not raise (telemetry never breaks the solve loop).
    monkeypatch.setenv("GRACE2_SOLVE_TELEMETRY_PATH", "/this/dir/does/not/exist/x.jsonl")
    rec = emit_solve_telemetry(
        run_id="R",
        backend="local-docker",
        active_cell_count=1,
        grid_resolution_m=30.0,
        vcpus=8,
        wall_clock_seconds=1.0,
        aoi_km2=1.0,
    )
    assert rec["run_id"] == "R"  # returned despite the sink failure
