"""The corridor MESH the run directory was staged with, when one was.

The corridor triangulation is authored server-side now: a mesh session builds it,
a human accepts it, and the accepted topology is staged into the solve's run
directory. What is left here is the worker's half of that hand-off - write the
mesh the build produced, and read it back into the exact dict shape the rest of
this worker reads.

Geometry ALONE cannot carry the hand-off: a SELAFIN states nodes, cells and which
nodes are on a boundary, and says nothing about which stretch of that boundary is
the inflow and which the outflow. Both classifications are the mesher's, so both
travel with the mesh.

Absent staging the worker meshes as it always has; a bundle whose node count
disagrees with its own arrays refuses rather than solving on half a mesh.
"""

from __future__ import annotations

import json
import os
from typing import Any

#: Basename the server's manifest stages the accepted corridor mesh under.
STAGED_MESH_FILENAME: str = "river_mesh.npz"

#: Boundary-node roles, as the small integer the bundle stores each one as.
_ROLES: tuple[str, ...] = ("wall", "inflow", "outflow")


class StagedMeshUnusableError(RuntimeError):
    """A mesh was staged and it cannot be read as one."""


def write_mesh_bundle(mesh: dict[str, Any], path: str) -> str:
    """Write ``mesh`` - the whole built dict - to ``path`` -> the path written.

    Everything the solve reads off a mesh is recorded: the geometry, the boundary
    ranking IPOBO is derived from, the per-node boundary-condition codes, the
    inflow/outflow node sets, and the recentered centerline the mesh was actually
    built on.
    """
    import numpy as np

    rings = [np.asarray(r, dtype=np.int64) for r in mesh["boundary_rings"]]
    offsets = np.cumsum([0] + [int(r.size) for r in rings]).astype(np.int64)
    roles = np.asarray([_ROLES.index(str(c)) for c in mesh["cls"]], dtype=np.int8)
    np.savez(
        path,
        X=np.asarray(mesh["X"], dtype=float),
        Y=np.asarray(mesh["Y"], dtype=float),
        ikle=np.asarray(mesh["ikle"], dtype=np.int64),
        ring=np.asarray(mesh["ring"], dtype=np.int64),
        ipob=np.asarray(mesh["ipob"], dtype=np.int32),
        lihbor=np.asarray(mesh["lihbor"], dtype=np.int64),
        liubor=np.asarray(mesh["liubor"], dtype=np.int64),
        livbor=np.asarray(mesh["livbor"], dtype=np.int64),
        litbor=np.asarray(mesh["litbor"], dtype=np.int64),
        roles=roles,
        in_nodes=np.asarray(sorted(mesh["in_nodes"]), dtype=np.int64),
        out_nodes=np.asarray(sorted(mesh["out_nodes"]), dtype=np.int64),
        ring_offsets=offsets,
        ring_nodes=(np.concatenate(rings) if rings
                    else np.zeros(0, dtype=np.int64)),
        centerline=np.asarray(mesh["centerline"], dtype=float),
        scalars=np.asarray(json.dumps({
            "npoin": int(mesh["npoin"]),
            "nptfr": int(mesh["nptfr"]),
            "n_in": int(mesh["n_in"]),
            "n_out": int(mesh["n_out"]),
            "n_islands": int(mesh["n_islands"]),
            "domain_mode": mesh.get("domain_mode"),
            "water_coverage_frac": mesh.get("water_coverage_frac"),
            "banks_ok": bool(mesh.get("banks_ok")),
            "smooth_tries": int(mesh.get("smooth_tries") or 0),
        })))
    return path


def staged_mesh_bundle(data_dir: str) -> dict[str, Any] | None:
    """The staged corridor mesh as the built dict, or ``None`` when none was staged."""
    import numpy as np

    path = os.path.join(data_dir, STAGED_MESH_FILENAME)
    if not os.path.exists(path):
        return None
    with np.load(path, allow_pickle=False) as bundle:
        scalars = json.loads(str(bundle["scalars"]))
        X = np.asarray(bundle["X"], dtype=float)
        ikle = np.asarray(bundle["ikle"], dtype=np.int64)
        if X.shape[0] != int(scalars["npoin"]) or ikle.size == 0:
            raise StagedMeshUnusableError(
                f"the staged corridor mesh at {path} states {scalars['npoin']} "
                f"nodes and carries {X.shape[0]} with {ikle.shape[0]} elements; "
                "it is not a mesh this run can solve on.")
        offsets = np.asarray(bundle["ring_offsets"], dtype=np.int64)
        ring_nodes = np.asarray(bundle["ring_nodes"], dtype=np.int64)
        mesh: dict[str, Any] = {
            "X": X,
            "Y": np.asarray(bundle["Y"], dtype=float),
            "ikle": ikle,
            "ring": np.asarray(bundle["ring"], dtype=np.int64),
            "ipob": np.asarray(bundle["ipob"], dtype=np.int32),
            "lihbor": np.asarray(bundle["lihbor"], dtype=np.int64),
            "liubor": np.asarray(bundle["liubor"], dtype=np.int64),
            "livbor": np.asarray(bundle["livbor"], dtype=np.int64),
            "litbor": np.asarray(bundle["litbor"], dtype=np.int64),
            "cls": np.asarray(
                [_ROLES[int(r)] for r in np.asarray(bundle["roles"])], dtype=object),
            "in_nodes": {int(n) for n in np.asarray(bundle["in_nodes"])},
            "out_nodes": {int(n) for n in np.asarray(bundle["out_nodes"])},
            "boundary_rings": [ring_nodes[offsets[i]:offsets[i + 1]]
                               for i in range(int(offsets.size) - 1)],
            "centerline": np.asarray(bundle["centerline"], dtype=float),
        }
    mesh.update({k: scalars[k] for k in
                 ("npoin", "nptfr", "n_in", "n_out", "n_islands", "domain_mode",
                  "water_coverage_frac", "banks_ok", "smooth_tries")})
    return mesh
