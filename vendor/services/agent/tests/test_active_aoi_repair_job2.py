"""JOB 2: active-AOI repair + per-turn [Case state] context note.

These tests pin the three load-bearing behaviors of JOB 2:

2a (active-AOI repair) — ``_turn_case_bbox`` reads the durable
    ``SessionState.case_bbox`` cache instead of the non-existent
    ``state.active_case`` attribute. Pre-fix it ALWAYS returned None (the read
    targeted a missing attribute), so the agent had no active-AOI signal and the
    reuse seams were AOI-starved. We drive the REAL case-select path
    (``_emit_case_open``) so the cache is populated exactly as production does,
    then assert ``_turn_case_bbox`` returns the Case AOI (and clears on
    deselect / no-active-Case).

2b (per-turn context note) — the layers-present + reuse-AOI note
    (``build_layers_present_note``) is built from the LIVE emitter layers + the
    cached Case AOI and carries the "REUSE this exact extent, do NOT re-geocode"
    instruction. We assert the builder includes the loaded layers AND the AOI
    bbox line, and that the dispatch-side injection shape (append as a synthetic
    [Case state] user turn) is well-formed.

2c (fetch reuse short-circuit, end-to-end via the real ``_turn_case_bbox``) — a
    bare follow-up fetch (no bbox of its own) whose AOI resolves to the Case AOI
    is answered by a loaded same-kind layer -> short-circuit, no re-fetch. This
    mirrors ``test_fetch_reuse_dispatch_f96.py`` but does NOT monkeypatch
    ``_turn_case_bbox`` — it proves the JOB-2 ``case_bbox`` field actually drives
    the reuse guard once populated by the real case-select path.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.adapter import build_layers_present_note
from grace2_agent.persistence import Persistence
from grace2_agent.scenario_reuse import reset_scenario_indexes_for_tests
from grace2_agent.server import (
    SessionState,
    _emit_case_open,
    _turn_case_bbox,
    get_persistence,
    set_persistence,
)
from grace2_agent.tools import RegisteredTool
from grace2_agent.uri_registry import reset_uri_registries_for_tests
from grace2_contracts.case import CaseCommandEnvelopePayload
from grace2_contracts.common import new_ulid
from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from .test_persistence import MockMCPClient, _fresh_case_summary

# The bbox _fresh_case_summary stamps onto every seeded Case (Fort Myers).
_CASE_AOI = (-82.0, 26.5, -81.8, 26.7)


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw) -> None:  # type: ignore[no-untyped-def]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)


@pytest.fixture()
def _persistence_bound():
    saved = get_persistence()
    set_persistence(Persistence(MockMCPClient()))
    try:
        yield get_persistence()
    finally:
        set_persistence(saved)


# --------------------------------------------------------------------------- #
# 2a — _turn_case_bbox reads the cached Case AOI after a real case-select
# --------------------------------------------------------------------------- #


def test_turn_case_bbox_none_before_any_case() -> None:
    """A fresh session with no active Case has no AOI anchor -> None."""
    state = SessionState(session_id=new_ulid())
    assert state.case_bbox is None
    assert _turn_case_bbox(state) is None


def test_turn_case_bbox_returns_cached_bbox_after_case_select(
    _persistence_bound: Persistence,
) -> None:
    """After the REAL case-open path, ``_turn_case_bbox`` returns the Case AOI.

    This is the keystone repair: pre-fix ``_turn_case_bbox`` read
    ``getattr(state, "active_case", None)`` — an attribute SessionState never
    had — so it always returned None. Now it reads ``state.case_bbox``, which
    ``_emit_case_open`` populates from the persisted ``CaseSummary.bbox``.
    """
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))

    # The cache is populated and matches the persisted Case AOI.
    assert state.case_bbox == list(_CASE_AOI)
    # And _turn_case_bbox surfaces it (the reuse seams + per-turn note read this).
    assert _turn_case_bbox(state) == list(_CASE_AOI)


def test_turn_case_bbox_cleared_on_deselect(
    _persistence_bound: Persistence,
) -> None:
    """Deselecting the Case (return to root) clears the cached AOI anchor."""
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    assert _turn_case_bbox(state) == list(_CASE_AOI)

    # Deselect: a root prompt auto-creates a FRESH Case, so the just-exited
    # Case's extent must NOT linger as the AOI anchor.
    asyncio.run(
        server._handle_case_command(
            ws, state, CaseCommandEnvelopePayload(command="deselect")
        )
    )
    assert state.case_bbox is None
    assert _turn_case_bbox(state) is None


def test_turn_case_bbox_none_when_id_present_but_no_cache() -> None:
    """A pinned case id with no cached bbox still yields None (no stale guess).

    Guards the contract: ``_turn_case_bbox`` returns ``state.case_bbox``
    verbatim (None when uncached) rather than fabricating an AOI.
    """
    state = SessionState(session_id=new_ulid())
    state.active_case_id = new_ulid()  # a case is active...
    state.case_bbox = None  # ...but its bbox was never cached
    assert _turn_case_bbox(state) is None


# --------------------------------------------------------------------------- #
# 2b — the per-turn [Case state] note includes the layers + AOI reuse line
# --------------------------------------------------------------------------- #


def test_layers_present_note_includes_layers_and_aoi() -> None:
    """The note lists the loaded layer AND the reuse-AOI bbox instruction."""
    loaded = [
        {
            "layer_id": "wdpa-fortmyers",
            "name": "Protected Areas (WDPA)",
            "layer_type": "vector",
            "uri": "https://qgis.example/ogc/wms?LAYERS=wdpa-fortmyers",
        }
    ]
    note = build_layers_present_note(loaded, case_bbox=list(_CASE_AOI))
    assert note is not None
    assert note.startswith("[Case state]")
    # The loaded layer is surfaced with its reusable handle.
    assert "wdpa-fortmyers" in note
    assert "handle=wdpa-fortmyers" in note
    # The AOI anchor + the "do NOT re-geocode" instruction are present.
    assert "Case AOI bbox" in note
    assert "-82.0" in note and "26.7" in note
    assert "re-geocode" in note.lower()


def test_layers_present_note_aoi_only_when_no_layers() -> None:
    """With no layers but a Case AOI, the note still carries the reuse-AOI line."""
    note = build_layers_present_note([], case_bbox=list(_CASE_AOI))
    assert note is not None
    assert "Case AOI bbox" in note
    assert "re-geocode" in note.lower()


def test_layers_present_note_none_when_empty() -> None:
    """No layers and no AOI -> no note (the dispatch injection is a no-op)."""
    assert build_layers_present_note([], case_bbox=None) is None


def test_per_turn_injection_shape_appends_case_state_user_turn() -> None:
    """The dispatch-side injection appends ONE synthetic [Case state] user turn.

    Mirrors the ``_stream_gemini_reply`` wiring: build the note from the live
    emitter dicts + the cached AOI, then append it as the last history turn
    (before the user message) WITHOUT mutating the entry-captured history list.
    """
    turn_history = [{"role": "user", "text": "model the flooding"}]
    loaded = [
        {
            "layer_id": "flood-depth-A",
            "name": "Flood depth",
            "layer_type": "raster",
            "role": "primary",
        }
    ]
    note = build_layers_present_note(loaded, case_bbox=list(_CASE_AOI))
    assert note is not None

    injected = list(turn_history) + [{"role": "user", "text": note}]
    # Entry-captured list is untouched (job-0269 contract).
    assert turn_history == [{"role": "user", "text": "model the flooding"}]
    # The note is the LAST history turn and carries the Case-state marker.
    assert injected[-1]["role"] == "user"
    assert injected[-1]["text"].startswith("[Case state]")
    assert "flood-depth-A" in injected[-1]["text"]


# --------------------------------------------------------------------------- #
# 2c — a bare re-fetch short-circuits via the REAL _turn_case_bbox (no patch)
# --------------------------------------------------------------------------- #

_FETCHES: list[dict] = []


@pytest.fixture()
def _stub_fetch_dem():
    """Launch-counting ``fetch_dem`` stub returning a DEM layer at an AOI."""
    name = "fetch_dem"
    original = agent_tools.TOOL_REGISTRY.get(name)
    _FETCHES.clear()
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(bbox=None, **_kw) -> LayerURI:
        _FETCHES.append({"bbox": bbox})
        bb = tuple(bbox) if bbox else _CASE_AOI
        return LayerURI(
            layer_id=f"dem-{len(_FETCHES)}",
            name="Elevation (DEM)",  # carries the 'dem' kind marker via name
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/WebMercatorQuad/"
                "{z}/{x}/{y}.png?url=s3://x/dem.tif"
            ),
            style_preset="dem",
            bbox=bb,  # type: ignore[arg-type]
        )

    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[name] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(name, None)
        reset_scenario_indexes_for_tests()
        reset_uri_registries_for_tests()


def test_bare_followup_refetch_short_circuits_via_real_case_bbox(
    _persistence_bound: Persistence, _stub_fetch_dem
) -> None:
    """A bare re-fetch reuses the loaded same-kind layer using the REAL AOI cache.

    The Case AOI comes through the genuine ``_emit_case_open`` -> ``case_bbox``
    cache (no monkeypatched ``_turn_case_bbox``), so this is the end-to-end proof
    that JOB 2's repair actually feeds the fetch reuse short-circuit: with the
    pre-fix always-None ``_turn_case_bbox`` the bare follow-up (no bbox) could not
    resolve its AOI and would re-fetch a duplicate.
    """
    case = _fresh_case_summary()
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    # Real case-select populates state.case_bbox from the persisted AOI.
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    assert _turn_case_bbox(state) == list(_CASE_AOI)

    # First fetch at the Case AOI: the DEM layer lands on the map.
    first = asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)}
        )
    )
    assert isinstance(first, LayerURI)
    assert len(_FETCHES) == 1

    # Bare follow-up: NO bbox -> requested AOI resolves to the Case AOI via the
    # real cache -> the loaded same-kind layer answers it -> short-circuit.
    second = asyncio.run(
        server._invoke_tool_via_emitter(ws, state, "fetch_dem", {})
    )
    assert len(_FETCHES) == 1, (
        "bare follow-up re-fetched a duplicate — the real _turn_case_bbox did "
        "not feed the fetch reuse guard (JOB 2 repair regressed)"
    )
    assert isinstance(second, dict)
    assert second.get("reused") is True
    assert second.get("status") == "reused_existing"
    assert "not re-fetch" in second.get("note", "").lower()


def test_bare_followup_refetches_without_case_bbox(
    _persistence_bound: Persistence, _stub_fetch_dem
) -> None:
    """Control: with NO cached AOI, a bare follow-up cannot resolve -> re-fetch.

    Proves the short-circuit in the prior test is driven specifically by the
    JOB-2 ``case_bbox`` cache, not by some unrelated dedup. Here ``case_bbox``
    is never populated (no case-select), so ``_turn_case_bbox`` is None and the
    bare follow-up (no bbox) has no AOI to compare -> conservative re-fetch.
    """
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    assert _turn_case_bbox(state) is None

    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "fetch_dem", {"bbox": list(_CASE_AOI)}
        )
    )
    assert len(_FETCHES) == 1

    # Bare follow-up with no AOI anchor -> re-fetch (conservative, by design).
    res = asyncio.run(
        server._invoke_tool_via_emitter(ws, state, "fetch_dem", {})
    )
    assert len(_FETCHES) == 2
    assert isinstance(res, LayerURI)
