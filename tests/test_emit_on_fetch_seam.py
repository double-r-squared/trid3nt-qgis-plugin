"""ADR 0244 -- the emit-on-fetch router seam.

Pins the IN-COMPOSER input-surfacing hook (``maybe_emit_input_on_fetch``) that
``route()`` fires after a successful LayerURI build:

  * declaration-present (a renderable raster/vector) emits a role="context"
    "Input: ..." row carrying the spec preset -- via BOTH the worker-thread
    off-load path (``run_coroutine_threadsafe``) and the on-loop path;
  * declaration-absent (a record source) never attempts;
  * ``visualize=False`` (a probe fetch) suppresses;
  * a repeat fetch of the same uri is de-duped (surfaced once per session);
  * a surfacing failure is non-fatal (never raises);
  * no emitter bound -> no-op;
  * a DIRECT dispatch (dispatched tool == the fetcher) is skipped -- the
    tool-wrapper already emits the returned LayerURI.

publish_layer + the vector inline-read are mocked; no network / boto3.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.tools.fetchers._router.emit_on_fetch import (
    input_layer_name,
    maybe_emit_input_on_fetch,
)
from trid3nt_server.emission.pipeline_emitter import (
    _CURRENT_EMITTER,
    _DISPATCHED_TOOL,
    PipelineEmitter,
)

_PUBLISH_LAYER_TARGET = (
    "trid3nt_server.tools.publish_layer.publish_layer.publish_layer"
)


class _Sink:
    async def __call__(self, text: str) -> None:
        import json

        json.loads(text)


def _emitter() -> PipelineEmitter:
    return PipelineEmitter(session_id=new_ulid(), sink=_Sink())


def _spec(
    *,
    name: str = "fetch_dem",
    source_class: str = "3dep",
    layer_type: str = "raster",
    native_hint: str | None = "3DEP 10 m",
) -> SimpleNamespace:
    """A minimal stand-in carrying only the attributes the seam reads."""
    res = ()
    if native_hint is not None:
        res = (SimpleNamespace(native_hint=native_hint),)
    return SimpleNamespace(
        name=name,
        source_class=source_class,
        output=SimpleNamespace(layer_type=layer_type),
        resolution_declarations=res,
    )


def _raster_layer(uri: str = "s3://cache/3dep/aoi.tif") -> LayerURI:
    return LayerURI(
        layer_id="raw", name="3dep elevation", layer_type="raster",
        uri=uri, style_preset="continuous_dem", role="primary",
    )


def _vector_layer(uri: str = "s3://cache/rivers/aoi.fgb") -> LayerURI:
    return LayerURI(
        layer_id="raw", name="osm rivers", layer_type="vector",
        uri=uri, style_preset="osm_waterways", role="primary",
        bbox=(-1.0, -1.0, 1.0, 1.0),
    )


def _bind(emitter: PipelineEmitter, *, dispatched: str, loop=None):
    """Bind the ambient composer scope: emitter + a NON-fetch dispatched tool."""
    emitter._bound_loop = loop
    t1 = _CURRENT_EMITTER.set(emitter)
    t2 = _DISPATCHED_TOOL.set(dispatched)
    return (t1, t2)


def _unbind(tokens):
    _CURRENT_EMITTER.reset(tokens[0])
    _DISPATCHED_TOOL.reset(tokens[1])


# --------------------------------------------------------------------------- #
# name builder
# --------------------------------------------------------------------------- #
def test_input_layer_name_shape_and_purpose():
    spec = _spec()
    assert input_layer_name(spec, {}, None) == "Input: 3dep (3dep, 3DEP 10 m)"
    # purpose contributes ONE word (label, not a pathway).
    assert input_layer_name(spec, {}, "mesh bed") == (
        "Input: mesh bed (3dep, 3DEP 10 m)"
    )
    # no native hint -> source only.
    spec2 = _spec(native_hint=None, source_class="osm")
    assert input_layer_name(spec2, {"variable": "waterways"}, None) == (
        "Input: waterways (osm)"
    )


# --------------------------------------------------------------------------- #
# worker-thread off-load path (the real composer path)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_raster_input_surfaced_via_worker_thread(monkeypatch):
    """A raster fetched by an OFF-LOADED sync fetcher (no running loop in that
    thread) is driven back onto the emitter's bound loop and surfaced as a
    role=context continuous_dem input."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="model_landlab_scenario",
                   loop=asyncio.get_running_loop())

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        return layer_uri  # raw s3 COG passes the emit guardrail (plugin /vsicurl/)

    try:
        with patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
            # route() runs the fetch (and this hook) inside asyncio.to_thread.
            await asyncio.to_thread(
                maybe_emit_input_on_fetch,
                _spec(), {}, _raster_layer(),
                visualize=None, purpose="mesh bed",
            )
    finally:
        _unbind(tokens)

    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.layer_type == "raster"
    assert row.style_preset == "continuous_dem"
    assert row.name == "Input: mesh bed (3dep, 3DEP 10 m)"
    assert row.layer_id.startswith("input-3dep-")


