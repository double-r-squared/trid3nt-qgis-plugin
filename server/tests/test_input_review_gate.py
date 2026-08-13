"""Offline tests for the two-mode INPUT_REQUIRED review gate (ADR 0107).

Mirrors the granularity-gate test pattern (a background driver resolves the
single pending ``_PENDING_CONFIRMATIONS`` future): no network, no daemon. Pins
the mode lever, the review envelope shape, the provide-values re-resolve loop,
the 3-round bound, and the fail-open (no-session / auto) paths.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_server.agent.gates import pending
from trid3nt_server.agent.gates.input_review import (
    ReviewOutcome,
    gate_input_review,
    render_input_review_lines,
    resolve_input_gate_mode,
)
from trid3nt_server.emission import pipeline_emitter as pe


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeEmitter:
    """Minimal emitter: records envelopes, exposes a session_id."""

    def __init__(self, session_id: str = "sess-review") -> None:
        self.session_id = session_id
        self.sent: list[tuple[str, object]] = []

    async def send_envelope(self, message_type: str, payload: object) -> None:
        self.sent.append((message_type, payload))


def _entries() -> list[SyntheticInput]:
    return [
        SyntheticInput(param="dam_break_depth_m", value=44.2, units="m",
                       basis="fetched",
                       real_source_if_any="fetch_usace_dams"),
        SyntheticInput(param="source_magnitude", value=8.0, units="Mw",
                       basis="default_demo"),
    ]


async def _drive(
    decision: str, revised_args=None, *, seen: set[str] | None = None,
    appear_timeout=5.0,
) -> str:
    """Resolve the next FRESH pending gate future once it appears.

    ``seen`` tracks warning_ids already resolved this test so a multi-round
    script never races on an already-resolved-but-not-yet-popped future (the
    helper pops in a ``finally`` one await after ``set_result``).
    """
    seen = seen if seen is not None else set()
    for _ in range(int(appear_timeout / 0.005)):
        fresh = [
            (wid, fut)
            for wid, (_sess, fut) in pending._PENDING_CONFIRMATIONS.items()
            if wid not in seen and not fut.done()
        ]
        if fresh:
            wid, fut = fresh[0]
            seen.add(wid)
            fut.set_result(
                PayloadConfirmationEnvelopePayload(
                    warning_id=wid, decision=decision, revised_args=revised_args
                )
            )
            return wid
        await asyncio.sleep(0.005)
    raise AssertionError("no fresh pending confirmation appeared")


# --------------------------------------------------------------------------- #
# Mode lever
# --------------------------------------------------------------------------- #
def test_mode_default_auto(monkeypatch) -> None:
    monkeypatch.delenv("TRID3NT_INPUT_GATE_MODE", raising=False)
    assert resolve_input_gate_mode(None) == "auto"
    assert resolve_input_gate_mode("garbage") == "auto"


def test_mode_param_overrides_session_default(monkeypatch) -> None:
    monkeypatch.setenv("TRID3NT_INPUT_GATE_MODE", "auto")
    assert resolve_input_gate_mode("user_gated") == "user_gated"
    monkeypatch.setenv("TRID3NT_INPUT_GATE_MODE", "user_gated")
    assert resolve_input_gate_mode(None) == "user_gated"
    assert resolve_input_gate_mode("auto") == "auto"


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #
def test_render_lines_one_per_input() -> None:
    lines = render_input_review_lines(_entries())
    assert lines == [
        "dam_break_depth_m = 44.2 m [site-derived, fetch_usace_dams]",
        "source_magnitude = 8.0 Mw [demo default]",
    ]


# --------------------------------------------------------------------------- #
# Auto mode: no-op pass-through (no pause, no envelope)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_mode_is_noop(monkeypatch) -> None:
    monkeypatch.delenv("TRID3NT_INPUT_GATE_MODE", raising=False)
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    out = await gate_input_review(
        tool_name="geoclaw_inundation", mode="auto",
        entries=_entries(), params={"dam_break_depth_m": 44.2},
    )
    assert out.proceed is True and out.cancelled is False
    assert out.mode == "auto"
    assert fake.sent == []  # no pause envelope


# --------------------------------------------------------------------------- #
# user_gated with NO live session: fail-open (proceed, labeled)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_user_gated_no_session_fails_open(monkeypatch) -> None:
    monkeypatch.setattr(pe, "current_emitter", lambda: None)
    out = await gate_input_review(
        tool_name="geoclaw_inundation", mode="user_gated",
        entries=_entries(), params={"dam_break_depth_m": 44.2},
    )
    assert out.proceed is True and out.cancelled is False
    assert out.mode == "user_gated"


# --------------------------------------------------------------------------- #
# user_gated proceed: pause emitted, entries stamped unchanged
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_user_gated_proceed(monkeypatch) -> None:
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    driver = asyncio.create_task(_drive("proceed"))
    out = await gate_input_review(
        tool_name="geoclaw_inundation", mode="user_gated",
        entries=_entries(), params={"dam_break_depth_m": 44.2},
    )
    await driver
    assert out.proceed is True and out.rounds_used == 1
    assert len(fake.sent) == 1
    mtype, env = fake.sent[0]
    assert mtype == "tool-payload-warning"
    assert env.synthetic_inputs is not None
    assert env.options == ["proceed", "narrow_scope", "cancel"]
    assert "dam_break_depth_m = 44.2 m" in env.recommendation
    # what-was-approved == what-ran: entries returned intact for stamping.
    assert [e.param for e in out.entries] == [
        "dam_break_depth_m", "source_magnitude"]
    assert not pending._PENDING_CONFIRMATIONS  # future cleaned up


# --------------------------------------------------------------------------- #
# user_gated cancel: no run
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_user_gated_cancel(monkeypatch) -> None:
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    driver = asyncio.create_task(_drive("cancel"))
    out = await gate_input_review(
        tool_name="geoclaw_inundation", mode="user_gated",
        entries=_entries(), params={"dam_break_depth_m": 44.2},
    )
    await driver
    assert out.proceed is False and out.cancelled is True
    assert not pending._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# provide values (narrow_scope): revise a param -> re-present -> proceed.
# The revised entry flips to user basis + the value updates (what-ran == approved).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_provide_values_then_proceed(monkeypatch) -> None:
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)

    async def _script() -> None:
        seen: set[str] = set()
        # round 1: revise the injection rate.
        await _drive("narrow_scope", revised_args={"injection_rate_m3_day": 999.0},
                     seen=seen)
        # round 2: approve.
        await _drive("proceed", seen=seen)

    driver = asyncio.create_task(_script())
    entries = [
        SyntheticInput(param="injection_rate_m3_day", value=500.0,
                       units="m^3/day", basis="user"),
        SyntheticInput(param="aquifer_k_ms", value=None, basis="default_demo"),
    ]
    out = await gate_input_review(
        tool_name="modflow_asr", mode="user_gated",
        entries=entries,
        params={"injection_rate_m3_day": 500.0, "aquifer_k_ms": None},
    )
    await driver
    assert out.proceed is True and out.rounds_used == 2
    assert out.params["injection_rate_m3_day"] == 999.0
    inj = next(e for e in out.entries if e.param == "injection_rate_m3_day")
    assert inj.value == 999.0 and inj.basis == "user"
    assert len(fake.sent) == 2  # two presentations


# --------------------------------------------------------------------------- #
# 3-round bound: three provide-values in a row -> honest cancel.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_three_round_bound_then_cancel(monkeypatch) -> None:
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)

    async def _script() -> None:
        seen: set[str] = set()
        for _ in range(3):
            await _drive("narrow_scope", revised_args={"amr_levels": 3}, seen=seen)

    driver = asyncio.create_task(_script())
    out = await gate_input_review(
        tool_name="geoclaw_inundation", mode="user_gated",
        entries=_entries(), params={"amr_levels": 2},
    )
    await driver
    assert out.proceed is False and out.cancelled is True
    assert "3 rounds" in (out.cancel_reason or "")
    assert len(fake.sent) == 3
    assert not pending._PENDING_CONFIRMATIONS


# --------------------------------------------------------------------------- #
# reresolve callback: a provide-values reply can re-run a fetcher.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reresolve_callback_invoked(monkeypatch) -> None:
    fake = _FakeEmitter()
    monkeypatch.setattr(pe, "current_emitter", lambda: fake)
    seen: list[dict] = []

    async def _reresolve(params):
        seen.append(dict(params))
        return (
            [SyntheticInput(param="dam_break_depth_m", value=12.0, units="m",
                            basis="fetched", real_source_if_any="fetch_usace_dams")],
            params,
        )

    async def _script() -> None:
        seen: set[str] = set()
        await _drive("narrow_scope", revised_args={"dam_name": "Other Dam"},
                     seen=seen)
        await _drive("proceed", seen=seen)

    driver = asyncio.create_task(_script())
    out = await gate_input_review(
        tool_name="geoclaw_inundation", mode="user_gated",
        entries=_entries(), params={"dam_name": "A"},
        reresolve=_reresolve,
    )
    await driver
    assert out.proceed is True
    assert seen and seen[0]["dam_name"] == "Other Dam"
    assert out.entries[0].value == 12.0
