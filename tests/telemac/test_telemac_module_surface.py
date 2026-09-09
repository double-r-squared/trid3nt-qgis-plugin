"""The module surface: the catalog is the keyword table, and the wrapper opines.

Every refusal here is a refusal BY NAME at declaration or at fill, which is the
whole point of spelling keywords raw: a misspelling, a value of the wrong type or
one outside the dictionary's own choices is answered while a person can still
read what they asked for, instead of stopping inside DAMOCLES blaming a keyword
nobody wrote.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

from trid3nt_server.workflows.runtime import ParamRef, Ref
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
    assert friction.engine_default is UNSET and friction.is_open
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
    with pytest.raises(SlotRefused, match="names no such field"):
        fill(T2D, produced={"mesh": {}}, GEOMETRY_FILE=Ref("mesh.geometry"))


def test_a_ref_reading_a_field_the_row_holds_as_nothing_states_nothing():
    """A row that HOLDS a field as nothing is answering: no wind was asked for,
    this run continues nothing. The composite reading it expands to no keyword at
    all, which is a different thing from naming a field nobody produced."""
    sheet = fill(T2D, produced={"mesh": {"geometry": None}},
                 GEOMETRY_FILE=Ref("mesh.geometry"))
    assert sheet.resolved() == ()


def test_a_fill_that_reads_itself_in_a_cycle_refuses_naming_it():
    with pytest.raises(SlotRefused, match="cycle"):
        fill(T2D, GEOMETRY_FILE=Ref("BOUNDARY_CONDITIONS_FILE"),
             BOUNDARY_CONDITIONS_FILE=Ref("GEOMETRY_FILE"))


# -- the sheet ---------------------------------------------------------------- #

def test_the_bare_sheet_opens_every_keyword_the_dictionary_answers_for_nobody():
    """The OPEN set is complete - lists included - because a list the dictionary
    writes no default for is an emptiness the engine substitutes something for,
    and the reader has to be able to see it. REQUIRED is the OBLIG files."""
    sheet = fill(T2D)
    assert len(sheet.open()) == 29
    opened = {slot.keyword for slot in sheet.open()}
    assert {"GEOMETRY FILE", "BOUNDARY CONDITIONS FILE", "LAW OF BOTTOM FRICTION",
            "NAMES OF TRACERS"} <= opened
    assert [slot.keyword for slot in sheet.required()] == [
        "GEOMETRY FILE", "BOUNDARY CONDITIONS FILE"]


def test_a_defaulted_oblig_file_is_not_a_question_the_sheet_asks():
    """The dictionary marks STEERING FILE, DICTIONARY and RESULTS FILE OBLIG and
    then answers all three itself, so none of them is open and none is required:
    a default IS an answer wherever it stands."""
    for identifier in ("STEERING_FILE", "DICTIONARY", "RESULTS_FILE"):
        slot = T2D.slot(identifier)
        assert slot.file_mandatory and not slot.is_open and not slot.is_required


def test_the_open_row_says_whether_the_run_cannot_begin_without_it():
    rows = {row["keyword"]: row["required"] for row in fill(T2D).state()["open"]}
    assert rows["GEOMETRY FILE"] is True
    assert rows["LAW OF BOTTOM FRICTION"] is False


def test_an_engine_default_is_never_written_into_the_deck():
    sheet = fill(T2D, DURATION=600.0)
    assert sheet.resolved() == (("DURATION", 600.0),)


def test_resolution_order_is_engine_then_the_parts_then_template_then_fill():
    class RIVER(T2D):
        LAW_OF_BOTTOM_FRICTION = 3
        TIDAL_FLATS = True

    class DYE(T2D):
        parts = [RIVER]
        LAW_OF_BOTTOM_FRICTION = 4

    rows = fill(DYE, TIDAL_FLATS=False).state()["filled"]
    assert rows["LAW_OF_BOTTOM_FRICTION"] == {
        "keyword": "LAW OF BOTTOM FRICTION", "value": 4, "provenance": "template"}
    assert rows["TIDAL_FLATS"] == {
        "keyword": "TIDAL FLATS", "value": False, "provenance": "fill"}


def test_a_composed_slot_says_which_part_asserted_it():
    class RIVER(T2D):
        TIDAL_FLATS = True

    class DYE(T2D):
        parts = [RIVER]
        DURATION = 600.0

    rows = fill(DYE).state()["filled"]
    assert rows["TIDAL_FLATS"]["provenance"] == "part RIVER"
    assert rows["DURATION"]["provenance"] == "template"


def test_a_value_the_run_measured_reads_as_derived_not_as_the_body_that_named_it():
    """A body states WHICH measurement a slot takes; the number is the accepted
    artifact's. Badging it template or part would say an author wrote a value
    nobody wrote down - the boundary walk, the normal depth, the CFL time step
    and the stage-discharge curve are all measured, and the card has to say so.
    A declared PARAM is not this: it is the invocation's own answer."""
    class RIVER(T2D):
        TIDAL_FLATS = Ref("settled.tidal_flats")

    class DYE(T2D):
        parts = [RIVER]
        TIME_STEP = Ref("settled.time_step_s")
        DURATION = ParamRef("sim_duration_s")
        SOLVER = 1

    rows = fill(DYE, produced={"settled": {"tidal_flats": True,
                                           "time_step_s": 2.5}},
                params={"sim_duration_s": 600.0}).state()["filled"]
    assert rows["TIDAL_FLATS"]["provenance"] == "derived"
    assert rows["TIME_STEP"]["provenance"] == "derived"
    assert rows["DURATION"]["provenance"] == "template"
    assert rows["SOLVER"]["provenance"] == "template"


