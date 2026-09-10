"""The floored AOI bbox must be the LAST persisted turn zoom-to.

Re-entry into a Case replays the persisted map commands newest-first, so a
composer's floored bbox has to reach the accumulator or re-entry snaps to the
small geocode extent. At the dispatch site, a result carrying a finite four-number
bbox appends a ``zoom-to`` after the geocode snap, deduped against the last one."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.scenario_reuse import reset_scenario_indexes_for_tests
from trid3nt_server.server import (
    SessionState,
    _is_finite_bbox4,
    _last_zoom_to_bbox,
)
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.emission.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

# The SMALL collapsed bbox the geocode early-snap appends (server.py ~2015).
_GEOCODE_SMALL_BBOX = [-82.55, 27.90, -82.54, 27.91]
# The FLOORED (peak, Wave 1) AOI the composer's LayerURI carries.
_FLOORED_BBOX = [-82.70, 27.70, -82.30, 28.10]


class MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw) -> None:  # type: ignore[no-untyped-def]
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)


def _seed_geocode_zoom_to(state: SessionState) -> None:
    """Replay the geocode early-snap: append the SMALL bbox zoom-to.

    The dispatch append is then exercised against a turn that already carries the
    collapsed geocode extent."""
    state.current_turn_map_commands.append(
        {"command": "zoom-to", "args": {"bbox": list(_GEOCODE_SMALL_BBOX)}}
    )


@pytest.fixture()
def _stub_composer():
    """A composer-style tool returning a LayerURI carrying a configurable bbox.

    The bbox the stub stamps is read from ``state`` at dispatch via the params
    so each test can drive a finite / non-finite / wrong-shape extent.
    """
    name = "model_flood_scenario"
    original = agent_tools.TOOL_REGISTRY.get(name)
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(bbox=None, **_kw) -> LayerURI:
        return LayerURI(
            layer_id=f"flood-{new_ulid()}",
            name="Flood depth",
            layer_type="raster",
            uri="s3://x/flood.tif",
            bbox=tuple(bbox) if bbox is not None else None,  # type: ignore[arg-type]
        )

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield name
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[name] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(name, None)
        reset_scenario_indexes_for_tests()
        reset_uri_registries_for_tests()


# --------------------------------------------------------------------------- #
# Keystone: geocode small bbox first, then a composer floored LayerURI -> the
# LAST current_turn_map_commands zoom-to is the FLOORED bbox (not the small one)
# --------------------------------------------------------------------------- #


def test_floored_bbox_is_last_zoom_to_after_geocode_small_snap(
    _stub_composer: str,
) -> None:
    """Re-entry replays newest-first, so the floored AOI must be the LAST zoom-to.

    Dispatching a layer with a finite floored bbox appends its zoom-to after the
    geocode snap; with only the geocode entry, re-entry reverts to the tiny AOI."""
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    # 1) geocode early-snap appended the SMALL collapsed bbox this turn.
    _seed_geocode_zoom_to(state)
    assert _last_zoom_to_bbox(state.current_turn_map_commands) == _GEOCODE_SMALL_BBOX

    # 2) the composer dispatch returns a LayerURI carrying the FLOORED bbox.
    result = asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _stub_composer, {"bbox": list(_FLOORED_BBOX)}
        )
    )
    assert isinstance(result, LayerURI)
    assert list(result.bbox) == _FLOORED_BBOX

    # 3) the FLOORED bbox is now the LAST zoom-to -> re-entry snaps to it.
    assert _last_zoom_to_bbox(state.current_turn_map_commands) == _FLOORED_BBOX
    # Both the geocode small snap AND the floored append are present, in order.
    zoom_bboxes = [
        c["args"]["bbox"]
        for c in state.current_turn_map_commands
        if c.get("command") == "zoom-to"
    ]
    assert zoom_bboxes == [_GEOCODE_SMALL_BBOX, _FLOORED_BBOX]


def test_floored_append_with_no_prior_geocode_snap(_stub_composer: str) -> None:
    """A composer-only turn (no geocode) still records the floored zoom-to.

    A direct composer run that never geocoded must still persist its AOI so a
    re-entry has something to snap to.
    """
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    result = asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _stub_composer, {"bbox": list(_FLOORED_BBOX)}
        )
    )
    assert isinstance(result, LayerURI)
    assert _last_zoom_to_bbox(state.current_turn_map_commands) == _FLOORED_BBOX


# --------------------------------------------------------------------------- #
# Guards: dedupe + non-finite / wrong-shape / absent bbox
# --------------------------------------------------------------------------- #


def test_no_double_append_when_floored_equals_last_zoom_to(
    _stub_composer: str,
) -> None:
    """Dedupe: a LayerURI bbox equal to the last accumulated zoom-to is skipped.

    Guards against a double-append when the floored extent already matches the
    most-recent zoom-to (e.g. a re-dispatch in the same turn).
    """
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    # Pre-seed a zoom-to that ALREADY equals the floored bbox.
    state.current_turn_map_commands.append(
        {"command": "zoom-to", "args": {"bbox": list(_FLOORED_BBOX)}}
    )
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _stub_composer, {"bbox": list(_FLOORED_BBOX)}
        )
    )
    zoom_count = sum(
        1
        for c in state.current_turn_map_commands
        if c.get("command") == "zoom-to"
    )
    assert zoom_count == 1, "floored bbox equal to the last zoom-to was double-appended"


def test_non_finite_layeruri_bbox_not_appended(_stub_composer: str) -> None:
    """A LayerURI with a non-finite bbox component appends no zoom-to.

    LayerURI permits any float, so a NaN/inf must NOT become a bad zoom-to.
    """
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _seed_geocode_zoom_to(state)

    bad = [-82.7, float("nan"), -82.3, 28.1]
    asyncio.run(
        server._invoke_tool_via_emitter(
            ws, state, _stub_composer, {"bbox": bad}
        )
    )
    # Only the geocode snap remains; the NaN bbox was rejected by the guard.
    assert _last_zoom_to_bbox(state.current_turn_map_commands) == _GEOCODE_SMALL_BBOX


def test_absent_layeruri_bbox_not_appended(_stub_composer: str) -> None:
    """A LayerURI with bbox=None appends no zoom-to (the bbox is optional)."""
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    _seed_geocode_zoom_to(state)

    # No bbox param -> the stub returns LayerURI(bbox=None).
    result = asyncio.run(
        server._invoke_tool_via_emitter(ws, state, _stub_composer, {})
    )
    assert isinstance(result, LayerURI)
    assert result.bbox is None
    assert _last_zoom_to_bbox(state.current_turn_map_commands) == _GEOCODE_SMALL_BBOX


# --------------------------------------------------------------------------- #
# Pure-helper unit coverage
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bbox,expected",
    [
        ([-82.7, 27.7, -82.3, 28.1], True),
        ((-82.7, 27.7, -82.3, 28.1), True),
        ([0, 0, 1, 1], True),
        (None, False),
        ([-82.7, 27.7, -82.3], False),  # too short
        ([-82.7, 27.7, -82.3, 28.1, 0.0], False),  # too long
        ([-82.7, float("nan"), -82.3, 28.1], False),
        ([-82.7, float("inf"), -82.3, 28.1], False),
        ([-82.7, "27.7", -82.3, 28.1], False),  # non-numeric
        ([True, False, 1, 2], False),  # bools are not coords
    ],
)
def test_is_finite_bbox4(bbox, expected) -> None:
    assert _is_finite_bbox4(bbox) is expected


def test_last_zoom_to_bbox_walks_newest_first() -> None:
    cmds = [
        {"command": "zoom-to", "args": {"bbox": [0, 0, 1, 1]}},
        {"command": "set-style", "args": {}},
        {"command": "zoom-to", "args": {"bbox": [10, 10, 11, 11]}},
    ]
    assert _last_zoom_to_bbox(cmds) == [10, 10, 11, 11]


def test_last_zoom_to_bbox_none_when_no_zoom_to() -> None:
    assert _last_zoom_to_bbox([]) is None
    assert _last_zoom_to_bbox([{"command": "set-style", "args": {}}]) is None


def test_last_zoom_to_bbox_none_on_malformed_args() -> None:
    """A zoom-to with missing/garbled args yields None (no crash, no stale)."""
    assert _last_zoom_to_bbox([{"command": "zoom-to"}]) is None
    assert _last_zoom_to_bbox([{"command": "zoom-to", "args": {}}]) is None
