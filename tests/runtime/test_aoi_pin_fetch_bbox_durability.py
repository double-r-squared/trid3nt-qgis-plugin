"""A plain FETCH's bbox durably anchors the Case AOI, not only a solve's.

``_pin_case_aoi_from_tool_bbox`` writes ``CaseSummary.bbox`` and the in-session
anchor from a bbox-carrying fetch, debounces an identical box at six decimal
places, overwrites a changed one, and writes nothing with no active Case. The
stub fetcher is ``fetch_buildings`` because it is bbox-taking and NOT gated."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.adapters.adapter import build_layers_present_note
from trid3nt_server.persistence import CASES_COLLECTION, Persistence
from trid3nt_server.server import (
    SessionState,
    _emit_case_open,
    _turn_case_bbox,
    get_persistence,
    set_persistence,
)
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.render.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from tests._fakes import MockMCPClient, _fresh_case_summary

_AOI = (-97.755, 30.26, -97.725, 30.285)
_AOI_WIDER = (-97.8, 30.2, -97.7, 30.3)
_ELSEWHERE = (-100.0, 40.0, -99.9, 40.1)

_FETCH_TOOL = "fetch_buildings"  # NOT in FETCH_CONFIRM_TOOLS — no gate to hang on


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
    mcp = MockMCPClient()
    set_persistence(Persistence(mcp))
    try:
        yield get_persistence(), mcp
    finally:
        set_persistence(saved)


@pytest.fixture()
def _stub_fetch_buildings():
    """Register a stub ``fetch_buildings`` that echoes its ``bbox`` onto the result."""
    assert _FETCH_TOOL not in server.FETCH_CONFIRM_TOOLS, (
        f"{_FETCH_TOOL} is now gated — pick a different unstubbed fetcher"
    )
    original = agent_tools.TOOL_REGISTRY.get(_FETCH_TOOL)
    reset_uri_registries_for_tests()

    async def _fn(bbox=None, **_kw) -> LayerURI:
        return LayerURI(
            layer_id=f"buildings-{new_ulid()}",
            name="Building footprints (OSM)",
            layer_type="vector",
            uri="https://qgis.example/ogc/wms?LAYERS=buildings",
            bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
        )

    meta = AtomicToolMetadata(
        name=_FETCH_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_FETCH_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[_FETCH_TOOL] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(_FETCH_TOOL, None)
        reset_uri_registries_for_tests()


def _mk_case_no_bbox():
    return _fresh_case_summary().model_copy(update={"bbox": None})




def test_fetch_with_bbox_seeds_bboxless_case(
    _persistence_bound, _stub_fetch_buildings
) -> None:
    persistence, _mcp = _persistence_bound
    case = _mk_case_no_bbox()
    asyncio.run(persistence.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    assert _turn_case_bbox(state) is None  # precondition: no AOI yet

    result = asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _FETCH_TOOL, {"bbox": list(_AOI)}
        )
    )
    assert isinstance(result, LayerURI)

    # In-session anchor is seeded.
    assert _turn_case_bbox(state) == list(_AOI)
    assert state.case_bbox == list(_AOI)
    # Durably persisted (a Case reopen / follow-up turn rehydrates the SAME AOI).
    persisted = asyncio.run(persistence.get_case(case.case_id))
    assert persisted is not None
    assert list(persisted.bbox) == list(_AOI)


#
# Direct unit calls to ``_pin_case_aoi_from_tool_bbox`` rather than the full
# dispatch: a real fetch dispatch ALSO durably writes ``loaded_layers`` (the
# emitter's own per-call layer persistence) via the SAME ``upsert_case`` seam,
# and every such write's ``$set`` carries the case's CURRENT ``bbox`` too
# (``Persistence.upsert_case`` sets the whole ``model_dump``) — so a raw
# call/field count at the dispatch level cannot distinguish "an unrelated
# write that happens to carry today's bbox" from "the pin itself redundantly
# wrote". Calling the pin function directly isolates exactly what it does.


def _install_upsert_spy(monkeypatch, persistence: Persistence) -> list:
    """Wrap ``persistence.upsert_case`` with a call-count spy (delegates through)."""
    calls: list = []
    original = persistence.upsert_case

    async def _spy(case, **kw):
        calls.append(case.case_id)
        return await original(case, **kw)

    monkeypatch.setattr(persistence, "upsert_case", _spy)
    return calls


def test_repeated_identical_bbox_no_redundant_upsert(
    _persistence_bound, monkeypatch
) -> None:
    persistence, _mcp = _persistence_bound
    case = _mk_case_no_bbox()
    asyncio.run(persistence.upsert_case(case))
    upserts = _install_upsert_spy(monkeypatch, persistence)

    state = SessionState(session_id=new_ulid())
    state.active_case_id = case.case_id  # _turn_case_bbox needs a turn-bound Case
    params = {"bbox": list(_AOI)}
    asyncio.run(
        server._pin_case_aoi_from_tool_bbox(
            state, case_id=case.case_id, tool_name=_FETCH_TOOL, params=params
        )
    )
    assert len(upserts) == 1
    assert _turn_case_bbox(state) == list(_AOI)

    # Same bbox again -> the in-session anchor is refreshed (latest-wins is a
    # cheap no-op here) but NO redundant durable write.
    asyncio.run(
        server._pin_case_aoi_from_tool_bbox(
            state, case_id=case.case_id, tool_name=_FETCH_TOOL, params=params
        )
    )
    assert len(upserts) == 1, (
        "a repeated identical bbox re-upserted the Case row (debounce failed)"
    )
    assert _turn_case_bbox(state) == list(_AOI)

    persisted = asyncio.run(persistence.get_case(case.case_id))
    assert list(persisted.bbox) == list(_AOI)




def test_bbox_change_updates_persisted_anchor(
    _persistence_bound, _stub_fetch_buildings
) -> None:
    persistence, _mcp = _persistence_bound
    case = _mk_case_no_bbox()
    asyncio.run(persistence.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))

    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _FETCH_TOOL, {"bbox": list(_AOI)}
        )
    )
    assert _turn_case_bbox(state) == list(_AOI)

    # An explicit WIDEN (encloses the current pin) -> honored verbatim by the
    # fetch-default rule, so the tool call actually runs at the WIDER extent;
    # the anchor follows it.
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws,
            state,
            _FETCH_TOOL,
            {"bbox": list(_AOI_WIDER), "force_refetch": True},
        )
    )
    assert _turn_case_bbox(state) == list(_AOI_WIDER)
    persisted = asyncio.run(persistence.get_case(case.case_id))
    assert list(persisted.bbox) == list(_AOI_WIDER)

    # A genuinely DIFFERENT place (disjoint) -> honored verbatim too, and the
    # anchor MOVES (latest-wins: "the bounding box" means the current focus).
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws,
            state,
            _FETCH_TOOL,
            {"bbox": list(_ELSEWHERE), "force_refetch": True},
        )
    )
    assert _turn_case_bbox(state) == list(_ELSEWHERE)
    persisted2 = asyncio.run(persistence.get_case(case.case_id))
    assert list(persisted2.bbox) == list(_ELSEWHERE)




def test_no_active_case_no_write(_persistence_bound, _stub_fetch_buildings) -> None:
    persistence, mcp = _persistence_bound
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    assert state.active_case_id is None

    result = asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _FETCH_TOOL, {"bbox": list(_AOI)}
        )
    )
    assert isinstance(result, LayerURI)
    assert state.case_bbox is None
    upserts = [
        args
        for name, args in mcp.calls
        if name == "update-one" and args.get("collection") == CASES_COLLECTION
    ]
    assert upserts == []




def test_layers_present_note_has_literal_bbox_array() -> None:
    note = build_layers_present_note([], case_bbox=list(_AOI))
    assert note is not None
    expected = f"[{_AOI[0]}, {_AOI[1]}, {_AOI[2]}, {_AOI[3]}]"
    assert expected in note
    assert "REUSE this exact extent" in note


def test_layers_present_note_end_to_end_after_fetch_seed(
    _persistence_bound, _stub_fetch_buildings
) -> None:
    """The literal bbox array survives the real fetch -> pin -> note path."""
    persistence, _mcp = _persistence_bound
    case = _mk_case_no_bbox()
    asyncio.run(persistence.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _FETCH_TOOL, {"bbox": list(_AOI)}
        )
    )

    note = build_layers_present_note([], case_bbox=_turn_case_bbox(state))
    assert note is not None
    expected = f"[{_AOI[0]}, {_AOI[1]}, {_AOI[2]}, {_AOI[3]}]"
    assert expected in note
