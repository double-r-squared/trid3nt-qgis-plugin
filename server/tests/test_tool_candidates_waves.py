"""LANE S -- ADR 0018 wave semantics: per-round stage-labeled tool-candidates.

Completion of the coarse single-wave stage_label: in ASK mode each ROUND's
pre-dispatch candidates emission derives its stage_label from the TOP
candidates' categories, and excludes this turn's already-dispatched tools, so a
multi-step turn surfaces a SEQUENCE of stage-labeled picks (acquisition ->
preprocessing -> analysis -> visualization) instead of one blob. AUTO mode is
unchanged (single near-tie emission only).

Driven end-to-end through ``_stream_gemini_reply`` on the scripted provider; the
retrieval ranking is patched at its module seam and tool dispatch is stubbed so
no real tool runs.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapter import GeminiSettings
from trid3nt_server.scripted_adapter import set_script
from trid3nt_server.tools.discovery import tool_retrieval as tr
from trid3nt_contracts import new_ulid


class _FakeSocket:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


def _cards(sock: _FakeSocket) -> list[dict]:
    return [
        m for m in sock.sent if isinstance(m, dict) and m.get("type") == "tool-candidates"
    ]


# A pipeline ranking: one tool per stage, best-first. As each round dispatches
# its tool, the exclusion advances the plurality (tie-broken to the earliest
# remaining stage) acquisition -> preprocessing -> analysis -> visualization.
_PIPELINE = [
    ("fetch_dem", 0.050),
    ("clip_raster_to_bbox", 0.040),
    ("compute_slope", 0.030),
    ("publish_layer", 0.020),
]

_SCRIPT = [
    {"text": "fetching", "tool_call": {"name": "fetch_dem", "args": {}}},
    {"text": "clipping", "tool_call": {"name": "clip_raster_to_bbox", "args": {}}},
    {"text": "analyzing", "tool_call": {"name": "compute_slope", "args": {}}},
    {"text": "done."},
]


@pytest.mark.asyncio
async def test_ask_mode_emits_stage_labeled_wave_per_round(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    monkeypatch.setenv("TRID3NT_MODE", "ask")
    # Each unanswered wave times out fast and proceeds (never hangs).
    monkeypatch.setenv("TRID3NT_TOOL_CHOICE_TIMEOUT_S", "0.1")
    set_script(_SCRIPT)
    monkeypatch.setattr(tr, "retrieve_ranked_tools", lambda text, k=25: list(_PIPELINE))

    # Stub dispatch so no real tool runs (the round's requested names are still
    # recorded in _turn_tools_dispatched before dispatch, which drives the
    # per-round exclusion).
    async def _noop_invoke(ws, state, name, args):
        return {"ok": True}

    monkeypatch.setattr(agent_server, "_invoke_tool_via_emitter", _noop_invoke)
    monkeypatch.setattr(agent_server, "validate_function_call", lambda name, allowed: None)

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    try:
        await asyncio.wait_for(
            agent_server._stream_gemini_reply(
                sock, state, _settings(), "map slope for the coast", "research"
            ),
            timeout=10.0,
        )
    finally:
        set_script(None)

    labels = [(c.get("payload") or {}).get("stage_label") for c in _cards(sock)]
    # A forward-marching sequence of stage labels (the waves).
    assert labels[:4] == [
        "acquisition",
        "preprocessing",
        "analysis",
        "visualization",
    ], labels


@pytest.mark.asyncio
async def test_auto_mode_emits_no_per_round_waves(monkeypatch):
    """AUTO mode is unchanged: a CONFIDENT ranking emits zero cards across rounds."""
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    monkeypatch.delenv("TRID3NT_MODE", raising=False)  # auto default
    set_script(_SCRIPT)
    # Confident ranking (relative top-1 vs top-2 margin 20% >> 1% threshold).
    monkeypatch.setattr(tr, "retrieve_ranked_tools", lambda text, k=25: list(_PIPELINE))

    async def _noop_invoke(ws, state, name, args):
        return {"ok": True}

    monkeypatch.setattr(agent_server, "_invoke_tool_via_emitter", _noop_invoke)
    monkeypatch.setattr(agent_server, "validate_function_call", lambda name, allowed: None)

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    try:
        await asyncio.wait_for(
            agent_server._stream_gemini_reply(
                sock, state, _settings(), "map slope for the coast", "research"
            ),
            timeout=10.0,
        )
    finally:
        set_script(None)
    assert _cards(sock) == [], "auto mode must not surface per-round waves"


# ---------------------------------------------------------------------------
# stage-label derivation (unit).
# ---------------------------------------------------------------------------


def test_stage_label_from_candidate_categories_plurality():
    slc = agent_server._stage_label_for_candidates
    assert slc([("fetch_dem", 1.0), ("fetch_landcover", 0.9), ("clip_raster_to_bbox", 0.8)]) == "acquisition"
    assert slc([("clip_raster_to_bbox", 1.0), ("cut_features_with_polygon", 0.9), ("compute_slope", 0.8)]) == "preprocessing"
    assert slc([("compute_slope", 1.0), ("run_swmm", 0.9), ("code_exec_request", 0.8)]) == "analysis"
    assert slc([("publish_layer", 1.0), ("generate_chart", 0.9), ("export_case_to_qgis", 0.8)]) == "visualization"


def test_stage_label_ties_break_to_earliest_stage():
    slc = agent_server._stage_label_for_candidates
    # one per stage -> tie -> earliest pipeline stage wins.
    assert slc(
        [("fetch_dem", 1.0), ("clip_raster_to_bbox", 0.9), ("compute_slope", 0.8), ("publish_layer", 0.7)]
    ) == "acquisition"


def test_stage_label_all_unknown_falls_back():
    slc = agent_server._stage_label_for_candidates
    assert slc([("weird_tool", 1.0), ("another_thing", 0.9)]) == "tool-selection"
