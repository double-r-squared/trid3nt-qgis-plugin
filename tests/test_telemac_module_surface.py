"""The module surface: the catalog is the keyword table, and the wrapper opines.

Every refusal here is a refusal BY NAME at declaration or at fill, which is the
whole point of spelling keywords raw: a misspelling, a value of the wrong type or
one outside the dictionary's own choices is answered while a person can still
read what they asked for, instead of stopping inside DAMOCLES blaming a keyword
nobody wrote.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from trid3nt_server.workflows.runtime import Ref
from trid3nt_server.workflows.telemac.modules import (
    Sheet,
    SheetIncomplete,
    SlotRefused,
    T2D,
    draw,
    fill,
    load_catalog,
    run,
)
from trid3nt_server.workflows.telemac.modules.module import UNSET, Module

_EXPOSED = ("telemac2d", "telemac3d", "artemis", "waqtel", "gaia")


# -- the catalog IS the keyword table ---------------------------------------- #

def test_every_slot_carries_the_dictionary_s_own_name_and_help():
    for module in _EXPOSED:
        catalog = load_catalog(module)
        assert catalog, module
        for identifier, slot in catalog.items():
            assert slot.identifier == identifier
            assert slot.keyword.strip() == slot.keyword.strip().upper()
            assert slot.desc, f"{module}/{identifier} describes nothing"
            assert "\\" not in slot.desc


def test_the_identifier_is_the_keyword_and_nothing_invented():
    """A raw keyword and its identifier differ only where a character cannot be
    one. Where the mechanical spelling IS an identifier it is the identifier;
    where it is not - a keyword opening on a digit - the image's own map decides,
    which is why the map is read out of the image rather than guessed at here."""
    for module in _EXPOSED:
        for slot in load_catalog(module).values():
            mechanical = "".join(c if c.isalnum() else "_"
                                 for c in slot.keyword.strip())
            assert slot.identifier.isidentifier()
            if mechanical.isidentifier():
                assert slot.identifier == mechanical, slot.keyword


def test_the_engine_default_is_on_the_slot_or_the_slot_is_a_question():
    friction = T2D.slot("LAW_OF_BOTTOM_FRICTION")
    assert friction.engine_default is UNSET and friction.mandatory
    assert T2D.slot("TIDAL_FLATS").engine_default is True
    assert T2D.slot("INITIAL_CONDITIONS").engine_default == "ZERO ELEVATION"


def test_a_wrapper_asserts_nothing_and_has_no_hook_to():
    """Every EXPOSED wrapper, not one built for the test: a wrapper that gains
    composites and outputs is still the analog of the engine's own defaults, and
    the moment one of them carries a value of its own the law is gone."""
    from trid3nt_server.workflows.telemac.modules import WRAPPERS

    for wrapper in WRAPPERS.values():
        assert dict(wrapper.ASSERTED) == {}, wrapper.MODULE
        assert not hasattr(wrapper, "defaults")
    assert not hasattr(Module, "defaults")


def test_a_body_s_own_code_and_private_names_are_never_assertions():
    """A wrapper carries its coupled-body constructors and its own helpers. A
    keyword's value is DATA, and no dictionary spells a keyword with a leading
    underscore, so both are the body's rather than the module's."""
    class BODY(T2D):
        _MINE = "not a keyword"
        DURATION = 600.0

        @staticmethod
        def helper():
            return None

        @classmethod
        def other(cls):
            return None

    assert dict(BODY.ASSERTED) == {"DURATION": 600.0}


def test_a_wrapper_is_a_declaration_and_refuses_to_be_a_value():
    with pytest.raises(SlotRefused, match="not a value"):
        T2D()


def test_an_unexposed_module_refuses_naming_the_ones_there_are():
    with pytest.raises(SlotRefused, match="no catalog"):
        Module("nosuchmodule")


# -- refusals at declaration -------------------------------------------------- #

def _body(**namespace):
    return type("BODY", (T2D,), namespace)


def test_a_keyword_the_module_does_not_have_refuses_naming_the_nearest():
    with pytest.raises(SlotRefused, match="TIDAL_FLATS"):
        _body(TIDAL_FLAT=True)


def test_a_value_of_the_wrong_type_refuses_naming_the_type():
    with pytest.raises(SlotRefused, match="TIDAL FLATS is LOGICAL"):
        _body(TIDAL_FLATS=1)
    with pytest.raises(SlotRefused, match="DURATION is REAL"):
        _body(DURATION="3600")


def test_a_value_outside_the_choices_refuses_naming_the_choices():
    with pytest.raises(SlotRefused, match="STRICKLER"):
        _body(LAW_OF_BOTTOM_FRICTION=99)
    with pytest.raises(SlotRefused, match="SAINT-VENANT FE"):
        _body(EQUATIONS="NOT AN EQUATION")


def test_a_list_keyword_refuses_a_scalar_and_the_wrong_length():
    with pytest.raises(SlotRefused, match="takes a list"):
        _body(TYPE_OF_ADVECTION=3)
    with pytest.raises(SlotRefused, match="exactly 3 values"):
        _body(ORIGINAL_DATE_OF_TIME=[2024, 1])


def test_a_list_s_choices_are_left_to_the_engine_s_own_reader():
    """The dictionary spells a tracer choice T*, T1*, kSi; telapy's reader is
    what knows those, and the round trip is where a list is judged."""
    assert fill(T2D, PRESCRIBED_TRACERS_VALUES=[0.0, 9.0]).resolved()


def test_a_multi_select_keyword_is_one_value_the_engine_splits_itself():
    """The value is a separator-joined selection, so no choice names the whole
    of it - and the arity the dictionary declares is ONE, which is what the
    engine reads. Written as a list, five of the six variables are lost in
    silence: the engine takes the first and says nothing about the rest."""
    variables = T2D.slot("VARIABLES_FOR_GRAPHIC_PRINTOUTS")
    assert variables.multi_select and not variables.is_list
    assert fill(T2D, VARIABLES_FOR_GRAPHIC_PRINTOUTS="U,V,H,S,B,T1").resolved()
    with pytest.raises(SlotRefused, match="is STRING"):
        fill(T2D, VARIABLES_FOR_GRAPHIC_PRINTOUTS=["U", "V"])


# -- refusals at fill --------------------------------------------------------- #

def test_a_keyword_the_module_does_not_have_refuses_at_fill_too():
    with pytest.raises(SlotRefused, match="TITLE"):
        fill(T2D, TITEL="x")


def test_a_ref_that_names_nothing_refuses_rather_than_binding_to_none():
    with pytest.raises(SlotRefused, match="names neither a producer"):
        fill(T2D, GEOMETRY_FILE=Ref("mesh.geometry"))


def test_a_ref_reading_a_field_that_is_not_there_refuses():
    with pytest.raises(SlotRefused, match="carries no value"):
        fill(T2D, produced={"mesh": {}}, GEOMETRY_FILE=Ref("mesh.geometry"))


def test_a_fill_that_reads_itself_in_a_cycle_refuses_naming_it():
    with pytest.raises(SlotRefused, match="cycle"):
        fill(T2D, GEOMETRY_FILE=Ref("BOUNDARY_CONDITIONS_FILE"),
             BOUNDARY_CONDITIONS_FILE=Ref("GEOMETRY_FILE"))


# -- the sheet ---------------------------------------------------------------- #

def test_the_bare_sheet_asks_the_three_questions_the_engine_has_no_answer_for():
    assert [slot.keyword for slot in fill(T2D).open()] == [
        "GEOMETRY FILE", "BOUNDARY CONDITIONS FILE", "LAW OF BOTTOM FRICTION"]


def test_an_engine_default_is_never_written_into_the_deck():
    sheet = fill(T2D, DURATION=600.0)
    assert sheet.resolved() == (("DURATION", 600.0),)


def test_resolution_order_is_engine_then_shared_body_then_template_then_fill():
    class RIVER(T2D):
        LAW_OF_BOTTOM_FRICTION = 3
        TIDAL_FLATS = True

    class DYE(RIVER):
        LAW_OF_BOTTOM_FRICTION = 4

    rows = fill(DYE, TIDAL_FLATS=False).state()["filled"]
    assert rows["LAW_OF_BOTTOM_FRICTION"] == {
        "keyword": "LAW OF BOTTOM FRICTION", "value": 4, "provenance": "template"}
    assert rows["TIDAL_FLATS"] == {
        "keyword": "TIDAL FLATS", "value": False, "provenance": "fill"}


def test_an_inherited_slot_says_which_body_asserted_it():
    class RIVER(T2D):
        TIDAL_FLATS = True

    class DYE(RIVER):
        DURATION = 600.0

    rows = fill(DYE).state()["filled"]
    assert rows["TIDAL_FLATS"]["provenance"] == "shared body RIVER"
    assert rows["DURATION"]["provenance"] == "template"


def test_a_fill_is_repeatable_and_the_later_one_stands():
    sheet = fill(fill(T2D, DURATION=600.0), DURATION=1200.0)
    assert sheet.resolved() == (("DURATION", 1200.0),)


def test_a_late_bound_read_binds_to_the_producer_that_answers_it():
    class RIVER(T2D):
        GEOMETRY_FILE = Ref("mesh.geometry")

    sheet = fill(RIVER, produced={"mesh": {"geometry": "river.slf"}})
    assert sheet.state()["filled"]["GEOMETRY_FILE"]["value"] == "river.slf"


# -- composites --------------------------------------------------------------- #

def _sources(value):
    return ({"ABSCISSAE_OF_SOURCES": [r["x"] for r in value],
             "ORDINATES_OF_SOURCES": [r["y"] for r in value],
             "SOURCES_FILE": "river_sources.txt"},
            {"river_sources.txt": "T Q\n0 1\n"})


def test_a_composite_becomes_several_slots_and_the_file_they_name():
    module = Module("telemac2d")
    module.composites(releases=_sources)

    class DYE(module):
        releases = [{"x": 1.0, "y": 2.0}, {"x": 3.0, "y": 4.0}]

    state = fill(DYE).state()
    assert state["filled"]["ABSCISSAE_OF_SOURCES"] == {
        "keyword": "ABSCISSAE OF SOURCES", "value": [1.0, 3.0],
        "provenance": "producer releases"}
    assert state["files"] == ["river_sources.txt"]


def test_a_composite_lives_on_the_wrapper_and_may_not_shadow_a_keyword():
    module = Module("telemac2d")
    with pytest.raises(SlotRefused, match="may not shadow"):
        module.composites(DURATION=_sources)


def test_a_composite_the_wrapper_never_registered_refuses_by_name():
    with pytest.raises(SlotRefused, match="no keyword 'tides'"):
        fill(T2D, tides=[{"x": 1.0, "y": 2.0}])


# -- the canvas ask ----------------------------------------------------------- #

def test_a_drawn_point_is_a_fill(monkeypatch):
    from trid3nt_server.gates import draw_input

    module = Module("telemac2d")
    module.composites(release=lambda point: (
        {"ABSCISSAE_OF_SOURCES": [point[0]], "ORDINATES_OF_SOURCES": [point[1]]},
        {}))

    async def _drawn(*, tool_name, param, geometry, prompt):
        assert (tool_name, param, geometry) == ("telemac2d", "release", "point")
        return draw_input.DrawOutcome(value=(407561.831, 4483518.635))

    monkeypatch.setattr(draw_input, "gate_draw_input", _drawn)
    sheet = asyncio.run(draw(module, "release", prompt="where does it enter?"))
    assert sheet.state()["filled"]["ABSCISSAE_OF_SOURCES"]["value"] == [407561.831]


def test_a_canvas_that_answers_nothing_refuses_and_invents_nothing(monkeypatch):
    from trid3nt_server.gates import draw_input

    async def _declined(**_kwargs):
        return draw_input.DrawOutcome(reason="there is no live map session to draw on")

    monkeypatch.setattr(draw_input, "gate_draw_input", _declined)
    with pytest.raises(SlotRefused, match="no live map session"):
        asyncio.run(draw(T2D, "GEOMETRY_FILE"))


# -- run is held -------------------------------------------------------------- #

def test_run_refuses_an_incomplete_sheet_naming_what_is_open():
    async def _never(**_kwargs):
        raise AssertionError("nothing dispatches on an incomplete sheet")

    with pytest.raises(SheetIncomplete, match="LAW OF BOTTOM FRICTION"):
        asyncio.run(run(fill(T2D), dispatch=_never, mesh_inputs=(), outputs=(),
                        results=("r2d.slf",), prefix="telemac", server_facts={}))


def test_run_serializes_then_stages_then_dispatches(monkeypatch, tmp_path):
    from trid3nt_server.workflows.telemac.authoring import assembler, serializer

    order: list[str] = []

    def _serialize(sheet, rundir, *, steering=None):
        order.append("serialize")
        return {"steering": steering or "t2d.cas"}

    async def _stage(rundir, run_tag, **kwargs):
        order.append("stage")
        return {"run_tag": run_tag, "manifest_uri": "s3://b/m.json"}

    async def _dispatch(*, run, compute_class):
        order.append("dispatch")
        return {"run_id": "R", "uri": run["manifest_uri"]}

    monkeypatch.setattr(serializer, "serialize", _serialize)
    monkeypatch.setattr(assembler, "stage_run", _stage)
    monkeypatch.setattr(assembler, "new_rundir", lambda: ("TAG", tmp_path))

    sheet = fill(T2D, GEOMETRY_FILE="river.slf",
                 BOUNDARY_CONDITIONS_FILE="river.cli",
                 LAW_OF_BOTTOM_FRICTION=3)
    out = asyncio.run(run(sheet, dispatch=_dispatch, mesh_inputs=(),
                          outputs=("r2d.slf",), results=("r2d.slf",),
                          prefix="telemac", server_facts={},
                          steering="t2d_river.cas"))
    assert order == ["serialize", "stage", "dispatch"]
    assert out["run_id"] == "R"


# -- the shared-body law ------------------------------------------------------ #

def test_every_shared_body_has_at_least_two_extenders():
    """One extender folds back into its template; a body is created when a good
    portion is shared. The guard fires the moment a body gains its first file."""
    shared = (Path(__file__).resolve().parents[1] / "trid3nt_server" / "workflows"
              / "telemac" / "templates" / "shared")
    if not shared.is_dir():
        return
    for body in sorted(shared.glob("*.py")):
        if body.name == "__init__.py":
            continue
        name = body.stem
        extenders = [p for p in shared.parent.rglob("*.py")
                     if p != body and f"shared.{name} import" in p.read_text()]
        assert len(extenders) >= 2, f"{name} has {len(extenders)} extenders"


# -- the wrappers' own composites --------------------------------------------- #

def _one_release(**over):
    from trid3nt_server.workflows.telemac.modules.telemac2d import Release

    return Release(**{"at": (407561.831, 4483518.635), "q": 8.0,
                      "tracers": [100.0], "window_s": 120.0,
                      "until_s": 600.0, **over})


def test_a_release_becomes_the_source_keywords_and_the_series_they_name():
    slots, files = T2D.COMPOSITES["releases"].expand([_one_release()])
    assert dict(slots) == {
        "ABSCISSAE_OF_SOURCES": [407561.831],
        "ORDINATES_OF_SOURCES": [4483518.635],
        "WATER_DISCHARGE_OF_SOURCES": [8.0],
        "VALUES_OF_THE_TRACERS_AT_THE_SOURCES": [100.0],
        "SOURCES_FILE": "river_sources.txt"}
    assert files["river_sources.txt"].splitlines() == [
        "#", "T Q(1) TR(1,1)", "s m3/s mg/l",
        "0.000 8 100", "120.000 8 100", "120.100 0 0", "700.000 0 0"]


def test_the_source_allocation_is_the_engine_s_until_a_run_needs_more():
    """The dictionary already allows for twenty sources. Restating that number
    would put an opinion in the deck; exceeding it in silence would drop every
    source past it."""
    allowed = int(T2D.slot("MAXIMUM_NUMBER_OF_SOURCES").engine_default)
    few, _ = T2D.COMPOSITES["releases"].expand([_one_release()] * allowed)
    many, _ = T2D.COMPOSITES["releases"].expand([_one_release()] * (allowed + 1))
    assert "MAXIMUM_NUMBER_OF_SOURCES" not in few
    assert many["MAXIMUM_NUMBER_OF_SOURCES"] == allowed + 1


def test_two_releases_are_one_longer_list_in_one_order():
    slots, files = T2D.COMPOSITES["releases"].expand([
        _one_release(at=(1.0, 2.0), q=3.0, tracers=[10.0]),
        _one_release(at=(4.0, 5.0), q=6.0, tracers=[20.0], window_s=None)])
    assert slots["ABSCISSAE_OF_SOURCES"] == [1.0, 4.0]
    assert slots["ORDINATES_OF_SOURCES"] == [2.0, 5.0]
    assert slots["WATER_DISCHARGE_OF_SOURCES"] == [3.0, 6.0]
    assert slots["VALUES_OF_THE_TRACERS_AT_THE_SOURCES"] == [10.0, 20.0]
    rows = files["river_sources.txt"].splitlines()
    assert rows[1] == "T Q(1) Q(2) TR(1,1) TR(2,1)"
    # The second release never closes, so its columns hold past the first's step.
    assert rows[-1].split()[1:] == ["0", "6", "0", "20"]


def test_a_permitted_discharge_holds_flat_for_the_whole_run():
    """A permitted discharge does not pulse: two rows, the second past the last
    simulated instant, so the time interpolation never reads off the end."""
    _, files = T2D.COMPOSITES["releases"].expand(
        [_one_release(window_s=None, tracers=[0.0, 2.0, 250.0, 0.0])])
    rows = files["river_sources.txt"].splitlines()
    assert rows[1] == "T Q(1) TR(1,1) TR(1,2) TR(1,3) TR(1,4)"
    assert rows[3:] == ["0.000 8 0 2 250 0", "700.000 8 0 2 250 0"]


def test_a_wind_from_the_north_drives_the_water_south():
    from trid3nt_server.workflows.telemac.modules.telemac2d import Wind

    slots, _ = T2D.COMPOSITES["wind"].expand(Wind(speed_mps=4.0, from_deg=0.0))
    assert slots["WIND"] is True and slots["OPTION_FOR_WIND"] == 1
    assert round(slots["WIND_VELOCITY_ALONG_X"], 9) == 0.0
    assert round(slots["WIND_VELOCITY_ALONG_Y"], 9) == -4.0
    assert "COEFFICIENT_OF_WIND_INFLUENCE" not in slots
    east, _ = T2D.COMPOSITES["wind"].expand(Wind(speed_mps=4.0, from_deg=270.0))
    assert round(east["WIND_VELOCITY_ALONG_X"], 9) == 4.0


def test_a_continuation_reads_the_previous_file_at_the_precision_it_was_written():
    from trid3nt_server.workflows.telemac.modules.telemac2d import Continuation

    slots, _ = T2D.COMPOSITES["continue_from"].expand(
        Continuation(previous="previous.slf"))
    assert slots == {"PREVIOUS_COMPUTATION_FILE": "previous.slf",
                     "PREVIOUS_COMPUTATION_FILE_FORMAT": "SERAFIND"}


def test_rain_carries_one_value_per_tracer():
    """DAMOCLES requires a rainwater concentration per tracer, and rainwater
    carries none of them - which is the array a hand-written deck gets wrong the
    moment a coupling adds a tracer behind the dye."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import Rain

    slots, _ = T2D.COMPOSITES["rain"].expand(Rain(mm_per_day=3.5, tracers=4))
    assert slots["VALUES_OF_TRACERS_IN_THE_RAIN"] == [0.0, 0.0, 0.0, 0.0]
    assert "DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS" not in slots
    windowed, _ = T2D.COMPOSITES["rain"].expand(
        Rain(mm_per_day=156.72, tracers=0, hours=24.0))
    assert windowed["DURATION_OF_RAIN_OR_EVAPORATION_IN_HOURS"] == 24.0
    # A run with no tracers states no rainwater concentrations at all: an empty
    # list is a keyword with nothing after it, not the absence of one.
    assert "VALUES_OF_TRACERS_IN_THE_RAIN" not in windowed