def test_a_measured_value_a_fill_overrides_still_reads_as_the_users():
    """The user's own edit beats the measurement, and the badge says the user."""
    class DYE(T2D):
        TIME_STEP = Ref("settled.time_step_s")

    sheet = fill(DYE, produced={"settled": {"time_step_s": 2.5}})
    assert sheet.state()["filled"]["TIME_STEP"]["provenance"] == "derived"
    edited = fill(sheet, TIME_STEP=1.0).state()["filled"]["TIME_STEP"]
    assert edited["value"] == 1.0 and edited["provenance"] == "fill"


def test_the_parts_merge_in_the_listed_order():
    class RIVER(T2D):
        SOLVER = 1

    class TRACER(T2D):
        NUMBER_OF_TRACERS = 1

    class DYE(T2D):
        parts = [RIVER, TRACER]

    assert [p.__name__ for p in DYE.PARTS] == ["RIVER", "TRACER"]
    rows = fill(DYE).state()["filled"]
    assert rows["SOLVER"]["provenance"] == "part RIVER"
    assert rows["NUMBER_OF_TRACERS"]["provenance"] == "part TRACER"


def test_a_keyword_two_parts_both_set_refuses_unless_the_template_settles_it():
    class RIVER(T2D):
        SOLVER = 1

    class SURGE(T2D):
        SOLVER = 3

    with pytest.raises(SlotRefused) as caught:
        class BOTH(T2D):
            parts = [RIVER, SURGE]
    assert "SOLVER" in str(caught.value)

    class SETTLED(T2D):
        parts = [RIVER, SURGE]
        SOLVER = 2

    assert fill(SETTLED).state()["filled"]["SOLVER"]["value"] == 2


def test_a_body_is_reused_by_composition_and_never_by_extension():
    class RIVER(T2D):
        SOLVER = 1

    with pytest.raises(SlotRefused) as caught:
        class DYE(RIVER):
            DURATION = 600.0
    assert "parts = [RIVER]" in str(caught.value)


def test_a_body_is_static_and_no_fill_changes_what_it_asserts():
    """A body reads no resolved value: its assertions are fixed when the module
    is imported, so two fills of the same body start from the same statement."""
    class RIVER(T2D):
        SOLVER = 1
        DURATION = ParamRef("sim_duration_s")

    before = dict(RIVER.ASSERTED)
    fill(RIVER, params={"sim_duration_s": 600.0})
    fill(RIVER, params={"sim_duration_s": 7200.0}, SOLVER=3)
    assert dict(RIVER.ASSERTED) == before


def test_a_param_a_body_reads_binds_from_the_sheet_the_invocation_resolved():
    class RIVER(T2D):
        DURATION = ParamRef("sim_duration_s")
        VELOCITY_DIFFUSIVITY = ParamRef("velocity_diffusivity")

    sheet = fill(RIVER, params={"sim_duration_s": 600.0,
                                "velocity_diffusivity": None})
    # The unset param states NOTHING, so the dictionary's own diffusivity stands.
    assert sheet.resolved() == (("DURATION", 600.0),)


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


def test_a_composite_sets_only_what_its_value_s_presence_defines():
    """A composite expands a value into the keywords that value IS. A literal
    the value does not carry is an opinion, and the two literals a presence does
    define are the arming boolean its input implies and the name of the file the
    composite itself writes. Anything else - a choice among the alternatives the
    dictionary offers - is the template's to assert, where a person reads it."""
    modules = Path(
        "trid3nt_server/workflows/telemac/modules").resolve()
    found = []
    for module in _EXPOSED:
        source = (modules / f"{module}.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        catalog = load_catalog(module)
        constants = {
            node.targets[0].id: node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)}
        expanders = {
            keyword.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "composites"
            for keyword in node.keywords
            if isinstance(keyword.value, ast.Name)}
        for node in tree.body:
            if not (isinstance(node, ast.FunctionDef) and node.name in expanders):
                continue
            for mapping in ast.walk(node):
                if not isinstance(mapping, ast.Dict):
                    continue
                for key, value in zip(mapping.keys, mapping.values):
                    if not (isinstance(key, ast.Constant)
                            and isinstance(key.value, str)):
                        continue
                    slot = catalog.get(key.value)
                    if slot is None:
                        continue
                    if isinstance(value, ast.Constant):
                        literal = value.value
                    elif isinstance(value, ast.Name) and value.id in constants:
                        literal = constants[value.id]
                    elif (isinstance(value, ast.UnaryOp)
                          and isinstance(value.op, (ast.USub, ast.UAdd))
                          and isinstance(value.operand, ast.Constant)):
                        literal = (-value.operand.value if isinstance(value.op, ast.USub)
                                   else value.operand.value)
                    else:
                        continue
                    if slot.is_file or (slot.type == "LOGICAL"
                                        and isinstance(literal, bool)):
                        continue
                    found.append(f"{module}.py:{value.lineno} {node.name} sets "
                                 f"{slot.keyword} to {literal!r}")
    assert not found, (
        "a composite states a value its input does not carry; assert it on the "
        "template that wants it: " + "; ".join(found))


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

