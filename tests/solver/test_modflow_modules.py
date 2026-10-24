"""The MODFLOW bodies carry their engine's own keyword surface.

A wrapper is values over the dictionary the mf6 definition files publish: a
body stating an unknown keyword refuses at import by name, a required keyword
(the dictionary marks it not optional and gives it no default) is listed as
required, and ``describe_keywords`` reads these dictionaries the same way it
reads any other carried module. ASCII only."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.modflow.modules import GWF, GWE, GWT, wrapper_for
from trid3nt_server.workflows.modflow.modules.module import module_input_dir
from trid3nt_server.workflows.runtime.module import (
    UNSET, SlotRefused, load_module_input,
)


def _dictionary(module: str):
    return load_module_input(module, directory=module_input_dir())


def test_every_exposed_body_carries_a_committed_dictionary():
    exposed = {path.stem for path in module_input_dir().glob("*.json")}
    assert exposed == {"gwf", "gwt", "gwe"}
    assert {body.MODULE: len(body.MODULE_INPUT)
            for body in (GWF, GWT, GWE)} == {
                "gwf": 508, "gwt": 259, "gwe": 230}


def test_every_body_carries_the_simulation_s_tdis_and_ims():
    for body in (GWF, GWT, GWE):
        assert {"TDIS_NPER", "TDIS_PERIODDATA", "IMS_OUTER_DVCLOSE"} <= set(
            body.MODULE_INPUT)


def test_a_body_stating_an_unknown_keyword_refuses_at_import_by_name():
    with pytest.raises(SlotRefused) as caught:
        class Misspelled(GWF):
            NPF_ICELTYPE = 0

    assert "NPF_ICELTYPE" in str(caught.value)


def test_a_body_stating_a_dictionary_keyword_is_checked_and_asserted():
    class Stated(GWF):
        NPF_ICELLTYPE = -1
        TDIS_NPER = 12

    assert dict(Stated.ASSERTED) == {"NPF_ICELLTYPE": -1, "TDIS_NPER": 12}


def test_a_value_the_dictionary_does_not_take_refuses_by_name():
    with pytest.raises(SlotRefused) as caught:
        class Wrong(GWF):
            TDIS_NPER = "twelve"

    assert "NPER is INTEGER" in str(caught.value)


def test_required_keywords_are_the_ones_marked_not_optional_and_undefaulted():
    gwf = _dictionary("gwf")
    # The stress package's period table: not optional, no default - a run
    # without it does not start.
    assert gwf["WEL_STRESS_PERIOD_DATA"].is_required is True
    # A bare ``optional`` line is not optional: mf6 refuses a run without K or
    # ICELLTYPE, and a required entry's DFN default_value is flopy's illustration.
    for identifier in ("NPF_K", "NPF_ICELLTYPE", "IC_STRT", "STO_ICONVERT",
                       "DIS_NROW", "TDIS_NPER", "TDIS_PERIODDATA"):
        assert gwf[identifier].is_required is True, identifier
        assert gwf[identifier].is_open is True, identifier
    # An optional keyword keeps the engine's own value, and a required flag is
    # the package's own tag.
    for identifier in ("NPF_SAVE_FLOWS", "IMS_OUTER_HCLOSE",
                       "RCHA_READASARRAYS"):
        assert gwf[identifier].is_required is False, identifier
    # An entry whose own description says the engine runs without it stated is not required.
    for identifier in ("STO_SY", "LAK_NOUTLETS", "LAK_NTABLES", "EVT_NSEG", "EVTA_AUX"):
        assert gwf[identifier].is_required is False, identifier
    # An entry shaped by an optional dimension alone has nothing to state while the count is unstated.
    assert gwf["CSUB_STRESS_PERIOD_DATA"].is_required is False
    assert gwf["CSUB_NINTERBEDS"].is_required is True
    assert sum(slot.is_required for slot in gwf.values()) == 104


def test_a_dictionary_default_is_the_value_the_engine_will_use():
    gwf = _dictionary("gwf")
    assert gwf["RCHA_READASARRAYS"].engine_default is True
    assert gwf["NPF_K"].engine_default is UNSET


def test_an_undefaulted_keyword_is_open_for_the_fill_to_state():
    gwf = _dictionary("gwf")
    assert gwf["WEL_STRESS_PERIOD_DATA"].is_open is True
    assert gwf["DISV_NCPL"].is_open is True


def test_a_griddata_array_takes_a_constant_a_list_or_an_input_name():
    slot = _dictionary("gwf")["NPF_ICELLTYPE"]
    assert slot.type == "ARRAY"
    for value in (0, -1, [0] * 4, "icelltype_from_input"):
        assert slot.check(value) == value


def test_a_structured_block_takes_a_filename_a_table_or_a_mapping():
    slot = _dictionary("gwf")["WEL_STRESS_PERIOD_DATA"]
    assert slot.type == "STRUCT"
    for value in ("wel_period.txt", [[(0, 0, 0), -100.0]],
                  {"period": [(0, 0, 0), -100.0]}):
        assert slot.check(value) == value


def test_the_wrapper_registry_answers_an_exposed_body_and_refuses_another():
    assert wrapper_for("gwf") is GWF
    assert wrapper_for("gwt") is GWT
    assert wrapper_for("gwe") is GWE
    with pytest.raises(SlotRefused) as caught:
        wrapper_for("prt")
    assert "prt" in str(caught.value)


def test_every_body_rows_what_a_solved_run_writes():
    assert set(GWF.MODULE_OUTPUT) == {"HEAD", "SATURATION", "SPECIFIC_DISCHARGE", "COMPACTION", "SUBSIDENCE"}
    assert set(GWT.MODULE_OUTPUT) == {"CONCENTRATION"}
    assert set(GWE.MODULE_OUTPUT) == {"TEMPERATURE"}


def test_the_committed_rows_are_the_extractors_shape():
    for body in ("gwf", "gwt", "gwe"):
        document = json.loads(
            (module_input_dir() / f"{body}.json").read_text())
        assert document["module"] == body
        for row in document["keywords"]:
            assert {"keyword", "identifier", "type", "size", "unbounded",
                    "help", "rubrique", "is_file", "mandatory"} <= set(row)
            assert row["type"] in ("INTEGER", "REAL", "LOGICAL", "STRING",
                                   "ARRAY", "STRUCT")
            if "default" in row:
                assert row["mandatory"] is False
