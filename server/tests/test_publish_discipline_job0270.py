"""job-0270 — colored-relief chain-to-pixels fixes, server-level evidence.

Live failure (third occurrence, /tmp/agent_demo7.log + /tmp/agent_demo8.log):
"Compute a colored relief map for Boulder, Colorado" fetched the DEM and
computed the relief, but the chain to visible pixels broke two ways:

1. VALIDATOR DETOURS — compute_colored_relief / compute_hillshade /
   publish_layer are real registered tools outside the hot set, so the
   post-hoc validator bounced Gemini's correct FIRST call to each
   (OutOfAllowedSetError) and Gemini burned 2-4 iterations guessing
   category names ('terrain_analysis', 'raster') before recovering.
2. PUBLISH OMISSION — after compute_colored_relief succeeded, Gemini ended
   the turn with text only; publish_layer never ran, so the computed raster
   stayed invisible (a layer is not on the map until publish_layer adds it
   to the QGIS Server project).

These tests drive ``_stream_gemini_reply`` end-to-end (fake Gemini, fake
tool dispatch — no live calls) and prove:

- FIX A: a registry-valid, non-hot-set tool dispatches on the FIRST call
  (auto-widen), with no OUT_OF_ALLOWED_SET envelope and the widened set
  persisting for the session; a hallucinated name still bounces with the
  structured error envelope (guard unweakened at the server level).
- FIX B: the function_response for a layer-producing tool carries the
  strengthened ``layer_handles_note`` that says the layer is NOT on the
  map yet and instructs calling publish_layer with the handle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.adapter import GeminiSettings
from grace2_contracts import new_ulid


@pytest.fixture(scope="module", autouse=True)
def _populate_registry() -> None:
    """The full registry must be loaded so compute_colored_relief is real."""
    from grace2_agent.main import _import_tools_registry

    _import_tools_registry()


# ---------------------------------------------------------------------------
# Fake-Gemini scaffolding (same shapes as test_multi_turn_loop.py)
# ---------------------------------------------------------------------------


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str = "c1"):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    fake_part = MagicMock()
    fake_part.function_call = fn_call
    fake_part.text = None
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


def _make_fake_chunk_with_text(text: str):
    fake_part = MagicMock()
    fake_part.function_call = None
    fake_part.text = text
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


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


async def _drive_loop(turn_chunks: list[list[Any]], fake_invoke) -> tuple[list[list[Any]], "_FakeSocket", Any]:
    """Run ``_stream_gemini_reply`` against pre-canned Gemini turns.

    Returns (contents captured per Gemini call, fake socket, session state).
    """
    from grace2_agent import server as agent_server
    from grace2_agent.server import SessionState

    turn_responses = iter([iter(chunks) for chunks in turn_chunks])
    contents_per_turn: list[list[Any]] = []

    def _capture_and_stream(**kwargs):
        contents_per_turn.append(list(kwargs["contents"]))
        return next(turn_responses)

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = _capture_and_stream

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro",
        project="test",
        location="us-central1",
        use_vertex=True,
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings,
            "Compute a colored relief map for Boulder, Colorado", "research",
        )
    return contents_per_turn, sock, state


# ---------------------------------------------------------------------------
# FIX A at the server level: first-call dispatch for a real non-hot-set tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_call_to_real_non_hot_set_tool_dispatches() -> None:
    """Gemini's FIRST call to compute_colored_relief (real tool, outside the
    hot set) must dispatch — no OUT_OF_ALLOWED_SET bounce, no detour turns.
    This is the demo7/demo8 failure mode, inverted."""
    from grace2_agent import server as agent_server

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
        [
            [_make_fake_chunk_with_function_call(
                "compute_colored_relief",
                {"dem_uri": "gs://grace2-tool-cache/dem/boulder.tif", "ramp": "terrain"},
                "call-relief",
            )],
            [_make_fake_chunk_with_text("Computed the colored relief for Boulder.")],
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
    # The auto-widened set persists for the session (monotonic growth).
    assert "compute_colored_relief" in state.allowed_tool_set.as_frozenset()


@pytest.mark.asyncio
async def test_hallucinated_tool_still_bounces_at_server_level() -> None:
    """The hallucination guard is unweakened: a name that exists nowhere in
    the registry never dispatches and surfaces the structured
    OUT_OF_ALLOWED_SET envelope to Gemini."""
    dispatch_log: list[str] = []

    async def _fake_invoke(_ws, _state, name, args):  # pragma: no cover — must not run
        dispatch_log.append(name)
        return {"status": "ok"}

    contents_per_turn, _sock, state = await _drive_loop(
        [
            [_make_fake_chunk_with_function_call(
                "compute_terrain_relief_v2", {"dem_uri": "gs://x/y.tif"}, "call-fake",
            )],
            [_make_fake_chunk_with_text("That tool does not exist; let me check the catalog.")],
        ],
        _fake_invoke,
    )

    # The hallucinated name never reached dispatch.
    assert dispatch_log == []
    payloads = _function_response_payloads(contents_per_turn)
    assert payloads, "no function_response reached the second Gemini turn"
    name, payload = payloads[0]
    assert name == "compute_terrain_relief_v2"
    assert payload.get("status") == "error"
    assert payload.get("error_code") == "OUT_OF_ALLOWED_SET"
    # And the bad name did not pollute the allowed set.
    assert "compute_terrain_relief_v2" not in state.allowed_tool_set.as_frozenset()


# ---------------------------------------------------------------------------
# FIX B: strengthened layer_handles_note in the function_response payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer_producing_tool_response_carries_publish_instruction() -> None:
    """The function_response for a layer-producing tool must tell Gemini the
    layer is NOT on the map yet and to call publish_layer with the handle —
    the demo8 publish-omission fix."""
    from grace2_agent import server as agent_server

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
        [
            [_make_fake_chunk_with_function_call(
                "compute_colored_relief",
                {"dem_uri": "gs://grace2-tool-cache/dem/boulder.tif", "ramp": "terrain"},
                "call-relief",
            )],
            [_make_fake_chunk_with_text("Done.")],
        ],
        _fake_invoke,
    )

    payloads = _function_response_payloads(contents_per_turn)
    assert payloads, "no function_response reached the second Gemini turn"
    _name, payload = payloads[0]

    # The handle announcement is present...
    handles = payload.get("layer_handles")
    assert handles and "colored-relief-boulder" in handles

    # ...and the note carries the publish instruction (job-0270 wording).
    note = payload.get("layer_handles_note", "")
    assert "NOT visible on the user's map" in note
    assert "publish_layer(layer_uri=<handle>" in note
    # The original handle-discipline guidance survives.
    assert "Do NOT construct or echo gs:// paths" in note
