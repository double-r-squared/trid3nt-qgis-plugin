"""The boundary topology pass over a TIN - the one home of the mesh walk.

``points`` (N,2) lon/lat and ``cells`` (M,3) 0-indexed triangles in. Pinch
cleaning, orphan re-indexing, CCW normalization and the boundary loop walk
happen here and nowhere else, so every consumer of one mesh reads one
numbering and one winding. numpy only."""

from __future__ import annotations

import numpy as np

__all__ = [
    "boundary_numbering",
    "extract_boundary_loops",
    "signed_area_ccw",
    "remove_boundary_pinch_points",
    "clean_and_orient",
    "BoundaryPinched",
]


#: How many shared nodes a pinched boundary is named by before the rest counted.
_NAMED_PINCH_NODES = 12


class BoundaryPinched(ValueError):
    """The boundary walk found a node on two rings, so no IPOBO numbers it."""


def _boundary_degree(cells: np.ndarray) -> dict[int, int]:
    """Boundary-edge count per node (a boundary edge is used by one triangle)."""
    ec: dict[tuple[int, int], int] = {}
    for tri in cells:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            k = (int(a), int(b)) if a < b else (int(b), int(a))
            ec[k] = ec.get(k, 0) + 1
    deg: dict[int, int] = {}
    for (a, b), n in ec.items():
        if n == 1:
            deg[a] = deg.get(a, 0) + 1
            deg[b] = deg.get(b, 0) + 1
    return deg


def remove_boundary_pinch_points(
    points: np.ndarray, cells: np.ndarray, *, max_passes: int = 25
) -> np.ndarray:
    """Drop the triangles that make the mesh boundary non-manifold -> the cells.

    A boundary vertex of degree > 2 loses its smallest incident triangle."""
    # A mesh reader rejects a "pinch"/bowtie boundary vertex - a node whose
    # element ball has more than one boundary opening - and a coastal TIN can
    # leave a few where two shoreline strands touch at a single node. Deleting
    # the smallest-area incident triangle opens the bowtie; the pass iterates
    # until every boundary node is a simple degree-2 vertex.
    cells = np.asarray(cells, dtype=np.int64)
    for _ in range(max_passes):
        deg = _boundary_degree(cells)
        pinch = {n for n, d in deg.items() if d > 2}
        if not pinch:
            return cells
        tri = points[cells]
        area = np.abs(
            0.5
            * (
                (tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
                - (tri[:, 2, 0] - tri[:, 0, 0]) * (tri[:, 1, 1] - tri[:, 0, 1])
            )
        )
        drop: set[int] = set()
        for pn in pinch:
            incident = np.where((cells == pn).any(axis=1))[0]
            incident = [e for e in incident if e not in drop]
            if incident:
                drop.add(int(min(incident, key=lambda e: area[e])))
        keep = np.ones(cells.shape[0], dtype=bool)
        keep[list(drop)] = False
        cells = cells[keep]
    return cells


def signed_area_ccw(points: np.ndarray, cells: np.ndarray) -> np.ndarray:
    """Signed area (shoelace) per triangle in the node XY plane; positive == CCW.

    Only the SIGN is load-bearing, and it survives the lon/lat scaling."""
    tri = points[cells]  # (M, 3, 2)
    x = tri[:, :, 0]
    y = tri[:, :, 1]
    return 0.5 * (
        (x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0])
        - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0])
    )


