"""The mesh under construction, presented on the one gate.

The mesh shows three faces of itself - the editable MDAL layer on the map, the
probes it is judged on, and the RECIPE with its ops NUMBERED - and the gate that
presents every other user-gated thing asks about them. AUTO builds inline."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping

from trid3nt_server.gates.input_review import GateCard, gate_input_review
from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.session import MeshSession

logger = logging.getLogger("trid3nt_server.workflows.mesh.gate")

__all__ = [
    "active_mesh_session",
    "gate_mesh_build",
    "present_mesh",
    "render_probe_lines",
]

#: Gate wait cap, in seconds.
_TTL_SECONDS = 300

#: How many times the gate re-presents before it stops asking.
_MAX_ROUNDS = 3

#: The mesh at the gate. One mesh is under construction at a time, and a runtime
#: op edits THAT one; with none open an op refuses by name rather than editing a
#: session nothing is watching.
_AT_GATE: MeshSession | None = None


def active_mesh_session(mesh_id: str | None = None) -> MeshSession:
    """The session a runtime recipe edit acts on, or a typed refusal.

    An unnamed edit means the mesh on screen; ``mesh_id`` names the one it must be."""
    session = _AT_GATE
    if session is None:
        raise MeshToolError(
            "MESH_NO_ACTIVE_SESSION",
            "no mesh is open at the gate, so there is no recipe to edit; build "
            "one with build_mesh(input_mode='user_gated') first.")
    if mesh_id and str(mesh_id) != session.mesh_id:
        raise MeshToolError(
            "MESH_SESSION_CLOSED",
            f"the mesh session {mesh_id!r} is closed, so it is not what an edit "
            f"acts on; the mesh at the gate is {session.mesh_id!r}.")
    return session


async def present_mesh(session: MeshSession) -> dict[str, Any]:
    """Put the mesh on the map and read it -> the layer, the probes, the recipe.

    The display face is an MDAL mesh layer, which is what makes it editable."""
    from trid3nt_server.render.layer_uri_emit import publish_input_layer
    from trid3nt_server.render.pipeline_emitter import current_emitter

    layer = await asyncio.to_thread(session.snapshot)
    emitter = current_emitter()
    await publish_input_layer(emitter, layer)
    if emitter is not None and layer.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(layer.bbox)})
        except Exception as exc:  # noqa: BLE001 -- the zoom is a nicety
            logger.warning("mesh gate zoom-to failed: %s", exc)
    return {
        "mesh_id": session.mesh_id,
        "mesher": session.mesher.name,
        "layer_id": layer.layer_id,
        "display_uri": layer.uri,
        "probes": session.probes(),
        "recipe": session.recipe.to_json(),
        "ops": session.recipe.numbered(),
        **({"regen_note": session.regen_note} if session.regen_note else {}),
    }


def render_probe_lines(probes: Mapping[str, Any]) -> list[str]:
    """The probe readout the gate card quotes, one measured fact per line."""
    lines = [f"{probes.get('node_count', 0)} nodes / "
             f"{probes.get('element_count', 0)} elements, "
             f"{probes.get('crs_authid', '?')}",
             ("bed painted" if probes.get("has_bed") else "NO bed painted")]
    edges = probes.get("edge_length_m") or {}
    if edges:
        lines.append(f"edge length {edges.get('min', 0.0):.1f} - "
                     f"{edges.get('max', 0.0):.1f} m (mean "
                     f"{edges.get('mean', 0.0):.1f} m)")
    if probes.get("min_angle_deg") is not None:
        lines.append(f"min angle {float(probes['min_angle_deg']):.1f} deg")
    if probes.get("boundary_edges") is not None:
        lines.append(f"{probes['boundary_edges']} boundary edges in "
                     f"{probes.get('boundary_loops', 0)} loop(s)")
    if probes.get("cells_realized_by_engine"):
        lines.append("cells are realized by the engine from the staged "
                     "authoring inputs, so no edge or angle was measured here")
    return lines


#: The cancel a gate reports -> the mesh error a refused build raises.
_REFUSALS = {
    "timeout": "MESH_GATE_TIMEOUT",
    "declined": "MESH_GATE_DECLINED",
    "not_approved": "MESH_GATE_NOT_APPROVED",
}


async def gate_mesh_build(session: MeshSession, *, tool_name: str,
                          input_mode: str | None = None,
                          max_rounds: int = _MAX_ROUNDS) -> MeshArtifact:
    """Build the demanded mesh under the gate -> the accepted :class:`MeshArtifact`.

    AUTO, or a headless run with no session to present on, builds inline."""
    global _AT_GATE

    async def card() -> GateCard:
        presentation = await present_mesh(session)
        lines = render_probe_lines(presentation.get("probes") or {})
        lines.append(
            f"the recipe has {len(session.recipe.ops)} op(s), numbered below; "
            "refine it further with mesh_op")
        return GateCard(
            lines=lines,
            param_sheet=_mesh_param_sheet(session, tool_name=tool_name),
            tool_args={"mesh_id": session.mesh_id,
                       "mesh_layer_id": presentation.get("layer_id"),
                       "mesh_display_uri": presentation.get("display_uri")})

    async def revise(revised: Mapping[str, Any]) -> None:
        await _apply_gate_revision(session, revised)

    _AT_GATE = session
    try:
        outcome = await gate_input_review(
            tool_name=tool_name, mode=input_mode, entries=[], params={},
            max_rounds=max_rounds, ttl_seconds=_TTL_SECONDS,
            present=card, apply_revision=revise)
    finally:
        _AT_GATE = None
    if not outcome.proceed:
        raise MeshToolError(
            _REFUSALS.get(str(outcome.cancel_code), "MESH_GATE_DECLINED"),
            f"the mesh for {tool_name} was not accepted at the gate, so the run "
            f"did not proceed: {outcome.cancel_reason}")
    return await asyncio.to_thread(session.accept)


#: The row every open gate offers beside the params: the revert to the recipe as
#: it was declared, which is a loop action rather than a recipe param.
_RESET_ROW = "reset"

#: The row a mesh reshaped by hand comes in on: the path to the edited layer.
_ADOPT_ROW = "adopt_layer"

#: What the ops rows are named by: their INDEX, which is what an alter or a
#: remove targets.
_OP_ROW = "op"


async def _apply_gate_revision(session: MeshSession,
                               revised: Mapping[str, Any]) -> None:
    """One gate reply as a change to the mesh: a revert, a hand-edit, a size word.

    Only those three rows move; every other change is an op."""
    if _truthy(revised.get(_RESET_ROW)):
        await asyncio.to_thread(session.reset)
        return
    layer = str(revised.get(_ADOPT_ROW) or "").strip()
    if layer:
        await asyncio.to_thread(session.adopt_layer, layer)
        return
    params = {name: _as_number(name, revised[name])
              for name in ("resolution_m",) if revised.get(name) not in (None, "")}
    if not params:
        raise MeshToolError(
            "MESH_GATE_REVISION_UNREADABLE",
            f"a mesh gate revision names {{'{_RESET_ROW}': true}}, "
            f"{{'{_ADOPT_ROW}': <path>}} or one of the recipe's agnostic params "
            f"('resolution_m'); got {sorted(revised)}. Every other change is an "
            "op - append, alter or remove one with mesh_op.")
    await asyncio.to_thread(session.set_params, **params)


def _truthy(value: Any) -> bool:
    """A card's answer to a yes/no row, whichever shape the client sent it in."""
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "y", "true", "1", "on")
    return bool(value)


