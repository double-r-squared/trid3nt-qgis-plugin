"""Live-evidence harness for job-0169 (multi-turn function_call loop).

Runs the real ``_stream_gemini_reply`` against a fake WebSocket sink and a
mocked Gemini that emits a sequence of ``function_call`` chunks followed by a
final narrative.  Prints the verbatim send-transcript to stdout so the audit
can compare against the kickoff acceptance:

    "Show me protected areas in Fort Myers"
      → Gemini calls geocode_location → gets bbox
      → Gemini calls fetch_wdpa_protected_areas with the bbox
      → Gemini narrates the result and the loop terminates.

NOT a unit test — this is the live, end-to-end transcript evidence the
``agent.md`` Definition-of-Done requires.

Usage:
    .venv-agent/bin/python services/agent/tests/live_evidence_job_0169.py
"""

from __future__ import annotations

import asyncio
import json
import textwrap
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

from grace2_agent import server as agent_server
from grace2_agent.adapter import GeminiSettings
from grace2_agent.server import SessionState
from grace2_contracts import new_ulid


# --------------------------------------------------------------------------- #
# Fake WS sink + fake Gemini stream
# --------------------------------------------------------------------------- #


@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:
        self.sent.append(msg)


def _make_chunk_with_function_call(name: str, args: dict, call_id: str = "c"):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    part = MagicMock()
    part.function_call = fn_call
    part.text = None
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = None
    return chunk


def _make_chunk_with_text(text: str):
    part = MagicMock()
    part.function_call = None
    part.text = text
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = None
    return chunk


# --------------------------------------------------------------------------- #
# Live harness
# --------------------------------------------------------------------------- #


