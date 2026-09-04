"""Automatic emission: a raster reaches the map without anyone asking.

NATE's directive, twice. First (2026-06-26): "we should not have the LLM
enforce publishing of layers -- this should just be done without LLM
intervention." Then ruling (b), 2026-08: emission is automatic EVERYWHERE, the
"display this" intent is retired, and the user hides what they do not want.

ADR 0313 landed the second half. The publish used to happen at a call site in
the dispatch layer (``server/dispatch/emitter.py`` ->
``results.py::_auto_publish_droppable_raster``), parallel to the emission seam
and reachable only from the WS server, gated by a per-tool ``auto_publish``
flag. Both are gone. The publish now happens inside
``PipelineEmitter.emit_tool_call``'s LayerURI branch via
``layer_uri_emit.publish_for_emission`` - the ONE seam ``emit_layer_uri``
guards - so these tests drive the emitter rather than the server, which is
also what makes them mean something: no LLM and no dispatch wrapper are in the
picture, so a published layer can only have come from the seam.

The honesty floor CHANGED SHAPE with the QGIS-native swap, and that is
asserted here rather than assumed. Publishing enriches a raster (COG
overviews, resolved style params, the data-driven legend); it does not make it
reachable, because the plugin reads a raw ``s3://`` COG via ``/vsicurl/``
either way. So a failed publish is a DEGRADE - the layer still reaches the map,
unstyled, with a warning - not the typed ``LAYER_AUTO_PUBLISH_FAILED`` the
MapLibre era needed. The guardrail that keeps genuinely un-renderable rasters
(``gs://`` / ``file://`` / empty) off the map is still ``emit_layer_uri``.

Asserted:
  * a raster ``s3://`` LayerURI publishes ONCE, unasked, and reaches
    ``loaded_layers`` carrying the PUBLISHED uri;
  * an INTERMEDIATE (the class that used to carry ``auto_publish: false``)
    publishes too - there is no opt-out;
  * every layer of a returned LIST takes the same trip;
  * an http(s) raster is untouched (already a rendered face);
  * a vector is untouched (inline-GeoJSON path);
  * a publish that RAISES, and one that returns a non-renderable value, both
    fail OPEN: the raw COG still reaches the map;
  * a ``gs://`` raster is still DROPPED by the guardrail after the publish
    step declines to touch it.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.emission import layer_uri_emit
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter

S3_COG = "s3://bucket/cache/hillshade.tif"


def _published(uri: str) -> str:
    """What the real publish returns: the overview sibling of the SAME COG.

    Per-input rather than a single constant, because ``add_loaded_layer`` dedups
    by COG identity - a stub that returned one uri for every input would make a
    three-frame series look like one layer and hide the bug this file is here
    to catch.
    """
    head, _, tail = uri.rpartition("/")
    return f"{head}/overviews/{tail}"


PUBLISHED = _published(S3_COG)


class _Sink:
    def __init__(self) -> None:
        self.frames: list[str] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(text)


def _emitter() -> tuple[PipelineEmitter, _Sink]:
    sink = _Sink()
    return PipelineEmitter(session_id=new_ulid(), sink=sink), sink


def _loaded_layers(sink: _Sink) -> list[dict]:
    """The LAST session-state's loaded_layers (the accumulator's final shape)."""
    out: list[dict] = []
    for raw in sink.frames:
        env = json.loads(raw)
        if env.get("type") == "session-state":
            out = (env.get("payload") or {}).get("loaded_layers") or out
    return out


@pytest.fixture
def publish_recorder(monkeypatch):
    """Record every publish the seam makes; return the published uri."""
    calls: list[dict] = []

    def _publish(**kwargs):
        calls.append(kwargs)
        return _published(kwargs["layer_uri"])

    from trid3nt_server.emission import publish as publish_mod

    monkeypatch.setattr(publish_mod, "publish_layer", _publish)
    return calls


def _raster(uri: str = S3_COG, **over) -> LayerURI:
    base = dict(
        layer_id="hillshade-1",
        name="Hillshade",
        layer_type="raster",
        uri=uri,
        role="primary",
    )
    base.update(over)
    return LayerURI(**base)


async def _run(emitter: PipelineEmitter, result):
    return await emitter.emit_tool_call(
        name="stub_tool", tool_name="stub_tool", invoke=lambda: result
    )


# --------------------------------------------------------------------------- #
# 1. The ordinary case: published once, unasked, and the map gets the result.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_raster_s3_publishes_once_and_reaches_the_map(publish_recorder) -> None:
    emitter, sink = _emitter()
    returned = await _run(emitter, _raster())

    assert len(publish_recorder) == 1, "the seam must publish exactly once"
    assert publish_recorder[0]["layer_uri"] == S3_COG
    # The TOOL's result is untouched -- the enrichment happens on the way to the
    # map, not on the value the caller (and the LLM) sees.
    assert returned.uri == S3_COG

    layers = _loaded_layers(sink)
    assert len(layers) == 1
    assert layers[0]["uri"] == PUBLISHED, "the map got the unpublished COG"


@pytest.mark.asyncio
async def test_intermediate_publishes_too_there_is_no_opt_out(
    publish_recorder,
) -> None:
    """ADR 0313: the ``auto_publish`` flag is deleted, not defaulted.

    The DEM was the flagship opt-out (``auto_publish: false`` in its
    source.yaml). NATE: the user hides what they do not want, so an
    intermediate is still a layer.
    """
    emitter, sink = _emitter()
    await _run(emitter, _raster(layer_id="dem-1", name="DEM", role="input"))

    assert len(publish_recorder) == 1
    assert _loaded_layers(sink)[0]["uri"] == PUBLISHED

    from trid3nt_contracts.tool_registry import AtomicToolMetadata

    assert not hasattr(AtomicToolMetadata, "model_fields") or (
        "auto_publish" not in AtomicToolMetadata.model_fields
    ), "the auto_publish opt-out is back"


@pytest.mark.asyncio
async def test_every_layer_of_a_list_takes_the_same_trip(publish_recorder) -> None:
    """A frame series is N layers, not one -- the dispatch site handled this
    and the seam previously did not."""
    emitter, sink = _emitter()
    frames = [
        _raster(layer_id=f"frame-{i}", uri=f"s3://bucket/f{i}.tif") for i in range(3)
    ]
    await _run(emitter, frames)

    assert len(publish_recorder) == 3
    layers = _loaded_layers(sink)
    assert len(layers) == 3, "the frames collapsed - dedup ate the series"
    assert [layer["uri"] for layer in layers] == [
        _published(f"s3://bucket/f{i}.tif") for i in range(3)
    ]


# --------------------------------------------------------------------------- #
# 2. What the seam declines to touch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_http_raster_is_not_republished(publish_recorder) -> None:
    emitter, sink = _emitter()
    await _run(emitter, _raster(uri="https://tiles.example/x/{z}/{x}/{y}.png"))

    assert publish_recorder == [], "an http(s) raster is already a rendered face"
    assert _loaded_layers(sink)[0]["uri"].startswith("https://")


@pytest.mark.asyncio
async def test_vector_is_not_published(publish_recorder) -> None:
    emitter, sink = _emitter()
    await _run(
        emitter,
        _raster(layer_id="v-1", layer_type="vector", uri="s3://bucket/x.fgb"),
    )

    assert publish_recorder == [], "vectors render inline from their own GeoJSON"
    assert _loaded_layers(sink)[0]["uri"] == "s3://bucket/x.fgb"


@pytest.mark.asyncio
async def test_gs_raster_is_still_dropped_by_the_guardrail(publish_recorder) -> None:
    """The publish declines it; ``emit_layer_uri`` then keeps it off the map."""
    emitter, sink = _emitter()
    await _run(emitter, _raster(uri="gs://bucket/x.tif"))

    assert publish_recorder == []
    assert _loaded_layers(sink) == []


# --------------------------------------------------------------------------- #
# 3. The honesty floor's new shape: a failed publish DEGRADES, never blanks.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_publish_that_raises_fails_open_to_the_raw_cog(monkeypatch) -> None:
    from trid3nt_server.emission import publish as publish_mod

    def _boom(**kwargs):
        raise publish_mod.PublishLayerError("no", error_code="PUBLISH_FAILED")

    monkeypatch.setattr(publish_mod, "publish_layer", _boom)

    emitter, sink = _emitter()
    await _run(emitter, _raster())

    layers = _loaded_layers(sink)
    assert len(layers) == 1, "a failed publish must not blank the layer"
    assert layers[0]["uri"] == S3_COG, (
        "the raw s3 COG is renderable via /vsicurl/ -- an unstyled layer is the "
        "honest degrade"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", None, "PUBLISH_FAILED", "gs://bucket/x.tif"])
async def test_non_renderable_publish_return_fails_open(monkeypatch, bad) -> None:
    from trid3nt_server.emission import publish as publish_mod

    monkeypatch.setattr(publish_mod, "publish_layer", lambda **kw: bad)

    emitter, sink = _emitter()
    await _run(emitter, _raster())

    layers = _loaded_layers(sink)
    assert len(layers) == 1
    assert layers[0]["uri"] == S3_COG, f"a {bad!r} return must not reach the map"


@pytest.mark.asyncio
async def test_publish_for_emission_is_the_only_seam() -> None:
    """The dispatch-layer twin is DELETED, not disabled (ADR 0313)."""
    assert hasattr(layer_uri_emit, "publish_for_emission")

    from trid3nt_server.server.dispatch import results

    assert not hasattr(results, "_auto_publish_droppable_raster")
    assert not hasattr(results, "_emit_auto_publish_failure")

    from trid3nt_server.tools import TOOL_REGISTRY

    assert "publish_layer" not in TOOL_REGISTRY, (
        "publish_layer is a mechanism in emission/, never a tool again"
    )
