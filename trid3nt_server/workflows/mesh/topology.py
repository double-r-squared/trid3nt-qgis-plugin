"""The accepted topology of a mesh - what its geometry file cannot state.

A SELAFIN says which nodes lie on a boundary; it never says which stretch of that
boundary is the inflow and which the outflow, and it never says which numbered
liquid boundary TELEMAC will call each stretch. Both are the MESHER's answers,
measured when the ``.cli`` was written from this geometry's own IPOBO, and the
steering author reads them here to state PRESCRIBED FLOWRATES and PRESCRIBED
ELEVATIONS in the order the solver will use.

The third answer is what each numbered boundary PRESCRIBES, read off the code
quad the ``.cli`` carries on it. The steering file writes its lists from that
rather than from a role-to-keyword table of its own, so it cannot state a level
at a boundary whose code never reads one.

The bundle rides beside the mesh objects and its uri lands on
``MeshArtifact.topology_uri``. It carries no geometry: the nodes, cells and bed
are the SELAFIN's, and duplicating them here would be a second mesh that could
disagree with the first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["FREE_EXIT_ROLE", "TOPOLOGY_FILENAME", "write_topology",
           "read_topology"]

#: Basename the bundle is written and staged under.
TOPOLOGY_FILENAME: str = "mesh_topology.json"

#: The role whose ``.cli`` quad prescribes NOTHING - the water leaves at whatever
#: level and velocity the interior brings to the face. A steering author reads
#: this name to tell a boundary that states no condition BY DESIGN from one whose
#: two files disagree; both read ``"nothing"`` and only one of them is a run.
#: The quad itself is the pair writer's table, which is the one decision; this
#: side of the mount carries the name that indexes it.
FREE_EXIT_ROLE: str = "free_exit"


def write_topology(rundir: Path | str, *, roles: Mapping[str, Sequence[int]],
                   liquid_boundary_order: Sequence[str],
                   liquid_boundary_prescribes: Sequence[str]) -> Path:
    """Write the accepted topology into ``rundir`` -> the path written."""
    path = Path(rundir) / TOPOLOGY_FILENAME
    path.write_text(json.dumps({
        "roles": {str(r): [int(n) for n in nodes] for r, nodes in roles.items()},
        "liquid_boundary_order": [str(r) for r in liquid_boundary_order],
        "liquid_boundary_prescribes": [
            str(p) for p in liquid_boundary_prescribes],
    }, indent=2), encoding="utf-8")
    return path


def read_topology(uri: str) -> dict[str, Any]:
    """Read a topology bundle from an ``s3://`` uri or a local path.

    A bundle that names no liquid boundary REFUSES: a mesh whose boundary carries
    no role is a mesh no reach steering file can be authored against, and
    returning empty sets would put the refusal downstream where the cause is no
    longer visible.

    A bundle that states no PRESCRIPTION per boundary refuses for the same
    reason. It was numbered by the superseded row-order rule, which disagrees
    with the engine's own numbering on any domain whose south-west corner falls
    on a liquid face, and a steering file authored against it prescribes into
    codes that never read it. There is no repair short of rebuilding the mesh.
    """
    if uri.startswith("s3://"):
        from trid3nt_server.tools.cache import read_object_bytes_s3
        raw = read_object_bytes_s3(uri).decode("utf-8")
    else:
        raw = Path(uri).read_text(encoding="utf-8")
    doc = json.loads(raw)
    roles = {str(r): [int(n) for n in nodes]
             for r, nodes in (doc.get("roles") or {}).items() if nodes}
    order = [str(r) for r in (doc.get("liquid_boundary_order") or [])]
    prescribes = [str(p) for p in (doc.get("liquid_boundary_prescribes") or [])]
    if not roles or not order:
        raise ValueError(
            f"the topology bundle at {uri} names {sorted(roles)} roles across "
            f"{len(order)} liquid boundaries; a steering file cannot be "
            "authored against a boundary with no roles on it")
    if len(prescribes) != len(order):
        raise ValueError(
            f"the topology bundle at {uri} states what {len(prescribes)} of its "
            f"{len(order)} liquid boundaries prescribe; it was numbered before "
            "the boundary numbering was measured by the engine's own rule, so "
            "rebuild the mesh rather than author a steering file against it")
    return {"roles": roles, "liquid_boundary_order": order,
            "liquid_boundary_prescribes": prescribes}
