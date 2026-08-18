"""ADR 0223: shared MODFLOW archetype input-review helper unit tests.

Proves the prose demo-aquifer caveats become STRUCTURED SyntheticInput review
entries routed through gate_input_review and stamped onto the layer envelope,
without a live solve.
"""

from __future__ import annotations

import pytest

from trid3nt_contracts.execution import LayerURI

from trid3nt_server.workflows.modflow._input_review import (
    aquifer_k_basis,
    aquifer_k_review_entry,
    gate_and_stamp_modflow_inputs,
    review_modflow_entries,
)


def test_aquifer_k_basis_mapping() -> None:
    assert aquifer_k_basis("user_supplied") == ("user", None)
    basis, src = aquifer_k_basis("soil_pedotransfer")
    assert basis == "derived" and "Saxton-Rawls" in src
    assert aquifer_k_basis("demo_default") == ("default_demo", None)
    assert aquifer_k_basis("") == ("default_demo", None)


def test_aquifer_k_review_entry_keeps_prose_note() -> None:
    note = "Aquifer K=0.0001 m/s and porosity=0.3 are demo defaults."
    e = aquifer_k_review_entry(
        k_source="demo_default", k_ms=1e-4, porosity=0.3, note=note)
    assert e.param == "aquifer_k_ms"
    assert e.basis == "default_demo"
    assert e.units == "m/s"
    assert e.note == note


def _layer() -> LayerURI:
    return LayerURI(
        layer_id="x", name="x", layer_type="raster", uri="s3://b/x.tif",
        style_preset="p", role="primary",
    )


@pytest.mark.asyncio
async def test_gate_and_stamp_auto_mode_stamps_resolved_entries() -> None:
    """auto mode stamps a RESOLVED (user-supplied) entry onto the layer."""
    entry = aquifer_k_review_entry(
        k_source="user_supplied", k_ms=5e-5, porosity=0.3, note="user K")
    layer, review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_x", layer=_layer(), entries=[entry], input_mode="auto")
    assert review.proceed is True
    assert review.cancelled is False
    assert len(layer.synthetic_inputs) == 1
    assert layer.synthetic_inputs[0].param == "aquifer_k_ms"


@pytest.mark.asyncio
async def test_gate_and_stamp_auto_mode_refuses_demo_physics() -> None:
    """auto mode REFUSES an unresolved demo aquifer K (law 9): the layer is not
    stamped, the review cancels with a typed PHYSICS_INPUT_REQUIRED reason."""
    entry = aquifer_k_review_entry(
        k_source="demo_default", k_ms=1e-4, porosity=0.3, note="demo")
    layer, review = await gate_and_stamp_modflow_inputs(
        tool_name="modflow_x", layer=_layer(), entries=[entry], input_mode="auto")
    assert review.proceed is False and review.cancelled is True
    assert "PHYSICS_INPUT_REQUIRED" in (review.cancel_reason or "")
    assert layer.synthetic_inputs == []  # nothing stamped on a refused run


@pytest.mark.asyncio
async def test_review_modflow_entries_passthrough() -> None:
    entry = aquifer_k_review_entry(
        k_source="user_supplied", k_ms=5e-5, porosity=0.25, note="user K")
    review = await review_modflow_entries(
        tool_name="modflow_x", entries=[entry], input_mode="auto")
    assert review.proceed is True
    assert [e.param for e in review.entries] == ["aquifer_k_ms"]
    assert review.entries[0].basis == "user"
