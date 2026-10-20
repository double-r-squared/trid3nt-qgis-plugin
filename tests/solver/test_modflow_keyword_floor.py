"""The keyword floor over a MODFLOW run's bodies: a qualified name reaches its coupled body, a bare
name resolves the one body that spells it and two refuse naming both, the simulation's own TDIS/DISV
stay the carrier's, and a run statement layers over the template's statement of the same body."""

from __future__ import annotations

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.modflow.modules import GWF, GWT
from trid3nt_server.workflows.modflow.workflow import _floor
from trid3nt_server.workflows.runtime.module import SlotRefused
from trid3nt_server.workflows.runtime.sheet import fill


def _steering():
    """The plume's steering: a carrier that couples one transport body."""
    return TOOL_REGISTRY["modflow_plume"].fn.workflow.steering


class Paired(GWF):
    """A run coupling a transport body beside the flow, stated over literals."""

    Transport = {"slots": {"MST_POROSITY": 0.25, "ADV_SCHEME": "upstream"}}


def test_a_run_s_bodies_are_the_carrier_and_the_body_it_couples():
    """The bodies the floor resolves against: the steering, then each Transport it states;
    a steering that couples none runs the carrier alone."""
    from trid3nt_server.workflows.modflow.workflow import run_bodies

    assert [body.MODULE for body in run_bodies(_steering())] == ["gwf", "gwt"]

    class Solo(GWF):
        pass

    assert [body.MODULE for body in run_bodies(Solo)] == ["gwf"]


def test_a_qualified_keyword_is_routed_to_its_body():
    """A name a module word qualifies resolves on that body alone - never the
    carrier's dictionary, which does not know the qualifier."""
    own, coupled = _floor(_steering(), {"gwt: IMS_PRINT_OPTION": "ALL"})
    assert own == {}
    assert coupled == {"gwt": {"IMS_PRINT_OPTION": "ALL"}}


def test_a_bare_keyword_two_bodies_spell_names_the_body_it_belongs_to():
    """Two bodies spell it, so the bare name is not the carrier's to guess: the
    refusal names both spellings and the run reads them apart."""
    with pytest.raises(SlotRefused) as caught:
        _floor(_steering(), {"IMS_PRINT_OPTION": "ALL"})
    said = str(caught.value)
    assert "gwf: IMS_PRINT_OPTION" in said
    assert "gwt: IMS_PRINT_OPTION" in said


def test_a_bare_keyword_only_the_coupled_body_spells_resolves_there():
    """A bare name the carrier's dictionary does not have resolves where it is
    spelled: the transport body states it as its own."""
    own, coupled = _floor(_steering(), {"MST_POROSITY": 0.3})
    assert own == {}
    assert coupled == {"gwt": {"MST_POROSITY": 0.3}}


def test_the_simulation_s_own_clock_and_grid_resolve_the_carrier():
    """One TDIS and one staged DISV describe the whole simulation, so the family
    resolves the carrier's deck wherever it is spelled - bare or qualified."""
    own, coupled = _floor(_steering(), {"TDIS_NPER": 1})
    assert own == {"TDIS_NPER": 1} and coupled == {}
    own, coupled = _floor(_steering(), {"DISV_NLAY": 1})
    assert own == {"DISV_NLAY": 1} and coupled == {}


def test_an_unknown_body_word_names_the_bodies_this_run_runs():
    """A module word no body carries is not silently dropped: the refusal names
    the word and every body this run does run."""
    with pytest.raises(SlotRefused) as caught:
        _floor(_steering(), {"boussinesq: AMPLIFICATION": 1.0})
    said = str(caught.value)
    assert "names no body of this run" in said
    assert "gwf" in said and "gwt" in said


def test_the_lookup_reads_whichever_body_carries_the_name():
    """The plan-time contract answers a keyword the same way the floor routes it:
    a qualifier names its body, the carrier family stays carried, a bare shared name refuses."""
    workflow = TOOL_REGISTRY["modflow_plume"].fn.workflow
    qualified = workflow.input("gwt: IMS_PRINT_OPTION")
    assert qualified.body is GWT and qualified.name == "IMS_PRINT_OPTION"
    assert qualified.carried is False
    carrier = workflow.input("TDIS_NPER")
    assert carrier.body is workflow.steering and carrier.carried is True
    with pytest.raises(SlotRefused) as caught:
        workflow.input("IMS_PRINT_OPTION")
    said = str(caught.value)
    assert "gwf: IMS_PRINT_OPTION" in said and "gwt: IMS_PRINT_OPTION" in said


