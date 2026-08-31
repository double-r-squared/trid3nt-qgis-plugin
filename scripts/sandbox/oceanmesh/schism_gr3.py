"""tin_to_hgrid -- the coastal-TIN -> SCHISM hgrid.gr3 format bridge (SCHISM spike).

SCHISM consumes its native triangular mesh as ``hgrid.gr3`` (a simple ASCII
format: a node table with per-node depth, an element connectivity table, then
open/land boundary segment blocks). The oceanmesh ``coastal_tin`` worker
 already produces exactly the geometry SCHISM needs -- lon/lat nodes
(EPSG:4326) + triangle connectivity -- so this module is the thin translator
that lets a TRID3NT-meshed coastal domain feed SCHISM.

This is the SPIKE's mesh-supply proof: the FORMAT bridge, not a full simulation.
Depths here are a documented placeholder (a real run samples bathymetry via
fetch_dem at the landing); boundary classification is geometric (exterior loops,
one edge optionally flagged open) -- SCHISM's ipre grid check reads and validates
the topology, which is the acceptance bar. Pure-Python + numpy; no SCHISM/server
imports, so it imports flat from this directory and stays offline-suite-neutral.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

__all__ = [
    "tin_to_hgrid",
    "extract_boundary_loops",
    "signed_area_ccw",
    "remove_boundary_pinch_points",
]


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
    """Drop the triangles that make the mesh boundary non-manifold.

    SCHISM's grid check (``AQUIRE_HGRID``) rejects a "pinch"/bowtie boundary
    vertex -- a node whose element ball has more than one boundary opening
    (boundary degree > 2), which SCHISM flags as an "Illegal bnd node". Coastal
    TINs from oceanmesh can leave a few of these where two shoreline strands
    touch at a single node. We resolve each by deleting the smallest-area
    triangle incident to the pinch node (opening the bowtie), iterating until
    every boundary node is a simple degree-2 vertex. Only a handful of slivers
    are removed on a real coastal mesh. Returns the cleaned cells."""
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
    """Signed area (shoelace) per triangle in the node XY plane. Positive == CCW.

    SCHISM requires counter-clockwise element node ordering (positive area) so
    the finite-volume geometry is consistent. Computed in the raw node units
    (degrees for a lon/lat mesh) -- only the SIGN is load-bearing for orientation,
    and sign is invariant under the anisotropic lon/lat scaling."""
    tri = points[cells]  # (M, 3, 2)
    x = tri[:, :, 0]
    y = tri[:, :, 1]
    return 0.5 * (
        (x[:, 1] - x[:, 0]) * (y[:, 2] - y[:, 0])
        - (x[:, 2] - x[:, 0]) * (y[:, 1] - y[:, 0])
    )


def extract_boundary_loops(cells: np.ndarray) -> list[list[int]]:
    """Assemble the mesh boundary into ordered node loops (0-indexed).

    A boundary edge belongs to exactly one triangle. SCHISM requires EVERY
    boundary node to appear in a boundary segment (an unlisted boundary node
    fails its "incomplete ball" ring check), so completeness is load-bearing.
    A naive node-walk strands nodes at pinch points -- boundary nodes of degree
    4 where two loops touch (oceanmesh's cleanup can leave a few). We instead
    consume boundary EDGES exactly once (an Eulerian-circuit decomposition:
    every boundary node has even boundary degree, so greedy edge-following
    closes every loop and covers every edge/node). Returns loops sorted
    longest-first (the longest is the domain exterior / mainland)."""
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


def _contiguous_runs(loop: list[int], removed: set[int]) -> list[list[int]]:
    """The maximal runs of ``loop`` that avoid ``removed``, walked as a cycle.

    A boundary segment SCHISM reads is one continuous walk, so removing the open
    stretches from a loop yields SEVERAL land segments rather than one list that
    jumps across them.
    """
    if not loop:
        return []
    n = len(loop)
    start = next((i for i, node in enumerate(loop) if node in removed), None)
    if start is None:
        return [list(loop)]
    runs: list[list[int]] = []
    current: list[int] = []
    for offset in range(n):
        node = loop[(start + offset) % n]
        if node in removed:
            if current:
                runs.append(current)
                current = []
        else:
            current.append(node)
    if current:
        runs.append(current)
    return runs


def tin_to_hgrid(
    points: np.ndarray,
    cells: np.ndarray,
    *,
    depth: float | Sequence[float] | np.ndarray = 10.0,
    grid_name: str = "trid3nt_coastal_tin",
    open_boundary_side: str | None = None,
    open_sections: Sequence[Sequence[int]] | None = None,
    clean_boundary: bool = True,
) -> str:
    """Convert a coastal TIN (lon/lat nodes + triangle cells) to hgrid.gr3 text.

    ``points`` (N,2) are lon/lat (EPSG:4326). ``cells`` (M,3) are 0-indexed
    triangle node references (the oceanmesh ``coastal_tin`` output). ``depth`` is
    positive-down bathymetry -- a scalar placeholder (spike) or a per-node array
    (landing, sampled from fetch_dem). Element orientation is normalized to CCW
    (SCHISM's requirement); boundary loops are extracted and written as land
    segments.

    ``open_sections`` are node runs a mesher already identified as contiguous
    ocean stretches; each becomes one open-boundary segment, so NOPE equals the
    number of stretches the mesh actually has. Failing that,
    ``open_boundary_side`` ('south'|'north'|'east'|'west') cuts one segment from
    the exterior loop. Land segments are the runs BETWEEN the open stretches, so
    every written segment is a continuous walk. Returns the gr3 ASCII string.
    """
    points = np.asarray(points, dtype=float)
    cells = np.asarray(cells, dtype=np.int64)
    n_nodes = points.shape[0]
    n_elem = cells.shape[0]
    if points.shape[1] < 2:
        raise ValueError("points must be (N,>=2) lon/lat")
    if cells.shape[1] != 3:
        raise ValueError("cells must be (M,3) triangles")
    if cells.min() < 0 or cells.max() >= n_nodes:
        raise ValueError("cell node index out of range")

    depths_in = (
        np.full(n_nodes, float(depth))
        if np.isscalar(depth)
        else np.asarray(depth, dtype=float)
    )
    if depths_in.shape[0] != n_nodes:
        raise ValueError("per-node depth length != n_nodes")

    # SCHISM rejects non-manifold boundary vertices; open the bowties, then drop
    # any node the cleaning orphaned and re-index nodes/cells/depths contiguously.
    if clean_boundary:
        cells = remove_boundary_pinch_points(points, cells)
        # ALWAYS resync n_elem: pinch removal can drop cells WITHOUT orphaning any
        # node (a removed pinch triangle whose nodes survive on other cells), and a
        # stale n_elem then over-indexes cells[] in the element-table loop below.
        n_elem = cells.shape[0]
        used = np.unique(cells)
        if used.shape[0] != n_nodes:
            remap = np.full(n_nodes, -1, dtype=np.int64)
            remap[used] = np.arange(used.shape[0])
            points = points[used]
            depths_in = depths_in[used]
            cells = remap[cells]
            n_nodes = points.shape[0]
            n_elem = cells.shape[0]

    # normalize to CCW (positive signed area); flip the winding of CW triangles
    area = signed_area_ccw(points, cells)
    cw = area < 0
    if cw.any():
        cells = cells.copy()
        cells[cw] = cells[cw][:, [0, 2, 1]]

    depths = depths_in

    loops = extract_boundary_loops(cells)

    # SCHISM boundary convention: traverse so the water domain is on the LEFT --
    # the exterior loop counter-clockwise, island/hole loops clockwise. A loop's
    # signed area sign encodes its winding; reverse where it disagrees.
    def _loop_signed_area(loop: list[int]) -> float:
        xy = points[loop]
        x, y = xy[:, 0], xy[:, 1]
        return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))

    oriented: list[list[int]] = []
    for li, loop in enumerate(loops):
        a = _loop_signed_area(loop)
        want_ccw = li == 0  # exterior CCW, islands CW
        if (a > 0) != want_ccw:
            loop = [loop[0]] + loop[1:][::-1]
        oriented.append(loop)
    loops = oriented

    # optional open-boundary designation: the identified stretches, else the loop
    # nodes on the named side.
    open_nodes: list[int] = []
    sections: list[list[int]] = []
    if open_sections:
        sections = [[int(n) for n in seg] for seg in open_sections if len(seg)]
        open_nodes = [n for seg in sections for n in seg]
    elif open_boundary_side and loops:
        ext = loops[0]  # exterior loop
        lon = points[ext, 0]
        lat = points[ext, 1]
        if open_boundary_side == "south":
            thr = lat <= np.percentile(lat, 15)
        elif open_boundary_side == "north":
            thr = lat >= np.percentile(lat, 85)
        elif open_boundary_side == "west":
            thr = lon <= np.percentile(lon, 15)
        elif open_boundary_side == "east":
            thr = lon >= np.percentile(lon, 85)
        else:
            raise ValueError(f"bad open_boundary_side {open_boundary_side!r}")
        open_nodes = [ext[i] for i in np.where(thr)[0]]
        sections = [open_nodes] if open_nodes else []

    open_set = set(open_nodes)
    lines: list[str] = []
    lines.append(grid_name)
    lines.append(f"{n_elem} {n_nodes} ! # of elements and nodes")
    # node table (1-indexed): id lon lat depth
    for i in range(n_nodes):
        lines.append(
            f"{i + 1} {points[i, 0]:.9f} {points[i, 1]:.9f} {depths[i]:.6f}"
        )
    # element table (1-indexed nodes): id 3 n1 n2 n3
    for e in range(n_elem):
        a, b, c = cells[e] + 1
        lines.append(f"{e + 1} 3 {a} {b} {c}")

    # --- open boundary block ---------------------------------------------------
    if sections:
        lines.append(f"{len(sections)} = Number of open boundaries")
        lines.append(f"{len(open_nodes)} = Total number of open boundary nodes")
        for si, seg in enumerate(sections, start=1):
            lines.append(f"{len(seg)} = Number of nodes for open boundary {si}")
            lines.extend(str(n + 1) for n in seg)
    else:
        lines.append("0 = Number of open boundaries")
        lines.append("0 = Total number of open boundary nodes")

    # --- land boundary block (exterior + islands, between the open stretches) ---
    land_segments: list[tuple[list[int], int]] = []
    for li, loop in enumerate(loops):
        for seg in _contiguous_runs(loop, open_set):
            if len(seg) >= 2:
                # itype: 0 == mainland (exterior loop), 1 == island (interior loops)
                land_segments.append((seg, 0 if li == 0 else 1))
    total_land = sum(len(s) for s, _ in land_segments)
    lines.append(f"{len(land_segments)} = Number of land boundaries")
    lines.append(f"{total_land} = Total number of land boundary nodes")
    for si, (seg, itype) in enumerate(land_segments, start=1):
        lines.append(f"{len(seg)} {itype} = # nodes for land boundary {si}")
        lines.extend(str(n + 1) for n in seg)

    return "\n".join(lines) + "\n"
