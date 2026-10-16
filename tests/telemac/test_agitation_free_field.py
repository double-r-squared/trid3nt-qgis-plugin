"""The harbour template with and without a structure.

Stated, the structure is punched out of the mesh, read along a transect and
reflects the coefficient stated with it; unstated, the basin is solved
free-field - no obstacle op, no transect, no reflecting face - and a
coefficient stated with no structure, or a structure with none, refuses."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.telemac.errors import TelemacError


def _workflow():
    from trid3nt_server.tools import TOOL_REGISTRY

    return TOOL_REGISTRY["artemis_harbor_agitation"].fn.workflow


_CLI = ("2 2 2 0.0 0.0 0.0 0.0 2 0.0 0.0 0.0 1 1\n"
        "2 2 2 0.0 0.0 0.0 0.0 2 0.0 0.0 0.0 2 2\n"
        "2 2 2 0.0 0.0 0.0 0.0 2 0.0 0.0 0.0 3 3\n")


def test_the_structure_and_its_reflection_are_stated_or_not():
    workflow = _workflow()
    for name in ("structure", "reflection_coef"):
        row = workflow.input(name)
        assert row.stated_only is True and row.required is False, name


def test_a_free_field_boundary_reflects_nothing():
    from trid3nt_server.workflows.telemac.modules.artemis import (
        KINC, KLOG, stamp_boundary_rows)

    rows = stamp_boundary_rows(_CLI, open_nodes=[0], structure_nodes=[],
                               height_m=2.0, reflection_coef=None).splitlines()
    kinds = [int(line.split()[0]) for line in rows]
    rp = [float(line.split()[6]) for line in rows]
    assert kinds == [KINC, KLOG, KLOG] and rp == [0.0, 0.0, 0.0]


def test_structure_faces_with_no_stated_reflection_refuse():
    from trid3nt_server.workflows.telemac.modules.artemis import stamp_boundary_rows

    with pytest.raises(ValueError, match="never a default"):
        stamp_boundary_rows(_CLI, open_nodes=[0], structure_nodes=[2],
                            height_m=2.0, reflection_coef=None)


@pytest.mark.parametrize(("structure", "reflection", "code"), [
    ([[0.0, 0.0], [0.001, 0.0]], None, "ARTEMIS_REFLECTION_UNSTATED"),
    (None, 0.5, "ARTEMIS_REFLECTION_UNPAIRED")])
def test_a_reflection_without_its_structure_or_the_reverse_refuses(
        structure, reflection, code):
    from trid3nt_server.workflows.telemac.authoring.walked_boundary import (
        settle_harbour)

    with pytest.raises(TelemacError) as caught:
        asyncio.run(settle_harbour(
            mesh={}, files={}, structure=structure, structure_width_m=20.0,
            wave_period_s=8.0, wave_height_m=1.0, wave_direction_deg=90.0,
            reflection_coef=reflection, result_basename="res.slf",
            deck="artemis_harbor_agitation", open_depth_threshold_m=-12.0))
    assert caught.value.error_code == code


def _recipe(untaken):
    from trid3nt_server.workflows.telemac.templates.agitation.agitation import MESH

    env = SimpleNamespace(data={}, run={
        "domain": "domain-uri", "mesh_resolution_m": None, "mesh_grade": 0.2,
        "bed": "bed-uri", "footprint": None if untaken else {"type": "x"},
        "open_depth_threshold_m": -12.0})
    return asyncio.run(_workflow()._recipe(env, MESH, untaken))


def test_an_unstated_structure_composes_no_obstacle_op():
    with_structure = [op["op"] for op in _recipe(set())["ops"]]
    free_field = [op["op"] for op in _recipe({"footprint"})["ops"]]
    assert "set_obstacle" in with_structure
    assert free_field == [fn for fn in with_structure if fn != "set_obstacle"]
