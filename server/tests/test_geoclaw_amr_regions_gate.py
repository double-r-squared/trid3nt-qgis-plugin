"""Offline gate tests for ``geoclaw_amr_refinement_regions`` window review (ADR 0107).

The explicit AMR windows are the consequential, model-invented input on this
template (they place WHERE the mesh refines), so they must ride the input-review
gate. These pin, with NO solver / network (``model_geoclaw_inundation`` is
stubbed):

  1. user_gated: the resolved window SURFACES in the pending-inputs payload
     (``tool-payload-warning`` ``synthetic_inputs``) with the right ``basis``.
  2. window_basis="user" carries through as ``basis="user"``.
  3. cancel at review -> USER_INPUT_CANCELLED, the solver never runs.
  4. auto mode -> the window provenance is stamped onto the result (assumptions
     block) and the solve proceeds.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.geoclaw_contracts import GeoClawDepthLayerURI
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_server.agent.gates import pending
from trid3nt_server.agent.workflows.geoclaw.amr_regions import amr_regions as ar
from trid3nt_server.emission import pipeline_emitter as pe

_AOI = (-124.24, 41.73, -124.16, 41.78)
_WIN = {
    "min_level": 4, "max_level": 4, "t_start_s": 0.0, "t_end_s": 900.0,
    "min_lon": -124.21, "max_lon": -124.18, "min_lat": 41.745, "max_lat": 41.770,
}


class _FakeEmitter:
    def __init__(self, session_id: str = "sess-amr") -> None:
        self.session_id = session_id
        self.sent: list[tuple[str, object]] = []

    async def send_envelope(self, message_type: str, payload: object) -> None:
        self.sent.append((message_type, payload))


def _fake_layer() -> GeoClawDepthLayerURI:
    return GeoClawDepthLayerURI(
        layer_id="L1", name="geoclaw peak", layer_type="raster",
        uri="s3://runs/x/geoclaw_depth_peak.tif",
        style_preset="continuous_flood_depth",
        max_depth_m=0.9, flooded_area_km2=0.05, max_inundation_m=0.9,
    )


def _stub_model(monkeypatch):
    async def _fake_model(run_args, **_kw):
        return _fake_layer()
    monkeypatch.setattr(ar, "model_geoclaw_inundation", _fake_model)


async def _drive(decision: str, appear_timeout: float = 5.0) -> str:
    for _ in range(int(appear_timeout / 0.005)):
        fresh = [
            (wid, fut)
            for wid, (_s, fut) in pending._PENDING_CONFIRMATIONS.items()
            if not fut.done()
        ]
        if fresh:
            wid, fut = fresh[0]
            fut.set_result(
                PayloadConfirmationEnvelopePayload(warning_id=wid, decision=decision)
            )
            return wid
        await asyncio.sleep(0.005)
    raise AssertionError("no pending confirmation appeared")


@pytest.mark.asyncio
async def test_window_surfaces_in_pending_payload_prompt_interpreted(monkeypatch):
    _stub_model(monkeypatch)
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)

    task = asyncio.create_task(
        ar.geoclaw_amr_refinement_regions(
            bbox=_AOI, amr_regions=[_WIN], amr_levels=4, input_mode="user_gated",
        )
    )
    await _drive("proceed")
    res = await task

    # the pause envelope carried the window as a provenance entry with basis.
    warns = [p for mt, p in fake.sent if mt == "tool-payload-warning"]
    assert warns, "no review envelope emitted"
    si = warns[0].synthetic_inputs
    assert si is not None
    entry = next(e for e in si if e.param == "amr_window_1")
    assert entry.basis == "prompt_interpreted"
    assert "L4-4" in str(entry.value)
    # proceed -> the run completed and stamped the window onto the result.
    assert isinstance(res, GeoClawDepthLayerURI)
    assert any(e.param == "amr_window_1" for e in res.synthetic_inputs)


@pytest.mark.asyncio
async def test_window_basis_user_carries_through(monkeypatch):
    _stub_model(monkeypatch)
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)

    task = asyncio.create_task(
        ar.geoclaw_amr_refinement_regions(
            bbox=_AOI, amr_regions=[_WIN], amr_levels=4,
            input_mode="user_gated", window_basis="user",
        )
    )
    await _drive("proceed")
    await task
    warns = [p for mt, p in fake.sent if mt == "tool-payload-warning"]
    entry = next(e for e in warns[0].synthetic_inputs if e.param == "amr_window_1")
    assert entry.basis == "user"


@pytest.mark.asyncio
async def test_cancel_at_review_does_not_solve(monkeypatch):
    called = {"n": 0}

    async def _fake_model(run_args, **_kw):
        called["n"] += 1
        return _fake_layer()

    monkeypatch.setattr(ar, "model_geoclaw_inundation", _fake_model)
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)

    task = asyncio.create_task(
        ar.geoclaw_amr_refinement_regions(
            bbox=_AOI, amr_regions=[_WIN], amr_levels=4, input_mode="user_gated",
        )
    )
    await _drive("cancel")
    res = await task
    assert isinstance(res, dict) and res["error_code"] == "USER_INPUT_CANCELLED"
    assert called["n"] == 0  # the solver never ran


@pytest.mark.asyncio
async def test_auto_mode_stamps_window_and_proceeds(monkeypatch):
    _stub_model(monkeypatch)
    monkeypatch.setattr(pe, "current_emitter", lambda: None)  # no session
    res = await ar.geoclaw_amr_refinement_regions(
        bbox=_AOI, amr_regions=[_WIN], amr_levels=4, input_mode="auto",
    )
    assert isinstance(res, GeoClawDepthLayerURI)
    win = [e for e in res.synthetic_inputs if e.param == "amr_window_1"]
    assert win and win[0].basis == "prompt_interpreted"


# --------------------------------------------------------------------------- #
# ADR 0159: a turn-bound drawn geometry OVERRIDES the model's proposal
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_drawn_geometry_overrides_model_proposal(monkeypatch):
    """A user-drawn region bound to the turn REPLACES the model's
    prompt-interpreted ``amr_regions`` with ONE finest-level window over the drawn
    bbox and forces ``basis="user"`` -- the WHERE-to-refine input becomes an
    explicit user choice, not an LLM guess."""
    _stub_model(monkeypatch)
    monkeypatch.setattr(pe, "current_emitter", lambda: None)  # auto (gate fails open)
    drawn_bbox = [-124.2000, 41.7500, -124.1850, 41.7650]
    tok = pe.bind_turn_drawn_geometry(
        {"geometry_type": "rectangle", "bbox": drawn_bbox}
    )
    try:
        res = await ar.geoclaw_amr_refinement_regions(
            # model proposed _WIN (a DIFFERENT box) + prompt_interpreted basis;
            # the drawn geometry must win.
            bbox=_AOI, amr_regions=[_WIN], amr_levels=4, input_mode="auto",
            window_basis="prompt_interpreted", sim_duration_s=1800.0,
        )
    finally:
        pe._TURN_DRAWN_GEOMETRY.reset(tok)

    assert isinstance(res, GeoClawDepthLayerURI)
    wins = [e for e in res.synthetic_inputs if e.param.startswith("amr_window_")]
    # exactly ONE window (the model's _WIN was REPLACED, not appended).
    assert len(wins) == 1
    assert wins[0].basis == "user"
    # the DRAWN bbox coordinates surface (not the model's _WIN coords).
    v = str(wins[0].value)
    assert "-124.2000" in v and "-124.1850" in v
    assert "41.7500" in v and "41.7650" in v


@pytest.mark.asyncio
async def test_no_drawn_geometry_keeps_model_proposal(monkeypatch):
    """With NO drawn geometry bound the model's window + basis flow through
    unchanged (byte-identical to the pre-ADR-0159 path)."""
    _stub_model(monkeypatch)
    monkeypatch.setattr(pe, "current_emitter", lambda: None)
    # Ensure the contextvar is clear (no leakage from a prior test).
    tok = pe.bind_turn_drawn_geometry(None)
    try:
        res = await ar.geoclaw_amr_refinement_regions(
            bbox=_AOI, amr_regions=[_WIN], amr_levels=4, input_mode="auto",
        )
    finally:
        pe._TURN_DRAWN_GEOMETRY.reset(tok)
    win = [e for e in res.synthetic_inputs if e.param == "amr_window_1"]
    assert win and win[0].basis == "prompt_interpreted"
    # the model's _WIN coordinates (lon -124.21..-124.18) are preserved.
    assert "-124.2100" in str(win[0].value)
