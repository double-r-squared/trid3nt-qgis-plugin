"""Tool-retrieval enforce + recall@k tests (the built-in surfacing path).

Enforce is unconditional now (K is the only lever). These pin:

1. ENFORCE subsets the registry to the visible set, the CORE FLOOR stays a
   subset, and the Case's monotonic visible set never shrinks across turns.
2. FAIL-OPEN: a retrieval error or an empty result never trims the catalog --
   it falls back to the DEFAULT declarable registry (full MINUS the pool-hidden
   internal/catalog tiers; engine templates are ordinary members).
3. The per-turn selection event fires (recall@k telemetry) with mode="enforce".
4. recall@k computation on a synthetic telemetry fixture (overall + per-flow +
   the missed-tool list).
5. fetch_glm_lightning is in the ALWAYS-OFFLOAD set.

ASCII only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server.adapters.adapter import ModelSettings
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
    """A fake turn (scripted-provider dict) emitting one narration delta."""
    return {"text": text}


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro",
        project="test",
        location="us-central1",
        use_vertex=True,
    )


def _non_template_names() -> set[str]:
    """The names in the DEFAULT declarable registry: the full TOOL_REGISTRY
    MINUS the pool-hidden tiers (tier=internal / tier=catalog).

    Engine templates (tier=template) ARE in the default declarable set -- they
    are ordinary retrieval-pool tools, callable directly. Only tier=internal (an
    absorbed seam, fetch_copernicus_dem) and tier=catalog (arm-flagged, none in
    the default config) are withheld. The object passed to build_tool_declarations
    is a NEW filtered dict (server._default_declarable_registry), not the live
    registry identity."""
    from trid3nt_server.tools import TOOL_REGISTRY

    return {
        name
        for name, entry in TOOL_REGISTRY.items()
        if getattr(entry.metadata, "tier", "general") not in ("internal", "catalog")
    }


async def _drive_one_turn(
    fake_llm,
    *,
    chunks: list,
    user_text: str = "show me the flood map",
    state=None,
    dispatch=None,
):
    """Drive ONE _stream_model_reply turn (enforce is unconditional).

    Returns (state, registries_seen, dispatch_log) where registries_seen is the
    list of objects passed to build_tool_declarations (one per turn iteration).
    """
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    fake_llm.script(chunks)

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

    with patch.object(
             agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
         ), \
         patch.object(
             agent_server, "build_tool_declarations", side_effect=_capture_build
         ):
        await agent_server._stream_model_reply(
            sock, state, _settings(), user_text, "research"
        )
    return state, registries_seen, dispatch_log


# --------------------------------------------------------------------------- #
# 1. The selection event fires with mode="enforce".
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enforce_emits_selection_event(fake_llm):
    from trid3nt_server import server as agent_server
    import trid3nt_server.tools.search.tool_retrieval as tr

    visible = {"geocode_location", "fetch_dem"}
    shadow_calls: list = []
    with patch.object(tr, "retrieve_visible_tools", return_value=visible), \
         patch.object(
             agent_server, "emit_shadow_selection_event",
             side_effect=lambda **kw: shadow_calls.append(kw),
         ):
        await _drive_one_turn(fake_llm, chunks=[_make_text_chunk("done")])

    assert len(shadow_calls) == 1
    assert shadow_calls[0]["visible_tools"] == visible
    assert shadow_calls[0]["mode"] == "enforce"


# --------------------------------------------------------------------------- #
# 2. FAIL-OPEN on retrieval error / empty result.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fail_open_on_retrieval_error(fake_llm):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    def _boom(*_a, **_k):
        raise RuntimeError("index exploded")

    with patch.object(tr, "retrieve_visible_tools", side_effect=_boom), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("done")]
        )
    # FAIL-OPEN: never trimmed -- falls back to the DEFAULT declarable registry.
    assert set(regs[0]) == _non_template_names()
    assert regs[0] is not TOOL_REGISTRY


@pytest.mark.asyncio
async def test_fail_open_on_empty_result(fake_llm):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    # An empty would-be set must FAIL-OPEN (never empty / core-only catalog).
    with patch.object(tr, "retrieve_visible_tools", return_value=set()), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("done")]
        )
    assert set(regs[0]) == _non_template_names()
    assert regs[0] is not TOOL_REGISTRY


# --------------------------------------------------------------------------- #
# 3. ENFORCE -- subsets, core-floor subset, monotonic no-shrink.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_enforce_subsets_registry_and_keeps_core_floor(fake_llm):
    from trid3nt_server import server as agent_server
    from trid3nt_server.tools.search.tool_retrieval import CORE_FLOOR
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    # Pick a small real subset of registered tools that includes the core floor.
    floor = {t for t in CORE_FLOOR if t in TOOL_REGISTRY}
    visible = set(floor) | {"fetch_dem"}
    visible &= set(TOOL_REGISTRY)

    with patch.object(tr, "retrieve_visible_tools", return_value=visible), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("done")]
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
async def test_enforce_visible_set_is_monotonic_across_turns(fake_llm):
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.tools import TOOL_REGISTRY
    import trid3nt_server.tools.search.tool_retrieval as tr

    state = SessionState(session_id=new_ulid())

    real = [t for t in ("fetch_dem", "fetch_topobathy", "geocode_location") if t in TOOL_REGISTRY]
    assert real, "expected at least one real tool to test with"

    # Turn 1: retrieval surfaces real[0].
    with patch.object(tr, "retrieve_visible_tools", return_value={real[0]}), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("a")], state=state
        )
    after_turn1 = set(state.visible_tools)
    assert real[0] in after_turn1

    # Turn 2: retrieval surfaces a DIFFERENT real tool. real[0] must NOT leave.
    other = real[1] if len(real) > 1 else real[0]
    with patch.object(tr, "retrieve_visible_tools", return_value={other}), \
         patch.object(agent_server, "emit_shadow_selection_event"):
        _state, regs, _disp = await _drive_one_turn(
            fake_llm, chunks=[_make_text_chunk("b")], state=state
        )
    after_turn2 = set(state.visible_tools)
    # MONOTONIC: the set only grows -- everything from turn 1 is still present.
    assert after_turn1 <= after_turn2
    assert real[0] in after_turn2
    # And the catalog sent on turn 2 includes the once-visible real[0].
    assert real[0] in set(regs[0])


# --------------------------------------------------------------------------- #
# 4. recall@k computation on a synthetic fixture.
# --------------------------------------------------------------------------- #
def test_compute_recall_at_k_synthetic():
    from trid3nt_server.server.protocol.catalog_http import compute_recall_at_k

    # Turn A (SWMM flow): dispatched 3 llm tools; retrieval would have kept 2,
    # dropped fetch_buildings -> recall 2/3 for this turn.
    # Turn B (SFINCS flow): dispatched 2; retrieval kept both -> recall 2/2.
    shadow = [
        {
            "record_type": "tool_retrieval_shadow",
            "session_id": "S1",
            "turn_id": "TA",
            "k": 25,
            "visible_tools": ["fetch_dem", "swmm_urban_flood"],
        },
        {
            "record_type": "tool_retrieval_shadow",
            "session_id": "S1",
            "turn_id": "TB",
            "k": 25,
            "visible_tools": ["fetch_topobathy", "sfincs_flood"],
        },
    ]
    tool_records = [
        # Turn A -- SWMM.
        {"source": "llm", "session_id": "S1", "turn_id": "TA", "tool_name": "fetch_dem"},
        {"source": "llm", "session_id": "S1", "turn_id": "TA", "tool_name": "fetch_buildings"},
        {"source": "llm", "session_id": "S1", "turn_id": "TA", "tool_name": "swmm_urban_flood"},
        # Turn B -- SFINCS.
        {"source": "llm", "session_id": "S1", "turn_id": "TB", "tool_name": "fetch_topobathy"},
        {"source": "llm", "session_id": "S1", "turn_id": "TB", "tool_name": "sfincs_flood"},
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
    from trid3nt_server.server.protocol.catalog_http import compute_recall_at_k

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
    from trid3nt_server.server.protocol import catalog_http as http

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

    # Telemetry is JSONL-only -> summary reads the sink directly.
    summary = asyncio.run(http.build_telemetry_summary())

    # The shadow row did NOT inflate the per-tool dispatch counts.
    assert summary["total_dispatches"] == 1
    rk = summary["recall_at_k"]
    assert rk["overall"] == pytest.approx(1.0, abs=1e-6)
    assert rk["hits"] == 1
    assert rk["missed_tools"] == []


# --------------------------------------------------------------------------- #
# 5. fetch_glm_lightning is in the ALWAYS-OFFLOAD set.
# --------------------------------------------------------------------------- #
def test_fetch_glm_lightning_always_offloaded():
    from trid3nt_server import server as agent_server

    assert "fetch_glm_lightning" in agent_server._ALWAYS_OFFLOAD_SYNC_TOOLS
    # And the predicate off-loads it even in dark off mode.
    assert agent_server._should_offload_sync_tool("fetch_glm_lightning") is True
