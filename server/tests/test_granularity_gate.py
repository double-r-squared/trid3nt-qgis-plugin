"""#154 granularity gate (sprint-16) — the SWMM half.

The granularity gate makes mesh resolution a USER lever before a heavy SWMM
solve (memory: feedback_user_controlled_granularity). The autoscaler emits a
SUGGESTED resolution + the active-cell count + estimated solve time + the chosen
compute class on a ``tool-payload-warning`` carrying a ``GranularitySuggestion``
block; the user can override the rung via the existing
``tool-payload-confirmation`` ``narrow_scope`` path (no new WS envelope type).

Covers:
- ``suggest_swmm_resolution`` is PARITY with ``build_swmm_mesh``'s inline
  autoscale prelude for the same DEM + requested resolution (the card and the
  build cannot diverge);
- the gate emits a ``tool-payload-warning`` carrying a ``granularity`` block;
- ``proceed`` pins the SUGGESTED resolution + disables autoscale;
- ``narrow_scope`` finer-than-cap is HARD-CLAMPED + ``enable_autoscale`` False;
- ``narrow_scope`` coarser-than-suggested is honoured exactly;
- ``cancel`` / timeout fail-CLOSED (no solve);
- a suggestion-build exception fails OPEN (proceed with original params);
- the groundwater + flood gate branches are UNCHANGED (granularity is None);
- no stuck ``_PENDING_CONFIRMATIONS`` entry after a CancelledError on the gate.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.ws import PayloadConfirmationEnvelopePayload


# --------------------------------------------------------------------------- #
# Synthetic projected-metres DEM (a real GeoTIFF, NOT a stub) — sized so the
# active-cell count at 10 m base resolution EXCEEDS the default cell cap (~273)
# so the autoscaler COARSENS, giving us a non-trivial suggestion to test.
# --------------------------------------------------------------------------- #
_N = 30  # 30x30 = 900 cells at base res -> well above the default ~273 cap
_CELL = 10.0
_EPSG = 32616  # UTM 16N
_OX, _OY = 500000.0, 4000000.0


def _write_dem_geotiff(path: Path) -> None:
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    dem = (30.0 - 0.02 * _CELL * (ii + jj)).astype("float32")
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": _N,
        "width": _N,
        "crs": CRS.from_epsg(_EPSG),
        "transform": from_origin(_OX, _OY, _CELL, _CELL),
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem, 1)


@pytest.fixture()
def dem_path(tmp_path: Path) -> str:
    p = tmp_path / "dem.tif"
    _write_dem_geotiff(p)
    return str(p)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()


# --------------------------------------------------------------------------- #
# 1) suggest_swmm_resolution PARITY with build_swmm_mesh's inline autoscale.
# --------------------------------------------------------------------------- #
def test_suggest_matches_build_inline_autoscale(dem_path: str) -> None:
    """The standalone suggestion uses the EXACT same DEM read + active-cell count
    + autoscale arithmetic the build uses, so the suggested resolution + active
    estimate the card shows is what the build would compute."""
    from trid3nt_server.workflows import swmm_mesh_builder as mb

    requested = 10.0
    # Reproduce build_swmm_mesh's inline prelude (~839-858) directly.
    grid = mb._read_and_resample_dem(dem_path, requested)
    active_at_base = int(np.isfinite(grid.elev).sum())
    assert active_at_base > 0
    inline = mb.autoscale_swmm_resolution(
        active_at_base, base_resolution_m=requested
    )

    suggestion = mb.suggest_swmm_resolution(dem_path, requested)

    assert suggestion.resolution_m == inline.resolution_m
    assert suggestion.estimated_active_cells == inline.estimated_active_cells
    assert suggestion.cell_cap == inline.cell_cap
    assert suggestion.base_resolution_m == inline.base_resolution_m
    assert (
        suggestion.estimated_active_cells_at_base
        == inline.estimated_active_cells_at_base
    )
    assert suggestion.estimated_solve_seconds == inline.estimated_solve_seconds
    assert suggestion.coarsened == inline.coarsened
    # A 900-cell base AOI exceeds the default cap -> the autoscaler coarsens.
    assert suggestion.coarsened is True
    assert suggestion.estimated_active_cells <= suggestion.cell_cap


def test_suggest_empty_dem_raises(tmp_path: Path) -> None:
    """An all-nodata DEM produces zero finite cells -> typed SWMM_EMPTY_MESH."""
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    from trid3nt_server.workflows.swmm_mesh_builder import (
        SWMMMeshError,
        suggest_swmm_resolution,
    )

    p = tmp_path / "nodata.tif"
    with rasterio.open(
        p, "w", driver="GTiff", dtype="float32", count=1, height=8, width=8,
        crs=CRS.from_epsg(_EPSG), transform=from_origin(_OX, _OY, _CELL, _CELL),
        nodata=-9999.0,
    ) as dst:
        dst.write(np.full((8, 8), -9999.0, dtype="float32"), 1)
    with pytest.raises(SWMMMeshError) as ei:
        suggest_swmm_resolution(str(p), 10.0)
    assert ei.value.error_code == "SWMM_EMPTY_MESH"


# --------------------------------------------------------------------------- #
# Gate helpers: patch the DEM-fetch so the gate reads our synthetic GeoTIFF and
# does no network. We patch _fetch_dem_for_urban at its import site (the gate
# imports it from model_urban_flood_swmm inside the helper).
# --------------------------------------------------------------------------- #
def _patch_dem_fetch(monkeypatch, dem_path: str) -> None:
    import trid3nt_server.workflows.model_urban_flood_swmm as mu

    monkeypatch.setattr(
        mu, "_fetch_dem_for_urban", lambda bbox: (dem_path, "synthetic")
    )


def _swmm_params() -> dict:
    # A real-ish bbox (Twin Falls, ID) — the gate floors+fetches DEM (patched).
    return {
        "bbox": [-114.48, 42.55, -114.46, 42.57],
        "return_period_yr": 100,
        "storm_duration_hr": 6.0,
        "target_resolution_m": 10.0,
        "building_representation": "drop",
    }


async def _drive_decision(server, decision: str, revised_args=None) -> None:
    """Resolve the single pending gate future with ``decision`` once it appears."""
    for _ in range(400):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(server._PENDING_CONFIRMATIONS))
    server._PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(
            warning_id=wid, decision=decision, revised_args=revised_args
        )
    )


# --------------------------------------------------------------------------- #
# 2) The gate emits a granularity block.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_gate_emits_granularity_block(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    await approver

    assert should_run is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    g = card["payload"]["granularity"]
    assert g is not None
    assert g["engine"] == "swmm"
    assert g["resolution_param"] == "target_resolution_m"
    assert g["suggested_resolution_m"] > 0
    assert len(g["resolution_choices"]) >= 1
    assert all(r > 0 for r in g["resolution_choices"])
    assert g["estimated_active_cells"] >= 0
    assert g["estimated_solve_seconds"] >= 0
    assert g["vcpus"] > 0
    assert g["cell_cap"] > 0
    # narrow_scope must be offered so the user can override the rung.
    assert "narrow_scope" in card["payload"]["options"]


# --------------------------------------------------------------------------- #
# 3) proceed pins the SUGGESTED resolution + disables autoscale.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_proceed_pins_suggested_resolution(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server
    from trid3nt_server.workflows.swmm_mesh_builder import suggest_swmm_resolution

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()
    expected = suggest_swmm_resolution(dem_path, 10.0)

    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    await approver

    assert should_run is True
    assert effective["confirmed"] is True
    assert effective["target_resolution_m"] == expected.resolution_m
    assert effective["enable_autoscale"] is False


# --------------------------------------------------------------------------- #
# 4) narrow_scope finer-than-cap is CLAMPED + enable_autoscale False.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_narrow_scope_finer_is_clamped(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server
    from trid3nt_server.workflows.swmm_mesh_builder import suggest_swmm_resolution

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()
    auto = suggest_swmm_resolution(dem_path, 10.0)
    # The cap-implied minimum resolution.
    min_res = auto.base_resolution_m * math.sqrt(
        auto.estimated_active_cells_at_base / float(auto.cell_cap)
    )

    # Ask for 1 m (the finest rung) — far finer than the cap permits.
    approver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"target_resolution_m": 1.0})
    )
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    await approver

    assert should_run is True
    assert effective["confirmed"] is True
    assert effective["enable_autoscale"] is False
    # Clamped UP to (at least) the cap-implied minimum resolution.
    assert effective["target_resolution_m"] >= min_res - 1e-6
    assert effective.get("_granularity_clamped") is True
    # The clamped resolution's cell count cannot exceed the cap.
    chosen = effective["target_resolution_m"]
    cells = auto.estimated_active_cells_at_base * (
        auto.base_resolution_m / chosen
    ) ** 2
    assert cells <= auto.cell_cap + 1  # +1 for rounding slack


# --------------------------------------------------------------------------- #
# 5) narrow_scope coarser-than-suggested is honoured exactly.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_narrow_scope_coarser_honored(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()

    # 20 m is the coarsest ladder rung — coarser than any suggestion -> honoured.
    approver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"target_resolution_m": 20.0})
    )
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    await approver

    assert should_run is True
    assert effective["confirmed"] is True
    assert effective["target_resolution_m"] == 20.0
    assert effective["enable_autoscale"] is False
    assert effective.get("_granularity_clamped") is not True


# --------------------------------------------------------------------------- #
# 6) cancel / timeout fail-CLOSED.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_fails_closed(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()

    canceller = asyncio.create_task(_drive_decision(server, "cancel"))
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    await canceller
    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"


@pytest.mark.asyncio
async def test_timeout_fails_closed(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server

    _patch_dem_fetch(monkeypatch, dem_path)
    monkeypatch.setattr(server, "CODE_EXEC_CONFIRM_TIMEOUT_SECONDS", 0)
    ws, state = _FakeWS(), _FakeState()

    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "CONFIRMATION_TIMEOUT"
    # No stuck pending entry after the timeout (the finally pops it).
    assert not server._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# 7) A suggestion-build exception fails OPEN (proceed w/ original params).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_suggestion_exception_fails_open(monkeypatch) -> None:
    from trid3nt_server import server
    import trid3nt_server.workflows.model_urban_flood_swmm as mu

    def _boom(bbox):
        raise RuntimeError("DEM fetch exploded")

    monkeypatch.setattr(mu, "_fetch_dem_for_urban", _boom)
    ws, state = _FakeWS(), _FakeState()
    params = _swmm_params()

    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", params
    )
    # FAIL OPEN: the gate must NEVER block/orphan a solve on its own error.
    assert should_run is True
    assert effective is params  # original params, unmodified
    assert "confirmed" not in effective
    # No half-built card emitted, no stuck pending entry.
    assert not any(e.get("type") == "tool-payload-warning" for e in ws.sent)
    assert not server._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# 8) The groundwater + flood branches are UNCHANGED (granularity is None).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_flood_branch_unchanged_no_granularity(monkeypatch) -> None:
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()
    params = {"location_query": "Fort Myers, Florida", "return_period_yr": 100}

    approver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await approver

    assert should_run is True and effective["confirmed"] is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    # The flood gate carries NO granularity block (back-compat) + no autoscale pin.
    assert card["payload"].get("granularity") is None
    assert card["payload"]["options"] == ["proceed", "cancel"]
    assert "enable_autoscale" not in effective
    assert "target_resolution_m" not in effective


@pytest.mark.asyncio
async def test_flood_narrow_scope_still_fails_closed(monkeypatch) -> None:
    """A narrow_scope reply to a NON-SWMM gate (no granularity) fails closed."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()
    params = {"location_query": "Fort Myers, Florida"}

    driver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"return_period_yr": 50})
    )
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver
    assert should_run is False
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"


