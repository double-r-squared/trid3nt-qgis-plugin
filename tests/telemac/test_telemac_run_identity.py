"""The two facts a run is recorded under: the ENGINE, and the MODULE of it that ran.

A question-named solver made every run of the family answer to one sibling's
name; one registration per engine plus the manifest's own ``case.module`` is what
replaced it. The provenance vocabulary is pinned here for the same reason: a
closed set is only closed if something reads it down. Offline - no solver."""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.telemac.modules.sheet import Origin

#: Every registered TELEMAC template, and the module its steering body fills.
TEMPLATE_MODULES: tuple[tuple[str, str], ...] = (
    ("telemac_river_dye", "telemac2d"),
    ("telemac_river_oil_spill", "telemac2d"),
    ("telemac_river_scour", "telemac2d"),
    ("telemac_river_sediment_plume", "telemac2d"),
    ("telemac_do_sag", "telemac2d"),
    ("telemac_rain_on_grid", "telemac2d"),
    ("telemac3d_stratified_flow", "telemac3d"),
    ("artemis_harbor_agitation", "artemis"),
)


def _steering(tool_name: str) -> type:
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY[tool_name].fn.workflow.plan_decl.steering


@pytest.mark.parametrize("tool_name,module", TEMPLATE_MODULES)
def test_every_template_is_values_over_a_module_of_the_one_engine(tool_name, module):
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY[tool_name].fn.workflow
    assert workflow.engine == "telemac"
    assert _steering(tool_name).MODULE == module


def test_the_vocabulary_is_the_six_the_card_renders():
    assert [origin.value for origin in Origin] == [
        "template", "user", "model", "producer", "derived", "calibrated"]


@pytest.mark.parametrize("tool_name,_module", TEMPLATE_MODULES)
def test_every_slot_of_a_template_s_default_fill_carries_one_of_the_six(
        tool_name, _module):
    """The default fill is what the body states before a run measures anything:
    the values that stand, and the assertions still waiting on a read. A writer
    that invented a seventh word is caught here, not on a card nobody can read."""
    from trid3nt_server.workflows.telemac.modules.sheet import _standing

    _body, standing, pending = _standing(_steering(tool_name))
    assert standing or pending, tool_name
    for name, row in standing.items():
        assert isinstance(row.provenance.origin, Origin), f"{tool_name}.{name}"
    for name, (_value, provenance) in pending.items():
        assert isinstance(provenance.origin, Origin), f"{tool_name}.{name}"


@pytest.mark.parametrize("tool_name,_module", TEMPLATE_MODULES)
def test_a_card_names_the_origin_of_every_set_row_and_of_no_other(
        tool_name, _module):
    """The chip is rendered off the sheet: a SET row always has one, and an open
    or engine-default row has none to claim."""
    from trid3nt_server.workflows.telemac.modules.sheet import Sheet, _standing
    from trid3nt_server.workflows.telemac.workflow import card_rows

    body = _steering(tool_name)
    _body, standing, _pending = _standing(body)
    for row in card_rows(Sheet(body=body, filled=standing)):
        expected = row.name in standing
        assert (row.origin in {origin.value for origin in Origin}) is expected, \
            row.name


def test_the_run_record_carries_the_engine_and_the_module_and_no_template():
    """A run is recorded under the engine and the module that ran it. The template
    is not a field of the line; it survives as the provenance of the slots it
    filled, which is how a reader - and the template pages - find its runs."""
    from trid3nt_server.workflows.runtime import journal

    record = journal.build_record(
        run_id="RUN1", engine="telemac", module="telemac2d",
        fill={"DURATION": "template: telemac_river_dye",
              "GEOMETRY FILE": "producer: mesh"},
        sheet=(), answer={}, provenance=(), result=None,
        wall_seconds=1.0, origin="session", executed=(), replayed=(), notes=())
    assert record["engine"] == "telemac" and record["module"] == "telemac2d"
    assert "template" not in record
    assert "template: telemac_river_dye" in record["fill"].values()


def test_the_module_and_the_fill_are_read_off_the_solve_step_the_workflow_declares():
    from types import SimpleNamespace

    from trid3nt_server.workflows.telemac.workflow import TelemacWorkflow

    run = SimpleNamespace(results={"solve": {
        "module": "artemis",
        "sheet": {"filled": {"DURATION": {"keyword": "DURATION", "value": 600.0,
                                          "provenance": "template: t"}}}}})
    assert TelemacWorkflow._module(TelemacWorkflow, run) == "artemis"
    assert TelemacWorkflow._fill(TelemacWorkflow, run) == {"DURATION": "template: t"}


@pytest.mark.parametrize("tool_name,_module", TEMPLATE_MODULES)
def test_a_template_s_own_name_is_what_its_slots_are_stamped_with(
        tool_name, _module):
    """The detail beside the ``template`` origin is the template, not the body
    class: eight templates share the class names, and a page that selects its own
    runs by the provenance needs the name that tells them apart."""
    from trid3nt_server.workflows.telemac.modules.sheet import Origin, _standing

    _body, standing, pending = _standing(_steering(tool_name), tool_name)
    stamped = [row.provenance for row in standing.values()]
    stamped += [provenance for _value, provenance in pending.values()]
    for provenance in stamped:
        if provenance.origin is Origin.TEMPLATE:
            assert str(provenance) == f"template: {tool_name}"