def test_run_refuses_an_incomplete_sheet_naming_the_required_file():
    async def _never(**_kwargs):
        raise AssertionError("nothing dispatches on an incomplete sheet")

    with pytest.raises(SheetIncomplete, match="GEOMETRY FILE"):
        asyncio.run(run(fill(T2D), dispatch=_never, mesh_inputs=(), outputs=(),
                        results=("r2d.slf",), prefix="telemac", server_facts={}))


def test_run_does_not_refuse_an_open_keyword_the_engine_may_yet_default():
    """LAW OF BOTTOM FRICTION has no engine default and is not an OBLIG file, so
    it is OPEN and never REQUIRED: which decks cannot run without it is LECDON's
    to say, from its own listing, by name."""
    sheet = fill(T2D, GEOMETRY_FILE="geo.slf", BOUNDARY_CONDITIONS_FILE="geo.cli")
    assert not sheet.required()
    assert "LAW_OF_BOTTOM_FRICTION" in {slot.identifier for slot in sheet.open()}


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

def test_every_shared_body_has_at_least_two_users():
    """One user folds back into its template; a body is created when a good
    portion is shared. The guard fires the moment a body gains its first file."""
    shared = (Path(__file__).resolve().parents[2] / "trid3nt_server" / "workflows"
              / "telemac" / "templates" / "shared")
    if not shared.is_dir():
        return
    for body in sorted(shared.glob("*.py")):
        if body.name == "__init__.py":
            continue
        name = body.stem
        users = [p for p in shared.parent.rglob("*.py")
                 if p != body and f"shared.{name} import" in p.read_text()]
        assert len(users) >= 2, f"{name} has {len(users)} users"


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
    # WIND arms what the value's presence implies; WHICH of the engine's three
    # wind options reads it is not this composite's to say, and the one a
    # constant speed and direction are read under is the dictionary's own.
    assert slots["WIND"] is True and "OPTION_FOR_WIND" not in slots
    assert round(slots["WIND_VELOCITY_ALONG_X"], 9) == 0.0
    assert round(slots["WIND_VELOCITY_ALONG_Y"], 9) == -4.0
    assert "COEFFICIENT_OF_WIND_INFLUENCE" not in slots
    east, _ = T2D.COMPOSITES["wind"].expand(Wind(speed_mps=4.0, from_deg=270.0))
    assert round(east["WIND_VELOCITY_ALONG_X"], 9) == 4.0


def test_a_continuation_names_the_file_and_leaves_its_format_to_the_template():
    """The file is what the value IS; the FORMAT it is read at is a choice among
    the three the dictionary offers, and river_dye states the double-precision
    one because a restart file is what it continues from."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import Continuation
    from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import (
        STEERING,
    )

    slots, _ = T2D.COMPOSITES["continue_from"].expand(
        Continuation(previous="previous.slf"))
    assert slots == {"PREVIOUS_COMPUTATION_FILE": "previous.slf"}
    assert STEERING.ASSERTED["PREVIOUS_COMPUTATION_FILE_FORMAT"] == "SERAFIND"


def test_a_curve_number_field_names_no_model_and_the_template_does():
    """The field is the SCS model's input, and four rainfall-runoff models read
    the same rain: which one runs is rain_on_grid's assertion, not the
    composite's."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import Runoff
    from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import (
        STEERING,
    )

    slots, _ = T2D.COMPOSITES["runoff"].expand(
        Runoff(node_xy=[(0.0, 0.0)], cn2=[74.0],
               antecedent_moisture=2, initial_abstraction=1))
    assert "RAINFALL_RUNOFF_MODEL" not in slots
    assert STEERING.ASSERTED["RAINFALL_RUNOFF_MODEL"] == 1


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


