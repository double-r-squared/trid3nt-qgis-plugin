"""The MESH recipe travels WHOLE from the template to the mesh it builds.

Offline. A template's mesh step carries the recipe - mesher, kind, extent, its one
size word and every op in declared order - rather than restating parts of it, so
the mapping round-trips back to a recipe equal to the frozen one, each op keeps
its own name and kwargs, and the ORDER is preserved, duplicates included."""

from __future__ import annotations

import importlib

import pytest

from trid3nt_server.workflows.mesh.tool import (
    mesh_op,
    recipe_from_plan_value,
    recipe_plan_value,
)


#: Every declared recipe in the tree, at the template that declares it; the
#: river templates each restate the reach recipe as their own value.
_TEMPLATES = (
    "trid3nt_server.workflows.telemac.templates.river_dye.river_dye",
    "trid3nt_server.workflows.telemac.templates.river_oil_spill.river_oil_spill",
    "trid3nt_server.workflows.telemac.templates.river_scour.river_scour",
    "trid3nt_server.workflows.telemac.templates.river_sediment_plume.river_sediment_plume",
    "trid3nt_server.workflows.telemac.templates.do_sag.do_sag",
    "trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid",
    "trid3nt_server.workflows.telemac.templates.agitation.agitation",
    "trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow",
)


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_the_mesh_step_carries_the_recipe_whole(dotted):
    """The step's ask names the mesher, the three agnostic params and the ops."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    ask = recipe_plan_value(declared)

    assert set(ask) == {"mesher", "kind", "extent", "resolution_m", "ops"}
    assert ask["mesher"] == declared.mesher
    assert ask["kind"] == declared.kind
    assert [entry["op"] for entry in ask["ops"]] == [op.fn for op in declared.ops]
    assert [set(entry["kwargs"]) for entry in ask["ops"]] == \
        [set(op.kwargs) for op in declared.ops]


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_the_recipe_a_step_rebuilds_equals_the_one_it_was_handed(dotted):
    """A round trip through the step's kwargs changes nothing about the ask."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    rebuilt = recipe_from_plan_value(recipe_plan_value(declared))

    assert rebuilt.mesher == declared.mesher
    assert rebuilt.kind == declared.kind
    # A late-bound read refuses comparison on purpose - it is a description of a
    # read, not the value - so what is checked is that the SAME description came
    # back rather than a value invented for it.
    assert rebuilt.extent is declared.extent
    assert rebuilt.resolution_m is declared.resolution_m
    assert [op.fn for op in rebuilt.ops] == [op.fn for op in declared.ops]
    for rebuilt_op, declared_op in zip(rebuilt.ops, declared.ops):
        assert list(rebuilt_op.kwargs) == list(declared_op.kwargs)
        for name in declared_op.kwargs:
            assert repr(rebuilt_op.kwargs[name]) == repr(declared_op.kwargs[name])


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_a_step_override_replaces_a_param_and_leaves_the_program_alone(dotted):
    """Overrides are for the params a STEP resolves; the ops are the template's."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    rebuilt = recipe_from_plan_value(recipe_plan_value(declared),
                                     extent=(-1.0, -1.0, 1.0, 1.0))

    assert rebuilt.extent == (-1.0, -1.0, 1.0, 1.0)
    assert [op.fn for op in rebuilt.ops] == [op.fn for op in declared.ops]


def test_two_entries_of_one_op_survive_the_trip_in_order():
    """Two distance-sizing lines refine two corridors, so both must arrive."""
    from trid3nt_server.workflows.telemac.templates.rain_on_grid import rain_on_grid as template

    declared = template.MESH.appending(
        mesh_op("distance_sizing_from_line_function", line_file="/tmp/second.geojson",
                rate=0.05))
    ask = recipe_plan_value(declared)
    sizing = [entry for entry in ask["ops"]
              if entry["op"] == "distance_sizing_from_line_function"]

    assert len(sizing) == 2
    assert sizing[-1]["kwargs"]["rate"] == 0.05
    rebuilt = recipe_from_plan_value(ask)
    assert [op.fn for op in rebuilt.ops] == [op.fn for op in declared.ops]
