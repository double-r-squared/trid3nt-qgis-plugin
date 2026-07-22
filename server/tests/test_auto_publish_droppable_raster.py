"""Deterministic layer auto-publish (NATE 2026-06-26).

NATE's directive: "we should not have the LLM enforce publishing of layers --
this should just be done without LLM intervention." A tool that returns a
renderable RASTER ``LayerURI`` carrying a raw object-store uri (``s3://`` /
``gs://``) is exactly the class ``layer_uri_emit.emit_layer_uri`` DROPS, so it
historically only rendered if the LLM SEPARATELY called ``publish_layer``. The
server dispatch wrapper (``_invoke_tool_via_emitter``) now AUTO-CALLS
``publish_layer`` server-side for any such droppable raster and feeds the
resulting http(s) tile URL through the same ``add_loaded_layer`` machinery -- no
LLM ``publish_layer`` call required.

These tests mock ``publish_layer`` (so no real PyQGIS / TiTiler dispatch) and
drive ``_invoke_tool_via_emitter`` directly via a stubbed raster tool, asserting:

  * a raster ``s3://`` LayerURI triggers ONE auto publish_layer + reaches
    ``loaded_layers`` as the http(s) URL, with NO LLM publish_layer call;
  * an ``auto_publish=False`` intermediate (e.g. fetch_dem) does NOT auto-publish;
  * an http(s) raster LayerURI is untouched (already renderable);
  * a vector LayerURI is untouched (inline-GeoJSON path);
  * a double-publish (the LLM ALSO calls publish_layer for the same COG) does NOT
    duplicate the layer (COG-identity dedup merges the rows);
  * a publish FAILURE surfaces a typed honesty-floor error envelope (never a
    silent green) and does NOT add a layer.
"""

from __future__ import annotations

import json

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.tools import RegisteredTool
from grace2_contracts.common import new_ulid
from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


TILE_URL = (
    "https://tiles.example.cloudfront.net/cog/tiles/{z}/{x}/{y}.png"
    "?url=s3%3A%2F%2Fbucket%2Fcache%2Fhillshade.tif"
)
S3_COG = "s3://bucket/cache/hillshade.tif"


# --------------------------------------------------------------------------- #
# Fixtures: a recording publish_layer stub + a raster-returning tool.
# --------------------------------------------------------------------------- #


class _PublishRecorder:
    """Records every publish_layer invocation; returns a configurable value."""

    def __init__(self, return_value: object = TILE_URL, raises: bool = False):
        self.calls: list[dict] = []
        self.return_value = return_value
        self.raises = raises

    def __call__(self, layer_uri: str, layer_id: str, **kw) -> object:
        self.calls.append({"layer_uri": layer_uri, "layer_id": layer_id, **kw})
        if self.raises:
            raise RuntimeError("PyQGIS worker boom")
        return self.return_value


def _install_tool(name: str, fn, *, auto_publish: bool = True) -> RegisteredTool | None:
    """Register a non-cacheable tool (returns the prior entry for teardown)."""
    original = agent_tools.TOOL_REGISTRY.get(name)
    meta = AtomicToolMetadata(
        name=name,
        ttl_class="live-no-cache",
        cacheable=False,
        auto_publish=auto_publish,
    )
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=fn, module=__name__
    )
    return original


def _restore_tool(name: str, original: RegisteredTool | None) -> None:
    if original is not None:
        agent_tools.TOOL_REGISTRY[name] = original
    else:
        agent_tools.TOOL_REGISTRY.pop(name, None)


@pytest.fixture
def publish_recorder():
    """Replace publish_layer with a recorder; restore on teardown."""
    rec = _PublishRecorder()
    original = agent_tools.TOOL_REGISTRY.get("publish_layer")
    agent_tools.TOOL_REGISTRY["publish_layer"] = RegisteredTool(
        metadata=AtomicToolMetadata(
            name="publish_layer", ttl_class="live-no-cache", cacheable=False
        ),
        fn=rec,
        module=__name__,
    )
    try:
        yield rec
    finally:
        _restore_tool("publish_layer", original)


def _session_states(ws: FakeWS) -> list[dict]:
    return [
        e
        for e in (json.loads(s) for s in ws.sent)
        if e.get("type") == "session-state"
    ]


def _error_envelopes(ws: FakeWS) -> list[dict]:
    return [
        e for e in (json.loads(s) for s in ws.sent) if e.get("type") == "error"
    ]