# --------------------------------------------------------------------------- #
# 9) No stuck _PENDING_CONFIRMATIONS when the gate coroutine is CancelledError.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_stuck_pending_on_cancelled(monkeypatch, dem_path: str) -> None:
    from trid3nt_server import server

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()

    gate = asyncio.create_task(
        server._gate_on_solver_confirm(  # type: ignore[arg-type]
            ws, state, "run_swmm_urban_flood", _swmm_params()
        )
    )
    # Wait until the gate has registered its pending future, then cancel it.
    for _ in range(400):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    assert server._PENDING_CONFIRMATIONS  # gate is blocked on the future
    gate.cancel()
    with pytest.raises(asyncio.CancelledError):
        await gate
    # The finally clause must have popped the pending entry (no leak/soft-lock).
    assert not server._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# 10) Registration + clamp-helper unit checks.
# --------------------------------------------------------------------------- #
def test_swmm_tool_in_confirm_set() -> None:
    from trid3nt_server import server

    assert "run_swmm_urban_flood" in server.SOLVER_CONFIRM_TOOLS


def test_clamp_helper_honours_coarser_and_clamps_finer() -> None:
    from trid3nt_server import server
    from trid3nt_server.workflows.swmm_mesh_builder import SWMMAutoscaleResult

    auto = SWMMAutoscaleResult(
        resolution_m=20.0,
        estimated_active_cells=225,
        cell_cap=300,
        base_resolution_m=10.0,
        estimated_active_cells_at_base=900,
        estimated_solve_seconds=100.0,
        coarsened=True,
        reason="test",
    )
    # cap-implied min res = 10 * sqrt(900/300) = 10*sqrt(3) ~= 17.32 m
    min_res = 10.0 * math.sqrt(900 / 300.0)
    # Finer than min -> clamped UP to min_res.
    clamped_res, clamped = server._clamp_swmm_resolution_to_cap(5.0, auto, 10.0)
    assert clamped is True
    assert clamped_res == pytest.approx(min_res)
    # Coarser than min -> honoured exactly.
    res2, clamped2 = server._clamp_swmm_resolution_to_cap(25.0, auto, 10.0)
    assert clamped2 is False
    assert res2 == 25.0


