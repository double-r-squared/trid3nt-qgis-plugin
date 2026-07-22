"""Tests for the ``impact-envelope`` WebSocket emission (Wave 4.11 Follow-up A).

Coverage:
1. ``test_compute_impact_envelope_dispatch_emits_impact_envelope`` — when
   ``_stream_gemini_reply`` dispatches ``compute_impact_envelope`` and the
   result carries a valid ``raw_envelope`` (with ``n_structures_total``), an
   extra ``impact-envelope`` envelope is sent on the wire IN ADDITION to the
   standard ``function_response``.
2. ``test_non_impact_tool_does_not_emit_impact_envelope`` — dispatching a
   non-impact tool (e.g. ``fetch_dem``) does NOT emit ``impact-envelope``.
3. ``test_impact_envelope_emission_without_raw_envelope`` — if
   ``compute_impact_envelope`` returns a result missing ``raw_envelope`` key,
   no ``impact-envelope`` is emitted (guard against malformed results).
4. ``test_impact_envelope_emission_raw_envelope_missing_n_structures_total``
   — if ``raw_envelope`` exists but lacks ``n_structures_total``, no emission.
5. ``test_maybe_emit_impact_envelope_helper_direct`` — unit-test
   ``_maybe_emit_impact_envelope`` directly: valid envelope → one ``impact-
   envelope`` send; wire failure is silently swallowed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trid3nt_contracts import new_ulid


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    """Minimal WebSocket shim that records every ``send`` payload."""

    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


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


# ---------------------------------------------------------------------------
# Sample ImpactEnvelope payload
# ---------------------------------------------------------------------------

_SAMPLE_RAW_ENVELOPE: dict[str, Any] = {
    "n_structures_total": 1500,
    "n_structures_damaged": 342,
    "n_structures_destroyed": 28,
    "expected_loss_usd": 7_800_000.0,
    "loss_percentile_95_usd": 12_000_000.0,
    "total_replacement_value_usd": 320_000_000.0,
    "damaged_replacement_value_usd": 95_000_000.0,
    "population_total": 4200,
    "population_displaced": 810,
    "population_at_high_risk": 612,
    "impact_area_km2": 18.3,
    "structure_inventory_source": "USACE_NSI",
    "pelicun_run_id": "pelicun-run-abc123",
}

_SAMPLE_IMPACT_TOOL_RESULT: dict[str, Any] = {
    "envelope_summary": {
        "n_structures_total": _SAMPLE_RAW_ENVELOPE["n_structures_total"],
        "n_structures_damaged": _SAMPLE_RAW_ENVELOPE["n_structures_damaged"],
        "expected_loss_usd": _SAMPLE_RAW_ENVELOPE["expected_loss_usd"],
    },
    "raw_envelope": _SAMPLE_RAW_ENVELOPE,
    "narrative": "342 structures impacted, $7,800,000 in expected damages, 612 population at high risk",
    "provenance": {
        "flood_layer_uri": "gs://grace2-runs-dev/flood.tif",
        "bbox": [-82.0, 26.5, -81.7, 26.8],
    },
}


# ---------------------------------------------------------------------------
# Test 1: compute_impact_envelope dispatch emits impact-envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_impact_envelope_dispatch_emits_impact_envelope():
    """compute_impact_envelope with valid raw_envelope triggers extra WS send."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapter import GeminiSettings

    # Turn 1: Gemini calls compute_impact_envelope.
    turn1_chunk = _make_fake_chunk_with_function_call(
        "compute_impact_envelope",
        {"flood_layer_uri": "gs://grace2-runs-dev/flood.tif", "bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-impact",
    )
    # Turn 2: Gemini narrates the result.
    turn2_chunk = _make_fake_chunk_with_text(
        "342 structures impacted with $7.8M in expected damages."
    )

    turn_iter = iter([iter([turn1_chunk]), iter([turn2_chunk])])
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    async def _fake_invoke(_ws, _state, name, args):
        if name == "compute_impact_envelope":
            return _SAMPLE_IMPACT_TOOL_RESULT
        return None

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    # Pre-open the damage_assessment category so compute_impact_envelope is in
    # the allowed set (mirrors what happens when the LLM calls
    # list_tools_in_category("damage_assessment") before the composer).
    state.allowed_tool_set.add_tools(["compute_impact_envelope"])
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="test", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Compute the impact envelope for Fort Myers flood.", "research"
        )

    # Collect all sent envelopes.
    sent_envelopes = [json.loads(m) for m in sock.sent]

    # There must be at least one ``impact-envelope`` envelope.
    impact_envelopes = [e for e in sent_envelopes if e.get("type") == "impact-envelope"]
    assert len(impact_envelopes) == 1, (
        f"Expected 1 impact-envelope, got {len(impact_envelopes)}; "
        f"envelope types: {[e['type'] for e in sent_envelopes]}"
    )

    # Payload must be the raw_envelope dict.
    ie_payload = impact_envelopes[0]["payload"]
    assert ie_payload["n_structures_total"] == 1500
    assert ie_payload["n_structures_damaged"] == 342
    assert ie_payload["expected_loss_usd"] == 7_800_000.0

    # session_id must be threaded through.
    assert impact_envelopes[0]["session_id"] == state.session_id

    # Standard function_response content must also have been fed back so
    # Gemini ran a second turn and emitted the narrative.
    narrative_chunks = [
        e for e in sent_envelopes
        if e.get("type") == "agent-message-chunk" and e["payload"].get("delta")
    ]
    text_seen = "".join(c["payload"]["delta"] for c in narrative_chunks)
    assert "impacted" in text_seen.lower(), (
        "expected narrative text missing — function_response may not have been fed back"
    )