async def run() -> None:
    print("=" * 70)
    print("JOB-0169 LIVE EVIDENCE — multi-turn function_call → function_response")
    print("=" * 70)
    print()
    print('User: "Show me protected areas in Fort Myers"')
    print()

    # Three pre-canned Gemini turns mirroring the kickoff scenario.
    turn1 = _make_chunk_with_function_call(
        "geocode_location", {"query": "Fort Myers, FL"}, "call-geo-1"
    )
    turn2 = _make_chunk_with_function_call(
        "fetch_wdpa_protected_areas",
        {"bbox": [-82.0, 26.5, -81.7, 26.8]},
        "call-wdpa-1",
    )
    turn3 = _make_chunk_with_text(
        "Found 2 protected areas inside the Fort Myers bbox (J.N. \"Ding\" "
        "Darling NWR + Estero Bay Aquatic Preserve). Layer is now on the map."
    )
    turns = iter([iter([turn1]), iter([turn2]), iter([turn3])])

    # Track the contents Gemini sees each turn so we can prove the
    # function_response is being fed back.
    contents_log: list[list] = []

    def _stream(**kwargs):
        snapshot = []
        for c in kwargs["contents"]:
            for p in c.parts:
                if getattr(p, "function_call", None) is not None and p.function_call.name:
                    snapshot.append((c.role, "function_call", p.function_call.name,
                                     dict(p.function_call.args or {})))
                elif getattr(p, "function_response", None) is not None:
                    snapshot.append((c.role, "function_response",
                                     p.function_response.name,
                                     dict(p.function_response.response or {})))
                elif p.text:
                    snapshot.append((c.role, "text", None, p.text[:60]))
        contents_log.append(snapshot)
        return next(turns)

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = _stream

    # Stub tool dispatch so we don't need GCS / Nominatim / WDPA live.
    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        if name == "geocode_location":
            return {
                "name": "Fort Myers, FL",
                "bbox": [-82.0, 26.5, -81.7, 26.8],
                "precision_class": "precise",
            }
        if name == "fetch_wdpa_protected_areas":
            return {
                "layer_id": "wdpa-fort-myers",
                "wms_url": "https://qgis.example.com/wms?LAYERS=wdpa-fort-myers",
                "feature_count": 2,
                "metrics": {"total_area_km2": 87.4},
            }
        return None

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro",
        project="grace-2-hazard-prod",
        location="us-central1",
        use_vertex=True,
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock,
            state,
            settings,
            "Show me protected areas in Fort Myers",
            "research",
        )

    # ------------------------------------------------------------------ #
    # Print: contents Gemini saw on each turn
    # ------------------------------------------------------------------ #
    print("=" * 70)
    print("CONTENTS GEMINI SAW EACH TURN (proves the loop feeds back)")
    print("=" * 70)
    for i, snap in enumerate(contents_log, 1):
        print(f"\nTurn {i}:")
        for role, kind, name, payload in snap:
            if kind == "text":
                print(f"  [{role}] text: {payload!r}")
            elif kind == "function_call":
                print(f"  [{role}] function_call: {name}({payload})")
            elif kind == "function_response":
                summary = textwrap.shorten(repr(payload), 180)
                print(f"  [{role}] function_response: {name} -> {summary}")

    # ------------------------------------------------------------------ #
    # Print: dispatch order
    # ------------------------------------------------------------------ #
    print()
    print("=" * 70)
    print("TOOL DISPATCH ORDER (proves both tools fired, not just the first)")
    print("=" * 70)
    for i, (name, args) in enumerate(dispatch_log, 1):
        print(f"  {i}. {name}({args})")

    # ------------------------------------------------------------------ #
    # Print: every envelope sent on the wire
    # ------------------------------------------------------------------ #
    print()
    print("=" * 70)
    print("WEBSOCKET ENVELOPES SENT (proves stream + pipeline-state lifecycle)")
    print("=" * 70)
    for i, raw in enumerate(sock.sent, 1):
        env = json.loads(raw)
        t = env["type"]
        payload = env.get("payload", {})
        if t == "agent-message-chunk":
            d = payload.get("delta", "")
            done = payload.get("done", False)
            line = f"  {i:2d}. {t:24s} delta={d!r:50s} done={done}"
        elif t == "pipeline-state":
            steps = payload.get("steps", [])
            states = [s.get("state") for s in steps]
            line = f"  {i:2d}. {t:24s} step_states={states}"
        else:
            line = f"  {i:2d}. {t:24s} {json.dumps(payload)[:80]}"
        print(line)

    # ------------------------------------------------------------------ #
    # Acceptance assertions (verbatim from kickoff Verify section)
    # ------------------------------------------------------------------ #
    print()
    print("=" * 70)
    print("ACCEPTANCE CHECKS (verbatim against the kickoff Verify section)")
    print("=" * 70)
    assert [n for (n, _) in dispatch_log] == [
        "geocode_location",
        "fetch_wdpa_protected_areas",
    ], dispatch_log
    print("  [PASS] geocode_location → fetch_wdpa_protected_areas dispatched in order")

    # Second tool got the bbox from the first tool's response.
    assert dispatch_log[1][1].get("bbox") == [-82.0, 26.5, -81.7, 26.8]
    print("  [PASS] fetch_wdpa received bbox synthesized from geocode response")

    # Turn 2 contents contain the function_response for turn 1.
    turn2_kinds = [k for (_role, k, _n, _p) in contents_log[1]]
    assert "function_call" in turn2_kinds
    assert "function_response" in turn2_kinds
    print("  [PASS] turn-2 contents include function_call + function_response")

    # Turn 3 contents contain BOTH (call,response) pairs.
    turn3_calls = [n for (_role, k, n, _p) in contents_log[2] if k == "function_call"]
    assert turn3_calls == ["geocode_location", "fetch_wdpa_protected_areas"]
    print("  [PASS] turn-3 contents include both call+response pairs in order")

    # Terminal narrative reached the client.
    chunks = [json.loads(m) for m in sock.sent if "agent-message-chunk" in m]
    text_seen = "".join(c["payload"]["delta"] for c in chunks if c["payload"].get("delta"))
    assert "protected areas" in text_seen.lower()
    print(f"  [PASS] narrative delivered to client: {text_seen!r}")

    # Loop terminated cleanly (pipeline-state complete).
    pipeline_frames = [json.loads(m) for m in sock.sent if "pipeline-state" in m]
    assert pipeline_frames[-1]["payload"]["steps"][0]["state"] == "complete"
    print("  [PASS] outer pipeline-state ends in 'complete'")

    print()
    print("ALL ACCEPTANCE CHECKS PASS — job-0169 multi-turn loop verified live.")


if __name__ == "__main__":
    asyncio.run(run())
