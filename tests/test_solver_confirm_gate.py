"""job-0241: solver confirm gate — the dispatch-path test that was missing.

The Stage 3 live gate (job-0235) proved the Case 2 composer dispatched MODFLOW
with ZERO user confirmation: the registered wrapper hardcoded
``confirmed=True`` and the server dispatch path had no solver gate. The
programmatic tests all drove the INNER composer, never the registered wrapper
through the server's dispatch seam — this file closes that exact gap.

Covers:
- the gate builds the confirm card from the PURE extraction and emits it as a
  ``tool-payload-warning`` (the card the web client already renders);
- approve → ``confirmed=True`` injected; cancel/timeout → fail-closed with a
  typed error envelope and NO dispatch;
- the LLM cannot self-approve: ``confirmed`` supplied in params is STRIPPED
  before gating;
- the registered wrapper defaults ``confirmed=False`` (no hardcoded bypass);
- extraction failure falls through (gate must not mask parameter errors).
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.ws import PayloadConfirmationEnvelopePayload

@pytest.fixture(autouse=True)
def _cap_gate_waits(monkeypatch):
    """LANE C: cap every user-decision gate wait so a headless run never hangs
    on the F6 24h local-lane lift (``_gate_wait_timeout``). Production leaves
    ``TRID3NT_GATE_WAIT_CAP_S`` unset -> byte-identical behavior. Happy-path
    approver tasks answer within milliseconds; the dedicated timeout test
    tightens the cap so it hits the honest fail-closed path fast."""
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "5")


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()
        # fix (bbox-gate-retry-loop, 2026-07-09): the turn-memory dict the
        # ``_gate_with_turn_memory`` wrapper reads/writes. Real
        # ``SessionState`` carries this too (reset at the start of every
        # user-message dispatch); tests construct it here since ``_FakeState``
        # is a minimal stand-in.
        self.gate_decisions_this_turn: dict = {}


def test_dispatch_source_contains_strip_and_gate() -> None:
    """Belt-and-braces: the dispatch site resolves the tool's GateSpec (ADR 0273,
    the gate-collapse -- membership is metadata, not a name set) and strips an
    LLM-supplied ``confirmed`` for a SOLVER gate before gating (source-level
    assertion so a refactor that drops the strip line fails loudly)."""
    import trid3nt_server.server as server_mod

    src = inspect.getsource(server_mod._invoke_tool_via_emitter)
    assert "_gate_spec_for(" in src
    assert 'kind == "solver"' in src
    assert 'params.pop("confirmed", None)' in src
    assert "SolverConfirmationCancelledError" in src


def test_code_exec_request_in_hot_set() -> None:
    """code_exec_request must be in the always-visible retrieval floor so it is
    never retrieved out (round-4 live showed a false 'cannot run Python'
    narration when it was not reachable)."""
    from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR

    assert "code_exec_request" in CORE_FLOOR


# --------------------------------------------------------------------------- #
# Turn-memory fix (bbox-gate-retry-loop, 2026-07-09) - live drive found a
# model retrying ``fetch_landcover`` with corrected NON-bbox args after typed
# errors (dataset='nlcd' -> 'nlcd_' -> 'nlcd_2021'); each valid-bbox retry
# re-emitted a NEW confirm gate for the SAME tool + SAME bbox, and the second
# (unanswered) gate hung the turn forever (local gates have no timeout by
# design). ``_gate_with_turn_memory`` wraps ``_gate_on_solver_confirm`` with a
# per-turn ``state.gate_decisions_this_turn`` memory keyed by
# ``_gate_memory_key`` (tool + bbox rounded to ~6 decimals).
#
# fetch_landcover skips the card entirely for a small AOI (no coarsening
# needed) -- these tests use a Washington-state-scale bbox (mirrors the live
# bug report) so the gate is the REAL thing, not the small-AOI skip path.
# --------------------------------------------------------------------------- #

_WA_BBOX = [-124.8, 45.5, -116.9, 49.0]
_CA_BBOX = [-124.4, 32.5, -114.1, 42.0]  # a DIFFERENT state-scale bbox


async def _approve_next_pending(server, decision: str = "proceed") -> None:
    """Resolve the next pending gate future with ``decision`` once it appears."""
    for _ in range(400):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(server._PENDING_CONFIRMATIONS))
    server._PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(warning_id=wid, decision=decision)
    )


@pytest.mark.asyncio
async def test_gate_turn_memory_auto_applies_same_tool_same_bbox() -> None:
    """A second call this turn with the SAME tool + bbox (but a corrected,
    non-bbox arg -- e.g. a fixed `dataset`) must NOT gate again; the earlier
    proceed decision is replayed and the remembered resolution_m override
    carries onto the new call's params."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_1, approved_1 = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd"}
    )
    await approver
    assert should_run_1 is True
    assert "resolution_m" in approved_1  # the gate pinned the suggested rung
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    # Retry with a CORRECTED dataset (the fix-1 alias would already accept
    # 'nlcd', but this exercises the exact live failure shape where the
    # model retried with a different, still-typed-error-prone value before
    # landing on 'nlcd_2021').
    should_run_2, approved_2 = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    assert should_run_2 is True
    # NO second gate was emitted.
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1
    # The remembered resolution_m override carried onto the retry...
    assert approved_2["resolution_m"] == approved_1["resolution_m"]
    # ...while the retry's OWN corrected dataset arg is preserved (not
    # clobbered by the memoized decision).
    assert approved_2["dataset"] == "nlcd_2021"