def test_a_run_statement_layers_over_the_template_s_statement_of_the_body():
    """The run's values land on the body the template also stated: the one key the
    run states wins and the template's other slots are kept, never replaced."""
    from trid3nt_server.workflows.modflow.workflow import _compose_coupled

    sheet = fill(Paired, produced={})
    _, coupled = _floor(Paired, {"gwt: MST_POROSITY": 0.3, "gwt: ADV_SCHEME": "tvd"})
    sheet = _compose_coupled(sheet, coupled, {})
    body = next(content for content in sheet.coupled
                if content["module"] == "gwt")
    assert body["slots"]["MST_POROSITY"] == 0.3
    assert body["slots"]["ADV_SCHEME"] == "tvd"
    assert sorted(body["stated"]) == ["ADV_SCHEME", "MST_POROSITY"]


def test_a_stress_statement_on_a_run_expands_like_the_template_s():
    """A stress period the run states on the coupled body is shaped by the wrapper's
    own composite: the package marked, the statement checked and placed later."""
    from trid3nt_server.workflows.modflow.workflow import _compose_coupled

    sheet = fill(Paired, produced={})
    _, coupled = _floor(Paired, {
        "gwt: SRC_STRESS_PERIOD_DATA": {
            "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
            "layer": 1, "rate": 1.0e-6}})
    sheet = _compose_coupled(sheet, coupled, {})
    body = next(content for content in sheet.coupled
                if content["module"] == "gwt")
    statement = body["slots"]["SRC_STRESS_PERIOD_DATA"]
    assert statement["stress"] == "SRC"
    assert statement["values"] == {"rate": 1.0e-6}
    assert statement["layer"] == 1


def test_a_qualified_simulation_own_keyword_refuses_at_the_carrier_s_composite():
    """A run may not restate the clock or grid onto a coupled body: the carrier's
    own composite refuses it there, naming the deck that owns the family."""
    from trid3nt_server.workflows.modflow.workflow import _compose_coupled

    sheet = fill(Paired, produced={})
    _, coupled = _floor(Paired, {"gwt: TDIS_NPER": 3})
    with pytest.raises(SlotRefused) as caught:
        _compose_coupled(sheet, coupled, {})
    assert "simulation's own clock" in str(caught.value)


def test_a_body_the_run_names_joins_its_bodies():
    """A qualifier may add the body it names where the carrier couples it, so the
    floor and the lookup have a body to resolve against before any fill; a bare
    name or an unknown module word adds nothing."""
    from trid3nt_server.workflows.modflow.workflow import run_bodies

    assert [body.MODULE for body in run_bodies(GWF)] == ["gwf"]
    assert [body.MODULE for body in run_bodies(
        GWF, ["gwt: MST_POROSITY", "ADV_SCHEME"])] == ["gwf", "gwt"]
    assert [body.MODULE for body in run_bodies(
        GWF, ["boussinesq: AMPLIFICATION"])] == ["gwf"]


def test_a_run_qualified_body_couples_where_the_template_stated_none():
    """No Transport on the template is not a wall: the run's own qualified keywords
    create the body through the same composite, carrying only what the run stated."""
    from trid3nt_server.workflows.modflow.workflow import _compose_coupled

    sheet = fill(GWF, produced={})
    assert sheet.coupled == ()
    _, coupled = _floor(GWF, {"gwt: MST_POROSITY": 0.3, "gwt: ADV_SCHEME": "tvd"})
    sheet = _compose_coupled(sheet, coupled, {})
    [body] = sheet.coupled
    assert body["module"] == "gwt"
    assert body["slots"] == {"MST_POROSITY": 0.3, "ADV_SCHEME": "tvd"}
    assert body["stated"] == ["ADV_SCHEME", "MST_POROSITY"]


def test_the_lookup_accepts_a_body_the_run_adds():
    """Plan-time reads a qualifier's body into the run too, so the answer a lookup
    gives matches what the fill will couple."""
    workflow = TOOL_REGISTRY["modflow_water_table"].fn.workflow
    keyword = workflow.input("gwt: MST_POROSITY")
    assert keyword.body is GWT and keyword.name == "MST_POROSITY"
    assert keyword.carried is False