#: Two calls of every coupled body, with no argument value shared between them.
#: A slot that comes back the SAME from both is a value the caller did not hand
#: in, which is the wrapper speaking for itself.
_COUPLED_CALLS = {
    ("waqtel", "decay"): ({"law": 1, "coefficient": 0.2},
                          {"law": 2, "coefficient": 0.7}),
    ("waqtel", "o2"): (
        {"water_temp_c": 20.0, "salinity_ppt": 0.0, "k1_per_day": 0.3,
         "k4_per_day": 0.0, "k2_per_day": 0.9, "k2_formula": 0,
         "saturation_mgl": 9.0, "benthic_demand": 0.0, "photosynthesis_p": 0.0,
         "respiration_r": 0.0},
        {"water_temp_c": 11.5, "salinity_ppt": 33.0, "k1_per_day": 0.44,
         "k4_per_day": 0.35, "k2_per_day": 1.7, "k2_formula": 2,
         "saturation_mgl": 11.2, "benthic_demand": 0.1,
         "photosynthesis_p": 1.0, "respiration_r": 0.06}),
    ("gaia", "graded"): (
        {"geometry": "a.slf", "boundary": "a.cli",
         "classes": [(63.0, 0.4), (200.0, 0.6)], "density": 2650.0,
         "thickness_m": 5.0, "formula": 1, "hiding_factor_formula": 1,
         "morphological_factor": 10.0, "printouts": "B,E,D50",
         "mass_balance": True},
        {"geometry": "b.slf", "boundary": "b.cli",
         "classes": [(90.0, 0.5), (400.0, 0.3), (900.0, 0.2)],
         "density": 2400.0, "thickness_m": 1.5, "formula": 3,
         "hiding_factor_formula": 0, "morphological_factor": 4.0,
         "printouts": "B,E", "mass_balance": False}),
    ("gaia", "erodible"): (
        {"geometry": "a.slf", "boundary": "a.cli", "d50_um": 200.0,
         "density": 2650.0, "thickness_m": 5.0, "formula": 1,
         "morphological_factor": 10.0, "printouts": "B,E",
         "mass_balance": True},
        {"geometry": "b.slf", "boundary": "b.cli", "d50_um": 90.0,
         "density": 2400.0, "thickness_m": 1.5, "formula": 3,
         "morphological_factor": 4.0, "printouts": "B,E,D50",
         "mass_balance": False}),
    ("gaia", "suspended"): (
        {"geometry": "a.slf", "boundary": "a.cli", "d50_um": 30.0,
         "density": 2650.0, "concentration_kgm3": 0.25, "transport_formula": 3,
         "advection_scheme": [1], "printouts": "B,E", "mass_balance": True},
        {"geometry": "b.slf", "boundary": "b.cli", "d50_um": 12.0,
         "density": 2400.0, "concentration_kgm3": 0.75, "transport_formula": 2,
         "advection_scheme": [5], "printouts": "B,E,D50",
         "mass_balance": False}),
}

#: The two keywords a sediment body ARMS by being the body it is: a shape named
#: for bedload that did not arm bedload would be a shape that does nothing.
_TRANSPORT_ARMING = ("BED_LOAD_FOR_ALL_SANDS", "SUSPENSION_FOR_ALL_SANDS")


def test_a_coupled_body_states_only_what_its_caller_handed_it():
    """A coupled body is on the WRAPPER, so a constant inside one is a wrapper
    opinion that reaches every template naming the body - and ASSERTED, which is
    a class body, never sees it. Called twice with no argument in common, the
    only values allowed to repeat are the file the wrapper names, the transport
    mode the body IS, a slot the dictionary states no default for, and the
    dictionary's own default at this body's class count."""
    from trid3nt_server.workflows.telemac.modules import WRAPPERS

    opinions = []
    for (module, body), (first, second) in _COUPLED_CALLS.items():
        wrapper = WRAPPERS[module]
        catalog = load_catalog(module)
        one = dict(getattr(wrapper, body)(**first)["slots"])
        two = dict(getattr(wrapper, body)(**second)["slots"])
        shared = ({_hashable(v) for v in first.values()}
                  & {_hashable(v) for v in second.values()})
        assert not shared, f"{module}.{body} calls share {shared}"
        for identifier, value in one.items():
            slot = catalog.get(identifier)
            if slot is None or _hashable(value) != _hashable(two.get(identifier)):
                continue
            if slot.is_file or identifier in _TRANSPORT_ARMING:
                continue
            if slot.engine_default is UNSET or slot.engine_default is None:
                continue
            if (isinstance(value, list) and isinstance(slot.engine_default, list)
                    and value == list(slot.engine_default)[:len(value)]):
                continue
            opinions.append(f"{module}.{body} states {slot.keyword} = {value!r}")
    assert not opinions, (
        "a coupled body speaks for itself; hand the value in from the template "
        "that wants it: " + "; ".join(opinions))


def _hashable(value):
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    return value


def test_a_coupled_body_is_checked_against_its_own_module_s_dictionary():
    from trid3nt_server.workflows.telemac.modules import WAQTEL

    body = WAQTEL.o2(water_temp_c=20.0, salinity_ppt=0.0, k1_per_day=0.3,
                     k4_per_day=0.0, k2_per_day=0.9, k2_formula=0,
                     saturation_mgl=9.0, benthic_demand=0.0,
                     photosynthesis_p=0.0, respiration_r=0.0)
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
                       hiding_factor_formula=1, morphological_factor=10.0,
                       printouts="B,E,D50", mass_balance=True)
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
                         printouts="B,E", mass_balance=True,
                         dredging=Dredging(action="A", polygon="P",
                                           surface_ref="R"))
    sheet = fill(GAIA, **dict(body["slots"]))
    written = dict(sheet.resolved())
    assert written["NESTOR"] is True
    assert sorted(sheet.files) == ["nestor.act", "nestor.pol", "nestor.ref"]
    plain = fill(GAIA, **dict(GAIA.erodible(
        geometry="river.slf", boundary="river.cli", d50_um=200.0,
        density=2650.0, thickness_m=5.0, formula=1,
        morphological_factor=10.0, printouts="B,E",
        mass_balance=True)["slots"]))
    assert "NESTOR" not in dict(plain.resolved())
    assert not plain.files


