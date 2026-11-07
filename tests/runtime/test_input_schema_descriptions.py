"""Every template input is described on the wire: its unit, range or shape first, then what it means, cut to INPUT_DESC_CHARS.

The description is generated from what the input declares (units, bounds, type, slot, desc), so no template writes a second
line; the cut is the setting, 0 sends each whole."""

from __future__ import annotations

import pytest

from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.point import Point
from trid3nt_server.inputs.slots import BED, DOMAIN, EXTENT
from trid3nt_server.model.adapters.adapter import _tool_schema

_SHAPE_WORDS = ("point [lon, lat]", "true/false", "integer", "number", "text", "layer name",
                "polygon layer", "polyline layer", "point layer", "rectangle layer", "raster",
                "object")


@pytest.fixture(scope="module")
def templates():
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    return {name: entry for name, entry in TOOL_REGISTRY.items()
            if getattr(entry.metadata, "tier", "") == "template"}


def _lead(row: Input) -> str:
    """The first words the declaration says the wire description begins with."""
    if row.is_row:
        return ""
    if row.units:
        return row.units
    if row.bounds is not None:
        return str(int(row.bounds[0])) if float(row.bounds[0]).is_integer() else f"{row.bounds[0]:g}"
    return ""


def test_a_value_leads_with_its_unit_and_range_then_says_what_it_is():
    row = Input(type=float, bounds=(0.0, 86400.0), units="s", desc="How long the spill releases. More text.")
    assert row.schema_description == "s, 0-86400; How long the spill releases"
    assert Input(type=float, bounds=(1.0, 2.5), desc="A factor").schema_description == "1-2.5; A factor"
    assert Input(default=False, desc="Whether it runs").schema_description == "true/false; Whether it runs"
    assert Input(type=Point, desc="Where it enters").schema_description == "point [lon, lat]; Where it enters"
    assert Input(desc="Some words").schema_description == "text; Some words"


def test_a_row_leads_with_the_shape_it_takes():
    assert Input(DOMAIN, name="domain").schema_description.startswith("polygon layer name or uri; ")
    assert Input(BED, name="bed").schema_description.startswith("layer name, uri or number; ")
    assert Input(EXTENT, name="extent").schema_description == "rectangle layer name or uri"


def test_a_sentence_end_is_not_an_abbreviation():
    assert Input(desc="A substance - sewage | E. coli | coliform. And more").schema_description == \
        "text; A substance - sewage | E. coli | coliform"


def test_every_template_input_carries_a_description_within_the_cut_that_leads_with_its_unit_or_shape(templates):
    from trid3nt_server.model.adapters.adapter import input_desc_cap

    cap = input_desc_cap()
    assert cap == 120 and templates
    bad = []
    for name, entry in sorted(templates.items()):
        rows = {row.name: row for row in entry.fn.workflow.inputs}
        for param, node in _tool_schema(entry.fn)["properties"].items():
            text = node.get("description")
            row = rows.get(param)
            lead = _lead(row) if row is not None else ""
            if not text or len(text) > cap or (lead and not text.startswith(lead)) \
                    or (not lead and not text.startswith(_SHAPE_WORDS)):
                bad.append((name, param, text))
    assert not bad, f"inputs whose description is missing, over the cut or not led by unit or shape: {bad[:8]}"


def test_the_cut_is_the_setting_and_zero_sends_each_whole(templates, user_settings):
    fn = templates["telemac_dye_release"].fn
    whole = fn.param_descriptions["release"]
    assert len(whole) > 120

    user_settings(INPUT_DESC_CHARS=40)
    assert len(_tool_schema(fn)["properties"]["release"]["description"]) <= 40

    user_settings(INPUT_DESC_CHARS=0)
    assert _tool_schema(fn)["properties"]["release"]["description"] == whole


def test_the_fetch_arguments_are_described():
    import trid3nt_server.main as _main

    _main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    props = _tool_schema(TOOL_REGISTRY["fetch"].fn)["properties"]
    assert set(props) == {"source", "args"}
    assert all(0 < len(node["description"]) <= 120 for node in props.values())
