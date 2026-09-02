"""The mesh gate loop: a built mesh presented for edit, reset or accept.

A mesh is expensive to get wrong and cheap to look at, so in USER-GATED mode the
build stops at a gate instead of going straight to a solver. The gate presents
three things about the SAME mesh - the editable MDAL layer on the map, the
numeric probes it is judged on, and the RECIPE with its ops NUMBERED - and then
hands over one edit surface for every mesher there will ever be.

ONE CARD PATH. The card carries the three agnostic params (the ones every mesher
means the same thing by), the numbered ops as the program that produced what is
on screen, and the reset row. Everything an op can say is said through
``mesh_op``, which is a registered tool rather than a mounted per-mesher one -
so nothing here knows what any library's functions are, and adding a mesher adds
no card code.

A hand-edit made in QGIS re-enters through ``mesh_adopt_layer`` and is HISTORY,
not program: the mesh is flagged, and a later recipe edit refuses rather than
throwing the hand-edit away.

AUTO mode builds inline: no presentation, no mounted tools, no pause.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import resolve_input_gate_mode
from trid3nt_server.tools import mount_tool, unmount_tool
from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.mesh.meshers import MeshToolError
from trid3nt_server.workflows.mesh.session import MeshSession

logger = logging.getLogger("trid3nt_server.workflows.mesh.gate")

__all__ = [
    "ACCEPT_TOOL",
    "ADOPT_TOOL",
    "RESET_TOOL",
    "MeshGate",
    "active_mesh_session",
    "close_mesh_gate",
    "gate_mesh_build",
    "open_mesh_gate",
    "open_mesh_gates",
    "present_mesh",
    "render_probe_lines",
]

#: The loop actions that are not a recipe edit: every open session can be frozen,
#: put back to its declaration, or handed a mesh somebody reshaped by hand.
ACCEPT_TOOL = "mesh_accept"
RESET_TOOL = "mesh_reset"
ADOPT_TOOL = "mesh_adopt_layer"

#: Gate wait cap (seconds), mirroring the input-review / precondition-gate TTL.
_TTL_SECONDS = 300

#: How many times the gate re-presents before it stops asking.
_MAX_ROUNDS = 3


@dataclass
class MeshGate:
    """One open mesh session plus the tool names mounted for it."""

    session: MeshSession
    tools: tuple[str, ...] = ()
    accepted: MeshArtifact | None = None

    @property
    def mesh_id(self) -> str:
        return self.session.mesh_id


#: The open gates, keyed by mesh id. A mounted tool reaches its session through
#: this map rather than closing over it, so a tool that outlives its gate refuses
#: by name instead of editing a session nothing is watching.
_OPEN: dict[str, MeshGate] = {}


def open_mesh_gates() -> tuple[MeshGate, ...]:
    """Every currently open gate, oldest first."""
    return tuple(_OPEN.values())


def active_mesh_session(mesh_id: str | None = None) -> MeshSession:
    """The session a runtime recipe edit acts on, or a typed refusal.

    ONE mesh is under construction at a time, so an unnamed edit means the one on
    screen; ``mesh_id`` names another when more than one is somehow open.
    """
    if mesh_id:
        return _gate_for(str(mesh_id)).session
    gates = open_mesh_gates()
    if not gates:
        raise MeshToolError(
            "MESH_NO_ACTIVE_SESSION",
            "no mesh is open at the gate, so there is no recipe to edit; build "
            "one with build_mesh(input_mode='user_gated') first.")
    return gates[-1].session


def _gate_for(mesh_id: str) -> MeshGate:
    gate = _OPEN.get(mesh_id)
    if gate is None:
        raise MeshToolError(
            "MESH_SESSION_CLOSED",
            f"the mesh session {mesh_id!r} is closed, so its gate tools no longer "
            "act on anything; build a mesh to open a new session.")
    return gate


# --------------------------------------------------------------------------- #
# Mount / unmount.
# --------------------------------------------------------------------------- #
def open_mesh_gate(session: MeshSession) -> MeshGate:
    """Open a gate over ``session`` -> the :class:`MeshGate`, tools mounted.

    One mesh is under construction at a time: opening a gate closes any other,
    so the mounted names always mean the mesh the user is looking at.
    """
    for other in list(_OPEN.values()):
        logger.info("mesh gate: superseding open session %s with %s",
                    other.mesh_id, session.mesh_id)
        close_mesh_gate(other)
    gate = MeshGate(session=session)
    _OPEN[gate.mesh_id] = gate
    mounted: list[str] = []
    try:
        for build in (_accept_tool, _reset_tool, _adopt_tool):
            mounted.append(mount_tool(*build(gate.mesh_id)))
    except Exception:
        for name in mounted:
            unmount_tool(name)
        _OPEN.pop(gate.mesh_id, None)
        raise
    gate.tools = tuple(mounted)
    logger.info("mesh gate OPEN session=%s mesher=%s tools=%s",
                gate.mesh_id, session.mesher.name, list(gate.tools))
    return gate


def close_mesh_gate(gate: "MeshGate | str") -> None:
    """Unmount a gate's tools and forget the session. Safe to call twice."""
    mesh_id = gate if isinstance(gate, str) else gate.mesh_id
    found = _OPEN.pop(mesh_id, None)
    if found is None:
        return
    for name in found.tools:
        unmount_tool(name)
    found.tools = ()
    logger.info("mesh gate CLOSED session=%s", mesh_id)


