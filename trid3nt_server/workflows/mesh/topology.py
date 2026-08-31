"""The accepted topology of a mesh - what its geometry file cannot state.

A SELAFIN says which nodes lie on a boundary; it never says which stretch of that
boundary is the inflow and which the outflow, and it never says which numbered
liquid boundary TELEMAC will call each stretch. Both are the MESHER's answers,
measured when the ``.cli`` was written from this geometry's own IPOBO, and a deck
author reads them here to state PRESCRIBED FLOWRATES and PRESCRIBED ELEVATIONS in
the order the solver will use.

The bundle rides beside the mesh objects and its uri lands on
``MeshArtifact.topology_uri``. It carries no geometry: the nodes, cells and bed
are the SELAFIN's, and duplicating them here would be a second mesh that could
disagree with the first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = ["TOPOLOGY_FILENAME", "match_boundary_roles", "write_topology",
           "read_topology"]

#: Basename the bundle is written and staged under.
TOPOLOGY_FILENAME: str = "mesh_topology.json"


def match_boundary_roles(points_utm: Any, contours: Sequence[Sequence[int]],
                         faces_utm: Mapping[str, Any], *,
                         tolerance_m: float) -> dict[str, list[int]]:
    """Which CONTIGUOUS run of the boundary each declared role names.

    -> ``{role: [node, ...]}``, each list a stretch of one contour in walk order.

    A TELEMAC liquid boundary IS a contiguous run of a contour - the solver
    numbers its boundaries by walking them - so a role is resolved as a RUN rather
    than as the nodes that happen to sit near a face. A declared TRANSECT names
    the run between the contour nodes nearest its two ends; a declared POINT names
    the run standing within ``tolerance_m`` of it. Either way the stretch has no
    holes, so the numbering counts exactly the boundaries that were declared.

    A role is named by the FACE the chain measured - the transect a section cut
    the domain square at - rather than by a node list somebody typed, because the
    nodes do not exist until the mesher has run. ``tolerance_m`` is measured off
    the mesh and gates the FACE, not its anchors: a triangulator conforms to a
    polygon within an edge along its sides and cuts its corners by more, so a
    tolerance on the two end anchors would reject the very reach whose middle the
    boundary follows exactly. A face no boundary node lies on at all comes back
    UNPLACED for the caller to refuse on - that one is a mesh and a face
    describing different domains.
    """
    import numpy as np
    from shapely.geometry import Point

    rings = [[int(n) for n in ring] for ring in contours if len(ring)]
    if not rings or not faces_utm:
        return {}
    pts = np.asarray(points_utm, dtype=float)

    def distance(geometry: Any, node: int) -> float:
        return float(geometry.distance(Point(pts[node, 0], pts[node, 1])))

    def nearest(geometry: Any, ring: Sequence[int]) -> tuple[int, float]:
        return min(((i, distance(geometry, node)) for i, node in enumerate(ring)),
                   key=lambda hit: hit[1])

    out: dict[str, list[int]] = {}
    for role, face in faces_utm.items():
        # WHICH contour carries the role: the one holding the node nearest the
        # whole face. A domain with an island has more than one, and a run that
        # jumped between them is a boundary no walk of the geometry produces.
        on, offset = min(((ring, nearest(face, ring)[1]) for ring in rings),
                         key=lambda hit: hit[1])
        if offset > float(tolerance_m):
            continue
        ends = list(getattr(face.boundary, "geoms", []))
        if len(ends) == 2:
            out[role] = _arc(on, nearest(ends[0], on)[0], nearest(ends[1], on)[0])
        else:
            out[role] = _window(on, nearest(face, on)[0],
                                lambda node: distance(face, node),
                                float(tolerance_m))
    return out


def _arc(ring: Sequence[int], start: int, end: int) -> list[int]:
    """The shorter of the two ways round ``ring`` from ``start`` to ``end``.

    A transect cuts one end off the domain, so the stretch it names is the short
    way between its two anchors; the long way is the rest of the boundary.
    """
    size = len(ring)
    forward = (end - start) % size
    if 2 * forward <= size:
        return [ring[(start + step) % size] for step in range(forward + 1)]
    return [ring[(start - step) % size] for step in range(size - forward + 1)]


def _window(ring: Sequence[int], index: int, offset: Any,
            tolerance_m: float) -> list[int]:
    """The run of ``ring`` about ``index`` that stays within ``tolerance_m``.

    Walking outward from the nearest node and STOPPING at the first node beyond
    the tolerance is what keeps a point-declared role one stretch: a node past a
    gap is on the far side of something, and a boundary with a hole in it numbers
    as two.
    """
    size = len(ring)
    sides: dict[int, list[int]] = {1: [], -1: []}
    for step in (1, -1):
        reach = step
        while (len(sides[1]) + len(sides[-1]) + 1 < size
               and offset(ring[(index + reach) % size]) <= tolerance_m):
            sides[step].append(ring[(index + reach) % size])
            reach += step
    return list(reversed(sides[-1])) + [ring[index]] + sides[1]


def write_topology(rundir: Path | str, *, roles: Mapping[str, Sequence[int]],
                   liquid_boundary_order: Sequence[str]) -> Path:
    """Write the accepted topology into ``rundir`` -> the path written."""
    path = Path(rundir) / TOPOLOGY_FILENAME
    path.write_text(json.dumps({
        "roles": {str(r): [int(n) for n in nodes] for r, nodes in roles.items()},
        "liquid_boundary_order": [str(r) for r in liquid_boundary_order],
    }, indent=2), encoding="utf-8")
    return path


def read_topology(uri: str) -> dict[str, Any]:
    """Read a topology bundle from an ``s3://`` uri or a local path.

    A bundle that names no liquid boundary REFUSES: a mesh whose boundary carries
    no role is a mesh no reach deck can be authored against, and returning empty
    sets would put the refusal downstream where the cause is no longer visible.
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
    if not roles or not order:
        raise ValueError(
            f"the topology bundle at {uri} names {sorted(roles)} roles across "
            f"{len(order)} liquid boundaries; a deck cannot be authored against "
            "a boundary with no roles on it")
    return {"roles": roles, "liquid_boundary_order": order}