def test_every_wrapper_binds_its_own_outputs_and_claims_no_other_s():
    """WAQTEL writes no result file of its own - the oxygen field is a carrier
    tracer - so it binds nothing, which is the honest reading of a module that
    publishes nothing."""
    from trid3nt_server.workflows.telemac.modules import GAIA, WAQTEL

    assert sorted(T2D.OUTPUTS) == [
        "dissolved_oxygen", "dye", "flood_depth", "oil_slick", "scour",
        "sediment_plume"]
    assert sorted(GAIA.OUTPUTS) == ["deposition", "mass_balance", "surface_d50"]
    assert not WAQTEL.OUTPUTS
    for wrapper in (T2D, GAIA, WAQTEL):
        assert all(callable(output.read) for output in wrapper.OUTPUTS.values())


# -- the flip: one template per question, and the door's own review ----------- #

#: The eight questions the surface answers. Six run on the fill/run door; the two
#: open-water fronts still declare a plan and are Stage 3's.
_FLIPPED = ("telemac_river_dye", "telemac_river_oil_spill", "telemac_river_scour",
            "telemac_river_sediment_plume", "telemac_do_sag",
            "telemac_rain_on_grid")


def _bodies():
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in _FLIPPED:
        plan = TOOL_REGISTRY[name].fn.workflow.plan
        fill_step = next(s for s in plan.declared() if s.label == "sheet")
        yield name, fill_step.kwargs["steering"]


def test_a_structural_fork_is_a_template_and_never_a_switch():
    """Four questions release something into the same reach and each fills
    DIFFERENT slots for it. The arity of the carrier's own tracer surface moves
    with the fork, which is why each body states it rather than a composite
    owning it out of sight."""
    from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries

    measured = {"inflow_q_m3s": 50.0, "outflow_stage_m": 97.8,
                "liquid_boundary_order": ["inflow", "outflow"],
                "liquid_boundary_prescribes": ["flowrate", "elevation"]}
    arity = {}
    for name, body in _bodies():
        stated = body.ASSERTED.get("boundaries")
        if stated is None:
            continue
        slots, _files = T2D.COMPOSITES["boundaries"].expand(
            Boundaries(measured=measured, tracers=[0.0] * len(stated["tracers"])))
        arity[name] = len(slots["PRESCRIBED_TRACERS_VALUES"])
    assert arity == {"telemac_river_dye": 2, "telemac_river_oil_spill": 2,
                     "telemac_river_scour": 2,
                     "telemac_river_sediment_plume": 4, "telemac_do_sag": 8}


def test_no_flipped_body_branches_on_anything():
    """A body is DATA. A template that switched on a resolved value would be two
    questions wearing one name, which is the fork this stage exists to end."""
    for _name, body in _bodies():
        for key, value in body.ASSERTED.items():
            assert not callable(value), f"{body.__name__}.{key} is code"


def test_the_review_is_the_doors_view_and_never_a_step_of_its_own():
    """The sheet is state; the door renders what fill returned and HOLDS. A gate
    in front of it would edit a sheet the door never reads."""
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in _FLIPPED:
        plan = TOOL_REGISTRY[name].fn.workflow.plan
        assert [s.label for s in plan.declared() if hasattr(s, "kind")] == [], name
        assert [s.label for s in plan.declared() if s.self_gating] == ["sheet"], name


def test_the_reach_body_is_written_at_the_derivation_it_was_solved_for():
    """ONE normal depth: the level the outflow is held to and the depth the run
    opens at are the same number, read off the same producer, so the initial free
    surface and the prescribed boundary agree by construction."""
    from trid3nt_server.workflows.telemac.templates.shared.river import RIVER

    assert RIVER.ASSERTED["INITIAL_DEPTH"] == Ref("settled.depth_m")
    assert RIVER.ASSERTED["FRICTION_COEFFICIENT"] == Ref("settled.friction_coefficient")
    for _name, body in _bodies():
        stated = body.ASSERTED.get("boundaries")
        if stated is not None:
            assert stated["measured"] == Ref("settled")


def test_the_basin_states_how_its_tracer_is_carried_and_under_what_ceiling():
    """The dictionary gives SCHEME FOR ADVECTION OF TRACERS no 3D default, so an
    unstated deck advects the temperature by whatever the VELOCITIES are advected
    by. The template states the scheme, the ceiling that scheme sub-iterates
    under, and the step - each from a param, so each is overridable on the sheet
    and each carries the basis a reader is owed."""
    from trid3nt_server.workflows.runtime import param_rows
    from trid3nt_server.workflows.telemac.modules import load_catalog
    from trid3nt_server.workflows.telemac.templates.stratified_flow.declarations import (
        PARAMS,
    )
    from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import (
        STEERING,
    )

    rows = {row.name: row for row in param_rows(PARAMS)}
    slot = load_catalog("telemac3d")["SCHEME_FOR_ADVECTION_OF_TRACERS"]
    assert slot.engine_default is UNSET
    scheme = rows["tracer_advection_scheme"].default
    # The NERD family, by the engine's own naming: telemac2d's TREATMENT OF
    # FLUXES AT THE BOUNDARIES help calls 13 and 14 "NERD", and 13 is what the
    # telemac2d dictionary defaults this same keyword to.
    assert str(scheme) in slot.choices
    assert scheme in (13, 14)
    asserted = STEERING.ASSERTED
    assert [r.name for r in asserted["SCHEME_FOR_ADVECTION_OF_TRACERS"]] == [
        "tracer_advection_scheme"]
    assert asserted[
        "MAXIMUM_NUMBER_OF_ITERATIONS_FOR_ADVECTION_SCHEMES"].name == \
        "max_advection_iterations"
    assert asserted["TIME_STEP"].path == "settled.time_step_s"
    for name in ("tracer_advection_scheme", "max_advection_iterations"):
        assert len(rows[name].desc) > 80, name
    assert rows["time_step_s"].derived_when_absent