# --------------------------------------------------------------------------- #
# The loop tools.
# --------------------------------------------------------------------------- #
def _metadata(name: str) -> AtomicToolMetadata:
    return AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False, tier="general")


def _accept_tool(mesh_id: str) -> tuple[AtomicToolMetadata, Any]:
    async def accept() -> dict[str, Any]:
        gate = _gate_for(mesh_id)
        art = await asyncio.to_thread(gate.session.accept)
        gate.accepted = art
        close_mesh_gate(gate)
        return _artifact_summary(art)

    accept.__name__ = ACCEPT_TOOL
    accept.__doc__ = (
        "FREEZE THE MESH under construction as this case's mesh artifact -> the "
        "solver-ready mesh record.\n\n"
        "    Call this when the presented mesh is the domain to model on. The "
        "recipe that produced it is frozen onto the artifact as its provenance. "
        "The session closes and its gate tools go away, so make every edit "
        "first. The accepted mesh stays in the case for any template that asks "
        "for one."
    )
    return _metadata(ACCEPT_TOOL), accept


def _reset_tool(mesh_id: str) -> tuple[AtomicToolMetadata, Any]:
    async def reset() -> dict[str, Any]:
        gate = _gate_for(mesh_id)
        await asyncio.to_thread(gate.session.reset)
        return await present_mesh(gate)

    reset.__name__ = RESET_TOOL
    reset.__doc__ = (
        "PUT THE RECIPE BACK to the way it was declared -> the mesh rebuilt from "
        "it, re-presented with its probes.\n\n"
        "    The one structured revert. Every op appended, altered or removed at "
        "this gate goes; the recipe the template declared survives. To undo one "
        "change rather than all of them, edit the recipe back with mesh_op."
    )
    return _metadata(RESET_TOOL), reset


def _adopt_tool(mesh_id: str) -> tuple[AtomicToolMetadata, Any]:
    async def mesh_adopt_layer(layer: str) -> dict[str, Any]:
        gate = _gate_for(mesh_id)
        await asyncio.to_thread(gate.session.adopt_layer, layer)
        return await present_mesh(gate)

    mesh_adopt_layer.__doc__ = (
        "ADOPT A HAND-EDITED mesh layer as the mesh under construction -> its "
        "probes.\n\n"
        "    For a mesh reshaped by hand in QGIS - nodes dragged, elements "
        "deleted. The change lives in the layer's bytes rather than in the "
        "recipe, so the mesh is FLAGGED: it can be accepted, but any later "
        "recipe edit refuses rather than regenerating the hand-edit away.\n\n"
        "    Params:\n"
        "        layer: path to the edited .2dm mesh layer."
    )
    return _metadata(ADOPT_TOOL), mesh_adopt_layer


# --------------------------------------------------------------------------- #
# Presentation.
# --------------------------------------------------------------------------- #
async def present_mesh(gate: MeshGate) -> dict[str, Any]:
    """Put the mesh on the map and read it -> the layer, the probes, the recipe.

    The display face is an MDAL mesh layer, which is what makes it editable in
    QGIS rather than a picture of a mesh; the wireframe style is how it renders.
    """
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    session = gate.session
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
        "gate_tools": list(gate.tools),
        "recipe": session.recipe.to_json(),
        "ops": session.recipe.numbered(),
        **({"regen_note": session.regen_note} if session.regen_note else {}),
    }


def _artifact_summary(art: MeshArtifact) -> dict[str, Any]:
    return {
        "mesh_id": art.mesh_id,
        "name": art.name,
        "mesher": art.mode,
        "node_count": art.node_count,
        "element_count": art.element_count,
        "crs_authid": art.crs_authid,
        "has_bathymetry": art.has_bathymetry,
        "unsolvable_reason": art.unsolvable_reason(),
        "display_uri": art.display_uri,
        "recipe_uri": art.recipe_uri,
    }


def render_probe_lines(probes: Mapping[str, Any]) -> list[str]:
    """The probe readout a gate card quotes, one measured fact per line."""
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


