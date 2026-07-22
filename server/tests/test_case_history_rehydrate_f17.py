"""F17 (ux-batch-1 J8): Case-reopen rehydrates the LLM conversation.

A follow-up turn in an existing Case must see prior work so the model stops
recomputing (e.g. asking for a hillshade in the Fort Myers flood Case should
NOT re-run the whole flood). The job-0245 fix clears ``state.chat_history`` on
Case open/sync to kill an in-memory CROSS-CASE leak; F17 refills it from the
PERSISTED PER-CASE store (which is keyed by Case, so the leak cannot return).

These tests pin:
- Reopening a Case rehydrates ``state.chat_history`` from persisted messages
  (non-empty, correct order, text turns).
- The "layers already present" note is injected and lists the persisted layers.
- History is bounded to the cap (head dropped, tail kept).
- Cross-case isolation: opening Case B does NOT surface Case A's messages.
- The job-0245 unbound-persistence clean-slate is preserved (no rehydration
  when there is nothing persisted to rehydrate from).
- The pure adapter helpers (``rehydrate_history_from_case`` /
  ``build_layers_present_note``) convert shapes correctly in isolation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from grace2_agent.adapter import (
    REHYDRATE_HISTORY_CAP,
    build_layers_present_note,
    rehydrate_history_from_case,
)
from grace2_agent.persistence import Persistence
from grace2_agent.server import (
    SessionState,
    _emit_case_open,
    get_persistence,
    set_persistence,
)
from grace2_contracts.case import (
    CaseChatMessage,
    CaseSummary,
    ToolCardRecord,
)
from grace2_contracts.common import new_ulid

from .test_persistence import MockMCPClient
from .test_server_case_handlers import MockWebSocket


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _persistence_bound():
    saved = get_persistence()
    set_persistence(Persistence(MockMCPClient()))
    try:
        yield get_persistence()
    finally:
        set_persistence(saved)


_T0 = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


def _case_with_layers(layers: list[dict] | None = None) -> CaseSummary:
    return CaseSummary(
        case_id=new_ulid(),
        title="Hurricane Ian — Fort Myers flood scenario",
        created_at=_T0,
        updated_at=_T0,
        status="active",
        bbox=(-82.0, 26.5, -81.8, 26.7),
        primary_hazard="flood",
        loaded_layer_summaries=layers or [],
    )


def _layer(layer_id: str, name: str, layer_type: str = "raster") -> dict:
    return {
        "layer_id": layer_id,
        "name": name,
        "layer_type": layer_type,
        "uri": f"gs://bucket/{layer_id}.tif",
        "style_preset": "depth",
        "visible": True,
        "role": "primary",
        "temporal": False,
    }


def _msg(case_id: str, role: str, content: str, *, seq: int = 0,
         tool_card: ToolCardRecord | None = None) -> CaseChatMessage:
    return CaseChatMessage(
        message_id=new_ulid(),
        case_id=case_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        tool_card=tool_card,
        created_at=_T0 + timedelta(seconds=seq),
    )


def _seed_case(p: Persistence, case: CaseSummary,
               messages: list[CaseChatMessage]) -> None:
    asyncio.run(p.upsert_case(case))
    for m in messages:
        asyncio.run(p.append_chat_message(m))


# --------------------------------------------------------------------------- #
# Pure adapter helper unit tests
# --------------------------------------------------------------------------- #


def test_rehydrate_converts_text_turns_in_order() -> None:
    case_id = new_ulid()
    rows = [
        _msg(case_id, "user", "model the Fort Myers flood", seq=0),
        _msg(case_id, "agent", "Done — flood depth raster published.", seq=2),
    ]
    history, dropped = rehydrate_history_from_case(rows, [])
    assert dropped == 0
    assert history == [
        {"role": "user", "text": "model the Fort Myers flood"},
        {"role": "agent", "text": "Done — flood depth raster published."},
    ]


def test_rehydrate_collapses_tool_rows_to_text_line() -> None:
    case_id = new_ulid()
    card = ToolCardRecord(tool_name="run_model_flood_scenario", state="complete")
    rows = [
        _msg(case_id, "user", "model the flood", seq=0),
        _msg(case_id, "tool", card.model_dump_json(), seq=1, tool_card=card),
        _msg(case_id, "agent", "flood done", seq=2),
    ]
    history, _ = rehydrate_history_from_case(rows, [])
    assert history[1] == {
        "role": "model",
        "text": "[tool run_model_flood_scenario completed]",
    }


def test_rehydrate_tool_row_failed_outcome() -> None:
    case_id = new_ulid()
    card = ToolCardRecord(tool_name="fetch_3dep_dem", state="failed")
    history, _ = rehydrate_history_from_case(
        [_msg(case_id, "tool", card.model_dump_json(), tool_card=card)], []
    )
    assert history == [{"role": "model", "text": "[tool fetch_3dep_dem failed]"}]


def test_rehydrate_tool_row_from_content_json_only() -> None:
    """No typed tool_card (pre-job-0267 doc) — parse the JSON ``content``."""
    case_id = new_ulid()
    row = _msg(case_id, "tool",
               '{"tool_name": "compute_hillshade", "state": "complete"}')
    history, _ = rehydrate_history_from_case([row], [])
    assert history == [{"role": "model", "text": "[tool compute_hillshade completed]"}]


def test_rehydrate_drops_empty_text_rows() -> None:
    case_id = new_ulid()
    rows = [
        _msg(case_id, "user", "do a thing", seq=0),
        _msg(case_id, "agent", "   ", seq=1),  # whitespace-only → dropped
        _msg(case_id, "agent", "ok done", seq=2),
    ]
    history, _ = rehydrate_history_from_case(rows, [])
    assert history == [
        {"role": "user", "text": "do a thing"},
        {"role": "agent", "text": "ok done"},
    ]


def test_rehydrate_bounded_to_cap_drops_head() -> None:
    case_id = new_ulid()
    rows = [_msg(case_id, "user", f"turn {i}", seq=i) for i in range(60)]
    history, dropped = rehydrate_history_from_case(rows, [], cap=40)
    assert dropped == 20
    assert len(history) == 40
    # Tail kept: the FIRST replayed turn is turn 20 (turns 0..19 dropped),
    # the LAST is turn 59.
    assert history[0] == {"role": "user", "text": "turn 20"}
    assert history[-1] == {"role": "user", "text": "turn 59"}


def test_rehydrate_default_cap_constant() -> None:
    assert REHYDRATE_HISTORY_CAP == 40


def test_layers_present_note_lists_layers() -> None:
    note = build_layers_present_note([
        _layer("flood-depth-01HX", "Flood depth (Ian)", "raster"),
        _layer("nlcd-fm", "Land cover", "raster"),
        _layer("wdpa-fm", "Protected areas", "vector"),
    ])
    assert note is not None
    assert "ALREADY produced" in note
    # job-0326: the note now forbids re-RUN as well as re-fetch/recompute and
    # tags each layer RESULT[...] / INPUT so the model recognizes an existing
    # simulation output and never re-launches the solver that made it.
    assert "do NOT re-run, re-fetch, or recompute" in note
    # job-0325 (F54) + job-0326: the per-layer line surfaces the reusable handle
    # (== layer_id), the underlying uri, AND the role label. A flood-depth layer
    # classifies as a RESULT of the flood-depth family; landcover is an INPUT.
    assert (
        "Flood depth (Ian) (id=flood-depth-01HX, RESULT[flood-depth], raster, "
        "handle=flood-depth-01HX, uri=gs://bucket/flood-depth-01HX.tif)"
    ) in note
    # The fixture marks every layer role="primary", so a non-scenario layer
    # still reads as a RESULT (role-based fallback); a scenario-family layer_id
    # would read RESULT[<family>] (covered by the flood-depth line above).
    assert (
        "Protected areas (id=wdpa-fm, RESULT, vector, handle=wdpa-fm, "
        "uri=gs://bucket/wdpa-fm.tif)"
    ) in note
    # The firm reuse / no-refetch / no-rerun instruction is appended.
    assert "REUSE these" in note
    assert "Do NOT re-fetch or recompute a layer" in note
    assert "FORBIDDEN" in note


def test_layers_present_note_none_when_empty() -> None:
    assert build_layers_present_note([]) is None
    assert build_layers_present_note(None) is None


def test_layers_present_note_includes_aoi_bbox() -> None:
    # F20 / panel-fix: the Case AOI bbox is a durable anchor in the note.
    note = build_layers_present_note(None, case_bbox=(-82.0, 26.5, -81.8, 26.7))
    assert note is not None
    assert "Case AOI bbox" in note
    assert "-82.0" in note and "26.7" in note
    assert "do NOT re-derive or re-geocode" in note
    # With BOTH layers and bbox, the note carries both segments.
    both = build_layers_present_note(
        [_layer("flood-depth-01HX", "Flood depth", "raster")],
        case_bbox=(-82.0, 26.5, -81.8, 26.7),
    )
    assert both is not None
    assert "ALREADY produced" in both and "Case AOI bbox" in both


def test_layers_present_note_ignores_malformed_bbox() -> None:
    assert build_layers_present_note(None, case_bbox=[1, 2, 3]) is None  # len != 4
    assert build_layers_present_note(None, case_bbox="nope") is None
    assert build_layers_present_note(None, case_bbox=None) is None


def test_rehydrate_long_case_retains_aoi_bbox_after_head_dropped() -> None:
    # Panel-flagged MAJOR (routing-recompute lens): on a long Case the tail cap
    # drops the head turn that named the AOI. The bbox carried in the note must
    # survive so a follow-up that fetches fresh data still has the extent.
    case_id = new_ulid()
    head = _msg(case_id, "user", "model the Fort Myers flood (Lee County FL)", seq=0)
    follow = [_msg(case_id, "user", f"follow-up {i}", seq=i + 1) for i in range(60)]
    rows = [head, *follow]
    history, dropped = rehydrate_history_from_case(
        rows,
        [_layer("flood-depth-01HX", "Flood depth", "raster")],
        cap=40,
        case_bbox=(-82.0, 26.5, -81.8, 26.7),
    )
    assert dropped == len(rows) - 40
    # The head AOI place name was elided by the cap...
    assert not any("Lee County" in h.get("text", "") for h in history[:-1])
    # ...but the bbox survives in the note (last turn) — the AOI is recoverable.
    note = history[-1]
    assert note["role"] == "model"
    assert "Case AOI bbox" in note["text"]
    assert "-82.0" in note["text"]


def test_rehydrate_appends_layers_note_last() -> None:
    case_id = new_ulid()
    rows = [_msg(case_id, "user", "model the flood", seq=0)]
    layers = [_layer("flood-depth-01HX", "Flood depth", "raster")]
    history, _ = rehydrate_history_from_case(rows, layers)
    # User turn first, layers note appended LAST as a model turn.
    assert history[0] == {"role": "user", "text": "model the flood"}
    assert history[-1]["role"] == "model"
    assert "flood-depth-01HX" in history[-1]["text"]


# --------------------------------------------------------------------------- #
# Server wiring: _emit_case_open rehydrates state.chat_history
# --------------------------------------------------------------------------- #


def test_case_open_rehydrates_chat_history(_persistence_bound: Persistence) -> None:
    p = _persistence_bound
    case = _case_with_layers()
    rows = [
        _msg(case.case_id, "user", "model the Fort Myers flood", seq=0),
        _msg(case.case_id, "agent", "Flood depth raster published.", seq=2),
    ]
    _seed_case(p, case, rows)

    state = SessionState(session_id=new_ulid())
    # Start with stale in-memory history from a DIFFERENT Case to prove the
    # clean-slate reset still runs before rehydration.
    state.chat_history = [{"role": "user", "text": "STALE other-case turn"}]
    ws = MockWebSocket()
    asyncio.run(_emit_case_open(ws, state, case.case_id))

    texts = [(h["role"], h["text"]) for h in state.chat_history]
    assert ("user", "model the Fort Myers flood") in texts
    assert ("agent", "Flood depth raster published.") in texts
    # Stale cross-case turn is gone (clean-slate held).
    assert not any("STALE" in t for _, t in texts)
    # Order preserved: user turn precedes the agent turn.
    assert texts.index(("user", "model the Fort Myers flood")) < texts.index(
        ("agent", "Flood depth raster published.")
    )


def test_case_open_injects_layers_present_note(
    _persistence_bound: Persistence,
) -> None:
    p = _persistence_bound
    case = _case_with_layers([
        _layer("flood-depth-01HX", "Flood depth (Ian)", "raster"),
        _layer("hillshade-01HY", "Hillshade", "raster"),
    ])
    _seed_case(p, case, [_msg(case.case_id, "user", "model the flood", seq=0)])

    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(MockWebSocket(), state, case.case_id))

    note_turn = state.chat_history[-1]
    assert note_turn["role"] == "model"
    assert "flood-depth-01HX" in note_turn["text"]
    assert "hillshade-01HY" in note_turn["text"]
    assert "do NOT re-run, re-fetch, or recompute" in note_turn["text"]
    # F20 / panel-fix: the Case AOI bbox (from session_state.case.bbox) flows
    # all the way through _emit_case_open into the note.
    assert "Case AOI bbox" in note_turn["text"]
    assert "-82.0" in note_turn["text"]


def test_case_open_bounds_history_to_cap(_persistence_bound: Persistence) -> None:
    p = _persistence_bound
    case = _case_with_layers()
    # 50 user turns → exceeds the 40-row cap.
    rows = [_msg(case.case_id, "user", f"turn {i}", seq=i) for i in range(50)]
    _seed_case(p, case, rows)

    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(MockWebSocket(), state, case.case_id))

    # The replayed transcript is the capped tail (40 rows). The Case has a bbox
    # (panel-fix), so a single AOI note is appended after the cap — it is the
    # durable anchor that must outlive the head-drop, so history is cap + 1.
    transcript = [h for h in state.chat_history if "Case AOI bbox" not in h["text"]]
    assert len(transcript) == REHYDRATE_HISTORY_CAP
    assert transcript[0] == {"role": "user", "text": "turn 10"}
    assert transcript[-1] == {"role": "user", "text": "turn 49"}
    # The note rides AFTER the capped tail.
    assert "Case AOI bbox" in state.chat_history[-1]["text"]
    assert len(state.chat_history) == REHYDRATE_HISTORY_CAP + 1


def test_case_open_cross_case_isolation(_persistence_bound: Persistence) -> None:
    """Opening Case B must NOT surface Case A's persisted messages (guardrail)."""
    p = _persistence_bound
    case_a = _case_with_layers()
    case_b = _case_with_layers()
    _seed_case(p, case_a, [
        _msg(case_a.case_id, "user", "CASE-A: model the Twin Falls spill", seq=0),
        _msg(case_a.case_id, "agent", "CASE-A: MODFLOW running", seq=1),
    ])
    _seed_case(p, case_b, [
        _msg(case_b.case_id, "user", "CASE-B: model the Fort Myers flood", seq=0),
    ])

    state = SessionState(session_id=new_ulid())
    # Open A first (loads A's history), then switch to B.
    asyncio.run(_emit_case_open(MockWebSocket(), state, case_a.case_id))
    assert any("CASE-A" in h["text"] for h in state.chat_history)

    asyncio.run(_emit_case_open(MockWebSocket(), state, case_b.case_id))
    joined = " ".join(h["text"] for h in state.chat_history)
    assert "CASE-B" in joined
    assert "CASE-A" not in joined  # the cross-case leak the guardrail forbids


