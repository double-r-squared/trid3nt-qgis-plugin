"""Combined run-settings gate (sprint-16) - the flood half of the gate.

The combined run-settings gate extends the flood solver-confirm gate into ONE
card carrying BOTH a ``GranularitySuggestion`` (SPATIAL resolution) AND a
``TimeScaleSuggestion`` (TEMPORAL cadence + window). The user reviews + overrides
both in ONE interaction; the override rides back on the existing
``tool-payload-confirmation`` ``narrow_scope`` path carrying both keys in one
``revised_args`` dict; the server pins them into the run.

Covers:
- the SFINCS bbox-area resolution suggestion is loop-safe (no DEM read) and
  mirrors the autoscaler's ladder/cap logic;
- a COASTAL flood (bbox + surge) gate emits BOTH a granularity block and a
  time_scale block, and offers narrow_scope;
- proceed pins the suggested grid_resolution_m AND the resolved
  output_interval_min;
- narrow_scope honours a user-chosen grid_resolution_m AND output_interval_min
  AND duration_hr in ONE revised_args dict;
- a PLUVIAL flood (no coastal signal) carries a granularity block (when a bbox
  is present) but NO time_scale block (hourly cadence is fixed);
- a bbox-less pluvial flood carries neither block and still fails closed on a
  narrow_scope reply (the card never offered it).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_contracts import new_ulid
from grace2_contracts.ws import PayloadConfirmationEnvelopePayload

# A small coastal Gulf AOI (Fort Myers / Mexico Beach scale) - used for the
# bbox-bearing gate paths.
COASTAL_BBOX = [-82.05, 26.50, -81.95, 26.60]


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()


async def _drive_decision(server, decision, revised=None):
    """Wait for the gate's pending future, then resolve it with ``decision``."""
    for _ in range(400):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(server._PENDING_CONFIRMATIONS))
    server._PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(
            warning_id=wid, decision=decision, revised_args=revised
        )
    )


# --------------------------------------------------------------------------- #
# 1) The SFINCS bbox-area suggestion is loop-safe + ladder/cap-correct.
# --------------------------------------------------------------------------- #
def test_sfincs_suggest_from_bbox_is_loop_safe_and_capped() -> None:
    from grace2_agent.workflows.sfincs_builder import (
        SFINCS_RES_LADDER,
        suggest_sfincs_resolution_from_bbox,
    )

    r = suggest_sfincs_resolution_from_bbox(tuple(COASTAL_BBOX))
    # Reads NO file, returns a GridAutoscaleResult shape.
    assert r.grid_resolution_m > 0
    assert r.estimated_active_cells >= 0
    assert r.vcpus > 0
    assert r.cell_cap > 0
    # The chosen rung is on the SFINCS ladder (or the base rung).
    assert r.grid_resolution_m in set(SFINCS_RES_LADDER) | {30.0}
    # The estimate fits the cap unless the AOI is so large no rung fits (this
    # small AOI fits at some rung).
    assert r.estimated_active_cells <= r.cell_cap


def test_sfincs_suggest_huge_aoi_coarsens() -> None:
    from grace2_agent.workflows.sfincs_builder import (
        suggest_sfincs_resolution_from_bbox,
    )

    # A continent-scale bbox forces a coarsen up the ladder.
    big = suggest_sfincs_resolution_from_bbox((-100.0, 25.0, -80.0, 45.0))
    small = suggest_sfincs_resolution_from_bbox(tuple(COASTAL_BBOX))
    assert big.grid_resolution_m >= small.grid_resolution_m
    assert big.coarsened is True


# --------------------------------------------------------------------------- #
# 2) A coastal flood gate emits BOTH a granularity + a time_scale block.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_coastal_gate_emits_combined_run_settings(monkeypatch) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    params = {
        "bbox": COASTAL_BBOX,
        "return_period_yr": 100,
        "duration_hr": 6,
        "coastal": True,
    }

    driver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is True and effective["confirmed"] is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    pl = card["payload"]
    # BOTH blocks present.
    g = pl["granularity"]
    ts = pl["time_scale"]
    assert g is not None and ts is not None
    assert g["engine"] == "sfincs"
    assert g["resolution_param"] == "grid_resolution_m"
    assert ts["cadence_param"] == "output_interval_min"
    assert ts["duration_param"] == "duration_hr"
    assert ts["is_coastal"] is True
    assert ts["estimated_frame_count"] >= 1
    assert ts["suggested_duration_hr"] == 6
    # narrow_scope offered (the override path).
    assert "narrow_scope" in pl["options"]


