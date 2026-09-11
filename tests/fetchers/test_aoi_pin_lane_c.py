"""Pin the Case AOI from a fetch bbox, and default follow-up fetches to it.

With no pinned AOI the model free-hands a different bbox for every follow-up, so
the layers stop covering the same ground. Pinned: the first bbox-carrying fetch
writes its extent to the Case and caches it; a follow-up with no bbox or a
drifted one runs at that extent, and a disjoint location is not forced to it."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.persistence import Persistence
from trid3nt_server.server import (
    SessionState,
    _emit_case_open,
    _maybe_default_fetch_bbox_to_pinned_aoi,
    _turn_case_bbox,
    get_persistence,
    set_persistence,
)
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from tests._fakes import MockMCPClient, _fresh_case_summary

_AOI = (-97.755, 30.26, -97.725, 30.285)


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
# (a) the first bbox-carrying fetch pins the Case AOI, and every follow-up with
#     no bbox or a drifted one runs at it
# --------------------------------------------------------------------------- #


def test_followup_fetch_defaults_to_pinned_aoi_end_to_end(
    _persistence_bound: Persistence, monkeypatch
) -> None:
    """Through the real dispatch path: the first fetch pins the AOI durably, then
    a bare follow-up and a drifted narrower one BOTH run at the pin."""
    case = _fresh_case_summary().model_copy(update={"bbox": None})
    asyncio.run(_persistence_bound.upsert_case(case))

    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    asyncio.run(_emit_case_open(ws, state, case.case_id))
    assert _turn_case_bbox(state) is None  # precondition: no AOI yet

    # Capture the bbox the fetch tool actually ran with.
    seen: list = []

    def _fn(bbox=None, **_kw) -> LayerURI:
        seen.append(list(bbox) if bbox else None)
        return LayerURI(
            layer_id=f"buildings-{new_ulid()}",
            name="Building footprints (OSM)",
            layer_type="vector",
            uri="https://qgis.example/ogc/wms?LAYERS=buildings",
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

    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "fetch_buildings", {"bbox": list(_AOI)}
        )
    )
    # The in-session anchor is pinned, and PERSISTED so a reopen rehydrates it.
    assert _turn_case_bbox(state) == list(_AOI)
    assert state.case_bbox == list(_AOI)
    persisted = asyncio.run(_persistence_bound.get_case(case.case_id))
    assert persisted is not None
    assert list(persisted.bbox) == list(_AOI)

    # Bare follow-up: NO bbox -> runs at the pinned AOI.
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "fetch_buildings", {"force_refetch": True}
        )
    )
    assert seen[-1] == list(_AOI), (
        "bare follow-up fetch did not default to the pinned AOI"
    )

    # Drifted narrower box (87% width / 63% height) -> snaps to the pin.
    drifted = [-97.755, 30.26, -97.73, 30.275]
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, "fetch_buildings", {"bbox": drifted, "force_refetch": True}
        )
    )
    assert seen[-1] == list(_AOI), (
        "drifted same-area fetch was not snapped to the pinned AOI"
    )


# --------------------------------------------------------------------------- #
# (c) a follow-up naming a DIFFERENT location is NOT forced to the old AOI
# --------------------------------------------------------------------------- #


def test_followup_different_location_not_forced_to_pin() -> None:
    """A disjoint bbox (a different place) is honored; an explicit widen too."""
    pin = list(_AOI)
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
    pin = list(_AOI)
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
    assert f("compute_layer_bounds", {}, pin) == {}
    # no pin -> no-op
    narrow = {"bbox": [-97.755, 30.26, -97.73, 30.275]}
    assert f("fetch_buildings", narrow, None) == narrow


# --------------------------------------------------------------------------- #
# The bbox-overlap test the four helpers share
# --------------------------------------------------------------------------- #


def test_the_three_bbox_helpers_agree_that_a_touching_edge_overlaps():
    """One shapely test behind three call sites, with one documented semantics."""
    from trid3nt_server.server.dispatch.aoi import _bbox_overlaps
    from trid3nt_server.tools.fetchers.terrain.fetch_dem.hooks import (
        _bbox_intersects as dem_intersects,
    )
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries.hooks import (
        _bbox_intersects as fields_intersects,
    )

    helpers = [_bbox_overlaps, dem_intersects, fields_intersects]
    cases = [
        (((0, 0, 1, 1), (2, 2, 3, 3)), False),   # disjoint
        (((0, 0, 2, 2), (1, 1, 3, 3)), True),    # partial overlap
        (((0, 0, 4, 4), (1, 1, 2, 2)), True),    # contained
        (((0, 0, 1, 1), (1, 0, 2, 1)), True),    # shared vertical edge
        (((0, 0, 1, 1), (0, 1, 1, 2)), True),    # shared horizontal edge
        (((0, 0, 1, 1), (1, 1, 2, 2)), True),    # single shared corner
        (((1, 1, 1, 1), (0, 0, 2, 2)), True),    # zero-area box inside
    ]
    for helper in helpers:
        for (a, b), expected in cases:
            assert helper(a, b) is expected, (helper.__module__, a, b)