# --------------------------------------------------------------------------- #
# 11) THE LOAD-BEARING CAP TEST -- REAL build count, not the area model.
#
# The verify-found breach: the narrow_scope override used to clamp via the AREA
# model (cells = base_cells*(base/res)**2) then build with enable_autoscale=False
# (no downstream cap re-check). But build_swmm_mesh RE-READS the DEM at the
# clamped resolution and counts active cells via the REAL ceil(extent/res) grid,
# which OVERSHOOTS the area model (~6% for a square fully-active AOI). So an
# over-fine override could solve OVER the cap. These tests exercise the REAL
# build.n_active_cells and assert it is <= cell_cap.
#
# A FULLY-ACTIVE square DEM makes the ceil-grid overshoot maximal (active
# fraction = 1.0), so the area-model clamp lands a real count strictly above the
# cap (~289 > 273 on the 30x30 fixture) -- these tests FAIL against the old
# area-model clamp and PASS after the real-grid clamp.
# --------------------------------------------------------------------------- #
def _real_active_cells_at(dem_path: str, res_m: float, representation: str = "drop") -> int:
    """Build the deck at ``res_m`` and return the REAL active-cell count the
    build counts (the authoritative number the solve uses)."""
    from trid3nt_server.workflows.swmm_mesh_builder import build_swmm_mesh

    import tempfile

    out = Path(tempfile.mkdtemp()) / "deck.inp"
    res = build_swmm_mesh(
        dem_path=dem_path,
        out_inp_path=str(out),
        target_resolution_m=res_m,
        enable_autoscale=False,  # the gate pins an explicit rung
        building_representation=representation,
    )
    return int(res.n_active_cells)