# --------------------------------------------------------------------------- #
# 3) proceed pins BOTH the suggested resolution AND the resolved cadence.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_coastal_proceed_pins_both(monkeypatch) -> None:
    from grace2_agent import server
    from grace2_agent.workflows.sfincs_builder import (
        suggest_sfincs_resolution_from_bbox,
    )

    expected = suggest_sfincs_resolution_from_bbox(tuple(COASTAL_BBOX))

    ws, state = _FakeWS(), _FakeState()
    params = {"bbox": COASTAL_BBOX, "duration_hr": 6, "coastal": True}

    driver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is True
    assert effective["grid_resolution_m"] == expected.grid_resolution_m
    # Coastal -> a fine minute-scale cadence pinned (not None / not hourly).
    assert effective["output_interval_min"] is not None
    assert effective["output_interval_min"] > 0


# --------------------------------------------------------------------------- #
# 4) narrow_scope honours BOTH overrides in ONE revised_args dict.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_coastal_narrow_scope_pins_both_overrides(monkeypatch) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    params = {"bbox": COASTAL_BBOX, "duration_hr": 6, "coastal": True}

    revised = {
        "grid_resolution_m": 100.0,
        "output_interval_min": 2.0,
        "duration_hr": 8.0,
    }
    driver = asyncio.create_task(_drive_decision(server, "narrow_scope", revised))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is True and effective["confirmed"] is True
    assert effective["grid_resolution_m"] == 100.0
    assert effective["enable_autoscale"] is False
    assert effective["output_interval_min"] == 2.0
    assert effective["duration_hr"] == 8.0


@pytest.mark.asyncio
async def test_coastal_narrow_scope_partial_override(monkeypatch) -> None:
    """Override ONLY the cadence; the resolution falls back to the suggestion."""
    from grace2_agent import server
    from grace2_agent.workflows.sfincs_builder import (
        suggest_sfincs_resolution_from_bbox,
    )

    expected = suggest_sfincs_resolution_from_bbox(tuple(COASTAL_BBOX))

    ws, state = _FakeWS(), _FakeState()
    params = {"bbox": COASTAL_BBOX, "duration_hr": 6, "coastal": True}

    revised = {"output_interval_min": 15.0}
    driver = asyncio.create_task(_drive_decision(server, "narrow_scope", revised))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is True
    # Cadence changed.
    assert effective["output_interval_min"] == 15.0
    # Resolution untouched -> pinned to the suggestion.
    assert effective["grid_resolution_m"] == expected.grid_resolution_m


@pytest.mark.asyncio
async def test_coastal_narrow_scope_interval_floored(monkeypatch) -> None:
    """A below-floor cadence override is floored at 1 min (deck floor parity)."""
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    params = {"bbox": COASTAL_BBOX, "duration_hr": 6, "coastal": True}

    revised = {"output_interval_min": 0.1}
    driver = asyncio.create_task(_drive_decision(server, "narrow_scope", revised))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is True
    assert effective["output_interval_min"] == 1.0


# --------------------------------------------------------------------------- #
# 5) A PLUVIAL flood with a bbox: granularity present, time_scale ABSENT.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_pluvial_bbox_gate_has_granularity_no_time_scale(monkeypatch) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    # No coastal/quadtree/surge signal -> pluvial -> hourly cadence (no row).
    params = {"bbox": COASTAL_BBOX, "duration_hr": 12}

    driver = asyncio.create_task(_drive_decision(server, "proceed"))
    should_run, effective = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is True
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    pl = card["payload"]
    assert pl["granularity"] is not None  # spatial resolution still a lever
    assert pl["time_scale"] is None  # no temporal row on the pluvial path
    # narrow_scope still offered (resolution override is meaningful).
    assert "narrow_scope" in pl["options"]
    # proceed pins the suggested resolution but NO output_interval_min (pluvial
    # keeps the legacy hourly default untouched).
    assert "grid_resolution_m" in effective
    assert "output_interval_min" not in effective


# --------------------------------------------------------------------------- #
# 6) A bbox-less pluvial flood carries neither block + fails closed on
#    narrow_scope (the card only offered proceed/cancel).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bbox_less_pluvial_narrow_scope_fails_closed(monkeypatch) -> None:
    from grace2_agent import server

    ws, state = _FakeWS(), _FakeState()
    params = {"location_query": "Fort Myers, Florida"}

    driver = asyncio.create_task(
        _drive_decision(server, "narrow_scope", {"grid_resolution_m": 50.0})
    )
    should_run, _ = await server._gate_on_solver_confirm(  # type: ignore[arg-type]
        ws, state, "run_model_flood_scenario", params
    )
    await driver

    assert should_run is False
    card = next(e for e in ws.sent if e.get("type") == "tool-payload-warning")
    pl = card["payload"]
    assert pl["granularity"] is None
    assert pl["time_scale"] is None
    assert pl["options"] == ["proceed", "cancel"]
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"
