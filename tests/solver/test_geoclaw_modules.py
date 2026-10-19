"""The GeoClaw body carries its engine's own keyword surface.

A wrapper is values over the dictionary extracted from Clawpack's own Data
classes: a body stating an unknown keyword refuses at import by name, a
required keyword (the engine reads it unconditionally and the class gives it
no default) is listed as required, a record table takes its record fields,
and a plausibility bound refuses an out-of-range value. ASCII only."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.workflows.geoclaw.modules import GEO, wrapper_for
from trid3nt_server.workflows.geoclaw.modules.module import module_input_dir
from trid3nt_server.workflows.runtime.module import (
    UNSET, SlotRefused, load_module_input,
)

GROUPS = {"CLAW", "GEO", "GAUGE", "TOPO", "DTOPO", "AMR", "ADJOINT", "REGION",
          "FLAGREGION", "REFINEMENT", "FGOUT", "FGMAX", "QINIT", "SURGE",
          "FRICTION", "MULTILAYER"}


def _dictionary():
    return load_module_input("geoclaw", directory=module_input_dir())


def test_the_committed_dictionary_is_the_whole_rundata_surface():
    document = json.loads(
        (module_input_dir() / "geoclaw.json").read_text())
    assert document["module"] == "geoclaw"
    assert len(document["keywords"]) == 159
    assert GEO.MODULE == "geoclaw"
    assert len(GEO.MODULE_INPUT) == 159
    assert {identifier.split("_", 1)[0] for identifier in GEO.MODULE_INPUT} \
        == GROUPS


def test_a_body_stating_an_unknown_keyword_refuses_at_import_by_name():
    with pytest.raises(SlotRefused) as caught:
        class Misspelled(GEO):
            CLAW_TFINL = 32400.0

    assert "CLAW_TFINL" in str(caught.value)


def test_a_body_stating_a_dictionary_keyword_is_checked_and_asserted():
    class Stated(GEO):
        CLAW_TFINAL = 32400.0
        GEO_GRAVITY = 9.80665

    assert dict(Stated.ASSERTED) == {
        "CLAW_TFINAL": 32400.0, "GEO_GRAVITY": 9.80665}


def test_a_value_the_dictionary_does_not_take_refuses_by_name():
    with pytest.raises(SlotRefused) as caught:
        class Wrong(GEO):
            CLAW_NUM_OUTPUT_TIMES = "eighteen"

    assert "NUM_OUTPUT_TIMES is INTEGER" in str(caught.value)


def test_required_keywords_are_undefaulted_and_read_unconditionally():
    geoclaw = _dictionary()
    required = {identifier for identifier, slot in geoclaw.items()
                if slot.is_required}
    assert required == {"CLAW_TFINAL", "CLAW_NUM_OUTPUT_TIMES"}
    # An undefaulted keyword is open for the fill: the engine answers none of
    # them on its own, whatever its reader tolerates.
    for identifier in ("CLAW_TFINAL", "CLAW_NUM_OUTPUT_TIMES",
                       "REFINEMENT_SPEED_TOLERANCE", "SURGE_STORM_FILE",
                       "ADJOINT_T1"):
        assert geoclaw[identifier].is_open is True, identifier
    assert sum(slot.is_open for slot in geoclaw.values()) == 12


def test_a_dictionary_default_is_the_value_the_engine_will_use():
    geoclaw = _dictionary()
    assert geoclaw["GEO_GRAVITY"].engine_default == 9.8
    assert geoclaw["AMR_AMR_LEVELS_MAX"].engine_default == 1
    assert geoclaw["CLAW_TFINAL"].engine_default is UNSET


def test_an_array_keyword_takes_a_scalar_or_a_list():
    geoclaw = _dictionary()
    slot = geoclaw["REFINEMENT_SPEED_TOLERANCE"]
    assert slot.type == "ARRAY"
    assert slot.check(0.1) == 0.1
    assert slot.check([0.1, 0.2]) == [0.1, 0.2]
    split = geoclaw["CLAW_DIMENSIONAL_SPLIT"]
    assert split.type == "ARRAY"
    assert split.check(1) == 1
    assert split.check("unsplit") == "unsplit"
    with pytest.raises(SlotRefused) as caught:
        split.check(True)
    assert "CLAW_DIMENSIONAL_SPLIT" in str(caught.value)


def test_a_record_table_carries_its_record_fields():
    geoclaw = _dictionary()
    gauges = geoclaw["GAUGE_GAUGES"]
    assert gauges.type == "STRUCT"
    assert gauges.shape == "(gaugeno, x, y, t1, t2)"
    assert gauges.check([[42, -71.4, -33.0, 0.0, 32400.0]]) \
        == [[42, -71.4, -33.0, 0.0, 32400.0]]
    assert geoclaw["TOPO_TOPOFILES"].shape == "(topo_type, filename)"
    assert geoclaw["FRICTION_FRICTION_REGIONS"].shape \
        == "(lower, upper, depths, manning_coefficients)"


def test_choices_are_the_spellings_the_writer_validates():
    geoclaw = _dictionary()
    assert geoclaw["CLAW_OUTPUT_STYLE"].check(1) == 1
    with pytest.raises(SlotRefused) as caught:
        geoclaw["CLAW_OUTPUT_STYLE"].check(4)
    assert "choices are 1, 2, 3" in str(caught.value)
    assert geoclaw["CLAW_DIMENSIONAL_SPLIT"].check("unsplit") == "unsplit"


def test_a_plausibility_bound_refuses_an_out_of_range_value():
    geoclaw = _dictionary()
    levels = geoclaw["AMR_AMR_LEVELS_MAX"]
    assert levels.check(3) == 3
    with pytest.raises(SlotRefused) as caught:
        levels.check(12)
    refused = str(caught.value)
    assert "AMR_AMR_LEVELS_MAX=12" in refused
    assert "outside the declared range" in refused
    assert levels.bounds == ((1.0, 10.0),)


def test_the_wrapper_registry_answers_the_exposed_module_and_refuses_another():
    assert wrapper_for("geoclaw") is GEO
    with pytest.raises(SlotRefused) as caught:
        wrapper_for("boussinesq")
    assert "boussinesq" in str(caught.value)


def test_a_body_over_the_dictionary_fills_like_a_modflow_body():
    class Chile(GEO):
        CLAW_NUM_DIM = 2
        CLAW_NUM_EQN = 3
        CLAW_TFINAL = 32400.0
        CLAW_NUM_OUTPUT_TIMES = 18
        GEO_GRAVITY = 9.8
        GEO_COORDINATE_SYSTEM = 2
        GEO_DRY_TOLERANCE = 1.0e-3
        GEO_SEA_LEVEL = 0.0
        AMR_AMR_LEVELS_MAX = 3
        TOPO_TOPOFILES = [[2, "etopo10min120W60W60S0S.asc"]]
        REFINEMENT_WAVE_TOLERANCE = 0.1

    assert dict(Chile.ASSERTED) == {
        "CLAW_NUM_DIM": 2, "CLAW_NUM_EQN": 3, "CLAW_TFINAL": 32400.0,
        "CLAW_NUM_OUTPUT_TIMES": 18, "GEO_GRAVITY": 9.8,
        "GEO_COORDINATE_SYSTEM": 2, "GEO_DRY_TOLERANCE": 1.0e-3,
        "GEO_SEA_LEVEL": 0.0, "AMR_AMR_LEVELS_MAX": 3,
        "TOPO_TOPOFILES": [[2, "etopo10min120W60W60S0S.asc"]],
        "REFINEMENT_WAVE_TOLERANCE": 0.1}