# --------------------------------------------------------------------------- #
# The gate loop for a DEMANDED build.
# --------------------------------------------------------------------------- #
async def gate_mesh_build(session: MeshSession, *, tool_name: str,
                          input_mode: str | None = None,
                          max_rounds: int = _MAX_ROUNDS) -> MeshArtifact:
    """Build the demanded mesh under the gate -> the accepted :class:`MeshArtifact`.

    AUTO (or a headless run with no session to present on) builds inline. Under
    USER-GATED the mesh is presented and the run waits: approve it, put the
    recipe back to its declaration, or change a param and look again.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None or resolve_input_gate_mode(input_mode) == "auto":
        return await asyncio.to_thread(session.accept)

    gate = open_mesh_gate(session)
    try:
        for round_idx in range(1, max_rounds + 1):
            presentation = await present_mesh(gate)
            decision = await _ask_mesh_gate(
                emitter, tool_name=tool_name, gate=gate,
                presentation=presentation, round_idx=round_idx,
                max_rounds=max_rounds)
            if decision is None:
                raise MeshToolError(
                    "MESH_GATE_TIMEOUT",
                    f"the mesh gate for {tool_name} was not answered, so the "
                    "mesh was not accepted and the run did not proceed.")
            if decision.decision == "proceed":
                art = await asyncio.to_thread(session.accept)
                gate.accepted = art
                return art
            if decision.decision == "cancel":
                raise MeshToolError(
                    "MESH_GATE_DECLINED",
                    f"the mesh for {tool_name} was declined at the gate; the run "
                    "did not proceed.")
            await _apply_gate_revision(session, decision.revised_args or {})
        raise MeshToolError(
            "MESH_GATE_NOT_APPROVED",
            f"the mesh for {tool_name} was not approved after {max_rounds} "
            "rounds; the run did not proceed.")
    finally:
        close_mesh_gate(gate)


#: The row every open gate offers beside the params: the revert to the recipe as
#: it was declared, which is a loop action rather than a recipe param.
_RESET_ROW = "reset"

#: What the ops rows are named by: their INDEX, which is what an alter or a
#: remove targets.
_OP_ROW = "op"


async def _apply_gate_revision(session: MeshSession,
                               revised: Mapping[str, Any]) -> None:
    """One gate reply as a recipe change: the revert, or the params that MOVED.

    The card carries the three agnostic params and the reset; everything an op
    can say is said through ``mesh_op``, so there is nothing per-mesher to unpack
    here and no mesher can have a card row this loop does not understand.
    """
    if _truthy(revised.get(_RESET_ROW)):
        await asyncio.to_thread(session.reset)
        return
    params = {name: _as_number(name, revised[name])
              for name in ("resolution_m",) if revised.get(name) not in (None, "")}
    if not params:
        raise MeshToolError(
            "MESH_GATE_REVISION_UNREADABLE",
            f"a mesh gate revision names either {{'{_RESET_ROW}': true}} or one "
            f"of the recipe's agnostic params ('resolution_m'); got "
            f"{sorted(revised)}. Every other change is an op - append, alter or "
            "remove one with mesh_op.")
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


def _mesh_param_sheet(session: MeshSession, *, tool_name: str,
                      round_idx: int, max_rounds: int) -> Any:
    """The gate card: the agnostic params, the numbered recipe, the revert.

    Generic by construction. The rows a user can MOVE are the params every mesher
    means the same thing by; the ops are shown numbered because an index is what
    an alter or a remove targets, and they are read-only here because the one
    place an op is written is ``mesh_op``.
    """
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
    return ParamSheet(
        workflow=tool_name,
        title=(f"Review the {recipe.mesher} mesh "
               f"(round {round_idx}/{max_rounds})")[:200],
        rows=rows)


async def _ask_mesh_gate(emitter: Any, *, tool_name: str, gate: MeshGate,
                         presentation: Mapping[str, Any], round_idx: int,
                         max_rounds: int) -> Any:
    """Present the card and wait -> the decision payload, or None on timeout."""
    from trid3nt_server.gates.pending import (
        _pop_pending_confirmation, _register_pending_confirmation,
    )

    session = gate.session
    warning_id = new_ulid()
    body = "; ".join(render_probe_lines(presentation.get("probes") or {}))
    recommendation = (
        f"The {session.mesher.name} mesh is on the map as an editable mesh "
        f"layer (round {round_idx}/{max_rounds}): {body}. Its recipe has "
        f"{len(session.recipe.ops)} op(s), numbered on the card. Submit "
        f"unchanged to solve on it, cancel to stop, or change a row to rebuild "
        f"it; refine it further with mesh_op."
    )[:512]
    envelope = PayloadWarningEnvelopePayload(
        warning_id=warning_id, tool_name=tool_name,
        tool_args={"mesh_id": session.mesh_id,
                   "mesh_layer_id": presentation.get("layer_id"),
                   "mesh_display_uri": presentation.get("display_uri")},
        estimated_mb=0.0, threshold_mb=0.0, recommendation=recommendation,
        options=["proceed", "cancel", "narrow_scope"], ttl_seconds=_TTL_SECONDS,
        param_sheet=_mesh_param_sheet(session, tool_name=tool_name,
                                      round_idx=round_idx,
                                      max_rounds=max_rounds))

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(emitter.session_id, warning_id, fut)
    await emitter.send_envelope("tool-payload-warning", envelope)
    logger.info("mesh gate emitted session=%s tool=%s warning_id=%s mesh=%s "
                "round=%d/%d", emitter.session_id, tool_name, warning_id,
                session.mesh_id, round_idx, max_rounds)
    try:
        return await asyncio.wait_for(fut, timeout=float(_TTL_SECONDS))
    except asyncio.TimeoutError:
        logger.warning("mesh gate timeout session=%s tool=%s mesh=%s",
                       emitter.session_id, tool_name, session.mesh_id)
        return None
    finally:
        _pop_pending_confirmation(warning_id)
