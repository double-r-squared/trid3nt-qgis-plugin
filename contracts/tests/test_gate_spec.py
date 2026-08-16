"""Tests for the confirm-gate declaration contract (ADR 0273, the gate-collapse).

Verifies the declarative carrier NATE's gate-collapse rides on:
- :class:`GateSpec` / :class:`LeverSpec` construct and validate.
- A gate declaring levers MUST name a pin provider (a lever with no pin is a
  dead knob).
- A lever declares a discrete ladder XOR a continuous window (not both), and a
  window's min <= max.
- ``AtomicToolMetadata`` carries an OPTIONAL ``gate_spec`` (default None -> every
  un-gated tool is unaffected; additive, same shape as ``resolution_specs``).
- JSON serialize -> deserialize round-trips; ``extra="forbid"`` is inherited.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from trid3nt_contracts.gate_spec import GateSpec, LeverSpec
from trid3nt_contracts.tool_registry import AtomicToolMetadata


def _solver_lever_gate() -> GateSpec:
    return GateSpec(
        kind="solver",
        estimate_provider="pkg.mod:estimate",
        pin_provider="pkg.mod:pin",
        levers=(
            LeverSpec(name="grid resolution", param="grid_resolution_m", unit="m"),
        ),
        title="Grid gate",
        rationale="A consequential solve.",
    )


# --- construction --- #

def test_plain_proceed_cancel_gate_needs_no_pin() -> None:
    gs = GateSpec(kind="solver", estimate_provider="pkg.mod:estimate")
    assert gs.kind == "solver"
    assert gs.pin_provider is None
    assert gs.levers == ()


def test_fetch_gate_with_lever_and_pin() -> None:
    gs = GateSpec(
        kind="fetch",
        estimate_provider="pkg.mod:estimate_fetch",
        pin_provider="pkg.mod:pin_fetch",
        levers=(LeverSpec(name="fetch resolution", param="resolution_m"),),
    )
    assert gs.kind == "fetch"
    assert gs.levers[0].param == "resolution_m"


def test_lever_with_discrete_rungs() -> None:
    lev = LeverSpec(name="r", param="p", rungs=(20.0, 50.0, 100.0))
    assert lev.rungs == (20.0, 50.0, 100.0)
    assert lev.pin_on_proceed is True


def test_lever_with_continuous_window() -> None:
    lev = LeverSpec(name="r", param="p", range_min=1.0, range_max=900.0)
    assert lev.range_min == 1.0 and lev.range_max == 900.0


# --- validators --- #

def test_lever_declaring_a_gate_without_pin_is_rejected() -> None:
    with pytest.raises(ValidationError, match="dead knob"):
        GateSpec(
            kind="solver",
            estimate_provider="pkg.mod:estimate",
            levers=(LeverSpec(name="r", param="p"),),
        )


def test_lever_rungs_and_window_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        LeverSpec(name="r", param="p", rungs=(1.0,), range_min=1.0)


def test_lever_window_min_le_max() -> None:
    with pytest.raises(ValidationError, match="range_min"):
        LeverSpec(name="r", param="p", range_min=900.0, range_max=1.0)


def test_kind_is_constrained() -> None:
    with pytest.raises(ValidationError):
        GateSpec(kind="not_a_kind", estimate_provider="pkg.mod:estimate")


def test_estimate_provider_required_nonempty() -> None:
    with pytest.raises(ValidationError):
        GateSpec(kind="solver", estimate_provider="")


# --- attachment to AtomicToolMetadata --- #

def test_metadata_gate_spec_defaults_none() -> None:
    m = AtomicToolMetadata(
        name="x", ttl_class="live-no-cache", cacheable=False
    )
    assert m.gate_spec is None


def test_metadata_carries_gate_spec() -> None:
    m = AtomicToolMetadata(
        name="y",
        ttl_class="live-no-cache",
        cacheable=False,
        gate_spec=_solver_lever_gate(),
    )
    assert m.gate_spec is not None
    assert m.gate_spec.kind == "solver"
    assert m.gate_spec.pin_provider == "pkg.mod:pin"


# --- round-trip + forbid-extra --- #

def test_gate_spec_json_roundtrip() -> None:
    gs = _solver_lever_gate()
    reloaded = GateSpec.model_validate_json(gs.model_dump_json())
    assert reloaded == gs


def test_gate_spec_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        GateSpec(
            kind="solver",
            estimate_provider="pkg.mod:estimate",
            bogus_field=1,  # type: ignore[call-arg]
        )
