"""Targeted regression tests for the live-drive bug-fix wave (ADR 0104).

Six defects found during remote live driving:

- Bug 1  TELEMAC degenerate-reach gate + hard mesh watchdog
- Bug 2/3 SWMM subprocess isolation (killable deadline + dead single-instance lock)
- Bug 4  SWMM stale positional fetch signatures + LOUD fallbacks
- Bug 5  SWMM deck END clock rolls past 24 h (no strptime crash)
- Bug 6  oil-slick upload-before-register (no dangling layer handle)

Offline-first: no live daemon, no network. The heavy SWMM solves are guarded by
``importorskip`` (pyswmm/swmm-api/rasterio) and run a tiny synthetic deck.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TELEMAC_WORKER = _REPO_ROOT / "workers" / "telemac"
if str(_TELEMAC_WORKER) not in sys.path:
    sys.path.insert(0, str(_TELEMAC_WORKER))


# ===================================================================== #
# Bug 1 -- TELEMAC degenerate-reach gate + hard watchdog
# ===================================================================== #
def test_reach_degenerate_gate_raises_on_wide_short_reach():
    """The live Longview case: a 292 m reach with the 500 m default width is
    degenerate (aspect < 2) and gates BEFORE meshing."""
    import telemac_river_dye_build as B

    cl = np.array([[0.0, 0.0], [292.0, 0.0]])  # 292 m straight stub
    cfg = B.ReachConfig(channel_width_m=500.0)
    with pytest.raises(B.ReachDegenerateError) as ei:
        B.validate_reach_geometry(cl, cfg)
    msg = str(ei.value)
    # The typed error names the corrective args (0091 gate pattern).
    assert "reach_length_km" in msg
    assert "river_name" in msg
    assert "constant_ribbon" in msg
    assert ei.value.aspect_ratio < 2.0


def test_reach_geometry_ok_for_normal_reach():
    """A normal 6 km / 60 m channel (aspect 100) is not degenerate."""
    import telemac_river_dye_build as B

    cl = np.array([[0.0, 0.0], [6000.0, 0.0]])
    cfg = B.ReachConfig(channel_width_m=60.0)
    B.validate_reach_geometry(cl, cfg)  # no raise


def test_guarded_build_fast_fails_degenerate_without_forking():
    """build_channel_mesh_guarded validates in-parent so a degenerate reach
    fails FAST (no gmsh, no child fork, no hang)."""
    import telemac_river_dye_build as B

    cl = np.array([[0.0, 0.0], [292.0, 0.0]])
    cfg = B.ReachConfig(channel_width_m=500.0)
    with pytest.raises(B.ReachDegenerateError):
        B.build_channel_mesh_guarded(cl, cfg)


def test_server_maps_reach_degenerate_metrics_to_typed_gate():
    """The worker's TELEMAC_REACH_DEGENERATE metrics surface as the typed,
    retryable server error with .suggestions."""
    from trid3nt_server.workflows.telemac.river_dye.river_dye import (  # noqa: E501
        TelemacReachDegenerateError,
        _raise_if_reach_degenerate,
    )

    _raise_if_reach_degenerate({"error_code": "SOMETHING_ELSE"})  # no-op

    with pytest.raises(TelemacReachDegenerateError) as ei:
        _raise_if_reach_degenerate({
            "error_code": "TELEMAC_REACH_DEGENERATE",
            "reach_length_m": 292.0,
            "degenerate_channel_width_m": 500.0,
        })
    assert ei.value.retryable is True
    assert ei.value.error_code == "TELEMAC_REACH_DEGENERATE"
    assert ei.value.suggestions  # rides the tool-retry loop


# ===================================================================== #
# Bug 2/3 -- SWMM subprocess isolation (deadline + dead lock)
# ===================================================================== #
def test_swmm_solve_timeout_seconds_override_and_clamp(monkeypatch):
    from trid3nt_server.mesh import raster_cell_mesh as R

    monkeypatch.setenv("SWMM_SOLVE_TIMEOUT_S", "42")
    assert R._swmm_solve_timeout_s(1000) == 42.0

    monkeypatch.delenv("SWMM_SOLVE_TIMEOUT_S", raising=False)
    assert R._swmm_solve_timeout_s(1) == R._SWMM_SOLVE_TIMEOUT_FLOOR_S  # floor
    assert R._swmm_solve_timeout_s(5_000_000) == R._SWMM_SOLVE_TIMEOUT_CAP_S  # cap


def test_swmm_solve_subprocess_times_out_and_kills():
    """A tiny deadline forces the child to be SIGKILLed -> typed
    SWMM_SOLVE_TIMEOUT (the runaway backstop; a C busy-loop cannot swallow it)."""
    from trid3nt_server.mesh.raster_cell_mesh import (
        SWMMMeshError,
        _solve_swmm_in_subprocess,
    )

    with tempfile.TemporaryDirectory() as td:
        inp = Path(td) / "mesh.inp"
        inp.write_text("[TITLE]\nx\n", encoding="utf-8")
        with pytest.raises(SWMMMeshError) as ei:
            _solve_swmm_in_subprocess(str(inp), 4, 4, 30, timeout_s=0.001)
        assert ei.value.error_code == "SWMM_SOLVE_TIMEOUT"


# ===================================================================== #
# Bug 4 -- stale positional signatures + LOUD fallback labels
# ===================================================================== #
def test_urban_envelope_suffix_labels():
    from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (  # noqa: E501
        _urban_envelope_suffix,
    )

    assert _urban_envelope_suffix(5, False, "USGS 3DEP 1m LiDAR") == (
        "(5 buildings as obstacles)"
    )
    assert _urban_envelope_suffix(0, True, "USGS 3DEP 1m LiDAR") == (
        "(no building obstructions - OSM footprints unavailable)"
    )
    assert _urban_envelope_suffix(0, True, "USGS 3DEP 10m") == (
        "(no building obstructions - OSM footprints unavailable; "
        "10 m DEM fallback)"
    )
    assert _urban_envelope_suffix(0, False, "USGS 3DEP 1m LiDAR") == ""


def test_fetch_buildings_uses_keyword_bbox(monkeypatch):
    """_fetch_buildings_for_urban must call the registry closure with a KEYWORD
    bbox (the post-fold closure is keyword-only; a positional arg TypeErrored and
    was swallowed -> zero footprints)."""
    import trid3nt_server.data as T
    from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (  # noqa: E501
        _fetch_buildings_for_urban,
    )

    fc = {"type": "FeatureCollection", "features": []}

    class _Layer:
        inline_geojson = fc

    def _keyword_only_stub(**kwargs):  # mirrors _promoted(**kwargs)
        assert "bbox" in kwargs, "bbox must be passed by keyword"
        return _Layer()

    class _FakeTool:
        fn = staticmethod(_keyword_only_stub)

    monkeypatch.setitem(T.TOOL_REGISTRY, "fetch_buildings", _FakeTool())
    out = _fetch_buildings_for_urban((-88.0, 36.0, -87.99, 36.01))
    assert out == fc  # a positional call would have TypeErrored -> None


# ===================================================================== #
# Bug 6 -- oil-slick upload-before-register (no dangling handle)
# ===================================================================== #
def test_s3_object_exists_guard():
    from trid3nt_server.workflows.telemac.river_dye.river_dye import (  # noqa: E501
        _s3_object_exists,
    )

    class _PresentS3:
        def head_object(self, **kw):
            return {"ContentLength": 10}

    class _AbsentS3:
        def head_object(self, **kw):
            raise RuntimeError("NoSuchKey")

    assert _s3_object_exists(_PresentS3(), "b", "k") is True
    assert _s3_object_exists(_AbsentS3(), "b", "k") is False


# ===================================================================== #
# Heavy SWMM chain (needs pyswmm + swmm-api + rasterio)
# ===================================================================== #
swmm_api = pytest.importorskip("swmm_api")
pyswmm = pytest.importorskip("pyswmm")
rasterio = pytest.importorskip("rasterio")

from swmm_api import SwmmInput  # noqa: E402
from swmm_api.input_file.section_labels import OPTIONS  # noqa: E402

from trid3nt_contracts.swmm_contracts import SWMMRunArgs  # noqa: E402
from trid3nt_server.workflows.swmm.run_swmm import (  # noqa: E402
    build_and_stage_swmm_deck,
    run_swmm_local,
)

_N, _CELL, _EPSG, _OX, _OY = 12, 10.0, 32616, 500000.0, 4000000.0


def _write_dem(path: Path) -> None:
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    ci = cj = (_N - 1) / 2.0
    pit = 2.0 * np.exp(-((ii - ci) ** 2 + (jj - cj) ** 2) / (2.0 * 3.0**2))
    dem = (30.0 - 0.02 * _CELL * (ii + jj) - pit).astype("float32")
    prof = {
        "driver": "GTiff", "dtype": "float32", "count": 1, "height": _N,
        "width": _N, "crs": CRS.from_epsg(_EPSG),
        "transform": from_origin(_OX, _OY, _CELL, _CELL), "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(dem, 1)


def _run_args(storm_hr: float) -> SWMMRunArgs:
    return SWMMRunArgs(
        bbox=(-88.0, 36.0, -87.99, 36.01),
        total_rain_depth_mm=120.0,
        storm_duration_hr=storm_hr,
        rain_interval_min=5,
        target_resolution_m=10.0,
        building_representation="drop",
        mass_balance_tolerance_pct=100.0,
    )


@pytest.mark.parametrize("storm_hr", [25.0, 48.0])
def test_swmm_deck_end_clock_rolls_past_24h(storm_hr, tmp_path):
    """A storm whose report window exceeds 24 h authors a VALID END clock that
    rolls into END_DATE -- SwmmInput.read_file (which strptime-crashed on
    '25:00:00') round-trips cleanly (Bug 5)."""
    dem = tmp_path / "dem.tif"
    _write_dem(dem)
    staging = build_and_stage_swmm_deck(
        _run_args(storm_hr), dem_path=str(dem), building_footprints=None
    )
    inp = SwmmInput.read_file(staging.inp_path)  # crashed on 25:00:00 before
    opts = inp[OPTIONS]
    # END_DATE advanced beyond the 01/01 start; END_TIME is a valid 0-24h clock.
    assert str(opts["END_DATE"]) != "2024-01-01"
    hh = int(str(opts["END_TIME"]).split(":")[0])
    assert 0 <= hh <= 24


def test_two_consecutive_swmm_solves_in_one_process(tmp_path):
    """Bug 2/3: subprocess isolation kills the pyswmm single-instance lock -- TWO
    consecutive run_swmm_local calls in ONE process both complete."""
    dem = tmp_path / "dem.tif"
    _write_dem(dem)
    results = []
    for _ in range(2):
        staging = build_and_stage_swmm_deck(
            _run_args(1.0), dem_path=str(dem), building_footprints=None
        )
        run = run_swmm_local(staging)
        assert Path(run.out_path).exists()
        assert run.n_steps > 1
        results.append(run)
    # Both solves produced a real .out with a continuity number (lock is dead).
    assert all(r.continuity_error_pct is not None for r in results)
