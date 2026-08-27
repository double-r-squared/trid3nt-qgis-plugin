"""The mesh gate loop: a built mesh presented for edit, restart or accept.

A mesh is expensive to get wrong and cheap to look at, so in USER-GATED mode the
build stops at a gate instead of going straight to a solver. The gate presents
three things about the SAME mesh - the editable MDAL layer on the map, the
numeric probes the mesh is judged on, and the wireframe the layer renders as -
and then hands over the edit surface: one agent tool per action the building
mesher REGISTERED, mounted for exactly as long as the session is open and
removed again on accept. Nothing here knows what an action does; the mesher's
registry is what says which tools exist.

A hand-edit made in QGIS re-enters through the same surface as
``edit("apply_layer_edits", layer)``, so a mesh the user reshaped by hand and a
mesh the agent refined by name are one chain and one recipe.

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
from trid3nt_server.workflows.mesh.meshers import EditAction, MeshField, MeshToolError
from trid3nt_server.workflows.mesh.session import MeshSession

logger = logging.getLogger("trid3nt_server.workflows.mesh.gate")

__all__ = [
    "ACCEPT_TOOL",
    "RESTART_TOOL",
    "MeshGate",
    "close_mesh_gate",
    "edit_tool_name",
    "gate_mesh_build",
    "open_mesh_gate",
    "open_mesh_gates",
    "present_mesh",
    "render_probe_lines",
]

#: The two loop actions that are not a mesher's to register: every open session
#: can be frozen or truncated regardless of which library built it.
ACCEPT_TOOL = "mesh_accept"
RESTART_TOOL = "mesh_restart"

#: Gate wait cap (seconds), mirroring the input-review / precondition-gate TTL.
_TTL_SECONDS = 300

#: How many times the gate re-presents before it stops asking.
_MAX_ROUNDS = 3


def edit_tool_name(action: str) -> str:
    """The mounted tool name for one registered edit action."""
    return f"mesh_edit_{action}"


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


def _gate_for(mesh_id: str) -> MeshGate:
    gate = _OPEN.get(mesh_id)
    if gate is None:
        raise MeshToolError(
            "MESH_SESSION_CLOSED",
            f"the mesh session {mesh_id!r} is closed, so its edit tools no longer "
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
        for action in session.mesher.actions.values():
            mounted.append(mount_tool(*_edit_tool(gate.mesh_id, action)))
        mounted.append(mount_tool(*_accept_tool(gate.mesh_id)))
        mounted.append(mount_tool(*_restart_tool(gate.mesh_id)))
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
# The generated tools.
# --------------------------------------------------------------------------- #
#: What a declared input's accepted types become on a generated signature. The
#: model reads a schema, so a field that takes several numeric types is one
#: number to it.
_ANNOTATIONS: tuple[tuple[type, str], ...] = (
    (bool, "bool"), (str, "str"), (float, "float"), (int, "float"),
    (dict, "dict"), (list, "list"), (tuple, "list"),
)


def _annotation(declared: MeshField) -> str:
    for kind, name in _ANNOTATIONS:
        if kind in declared.types:
            return name
    return "str"


def _input_line(declared: MeshField) -> str:
    choices = (f" one of {list(declared.choices)}." if declared.choices else "")
    return (f"        {declared.name}: {declared.doc or 'no description'}."
            f"{choices}")


def _metadata(name: str) -> AtomicToolMetadata:
    return AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False, tier="general")


def _edit_tool(mesh_id: str, action: EditAction) -> tuple[AtomicToolMetadata, Any]:
    """One registered edit action as a mounted agent tool."""
    name = edit_tool_name(action.name)
    params = []
    for field_name, declared in action.inputs.items():
        annotation = _annotation(declared)
        params.append(f"{field_name}: {annotation}" if declared.required
                      else f"{field_name}: {annotation} | None = None")

    async def apply(**inputs: Any) -> dict[str, Any]:
        gate = _gate_for(mesh_id)
        supplied = {k: v for k, v in inputs.items() if v is not None}
        await asyncio.to_thread(gate.session.edit, action.name, **supplied)
        return await present_mesh(gate)

    fn = _compiled(name, params, tuple(action.inputs), apply)
    replay = ("" if action.replayable else
              " This edit is NOT replayable: its change lives in the layer's "
              "bytes, so the recipe records the layer's digest and refuses to "
              "rebuild from it.")
    fn.__doc__ = (
        f"{action.doc or f'Apply the {action.name} edit.'} Edits the mesh "
        f"currently open at the mesh gate -> the rebuilt mesh's probes.\n\n"
        "    The mesh is rebuilt and re-presented on the map; the returned "
        "probes (node/element count, edge lengths, min angle, boundary "
        "segments) are what the edit actually produced. Call "
        f"{ACCEPT_TOOL} when the mesh is right, or {RESTART_TOOL} to "
        f"throw the gate-time edits away.{replay}\n\n"
        "    Params:\n"
        + "\n".join(_input_line(d) for d in action.inputs.values()))
    return _metadata(name), fn


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
        "session closes and its edit tools go away, so make every edit first. "
        "The accepted mesh stays in the case for any template that asks for one."
    )
    return _metadata(ACCEPT_TOOL), accept

def _restart_tool(mesh_id: str) -> tuple[AtomicToolMetadata, Any]:
    async def restart() -> dict[str, Any]:
        gate = _gate_for(mesh_id)
        await asyncio.to_thread(gate.session.restart)
        return await present_mesh(gate)

    restart.__name__ = RESTART_TOOL
    restart.__doc__ = (
        "THROW AWAY the gate-time mesh edits -> the mesh as it was declared, "
        "re-presented with its probes.\n\n"
        "    Truncates the edit chain back to the declared prefix and rebuilds. "
        "The declared edits the template asked for survive; everything added at "
        "this gate does not."
    )
    return _metadata(RESTART_TOOL), restart


def _compiled(name: str, params: list[str], input_names: tuple[str, ...],
              apply: Any) -> Any:
    """A named async function with a REAL signature over ``params``.

    The model is handed a schema built from the signature, so a generated tool
    that took ``**kwargs`` would advertise no arguments at all.
    """
    call = ", ".join(f"{n}={n}" for n in input_names)
    source = (f"async def {name}({', '.join(params)}):\n"
              f"    return await _apply({call})\n")
    namespace: dict[str, Any] = {"_apply": apply}
    exec(compile(source, f"<mesh-edit:{name}>", "exec"), namespace)  # noqa: S102
    fn = namespace[name]
    fn.__module__ = __name__
    return fn


# --------------------------------------------------------------------------- #
# Presentation.
# --------------------------------------------------------------------------- #
async def present_mesh(gate: MeshGate) -> dict[str, Any]:
    """Put the mesh on the map and read it -> the layer, the probes, the actions.

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
        "edit_tools": list(gate.tools),
        "recipe": session.recipe_lines(),
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
        "engine_compat": list(art.engine_compat or []),
        "display_uri": art.display_uri,
        "recipe_uri": art.recipe_uri,
    }


