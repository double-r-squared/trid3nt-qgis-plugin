"""Gate-collapse (ADR 0273) spec + provider regression guards.

Verifies NATE's design call landed: the confirm gates are built from tool
METADATA (a declared ``GateSpec``), not the hand-wired ``SOLVER_CONFIRM_TOOLS``
/ ``FETCH_CONFIRM_TOOLS`` name-set literals + a per-engine ``if/elif`` chain.

- The registry-DERIVED membership views match what the tree declares.
- The hand-wired name-set LITERALS are gone from ``_core`` (absence guard).
- Each spec's declared estimate / pin providers IMPORT and are the right shape.

Every engine template in the tree now stops at the STANDARD MESH GATE, so the
solver lane is EMPTY and the fetch lane carries the whole surface. That emptiness
is asserted rather than assumed: a template that re-introduces a per-engine
approve card has to say so here.
"""
from __future__ import annotations

import inspect

import pytest

import trid3nt_server  # noqa: F401 -- triggers tool registration
from trid3nt_server.gates.cards.estimate import resolve_provider
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.gates import confirm as _core


#: No engine declares a per-engine solver card: the mesh session presents the
#: mesh it built and mounts the mesher's own edit actions, which is a superset of
#: what an approve-mesh card could offer.
_EXPECTED_SOLVER: set[str] = set()
_EXPECTED_FETCH = {"fetch_dem", "fetch_topobathy", "fetch_bluetopo", "fetch_landcover"}


# --- membership is derived from metadata --- #

def test_every_fetch_tool_declares_a_fetch_gate_spec() -> None:
    for name in _EXPECTED_FETCH:
        gs = TOOL_REGISTRY[name].metadata.gate_spec
        assert gs is not None, f"{name} lost its gate_spec"
        assert gs.kind == "fetch", name


def test_every_engine_template_is_mesh_gated() -> None:
    """An engine template declares NO solver gate spec - it stops at the mesh gate."""
    for name, tool in TOOL_REGISTRY.items():
        if getattr(tool.metadata, "tier", None) != "template":
            continue
        assert tool.metadata.gate_spec is None, name
        assert name not in _core.SOLVER_CONFIRM_TOOLS, name


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
