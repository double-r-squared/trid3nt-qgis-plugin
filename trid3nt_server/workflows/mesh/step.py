"""The declared MESH step: a template's ``tool.build_mesh`` ask, built under the gate.

One step for every template, because a mesh is a mesh: the declaration travels
WHOLE - its mesher, its kind, every field the router checked and the edits the
template declared on it - as the plain mapping the interpreter binds late-bound
reads inside, a session opens over it, and what comes back is the ACCEPTED
topology. Nothing about the ask is restated here, so a knob or a declared edit
cannot go missing between the template and the mesh.

Restated and not repeated: a per-domain wrapper around this would be a second
place a mesh gets built, and the mesh a human approved and the mesh a solver ran
on would be two objects that happen to agree.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.mesh.tool import declaration_plan_value

logger = logging.getLogger("trid3nt_server.workflows.mesh.step")

__all__ = ["MeshStep", "build_declared_mesh"]

_RUNNER = "trid3nt_server.workflows.mesh.step.build_declared_mesh"


class MeshStep:
    """The declared mesh build, as the step a plan puts before its author stage."""

    #: The label the mesh gate's card carries. It names the ASK - the mesh this
    #: run is about to solve on - rather than whichever template demanded it,
    #: because the same gate presents the same mesh to every one of them.
    GATE_LABEL: str = "build_mesh"

    @staticmethod
    def build(*, mesh: Any, name: Any = None) -> Step:
        """Build the template's declared mesh under the mesh gate.

        ``name`` is what the session is PRESENTED as and nothing more - which
        place the mesh at the gate belongs to is a step result rather than
        anything a frozen declaration can name.
        """
        return Step(runner=_RUNNER, stage="mesh",
                    kwargs={"mesh": declaration_plan_value(mesh), "name": name})


async def build_declared_mesh(*, mesh: dict[str, Any],
                              name: Any = None) -> dict[str, Any]:
    """The mesh a solve runs on -> the accepted mesh's record.

    The declaration is rebuilt exactly as the template declared it and a session
    opens over it: the mesh is built, its declared edits prefixing the recipe,
    then presented at the mesh gate with its probes and its editable layer,
    edited or restarted if the user says so, and accepted. A ``restart``
    therefore truncates to the declared chain rather than past it.
    """
    import asyncio

    from trid3nt_server.emission.pipeline_emitter import current_turn_case
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m
    from trid3nt_server.workflows.mesh.gate import gate_mesh_build
    from trid3nt_server.workflows.mesh.session import MeshSession
    from trid3nt_server.workflows.mesh.tool import declaration_from_plan_value

    declaration = declaration_from_plan_value(mesh)
    session = await asyncio.to_thread(
        MeshSession, declaration, case_id=current_turn_case(),
        name=_session_name(name, declaration.spec.mesher))
    art = await gate_mesh_build(session, tool_name=MeshStep.GATE_LABEL)
    logger.info("mesh accepted: %s -> %d nodes / %d elements, min edge %s m",
                art.mesh_id, art.node_count, art.element_count,
                measured_min_edge_m(art))
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