def test_old_area_model_clamp_would_overshoot_real_cap(dem_path: str) -> None:
    """REGRESSION-GUARD: the OLD area-model clamp's resolution yields a REAL
    build count OVER the cap on a fully-active square AOI.

    This documents the breach: it shows the area-model clamp (the helper still in
    the tree) picks a resolution whose REAL ceil-grid count exceeds the cap, so
    the gate must NOT use it as-is. If this assertion ever stops holding (e.g. the
    area model is taught the real grid), the real-cap test below still guards the
    invariant; this one just keeps the breach visible."""
    from trid3nt_server import server
    from trid3nt_server.workflows.swmm_mesh_builder import suggest_swmm_resolution

    auto = suggest_swmm_resolution(dem_path, 10.0)
    # Drive the OLD area-model clamp directly with a 1 m (over-fine) override.
    old_res, old_clamped = server._clamp_swmm_resolution_to_cap(1.0, auto, 10.0)
    assert old_clamped is True
    real = _real_active_cells_at(dem_path, old_res)
    # The 30x30 fully-active square overshoots: real ~289 > cap ~273.
    assert real > auto.cell_cap, (
        f"expected the area-model clamp to overshoot the REAL cap "
        f"(real={real} cap={auto.cell_cap} res={old_res:.3f})"
    )


