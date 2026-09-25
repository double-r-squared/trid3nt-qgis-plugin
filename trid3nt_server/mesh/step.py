"""The declared MESH step: a template's ``tool.build_mesh`` ask, built under the gate.

One step for every template, and the RECIPE travels WHOLE - its mesher, its kind,
its extent, its size word and every op in declared order. Nothing about the ask
is restated here, so a param or an op cannot go missing between the two."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.workflows.runtime import Step
from trid3nt_server.mesh.artifact import measured_min_edge_m
from trid3nt_server.mesh.tool import recipe_plan_value

logger = logging.getLogger("trid3nt_server.mesh.step")

__all__ = ["MeshStep", "build_declared_mesh", "mesh_record"]

_RUNNER = "trid3nt_server.mesh.step.build_declared_mesh"


class MeshStep:
    """The declared mesh build, as the step a plan puts before its author stage."""

    #: The label the mesh gate's card carries. It names the ASK - the mesh this
    #: run is about to solve on - never the template that demanded it.
    GATE_LABEL: str = "build_mesh"

    @staticmethod
    def build(*, mesh: Any, name: Any = None, supplied: Any = None,
              tool: Any = None) -> Step:
        """Build the declared mesh under the gate; ``supplied`` adopts a built one.

        ``name`` presents the session; a supply is checked against ``tool``'s row."""
        return Step(runner=_RUNNER, stage="mesh",
                    kwargs={"mesh": recipe_plan_value(mesh), "name": name,
                            "supplied": supplied, "tool": tool})


async def build_declared_mesh(*, mesh: dict[str, Any], name: Any = None,
                              supplied: Any = None,
                              tool: Any = None) -> dict[str, Any]:
    """The mesh a solve runs on -> the accepted mesh's record.

    A SUPPLIED mesh is adopted whole - never rebuilt, never re-gated."""
    import asyncio

    from trid3nt_server.render.pipeline_emitter import current_turn_case
    from trid3nt_server.mesh.gate import gate_mesh_build
    from trid3nt_server.mesh.session import MeshSession
    from trid3nt_server.mesh.tool import (
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
    await asyncio.to_thread(_mesh_coverage, recipe.extent, art)
    return mesh_record(art)


def _mesh_coverage(extent: Any, art: Any) -> None:
    """Say how much of the domain's own centerline the built cells actually hold.

    Only a domain whose producer MEASURED a centerline has one to measure
    against; a drawn outline carries none and nothing is said."""
    from trid3nt_server.inputs.geometry import covered_fraction
    from trid3nt_server.workflows.runtime import journal_note

    line = (getattr(extent, "companions", None) or {}).get("centerline")
    if not line or not art.display_uri:
        return
    try:
        measured = covered_fraction(line, art.display_uri,
                                    within_epsg=art.utm_epsg)
    except Exception as exc:  # noqa: BLE001 - an unmeasurable overlap is not a build fault
        logger.info("mesh coverage not measured (%s)", exc)
        return
    journal_note(measured["note"])


def mesh_record(art: Any) -> dict[str, Any]:
    """One accepted mesh, as every consumer of the mesh step reads it."""
    return {
        "artifact": art,
        "mesh_id": art.mesh_id,
        "engine_files": dict(art.engine_files or {}),
        "boundary_roles": dict(art.boundary_roles or {}),
        "display_uri": art.display_uri,
        "recipe_uri": art.recipe_uri,
        "node_count": art.node_count,
        "element_count": art.element_count,
        "min_edge_m": measured_min_edge_m(art),
        "provenance": dict(art.provenance or {}),
    }


def _session_name(name: Any, mesher: str) -> str:
    """What the gate card calls this mesh: the step's own name, else the mesher's.

    A mapping ``name`` is read for its ``name``/``slug`` key, never stringified."""
    if isinstance(name, dict):
        name = name.get("name") or name.get("slug")
    text = str(name or "").strip()
    return f"{text} mesh" if text else f"{mesher} mesh"