# --------------------------------------------------------------------------- #
# 1. A raster s3:// LayerURI auto-publishes (no LLM publish_layer call).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_raster_s3_layer_uri_auto_publishes(publish_recorder) -> None:
    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="hillshade-x",
            name="Hillshade",
            layer_type="raster",
            uri=S3_COG,
            style_preset="",
        )

    original = _install_tool("compute_hillshade_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())

        result = await server._invoke_tool_via_emitter(
            ws, state, "compute_hillshade_stub", {}
        )

        # The tool's OWN return is unchanged (still the raw s3:// LayerURI) -- the
        # LLM-visible result is not rewritten.
        assert isinstance(result, LayerURI)
        assert result.uri == S3_COG

        # publish_layer was auto-called EXACTLY once, server-side, with the COG.
        assert len(publish_recorder.calls) == 1
        assert publish_recorder.calls[0]["layer_uri"] == S3_COG

        # The RENDERABLE http(s) tile URL reached loaded_layers (NOT the s3://).
        loaded = state.emitter.loaded_layers
        uris = [layer.uri for layer in loaded]
        assert TILE_URL in uris, f"auto-published tile URL not loaded: {uris}"
        assert S3_COG not in uris, "raw s3:// must never reach loaded_layers"

        # A session-state envelope announced it.
        assert _session_states(ws), "no session-state emitted after auto-publish"
    finally:
        _restore_tool("compute_hillshade_stub", original)


@pytest.mark.asyncio
async def test_auto_publish_passes_no_llm_publish_call(publish_recorder) -> None:
    """The auto-publish is SERVER-driven: the LLM never issued publish_layer.

    We prove this by asserting the ONLY publish_layer invocation came from the
    auto-publish branch (single call) while the tool the LLM 'called' was the
    raster producer, not publish_layer.
    """

    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="slope-y",
            name="Slope",
            layer_type="raster",
            uri="gs://bucket/cache/slope.tif",
            style_preset="",
        )

    original = _install_tool("compute_slope_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._invoke_tool_via_emitter(ws, state, "compute_slope_stub", {})
        assert len(publish_recorder.calls) == 1  # exactly the auto-publish
        # gs:// is also a droppable object-store uri -> auto-published.
        assert publish_recorder.calls[0]["layer_uri"] == "gs://bucket/cache/slope.tif"
        assert any(layer.uri == TILE_URL for layer in state.emitter.loaded_layers)
    finally:
        _restore_tool("compute_slope_stub", original)


# --------------------------------------------------------------------------- #
# 2. auto_publish=False intermediate does NOT auto-publish.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_publish_false_intermediate_does_not_publish(
    publish_recorder,
) -> None:
    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="raw-dem",
            name="DEM",
            layer_type="raster",
            uri="s3://bucket/cache/dem.tif",
            style_preset="",
        )

    # Mirror fetch_dem's opt-out.
    original = _install_tool("fetch_dem_stub", _tool, auto_publish=False)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._invoke_tool_via_emitter(ws, state, "fetch_dem_stub", {})
        assert publish_recorder.calls == [], "intermediate must NOT auto-publish"
        # The raw DEM is correctly DROPPED by the emit seam -> nothing loaded.
        assert state.emitter.loaded_layers == []
    finally:
        _restore_tool("fetch_dem_stub", original)


def test_fetch_dem_metadata_opts_out_of_auto_publish() -> None:
    """The real fetch_dem (+ fetch_topobathy / fetch_3dep_extra) opt OUT."""
    import grace2_agent.server  # noqa: F401 - ensures tool registration

    for name in ("fetch_dem", "fetch_topobathy", "fetch_3dep_extra"):
        entry = agent_tools.TOOL_REGISTRY.get(name)
        assert entry is not None, f"{name} not registered"
        assert entry.metadata.auto_publish is False, f"{name} should opt out"


def test_terminal_raster_products_default_auto_publish_true() -> None:
    import grace2_agent.server  # noqa: F401

    for name in (
        "compute_hillshade",
        "compute_slope",
        "compute_aspect",
        "compute_colored_relief",
        "clip_raster_to_bbox",
    ):
        entry = agent_tools.TOOL_REGISTRY.get(name)
        if entry is None:
            continue
        assert entry.metadata.auto_publish is True, f"{name} should default True"


# --------------------------------------------------------------------------- #
# 3. An http(s) raster LayerURI is untouched (already renderable).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_http_raster_layer_uri_not_auto_published(publish_recorder) -> None:
    http_uri = "https://tiles.example.com/wms?LAYERS=relief"

    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="relief-http",
            name="Relief",
            layer_type="raster",
            uri=http_uri,
            style_preset="",
        )

    original = _install_tool("compute_relief_http_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._invoke_tool_via_emitter(
            ws, state, "compute_relief_http_stub", {}
        )
        # Already-renderable http(s) raster: no auto-publish, and the emit seam
        # passes it straight to loaded_layers via the emit_tool_call gate.
        assert publish_recorder.calls == []
        assert any(layer.uri == http_uri for layer in state.emitter.loaded_layers)
    finally:
        _restore_tool("compute_relief_http_stub", original)