def render_probe_lines(probes: Mapping[str, Any]) -> list[str]:
    """The probe readout a gate card quotes, one measured fact per line."""
    lines = [f"{probes.get('node_count', 0)} nodes / "
             f"{probes.get('element_count', 0)} elements, "
             f"{probes.get('crs_authid', '?')}",
             ("bed sampled" if probes.get("has_bed") else "NO bed sampled")]
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
    USER-GATED the mesh is presented and the run waits: approve it, truncate the
    gate-time edits, or apply one more named edit and look again.
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


async def _apply_gate_revision(session: MeshSession,
                               revised: Mapping[str, Any]) -> None:
    """One gate reply as a chain change: a truncation or one named edit."""
    if revised.get("restart"):
        await asyncio.to_thread(session.restart)
        return
    action = revised.get("edit")
    if not action:
        raise MeshToolError(
            "MESH_GATE_REVISION_UNREADABLE",
            f"a mesh gate revision names either {{'restart': true}} or "
            f"{{'edit': '<action>', ...inputs}}; got {sorted(revised)}. The "
            f"registered actions are {sorted(session.mesher.actions)}.")
    inputs = {k: v for k, v in revised.items() if k not in ("edit", "restart")}
    await asyncio.to_thread(session.edit, str(action), **inputs)


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
    actions = ", ".join(sorted(session.mesher.actions))
    recommendation = (
        f"The {session.mesher.name} mesh is on the map as an editable mesh "
        f"layer (round {round_idx}/{max_rounds}): {body}. Reply 'proceed' to "
        f"solve on it, 'cancel' to stop, or 'provide values' with "
        f"{{'edit': '<action>', ...}} to refine it / {{'restart': true}} to "
        f"drop the gate-time edits. Registered actions: {actions}."
    )[:512]
    envelope = PayloadWarningEnvelopePayload(
        warning_id=warning_id, tool_name=tool_name,
        tool_args={"mesh_id": session.mesh_id,
                   "mesh_layer_id": presentation.get("layer_id"),
                   "mesh_display_uri": presentation.get("display_uri")},
        estimated_mb=0.0, threshold_mb=0.0, recommendation=recommendation,
        options=["proceed", "cancel", "narrow_scope"], ttl_seconds=_TTL_SECONDS)

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