# ---------------------------------------------------------------------------
# Test 2: non-impact tool does not emit impact-envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_impact_tool_does_not_emit_impact_envelope():
    """Dispatching fetch_dem (or any non-impact tool) emits no impact-envelope."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapter import GeminiSettings

    turn1_chunk = _make_fake_chunk_with_function_call(
        "fetch_dem",
        {"bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-dem",
    )
    turn2_chunk = _make_fake_chunk_with_text("Here is the DEM layer.")

    turn_iter = iter([iter([turn1_chunk]), iter([turn2_chunk])])
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    async def _fake_invoke(_ws, _state, name, args):
        return {"layer_id": "dem-layer", "wms_url": "https://qgis.example.com/wms?LAYERS=dem"}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="test", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Show me a DEM for Fort Myers.", "research"
        )

    sent_envelopes = [json.loads(m) for m in sock.sent]
    impact_envelopes = [e for e in sent_envelopes if e.get("type") == "impact-envelope"]
    assert impact_envelopes == [], (
        f"Unexpected impact-envelope from non-impact tool: {impact_envelopes}"
    )


# ---------------------------------------------------------------------------
# Test 3: compute_impact_envelope result missing raw_envelope → no emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_impact_envelope_emission_without_raw_envelope():
    """No impact-envelope when result lacks the raw_envelope key."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapter import GeminiSettings

    turn1_chunk = _make_fake_chunk_with_function_call(
        "compute_impact_envelope",
        {"flood_layer_uri": "gs://runs/flood.tif", "bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-impact",
    )
    turn2_chunk = _make_fake_chunk_with_text("Impact assessed.")

    turn_iter = iter([iter([turn1_chunk]), iter([turn2_chunk])])
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    async def _fake_invoke(_ws, _state, name, args):
        # Malformed result — no raw_envelope key.
        return {"envelope_summary": {"n_structures_total": 100}, "narrative": "100 structures."}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    state.allowed_tool_set.add_tools(["compute_impact_envelope"])
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="test", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Compute impact.", "research"
        )

    sent_envelopes = [json.loads(m) for m in sock.sent]
    impact_envelopes = [e for e in sent_envelopes if e.get("type") == "impact-envelope"]
    assert impact_envelopes == [], "Should not emit without raw_envelope key"


# ---------------------------------------------------------------------------
# Test 4: raw_envelope missing n_structures_total → no emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_impact_envelope_emission_raw_envelope_missing_n_structures_total():
    """No impact-envelope when raw_envelope lacks n_structures_total."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapter import GeminiSettings

    turn1_chunk = _make_fake_chunk_with_function_call(
        "compute_impact_envelope",
        {"flood_layer_uri": "gs://runs/flood.tif", "bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-impact",
    )
    turn2_chunk = _make_fake_chunk_with_text("Done.")

    turn_iter = iter([iter([turn1_chunk]), iter([turn2_chunk])])
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(turn_iter)

    async def _fake_invoke(_ws, _state, name, args):
        # raw_envelope present but missing the key signal field.
        return {
            "raw_envelope": {"n_structures_damaged": 50},  # no n_structures_total
            "narrative": "Partial.",
        }

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    state.allowed_tool_set.add_tools(["compute_impact_envelope"])
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="test", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "Compute impact.", "research"
        )

    sent_envelopes = [json.loads(m) for m in sock.sent]
    impact_envelopes = [e for e in sent_envelopes if e.get("type") == "impact-envelope"]
    assert impact_envelopes == [], "Should not emit when n_structures_total absent"


# ---------------------------------------------------------------------------
# Test 5: _maybe_emit_impact_envelope helper — direct unit test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_emit_impact_envelope_helper_valid():
    """Direct call to _maybe_emit_impact_envelope sends one impact-envelope."""
    from trid3nt_server.server import SessionState, _maybe_emit_impact_envelope

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    await _maybe_emit_impact_envelope(sock, state, _SAMPLE_RAW_ENVELOPE)

    assert len(sock.sent) == 1
    envelope = json.loads(sock.sent[0])
    assert envelope["type"] == "impact-envelope"
    assert envelope["session_id"] == state.session_id
    assert envelope["payload"]["n_structures_total"] == 1500


@pytest.mark.asyncio
async def test_maybe_emit_impact_envelope_helper_wire_failure_swallowed():
    """_maybe_emit_impact_envelope swallows send exceptions (best-effort)."""
    from trid3nt_server.server import SessionState, _maybe_emit_impact_envelope

    class _ErrorSocket:
        async def send(self, msg: str) -> None:
            raise ConnectionError("socket closed")

    state = SessionState(session_id=new_ulid())

    # Must not raise — a wire failure is a best-effort side effect.
    await _maybe_emit_impact_envelope(_ErrorSocket(), state, _SAMPLE_RAW_ENVELOPE)
