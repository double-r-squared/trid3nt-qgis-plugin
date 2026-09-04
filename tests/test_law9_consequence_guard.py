"""Law-9 sweep guard: an invented-physics demo default cannot be born or run.

Three enforcing layers (the demo-physics-defaults audit's nip-in-the-bud design):

  a. SCHEMA -- ``SyntheticInput(basis="default_demo")`` without a ``consequence``
     tag cannot construct; a pre-law-9 persisted record loads tolerantly.
  b. STATIC LINT -- every ``SyntheticInput(...)`` construction site in
     ``trid3nt_server/`` whose block names ``default_demo`` carries an explicit
     ``consequence=`` kwarg, so a new naked demo default fails here instead of
     shipping.
  c. BEHAVIORAL -- ``gate_input_review`` in auto mode (and the headless no-emitter
     path) REFUSES a ``consequence="physics"`` demo default while letting
     scenario / numerical / aoi demo defaults proceed.
"""

from __future__ import annotations

import pathlib

import pytest

from trid3nt_contracts.common import SyntheticInput
from trid3nt_server.gates.input_review import (
    gate_input_review,
    physics_refusal_reason,
)
from trid3nt_server.emission import pipeline_emitter as pe

_SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1] / "trid3nt_server"


# --------------------------------------------------------------------------- #
# (a) SCHEMA
# --------------------------------------------------------------------------- #
def test_schema_demo_without_consequence_cannot_construct() -> None:
    with pytest.raises(ValueError, match="consequence"):
        SyntheticInput(param="aquifer_k_ms", value=1e-4, basis="default_demo")


@pytest.mark.parametrize("consequence", ["physics", "scenario", "numerical", "aoi"])
def test_schema_demo_with_consequence_constructs(consequence) -> None:
    s = SyntheticInput(param="p", basis="default_demo", consequence=consequence)
    assert s.consequence == consequence


def test_schema_non_demo_basis_needs_no_consequence() -> None:
    assert SyntheticInput(param="p", basis="fetched").consequence is None
    assert SyntheticInput(param="p", basis="user").consequence is None


def test_schema_tolerant_history_read_backfills_scenario() -> None:
    """A pre-law-9 record (default_demo, no consequence) loads, never crashes."""
    old = {"param": "old", "value": 1.0, "basis": "default_demo"}
    loaded = SyntheticInput.model_validate(old, context={"tolerant_history": True})
    assert loaded.consequence == "scenario"
    assert "pre-law-9" in (loaded.note or "")
    # tolerance propagates to nested models on the envelope contracts.
    from trid3nt_contracts.execution import LayerURI

    env = {
        "layer_id": "l", "name": "t", "layer_type": "raster", "uri": "s3://x",
        "synthetic_inputs": [old],
    }
    lyr = LayerURI.model_validate(env, context={"tolerant_history": True})
    assert lyr.synthetic_inputs[0].consequence == "scenario"


# --------------------------------------------------------------------------- #
# (b) STATIC LINT
# --------------------------------------------------------------------------- #
def _synthetic_input_blocks(src: str):
    """Yield (line, block) for every balanced ``SyntheticInput(...)`` call."""
    i = 0
    while True:
        m = src.find("SyntheticInput(", i)
        if m < 0:
            return
        depth = 0
        k = src.find("(", m)
        while k < len(src):
            if src[k] == "(":
                depth += 1
            elif src[k] == ")":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        yield src[:m].count("\n") + 1, src[m:k + 1]
        i = k + 1


def test_lint_every_default_demo_site_carries_consequence() -> None:
    offenders = []
    for p in sorted(_SERVER_ROOT.rglob("*.py")):
        src = p.read_text()
        for line, block in _synthetic_input_blocks(src):
            if "default_demo" in block and "consequence=" not in block:
                offenders.append(f"{p.relative_to(_SERVER_ROOT.parent)}:{line}")
    assert not offenders, (
        "SyntheticInput sites naming default_demo without a consequence= tag "
        "(law 9 -- classify physics|scenario|numerical|aoi):\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------- #
# (c) BEHAVIORAL
# --------------------------------------------------------------------------- #
def _demo_entry(consequence: str) -> SyntheticInput:
    return SyntheticInput(param="p", value=None, basis="default_demo",
                          consequence=consequence, note="no real source")


def test_physics_refusal_reason_names_the_param() -> None:
    reason = physics_refusal_reason("t", [_demo_entry("physics")])
    assert reason and "PHYSICS_INPUT_REQUIRED" in reason and "user_gated" in reason
    assert physics_refusal_reason("t", [_demo_entry("scenario")]) is None


@pytest.mark.parametrize(
    "consequence,should_refuse",
    [("physics", True), ("scenario", False), ("numerical", False), ("aoi", False)],
)
@pytest.mark.asyncio
async def test_auto_mode_refuses_only_physics(monkeypatch, consequence, should_refuse) -> None:
    monkeypatch.delenv("TRID3NT_INPUT_GATE_MODE", raising=False)
    out = await gate_input_review(
        tool_name="some_tool", mode="auto",
        entries=[_demo_entry(consequence)], params={},
    )
    if should_refuse:
        assert out.proceed is False and out.cancelled is True
        assert "PHYSICS_INPUT_REQUIRED" in (out.cancel_reason or "")
    else:
        assert out.proceed is True and out.cancelled is False


@pytest.mark.asyncio
async def test_headless_no_emitter_refuses_physics(monkeypatch) -> None:
    monkeypatch.setattr(pe, "current_emitter", lambda: None)
    out = await gate_input_review(
        tool_name="some_tool", mode="user_gated",
        entries=[_demo_entry("physics")], params={},
    )
    assert out.proceed is False and out.cancelled is True
    assert "PHYSICS_INPUT_REQUIRED" in (out.cancel_reason or "")


@pytest.mark.asyncio
async def test_auto_mode_mixed_entries_refuses_when_any_physics(monkeypatch) -> None:
    monkeypatch.delenv("TRID3NT_INPUT_GATE_MODE", raising=False)
    entries = [
        SyntheticInput(param="dur_days", value=2, basis="default_demo",
                       consequence="scenario"),
        SyntheticInput(param="aquifer_k_ms", value=None, basis="default_demo",
                       consequence="physics", note="hydraulic conductivity required"),
    ]
    out = await gate_input_review(tool_name="t", mode="auto", entries=entries, params={})
    assert out.proceed is False
    assert "aquifer_k_ms" in (out.cancel_reason or "")
