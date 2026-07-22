"""bbox-durability (live-reported): a plain FETCH's bbox durably anchors the
Case AOI, not just a domain-producing solve's.

CONFIRMED ROOT CAUSE: ``_pin_case_aoi_from_solve`` (LANE-C #1, see
``test_aoi_pin_lane_c.py``) only fires for a domain-producing SOLVER
(SWMM/SFINCS/MODFLOW). A Case whose activity is plain fetches (``fetch_dem``,
``fetch_landcover``, ...) never wrote ``CaseSummary.bbox`` at all — every such
Case row sat at ``bbox: None`` forever. Without an anchor,
``build_layers_present_note`` carried no AOI line, and a follow-up like "show
me the hillshade in the bounding box" made the model reverse-engineer the
extent from layer-id strings instead of reading it (live transcript: a small
local model burned its whole thinking budget trying to recover a bbox from a
TiTiler URI).

These tests pin the fix, ``_pin_case_aoi_from_tool_bbox`` (server.py), wired
into the real fetch-dispatch path:

(a) a bbox-carrying fetch on a bbox-less Case durably pins ``CaseSummary.bbox``
    AND the in-session ``state.case_bbox`` anchor.
(b) a second call with the SAME bbox does not redundantly upsert (debounced on
    a tight 6-decimal-place comparison).
(c) a bbox CHANGE (an explicit widen / a genuinely different place) updates
    the persisted anchor — latest-wins, matching the solve-pin's unconditional
    overwrite semantics.
(d) no active Case -> no write (and no crash).
(e) the per-turn [Case state] note renders the literal machine-usable
    ``[min_lon, min_lat, max_lon, max_lat]`` array with the REUSE instruction.

Uses ``fetch_buildings`` as the stub fetcher (mirrors ``test_aoi_pin_lane_c.
py``'s ``test_followup_fetch_defaults_to_pinned_aoi_end_to_end``) rather than
``fetch_dem`` / ``fetch_landcover`` — those two ARE the tools named in the
live bug report, but both sit in ``server.FETCH_CONFIRM_TOOLS`` (the
resolution-confirm gate) and a bare test dispatch never answers that gate, so
the call hangs to the 5-minute gate timeout (the KNOWN pre-existing
``test_active_aoi_repair_job2.py`` flake this task's brief warned not to
chase). ``fetch_buildings`` is a recognized bbox-taking fetcher
(``fetched_kind_for_tool``) that is NOT gated, so it exercises the exact same
``_pin_case_aoi_from_tool_bbox`` code path deterministically and fast.

Mirrors the harness in ``test_aoi_pin_lane_c.py`` / ``test_active_aoi_repair_
job2.py`` (real ``_emit_case_open`` / ``_invoke_tool_via_emitter`` paths,
MockMCPClient persistence).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.adapter import build_layers_present_note
from grace2_agent.persistence import CASES_COLLECTION, Persistence
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
from grace2_contracts.common import new_ulid
from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from .test_persistence import MockMCPClient, _fresh_case_summary

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
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    async def _fn(bbox=None, **_kw) -> LayerURI:
        return LayerURI(
            layer_id=f"buildings-{new_ulid()}",
            name="Building footprints (OSM)",
            layer_type="vector",
            uri="https://qgis.example/ogc/wms?LAYERS=buildings",
            style_preset="",
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
        reset_scenario_indexes_for_tests()
        reset_uri_registries_for_tests()


def _mk_case_no_bbox():
    return _fresh_case_summary().model_copy(update={"bbox": None})


# --------------------------------------------------------------------------- #
# (a) a bbox-carrying fetch on a bbox-less Case durably pins the AOI
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# (b) a repeated identical bbox does not redundantly upsert
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# (c) a bbox CHANGE updates the persisted anchor (latest-wins)
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# (d) no active Case -> no write, no crash
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# (e) the [Case state] note carries the literal machine-usable bbox array
# --------------------------------------------------------------------------- #


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