def test_every_open_water_recipe_sizes_the_domain_rim_it_meshes():
    """Nothing else sizes the rim: every sizing function measures the SHORELINE,
    and an AOI's own box is not one, so an undeclared rim comes back an order of
    magnitude past the size word and the band where it meets the shoreline
    triangulates into slivers. The op runs after the sizing and before the
    gradation that grades the step in."""
    from trid3nt_server.workflows.mesh.tool import recipe_plan_value
    from trid3nt_server.workflows.telemac.templates.agitation.agitation import (
        MESH as HARBOUR,
    )
    from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import (
        MESH as BASIN,
    )

    for recipe in (HARBOUR, BASIN):
        ops = [op["op"] for op in recipe_plan_value(recipe)["ops"]]
        assert "set_rim_size" in ops
        sizing = [i for i, op in enumerate(ops)
                  if op.endswith("_sizing_function")]
        gradation = [i for i, op in enumerate(ops)
                     if op == "enforce_mesh_gradation"]
        rim = ops.index("set_rim_size")
        assert all(i < rim for i in sizing)
        assert all(i > rim for i in gradation)
    # No edge is stated on either, so both lock the rim at the recipe's own
    # size word rather than at a metre value one of them invented.
    for recipe in (HARBOUR, BASIN):
        entry = next(op for op in recipe_plan_value(recipe)["ops"]
                     if op["op"] == "set_rim_size")
        assert not entry["kwargs"]


def test_every_open_water_recipe_carries_the_boundary_cleaning_chain():
    """A domain cut from a shoreline can leave scraps that touch at points, and
    the library's own clean passes are what remove them before the boundary is
    walked. The reach recipe lists them; the two open-water recipes list the
    same four, in the same order."""
    from trid3nt_server.workflows.mesh.tool import recipe_plan_value
    from trid3nt_server.workflows.telemac.templates.agitation.agitation import (
        MESH as HARBOUR,
    )
    from trid3nt_server.workflows.telemac.templates.shared.river import MESH as REACH
    from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import (
        MESH as BASIN,
    )

    cleaning = ("delete_boundary_faces", "delete_faces_connected_to_one_face",
                "make_mesh_boundaries_traversable", "fix_mesh")
    for recipe in (REACH, HARBOUR, BASIN):
        ops = [op["op"] for op in recipe_plan_value(recipe)["ops"]]
        assert [op for op in ops if op in cleaning] == list(cleaning)


# -- the LLM surface: the raw floor, the docstring, the card ------------------ #

#: Every question on the surface. All eight fill and run through the door, so all
#: eight carry the floor, the sheet line and the card.
_TEMPLATES = ("telemac_river_dye", "telemac_river_oil_spill", "telemac_river_scour",
              "telemac_river_sediment_plume", "telemac_do_sag",
              "telemac_rain_on_grid", "artemis_harbor_agitation",
              "telemac3d_stratified_flow")


class _STEERING(T2D):
    """A body that states a friction law, so the floor has an opinion to beat."""

    LAW_OF_BOTTOM_FRICTION = 3
    FRICTION_COEFFICIENT = 0.03


def _filled(**keywords):
    """A sheet filled the way the door fills one: the body, then the raw floor."""
    stated = {_STEERING.identify(name): value for name, value in keywords.items()}
    return fill(_STEERING, **stated)


def test_a_raw_keyword_on_the_wire_fills_the_slot_it_names():
    """The dictionary's own spelling reaches the sheet, and the row says the fill
    put it there - not the template, which said something else about it."""
    sheet = _filled(**{"LAW OF BOTTOM FRICTION": 4})
    row = sheet.filled["LAW_OF_BOTTOM_FRICTION"]
    assert _STEERING.ASSERTED["LAW_OF_BOTTOM_FRICTION"] == 3
    assert row.value == 4
    assert row.provenance == "fill"
    assert ("LAW OF BOTTOM FRICTION", 4) in sheet.resolved()


def test_a_raw_keyword_the_module_does_not_have_refuses_naming_the_nearest():
    """Named back in the dictionary's own spelling, because that is the name the
    caller was reaching for."""
    with pytest.raises(SlotRefused) as caught:
        T2D.identify("LAW OF BOTOM FRICTION")
    assert "LAW OF BOTTOM FRICTION" in str(caught.value)


def test_a_raw_keyword_value_outside_the_choices_refuses_naming_the_choices():
    with pytest.raises(SlotRefused) as caught:
        _filled(**{"LAW OF BOTTOM FRICTION": 9})
    said = str(caught.value)
    assert "MANNING" in said and "STRICKLER" in said