def test_a_value_of_none_states_nothing_and_the_engine_default_stands():
    class QUIET(T2D):
        wind = None
        FRICTION_COEFFICIENT = None
        DURATION = 600.0

    state = fill(QUIET).state()
    assert list(state["filled"]) == ["DURATION"]
    assert fill(QUIET, DURATION=None).state()["filled"] == {}


# -- the coupled modules ------------------------------------------------------ #

def test_a_coupling_states_only_what_the_carrier_names_it_by():
    from trid3nt_server.workflows.telemac.modules import WAQTEL

    slots, files = T2D.COMPOSITES["coupling"].expand(
        [WAQTEL.decay(law=1, coefficient=2.0)])
    assert slots == {"COUPLING_WITH": "WAQTEL",
                     "WAQTEL_STEERING_FILE": "t2d_river.waqtel",
                     "WATER_QUALITY_PROCESS": 17}
    # The body's own slots are WAQTEL's, not the carrier's: they go to WAQTEL's
    # own deck, checked against WAQTEL's own dictionary.
    assert files["t2d_river.waqtel"]["module"] == "waqtel"
    assert dict(files["t2d_river.waqtel"]["slots"]) == {
        "LAW_OF_TRACERS_DEGRADATION": [1],
        "COEFFICIENT_1_FOR_LAW_OF_TRACERS_DEGRADATION": [2.0]}


