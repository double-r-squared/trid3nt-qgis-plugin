"""The Case AOI is the extent a run solved over, and a later fetch fills from it.

A completed workflow publishes its primary layer at the domain it solved, so that
box is written onto the Case and cached in the session; the write is skipped at
the same extent and follows a changed one. A plain fetch states an area, it does
not decide the Case's."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.adapters.adapter import build_layers_present_note
from trid3nt_server.persistence import CASES_COLLECTION, Persistence
from trid3nt_server.render.uri_registry import reset_uri_registries_for_tests
from trid3nt_server.server import (
    SessionState,
    _emit_case_open,
    _turn_case_bbox,
    get_persistence,
    set_persistence,
)
from trid3nt_server.tools import RegisteredTool

from tests._fakes import MockMCPClient, _fresh_case_summary

_DOMAIN = (-97.755, 30.26, -97.725, 30.285)
_OTHER_DOMAIN = (-100.0, 40.0, -99.9, 40.1)

_SOLVE_TOOL = "telemac_rain_on_grid"
_FETCH_TOOL = "fetch_buildings"  # bbox-taking and NOT gated


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


def _stub(name: str, *, is_workflow: bool, role: str):
    """Register a stub tool that publishes one layer at the bbox it was given."""
    original = agent_tools.TOOL_REGISTRY.get(name)
    reset_uri_registries_for_tests()

    async def _fn(bbox, **_kw) -> LayerURI:
        # bbox is REQUIRED, as every bbox-taking tool declares it, so the
        # dispatch's fill rule has a slot to fill.
        return LayerURI(
            layer_id=f"{name}-{new_ulid()}",
            name=f"{name} output",
            layer_type="vector",
            uri="https://qgis.example/ogc/wms?LAYERS=out",
            role=role,  # type: ignore[arg-type]
            bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
        )

    if is_workflow:
        _fn.workflow = object()  # the declaration that this tool RUNS something
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
        reset_uri_registries_for_tests()


@pytest.fixture()
def _stub_solver():
    yield from _stub(_SOLVE_TOOL, is_workflow=True, role="primary")


@pytest.fixture()
def _stub_fetch():
    assert _FETCH_TOOL not in server.FETCH_CONFIRM_TOOLS, (
        f"{_FETCH_TOOL} is now gated - pick a different unstubbed fetcher"
    )
    yield from _stub(_FETCH_TOOL, is_workflow=False, role="context")


def _open_case(persistence: Persistence):
    case = _fresh_case_summary().model_copy(update={"bbox": None})
    asyncio.run(persistence.upsert_case(case))
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    assert _turn_case_bbox(state) is None
    return case, ws, state


def _run(ws, state, tool, params):
    return asyncio.run(server._invoke_tool_via_emitter(ws, state, tool, params))


def test_a_completed_solve_pins_the_case_aoi_to_the_extent_it_solved(
    _persistence_bound, _stub_solver
) -> None:
    persistence, _mcp = _persistence_bound
    case, ws, state = _open_case(persistence)

    result = _run(ws, state, _SOLVE_TOOL, {"bbox": list(_DOMAIN)})
    assert isinstance(result, LayerURI)

    assert _turn_case_bbox(state) == list(_DOMAIN)
    assert state.case_bbox == list(_DOMAIN)
    persisted = asyncio.run(persistence.get_case(case.case_id))
    assert persisted is not None
    assert list(persisted.bbox) == list(_DOMAIN)


def test_a_second_run_at_the_same_extent_writes_nothing_new(
    _persistence_bound, monkeypatch
) -> None:
    persistence, _mcp = _persistence_bound
    case = _fresh_case_summary().model_copy(update={"bbox": None})
    asyncio.run(persistence.upsert_case(case))

    upserts: list = []
    original = persistence.upsert_case

    async def _spy(row, **kw):
        upserts.append(row.case_id)
        return await original(row, **kw)

    monkeypatch.setattr(persistence, "upsert_case", _spy)

    state = SessionState(session_id=new_ulid())
    state.active_case_id = case.case_id
    pin = server.pin_case_aoi_from_solve
    asyncio.run(pin(state, case_id=case.case_id, bbox=_DOMAIN))
    assert len(upserts) == 1
    asyncio.run(pin(state, case_id=case.case_id, bbox=_DOMAIN))
    assert len(upserts) == 1, "a re-run at the same extent re-wrote the Case row"
    assert _turn_case_bbox(state) == list(_DOMAIN)

    # A run over different ground moves the anchor - latest run wins.
    asyncio.run(pin(state, case_id=case.case_id, bbox=_OTHER_DOMAIN))
    assert len(upserts) == 2
    persisted = asyncio.run(persistence.get_case(case.case_id))
    assert list(persisted.bbox) == list(_OTHER_DOMAIN)


def test_a_fetch_states_its_own_area_and_never_pins_the_case(
    _persistence_bound, _stub_fetch
) -> None:
    persistence, _mcp = _persistence_bound
    case, ws, state = _open_case(persistence)

    _run(ws, state, _FETCH_TOOL, {"bbox": list(_DOMAIN)})

    assert state.case_bbox is None
    persisted = asyncio.run(persistence.get_case(case.case_id))
    assert persisted.bbox is None


def test_a_later_fetch_with_no_area_of_its_own_runs_at_the_pin(
    _persistence_bound, _stub_solver, _stub_fetch
) -> None:
    persistence, _mcp = _persistence_bound
    case, ws, state = _open_case(persistence)
    _run(ws, state, _SOLVE_TOOL, {"bbox": list(_DOMAIN)})

    landed = _run(ws, state, _FETCH_TOOL, {})
    assert isinstance(landed, LayerURI)
    assert list(landed.bbox) == list(_DOMAIN)

    # A fetch that DOES state an area keeps it - the user's box wins over the pin.
    elsewhere = _run(ws, state, _FETCH_TOOL, {"bbox": list(_OTHER_DOMAIN)})
    assert list(elsewhere.bbox) == list(_OTHER_DOMAIN)


def test_with_no_active_case_the_pin_writes_nothing(
    _persistence_bound, _stub_solver
) -> None:
    _persistence, mcp = _persistence_bound
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    assert state.active_case_id is None

    result = _run(ws, state, _SOLVE_TOOL, {"bbox": list(_DOMAIN)})
    assert isinstance(result, LayerURI)
    assert state.case_bbox is None
    upserts = [
        args
        for name, args in mcp.calls
        if name == "update-one" and args.get("collection") == CASES_COLLECTION
    ]
    assert upserts == []


def test_the_case_state_note_carries_the_pinned_extent_literally(
    _persistence_bound, _stub_solver
) -> None:
    persistence, _mcp = _persistence_bound
    _case, ws, state = _open_case(persistence)
    _run(ws, state, _SOLVE_TOOL, {"bbox": list(_DOMAIN)})

    note = build_layers_present_note([], case_bbox=_turn_case_bbox(state))
    assert note is not None
    assert f"[{_DOMAIN[0]}, {_DOMAIN[1]}, {_DOMAIN[2]}, {_DOMAIN[3]}]" in note
    assert "REUSE this exact extent" in note