def test_the_identifier_and_the_composite_name_reach_the_same_floor():
    """One floor, three spellings of a name: the dictionary's, the identifier the
    image spells it by, and a composite the wrapper registers."""
    assert T2D.identify("LAW OF BOTTOM FRICTION") == "LAW_OF_BOTTOM_FRICTION"
    assert T2D.identify("LAW_OF_BOTTOM_FRICTION") == "LAW_OF_BOTTOM_FRICTION"
    assert T2D.identify("releases") == "releases"


def test_two_releases_on_the_floor_are_two_sources_in_the_deck():
    """A longer list IS the second release: one order, four arrays, two columns
    of series - and nothing in the body changed to allow it."""
    release = {"q": 8.0, "tracers": [100.0], "window_s": 120.0, "until_s": 600.0}
    sheet = _filled(releases=[{**release, "at": [500.0, 1000.0]},
                              {**release, "at": [900.0, 1100.0]}])
    deck = dict(sheet.resolved())
    assert deck["ABSCISSAE OF SOURCES"] == [500.0, 900.0]
    assert deck["ORDINATES OF SOURCES"] == [1000.0, 1100.0]
    assert deck["WATER DISCHARGE OF SOURCES"] == [8.0, 8.0]
    assert deck["VALUES OF THE TRACERS AT THE SOURCES"] == [100.0, 100.0]
    series = sheet.files["river_sources.txt"]
    assert "Q(1) Q(2)" in series


def test_a_floor_that_is_not_a_mapping_refuses_by_name():
    """The floor is a mapping of keyword to value. Anything else is named as the
    argument it is, rather than raising out of the fill and blaming a step."""
    import asyncio

    from trid3nt_server.workflows.telemac.workflow import fill_sheet

    with pytest.raises(SlotRefused) as caught:
        asyncio.run(fill_sheet(steering=T2D, produced={}, params={}, slots={},
                               workflow="probe", title="", input_mode="auto",
                               keywords='{"LAW OF BOTTOM FRICTION": 4}'))
    assert "mapping" in str(caught.value) and "got str" in str(caught.value)


def test_every_template_wire_carries_the_raw_keyword_floor():
    """The floor is a CONTROL, on every wire, and it reaches the fill step."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime import RawKeywords

    for name in _TEMPLATES:
        entry = TOOL_REGISTRY[name]
        assert "keywords" in inspect.signature(entry.fn).parameters, name
        plan = entry.fn.workflow.plan
        fill_step = next(s for s in plan.declared() if s.label == "sheet")
        assert fill_step.kwargs["keywords"] is RawKeywords, name


def test_every_template_names_the_reader_its_own_question_needs():
    """The reader is an OUTPUTS binding a template names, never one publisher
    branching on a class string. Two questions that publish different fields -
    a dye, a slick, a scoured bed, a deposited plume - name different readers,
    and the runner each names is the module's own bound output."""
    from trid3nt_server.tools import TOOL_REGISTRY

    readers = {}
    for name in _TEMPLATES:
        door = TOOL_REGISTRY[name].fn.workflow.plan_decl
        readers[name] = door.read(None).runner
    assert len(set(readers.values())) == len(readers), readers
    bound = {f"{fn.__module__}.{fn.__name__}"
             for wrapper in (T2D,) for fn in
             (output.read for output in wrapper.OUTPUTS.values())}
    for name in ("telemac_river_dye", "telemac_river_oil_spill",
                 "telemac_river_scour", "telemac_river_sediment_plume",
                 "telemac_do_sag", "telemac_rain_on_grid"):
        assert readers[name] in bound, f"{name} reads {readers[name]}"


def test_the_fill_docstring_names_the_module_its_rubriques_and_its_open_slots():
    """The prose is READ OFF the declaration, so it cannot claim a surface the
    body does not state, and it names the two ways past what it lists."""
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in _TEMPLATES:
        entry = TOOL_REGISTRY[name]
        body = entry.fn.workflow.plan_decl.steering
        doc = entry.fn.__doc__
        assert f"Sheet: {body.MODULE}" in doc, name
        assert f"dictionary has {len(body.CATALOG)} keywords" in doc, name
        assert "Open mandatory slots:" in doc, name
        assert "describe_keywords" in doc and "keywords=" in doc, name
        stated = {n for part in (*body.PARTS, body) for n in part.ASSERTED}
        stated |= set(entry.fn.workflow.plan_decl.slots)
        for identifier in stated:
            slot = body.CATALOG.get(identifier)
            if slot is not None and slot.rubrique:
                assert slot.rubrique[0] in doc, f"{name}: {slot.keyword}"


def test_every_required_file_is_the_template_s_own_statement():
    """A slot the dictionary marks OBLIG is named in the BODY, never by a
    composite. A composite's expansion is not readable until a fill runs it, so a
    required file it filled would read as an open mandatory slot on every surface
    that shows the declaration - the docstring and the card both."""
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in _TEMPLATES:
        door = TOOL_REGISTRY[name].fn.workflow.plan_decl
        body = door.steering
        stated = {n for part in (*body.PARTS, body) for n in part.ASSERTED}
        stated |= set(door.slots)
        unstated = sorted(slot.keyword for n, slot in body.CATALOG.items()
                          if slot.is_required and n not in stated)
        assert not unstated, f"{name} leaves {unstated} to something else"