def test_a_coupled_body_is_checked_against_its_own_module_s_dictionary():
    from trid3nt_server.workflows.telemac.modules import WAQTEL

    body = WAQTEL.o2(water_temp_c=20.0, k1_per_day=0.3, k2_per_day=0.9,
                     k2_formula=0, saturation_mgl=9.0)
    sheet = fill(WAQTEL, **dict(body["slots"]))
    assert dict(sheet.resolved())["O2 SATURATION DENSITY OF WATER (CS)"] == 9.0
    with pytest.raises(SlotRefused, match="is REAL"):
        fill(WAQTEL, WATER_TEMPERATURE="warm")


def test_the_gaia_classes_lists_are_four_of_one_length():
    """The four parallel CLASSES lists are written from ONE gradation, so they
    cannot disagree about how many classes there are."""
    from trid3nt_server.workflows.telemac.modules import GAIA

    body = GAIA.graded(geometry="river.slf", boundary="river.cli",
                       classes=[(63.0, 0.4), (200.0, 0.35), (600.0, 0.25)],
                       density=2650.0, thickness_m=5.0, formula=1,
                       morphological_factor=10.0)
    slots = dict(body["slots"])
    assert {len(slots[k]) for k in (
        "CLASSES_TYPE_OF_SEDIMENT", "CLASSES_SEDIMENT_DIAMETERS",
        "CLASSES_SEDIMENT_DENSITY", "CLASSES_INITIAL_FRACTION")} == {3}
    assert slots["CLASSES_SEDIMENT_DIAMETERS"] == pytest.approx(
        [63.0e-6, 200.0e-6, 600.0e-6])
    assert slots["HIDING_FACTOR_FORMULA"] == 1


