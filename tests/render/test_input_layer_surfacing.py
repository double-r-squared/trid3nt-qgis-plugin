"""Input-layer surfacing: the primitives and the one worker case.

A router-fetched renderable input surfaces through the emit-on-fetch seam, so
what is pinned here is what the seam does NOT own: the emission primitives it
rides, which force the role, are best-effort and honour the guardrail; the
in-worker bathymetry. All I/O mocked."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts import new_ulid

from trid3nt_server.render.layer_uri_emit import (
    publish_input_layer,
    publish_raster_input_cog,
)
from trid3nt_server.render.pipeline_emitter import (
    _CURRENT_EMITTER,
    PipelineEmitter,
)


class _Sink:
    async def __call__(self, text: str) -> None:  # pragma: no cover - trivial
        import json

        json.loads(text)


def _emitter() -> PipelineEmitter:
    return PipelineEmitter(session_id=new_ulid(), sink=_Sink())


@pytest.mark.asyncio
async def test_publish_input_layer_forces_role_input_and_no_bbox():
    """A vector with role!=input + a bbox is COPIED to role="input" + bbox=None
    (an input must render non-intrusively and emit NO competing zoom-to)."""
    import json

    frames: list[dict] = []

    async def _capture(text: str) -> None:
        frames.append(json.loads(text))

    emitter = PipelineEmitter(session_id=new_ulid(), sink=_capture)
    layer = LayerURI(
        layer_id="rivers-1",
        name="Rivers",
        layer_type="vector",
        uri="s3://runs/r/rivers.fgb",
        role="primary",
        bbox=(-1.0, -1.0, 1.0, 1.0),
    )
    ok = await publish_input_layer(emitter, layer)

    assert ok is True
    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "input"
    assert row.layer_id == "rivers-1"
    # bbox forced to None => NO zoom-to map-command was emitted for the input.
    map_cmds = [f for f in frames if f.get("type") == "map-command"]
    zoom_tos = [
        f for f in map_cmds if (f.get("payload") or {}).get("command") == "zoom-to"
    ]
    assert zoom_tos == [], f"an input must not emit a zoom-to; got {zoom_tos}"


@pytest.mark.asyncio
async def test_publish_input_layer_surfaces_raw_s3_raster():
    """NEW CONTRACT (TiTiler exit / QGIS-native swap): a raster carrying a raw
    s3:// COG uri PASSES the guardrail (the plugin reads it via /vsicurl/) and
    IS surfaced as an input row."""
    emitter = _emitter()
    layer = LayerURI(
        layer_id="dem-raw",
        name="DEM",
        layer_type="raster",
        uri="s3://runs/r/dem.tif",  # raw s3 COG - now renderable
        role="input",
    )
    ok = await publish_input_layer(emitter, layer)
    assert ok is True
    assert len(emitter._loaded_layers) == 1
    assert emitter._loaded_layers[0].uri == "s3://runs/r/dem.tif"
    assert emitter._loaded_layers[0].role == "input"


@pytest.mark.asyncio
async def test_publish_input_layer_drops_gs_raster():
    """A raster carrying a raw gs:// uri is still DROPPED by the guardrail
    (no face on this stack can fetch it) -> not surfaced, returns False,
    NEVER raises."""
    emitter = _emitter()
    layer = LayerURI(
        layer_id="dem-gs",
        name="DEM",
        layer_type="raster",
        uri="gs://runs/r/dem.tif",  # genuinely un-renderable
        role="input",
    )
    ok = await publish_input_layer(emitter, layer)
    assert ok is False
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_publish_input_layer_none_emitter_is_noop():
    """No emitter bound (verify/CI direct-call) -> no-op, returns False, no raise."""
    layer = LayerURI(
        layer_id="x", name="x", layer_type="vector", uri="s3://r/x.fgb",
        role="input",
    )
    assert await publish_input_layer(None, layer) is False
    assert await publish_input_layer(_emitter(), None) is False


@pytest.mark.asyncio
async def test_publish_input_layer_swallows_add_loaded_layer_failure():
    """A failure inside add_loaded_layer is swallowed (best-effort): returns
    False, NEVER raises -- the solve is unaffected."""
    emitter = _emitter()

    async def _boom(_layer):
        raise RuntimeError("emit blew up")

    emitter.add_loaded_layer = _boom  # type: ignore[method-assign]
    layer = LayerURI(
        layer_id="v", name="v", layer_type="vector", uri="s3://r/v.fgb",
        role="input",
    )
    # Must NOT raise.
    ok = await publish_input_layer(emitter, layer)
    assert ok is False


# (1b) publish_raster_input_cog -- the EXISTING-COG raster input seam
#      The bathymetry-consuming coastal templates surface their
#      fetched topobathy the same way the flood DEM path does.
_PUBLISH_LAYER_TARGET = (
    "trid3nt_server.render.publish.publish_layer"
)
_COG_EXISTS_TARGET = "trid3nt_server.render.layer_uri_emit._cog_object_exists"


@pytest.mark.asyncio
async def test_publish_raster_input_cog_surfaces_with_provenance():
    """An existing COG rounds through the publish and reaches the emitter.

    It arrives as a context raster carrying its provenance name and its ramp: a valid
    input layer MUST reach the emitter, never silently drop."""
    published: list[dict] = []

    def _mock_publish_layer(layer_uri, layer_id, style=None, name=None, **kw):  # noqa: ANN001
        published.append(
            {"layer_uri": layer_uri, "layer_id": layer_id,
             "style": style, "name": name}
        )
        return "s3://test-runs/RID/input-bathymetry.tif"

    emitter = _emitter()
    with patch(_COG_EXISTS_TARGET, return_value=True), \
         patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
        ok = await publish_raster_input_cog(
            emitter,
            cog_uri="s3://test-cache/topobathy/aoi.tif",
            layer_id="input-bathymetry-RID",
            name='Input: bathymetry (topobathy, native CUDEM 1/9")',
            style={"kind": "continuous", "ramp": "gray", "units": "m",
                   "label": "Elevation"},
        )

    assert ok is True
    # It rode the EXISTING object (no re-upload): the cog_uri went straight to
    # publish_layer as the layer_uri.
    assert len(published) == 1
    assert published[0]["layer_uri"] == "s3://test-cache/topobathy/aoi.tif"
    assert published[0]["style"]["label"] == "Elevation"
    # The valid LayerURI reached the emitter (cannot silently drop).
    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.layer_type == "raster"
    assert row.name.startswith("Input: bathymetry (")
    assert row.uri == "s3://test-runs/RID/input-bathymetry.tif"


@pytest.mark.asyncio
async def test_publish_raster_input_cog_publish_failure_non_fatal():
    """A publish_layer PublishLayerError is swallowed (best-effort): returns
    False, surfaces nothing, NEVER raises -- a failed input can never fail the
    solve."""
    from trid3nt_server.render.publish import (
        PublishLayerError,
    )

    def _boom(*a, **k):
        raise PublishLayerError("PUBLISH_FAILED", "boom")

    emitter = _emitter()
    with patch(_COG_EXISTS_TARGET, return_value=True), \
         patch(_PUBLISH_LAYER_TARGET, side_effect=_boom):
        ok = await publish_raster_input_cog(
            emitter, cog_uri="s3://c/x.tif", layer_id="input-bathymetry-x",
            name="Input: bathymetry (x)", )
    assert ok is False
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_publish_raster_input_cog_skips_missing_object_loudly(caplog):
    """The dead-COG class: a manifest naming a file the store never received.

    The head reports it absent, so the object is SKIPPED before the publish - no 404
    layer registered, a LOUD warning naming the layer and uri, and no raise."""
    called = {"n": 0}

    def _spy(*a, **k):  # pragma: no cover - must not run
        called["n"] += 1
        return "s3://x"

    emitter = _emitter()
    with patch(_COG_EXISTS_TARGET, return_value=False), \
         patch(_PUBLISH_LAYER_TARGET, side_effect=_spy), \
         caplog.at_level(
             "WARNING", logger="trid3nt_server.render.layer_uri_emit"):
        ok = await publish_raster_input_cog(
            emitter, cog_uri="s3://test-runs/RID/bed_bathymetry.tif",
            layer_id="input-river-bed-DEAD", name="Input: river bed bathymetry",
            role="context",
        )

    assert ok is False
    assert emitter._loaded_layers == []
    assert called["n"] == 0  # publish_layer never even called -- no upload race
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "SKIPPING" in msgs
    assert "input-river-bed-DEAD" in msgs
    assert "s3://test-runs/RID/bed_bathymetry.tif" in msgs


@pytest.mark.asyncio
async def test_publish_raster_input_cog_none_emitter_or_uri_noop():
    """No emitter bound OR a falsy cog_uri -> no-op, returns False, no raise,
    and publish_layer is never even called."""
    called = {"n": 0}

    def _spy(*a, **k):  # pragma: no cover - must not run
        called["n"] += 1
        return "s3://x"

    with patch(_PUBLISH_LAYER_TARGET, side_effect=_spy):
        assert await publish_raster_input_cog(
            None, cog_uri="s3://c/x.tif", layer_id="i", name="n",
        ) is False
        assert await publish_raster_input_cog(
            _emitter(), cog_uri="", layer_id="i", name="n",
        ) is False
    assert called["n"] == 0
