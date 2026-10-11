"""A MESH the caller already has: the record a built mesh is, or a refusal."""

from __future__ import annotations

from typing import Any, Sequence

__all__ = ["mesh"]


def mesh(value: Any, *, label: str = "mesh", kinds: Sequence[str] = ()) -> dict[str, Any]:
    """A supplied mesh -> the accepted mesh's record, as the mesh step returns one.

    ``kinds`` is what the engine solves on; a mesh of another kind, or of no recorded kind, refuses."""
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
    return mesh_record(art)
