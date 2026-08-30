"""The MESH declaration travels WHOLE from the template to the mesh it builds.

Offline. A template's mesh step carries the declaration - its mesher, its kind,
every field the router checked and the DECLARED edit chain in its order - rather
than restating parts of it, so a knob or an edit the template asked for cannot go
missing between the declaration and the mesh. What is pinned here:

  1. THE ASK IS ONE VALUE. Every template that declares a mesh step hands that
     step the whole declaration, and the mapping round-trips back to a
     declaration equal to the one the template froze.
  2. A DECLARED EDIT IS PART OF THE ASK. The edit a template declares on its mesh
     reaches the step's kwargs and rebuilds as the chain's PREFIX.
  3. A PATH THAT CANNOT HONOUR AN EDIT REFUSES BY NAME. The catchment generation
     runs its delineation strategy rather than a mesh session, so a declared edit
     there is refused rather than silently dropped.

That a declared prefix survives a restart while a gate-time edit does not is the
mesh session's own law, pinned on the session and at the gate.
"""

from __future__ import annotations

import importlib

import pytest

from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.tool import (
    declaration_from_plan_value,
    declaration_plan_value,
    tool,
)


# --------------------------------------------------------------------------- #
# 1. Every template's mesh step carries the whole ask.
# --------------------------------------------------------------------------- #
_TEMPLATES = (
    "trid3nt_server.workflows.telemac.river_dye.river_dye",
    "trid3nt_server.workflows.telemac.do_sag.do_sag",
    "trid3nt_server.workflows.telemac.rain_on_grid.rain_on_grid",
)


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_the_mesh_step_carries_the_declaration_whole(dotted):
    """The step's ask names the mesher, the router-checked fields and the chain."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    ask = declaration_plan_value(declared)

    assert set(ask) == {"mesher", "fields", "edits"}
    assert ask["mesher"] == declared.spec.mesher
    assert set(ask["fields"]) == set(declared.spec.fields)
    assert [e["action"] for e in ask["edits"]] == [e.action for e in declared.edits]


@pytest.mark.parametrize("dotted", _TEMPLATES)
def test_the_declaration_a_step_rebuilds_equals_the_one_it_was_handed(dotted):
    """A round trip through the step's kwargs changes nothing about the ask."""
    module = importlib.import_module(dotted)
    declared = module.MESH
    rebuilt = declaration_from_plan_value(declaration_plan_value(declared))

    assert rebuilt.spec.mesher == declared.spec.mesher
    assert dict(rebuilt.spec.fields) == dict(declared.spec.fields)
    assert [(e.action, dict(e.inputs)) for e in rebuilt.edits] == \
        [(e.action, dict(e.inputs)) for e in declared.edits]


# --------------------------------------------------------------------------- #
# 2. A declared edit is part of the ask.
# --------------------------------------------------------------------------- #
def test_a_declared_edit_survives_the_trip_the_template_sends_it_on():
    """The edit the template declared is in the ask the step is handed."""
    from trid3nt_server.workflows.telemac.river_dye import river_dye as template

    declared = template.MESH.edit("set_boundary", side="east")
    ask = declaration_plan_value(declared)

    assert ask["edits"] == [{"action": "set_boundary",
                             "inputs": {"side": "east", "type": "open",
                                        "depth_threshold": -50.0,
                                        "min_section_nodes": 10}}]
    rebuilt = declaration_from_plan_value(ask)
    assert [e.action for e in rebuilt.edits] == ["set_boundary"]
    assert rebuilt.spec.mesher == "om2d"


# --------------------------------------------------------------------------- #
# 3. A path with no chain to prefix refuses the edit rather than dropping it.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_catchment_path_refuses_a_declared_edit_by_name():
    from trid3nt_server.workflows.telemac.steps.rain_on_grid import (
        build_catchment_mesh,
    )

    declaration = tool.build_mesh(
        mesher="om2d", kind="unstructured_tri",
        extent=(-83.5, 35.0, -83.4, 35.09),
    ).edit("set_boundary", side="east")

    with pytest.raises(MeshToolError) as excinfo:
        await build_catchment_mesh(mesh=declaration_plan_value(declaration),
                                   supplied=None, bed_dem={}, rivers=None)
    assert excinfo.value.error_code == "MESH_DECLARED_EDIT_UNSUPPORTED"
    assert "set_boundary" in str(excinfo.value)