def test_real_cap_clamp_keeps_build_under_cap(dem_path: str) -> None:
    """The real-grid clamp's resolution yields a REAL build count AT or UNDER the
    cap on the same fully-active square AOI (the breach is closed)."""
    from trid3nt_server.workflows.swmm_mesh_builder import (
        clamp_swmm_resolution_to_real_cap,
        suggest_swmm_resolution,
    )

    auto = suggest_swmm_resolution(dem_path, 10.0)
    rc = clamp_swmm_resolution_to_real_cap(dem_path, 1.0, cell_cap=auto.cell_cap)
    assert rc.clamped is True
    # The helper's reported real count already fits the cap.
    assert rc.real_active_cells <= auto.cell_cap
    # And the ACTUAL build at that resolution agrees + fits the cap.
    real = _real_active_cells_at(dem_path, rc.resolution_m)
    assert real == rc.real_active_cells
    assert real <= auto.cell_cap, (
        f"REAL build count {real} exceeds cap {auto.cell_cap} at "
        f"res={rc.resolution_m:.3f}"
    )


@pytest.mark.asyncio
async def test_narrow_scope_override_real_build_under_cap(
    monkeypatch, dem_path: str
) -> None:
    """END-TO-END: drive the gate's narrow_scope override with an over-fine
    resolution, then BUILD at the gate-chosen resolution and assert the REAL
    active-cell count the solve uses is <= cell_cap.

    This is the load-bearing test: it runs the FULL override path (gate ->
    clamp -> approved target_resolution_m + enable_autoscale=False) and verifies
    the invariant against the REAL build count, not the area model. It FAILS
    against the old area-model clamp (real ~289 > cap ~273) and PASSES after the
    real-grid clamp."""
    from trid3nt_server import server
    from trid3nt_server.workflows.swmm_mesh_builder import suggest_swmm_resolution

    _patch_dem_fetch(monkeypatch, dem_path)
    ws, state = _FakeWS(), _FakeState()
    auto = suggest_swmm_resolution(dem_path, 10.0)

    # Ask for 1 m (far finer than the cap permits) via narrow_scope.
    approver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"target_resolution_m": 1.0})
    )
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_swmm_urban_flood", _swmm_params()
    )
    await approver

    assert should_run is True
    assert effective["confirmed"] is True
    # The gate pins an explicit rung -> autoscale OFF (no downstream cap re-check).
    assert effective["enable_autoscale"] is False
    assert effective.get("_granularity_clamped") is True

    chosen_res = float(effective["target_resolution_m"])
    # Build EXACTLY as the solve will (enable_autoscale=False, same DEM, same
    # building_representation) and assert the REAL count fits the cap.
    real = _real_active_cells_at(
        dem_path, chosen_res, effective.get("building_representation", "drop")
    )
    assert real <= auto.cell_cap, (
        f"narrow_scope override solved OVER the cap: REAL n_active={real} "
        f"> cap={auto.cell_cap} at res={chosen_res:.3f} m"
    )
