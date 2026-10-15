"""What any accepted mesh hands the MODFLOW author: its artifact, display face and bed.

Every number is read from the mesh the acceptance produced; a mesh carrying none refuses by name.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import ModflowError

__all__ = ["mesh_artifact", "mesh_missing", "mesh_nodes"]


def mesh_missing(message: str) -> Exception:
    return ModflowError(message, error_code="MODFLOW_MESH_NOT_ACCEPTED")


def mesh_artifact(mesh: Mapping[str, Any]) -> Any:
    return (mesh or {}).get("artifact")


def mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any, Any, Any]:
    """The accepted mesh's (points, cells, bed, lonlat), read off its display face.

    The ``.2dm`` is the one readable record of the geometry file's numbering.
    """
    from trid3nt_server.tools.mesh.shared.nodes import read_accepted_mesh_nodes

    display = str((mesh or {}).get("display_uri") or "")
    if not display:
        raise mesh_missing(
            f"this run carries no accepted mesh's display face to solve MODFLOW "
            f"on (mesh record: {sorted((mesh or {}))}).")
    points, cells, bed, lonlat = read_accepted_mesh_nodes(display)
    if points is None or cells is None:
        raise mesh_missing(
            "the accepted mesh's display face carries no readable nodes and "
            "cells, so no DISV can be written from it.")
    return points, cells, bed, lonlat