def extract_boundary_loops(cells: np.ndarray) -> list[list[int]]:
    """Assemble the mesh boundary into ordered node loops (0-indexed).

    Longest loop first - the domain exterior; every boundary node appears."""
    # Boundary EDGES are consumed exactly once - an Eulerian-circuit
    # decomposition: every boundary node has even boundary degree, so greedy
    # edge-following closes every loop and covers every edge and node. A naive
    # node-walk strands nodes at pinch points, where two loops touch, and a
    # reader's ring check fails on a boundary node no segment lists.
    edge_count: dict[tuple[int, int], int] = {}
    for tri in cells:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (int(a), int(b)) if a < b else (int(b), int(a))
            edge_count[key] = edge_count.get(key, 0) + 1

    # unused boundary half-edges: node -> multiset of neighbor nodes
    adj: dict[int, list[int]] = {}
    for (a, b), n in edge_count.items():
        if n == 1:
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)

    def _pop_edge(u: int, v: int) -> None:
        adj[u].remove(v)
        adj[v].remove(u)

    loops: list[list[int]] = []
    starts = list(adj.keys())
    for s in starts:
        while adj.get(s):
            loop = [s]
            cur = s
            nxt = adj[cur][0]
            _pop_edge(cur, nxt)
            while nxt != s:
                loop.append(nxt)
                cur = nxt
                if not adj.get(cur):
                    break  # open chain (should not happen on a manifold bnd)
                nnl = adj[cur][0]
                _pop_edge(cur, nnl)
                nxt = nnl
            if len(loop) >= 3:
                loops.append(loop)
    loops.sort(key=len, reverse=True)
    return loops


def clean_and_orient(
    points: np.ndarray, cells: np.ndarray, depths: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pinch-clean, re-index, and CCW-normalize -- the shared topology pass."""
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    depths = np.asarray(depths, dtype=float)
    n_nodes = points.shape[0]

    cells = remove_boundary_pinch_points(points, cells)
    used = np.unique(cells)
    if used.shape[0] != n_nodes:
        remap = np.full(n_nodes, -1, dtype=np.int64)
        remap[used] = np.arange(used.shape[0])
        points = points[used]
        depths = depths[used]
        cells = remap[cells]

    area = signed_area_ccw(points, cells)
    cw = area < 0
    if cw.any():
        cells = cells.copy()
        cells[cw] = cells[cw][:, [0, 2, 1]]
    return points, cells, depths


def boundary_numbering(
    cells: np.ndarray, n_nodes: int
) -> tuple[np.ndarray, list[list[int]]]:
    """Number every boundary node along the loops -> ``(ipobo, loops)``.

    One count continues across loops, so IPOBO is a permutation of 1..NPTFR."""
    # TELEMAC's IPOBO is a permutation: one position per boundary node, counted
    # along the contours without restarting, and a per-loop count breaks it. A
    # node two loops both walk through would need two positions and gets the
    # second, which leaves the successor array longer than the boundary and the
    # numbering silently wrong, so that domain is refused here rather than
    # written.
    loops = extract_boundary_loops(np.asarray(cells, dtype=np.int64))
    seen: dict[int, int] = {}
    for loop in loops:
        for node in loop:
            seen[int(node)] = seen.get(int(node), 0) + 1
    shared = sorted(node for node, count in seen.items() if count > 1)
    if shared:
        rows = sum(len(loop) for loop in loops)
        named = ", ".join(str(n) for n in shared[:_NAMED_PINCH_NODES])
        more = ("" if len(shared) <= _NAMED_PINCH_NODES
                else f" (and {len(shared) - _NAMED_PINCH_NODES} more)")
        raise BoundaryPinched(
            f"the boundary walk returned {len(loops)} rings over {rows} rows but "
            f"only {len(seen)} distinct nodes: {len(shared)} node(s) - {named}"
            f"{more} - lie on more than one ring, so the domain is pinched to a "
            "point there and has no IPOBO permutation to be numbered by. It is a "
            "domain the domain cut into pieces that touch rather than a mesh "
            "defect: give it an outline whose narrows the asked edge can "
            "resolve, or narrow the domain to the water body the question is "
            "about.")
    ipobo = np.zeros(int(n_nodes), dtype=np.int64)
    position = 0
    for loop in loops:
        for node in loop:
            position += 1
            ipobo[int(node)] = position
    return ipobo, loops
