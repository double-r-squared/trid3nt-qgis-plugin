"""job-0272: atomic publish_layer must announce its layer to the map.

Live failure (third terrain incident, 2026-06-10): the LLM-driven
fetch→compute→publish chain published the layer server-side
(CONDITION_SUCCEEDED, WMS renders when curled) but the map stayed empty —
``emit_tool_call`` only feeds ``add_loaded_layer`` / the ``session-state``
envelope when a tool RETURNS a typed ``LayerURI``, and the atomic
``publish_layer`` returns a bare WMS URL string. Composer layers (floods,
plumes) always rendered because composers return LayerURIs.

The fix wraps the WMS string in a LayerURI at the ``_invoke_tool_via_emitter``
publish_layer tracking site, so the existing emission machinery announces it.
"""

from __future__ import annotations

import json

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.tools import RegisteredTool
from grace2_contracts.common import new_ulid
from grace2_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


WMS = (
    "https://qgis.example.run.app/ogc/wms"
    "?MAP=/mnt/qgs/grace2-sample.qgs&LAYERS=colored-relief-boulder"
)


@pytest.fixture(autouse=True)
def _fake_publish_layer_tool():
    """Shadow the real publish_layer with a stub returning a WMS string."""
    name = "publish_layer"
    original = agent_tools.TOOL_REGISTRY.get(name)

    def _fn(layer_uri: str, layer_id: str, **_kw) -> str:
        return WMS

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
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


@pytest.mark.asyncio
async def test_atomic_publish_announces_layer_via_session_state() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    result = await server._invoke_tool_via_emitter(
        ws,
        state,
        "publish_layer",
        {"layer_uri": "gs://bucket/cache/x.tif", "layer_id": "colored-relief-boulder"},
    )
    assert result == WMS

    # The emitter accumulated the layer...
    loaded = state.emitter.loaded_layers
    assert any(l.layer_id == "colored-relief-boulder" for l in loaded), (
        f"publish_layer did not reach the emitter's loaded layers: {loaded}"
    )
    matching = next(l for l in loaded if l.layer_id == "colored-relief-boulder")
    assert matching.uri == WMS  # the RENDERABLE WMS URL, not the gs:// input

    # ...and a session-state envelope carrying it went over the wire.
    session_states = [
        e
        for e in (json.loads(s) for s in ws.sent)
        if e.get("type") == "session-state"
    ]
    assert session_states, "no session-state envelope emitted after publish"
    last = session_states[-1]
    layer_ids = [
        l.get("layer_id")
        for l in last.get("payload", {}).get("loaded_layers", [])
    ]
    assert "colored-relief-boulder" in layer_ids, (
        f"session-state does not announce the published layer: {layer_ids}"
    )


@pytest.mark.asyncio
async def test_turn_layer_accumulator_still_tracks_layer_id() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._invoke_tool_via_emitter(
        ws,
        state,
        "publish_layer",
        {"layer_uri": "gs://bucket/cache/x.tif", "layer_id": "relief-2"},
    )
    assert "relief-2" in state.current_turn_layer_ids


# --------------------------------------------------------------------------- #
# OPEN-9 (2026-07-10): the LayerURI.name the wrap-site constructs must be a
# READABLE name, not the bare layer_id, when the model omits a usable one —
# live bug: a bare-ULID layer_id (derive_layer_id's last resort) surfaced
# directly in the UI's layer list as e.g. "01KX5TEZ20BK86EE6DG8PSVFJK".
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bare_ulid_layer_id_gets_a_readable_name_from_style_preset() -> None:
    """A local model that omits ``name`` AND lands on a bare-ULID layer_id
    (via ``derive_layer_id``'s last resort) still gets a HUMAN name in the
    layer summary, derived from ``style_preset``."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    bare_ulid = new_ulid()

    await server._invoke_tool_via_emitter(
        ws,
        state,
        "publish_layer",
        {
            "layer_uri": "gs://bucket/cache/hillshade/x.tif",
            "layer_id": bare_ulid,
            "style_preset": "standard_hillshade",
        },
    )

    matching = next(l for l in state.emitter.loaded_layers if l.layer_id == bare_ulid)
    assert matching.name != bare_ulid, "the bare ULID must never surface as the name"
    assert matching.name.startswith("Hillshade")


@pytest.mark.asyncio
async def test_explicit_name_param_passes_through_untouched() -> None:
    """A model (or composer) that DOES supply an explicit ``name`` keeps it
    verbatim — the derivation only kicks in when nothing usable was given."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    await server._invoke_tool_via_emitter(
        ws,
        state,
        "publish_layer",
        {
            "layer_uri": "gs://bucket/cache/x.tif",
            "layer_id": "colored-relief-boulder",
            "name": "Boulder Colored Relief (2026 flyover)",
        },
    )

    matching = next(
        l for l in state.emitter.loaded_layers if l.layer_id == "colored-relief-boulder"
    )
    assert matching.name == "Boulder Colored Relief (2026 flyover)"
