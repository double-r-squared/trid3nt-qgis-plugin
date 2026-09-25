"""The files TELEMAC asks an accepted mesh for, written from that mesh.

The pair is ONE artifact written from ONE walk of the accepted geometry, and the
boundary numbering that walk measures rides beside it. Nothing here is a mesh
fact: a mesh states its nodes, its cells and the named stretches of its boundary,
and this stage is what TELEMAC needs made out of them."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .accepted_mesh import mesh_artifact, mesh_missing
from .selafin_io import (
    BOUNDARY_CONDITIONS_FILE,
    GEOMETRY_FILE,
    telemac_boundary,
    write_telemac_pair,
)
from .topology import boundary_topology

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.mesh_files")

__all__ = ["telemac_mesh_files"]


async def telemac_mesh_files(*, mesh: Mapping[str, Any]) -> dict[str, Any]:
    """The accepted mesh's TELEMAC pair and the topology numbering it -> both.

    A mesh whose record already carries the pair under TELEMAC's own names is
    not written again; its boundary is walked either way, because the numbering
    the steering author reads is measured and never stored."""
    import asyncio

    return await asyncio.to_thread(_mesh_files, mesh)


def _mesh_files(mesh: Mapping[str, Any]) -> dict[str, Any]:
    from trid3nt_server.workflows.mesh.shared.nodes import read_accepted_mesh_nodes

    art = mesh_artifact(mesh)
    display = (mesh or {}).get("display_uri")
    if art is None or not display:
        raise mesh_missing(
            "this run was handed no accepted mesh to write a geometry from "
            f"(mesh record: {sorted((mesh or {}))}).")
    points, cells, bed, _lonlat = read_accepted_mesh_nodes(str(display))
    if points is None or cells is None:
        raise mesh_missing(
            f"the accepted mesh {getattr(art, 'name', '?')!r} carries no readable "
            "nodes and cells, so no geometry can be written from it.")
    roles = dict(getattr(art, "boundary_roles", None) or {})
    files = dict(getattr(art, "engine_files", None) or {})
    if files.get(GEOMETRY_FILE) and files.get(BOUNDARY_CONDITIONS_FILE):
        walk = telemac_boundary(x=points[:, 0], y=points[:, 1], cells=cells,
                                roles=roles)
    else:
        with tempfile.TemporaryDirectory() as rundir:
            written = write_telemac_pair(
                Path(rundir), x=points[:, 0], y=points[:, 1], cells=cells,
                bed=bed, roles=roles, title=f"TRID3NT {art.mesh_id}")
            walk = {"stats": written["stats"]}
            files[GEOMETRY_FILE] = _stage(art.mesh_id, written["geo_slf"])
            files[BOUNDARY_CONDITIONS_FILE] = _stage(art.mesh_id, written["cli"])
        _record(art, files)
        logger.info("telemac mesh files written for %s: %s", art.mesh_id,
                    sorted(files))
    stats = walk["stats"]
    return {
        "engine_files": files,
        "topology": boundary_topology(
            roles=roles,
            liquid_boundary_order=stats.get("liquid_boundary_roles") or [],
            liquid_boundary_prescribes=(
                stats.get("liquid_boundary_prescribes") or [])),
    }


def _stage(mesh_id: str, local: Path) -> str:
    """Upload one written file beside the mesh's other objects -> its uri.

    With no cache bucket configured the file has nowhere to go and refuses:
    a run staged from a path inside a temporary directory reads nothing."""
    from trid3nt_server import storage

    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise mesh_missing(
            f"TRID3NT_CACHE_BUCKET is unset, so {local.name} written for mesh "
            f"{mesh_id} has nowhere to be staged and the run would read nothing.")
    key = f"mesh/{mesh_id}/{local.name}"
    storage.client().put_object(Bucket=bucket, Key=key, Body=local.read_bytes())
    return f"s3://{bucket}/{key}"


def _record(art: Any, files: Mapping[str, str]) -> None:
    """Put what was written onto the mesh's own record, under the engine's names.

    The sidecar is durability, never correctness: a write that fails leaves the
    run holding the files it just wrote."""
    from trid3nt_server import storage
    from trid3nt_server.workflows.mesh.artifact import write_mesh_artifact_sidecar

    art.engine_files = dict(files)
    if str(art.display_uri or "").startswith("s3://"):
        write_mesh_artifact_sidecar(art, storage.client())
