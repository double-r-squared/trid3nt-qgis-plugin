"""The presentation surface: the ask beats the declaration, and hide is the un-emit.

Emission is automatic, so nothing here puts a layer on the map. What this
covers is everything a reader may change about one afterwards - which of the
four shapes draws it, its ramp, its title, the range it is read on, and whether
it is on the canvas at all - and the two rules that make those changes
predictable: an ask wins field by field over what the data declared, and an ask
nobody made leaves the declaration untouched rather than re-asserting a default
over it.

The re-paint is asserted on the RESOLVED preset the surface returns, which is
the same resolution the .qml and the legend sentence are both built from.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from trid3nt_contracts import new_ulid

from trid3nt_server.emission import pipeline_emitter as emitter_module
from trid3nt_server.emission import publish as publish_module
from trid3nt_server.emission import restyle as restyle_module
from trid3nt_server.emission.pipeline_emitter import PipelineEmitter
from trid3nt_server.emission.restyle import RestyleError, apply_style
from trid3nt_server.tools.display.restyle_layer.restyle_layer import restyle_layer

#: A declared row with something to say in every field, so an override that
#: leaked into a neighbour is visible rather than masked by a default.
DECLARED = {
    "kind": "continuous",
    "ramp": "reds",
    "units": "m",
    "label": "Flood depth",
    "scale": {"policy": "fixed", "range": [0.0, 1.0], "transform": "linear"},
}

URI = "s3://trid3nt-runs/case/depth.tif"


@pytest.fixture()
def published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what the surface hands the publish path, and read no bytes.

    A restyle re-runs the publish path so the resolution stays in one place;
    the object store is not what these tests are about.
    """
    calls: list[dict[str, Any]] = []

    def _publish(**kwargs: Any) -> str:
        calls.append(kwargs)
        return str(kwargs.get("layer_uri"))

    monkeypatch.setattr(publish_module, "publish_layer", _publish)
    return calls


# --------------------------------------------------------------------------- #
# the ask against the declaration
# --------------------------------------------------------------------------- #


def test_the_ask_beats_the_declared_row_field_by_field(published) -> None:
    resolved = apply_style(
        layer_uri=URI, layer_id="depth", declared=DECLARED,
        ramp="blues", policy="fixed", value_range=(0.0, 30.0))

    assert resolved.preset.ramp == "blues"
    assert resolved.range == (0.0, 30.0)
    # The fields nobody asked about are still the data's own.
    assert resolved.preset.units == "m"
    assert resolved.preset.label == "Flood depth"
    assert resolved.kind == "continuous"
    # The publish path gets the SAME row, so the .qml it writes is this one.
    assert published[0]["style"]["ramp"] == "blues"
    assert published[0]["style"]["label"] == "Flood depth"


def test_an_ask_nobody_made_leaves_the_declaration_alone(published) -> None:
    resolved = apply_style(layer_uri=URI, layer_id="depth", declared=DECLARED,
                           label="Depth over the floodplain")

    assert resolved.preset.label == "Depth over the floodplain"
    # No scale ask was made, so the declared fixed scale stands - it is not
    # merged over with an empty override that would re-assert the defaults.
    assert resolved.range == (0.0, 1.0)
    assert resolved.legend_note() == "fixed domain scale: 0 to 1 m"


def test_a_kind_outside_the_family_is_refused_rather_than_painted(published) -> None:
    with pytest.raises(RestyleError) as caught:
        apply_style(layer_uri=URI, layer_id="depth", declared=DECLARED,
                    kind="heatmap")
    assert caught.value.error_code == "STYLE_KIND_UNKNOWN"
    assert not published


def test_a_comparison_paints_every_layer_on_the_one_range(published) -> None:
    shared = (0.0, 9.0)
    depth = apply_style(layer_uri=URI, layer_id="depth", declared=DECLARED,
                        shared=shared)
    rematch = apply_style(layer_uri=URI + ".refined", layer_id="depth-refined",
                          declared=DECLARED, shared=shared)

    assert depth.range == rematch.range == shared
    assert depth.legend_note() == (
        "one range shared across the compared set: 0 to 9 m")


def test_the_restyle_is_journaled_with_the_sentence_the_legend_says(
    published, monkeypatch: pytest.MonkeyPatch
) -> None:
    notes: list[str] = []
    monkeypatch.setattr(restyle_module, "journal_note", notes.append)

    resolved = apply_style(layer_uri=URI, layer_id="depth", declared=DECLARED)

    assert notes == [f"restyle depth: {resolved.legend_note()}"]


# --------------------------------------------------------------------------- #
# the un-emit
# --------------------------------------------------------------------------- #


class _CapturingSink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.fixture()
def bound_emitter(monkeypatch: pytest.MonkeyPatch):
    """An emitter holding one published layer, bound as the current one."""
    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)
    emitter.reset_loaded_layers([{
        "layer_id": "depth", "name": "Flood depth", "layer_type": "raster",
        "uri": URI, "visible": True, "role": "primary", "temporal": False,
    }])
    token = emitter_module._CURRENT_EMITTER.set(emitter)
    try:
        yield emitter, sink
    finally:
        emitter_module._CURRENT_EMITTER.reset(token)


def _row(sink: _CapturingSink) -> dict[str, Any]:
    states = [f for f in sink.frames if f["type"] == "session-state"]
    return states[-1]["payload"]["loaded_layers"][0]


@pytest.mark.asyncio
async def test_hiding_a_layer_is_the_un_emit_and_unhiding_puts_it_back(
    bound_emitter,
) -> None:
    _emitter, sink = bound_emitter

    hidden = await restyle_layer(layer_ids="depth", hide=True)
    assert hidden["status"] == "ok"
    assert hidden["layers"] == [{"layer_id": "depth", "hidden": True}]
    assert _row(sink)["visible"] is False

    shown = await restyle_layer(layer_ids="depth", hide=False)
    assert shown["status"] == "ok"
    assert _row(sink)["visible"] is True


@pytest.mark.asyncio
async def test_hiding_a_layer_nobody_published_refuses_rather_than_creating_one(
    bound_emitter,
) -> None:
    _emitter, sink = bound_emitter

    refused = await restyle_layer(layer_ids="nothing-published", hide=True)

    assert refused["status"] == "error"
    assert refused["error_code"] == "LAYER_NOT_PUBLISHED"
    assert not [f for f in sink.frames if f["type"] == "session-state"]
