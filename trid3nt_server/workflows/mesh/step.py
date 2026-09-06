"""The declared MESH step: a template's ``tool.build_mesh`` ask, built under the gate.

One step for every template, because a mesh is a mesh: the RECIPE travels WHOLE -
its mesher, its kind, its extent, its size word and every op in declared order -
as the plain mapping the interpreter binds late-bound reads inside, a session
opens over it, and what comes back is the ACCEPTED topology. Nothing about the
ask is restated here, so a param or an op cannot go missing between the template
and the mesh.

Restated and not repeated: a per-domain wrapper around this would be a second
place a mesh gets built, and the mesh a human approved and the mesh a solver ran
on would be two objects that happen to agree.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m
from trid3nt_server.workflows.mesh.tool import recipe_plan_value

logger = logging.getLogger("trid3nt_server.workflows.mesh.step")

__all__ = ["MeshStep", "build_declared_mesh", "mesh_record"]

_RUNNER = "trid3nt_server.workflows.mesh.step.build_declared_mesh"


class MeshStep:
    """The declared mesh build, as the step a plan puts before its author stage."""

    #: The label the mesh gate's card carries. It names the ASK - the mesh this
    #: run is about to solve on - rather than whichever template demanded it,
    #: because the same gate presents the same mesh to every one of them.
    GATE_LABEL: str = "build_mesh"

    @staticmethod
    def build(*, mesh: Any, name: Any = None, supplied: Any = None,
              tool: Any = None) -> Step:
        """Build the template's declared mesh under the mesh gate, or adopt one.

        ``name`` is what the session is PRESENTED as and nothing more - which
        place the mesh at the gate belongs to is a step result rather than
        anything a frozen recipe can name. ``supplied`` is the slot a caller
        hands a built mesh in, and ``tool`` the registered name whose declared
        mesh row that supply is checked for membership against.
        """
        return Step(runner=_RUNNER, stage="mesh",
                    kwargs={"mesh": recipe_plan_value(mesh), "name": name,
                            "supplied": supplied, "tool": tool})


async def build_declared_mesh(*, mesh: dict[str, Any], name: Any = None,
                              supplied: Any = None,
                              tool: Any = None) -> dict[str, Any]:
    """The mesh a solve runs on -> the accepted mesh's record.

    The recipe is rebuilt exactly as the template declared it and a session opens
    over it: the mesh is built from the whole program, then presented at the mesh
    gate with its probes, its numbered ops and its editable layer, edited or
    reset if the user says so, and accepted. A ``reset`` therefore goes back to
    the declaration rather than past it.

    A mesh SUPPLIED on the run is adopted instead, whole: it was built and
    accepted already, so rebuilding it would be a second mesh and re-gating one
    nobody changed would ask the same question twice. It is checked for
    membership in the calling template's declared mesh row, so a supply outside
    that row refuses by name rather than answering a different question.
    """
    import asyncio

    from trid3nt_server.emission.pipeline_emitter import current_turn_case
    from trid3nt_server.workflows.mesh.gate import gate_mesh_build
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import (
        recipe_from_plan_value,
        supplied_mesh_artifact,
    )

    art = None
    if supplied:
        art = await asyncio.to_thread(supplied_mesh_artifact, supplied,
                                      tool_name=str(tool or ""))
    if art is not None:
        logger.info("mesh supplied: %s -> %d nodes / %d elements",
                    art.mesh_id, art.node_count, art.element_count)
        return mesh_record(art)
    recipe = recipe_from_plan_value(mesh)
    session = await asyncio.to_thread(
        MeshSession, recipe, case_id=current_turn_case(),
        name=_session_name(name, recipe.mesher))
    art = await gate_mesh_build(session, tool_name=MeshStep.GATE_LABEL)
    logger.info("mesh accepted: %s -> %d nodes / %d elements, min edge %s m",
                art.mesh_id, art.node_count, art.element_count,
                measured_min_edge_m(art))
    return mesh_record(art)


def mesh_record(art: Any) -> dict[str, Any]:
    """One accepted mesh, as every consumer of the mesh step reads it."""
    return {
        "artifact": art,
        "mesh_id": art.mesh_id,
        "slf_uri": art.slf_uri,
        "cli_uri": art.cli_uri,
        "topology_uri": art.topology_uri,
        "display_uri": art.display_uri,
        "recipe_uri": art.recipe_uri,
        "node_count": art.node_count,
        "element_count": art.element_count,
        "min_edge_m": measured_min_edge_m(art),
        "provenance": dict(art.provenance or {}),
    }


def _session_name(name: Any, mesher: str) -> str:
    """What the gate card calls this mesh: the step's own name, else the mesher's.

    A step result arrives as whatever its producer returned, so a mapping is read
    for the two keys a domain step names itself by rather than stringified whole.
    """
    if isinstance(name, dict):
        name = name.get("name") or name.get("slug")
    text = str(name or "").strip()
    return f"{text} mesh" if text else f"{mesher} mesh"
