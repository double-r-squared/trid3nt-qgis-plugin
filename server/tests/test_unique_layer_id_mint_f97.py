"""F97: two loaded layers from the SAME source get DISTINCT layer_ids.

ROOT CAUSE of F97 (deleting one of two duplicate WDPA layers removed BOTH):
the WDPA fetcher (and other fetchers) mint a SOURCE-DERIVED ``layer_id``
(e.g. ``wdpa-<lon>-<lat>``), so two fetches for the same bbox returned the
SAME id. Map.tsx keys MapLibre sources by ``layer_id`` — two layers sharing
an id collide onto ONE source, and a delete-by-id tears that source down so
BOTH vanish.

FIX (this track): ``_invoke_tool_via_emitter`` mints a fresh ULID for every
FRESHLY-fetched layer at the dispatch seam, BEFORE ``add_loaded_layer`` /
the URI registry / the reuse index see it. The reuse short-circuit
(``_ReuseEntry``) is the deliberate exception — it hands back an already-loaded
layer, so it keeps that layer's existing (already-minted) id for per-Case
durability.

These tests drive the REAL dispatch with a stub fetcher returning a
collision-prone source-derived ``layer_id`` and assert the dispatch hands back
DISTINCT, ULID-shaped ids.
"""

from __future__ import annotations

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.scenario_reuse import reset_scenario_indexes_for_tests
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


_STUB_TOOL = "fetch_collision_prone_layer"
# Both fetches return the SAME source-derived id (the F97 cause) even though
# they point at genuinely different data (distinct uris) — exactly the WDPA
# situation where ``wdpa-<lon>-<lat>`` collides across two distinct layers.
_SOURCE_DERIVED_ID = "wdpa-26.6400--81.8700"
# Per-fetch counter so each call returns a DISTINCT uri (two real layers) while
# keeping the colliding source-derived layer_id.
_FETCH_N: list[int] = []


@pytest.fixture(autouse=True)
def _stub_collision_tool():
    """Register a fetcher that ALWAYS returns the same source-derived layer_id.

    The name is not a known scenario / solver tool, so neither the reuse
    short-circuit nor the confirm gate fires — we exercise the bare fresh-fetch
    mint path.
    """
    original = agent_tools.TOOL_REGISTRY.get(_STUB_TOOL)
    _FETCH_N.clear()
    reset_scenario_indexes_for_tests()
    reset_uri_registries_for_tests()

    def _fn(**_kw) -> LayerURI:
        _FETCH_N.append(1)
        return LayerURI(
            layer_id=_SOURCE_DERIVED_ID,  # collision-prone, source-derived
            name="Protected Areas — WDPA",
            layer_type="raster",
            # Distinct uri per fetch -> two genuinely different layers that the
            # tool would otherwise have collapsed onto one colliding layer_id.
            uri=f"https://qgis.example.run.app/ogc/wms?LAYERS=wdpa&n={len(_FETCH_N)}",
            style_preset="wdpa_protected_areas",
            role="context",
        )

    meta = AtomicToolMetadata(
        name=_STUB_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_STUB_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[_STUB_TOOL] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(_STUB_TOOL, None)
        reset_scenario_indexes_for_tests()
        reset_uri_registries_for_tests()


def _is_ulid(value: str) -> bool:
    # new_ulid() returns a 26-char Crockford-base32 string.
    return isinstance(value, str) and len(value) == 26


@pytest.mark.asyncio
async def test_two_fetches_same_source_get_distinct_layer_ids() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    first = await server._invoke_tool_via_emitter(ws, state, _STUB_TOOL, {})
    second = await server._invoke_tool_via_emitter(ws, state, _STUB_TOOL, {})

    assert isinstance(first, LayerURI)
    assert isinstance(second, LayerURI)

    # The minted ids are DISTINCT (the core F97 guarantee) ...
    assert first.layer_id != second.layer_id, (
        "two layers from the same source collided on one layer_id (F97)"
    )
    # ... and NEITHER is the collision-prone source-derived id ...
    assert first.layer_id != _SOURCE_DERIVED_ID
    assert second.layer_id != _SOURCE_DERIVED_ID
    # ... they are freshly minted ULIDs.
    assert _is_ulid(first.layer_id)
    assert _is_ulid(second.layer_id)


@pytest.mark.asyncio
async def test_both_layers_coexist_in_loaded_layers_with_distinct_ids() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    first = await server._invoke_tool_via_emitter(ws, state, _STUB_TOOL, {})
    second = await server._invoke_tool_via_emitter(ws, state, _STUB_TOOL, {})

    loaded_ids = [l.layer_id for l in state.emitter.loaded_layers]
    # Both distinct minted ids are present — two separate map layers, not one
    # collapsed entry. The client keys sources by layer_id, so distinct ids are
    # what keeps the two layers independently deletable (the F97 fix).
    assert first.layer_id in loaded_ids
    assert second.layer_id in loaded_ids
    assert len(set(loaded_ids)) == len(loaded_ids), "duplicate layer_id persisted"


@pytest.mark.asyncio
async def test_minted_id_is_what_session_state_carries() -> None:
    """The emitted session-state must carry the MINTED id (so the client renders
    + later deletes against the unique id), not the source-derived one."""
    import json

    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    first = await server._invoke_tool_via_emitter(ws, state, _STUB_TOOL, {})

    session_states = [
        e
        for e in (json.loads(s) for s in ws.sent)
        if e.get("type") == "session-state"
    ]
    assert session_states
    emitted_ids = [
        l.get("layer_id")
        for l in session_states[-1].get("payload", {}).get("loaded_layers", [])
    ]
    assert first.layer_id in emitted_ids
    assert _SOURCE_DERIVED_ID not in emitted_ids
