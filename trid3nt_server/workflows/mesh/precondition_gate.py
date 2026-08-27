"""Mesh precondition gate -- "use this case's mesh?".

NATE's precondition-polymorphism design: mesh creation is an EXPLICIT user act
(a standalone ``build_mesh`` call), never auto-guessed inside a model
template. A model template instead ASKS, at run start, whether to consume a mesh
that already exists in the active case:

  * a case mesh exists AND is engine-compatible -> fire a yes/no gate ("use this
    mesh?", labeled default = USE, because the basis ranking is
    user mesh > drawn box > geocoded AOI); accepted -> the template's
    supplied-mesh path; declined -> unchanged AOI/pour-point authoring;
  * a case mesh exists but is INCOMPATIBLE (wrong engine geometry) -> do NOT gate;
    proceed with the fallback and narrate ONE loud line saying why it was skipped;
  * no case mesh -> return ``None``; the template authors its own mesh as before.

The gate rides the EXACT ``tool-payload-warning`` / pending-confirmation spine the
input-review gate uses -- no new WS event, no new envelope, no plugin
change. Its semantics differ in ONE way: a decline here means "build a fresh mesh"
(the run continues), NOT "cancel the run". In AUTO mode (session default) or a
headless direct-call with no live emitter it applies the labeled default (USE the
compatible mesh) WITHOUT pausing, so headless seeding keeps working.

Engine-generic: ``telemac_rain_on_grid`` is the first consumer; SCHISM/SWAN
templates adopt it by passing their own ``engine`` -- the compatibility facts live
in :data:`artifact.ENGINE_MESH_REQUIREMENTS`, not here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload

from trid3nt_server.gates.input_review import resolve_input_gate_mode
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    find_case_mesh_artifacts,
    mesh_compatible_with_engine,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.mesh.precondition_gate")

__all__ = ["SuppliedMeshDecision", "gate_supplied_mesh", "materialize_supplied_mesh"]

#: Gate wait cap (seconds), mirroring the input-review / solver-confirm TTL.
_TTL_SECONDS = 300


@dataclass
class SuppliedMeshDecision:
    """Outcome of the mesh precondition gate.

    ``use`` True -> solve on ``artifact`` (the template downloads its solver
    geometry via :func:`materialize_supplied_mesh`). ``use`` False -> author a
    fresh mesh as usual. ``note`` is a short human-readable provenance line the
    template folds into its result ``synthetic_inputs`` (honesty floor)."""

    use: bool
    artifact: MeshArtifact | None
    note: str | None = None
    gated: bool = False  # True iff a user was actually prompted


def _synthetic_input_for(art: MeshArtifact, engine: str) -> SyntheticInput:
    return SyntheticInput(
        param="mesh_domain",
        value=f"{art.name} ({art.element_count} elements)",
        basis="user",
        real_source_if_any=f"build_mesh (mesher={art.mode})",
        note=f"user-supplied mesh consumed by {engine} instead of AOI authoring",
    )


async def gate_supplied_mesh(
    *,
    tool_name: str,
    engine: str,
    input_mode: str | None,
    case_id: str | None = None,
    loaded_mesh_uris: list[str] | None = None,
    s3_client: Any = None,
    default_use: bool = True,
) -> SuppliedMeshDecision:
    """Ask whether to consume a case mesh for ``engine``; return the decision.

    Discovers case mesh artifacts (stash + durable sidecar), keeps only those
    ``engine`` can actually solve on (loudly logging why an incompatible one is
    skipped), and -- when a compatible mesh exists -- fires the yes/no gate. A
    ``SuppliedMeshDecision`` with ``use=False, artifact=None`` means "no usable
    mesh; author as before" (the common no-mesh case)."""
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter, current_turn_case,
    )

    if case_id is None:
        case_id = current_turn_case()
    arts = find_case_mesh_artifacts(
        case_id=case_id, loaded_mesh_uris=loaded_mesh_uris, s3_client=s3_client)
    if not arts:
        return SuppliedMeshDecision(use=False, artifact=None)

    compatible: list[MeshArtifact] = []
    skipped: list[str] = []
    for art in arts:
        ok, reason = mesh_compatible_with_engine(art, engine)
        if ok:
            compatible.append(art)
        else:
            skipped.append(reason)
            logger.warning(
                "mesh precondition gate: SKIPPING case mesh %r for %s -- %s",
                art.name, engine, reason)

    if not compatible:
        # A mesh exists but none is engine-compatible: proceed with the fallback,
        # narrating ONE loud line (never a silent force-fit).
        note = ("a mesh exists in this case but is not compatible with "
                f"{engine}; authored a fresh mesh instead ({skipped[0]})"
                if skipped else None)
        return SuppliedMeshDecision(use=False, artifact=None, note=note)

    art = compatible[-1]  # most-recently built
    emitter = current_emitter()
    mode = resolve_input_gate_mode(input_mode)

    # AUTO mode (or no live session): apply the labeled default without pausing.
    if emitter is None or mode == "auto":
        if default_use:
            logger.info(
                "mesh precondition gate: AUTO default -> using case mesh %r "
                "(%d elements) for %s", art.name, art.element_count, engine)
            return SuppliedMeshDecision(
                use=True, artifact=art,
                note=(f"used the case mesh {art.name!r} ({art.element_count} "
                      "elements) [labeled default: user mesh > AOI]"))
        return SuppliedMeshDecision(use=False, artifact=None)

    # user_gated: fire the yes/no gate on the shared pending-confirmation spine.
    decided_use = await _ask_use_mesh(
        emitter, tool_name=tool_name, engine=engine, art=art)
    if decided_use:
        return SuppliedMeshDecision(
            use=True, artifact=art, gated=True,
            note=(f"used the case mesh {art.name!r} ({art.element_count} "
                  "elements) [user-approved]"))
    return SuppliedMeshDecision(
        use=False, artifact=None, gated=True,
        note=f"declined the case mesh {art.name!r}; authored a fresh mesh")