@pytest.mark.asyncio
async def test_vector_input_surfaced_on_loop():
    """A vector fetched WITHOUT off-load (route on the loop thread) is surfaced
    fire-and-forget; the created task runs on the next loop tick."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="model_river_dye_scenario",
                   loop=asyncio.get_running_loop())
    try:
        with patch(
            "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
            return_value={"type": "FeatureCollection", "features": []},
        ):
            maybe_emit_input_on_fetch(
                _spec(name="fetch_river_geometry", source_class="osm",
                      layer_type="vector", native_hint=None),
                {}, _vector_layer(), visualize=None, purpose="river geometry",
            )
            # let the fire-and-forget task run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
    finally:
        _unbind(tokens)

    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.layer_type == "vector"
    assert row.name == "Input: river geometry (osm)"


# --------------------------------------------------------------------------- #
# the gates
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_record_source_never_attempts():
    """A record source (no visual form) surfaces nothing."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="some_composer",
                   loop=asyncio.get_running_loop())
    try:
        maybe_emit_input_on_fetch(
            _spec(name="fetch_lehd_jobs", layer_type="record"),
            {}, _raster_layer(), visualize=None, purpose=None,
        )
        await asyncio.sleep(0)
    finally:
        _unbind(tokens)
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_visualize_false_suppresses():
    """visualize=False (a probe fetch) suppresses surfacing."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="some_composer",
                   loop=asyncio.get_running_loop())
    try:
        with patch(_PUBLISH_LAYER_TARGET, side_effect=lambda **k: "s3://x"):
            maybe_emit_input_on_fetch(
                _spec(), {}, _raster_layer(), visualize=False, purpose=None,
            )
        await asyncio.sleep(0)
    finally:
        _unbind(tokens)
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_direct_dispatch_is_skipped():
    """A DIRECT chat fetch (dispatched tool == the fetcher) is skipped -- the
    tool-wrapper already emits the returned LayerURI, so the seam must not
    double-surface it as a context input."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="fetch_dem",  # == spec.name -> direct
                   loop=asyncio.get_running_loop())
    try:
        with patch(_PUBLISH_LAYER_TARGET, side_effect=lambda **k: "s3://x"):
            maybe_emit_input_on_fetch(
                _spec(name="fetch_dem"), {}, _raster_layer(),
                visualize=None, purpose=None,
            )
        await asyncio.sleep(0)
    finally:
        _unbind(tokens)
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_dedup_by_uri_within_session(monkeypatch):
    """Two composers re-fetching the SAME uri surface it once (session dedup)."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="model_a",
                   loop=asyncio.get_running_loop())

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        return layer_uri

    try:
        with patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
            await asyncio.to_thread(
                maybe_emit_input_on_fetch, _spec(), {}, _raster_layer(),
                visualize=None, purpose=None,
            )
            await asyncio.to_thread(
                maybe_emit_input_on_fetch, _spec(), {}, _raster_layer(),
                visualize=None, purpose=None,
            )
    finally:
        _unbind(tokens)
    assert len(emitter._loaded_layers) == 1


def test_no_emitter_bound_is_noop():
    """No emitter bracketing the call -> no-op, never raises (verify/CI path)."""
    # No _CURRENT_EMITTER set.
    maybe_emit_input_on_fetch(_spec(), {}, _raster_layer(), visualize=None, purpose=None)


@pytest.mark.asyncio
async def test_surfacing_failure_is_non_fatal():
    """A blow-up inside the emit path is swallowed (best-effort): never raises,
    and the dispatched fetch is unaffected."""
    import asyncio

    emitter = _emitter()
    tokens = _bind(emitter, dispatched="model_a",
                   loop=asyncio.get_running_loop())
    try:
        with patch(_PUBLISH_LAYER_TARGET,
                   side_effect=RuntimeError("publish blew up")):
            # Must NOT raise.
            await asyncio.to_thread(
                maybe_emit_input_on_fetch, _spec(), {}, _raster_layer(),
                visualize=None, purpose=None,
            )
    finally:
        _unbind(tokens)
    assert emitter._loaded_layers == []
