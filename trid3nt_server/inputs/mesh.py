"""A MESH the caller already has: the record a built mesh is, or a refusal."""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["mesh"]


def mesh(value: Any, *, label: str = "mesh", kinds: Sequence[str] = (),
         over: Any = None) -> dict[str, Any]:
    """A supplied mesh -> the accepted mesh's record, as the mesh step returns one.

    ``kinds`` is what the engine solves on; a mesh of another kind, or of no recorded kind, refuses.
    So does one with no bed painted on it, and one off the lon/lat box ``over`` the run solves over."""
    from trid3nt_server.inputs.extent import bbox_overlaps
    from trid3nt_server.store import objects as storage
    from trid3nt_server.tools.mesh.artifact import (MeshArtifact,
                                                    read_mesh_artifact_sidecar)
    from trid3nt_server.tools.mesh.meshers import MeshToolError
    from trid3nt_server.tools.mesh.step import mesh_record

    art = value if isinstance(value, MeshArtifact) else read_mesh_artifact_sidecar(
        str(value), storage.client())
    if art is None:
        raise MeshToolError(
            "MESH_EXPLICIT_UNREADABLE",
            f"the {label} supplied for this run ({value!r}) carries no readable "
            "mesh artifact record, so what it is cannot be checked against the "
            "engine; supply a mesh this case built.")
    got = ((art.provenance or {}).get("recipe") or {}).get("kind")
    if kinds and got not in kinds:
        is_a = repr(got) if got is not None else "of no recorded kind"
        raise MeshToolError(
            "MESH_KIND_MISMATCH",
            f"this engine solves on {list(kinds)!r} meshes; the {label} supplied "
            f"({art.name!r}, built by {art.mode!r}) is {is_a}. Build or supply "
            f"one of {list(kinds)!r} instead.")
    if not art.has_bathymetry:
        raise MeshToolError(
            "MESH_BED_UNPAINTED",
            f"the {label} supplied ({art.name!r}) carries no bed: no elevation "
            "was ever painted onto its nodes, and a run does not solve over a "
            "flat one nobody measured. Paint the bed onto the mesh (its set_bed "
            "op) and supply that mesh.")
    if over is not None and not bbox_overlaps(art.bbox, over):
        raise MeshToolError(
            "SUPPLIED_COVERAGE_MISMATCH",
            f"the {label} supplied ({art.name!r}) lies over {tuple(art.bbox)!r} "
            f"and this run's domain over {tuple(over)!r}: they share no ground. "
            "Supply a mesh of this domain, or the domain that mesh was built on.")
    return mesh_record(art)
