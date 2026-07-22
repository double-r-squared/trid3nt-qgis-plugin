"""job-0272: atomic publish_layer must announce its layer to the map.

Live failure (third terrain incident, 2026-06-10): the LLM-driven
fetch→compute→publish chain published the layer server-side
(CONDITION_SUCCEEDED, WMS renders when curled) but the map stayed empty —
``emit_tool_call`` only feeds ``add_loaded_layer`` / the ``session-state``
envelope when a tool RETURNS a typed ``LayerURI``, and the atomic
``publish_layer`` returns a bare WMS URL string. Composer layers (floods,
plumes) always rendered because composers return LayerURIs.

The fix wraps the published uri in a LayerURI at the ``_invoke_tool_via_emitter``
publish_layer tracking site, so the existing emission machinery announces it.

TiTiler exit / QGIS-native swap (2026-07): ``publish_layer``'s raster SUCCESS
shape is now the raw ``s3://`` COG uri (the plugin reads it via /vsicurl/), so
the wrap-site must announce s3 returns exactly as it announced http(s) ones.
The stub below returns the s3 shape, pinning the NEW contract positively; one
test keeps the http(s) face (vector WMS / durable GeoJSON) covered.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


S3_COG = "s3://bucket/cache/colored_relief_boulder.tif"


def _install_publish_stub(return_value: str):
    """Shadow the real publish_layer with a stub returning ``return_value``."""
    name = "publish_layer"
    original = agent_tools.TOOL_REGISTRY.get(name)

    def _fn(layer_uri: str, layer_id: str, **_kw) -> str:
        return return_value

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    return original


def _restore_publish_stub(original) -> None:
    if original is not None:
        agent_tools.TOOL_REGISTRY["publish_layer"] = original
    else:
        agent_tools.TOOL_REGISTRY.pop("publish_layer", None)


@pytest.fixture(autouse=True)
def _fake_publish_layer_tool():
    """Default stub: the raw s3:// COG return (the raster publish shape)."""
    original = _install_publish_stub(S3_COG)
    try:
        yield
    finally:
        _restore_publish_stub(original)


@pytest.mark.asyncio
async def test_atomic_publish_announces_s3_layer_via_session_state() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())

    result = await server._invoke_tool_via_emitter(
        ws,
        state,
        "publish_layer",
        {"layer_uri": S3_COG, "layer_id": "colored-relief-boulder"},
    )
    assert result == S3_COG

    # The emitter accumulated the layer...
    loaded = state.emitter.loaded_layers
    assert any(l.layer_id == "colored-relief-boulder" for l in loaded), (
        f"publish_layer did not reach the emitter's loaded layers: {loaded}"
    )
    matching = next(l for l in loaded if l.layer_id == "colored-relief-boulder")
    # The raw s3:// COG IS the renderable envelope uri (plugin /vsicurl/).
    assert matching.uri == S3_COG

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
async def test_atomic_publish_http_face_still_announces() -> None:
    """The http(s) publish face (vector WMS / durable GeoJSON) still emits --
    widening the wrap-site gate to s3:// must not regress http returns."""
    wms = (
        "https://qgis.example.run.app/ogc/wms"
        "?MAP=/mnt/qgs/grace2-sample.qgs&LAYERS=colored-relief-boulder"
    )
    original = _install_publish_stub(wms)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        result = await server._invoke_tool_via_emitter(
            ws,
            state,
            "publish_layer",
            {"layer_uri": S3_COG, "layer_id": "colored-relief-boulder"},
        )
        assert result == wms
        matching = next(
            l
            for l in state.emitter.loaded_layers
            if l.layer_id == "colored-relief-boulder"
        )
        assert matching.uri == wms
    finally:
        _restore_publish_stub(original)


@pytest.mark.asyncio
async def test_turn_layer_accumulator_still_tracks_layer_id() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._invoke_tool_via_emitter(
        ws,
        state,
        "publish_layer",
        {"layer_uri": "s3://bucket/cache/x.tif", "layer_id": "relief-2"},
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
            "layer_uri": "s3://bucket/cache/hillshade/x.tif",
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
            "layer_uri": "s3://bucket/cache/x.tif",
            "layer_id": "colored-relief-boulder",
            "name": "Boulder Colored Relief (2026 flyover)",
        },
    )

    matching = next(
        l for l in state.emitter.loaded_layers if l.layer_id == "colored-relief-boulder"
    )
    assert matching.name == "Boulder Colored Relief (2026 flyover)"
