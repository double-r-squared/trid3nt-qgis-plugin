"""A crisp turn end once a terminal composer delivers.

A composer that PRODUCES its artifact latches a done flag and stamps a one-time
wrap-up directive on its function_response; a model that keeps spinning with no
new progress afterwards concludes the turn CLEANLY within a small safety budget.
The runaway guard is untouched: a turn with no deliverable still runs to the cap."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server.adapters.adapter import ModelSettings, MAX_TURN_ITERATIONS
from trid3nt_server.server import (
    SessionState,
    _POST_DELIVERABLE_WRAPUP_ROUNDS,
    _is_terminal_composer,
)
from trid3nt_contracts import new_ulid


# ---------------------------------------------------------------------------
# Minimal socket + chunk helpers (mirror test_loop_exhausted_envelope.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str):
    return {"tool_call": {"name": name, "args": args, "call_id": call_id}}


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


# ---------------------------------------------------------------------------
# Unit: the terminal-composer classifier
# ---------------------------------------------------------------------------


def test_a_template_is_a_terminal_composer():
    """A top-level engine template is recognized as a terminal deliverable."""
    assert _is_terminal_composer("telemac_river_dye") is True


def test_helper_compute_tool_is_not_terminal_composer():
    """A mid-pipeline workflow-dispatch helper is NOT a turn-ending deliverable."""
    # compute_cross_section is source_class="workflow_dispatch" but lacks the
    # run_ prefix -> excluded (drawing/profiling is mid-pipeline, not the answer).
    assert _is_terminal_composer("compute_cross_section") is False


def test_unknown_tool_is_not_terminal_composer():
    assert _is_terminal_composer("definitely_not_a_tool") is False


# ---------------------------------------------------------------------------
# End-to-end: a delivered composer concludes crisply (NO loop_exhausted)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivered_composer_concludes_without_loop_exhausted(fake_llm):
    """A composer that delivers, then a model that spins, ends CLEANLY.

    The post-deliverable safety concludes the turn within a couple of idle rounds,
    without ``loop_exhausted`` and far under the iteration cap."""
    from trid3nt_server import server as agent_server

    def _next_turn(i, _c):
        if i == 0:
            # Deliver the SFINCS flood depth layer.
            return _make_fake_chunk_with_function_call(
                "telemac_river_dye",
                {"location": "Mexico Beach"},
                "call-composer",
            )
        # The model keeps spinning with an unproductive call (varied args so the
        # repeat-watchdog is NOT what stops it -- our crisp-end is).
        return _make_fake_chunk_with_function_call(
            "fetch_dem", {"bbox": [0, 0, i + 1, i + 1]}, f"c-{i + 1}"
        )

    fake_llm.on_call(_next_turn)

    dispatches = {"n": 0}

    async def _dispatch(_ws, _state, name, _args):
        dispatches["n"] += 1
        if name == "telemac_river_dye":
            # Layer-bearing deliverable -> _dispatch_made_progress is True.
            return {"status": "ok", "layers": ["flood-depth"], "layer_id": "flood-depth-cog"}
        # A bare ack -> NO progress (the post-deliverable idle shape).
        return {"ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    with patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch
         ), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "model the Mexico Beach flood", "research"
        )

    # NO loop_exhausted envelope: the turn ended crisply, not at the cap.
    exhausted = [m for m in sock.sent if m.get("type") == "loop_exhausted"]
    assert not exhausted, (
        "deliverable turn must NOT emit loop_exhausted; "
        f"sent types: {[m.get('type') for m in sock.sent]!r}"
    )

    # The composer ran, then at most a couple of idle rounds before concluding,
    # well under the cap. 1 composer + _POST_DELIVERABLE_WRAPUP_ROUNDS idle.
    assert dispatches["n"] == 1 + _POST_DELIVERABLE_WRAPUP_ROUNDS, dispatches["n"]
    assert dispatches["n"] < MAX_TURN_ITERATIONS

    # The client still gets a stream-closing done=True so its spinner stops.
    terminal = [
        m for m in sock.sent
        if m.get("type") == "agent-message-chunk"
        and m.get("payload", {}).get("done") is True
    ]
    assert terminal, (
        "expected a terminal agent-message-chunk(done=True); "
        f"sent types: {[m.get('type') for m in sock.sent]!r}"
    )


@pytest.mark.asyncio
async def test_composer_function_response_carries_completion_directive(fake_llm):
    """The delivered composer's function_response is stamped with the wrap-up note.

    The directive rides in the contents handed to the model on the NEXT round, which
    is what nudges a well-behaved model to summarize and stop on its own."""
    from trid3nt_server import server as agent_server

    def _next_turn(i, _c):
        if i == 0:
            return _make_fake_chunk_with_function_call(
                "telemac_river_dye", {"location": "X"}, "call-composer"
            )
        return _make_fake_chunk_with_function_call(
            "fetch_dem", {"bbox": [0, 0, i + 1, i + 1]}, f"c-{i + 1}"
        )

    fake_llm.on_call(_next_turn)

    async def _dispatch(_ws, _state, name, _args):
        if name == "telemac_river_dye":
            return {"status": "ok", "layers": ["d"], "layer_id": "d-cog"}
        return {"ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    with patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch
         ), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(sock, state, _settings(), "x", "research")

    captured_contents = [call["contents"] for call in fake_llm.calls]

    # The follow-up round (>=2) must have been handed the composer's
    # function_response carrying the wrap-up directive.
    assert len(captured_contents) >= 2, "model was not re-streamed after delivery"
    follow_up = str(captured_contents[1])
    assert "completion_directive" in follow_up, follow_up[:2000]
    assert "DELIVERABLE COMPLETE" in follow_up, follow_up[:2000]


# ---------------------------------------------------------------------------
# Runaway guard INTACT: a turn that never delivers still trips loop_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_composer_runaway_still_trips_loop_exhausted(fake_llm):
    """A turn that NEVER produces a terminal deliverable still hits the cap.

    The looped tool returns a layer-bearing dict every round, so the no-progress
    watchdog stays quiet and the crisp-end path stays dormant."""
    from trid3nt_server import server as agent_server

    # Vary args so the no-progress watchdog is not the thing that stops it;
    # each round PRODUCES a layer so the watchdog never counts no-progress.
    fake_llm.on_call(
        lambda i, _c: _make_fake_chunk_with_function_call(
            "fetch_dem", {"bbox": [0, 0, i + 1, i + 1]}, f"c-{i + 1}"
        )
    )

    dispatches = {"n": 0}

    async def _dispatch(_ws, _state, _name, _args):
        dispatches["n"] += 1
        return {"layer_id": f"dem-{dispatches['n']}", "wms_url": "http://x"}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    with patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch
         ), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(sock, state, _settings(), "spin", "research")

    # The historical runaway guard is untouched.
    assert dispatches["n"] == MAX_TURN_ITERATIONS, dispatches["n"]
    exhausted = [m for m in sock.sent if m.get("type") == "loop_exhausted"]
    assert exhausted, (
        "a genuine runaway (no terminal deliverable) must still emit "
        f"loop_exhausted; sent types: {[m.get('type') for m in sock.sent]!r}"
    )