def test_dredging_names_every_nestor_file_or_none_of_them():
    """NESTOR reads all three on every action, so a run naming two of them is a
    run it cannot read."""
    from trid3nt_server.workflows.telemac.modules import GAIA
    from trid3nt_server.workflows.telemac.modules.gaia import Dredging

    body = GAIA.erodible(geometry="river.slf", boundary="river.cli",
                         d50_um=200.0, density=2650.0, thickness_m=5.0,
                         formula=1, morphological_factor=10.0,
                         dredging=Dredging(action="A", polygon="P",
                                           surface_ref="R"))
    sheet = fill(GAIA, **dict(body["slots"]))
    written = dict(sheet.resolved())
    assert written["NESTOR"] is True
    assert sorted(sheet.files) == ["nestor.act", "nestor.pol", "nestor.ref"]
    plain = fill(GAIA, **dict(GAIA.erodible(
        geometry="river.slf", boundary="river.cli", d50_um=200.0,
        density=2650.0, thickness_m=5.0, formula=1,
        morphological_factor=10.0)["slots"]))
    assert "NESTOR" not in dict(plain.resolved())
    assert not plain.files


def test_every_wrapper_binds_its_own_outputs_and_claims_no_other_s():
    """WAQTEL writes no result file of its own - the oxygen field is a carrier
    tracer - so it binds nothing, which is the honest reading of a module that
    publishes nothing."""
    from trid3nt_server.workflows.telemac.modules import GAIA, WAQTEL

    assert sorted(T2D.OUTPUTS) == ["dissolved_oxygen", "dye", "flood_depth"]
    assert sorted(GAIA.OUTPUTS) == ["deposition", "mass_balance", "surface_d50"]
    assert not WAQTEL.OUTPUTS
    for wrapper in (T2D, GAIA, WAQTEL):
        assert all(callable(output.read) for output in wrapper.OUTPUTS.values())
