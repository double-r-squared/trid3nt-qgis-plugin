"""LANE-C (#159 follow-up): pin the solve domain as the Case AOI + default
follow-up fetches to it.

CONFIRMED ROOT CAUSE (case 01KVM4NH7M8BT5HV21JV72MD97): there was NO pinned AOI.
``case.bbox`` stayed None (no ``upsert_case`` caller wrote it from a solve), so
``_turn_case_bbox`` returned None and the LLM free-handed a DIFFERENT bbox for
every follow-up tool call (5 boxes in one case). The SWMM solve ran on one
extent; ``fetch_buildings`` got a narrower+shorter box (87% width / 63% height of
the flood domain); rivers/dem/roads each got yet another smaller box.

These tests pin the four load-bearing behaviors of the fix:

(a) after a domain-producing solve the Case bbox is PINNED to the solve domain
    (persisted to ``CaseSummary.bbox`` AND cached on ``state.case_bbox`` so
    ``_turn_case_bbox`` returns it).
(b) a follow-up fetch with NO explicit bbox (and a drifted same-area bbox) uses
    the pinned AOI.
(c) a follow-up that names a DIFFERENT location (disjoint bbox) is NOT forced to
    the old AOI; an explicit WIDEN (encloses the pin) is also honored.
(d) the post-solve zoom-to is a SINGLE domain rectangle (the geocode snap is
    purged), not the #159 geocode-then-domain double.

Mirrors the harness in ``test_active_aoi_repair_job2.py`` (real ``_emit_case_open``
/ ``_invoke_tool_via_emitter`` paths, MockMCPClient persistence).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.persistence import Persistence
from trid3nt_server.scenario_reuse import reset_scenario_indexes_for_tests
from trid3nt_server.server import (
    SessionState,
    _emit_case_open,
    _maybe_default_fetch_bbox_to_pinned_aoi,
    _turn_case_bbox,
    get_persistence,
    set_persistence,
)
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from .test_persistence import MockMCPClient, _fresh_case_summary

# The solve domain (the live Austin AOI from the root-cause case).
_SOLVE_DOMAIN = (-97.755, 30.26, -97.725, 30.285)


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


@pytest.fixture()
def _stub_swmm_solver(monkeypatch):
    """Register a stub ``run_swmm_urban_flood`` that returns a peak LayerURI whose
    bbox IS the floored solve domain, and pass the solver-confirm gate through.

    The stub stands in for the real (pyswmm) workflow: the production workflow
    stamps the floored domain onto the returned peak ``bbox`` (see
    model_urban_flood_swmm.py ~815), so the stub returning ``_SOLVE_DOMAIN`` as the
    LayerURI bbox faithfully exercises the dispatch-site pin path.
    """
    name = "run_swmm_urban_flood"
    original = agent_tools.TOOL_REGISTRY.get(name)
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    async def _fn(bbox=None, **_kw) -> LayerURI:
        return LayerURI(
            layer_id=f"swmm-peak-{new_ulid()}",
            name="Peak flood depth",
            layer_type="raster",
            uri=(
                "https://titiler.example/cog/tiles/WebMercatorQuad/"
                "{z}/{x}/{y}.png?url=s3://x/peak.tif"
            ),
            style_preset="swmm_depth",
            bbox=tuple(_SOLVE_DOMAIN),  # type: ignore[arg-type]
        )

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    # The server-owned solver-confirm gate would PAUSE awaiting a user "proceed";
    # pass it through so the test exercises the post-solve pin path deterministically.
    monkeypatch.setattr(
        server,
        "_gate_on_solver_confirm",
        lambda ws, st, tn, params, _warning_id_out=None: _passthrough(params),
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


async def _passthrough(params):
    return True, params


# --------------------------------------------------------------------------- #
# (a) after a SWMM solve the Case bbox is pinned to the solve domain
# --------------------------------------------------------------------------- #


def test_solve_pins_case_aoi_to_solve_domain(
    _persistence_bound: Persistence, _stub_swmm_solver
) -> None:
    """After a domain-producing solve, ``CaseSummary.bbox`` + ``state.case_bbox``
    are pinned to the EXACT solve-domain extent (the peak LayerURI bbox)."""
    case = _fresh_case_summary()
    # Start with NO pinned AOI (the live pre-fix state).
    case = case.model_copy(update={"bbox": None})
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    assert _turn_case_bbox(state) is None  # precondition: no AOI yet

    peak = asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "run_swmm_urban_flood", {"bbox": list(_SOLVE_DOMAIN)}
        )
    )
    assert isinstance(peak, LayerURI)

    # The in-session anchor is pinned to the solve domain.
    assert _turn_case_bbox(state) == list(_SOLVE_DOMAIN)
    assert state.case_bbox == list(_SOLVE_DOMAIN)
    # And it is PERSISTED on the Case (a reopen rehydrates the same AOI).
    persisted = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert persisted is not None
    assert list(persisted.bbox) == list(_SOLVE_DOMAIN)


# --------------------------------------------------------------------------- #
# (b) a follow-up fetch with no explicit / a drifted bbox uses the pinned AOI
# --------------------------------------------------------------------------- #


def test_followup_fetch_defaults_to_pinned_aoi_end_to_end(
    _persistence_bound: Persistence, _stub_swmm_solver, monkeypatch
) -> None:
    """A bare follow-up fetch (no bbox) and a drifted narrower fetch BOTH run at
    the pinned solve domain, via the real dispatch path."""
    case = _fresh_case_summary().model_copy(update={"bbox": None})
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "run_swmm_urban_flood", {"bbox": list(_SOLVE_DOMAIN)}
        )
    )
    assert _turn_case_bbox(state) == list(_SOLVE_DOMAIN)

    # Capture the bbox the fetch tool actually ran with.
    seen: list = []

    def _fn(bbox=None, **_kw) -> LayerURI:
        seen.append(list(bbox) if bbox else None)
        return LayerURI(
            layer_id=f"buildings-{new_ulid()}",
            name="Building footprints (OSM)",
            layer_type="vector",
            uri="https://qgis.example/ogc/wms?LAYERS=buildings",
            style_preset="",
            bbox=tuple(bbox) if bbox else None,  # type: ignore[arg-type]
        )

    meta = AtomicToolMetadata(
        name="fetch_buildings", ttl_class="live-no-cache", cacheable=False
    )
    monkeypatch.setitem(
        agent_tools.TOOL_REGISTRY,
        "fetch_buildings",
        RegisteredTool(metadata=meta, fn=_fn, module=__name__),
    )

    # Bare follow-up: NO bbox -> runs at the pinned AOI.
    asyncio.run(
        server._invoke_tool_via_emitter(ws, state, "fetch_buildings", {})
    )
    assert seen[-1] == list(_SOLVE_DOMAIN), (
        "bare follow-up fetch did not default to the pinned solve domain"
    )

    # Drifted narrower box (the live bug: 87% width / 63% height) -> snaps to pin.
    drifted = [-97.755, 30.26, -97.73, 30.275]
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "fetch_buildings", {"bbox": drifted, "force_refetch": True}
        )
    )
    assert seen[-1] == list(_SOLVE_DOMAIN), (
        "drifted same-area fetch was not snapped to the pinned solve domain"
    )


# --------------------------------------------------------------------------- #
# (c) a follow-up naming a DIFFERENT location is NOT forced to the old AOI
# --------------------------------------------------------------------------- #


def test_followup_different_location_not_forced_to_pin() -> None:
    """A disjoint bbox (a different place) is honored; an explicit widen too."""
    pin = list(_SOLVE_DOMAIN)
    # Disjoint -> honored verbatim.
    elsewhere = {"bbox": [-100.0, 40.0, -99.9, 40.1]}
    assert (
        _maybe_default_fetch_bbox_to_pinned_aoi("fetch_buildings", elsewhere, pin)
        == elsewhere
    )
    # Explicit WIDEN (encloses the pin) -> honored verbatim.
    wider = {"bbox": [-98.0, 30.0, -97.5, 30.5]}
    assert (
        _maybe_default_fetch_bbox_to_pinned_aoi("fetch_river_geometry", wider, pin)
        == wider
    )


def test_fetch_default_snaps_drifted_but_honors_other(
    _persistence_bound: Persistence,
) -> None:
    """Pure-rule coverage of every branch of the fetch-default decision."""
    pin = list(_SOLVE_DOMAIN)
    f = _maybe_default_fetch_bbox_to_pinned_aoi
    # bare -> inject the pin
    assert f("fetch_buildings", {}, pin)["bbox"] == pin
    # drifted narrower same-area -> snap
    assert f("fetch_dem", {"bbox": [-97.755, 30.26, -97.73, 30.275]}, pin)[
        "bbox"
    ] == pin
    # near-exact pin (jitter under the tight tol) -> no change
    jitter = {"bbox": [-97.755001, 30.260001, -97.724999, 30.285001]}
    assert f("fetch_roads_osm", jitter, pin) == jitter
    # disjoint -> honored
    far = {"bbox": [-100.0, 40.0, -99.9, 40.1]}
    assert f("fetch_buildings", far, pin) == far
    # non-fetch tool -> no-op (even with a pin)
    assert f("run_swmm_urban_flood", {}, pin) == {}
    # no pin -> no-op
    narrow = {"bbox": [-97.755, 30.26, -97.73, 30.275]}
    assert f("fetch_buildings", narrow, None) == narrow


# --------------------------------------------------------------------------- #
# (d) the post-solve zoom-to is a single domain rectangle (no #159 double)
# --------------------------------------------------------------------------- #


def test_solve_emits_single_domain_zoom_to(
    _persistence_bound: Persistence, _stub_swmm_solver
) -> None:
    """A pre-solve geocode snap (small collapsed bbox) is PURGED so the closing
    turn accumulator carries ONLY the domain zoom-to (the #159 double-rectangle
    fix)."""
    case = _fresh_case_summary().model_copy(update={"bbox": None})
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))

    # Simulate the earlier geocode snap to the SMALL collapsed bbox this turn.
    small_geocode = [-97.7405, 30.2718, -97.7395, 30.2728]
    state.current_turn_map_commands.append(
        {"command": "zoom-to", "args": {"bbox": small_geocode}}
    )

    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "run_swmm_urban_flood", {"bbox": list(_SOLVE_DOMAIN)}
        )
    )

    zoom_tos = [
        c["args"]["bbox"]
        for c in state.current_turn_map_commands
        if isinstance(c, dict) and c.get("command") == "zoom-to"
    ]
    assert zoom_tos == [list(_SOLVE_DOMAIN)], (
        f"expected a single domain zoom-to, got {zoom_tos!r} (the geocode snap "
        "was not purged -> the #159 double rectangle persists)"
    )