def _as_number(name: str, value: Any) -> float:
    """A card row's text as the number the recipe param is, checked against it."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise MeshToolError(
            "MESH_GATE_REVISION_UNREADABLE",
            f"the gate reply set {name!r} to {value!r}, and it takes a number."
        ) from None


def _mesh_param_sheet(session: MeshSession, *, tool_name: str) -> Any:
    """The mesh's rows on the gate card: the size word, the recipe, the actions.

    The ops rows are read-only: the one place an op is written is ``mesh_op``."""
    from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

    recipe = session.recipe
    rows = [ParamSheetRow(
        name="resolution_m",
        value=(None if recipe.resolution_m is None
               else str(recipe.resolution_m)),
        door="gate", basis="user",
        desc="the finest cell or triangle edge, in metres - the one size word "
             "every mesher reads. Leave blank to keep this mesh",
        source_badge=f"{recipe.mesher} recipe", user_lever=True)]
    for index, line in enumerate(recipe.numbered()):
        rows.append(ParamSheetRow(
            name=f"{_OP_ROW}[{index}]", value=line.split(": ", 1)[-1][:512],
            door="gate", basis="user",
            desc="one step of the program that produced this mesh; append, alter "
                 f"or remove it by index with mesh_op"[:512],
            source_badge="recipe op", user_lever=False))
    rows.append(ParamSheetRow(
        name=_RESET_ROW, value="no", door="gate", basis="user",
        desc="type yes to put the recipe back to the way it was declared and "
             "rebuild",
        source_badge="loop action", user_lever=True))
    rows.append(ParamSheetRow(
        name=_ADOPT_ROW, value=None, door="gate", basis="user",
        desc="path to a .2dm this mesh was reshaped into by hand; the mesh is "
             "flagged, so any later recipe edit refuses rather than regenerating "
             "the edit away",
        source_badge="loop action", user_lever=True))
    return ParamSheet(
        workflow=tool_name,
        title=f"Review the {recipe.mesher} mesh"[:200],
        rows=rows)
