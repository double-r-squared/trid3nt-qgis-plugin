"""Layer-handle announcement, server-level evidence.

These tests drive ``_stream_model_reply`` end-to-end (fake model, fake tool
dispatch - no live calls) and prove:

- a registry-valid tool dispatches on the FIRST call and sticks in the Case's
  monotonic visible set for the rest of the session.
- the function_response for a layer-producing tool announces its handles and
  carries the ``layer_handles_note``: the layer is already on the map, and a
  handle is passed rather than a storage URI rebuilt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.adapters.adapter import ModelSettings
from trid3nt_contracts import new_ulid


@pytest.fixture(scope="module", autouse=True)
def _populate_registry() -> None:
    """The full registry must be loaded so compute_colored_relief is real."""
    from trid3nt_server.main import _import_tools_registry

    _import_tools_registry()


# ---------------------------------------------------------------------------
# Fake-Gemini scaffolding (same shapes as test_multi_turn_loop.py)
# ---------------------------------------------------------------------------


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str = "c1"):
    """A fake turn (scripted-provider dict) emitting ONE function call."""
    return {"tool_call": {"name": name, "args": args, "call_id": call_id}}


def _make_fake_chunk_with_text(text: str):
    """A fake turn (scripted-provider dict) emitting one narration delta."""
    return {"text": text}


@dataclass
class _FakeSocket:
    """Minimal WebSocket shim that records every ``send`` payload."""

    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 — protocol shim
        self.sent.append(msg)


def _function_response_payloads(contents_per_turn: list[list[Any]]) -> list[tuple[str, dict]]:
    """Extract (name, payload) for every function_response Part captured."""
    out: list[tuple[str, dict]] = []
    for contents in contents_per_turn:
        for content in contents:
            for part in content.parts:
                fr = getattr(part, "function_response", None)
                if fr is not None and not isinstance(fr, MagicMock):
                    out.append((fr.name, dict(fr.response)))
    return out


async def _drive_loop(fake_llm, turns: list[dict], fake_invoke) -> tuple[list[list[Any]], "_FakeSocket", Any]:
    """Run ``_stream_model_reply`` against pre-canned scripted-provider turns.

    Returns (contents captured per model call, fake socket, session state).
    """
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    fake_llm.script(turns)

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = ModelSettings(
        model="gemini-2.5-pro",
        project="test",
        location="us-central1",
        use_vertex=True,
    )

    with patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, settings,
            "Compute a colored relief map for Boulder, Colorado", "research",
        )
    contents_per_turn = [call["contents"] for call in fake_llm.calls]
    return contents_per_turn, sock, state


# ---------------------------------------------------------------------------
# FIX A at the server level: first-call dispatch for a real non-hot-set tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_call_to_real_non_hot_set_tool_dispatches(fake_llm) -> None:
    """The FIRST call to compute_colored_relief (a real tool outside the core
    floor) must dispatch and stick in the Case's monotonic visible set."""
    from trid3nt_server import server as agent_server

    dispatch_log: list[str] = []

    async def _fake_invoke(_ws, state, name, args):
        dispatch_log.append(name)
        result = {
            "layer_id": "colored-relief-boulder",
            "uri": "gs://grace2-tool-cache/colored_relief/deadbeef1234.tif",
            "ramp": "terrain",
        }
        # Mirror the real _invoke_tool_via_emitter: register the result's
        # layer handle so the server's drain_announcements sees it.
        agent_server.get_uri_registry(state.session_id).register_tool_result(
            name, result
        )
        return result

    contents_per_turn, _sock, state = await _drive_loop(
        fake_llm,
        [
            _make_fake_chunk_with_function_call(
                "compute_colored_relief",
                {"dem_uri": "gs://grace2-tool-cache/dem/boulder.tif", "ramp": "terrain"},
                "call-relief",
            ),
            _make_fake_chunk_with_text("Computed the colored relief for Boulder."),
        ],
        _fake_invoke,
    )

    # Dispatched on the first call — exactly once, no detours.
    assert dispatch_log == ["compute_colored_relief"]
    # Exactly two Gemini turns: the call turn + the terminal narration.
    assert len(contents_per_turn) == 2
    # The function_response Gemini saw is an ok envelope, not the bounce.
    payloads = _function_response_payloads(contents_per_turn)
    assert payloads, "no function_response reached the second Gemini turn"
    name, payload = payloads[0]
    assert name == "compute_colored_relief"
    assert payload.get("error_code") != "OUT_OF_ALLOWED_SET"
    assert payload.get("status") == "ok"
    # It persists in the Case's monotonic visible set (never hidden mid-task).
    assert "compute_colored_relief" in state.visible_tools


# ---------------------------------------------------------------------------
# layer_handles_note in the function_response payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer_producing_tool_response_carries_handle_instruction(fake_llm) -> None:
    """The function_response for a layer-producing tool announces the handle
    and tells the model to pass it rather than rebuild a storage URI."""
    from trid3nt_server import server as agent_server

    async def _fake_invoke(_ws, state, name, args):
        result = {
            "layer_id": "colored-relief-boulder",
            "uri": "gs://grace2-tool-cache/colored_relief/deadbeef1234.tif",
            "ramp": "terrain",
        }
        agent_server.get_uri_registry(state.session_id).register_tool_result(
            name, result
        )
        return result

    contents_per_turn, _sock, _state = await _drive_loop(
        fake_llm,
        [
            _make_fake_chunk_with_function_call(
                "compute_colored_relief",
                {"dem_uri": "gs://grace2-tool-cache/dem/boulder.tif", "ramp": "terrain"},
                "call-relief",
            ),
            _make_fake_chunk_with_text("Done."),
        ],
        _fake_invoke,
    )

    payloads = _function_response_payloads(contents_per_turn)
    assert payloads, "no function_response reached the second Gemini turn"
    _name, payload = payloads[0]

    # The handle announcement is present...
    handles = payload.get("layer_handles")
    assert handles and "colored-relief-boulder" in handles

    # ...and the note carries the handle discipline.
    note = payload.get("layer_handles_note", "")
    assert "already on the user's map" in note
    assert "Do NOT construct or echo s3:// paths" in note
