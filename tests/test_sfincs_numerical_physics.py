"""Offline coverage for the ``sfincs_advanced_numerical_physics_knobs`` template.

Composer-level determinism (no live solve): registration identity, knob
validation (out-of-range/typed rejection before any dispatch), and exactly what
the composer threads onto the SFINCS flood pipeline (the resolved advanced_physics
delta, or ``None`` for a byte-identical baseline).

ASCII only.
"""

from __future__ import annotations

import types

import pytest

import trid3nt_server.workflows.sfincs.flood.flood as flood_mod
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.sfincs.numerical_physics.numerical_physics import (
    sfincs_advanced_numerical_physics_knobs,
)

TOOL = "sfincs_advanced_numerical_physics_knobs"


def _capture(monkeypatch):
    """Patch model_flood_scenario to capture kwargs and return a sentinel."""
    seen: dict = {}

    async def _fake(**kwargs):
        seen.update(kwargs)
        return types.SimpleNamespace(envelope_id="ENV-TEST", bbox=None)

    monkeypatch.setattr(flood_mod, "model_flood_scenario", _fake)
    return seen


def test_registered_as_sfincs_template():
    assert TOOL in TOOL_REGISTRY
    md = TOOL_REGISTRY[TOOL].metadata
    assert md.tier == "template"
    assert md.engine == "sfincs"
    assert md.cacheable is False


@pytest.mark.asyncio
async def test_valid_knobs_thread_resolved_delta(monkeypatch):
    seen = _capture(monkeypatch)
    out = await sfincs_advanced_numerical_physics_knobs(
        location_query="Boulder, CO", theta=0.9, advection=0, huthresh=0.02,
        return_period_yr=25, duration_hr=12,
    )
    assert out.envelope_id == "ENV-TEST"
    assert seen["advanced_physics"] == {"theta": 0.9, "advection": 0, "huthresh": 0.02}
    assert seen["location_query"] == "Boulder, CO"
    assert seen["return_period_yr"] == 25
    assert seen["duration_hr"] == 12


@pytest.mark.asyncio
async def test_no_knob_is_byte_identical_baseline(monkeypatch):
    seen = _capture(monkeypatch)
    await sfincs_advanced_numerical_physics_knobs(location_query="Boulder, CO")
    # No override -> advanced_physics is None -> deck byte-identical to sfincs_flood.
    assert seen["advanced_physics"] is None


@pytest.mark.asyncio
async def test_out_of_range_knob_rejected_before_dispatch(monkeypatch):
    seen = _capture(monkeypatch)
    # theta manual range is [0.8, 1.0]; 2.0 is out of range.
    out = await sfincs_advanced_numerical_physics_knobs(location_query="X", theta=2.0)
    assert isinstance(out, dict)
    assert out["status"] == "error"
    assert out["error_code"] == "ADVANCED_PHYSICS_INVALID"
    assert seen == {}  # never dispatched to the flood pipeline


@pytest.mark.asyncio
async def test_invalid_advection_value_rejected(monkeypatch):
    seen = _capture(monkeypatch)
    # SFINCS advection is only 0 or 1; 2 must be rejected (Invariant 7).
    out = await sfincs_advanced_numerical_physics_knobs(location_query="X", advection=2)
    assert isinstance(out, dict)
    assert out["error_code"] == "ADVANCED_PHYSICS_INVALID"
    assert seen == {}


@pytest.mark.asyncio
async def test_wind_drag_curve_knob_threads_resolved_delta(monkeypatch):
    """ADR 0162: ``wind_drag_curve`` (a list of (wind_mps, cd) pairs) resolves
    through the SAME physics_registry validator + threads onto
    ``model_flood_scenario`` alongside the other knobs."""
    seen = _capture(monkeypatch)
    curve = [[0.0, 0.001], [28.0, 0.0025], [50.0, 0.0018]]
    out = await sfincs_advanced_numerical_physics_knobs(
        location_query="Boulder, CO", wind_drag_curve=curve,
    )
    assert out.envelope_id == "ENV-TEST"
    assert seen["advanced_physics"]["wind_drag_curve"] == (
        (0.0, 0.001), (28.0, 0.0025), (50.0, 0.0018),
    )


@pytest.mark.asyncio
async def test_wind_drag_curve_out_of_range_rejected(monkeypatch):
    seen = _capture(monkeypatch)
    # A single pair is below the >=2 breakpoint minimum -- typed rejection.
    out = await sfincs_advanced_numerical_physics_knobs(
        location_query="X", wind_drag_curve=[[0.0, 0.001]],
    )
    assert isinstance(out, dict)
    assert out["error_code"] == "ADVANCED_PHYSICS_INVALID"
    assert seen == {}
