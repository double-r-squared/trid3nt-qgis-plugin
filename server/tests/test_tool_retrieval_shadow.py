"""Tool-retrieval SHADOW + recall@k tests (tool-retrieval kickoff, orchestrator half).

Pins the orchestrator half of the tool-retrieval feature:

1. DEFAULT OFF is BYTE-IDENTICAL to today -- no retrieval computed, no shadow
   row logged, build_tool_declarations gets the FULL registry.
2. SHADOW mode logs the would-be-visible set WITHOUT changing the sent catalog
   (build_tool_declarations still gets the FULL registry; the shadow event fires).
3. FAIL-OPEN: a retrieval error in shadow/enforce never trims the catalog.
4. ENFORCE mode subsets the registry to the visible set, the CORE FLOOR stays a
   subset, and the Case's monotonic AllowedToolSet never shrinks across turns.
5. recall@k computation on a synthetic telemetry fixture (overall + per-flow +
   the missed-tool list).
6. fetch_glm_lightning is in the ALWAYS-OFFLOAD set (#6 escalation).

These cover the shadow=zero-behavior-change + enforce-invariants + recall surfaces
the kickoff names. ASCII only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.adapter import GeminiSettings
from trid3nt_contracts import new_ulid


# --------------------------------------------------------------------------- #
# Minimal harness (mirrors test_multi_turn_loop).
# --------------------------------------------------------------------------- #
@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 — protocol shim
        self.sent.append(msg)


def _make_text_chunk(text: str):
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


def _make_fc_chunk(name: str, args: dict, call_id: str = "c1"):
    fc = MagicMock()
    fc.name = name
    fc.id = call_id
    fc.args = args
    part = MagicMock()
    part.function_call = fc
    part.text = None
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = None
    return chunk


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro",
        project="test",
        location="us-central1",
        use_vertex=True,
    )


async def _drive_one_turn(
    monkeypatch,
    *,
    mode: str,
    chunks: list,
    user_text: str = "show me the flood map",
    state=None,
    dispatch=None,
):
    """Drive ONE _stream_gemini_reply turn with the given retrieval mode.

    Returns (state, registries_seen, dispatch_log) where registries_seen is the
    list of objects passed to build_tool_declarations (one per turn iteration).
    """
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    monkeypatch.setenv("TRID3NT_TOOL_RETRIEVAL", mode)

    turn_responses = iter([iter([c]) for c in chunks])
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = (
        lambda **_: next(turn_responses)
    )

    registries_seen: list = []

    def _capture_build(reg):
        registries_seen.append(reg)
        return []

    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        if dispatch is not None:
            return dispatch(name, args)
        return {"ok": True}

    sock = _FakeSocket()
    if state is None:
        state = SessionState(session_id=new_ulid())

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
         ), \
         patch.object(
             agent_server, "build_tool_declarations", side_effect=_capture_build
         ):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), user_text, "research"
        )
    return state, registries_seen, dispatch_log


# --------------------------------------------------------------------------- #
# 1. DEFAULT OFF -- byte-identical to today.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_off_mode_passes_full_registry_no_shadow(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY

    shadow_calls: list = []
    with patch.object(
        agent_server, "emit_shadow_selection_event",
        side_effect=lambda **kw: shadow_calls.append(kw),
    ):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="off", chunks=[_make_text_chunk("done")]
        )

    # OFF computes NO retrieval and logs NO shadow row.
    assert shadow_calls == []
    # The object passed to build_tool_declarations IS the live TOOL_REGISTRY.
    assert len(regs) == 1
    assert regs[0] is TOOL_REGISTRY


@pytest.mark.asyncio
async def test_unknown_mode_is_treated_as_off(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY

    shadow_calls: list = []
    with patch.object(
        agent_server, "emit_shadow_selection_event",
        side_effect=lambda **kw: shadow_calls.append(kw),
    ):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="enabled-please", chunks=[_make_text_chunk("done")]
        )
    assert shadow_calls == []
    assert regs[0] is TOOL_REGISTRY


# --------------------------------------------------------------------------- #
# 2. SHADOW -- logs would-be set, FULL registry still sent.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_shadow_mode_logs_set_but_sends_full_registry(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.discovery.tool_retrieval as tr

    fake_visible = {"geocode_location", "fetch_dem", "list_categories"}
    shadow_calls: list = []

    with patch.object(tr, "retrieve_visible_tools", return_value=fake_visible), \
         patch.object(
             agent_server, "emit_shadow_selection_event",
             side_effect=lambda **kw: shadow_calls.append(kw),
         ):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="shadow", chunks=[_make_text_chunk("done")]
        )

    # The shadow event fired with the would-be set + mode=shadow.
    assert len(shadow_calls) == 1
    assert shadow_calls[0]["visible_tools"] == fake_visible
    assert shadow_calls[0]["mode"] == "shadow"
    # ZERO behavior change: build_tool_declarations STILL got the FULL registry.
    assert regs[0] is TOOL_REGISTRY


# --------------------------------------------------------------------------- #
# 3. FAIL-OPEN on retrieval error / empty result.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_shadow_fail_open_on_retrieval_error(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.discovery.tool_retrieval as tr

    def _boom(*_a, **_k):
        raise RuntimeError("index exploded")

    with patch.object(tr, "retrieve_visible_tools", side_effect=_boom), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="shadow", chunks=[_make_text_chunk("done")]
        )
    # FAIL-OPEN: the full registry is sent, never trimmed.
    assert regs[0] is TOOL_REGISTRY


@pytest.mark.asyncio
async def test_enforce_fail_open_on_empty_result(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.discovery.tool_retrieval as tr

    # An empty would-be set must FAIL-OPEN (never empty / core-only catalog).
    with patch.object(tr, "retrieve_visible_tools", return_value=set()), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="enforce", chunks=[_make_text_chunk("done")]
        )
    assert regs[0] is TOOL_REGISTRY


# --------------------------------------------------------------------------- #
# 4. ENFORCE -- subsets, core-floor subset, monotonic no-shrink.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enforce_subsets_registry_and_keeps_core_floor(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.categories import HOT_SET_TOOLS
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.discovery.tool_retrieval as tr

    # Pick a small real subset of registered tools that includes the core floor.
    floor = {t for t in HOT_SET_TOOLS if t in TOOL_REGISTRY}
    visible = set(floor) | {"fetch_dem"}
    visible &= set(TOOL_REGISTRY)

    with patch.object(tr, "retrieve_visible_tools", return_value=visible), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="enforce", chunks=[_make_text_chunk("done")]
        )

    sent = regs[0]
    # Enforce -> a NEW (subset) dict, NOT the live registry.
    assert sent is not TOOL_REGISTRY
    sent_names = set(sent)
    # It is a strict subset of the full registry.
    assert sent_names <= set(TOOL_REGISTRY)
    assert len(sent_names) < len(TOOL_REGISTRY)
    # CORE FLOOR is a subset of what was sent.
    assert floor <= sent_names
    # fetch_dem (the requested tool) survived.
    assert "fetch_dem" in sent_names


@pytest.mark.asyncio
async def test_enforce_allowed_set_is_monotonic_across_turns(monkeypatch):
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.discovery.tool_retrieval as tr

    state = SessionState(session_id=new_ulid())

    real = [t for t in ("fetch_dem", "fetch_topobathy", "geocode_location") if t in TOOL_REGISTRY]
    assert real, "expected at least one real tool to test with"

    # Turn 1: retrieval surfaces real[0].
    with patch.object(tr, "retrieve_visible_tools", return_value={real[0]}), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        await _drive_one_turn(
            monkeypatch, mode="enforce", chunks=[_make_text_chunk("a")], state=state
        )
    after_turn1 = set(state.allowed_tool_set.as_frozenset())
    assert real[0] in after_turn1

    # Turn 2: retrieval surfaces a DIFFERENT real tool. real[0] must NOT leave.
    other = real[1] if len(real) > 1 else real[0]
    with patch.object(tr, "retrieve_visible_tools", return_value={other}), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            monkeypatch, mode="enforce", chunks=[_make_text_chunk("b")], state=state
        )
    after_turn2 = set(state.allowed_tool_set.as_frozenset())
    # MONOTONIC: the set only grows -- everything from turn 1 is still present.
    assert after_turn1 <= after_turn2
    assert real[0] in after_turn2
    # And the catalog sent on turn 2 includes the once-visible real[0].
    assert real[0] in set(regs[0])


# --------------------------------------------------------------------------- #
# 5. recall@k computation on a synthetic fixture.
# --------------------------------------------------------------------------- #
def test_compute_recall_at_k_synthetic():
    from trid3nt_server.tool_catalog_http import compute_recall_at_k

    # Turn A (SWMM flow): dispatched 3 llm tools; retrieval would have kept 2,
    # dropped fetch_buildings -> recall 2/3 for this turn.
    # Turn B (SFINCS flow): dispatched 2; retrieval kept both -> recall 2/2.
    shadow = [
        {
            "record_type": "tool_retrieval_shadow",
            "session_id": "S1",
            "turn_id": "TA",
            "k": 25,
            "visible_tools": ["fetch_dem", "run_swmm_urban_flood"],
        },
        {
            "record_type": "tool_retrieval_shadow",
            "session_id": "S1",
            "turn_id": "TB",
            "k": 25,
            "visible_tools": ["fetch_topobathy", "run_model_flood_scenario"],
        },
    ]
    tool_records = [
        # Turn A -- SWMM.
        {"source": "llm", "session_id": "S1", "turn_id": "TA", "tool_name": "fetch_dem"},
        {"source": "llm", "session_id": "S1", "turn_id": "TA", "tool_name": "fetch_buildings"},
        {"source": "llm", "session_id": "S1", "turn_id": "TA", "tool_name": "run_swmm_urban_flood"},
        # Turn B -- SFINCS.
        {"source": "llm", "session_id": "S1", "turn_id": "TB", "tool_name": "fetch_topobathy"},
        {"source": "llm", "session_id": "S1", "turn_id": "TB", "tool_name": "run_model_flood_scenario"},
        # A workflow-sourced dispatch must be IGNORED by recall.
        {"source": "workflow", "session_id": "S1", "turn_id": "TA", "tool_name": "publish_layer"},
        # A dispatch with NO shadow row (different turn) -- excluded.
        {"source": "llm", "session_id": "S1", "turn_id": "TZ", "tool_name": "fetch_dem"},
    ]

    out = compute_recall_at_k(tool_records, shadow)

    # Overall: 4 hits / 5 measured dispatches = 0.8.
    assert out["dispatches_measured"] == 5
    assert out["hits"] == 4
    assert out["misses"] == 1
    assert out["overall"] == pytest.approx(0.8, abs=1e-6)
    assert out["turns_measured"] == 2
    assert out["k"] == 25

    by_flow = {row["flow"]: row for row in out["by_flow"]}
    assert by_flow["SWMM"]["recall"] == pytest.approx(2 / 3, abs=1e-4)
    assert by_flow["SWMM"]["misses"] == 1
    assert by_flow["SFINCS"]["recall"] == pytest.approx(1.0, abs=1e-6)
    # MODFLOW never ran -> null recall, zero dispatches.
    assert by_flow["MODFLOW"]["recall"] is None
    assert by_flow["MODFLOW"]["dispatches"] == 0

    # The missed-tool list names fetch_buildings under the SWMM flow.
    missed = {m["name"]: m for m in out["missed_tools"]}
    assert "fetch_buildings" in missed
    assert missed["fetch_buildings"]["count"] == 1
    assert missed["fetch_buildings"]["flows"] == ["SWMM"]


def test_compute_recall_at_k_empty_when_no_shadow():
    from trid3nt_server.tool_catalog_http import compute_recall_at_k

    out = compute_recall_at_k(
        [{"source": "llm", "turn_id": "T1", "tool_name": "fetch_dem"}],
        [],
    )
    assert out["overall"] is None
    assert out["turns_measured"] == 0
    assert out["missed_tools"] == []


def test_build_telemetry_summary_folds_recall_section(monkeypatch, tmp_path):
    """The summary carries a recall_at_k section read from the SAME JSONL sink."""
    import json as _json
    from trid3nt_server import tool_catalog_http as http

    path = tmp_path / "tel.jsonl"
    rows = [
        # A shadow row + a matching dispatched llm tool that WAS in the set.
        {
            "record_type": "tool_retrieval_shadow",
            "session_id": "S1",
            "turn_id": "T1",
            "k": 25,
            "ts": "2026-06-23T00:00:00.000Z",
            "visible_tools": ["fetch_dem"],
        },
        {
            "session_id": "S1",
            "turn_id": "T1",
            "ts": "2026-06-23T00:00:01.000Z",
            "tool_name": "fetch_dem",
            "source": "llm",
            "success": True,
            "latency_ms": 10.0,
        },
    ]
    path.write_text("\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setenv("TRID3NT_TELEMETRY_PATH", str(path))

    # No Persistence bound -> file path.
    with patch.object(http, "_load_recent_records_from_mongo", return_value=[]):
        summary = asyncio.run(http.build_telemetry_summary())

    # The shadow row did NOT inflate the per-tool dispatch counts.
    assert summary["total_dispatches"] == 1
    rk = summary["recall_at_k"]
    assert rk["overall"] == pytest.approx(1.0, abs=1e-6)
    assert rk["hits"] == 1
    assert rk["missed_tools"] == []


# --------------------------------------------------------------------------- #
# 6. fetch_glm_lightning is in the ALWAYS-OFFLOAD set (#6).
# --------------------------------------------------------------------------- #
def test_fetch_glm_lightning_always_offloaded():
    from trid3nt_server import server as agent_server

    assert "fetch_glm_lightning" in agent_server._ALWAYS_OFFLOAD_SYNC_TOOLS
    # And the predicate off-loads it even in dark off mode.
    assert agent_server._should_offload_sync_tool("fetch_glm_lightning") is True