# --------------------------------------------------------------------------- #
# 4. A vector LayerURI is untouched (inline-GeoJSON path).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_vector_layer_uri_not_auto_published(publish_recorder) -> None:
    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="rivers",
            name="Rivers",
            layer_type="vector",
            uri="s3://bucket/cache/rivers.fgb",
            style_preset="",
        )

    original = _install_tool("fetch_rivers_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._invoke_tool_via_emitter(ws, state, "fetch_rivers_stub", {})
        # Vectors flow through the inline-GeoJSON path; auto-publish must NOT fire
        # (publish_layer is a RASTER seam).
        assert publish_recorder.calls == [], "vector must never auto-publish"
    finally:
        _restore_tool("fetch_rivers_stub", original)


# --------------------------------------------------------------------------- #
# 5. Double-publish (LLM ALSO calls publish_layer) does not duplicate.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_double_publish_does_not_duplicate(publish_recorder) -> None:
    """Auto-publish + a redundant LLM publish_layer of the same COG MERGE."""

    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="canopy-1",
            name="Canopy",
            layer_type="raster",
            uri=S3_COG,
            style_preset="",
        )

    original = _install_tool("compute_canopy_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())

        # 1) The producing tool auto-publishes.
        await server._invoke_tool_via_emitter(ws, state, "compute_canopy_stub", {})
        # 2) The LLM ALSO calls publish_layer for the SAME COG (returns same URL).
        await server._invoke_tool_via_emitter(
            ws,
            state,
            "publish_layer",
            {"layer_uri": S3_COG, "layer_id": "canopy-1"},
        )

        # add_loaded_layer dedups by underlying-COG identity (the TILE_URL's
        # ?url=<cog> query) -> exactly ONE row carrying the tile URL, not two.
        tile_rows = [
            layer
            for layer in state.emitter.loaded_layers
            if layer.uri == TILE_URL
        ]
        assert len(tile_rows) == 1, (
            f"double-publish duplicated the layer: {state.emitter.loaded_layers}"
        )
    finally:
        _restore_tool("compute_canopy_stub", original)


# --------------------------------------------------------------------------- #
# 6. Honesty floor: a publish FAILURE surfaces a typed error, adds no layer.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_publish_failure_surfaces_honesty_floor_error() -> None:
    rec = _PublishRecorder(raises=True)
    original_pub = agent_tools.TOOL_REGISTRY.get("publish_layer")
    agent_tools.TOOL_REGISTRY["publish_layer"] = RegisteredTool(
        metadata=AtomicToolMetadata(
            name="publish_layer", ttl_class="live-no-cache", cacheable=False
        ),
        fn=rec,
        module=__name__,
    )

    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="ndvi-fail",
            name="NDVI",
            layer_type="raster",
            uri=S3_COG,
            style_preset="",
        )

    original = _install_tool("compute_ndvi_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        result = await server._invoke_tool_via_emitter(
            ws, state, "compute_ndvi_stub", {}
        )

        # The tool result is unchanged (the LLM still sees the produced LayerURI).
        assert isinstance(result, LayerURI)
        # No renderable layer was silently added.
        assert all(
            layer.uri != S3_COG and layer.uri != TILE_URL
            for layer in state.emitter.loaded_layers
        )
        # A typed honesty-floor error envelope went over the wire: an
        # INTERNAL_ERROR (the closed A.6 wire code) carrying the typed
        # LAYER_AUTO_PUBLISH_FAILED marker in its message.
        errors = _error_envelopes(ws)
        assert any(
            e.get("payload", {}).get("error_code") == "INTERNAL_ERROR"
            and "LAYER_AUTO_PUBLISH_FAILED" in e.get("payload", {}).get("message", "")
            for e in errors
        ), f"no honesty-floor error envelope on publish failure: {errors}"
    finally:
        _restore_tool("compute_ndvi_stub", original)
        _restore_tool("publish_layer", original_pub)


@pytest.mark.asyncio
async def test_publish_non_http_return_surfaces_honesty_floor_error() -> None:
    """publish_layer returning a NON-http value (e.g. raw s3://) is a failure."""
    rec = _PublishRecorder(return_value=S3_COG)  # echoes the raw uri, not http
    original_pub = agent_tools.TOOL_REGISTRY.get("publish_layer")
    agent_tools.TOOL_REGISTRY["publish_layer"] = RegisteredTool(
        metadata=AtomicToolMetadata(
            name="publish_layer", ttl_class="live-no-cache", cacheable=False
        ),
        fn=rec,
        module=__name__,
    )

    def _tool(**_kw) -> LayerURI:
        return LayerURI(
            layer_id="blend-1",
            name="Blended",
            layer_type="raster",
            uri=S3_COG,
            style_preset="",
        )

    original = _install_tool("compute_blended_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._invoke_tool_via_emitter(ws, state, "compute_blended_stub", {})
        # The raw s3:// echo is NOT renderable -> not added, error surfaced.
        assert all(
            layer.uri != S3_COG for layer in state.emitter.loaded_layers
        )
        errors = _error_envelopes(ws)
        assert any(
            e.get("payload", {}).get("error_code") == "INTERNAL_ERROR"
            and "LAYER_AUTO_PUBLISH_FAILED" in e.get("payload", {}).get("message", "")
            for e in errors
        ), f"non-http publish return must surface an error: {errors}"
    finally:
        _restore_tool("compute_blended_stub", original)
        _restore_tool("publish_layer", original_pub)
