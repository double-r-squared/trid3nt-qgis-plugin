"""Deterministic layer auto-publish (NATE 2026-06-26).

NATE's directive: "we should not have the LLM enforce publishing of layers --
this should just be done without LLM intervention." A tool that returns a
renderable RASTER ``LayerURI`` carrying a raw object-store uri triggers a
server-side ``publish_layer`` call from the dispatch wrapper
(``_invoke_tool_via_emitter``) -- no LLM ``publish_layer`` call required.

TiTiler exit / QGIS-native swap (2026-07): ``publish_layer``'s raster SUCCESS
shape is now the raw ``s3://`` COG uri itself (the plugin reads it via
/vsicurl/), and the ``emit_layer_uri`` seam PASSES raster ``s3://`` (only
``gs://`` / ``file://`` / empty still drop). The auto-publish remains the
deterministic enrichment pass (overviews, style resolution, legend stash, URI
registry) and its honesty floor still fires on genuinely bad publish returns.

These tests mock ``publish_layer`` (so no real PyQGIS / TiTiler dispatch) and
drive ``_invoke_tool_via_emitter`` directly via a stubbed raster tool, asserting:

  * a raster ``s3://`` LayerURI triggers ONE auto publish_layer + reaches
    ``loaded_layers`` with the published s3 COG uri, with NO LLM publish_layer
    call (and no duplicate row alongside the direct seam emission);
  * an ``auto_publish=False`` intermediate (e.g. fetch_dem) does NOT
    auto-publish (its raw s3 layer still emits through the seam, unenriched);
  * an http(s) raster LayerURI is untouched (already renderable);
  * a vector LayerURI is untouched (inline-GeoJSON path);
  * a double-publish (the LLM ALSO calls publish_layer for the same COG) does NOT
    duplicate the layer (COG-identity dedup merges the rows);
  * a publish FAILURE (raise, or a non-renderable return such as an empty/error
    string) surfaces a typed honesty-floor error envelope (never a silent
    green).
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


S3_COG = "s3://bucket/cache/hillshade.tif"


# --------------------------------------------------------------------------- #
# Fixtures: a recording publish_layer stub + a raster-returning tool.
# --------------------------------------------------------------------------- #


class _PublishRecorder:
    """Records every publish_layer invocation; returns a configurable value.

    Default return: the raw s3:// COG uri -- publish_layer's raster SUCCESS
    shape since the TiTiler exit (the plugin renders it via /vsicurl/).
    """

    def __init__(self, return_value: object = S3_COG, raises: bool = False):
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

        # NEW CONTRACT: the published s3:// COG uri IS the renderable envelope
        # (the plugin reads it via /vsicurl/). The direct seam emission and the
        # auto-publish row carry the same COG uri, so dedup collapses them to
        # EXACTLY ONE row.
        loaded = state.emitter.loaded_layers
        uris = [layer.uri for layer in loaded]
        assert uris.count(S3_COG) == 1, (
            f"expected exactly one merged s3 COG row: {uris}"
        )

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
        # gs:// is still a droppable object-store uri (the seam refuses it) ->
        # auto-published; the publish's s3:// return is what renders.
        assert publish_recorder.calls[0]["layer_uri"] == "gs://bucket/cache/slope.tif"
        assert any(layer.uri == S3_COG for layer in state.emitter.loaded_layers)
        # The raw gs:// never reaches the map (still un-renderable).
        assert all(
            not layer.uri.startswith("gs://")
            for layer in state.emitter.loaded_layers
        )
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
        # NEW CONTRACT: the raw s3 DEM is renderable (plugin /vsicurl/), so the
        # emit seam PASSES it -- it reaches the map through the direct emission
        # gate, just WITHOUT the auto-publish enrichment (no publish call).
        assert [layer.uri for layer in state.emitter.loaded_layers] == [
            "s3://bucket/cache/dem.tif"
        ]
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
        # 2) The LLM ALSO calls publish_layer for the SAME COG (returns the same
        #    raw s3:// COG uri -- the new publish shape).
        await server._invoke_tool_via_emitter(
            ws,
            state,
            "publish_layer",
            {"layer_uri": S3_COG, "layer_id": "canopy-1"},
        )

        # add_loaded_layer dedups by underlying-COG identity (for a plain s3
        # COG that is the uri itself) -> exactly ONE row, not two/three.
        cog_rows = [
            layer
            for layer in state.emitter.loaded_layers
            if layer.uri == S3_COG
        ]
        assert len(cog_rows) == 1, (
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
        # The direct seam emission still carries the raw (renderable) s3 row --
        # ONLY the enrichment publish failed; nothing beyond that row appears.
        assert [layer.uri for layer in state.emitter.loaded_layers] == [S3_COG]
        # A typed honesty-floor error envelope went over the wire: an
        # INTERNAL_ERROR (the closed A.6 wire code) carrying the typed
        # LAYER_AUTO_PUBLISH_FAILED marker in its message. A failed publish is
        # NEVER a silent green.
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
async def test_publish_s3_return_is_success_no_error_envelope() -> None:
    """NEW CONTRACT: publish_layer echoing the raw s3:// COG uri is the SUCCESS
    shape (TiTiler exit) -- the layer renders, NO honesty-floor error fires."""
    rec = _PublishRecorder(return_value=S3_COG)
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
        # The published s3 COG IS the renderable envelope -> exactly one merged
        # row, and NO honesty-floor error envelope.
        uris = [layer.uri for layer in state.emitter.loaded_layers]
        assert uris.count(S3_COG) == 1, f"expected one merged s3 row: {uris}"
        assert _error_envelopes(ws) == [], (
            "an s3:// publish return is SUCCESS and must not raise the floor"
        )
    finally:
        _restore_tool("compute_blended_stub", original)
        _restore_tool("publish_layer", original_pub)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_return",
    ["", None, "PUBLISH_FAILED: boom", "gs://bucket/cache/echo.tif"],
)
async def test_publish_non_renderable_return_surfaces_honesty_floor_error(
    bad_return,
) -> None:
    """The floor stays HONEST: a publish return that is neither http(s) nor
    s3:// (empty/None/error strings, gs://) is still a FAILURE -- typed error
    envelope, no enriched row."""
    rec = _PublishRecorder(return_value=bad_return)
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
            layer_id="blend-bad",
            name="Blended",
            layer_type="raster",
            uri=S3_COG,
            style_preset="",
        )

    original = _install_tool("compute_blended_bad_stub", _tool)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._invoke_tool_via_emitter(
            ws, state, "compute_blended_bad_stub", {}
        )
        # Only the direct seam emission of the tool's own (renderable) s3 row;
        # the failed publish added NOTHING.
        assert [layer.uri for layer in state.emitter.loaded_layers] == [S3_COG]
        errors = _error_envelopes(ws)
        assert any(
            e.get("payload", {}).get("error_code") == "INTERNAL_ERROR"
            and "LAYER_AUTO_PUBLISH_FAILED" in e.get("payload", {}).get("message", "")
            for e in errors
        ), f"non-renderable publish return must surface an error: {errors}"
    finally:
        _restore_tool("compute_blended_bad_stub", original)
        _restore_tool("publish_layer", original_pub)