def test_case_open_unbound_persistence_keeps_clean_slate() -> None:
    """job-0245: with Persistence unbound, the reset still yields empty history
    (rehydration runs only AFTER the persistence binding check)."""
    saved = get_persistence()
    set_persistence(None)
    try:
        state = SessionState(session_id=new_ulid())
        state.chat_history = [{"role": "user", "text": "stale"}]
        asyncio.run(_emit_case_open(MockWebSocket(), state, new_ulid()))
        assert state.chat_history == []
    finally:
        set_persistence(saved)


# --------------------------------------------------------------------------- #
# cold-raster fix: a pure case-OPEN must materialize the cold snapshot
# --------------------------------------------------------------------------- #


def test_case_open_writes_case_view_snapshot(
    _persistence_bound: Persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful ``_emit_case_open`` fires a best-effort case-view snapshot
    (+ thin manifest) write for the OPENED Case.

    The cold view (box asleep) fetches the presigned ``case-views/{id}.json``
    snapshot; before this fix a pure OPEN wrote none, so a freshly-opened Case's
    rasters could not paint until a later mutation. We mock the two persisters
    and assert each is called once with the opened ``case_id``. The production
    calls are fire-and-forget (``asyncio.create_task``), so we drive the open in
    an async test and yield the loop a couple of times to let the detached tasks
    run before asserting.
    """
    import grace2_agent.server as server

    p = _persistence_bound
    case = _case_with_layers()
    _seed_case(p, case, [_msg(case.case_id, "user", "model the flood", seq=0)])

    snap_calls: list[str | None] = []
    manifest_calls: list[str | None] = []

    async def _fake_snapshot(state, *, case_id=None):  # noqa: ANN001, ANN202
        snap_calls.append(case_id)

    async def _fake_manifest(state, *, case_id=None):  # noqa: ANN001, ANN202
        manifest_calls.append(case_id)

    monkeypatch.setattr(server, "_persist_case_view_snapshot", _fake_snapshot)
    monkeypatch.setattr(server, "_persist_case_manifest", _fake_manifest)

    async def _drive() -> None:
        state = SessionState(session_id=new_ulid())
        await _emit_case_open(MockWebSocket(), state, case.case_id)
        # The snapshot + manifest are detached via asyncio.create_task so the
        # open never blocks on the Dynamo+S3 round-trips; yield the loop so the
        # background tasks run before we assert.
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(_drive())

    assert snap_calls == [case.case_id]
    assert manifest_calls == [case.case_id]


def test_case_open_snapshot_failure_does_not_break_open(
    _persistence_bound: Persistence, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot write is best-effort: even if the persister raises (it
    should not - both swallow their own errors - but defend the contract), the
    open still completes and rehydrates chat history normally."""
    import grace2_agent.server as server

    p = _persistence_bound
    case = _case_with_layers()
    _seed_case(p, case, [_msg(case.case_id, "user", "model the flood", seq=0)])

    async def _boom_snapshot(state, *, case_id=None):  # noqa: ANN001, ANN202
        raise RuntimeError("S3 down")

    async def _boom_manifest(state, *, case_id=None):  # noqa: ANN001, ANN202
        raise RuntimeError("S3 down")

    monkeypatch.setattr(server, "_persist_case_view_snapshot", _boom_snapshot)
    monkeypatch.setattr(server, "_persist_case_manifest", _boom_manifest)

    async def _drive() -> None:
        state = SessionState(session_id=new_ulid())
        # The open itself must NOT raise - the persisters are detached.
        await _emit_case_open(MockWebSocket(), state, case.case_id)
        for _ in range(3):
            await asyncio.sleep(0)
        # The open still did its real work: chat history rehydrated.
        assert any(
            h.get("text") == "model the flood" for h in state.chat_history
        )

    asyncio.run(_drive())
