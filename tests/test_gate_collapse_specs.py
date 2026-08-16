"""Gate-collapse (ADR 0273) spec + provider regression guards.

Verifies NATE's design call landed: the per-engine confirm gates are built from
tool METADATA (a declared ``GateSpec``), not the hand-wired ``SOLVER_CONFIRM_TOOLS``
/ ``FETCH_CONFIRM_TOOLS`` name-set literals + a per-engine ``if/elif`` chain.

- Every previously-gated tool carries a ``gate_spec`` of the right kind, and the
  registry-DERIVED membership views match the historical sets exactly.
- The hand-wired name-set LITERALS are gone from ``_core`` (absence guard).
- Each spec's declared estimate / pin providers IMPORT and are the right shape.
- BYTE-EQUIVALENCE: for the pure-arithmetic proceed/cancel engines the estimate
  provider's envelope is byte-identical to the pre-collapse builder's envelope
  (modulo the per-call ``warning_id`` ULID).
"""
from __future__ import annotations

import inspect

import pytest

import trid3nt_server  # noqa: F401 -- triggers tool registration
from trid3nt_server.agent.gates.cards import solver_confirm as sc
from trid3nt_server.agent.gates.cards.estimate import CardEstimate, resolve_provider
from trid3nt_server.agent.tools import TOOL_REGISTRY
from trid3nt_server.server import _core


_EXPECTED_SOLVER = {
    "sfincs_flood",
    "swmm_urban_flood",
    "openquake_psha",
    "openquake_scenario_gmf",
    "openquake_secondary_perils",
    "telemac_river_dye",
    "elmfire_fire_spread",
    "geoclaw_inundation",
    "geoclaw_tsunami_gauge_timeseries",
}
_EXPECTED_FETCH = {"fetch_dem", "fetch_topobathy", "fetch_landcover"}


# --- membership is derived from metadata --- #

def test_every_solver_tool_declares_a_solver_gate_spec() -> None:
    for name in _EXPECTED_SOLVER:
        gs = TOOL_REGISTRY[name].metadata.gate_spec
        assert gs is not None, f"{name} lost its gate_spec"
        assert gs.kind == "solver", name


def test_every_fetch_tool_declares_a_fetch_gate_spec() -> None:
    for name in _EXPECTED_FETCH:
        gs = TOOL_REGISTRY[name].metadata.gate_spec
        assert gs is not None, f"{name} lost its gate_spec"
        assert gs.kind == "fetch", name


def test_derived_views_match_historical_sets() -> None:
    assert set(_core.SOLVER_CONFIRM_TOOLS) == _EXPECTED_SOLVER
    assert set(_core.FETCH_CONFIRM_TOOLS) == _EXPECTED_FETCH
    # a fetch is NOT a solve: the two lanes never overlap.
    assert not (_core.FETCH_CONFIRM_TOOLS & _core.SOLVER_CONFIRM_TOOLS)


# --- absence guard: the hand-wired literals are gone --- #

def test_hand_wired_name_set_literals_are_deleted() -> None:
    src = inspect.getsource(_core)
    assert "SOLVER_CONFIRM_TOOLS: set[str] = {" not in src
    assert "FETCH_CONFIRM_TOOLS: set[str] = {" not in src
    # the seven per-engine locals + the per-engine card-building if/elif chain
    # are gone: the old orchestrator function name is replaced by the generic
    # engine + a thin compat shim.
    assert "async def _gate_on_confirm(" in src
    assert "swmm_autoscale: Any = None" not in src
    assert "flood_grid_autoscale: Any = None" not in src


# --- providers import and are the right shape --- #

@pytest.mark.parametrize("name", sorted(_EXPECTED_SOLVER | _EXPECTED_FETCH))
def test_declared_providers_import(name: str) -> None:
    gs = TOOL_REGISTRY[name].metadata.gate_spec
    est = resolve_provider(gs.estimate_provider)
    assert callable(est)
    if gs.pin_provider is not None:
        assert callable(resolve_provider(gs.pin_provider))
    # a gate with levers MUST name a pin provider (contract-enforced, re-checked
    # here so a registration that drops the pin fails loudly).
    if gs.levers:
        assert gs.pin_provider is not None, name


# --- byte-equivalence: pure-arithmetic proceed/cancel engines --- #

def _dump_no_wid(env) -> dict:
    d = env.model_dump()
    d.pop("warning_id", None)
    return d


def test_psha_estimate_provider_byte_equivalent() -> None:
    params = {
        "bbox": [-122.5, 37.7, -122.3, 37.9],
        "imt": "PGA",
        "poe": 0.10,
        "investigation_time_years": 50.0,
    }
    est = sc.estimate_psha(params)
    assert isinstance(est, CardEstimate)
    assert _dump_no_wid(est.envelope) == _dump_no_wid(
        sc._build_psha_confirm_envelope(params)
    )


@pytest.mark.parametrize(
    "tool", ["openquake_scenario_gmf", "openquake_secondary_perils"]
)
def test_scenario_estimate_provider_byte_equivalent(tool: str) -> None:
    params = {"bbox": [-118.5, 33.9, -118.2, 34.1], "magnitude": 6.7}
    est = sc.estimate_scenario(params, tool_name=tool)
    assert _dump_no_wid(est.envelope) == _dump_no_wid(
        sc._build_scenario_confirm_envelope(params, tool)
    )


def test_fire_estimate_provider_byte_equivalent() -> None:
    params = {
        "bbox": [-120.5, 38.9, -120.3, 39.1],
        "ignition_lonlat": [-120.4, 39.0],
        "cellsize_m": 30.0,
        "duration_hours": 6.0,
    }
    est = sc.estimate_fire(params)
    assert _dump_no_wid(est.envelope) == _dump_no_wid(
        sc._build_fire_confirm_envelope(params)
    )


@pytest.mark.parametrize("scenario", ["dam_break", "tsunami"])
def test_geoclaw_estimate_provider_byte_equivalent(scenario: str) -> None:
    params = {
        "bbox": [-124.5, 41.9, -124.2, 42.1],
        "scenario": scenario,
        "sim_duration_s": 3600.0,
        "amr_levels": 2,
    }
    est = sc.estimate_geoclaw(params)
    assert _dump_no_wid(est.envelope) == _dump_no_wid(
        sc._build_geoclaw_confirm_envelope(params)
    )


# --- pin providers: the plain proceed/cancel tail (no lever) --- #

def test_simple_solver_gate_has_no_pin_provider() -> None:
    # psha / scenario / fire / geoclaw are plain proceed/cancel: no lever, no pin.
    for name in (
        "openquake_psha",
        "openquake_scenario_gmf",
        "openquake_secondary_perils",
        "elmfire_fire_spread",
        "geoclaw_inundation",
        "geoclaw_tsunami_gauge_timeseries",
    ):
        gs = TOOL_REGISTRY[name].metadata.gate_spec
        assert gs.levers == ()
        assert gs.pin_provider is None, name
