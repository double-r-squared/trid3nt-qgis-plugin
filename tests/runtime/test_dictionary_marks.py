"""The dictionary marks a slot is required by: an engine-neutral ``mandatory``
flag beside the TELEMAC file mark, and the ARRAY and STRUCT type words a
grid-shaped or block-shaped value is checked against. ASCII only."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.runtime.module import Slot, SlotRefused


def _slot(**over) -> Slot:
    row = {"keyword": "A KEYWORD", "identifier": "A_KEYWORD", "type": "REAL",
           "size": 1, "unbounded": False, "desc": "", "rubrique": ("A",),
           "level": 0, "is_file": False}
    row.update(over)
    return Slot(**row)


def test_an_undefaulted_mandatory_keyword_is_required():
    assert _slot(mandatory=True).is_required is True


def test_a_mandatory_keyword_the_dictionary_defaults_is_not_required():
    assert _slot(mandatory=True, engine_default=0).is_required is False


def test_an_open_keyword_nothing_marks_is_not_required():
    assert _slot().is_required is False


def test_an_oblig_file_keeps_its_requirement():
    assert _slot(is_file=True, file_mandatory=True).is_required is True
    assert _slot(is_file=True, file_mandatory=True,
                 mandatory=False).is_required is True


def test_an_array_takes_a_constant_a_list_or_an_input_name():
    slot = _slot(type="ARRAY")
    assert slot.check(1.5) == 1.5
    assert slot.check([1.0, 2.0]) == [1.0, 2.0]
    assert slot.check("an_input") == "an_input"


def test_an_array_is_not_a_boolean():
    with pytest.raises(SlotRefused):
        _slot(type="ARRAY").check(True)


def test_a_struct_takes_a_filename_a_table_or_a_mapping():
    slot = _slot(type="STRUCT")
    for value in ("block.txt", [(1, 2.0)], {"a": 1}):
        assert slot.check(value) == value


def test_a_struct_is_not_a_boolean_or_a_number():
    with pytest.raises(SlotRefused):
        _slot(type="STRUCT").check(True)
    with pytest.raises(SlotRefused):
        _slot(type="STRUCT").check(1.5)
