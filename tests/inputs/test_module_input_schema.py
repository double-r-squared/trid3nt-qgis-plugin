"""Hydrate: a value's schema and prose arrive from the module input JSON before the body is declared.

The JSON is the schema's home; a template states bare declarations and the
registration takes the section on.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.errors import DeclarationError
from trid3nt_server.inputs.input import Input, declared, hydrate


def _source(tmp_path, template: str = "demo_flow", inputs=None,
            module: str = "demo"):
    directory = tmp_path / "module_input"
    directory.mkdir(exist_ok=True)
    (directory / f"{module}.json").write_text(json.dumps(
        {"module": module, "keywords": [],
         "inputs": {template: inputs or {}}}))
    return (directory, module), template


def test_no_source_passes_the_body_through(tmp_path):
    class Body:
        seed = Input()

    body = Body
    assert hydrate(body, None, "demo_flow") is body
    with pytest.raises(DeclarationError, match="declares no desc"):
        declared(body)


def test_a_values_schema_and_prose_arrive_and_the_class_carries_them(tmp_path):
    source, template = _source(tmp_path, inputs={"seed": {
        "desc": "where the run starts", "bounds": [0.0, 1.0],
        "units": "m", "default": 0.5, "type": "float"}})

    class Body:
        seed = Input()

    rows = hydrate(Body, source, template)
    seed = Body.seed
    assert seed in rows
    assert (seed.desc, seed.bounds, seed.units, seed.default, seed.type) == (
        "where the run starts", (0.0, 1.0), "m", 0.5, float)
    assert not seed.required
    assert declared(rows) == (seed,)


def test_the_declared_type_is_read_back_as_a_type(tmp_path):
    from trid3nt_server.inputs.point import Point

    source, template = _source(
        tmp_path, inputs={"at": {"desc": "d", "type": "Point"},
                          "series": {"desc": "d", "type": "list[float] | None"}})

    class Body:
        at = Input()
        series = Input()

    rows = {row.name: row for row in hydrate(Body, source, template)
            if isinstance(row, Input)}
    assert rows["at"].type is Point
    assert rows["series"].type == list[float] | None


def test_a_rows_name_in_the_section_refuses(tmp_path):
    source, template = _source(tmp_path, inputs={"line": {"desc": "d"}})

    class Body:
        line = Input().need(geometry="polyline")

    with pytest.raises(DeclarationError, match="which is a row"):
        hydrate(Body, source, template)


def test_a_field_outside_the_five_refuses(tmp_path):
    source, template = _source(tmp_path, inputs={"seed": {"absent_note": "x"}})

    class Body:
        seed = Input()

    with pytest.raises(DeclarationError, match="carries"):
        hydrate(Body, source, template)


def test_a_missing_module_input_file_refuses(tmp_path):
    directory = tmp_path / "empty"
    directory.mkdir()
    with pytest.raises(DeclarationError, match="does not exist"):
        hydrate(type("Body", (), {}), (directory, "demo"), "demo_flow")


def test_a_type_that_does_not_read_as_a_type_refuses(tmp_path):
    source, template = _source(tmp_path, inputs={"seed": {"desc": "d", "type": "Quux"}})

    class Body:
        seed = Input()

    with pytest.raises(DeclarationError, match="does not read as a type"):
        hydrate(Body, source, template)


def test_bounds_that_are_not_a_pair_refuse(tmp_path):
    source, template = _source(tmp_path, inputs={"seed": {"desc": "d", "bounds": [1, 2, 3]}})

    class Body:
        seed = Input()

    with pytest.raises(DeclarationError, match="not a pair"):
        hydrate(Body, source, template)


def test_a_section_naming_another_template_leaves_this_body_to_the_guard(tmp_path):
    source, template = _source(tmp_path, inputs={"other": {"desc": "d"}})

    class Body:
        seed = Input()

    with pytest.raises(DeclarationError, match="declares no desc"):
        declared(hydrate(Body, source, template))


def test_a_sequence_passes_its_merged_rows(tmp_path):
    source, template = _source(
        tmp_path, inputs={"seed": {"desc": "where it starts", "default": 2,
                                   "type": "int"}})

    rows = hydrate([Input(name="seed")], source, template)
    assert rows[0].desc == "where it starts" and rows[0].default == 2