def test_the_artemis_forcing_composite_carries_the_file_and_not_its_name():
    """ARTEMIS reads its forcing out of the boundary file, so the composite
    stands for that file's ROWS; the keyword that names the file is stated beside
    the geometry it is the boundary of."""
    from trid3nt_server.workflows.telemac.modules.artemis import (
        ART, BOUNDARY_FILENAME, IncidentWave,
    )
    from trid3nt_server.workflows.telemac.templates.agitation.agitation import (
        STEERING as AGITATION,
    )

    slots, files = ART.COMPOSITES["incident_wave"].expand(IncidentWave(
        cli_text="1 1 1 0.0 0.0 0.0 0.0 lit 2 0.0 0.0 0.0 1 1\n",
        open_nodes=[1], structure_nodes=[], height_m=1.0, reflection_coef=0.3))
    assert slots == {}
    assert list(files) == [BOUNDARY_FILENAME]
    assert AGITATION.ASSERTED["BOUNDARY_CONDITIONS_FILE"] == BOUNDARY_FILENAME


def test_the_card_shows_what_is_set_and_open_and_folds_the_rest_by_rubrique():
    """Set slots and open mandatory ones are the review; the whole rest of the
    module is under advanced, grouped by the dictionary's own section and
    carrying the engine default it will otherwise run on."""
    from trid3nt_server.workflows.telemac.workflow import card_rows

    sheet = _filled(**{"LAW OF BOTTOM FRICTION": 4})
    rows = {row.name: row for row in card_rows(sheet)}
    assert len(rows) == len(T2D.CATALOG)
    assert not rows["LAW_OF_BOTTOM_FRICTION"].advanced
    assert rows["LAW_OF_BOTTOM_FRICTION"].source_badge == "fill"
    # The two OBLIG files this bare fill leaves open are shown, not folded.
    for identifier in ("GEOMETRY_FILE", "BOUNDARY_CONDITIONS_FILE"):
        assert not rows[identifier].advanced
        assert "required" in rows[identifier].source_badge
    folded = rows["MAXIMUM_NUMBER_OF_FRICTION_DOMAINS"]
    assert folded.advanced and folded.value == 10
    assert folded.source_badge == "engine default"
    assert folded.group == "HYDRO"
    # A section is contiguous: the fold is read down the dictionary's sections.
    groups = [row.group for row in card_rows(sheet) if row.advanced]
    assert len(set(groups)) == len([g for i, g in enumerate(groups)
                                    if i == 0 or g != groups[i - 1]])


def test_an_open_slot_under_the_fold_says_the_dictionary_answers_it_for_nobody():
    """An engine default that does not exist is not rendered as one: the row says
    the dictionary gives the keyword none, which is what makes a substitution
    the engine makes visible rather than assumed."""
    from trid3nt_server.workflows.telemac.workflow import card_rows

    sheet = fill(T2D)
    rows = {row.name: row for row in card_rows(sheet)}
    open_row = rows["NAMES_OF_TRACERS"]
    assert open_row.advanced and open_row.value is None
    assert open_row.source_badge == "open: the dictionary gives it no default"


# -- the steering format has ONE writer -------------------------------------- #


#: The two modules that write the steering format: the serializer resolves the
#: sheet into decks, and the in-image driver hands each keyword to telapy.
_STEERING_WRITERS = (
    "trid3nt_server/workflows/telemac/authoring/serializer.py",
    "trid3nt_server/workflows/mesh/meshers/drivers/telemac_cas_driver.py",
)


def _keyword_writer_lines(path: Path, keywords: set[str]) -> list[tuple[int, str]]:
    """Every string CONSTANT in ``path`` that spells a raw keyword and assigns it.

    A docstring is prose about a keyword and never a deck line, so the module,
    class and function docstrings are read past; what remains is a literal the
    module could write into a file.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prose = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                prose.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in prose:
            continue
        for match in _ASSIGNS_A_KEYWORD.finditer(node.value):
            if match.group("keyword") in keywords:
                found.append((node.lineno, match.group("keyword")))
    return found


#: A deck line: a raw keyword at the head of a line, then the format's own
#: assignment. This is the shape the hand-written author wrote and the shape a
#: second writer would take again.
_ASSIGNS_A_KEYWORD = re.compile(
    r"(?m)^\s*(?P<keyword>[A-Z][A-Z0-9 ,'()/.+\-]*[A-Z0-9)])\s*[:=]")


def test_the_serializer_is_the_only_module_that_writes_a_keyword_into_a_deck():
    """No module outside the two writers spells a keyword and assigns it.

    telapy writes the steering format and the serializer is the only thing that
    hands it a sheet. A keyword formatted into a string anywhere else is a second
    author of the format - which is what the surface replaced - and it is caught
    here by the dictionary's own names rather than by a list somebody maintains.
    """
    keywords = {slot.keyword for module in _EXPOSED + ("tomawac",)
                for slot in load_catalog(module).values()}
    offenders = {}
    for tree_root in (Path("trid3nt_server"), Path("workers")):
        for path in sorted(tree_root.rglob("*.py")):
            if path.as_posix() in _STEERING_WRITERS:
                continue
            lines = _keyword_writer_lines(path, keywords)
            if lines:
                offenders[path.as_posix()] = lines
    assert not offenders, (
        "a keyword is spelled and assigned outside the serializer and its "
        f"driver, which is a second writer of the steering format: {offenders}")