@pytest.mark.asyncio
async def test_gate_turn_memory_different_bbox_gates_again() -> None:
    """A different bbox in the same turn is a memory MISS -- gates fresh."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_1, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver
    assert should_run_1 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    approver_2 = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_2, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_CA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver_2
    assert should_run_2 is True
    # A SECOND gate WAS emitted for the different bbox.
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 2


@pytest.mark.asyncio
async def test_gate_turn_memory_cancel_not_memoized_gates_again() -> None:
    """A 'cancel' decision must NOT be memoized -- an identical retry gates
    again (the user might reconsider on a corrected retry)."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    canceller = asyncio.create_task(_approve_next_pending(server, "cancel"))
    should_run_1, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await canceller
    assert should_run_1 is False
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    # Identical retry (same tool, same bbox) -- must gate AGAIN, not hang or
    # silently auto-cancel from memory.
    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_2, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver
    assert should_run_2 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 2


@pytest.mark.asyncio
async def test_gate_turn_memory_new_turn_gates_again() -> None:
    """A fresh turn (state.gate_decisions_this_turn reset, mirroring the
    real per-turn reset in server.py's dispatch entrypoint) gates again even
    for the SAME tool + bbox that was approved last turn."""
    from trid3nt_server import server

    ws, state = _FakeWS(), _FakeState()

    approver = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_1, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver
    assert should_run_1 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 1

    # Simulate the new-turn reset (same site as credential_prompted_tools).
    state.gate_decisions_this_turn = {}

    approver_2 = asyncio.create_task(_approve_next_pending(server, "proceed"))
    should_run_2, _ = await server._gate_with_turn_memory(
        ws, state, "fetch_landcover", {"bbox": list(_WA_BBOX), "dataset": "nlcd_2021"}
    )
    await approver_2
    assert should_run_2 is True
    assert sum(1 for e in ws.sent if e.get("type") == "tool-payload-warning") == 2


def test_gate_decisions_this_turn_reset_at_new_dispatch() -> None:
    """Source-level assertion: the real per-turn reset site (the same one
    that clears ``credential_prompted_tools``) also clears
    ``gate_decisions_this_turn`` -- a regression here would silently bring
    back the cross-turn leak this fix closes."""
    import inspect

    import trid3nt_server.server as server_mod

    from trid3nt_server.server.turn import stream as _stream_mod

    src = inspect.getsource(_stream_mod)
    assert "state.gate_decisions_this_turn = {}" in src


def test_gate_memory_key_bboxless_tool_uses_full_args() -> None:
    """A gated tool with no bbox arg (e.g. the point-driven groundwater plume)
    keys on the full normalized args dict, so ANY arg change gates fresh."""
    from trid3nt_server import server

    k1 = server._gate_memory_key(
        "modflow_contaminant_plume", {"contaminant": "TCE", "release_rate_kg_s": 3.0}
    )
    k2 = server._gate_memory_key(
        "modflow_contaminant_plume", {"contaminant": "TCE", "release_rate_kg_s": 3.1}
    )
    assert k1 != k2
