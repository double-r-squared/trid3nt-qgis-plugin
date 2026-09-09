"""The accepted topology of a mesh - what its geometry file cannot state.

Which stretch of a boundary is the inflow, which numbered liquid boundary the
engine will call each stretch, and what each of those prescribes. The bundle
carries no geometry: the nodes, cells and bed are the SELAFIN's."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["FREE_EXIT_ROLE", "RATING_CURVE_ROLE", "TOPOLOGY_FILENAME",
           "write_topology", "read_topology"]

#: Basename the bundle is written and staged under.
TOPOLOGY_FILENAME: str = "mesh_topology.json"

#: The role whose ``.cli`` quad prescribes NOTHING - the water leaves at whatever
#: level and velocity the interior brings to the face. A steering author reads
#: this name to tell a boundary that states no condition BY DESIGN from one whose
#: two files disagree; both read ``"nothing"`` and only one of them is a run.
FREE_EXIT_ROLE: str = "free_exit"

#: The role whose prescribed level is read off a STAGE-DISCHARGE CURVE rather
#: than written as a constant. Its quad is the outflow's - the engine consumes a
#: curve only where the depth is prescribed - so the quad alone cannot say where
#: the level comes from and the steering author reads this name to know it owes
#: the curve keywords at that boundary's number instead of an elevation.
RATING_CURVE_ROLE: str = "rating_curve"


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

    A bundle stating no prescription per boundary is unrepairable and refuses."""
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
    if len(prescribes) != len(order):
        raise ValueError(
            f"the topology bundle at {uri} states what {len(prescribes)} of its "
            f"{len(order)} liquid boundaries prescribe; it was numbered before "
            "the boundary numbering was measured by the engine's own rule, so "
            "rebuild the mesh rather than author a steering file against it")
    # A bundle naming NO liquid boundary is a recorded fact, not a gap: a closed
    # basin - a lake solved for its vertical structure - has no stretch of its
    # boundary the water crosses. ``states`` carries that sentence, so a reader
    # that needs a role refuses in its own words about the role it needed rather
    # than about an absent file.
    return {"roles": roles, "liquid_boundary_order": order,
            "liquid_boundary_prescribes": prescribes,
            "states": ("this domain names no liquid boundary; its whole boundary "
                       "is solid wall"
                       if not order else
                       f"{len(order)} liquid boundar"
                       f"{'y' if len(order) == 1 else 'ies'}, numbered "
                       f"{', '.join(f'{i}={r}' for i, r in enumerate(order, 1))}")}
