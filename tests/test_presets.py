"""The preset family: four kinds, parameters, and the .qml the writer emits.

The load-validation half of this lives in ``scripts/qml_preset_smoke.py``,
which needs the installed QGIS. What is offline-provable is here: the family
is closed at four, a quantity parameterises a preset instead of minting one,
the override order is the declared one, and the document the writer produces
says what the resolved preset says.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from trid3nt_server.emission import presets
from trid3nt_server.emission.presets import Preset, Scale


def _doc(resolved):
    return ET.fromstring(presets.qml(resolved))


# --------------------------------------------------------------------------- #
# the family
# --------------------------------------------------------------------------- #

def test_the_family_is_four_kinds_and_every_one_of_them_writes_a_document():
    assert presets.KINDS == ("continuous", "classed", "reference", "mesh")
    parameterised = {
        "continuous": Preset(kind="continuous"),
        "classed": Preset(kind="classed", geometry="polygon",
                          classes=((0.0, 1.0, "#ffffcc", "low"),)),
        "reference": Preset(kind="reference", geometry="line"),
        "mesh": Preset(kind="mesh", dataset_group="WATER DEPTH"),
    }
    for kind in presets.KINDS:
        assert _doc(presets.resolve(parameterised[kind])).tag == "qgis"


def test_a_preset_with_nothing_to_say_about_this_layer_writes_no_document():
    # QGIS binds a mesh dataset group BY NAME and silently drops an unbound
    # block; a reference symbol of the wrong shape loads and draws nothing.
    assert presets.qml(presets.resolve(Preset(kind="mesh"))) is None
    assert presets.qml(presets.resolve(Preset(kind="reference"))) is None


def test_a_declaration_that_names_no_parameters_gets_its_kinds_bare_default():
    for kind in presets.KINDS:
        assert presets.from_row({"kind": kind}) == presets.bare_default(kind)
    # An absent row is a continuous raster, which is what a raster product is.
    assert presets.from_row(None).kind == "continuous"


def test_a_quantity_parameterises_the_preset_it_never_mints_one():
    depth = presets.from_row({
        "kind": "continuous", "ramp": "ylgnbu", "units": "m", "label": "Flood depth",
        "scale": {"policy": "data", "transform": "percentile", "clip": [2, 98],
                  "range": [0, 3]}})
    velocity = presets.from_row({
        "kind": "continuous", "ramp": "plasma", "units": "m/s",
        "label": "Flow velocity"})
    assert depth.kind == velocity.kind == "continuous"
    assert (depth.units, depth.label, depth.ramp) == ("m", "Flood depth", "ylgnbu")
    assert (velocity.units, velocity.label, velocity.ramp) == (
        "m/s", "Flow velocity", "plasma")


def test_titling_a_preset_for_a_quantity_leaves_the_shape_alone():
    titled = presets.bare_default("continuous").titled("Head", "m")
    assert (titled.kind, titled.label, titled.units) == ("continuous", "Head", "m")


# --------------------------------------------------------------------------- #
# the one scale
# --------------------------------------------------------------------------- #

def test_a_fixed_scale_never_asks_for_the_layers_own_range():
    asked: list[Scale] = []

    def _read(scale):
        asked.append(scale)
        return (99.0, 100.0)

    resolved = presets.resolve(
        Preset(scale=Scale(policy="fixed", range=(0.0, 1.0))), read_range=_read)
    assert resolved.range == (0.0, 1.0)
    assert resolved.source == presets.FIXED
    assert asked == []


def test_a_data_policy_reads_the_range_off_the_layer_and_says_so():
    resolved = presets.resolve(Preset(units="m"), read_range=lambda _s: (0.4, 2.6))
    assert resolved.range == (0.4, 2.6)
    assert resolved.source == presets.FROM_DATA
    assert resolved.legend_note() == "scaled to this run (p2-p98): 0.4 to 2.6 m"


def test_an_unreadable_layer_falls_back_to_the_declared_range_and_says_that():
    resolved = presets.resolve(
        Preset(scale=Scale(policy="data", range=(0.0, 5.0))), read_range=lambda _s: None)
    assert resolved.range == (0.0, 5.0)
    assert resolved.source == presets.FALLBACK
    assert "declared fallback range" in resolved.legend_note()


def test_a_shared_range_beats_a_read_and_the_legend_stops_naming_percentiles():
    resolved = presets.resolve(
        Preset(), read_range=lambda _s: (0.0, 1.0), shared=(0.0, 9.0))
    assert resolved.range == (0.0, 9.0)
    assert resolved.legend_note() == (
        "one range shared across the compared set: 0 to 9")


def test_one_range_spans_a_compared_set():
    assert presets.shared_range([(0.0, 3.0), None, (-1.0, 2.0)]) == (-1.0, 3.0)
    assert presets.shared_range([None, None]) is None


def test_a_zero_width_range_is_padded_because_a_renderer_rejects_one():
    lo, hi = presets.resolve(Preset(), read_range=lambda _s: (2.0, 2.0)).range
    assert lo < 2.0 < hi


def test_the_override_wins_field_by_field_over_the_declaration():
    declared = Preset(ramp="reds", scale=Scale(policy="fixed", range=(0.0, 1.0)))
    resolved = presets.resolve(
        declared, override=Scale(policy="fixed", range=(0.0, 30.0)))
    assert resolved.range == (0.0, 30.0)
    assert resolved.preset.ramp == "reds"


def test_a_reference_layer_is_drawn_not_measured_so_it_carries_no_scale():
    resolved = presets.resolve(presets.bare_default("reference"),
                               read_range=lambda _s: (0.0, 1.0))
    assert resolved.range is None
    assert resolved.legend_note() == "no scale (drawn, not measured)"


def test_declared_classes_are_the_scale_and_the_legend_says_how_many():
    resolved = presets.resolve(Preset(
        kind="classed", classes=((0.0, 1.0, "#ffffcc", "low"),
                                 (1.0, 5.0, "#bd0026", "high"))))
    assert resolved.legend_note() == "2 declared classes"


# --------------------------------------------------------------------------- #
# ramps
# --------------------------------------------------------------------------- #

def test_a_reversed_ramp_is_the_base_reversed():
    assert presets.ramp_stops("ylorrd_r") == tuple(reversed(presets.ramp_stops("ylorrd")))


def test_an_unknown_ramp_takes_the_default_never_grey():
    assert presets.ramp_stops("chartreuse_deluxe") == presets.ramp_stops(
        presets.DEFAULT_RAMP)


def test_the_compass_ramp_is_closed_because_a_bearing_wraps():
    stops = presets.ramp_stops("hsv")
    assert stops[0] == stops[-1]


# --------------------------------------------------------------------------- #
# the writer
# --------------------------------------------------------------------------- #

def test_the_continuous_document_carries_the_resolved_range_and_the_ramp():
    resolved = presets.resolve(Preset(ramp="blues", units="m/s"),
                               read_range=lambda _s: (0.0, 4.0))
    renderer = _doc(resolved).find("./pipe/rasterrenderer")
    assert renderer.get("type") == "singlebandpseudocolor"
    assert renderer.get("classificationMin") == "0"
    assert renderer.get("classificationMax") == "4"
    items = renderer.findall("./rastershader/colorrampshader/item")
    assert [i.get("color") for i in items] == list(presets.ramp_stops("blues"))
    assert [i.get("value") for i in items] == ["0", "1", "2", "3", "4"]
    assert items[-1].get("label") == "4 m/s"


def test_a_classed_raster_paints_discrete_bands_not_a_ramp():
    # A field spanning orders of magnitude has no linear ramp that shows both
    # ends, and a classed row with no geometry is a raster.
    resolved = presets.resolve(Preset(
        kind="classed", classes=((0.0, 1.0, "#ffffcc", "low"),
                                 (1.0, 5.0, "#bd0026", "high"))))
    shader = _doc(resolved).find("./pipe/rasterrenderer/rastershader/colorrampshader")
    assert shader.get("colorRampType") == "DISCRETE"
    assert [i.get("value") for i in shader.findall("./item")] == ["1", "5"]


def test_a_classed_document_carries_every_declared_break_once():
    resolved = presets.resolve(Preset(
        kind="classed", geometry="polygon", attribute="yield",
        classes=((0.0, 1.0, "#ffffcc", "low"), (1.0, 5.0, "#bd0026", "high"))))
    renderer = _doc(resolved).find("./renderer-v2")
    assert renderer.get("type") == "graduatedSymbol"
    assert renderer.get("attr") == "yield"
    labels = [r.get("label") for r in renderer.findall("./ranges/range")]
    assert labels == ["low", "high"]
    assert len(renderer.findall("./symbols/symbol")) == 2


@pytest.mark.parametrize("geometry,symbol_class", [
    ("point", "SimpleMarker"), ("line", "SimpleLine"), ("polygon", "SimpleFill")])
def test_a_reference_document_draws_the_symbol_its_geometry_needs(geometry, symbol_class):
    resolved = presets.resolve(Preset(kind="reference", geometry=geometry))
    layer = _doc(resolved).find("./renderer-v2/symbols/symbol/layer")
    assert layer.get("class") == symbol_class


def test_a_mesh_document_binds_its_group_by_name_because_an_index_does_not_survive():
    resolved = presets.resolve(
        Preset(kind="mesh", dataset_group="WATER DEPTH",
               scale=Scale(policy="fixed", range=(0.0, 2.0))))
    doc = _doc(resolved)
    assert doc.find("./mesh-renderer-settings/scalar-settings").get("min-val") == "0"
    assert doc.find("./name-to-global-index").get("name") == "WATER DEPTH"


def test_a_label_with_xml_punctuation_survives_the_writer():
    resolved = presets.resolve(Preset(units='m<sup>3</sup>/s & "cfs"'))
    items = _doc(resolved).findall("./pipe/rasterrenderer/rastershader/colorrampshader/item")
    assert items[-1].get("label").endswith('m<sup>3</sup>/s & "cfs"')