async def _ask_use_mesh(
    emitter: Any, *, tool_name: str, engine: str, art: MeshArtifact
) -> bool:
    """Pause on the shared spine for a 'use this mesh?' decision -> bool.

    ``proceed`` == use the mesh; anything else (cancel / adjust / timeout) ==
    build a fresh mesh (NEVER cancels the run -- unlike input-review's cancel)."""
    from trid3nt_server.gates.pending import (
        _pop_pending_confirmation, _register_pending_confirmation,
    )

    warning_id = new_ulid()
    bathy = "with bathymetry" if art.has_bathymetry else "no bathymetry"
    recommendation = (
        f"This case has a mesh: {art.name} -- {art.element_count} elements, "
        f"{art.node_count} nodes, {art.crs_authid}, {bathy}. Use it as the "
        f"{engine} model domain? Reply 'proceed' to solve on this mesh, or "
        "'cancel' to build a fresh mesh from the AOI/pour-point instead.")
    envelope = PayloadWarningEnvelopePayload(
        warning_id=warning_id, tool_name=tool_name, tool_args={},
        estimated_mb=0.0, threshold_mb=0.0, recommendation=recommendation,
        options=["proceed", "cancel"], ttl_seconds=_TTL_SECONDS,
        synthetic_inputs=[_synthetic_input_for(art, engine)])

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(emitter.session_id, warning_id, fut)
    await emitter.send_envelope("tool-payload-warning", envelope)
    logger.info(
        "mesh precondition gate emitted session=%s tool=%s warning_id=%s mesh=%r",
        emitter.session_id, tool_name, warning_id, art.name)
    try:
        decision = await asyncio.wait_for(fut, timeout=float(_TTL_SECONDS))
    except asyncio.TimeoutError:
        logger.warning(
            "mesh precondition gate timeout session=%s tool=%s -- building fresh",
            emitter.session_id, tool_name)
        return False
    finally:
        _pop_pending_confirmation(warning_id)
    return getattr(decision, "decision", None) == "proceed"


def materialize_supplied_mesh(
    art: MeshArtifact, rundir: str, s3_client: Any, *, engine: str = "telemac"
) -> str:
    """Download ``art``'s solver geometry for ``engine`` to a local path.

    ``use_supplied_mesh`` validates a LOCAL mesh path; the artifact carries an
    ``s3://`` uri, so the accepted-gate path stages it down first. Returns the
    local file path."""
    from trid3nt_server.workflows.mesh.artifact import ENGINE_MESH_REQUIREMENTS

    req = ENGINE_MESH_REQUIREMENTS[str(engine).lower()]
    uri = getattr(art, str(req["uri_field"]))
    rest = str(uri)[len("s3://"):]
    bucket, key = rest.split("/", 1)
    dst = Path(rundir) / Path(key).name
    dst.parent.mkdir(parents=True, exist_ok=True)
    s3_client.download_file(bucket, key, str(dst))
    return str(dst)
